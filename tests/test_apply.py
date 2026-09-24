"""FEAT-007: apply and undo."""

from __future__ import annotations

import asyncio
import json
import os
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from conftest import dn_json, open_ws, run_dn, tree_state, with_taxonomy, write

import dn.apply as apply_mod
from dn.apply import PlanValidationError, apply_plan, load_manifest, undo
from dn.pipeline import Options, Pipeline
from dn.plan import load_plan

FILES = {
    "inbox/nda.txt": "contracts nda agreement between Nexus and Vendor A",
    "inbox/api.md": "# specs specification\n\nThe gateway spec.\n",
    "notes/sync.md": "# meeting minutes sync\n\nWeekly sync.\n",
}


def planned(root: Path, files: dict[str, str] = FILES, **opts: Any):  # type: ignore[no-untyped-def]
    with_taxonomy(root)
    for rel, text in files.items():
        write(root, rel, text)
    ws = open_ws(root, out=opts.pop("out", None))
    report = asyncio.run(Pipeline(ws, Options(dry_llm=True, progress=lambda m: None, **opts)).cmd_plan())
    assert report.exit_code == 0, report.data
    return ws


def moves(ws) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    return [m.model_dump(by_alias=True) for r in load_manifest(ws).runs for m in r.moves]


def test_apply_missing_source_moves_nothing(target: Path):
    """AC-049: a source deleted after planning fails validation; nothing moves; exit 1."""
    planned(target)
    (target / "inbox/api.md").unlink()
    before = tree_state(target)
    p = run_dn("apply", str(target), "--yes")
    assert p.returncode == 1
    assert "source missing: inbox/api.md" in p.stderr
    assert tree_state(target) == before
    assert not (target / "organized").exists()


def test_apply_moves_and_records(target: Path):
    """AC-050: all files move; each move is appended with relative POSIX from/to, hash, op; mtime kept."""
    ws = planned(target)
    before = tree_state(target)
    code, out = dn_json("apply", str(target), "--yes")
    assert code == 0 and out["apply"]["moved"] == 3
    recorded = moves(ws)
    assert len(recorded) == 3
    for m in recorded:
        assert set(m) >= {"from", "to", "hash", "op"} and m["op"] == "move"
        assert not Path(m["to"]).is_absolute() and "\\" not in m["to"] and m["to"].startswith("organized/")
        assert not (target / m["from"]).exists()
        dst = target / m["to"]
        assert dst.is_file()
        assert dst.stat().st_mtime_ns == before[m["from"]][1]


def test_apply_copy_keeps_sources(target: Path):
    """AC-051: with --copy sources stay in place and copies are created."""
    planned(target, copy=True)
    before = tree_state(target)
    code, out = dn_json("apply", str(target), "--yes")
    assert code == 0 and out["apply"]["copied"] == 3
    assert tree_state(target) == before
    ws = open_ws(target)
    for m in moves(ws):
        assert m["op"] == "copy" and (target / m["to"]).read_bytes() == (target / m["from"]).read_bytes()


def test_undo_restores_exactly_and_once(target: Path):
    """AC-052: undo restores paths/hashes/mtimes and records undone_at; a second undo moves nothing."""
    ws = planned(target)
    before = tree_state(target)
    assert run_dn("apply", str(target), "--yes").returncode == 0
    assert tree_state(target) != before
    code, out = dn_json("undo", str(target))
    assert code == 0 and out["undo"]["restored"] == 3
    assert tree_state(target) == before
    assert load_manifest(ws).runs[0].undone_at is not None
    code2, out2 = dn_json("undo", str(target))
    assert code2 == 0 and out2["undo"]["restored"] == 0 and out2.get("message") == "nothing to undo"
    assert tree_state(target) == before


def test_undo_skips_modified_and_occupied(target: Path):
    """AC-053: a modified destination or an occupied origin is skipped with a warning; the rest is restored."""
    ws = planned(target)
    assert run_dn("apply", str(target), "--yes").returncode == 0
    by_from = {m["from"]: m for m in moves(ws)}
    (target / by_from["inbox/api.md"]["to"]).write_text("edited after apply", encoding="utf-8")
    write(target, "notes/sync.md", "a new file where the old one was")
    p = run_dn("undo", str(target))
    assert p.returncode == 3
    assert "modified after apply" in p.stderr and "occupies the original location" in p.stderr
    assert (target / "inbox/nda.txt").is_file()
    assert (target / by_from["inbox/api.md"]["to"]).read_text(encoding="utf-8") == "edited after apply"
    assert (target / "notes/sync.md").read_text(encoding="utf-8") == "a new file where the old one was"


def test_apply_refuses_live_lock_and_escape(target: Path):
    """AC-054: a live lock blocks apply and undo; a destination outside target/--out is refused (exit 1)."""
    ws = planned(target)
    write(target, ".dn/lock", str(os.getpid()))  # this test process is alive
    before = tree_state(target)
    p = run_dn("apply", str(target), "--yes")
    assert p.returncode == 1 and "holds" in p.stderr
    write(
        target, "organized/manifest.json", json.dumps({"version": 1, "root": target.as_posix(), "runs": []})
    )
    u = run_dn("undo", str(target))
    assert u.returncode == 1 and "holds" in u.stderr
    (target / ".dn/lock").unlink()
    (target / "organized/manifest.json").unlink()
    plan = json.loads(ws.plan_path.read_text(encoding="utf-8"))
    plan["entries"][0]["to"] = "../escaped.txt"
    ws.plan_path.write_text(json.dumps(plan), encoding="utf-8")
    p2 = run_dn("apply", str(target), "--yes")
    assert p2.returncode == 1 and "outside target" in p2.stderr
    assert tree_state(target) == before
    assert not (target.parent / "escaped.txt").exists()
    if os.name != "nt":  # escape through a symlink inside organized/
        outside = target.parent / "outside-dir"
        outside.mkdir()
        (target / "organized").mkdir(exist_ok=True)
        (target / "organized" / "link").symlink_to(outside, target_is_directory=True)
        plan["entries"][0]["to"] = "organized/link/escaped.txt"
        ws.plan_path.write_text(json.dumps(plan), encoding="utf-8")
        p3 = run_dn("apply", str(target), "--yes")
        assert p3.returncode == 1 and "outside target" in p3.stderr
        assert list(outside.iterdir()) == []
        assert tree_state(target) == before


@pytest.mark.parametrize("case", ["dest_exists", "source_changed", "not_writable"])
def test_apply_prevalidation(target: Path, case: str, monkeypatch: pytest.MonkeyPatch):
    """AC-089: new file at a destination, changed source, or unwritable destination -> nothing moves, exit 1."""
    ws = planned(target)
    plan = load_plan(ws)
    victim = next(pe for pe in plan.entries if pe.op == "move")
    if case == "dest_exists":
        write(target, victim.to, "someone put this here")  # type: ignore[arg-type]
    elif case == "source_changed":
        (target / victim.from_).write_text("changed after planning", encoding="utf-8")
    else:
        real_access = os.access
        monkeypatch.setattr(
            apply_mod.os, "access", lambda p, mode: False if mode == os.W_OK else real_access(p, mode)
        )
    before = tree_state(target)
    with pytest.raises(PlanValidationError) as ei:
        apply_plan(ws, plan)
    assert ei.value.exit_code == 1
    expected = {
        "dest_exists": "destination already exists",
        "source_changed": "source changed",
        "not_writable": "not writable",
    }[case]
    assert any(expected in p for p in ei.value.problems)
    assert any((victim.to or victim.from_) in p or victim.from_ in p for p in ei.value.problems)
    assert tree_state(target) == before
    if case == "dest_exists":
        assert (target / victim.to).read_text(encoding="utf-8") == "someone put this here"  # type: ignore[arg-type]


def test_undo_trash_warns(target: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """AC-090: trashed duplicates are listed for manual restore; others are restored; exit 3."""
    files = {**FILES, "dup/nda-copy.txt": FILES["inbox/nda.txt"]}
    ws = planned(target, files, dedupe="trash")
    trash = tmp_path / "fake-trash"
    trash.mkdir()
    monkeypatch.setattr(apply_mod, "to_trash", lambda p: os.replace(p, trash / p.name))
    res = apply_plan(ws, load_plan(ws))
    assert res.trashed == 1 and res.moved == 3
    trashed_path = next(m["from"] for m in moves(ws) if m["op"] == "trash")
    p = run_dn("undo", str(target))
    assert p.returncode == 3
    assert "sent to the OS trash and must be restored manually" in p.stderr
    assert trashed_path in p.stderr
    for rel in FILES:
        assert (target / rel).is_file() or rel == trashed_path
    assert not (target / trashed_path).exists()


def test_undo_copy_removes_only_copies(target: Path):
    """AC-091: undo of --copy deletes hash-matching copies; originals keep content and mtime."""
    planned(target, copy=True)
    before = tree_state(target)
    assert run_dn("apply", str(target), "--yes").returncode == 0
    ws = open_ws(target)
    copies = [target / m["to"] for m in moves(ws)]
    assert all(c.exists() for c in copies)
    code, out = dn_json("undo", str(target))
    assert code == 0 and out["undo"]["removed_copies"] == 3
    assert not any(c.exists() for c in copies)
    assert tree_state(target) == before


def test_undo_copy_keeps_modified_copy(target: Path):
    """AC-091: a copy whose hash no longer matches is not deleted; the other copies are; originals unchanged."""
    planned(target, copy=True)
    before = tree_state(target)
    assert run_dn("apply", str(target), "--yes").returncode == 0
    ws = open_ws(target)
    copies = [target / m["to"] for m in moves(ws)]
    copies[0].write_text("the user edited this copy", encoding="utf-8")
    code, out = dn_json("undo", str(target))
    assert code == 3 and out["undo"]["removed_copies"] == 2
    assert copies[0].read_text(encoding="utf-8") == "the user edited this copy"
    assert not copies[1].exists() and not copies[2].exists()
    assert tree_state(target) == before


def test_apply_continues_after_file_error(target: Path, monkeypatch: pytest.MonkeyPatch):
    """AC-092: a PermissionError on the 3rd of 5 moves is recorded; the other 4 move; manifest has 4; exit 3."""
    files = {f"f{i}.md": f"# specs specification {i}\n" for i in range(5)}
    ws = planned(target, files)
    real = apply_mod._move_file
    calls = {"n": 0}

    def flaky(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise PermissionError("file is locked by another process")
        real(src, dst)

    monkeypatch.setattr(apply_mod, "_move_file", flaky)
    res = apply_plan(ws, load_plan(ws))
    assert res.moved == 4 and res.failed == 1 and res.exit_code == 3
    assert len(moves(ws)) == 4
    err = json.loads(ws.errors_path.read_text(encoding="utf-8").splitlines()[-1])
    assert err["phase"] == "apply" and "PermissionError" in err["error"]


@pytest.mark.parametrize("ending", ["normal", "exception", "interrupt"])
def test_lock_released(target: Path, ending: str, monkeypatch: pytest.MonkeyPatch):
    """AC-093: .dn/lock is gone after a normal end, an exception, or KeyboardInterrupt."""
    ws = planned(target)
    if ending == "exception":
        monkeypatch.setattr(
            apply_mod, "save_manifest", lambda *a: (_ for _ in ()).throw(RuntimeError("disk gone"))
        )
    elif ending == "interrupt":
        monkeypatch.setattr(apply_mod, "_move_file", lambda s, d: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        apply_plan(ws, load_plan(ws))
    except (RuntimeError, KeyboardInterrupt):
        assert ending != "normal"
    assert not ws.lock_path.exists()


def test_stale_lock_taken_over(target: Path):
    """AC-094: a lock with a dead PID is reported as stale and the apply proceeds."""
    planned(target)
    write(target, ".dn/lock", "999999")
    code, out = dn_json("apply", str(target), "--yes")
    assert code == 0 and out["apply"]["moved"] == 3
    assert any("stale lock" in w for w in out["warnings"])
    assert not (target / ".dn/lock").exists()


def _second_run_moving(ws, src_rel: str, dst_rel: str) -> str:  # type: ignore[no-untyped-def]
    from dn.scan import hash_file
    from dn.schemas import Plan, PlanEntry

    h = hash_file(ws.root / src_rel, full=True)
    plan = Plan(
        root=ws.root.as_posix(),
        out_root=ws.out.as_posix(),
        entries=[
            PlanEntry.model_validate(
                {
                    "from": src_rel,
                    "to": dst_rel,
                    "hash": h,
                    "hash_mode": "full",
                    "op": "move",
                    "category": "meetings",
                    "confidence": 1.0,
                }
            )
        ],
    )
    ws2 = open_ws(ws.root)
    apply_plan(ws2, plan)
    return ws2.run_id


def test_undo_run_order(tmp_path: Path):
    """AC-095: plain undo reverts only the latest run; undo --run-id A skips files B moved again."""
    for scenario in ("latest", "by-id"):
        root = tmp_path / scenario
        root.mkdir()
        ws = planned(root)
        run_a = apply_plan(ws, load_plan(ws)).run_id
        a_to = next(m["to"] for m in moves(ws) if m["from"] == "inbox/api.md")
        run_b = _second_run_moving(ws, a_to, "organized/meetings/api.md")
        if scenario == "latest":
            res = undo(open_ws(root))
            assert res.run_id == run_b and res.restored == 1
            assert (root / a_to).is_file()
            runs = {r.run_id: r for r in load_manifest(ws).runs}
            assert runs[run_a].undone_at is None and runs[run_b].undone_at is not None
        else:
            res = undo(open_ws(root), run_id=run_a)
            assert res.run_id == run_a
            assert any("moved again by a later run" in s for s in res.skipped)
            assert (root / "organized/meetings/api.md").is_file()
            assert (root / "inbox/nda.txt").is_file() and (root / "notes/sync.md").is_file()


def test_nfd_name_round_trip(target: Path):
    """AC-096: an NFD file name is recorded as-is in manifest.from and restored with the same code points."""
    nfd = unicodedata.normalize("NFD", "がくしゅう.txt")
    ws = planned(target, {nfd: "contracts nda agreement text"})
    if nfd not in os.listdir(target):
        pytest.skip("filesystem normalizes names; cannot hold an NFD name")
    apply_plan(ws, load_plan(ws))
    (m,) = moves(ws)
    assert m["from"] == nfd
    assert Path(m["to"]).name == unicodedata.normalize("NFC", nfd)
    undo(open_ws(target))
    assert nfd in os.listdir(target)


def test_external_out_round_trip(tmp_path: Path):
    """AC-097: --out outside the target moves files to <out>/<category>/ and undo puts them back."""
    root = tmp_path / "root"
    root.mkdir()
    out_dir = tmp_path / "elsewhere"
    planned(root, out=out_dir)
    before = tree_state(root)
    code, res = dn_json("apply", str(root), "--yes", "--out", str(out_dir))
    assert code == 0 and res["apply"]["moved"] == 3
    assert (out_dir / "contracts" / "nda.txt").is_file()
    assert (out_dir / "specs" / "api.md").is_file()
    code2, _ = dn_json("undo", str(root), "--out", str(out_dir))
    assert code2 == 0
    assert tree_state(root) == before
