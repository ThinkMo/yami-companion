from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.main import app
from backend.db import Base, get_db
from backend.models import Family, Student


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = TestingSessionLocal()
    try:
        # Two families, two students in fam_a (小禾, 乐乐), one student in fam_b
        fam_a = Family(id="fam_a", name="小禾乐乐家")
        fam_b = Family(id="fam_b", name="邻居家")
        db.add_all(
            [
                fam_a,
                fam_b,
                Student(
                    id="stu_he",
                    family_id="fam_a",
                    name="小禾",
                    grade=4,
                    textbook_version="人教版",
                    graphiti_group_id="stu_grp_he_01",
                    voiceprint_consent=True,
                    camera_consent=True,
                ),
                Student(
                    id="stu_le",
                    family_id="fam_a",
                    name="乐乐",
                    grade=3,
                    textbook_version="北师大版",
                    graphiti_group_id="stu_grp_le_02",
                    voiceprint_consent=False,
                    camera_consent=False,
                ),
                Student(
                    id="stu_other",
                    family_id="fam_b",
                    name="天天",
                    grade=4,
                    textbook_version="苏教版",
                    graphiti_group_id="stu_grp_other_03",
                    voiceprint_consent=True,
                ),
            ]
        )
        db.commit()
        yield db
    finally:
        db.close()


@pytest.fixture()
def client(db_session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
