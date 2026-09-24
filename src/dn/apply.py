# @covers AC-049, AC-050, AC-051, AC-052, AC-053, AC-054, AC-074, AC-089, AC-090, AC-091, AC-092, AC-093, AC-094, AC-095, AC-096, AC-097
"""Phase 5: execute ``plan.json`` safely, record ``manifest.json``, and undo it.

Safety rules (§5.5, §9):
- every entry is validated before anything moves (existence, unchanged content,
  free destination, writable parent, free space, destination inside root/out);
- existing files are never overwritten; nothing is ever deleted except a copy
  we made ourselves (undo of --copy) after its hash is confirmed;
- each completed move is written to the manifest immediately (atomic replace),
  so a crash leaves a readable manifest describing exactly what happened.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from dn.errors import EXIT_OK, EXIT_PARTIAL, DnError
from dn.platform import long_path, pid_alive, to_trash
from dn.scan import hash_file
from dn.schemas import Manifest, ManifestRun, Move, Plan, PlanEntry
from dn.workspace import Workspace, atomic_write_text

log = logging.getLogger("dn.apply")


# ------------------------------------------------------------------ lock


@contextmanager
def dn_lock(ws: Workspace) -> Iterator[None]:
    """``.dn/lock`` holding our PID; stale locks (dead PID) are taken over."""
    # @assumption AS-029
    path = ws.lock_path
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(path.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if pid and pid_alive(pid):
                raise DnError(f"another dn process (pid {pid}) holds {path}") from None
            log.warning("removing stale lock %s (pid %s is not running)", path, pid)
            ws.warnings.append(f"stale lock removed (pid {pid})")
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        break
    else:  # pragma: no cover - lost a race twice
        raise DnError(f"could not acquire {path}")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


# ------------------------------------------------------------------ manifest


def load_manifest(ws: Workspace, must_exist: bool = False) -> Manifest:
    if ws.manifest_path.exists():
        return Manifest.model_validate_json(ws.manifest_path.read_text(encoding="utf-8"))
    if must_exist:
        raise DnError(f"no manifest found at {ws.manifest_path}; nothing to undo", exit_code=2)
    return Manifest(root=ws.root.as_posix())


def save_manifest(ws: Workspace, manifest: Manifest) -> None:
    # @assumption AS-028
    atomic_write_text(ws.manifest_path, manifest.model_dump_json(by_alias=True, indent=2))


# ------------------------------------------------------------------ helpers


def _within(p: Path, bases: list[Path]) -> bool:
    rp = p.resolve()
    for b in bases:
        try:
            rp.relative_to(b.resolve())
            return True
        except ValueError:
            continue
    return False


def _nearest_existing(p: Path) -> Path:
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _hash_of(path: Path, mode: str | None) -> str:
    return hash_file(path, full=(mode == "full"))


def _move_file(src: Path, dst: Path) -> None:
    """Rename when possible; across devices copy, verify size + hash, then remove."""
    if dst.exists():
        raise FileExistsError(f"destination exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(long_path(src), long_path(dst))
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    shutil.copy2(long_path(src), long_path(dst))
    if os.path.getsize(dst) != os.path.getsize(src) or hash_file(dst, True) != hash_file(src, True):
        os.unlink(long_path(dst))
        raise OSError(f"verification failed after copying {src} to {dst}")
    os.unlink(long_path(src))


@dataclass
class ApplyResult:
    moved: int = 0
    copied: int = 0
    trashed: int = 0
    failed: int = 0
    skipped: int = 0
    run_id: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_PARTIAL if self.failed else EXIT_OK


def actionable(plan: Plan) -> list[PlanEntry]:
    return [pe for pe in plan.entries if pe.op in ("move", "copy", "trash")]


def validate_plan(ws: Workspace, plan: Plan) -> list[str]:
    """All-or-nothing pre-flight check. Returns a list of problems."""
    problems: list[str] = []
    bases = [ws.root, Path(plan.out_root)]
    targets: set[str] = set()
    need_bytes: dict[Path, int] = {}
    for pe in actionable(plan):
        src = ws.abs(pe.from_)
        if not src.is_file():
            problems.append(f"source missing: {pe.from_}")
            continue
        try:
            if _hash_of(src, pe.hash_mode) != pe.hash:
                problems.append(f"source changed since plan: {pe.from_}")
        except OSError as e:
            problems.append(f"source unreadable: {pe.from_}: {e}")
        if pe.op == "trash":
            continue
        assert pe.to is not None
        dst = ws.abs(pe.to)
        if not _within(dst, bases) or ".." in Path(pe.to).parts:
            problems.append(f"destination outside target and --out: {pe.to}")
            continue
        if dst.exists() or os.path.lexists(dst):
            problems.append(f"destination already exists: {pe.to}")
        key = dst.as_posix().casefold()
        if key in targets:
            problems.append(f"two entries share destination: {pe.to}")
        targets.add(key)
        anchor = _nearest_existing(dst.parent)
        if not os.access(anchor, os.W_OK):
            problems.append(f"destination not writable: {pe.to} ({anchor})")
        if pe.op == "copy" or src.stat().st_dev != anchor.stat().st_dev:
            need_bytes[anchor] = need_bytes.get(anchor, 0) + src.stat().st_size
    for anchor, need in need_bytes.items():
        free = shutil.disk_usage(anchor).free
        if need > free:
            problems.append(f"not enough free space at {anchor}: need {need} bytes, free {free}")
    return problems


def apply_plan(ws: Workspace, plan: Plan) -> ApplyResult:
    result = ApplyResult(run_id=ws.run_id)
    problems = validate_plan(ws, plan)
    if problems:
        result.problems = problems
        raise PlanValidationError(problems)
    with dn_lock(ws):
        manifest = load_manifest(ws)
        run = ManifestRun(run_id=ws.run_id, out_root=plan.out_root)
        entries = actionable(plan)
        if not entries:
            return result
        manifest.runs.append(run)
        save_manifest(ws, manifest)
        for pe in entries:
            src = ws.abs(pe.from_)
            try:
                if pe.op == "trash":
                    to_trash(src)
                    result.trashed += 1
                else:
                    assert pe.to is not None
                    dst = ws.abs(pe.to)
                    if pe.op == "copy":
                        if dst.exists():
                            raise FileExistsError(f"destination exists: {dst}")
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(long_path(src), long_path(dst))
                        result.copied += 1
                    else:
                        _move_file(src, dst)
                        result.moved += 1
            except Exception as exc:  # file-level failure: record and continue
                # @assumption AS-030
                result.failed += 1
                ws.record_error(pe.from_, "apply", exc)
                continue
            run.moves.append(
                Move.model_validate(
                    {"from": pe.from_, "to": pe.to, "hash": pe.hash, "hash_mode": pe.hash_mode, "op": pe.op}
                )
            )
            save_manifest(ws, manifest)
    return result


class PlanValidationError(DnError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("plan validation failed; nothing was moved:\n  " + "\n  ".join(problems))
        self.problems = problems


# ------------------------------------------------------------------ undo


@dataclass
class UndoResult:
    run_id: str | None = None
    restored: int = 0
    removed_copies: int = 0
    skipped: list[str] = field(default_factory=list)
    trashed: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_PARTIAL if (self.skipped or self.trashed) else EXIT_OK


def _remove_empty_dirs(start: Path, stop: Path) -> None:
    # @assumption AS-015
    p = start
    while p != stop and _within(p, [stop]):
        try:
            p.rmdir()
        except OSError:
            return
        p = p.parent


def undo(ws: Workspace, run_id: str | None = None) -> UndoResult:
    manifest = load_manifest(ws, must_exist=True)
    with dn_lock(ws):
        pending = [r for r in manifest.runs if r.undone_at is None]
        if run_id:
            run = next((r for r in manifest.runs if r.run_id == run_id), None)
            if run is None:
                raise DnError(f"unknown run id: {run_id}", exit_code=2)
            if run.undone_at:
                return UndoResult(run_id=run_id)
        elif pending:
            run = pending[-1]
        else:
            return UndoResult()
        result = UndoResult(run_id=run.run_id)
        later = manifest.runs[manifest.runs.index(run) + 1 :]
        later_sources = {m.from_ for r in later if r.undone_at is None for m in r.moves}
        out_root = Path(run.out_root)
        for mv in reversed(run.moves):
            if mv.op == "trash":
                result.trashed.append(mv.from_)
                log.warning("cannot restore from the OS trash automatically: %s", mv.from_)
                continue
            if mv.to is None or mv.op not in ("move", "copy"):
                continue
            src, dst = ws.abs(mv.from_), ws.abs(mv.to)
            if mv.to in later_sources:
                result.skipped.append(f"{mv.to}: moved again by a later run; undo that run first")
                continue
            if not dst.exists():
                result.skipped.append(f"{mv.to}: missing")
                continue
            try:
                same = _hash_of(dst, mv.hash_mode) == mv.hash
            except OSError as e:
                result.skipped.append(f"{mv.to}: unreadable ({e})")
                continue
            if not same:
                result.skipped.append(f"{mv.to}: modified after apply")
                continue
            if mv.op == "copy":
                os.unlink(long_path(dst))
                result.removed_copies += 1
                _remove_empty_dirs(dst.parent, out_root)
                continue
            if src.exists():
                result.skipped.append(f"{mv.from_}: another file now occupies the original location")
                continue
            try:
                _move_file(dst, src)
            except OSError as e:
                result.skipped.append(f"{mv.to}: {e}")
                continue
            result.restored += 1
            _remove_empty_dirs(dst.parent, out_root)
        for s in result.skipped:
            log.warning("undo skipped %s", s)
        run.undone_at = datetime.now(UTC).isoformat()
        save_manifest(ws, manifest)
    return result
