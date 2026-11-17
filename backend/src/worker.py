import time,traceback,uuid
from pathlib import Path
import redis
from src.config.database import Base,engine,Session
from src.config.settings import REDIS_URL
from src.models import identity,source,run,sampling
from src.models.run import Run
from src.services.calculation import calculate
from src.services.delivery import tick
from src.services import sampling as sampling_service
from src.services.uncertainty import QUEUE_KEY
Base.metadata.create_all(engine)
r=redis.Redis.from_url(REDIS_URL)
WORKER_ID='worker-'+uuid.uuid4().hex[:12]


def run_report(item):
 with Session() as db:
  job=db.get(Run,int(item))
  if job and job.status=='queued':
   job.status='running';db.commit()
   try:job.result=calculate(job.snapshot['sources'],job.snapshot['policy']);job.status='completed'
   except Exception as exc:job.status='failed';job.error=str(exc);traceback.print_exc()
   db.commit()


def drain_sampling():
 '''数据库驱动：先排空可领取分块，再排空待最终化作业。消息只作唤醒，领取/去重/回收都在行锁事务内完成。'''
 while True:
  with Session() as db:chunk=sampling_service.claim_chunk(db,WORKER_ID)
  if chunk is None:break
  try:
   with Session() as db:sampling_service.execute_chunk(db,chunk,WORKER_ID)
  except Exception:
   traceback.print_exc();return  # 本块保持 running，超过僵块阈值后由其他执行者接走重试
 # 分块全部完成的作业进入最终化；同样可被本/其他执行者恢复
 while True:
  with Session() as db:job=sampling_service.claim_finalizing(db,WORKER_ID)
  if job is None:return
  try:
   with Session() as db:sampling_service.finalize(db,job.id)
  except Exception:traceback.print_exc()


while True:
 Path('/tmp/worker-heartbeat').write_text(str(time.time()))
 try:
  item=r.lpop('report-jobs')
  if item:run_report(item)
  r.lpop(QUEUE_KEY)  # 仅唤醒；真正的领取依据是数据库，消息丢失也不影响恢复
  drain_sampling()
  with Session() as db:
   sampling_service.sweep(db);tick(db)
 except Exception:traceback.print_exc()
 time.sleep(1)
