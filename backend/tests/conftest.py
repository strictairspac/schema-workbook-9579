import os, tempfile
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'test.db'))
os.environ.setdefault('SEED_DEMO', 'false')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6399/0')
import pytest
from fastapi.testclient import TestClient
from src.config.database import Base, engine, Session
from src.main import app
from src.models.entities import User, Relation, Dependency
from src.utils.security import password_hash

@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as db:
        for role in ('admin', 'operator', 'viewer'):
            db.add(User(email=role + '@t', password=password_hash('pw'), role=role))
        db.commit()
    with TestClient(app) as c:
        yield c

def login(client, role='operator'):
    res = client.post('/auth/login', json={'email': role + '@t', 'password': 'pw'})
    assert res.status_code == 200, res.text
    return {'Authorization': 'Bearer ' + res.json()['token']}

@pytest.fixture()
def auth(client):
    return login(client, 'operator')

@pytest.fixture()
def admin_auth(client):
    return login(client, 'admin')
