import pytest

from app.infra.embeddings import HashEmbedder, SparseEncoder
from app.infra.graphstore import InMemoryGraphStore
from app.schemas.domain import Chunk, ScoredChunk, Triple
from app.services.chunking import chunk_text
from app.services.extraction import heuristic_triples
from app.services.retrieval import reciprocal_rank_fusion


def test_chunking_keeps_sections_and_bounds_size():
    text = "# Title\n\n## Alpha\n\n" + ("word " * 400) + "\n\n## Beta\n\nshort body"
    chunks = chunk_text(text, size=500, overlap=80)
    assert all(len(c.text) <= 500 + 80 for c in chunks)
    assert {c.section for c in chunks} == {"Alpha", "Beta"}
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_heuristic_triples_handles_coordination():
    t = heuristic_triples("Ledger Service stores data in Aurora PostgreSQL and is owned by Team Atlas. "
                          "The Payments API depends on Ledger Service and Fraud Engine.")
    assert ("Ledger Service", "owned by", "Team Atlas") in t
    assert ("Payments API", "depends on", "Fraud Engine") in t
    assert not any(s == "Aurora PostgreSQL" and p == "owned by" for s, p, _ in t)


async def test_hash_embedder_similarity():
    e = HashEmbedder(256)
    a, b, c = await e.embed_documents(["payments api latency slo", "latency objective of payments api",
                                       "vacation policy for weekends"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y, strict=True))  # noqa: E731
    assert dot(a, b) > dot(a, c)


def test_sparse_encoder_deterministic():
    s = SparseEncoder()
    assert s.encode("Fraud Engine owner") == s.encode("Fraud Engine owner")


def test_rrf_rewards_agreement():
    def sc(cid: str) -> ScoredChunk:
        return ScoredChunk(Chunk(cid, "t", "d", 0, "T", None, "x"), 1.0, "dense")
    fused = reciprocal_rank_fusion([[sc("a"), sc("b")], [sc("b"), sc("c")]])
    assert fused[0].chunk.chunk_id == "b"


async def test_graph_two_hop_and_tenant_isolation():
    g = InMemoryGraphStore()
    await g.add_triples("t1", [Triple("Payments API", "depends on", "Fraud Engine", "d", "c"),
                               Triple("Fraud Engine", "owned by", "Team Sentinel", "d", "c")])
    assert await g.link_entities("t1", "who runs the payments api?") == ["payments api"]
    one = await g.neighborhood("t1", ["payments api"], hops=1, limit=10)
    two = await g.neighborhood("t1", ["payments api"], hops=2, limit=10)
    assert len(one) == 1 and len(two) == 2
    assert await g.neighborhood("t2", ["payments api"], hops=2, limit=10) == []


@pytest.mark.parametrize("q,expected", [("hello", "direct"), ("What is the SLO?", "retrieve")])
async def test_planner_routes(q, expected):
    from app.agents.components import Planner
    assert (await Planner(None).plan(q)).route == expected
