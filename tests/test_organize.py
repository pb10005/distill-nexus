"""FEAT-004: classify, propose-taxonomy, plan."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import yaml
from conftest import (
    FakeClient,
    batch_aware,
    dn_json,
    label,
    make_llm,
    make_pdf,
    open_ws,
    smart_handler,
    tool_of,
    user_text,
    with_taxonomy,
    write,
)

from dn.classify import classify, load_labels, taxonomy_key
from dn.extract import extract_all
from dn.pipeline import Options, Pipeline
from dn.plan import build_plan
from dn.scan import scan
from dn.schemas import Label


def _prepare(
    root: Path,
    files: dict[str, str | bytes],
    labels_by_marker: dict[str, dict[str, Any]],
    taxonomy: bool = True,
):  # type: ignore[no-untyped-def]
    """Scan + extract + classify with a fake LLM answering by a marker string in the file text."""
    if taxonomy:
        with_taxonomy(root)
    for rel, data in files.items():
        write(root, rel, data)
    ws = open_ws(root)
    entries = scan(ws).entries
    asyncio.run(extract_all(ws, entries, make_llm(root / ".dn", mode="dry")))

    def handler(req: dict[str, Any], n: int) -> Any:
        text = user_text(req)
        for marker, lab in labels_by_marker.items():
            if marker in text:
                return lab
        return label("misc", 0.2)

    client = FakeClient(batch_aware(handler))
    asyncio.run(classify(ws, entries, make_llm(root / ".dn", client)))
    return ws, entries, client


def test_classify_threshold(target: Path):
    """AC-040: confidence 0.9 and 0.6 keep the LLM category; 0.4 becomes misc (threshold 0.6)."""
    ws, entries, _ = _prepare(
        target,
        {"a.md": "MARK-A", "b.md": "MARK-B", "c.md": "MARK-C"},
        {"MARK-A": label("contracts", 0.9), "MARK-B": label("specs", 0.6), "MARK-C": label("specs", 0.4)},
    )
    by_path = {r["path"]: r for r in map(json.loads, ws.labels_path.read_text(encoding="utf-8").splitlines())}
    assert by_path["a.md"]["label"]["category"] == "contracts"
    assert by_path["b.md"]["label"]["category"] == "specs"
    assert by_path["c.md"]["label"]["category"] == "misc"
    assert by_path["c.md"]["llm_category"] == "specs"


def test_category_enum_from_taxonomy():
    """AC-041: the tool schema's category enum is exactly the taxonomy slugs plus misc."""
    schema = Label.tool_schema(categories=["contracts", "specs", "meetings"])
    assert schema["properties"]["category"]["enum"] == ["contracts", "specs", "meetings", "misc"]


def test_classify_once_per_hash(target: Path):
    """AC-042: three files with one hash cause one LLM call and share the label."""
    ws, entries, client = _prepare(
        target,
        {"a.md": "MARK-SAME", "x/b.md": "MARK-SAME", "y/c.md": "MARK-SAME"},
        {"MARK-SAME": label("specs", 0.8, title="Shared")},
    )
    assert len(client.requests) == 1
    labels = load_labels(ws, taxonomy_key(ws.taxonomy))
    assert len({e.hash for e in entries}) == 1
    for e in entries:
        assert labels[e.hash].label.title == "Shared"


def test_propose_taxonomy(target: Path):
    """AC-043: --propose-taxonomy writes 5-15 categories (slug/name/description/examples) and no plan.json."""
    for i in range(6):
        write(target, f"doc{i}.md", f"# Document {i}\n\ncontent {i}\n")
    ws = open_ws(target)
    proposal = {
        "categories": [
            {"slug": f"cat-{i}", "name": f"Cat {i}", "description": "d", "examples": ["x.md"]}
            for i in range(6)
        ]
    }
    client = FakeClient(smart_handler({"submit_propose_taxonomy": proposal}))
    report = asyncio.run(
        Pipeline(ws, Options(llm_client=client, progress=lambda m: None)).cmd_plan(propose=True)
    )
    assert report.exit_code == 0
    path = target / ".dn" / "taxonomy.proposed.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert 5 <= len(data["categories"]) <= 15
    for c in data["categories"]:
        assert set(c) >= {"slug", "name", "description", "examples"}
    assert not ws.plan_path.exists()
    assert not (target / "taxonomy.yaml").exists()
    assert "submit_propose_taxonomy" in [tool_of(r) for r in client.requests]


def test_plan_destination_layout(target: Path):
    """AC-044: contracts/nda/2024 layout, at most 3 levels below organized/."""
    ws, entries, _ = _prepare(
        target,
        {"inbox/nda.pdf": make_pdf("MARK-NDA")},
        {"MARK-NDA": label("contracts", 0.9, subcategory="nda", date="2024-03-12")},
    )
    plan = build_plan(ws, entries)
    (pe,) = plan.entries
    assert pe.to == "organized/contracts/nda/2024/nda.pdf"
    for p in plan.entries:
        assert len(Path(p.to).relative_to("organized").parts) - 1 <= 3  # type: ignore[arg-type]


def test_plan_dedupe_modes(target: Path):
    """AC-045: the first duplicate is canonical; later ones become _duplicates/ move, trash, or skip."""
    ws, entries, _ = _prepare(
        target, {"a/first.md": "MARK-D", "b/second.md": "MARK-D"}, {"MARK-D": label("specs", 0.9)}
    )
    ops = {}
    for mode in ("move", "trash", "keep"):
        plan = build_plan(ws, entries, dedupe=mode)
        by = {pe.from_: pe for pe in plan.entries}
        assert by["a/first.md"].op == "move" and by["a/first.md"].to == "organized/specs/first.md"
        ops[mode] = (by["b/second.md"].op, by["b/second.md"].to)
    assert ops["move"] == ("move", "organized/_duplicates/second.md")
    assert ops["trash"] == ("trash", None)
    assert ops["keep"] == ("skip", None)


def test_plan_casefold_collision(target: Path):
    """AC-046: Report.pdf and report.pdf in one folder -> the second becomes report-2.pdf."""
    ws, entries, _ = _prepare(
        target,
        {"a/Report.pdf": make_pdf("MARK-1"), "b/report.pdf": make_pdf("MARK-2")},
        {"MARK-1": label("specs", 0.9), "MARK-2": label("specs", 0.9)},
    )
    Pipeline(ws, Options(progress=lambda m: None)).plan(entries)  # writes plan.json
    saved = json.loads(ws.plan_path.read_text(encoding="utf-8"))
    tos = {pe["from"]: pe["to"] for pe in saved["entries"]}
    assert tos == {
        "a/Report.pdf": "organized/specs/Report.pdf",
        "b/report.pdf": "organized/specs/report-2.pdf",
    }


def test_plan_rename(target: Path):
    """AC-047: --rename gives vendor-a-nda_2024-03-12.pdf, and <slug>.<ext> when the date is null."""
    ws, entries, _ = _prepare(
        target,
        {"x.pdf": make_pdf("MARK-X"), "y.pdf": make_pdf("MARK-Y")},
        {
            "MARK-X": label("contracts", 0.9, title="Vendor A NDA", date="2024-03-12"),
            "MARK-Y": label("contracts", 0.9, title="Service Terms", date=None),
        },
    )
    plan = build_plan(ws, entries, rename=True)
    names = sorted(Path(pe.to).name for pe in plan.entries)  # type: ignore[arg-type]
    assert names == ["service-terms.pdf", "vendor-a-nda_2024-03-12.pdf"]


def test_plan_json_is_dry_run(target: Path):
    """AC-048: `dn plan --json` moves nothing and prints from/to/category/confidence/op."""
    with_taxonomy(target)
    write(target, "specs/api.md", "# specs specification\n")
    write(target, "notes.txt", "meeting minutes sync")
    before = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    code, out = dn_json("plan", str(target), "--dry-llm")
    assert code == 0
    after = sorted(
        p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file() and ".dn" not in p.parts
    )
    assert after == before
    entries = out["plan"]["entries"]
    assert len(entries) == 2
    for pe in entries:
        assert set(pe) >= {"from", "to", "category", "confidence", "op"}
    assert (target / ".dn/cache/plan.json").is_file()


def test_plan_noop_when_in_place(target: Path):
    """AC-085: a file already at its destination is op noop and not counted as a move."""
    ws, entries, _ = _prepare(
        target,
        {"organized/contracts/2024/x.pdf": make_pdf("MARK-X")},
        {"MARK-X": label("contracts", 0.9, date="2024-01-02")},
    )
    plan = build_plan(ws, entries)
    (pe,) = plan.entries
    assert pe.op == "noop" and pe.to == pe.from_ == "organized/contracts/2024/x.pdf"
    from dn.plan import plan_summary

    assert plan_summary(plan) == {"noop": 1}


def test_plan_rejects_bad_subcategory(target: Path):
    """AC-086: ../../secret, .., or unknown subcategories are dropped; nothing escapes organized/."""
    files = {"a.md": "MARK-1", "b.md": "MARK-2", "c.md": "MARK-3"}
    ws, entries, _ = _prepare(
        target,
        files,
        {
            "MARK-1": label("contracts", 0.9, subcategory="../../secret", date="2024-05-01"),
            "MARK-2": label("contracts", 0.9, subcategory=".."),
            "MARK-3": label("contracts", 0.9, subcategory="foo"),
        },
    )
    plan = build_plan(ws, entries)
    by = {pe.from_: pe.to for pe in plan.entries}
    assert by == {
        "a.md": "organized/contracts/2024/a.md",
        "b.md": "organized/contracts/b.md",
        "c.md": "organized/contracts/c.md",
    }
    for to in by.values():
        assert (target / to).resolve().is_relative_to((target / "organized").resolve())


def test_plan_rename_non_ascii(target: Path):
    """AC-087: Japanese titles keep a non-empty slug; symbol-only titles fall back to the file stem."""
    ws, entries, _ = _prepare(
        target,
        {"keiyaku.pdf": make_pdf("MARK-J"), "Weird Name.pdf": make_pdf("MARK-S")},
        {
            "MARK-J": label("contracts", 0.9, title="業務委託基本契約書", date="2024-03-12"),
            "MARK-S": label("contracts", 0.9, title="***", date="2024-03-12"),
        },
    )
    plan = build_plan(ws, entries, rename=True)
    names = sorted(Path(pe.to).name for pe in plan.entries)  # type: ignore[arg-type]
    assert names == ["weird-name_2024-03-12.pdf", "業務委託基本契約書_2024-03-12.pdf"]
    for n in names:
        assert not n.startswith("_") and not n.startswith(".")


def test_plan_interactive(target: Path):
    """AC-088: --interactive lets the user move a low-confidence misc file to contracts."""
    ws, entries, _ = _prepare(target, {"unsure.md": "MARK-U"}, {"MARK-U": label("contracts", 0.3)})
    asked: list[str] = []
    opts = Options(interactive=True, progress=lambda m: None, ask=lambda q: (asked.append(q), "contracts")[1])
    plan = Pipeline(ws, opts).plan(entries)
    assert asked and "unsure.md" in asked[0]
    assert plan.entries[0].category == "contracts"
    saved = json.loads(ws.plan_path.read_text(encoding="utf-8"))
    assert saved["entries"][0]["category"] == "contracts"
    assert saved["entries"][0]["to"] == "organized/contracts/unsure.md"
