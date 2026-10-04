"""Text normalization and tokenization shared by sparse retrieval, hash embeddings and heuristics."""

import re
import unicodedata

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)

STOPWORDS = frozenset(
    """a about above after again against all am an and any are as at be because been before being below
    between both but by can could did do does doing down during each few for from further had has have
    having he her here hers herself him himself his how i if in into is it its itself just me more most my
    myself no nor not now of off on once only or other our ours ourselves out over own same she should so
    some such than that the their theirs them themselves then there these they this those through to too
    under until up very was we were what when where which while who whom why will with would you your
    yours yourself yourselves tell please explain describe give list show me know need want get""".split()
)


def normalize(text: str) -> str:
    """NFKC + strip zero-width chars (defeats common homoglyph / hidden-char obfuscation)."""
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(normalize(text).lower())


def content_terms(text: str) -> list[str]:
    return [_stem(t) for t in tokenize(text) if t not in STOPWORDS and len(t) > 1]


def _stem(token: str) -> str:
    """Tiny suffix stripper -- good enough for lexical matching without an NLP dependency."""
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return token


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    return [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]


def overlap(query: str, text: str) -> float:
    """Fraction of query content terms present in text."""
    q = set(content_terms(query))
    if not q:
        return 0.0
    return len(q & set(content_terms(text))) / len(q)
