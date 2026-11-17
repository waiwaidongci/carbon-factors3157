from sqlalchemy import Column,Integer,String,JSON,Text,Float
from src.config.database import Base
class SamplingJob(Base):
 __tablename__='sampling_jobs'
 id=Column(Integer,primary_key=True);name=Column(String(120));status=Column(String(20),default='queued')
 snapshot=Column(JSON,nullable=False);checksum=Column(String(64));sample_count=Column(Integer);chunk_size=Column(Integer);seed=Column(Integer)
 progress=Column(Integer,default=0);result=Column(JSON);error=Column(Text);created=Column(Float);finished=Column(Float)
 finalize_attempts=Column(Integer,default=0);retry_after=Column(Float)
class SamplingChunk(Base):
 __tablename__='sampling_chunks'
 id=Column(Integer,primary_key=True);job_id=Column(Integer,index=True);seq=Column(Integer);start=Column(Integer);size=Column(Integer)
 status=Column(String(20),default='pending');attempts=Column(Integer,default=0)
 claim_by=Column(String(80));claim_at=Column(Float);partial=Column(JSON)
