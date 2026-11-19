import pytest
from fastapi import HTTPException
from sqlalchemy import select
from src.config.database import Session
from src.models.entities import Dependency, DependencyMember, Relation
from src.services import dependencies as dep_service, migration, members as member_service, reports
from src.schemas.dependency import DependencyInput


def make_relation(db, code='R1', attributes=('A', 'B', 'C')):
    rel = Relation(code=code, title=code, attributes=list(attributes))
    db.add(rel); db.commit(); db.refresh(rel)
    return rel


def add_rule(db, rel, lhs, rhs, note='n'):
    data = DependencyInput(relation_id=rel.id, lhs=list(lhs), rhs=list(rhs), note=note)
    return dep_service.create(db, data, type('U', (), {'email': 'op@t'})())


def rows(db, dependency_id):
    return list(db.scalars(select(DependencyMember).where(DependencyMember.dependency_id == dependency_id).order_by(DependencyMember.side, DependencyMember.ordinal)))


def test_dual_write_creates_member_rows_for_both_sides(client):
    with Session() as db:
        rel = make_relation(db)
        created = add_rule(db, rel, ['A'], ['B', 'C'])
        got = rows(db, created['id'])
        assert [(r.side, r.ordinal, r.attribute) for r in got] == [('lhs', 0, 'A'), ('rhs', 0, 'B'), ('rhs', 1, 'C')]
        assert len({r.edit_token for r in got}) == 1


def test_empty_lhs_rule_stays_complete(client):
    with Session() as db:
        rel = make_relation(db)
        created = add_rule(db, rel, [], ['C'])
        assert [(r.side, r.attribute) for r in rows(db, created['id'])] == [('rhs', 'C')]
        rebuilt = member_service.rules_for_relation(db, rel.id)
        assert rebuilt == [{'dependency_id': created['id'], 'lhs': [], 'rhs': ['C'], 'edit_token': rebuilt[0]['edit_token']}]


def test_update_rewrites_both_sides_with_same_token(client):
    with Session() as db:
        rel = make_relation(db)
        created = add_rule(db, rel, ['A'], ['B'])
        data = DependencyInput(relation_id=rel.id, lhs=['A', 'C'], rhs=['B'], note='changed')
        dep_service.update(db, created['id'], data, type('U', (), {'email': 'op@t'})())
        got = rows(db, created['id'])
        assert [(r.side, r.attribute) for r in got] == [('lhs', 'A'), ('lhs', 'C'), ('rhs', 'B')]
        assert len({r.edit_token for r in got}) == 1
        assert member_service.verify(db)['consistent']


def test_row_level_lookup_by_attribute(client):
    with Session() as db:
        rel = make_relation(db)
        add_rule(db, rel, ['A'], ['B'])
        add_rule(db, rel, ['C'], ['B'])
        hits = member_service.listing(db, attribute='B', side='rhs')
        assert len(hits) == 2
        assert all(x['side'] == 'rhs' and x['attribute'] == 'B' for x in hits)


def test_backfill_is_resumable_and_idempotent(client):
    with Session() as db:
        rel = make_relation(db)
        ids = [add_rule(db, rel, ['A'], ['B'])['id'], add_rule(db, rel, ['A'], ['C'])['id'], add_rule(db, rel, [], ['B'])['id']]
        # simulate pre-existing legacy-only rows by deleting members written at create time
        db.execute(DependencyMember.__table__.delete())
        db.commit()
        user = type('U', (), {'email': 'op@t'})()
        first = migration.backfill(db, user, limit=2)
        assert first['cursor'] == ids[1] and first['written'] == 2 and not first['done']
        assert db.scalar(select(__import__('sqlalchemy').func.count()).select_from(DependencyMember)) == 4
        second = migration.backfill(db, user, limit=2)
        assert second['done'] and second['cursor'] == ids[2]
        total = db.scalar(select(__import__('sqlalchemy').func.count()).select_from(DependencyMember))
        assert total == 5
        rerun = migration.backfill(db, user, limit=2)
        assert rerun['written'] == 0 and rerun['skipped'] == 0


def test_backfill_interruption_keeps_progress(client, monkeypatch):
    with Session() as db:
        rel = make_relation(db)
        first = add_rule(db, rel, ['A'], ['B'])['id']
        add_rule(db, rel, ['A'], ['C'])
        db.execute(DependencyMember.__table__.delete())
        db.commit()
        original = member_service.sync
        calls = {'n': 0}
        def flaky(db_, rule):
            calls['n'] += 1
            if calls['n'] == 2:
                raise RuntimeError('boom')
            return original(db_, rule)
        monkeypatch.setattr(member_service, 'sync', flaky)
        user = type('U', (), {'email': 'op@t'})()
        with pytest.raises(HTTPException) as err:
            migration.backfill(db, user, limit=10)
        assert err.value.status_code == 500
        state = migration.get_state(db)
        assert state.cursor == first
        assert len(rows(db, first)) == 2
        monkeypatch.setattr(member_service, 'sync', original)
        done = migration.backfill(db, user, limit=10)
        assert done['done']
        assert done['written'] == 1


def test_switch_requires_consistent_members_and_gates_report_reads(client):
    with Session() as db:
        rel = make_relation(db)
        created = add_rule(db, rel, ['A'], ['B'])
        user = type('U', (), {'email': 'op@t'})()
        db.get(DependencyMember, rows(db, created['id'])[0].id).attribute = 'B'
        db.commit()
        with pytest.raises(HTTPException) as err:
            migration.switch(db, user)
        assert err.value.status_code == 409
        db.rollback()
        db.execute(DependencyMember.__table__.delete())
        db.commit()
        with pytest.raises(HTTPException):
            migration.switch(db, user)
        migration.backfill(db, user)
        assert migration.switch(db, user)['status'] == 'switched'
        assert migration.reads_members(db)


def test_switched_reports_read_member_rows_and_legacy_edits_stay_visible_after_rollback(client, monkeypatch):
    with Session() as db:
        rel = make_relation(db)
        created = add_rule(db, rel, ['A'], ['B'])
        user = type('U', (), {'email': 'op@t'})()
        migration.backfill(db, user)
        migration.switch(db, user)
        pushed = []
        monkeypatch.setattr(reports.queue, 'rpush', lambda *a: pushed.append(a))
        report = reports.create(db, type('D', (), {'model_dump': lambda self: {'name': 'r1', 'relation_id': rel.id}, 'name': 'r1', 'relation_id': rel.id})(), user)
        snap = db.get(__import__('src.models.entities', fromlist=['Report']).Report, report['id']).input_snapshot
        assert snap['source'] == 'members' and snap['dependencies'] == [{'lhs': ['A'], 'rhs': ['B']}]
        # legacy editor keeps writing after switch; both copies move together
        dep_service.update(db, created['id'], DependencyInput(relation_id=rel.id, lhs=['A', 'C'], rhs=['B']), user)
        assert member_service.verify(db)['consistent']
        migration.rollback(db, user)
        assert not migration.reads_members(db)
        report2 = reports.create(db, type('D', (), {'model_dump': lambda self: {'name': 'r2', 'relation_id': rel.id}, 'name': 'r2', 'relation_id': rel.id})(), user)
        snap2 = db.get(__import__('src.models.entities', fromlist=['Report']).Report, report2['id']).input_snapshot
        assert snap2['source'] == 'legacy'
        assert snap2['dependencies'] == [{'lhs': ['A', 'C'], 'rhs': ['B']}]


def test_switched_report_refuses_incomplete_members(client):
    with Session() as db:
        rel = make_relation(db)
        add_rule(db, rel, ['A'], ['B'])
        user = type('U', (), {'email': 'op@t'})()
        migration.backfill(db, user)
        migration.switch(db, user)
        db.execute(DependencyMember.__table__.delete())
        db.commit()
        with pytest.raises(HTTPException) as err:
            reports.create(db, type('D', (), {'model_dump': lambda self: {'name': 'r', 'relation_id': rel.id}, 'name': 'r', 'relation_id': rel.id})(), user)
        assert err.value.status_code == 409


def test_verify_detects_split_edit_and_missing_rows_without_writing(client):
    with Session() as db:
        rel = make_relation(db)
        first = add_rule(db, rel, ['A'], ['B'])['id']
        second = add_rule(db, rel, ['A', 'C'], ['B'])['id']
        # Tamper: give the rhs row of rule #1 the token of another edit.
        rhs_row = [r for r in rows(db, first) if r.side == 'rhs'][0]
        other_token = rows(db, second)[0].edit_token
        rhs_row.edit_token = other_token
        db.commit()
        report = member_service.verify(db)
        assert not report['consistent']
        assert '两侧成员来自不同次编辑' in report['issues'][0]['issues']
        with pytest.raises(ValueError):
            member_service.rules_for_relation(db, rel.id)
        before = len(rows(db, first))
        db.execute(DependencyMember.__table__.delete().where(DependencyMember.dependency_id == first, DependencyMember.side == 'lhs'))
        db.commit()
        report = member_service.verify(db)
        assert not report['consistent']
        assert any('缺少成员' in issue for issue in report['issues'][0]['issues'])
        assert len(rows(db, first)) == before - 1  # verification is read-only


def test_switched_switch_rollback_roundtrip_requires_admin_route_only(client, auth, admin_auth):
    assert client.post('/migration/switch', headers=auth).status_code == 403
    switch = client.post('/migration/switch', headers=admin_auth)
    assert switch.json()['status'] == 'switched'
    assert client.post('/migration/resume', headers=auth).status_code == 409
    rolled = client.post('/migration/rollback', headers=admin_auth)
    assert rolled.json()['status'] == 'rolled_back'
    assert client.get('/migration', headers=auth).json()['status'] == 'rolled_back'
    # a second switch after rollback re-verifies and is allowed
    assert client.post('/migration/switch', headers=admin_auth).json()['status'] == 'switched'


def test_api_surface_and_roles(client, auth, admin_auth):
    res = client.get('/migration', headers=auth)
    assert res.status_code == 200 and res.json()['status'] == 'migrating'
    assert client.get('/migration', headers={'Authorization': 'Bearer nope'}).status_code == 401
    viewer = client.post('/auth/login', json={'email': 'viewer@t', 'password': 'pw'}).json()
    viewer_auth = {'Authorization': 'Bearer ' + viewer['token']}
    assert client.post('/migration/backfill', headers=viewer_auth).status_code == 403
    assert client.post('/migration/switch', headers=auth).status_code == 403
    with Session() as db:
        rel = make_relation(db)
        add_rule(db, rel, ['A'], ['B'])
    assert client.post('/migration/backfill', headers=auth).status_code == 200
    assert client.post('/migration/verify', headers=viewer_auth).json()['report']['consistent'] is True
    assert client.post('/migration/switch', headers=admin_auth).json()['status'] == 'switched'
    members = client.get('/dependencies/members?attribute=B', headers=auth).json()
    assert [(m['side'], m['attribute']) for m in members] == [('rhs', 'B')]
    assert client.post('/migration/rollback', headers=viewer_auth).status_code == 403
    assert client.post('/migration/rollback', headers=admin_auth).json()['status'] == 'rolled_back'
