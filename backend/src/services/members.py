"""Attribute-granular member rows for function dependencies.

A Dependency rule stores its two sides as JSON arrays (legacy layout). The
member table stores one row per (rule, side, attribute), which makes rules
searchable by attribute. Every row written for one rule carries the same
edit_token derived from the rule content, so verification can prove both
sides came from the same edit. A rule with an empty lhs still has complete
members: it simply owns zero lhs rows plus its rhs rows.
"""
import hashlib
import json
from collections import defaultdict
from sqlalchemy import delete, select
from src.models.entities import Dependency, DependencyMember
SIDES = ('lhs', 'rhs')


def edit_token(rule):
    payload = {'relation_id': rule.relation_id, 'lhs': list(rule.lhs), 'rhs': list(rule.rhs), 'note': rule.note or ''}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def expected_rows(rule):
    rows = []
    for side in SIDES:
        for ordinal, attribute in enumerate(getattr(rule, side)):
            rows.append((side, ordinal, attribute))
    return rows


def rows_for(db, dependency_id):
    return list(db.scalars(select(DependencyMember).where(DependencyMember.dependency_id == dependency_id).order_by(DependencyMember.side, DependencyMember.ordinal)))


def matches(rule, rows):
    """True when rows are exactly the complete member set of the current rule."""
    token = edit_token(rule)
    expected = expected_rows(rule)
    actual = [(row.side, row.ordinal, row.attribute) for row in sorted(rows, key=lambda r: (r.side, r.ordinal))]
    if actual != expected:
        return False
    return all(row.edit_token == token and row.relation_id == rule.relation_id and row.legacy_dependency_id == rule.id for row in rows)


def replace(db, rule):
    """Rewrite all member rows for one rule inside the caller's transaction.

    The delete happens immediately, so reused ordinals cannot collide with the
    unique (dependency_id, side, ordinal) slot. Rerunning is idempotent: a rule
    that already matches is skipped by sync() and replace() never duplicates.
    """
    db.execute(delete(DependencyMember).where(DependencyMember.dependency_id == rule.id))
    if rule.id is None:
        raise ValueError('规则必须先落库再写成员行')
    token = edit_token(rule)
    db.add_all([DependencyMember(dependency_id=rule.id, legacy_dependency_id=rule.id, relation_id=rule.relation_id, side=side, ordinal=ordinal, attribute=attribute, edit_token=token) for side, ordinal, attribute in expected_rows(rule)])
    return len(expected_rows(rule))


def sync(db, rule):
    """Make member rows agree with the rule; returns 'written' or 'skipped'."""
    if matches(rule, rows_for(db, rule.id)):
        return 'skipped'
    replace(db, rule)
    return 'written'


def describe(rule, rows):
    """Human-readable reasons a rule's member rows do not agree with it."""
    token = edit_token(rule)
    issues = []
    expected = expected_rows(rule)
    actual = [(row.side, row.ordinal, row.attribute) for row in sorted(rows, key=lambda r: (r.side, r.ordinal))]
    if not rows:
        issues.append('缺少成员行')
    elif actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        if missing:
            issues.append('缺少成员 ' + '、'.join(f'{side}[{ordinal}]={attribute}' for side, ordinal, attribute in missing))
        if extra:
            issues.append('多余成员 ' + '、'.join(f'{side}[{ordinal}]={attribute}' for side, ordinal, attribute in extra))
        if not missing and not extra:
            issues.append('成员顺序与规则不一致')
    bad_tokens = {row.edit_token for row in rows if row.edit_token != token}
    if bad_tokens:
        issues.append('两侧成员来自不同次编辑')
    if any(row.relation_id != rule.relation_id for row in rows):
        issues.append('成员所属关系与规则不一致')
    if any(row.legacy_dependency_id != rule.id for row in rows):
        issues.append('成员来源规则编号不一致')
    return issues


def verify(db):
    """Reconcile every legacy rule against its member rows, read-only.

    Verification never locks or rewrites rules, so the legacy editor keeps
    accepting edits while it runs. Dual writes make each rule's two sides
    atomic, and this comparison reads one consistent snapshot.
    """
    rules = list(db.scalars(select(Dependency).order_by(Dependency.id)))
    grouped = defaultdict(list)
    total_rows = 0
    for row in db.scalars(select(DependencyMember).order_by(DependencyMember.dependency_id, DependencyMember.side, DependencyMember.ordinal)):
        grouped[row.dependency_id].append(row)
        total_rows += 1
    issues = []
    for rule in rules:
        problems = describe(rule, grouped.get(rule.id, []))
        if problems:
            issues.append({'dependency_id': rule.id, 'relation_id': rule.relation_id, 'issues': problems})
    known = {rule.id for rule in rules}
    orphans = sorted(dep_id for dep_id in grouped if dep_id not in known)
    if orphans:
        issues.append({'dependency_id': None, 'relation_id': None, 'issues': ['存在无对应规则的成员行：' + '、'.join(map(str, orphans))]})
    return {'consistent': not issues, 'rules': len(rules), 'members': total_rows, 'issue_count': len(issues), 'issues': issues}


def rules_for_relation(db, relation_id):
    """Rebuild complete rules for a relation from member rows (new read path).

    Raises ValueError when members are incomplete or disagree with each other,
    so a report is never queued from silently truncated input.
    """
    rule_ids = list(db.scalars(select(Dependency.id).where(Dependency.relation_id == relation_id).order_by(Dependency.id)))
    rows = list(db.scalars(select(DependencyMember).where(DependencyMember.relation_id == relation_id).order_by(DependencyMember.dependency_id, DependencyMember.side, DependencyMember.ordinal)))
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.dependency_id].append(row)
    missing = [ident for ident in rule_ids if ident not in grouped]
    if missing:
        raise ValueError('成员行缺失，请先完成回填：规则 ' + '、'.join(map(str, missing)))
    orphans = sorted(dep_id for dep_id in grouped if dep_id not in set(rule_ids))
    if orphans:
        raise ValueError('存在无对应规则的成员行，请重新回填：' + '、'.join(map(str, orphans)))
    rebuilt = []
    for ident in rule_ids:
        rule_rows = grouped[ident]
        tokens = {row.edit_token for row in rule_rows}
        if len(tokens) != 1:
            raise ValueError(f'规则 {ident} 的两侧成员来自不同次编辑')
        ordered = {'lhs': [], 'rhs': []}
        for row in sorted(rule_rows, key=lambda r: (r.side, r.ordinal)):
            if row.side not in ordered:
                raise ValueError(f'规则 {ident} 的成员方向非法')
            ordered[row.side].append(row.attribute)
        rebuilt.append({'dependency_id': ident, 'lhs': ordered['lhs'], 'rhs': ordered['rhs'], 'edit_token': rule_rows[0].edit_token})
    return rebuilt


def listing(db, attribute=None, side=None, relation_id=None):
    stmt = select(DependencyMember)
    if attribute is not None:
        stmt = stmt.where(DependencyMember.attribute == attribute)
    if side is not None:
        stmt = stmt.where(DependencyMember.side == side)
    if relation_id is not None:
        stmt = stmt.where(DependencyMember.relation_id == relation_id)
    return [{'id': row.id, 'dependency_id': row.dependency_id, 'relation_id': row.relation_id, 'side': row.side, 'ordinal': row.ordinal, 'attribute': row.attribute, 'edit_token': row.edit_token} for row in db.scalars(stmt.order_by(DependencyMember.relation_id, DependencyMember.dependency_id, DependencyMember.side, DependencyMember.ordinal))]
