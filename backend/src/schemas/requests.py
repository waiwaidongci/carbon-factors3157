from pydantic import BaseModel,Field
class Login(BaseModel): email:str; password:str
class DataInput(BaseModel): data:dict
class RunInput(BaseModel): name:str=Field(min_length=2,max_length=120); policy_id:int; period:str='全部'
class ReviewInput(BaseModel): note:str=Field(min_length=4,max_length=2000)
class ScheduleInput(BaseModel): name:str=Field(min_length=2,max_length=120); run_id:int; interval_seconds:int=Field(ge=5,le=86400)
class SamplingInput(BaseModel):
 name:str=Field(min_length=2,max_length=120);policy_id:int;period:str='全部'
 sample_count:int=Field(default=20000,ge=100,le=1_000_000);chunk_size:int=Field(default=10000,ge=100,le=100000);seed:int=Field(default=42,ge=0,le=2**31-1)
 means:dict[str,float];stds:dict[str,float];correlation:list[list[float]]
