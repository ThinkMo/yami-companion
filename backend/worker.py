from __future__ import annotations

import argparse
from datetime import datetime, timezone
import logging
import time
from typing import Optional

from sqlalchemy import select

from backend.config import settings
from backend.db import SessionLocal, init_db
from backend.memory.graphiti_client import GraphitiStore, GraphitiUnavailableError, create_graphiti_store
from backend.memory.service import process_memory_outbox, reconcile_graphiti, run_retention_cleanup
from backend.models import Student

logger = logging.getLogger("yami.worker")


def run_worker_tick(
    *,
    graphiti: GraphitiStore,
    run_maintenance: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """
    Execute one decoupled background worker tick:
      1) Consume PostgreSQL `memory_outbox` -> project upserts/deletes into Neo4j 5 (Graphiti)
      2) Periodically run retention hard-delete (7d assignments/photos, 30d learning events)
         and reconcile Neo4j 5 episodes against PostgreSQL truth per student `group_id`.
    """
    now = now or datetime.now(timezone.utc)
    stats = {
        "outbox_processed": 0,
        "expired_assignments": 0,
        "expired_photos": 0,
        "expired_learning_events": 0,
        "reconciled_students": 0,
        "reconciled_removed_orphans": 0,
        "reconciled_reindexed": 0,
    }
    db = SessionLocal()
    try:
        try:
            stats["outbox_processed"] = process_memory_outbox(db, graphiti=graphiti, now=now)
        except GraphitiUnavailableError as exc:
            logger.warning("Graphiti temporarily unavailable during outbox projection: %s", exc)

        if run_maintenance:
            ret = run_retention_cleanup(db, now=now)
            stats["expired_assignments"] = ret.get("expired_assignments", 0)
            stats["expired_photos"] = ret.get("purged_photo_refs", ret.get("expired_photos", 0))
            stats["expired_learning_events"] = ret.get("expired_learning_events", 0)

            students = list(db.scalars(select(Student)))
            for stu in students:
                try:
                    removed = reconcile_graphiti(
                        db,
                        family_id=stu.family_id,
                        student_id=stu.id,
                        graphiti=graphiti,
                    )
                    stats["reconciled_students"] += 1
                    stats["reconciled_removed_orphans"] += int(removed)
                except GraphitiUnavailableError as exc:
                    logger.warning("Graphiti unavailable during reconcile for %s: %s", stu.id, exc)
                    break
    finally:
        db.close()
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Yami decoupled background worker (Graphiti Outbox + Retention)")
    parser.add_argument("--once", action="store_true", help="Run a single tick (including maintenance) and exit")
    parser.add_argument("--poll-seconds", type=float, default=3.0, help="Outbox poll interval in seconds")
    parser.add_argument("--maintenance-seconds", type=float, default=3600.0, help="Retention & reconcile interval")
    args = parser.parse_args()

    init_db()
    graphiti = create_graphiti_store(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        prefer_neo4j=True,
    )
    logger.info("Yami worker started (db=%s, graphiti=%s)", settings.database_url.split("@")[-1], type(graphiti).__name__)

    if args.once:
        res = run_worker_tick(graphiti=graphiti, run_maintenance=True)
        logger.info("Worker --once completed: %s", res)
        return

    last_maint = 0.0
    try:
        while True:
            now_mono = time.monotonic()
            do_maint = (now_mono - last_maint) >= args.maintenance_seconds
            res = run_worker_tick(graphiti=graphiti, run_maintenance=do_maint)
            if do_maint:
                last_maint = now_mono
            if res["outbox_processed"] > 0 or do_maint:
                logger.info("Worker tick: %s", res)
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        logger.info("Yami worker stopped gracefully.")


if __name__ == "__main__":
    main()
