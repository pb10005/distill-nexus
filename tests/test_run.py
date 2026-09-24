"""FEAT-006: run / status / export and CLI contracts."""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    FakeClient,
    copy_sample,
    dn_json,
    open_ws,
    run_dn,
    smart_handler,
    tool_of,
    tree_state,
    with_taxonomy,
    write,
)

from dn.apply import load_manifest
from dn.pipeline import Options, Pipeline


def test_run_without_apply(tmp_path: Path):
    """AC-070: `dn run --dry-llm` writes plan.json and _knowledge/ and moves no original file."""
    root = copy_sample(tmp_path / "tree")
    before = tree_state(root)
    code, out = dn_json("run", str(root), "--dry-llm")
    assert code == 0, out
    assert (root / ".dn/cache/plan.json").is_file()
    assert (root / "organized/_knowledge/INDEX.md").is_file()
    assert tree_state(root) == before
    assert out["plan"]["summary"].get("move", 0) > 0


def test_run_apply_then_undo(tmp_path: Path):
    """AC-071: run --apply --yes followed by undo leaves every file's path/hash/mtime as before."""
    root = copy_sample(tmp_path / "tree")
    before = tree_state(root)
    code, out = dn_json("run", str(root), "--dry-llm", "--apply", "--yes")
    assert code == 0, out
    assert out["apply"]["moved"] > 30
    assert tree_state(root) != before
    code2, _ = dn_json("undo", str(root))
    assert code2 == 0
    assert tree_state(root) == before


def test_second_run_no_changes(tmp_path: Path):
    """AC-072: a second `dn run` makes zero LLM calls and prints `no changes`."""
    root = copy_sample(tmp_path / "tree")
    assert dn_json("run", str(root), "--dry-llm")[0] == 0
    p = run_dn("run", str(root), "--dry-llm", "--json")
    out = json.loads(p.stdout)
    assert p.returncode == 0
    assert out["llm"]["generated"] == 0 and out["llm"]["api_calls"] == 0
    assert out["changes"] is False and out["message"] == "no changes"
    assert "no changes" in p.stderr


def test_resume_after_interrupt(target: Path):
    """AC-073: after a KeyboardInterrupt on the 3rd classify call, rerun skips the 2 finished files."""
    with_taxonomy(target)
    for i in range(5):
        write(target, f"doc{i}.md", f"# Doc {i}\n\nspecs specification {i}\n")
    handler = smart_handler()
    classify_calls: list[str] = []

    def interrupting(req: dict[str, Any], n: int) -> Any:
        if tool_of(req) == "submit_classify":
            classify_calls.append(req["messages"][0]["content"])
            if len(classify_calls) == 3:
                return KeyboardInterrupt()
        return handler(req, n)

    ws = open_ws(target)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            Pipeline(
                ws, Options(llm_client=FakeClient(interrupting), concurrency=1, progress=lambda m: None)
            ).cmd_run()
        )
    with ws.labels_path.open("a", encoding="utf-8") as f:
        f.write('{"hash": "half-written')  # a torn last line from the crash
    done = [json.loads(ln)["path"] for ln in ws.labels_path.read_text(encoding="utf-8").splitlines()[:-1]]
    assert len(done) == 2
    client = FakeClient(handler)
    ws2 = open_ws(target)
    report = asyncio.run(
        Pipeline(ws2, Options(llm_client=client, concurrency=1, progress=lambda m: None)).cmd_run()
    )
    assert report.exit_code == 0
    second = [r for r in client.requests if tool_of(r) == "submit_classify"]
    assert len(second) == 3
    asked = "\n".join(r["messages"][0]["content"] for r in second)
    for path in done:
        assert f"Path: {path}\n" not in asked
    from dn.classify import load_labels, taxonomy_key

    assert len(load_labels(ws2, taxonomy_key(ws2.taxonomy))) == 5


def test_manifest_valid_after_failed_apply(target: Path, monkeypatch: pytest.MonkeyPatch):
    """AC-074: after an apply that stops mid-way the manifest parses and undo restores only completed moves."""
    import dn.apply as apply_mod

    with_taxonomy(target)
    for i in range(4):
        write(target, f"f{i}.md", f"# specs specification {i}\n")
    ws = open_ws(target)
    asyncio.run(Pipeline(ws, Options(dry_llm=True, progress=lambda m: None)).cmd_plan())
    real = apply_mod._move_file
    count = {"n": 0}

    def dies(src: Path, dst: Path) -> None:
        count["n"] += 1
        if count["n"] == 3:
            raise KeyboardInterrupt  # process killed mid-apply
        real(src, dst)

    monkeypatch.setattr(apply_mod, "_move_file", dies)
    with pytest.raises(KeyboardInterrupt):
        Pipeline(ws, Options(yes=True, progress=lambda m: None)).cmd_apply()
    manifest = json.loads(ws.manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["runs"][0]["moves"]) == 2
    monkeypatch.setattr(apply_mod, "_move_file", real)
    code, out = dn_json("undo", str(target))
    assert code == 0 and out["undo"]["restored"] == 2
    for i in range(4):
        assert (target / f"f{i}.md").is_file()


def test_partial_failure_exit_3(target: Path):
    """AC-075: one unreadable file does not stop the run; exit code 3."""
    with_taxonomy(target)
    write(target, "broken.docx", b"PK\x03\x04 corrupt")
    write(target, "good.md", "# specs specification\n\nok\n")
    code, out = dn_json("run", str(target), "--dry-llm")
    assert code == 3
    assert out["extract"]["failed"] == 1
    assert (target / "organized/_knowledge/INDEX.md").is_file()


def test_status_json(target: Path):
    """AC-076: `dn status --json` reports cached counts, unprocessed counts and estimated cost."""
    with_taxonomy(target)
    write(target, "a.md", "# a\n\ntext\n")
    write(target, "b.md", "# b\n\ntext\n")
    assert run_dn("scan", str(target)).returncode == 0
    code, out = dn_json("status", str(target))
    assert code == 0
    assert out["cached"]["extracted"] == 0 and out["unprocessed"]["classify"] == 2
    assert out["estimate"]["cost_usd"] > 0
    assert set(out["cached"]) >= {"extracted", "labeled", "facts", "llm_responses"}


@pytest.mark.parametrize(
    "args", [["scan"], ["plan", "--dry-llm"], ["run", "--dry-llm"], ["status"], ["undo"], ["init"]]
)
def test_json_stdout_only(target: Path, args: list[str]):
    """AC-077: with --json stdout is exactly one JSON document; progress goes to stderr."""
    with_taxonomy(target)
    write(target, "a.md", "# specs\n")
    p = run_dn(args[0], str(target), *args[1:], "--json")
    json.loads(p.stdout)  # the whole stdout parses as one document
    assert p.stdout.strip().count("\n") == 0
    if args[0] in ("plan", "run"):
        assert "scan..." in p.stderr and "scan..." not in p.stdout


def test_apply_prompt_declined(target: Path):
    """AC-078: answering n at the confirmation prompt moves nothing and exits 1."""
    with_taxonomy(target)
    write(target, "a.md", "# specs specification\n")
    ws = open_ws(target)
    asyncio.run(Pipeline(ws, Options(dry_llm=True, progress=lambda m: None)).cmd_plan())
    before = tree_state(target)
    questions: list[str] = []
    from dn.errors import DnError

    with pytest.raises(DnError) as ei:
        Pipeline(
            ws, Options(confirm=lambda q: (questions.append(q), False)[1], progress=lambda m: None)
        ).cmd_apply()
    assert ei.value.exit_code == 1 and questions
    assert tree_state(target) == before
    assert not ws.manifest_path.exists()


def test_run_log_file(target: Path):
    """AC-079: every command writes .dn/logs/<run_id>.log."""
    write(target, "a.md", "# a\n")
    code, out = dn_json("scan", str(target))
    log = target / ".dn" / "logs" / f"{out['run_id']}.log"
    assert code == 0 and log.is_file()
    assert Path(out["log"]) == log


def test_export_formats(tmp_path: Path):
    """AC-068: export --format md / jsonl / zip produce one .md, one line per file, and a zip of _knowledge/."""
    root = copy_sample(tmp_path / "tree")
    assert dn_json("run", str(root), "--dry-llm")[0] == 0
    kdir = root / "organized/_knowledge"
    files = sorted(p.relative_to(kdir).as_posix() for p in kdir.rglob("*") if p.is_file())
    code, md = dn_json("export", str(root), "--format", "md")
    text = Path(md["export"]).read_text(encoding="utf-8")
    assert code == 0 and md["export"].endswith(".md")
    assert all(f"file: {f}" in text for f in files)
    assert text.index("file: INDEX.md") < text.index("file: overview.md")
    code, jl = dn_json("export", str(root), "--format", "jsonl")
    lines = [json.loads(ln) for ln in Path(jl["export"]).read_text(encoding="utf-8").splitlines()]
    assert code == 0 and sorted(x["path"] for x in lines) == files
    code, zp = dn_json("export", str(root), "--format", "zip")
    with zipfile.ZipFile(zp["export"]) as z:
        assert sorted(n.removeprefix("_knowledge/") for n in z.namelist()) == files
    assert dn_json("export", str(root), "--format", "pdf")[0] == 2


def test_second_apply_run_no_changes(tmp_path: Path):
    """AC-101: a second `run --apply --yes` makes no LLM calls, appends no moves, prints `no changes`."""
    root = copy_sample(tmp_path / "tree")
    assert dn_json("run", str(root), "--dry-llm", "--apply", "--yes")[0] == 0
    ws = open_ws(root)
    moves_before = sum(len(r.moves) for r in load_manifest(ws).runs)
    code, out = dn_json("run", str(root), "--dry-llm", "--apply", "--yes")
    assert code == 0, out
    assert out["llm"]["generated"] == 0 and out["changes"] is False and out["message"] == "no changes"
    assert sum(len(r.moves) for r in load_manifest(ws).runs) == moves_before
    assert "apply" not in out


@pytest.mark.parametrize("args", [["apply"], ["run", "--apply", "--dry-llm"]])
def test_non_tty_requires_yes(target: Path, args: list[str]):
    """AC-102: without a TTY and without --yes, apply refuses immediately with exit 2 and moves nothing."""
    with_taxonomy(target)
    write(target, "a.md", "# specs specification\n")
    ws = open_ws(target)
    asyncio.run(Pipeline(ws, Options(dry_llm=True, progress=lambda m: None)).cmd_plan())
    before = tree_state(target)
    p = run_dn(args[0], str(target), *args[1:], input="")
    assert p.returncode == 2
    assert "--yes" in p.stderr
    assert tree_state(target) == before


def test_missing_plan_or_manifest(target: Path):
    """AC-103: apply without plan.json and undo without manifest.json exit 2 with an error."""
    write(target, "a.md", "# a\n")
    p = run_dn("apply", str(target), "--yes")
    assert p.returncode == 2 and "no plan found" in p.stderr
    u = run_dn("undo", str(target))
    assert u.returncode == 2 and "no manifest found" in u.stderr
