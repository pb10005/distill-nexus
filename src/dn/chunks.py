# @covers AC-063
"""RAG chunks for ``chunks.jsonl``: fixed-size windows with overlap (no LLM)."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from dn.llm import estimate_tokens

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$", re.M)


def chars_per_token(lang: str) -> float:
    # @assumption AS-016
    return 1.5 if lang == "ja" else 2.5


def _heading_path_at(headings: list[tuple[int, int, str]], pos: int) -> str:
    stack: list[str] = []
    for start, level, title in headings:
        if start > pos:
            break
        stack = stack[: level - 1] + [title]
    return " > ".join(stack)


def make_chunks(
    text: str,
    source_hash: str,
    source_path: str,
    lang: str,
    chunk_tokens: int = 800,
    overlap_ratio: float = 0.15,
) -> list[dict[str, Any]]:
    f = chars_per_token(lang)
    max_chars = max(1, int(chunk_tokens * f))
    overlap = int(round(chunk_tokens * overlap_ratio * f))
    headings = [(m.start(), len(m.group(1)), m.group(2)) for m in _HEADING.finditer(text)]
    out: list[dict[str, Any]] = []
    n = len(text)
    start = 0
    while start < n:
        end = min(start + max_chars, n)
        if end < n:  # prefer to cut at a line break / space in the last 10% of the window
            floor = end - max_chars // 10
            cut = max(text.rfind("\n", floor, end), text.rfind(" ", floor, end))
            if cut > start + overlap:
                end = cut
        piece = text[start:end]
        if piece.strip():
            cid = hashlib.blake2b(f"{source_hash}:{start}".encode(), digest_size=8).hexdigest()
            out.append(
                {
                    "id": f"C-{cid}",
                    "source_hash": source_hash,
                    "source_path": source_path,
                    "heading_path": _heading_path_at(headings, start),
                    "text": piece,
                    "tokens": estimate_tokens(piece, lang),
                    "start": start,
                    "end": end,
                }
            )
        if end >= n:
            break
        nxt = end - overlap
        start = nxt if nxt > start else end
    return out
