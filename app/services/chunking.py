"""Structure-aware chunking: split on markdown headings, then pack paragraphs into size-bounded,
overlapping windows. Each chunk keeps its section heading for better retrieval and citations."""

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


@dataclass(slots=True)
class TextChunk:
    index: int
    section: str | None
    text: str


def _sections(text: str) -> list[tuple[str | None, str]]:
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [(None, text)]
    out: list[tuple[str | None, str]] = []
    if matches[0].start() > 0 and text[: matches[0].start()].strip():
        out.append((None, text[: matches[0].start()]))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        if body:
            out.append((m.group(2).strip(), body))
    return out


def _windows(paragraphs: list[str], size: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    for p in paragraphs:  # hard-split giant paragraphs
        while len(p) > size:
            cut = p.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            pieces.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            pieces.append(p)

    windows: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) + 2 > size:
            windows.append(current)
            tail = current[-overlap:] if overlap else ""
            tail = tail[tail.find(" ") + 1 :] if " " in tail else tail
            current = f"{tail}\n\n{piece}" if tail else piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        windows.append(current)
    return windows


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[TextChunk]:
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    chunks: list[TextChunk] = []
    for section, body in _sections(text.replace("\r\n", "\n")):
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        for window in _windows(paragraphs, size, overlap):
            chunks.append(TextChunk(index=len(chunks), section=section, text=window))
    return chunks
