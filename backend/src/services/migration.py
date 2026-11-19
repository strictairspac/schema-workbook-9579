"""Dual-write dependency→member migration with resumable backfill and reconciliation.

Lifecycle: migrating -> (verify consistent) -> switched -> (rollback) -> rolled_back.
Reports read member rows only while status is switched. Rollback flips the read
path back to the legacy JSON columns, which dual writes kept authoritative, so
rules edited after the switch still resolve to the same values.
"""
from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from src.models.entities import Audit, Dependency, DependencyMember, MigrationState
from src.constants.enums import MIGRATION_STATES
from src.services import members as member_service
from src.utils.serialization import record

NAME = 'dependencies_to_members'
BATCH = 50


def get_state(db):
    state = db.scalar(select(MigrationState).where(MigrationState.name == NAME))
    if state:
        return state
    state = MigrationState(name=NAME, status='migrating', cursor=0)
    db.add(state)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        state = db.scalar(select(MigrationState).where(MigrationState.name == NAME))
        if not state:
            raise
    return state


def status(db):
    state = get_state(db)
    data = record(state)
    data['member_rows'] = db.scalar(select(func.count()).select_from(DependencyMember))
    data['rule_rows'] = db.scalar(select(func.count()).select_from(Dependency))
    return data


def _save_state(db, state, user, action, **fields):
    if fields.get('status', state.status) not in MIGRATION_STATES:
        raise ValueError('未知的迁移状态')
    for key, value in fields.items():
        setattr(state, key, value)
    db.add(Audit(actor=user.email, action=action))
    db.commit()
    return status(db)


def backfill(db, user, limit=BATCH):
    """Copy legacy rules into member rows starting from the stored cursor.

    Idempotent per rule: rules whose members already match are skipped, so an
    interrupted run resumes at its saved cursor and a full rerun adds nothing.
    Each rule is re-read under a row lock, so a concurrent legacy edit either
    lands before the copy or waits for it; the cursor only advances past a rule
    after its rows and the cursor are committed together.
    """
    state = get_state(db)
    if state.status == 'switched':
        raise HTTPException(409, '已完成切换，无需回填')
    limit = min(max(limit, 1), 500)
    ids = list(db.scalars(select(Dependency.id).where(Dependency.id > state.cursor).order_by(Dependency.id).limit(limit)))
    written = skipped = 0
    for ident in ids:
        try:
            rule = db.scalar(select(Dependency).where(Dependency.id == ident).with_for_update())
            if rule is None:
                state.cursor = ident
                db.commit()
                continue
            outcome = member_service.sync(db, rule)
            state.cursor = ident
            state.last_error = None
            db.commit()
            if outcome == 'written':
                written += 1
            else:
                skipped += 1
        except Exception as exc:
            db.rollback()
            state = get_state(db)
            state.last_error = str(exc)[:300]
            db.add(Audit(actor=user.email, action='回填中断：' + str(exc)[:100]))
            db.commit()
            raise HTTPException(500, '回填中断，可从原进度重试')
    remaining = db.scalar(select(Dependency.id).where(Dependency.id > state.cursor).order_by(Dependency.id).limit(1))
    data = status(db)
    data.update({'cursor': state.cursor, 'written': written, 'skipped': skipped, 'done': remaining is None})
    return data


def verify(db):
    """Reconcile legacy rules with member rows without locking the editor."""
    return {'state': status(db), 'report': member_service.verify(db)}


def switch(db, user):
    """Enable member-row reads only after a clean reconciliation."""
    state = get_state(db)
    if state.status == 'switched':
        return status(db)
    report = member_service.verify(db)
    if not report['consistent']:
        detail = '；'.join('、'.join(issue['issues']) for issue in report['issues'][:5])
        raise HTTPException(409, '成员行与规则不一致，暂不能切换：' + detail)
    now = datetime.now(timezone.utc)
    data = _save_state(db, state, user, '切换分解报告读取成员行', status='switched', verified_at=now, switched_at=now, rolled_back_at=None, last_error=None)
    return data


def rollback(db, user):
    state = get_state(db)
    if state.status != 'switched':
        raise HTTPException(409, '当前不在已切换状态')
    return _save_state(db, state, user, '回退到旧依赖读法', status='rolled_back', rolled_back_at=datetime.now(timezone.utc))


def resume(db, user):
    """Clear the interruption marker so the cursor can be picked up again.

    The status is left untouched: an interrupted backfill during migration
    still reads as migrating, and one attempted after a rollback still reads
    as rolled_back until an explicit switch succeeds.
    """
    state = get_state(db)
    if state.status == 'switched':
        raise HTTPException(409, '已完成切换，无需续跑')
    return _save_state(db, state, user, '恢复依赖迁移', last_error=None)


def reads_members(db):
    return get_state(db).status == 'switched'
