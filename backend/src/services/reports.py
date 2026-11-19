from sqlalchemy import select
from redis import Redis
from fastapi import HTTPException
from src.models.entities import Relation,Dependency,Report
from src.config.settings import REDIS_URL
from src.constants.enums import RUN_STATES
from src.services import members as member_service
from src.services import migration
from src.utils.serialization import record
from .common import save
queue=Redis.from_url(REDIS_URL,decode_responses=True)
def listing(db):return [record(x) for x in db.scalars(select(Report).order_by(Report.id.desc()))]
def detail(db,ident):
    obj=db.get(Report,ident)
    if not obj:raise HTTPException(404,'报告不存在')
    return record(obj)
def _input_dependencies(db,relation,use_members):
    """Freeze the rule set for a new report.

    After the switch, rules are rebuilt from member rows so the report reads
    the migrated layout; a truncated or split-edit member set is refused
    rather than silently producing a report from partial input. Before the
    switch, or after a rollback, the legacy JSON columns are read, which dual
    writes keep equal to the members for rules edited after the switch.
    """
    if use_members:
        try:rebuilt=member_service.rules_for_relation(db,relation.id)
        except ValueError as exc:raise HTTPException(409,str(exc))
        return 'members',[{'lhs':x['lhs'],'rhs':x['rhs']} for x in rebuilt]
    deps=list(db.scalars(select(Dependency).where(Dependency.relation_id==relation.id).order_by(Dependency.id)))
    return 'legacy',[{'lhs':x.lhs,'rhs':x.rhs} for x in deps]
def create(db,data,user):
    relation=db.get(Relation,data.relation_id)
    if not relation:raise HTTPException(404,'关系不存在')
    source,dependencies=_input_dependencies(db,relation,migration.reads_members(db))
    snapshot={'relation':record(relation),'dependencies':dependencies,'source':source}
    obj=Report(**data.model_dump(),input_snapshot=snapshot,status=RUN_STATES[0]);result=save(db,obj,user,'范式分解入队 '+data.name)
    try:queue.rpush('schemaworkbook:queue',str(obj.id))
    except Exception:
        obj.status='failed';obj.error='队列暂时不可用，请重新创建';db.commit();raise HTTPException(503,obj.error)
    return result
