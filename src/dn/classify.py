# @covers AC-040, AC-041, AC-042, AC-043, AC-073
"""Phase 3: LLM classification into the taxonomy, and taxonomy proposal (§5.3, §6.1, §6.2)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from dn.config import Taxonomy
from dn.extract import read_extracted, text_path
from dn.llm import LLM
from dn.schemas import InventoryEntry, Label, LabelRecord, TaxonomyProposal
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
    low_confidence: list[str] = field(default_factory=list)


async def classify(ws: Workspace, entries: list[InventoryEntry], llm: LLM) -> ClassifyStats:
    tax = ws.taxonomy
    key = taxonomy_key(tax)
    done = load_labels(ws, key)
    threshold = ws.config.confidence_threshold
    context = taxonomy_context(tax)
    categories = tax.slugs if tax else []
    stats = ClassifyStats()
    file_sem = asyncio.Semaphore(ws.config.concurrency)

    # one call per content hash; duplicates share the label (§5.3)
    firsts: dict[str, InventoryEntry] = {}
    for e in entries:
        if e.hash and e.hash not in firsts:
            firsts[e.hash] = e

    def save(h: str, e: InventoryEntry, label: Label, llm_category: str | None) -> None:
        rec = LabelRecord(hash=h, path=e.path, label=label, llm_category=llm_category)
        append_jsonl(ws.labels_path, {**rec.model_dump(mode="json"), "taxonomy_key": key})
        done[h] = rec

    async def one(h: str, e: InventoryEntry) -> None:
        tp = text_path(ws, h)
        if e.skipped or not tp.exists():
            save(h, e, _unextractable_label(e), None)
            return
        meta, body = read_extracted(tp)
        head = body[:HEAD_CHARS]
        content = (
            f"File name: {Path(e.path).name}\nPath: {e.path}\nType: {meta.type}\n"
            f"Size: {e.size} bytes\nLanguage: {meta.lang}\n\n--- content (first {HEAD_CHARS} chars) ---\n{head}"
        )
        async with file_sem:
            try:
                label = await llm.structured(
                    "classify",
                    "classify",
                    Label,
                    content,
                    dry=lambda: _dry_label(tax, head, Path(e.path).name),
                    schema_overrides={"categories": categories},
                    shared_context=context,
                    validate=lambda lb: _check_category(lb, categories),
                )
            except Exception as exc:
                stats.failed += 1
                ws.record_error(e.path, "classify", exc)
                return
        llm_category = label.category
        if label.confidence < threshold and label.category != "misc":
            label = label.model_copy(update={"category": "misc", "subcategory": None})
            stats.low_confidence.append(e.path)
        save(h, e, label, llm_category)
        stats.classified += 1

    todo = [(h, e) for h, e in firsts.items() if h not in done]
    stats.cached = len(firsts) - len(todo)
    await asyncio.gather(*(one(h, e) for h, e in todo))
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
