import os,sys,types,math,json,time
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 在导入应用设置前准备隔离环境：项目配置走临时文件，数据库用内存 SQLite，Redis 用内存假实现
TMP_PROJECT='/tmp/sampling-test-project.json'
with open(TMP_PROJECT,'w') as f:json.dump({'fields':{'period':'月份','department':'研究部门','activity':'活动类型','quantity':'活动量','unit':'单位'}},f)
os.environ['PROJECT_JSON']=TMP_PROJECT
os.environ['DATABASE_URL']='sqlite+pysqlite:///:memory:'
os.environ['JWT_SECRET']='test-secret'

import redis as _redis
class FakeRedis:
 def __init__(self):self.q=[]
 def rpush(self,k,v):self.q.append(int(v));return len(self.q)
 def lpop(self,k):return str(self.q.pop(0)).encode() if self.q else None
 def ping(self):return True
_redis.Redis.from_url=lambda url:FakeRedis()

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import src.config.database as database
test_engine=create_engine('sqlite+pysqlite:///:memory:',connect_args={'check_same_thread':False},
 poolclass=StaticPool)
database.engine=test_engine;database.Session=sessionmaker(bind=test_engine,expire_on_commit=False)
from src.config.database import Base,Session
from src.models import identity,source,run,sampling
from src.models.source import Source,Policy
from src.models.sampling import SamplingJob,SamplingChunk
from src.services import uncertainty as U
from src.services import sampling as S

POLICY={'version':'因子-v1','scope':'合成研究用活动量；仅覆盖电力、柴油、通勤，不是组织完整碳盘查',
 'factors':{'电力':{'unit':'kWh','kg_per_unit':0.57},'柴油':{'unit':'L','kg_per_unit':2.68},'通勤':{'unit':'km','kg_per_unit':0.12}}}
SOURCES=[
 {'period':'2026-11','department':'材料试验室','activity':'电力','quantity':1.5,'unit':'MWh'},
 {'period':'2026-11','department':'工艺试验室','activity':'电力','quantity':800,'unit':'kWh'},
 {'period':'2026-11','department':'材料试验室','activity':'柴油','quantity':120,'unit':'L'},
 {'period':'2026-11','department':'光学试验室','activity':'通勤','quantity':300,'unit':'km'},
 {'period':'2026-11','department':'工艺试验室','activity':'通勤','quantity':150,'unit':'km'}]
MEANS={'电力':0.57,'柴油':2.68,'通勤':0.12}
STDS={'电力':0.06,'柴油':0.3,'通勤':0.02}
CORR=[[1.0,0.4,0.2],[0.4,1.0,-0.3],[0.2,-0.3,1.0]]


def basis(seed=42):return U.build_basis(SOURCES,POLICY,MEANS,STDS,CORR,seed)


def test_rng_determinism():
 b=basis()
 f1,_,_=U.chunk_emissions(b,0,500,1000)
 f2,_,_=U.chunk_emissions(b,0,500,1000)
 assert np.array_equal(f1,f2),'同种子同序号必须逐样本一致'
 # 分块切分不影响每个全局序号的样本
 fa,_,_=U.chunk_emissions(b,0,1000,1000)
 fb1,_,_=U.chunk_emissions(b,0,300,1000);fb2,_,_=U.chunk_emissions(b,300,700,1000)
 assert np.allclose(fa,np.vstack((fb1,fb2))),'分块边界不得改变样本序列'
 print('test_rng_determinism OK')


def test_chunk_size_independence():
 b=basis();n=6000
 r1=U.streaming_quantiles(b,n,777)
 r2=U.streaming_quantiles(b,n,2000)
 r3=U.streaming_quantiles(b,n,6000)
 assert np.allclose(r1,r2,rtol=1e-12,atol=1e-12),'区间必须与分块大小无关'
 assert np.allclose(r2,r3,rtol=1e-12,atol=1e-12)
 print('test_chunk_size_independence OK')


def test_exact_quantile_against_brute_force():
 b=basis();n=2000
 q=U.streaming_quantiles(b,n,257)
 # 暴力：一次性物化全体样本排序（仅测试用，生产路径不允许随 n 线性占内存）
 totals=[];depts=[[] for _ in b['departments']]
 for st in range(0,n,333):
  size=min(333,n-st);_,total,dept=U.chunk_emissions(b,st,size,n)
  totals.extend(total.tolist())
  for j in range(len(depts)):depts[j].extend(dept[:,j].tolist())
 def nr(vals,p):
  vals=sorted(vals);k=max(1,math.ceil(p*n));return vals[k-1]
 assert math.isclose(q[0,0],nr(totals,0.025),rel_tol=1e-12,abs_tol=1e-12)
 assert math.isclose(q[0,1],nr(totals,0.5),rel_tol=1e-12,abs_tol=1e-12)
 assert math.isclose(q[0,2],nr(totals,0.975),rel_tol=1e-12,abs_tol=1e-12)
 for j in range(len(depts)):
  assert math.isclose(q[1+j,0],nr(depts[j],0.025),rel_tol=1e-12,abs_tol=1e-12)
  assert math.isclose(q[1+j,2],nr(depts[j],0.975),rel_tol=1e-12,abs_tol=1e-12)
 print('test_exact_quantile_against_brute_force OK')


def test_lognormal_moments():
 b=basis();n=120000;b={**b,'total_n':n}
 factors=U.draw_factors(b,0,n)
 for j,a in enumerate(b['activities']):
  sample_mean=factors[:,j].mean();sample_sd=factors[:,j].std(ddof=1)
  mu,sigma=b['params'][j];exp_mean=math.exp(mu+sigma**2/2);exp_sd=exp_mean*math.sqrt(math.exp(sigma**2)-1)
  assert abs(sample_mean-exp_mean)/exp_mean<0.03,(a,sample_mean,exp_mean)
  assert abs(sample_sd-exp_sd)/exp_sd<0.05,(a,sample_sd,exp_sd)
 # 相关性：经验相关系数接近输入矩阵
 emp=np.corrcoef(np.log(factors).T)
 chol=U.validate_correlation(b['activities'],CORR)
 assert np.max(np.abs(emp-np.array(CORR)))<0.05
 print('test_lognormal_moments OK')


def test_invalid_correlation_rejected():
 bad_cases=[
  [[1.0,0.9],[0.9,1.0],[0.0,0.0]],                       # 阶数不符
  [[1.0,2.0],[2.0,1.0]],                                # 越界
  [[1.0,0.5],[0.6,1.0]],                                # 不对称
  [[1.0,0.5,0.4,0.2],[0.5,1.0,0.9,0.1],[0.4,0.9,1.0,0.3],[0.2,0.1,0.3,1.0]],  # 阶数不符（4阶）
  [[1.0,0.99,0.99],[0.99,1.0,0.1],[0.99,0.1,1.0]],      # 非正定
  [[2.0,0.0,0.0],[0.0,1.0,0.0],[0.0,0.0,1.0]]]          # 对角非1
 for bad in bad_cases:
  try:
   U.validate_correlation(['电力','柴油','通勤'],bad);raise AssertionError('应拒绝非法矩阵: %s'%bad)
  except ValueError:pass
 print('test_invalid_correlation_rejected OK')


def test_memory_not_linear_in_n():
 import tracemalloc
 b=basis()
 tracemalloc.start()
 U.streaming_quantiles(b,20000,5000);_,peak_small=tracemalloc.get_traced_memory();tracemalloc.stop()
 tracemalloc.start()
 U.streaming_quantiles(b,200000,5000);_,peak_big=tracemalloc.get_traced_memory();tracemalloc.stop()
 ratio=peak_big/peak_small
 assert ratio<1.6,'10倍抽样次数内存仅可微增，实际峰值比 %.2f'%ratio
 print('test_memory_not_linear_in_n OK (peak ratio %.2f)'%ratio)


def _seed_db():
 Base.metadata.create_all(test_engine)
 with Session() as db:
  if not db.query(Policy).filter_by(version='因子-v1').first():db.add(Policy(version='因子-v1',data=POLICY))
  for i,s in enumerate(SOURCES):
   if not db.query(Source).filter_by(natural_key='t-%d'%i).first():db.add(Source(natural_key='t-%d'%i,data=s))
  db.commit()


class _User:email='analyst@demo.local'
class _Body:
 def __init__(self,**kw):
  self.name='不确定性抽样';self.policy_id=1;self.period='全部'
  self.sample_count=3000;self.chunk_size=1000;self.seed=42
  self.means=MEANS;self.stds=STDS;self.correlation=CORR
  for k,v in kw.items():setattr(self,k,v)


def test_queue_rejects_invalid_matrix():
 _seed_db()
 from fastapi import HTTPException
 with Session() as db:
  bad=[[1.0,0.99,0.99],[0.99,1.0,0.1],[0.99,0.1,1.0]]
  try:S.submit_sampling(db,_User(),_Body(correlation=bad));raise AssertionError('非法矩阵应在排队时拒绝')
  except HTTPException as e:assert e.status_code==422
  assert db.query(SamplingJob).count()==0,'拒绝时不得留下作业'
 print('test_queue_rejects_invalid_matrix OK')


def test_full_job_resume_and_idempotency():
 _seed_db()
 with Session() as db:job=S.submit_sampling(db,_User(),_Body())
 jid=job.id
 assert db.query(SamplingChunk).filter_by(job_id=jid).count()==3
 # 模拟两个执行者交错领取，第二个重复领取同一块必须失败
 with Session() as db:
  c1=S.claim_chunk(db,'w1');assert c1.seq==0
  c1b=S.claim_chunk(db,'w2')  # 跳过锁定/已领取，应拿到 seq=1
  assert c1b.seq==1
  S.execute_chunk(db,c1,'w1');S.execute_chunk(db,c1b,'w2')
 # 旧执行者 w1 迟到重交 seq=0：必须被幂等丢弃，进度不重复
 with Session() as db:
  stale=db.get(SamplingChunk,c1.id)
  out=S.complete_chunk(db,stale.id,'w1-late',stale.partial)
  assert out is None
  job=db.get(SamplingJob,jid);assert job.progress==2000,job.progress
 # 中断后新执行者领取最后一块并完成，自动进入 finalizing
 with Session() as db:
  c2=S.claim_chunk(db,'w3');assert c2.seq==2
  j=S.execute_chunk(db,c2,'w3');assert j.status=='finalizing' and j.progress==3000
 with Session() as db:
  j=S.finalize(db,jid);assert j.status=='completed'
  res=j.result
  assert res['summary']['抽样次数']==3000
  assert len(res['intervals'])==1+3
  for row_ in res['intervals']:
   assert row_['P2.5下限kgCO2e']<=row_['P50中位数kgCO2e']<=row_['P97.5上限kgCO2e']
  # 完成后再收到队列消息：无副作用
  assert S.claim_chunk(db,'w4') is None
 print('test_full_job_resume_and_idempotency OK')


def test_stale_claim_recovery():
 _seed_db()
 with Session() as db:job=S.submit_sampling(db,_User(),_Body(sample_count=1500,chunk_size=500))
 jid=job.id
 with Session() as db:
  c=S.claim_chunk(db,'crashed');assert c.seq==0
 # 不提交完成，模拟执行者崩溃；超过 STALE_SECONDS 后被别人接走
 with Session() as db:
  ch=db.get(SamplingChunk,c.id);ch.claim_at=time.time()-S.STALE_SECONDS-5;db.commit()
 with Session() as db:
  again=S.claim_chunk(db,'replacement');assert again.id==c.id and again.attempts==2
 # 旧持有者迟到提交被拒
 with Session() as db:
  assert S.complete_chunk(db,c.id,'crashed',{'x':1}) is None
  partial={'seq':0,'start':0,'size':500,'sum_total':1.0,'min_total':1.0,'max_total':1.0,'sum_dept':[0.0]*3,'min_dept':[0.0]*3,'max_dept':[0.0]*3}
  assert S.complete_chunk(db,c.id,'replacement',partial) is not None
 print('test_stale_claim_recovery OK')


def test_snapshot_frozen_and_checksum():
 _seed_db()
 with Session() as db:
  job=S.submit_sampling(db,_User(),_Body())
  snap=job.snapshot
  assert snap['sampling']['means']==MEANS and snap['sampling']['correlation']==CORR
  assert len(job.checksum)==64
  # 改底稿/口径不影响已冻结快照
  src=db.query(Source).first();src.data={**src.data,'quantity':999};db.commit()
  fresh=db.get(SamplingJob,job.id)
  assert fresh.snapshot['sources'][0]['quantity']!=999
 print('test_snapshot_frozen_and_checksum OK')


def test_max_attempts_fails_job():
 _seed_db()
 with Session() as db:job=S.submit_sampling(db,_User(),_Body(sample_count=500,chunk_size=500))
 jid=job.id
 # 每次领取后令其僵死，再由下一个执行者接走；attempts 从 1 累加到 MAX_ATTEMPTS
 for attempt in range(1,S.MAX_ATTEMPTS+1):
  with Session() as db:
   got=S.claim_chunk(db,'w%d'%attempt,jid)
   assert got is not None and got.attempts==attempt,(attempt,got.attempts if got else None)
   ch=db.get(SamplingChunk,got.id);ch.claim_at=time.time()-S.STALE_SECONDS-5;db.commit()
 # 下次领取：attempts 已达上限，作业与分块置 failed，领取返回 None
 with Session() as db:
  assert S.claim_chunk(db,'w-final',jid) is None
 with Session() as db:
  job=db.get(SamplingJob,jid)
  ch=db.query(SamplingChunk).filter_by(job_id=jid,seq=0).first()
  assert job.status=='failed' and '重试超过上限' in job.error
  assert ch.status=='failed'
 print('test_max_attempts_fails_job OK')


def test_finalize_failure_requeues():
 _seed_db()
 with Session() as db:job=S.submit_sampling(db,_User(),_Body(sample_count=1000,chunk_size=1000))
 jid=job.id
 with Session() as db:
  chunk=S.claim_chunk(db,'w1',jid);j=S.execute_chunk(db,chunk,'w1');assert j.status=='finalizing'
 # 让最终化抛错：临时破坏作业的相关矩阵（整体重赋 snapshot 以确保 JSON 变更落库）
 with Session() as db:
  j=db.get(SamplingJob,jid);bad={**j.snapshot,'sampling':{**j.snapshot['sampling'],'correlation':[[1.0,2.0],[2.0,1.0]]}}
  j.snapshot=bad;db.commit()
  try:S.finalize(db,jid);raise AssertionError('最终化应抛错')
  except Exception:pass
  j=db.get(SamplingJob,jid)
  assert j.status=='queued' and j.finalize_attempts==1 and j.retry_after>time.time()  # 退避后重新入队
  assert db.query(SamplingChunk).filter_by(job_id=jid,status='done').count()==1
 # 退避期内 claim_finalizing 跳过；清除退避、恢复矩阵后可正常完成
 with Session() as db:
  assert S.claim_finalizing(db,'w2') is None or S.claim_finalizing(db,'w2').id!=jid
  j=db.get(SamplingJob,jid)
  good={**j.snapshot,'sampling':{**j.snapshot['sampling'],'correlation':CORR}}
  j.snapshot=good;j.retry_after=None;db.commit()
  claimed=S.claim_finalizing(db,'w2');assert claimed.id==jid
  j=S.finalize(db,jid);assert j.status=='completed' and j.finalize_attempts==2
 print('test_finalize_failure_requeues OK')


def test_finalize_exhausts_and_fails():
 _seed_db()
 with Session() as db:job=S.submit_sampling(db,_User(),_Body(sample_count=1000,chunk_size=1000));jid=job.id
 with Session() as db:
  chunk=S.claim_chunk(db,'w1',jid);S.execute_chunk(db,chunk,'w1')
 # 持续令最终化失败，达到上限后作业 failed，不再无限重试
 with Session() as db:
  j=db.get(SamplingJob,jid)
  j.snapshot={**j.snapshot,'sampling':{**j.snapshot['sampling'],'correlation':[[1.0,2.0],[2.0,1.0]]}};db.commit()
 for attempt in range(1,S.MAX_FINALIZE_ATTEMPTS+1):
  with Session() as db:
   j=db.get(SamplingJob,jid);j.status='finalizing';j.retry_after=None;db.commit()
   try:S.finalize(db,jid)
   except Exception:pass
 with Session() as db:
  j=db.get(SamplingJob,jid)
  assert j.status=='failed' and j.finalize_attempts==S.MAX_FINALIZE_ATTEMPTS and '连续失败' in j.error
 print('test_finalize_exhausts_and_fails OK')


def test_finalize_lease_mutual_exclusion():
 _seed_db()
 with Session() as db:job=S.submit_sampling(db,_User(),_Body(sample_count=1000,chunk_size=1000));jid=job.id
 with Session() as db:
  ch=S.claim_chunk(db,'w1',jid);S.execute_chunk(db,ch,'w1')
 # 第一个执行者领取最终化，写入租约；租约期内其他执行者不可重复领取
 with Session() as db:
  first=S.claim_finalizing(db,'worker-A');assert first.id==jid
 with Session() as db:
  assert S.claim_finalizing(db,'worker-B') is None  # 租约互斥
 # 租约未过期时 sweep 不重复唤醒
 with Session() as db:
  before=db.get(SamplingJob,jid).retry_after;S.sweep(db);assert db.get(SamplingJob,jid).retry_after==before
 # 模拟执行者崩溃：租约过期后 sweep 清租约，worker-B 可接管
 with Session() as db:
  j=db.get(SamplingJob,jid);j.retry_after=time.time()-1;db.commit()
  S.sweep(db);assert db.get(SamplingJob,jid).retry_after is None
  taken=S.claim_finalizing(db,'worker-B');assert taken.id==jid
 with Session() as db:
  j=S.finalize(db,jid);assert j.status=='completed' and j.retry_after is None  # 成功后清租约
 print('test_finalize_lease_mutual_exclusion OK')


def test_worker_drain_end_to_end():
 '''模拟 worker 主循环：仅靠 claim_chunk/claim_finalizing 即可把作业从零推进到完成，含最终化闭环。'''
 _seed_db()
 with Session() as db:job=S.submit_sampling(db,_User(),_Body(sample_count=2500,chunk_size=1000));jid=job.id
 for _ in range(20):
  with Session() as db:chunk=S.claim_chunk(db,'worker-e2e',jid)
  if chunk is not None:
   with Session() as db:S.execute_chunk(db,chunk,'worker-e2e');continue
  with Session() as db:fj=S.claim_finalizing(db,'worker-e2e')
  if fj is not None:
   with Session() as db:S.finalize(db,fj.id)
  with Session() as db:
   if db.get(SamplingJob,jid).status=='completed':break
 with Session() as db:
  j=db.get(SamplingJob,jid);assert j.status=='completed' and j.progress==2500
  assert j.result and len(j.result['intervals'])==1+3
 print('test_worker_drain_end_to_end OK')


if __name__=='__main__':
 test_rng_determinism()
 test_chunk_size_independence()
 test_exact_quantile_against_brute_force()
 test_lognormal_moments()
 test_invalid_correlation_rejected()
 test_memory_not_linear_in_n()
 test_queue_rejects_invalid_matrix()
 test_full_job_resume_and_idempotency()
 test_stale_claim_recovery()
 test_snapshot_frozen_and_checksum()
 test_max_attempts_fails_job()
 test_finalize_failure_requeues()
 test_finalize_exhausts_and_fails()
 test_finalize_lease_mutual_exclusion()
 test_worker_drain_end_to_end()
 print('\n全部抽样测试通过')
