# @covers AC-019, AC-020, AC-021, AC-022, AC-023, AC-024, AC-025, AC-026, AC-027
"""Phase 2: dispatch each file to an extractor and write ``text/<hash>.md``.

The file kind is decided by content (puremagic) before extension (§5.2); a
mismatch is logged. Failures are isolated per file into ``errors.jsonl``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dn.extract import code, docx, html, image, pdf, pptx, text, xlsx
from dn.extract.base import Extracted, ImageRequest, truncate
from dn.llm import LLM, detect_lang, image_block
from dn.platform import long_path
from dn.schemas import ExtractMeta, InventoryEntry, VisionResult
from dn.workspace import Workspace, atomic_write_text

log = logging.getLogger("dn.extract")

TEXT_EXT = {
    ".md",
    ".markdown",
    ".txt",
    ".rst",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".log",
    ".ini",
    ".cfg",
}
HTML_EXT = {".html", ".htm", ".xhtml"}
CODE_EXT = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".cs",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".rb",
    ".php",
    ".kt",
    ".swift",
    ".scala",
    ".sh",
    ".ps1",
    ".sql",
}
IMAGE_EXT = set(image.MEDIA_TYPES)
OFFICE = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx"}
# kinds identified by magic bytes that override the extension (AS-008)
MAGIC_KIND = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
}


def kind_by_ext(ext: str) -> str:
    ext = ext.lower()
    if ext in OFFICE:
        return OFFICE[ext]
    if ext in IMAGE_EXT:
        return "image"
    if ext in HTML_EXT:
        return "html"
    if ext in CODE_EXT:
        return "code"
    if ext in TEXT_EXT:
        return "text"
    return "misc"


def detect_kind(path: Path) -> tuple[str, str | None]:
    """Return (kind, magic_ext). Magic wins for binary formats; a mismatch is warned."""
    by_ext = kind_by_ext(path.suffix)
    magic_ext: str | None = None
    try:
        import puremagic

        magic_ext = str(puremagic.from_file(long_path(path))).lower() or None
    except Exception:  # plain text has no magic number
        magic_ext = None
    # @assumption AS-008
    by_magic = MAGIC_KIND.get(magic_ext or "")
    if by_magic and by_magic != by_ext:
        if by_ext in ("docx", "xlsx", "pptx") and by_magic in ("docx", "xlsx", "pptx"):
            return by_ext, magic_ext  # all OOXML zips; trust the extension among them
        log.warning("type mismatch for %s: extension says %s, content says %s", path.name, by_ext, by_magic)
        return by_magic, magic_ext
    return by_ext, magic_ext


EXTRACTABLE = {"text", "html", "code", "pdf", "docx", "xlsx", "pptx", "image"}


def run_extractor(path: Path, kind: str, want_images: bool, magic_ext: str | None = None) -> Extracted:
    if kind == "pdf":
        return pdf.extract(path, want_images=want_images)
    if kind == "image":
        mt = image.MEDIA_TYPES.get(magic_ext or "", None)
        return image.extract(path, want_images=want_images, media_type=mt)
    extractor: dict[str, Callable[[Path], Extracted]] = {
        "text": text.extract,
        "html": html.extract,
        "code": code.extract,
        "docx": docx.extract,
        "xlsx": xlsx.extract,
        "pptx": pptx.extract,
    }
    return extractor[kind](path)


def frontmatter(meta: ExtractMeta) -> str:
    return "---\n" + yaml.safe_dump(meta.model_dump(), allow_unicode=True, sort_keys=False) + "---\n\n"


def read_extracted(path: Path) -> tuple[ExtractMeta, str]:
    raw = path.read_text(encoding="utf-8")
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        meta = ExtractMeta.model_validate(yaml.safe_load(raw[4:end]))
        return meta, raw[end + 5 :].lstrip("\n")
    return ExtractMeta(source="", type="text"), raw


async def _vision(llm: LLM, req: ImageRequest, rel: str) -> str:
    content: list[dict[str, Any]] = [
        image_block(base64.b64encode(req.data).decode("ascii"), req.media_type),
        {"type": "text", "text": f"File: {rel} ({req.locator})"},
    ]
    result = await llm.structured(
        "vision",
        "vision",
        VisionResult,
        content,
        dry=lambda: VisionResult(transcript="", description=f"(dry-llm) image {rel} {req.locator}"),
    )
    parts = []
    if result.transcript.strip():
        parts.append(result.transcript.strip())
    if result.description.strip():
        parts.append(f"> {result.description.strip()}")
    return "\n\n".join(parts)


@dataclass
class ExtractStats:
    extracted: int = 0
    cached: int = 0
    failed: int = 0
    unsupported: int = 0
    kinds: dict[str, str] = field(default_factory=dict)  # hash -> kind


def text_path(ws: Workspace, h: str) -> Path:
    return ws.text_dir / f"{h}.md"


async def extract_one(ws: Workspace, entry: InventoryEntry, llm: LLM, want_images: bool) -> str:
    """Extract a single file; returns its kind. Raises on failure."""
    assert entry.hash
    path = ws.root / entry.path
    kind, magic_ext = detect_kind(path)
    if kind not in EXTRACTABLE:
        return kind
    ex = await asyncio.to_thread(run_extractor, path, kind, want_images, magic_ext)
    if ex.images:
        results = await asyncio.gather(*(_vision(llm, r, entry.path) for r in ex.images))
        if ex.page_texts is not None:  # splice OCR back into its pages
            by_loc = {r.locator: t for r, t in zip(ex.images, results, strict=True)}
            ex.text = "\n\n".join(
                f"## Page {i}\n\n{by_loc.get(f'p{i}', t)}" for i, t in enumerate(ex.page_texts, start=1)
            )
        else:
            ex.text = "\n\n".join(results)
    body, truncated = truncate(ex.text)
    meta = ExtractMeta(
        source=entry.path,
        type=ex.type,
        pages=ex.pages,
        chars=len(body),
        lang=detect_lang(body),
        quality=ex.quality,  # type: ignore[arg-type]
        truncated=truncated,
    )
    atomic_write_text(text_path(ws, entry.hash), frontmatter(meta) + body)
    return kind


async def extract_all(
    ws: Workspace, entries: list[InventoryEntry], llm: LLM, want_images: bool = True
) -> ExtractStats:
    stats = ExtractStats()
    seen: set[str] = set()
    todo: list[InventoryEntry] = []
    for e in entries:
        if e.skipped or not e.hash or e.hash in seen:
            continue
        seen.add(e.hash)
        if text_path(ws, e.hash).exists():
            stats.cached += 1
            stats.kinds[e.hash] = kind_by_ext(e.ext)
            continue
        todo.append(e)

    async def one(e: InventoryEntry) -> None:
        assert e.hash
        try:
            kind = await extract_one(ws, e, llm, want_images)
        except Exception as exc:
            stats.failed += 1
            ws.record_error(e.path, "extract", exc)
            return
        stats.kinds[e.hash] = kind
        if kind in EXTRACTABLE:
            stats.extracted += 1
        else:
            stats.unsupported += 1

    await asyncio.gather(*(one(e) for e in todo))
    return stats
