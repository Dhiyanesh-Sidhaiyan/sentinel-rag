"""Vector store with named dense + sparse vectors and strict tenant isolation.

QdrantVectorStore is the production backend; InMemoryVectorStore mirrors its semantics for tests/local.
Every read and delete is filtered by tenant_id -- there is no code path that queries across tenants.
"""

import math
import uuid
from collections import Counter
from typing import Protocol

from app.core.config import Settings
from app.schemas.domain import Chunk, ScoredChunk, SparseVector

_NS = uuid.UUID("6f1c2a8e-3b7d-4e0a-9c55-1d2e3f4a5b6c")


def point_id(tenant_id: str, chunk_id: str) -> str:
    return str(uuid.uuid5(_NS, f"{tenant_id}:{chunk_id}"))


class VectorStore(Protocol):
    async def ensure(self) -> None: ...
    async def upsert(self, chunks: list[Chunk], dense: list[list[float]], sparse: list[SparseVector]) -> None: ...
    async def search_dense(self, tenant_id: str, vector: list[float], k: int) -> list[ScoredChunk]: ...
    async def search_sparse(self, tenant_id: str, vector: SparseVector, k: int) -> list[ScoredChunk]: ...
    async def delete_document(self, tenant_id: str, doc_id: str) -> None: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


def _chunk_from_payload(p: dict) -> Chunk:
    return Chunk(
        chunk_id=p["chunk_id"], tenant_id=p["tenant_id"], doc_id=p["doc_id"], chunk_index=p["chunk_index"],
        title=p["title"], section=p.get("section"), text=p["text"], source=p.get("source"),
        metadata=p.get("metadata", {}),
    )


def _payload(c: Chunk) -> dict:
    return {
        "chunk_id": c.chunk_id, "tenant_id": c.tenant_id, "doc_id": c.doc_id, "chunk_index": c.chunk_index,
        "title": c.title, "section": c.section, "text": c.text, "source": c.source, "metadata": c.metadata,
    }


class InMemoryVectorStore:
    def __init__(self) -> None:
        self._rows: dict[str, tuple[Chunk, list[float], SparseVector]] = {}

    async def ensure(self) -> None:
        return None

    async def upsert(self, chunks: list[Chunk], dense: list[list[float]], sparse: list[SparseVector]) -> None:
        for c, d, s in zip(chunks, dense, sparse, strict=True):
            self._rows[point_id(c.tenant_id, c.chunk_id)] = (c, d, s)

    def _tenant_rows(self, tenant_id: str) -> list[tuple[Chunk, list[float], SparseVector]]:
        return [r for r in self._rows.values() if r[0].tenant_id == tenant_id]

    async def search_dense(self, tenant_id: str, vector: list[float], k: int) -> list[ScoredChunk]:
        scored = [
            ScoredChunk(c, sum(a * b for a, b in zip(vector, d, strict=True)), "dense")
            for c, d, _ in self._tenant_rows(tenant_id)
        ]
        return sorted(scored, key=lambda s: s.score, reverse=True)[:k]

    async def search_sparse(self, tenant_id: str, vector: SparseVector, k: int) -> list[ScoredChunk]:
        rows = self._tenant_rows(tenant_id)
        df: Counter[int] = Counter(i for _, _, s in rows for i in set(s.indices))
        n = len(rows)
        q = dict(zip(vector.indices, vector.values, strict=True))
        scored = []
        for c, _, s in rows:
            score = sum(
                q[i] * v * math.log(1 + (n - df[i] + 0.5) / (df[i] + 0.5))
                for i, v in zip(s.indices, s.values, strict=True) if i in q
            )
            if score > 0:
                scored.append(ScoredChunk(c, score, "sparse"))
        return sorted(scored, key=lambda s: s.score, reverse=True)[:k]

    async def delete_document(self, tenant_id: str, doc_id: str) -> None:
        for key in [k for k, (c, _, _) in self._rows.items() if c.tenant_id == tenant_id and c.doc_id == doc_id]:
            del self._rows[key]

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class QdrantVectorStore:
    def __init__(self, settings: Settings) -> None:
        from qdrant_client import AsyncQdrantClient, models

        self._m = models
        self._collection = settings.qdrant_collection
        self._dim = settings.embedding_dim
        self._client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None,
            timeout=10,
        )

    def _tenant_filter(self, tenant_id: str, doc_id: str | None = None):
        m = self._m
        must = [m.FieldCondition(key="tenant_id", match=m.MatchValue(value=tenant_id))]
        if doc_id:
            must.append(m.FieldCondition(key="doc_id", match=m.MatchValue(value=doc_id)))
        return m.Filter(must=must)

    async def ensure(self) -> None:
        m = self._m
        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config={"dense": m.VectorParams(size=self._dim, distance=m.Distance.COSINE)},
                sparse_vectors_config={"sparse": m.SparseVectorParams(modifier=m.Modifier.IDF)},
                hnsw_config=m.HnswConfigDiff(payload_m=16, m=0),  # tenant-partitioned HNSW graphs
            )
            await self._client.create_payload_index(
                self._collection, "tenant_id",
                field_schema=m.KeywordIndexParams(type=m.KeywordIndexType.KEYWORD, is_tenant=True),
            )
            await self._client.create_payload_index(
                self._collection, "doc_id", field_schema=m.PayloadSchemaType.KEYWORD
            )

    async def upsert(self, chunks: list[Chunk], dense: list[list[float]], sparse: list[SparseVector]) -> None:
        m = self._m
        points = [
            m.PointStruct(
                id=point_id(c.tenant_id, c.chunk_id),
                vector={"dense": d, "sparse": m.SparseVector(indices=s.indices, values=s.values)},
                payload=_payload(c),
            )
            for c, d, s in zip(chunks, dense, sparse, strict=True)
        ]
        for i in range(0, len(points), 128):
            await self._client.upsert(self._collection, points=points[i : i + 128], wait=True)

    async def _query(self, tenant_id: str, query, using: str, k: int) -> list[ScoredChunk]:
        res = await self._client.query_points(
            collection_name=self._collection, query=query, using=using,
            query_filter=self._tenant_filter(tenant_id), limit=k, with_payload=True,
        )
        return [ScoredChunk(_chunk_from_payload(p.payload or {}), p.score, using) for p in res.points]

    async def search_dense(self, tenant_id: str, vector: list[float], k: int) -> list[ScoredChunk]:
        return await self._query(tenant_id, vector, "dense", k)

    async def search_sparse(self, tenant_id: str, vector: SparseVector, k: int) -> list[ScoredChunk]:
        if not vector.indices:
            return []
        sparse = self._m.SparseVector(indices=vector.indices, values=vector.values)
        return await self._query(tenant_id, sparse, "sparse", k)

    async def delete_document(self, tenant_id: str, doc_id: str) -> None:
        await self._client.delete(
            self._collection, points_selector=self._m.FilterSelector(filter=self._tenant_filter(tenant_id, doc_id)),
            wait=True,
        )

    async def ping(self) -> bool:
        try:
            await self._client.get_collections()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.close()


def build_vector_store(settings: Settings) -> VectorStore:
    return QdrantVectorStore(settings) if settings.vector_backend == "qdrant" else InMemoryVectorStore()
