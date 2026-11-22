from __future__ import annotations

from datetime import datetime, timedelta, timezone
import socket

import pytest
from sqlalchemy.orm import sessionmaker

from backend.db import init_db, make_engine
from backend.memory.graphiti_client import InMemoryGraphitiStore, Neo4jGraphitiStore
from backend.memory.service import (
    delete_learner_memory,
    process_memory_outbox,
    recall_for_turn,
    reconcile_graphiti,
    write_learner_memory,
)
from backend.models import Family, Student
from backend.worker import run_worker_tick


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def test_decoupled_worker_tick_outbox_and_reconcile(db_session, monkeypatch):
    """Offline test verifying decoupled backend.worker tick consumes outbox & reconciles."""
    import backend.worker as worker_mod

    monkeypatch.setattr(worker_mod, "SessionLocal", lambda: db_session)
    # Prevent worker_tick's finally: db.close() from closing the shared fixture session
    monkeypatch.setattr(db_session, "close", lambda: None)

    graphiti = InMemoryGraphitiStore()
    now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)

    mem = write_learner_memory(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        kind="preference",
        summary="以后有数学就先做数学",
    )
    stats = run_worker_tick(graphiti=graphiti, run_maintenance=True, now=now)
    assert stats["outbox_processed"] == 1
    assert stats["reconciled_students"] >= 2

    recalled = recall_for_turn(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        intent="start",
        graphiti=graphiti,
    )
    assert any(r.memory_id == mem.id and r.source == "graphiti_verified" for r in recalled)


def test_live_postgres_and_neo4j5_graphiti_e2e():
    """Live Docker verification against local PostgreSQL 16 + Neo4j 5 (skips if containers stopped)."""
    if not (_port_open("127.0.0.1", 5432) and _port_open("127.0.0.1", 7687)):
        pytest.skip("Local Docker PostgreSQL (5432) or Neo4j 5 (7687) not running")

    pg_url = "postgresql+psycopg2://yami:yami_dev_password@127.0.0.1:5432/yami"
    eng = make_engine(pg_url)
    init_db(eng)
    PgSession = sessionmaker(bind=eng, autoflush=False, autocommit=False, expire_on_commit=False)

    graphiti = Neo4jGraphitiStore(
        uri="bolt://127.0.0.1:7687",
        user="neo4j",
        password="yami_dev_password",
    )
    graphiti.verify_connectivity()

    db = PgSession()
    now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    fam_id = "fam_docker_test"
    stu_he_id = "stu_docker_he"
    stu_le_id = "stu_docker_le"
    grp_he = "grp_docker_he"
    grp_le = "grp_docker_le"

    try:
        # Clean previous test run if any
        graphiti.purge_group(group_id=grp_he)
        graphiti.purge_group(group_id=grp_le)
        old_fam = db.get(Family, fam_id)
        if old_fam is not None:
            db.delete(old_fam)
            db.commit()

        # 1. Seed family + 2 isolated students in real PostgreSQL 16
        fam = Family(id=fam_id, name="本地Docker测试家庭")
        he = Student(id=stu_he_id, family_id=fam_id, name="小禾", grade=4, graphiti_group_id=grp_he)
        le = Student(id=stu_le_id, family_id=fam_id, name="乐乐", grade=3, graphiti_group_id=grp_le)
        db.add_all([fam, he, le])
        db.commit()

        # 2. Write preference & strategy -> enqueue memory_outbox -> project into real Neo4j 5
        m_pref = write_learner_memory(
            db,
            family_id=fam_id,
            student_id=stu_he_id,
            kind="preference",
            summary="以后有数学就先做数学",
        )
        m_strat = write_learner_memory(
            db,
            family_id=fam_id,
            student_id=stu_he_id,
            kind="strategy",
            topic_id="unit_fraction_compare",
            summary="画两个同样大的整体比较单位分数",
        )
        processed = process_memory_outbox(db, graphiti=graphiti, now=now)
        assert processed >= 2

        # 3. Cross-session recall from real Neo4j 5 hits graphiti_verified & isolates sibling 乐乐
        recalled_he = recall_for_turn(
            db,
            family_id=fam_id,
            student_id=stu_he_id,
            intent="help",
            topic_id="unit_fraction_compare",
            graphiti=graphiti,
        )
        assert any(r.memory_id == m_strat.id and r.source == "graphiti_verified" for r in recalled_he)

        recalled_le = recall_for_turn(
            db,
            family_id=fam_id,
            student_id=stu_le_id,
            intent="help",
            topic_id="unit_fraction_compare",
            graphiti=graphiti,
        )
        assert recalled_le == []

        # 4. Delete memory -> remove_episode removes it from real Neo4j 5 immediately
        delete_learner_memory(
            db,
            family_id=fam_id,
            student_id=stu_he_id,
            memory_id=m_pref.id,
            graphiti=graphiti,
        )
        after_del = recall_for_turn(
            db,
            family_id=fam_id,
            student_id=stu_he_id,
            intent="start",
            graphiti=graphiti,
        )
        assert all(r.memory_id != m_pref.id for r in after_del)

        # 5. Reconcile catches orphan episodes in Neo4j 5 and removes them cleanly
        orphan_ep = graphiti.add_episode(
            group_id=grp_he,
            name="orphan",
            episode_body="不应存在的孤立记忆",
            source_description="mem:orphan:v1",
        )
        assert orphan_ep in graphiti.list_group_episode_uuids(group_id=grp_he)
        removed = reconcile_graphiti(db, family_id=fam_id, student_id=stu_he_id, graphiti=graphiti)
        assert removed >= 1
        assert orphan_ep not in graphiti.list_group_episode_uuids(group_id=grp_he)
    finally:
        # Clean up test records in both Neo4j 5 & PostgreSQL 16
        try:
            graphiti.purge_group(group_id=grp_he)
            graphiti.purge_group(group_id=grp_le)
        except Exception:
            pass
        old_fam = db.get(Family, fam_id)
        if old_fam is not None:
            db.delete(old_fam)
            db.commit()
        db.close()
        graphiti.close()
        eng.dispose()
