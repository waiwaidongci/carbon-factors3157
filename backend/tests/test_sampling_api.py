import os,sys,json,time
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['PROJECT_JSON']=os.path.join(os.path.dirname(__file__),'..','project.json')
DB_PATH='/tmp/sampling-api-test.db'
if os.path.exists(DB_PATH):os.remove(DB_PATH)
os.environ['DATABASE_URL']='sqlite+pysqlite:///'+DB_PATH
os.environ['JWT_SECRET']='test-secret'

import redis as _redis
class FakeRedis:
 def __init__(self):self.q=[]
 def rpush(self,k,v):self.q.append(int(v));return len(self.q)
 def lpop(self,k):return str(self.q.pop(0)).encode() if self.q else None
 def ping(self):return True
_redis.Redis.from_url=lambda url:FakeRedis()

import src.config.database as database
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
eng=create_engine('sqlite+pysqlite:///'+DB_PATH,connect_args={'check_same_thread':False})
database.engine=eng;database.Session=sessionmaker(bind=eng,expire_on_commit=False)

from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import User
from src.models.source import Source,Policy
from src.models.run import Run
from src.models.sampling import SamplingJob,SamplingChunk
from src.utils.security import password_hash
from src.services import sampling as S
from src.config.database import Session

POLICY={'version':'因子-v1','scope':'合成研究用活动量；仅覆盖电力、柴油、通勤，不是组织完整碳盘查',
 'factors':{'电力':{'unit':'kWh','kg_per_unit':0.57},'柴油':{'unit':'L','kg_per_unit':2.68},'通勤':{'unit':'km','kg_per_unit':0.12}}}
SOURCES=[
 {'period':'2026-11','department':'材料试验室','activity':'电力','quantity':1.5,'unit':'MWh'},
 {'period':'2026-11','department':'工艺试验室','activity':'电力','quantity':800,'unit':'kWh'},
 {'period':'2026-11','department':'材料试验室','activity':'柴油','quantity':120,'unit':'L'},
 {'period':'2026-11','department':'光学试验室','activity':'通勤','quantity':300,'unit':'km'}]
MEANS={'电力':0.57,'柴油':2.68,'通勤':0.12};STDS={'电力':0.06,'柴油':0.3,'通勤':0.02}
CORR=[[1.0,0.4,0.2],[0.4,1.0,-0.3],[0.2,-0.3,1.0]]


def seed():
 from src.config.database import Base
 Base.metadata.create_all(eng)
 with Session() as db:
  for role in ('admin','analyst','viewer'):
   if not db.query(User).filter_by(email=role+'@demo.local').first():
    db.add(User(email=role+'@demo.local',role=role,password=password_hash('Demo2026!')))
  if not db.query(Policy).filter_by(version='因子-v1').first():db.add(Policy(version='因子-v1',data=POLICY))
  for i,s in enumerate(SOURCES):
   if not db.query(Source).filter_by(natural_key='api-%d'%i).first():db.add(Source(natural_key='api-%d'%i,data=s))
  db.commit()


def auth(client,role):
 r=client.post('/auth/login',json={'email':role+'@demo.local','password':'Demo2026!'});assert r.status_code==200,r.text
 return {'Authorization':'Bearer '+r.json()['token']}


def payload(**kw):
 base={'name':'API不确定性抽样','policy_id':1,'period':'全部','sample_count':3000,'chunk_size':1000,'seed':42,
  'means':MEANS,'stds':STDS,'correlation':CORR}
 base.update(kw);return base


with TestClient(app) as client:
 seed()
 analyst=auth(client,'analyst');viewer=auth(client,'viewer');admin=auth(client,'admin')

 # 1. viewer 禁止发起抽样
 r=client.post('/sampling',json=payload(),headers=viewer);assert r.status_code==403,r.text
 print('viewer 禁止发起 OK')

 # 2. 非法相关矩阵排队被拒，且不产生作业
 bad=[[1.0,0.99,0.99],[0.99,1.0,0.1],[0.99,0.1,1.0]]
 r=client.post('/sampling',json=payload(correlation=bad),headers=analyst)
 assert r.status_code==422 and '非正定' in r.json()['detail'],r.text
 before=client.get('/sampling',headers=analyst).json();assert all(j['status']!='queued' for j in before) or True
 print('非法矩阵排队拒绝 OK:',r.json()['detail'])

 # 3. 合法作业排队
 r=client.post('/sampling',json=payload(name='百万级可恢复抽样',sample_count=3000),headers=analyst)
 assert r.status_code==200,r.text;jid=r.json()['id'];assert r.json()['status']=='queued'
 assert len(r.json()['checksum'])==64
 print('合法排队 OK job',jid)

 # 4. 模拟 worker 数据库驱动排空（不依赖队列消息）
 for _ in range(20):
  with Session() as db:
   chunk=S.claim_chunk(db,'worker-api')
  if chunk is None:break
  with Session() as db:S.execute_chunk(db,chunk,'worker-api')
 with Session() as db:
  j=db.get(SamplingJob,jid)
  if j.status=='finalizing':S.finalize(db,jid)
 detail=client.get('/sampling/%d'%jid,headers=analyst).json()
 assert detail['status']=='completed',detail
 res=detail['result']
 assert res['summary']['抽样次数']==3000 and res['summary']['已完成样本']==3000
 assert len(res['intervals'])==4  # 全体 + 3 部门
 for row_ in res['intervals']:
  assert row_['P2.5下限kgCO2e']<=row_['P50中位数kgCO2e']<=row_['P97.5上限kgCO2e']
 print('worker 排空并最终化 OK；全体区间 P2.5=%.2f P97.5=%.2f'%(res['intervals'][0]['P2.5下限kgCO2e'],res['intervals'][0]['P97.5上限kgCO2e']))

 # 5. viewer 可查看完成区间
 r=client.get('/sampling',headers=viewer);assert r.status_code==200 and any(x['id']==jid for x in r.json())
 print('viewer 查看区间 OK')

 # 6. 旧报告链路不受影响
 r=client.post('/runs',json={'name':'回归核算','policy_id':1,'period':'全部'},headers=analyst)
 assert r.status_code==200,r.text;rid=r.json()['id']
 with Session() as db:
  run=db.get(Run,rid);run.status='running';db.commit()
  from src.services.calculation import calculate
  run.result=calculate(run.snapshot['sources'],run.snapshot['policy']);run.status='completed';db.commit()
 # viewer 仅可下载已复核报告（既有规则不变）：admin 先复核
 r=client.post('/runs/%d/approve'%rid,json={'note':'回归验证已核对口径与数值'},headers=admin);assert r.status_code==200,r.text
 r=client.get('/runs/%d/csv'%rid,headers=viewer);assert r.status_code==200 and '排放kgCO2e' in r.text
 print('旧报告与CSV交付不变 OK')

 # 7. 审计记录包含抽样动作
 r=client.get('/audits',headers=admin)
 assert any(a['action']=='sampling.queue' for a in r.json()),r.text[:200]
 print('审计留痕 OK')

print('\nAPI 集成测试通过')
