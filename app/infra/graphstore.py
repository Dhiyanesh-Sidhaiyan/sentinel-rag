"""Knowledge graph store (subject -predicate-> object triples with provenance).

PostgresGraphStore keeps triples in a single indexed table and does k-hop traversal with a recursive CTE,
which scales to millions of edges without a separate graph database. InMemoryGraphStore mirrors it.
"""

import re
from collections import defaultdict
from typing import Any, Protocol

from app.core.config import Settings
from app.schemas.domain import Triple


def norm_entity(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


class GraphStore(Protocol):
    async def ensure(self) -> None: ...
    async def add_triples(self, tenant_id: str, triples: list[Triple]) -> int: ...
    async def link_entities(self, tenant_id: str, text: str, limit: int = 10) -> list[str]: ...
    async def neighborhood(self, tenant_id: str, entities: list[str], hops: int, limit: int) -> list[Triple]: ...
    async def delete_document(self, tenant_id: str, doc_id: str) -> None: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


def _mentions(entity: str, text: str) -> bool:
    return len(entity) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(entity)}(?![a-z0-9])", text) is not None


class InMemoryGraphStore:
    def __init__(self) -> None:
        self._triples: dict[str, set[Triple]] = defaultdict(set)

    async def ensure(self) -> None:
        return None

    async def add_triples(self, tenant_id: str, triples: list[Triple]) -> int:
        before = len(self._triples[tenant_id])
        self._triples[tenant_id].update(triples)
        return len(self._triples[tenant_id]) - before

    async def link_entities(self, tenant_id: str, text: str, limit: int = 10) -> list[str]:
        t = norm_entity(text)
        names = {norm_entity(x) for tr in self._triples[tenant_id] for x in (tr.subject, tr.object)}
        found = sorted((n for n in names if _mentions(n, t)), key=len, reverse=True)
        # drop entities fully contained in a longer matched entity ("api" inside "payments api")
        kept = [n for i, n in enumerate(found) if not any(n in longer for longer in found[:i])]
        return kept[:limit]

    async def neighborhood(self, tenant_id: str, entities: list[str], hops: int, limit: int) -> list[Triple]:
        frontier = {norm_entity(e) for e in entities}
        seen_nodes = set(frontier)
        result: list[Triple] = []
        seen: set[tuple[str, str, str]] = set()
        triples = self._triples[tenant_id]
        for _ in range(hops):
            nxt: set[str] = set()
            for tr in sorted(triples, key=lambda x: (x.subject, x.predicate, x.object)):
                s, o = norm_entity(tr.subject), norm_entity(tr.object)
                if s in frontier or o in frontier:
                    key = (s, tr.predicate, o)
                    if key not in seen:
                        seen.add(key)
                        result.append(tr)
                    nxt.update({s, o} - seen_nodes)
            seen_nodes |= nxt
            frontier = nxt
            if not frontier:
                break
        return result[:limit]

    async def delete_document(self, tenant_id: str, doc_id: str) -> None:
        self._triples[tenant_id] = {t for t in self._triples[tenant_id] if t.doc_id != doc_id}

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_triples (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    subject      TEXT NOT NULL,
    subject_norm TEXT NOT NULL,
    predicate    TEXT NOT NULL,
    object       TEXT NOT NULL,
    object_norm  TEXT NOT NULL,
    doc_id       TEXT NOT NULL,
    chunk_id     TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, subject_norm, predicate, object_norm, chunk_id)
);
CREATE INDEX IF NOT EXISTS kg_subject_idx ON kg_triples (tenant_id, subject_norm);
CREATE INDEX IF NOT EXISTS kg_object_idx  ON kg_triples (tenant_id, object_norm);
CREATE INDEX IF NOT EXISTS kg_doc_idx     ON kg_triples (tenant_id, doc_id);
"""

_NEIGHBORHOOD_SQL = """
WITH RECURSIVE walk(node, depth) AS (
    SELECT unnest($2::text[]), 0
    UNION
    SELECT CASE WHEN t.subject_norm = w.node THEN t.object_norm ELSE t.subject_norm END, w.depth + 1
    FROM kg_triples t
    JOIN walk w ON t.subject_norm = w.node OR t.object_norm = w.node
    WHERE t.tenant_id = $1 AND w.depth < $3 - 1
),
nodes AS (SELECT node, min(depth) AS d FROM walk GROUP BY node)
SELECT t.subject, t.predicate, t.object, min(t.doc_id) AS doc_id, min(t.chunk_id) AS chunk_id, min(n.d) AS d
FROM kg_triples t
JOIN nodes n ON t.subject_norm = n.node OR t.object_norm = n.node
WHERE t.tenant_id = $1
GROUP BY t.subject, t.predicate, t.object
ORDER BY d, t.subject, t.predicate, t.object
LIMIT $4
"""


class PostgresGraphStore:
    def __init__(self, settings: Settings) -> None:
        assert settings.postgres_dsn is not None
        self._dsn = settings.postgres_dsn.get_secret_value()
        self._pool_max = settings.postgres_pool_max
        self._pool: Any = None  # asyncpg.Pool (imported lazily)

    async def ensure(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=self._pool_max, command_timeout=10)
        async with self._pool.acquire() as conn:
            # advisory lock so concurrent pods don't race on DDL
            await conn.execute("SELECT pg_advisory_lock(424242)")
            try:
                await conn.execute(_SCHEMA)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(424242)")

    @property
    def pool(self):
        if self._pool is None:
            raise RuntimeError("PostgresGraphStore.ensure() was not called")
        return self._pool

    async def add_triples(self, tenant_id: str, triples: list[Triple]) -> int:
        if not triples:
            return 0
        rows = [
            (tenant_id, t.subject, norm_entity(t.subject), t.predicate, t.object, norm_entity(t.object),
             t.doc_id, t.chunk_id)
            for t in triples
        ]
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO kg_triples (tenant_id, subject, subject_norm, predicate, object, object_norm,
                   doc_id, chunk_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING""",
                rows,
            )
        return len(rows)

    async def link_entities(self, tenant_id: str, text: str, limit: int = 10) -> list[str]:
        t = norm_entity(text)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT DISTINCT e FROM (
                       SELECT subject_norm AS e FROM kg_triples WHERE tenant_id = $1
                       UNION SELECT object_norm FROM kg_triples WHERE tenant_id = $1) s
                   WHERE length(e) >= 3 AND position(e IN $2) > 0
                   ORDER BY e LIMIT 200""",
                tenant_id, t,
            )
        found = sorted((r["e"] for r in rows if _mentions(r["e"], t)), key=len, reverse=True)
        kept = [n for i, n in enumerate(found) if not any(n in longer for longer in found[:i])]
        return kept[:limit]

    async def neighborhood(self, tenant_id: str, entities: list[str], hops: int, limit: int) -> list[Triple]:
        if not entities:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(_NEIGHBORHOOD_SQL, tenant_id, [norm_entity(e) for e in entities], hops, limit)
        return [Triple(r["subject"], r["predicate"], r["object"], r["doc_id"], r["chunk_id"]) for r in rows]

    async def delete_document(self, tenant_id: str, doc_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM kg_triples WHERE tenant_id = $1 AND doc_id = $2", tenant_id, doc_id)

    async def ping(self) -> bool:
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetchval("SELECT 1") == 1
        except Exception:
            return False

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()


def build_graph_store(settings: Settings) -> GraphStore:
    return PostgresGraphStore(settings) if settings.graph_backend == "postgres" else InMemoryGraphStore()
