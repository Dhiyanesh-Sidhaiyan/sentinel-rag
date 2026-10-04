"""Prompt-injection & jailbreak detection (direct: user input; indirect: ingested documents).

Layered, explainable heuristic scorer: Unicode normalization -> weighted signature rules ->
structural signals (role tags, encoded payloads). Score in [0, 1]. Designed to be swapped or ensembled
with a classifier model (e.g. a fine-tuned DeBERTa) behind the same interface.
"""

import base64
import re
from dataclasses import dataclass, field

from app.services.text import normalize

_RULES: list[tuple[str, float, re.Pattern[str]]] = [
    (name, weight, re.compile(pattern, re.IGNORECASE))
    for name, weight, pattern in [
        ("ignore_instructions", 0.8,
         r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|your|system)\b"
         r".{0,30}\b(instructions?|rules?|prompts?|directives?|guidelines?)"),
        ("reveal_system_prompt", 0.75,
         r"\b(reveal|show|print|repeat|output|leak|display|tell me)\b.{0,40}\b(system|hidden|initial|original)\s+"
         r"(prompt|instructions?|message|rules)"),
        ("role_override", 0.55,
         r"\b(you are now|from now on,? you|act as|pretend (to be|you are)|roleplay as)\b"),
        ("jailbreak_persona", 0.8, r"\b(DAN|do anything now|developer mode|jailbreak|god mode|unfiltered mode)\b"),
        ("new_instructions", 0.5, r"\b(new|updated|real|actual)\s+(instructions?|system prompt|rules)\s*[:\-]"),
        ("chat_template_tokens", 0.9, r"(<\|im_start\|>|<\|im_end\|>|<\|system\|>|\[INST\]|<<SYS>>|<\|endoftext\|>)"),
        ("fake_role_header", 0.6, r"(^|\n)\s*(#{2,}\s*)?(system|assistant|developer)\s*[:>]"),
        ("exfiltration", 0.7,
         r"\b(send|post|upload|exfiltrate|forward)\b.{0,50}\b(https?://|webhook|email|api key|credentials|secrets?)"),
        ("markdown_image_exfil", 0.7, r"!\[[^\]]*\]\(https?://[^)]*\?[^)]*=\s*[{(\[]?"),
        ("tool_hijack", 0.5,
         r"\b(call|invoke|execute|run)\b.{0,20}\b(tool|function|shell|command|sql)\b.{0,30}\b(delete|drop|rm -rf)"),
        ("safety_disable", 0.6,
         r"\b(disable|turn off|remove|without)\b.{0,25}\b(safety|guardrails?|filters?|restrictions?|censorship)"),
    ]
]

_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


@dataclass(slots=True)
class InjectionResult:
    score: float
    flags: list[str] = field(default_factory=list)

    def blocked(self, threshold: float) -> bool:
        return self.score >= threshold


def _decoded_payloads(text: str) -> list[str]:
    out = []
    for m in _BASE64_RE.findall(text)[:5]:
        try:
            decoded = base64.b64decode(m + "=" * (-len(m) % 4), validate=True).decode("utf-8")
            if decoded.isprintable():
                out.append(decoded)
        except Exception:  # noqa: S112 - not base64, skip
            continue
    return out


def scan(text: str) -> InjectionResult:
    normalized = normalize(text)
    # collapse obfuscation like "i.g.n.o.r.e" or "i g n o r e"
    collapsed = re.sub(r"(?<=\b\w)[\s.\-_*](?=\w\b)", "", normalized)
    candidates = [normalized, collapsed, *_decoded_payloads(normalized)]

    flags: dict[str, float] = {}
    for candidate in candidates:
        for name, weight, pattern in _RULES:
            if pattern.search(candidate):
                flags[name] = max(flags.get(name, 0.0), weight)
    if len(candidates) > 2 and flags:
        flags["encoded_payload"] = 0.3

    # noisy-OR combination: independent weak signals add up, never exceed 1
    remaining = 1.0
    for w in flags.values():
        remaining *= 1 - w
    return InjectionResult(score=round(1 - remaining, 3), flags=sorted(flags))
