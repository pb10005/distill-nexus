# @covers AC-019, AC-020, AC-022, AC-024, AC-025
"""Common result type and text helpers for the extractors."""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_CHARS = 200_000
TRUNC_PART = 60_000


@dataclass
class ImageRequest:
    """An image that must be sent to Claude vision (whole image, or one PDF page)."""

    locator: str
    data: bytes
    media_type: str


@dataclass
class Extracted:
    text: str
    type: str
    pages: int | None = None
    quality: str = "text"
    images: list[ImageRequest] = field(default_factory=list)
    # PDF: text per page so OCR results can be spliced back in page order
    page_texts: list[str] | None = None


def normalize_newlines(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def truncate(text: str) -> tuple[str, bool]:
    """Keep head / middle / tail 60k chars each when over 200k (§5.2)."""
    if len(text) <= MAX_CHARS:
        return text, False
    mid = len(text) // 2
    head = text[:TRUNC_PART]
    middle = text[mid - TRUNC_PART // 2 : mid + TRUNC_PART // 2]
    tail = text[-TRUNC_PART:]
    marker = "\n\n[... truncated ...]\n\n"
    return head + marker + middle + marker + tail, True


def md_cell(v: object) -> str:
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def gfm_table(rows: list[list[object]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [[md_cell(c) for c in r] + [""] * (width - len(r)) for r in rows]
    header, body = norm[0], norm[1:]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * width) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)
