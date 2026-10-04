"""Hybrid retrieval: (dense + sparse) x N sub-queries fanned out concurrently with asyncio,
fused with Reciprocal Rank Fusion, re-ranked, and diversified (max chunks per document)."""

import asyncio
from collections import defaultdict

from app.infra.embeddings import Embedder, SparseEncoder
from app.infra.graphstore import GraphStore
from app.infra.vectorstore import VectorStore
from app.schemas.domain import ScoredChunk, Triple
from app.services.text import overlap


def reciprocal_rank_fusion(result_lists: list[list[ScoredChunk]], k: int = 60) -> list[ScoredChunk]:
    scores: dict[str, float] = defaultdict(float)
    by_id: dict[str, ScoredChunk] = {}
    for results in result_lists:
        for rank, sc in enumerate(results):
            scores[sc.chunk.chunk_id] += 1.0 / (k + rank + 1)
            by_id.setdefault(sc.chunk.chunk_id, sc)
    fused = [ScoredChunk(by_id[cid].chunk, s, "fused") for cid, s in scores.items()]
    return sorted(fused, key=lambda s: s.score, reverse=True)


class HybridRetriever:
    def __init__(self, embedder: Embedder, sparse: SparseEncoder, vectors: VectorStore, graph: GraphStore) -> None:
        self.embedder, self.sparse, self.vectors, self.graph = embedder, sparse, vectors, graph

    async def _search_one(self, tenant_id: str, query: str, k: int) -> list[list[ScoredChunk]]:
        vec = await self.embedder.embed_query(query)
        dense, sparse = await asyncio.gather(
            self.vectors.search_dense(tenant_id, vec, k),
            self.vectors.search_sparse(tenant_id, self.sparse.encode(query, query=True), k),
        )
        return [dense, sparse]

    async def retrieve(self, tenant_id: str, question: str, queries: list[str], candidates: int,
                       top_k: int, max_per_doc: int = 2) -> list[ScoredChunk]:
        per_query = await asyncio.gather(*(self._search_one(tenant_id, q, candidates) for q in queries))
        fused = reciprocal_rank_fusion([lst for pair in per_query for lst in pair])

        # lightweight rerank: blend normalized RRF with lexical coverage of the *original* question
        # (swap in a cross-encoder / LLM reranker behind this function for higher precision)
        if fused:
            top = fused[0].score
            for sc in fused:
                cover = max(overlap(question, sc.chunk.text), max((overlap(q, sc.chunk.text) for q in queries),
                                                                  default=0.0))
                sc.score = round(0.6 * (sc.score / top) + 0.4 * cover, 4)
            fused.sort(key=lambda s: s.score, reverse=True)

        per_doc: dict[str, int] = defaultdict(int)
        out: list[ScoredChunk] = []
        for sc in fused:
            if per_doc[sc.chunk.doc_id] >= max_per_doc:
                continue
            per_doc[sc.chunk.doc_id] += 1
            out.append(sc)
            if len(out) >= top_k:
                break
        return out

    async def graph_facts(self, tenant_id: str, question: str, entities: list[str], hops: int,
                          limit: int) -> tuple[list[str], list[Triple]]:
        linked = await self.graph.link_entities(tenant_id, " ".join([question, *entities]))
        if not linked:
            return [], []
        return linked, await self.graph.neighborhood(tenant_id, linked, hops, limit)
