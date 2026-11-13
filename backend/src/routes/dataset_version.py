from fastapi import APIRouter,Depends
from src.controllers import supplement as controller
from src.services.auth import current
router=APIRouter(prefix="/datasets",tags=["datasets"])
@router.get("/{dataset_id}/version-trail")
def version_trail(dataset_id:str,user=Depends(current)):
 return controller.version_trail(dataset_id)
