"""Agent capabilities. Each uses the LLM when configured and falls back to a deterministic heuristic on
failure (or in offline mode). This keeps the service available during LLM outages and makes CI hermetic."""

import re

from app.core.logging import get_logger
from app.llm.client import LLMClient
from app.llm.prompts import (
    GENERATOR_STRICT_ADDENDUM,
    GENERATOR_SYSTEM,
    GRADER_SYSTEM,
    PLANNER_SYSTEM,
    REWRITE_SYSTEM,
    VERIFIER_SYSTEM,
    render_context,
    render_graph,
)
from app.schemas.domain import GroundingVerdict, QueryPlan, RelevanceGrades, ScoredChunk, Triple
from app.services.text import STOPWORDS, content_terms, overlap, split_sentences, tokenize

log = get_logger(__name__)

NO_ANSWER = "I don't have enough information in the knowledge base to answer that."
_SMALLTALK = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|good (morning|afternoon|evening)|who are you)\b[\s!?.]*$",
                        re.IGNORECASE)
_SPLIT = re.compile(r"\?\s+|;\s+|\b(?:and also|and then|as well as|and what|and who|and how|and which)\b",
                    re.IGNORECASE)
_SYNONYMS = {
    "outage": "incident sev-1 downtime", "down": "outage incident", "owner": "owned by team",
    "owns": "owned by team", "who": "team owner", "rotate": "rotation credentials", "secret": "credentials vault",
    "sla": "service level response time", "pager": "on-call escalation", "oncall": "on-call",
    "db": "database", "deploy": "deployment release", "pto": "time off leave",
}


class Planner:
    def __init__(self, llm: LLMClient | None) -> None:
        self.llm = llm

    async def plan(self, question: str) -> QueryPlan:
        if self.llm is not None:
            try:
                return await self.llm.structured(PLANNER_SYSTEM, question, QueryPlan, "plan")
            except Exception:
                log.warning("planner_fallback")
        if _SMALLTALK.match(question):
            return QueryPlan(route="direct", sub_queries=[], entities=[])
        parts = [p.strip(" ?.,") for p in _SPLIT.split(question) if p and len(content_terms(p)) >= 2]
        sub_queries = (parts or [question.strip()])[:3]
        if len(sub_queries) > 1 and question.strip() not in sub_queries:
            sub_queries = [question.strip(), *sub_queries][:3]
        entities = re.findall(r"\b[A-Z][A-Za-z0-9]+(?:[- ][A-Z0-9][A-Za-z0-9]+)*\b", question)
        entities = [e for e in entities if e.lower() not in STOPWORDS and len(e) > 2]
        return QueryPlan(route="retrieve", sub_queries=sub_queries, entities=list(dict.fromkeys(entities)))


class Grader:
    def __init__(self, llm: LLMClient | None, threshold: float) -> None:
        self.llm, self.threshold = llm, threshold

    async def grade(self, question: str, chunks: list[ScoredChunk]) -> list[ScoredChunk]:
        if not chunks:
            return []
        if self.llm is not None:
            try:
                listing = "\n\n".join(f"[{i}] {c.chunk.text[:1200]}" for i, c in enumerate(chunks))
                grades = await self.llm.structured(
                    GRADER_SYSTEM, f"Question: {question}\n\nPassages:\n{listing}", RelevanceGrades, "grade"
                )
                keep = {g.index for g in grades.grades if g.relevant}
                return [c for i, c in enumerate(chunks) if i in keep]
            except Exception:
                log.warning("grader_fallback")
        return [c for c in chunks if overlap(question, f"{c.chunk.section or ''} {c.chunk.text}") >= self.threshold]


class Rewriter:
    def __init__(self, llm: LLMClient | None) -> None:
        self.llm = llm

    async def rewrite(self, question: str, entities: list[str]) -> str:
        if self.llm is not None:
            try:
                return await self.llm.generate(REWRITE_SYSTEM, question, "rewrite")
            except Exception:
                log.warning("rewrite_fallback")
        terms = [t for t in tokenize(question) if t not in STOPWORDS]
        expanded = terms + [_SYNONYMS[t] for t in terms if t in _SYNONYMS] + entities
        return " ".join(dict.fromkeys(expanded)) or question


class Generator:
    def __init__(self, llm: LLMClient | None) -> None:
        self.llm = llm

    async def generate(self, question: str, chunks: list[ScoredChunk], facts: list[Triple], canary: str,
                       strict: bool = False) -> str:
        if not chunks and not facts:
            return NO_ANSWER
        if self.llm is not None:
            try:
                system = GENERATOR_SYSTEM.format(canary=canary) + (GENERATOR_STRICT_ADDENDUM if strict else "")
                ctx = render_context([(i + 1, c.chunk.title, c.chunk.text) for i, c in enumerate(chunks)])
                graph = render_graph([(f.subject, f.predicate, f.object) for f in facts])
                return await self.llm.generate(system, f"{ctx}\n{graph}\n\nQuestion: {question}", "generate")
            except Exception:
                log.warning("generator_fallback")
        return self._extractive(question, chunks, facts)

    @staticmethod
    def _extractive(question: str, chunks: list[ScoredChunk], facts: list[Triple]) -> str:
        """Query-focused extractive summarization with citations -- grounded by construction."""
        q_terms = set(content_terms(question))
        scored: list[tuple[float, int, int, str]] = []
        for ref, c in enumerate(chunks, start=1):
            section_terms = set(content_terms(c.chunk.section or ""))
            for pos, sent in enumerate(split_sentences(c.chunk.text)):
                if len(sent) < 25 or sent.startswith("#"):
                    continue
                terms = set(content_terms(sent))
                if not terms:
                    continue
                # a sentence under a matching heading ("Which carriers do we support?") is likely the answer
                hit = (len(q_terms & terms) + 0.5 * len((q_terms & section_terms) - terms)) / max(len(q_terms), 1)
                score = hit + 0.15 / ref - 0.002 * pos
                if hit > 0:
                    scored.append((score, ref, pos, sent))
        scored.sort(reverse=True)
        picked: list[tuple[int, int, str]] = []
        seen_terms: set[str] = set()
        for _, ref, pos, sent in scored:
            terms = set(content_terms(sent))
            if terms and len(terms & seen_terms) / len(terms) > 0.7:
                continue  # redundant
            picked.append((ref, pos, sent))
            seen_terms |= terms
            if len(picked) == 4:
                break
        if not picked and not facts:
            return NO_ANSWER
        picked.sort()
        lines = [f"{sent.rstrip()} [{ref}]" for ref, _, sent in picked]
        relevant_facts = [f for f in facts if q_terms & set(content_terms(f"{f.subject} {f.object}"))][:3]
        if relevant_facts:
            lines.append("Related: " + "; ".join(f"{f.subject} {f.predicate} {f.object}" for f in relevant_facts) + ".")
        return " ".join(lines)


class Verifier:
    def __init__(self, llm: LLMClient | None) -> None:
        self.llm = llm

    async def verify(self, answer: str, chunks: list[ScoredChunk], facts: list[Triple]) -> GroundingVerdict:
        if answer == NO_ANSWER:
            return GroundingVerdict(grounded=True, score=1.0)
        context = "\n\n".join(c.chunk.text for c in chunks)
        context += "\n" + "\n".join(f"{f.subject} {f.predicate} {f.object}" for f in facts)
        if self.llm is not None:
            try:
                return await self.llm.structured(
                    VERIFIER_SYSTEM, f"<context>\n{context}\n</context>\n\n<answer>\n{answer}\n</answer>",
                    GroundingVerdict, "verify",
                )
            except Exception:
                log.warning("verifier_fallback")
        sentences = [s for s in split_sentences(re.sub(r"\[\d+\]", "", answer)) if content_terms(s)]
        if not sentences:
            return GroundingVerdict(grounded=False, score=0.0)
        ctx_terms = set(content_terms(context))
        unsupported = [s for s in sentences
                       if len(set(content_terms(s)) & ctx_terms) / len(set(content_terms(s))) < 0.8]
        score = 1 - len(unsupported) / len(sentences)
        return GroundingVerdict(grounded=not unsupported, score=round(score, 3), unsupported_claims=unsupported)
