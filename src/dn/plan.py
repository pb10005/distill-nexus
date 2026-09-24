# @covers AC-044, AC-045, AC-046, AC-047, AC-048, AC-085, AC-086, AC-087, AC-088
"""Phase 4: turn labels into a dry-run move plan (``plan.json``). Touches no files."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

from dn.classify import load_labels, taxonomy_key
from dn.errors import DnError
from dn.platform import fit_path, name_key, normalize, safe_name, slugify
from dn.schemas import InventoryEntry, LabelRecord, Plan, PlanEntry
from dn.workspace import Workspace, atomic_write_text

log = logging.getLogger("dn.plan")


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _valid_subcategory(ws: Workspace, category: str, sub: str | None) -> str | None:
    # @assumption AS-026
    if not sub or ws.taxonomy is None:
        return None
    cat = ws.taxonomy.get(category)
    return sub if cat is not None and sub in cat.subcategories else None


def _file_name(entry: InventoryEntry, rec: LabelRecord | None, rename: bool) -> str:
    original = Path(entry.path).name
    if not rename or rec is None:
        return safe_name(normalize(original))
    # @assumption AS-027
    ext = Path(original).suffix.lower()
    slug = slugify(rec.label.title) or slugify(Path(original).stem) or "file"
    date = rec.label.date
    return safe_name(f"{slug}_{date}{ext}" if date else f"{slug}{ext}")


def _with_suffix(name: str, n: int) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return f"{name}-{n}"
    return f"{stem}-{n}.{ext}"


class _Allocator:
    """Assigns collision-free destinations (casefold + NFC comparison, and on-disk files)."""

    def __init__(self, sources: set[str]) -> None:
        self.used: set[str] = set()
        self.sources = sources  # name keys of files that will leave their location

    def reserve(self, path: Path) -> None:
        self.used.add(name_key(path.as_posix()))

    def allocate(self, folder: Path, name: str) -> Path:
        name = fit_path(folder, name)
        n = 1
        candidate = folder / name
        while True:
            key = name_key(candidate.as_posix())
            on_disk = candidate.exists() and key not in self.sources
            if key not in self.used and not on_disk:
                self.used.add(key)
                return candidate
            n += 1
            candidate = folder / fit_path(folder, _with_suffix(name, n))


def _in_place(src: Path, folder: Path, name: str) -> bool:
    """The file already sits in its destination folder under its name (or a -N variant)."""
    if name_key(src.parent.as_posix()) != name_key(folder.as_posix()):
        return False
    want = name_key(fit_path(folder, name))
    have = name_key(src.name)
    if have == want:
        return True
    stem, dot, ext = want.rpartition(".")
    base, ext_have = (stem, "." + ext) if dot and stem else (want, "")
    if not have.endswith(ext_have):
        return False
    middle = have[: len(have) - len(ext_have)] if ext_have else have
    return middle.startswith(base + "-") and middle[len(base) + 1 :].isdigit()


def build_plan(
    ws: Workspace,
    entries: list[InventoryEntry],
    *,
    copy: bool = False,
    rename: bool | None = None,
    dedupe: str | None = None,
    ask: Callable[[InventoryEntry, LabelRecord], str | None] | None = None,
) -> Plan:
    rename = ws.config.rename if rename is None else rename
    dedupe = dedupe or ws.config.dedupe
    labels = load_labels(ws, taxonomy_key(ws.taxonomy))
    out = ws.out
    ordered = sorted((e for e in entries if e.hash), key=lambda e: e.path)
    plan = Plan(root=ws.root.as_posix(), out_root=out.as_posix(), dedupe=dedupe, copy_mode=copy)  # type: ignore[arg-type]
    op_main = "copy" if copy else "move"

    # pass 1: decide category / folder / name, find duplicates and files already in place
    seen: dict[str, str] = {}
    decided: list[
        tuple[InventoryEntry, str, float, Path | None, str, str]
    ] = []  # e, cat, conf, folder, name, kind
    for e in ordered:
        assert e.hash
        rec = labels.get(e.hash)
        category = rec.label.category if rec else "misc"
        confidence = rec.label.confidence if rec else 0.0
        if (
            rec is not None
            and ask is not None
            and category == "misc"
            and confidence < ws.config.confidence_threshold
        ):
            chosen = ask(e, rec)
            if chosen and ws.taxonomy and ws.taxonomy.get(chosen):
                category = chosen
        src = ws.root / e.path
        if e.hash in seen:
            decided.append((e, category, confidence, None, safe_name(normalize(src.name)), "dup"))
            continue
        seen[e.hash] = e.path
        folder = out / safe_name(category)
        sub = _valid_subcategory(ws, category, rec.label.subcategory if rec else None)
        if sub:
            folder = folder / safe_name(sub)
        if rec and rec.label.date:
            folder = folder / rec.label.date[:4]
        name = _file_name(e, rec, rename)
        kind = "noop" if _in_place(src, folder, name) else "main"
        decided.append((e, category, confidence, folder, name, kind))

    leaving = {
        name_key((ws.root / e.path).as_posix())
        for e, _c, _f, _fo, _n, kind in decided
        if (kind == "main" and not copy) or (kind == "dup" and dedupe in ("move", "trash"))
    }
    alloc = _Allocator(leaving)
    for e, _c, _f, _fo, _n, kind in decided:
        if kind == "noop" or (kind == "dup" and dedupe == "keep") or (kind == "main" and copy):
            alloc.reserve(ws.root / e.path)

    # pass 2: allocate destinations
    for e, category, confidence, dest_folder, name, kind in decided:
        assert e.hash
        src = ws.root / e.path
        if kind == "dup":
            why = f"duplicate of {seen[e.hash]}"
            if dedupe == "keep":
                plan.entries.append(_entry(ws, e, None, "skip", category, confidence, why))
            elif dedupe == "trash":
                plan.entries.append(_entry(ws, e, None, "trash", category, confidence, why))
            else:
                dest = alloc.allocate(ws.duplicates_dir, name)
                plan.entries.append(_entry(ws, e, dest, "move", category, confidence, why))
        elif kind == "noop":
            plan.entries.append(_entry(ws, e, src, "noop", category, confidence, "already in place"))
        else:
            assert dest_folder is not None
            dest = alloc.allocate(dest_folder, name)
            plan.entries.append(_entry(ws, e, dest, op_main, category, confidence, ""))

    for pe in plan.entries:  # nothing may leave the output root (LLM-derived names)
        if pe.to is not None and pe.op != "noop" and not _is_within(ws.abs(pe.to), out):
            raise DnError(f"refusing plan: destination escapes {out}: {pe.to}")
    return plan


def _entry(
    ws: Workspace,
    e: InventoryEntry,
    dest: Path | None,
    op: str,
    category: str,
    confidence: float,
    reason: str,
) -> PlanEntry:
    assert e.hash
    return PlanEntry.model_validate(
        {
            "from": e.path,
            "to": ws.rel(dest) if dest is not None else None,
            "hash": e.hash,
            "hash_mode": e.hash_mode,
            "op": op,
            "category": category,
            "confidence": confidence,
            "reason": reason,
        }
    )


def save_plan(ws: Workspace, plan: Plan) -> None:
    atomic_write_text(ws.plan_path, json.dumps(plan.model_dump(by_alias=True), ensure_ascii=False, indent=2))


def load_plan(ws: Workspace) -> Plan:
    if not ws.plan_path.exists():
        raise DnError(f"no plan found at {ws.plan_path}; run `dn plan` first", exit_code=2)
    return Plan.model_validate_json(ws.plan_path.read_text(encoding="utf-8"))


def plan_summary(plan: Plan) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pe in plan.entries:
        counts[pe.op] = counts.get(pe.op, 0) + 1
    return counts
