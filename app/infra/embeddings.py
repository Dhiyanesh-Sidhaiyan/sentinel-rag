"""Dense + sparse encoders.

* HashEmbedder: deterministic feature-hashing embedder (no network / model download) used for local
  dev, CI and as an air-gapped fallback.
* OpenAIEmbedder: any OpenAI-compatible embeddings endpoint via LangChain.
* SparseEncoder: BM25-style term-frequency sparse vectors; IDF is applied by Qdrant (Modifier.IDF).
"""

import asyncio
import hashlib
import math
from collections import Counter
from typing import Protocol

from app.core.config import Settings
from app.schemas.domain import SparseVector
from app.services.text import content_terms, tokenize


class Embedder(Protocol):
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...


def _h(feature: str) -> int:
    return int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "little")


class HashEmbedder:
    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        terms = content_terms(text)
        feats: list[tuple[str, float]] = [(t, 1.0) for t in terms]
        feats += [(f"{a}_{b}", 0.7) for a, b in zip(terms, terms[1:], strict=False)]
        feats += [(f"#{t[i:i + 4]}", 0.25) for t in terms if len(t) > 5 for i in range(len(t) - 3)]
        for feat, weight in feats:
            h = _h(feat)
            vec[h % self.dim] += weight if (h >> 63) & 1 else -weight
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class OpenAIEmbedder:
    def __init__(self, settings: Settings) -> None:
        from langchain_openai import OpenAIEmbeddings

        assert settings.llm_api_key is not None
        self.dim = settings.embedding_dim
        self._batch = settings.embedding_batch_size
        self._client = OpenAIEmbeddings(
            model=settings.embedding_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            dimensions=settings.embedding_dim,
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        batches = [texts[i : i + self._batch] for i in range(0, len(texts), self._batch)]
        results = await asyncio.gather(*(self._client.aembed_documents(b) for b in batches))
        return [v for batch in results for v in batch]

    async def embed_query(self, text: str) -> list[float]:
        return await self._client.aembed_query(text)


class SparseEncoder:
    """Hashed-vocabulary BM25 term weights: tf*(k1+1)/(tf+k1*(1-b+b*dl/avgdl))."""

    def __init__(self, k1: float = 1.2, b: float = 0.75, avgdl: float = 120.0, vocab_bits: int = 24) -> None:
        self.k1, self.b, self.avgdl = k1, b, avgdl
        self._mask = (1 << vocab_bits) - 1

    def encode(self, text: str, query: bool = False) -> SparseVector:
        terms = content_terms(text) or tokenize(text)
        counts = Counter(_h(t) & self._mask for t in terms)
        dl = max(len(terms), 1)
        indices, values = [], []
        for idx, tf in sorted(counts.items()):
            weight = 1.0 if query else tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            indices.append(idx)
            values.append(round(weight, 6))
        return SparseVector(indices=indices, values=values)


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "openai":
        return OpenAIEmbedder(settings)
    return HashEmbedder(settings.embedding_dim)
