import hashlib,json,time
from fastapi import HTTPException
from src.config.settings import REDIS_URL
from src.models.source import Source,Policy
from src.models.sampling import SamplingJob,SamplingChunk
from src.services.audit import log
from src.services.uncertainty import build_basis,chunk_emissions,assemble_result,QUEUE_KEY
import redis

STALE_SECONDS=90
MAX_ATTEMPTS=5
MAX_FINALIZE_ATTEMPTS=3
FINALIZE_LEASE=120
FINALIZE_BACKOFF=(5,10,20)


def submit_sampling(db,user,body):
 policy=db.get(Policy,body.policy_id)
 if not policy:raise HTTPException(404,'口径版本不存在')
 sources=[x.data for x in db.query(Source).order_by(Source.id).all() if body.period=='全部' or x.data.get('period')==body.period]
 if not sources:raise HTTPException(422,'所选期间没有底稿')
 # 非法相关矩阵/参数在排队事务内拒绝，不产生作业
 try:
  basis=build_basis(sources,policy.data,body.means,body.stds,body.correlation,body.seed)
 except ValueError as exc:
  raise HTTPException(422,str(exc))
 if body.chunk_size>body.sample_count:raise HTTPException(422,'分块大小不能超过抽样次数')
 snapshot={'policy':policy.data,'sources':sources,'period':body.period,
  'sampling':{'means':body.means,'stds':body.stds,'correlation':body.correlation,'seed':body.seed,
   'sample_count':body.sample_count,'chunk_size':body.chunk_size,
   'activities':basis['activities'],'departments':basis['departments'],
   'log_params':basis['params'],'factor_units':{a:policy.data['factors'][a]['unit'] for a in basis['activities']}}}
 checksum=hashlib.sha256(json.dumps(snapshot,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
 job=SamplingJob(name=body.name,status='queued',snapshot=snapshot,checksum=checksum,
  sample_count=body.sample_count,chunk_size=body.chunk_size,seed=body.seed,progress=0,created=time.time())
 db.add(job);db.flush()
 start=0;seq=0
 while start<body.sample_count:
  size=min(body.chunk_size,body.sample_count-start)
  db.add(SamplingChunk(job_id=job.id,seq=seq,start=start,size=size,status='pending',attempts=0))
  start+=size;seq+=1
 log(db,user.email,'sampling.queue',body.name);db.commit()
 redis.Redis.from_url(REDIS_URL).rpush(QUEUE_KEY,job.id)
 return job


def _basis(job):
 s=job.snapshot['sampling']
 return build_basis(job.snapshot['sources'],job.snapshot['policy'],s['means'],s['stds'],s['correlation'],s['seed'])


def claim_chunk(db,worker_id,job_id=None):
 '''行级锁领取一个待办或僵块；已完成分块永不重领，天然去重。job_id 用于定向领取，缺省全局领取。'''
 now=time.time()
 q=db.query(SamplingChunk).filter(SamplingChunk.status!='done')
 if job_id is not None:q=q.filter(SamplingChunk.job_id==job_id)
 for chunk in q.order_by(SamplingChunk.id).with_for_update(skip_locked=True).all():
  if chunk.status=='pending' or (chunk.status=='running' and now-(chunk.claim_at or 0)>STALE_SECONDS):
   if chunk.attempts>=MAX_ATTEMPTS:
    job=db.get(SamplingJob,chunk.job_id)
    if job and job.status in ('queued','running','finalizing'):
     job.status='failed';job.error='分块重试超过上限：seq=%d'%chunk.seq;job.finished=now
    chunk.status='failed';db.commit();return None
   chunk.status='running';chunk.claim_by=worker_id;chunk.claim_at=now;chunk.attempts+=1
   job=db.get(SamplingJob,chunk.job_id)
   if job and job.status=='queued':job.status='running'
   db.commit();return chunk
 return None


def complete_chunk(db,chunk_id,worker_id,partial):
 '''栅栏提交：仅当前持有者可写；进度与分块结果同事务同成同败，迟到执行者不重复累计。
 每个分块在状态守卫下只可能完成一次，故进度 O(1) 增量累加即可，无需回扫全部分块。'''
 chunk=db.get(SamplingChunk,chunk_id,with_for_update=True)
 if not chunk or chunk.status=='done' or chunk.claim_by!=worker_id:return None  # 旧执行者晚到：幂等丢弃
 chunk.status='done';chunk.partial=partial;chunk.claim_by=None
 job=db.get(SamplingJob,chunk.job_id)
 job.progress=(job.progress or 0)+chunk.size
 remaining=db.query(SamplingChunk).filter(SamplingChunk.job_id==job.id,SamplingChunk.status!='done').count()
 if remaining==0 and job.status in ('queued','running'):job.status='finalizing'
 db.commit();return job


def execute_chunk(db,chunk,worker_id):
 job=db.get(SamplingJob,chunk.job_id)
 basis=_basis(job)
 _,total,dept=chunk_emissions(basis,chunk.start,chunk.size,job.sample_count)
 partial={'seq':chunk.seq,'start':chunk.start,'size':chunk.size,
  'sum_total':float(total.sum()),'min_total':float(total.min()),'max_total':float(total.max()),
  'sum_dept':[float(x) for x in dept.sum(axis=0)],
  'min_dept':[float(x) for x in dept.min(axis=0)],'max_dept':[float(x) for x in dept.max(axis=0)]}
 return complete_chunk(db,chunk.id,worker_id,partial)


def claim_finalizing(db,worker_id):
 '''领取一个“全部分块已完成、待最终化”的作业（queued 或 finalizing），行锁防多执行者并发；
 退避/租约期内跳过。领取即写入 FINALIZE_LEASE 租约，崩溃后由 sweep 在租约过期时接管。'''
 now=time.time()
 q=db.query(SamplingJob).filter(SamplingJob.status.in_(('queued','finalizing')))
 for job in q.order_by(SamplingJob.id).with_for_update(skip_locked=True).all():
  open_cnt=db.query(SamplingChunk).filter(SamplingChunk.job_id==job.id,SamplingChunk.status!='done').count()
  if open_cnt or (job.retry_after and job.retry_after>now):continue
  job.status='finalizing';job.retry_after=now+FINALIZE_LEASE;db.commit();return job
 return None


def finalize(db,job_id):
 '''全部块完成后按全局序号重放确定性流，产出精确区间；失败保留分块进度并退避后重新入队，可恢复。
 超过 MAX_FINALIZE_ATTEMPTS 仍失败则置 failed，避免坏数据无限重试。'''
 job=db.get(SamplingJob,job_id)
 if not job or job.status!='finalizing':return job
 pending=db.query(SamplingChunk).filter(SamplingChunk.job_id==job_id,SamplingChunk.status!='done').count()
 if pending:
  job.status='running';job.retry_after=None;db.commit();redis.Redis.from_url(REDIS_URL).rpush(QUEUE_KEY,job_id);return job
 try:
  basis=_basis(job)
  job.result=assemble_result(basis,job);job.status='completed';job.finished=time.time();job.error=None;job.retry_after=None
  job.finalize_attempts=(job.finalize_attempts or 0)+1
  db.commit()
 except Exception as exc:
  db.rollback();job=db.get(SamplingJob,job_id)
  # 计数在回滚之后自增并随恢复事务一起提交，避免被 rollback 一并撤销
  attempts=(job.finalize_attempts or 0)+1;job.finalize_attempts=attempts
  if attempts>=MAX_FINALIZE_ATTEMPTS:
   job.status='failed';job.error='最终化连续失败：%s'%exc;job.finished=time.time();db.commit()
  else:
   job.status='queued';job.error='最终化失败，将退避重试：%s'%exc
   job.retry_after=time.time()+FINALIZE_BACKOFF[min(attempts-1,len(FINALIZE_BACKOFF)-1)]
   db.commit();redis.Redis.from_url(REDIS_URL).rpush(QUEUE_KEY,job_id)
  raise
 return job


def sweep(db):
 '''兜底回收：把“全部分块已完成却停在 queued/running/finalizing”的作业推进到可最终化状态并唤醒。
 queued 的 retry_after 是失败退避（未到期不抢）；finalizing 的 retry_after 是最终化租约（过期即视为执行者崩溃，清租约后接管）。'''
 now=time.time()
 for job in db.query(SamplingJob).filter(SamplingJob.status.in_(('queued','running','finalizing'))).all():
  open_cnt=db.query(SamplingChunk).filter(SamplingChunk.job_id==job.id,SamplingChunk.status!='done').count()
  if open_cnt:continue
  if job.status=='finalizing' and job.retry_after and job.retry_after<=now:
   job.retry_after=None  # 租约过期：执行者可能已崩溃，允许重新领取
  if job.retry_after and job.retry_after>now:continue  # 退避或有效租约期内
  job.status='finalizing';db.commit();redis.Redis.from_url(REDIS_URL).rpush(QUEUE_KEY,job.id)
