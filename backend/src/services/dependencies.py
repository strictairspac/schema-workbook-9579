from sqlalchemy import select,func
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from src.models.entities import Relation,Dependency,Audit
from src.utils.serialization import record
from src.services import members as member_service
def listing(db):return [record(x) for x in db.scalars(select(Dependency).order_by(Dependency.id))]
def validate(db,data,exclude=None):
    relation=db.get(Relation,data.relation_id)
    if not relation:raise HTTPException(404,'关系不存在')
    if not set(data.lhs+data.rhs)<=set(relation.attributes):raise HTTPException(422,'依赖属性必须属于选定关系')
    count=db.scalar(select(func.count()).select_from(Dependency).where(Dependency.relation_id==data.relation_id))
    if not exclude and count>=64:raise HTTPException(422,'最多64条函数依赖，请合并后重试')
def _commit_dual(db,obj,user,action):
    """Persist the rule and its member rows in one transaction.

    The rule's two sides and the member rows are written from the same edit:
    if member writing fails the whole edit rolls back, so the legacy copy and
    the member copy never diverge. Verification does not lock the editor.
    """
    try:
        db.flush()
    except IntegrityError:
        db.rollback();raise HTTPException(409,'编号已存在或引用对象无效，请调整后重试')
    try:
        member_service.replace(db,obj)
        db.add(Audit(actor=user.email,action=action))
        db.commit()
    except IntegrityError:
        db.rollback();raise HTTPException(409,'编号已存在或引用对象无效，请调整后重试')
    except Exception:
        db.rollback();raise
    return record(obj)
def create(db,data,user):
    validate(db,data);obj=Dependency(**data.model_dump());db.add(obj)
    return _commit_dual(db,obj,user,'创建函数依赖')
def update(db,ident,data,user):
    # Lock the rule so a concurrent backfill either copies this edit or waits
    # for it; both sides and the member rows then commit together.
    obj=db.get(Dependency,ident,with_for_update=True)
    if not obj:raise HTTPException(404,'依赖不存在')
    validate(db,data,ident)
    for k,v in data.model_dump().items():setattr(obj,k,v)
    return _commit_dual(db,obj,user,'更新函数依赖')
