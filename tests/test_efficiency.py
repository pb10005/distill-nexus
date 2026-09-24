"""FEAT-009: rule pre-sorting, batch classification, vision off by default."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    SAMPLE_TREE,
    FakeClient,
    batch_aware,
    batch_sections,
    dn_json,
    label,
    make_llm,
    open_ws,
    run_dn,
    smart_handler,
    tool_of,
    user_text,
    with_taxonomy,
    write,
)

from dn.classify import classify, load_labels, taxonomy_key
from dn.extract import extract_all, read_extracted, text_path
from dn.pipeline import Options, Pipeline
from dn.scan import load_inventory, scan


def _prepared(root: Path, files: dict[str, str], config: str | None = None):  # type: ignore[no-untyped-def]
    with_taxonomy(root)
    if config:
        write(root, ".dn/config.yaml", config)
    for rel, text in files.items():
        write(root, rel, text)
    ws = open_ws(root)
    entries = scan(ws).entries
    asyncio.run(extract_all(ws, entries, make_llm(root / ".dn", mode="dry")))
    return ws, entries


def _classify(ws, entries, handler) -> FakeClient:  # type: ignore[no-untyped-def]
    client = FakeClient(handler)
    asyncio.run(classify(ws, entries, make_llm(ws.root / ".dn", client, concurrency=ws.config.concurrency)))
    return client


def _labels(ws) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    by_hash = load_labels(ws, taxonomy_key(ws.taxonomy))
    return {e.path: by_hash[e.hash].label for e in load_inventory(ws) if e.hash in by_hash}


def _classify_calls(client: FakeClient) -> list[dict[str, Any]]:
    return [r for r in client.requests if tool_of(r) in ("submit_classify", "submit_classify_batch")]


def _doc(i: int, chars: int) -> str:
    head = f"# specs specification {i}\n\n"
    return head + (f"line {i} " * chars)[: chars - len(head)]


def test_batches_reduce_calls(target: Path):
    """AC-106: 25 files with batch size 10 -> 3 classification calls; 25 labels recorded."""
    ws, entries = _prepared(target, {f"d{i:02d}.md": _doc(i, 1000) for i in range(25)})
    client = _classify(ws, entries, batch_aware(smart_handler()))
    calls = _classify_calls(client)
    assert len(calls) == 3
    assert [len(batch_sections(r)) for r in calls] == [10, 10, 5]
    labels = _labels(ws)
    assert len(labels) == 25
    assert all(lb.category == "contracts" for lb in labels.values())


def test_batch_size_limits(target: Path):
    """AC-107: per call at most 24,000 content chars in total, at most 6,000 per file."""
    ws, entries = _prepared(target, {f"d{i:02d}.md": _doc(i, 5000) for i in range(10)})
    client = _classify(ws, entries, batch_aware(smart_handler()))
    calls = _classify_calls(client)
    assert len(calls) == 3  # 4 + 4 + 2 files
    for r in calls:
        sections = batch_sections(r) or [("F1", user_text(r))]
        bodies = [s.split(" chars) ---\n", 1)[1] for _, s in sections]
        assert sum(len(b) for b in bodies) <= 24000
        assert all(len(b) <= 6000 for b in bodies)
    assert len(_labels(ws)) == 10


def test_invalid_batch_falls_back_to_single(target: Path):
    """AC-108: a batch that twice omits a file_id is redone one file at a time; all labels recorded."""
    ws, entries = _prepared(target, {f"d{i}.md": _doc(i, 800) for i in range(4)})
    single = smart_handler()

    def handler(req: dict[str, Any], n: int) -> Any:
        if tool_of(req) == "submit_classify_batch":
            ids = [fid for fid, _ in batch_sections(req)]
            return {"labels": [{"file_id": fid, **label("specs", 0.9)} for fid in ids[:-1]]}  # drops one
        return single(req, n)

    client = _classify(ws, entries, handler)
    tools = [tool_of(r) for r in _classify_calls(client)]
    assert tools.count("submit_classify_batch") == 2
    assert tools.count("submit_classify") == 4
    assert "missing ['F4']" in user_text(
        [r for r in client.requests if tool_of(r) == "submit_classify_batch"][1]
    )
    assert len(_labels(ws)) == 4


def test_rules_skip_llm(target: Path):
    """AC-109: a config rule and the default *.log rule label files without any LLM call."""
    config = 'rules:\n  - glob: "docs/legal/**"\n    category: contracts\n'  # *.log comes from the defaults
    ws, entries = _prepared(target, {"docs/legal/nda.txt": "legal text", "logs/app.log": "INFO ok\n"}, config)
    client = _classify(ws, entries, batch_aware(smart_handler()))
    assert _classify_calls(client) == []
    labels = _labels(ws)
    assert labels["docs/legal/nda.txt"].category == "contracts"
    assert labels["logs/app.log"].category == "misc"
    assert labels["logs/app.log"].has_domain_knowledge is False
    # the built-in default rule set also contains *.log -> misc
    from dn.config import Config

    assert [(r.glob, r.category) for r in Config().effective_rules] == [("*.log", "misc")]


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        ('  - glob: "a/**"\n    category: nope\n', ("rules[0]", "'nope' is not in taxonomy.yaml")),
        ('  - glob: ""\n    category: misc\n', ("rules.0.glob", "at least 1 character")),
    ],
)
def test_invalid_rules_exit_2(target: Path, rules: str, expected: tuple[str, str]):
    """AC-110: a rule with an unknown category or an empty glob -> error naming the rule, exit 2; misc is valid."""
    with_taxonomy(target)
    write(target, "a.md", "# specs\n")
    write(target, ".dn/config.yaml", "rules:\n" + rules)
    p = run_dn("plan", str(target), "--dry-llm")
    assert p.returncode == 2
    where, reason = expected
    assert where in p.stderr  # which rule
    assert reason in p.stderr  # why it is invalid
    write(target, ".dn/config.yaml", 'rules:\n  - glob: "a/**"\n    category: misc\n')
    assert run_dn("plan", str(target), "--dry-llm").returncode == 0


def test_vision_off_by_default(target: Path):
    """AC-111: without --images no image is sent; PNG is quality none, the scanned PDF is quality text."""
    with_taxonomy(target)
    write(target, "diagram.png", (SAMPLE_TREE / "specs/er-diagram.png").read_bytes())
    write(target, "scan.pdf", (SAMPLE_TREE / "inbox/scanned-contract.pdf").read_bytes())
    ws = open_ws(target)
    client = FakeClient(smart_handler())
    report = asyncio.run(Pipeline(ws, Options(llm_client=client, progress=lambda m: None)).cmd_plan())
    assert report.exit_code == 0
    assert [r for r in client.requests if tool_of(r) == "submit_vision"] == []
    for r in client.requests:
        content = r["messages"][0]["content"]
        assert not (isinstance(content, list) and any(c.get("type") == "image" for c in content))
    inv = {e.path: e for e in load_inventory(ws)}
    png_meta, png_text = read_extracted(text_path(ws, inv["diagram.png"].hash))
    pdf_meta, pdf_text = read_extracted(text_path(ws, inv["scan.pdf"].hash))
    assert png_meta.quality == "none" and png_text.strip() == ""
    assert pdf_meta.quality == "text" and "agreement term is 12 months" in pdf_text


def test_status_estimate_without_vision(target: Path):
    """AC-112: the default estimate excludes vision calls (smaller than with --images)."""
    with_taxonomy(target)
    write(target, "diagram.png", (SAMPLE_TREE / "specs/er-diagram.png").read_bytes())
    write(target, "a.md", "# specs\n\ntext\n")
    assert run_dn("scan", str(target)).returncode == 0
    code, default = dn_json("status", str(target))
    code2, with_images = dn_json("status", str(target), "--images")
    assert code == code2 == 0
    assert default["estimate"]["cost_usd"] < with_images["estimate"]["cost_usd"]
    assert default["estimate"]["input_tokens"] < with_images["estimate"]["input_tokens"]


def test_rule_applies_to_whole_hash(target: Path):
    """AC-113: identical content where one path matches a rule -> the hash is labelled by the rule, no LLM."""
    config = 'rules:\n  - glob: "docs/legal/**"\n    category: contracts\n'
    ws, entries = _prepared(
        target, {"docs/legal/nda.txt": "same nda text", "archive/nda.txt": "same nda text"}, config
    )
    client = _classify(ws, entries, batch_aware(smart_handler({"submit_classify": label("specs", 0.9)})))
    assert _classify_calls(client) == []
    labels = _labels(ws)
    assert labels["docs/legal/nda.txt"].category == labels["archive/nda.txt"].category == "contracts"


def test_new_rule_relabels_without_llm(target: Path):
    """AC-114: a rule added after a run relabels matching files with zero classification calls."""
    with_taxonomy(target)
    write(target, "docs/legal/nda.txt", "meeting minutes sync")  # the LLM first calls this meetings
    write(target, "other.md", "# specs specification\n")
    handler = batch_aware(smart_handler({"submit_classify": label("meetings", 0.9)}))
    ws = open_ws(target)
    asyncio.run(Pipeline(ws, Options(llm_client=FakeClient(handler), progress=lambda m: None)).cmd_run())
    assert _labels(ws)["docs/legal/nda.txt"].category == "meetings"
    write(target, ".dn/config.yaml", 'rules:\n  - glob: "docs/legal/**"\n    category: contracts\n')
    ws2 = open_ws(target)
    client = FakeClient(handler)
    asyncio.run(Pipeline(ws2, Options(llm_client=client, progress=lambda m: None)).cmd_run())
    assert _classify_calls(client) == []
    assert _labels(ws2)["docs/legal/nda.txt"].category == "contracts"
    assert _labels(ws2)["other.md"].category == "meetings"


def test_batch_resume_after_interrupt(target: Path):
    """AC-115: interrupted on the 3rd batch call, a rerun re-sends none of the 20 finished files."""
    files = {f"d{i:02d}.md": _doc(i, 600) for i in range(30)}
    ws, entries = _prepared(target, files, "concurrency: 1\n")
    single = batch_aware(smart_handler())
    calls = {"n": 0}

    def interrupting(req: dict[str, Any], n: int) -> Any:
        if tool_of(req) == "submit_classify_batch":
            calls["n"] += 1
            if calls["n"] == 3:
                return KeyboardInterrupt()
        return single(req, n)

    with pytest.raises(KeyboardInterrupt):
        _classify(ws, entries, interrupting)
    done_first = set(_labels(ws))
    assert len(done_first) == 20
    ws2 = open_ws(target)
    client = _classify(ws2, entries, single)
    resent = [s for r in _classify_calls(client) for _, s in (batch_sections(r) or [("F1", user_text(r))])]
    assert len(resent) == 10
    for path in done_first:
        assert all(f"Path: {path}\n" not in s for s in resent)
    assert len(_labels(ws2)) == 30


def test_single_failure_after_fallback(target: Path):
    """AC-116: after the batch fallback, one file failing twice goes to errors.jsonl; the rest are labelled."""
    ws, entries = _prepared(target, {f"d{i}.md": _doc(i, 800) for i in range(3)})

    def handler(req: dict[str, Any], n: int) -> Any:
        if tool_of(req) == "submit_classify_batch":
            return {"labels": []}
        if "Path: d1.md\n" in user_text(req):
            return label("specs", 2)  # confidence out of range, every time
        return label("specs", 0.9)

    client = _classify(ws, entries, handler)
    errors = [json.loads(ln) for ln in ws.errors_path.read_text(encoding="utf-8").splitlines()]
    assert [(e["path"], e["phase"]) for e in errors] == [("d1.md", "classify")]
    labels = _labels(ws)
    assert set(labels) == {"d0.md", "d2.md"}
    assert sum(1 for r in _classify_calls(client) if "Path: d1.md\n" in user_text(r)) >= 2


def test_threshold_inside_batch(target: Path):
    """AC-117: one batch answer with confidence 0.9 / 0.6 / 0.4 -> category, category, misc."""
    ws, entries = _prepared(target, {"a.md": "MARK-A", "b.md": "MARK-B", "c.md": "MARK-C"})
    per = {"MARK-A": label("contracts", 0.9), "MARK-B": label("specs", 0.6), "MARK-C": label("specs", 0.4)}

    def handler(req: dict[str, Any], n: int) -> Any:
        assert tool_of(req) == "submit_classify_batch"
        return {
            "labels": [
                {"file_id": fid, **next(v for k, v in per.items() if k in s)}
                for fid, s in batch_sections(req)
            ]
        }

    client = _classify(ws, entries, handler)
    assert len(_classify_calls(client)) == 1
    labels = _labels(ws)
    assert (labels["a.md"].category, labels["b.md"].category, labels["c.md"].category) == (
        "contracts",
        "specs",
        "misc",
    )
