"""FEAT-005: distill and synthesize."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    FakeClient,
    label,
    make_llm,
    open_ws,
    smart_handler,
    tool_of,
    user_text,
    with_taxonomy,
    write,
)

from dn.chunks import make_chunks
from dn.distill import distill
from dn.errors import DnError
from dn.extract import extract_all
from dn.pipeline import Options, Pipeline
from dn.scan import scan
from dn.synthesize import parse_frontmatter, validate_knowledge

DOC = "# API Gateway\n\nThe gateway routes requests.\n\n## Rate limit\n\nThe limit is 100 rps.\n"


def distilled(**parts: list[dict[str, Any]]) -> dict[str, Any]:
    base: dict[str, Any] = {"terms": [], "entities": [], "facts": [], "relations": [], "questions": []}
    base.update(parts)
    return base


def fact(statement: str, subject: str, predicate: str, obj: str, loc: str = "L1") -> dict[str, Any]:
    return {
        "statement": statement,
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "confidence": 0.9,
        "locator": loc,
    }


def build(
    root: Path,
    files: dict[str, str],
    overrides: dict[str, Any] | None = None,
    config: str | None = None,
    distill_all: bool = False,
):  # type: ignore[no-untyped-def]
    with_taxonomy(root)
    if config:
        write(root, ".dn/config.yaml", config)
    for rel, text in files.items():
        write(root, rel, text)
    ws = open_ws(root)
    client = FakeClient(smart_handler(overrides))
    report = asyncio.run(
        Pipeline(
            ws, Options(llm_client=client, progress=lambda m: None, distill_all=distill_all)
        ).cmd_distill()
    )
    return ws, client, report


def test_distill_chunks_by_heading(target: Path):
    """AC-055: 20,000 chars are split at headings into chunks of at most 8,000 chars; facts/<hash>.json saved."""
    sections = [f"## Section {i}\n\n" + ("word " * 700) for i in range(6)]  # ~3,500 chars each
    ws, client, _ = build(target, {"big.md": "# Big\n\n" + "\n\n".join(sections)})
    reqs = [r for r in client.requests if tool_of(r) == "submit_distill"]
    assert len(reqs) >= 3
    for r in reqs:
        chunk = user_text(r).split("\n\n", 1)[1]
        assert len(chunk) <= 8000
        assert chunk.lstrip().startswith("#")  # every chunk starts at a heading
    (facts_file,) = list(ws.facts_dir.glob("*.json"))
    assert facts_file.stem == scan(ws).entries[0].hash


def test_facts_have_sources(target: Path):
    """AC-056: every term/entity/fact/relation in facts/<hash>.json has source.path/hash/locator."""
    rel = {"from": "API Gateway", "relation": "routes to", "to": "Order service", "locator": "L3"}
    ws, _, _ = build(
        target,
        {"api.md": DOC},
        {
            "submit_distill": distilled(
                terms=[{"term": "API Gateway", "definition": "entry point", "aliases": [], "locator": "L1"}],
                entities=[{"name": "API Gateway", "type": "system", "description": "d", "locator": ""}],
                facts=[fact("The limit is 100 rps.", "limit", "is", "100 rps", "L7")],
                relations=[rel],
            )
        },
    )
    data = json.loads(next(ws.facts_dir.glob("*.json")).read_text(encoding="utf-8"))
    for bucket in ("terms", "entities", "facts", "relations"):
        assert data[bucket], bucket
        for item in data[bucket]:
            src = item["source"]
            assert src["path"] == "api.md" and src["hash"] == data["hash"] and src["locator"]


def test_distill_skips_misc_without_knowledge(target: Path):
    """AC-057: misc + has_domain_knowledge=false is not distilled unless --distill-all."""
    with_taxonomy(target)
    write(target, "app.log", "2024-05-02 INFO started\n")
    ws = open_ws(target)
    entries = scan(ws).entries
    asyncio.run(extract_all(ws, entries, make_llm(target / ".dn", mode="dry")))
    from dn.classify import classify

    asyncio.run(
        classify(
            ws,
            entries,
            make_llm(target / ".dn", FakeClient(lambda r, n: label("misc", 0.9, has_domain_knowledge=False))),
        )
    )
    client = FakeClient(smart_handler())
    stats = asyncio.run(distill(ws, entries, make_llm(target / ".dn", client)))
    assert client.requests == [] and stats.skipped == 1
    stats2 = asyncio.run(distill(ws, entries, make_llm(target / ".dn", client), distill_all=True))
    assert len(client.requests) == 1 and stats2.distilled == 1
    assert len(list(ws.facts_dir.glob("*.json"))) == 1


def test_synthesize_outputs(target: Path):
    """AC-058: synthesize writes the complete _knowledge/ file set."""
    ws, _, report = build(target, {"api.md": DOC})
    k = ws.knowledge_dir
    for name in [
        "INDEX.md",
        "overview.md",
        "glossary.md",
        "entities.json",
        "facts.jsonl",
        "relations.json",
        "chunks.jsonl",
        "sources.md",
        "open-questions.md",
    ]:
        assert (k / name).is_file(), name
    assert list((k / "topics").glob("*.md"))
    assert report.exit_code == 0


def test_normalization_merges_variants(target: Path):
    """AC-059: API Gateway / API gateway judged same -> one canonical entry with the other as alias."""

    def dist(req: dict[str, Any], n: int) -> dict[str, Any]:
        name = "API Gateway" if "a.md" in user_text(req) else "API gateway"
        return distilled(
            terms=[{"term": name, "definition": "routes requests", "aliases": [], "locator": "L1"}],
            entities=[{"name": name, "type": "system", "description": "gw", "locator": "L1"}],
        )

    def same(req: dict[str, Any], n: int) -> dict[str, Any]:
        return {"pairs": [{"a": "API Gateway", "b": "API gateway", "same": True, "canonical": "API Gateway"}]}

    ws, client, _ = build(
        target,
        {"a.md": "# A\n\nfirst\n", "b.md": "# B\n\nsecond\n"},
        {"submit_distill": dist, "submit_normalize": same},
    )
    assert any(tool_of(r) == "submit_normalize" and "API gateway" in user_text(r) for r in client.requests)
    ents = json.loads((ws.knowledge_dir / "entities.json").read_text(encoding="utf-8"))
    assert [e["name"] for e in ents] == ["API Gateway"] and ents[0]["aliases"] == ["API gateway"]
    gloss = (ws.knowledge_dir / "glossary.md").read_text(encoding="utf-8")
    rows = [ln for ln in gloss.splitlines() if ln.startswith("| API")]
    assert len(rows) == 1 and "| API gateway |" in rows[0]


def test_unassigned_entities_go_to_general(target: Path):
    """AC-060: entities the topic split leaves out appear in topics/general.md."""

    def dist(req: dict[str, Any], n: int) -> dict[str, Any]:
        return distilled(
            entities=[
                {"name": "Order service", "type": "system", "description": "orders", "locator": "L1"},
                {"name": "Billing service", "type": "system", "description": "bills", "locator": "L2"},
            ]
        )

    def topics(req: dict[str, Any], n: int) -> dict[str, Any]:
        eid = next(line.split(":")[0] for line in user_text(req).splitlines() if "Order service" in line)
        return {
            "topics": [
                {"slug": "orders", "name": "Orders", "description": "", "entity_ids": [eid], "fact_ids": []}
            ]
        }

    ws, _, _ = build(target, {"a.md": DOC}, {"submit_distill": dist, "submit_topics": topics})
    general = (ws.knowledge_dir / "topics" / "general.md").read_text(encoding="utf-8")
    assert "Billing service" in general
    assert "Order service" in (ws.knowledge_dir / "topics" / "orders.md").read_text(encoding="utf-8")


def test_invalid_src_ids_rejected(target: Path):
    """AC-061: an unknown [src: F-999] is regenerated once, then the topic is not written, errors.jsonl, exit 3."""
    ws, client, report = build(
        target, {"a.md": DOC}, {"submit_topic_doc": {"markdown": "Claim. [src: F-999]"}}
    )
    doc_calls = [r for r in client.requests if tool_of(r) == "submit_topic_doc"]
    assert len(doc_calls) == 2
    assert "F-999" in user_text(doc_calls[1])
    assert not (ws.knowledge_dir / "topics" / "general.md").exists()
    err = json.loads(ws.errors_path.read_text(encoding="utf-8").splitlines()[-1])
    assert err["path"].endswith("topics/general.md") and err["phase"] == "synthesize"
    assert report.exit_code == 3


def test_topic_frontmatter(target: Path):
    """AC-062: topic frontmatter has title, aliases, sources, confidence, updated."""
    ws, _, _ = build(target, {"a.md": DOC})
    for p in (ws.knowledge_dir / "topics").glob("*.md"):
        fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
        assert fm is not None and {"title", "aliases", "sources", "confidence", "updated"} <= set(fm)


def test_chunks_size_and_overlap(target: Path):
    """AC-063: chunk fields; tokens <= 800; neighbours overlap 120 +/- 20 tokens."""
    text = "# Intro\n\n" + "\n\n".join(
        f"## Part {i}\n\n" + "lorem ipsum dolor sit amet " * 40 for i in range(7)
    )
    assert 2800 <= len(text) / 2.5 <= 3200  # about 3,000 tokens
    ws, _, _ = build(target, {"long.md": text})
    rows = [
        json.loads(ln) for ln in (ws.knowledge_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) >= 4
    for r in rows:
        assert {"id", "source_hash", "source_path", "heading_path", "text", "tokens"} <= set(r)
        assert r["tokens"] <= 800 and r["source_path"] == "long.md"
    for a, b in zip(rows, rows[1:], strict=False):
        overlap_tokens = (a["end"] - b["start"]) / 2.5
        assert abs(overlap_tokens - 120) <= 20
        assert a["text"][-(a["end"] - b["start"]) :] == b["text"][: a["end"] - b["start"]]
    ja = make_chunks("# 見出し\n\n" + "日本語の本文です。" * 800, "h", "ja.md", "ja")
    for a, b in zip(ja, ja[1:], strict=False):
        assert abs((a["end"] - b["start"]) / 1.5 - 120) <= 20


def test_conflicts_listed(target: Path):
    """AC-064: facts with the same subject+predicate but different objects are listed with sources."""

    def dist(req: dict[str, Any], n: int) -> dict[str, Any]:
        if "spec.md" in user_text(req):
            return distilled(facts=[fact("The rate limit is 100 rps.", "rate limit", "is", "100 rps", "L5")])
        return distilled(facts=[fact("The rate limit is 200 rps.", "Rate limit", "is", "200 rps", "L9")])

    ws, _, _ = build(
        target, {"spec.md": DOC, "meeting.md": "# Meeting\n\nNew limit.\n"}, {"submit_distill": dist}
    )
    oq = (ws.knowledge_dir / "open-questions.md").read_text(encoding="utf-8")
    assert "The rate limit is 100 rps." in oq and "The rate limit is 200 rps." in oq
    assert "spec.md#L5" in oq and "meeting.md#L9" in oq


def test_budget_split(target: Path):
    """AC-065: over budget -> glossary/overview detail move to topics/, INDEX links them, exit 0 when it fits."""
    terms = [
        {
            "term": f"Term {i:03d}",
            "definition": "a definition that takes some room " * 2,
            "aliases": [],
            "locator": "L1",
        }
        for i in range(150)
    ]
    overview = "## Background\n\nShort.\n\n" + "\n\n".join(
        f"## Detail {i}\n\n" + "details " * 60 for i in range(6)
    )
    ws, _, report = build(
        target,
        {"a.md": DOC},
        {"submit_distill": distilled(terms=terms), "submit_overview": {"markdown": overview}},
        config="knowledge:\n  index_token_budget: 1500\n",
    )
    k = ws.knowledge_dir
    assert report.exit_code == 0 and report.data["synthesize"]["split"] is True
    assert (k / "topics" / "glossary.md").is_file() and (k / "topics" / "overview-detail.md").is_file()
    index = (k / "INDEX.md").read_text(encoding="utf-8")
    assert "(topics/glossary.md)" in index and "(topics/overview-detail.md)" in index
    assert report.data["synthesize"]["index_tokens"] <= 1500
    assert "Term 149" in (k / "topics" / "glossary.md").read_text(encoding="utf-8")


def test_validation_failures_exit_1(target: Path, tmp_path: Path):
    """AC-100: a broken INDEX link or a budget still exceeded after splitting -> failure listed, exit 1."""
    kdir = tmp_path / "k"
    write(kdir, "INDEX.md", "# INDEX\n\n- [missing](nope.md)\n")
    problems = validate_knowledge(kdir, 20000)
    assert any("broken link nope.md" in p for p in problems)
    huge = "Summary. " + "words " * 3000  # INDEX itself cannot be split
    with pytest.raises(DnError) as ei:
        build(
            target,
            {"a.md": DOC},
            {"submit_index": {"markdown": huge}},
            config="knowledge:\n  index_token_budget: 1000\n",
        )
    assert ei.value.exit_code == 1 and "budget" in str(ei.value)


def test_knowledge_rules(target: Path):
    """AC-066: fact sources are in sources.md; headings <= ###; no HTML outside code (manual comments allowed)."""
    md = "## Part\n\n#### Too deep\n\n<div>raw html</div> text [src: F-001]\n\n```html\n<b>code is fine</b>\n```\n"
    ws, _, report = build(target, {"a.md": DOC}, {"submit_topic_doc": {"markdown": md}})
    k = ws.knowledge_dir
    sources = (k / "sources.md").read_text(encoding="utf-8")
    for line in (k / "facts.jsonl").read_text(encoding="utf-8").splitlines():
        assert json.loads(line)["source"].split("#", 1)[0] in sources
    topic = (k / "topics" / "general.md").read_text(encoding="utf-8")
    assert "#### " not in topic and "### Too deep" in topic
    assert "<div>" not in topic and "raw html" in topic and "<b>code is fine</b>" in topic
    assert validate_knowledge(k, 20000) == []
    (k / "topics" / "general.md").write_text(
        topic + "\n<!-- manual -->\nnote\n<!-- /manual -->\n", encoding="utf-8"
    )
    assert validate_knowledge(k, 20000) == []
    (k / "overview.md").write_text("# O\n\n<span>bad</span>\n", encoding="utf-8")
    assert any("HTML outside code" in p for p in validate_knowledge(k, 20000))


def test_manual_sections_preserved(target: Path):
    """AC-067: a <!-- manual --> block in topics/<slug>.md survives regeneration verbatim."""
    ws, _, _ = build(target, {"a.md": DOC})
    topic = ws.knowledge_dir / "topics" / "general.md"
    block = "<!-- manual -->\n## 補足\n\n人が書いたメモ: 例外は月末処理。\n<!-- /manual -->"
    topic.write_text(topic.read_text(encoding="utf-8") + "\n" + block + "\n", encoding="utf-8")
    ws2, _, _ = build(target, {})
    assert block in (ws2.knowledge_dir / "topics" / "general.md").read_text(encoding="utf-8")


def test_glossary_order(target: Path):
    """AC-069: glossary is a GFM table sorted alphabetically first, then in kana order."""
    names = ["Zeta", "イベントバス", "alpha", "あいさつ", "API", "さくら"]
    terms = [{"term": n, "definition": f"def of {n}", "aliases": [], "locator": "L1"} for n in names]
    ws, _, _ = build(target, {"a.md": DOC}, {"submit_distill": distilled(terms=terms)})
    gloss = (ws.knowledge_dir / "glossary.md").read_text(encoding="utf-8").splitlines()
    header = next(i for i, ln in enumerate(gloss) if ln.startswith("| "))
    assert gloss[header + 1] == "|---|---|---|---|"
    rows = [ln.split("|")[1].strip() for ln in gloss[header + 2 :] if ln.startswith("| ")]
    assert rows == ["alpha", "API", "Zeta", "あいさつ", "イベントバス", "さくら"]
    assert len(gloss[header].split("|")) == 6  # 4 columns: term / definition / aliases / first source
