"""Knowledge-graph triple extraction: LLM structured output, with a rule-based extractor as fallback."""

import re

from app.core.logging import get_logger
from app.llm.client import LLMClient
from app.llm.prompts import TRIPLE_SYSTEM
from app.schemas.domain import TripleExtraction
from app.services.text import split_sentences

log = get_logger(__name__)

RELATIONS = [
    "is owned by", "owned by", "is maintained by", "maintained by", "is managed by", "managed by",
    "depends on", "integrates with", "stores data in", "writes to", "reads from", "publishes to",
    "consumes from", "escalates to", "reports to", "is part of", "part of", "replaces", "replaced",
    "requires", "is approved by", "approved by", "owns", "maintains", "manages", "uses", "calls",
]
_CANON = {
    "is owned by": "owned by", "is maintained by": "maintained by", "is managed by": "managed by",
    "is part of": "part of", "is approved by": "approved by", "replaced": "replaces",
}
_REL_RE = re.compile(r"\b(" + "|".join(re.escape(r) for r in sorted(RELATIONS, key=len, reverse=True)) + r")\b")
_WORD = r"[A-Z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)*"
_ENTITY_RE = re.compile(rf"\b{_WORD}(?:\s+(?:{_WORD}|of|for)){{0,4}}")  # "VP of Platform Engineering"
_LEADING = re.compile(r"^((The|A|An|All|Every|Each|Our|This|That|If|When|Both|Only)(\s+|$))+")
_TRAILING = re.compile(r"(\s+(of|for))+$")
# text allowed between the subject entity and the relation phrase ("X is owned by", "X, which depends on")
_SUBJECT_GAP = re.compile(r"^\s*(,?\s*(which|that)\s+)?(is|are|was|also|has been)?\s*$")
_COORD_GAP = re.compile(r"^\s*(,\s*)?and\s+(is\s+|also\s+)?$")  # "X stores data in Y and is owned by Z"
_OBJECT_GAP = re.compile(r"^\s*(the\s+)?$", re.IGNORECASE)
_COORD_OBJ = re.compile(r"^\s*(?:,\s*and|,|and)\s+(?:the\s+)?(" + _ENTITY_RE.pattern + ")")


def _clean(entity: str) -> str:
    entity = _TRAILING.sub("", _LEADING.sub("", entity.strip()))
    return entity.strip(" ,.;:")


def heuristic_triples(text: str) -> list[tuple[str, str, str]]:
    """High-precision pattern extractor: <Entity> <relation> <Entity>, with coordination handling."""
    triples: list[tuple[str, str, str]] = []
    text = re.sub(r"^\s*#{1,6}\s.*$", "\n", text, flags=re.MULTILINE)  # headings are not sentences
    for sentence in split_sentences(text):
        for m in _REL_RE.finditer(sentence):
            left_text, right_text = sentence[: m.start()], sentence[m.end():]
            left = [e for e in _ENTITY_RE.finditer(left_text) if _clean(e.group(0))]
            right_m = _ENTITY_RE.search(right_text)
            if not left or not right_m or not _OBJECT_GAP.match(right_text[: right_m.start()]):
                continue
            gap = left_text[left[-1].end():]
            if _SUBJECT_GAP.match(gap):
                subj = _clean(left[-1].group(0))
            elif _COORD_GAP.match(gap):
                subj = _clean(left[0].group(0))  # coordinated predicate shares the sentence subject
            else:
                continue
            obj = _clean(right_m.group(0))
            if len(subj) < 2 or len(obj) < 2 or subj.lower() == obj.lower():
                continue
            rel = _CANON.get(m.group(1), m.group(1))
            triples.append((subj, rel, obj))
            # coordinated objects: "depends on Ledger Service and Fraud Engine", "FedEx, UPS, and DHL"
            rest = right_text[right_m.end():]
            while (nxt := _COORD_OBJ.match(rest)) and _clean(nxt.group(1)):
                triples.append((subj, rel, _clean(nxt.group(1))))
                rest = rest[nxt.end():]
    return list(dict.fromkeys(triples))


class TripleExtractor:
    def __init__(self, llm: LLMClient | None) -> None:
        self._llm = llm

    async def extract(self, text: str) -> list[tuple[str, str, str]]:
        if self._llm is not None:
            try:
                out = await self._llm.structured(TRIPLE_SYSTEM, text[:6000], TripleExtraction, "extract_triples")
                return [(t.subject.strip(), t.predicate.strip().lower(), t.object.strip())
                        for t in out.triples if t.subject.strip() and t.object.strip()]
            except Exception:
                log.warning("triple_extraction_fallback")
        return heuristic_triples(text)
