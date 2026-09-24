# @covers AC-012, AC-026, AC-037, AC-079
"""Per-target working area (``.dn/``): paths, run ids, logs and ``errors.jsonl``."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import traceback
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dn.config import DN_DIR, Config, Taxonomy, load_config, load_taxonomy
from dn.errors import ConfigError
from dn.platform import is_dangerous_root

log = logging.getLogger("dn")


def new_run_id() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S-%f")


def atomic_write_text(path: Path, text: str) -> None:
    """Write via temp file + replace so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield records; a truncated trailing line (crash mid-append) is skipped."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                log.warning("skipping unreadable line in %s", path)


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    atomic_write_text(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    torn = False
    if path.exists() and path.stat().st_size:
        with path.open("rb") as rf:
            rf.seek(-1, os.SEEK_END)
            torn = rf.read(1) != b"\n"  # a crash left a partial last line
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(("\n" if torn else "") + json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


@dataclass
class Workspace:
    root: Path
    config: Config
    taxonomy: Taxonomy | None
    out: Path
    run_id: str = field(default_factory=new_run_id)
    error_count: int = 0
    warnings: list[str] = field(default_factory=list)
    _log_handler: logging.Handler | None = None

    @classmethod
    def open(cls, root: Path, out: Path | None = None, allow_dangerous: bool = False) -> Workspace:
        root = Path(root).expanduser()
        if not root.is_dir():
            raise ConfigError(f"not a directory: {root}")
        root = root.resolve()
        if is_dangerous_root(root) and not allow_dangerous:
            raise ConfigError(
                f"refusing to operate on {root} (filesystem root or home directory); "
                "pass --i-know-what-i-am-doing to override"
            )
        cfg = load_config(root)
        tax = load_taxonomy(root)
        out_dir = (Path(out).expanduser() if out else root / "organized").resolve()
        return cls(root=root, config=cfg, taxonomy=tax, out=out_dir)

    # ---- layout
    @property
    def dn_dir(self) -> Path:
        return self.root / DN_DIR

    @property
    def cache(self) -> Path:
        return self.dn_dir / "cache"

    @property
    def inventory_path(self) -> Path:
        return self.cache / "inventory.jsonl"

    @property
    def text_dir(self) -> Path:
        return self.cache / "text"

    @property
    def labels_path(self) -> Path:
        return self.cache / "labels.jsonl"

    @property
    def plan_path(self) -> Path:
        return self.cache / "plan.json"

    @property
    def facts_dir(self) -> Path:
        return self.cache / "facts"

    @property
    def llm_cache_dir(self) -> Path:
        return self.cache / "llm"

    @property
    def errors_path(self) -> Path:
        return self.dn_dir / "errors.jsonl"

    @property
    def logs_dir(self) -> Path:
        return self.dn_dir / "logs"

    @property
    def lock_path(self) -> Path:
        return self.dn_dir / "lock"

    @property
    def knowledge_dir(self) -> Path:
        return self.out / "_knowledge"

    @property
    def duplicates_dir(self) -> Path:
        return self.out / "_duplicates"

    @property
    def manifest_path(self) -> Path:
        return self.out / "manifest.json"

    def rel(self, path: Path) -> str:
        """POSIX path relative to root when inside it, absolute POSIX otherwise."""
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def abs(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    # ---- errors
    def record_error(self, path: str, phase: str, exc: BaseException) -> None:
        self.error_count += 1
        append_jsonl(
            self.errors_path,
            {
                "run_id": self.run_id,
                "path": path,
                "phase": phase,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": "".join(traceback.format_exception(exc)),
            },
        )
        log.warning("%s failed for %s: %s", phase, path, exc)

    # ---- logging
    def setup_logging(self, verbosity: int = 0) -> Path:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / f"{self.run_id}.log"
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.setLevel(logging.DEBUG)
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        self._log_handler = handler
        return log_path

    def close_logging(self) -> None:
        if self._log_handler is not None:
            log.removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None

    def write_usage(self, usage: dict[str, Any]) -> Path:
        path = self.logs_dir / f"{self.run_id}.json"
        atomic_write_text(path, json.dumps({"run_id": self.run_id, **usage}, ensure_ascii=False, indent=2))
        return path
