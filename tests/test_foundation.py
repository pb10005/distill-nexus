"""FEAT-001: package, config, schemas, OS differences, safety checks."""

from __future__ import annotations

import os
import stat
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import run_dn, write
from pydantic import ValidationError

import dn
from dn.platform import fit_path, is_hidden, long_path, normalize, safe_name, same_name
from dn.schemas import Label


def test_version():
    """AC-001: `dn --version` prints the package version and exits 0."""
    p = run_dn("--version")
    assert p.returncode == 0
    assert p.stdout.strip() == dn.__version__


def test_init_creates_templates(target: Path):
    """AC-002: `dn init` creates .dn/config.yaml and taxonomy.yaml templates."""
    p = run_dn("init", str(target))
    assert p.returncode == 0, p.stderr
    cfg = target / ".dn" / "config.yaml"
    tax = target / "taxonomy.yaml"
    assert cfg.is_file() and "model: claude-sonnet-4-6" in cfg.read_text(encoding="utf-8")
    assert tax.is_file() and "categories:" in tax.read_text(encoding="utf-8")


def test_init_keeps_existing(target: Path):
    """AC-003: re-running `dn init` leaves existing files unchanged."""
    write(target, ".dn/config.yaml", "concurrency: 3\n")
    write(target, "taxonomy.yaml", "categories:\n  - slug: mine\n")
    run_dn("init", str(target))
    assert (target / ".dn/config.yaml").read_text(encoding="utf-8") == "concurrency: 3\n"
    assert (target / "taxonomy.yaml").read_text(encoding="utf-8") == "categories:\n  - slug: mine\n"


@pytest.mark.parametrize(
    ("config", "key"),
    [
        ("concurrency: 0\n", "concurrency"),
        ("confidence_threshold: 1.5\n", "confidence_threshold"),
        ("dedupe: shred\n", "dedupe"),
    ],
)
def test_invalid_config_exit_2(target: Path, config: str, key: str):
    """AC-004: an invalid config value names the key and exits with code 2."""
    write(target, ".dn/config.yaml", config)
    write(target, "a.txt", "hello")
    p = run_dn("scan", str(target))
    assert p.returncode == 2
    assert key in p.stderr


@pytest.mark.parametrize(
    "taxonomy",
    ["categories:\n  - slug: ''\n", "categories:\n  - slug: specs\n  - slug: specs\n"],
)
def test_invalid_taxonomy_exit_2(target: Path, taxonomy: str):
    """AC-005: empty or duplicate taxonomy slugs are rejected with exit code 2."""
    write(target, "taxonomy.yaml", taxonomy)
    write(target, "a.txt", "hello")
    p = run_dn("scan", str(target))
    assert p.returncode == 2
    assert "taxonomy.yaml" in p.stderr


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("Com1.log", "_Com1.log"),
        ("LPT9", "_LPT9"),
        ("nul", "_nul"),
        ("report. ", "report"),
        ("notes...", "notes"),
        ("CONSOLE.txt", "CONSOLE.txt"),
    ],
)
def test_safe_name_reserved_and_trailing(name: str, expected: str):
    """AC-006: reserved names get `_`; trailing dots / spaces are removed."""
    assert safe_name(name) == expected


def test_safe_name_forbidden_chars():
    """AC-007: <>:"/\\|?* and control characters are removed."""
    assert safe_name('a<b>c:d"e/f\\g|h?i*j.txt') == "abcdefghij.txt"
    assert safe_name("x\x00y\x1fz\tw.md") == "xyzw.md"


@pytest.mark.parametrize("name", ["..", ".", "", "<>|", "..."])
def test_safe_name_never_empty(name: str):
    """AC-104: names that become empty (.., ., '', only forbidden chars) become `_`."""
    assert safe_name(name) == "_"


def test_fit_path_truncates_keeping_extension():
    """AC-008: names too long for the OS path limit are truncated with the extension kept."""
    parent = "C:\\Users\\someone\\" + "d" * 150
    name = "very-long-name-" + "x" * 200 + ".docx"
    fitted = fit_path(parent, name, os_name="windows")
    assert fitted.endswith(".docx")
    assert len(parent) + 1 + len(fitted) <= 259
    posix = fit_path("/tmp", "y" * 300 + ".pdf", os_name="linux")
    assert posix.endswith(".pdf") and len(posix.encode()) <= 255
    assert fit_path("/tmp", "short.txt", os_name="linux") == "short.txt"
    long_win = "C:\\" + "a\\" * 140 + "file.txt"
    assert long_path(long_win, os_name="windows").startswith("\\\\?\\")
    assert long_path("/x/y", os_name="linux") == "/x/y"


def test_normalize_and_same_name():
    """AC-009: normalize() returns NFC and same_name() matches NFD/NFC and case variants."""
    nfd = unicodedata.normalize("NFD", "がくしゅう.txt")
    nfc = unicodedata.normalize("NFC", "がくしゅう.txt")
    assert nfd != nfc
    assert normalize(nfd) == nfc
    assert same_name(nfd, nfc) is True
    assert same_name("Report.PDF", "report.pdf") is True


@pytest.mark.parametrize(
    ("patch", "field"),
    [({"confidence": 1.5}, "confidence"), ({"date": "2024/03/12"}, "date"), ({"summary": None}, "summary")],
)
def test_label_schema_rejects(patch: dict[str, object], field: str):
    """AC-010: out-of-range confidence, bad date, missing summary raise ValidationError naming the field."""
    data: dict[str, object] = {
        "category": "specs",
        "confidence": 0.5,
        "title": "t",
        "summary": "s",
        "tags": [],
        "date": None,
    }
    data.update(patch)
    if patch.get("summary", "") is None:
        del data["summary"]
    with pytest.raises(ValidationError) as ei:
        Label.model_validate(data)
    assert field in str(ei.value)


@pytest.mark.parametrize("cmd", ["plan", "distill", "run"])
def test_missing_api_key_exit_2(target: Path, cmd: str):
    """AC-011: without ANTHROPIC_API_KEY (and no dry/replay) LLM commands exit 2 with a clear message."""
    write(target, "a.md", "# a\n")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    import subprocess
    import sys

    p = subprocess.run(
        [sys.executable, "-m", "dn.cli", cmd, str(target)], env=env, capture_output=True, text=True
    )
    assert p.returncode == 2
    assert "ANTHROPIC_API_KEY" in p.stderr


@pytest.mark.parametrize("cmd", ["scan", "plan", "apply", "run"])
def test_refuses_home(tmp_path: Path, cmd: str):
    """AC-012: the home directory itself is refused without --i-know-what-i-am-doing (exit 2)."""
    home = tmp_path / "home"
    home.mkdir()
    write(home, "a.md", "# a\n")
    extra = ["--dry-llm"] if cmd in ("plan", "run") else []
    p = run_dn(cmd, str(home), *extra, env={"HOME": str(home), "USERPROFILE": str(home)})
    assert p.returncode == 2, p.stderr
    assert "i-know-what-i-am-doing" in p.stderr
    assert not (home / ".dn" / "cache").exists()
    ok = run_dn(
        "scan", str(home), "--i-know-what-i-am-doing", env={"HOME": str(home), "USERPROFILE": str(home)}
    )
    assert ok.returncode == 0, ok.stderr


def test_refuses_filesystem_root():
    """AC-012: the filesystem root is refused before anything is read (exit code 2)."""
    from dn.errors import ConfigError
    from dn.workspace import Workspace

    root = Path(Path.cwd().anchor)
    with pytest.raises(ConfigError) as ei:
        Workspace.open(root)
    assert ei.value.exit_code == 2
    p = run_dn("apply", str(root), "--yes")
    assert p.returncode == 2 and "i-know-what-i-am-doing" in p.stderr


def test_windows_drive_root_is_dangerous(monkeypatch: pytest.MonkeyPatch):
    """AC-012: a drive root such as C:\\ counts as a filesystem root."""
    from dn import platform as plat

    fake = SimpleNamespace(resolve=lambda: fake, anchor="C:\\", parent=None)
    fake.parent = fake  # the root is its own parent
    assert plat.is_dangerous_root(fake)  # type: ignore[arg-type]


def test_is_hidden(tmp_path: Path):
    """AC-013: dot files, and FILE_ATTRIBUTE_HIDDEN on Windows, are hidden."""
    dot = write(tmp_path, ".secret", "x")
    plain = write(tmp_path, "visible.txt", "x")
    assert is_hidden(dot) is True
    assert is_hidden(plain) is False
    hidden_attr = SimpleNamespace(st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2))
    assert is_hidden(plain, hidden_attr, os_name="windows") is True  # type: ignore[arg-type]
    assert is_hidden(plain, SimpleNamespace(st_file_attributes=0), os_name="windows") is False  # type: ignore[arg-type]
