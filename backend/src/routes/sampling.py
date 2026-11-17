from fastapi import APIRouter,Depends,HTTPException
from src.config.database import database
from src.controllers.auth import current,editor
from src.models.sampling import SamplingJob
from src.schemas.requests import SamplingInput
from src.services.sampling import submit_sampling
from src.utils.serialize import row
router=APIRouter()
@router.get('/sampling')
def jobs(db=Depends(database),u=Depends(current)):return [row(x) for x in db.query(SamplingJob).order_by(SamplingJob.id.desc()).all()]
@router.post('/sampling')
def create(b:SamplingInput,db=Depends(database),u=Depends(editor)):return row(submit_sampling(db,u,b))
@router.get('/sampling/{id}')
def detail(id:int,db=Depends(database),u=Depends(current)):
 obj=db.get(SamplingJob,id)
 if not obj:raise HTTPException(404,'抽样作业不存在')
 return row(obj)
