# @covers AC-055, AC-056, AC-057
"""Phase 6: per-chunk knowledge extraction into ``facts/<hash>.json`` (§5.6, §6.3)."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dn.classify import load_labels, taxonomy_key
from dn.extract import read_extracted, text_path
from dn.llm import LLM
from dn.schemas import Distilled, Entity, FactsFile, InventoryEntry, Term
from dn.workspace import Workspace, atomic_write_text

log = logging.getLogger("dn.distill")

CHUNK_CHARS = 8000
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass
class Chunk:
    heading_path: str
    text: str
    start_line: int
    end_line: int


def chunk_by_headings(body: str, limit: int = CHUNK_CHARS) -> list[Chunk]:
    """Split at headings; merge small sections, split oversized ones at paragraphs."""
    lines = body.split("\n")
    sections: list[Chunk] = []
    stack: list[str] = []
    buf: list[str] = []
    start = 1
    in_code = False

    def flush(end: int) -> None:
        text = "\n".join(buf).strip()
        if text:
            sections.append(Chunk(" > ".join(stack) or "(top)", text, start, end))

    for i, line in enumerate(lines, start=1):
        if line.startswith("```"):
            in_code = not in_code
        m = None if in_code else _HEADING.match(line)
        if m:
            flush(i - 1)
            buf, start = [], i
            level = len(m.group(1))
            stack = stack[: level - 1] + [m.group(2)]
        buf.append(line)
    flush(len(lines))

    chunks: list[Chunk] = []
    for s in sections:
        if len(s.text) <= limit:
            if chunks and len(chunks[-1].text) + len(s.text) + 2 <= limit:
                prev = chunks[-1]
                chunks[-1] = Chunk(
                    prev.heading_path, prev.text + "\n\n" + s.text, prev.start_line, s.end_line
                )
            else:
                chunks.append(s)
            continue
        part: list[str] = []
        for para in s.text.split("\n\n"):
            while len(para) > limit:  # a single giant paragraph
                if part:
                    chunks.append(Chunk(s.heading_path, "\n\n".join(part), s.start_line, s.end_line))
                    part = []
                chunks.append(Chunk(s.heading_path, para[:limit], s.start_line, s.end_line))
                para = para[limit:]
            if part and len("\n\n".join(part)) + len(para) + 2 > limit:
                chunks.append(Chunk(s.heading_path, "\n\n".join(part), s.start_line, s.end_line))
                part = []
            part.append(para)
        if part:
            chunks.append(Chunk(s.heading_path, "\n\n".join(part), s.start_line, s.end_line))
    return chunks


def facts_path(ws: Workspace, h: str) -> Path:
    return ws.facts_dir / f"{h}.json"


def _dry_distilled(chunk: Chunk) -> Distilled:
    """--dry-llm: headings become terms/entities; the first sentence becomes a fact."""
    title = chunk.heading_path.split(" > ")[-1]
    first = next(
        (ln.strip() for ln in chunk.text.splitlines() if ln.strip() and not ln.startswith("#")), title
    )
    loc = f"L{chunk.start_line}-{chunk.end_line}"
    return Distilled(
        terms=[Term(term=title, definition=first[:200], locator=loc)] if title != "(top)" else [],
        entities=[Entity(name=title, type="concept", description=first[:200], locator=loc)]
        if title != "(top)"
        else [],
        facts=[],
    )


def _attach(items: list[Any], path: str, h: str, default_loc: str) -> list[dict[str, Any]]:
    out = []
    for it in items:
        d = it.model_dump(by_alias=True)
        loc = d.pop("locator", "") or default_loc
        d["source"] = {"path": path, "hash": h, "locator": loc}
        out.append(d)
    return out


@dataclass
class DistillStats:
    distilled: int = 0
    cached: int = 0
    skipped: int = 0
    failed: int = 0


def should_distill(label_category: str | None, has_knowledge: bool, distill_all: bool) -> bool:
    return distill_all or not (label_category == "misc" and not has_knowledge)


async def distill(
    ws: Workspace, entries: list[InventoryEntry], llm: LLM, distill_all: bool = False
) -> DistillStats:
    labels = load_labels(ws, taxonomy_key(ws.taxonomy))
    stats = DistillStats()
    firsts: dict[str, InventoryEntry] = {}
    for e in entries:
        if e.hash and e.hash not in firsts and not e.skipped:
            firsts[e.hash] = e
    file_sem = asyncio.Semaphore(ws.config.concurrency)

    async def one(h: str, e: InventoryEntry) -> None:
        tp = text_path(ws, h)
        if not tp.exists():
            return
        rec = labels.get(h)
        if rec is not None and not should_distill(
            rec.label.category, rec.label.has_domain_knowledge, distill_all
        ):
            stats.skipped += 1
            return
        fp = facts_path(ws, h)
        if fp.exists():
            stats.cached += 1
            return
        meta, body = read_extracted(tp)
        chunks = chunk_by_headings(body)
        result = FactsFile(hash=h, path=e.path)
        async with file_sem:
            try:
                for i, ch in enumerate(chunks, start=1):
                    content = (
                        f"Source: {e.path} (type {meta.type}, chunk {i}/{len(chunks)})\n"
                        f"Heading path: {ch.heading_path}\nLines: {ch.start_line}-{ch.end_line}\n\n{ch.text}"
                    )
                    d = await llm.structured(
                        "distill", "distill", Distilled, content, dry=functools.partial(_dry_distilled, ch)
                    )
                    loc = f"{ch.heading_path} (L{ch.start_line}-{ch.end_line})"
                    result.terms += _attach(d.terms, e.path, h, loc)
                    result.entities += _attach(d.entities, e.path, h, loc)
                    result.facts += _attach(d.facts, e.path, h, loc)
                    result.relations += _attach(d.relations, e.path, h, loc)
                    result.questions += [
                        q.model_dump() | {"source": {"path": e.path, "hash": h, "locator": loc}}
                        for q in d.questions
                    ]
            except Exception as exc:
                stats.failed += 1
                ws.record_error(e.path, "distill", exc)
                return
        atomic_write_text(fp, json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        stats.distilled += 1

    await asyncio.gather(*(one(h, e) for h, e in firsts.items()))
    return stats
