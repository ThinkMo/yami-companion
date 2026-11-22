from __future__ import annotations

from backend.memory.graphiti_client import InMemoryGraphitiStore
from backend.memory.service import (
    delete_learner_memory,
    process_memory_outbox,
    rebuild_student_graph,
    recall_for_turn,
    reconcile_graphiti,
    write_learner_memory,
)


def test_cross_session_graphiti_recall_isolation_forgetting_and_fallback(db_session):
    graphiti = InMemoryGraphitiStore()

    # 1. Night 1: Record 小禾's preference + learning strategy, project to Graphiti
    pref = write_learner_memory(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        kind="preference",
        summary="以后有数学就先做数学",
    )
    strat = write_learner_memory(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        kind="strategy",
        topic_id="unit_fraction_compare",
        summary="画两个一样大的整体后能想明白单位分数",
    )
    projected = process_memory_outbox(db_session, graphiti=graphiti)
    assert projected == 2

    # 2. Night 2 (after device restart): 小禾 recalls both via Graphiti (re-verified against PG)
    he_recalled = recall_for_turn(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        intent="help",
        topic_id="unit_fraction_compare",
        graphiti=graphiti,
    )
    assert len(he_recalled) == 2
    assert all(r.source == "graphiti_verified" for r in he_recalled)

    # 3. Independent review first attempt MUST hide strategy spoilers (but preference is fine)
    review_recalled = recall_for_turn(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        intent="review",
        topic_id="unit_fraction_compare",
        graphiti=graphiti,
        is_independent_review_first_attempt=True,
    )
    assert all(r.kind != "strategy" for r in review_recalled)

    # 4. Sibling 乐乐's recall is completely isolated
    le_recalled = recall_for_turn(
        db_session,
        family_id="fam_a",
        student_id="stu_le",
        intent="help",
        topic_id="unit_fraction_compare",
        graphiti=graphiti,
    )
    assert le_recalled == []

    # 5. Update preference -> old version filtered out, new version recalled
    write_learner_memory(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        kind="preference",
        summary="现在想先做语文再做数学",
        replace_memory_id=pref.id,
    )
    process_memory_outbox(db_session, graphiti=graphiti)
    updated = recall_for_turn(db_session, family_id="fam_a", student_id="stu_he", intent="start", graphiti=graphiti)
    assert any("先做语文" in r.summary for r in updated)
    assert not any("以后有数学就先做数学" in r.summary for r in updated)

    # 6. Delete strategy -> immediately gone from PG + Graphiti
    delete_learner_memory(db_session, family_id="fam_a", student_id="stu_he", memory_id=strat.id, graphiti=graphiti)
    after_del = recall_for_turn(
        db_session, family_id="fam_a", student_id="stu_he", intent="help", topic_id="unit_fraction_compare", graphiti=graphiti
    )
    assert not any(r.memory_id == strat.id for r in after_del)

    # 7. Graphiti outage degrades cleanly to PG structured query
    graphiti.available = False
    degraded = recall_for_turn(db_session, family_id="fam_a", student_id="stu_he", intent="start", graphiti=graphiti)
    assert len(degraded) == 1 and degraded[0].source == "pg_structured"

    # 8. Recovery: daily reconciliation purges orphan episodes, and full rebuild restores graph from PG
    graphiti.available = True
    graphiti.add_episode(
        group_id="stu_grp_he_01",
        name="orphan",
        episode_body="stale orphan",
        source_description="mem:ghost:v1",
    )
    purged = reconcile_graphiti(db_session, family_id="fam_a", student_id="stu_he", graphiti=graphiti)
    assert purged == 1

    rebuilt = rebuild_student_graph(db_session, family_id="fam_a", student_id="stu_he", graphiti=graphiti)
    assert rebuilt == 1
