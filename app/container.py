"""Composition root: builds and owns every long-lived dependency (DI without a framework)."""

import contextlib
from dataclasses import dataclass

from app.agents.components import Generator, Grader, Planner, Rewriter, Verifier
from app.agents.graph import build_agent
from app.agents.service import AgentService
from app.core.config import Settings
from app.infra.cache import Cache, build_cache
from app.infra.embeddings import SparseEncoder, build_embedder
from app.infra.graphstore import GraphStore, build_graph_store
from app.infra.vectorstore import VectorStore, build_vector_store
from app.llm.client import build_llm
from app.services.extraction import TripleExtractor
from app.services.ingestion import IngestionService
from app.services.retrieval import HybridRetriever


@dataclass
class Container:
    settings: Settings
    vectors: VectorStore
    graph: GraphStore
    cache: Cache
    ingestion: IngestionService
    retriever: HybridRetriever
    agent: AgentService

    @classmethod
    def build(cls, settings: Settings) -> "Container":
        llm = build_llm(settings)
        embedder = build_embedder(settings)
        sparse = SparseEncoder()
        vectors, graph, cache = build_vector_store(settings), build_graph_store(settings), build_cache(settings)
        retriever = HybridRetriever(embedder, sparse, vectors, graph)
        graph_app = build_agent(
            settings, retriever, Planner(llm), Grader(llm, settings.relevance_threshold), Rewriter(llm),
            Generator(llm), Verifier(llm),
        )
        return cls(
            settings=settings, vectors=vectors, graph=graph, cache=cache,
            ingestion=IngestionService(settings, embedder, sparse, vectors, graph, TripleExtractor(llm)),
            retriever=retriever, agent=AgentService(settings, graph_app, cache),
        )

    async def start(self) -> None:
        await self.vectors.ensure()
        await self.graph.ensure()

    async def stop(self) -> None:
        for closable in (self.vectors, self.graph, self.cache):
            with contextlib.suppress(Exception):  # best-effort shutdown
                await closable.close()

    async def health(self) -> dict[str, str]:
        return {
            "vector_store": "ok" if await self.vectors.ping() else "down",
            "graph_store": "ok" if await self.graph.ping() else "down",
            "cache": "ok" if await self.cache.ping() else "down",
        }
