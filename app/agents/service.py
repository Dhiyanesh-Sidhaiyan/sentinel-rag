"""AgentService: runs the LangGraph agent, maps state -> API response, handles answer caching & streaming."""

import hashlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from app.agents.components import NO_ANSWER
from app.agents.graph import AgentState
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import CACHE_EVENTS
from app.infra.cache import Cache
from app.schemas.api import Citation, GraphFact, GuardrailReport, PlanReport, QueryResponse, TraceStep

log = get_logger(__name__)


class AgentService:
    def __init__(self, settings: Settings, agent, cache: Cache) -> None:
        self.s, self.agent, self.cache = settings, agent, cache

    def _cache_key(self, tenant_id: str, question: str, top_k: int | None) -> str:
        raw = f"{tenant_id}\x1f{' '.join(question.lower().split())}\x1f{top_k}"
        return "ans:" + hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _initial(tenant_id: str, question: str, top_k: int | None) -> AgentState:
        state: AgentState = {"tenant_id": tenant_id, "question": question, "trace": []}
        if top_k:
            state["top_k"] = top_k
        return state

    async def query(self, tenant_id: str, question: str, request_id: str, top_k: int | None = None,
                    use_cache: bool = True, include_trace: bool = True) -> QueryResponse:
        start = time.perf_counter()
        key = self._cache_key(tenant_id, question, top_k)
        if use_cache and (hit := await self._cache_get(key)):
            CACHE_EVENTS.labels("hit").inc()
            hit.update(request_id=request_id, cached=True, latency_ms=round((time.perf_counter() - start) * 1000, 2))
            if not include_trace:
                hit["trace"] = []
            return QueryResponse.model_validate(hit)
        CACHE_EVENTS.labels("miss").inc()

        final: AgentState = await self.agent.ainvoke(self._initial(tenant_id, question, top_k))
        resp = self.to_response(final, request_id, start)
        if use_cache and resp.grounded and not resp.guardrails.blocked and resp.confidence > 0:
            await self._cache_set(key, resp)
        if not include_trace:
            resp.trace = []
        log.info("query_answered", route=resp.plan.route, blocked=resp.guardrails.blocked,
                 grounded=resp.grounded, confidence=resp.confidence, citations=len(resp.citations),
                 latency_ms=resp.latency_ms)
        return resp

    async def stream(self, tenant_id: str, question: str, request_id: str,
                     top_k: int | None = None) -> AsyncIterator[str]:
        """Server-Sent Events: one `step` event per graph node, then a final `result` event."""
        start = time.perf_counter()
        state: dict[str, Any] = dict(self._initial(tenant_id, question, top_k))
        async for update in self.agent.astream(state, stream_mode="updates"):
            for node, delta in update.items():
                delta = delta or {}
                for k, v in delta.items():
                    state[k] = state.get("trace", []) + v if k == "trace" else v
                step = (delta.get("trace") or [{"node": node}])[-1]
                yield f"event: step\ndata: {json.dumps(step, default=str)}\n\n"
        resp = self.to_response(state, request_id, start)  # type: ignore[arg-type]
        yield f"event: result\ndata: {resp.model_dump_json()}\n\n"

    def to_response(self, st: AgentState, request_id: str, start: float) -> QueryResponse:
        relevant = st.get("relevant", [])
        cited = set(st.get("cited_refs", []))
        citations = [
            Citation(ref=i, doc_id=c.chunk.doc_id, chunk_id=c.chunk.chunk_id, title=c.chunk.title,
                     section=c.chunk.section, score=round(c.score, 4),
                     snippet=c.chunk.text[:280] + ("..." if len(c.chunk.text) > 280 else ""))
            for i, c in enumerate(relevant, start=1) if i in cited
        ]
        facts = [GraphFact(subject=f.subject, predicate=f.predicate, object=f.object, doc_id=f.doc_id)
                 for f in st.get("facts", [])] if relevant else []
        verdict = st.get("verdict")
        blocked = bool(st.get("blocked"))
        answer = st.get("answer", "") or NO_ANSWER
        grounded = bool(verdict and verdict.grounded) and not blocked
        if blocked or answer == NO_ANSWER or not citations and st.get("plan") and st["plan"].route == "retrieve":
            confidence = 0.0
        else:
            retrieval_q = sum(c.score for c in citations[:3]) / max(len(citations[:3]), 1) if citations else 1.0
            confidence = round(min(1.0, 0.6 * (verdict.score if verdict else 0) + 0.4 * retrieval_q), 3)

        p = st.get("plan")
        plan = PlanReport(route="blocked" if blocked else (p.route if p else "retrieve"),
                          sub_queries=st.get("queries", []), entities=st.get("linked_entities", []),
                          rewrites=st.get("rewrites", 0))
        return QueryResponse(
            request_id=request_id, answer=answer, citations=citations, graph_facts=facts,
            confidence=confidence, grounded=grounded,
            guardrails=GuardrailReport(
                blocked=blocked, block_reason=st.get("block_reason"), input_flags=st.get("input_flags", []),
                output_flags=st.get("output_flags", []), pii_redacted=st.get("pii_redacted", []),
                injection_score=st.get("injection_score", 0.0),
            ),
            plan=plan, trace=[TraceStep(**t) for t in st.get("trace", [])],
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
        )

    async def _cache_get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self.cache.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            log.warning("cache_get_failed")
            return None

    async def _cache_set(self, key: str, resp: QueryResponse) -> None:
        try:
            await self.cache.set(key, resp.model_dump_json(), self.s.answer_cache_ttl_s)
        except Exception:
            log.warning("cache_set_failed")
