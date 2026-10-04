"""LangGraph agent: guarded, self-correcting (Corrective-RAG + Self-RAG style) GraphRAG workflow.

    START -> input_guard --blocked--------------------------------------------------> END
                 |
               plan --direct--> respond_direct ------------------------------+
                 |                                                           |
             retrieve (hybrid vectors || knowledge graph, concurrent)        |
                 |                                                           v
               grade --no relevant ctx & budget left--> rewrite --> retrieve |
                 |                                                           |
             generate <--ungrounded & budget left-- verify ------------> output_guard -> END
"""

import asyncio
import operator
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.components import NO_ANSWER, Generator, Grader, Planner, Rewriter, Verifier
from app.core.config import Settings
from app.core.metrics import AGENT_NODE_LATENCY, RETRIEVAL_RESULTS
from app.guardrails import policy
from app.schemas.domain import GroundingVerdict, QueryPlan, ScoredChunk, Triple
from app.services.retrieval import HybridRetriever


class AgentState(TypedDict, total=False):
    tenant_id: str
    question: str
    safe_question: str
    top_k: int
    canary: str
    # guardrails
    blocked: bool
    block_reason: str | None
    input_flags: list[str]
    pii_redacted: list[str]
    injection_score: float
    output_flags: list[str]
    # reasoning
    plan: QueryPlan
    queries: list[str]
    rewrites: int
    candidates: list[ScoredChunk]
    relevant: list[ScoredChunk]
    linked_entities: list[str]
    facts: list[Triple]
    answer: str
    gen_attempts: int
    verdict: GroundingVerdict
    cited_refs: list[int]
    trace: Annotated[list[dict[str, Any]], operator.add]


NodeFn = Callable[[AgentState], Awaitable[dict[str, Any]]]


def traced(name: str, fn: NodeFn) -> NodeFn:
    async def wrapper(state: AgentState) -> dict[str, Any]:
        start = time.perf_counter()
        update = await fn(state)
        elapsed = time.perf_counter() - start
        AGENT_NODE_LATENCY.labels(name).observe(elapsed)
        detail = update.pop("_detail", {})
        update["trace"] = [{"node": name, "ms": round(elapsed * 1000, 2), "detail": detail}]
        return update

    return wrapper


def build_agent(settings: Settings, retriever: HybridRetriever, planner: Planner, grader: Grader,
                rewriter: Rewriter, generator: Generator, verifier: Verifier):
    max_iter = settings.max_agent_iterations

    async def input_guard(state: AgentState) -> dict[str, Any]:
        q = state["question"]
        if len(q) > settings.max_question_chars:
            return {"blocked": True, "block_reason": "input_too_long", "answer": "Your question is too long.",
                    "input_flags": ["input_too_long"], "_detail": {"blocked": True}}
        d = policy.check_input(q, settings.injection_block_threshold)
        return {
            "safe_question": d.text, "blocked": d.blocked, "block_reason": d.reason,
            "answer": d.refusal or "", "input_flags": d.flags, "pii_redacted": d.pii_types,
            "injection_score": d.injection_score, "canary": policy.new_canary(),
            "_detail": {"blocked": d.blocked, "flags": d.flags, "injection_score": d.injection_score,
                        "pii": d.pii_types},
        }

    async def plan(state: AgentState) -> dict[str, Any]:
        p = await planner.plan(state["safe_question"])
        queries = p.sub_queries or [state["safe_question"]]
        return {"plan": p, "queries": queries, "rewrites": 0, "gen_attempts": 0,
                "_detail": {"route": p.route, "sub_queries": queries, "entities": p.entities}}

    async def retrieve(state: AgentState) -> dict[str, Any]:
        k = state.get("top_k") or settings.final_top_k
        chunks, (linked, facts) = await asyncio.gather(
            retriever.retrieve(state["tenant_id"], state["safe_question"], state["queries"],
                               settings.retrieval_candidates, k),
            retriever.graph_facts(state["tenant_id"], state["safe_question"], state["plan"].entities,
                                  settings.graph_hops, settings.graph_fact_limit),
        )
        return {"candidates": chunks, "linked_entities": linked, "facts": facts,
                "_detail": {"queries": state["queries"], "candidates": len(chunks), "graph_entities": linked,
                            "graph_facts": len(facts)}}

    async def grade(state: AgentState) -> dict[str, Any]:
        relevant = await grader.grade(state["safe_question"], state["candidates"])
        RETRIEVAL_RESULTS.observe(len(relevant))
        return {"relevant": relevant,
                "_detail": {"relevant": len(relevant), "dropped": len(state["candidates"]) - len(relevant)}}

    async def rewrite(state: AgentState) -> dict[str, Any]:
        new_q = await rewriter.rewrite(state["safe_question"], state.get("linked_entities", []))
        return {"queries": [new_q], "rewrites": state.get("rewrites", 0) + 1, "_detail": {"rewritten": new_q}}

    async def generate(state: AgentState) -> dict[str, Any]:
        attempts = state.get("gen_attempts", 0)
        answer = await generator.generate(state["safe_question"], state.get("relevant", []),
                                          state.get("facts", []) if state.get("relevant") else [],
                                          state["canary"], strict=attempts > 0)
        return {"answer": answer, "gen_attempts": attempts + 1, "_detail": {"attempt": attempts + 1,
                                                                            "strict": attempts > 0}}

    async def verify(state: AgentState) -> dict[str, Any]:
        v = await verifier.verify(state["answer"], state.get("relevant", []), state.get("facts", []))
        update: dict[str, Any] = {"verdict": v, "_detail": {"grounded": v.grounded, "score": v.score}}
        if not v.grounded and state.get("gen_attempts", 0) >= max_iter:
            update["answer"] = NO_ANSWER  # refuse rather than ship an unverified answer
        return update

    async def respond_direct(state: AgentState) -> dict[str, Any]:
        return {"answer": "Hello! I'm Sentinel. Ask me anything about the documents in your knowledge base.",
                "verdict": GroundingVerdict(grounded=True, score=1.0), "relevant": [], "facts": [],
                "_detail": {}}

    async def output_guard(state: AgentState) -> dict[str, Any]:
        refs = set(range(1, len(state.get("relevant", [])) + 1))
        d = policy.check_output(state["answer"], state.get("canary", ""), refs)
        return {"answer": d.text, "output_flags": d.flags, "cited_refs": d.valid_refs,
                "pii_redacted": sorted(set(state.get("pii_redacted", [])) | set(d.pii_types)),
                "_detail": {"flags": d.flags, "cited": d.valid_refs}}

    def after_guard(state: AgentState) -> str:
        return END if state.get("blocked") else "plan"

    def after_plan(state: AgentState) -> str:
        return "respond_direct" if state["plan"].route == "direct" else "retrieve"

    def after_grade(state: AgentState) -> str:
        if not state.get("relevant") and state.get("rewrites", 0) < max_iter - 1:
            return "rewrite"
        return "generate"

    def after_verify(state: AgentState) -> str:
        v = state["verdict"]
        ok = v.grounded and v.score >= settings.grounding_threshold
        return "output_guard" if ok or state.get("gen_attempts", 0) >= max_iter else "generate"

    g = StateGraph(AgentState)
    for name, fn in [("input_guard", input_guard), ("plan", plan), ("retrieve", retrieve), ("grade", grade),
                     ("rewrite", rewrite), ("generate", generate), ("verify", verify),
                     ("respond_direct", respond_direct), ("output_guard", output_guard)]:
        g.add_node(name, traced(name, fn))  # type: ignore[call-overload]
    g.add_edge(START, "input_guard")
    g.add_conditional_edges("input_guard", after_guard, ["plan", END])
    g.add_conditional_edges("plan", after_plan, ["respond_direct", "retrieve"])
    g.add_edge("retrieve", "grade")
    g.add_conditional_edges("grade", after_grade, ["rewrite", "generate"])
    g.add_edge("rewrite", "retrieve")
    g.add_edge("generate", "verify")
    g.add_conditional_edges("verify", after_verify, ["generate", "output_guard"])
    g.add_edge("respond_direct", "output_guard")
    g.add_edge("output_guard", END)
    return g.compile()
