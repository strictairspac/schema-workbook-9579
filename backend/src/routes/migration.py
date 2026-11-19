from fastapi import APIRouter,Depends,Query
from src.config.database import db_session
from src.utils.security import current,writer,admin
from src.services import members as member_service
from src.services import migration as migration_service
router=APIRouter()
@router.get('/dependencies/members')
def listing(attribute:str|None=Query(default=None,max_length=40),side:str|None=Query(default=None,pattern='^(lhs|rhs)$'),relation_id:int|None=Query(default=None,gt=0),db=Depends(db_session),user=Depends(current)):
    return member_service.listing(db,attribute,side,relation_id)
@router.get('/migration')
def status(db=Depends(db_session),user=Depends(current)):
    return migration_service.status(db)
@router.post('/migration/backfill')
def backfill(limit:int=Query(default=50,ge=1,le=500),db=Depends(db_session),user=Depends(writer)):
    return migration_service.backfill(db,user,limit)
@router.post('/migration/verify')
def verify(db=Depends(db_session),user=Depends(current)):
    return migration_service.verify(db)
@router.post('/migration/switch')
def switch(db=Depends(db_session),user=Depends(admin)):
    return migration_service.switch(db,user)
@router.post('/migration/rollback')
def rollback(db=Depends(db_session),user=Depends(admin)):
    return migration_service.rollback(db,user)
@router.post('/migration/resume')
def resume(db=Depends(db_session),user=Depends(writer)):
    return migration_service.resume(db,user)
