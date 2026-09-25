"""服务端业务模块。"""
from __future__ import annotations
from collections.abc import Iterator
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import get_db
from app.main import app
from app.models import Base

test_engine = create_engine(
    "sqlite:///./practice_hours_test.db",
    connect_args={"check_same_thread": False, "timeout": 30},
)
TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False, future=True)

@pytest.fixture(autouse=True)
def _schema() -> Iterator[None]:
    Base.metadata.create_all(test_engine)
    yield
    Base.metadata.drop_all(test_engine)

@pytest.fixture
def db() -> Iterator[Session]:
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.close()

@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    def override() -> Iterator[Session]:
        yield db
    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()

SHANGHAI_PLAN = {"plan_version": "P-SH-2024", "iana_timezone": "Asia/Shanghai", "required_seconds": 10800}
NY_PLAN = {"plan_version": "P-NY-2024", "iana_timezone": "America/New_York", "required_seconds": 3600}
