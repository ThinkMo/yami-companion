from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional
import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.memory.graphiti_client import GraphitiStore, GraphitiUnavailableError
from backend.models import (
    Assignment,
    GrowthRecord,
    LearnerMemory,
    LearningEvent,
    MemoryOutbox,
    MethodCardUnlock,
    Student,
    SubmissionEvidence,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RecalledMemory:
    memory_id: str
    kind: str
    topic_id: Optional[str]
    summary: str
    source: Literal["graphiti_verified", "pg_structured"]


def write_learner_memory(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    kind: Literal["preference", "strategy", "agreement"],
    summary: str,
    topic_id: Optional[str] = None,
    source_event_id: Optional[str] = None,
    replace_memory_id: Optional[str] = None,
) -> LearnerMemory:
    """Write/update a verified long-term memory in PostgreSQL and enqueue Graphiti outbox."""
    student = db.get(Student, student_id)
    if student is None or student.family_id != family_id:
        raise PermissionError("student_outside_family")

    if replace_memory_id:
        mem = db.get(LearnerMemory, replace_memory_id)
        if mem is None or mem.student_id != student_id:
            raise PermissionError("memory_outside_student_scope")
        mem.summary = summary.strip()
        mem.topic_id = topic_id
        mem.source_event_id = source_event_id
        mem.version += 1
        mem.active = True
    else:
        mem = LearnerMemory(
            id=f"mem_{uuid.uuid4().hex[:12]}",
            family_id=family_id,
            student_id=student_id,
            kind=kind,
            topic_id=topic_id,
            summary=summary.strip(),
            source_event_id=source_event_id,
            version=1,
            active=True,
        )
        db.add(mem)
        db.flush()

    db.add(MemoryOutbox(memory_id=mem.id, student_id=student_id, op="upsert"))
    db.commit()
    db.refresh(mem)
    return mem


def delete_learner_memory(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    memory_id: str,
    graphiti: Optional[GraphitiStore] = None,
) -> None:
    """
    Two-step forgetting (Architecture §6 & §7):
    1. Immediately mark inactive in PostgreSQL so reads/speech never see it again.
    2. Remove episode from Graphiti (or queue outbox if Graphiti is temporarily down).
    """
    mem = db.get(LearnerMemory, memory_id)
    if mem is None or mem.family_id != family_id or mem.student_id != student_id:
        raise PermissionError("memory_outside_student_scope")
    mem.active = False
    ep_uuid = mem.graphiti_episode_uuid
    mem.graphiti_episode_uuid = None

    if graphiti is not None and ep_uuid:
        try:
            graphiti.remove_episode(episode_uuid=ep_uuid)
        except GraphitiUnavailableError:
            db.add(MemoryOutbox(memory_id=mem.id, student_id=student_id, op="delete"))
    elif ep_uuid:
        db.add(MemoryOutbox(memory_id=mem.id, student_id=student_id, op="delete"))
    db.commit()


def process_memory_outbox(db: Session, *, graphiti: GraphitiStore, now: Optional[datetime] = None) -> int:
    """Async worker projection from PostgreSQL outbox to student-scoped Graphiti group_id."""
    now = now or utcnow()
    pending = list(
        db.scalars(select(MemoryOutbox).where(MemoryOutbox.processed_at.is_(None)).order_by(MemoryOutbox.id.asc()))
    )
    processed = 0
    for row in pending:
        mem = db.get(LearnerMemory, row.memory_id)
        if mem is None:
            row.processed_at = now
            processed += 1
            continue
        student = db.get(Student, mem.student_id)
        if student is None:
            row.processed_at = now
            processed += 1
            continue
        try:
            if row.op == "delete" or not mem.active:
                if mem.graphiti_episode_uuid:
                    graphiti.remove_episode(episode_uuid=mem.graphiti_episode_uuid)
                    mem.graphiti_episode_uuid = None
            else:
                if mem.graphiti_episode_uuid:
                    graphiti.remove_episode(episode_uuid=mem.graphiti_episode_uuid)
                ep_uuid = graphiti.add_episode(
                    group_id=student.graphiti_group_id,
                    name=f"{mem.kind}:{mem.topic_id or 'general'}",
                    episode_body=f"{mem.topic_id or ''} {mem.summary}".strip(),
                    source_description=f"mem:{mem.id}:v{mem.version}",
                )
                mem.graphiti_episode_uuid = ep_uuid
            row.processed_at = now
            processed += 1
        except GraphitiUnavailableError:
            break
    db.commit()
    return processed


def recall_for_turn(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    intent: str,
    topic_id: Optional[str] = None,
    graphiti: Optional[GraphitiStore] = None,
    is_independent_review_first_attempt: bool = False,
    limit: int = 3,
) -> list[RecalledMemory]:
    """
    Cross-session personalized recall (Architecture §6):
    1. Queries Graphiti strictly within the student's opaque group_id.
    2. Verifies EVERY Graphiti hit back against PostgreSQL (student, active, version).
    3. Falls back seamlessly to PostgreSQL structured query when Graphiti is unavailable or lagging.
    4. Filters out solving strategies before the child's first independent review attempt.
    """
    student = db.get(Student, student_id)
    if student is None or student.family_id != family_id:
        raise PermissionError("student_outside_family")

    results: list[RecalledMemory] = []
    seen_ids: set[str] = set()

    if graphiti is not None:
        try:
            query = f"{intent} {topic_id or ''}".strip()
            hits = graphiti.search(query=query, group_ids=[student.graphiti_group_id], limit=limit * 2)
            for hit in hits:
                if not hit.source_description.startswith("mem:"):
                    continue
                parts = hit.source_description.split(":")
                if len(parts) != 3 or not parts[2].startswith("v"):
                    continue
                mem_id, ver_str = parts[1], parts[2][1:]
                mem = db.get(LearnerMemory, mem_id)
                # Strict PostgreSQL re-verification
                if (
                    mem is None
                    or mem.family_id != family_id
                    or mem.student_id != student_id
                    or not mem.active
                    or str(mem.version) != ver_str
                ):
                    continue
                if is_independent_review_first_attempt and mem.kind == "strategy":
                    continue
                if mem.id not in seen_ids:
                    seen_ids.add(mem.id)
                    results.append(
                        RecalledMemory(
                            memory_id=mem.id,
                            kind=mem.kind,
                            topic_id=mem.topic_id,
                            summary=mem.summary,
                            source="graphiti_verified",
                        )
                    )
                if len(results) >= limit:
                    return results
        except GraphitiUnavailableError:
            pass

    # Fallback / supplement from PostgreSQL structured query
    stmt = (
        select(LearnerMemory)
        .where(
            LearnerMemory.family_id == family_id,
            LearnerMemory.student_id == student_id,
            LearnerMemory.active.is_(True),
        )
        .order_by(LearnerMemory.updated_at.desc())
    )
    for mem in db.scalars(stmt):
        if mem.id in seen_ids:
            continue
        if topic_id and mem.topic_id and mem.topic_id != topic_id:
            continue
        if is_independent_review_first_attempt and mem.kind == "strategy":
            continue
        seen_ids.add(mem.id)
        results.append(
            RecalledMemory(
                memory_id=mem.id,
                kind=mem.kind,
                topic_id=mem.topic_id,
                summary=mem.summary,
                source="pg_structured",
            )
        )
        if len(results) >= limit:
            break
    return results


def rebuild_student_graph(db: Session, *, family_id: str, student_id: str, graphiti: GraphitiStore) -> int:
    """Rebuilds a student's entire Graphiti subgraph from PostgreSQL (unique source of truth)."""
    student = db.get(Student, student_id)
    if student is None or student.family_id != family_id:
        raise PermissionError("student_outside_family")
    graphiti.purge_group(group_id=student.graphiti_group_id)
    mems = list(
        db.scalars(
            select(LearnerMemory).where(
                LearnerMemory.family_id == family_id,
                LearnerMemory.student_id == student_id,
                LearnerMemory.active.is_(True),
            )
        )
    )
    for mem in mems:
        ep_uuid = graphiti.add_episode(
            group_id=student.graphiti_group_id,
            name=f"{mem.kind}:{mem.topic_id or 'general'}",
            episode_body=f"{mem.topic_id or ''} {mem.summary}".strip(),
            source_description=f"mem:{mem.id}:v{mem.version}",
        )
        mem.graphiti_episode_uuid = ep_uuid
    db.commit()
    return len(mems)


def reconcile_graphiti(db: Session, *, family_id: str, student_id: str, graphiti: GraphitiStore) -> int:
    """Daily reconciliation: deletes any Graphiti episode not backed by an active PostgreSQL memory."""
    student = db.get(Student, student_id)
    if student is None or student.family_id != family_id:
        raise PermissionError("student_outside_family")
    valid_uuids = {
        m.graphiti_episode_uuid
        for m in db.scalars(
            select(LearnerMemory).where(
                LearnerMemory.family_id == family_id,
                LearnerMemory.student_id == student_id,
                LearnerMemory.active.is_(True),
                LearnerMemory.graphiti_episode_uuid.is_not(None),
            )
        )
    }
    graph_uuids = graphiti.list_group_episode_uuids(group_id=student.graphiti_group_id)
    orphans = graph_uuids - valid_uuids
    for ep in orphans:
        graphiti.remove_episode(episode_uuid=ep)
    return len(orphans)


def run_retention_cleanup(db: Session, *, now: datetime) -> dict[str, int]:
    """
    Controllable-clock retention cleanup:
    - Deletes expired assignments (> 7d past due), expired short-term learning events (> 30d),
      and wipes expired submission photo references (> 7d).
    - NEVER auto-expires verified GrowthRecord entries.
    """
    expired_asg = db.execute(delete(Assignment).where(Assignment.expires_at <= now)).rowcount or 0
    expired_ev = db.execute(delete(LearningEvent).where(LearningEvent.expires_at <= now)).rowcount or 0

    photo_rows = list(
        db.scalars(
            select(SubmissionEvidence).where(
                SubmissionEvidence.photo_object_key.is_not(None),
                SubmissionEvidence.photo_expires_at <= now,
            )
        )
    )
    for row in photo_rows:
        row.photo_object_key = None
        row.photo_expires_at = None

    db.commit()
    return {
        "expired_assignments": expired_asg,
        "expired_learning_events": expired_ev,
        "purged_photos": len(photo_rows),
    }


def revoke_camera_consent_and_purge_photos(db: Session, *, family_id: str, student_id: str) -> int:
    student = db.get(Student, student_id)
    if student is None or student.family_id != family_id:
        raise PermissionError("student_outside_family")
    student.camera_consent = False
    rows = list(
        db.scalars(
            select(SubmissionEvidence).where(
                SubmissionEvidence.student_id == student_id,
                SubmissionEvidence.photo_object_key.is_not(None),
            )
        )
    )
    for r in rows:
        r.photo_object_key = None
        r.photo_expires_at = None
    db.commit()
    return len(rows)


def delete_growth_record_and_revoke_cards(db: Session, *, family_id: str, student_id: str, growth_id: str) -> int:
    """Deleting a growth record also revokes any method card whose mastery proof depended on it."""
    rec = db.get(GrowthRecord, growth_id)
    if rec is None or rec.family_id != family_id or rec.student_id != student_id:
        raise PermissionError("growth_outside_student_scope")
    rec.deleted = True
    cards = list(
        db.scalars(
            select(MethodCardUnlock).where(
                MethodCardUnlock.student_id == student_id,
                MethodCardUnlock.growth_record_id == growth_id,
                MethodCardUnlock.active.is_(True),
            )
        )
    )
    for c in cards:
        c.active = False
    db.commit()
    return len(cards)
