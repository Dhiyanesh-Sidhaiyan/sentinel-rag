"""PII & secret detection/redaction. Applied to user input, ingested documents and model output, so
sensitive values are never embedded, stored, logged, sent to the LLM or echoed back."""

import re
from collections import Counter
from dataclasses import dataclass, field


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


# Order matters: secrets first (most specific), then structured PII.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("API_KEY", re.compile(r"\b(sk|pk|rk)[-_](live|test|proj)?[-_]?[A-Za-z0-9]{20,}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("BEARER_TOKEN", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*")),
    ("PASSWORD", re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S{4,}")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("SSN", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("PHONE", re.compile(r"(?<!\w)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\w)")),
    ("IP_ADDRESS", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
]

SECRET_TYPES = frozenset(
    {"PRIVATE_KEY", "AWS_ACCESS_KEY", "API_KEY", "GITHUB_TOKEN", "JWT", "BEARER_TOKEN", "PASSWORD"}
)


@dataclass(slots=True)
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def types(self) -> list[str]:
        return sorted(self.counts)

    @property
    def has_secrets(self) -> bool:
        return any(t in SECRET_TYPES for t in self.counts)


def redact(text: str, allow: frozenset[str] = frozenset()) -> RedactionResult:
    counts: Counter[str] = Counter()
    for kind, pattern in _PATTERNS:
        if kind in allow:
            continue

        def _sub(m: re.Match[str], kind: str = kind) -> str:
            if kind == "CREDIT_CARD" and not _luhn_ok(m.group(0)):
                return m.group(0)
            counts[kind] += 1
            return f"[REDACTED_{kind}]"

        text = pattern.sub(_sub, text)
    return RedactionResult(text=text, counts=dict(counts))
