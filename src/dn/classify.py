# @covers AC-040, AC-041, AC-042, AC-043, AC-073, AC-106, AC-107, AC-108, AC-109, AC-113, AC-114, AC-115, AC-116, AC-117
"""Phase 3: LLM classification into the taxonomy, and taxonomy proposal (§5.3, §6.1, §6.2)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

import pathspec
import yaml

from dn.config import Taxonomy
from dn.extract import read_extracted, text_path
from dn.llm import LLM, LLMSchemaError
from dn.scan import gitignore_spec
from dn.schemas import BatchLabel, BatchLabels, InventoryEntry, Label, LabelRecord, TaxonomyProposal
from dn.workspace import Workspace, append_jsonl, atomic_write_text, read_jsonl

log = logging.getLogger("dn.classify")

HEAD_CHARS = 6000
PROPOSAL_SAMPLE = 500


# @assumption AS-033
def taxonomy_key(tax: Taxonomy | None) -> str:
    blob = json.dumps(tax.model_dump() if tax else None, sort_keys=True, ensure_ascii=False)
    return hashlib.blake2b(blob.encode(), digest_size=8).hexdigest()


def taxonomy_context(tax: Taxonomy | None) -> str:
    if tax is None:
        return "Categories: (none yet) - use `misc` and focus on an accurate title, summary and tags."
    lines = ["Categories (slug: name - description; examples; subcategories):"]
    for c in tax.categories:
        lines.append(
            f"- {c.slug}: {c.name} - {c.description}"
            + (f"; examples: {', '.join(c.examples)}" if c.examples else "")
            + (f"; subcategories: {', '.join(c.subcategories)}" if c.subcategories else "")
        )
    lines.append("- misc: anything that fits no category above")
    return "\n".join(lines)


def load_labels(ws: Workspace, key: str | None = None) -> dict[str, LabelRecord]:
    out: dict[str, LabelRecord] = {}
    for r in read_jsonl(ws.labels_path):
        try:
            rec = LabelRecord.model_validate(r)
        except Exception:
            continue
        if key is None or r.get("taxonomy_key") == key:
            out[rec.hash] = rec
    return out


# @assumption AS-031
def _dry_label(tax: Taxonomy | None, text: str, name: str) -> Label:
    """--dry-llm: deterministic keyword match against the taxonomy; schema-valid."""
    hay = (name + "\n" + text).casefold()
    best, score = "misc", 0
    for c in tax.categories if tax else []:
        words = {c.slug, *c.slug.split("-"), *c.name.casefold().split(), *c.subcategories}
        s = sum(hay.count(w.casefold()) for w in words if len(w) >= 3)
        if s > score:
            best, score = c.slug, s
    first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), name)
    return Label(
        category=best,
        confidence=0.9 if best != "misc" else 0.3,
        title=(first or name)[:40],
        summary=f"(dry-llm) {name}"[:200],
        tags=[best],
        has_domain_knowledge=best != "misc",
    )


def _unextractable_label(entry: InventoryEntry) -> Label:
    return Label(
        category="misc",
        confidence=0.0,
        title=Path(entry.path).name[:80],
        summary="No extractable text (binary, media or skipped file).",
        tags=[],
        has_domain_knowledge=False,
    )


@dataclass
class ClassifyStats:
    classified: int = 0
    cached: int = 0
    failed: int = 0
    by_rule: int = 0
    batches: int = 0
    low_confidence: list[str] = field(default_factory=list)


@dataclass
class _Item:
    h: str
    entry: InventoryEntry
    type: str
    lang: str
    head: str

    def describe(self) -> str:
        e = self.entry
        return (
            f"File name: {Path(e.path).name}\nPath: {e.path}\nType: {self.type}\n"
            f"Size: {e.size} bytes\nLanguage: {self.lang}\n\n"
            f"--- content (first {HEAD_CHARS} chars) ---\n{self.head}"
        )


def _rule_specs(ws: Workspace) -> list[tuple[str, str, pathspec.PathSpec]]:  # type: ignore[type-arg]
    """(glob, category, matcher) for every usable rule, in config order."""
    specs = []
    for r in ws.config.rules:
        if r.category != "misc" and (ws.taxonomy is None or ws.taxonomy.get(r.category) is None):
            continue  # reported by check_rules()
        specs.append((r.glob, r.category, gitignore_spec([r.glob])))
    return specs


def rule_labels(ws: Workspace, entries: list[InventoryEntry]) -> dict[str, tuple[InventoryEntry, Label]]:
    """AS-040: labels decided by config rules, per content hash (first matching path, first rule)."""
    specs = _rule_specs(ws)
    out: dict[str, tuple[InventoryEntry, Label]] = {}
    if not specs:
        return out
    for e in sorted(entries, key=lambda x: x.path):
        if not e.hash or e.hash in out:
            continue
        for glob, category, spec in specs:
            if spec.match_file(e.path):
                out[e.hash] = (
                    e,
                    Label(
                        category=category,
                        confidence=1.0,
                        title=Path(e.path).name[:80],
                        summary=f"Classified by rule `{glob}`."[:200],
                        tags=[],
                        has_domain_knowledge=category != "misc",
                    ),
                )
                break
    return out


# @assumption AS-038
def _batches(items: list[_Item], size: int, max_chars: int) -> list[list[_Item]]:
    """AS-038: greedy, path-ordered batches bounded by count and total content chars."""
    batches: list[list[_Item]] = []
    cur: list[_Item] = []
    chars = 0
    for it in items:
        if cur and (len(cur) >= size or chars + len(it.head) > max_chars):
            batches.append(cur)
            cur, chars = [], 0
        cur.append(it)
        chars += len(it.head)
    if cur:
        batches.append(cur)
    return batches


def batch_max_tokens(n: int) -> int:
    # @assumption AS-039
    return min(8000, 500 * n + 500)


async def classify(ws: Workspace, entries: list[InventoryEntry], llm: LLM) -> ClassifyStats:
    tax = ws.taxonomy
    key = taxonomy_key(tax)
    done = load_labels(ws, key)
    threshold = ws.config.confidence_threshold
    context = taxonomy_context(tax)
    categories = tax.slugs if tax else []
    stats = ClassifyStats()
    sem = asyncio.Semaphore(ws.config.concurrency)

    # one label per content hash; duplicates share the label (§5.3)
    firsts: dict[str, InventoryEntry] = {}
    for e in sorted(entries, key=lambda x: x.path):
        if e.hash and e.hash not in firsts:
            firsts[e.hash] = e

    def save(h: str, e: InventoryEntry, label: Label, llm_category: str | None) -> None:
        rec = LabelRecord(hash=h, path=e.path, label=label, llm_category=llm_category)
        append_jsonl(ws.labels_path, {**rec.model_dump(mode="json"), "taxonomy_key": key})
        done[h] = rec

    def record(it: _Item, label: Label) -> None:
        llm_category = label.category
        if label.confidence < threshold and label.category != "misc":
            label = label.model_copy(update={"category": "misc", "subcategory": None})
            stats.low_confidence.append(it.entry.path)
        save(it.h, it.entry, label, llm_category)
        stats.classified += 1

    # 1. rules: no LLM, re-evaluated every run and preferred over stored labels
    ruled = rule_labels(ws, entries)
    for h, (e, label) in ruled.items():
        prev = done.get(h)
        if prev is None or prev.label != label:
            save(h, e, label, None)
            stats.by_rule += 1

    # 2. what still needs the LLM
    items: list[_Item] = []
    for h, e in firsts.items():
        if h in ruled or h in done:
            continue
        tp = text_path(ws, h)
        if e.skipped or not tp.exists():
            save(h, e, _unextractable_label(e), None)
            continue
        meta, body = read_extracted(tp)
        items.append(_Item(h, e, meta.type, meta.lang, body[:HEAD_CHARS]))
    stats.cached = len(firsts) - len(items) - len([h for h in ruled if h in firsts])

    async def single(it: _Item) -> None:
        try:
            label = await llm.structured(
                "classify",
                "classify",
                Label,
                it.describe(),
                dry=lambda: _dry_label(tax, it.head, Path(it.entry.path).name),
                schema_overrides={"categories": categories},
                shared_context=context,
                validate=lambda lb: _check_category(lb, categories),
            )
        except Exception as exc:
            stats.failed += 1
            ws.record_error(it.entry.path, "classify", exc)
            return
        record(it, label)

    async def batch(group: list[_Item]) -> None:
        ids = {f"F{i + 1}": it for i, it in enumerate(group)}
        content = "\n\n".join(f"=== file_id: {fid} ===\n{it.describe()}" for fid, it in ids.items())

        def check(res: BatchLabels) -> None:
            got = [lb.file_id for lb in res.labels]
            missing = sorted(set(ids) - set(got))
            extra = sorted({g for g in got if g not in ids} | {g for g in got if got.count(g) > 1})
            if missing or extra:
                raise ValueError(
                    f"every file_id must appear exactly once; missing {missing}, unknown/duplicated {extra}"
                )
            for lb in res.labels:
                _check_category(lb, categories)

        def dry() -> BatchLabels:
            return BatchLabels(
                labels=[
                    BatchLabel(file_id=fid, **_dry_label(tax, it.head, Path(it.entry.path).name).model_dump())
                    for fid, it in ids.items()
                ]
            )

        try:
            res = await llm.structured(
                "classify",
                "classify_batch",
                BatchLabels,
                content,
                dry=dry,
                schema_overrides={"categories": categories},
                shared_context=context,
                validate=check,
                max_tokens=batch_max_tokens(len(group)),
            )
        except LLMSchemaError as exc:
            # @assumption AS-039 - fall back to one call per file
            log.info("batch of %d failed validation twice (%s); classifying one by one", len(group), exc)
            for it in group:
                await single(it)
            return
        except Exception as exc:
            for it in group:
                stats.failed += 1
                ws.record_error(it.entry.path, "classify", exc)
            return
        for lb in res.labels:  # saved together once the whole batch validated
            record(ids[lb.file_id], Label.model_validate(lb.model_dump(exclude={"file_id"})))

    async def run(group: list[_Item]) -> None:
        async with sem:
            stats.batches += 1
            if len(group) == 1:
                await single(group[0])
            else:
                await batch(group)

    groups = _batches(items, ws.config.classify_batch_size, ws.config.classify_batch_chars)
    await asyncio.gather(*(run(g) for g in groups))
    return stats


def _check_category(label: Label, categories: list[str]) -> None:
    if label.category != "misc" and label.category not in categories:
        raise ValueError(f"category {label.category!r} is not one of {categories + ['misc']}")


async def propose_taxonomy(ws: Workspace, entries: list[InventoryEntry], llm: LLM) -> Path:
    """Summaries first (classification without taxonomy), then one bundling call (§5.3, §6.2)."""
    await classify(ws, entries, llm)
    labels = list(load_labels(ws, taxonomy_key(ws.taxonomy)).values())
    rng = random.Random(0)
    if len(labels) > PROPOSAL_SAMPLE:
        labels = rng.sample(labels, PROPOSAL_SAMPLE)
    lines = [f"- {r.label.summary} [tags: {', '.join(r.label.tags)}]" for r in labels]
    content = f"{len(lines)} files:\n" + "\n".join(lines)

    def dry() -> TaxonomyProposal:
        from dn.schemas import ProposedCategory

        names = ["documents", "specs", "notes", "data", "media"]
        return TaxonomyProposal(
            categories=[
                ProposedCategory(slug=n, name=n.title(), description=f"(dry-llm) {n}", examples=[])
                for n in names
            ]
        )

    proposal = await llm.structured("propose", "propose_taxonomy", TaxonomyProposal, content, dry=dry)
    out = ws.dn_dir / "taxonomy.proposed.yaml"
    data = {"categories": [c.model_dump() for c in proposal.categories]}
    atomic_write_text(
        out,
        "# Proposed by `dn plan --propose-taxonomy`. Review, edit, then save as taxonomy.yaml.\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
    )
    return out
