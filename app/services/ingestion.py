"""Ingestion pipeline: sanitize -> chunk -> indirect-injection quarantine -> embed (dense+sparse)
-> upsert vectors -> extract KG triples. Idempotent: re-ingesting a doc_id replaces it atomically per doc."""

import asyncio
import hashlib
import time

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import GUARDRAIL_EVENTS, INGESTED_CHUNKS
from app.guardrails import injection, pii
from app.infra.embeddings import Embedder, SparseEncoder
from app.infra.graphstore import GraphStore
from app.infra.vectorstore import VectorStore
from app.schemas.api import DocumentIn, DocumentIngestResult, IngestResponse
from app.schemas.domain import Chunk, Triple
from app.services.chunking import chunk_text
from app.services.extraction import TripleExtractor
from app.services.text import normalize

log = get_logger(__name__)


class IngestionService:
    def __init__(
        self, settings: Settings, embedder: Embedder, sparse: SparseEncoder,
        vectors: VectorStore, graph: GraphStore, extractor: TripleExtractor,
    ) -> None:
        self.s = settings
        self.embedder, self.sparse, self.vectors, self.graph, self.extractor = (
            embedder, sparse, vectors, graph, extractor
        )
        self._extract_sem = asyncio.Semaphore(8)

    async def ingest(self, tenant_id: str, docs: list[DocumentIn]) -> IngestResponse:
        start = time.perf_counter()
        results = await asyncio.gather(*(self._ingest_one(tenant_id, d) for d in docs))
        return IngestResponse(
            tenant_id=tenant_id,
            documents=list(results),
            total_chunks=sum(r.chunks_indexed for r in results),
            total_triples=sum(r.triples_extracted for r in results),
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
        )

    async def _ingest_one(self, tenant_id: str, doc: DocumentIn) -> DocumentIngestResult:
        warnings: list[str] = []
        if len(doc.text) > self.s.max_document_chars:
            raise ValueError(f"document {doc.doc_id} exceeds {self.s.max_document_chars} characters")

        red = pii.redact(normalize(doc.text))
        if red.counts:
            warnings.append(f"redacted sensitive values: {', '.join(red.types)}")

        chunks: list[Chunk] = []
        quarantined = 0
        for tc in chunk_text(red.text, self.s.chunk_size, self.s.chunk_overlap):
            verdict = injection.scan(tc.text)
            if verdict.blocked(self.s.injection_block_threshold):
                quarantined += 1
                GUARDRAIL_EVENTS.labels("ingest", "indirect_injection").inc()
                INGESTED_CHUNKS.labels("quarantined").inc()
                warnings.append(f"chunk {tc.index} quarantined (indirect prompt injection: {','.join(verdict.flags)})")
                continue
            digest = hashlib.sha256(tc.text.encode()).hexdigest()[:12]
            chunks.append(Chunk(
                chunk_id=f"{doc.doc_id}#{tc.index}-{digest}", tenant_id=tenant_id, doc_id=doc.doc_id,
                chunk_index=tc.index, title=doc.title, section=tc.section, text=tc.text,
                source=doc.source, metadata=doc.metadata,
            ))

        # embed with the section title prepended (contextual chunk headers improve recall)
        texts = [f"{c.title} | {c.section or ''}\n{c.text}" for c in chunks]
        dense = await self.embedder.embed_documents(texts) if texts else []
        sparse = [self.sparse.encode(t) for t in texts]
        triple_lists = await asyncio.gather(*(self._extract(c) for c in chunks))
        triples = [t for ts in triple_lists for t in ts]

        await self.vectors.delete_document(tenant_id, doc.doc_id)
        await self.graph.delete_document(tenant_id, doc.doc_id)
        if chunks:
            await self.vectors.upsert(chunks, dense, sparse)
        await self.graph.add_triples(tenant_id, triples)
        INGESTED_CHUNKS.labels("indexed").inc(len(chunks))
        log.info("document_ingested", doc_id=doc.doc_id, chunks=len(chunks), quarantined=quarantined,
                 triples=len(triples))
        return DocumentIngestResult(
            doc_id=doc.doc_id, chunks_indexed=len(chunks), chunks_quarantined=quarantined,
            triples_extracted=len(triples), pii_redactions=red.counts, warnings=warnings,
        )

    async def _extract(self, chunk: Chunk) -> list[Triple]:
        async with self._extract_sem:
            raw = await self.extractor.extract(chunk.text)
        return [Triple(s, p, o, chunk.doc_id, chunk.chunk_id) for s, p, o in raw]

    async def delete(self, tenant_id: str, doc_id: str) -> None:
        await asyncio.gather(
            self.vectors.delete_document(tenant_id, doc_id), self.graph.delete_document(tenant_id, doc_id)
        )
