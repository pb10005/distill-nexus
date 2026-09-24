# @covers AC-058, AC-059, AC-060, AC-061, AC-062, AC-064, AC-065, AC-066, AC-067, AC-069, AC-100
"""Phase 7: merge all facts into ``_knowledge/`` and validate it (§5.7, §6.4).

``_knowledge/`` is regenerated from scratch every run; only ``<!-- manual -->``
blocks in ``topics/*.md`` survive (re-attached to the same slug).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml

from dn.chunks import make_chunks
from dn.classify import load_labels, taxonomy_key
from dn.distill import facts_path
from dn.errors import DnError
from dn.extract import read_extracted, text_path
from dn.llm import LLM, LLMSchemaError, estimate_tokens
from dn.platform import slugify
from dn.schemas import FactsFile, InventoryEntry, MarkdownDoc, SamePair, SamePairs, Topics, TopicSpec
from dn.workspace import Workspace, atomic_write_text

log = logging.getLogger("dn.synthesize")

PAIR_BATCH = 50
SIMILARITY = 0.8
MANUAL_RE = re.compile(r"<!-- manual -->.*?<!-- /manual -->", re.S)
SRC_RE = re.compile(r"\[src:\s*([^\]]*)\]")
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
HTML_RE = re.compile(r"<(?!!-- /?manual -->)[A-Za-z!/][^>]*>")


# ------------------------------------------------------------------ text helpers


def md_escape(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def sanitize_md(md: str) -> str:
    """Enforce the AI-input rules on LLM-written Markdown: headings <= ###, no HTML."""
    out: list[str] = []
    in_code = False
    for line in md.replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if not in_code:
            m = re.match(r"^(#{4,})\s+(.*)$", line)
            if m:
                line = "### " + m.group(2)
            line = HTML_RE.sub("", line)
        out.append(line)
    return "\n".join(out).strip() + "\n"


def sort_key(term: str) -> tuple[int, str]:
    """Latin alphabet first, then kana order (katakana folded to hiragana), then the rest."""
    s = unicodedata.normalize("NFKC", term).strip()
    first = s[:1]
    if first.isascii() and first.isalpha():
        group = 0
    elif first.isascii():
        group = 1
    else:
        group = 2
    folded = "".join(chr(ord(c) - 0x60) if "\u30a1" <= c <= "\u30f6" else c for c in s.casefold())
    return group, folded


def frontmatter(data: dict[str, Any]) -> str:
    return "---\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False) + "---\n\n"


def parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return None, text
    try:
        data = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None, text
    return (data if isinstance(data, dict) else None), text[end + 5 :]


# ------------------------------------------------------------------ loading


@dataclass
class Knowledge:
    lang: str
    files: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # hash -> {path, type, summary, quality, tags, category}
    terms: list[dict[str, Any]] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)


def _src_str(src: dict[str, Any], path: str) -> str:
    loc = src.get("locator") or ""
    return f"{path}#{loc}" if loc else path


# @assumption AS-019 - source = current path + '#locator'
def load_knowledge(ws: Workspace, entries: list[InventoryEntry]) -> Knowledge:
    labels = load_labels(ws, taxonomy_key(ws.taxonomy))
    paths: dict[str, str] = {}
    for e in sorted(entries, key=lambda e: e.path):
        if e.hash and e.hash not in paths:
            paths[e.hash] = e.path
    langs: Counter[str] = Counter()
    k = Knowledge(lang="en")
    for h, path in paths.items():
        rec = labels.get(h)
        info: dict[str, Any] = {
            "path": path,
            "type": "",
            "quality": "",
            "summary": rec.label.summary if rec else "",
            "tags": rec.label.tags if rec else [],
            "category": rec.label.category if rec else "misc",
        }
        tp = text_path(ws, h)
        if tp.exists():
            meta, body = read_extracted(tp)
            info.update(
                type=meta.type,
                quality=meta.quality + (" (truncated)" if meta.truncated else ""),
                lang=meta.lang,
            )
            langs[meta.lang] += len(body)
        k.files[h] = info
        fp = facts_path(ws, h)
        if not fp.exists():
            continue
        ff = FactsFile.model_validate_json(fp.read_text(encoding="utf-8"))
        # AS-019: source paths are re-attached from the current inventory (post-apply paths)
        for bucket, items in (
            ("terms", ff.terms),
            ("entities", ff.entities),
            ("facts", ff.facts),
            ("relations", ff.relations),
            ("questions", ff.questions),
        ):
            for it in items:
                it = dict(it)
                it["source"] = _src_str(it.get("source") or {}, path)
                it["_path"] = path
                it["_hash"] = h
                getattr(k, bucket).append(it)
    if ws.config.lang != "auto":
        k.lang = ws.config.lang
    elif langs:
        ja = langs.get("ja", 0)
        k.lang = "ja" if ja and ja >= sum(v for kk, v in langs.items() if kk != "ja") else "en"
    return k


# ------------------------------------------------------------------ normalization


# @assumption AS-017
def candidate_pairs(names: list[str]) -> list[tuple[str, str]]:
    """AS-017: similarity >= 0.8, or at least one shared whitespace token."""
    uniq = sorted(set(names))
    pairs: list[tuple[str, str]] = []
    tokens = {n: {t for t in n.casefold().split() if len(t) >= 2} for n in uniq}
    for i, a in enumerate(uniq):
        for b in uniq[i + 1 :]:
            if SequenceMatcher(None, a.casefold(), b.casefold()).ratio() >= SIMILARITY or (
                tokens[a] & tokens[b]
            ):
                pairs.append((a, b))
    return pairs


class _UF:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.canonical: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str, canonical: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra
        self.canonical[self.find(a)] = canonical


async def normalize_names(llm: LLM, names: list[str]) -> dict[str, str]:
    """Map every name to its canonical form using LLM same-pair judgments."""
    uf = _UF()
    for n in names:
        uf.find(n)
    pairs = candidate_pairs(names)
    batches = [pairs[i : i + PAIR_BATCH] for i in range(0, len(pairs), PAIR_BATCH)]

    async def judge(batch: list[tuple[str, str]]) -> SamePairs:
        content = "Candidate pairs:\n" + "\n".join(
            json.dumps({"a": a, "b": b}, ensure_ascii=False) for a, b in batch
        )

        def dry() -> SamePairs:
            return SamePairs(
                pairs=[SamePair(a=a, b=b, same=a.casefold() == b.casefold(), canonical=a) for a, b in batch]
            )

        return await llm.structured("synthesize", "normalize", SamePairs, content, dry=dry)

    results = await asyncio.gather(*(judge(b) for b in batches))
    valid = set(names)
    for res in results:
        for p in res.pairs:
            if p.same and p.a in valid and p.b in valid:
                canon = p.canonical if p.canonical in (p.a, p.b) else p.a
                uf.union(p.a, p.b, canon)
    mapping: dict[str, str] = {}
    for n in names:
        root = uf.find(n)
        mapping[n] = uf.canonical.get(root, root)
    return mapping


def merge_terms(terms: list[dict[str, Any]], canon: dict[str, str]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for t in terms:
        name = canon.get(t["term"], t["term"])
        m = merged.setdefault(
            name,
            {
                "term": name,
                "definition": t.get("definition", ""),
                "aliases": [],
                "sources": [],
                "first_source": t["source"],
            },
        )
        for a in [t["term"], *t.get("aliases", [])]:
            if a != name and a not in m["aliases"]:
                m["aliases"].append(a)
        if t["source"] not in m["sources"]:
            m["sources"].append(t["source"])
        if not m["definition"]:
            m["definition"] = t.get("definition", "")
    out = sorted(merged.values(), key=lambda m: sort_key(m["term"]))
    for i, m in enumerate(out, start=1):
        m["id"] = f"T-{i:03d}"
    return out


def merge_entities(entities: list[dict[str, Any]], canon: dict[str, str]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for e in entities:
        name = canon.get(e["name"], e["name"])
        m = merged.setdefault(
            name,
            {
                "type": e.get("type", "other"),
                "name": name,
                "aliases": [],
                "description": e.get("description", ""),
                "sources": [],
            },
        )
        if e["name"] != name and e["name"] not in m["aliases"]:
            m["aliases"].append(e["name"])
        if e["source"] not in m["sources"]:
            m["sources"].append(e["source"])
    out = sorted(merged.values(), key=lambda m: sort_key(m["name"]))
    return [{"id": f"E-{i:03d}", **m} for i, m in enumerate(out, start=1)]


# ------------------------------------------------------------------ topics


async def split_topics(
    llm: LLM, k: Knowledge, entities: list[dict[str, Any]], facts: list[dict[str, Any]]
) -> list[TopicSpec]:
    tag_pairs: Counter[tuple[str, str]] = Counter()
    for f in k.files.values():
        tags = sorted(set(f["tags"]))
        for i, a in enumerate(tags):
            for b in tags[i + 1 :]:
                tag_pairs[(a, b)] += 1
    content = (
        "Entities:\n"
        + "\n".join(f"{e['id']}: {e['name']} ({e['type']}) - {e['description'][:120]}" for e in entities)
        + "\n\nFacts:\n"
        + "\n".join(f"{f['id']}: {f['statement'][:200]}" for f in facts)
        + "\n\nTag co-occurrence (top 50):\n"
        + "\n".join(f"{a} + {b}: {n}" for (a, b), n in tag_pairs.most_common(50))
    )

    def dry() -> Topics:
        by_cat: dict[str, TopicSpec] = {}
        path_cat = {f["path"]: f["category"] for f in k.files.values()}
        for e in entities:
            cat = path_cat.get(e["sources"][0].split("#", 1)[0], "general") if e["sources"] else "general"
            by_cat.setdefault(cat, TopicSpec(slug=cat, name=cat)).entity_ids.append(e["id"])
        for f in facts:
            cat = path_cat.get(f["source"].split("#", 1)[0], "general")
            by_cat.setdefault(cat, TopicSpec(slug=cat, name=cat)).fact_ids.append(f["id"])
        return Topics(topics=[t for t in by_cat.values() if t.slug != "misc"])

    res = await llm.structured("synthesize", "topics", Topics, content, dry=dry)
    ent_ids = {e["id"] for e in entities}
    fact_ids = {f["id"] for f in facts}
    topics: list[TopicSpec] = []
    used_slugs: set[str] = set()
    assigned_e: set[str] = set()
    assigned_f: set[str] = set()
    for t in res.topics:
        slug = slugify(t.slug) or slugify(t.name) or f"topic-{len(topics) + 1}"
        if slug == "general" or slug in used_slugs:
            slug = f"{slug}-{len(topics) + 1}"
        used_slugs.add(slug)
        spec = TopicSpec(
            slug=slug,
            name=t.name or slug,
            description=t.description,
            entity_ids=[i for i in t.entity_ids if i in ent_ids],
            fact_ids=[i for i in t.fact_ids if i in fact_ids],
        )
        assigned_e |= set(spec.entity_ids)
        assigned_f |= set(spec.fact_ids)
        topics.append(spec)
    rest_e = [e["id"] for e in entities if e["id"] not in assigned_e]
    rest_f = [f["id"] for f in facts if f["id"] not in assigned_f]
    if rest_e or rest_f or not topics:
        topics.append(
            TopicSpec(
                slug="general",
                name="General",
                description="Knowledge not assigned to another topic.",
                entity_ids=rest_e,
                fact_ids=rest_f,
            )
        )
    return topics


# ------------------------------------------------------------------ writers


@dataclass
class SynthResult:
    files: list[str] = field(default_factory=list)
    topics: int = 0
    failed_topics: list[str] = field(default_factory=list)
    split: bool = False
    tokens: int = 0


L10N = {
    "ja": {
        "glossary": "用語集",
        "term": "用語",
        "definition": "定義",
        "aliases": "別名",
        "first": "初出ソース",
        "sources": "ソース一覧",
        "path": "整理後パス",
        "type": "種別",
        "summary": "要約",
        "quality": "抽出品質",
        "questions": "未解決の論点",
        "conflicts": "ソース間の矛盾",
        "gaps": "欠落・曖昧な情報",
        "facts": "根拠となる事実",
        "entities": "エンティティ",
        "topics": "トピック",
        "files": "ナレッジファイル",
        "overview": "概要",
        "more": "全件",
    },
    "en": {
        "glossary": "Glossary",
        "term": "Term",
        "definition": "Definition",
        "aliases": "Aliases",
        "first": "First source",
        "sources": "Sources",
        "path": "Organized path",
        "type": "Type",
        "summary": "Summary",
        "quality": "Extraction quality",
        "questions": "Open questions",
        "conflicts": "Conflicts between sources",
        "gaps": "Missing or ambiguous information",
        "facts": "Supporting facts",
        "entities": "Entities",
        "topics": "Topics",
        "files": "Knowledge files",
        "overview": "Overview",
        "more": "Full list",
    },
}


def glossary_md(
    terms: list[dict[str, Any]], t: dict[str, str], limit: int | None = None, more_link: str | None = None
) -> str:
    rows = terms if limit is None else terms[:limit]
    lines = [
        f"# {t['glossary']}",
        "",
        f"| {t['term']} | {t['definition']} | {t['aliases']} | {t['first']} |",
        "|---|---|---|---|",
    ]
    for m in rows:
        lines.append(
            f"| {md_escape(m['term'])} | {md_escape(m['definition'])} | {md_escape(', '.join(m['aliases']))} | {md_escape(m['first_source'])} |"
        )
    if limit is not None and len(terms) > limit and more_link:
        lines += ["", f"{t['more']} ({len(terms)}): [{more_link}]({more_link})"]
    return "\n".join(lines) + "\n"


def _rel_link(from_dir: Path, target: Path) -> str:
    return Path(os.path.relpath(target, from_dir)).as_posix()


def sources_md(ws: Workspace, k: Knowledge, t: dict[str, str]) -> str:
    lines = [
        f"# {t['sources']}",
        "",
        f"| {t['path']} | {t['type']} | {t['summary']} | {t['quality']} |",
        "|---|---|---|---|",
    ]
    for info in sorted(k.files.values(), key=lambda i: i["path"]):
        p = info["path"]
        target = ws.abs(p)
        cell = f"[{md_escape(p)}]({_rel_link(ws.knowledge_dir, target)})" if target.exists() else md_escape(p)
        lines.append(
            f"| {cell} | {md_escape(info['type'])} | {md_escape(info['summary'])} | {md_escape(info['quality'])} |"
        )
    return "\n".join(lines) + "\n"


def conflicts(facts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for f in facts:
        groups[(f["subject"].strip().casefold(), f["predicate"].strip().casefold())].append(f)
    return [g for g in groups.values() if len({f["object"].strip().casefold() for f in g}) > 1]


# @assumption AS-036
def open_questions_md(k: Knowledge, facts: list[dict[str, Any]], t: dict[str, str]) -> str:
    lines = [f"# {t['questions']}", "", f"## {t['conflicts']}", ""]
    cs = conflicts(facts)
    if not cs:
        lines.append("- (none)")
    for g in cs:
        lines.append(f"### {md_escape(g[0]['subject'])} / {md_escape(g[0]['predicate'])}")
        lines.append("")
        for f in g:
            lines.append(f"- {f['id']}: {md_escape(f['statement'])} (source: {f['source']})")
        lines.append("")
    lines += ["", f"## {t['gaps']}", ""]
    if not k.questions:
        lines.append("- (none)")
    for q in k.questions:
        why = f" - {md_escape(q.get('why', ''))}" if q.get("why") else ""
        lines.append(f"- {md_escape(q['question'])}{why} (source: {q['source']})")
    return "\n".join(lines) + "\n"


# @assumption AS-018
def harvest_manual(kdir: Path) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    tdir = kdir / "topics"
    if tdir.is_dir():
        for p in tdir.glob("*.md"):
            found = MANUAL_RE.findall(p.read_text(encoding="utf-8"))
            if found:
                blocks[p.stem] = found
    return blocks


# ------------------------------------------------------------------ main


async def synthesize(
    ws: Workspace, entries: list[InventoryEntry], llm: LLM, synth_llm: LLM | None = None
) -> SynthResult:
    synth_llm = synth_llm or llm
    k = load_knowledge(ws, entries)
    t = L10N["ja" if k.lang == "ja" else "en"]
    result = SynthResult()
    kdir = ws.knowledge_dir
    manual = harvest_manual(kdir)

    # 1. normalization
    names = [x["term"] for x in k.terms] + [x["name"] for x in k.entities]
    canon = await normalize_names(llm, names)
    terms = merge_terms(k.terms, canon)
    entities = merge_entities(k.entities, canon)
    facts = [
        {
            "id": f"F-{i:03d}",
            "statement": f["statement"],
            "subject": canon.get(f["subject"], f["subject"]),
            "predicate": f["predicate"],
            "object": canon.get(f["object"], f["object"]),
            "source": f["source"],
            "span": f["source"].split("#", 1)[1] if "#" in f["source"] else "",
            "confidence": f.get("confidence", 0.8),
        }
        for i, f in enumerate(k.facts, start=1)
    ]
    ent_by_name = {e["name"]: e["id"] for e in entities}
    relations = [
        {
            "id": f"R-{i:03d}",
            "from": canon.get(r["from"], r["from"]),
            "relation": r["relation"],
            "to": canon.get(r["to"], r["to"]),
            "from_id": ent_by_name.get(canon.get(r["from"], r["from"])),
            "to_id": ent_by_name.get(canon.get(r["to"], r["to"])),
            "source": r["source"],
        }
        for i, r in enumerate(k.relations, start=1)
    ]

    # 2. topics
    topics = await split_topics(llm, k, entities, facts)

    # 3. topic documents (validated src ids; one regeneration, then skip)
    ent_by_id = {e["id"]: e for e in entities}
    fact_by_id = {f["id"]: f for f in facts}

    async def topic_doc(spec: TopicSpec) -> tuple[TopicSpec, str | None]:
        tfacts = [fact_by_id[i] for i in spec.fact_ids]
        tents = [ent_by_id[i] for i in spec.entity_ids]
        names_in = {e["name"] for e in tents}
        tterms = [m for m in terms if m["term"] in names_in or any(a in names_in for a in m["aliases"])]
        allowed = {f["id"] for f in tfacts} | {e["id"] for e in tents} | {m["id"] for m in tterms}
        content = (
            f"Topic: {spec.name}\nDescription: {spec.description}\n\nFacts:\n"
            + "\n".join(f"{f['id']}: {f['statement']} (source: {f['source']})" for f in tfacts)
            + "\n\nEntities:\n"
            + "\n".join(f"{e['id']}: {e['name']} - {e['description']}" for e in tents)
            + "\n\nTerms:\n"
            + "\n".join(f"{m['id']}: {m['term']} - {m['definition']}" for m in tterms)
        )

        def check(doc: MarkdownDoc) -> None:
            bad = sorted(
                {i.strip() for m in SRC_RE.finditer(doc.markdown) for i in m.group(1).split(",")}
                - allowed
                - {""}
            )
            if bad:
                raise ValueError(f"unknown src ids: {', '.join(bad)}; use only {', '.join(sorted(allowed))}")

        def dry() -> MarkdownDoc:
            body = "\n\n".join(f"{f['statement']} [src: {f['id']}]" for f in tfacts[:20])
            return MarkdownDoc(markdown=f"## {spec.name}\n\n{spec.description or ''}\n\n{body}\n")

        try:
            doc = await llm.structured(
                "synthesize", "topic_doc", MarkdownDoc, content, dry=dry, validate=check
            )
        except LLMSchemaError as exc:
            ws.record_error(f"_knowledge/topics/{spec.slug}.md", "synthesize", exc)
            return spec, None
        return spec, doc.markdown

    docs = await asyncio.gather(*(topic_doc(s) for s in topics))

    # 4. overview and index with the synthesis model
    written = [(s, md) for s, md in docs if md is not None]
    result.failed_topics = [s.slug for s, md in docs if md is None]
    topic_digest = "\n\n".join(f"# {s.name}\n{md[:4000]}" for s, md in written)
    overview = await synth_llm.structured(
        "synthesize",
        "overview",
        MarkdownDoc,
        topic_digest or "(no topics)",
        dry=lambda: MarkdownDoc(
            markdown="\n".join(f"## {s.name}\n\n{s.description or s.name}\n" for s, _ in written)
        ),
    )
    index_summary = await synth_llm.structured(
        "synthesize",
        "index",
        MarkdownDoc,
        topic_digest or "(no topics)",
        dry=lambda: MarkdownDoc(
            markdown=f"{len(k.files)} source files, {len(facts)} facts, {len(entities)} entities."
        ),
    )

    # 5. write everything fresh
    if kdir.exists():
        shutil.rmtree(kdir)
    (kdir / "topics").mkdir(parents=True)
    today = date.today().isoformat()

    def write(rel: str, text: str) -> None:
        atomic_write_text(kdir / rel, text)
        result.files.append(rel)

    for spec, md in written:
        tents = [ent_by_id[i] for i in spec.entity_ids]
        tfacts = [fact_by_id[i] for i in spec.fact_ids]
        srcs = sorted(
            {
                x.split("#", 1)[0]
                for x in [*(f["source"] for f in tfacts), *(s for e in tents for s in e["sources"])]
            }
        )
        conf = round(sum(f["confidence"] for f in tfacts) / len(tfacts), 2) if tfacts else 0.5
        fm = {
            "title": spec.name,
            "aliases": sorted({a for e in tents for a in e["aliases"]}),
            "sources": srcs,
            "confidence": conf,
            "updated": today,
        }
        body = [f"# {spec.name}", ""]
        if spec.description:
            body += [spec.description, ""]
        body.append(sanitize_md(md))
        if tents:
            body += ["", f"## {t['entities']}", ""] + [
                f"- {e['id']}: {md_escape(e['name'])} ({e['type']})"
                + (f" - {md_escape(e['description'])}" if e["description"] else "")
                for e in tents
            ]
        if tfacts:
            body += ["", f"## {t['facts']}", ""] + [
                f"- {f['id']}: {md_escape(f['statement'])} (source: {f['source']})" for f in tfacts
            ]
        for block in manual.get(spec.slug, []):
            body += ["", block]
        write(f"topics/{spec.slug}.md", frontmatter(fm) + "\n".join(body).rstrip() + "\n")
    result.topics = len(written)

    write("entities.json", json.dumps(entities, ensure_ascii=False, indent=2) + "\n")
    write("facts.jsonl", "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in facts))
    write("relations.json", json.dumps(relations, ensure_ascii=False, indent=2) + "\n")
    write("sources.md", sources_md(ws, k, t))
    write("open-questions.md", open_questions_md(k, facts, t))
    chunks: list[dict[str, Any]] = []
    for h, info in sorted(k.files.items(), key=lambda kv: kv[1]["path"]):
        tp = text_path(ws, h)
        if tp.exists():
            meta, body_text = read_extracted(tp)
            kc = ws.config.knowledge
            chunks += make_chunks(body_text, h, info["path"], meta.lang, kc.chunk_tokens, kc.overlap_ratio)
    write("chunks.jsonl", "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks))

    overview_md = f"# {t['overview']}\n\n" + sanitize_md(overview.markdown)
    gloss = glossary_md(terms, t)

    def index_md(extra: list[str]) -> str:
        lines = [
            "# INDEX",
            "",
            sanitize_md(index_summary.markdown),
            f"## {t['files']}",
            "",
            f"- [overview.md](overview.md) - {t['overview']}",
            f"- [glossary.md](glossary.md) - {t['glossary']}",
            f"- [sources.md](sources.md) - {t['sources']}",
            f"- [open-questions.md](open-questions.md) - {t['questions']}",
            "- [entities.json](entities.json), [facts.jsonl](facts.jsonl), [relations.json](relations.json), [chunks.jsonl](chunks.jsonl)",
            *extra,
            "",
            f"## {t['topics']}",
            "",
        ]
        lines += [
            f"- [{s.name}](topics/{s.slug}.md)" + (f" - {md_escape(s.description)}" if s.description else "")
            for s, _ in written
        ]
        return "\n".join(lines) + "\n"

    index = index_md([])
    budget = ws.config.knowledge.index_token_budget
    lang = k.lang

    def total(*parts: str) -> int:
        return sum(estimate_tokens(p, lang) for p in parts)

    if total(index, overview_md, gloss) > budget:
        # split: full glossary and overview detail move to topics/, INDEX links them
        result.split = True
        write(
            "topics/glossary.md",
            frontmatter(
                {
                    "title": t["glossary"],
                    "aliases": [],
                    "sources": sorted({m["first_source"].split("#", 1)[0] for m in terms}),
                    "confidence": 1.0,
                    "updated": today,
                }
            )
            + glossary_md(terms, t),
        )
        sections = re.split(r"(?m)^(?=## )", overview_md)
        keep = sections[: max(1, len(sections) // 3)]
        detail = "".join(sections[len(keep) :])
        if detail.strip():
            write(
                "topics/overview-detail.md",
                frontmatter(
                    {
                        "title": t["overview"],
                        "aliases": [],
                        "sources": [],
                        "confidence": 1.0,
                        "updated": today,
                    }
                )
                + f"# {t['overview']}\n\n"
                + detail,
            )
            overview_md = (
                "".join(keep).rstrip()
                + f"\n\n{t['more']}: [topics/overview-detail.md](topics/overview-detail.md)\n"
            )
        index = index_md(
            [f"- [topics/glossary.md](topics/glossary.md) - {t['glossary']} ({t['more']})"]
            + (["- [topics/overview-detail.md](topics/overview-detail.md)"] if detail.strip() else [])
        )
        rows = 0
        gloss = glossary_md(terms, t, limit=0, more_link="topics/glossary.md")
        while rows < len(terms):
            cand = glossary_md(terms, t, limit=rows + 1, more_link="topics/glossary.md")
            if total(index, overview_md, cand) > budget:
                break
            rows += 1
            gloss = cand
    write("glossary.md", gloss)
    write("overview.md", overview_md)
    write("INDEX.md", index)
    result.tokens = total(index, overview_md, gloss)

    problems = validate_knowledge(kdir, budget, lang)
    if problems:
        raise DnError("knowledge base validation failed:\n  " + "\n  ".join(problems))
    return result


# ------------------------------------------------------------------ validation


def _outside_code(text: str) -> list[str]:
    lines, in_code = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if not in_code:
            lines.append(line)
    return lines


def validate_knowledge(kdir: Path, budget: int, lang: str = "en") -> list[str]:
    problems: list[str] = []
    for p in sorted(kdir.rglob("*.md")):
        rel = p.relative_to(kdir).as_posix()
        text = p.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        if rel.startswith("topics/"):
            need = {"title", "aliases", "sources", "confidence", "updated"}
            if fm is None or not need <= set(fm):
                problems.append(f"{rel}: frontmatter must contain {sorted(need)}")
            elif not (
                isinstance(fm["aliases"], list)
                and isinstance(fm["sources"], list)
                and isinstance(fm["confidence"], int | float)
                and 0 <= fm["confidence"] <= 1
            ):
                problems.append(f"{rel}: frontmatter has invalid types")
        for line in _outside_code(body):
            if re.match(r"^#{4,}\s", line):
                problems.append(f"{rel}: heading deeper than ###: {line[:60]}")
            if HTML_RE.search(line):
                problems.append(f"{rel}: HTML outside code blocks: {line[:60]}")
            for m in LINK_RE.finditer(line):
                target = m.group(1)
                if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                    continue
                if not (p.parent / target.split("#", 1)[0]).exists():
                    problems.append(f"{rel}: broken link {target}")
    facts_file = kdir / "facts.jsonl"
    sources_file = kdir / "sources.md"
    if facts_file.exists() and sources_file.exists():
        listed = sources_file.read_text(encoding="utf-8")
        for line in facts_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                path = json.loads(line)["source"].split("#", 1)[0]
                if md_escape(path) not in listed:
                    problems.append(f"facts.jsonl: source {path} is not listed in sources.md")
    parts = [kdir / n for n in ("INDEX.md", "overview.md", "glossary.md")]
    tokens = sum(estimate_tokens(p.read_text(encoding="utf-8"), lang) for p in parts if p.exists())
    if tokens > budget:
        problems.append(f"INDEX.md + overview.md + glossary.md = {tokens} tokens > budget {budget}")
    return problems
