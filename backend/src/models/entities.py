from sqlalchemy import Column,Integer,String,JSON,DateTime,ForeignKey,Text,UniqueConstraint,Index
from datetime import datetime,timezone
from src.config.database import Base
class User(Base):
    __tablename__='users'
    id=Column(Integer,primary_key=True);email=Column(String(120),unique=True,nullable=False);password=Column(String(300),nullable=False);role=Column(String(20),nullable=False)
class Relation(Base):
    __tablename__='relations'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False);title=Column(String(160),nullable=False);attributes=Column(JSON,nullable=False);note=Column(String(300),default='')
class Dependency(Base):
    __tablename__='dependencies'
    id=Column(Integer,primary_key=True);relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False);lhs=Column(JSON,nullable=False);rhs=Column(JSON,nullable=False);note=Column(String(300),default='')
class DependencyMember(Base):
    """Attribute-granular rows derived from a Dependency rule.

    One row per (dependency, side, attribute): side is 'lhs' or 'rhs'.
    A rule with an empty lhs still owns no lhs rows but keeps its rhs rows,
    so it remains a complete rule and can be rebuilt from the member rows.
    edit_token is the content digest of the rule at the edit that produced
    the rows: every row of one rule carries the same token, and verification
    rejects a rule whose two sides came from different edits.
    legacy_dependency_id preserves the source rule id if rows are ever moved.
    """
    __tablename__='dependency_members'
    __table_args__=(UniqueConstraint('dependency_id','side','ordinal',name='uq_dependency_member_slot'),Index('ix_dependency_member_lookup','relation_id','side','attribute'))
    id=Column(Integer,primary_key=True)
    dependency_id=Column(Integer,ForeignKey('dependencies.id',ondelete='CASCADE'),nullable=False)
    legacy_dependency_id=Column(Integer,nullable=False)
    relation_id=Column(Integer,nullable=False)
    side=Column(String(3),nullable=False)
    ordinal=Column(Integer,nullable=False)
    attribute=Column(String(40),nullable=False)
    edit_token=Column(String(64),nullable=False)
class MigrationState(Base):
    """Single-row state machine for the dependency-to-members migration."""
    __tablename__='migration_state'
    id=Column(Integer,primary_key=True)
    name=Column(String(80),unique=True,nullable=False)
    status=Column(String(20),nullable=False,default='migrating')
    cursor=Column(Integer,nullable=False,default=0)
    verified_at=Column(DateTime(timezone=True),nullable=True)
    switched_at=Column(DateTime(timezone=True),nullable=True)
    rolled_back_at=Column(DateTime(timezone=True),nullable=True)
    last_error=Column(Text,nullable=True)
class Report(Base):
    __tablename__='reports'
    id=Column(Integer,primary_key=True);name=Column(String(120),nullable=False);relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False)
    status=Column(String(20),nullable=False,default='queued');input_snapshot=Column(JSON,nullable=False);result=Column(JSON,nullable=True);error=Column(Text,nullable=True)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class Audit(Base):
    __tablename__='audit_logs'
    id=Column(Integer,primary_key=True);actor=Column(String(120),nullable=False);action=Column(String(160),nullable=False);created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
