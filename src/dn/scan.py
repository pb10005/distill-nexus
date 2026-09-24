# @covers AC-014, AC-015, AC-016, AC-017, AC-018, AC-098
"""Phase 1: walk the target, hash files, and write ``inventory.jsonl``."""

from __future__ import annotations

import hashlib
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pathspec

from dn.config import TAXONOMY_NAME
from dn.platform import is_hidden, is_link, long_path
from dn.schemas import InventoryEntry
from dn.workspace import Workspace, read_jsonl, write_jsonl

log = logging.getLogger("dn.scan")

HEAD_BYTES = 4 * 1024 * 1024
EXCLUDED_DIRS = {".git", ".dn", "node_modules", "__pycache__"}
EXCLUDED_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
IGNORE_FILE = ".dnignore"  # @assumption AS-001


def hash_file(path: Path, full: bool) -> str:
    """blake2b over the first 4MB (or everything) followed by the size."""
    h = hashlib.blake2b(digest_size=20)
    size = 0
    with open(long_path(path), "rb") as f:
        remaining = None if full else HEAD_BYTES
        while True:
            chunk = f.read(1 << 20 if remaining is None else min(1 << 20, remaining))
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
            if remaining is not None:
                remaining -= len(chunk)
                if remaining <= 0:
                    break
    h.update(str(os.path.getsize(long_path(path))).encode())
    return h.hexdigest()


@dataclass
class ScanResult:
    entries: list[InventoryEntry]
    hash_computed: int = 0
    added: int = 0
    changed: int = 0
    removed: int = 0
    duplicates: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    @property
    def changed_total(self) -> int:
        return self.added + self.changed + self.removed

    def summary(self) -> dict[str, object]:
        by_ext: dict[str, int] = defaultdict(int)
        for e in self.entries:
            by_ext[e.ext or "(none)"] += 1
        return {
            "files": len(self.entries),
            "by_type": dict(sorted(by_ext.items())),
            "hash_computed": self.hash_computed,
            "added": self.added,
            "changed": self.changed,
            "removed": self.removed,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
        }


def _ignore_spec(ws: Workspace) -> pathspec.PathSpec:  # type: ignore[type-arg]
    patterns = list(ws.config.exclude)
    ignore = ws.root / IGNORE_FILE
    if ignore.exists():
        patterns += ignore.read_text(encoding="utf-8").splitlines()
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def _generated_paths(ws: Workspace) -> set[Path]:
    """Outputs of dn itself that must never be re-ingested."""
    return {ws.knowledge_dir, ws.duplicates_dir, ws.manifest_path, ws.dn_dir, ws.root / TAXONOMY_NAME}


def walk(ws: Workspace, follow_symlinks: bool = False, include_hidden: bool = False) -> list[Path]:
    spec = _ignore_spec(ws)
    generated = _generated_paths(ws)
    found: list[Path] = []

    def visit(d: Path) -> None:
        try:
            it = os.scandir(long_path(d))
        except OSError as e:
            log.warning("cannot read directory %s: %s", d, e)
            return
        with it:
            for entry in sorted(it, key=lambda e: e.name):
                p = d / entry.name
                if p in generated:
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as e:
                    log.warning("cannot stat %s: %s", p, e)
                    continue
                if is_link(p, st) and not follow_symlinks:
                    continue
                if not include_hidden and is_hidden(p, st):
                    continue
                rel = p.relative_to(ws.root).as_posix()
                is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
                if is_dir:
                    if entry.name in EXCLUDED_DIRS or spec.match_file(rel + "/"):
                        continue
                    visit(p)
                    continue
                if entry.name in EXCLUDED_FILES or entry.name.startswith("~$") or spec.match_file(rel):
                    continue
                if entry.is_file(follow_symlinks=follow_symlinks):
                    found.append(p)

    visit(ws.root)
    return found


def load_inventory(ws: Workspace) -> list[InventoryEntry]:
    return [InventoryEntry.model_validate(r) for r in read_jsonl(ws.inventory_path)]


def scan(
    ws: Workspace, follow_symlinks: bool = False, include_hidden: bool = False, full_hash: bool = False
) -> ScanResult:
    previous = {e.path: e for e in load_inventory(ws)}
    max_bytes = ws.config.max_file_mb * 1024 * 1024
    result = ScanResult(entries=[])
    for p in walk(ws, follow_symlinks, include_hidden):
        rel = p.relative_to(ws.root).as_posix()
        try:
            st = os.stat(long_path(p))
        except OSError as e:
            ws.record_error(rel, "scan", e)
            continue
        entry = InventoryEntry(path=rel, size=st.st_size, mtime_ns=st.st_mtime_ns, ext=p.suffix.lower())
        if st.st_size > max_bytes:
            entry.skipped = "too_large"
            result.skipped.append(rel)
            log.info("skipping %s: larger than %s MB", rel, ws.config.max_file_mb)
        else:
            prev = previous.get(rel)
            reusable = (
                prev is not None
                and prev.hash is not None
                and prev.size == st.st_size
                and prev.mtime_ns == st.st_mtime_ns
                and (not full_hash or prev.hash_mode == "full")
            )
            if reusable and prev is not None:
                entry.hash, entry.hash_mode = prev.hash, prev.hash_mode
            else:
                try:
                    entry.hash = hash_file(p, full=full_hash)
                except OSError as e:
                    ws.record_error(rel, "scan", e)
                    continue
                entry.hash_mode = "full" if (full_hash or st.st_size <= HEAD_BYTES) else "head4m"
                result.hash_computed += 1
        if rel not in previous:
            result.added += 1
        elif previous[rel].hash != entry.hash or previous[rel].size != entry.size:
            result.changed += 1
        result.entries.append(entry)
    result.removed = len(set(previous) - {e.path for e in result.entries})

    _confirm_head_collisions(ws, result)
    groups: dict[str, list[str]] = defaultdict(list)
    for ent in result.entries:
        if ent.hash:
            groups[ent.hash].append(ent.path)
    result.duplicates = {h: paths for h, paths in groups.items() if len(paths) > 1}
    write_jsonl(ws.inventory_path, (e.model_dump() for e in result.entries))
    return result


def _confirm_head_collisions(ws: Workspace, result: ScanResult) -> None:
    """A head-4MB match on files >4MB is only a candidate: confirm with a full hash."""
    groups: dict[str, list[InventoryEntry]] = defaultdict(list)
    for e in result.entries:
        if e.hash and e.hash_mode == "head4m":
            groups[e.hash].append(e)
    for members in groups.values():
        if len(members) < 2:
            continue
        for e in members:
            e.hash = hash_file(ws.root / e.path, full=True)
            e.hash_mode = "full"
            result.hash_computed += 1
