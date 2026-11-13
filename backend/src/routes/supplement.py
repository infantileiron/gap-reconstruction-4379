from fastapi import APIRouter,Depends,Response
from src.controllers import supplement as controller
from src.schemas.entities import SupplementInput
from src.services.auth import current,editor
router=APIRouter(prefix="/supplements",tags=["supplements"])
@router.get("")
def list_items(dataset_id:str|None=None,user=Depends(current)):
 return controller.list_items(dataset_id)
@router.post("")
def create(data:SupplementInput,response:Response,user=Depends(editor)):
 batch=controller.create(data,user)
 if batch.get("replayed"): response.status_code=200
 else: response.status_code=201
 return batch
@router.get("/{id}")
def detail(id:str,user=Depends(current)): return controller.detail(id)
