from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Optional, Protocol
import uuid


@dataclass(frozen=True)
class GraphitiHit:
    episode_uuid: str
    group_id: str
    body: str
    source_description: str  # Format: "mem:<memory_id>:v<version>"


class GraphitiStore(Protocol):
    def add_episode(self, *, group_id: str, name: str, episode_body: str, source_description: str) -> str: ...
    def search(self, *, query: str, group_ids: list[str], limit: int = 5) -> list[GraphitiHit]: ...
    def remove_episode(self, *, episode_uuid: str) -> None: ...
    def list_group_episode_uuids(self, *, group_id: str) -> set[str]: ...
    def purge_group(self, *, group_id: str) -> None: ...


class GraphitiUnavailableError(RuntimeError):
    """Raised when Neo4j / Graphiti is temporarily unreachable."""


def _extract_terms(text: str) -> list[str]:
    """Extract lightweight Chinese/ASCII concept terms for graph node linking & hybrid recall."""
    cleaned = re.sub(r"[：:，,。！？!?\n\r\t]+", " ", text or "")
    raw_tokens = [tok.strip() for tok in cleaned.split() if len(tok.strip()) >= 2]
    # Deduplicate preserving order
    seen: set[str] = set()
    out: list[str] = []
    for tok in raw_tokens:
        if tok not in seen:
            seen.add(tok)
            out.append(tok[:48])
    return out[:16]


class InMemoryGraphitiStore:
    """
    Deterministic local stand-in for graphiti-core + Neo4j 5 used in integration tests
    and offline dev. Enforces exact group_id isolation, remove_episode cascade, and
    simulated outage mode for degradation testing.
    """

    def __init__(self) -> None:
        self._episodes: dict[str, GraphitiHit] = {}
        self.available: bool = True

    def _ensure_up(self) -> None:
        if not self.available:
            raise GraphitiUnavailableError("graphiti_unavailable")

    def add_episode(self, *, group_id: str, name: str, episode_body: str, source_description: str) -> str:
        self._ensure_up()
        ep_uuid = f"ep_{uuid.uuid4().hex[:12]}"
        self._episodes[ep_uuid] = GraphitiHit(
            episode_uuid=ep_uuid,
            group_id=group_id,
            body=episode_body,
            source_description=source_description,
        )
        return ep_uuid

    def search(self, *, query: str, group_ids: list[str], limit: int = 5) -> list[GraphitiHit]:
        self._ensure_up()
        allowed = set(group_ids)
        q_tokens = [tok for tok in query.replace("：", " ").split() if tok]
        scored: list[tuple[int, GraphitiHit]] = []
        for hit in self._episodes.values():
            if hit.group_id not in allowed:
                continue
            score = sum(1 for tok in q_tokens if tok in hit.body)
            scored.append((score, hit))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [hit for _, hit in scored[:limit]]

    def remove_episode(self, *, episode_uuid: str) -> None:
        self._ensure_up()
        self._episodes.pop(episode_uuid, None)

    def list_group_episode_uuids(self, *, group_id: str) -> set[str]:
        self._ensure_up()
        return {ep.episode_uuid for ep in self._episodes.values() if ep.group_id == group_id}

    def purge_group(self, *, group_id: str) -> None:
        self._ensure_up()
        to_del = [k for k, v in self._episodes.items() if v.group_id == group_id]
        for k in to_del:
            del self._episodes[k]


class Neo4jGraphitiStore:
    """
    Production Neo4j 5 Graphiti store enforcing strict per-student `group_id` isolation:
      - (:YamiEpisode {episode_uuid, group_id, name, body, source_description, created_at})
      - (:YamiConcept {group_id, term})<-[:MENTIONS]-(:YamiEpisode)
      - Any Neo4j connectivity failure raises GraphitiUnavailableError so callers degrade
        gracefully to PostgreSQL structured queries without interrupting the child.
    """

    def __init__(self, *, uri: str, user: str, password: str, database: str = "neo4j") -> None:
        try:
            from neo4j import GraphDatabase  # type: ignore[import-not-found]
        except ImportError as exc:
            raise GraphitiUnavailableError("neo4j_driver_not_installed") from exc
        self._uri = uri
        self._database = database
        self._driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=3.0)
        self._schema_ready = False

    def close(self) -> None:
        self._driver.close()

    def verify_connectivity(self) -> None:
        try:
            self._driver.verify_connectivity()
        except Exception as exc:
            raise GraphitiUnavailableError(f"neo4j_unreachable: {exc}") from exc

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        try:
            with self._driver.session(database=self._database) as sess:
                sess.run(
                    "CREATE CONSTRAINT yami_ep_uuid_uq IF NOT EXISTS "
                    "FOR (e:YamiEpisode) REQUIRE e.episode_uuid IS UNIQUE"
                )
                sess.run(
                    "CREATE INDEX yami_ep_group_idx IF NOT EXISTS "
                    "FOR (e:YamiEpisode) ON (e.group_id)"
                )
                sess.run(
                    "CREATE INDEX yami_concept_group_term_idx IF NOT EXISTS "
                    "FOR (c:YamiConcept) ON (c.group_id, c.term)"
                )
            self._schema_ready = True
        except Exception as exc:
            raise GraphitiUnavailableError(f"neo4j_schema_error: {exc}") from exc

    def add_episode(self, *, group_id: str, name: str, episode_body: str, source_description: str) -> str:
        self._ensure_schema()
        ep_uuid = f"ep_{uuid.uuid4().hex[:12]}"
        terms = _extract_terms(episode_body)
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with self._driver.session(database=self._database) as sess:
                sess.run(
                    """
                    CREATE (e:YamiEpisode {
                        episode_uuid: $ep_uuid,
                        group_id: $group_id,
                        name: $name,
                        body: $body,
                        source_description: $source_description,
                        created_at: $created_at
                    })
                    WITH e
                    UNWIND $terms AS term
                    MERGE (c:YamiConcept {group_id: $group_id, term: term})
                    MERGE (e)-[:MENTIONS]->(c)
                    """,
                    ep_uuid=ep_uuid,
                    group_id=group_id,
                    name=name,
                    body=episode_body,
                    source_description=source_description,
                    created_at=now_iso,
                    terms=terms or ["general"],
                )
            return ep_uuid
        except Exception as exc:
            raise GraphitiUnavailableError(f"neo4j_add_episode_failed: {exc}") from exc

    def search(self, *, query: str, group_ids: list[str], limit: int = 5) -> list[GraphitiHit]:
        if not group_ids:
            return []
        self._ensure_schema()
        q_tokens = [tok for tok in query.replace("：", " ").split() if tok]
        try:
            with self._driver.session(database=self._database) as sess:
                rows = sess.run(
                    """
                    MATCH (e:YamiEpisode)
                    WHERE e.group_id IN $group_ids
                    OPTIONAL MATCH (e)-[:MENTIONS]->(c:YamiConcept)
                    WHERE c.group_id = e.group_id
                    WITH e, collect(c.term) AS concepts
                    WITH e,
                         reduce(s = 0, tok IN $tokens |
                            s + CASE WHEN e.body CONTAINS tok THEN 2 ELSE 0 END
                              + CASE WHEN any(ct IN concepts WHERE ct CONTAINS tok OR tok CONTAINS ct) THEN 1 ELSE 0 END
                         ) AS score
                    RETURN e.episode_uuid AS episode_uuid,
                           e.group_id AS group_id,
                           e.body AS body,
                           e.source_description AS source_description,
                           score
                    ORDER BY score DESC, e.created_at DESC
                    LIMIT $limit
                    """,
                    group_ids=group_ids,
                    tokens=q_tokens,
                    limit=limit,
                )
                return [
                    GraphitiHit(
                        episode_uuid=r["episode_uuid"],
                        group_id=r["group_id"],
                        body=r["body"],
                        source_description=r["source_description"],
                    )
                    for r in rows
                ]
        except Exception as exc:
            raise GraphitiUnavailableError(f"neo4j_search_failed: {exc}") from exc

    def remove_episode(self, *, episode_uuid: str) -> None:
        self._ensure_schema()
        try:
            with self._driver.session(database=self._database) as sess:
                sess.run(
                    """
                    MATCH (e:YamiEpisode {episode_uuid: $ep_uuid})
                    OPTIONAL MATCH (e)-[:MENTIONS]->(c:YamiConcept)
                    DETACH DELETE e
                    WITH c
                    WHERE c IS NOT NULL AND NOT (c)<-[:MENTIONS]-()
                    DELETE c
                    """,
                    ep_uuid=episode_uuid,
                )
        except Exception as exc:
            raise GraphitiUnavailableError(f"neo4j_remove_episode_failed: {exc}") from exc

    def list_group_episode_uuids(self, *, group_id: str) -> set[str]:
        self._ensure_schema()
        try:
            with self._driver.session(database=self._database) as sess:
                rows = sess.run(
                    "MATCH (e:YamiEpisode {group_id: $group_id}) RETURN e.episode_uuid AS ep_uuid",
                    group_id=group_id,
                )
                return {r["ep_uuid"] for r in rows}
        except Exception as exc:
            raise GraphitiUnavailableError(f"neo4j_list_group_failed: {exc}") from exc

    def purge_group(self, *, group_id: str) -> None:
        self._ensure_schema()
        try:
            with self._driver.session(database=self._database) as sess:
                sess.run(
                    """
                    MATCH (e:YamiEpisode {group_id: $group_id})
                    DETACH DELETE e
                    """,
                    group_id=group_id,
                )
                sess.run(
                    """
                    MATCH (c:YamiConcept {group_id: $group_id})
                    DETACH DELETE c
                    """,
                    group_id=group_id,
                )
        except Exception as exc:
            raise GraphitiUnavailableError(f"neo4j_purge_group_failed: {exc}") from exc


def create_graphiti_store(
    *,
    uri: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    prefer_neo4j: bool = True,
) -> GraphitiStore:
    """Create a Neo4jGraphitiStore if Neo4j is configured & reachable; else InMemoryGraphitiStore."""
    if prefer_neo4j and uri:
        try:
            store = Neo4jGraphitiStore(
                uri=uri,
                user=user or "neo4j",
                password=password or "",
            )
            store.verify_connectivity()
            return store
        except Exception:
            pass
    return InMemoryGraphitiStore()
