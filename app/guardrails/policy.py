"""Input/output policy enforcement composed from the individual detectors."""

import re
import secrets
from dataclasses import dataclass, field

from app.core.metrics import GUARDRAIL_EVENTS
from app.guardrails import injection, pii

REFUSAL_INJECTION = (
    "I can't help with that request. It appears to try to override my instructions. "
    "Please rephrase your question about the knowledge base."
)
REFUSAL_HARMFUL = "I can't help with that. Please ask a question related to the company knowledge base."
REFUSAL_SENSITIVE = (
    "Your message appears to contain a credential or secret, so I did not process it. "
    "Please rotate that credential immediately and contact your security team."
)

# Narrow, high-precision deny-list. A production deployment would add a moderation model here.
_HARMFUL = re.compile(
    r"\b(build|make|create|synthesi[sz]e)\b.{0,30}\b(bomb|explosive|nerve agent|bioweapon|ransomware|keylogger)\b"
    r"|\b(steal|phish|harvest)\b.{0,20}\b(credentials|passwords|credit cards?)\b",
    re.IGNORECASE,
)

_CITATION_RE = re.compile(r"\[(\d{1,2})\]")


@dataclass(slots=True)
class InputDecision:
    text: str
    blocked: bool = False
    reason: str | None = None
    refusal: str | None = None
    flags: list[str] = field(default_factory=list)
    pii_types: list[str] = field(default_factory=list)
    injection_score: float = 0.0


def check_input(question: str, threshold: float) -> InputDecision:
    red = pii.redact(question)
    for kind in red.types:
        GUARDRAIL_EVENTS.labels("input", f"pii_{kind.lower()}").inc()
    if red.has_secrets:
        return InputDecision(red.text, True, "secret_in_input", REFUSAL_SENSITIVE, ["secret_detected"], red.types)

    inj = injection.scan(question)
    flags = [f"injection:{f}" for f in inj.flags]
    if inj.blocked(threshold):
        GUARDRAIL_EVENTS.labels("input", "prompt_injection").inc()
        return InputDecision(red.text, True, "prompt_injection", REFUSAL_INJECTION, flags, red.types, inj.score)
    if _HARMFUL.search(question):
        GUARDRAIL_EVENTS.labels("input", "harmful").inc()
        return InputDecision(red.text, True, "harmful_content", REFUSAL_HARMFUL, [*flags, "harmful"], red.types,
                             inj.score)
    return InputDecision(red.text, False, None, None, flags, red.types, inj.score)


def new_canary() -> str:
    return f"CANARY-{secrets.token_hex(6)}"


@dataclass(slots=True)
class OutputDecision:
    text: str
    flags: list[str] = field(default_factory=list)
    pii_types: list[str] = field(default_factory=list)
    valid_refs: list[int] = field(default_factory=list)
    leaked: bool = False


def check_output(answer: str, canary: str, available_refs: set[int]) -> OutputDecision:
    flags: list[str] = []
    if canary and canary in answer:
        GUARDRAIL_EVENTS.labels("output", "prompt_leak").inc()
        return OutputDecision(REFUSAL_INJECTION, ["system_prompt_leak"], leaked=True)

    red = pii.redact(answer)
    if red.counts:
        flags.append("pii_redacted")
        for kind in red.types:
            GUARDRAIL_EVENTS.labels("output", f"pii_{kind.lower()}").inc()

    # strip hallucinated citations that point to passages that were never retrieved
    cited = {int(n) for n in _CITATION_RE.findall(red.text)}
    invalid = cited - available_refs
    text = red.text
    if invalid:
        flags.append("invalid_citations_removed")
        GUARDRAIL_EVENTS.labels("output", "invalid_citation").inc()
        text = _CITATION_RE.sub(lambda m: "" if int(m.group(1)) in invalid else m.group(0), text)

    if injection.scan(text).score >= 0.75:  # model was steered into emitting injection payloads
        flags.append("suspicious_output")
        GUARDRAIL_EVENTS.labels("output", "suspicious").inc()
    return OutputDecision(text.strip(), flags, red.types, sorted(cited & available_refs))
