# @covers AC-006, AC-007, AC-008, AC-009, AC-012, AC-013, AC-104
"""OS differences (Windows / Linux / macOS) are confined to this module.

Names are generated with one common rule set whose constraints are the union
of all three OSes (Windows being the strictest), so a tree organized on one OS
can be copied to another unchanged.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import unicodedata
from pathlib import Path

_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# @assumption AS-004
WINDOWS_MAX_PATH = 260
MAX_NAME_CHARS = 200
MAX_NAME_BYTES = 255
POSIX_MAX_PATH = 4096


def current_os() -> str:
    return "windows" if sys.platform.startswith("win") else ("macos" if sys.platform == "darwin" else "linux")


def normalize(s: str) -> str:
    """NFC normalization used for comparison keys and written names."""
    return unicodedata.normalize("NFC", s)


def name_key(s: str) -> str:
    """Comparison key: NFC + casefold (macOS/Windows are case-insensitive by default)."""
    return normalize(s).casefold()


def same_name(a: str, b: str) -> bool:
    return name_key(a) == name_key(b)


def safe_name(name: str) -> str:
    """Make a single path component valid on all three OSes.

    Removes ``<>:"/\\|?*`` and control characters, strips trailing dots/spaces,
    and prefixes Windows reserved device names with ``_``. Never returns an
    empty name, ``.`` or ``..``.
    """
    s = _FORBIDDEN.sub("", normalize(name))
    s = s.rstrip(". ")
    if not s:
        return "_"
    stem = s.split(".", 1)[0]
    if stem.upper() in _RESERVED:
        s = "_" + s
    return s


def _truncate_stem(name: str, max_chars: int) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext_part = name, ""
    else:
        ext_part = "." + ext
    budget = max(1, max_chars - len(ext_part))
    stem = stem[:budget]
    while len((stem + ext_part).encode("utf-8")) > MAX_NAME_BYTES and len(stem) > 1:
        stem = stem[:-1]
    return (stem.rstrip(". ") or "_") + ext_part


def fit_path(parent: str | Path, name: str, os_name: str | None = None) -> str:
    """Return ``name`` truncated (extension kept) so ``parent/name`` fits the OS limits."""
    os_name = os_name or current_os()
    limit = MAX_NAME_CHARS
    if os_name == "windows":
        # total must stay below MAX_PATH (259 usable chars) including the separator
        limit = min(limit, WINDOWS_MAX_PATH - 1 - len(str(parent)) - 1)
    else:
        limit = min(limit, POSIX_MAX_PATH - 1 - len(str(parent)) - 1)
    if len(name) <= limit and len(name.encode("utf-8")) <= MAX_NAME_BYTES:
        return name
    return _truncate_stem(name, limit)


def long_path(path: str | Path, os_name: str | None = None) -> str:
    """On Windows prefix paths of 260+ chars with ``\\\\?\\``; elsewhere unchanged."""
    os_name = os_name or current_os()
    s = str(path)
    if os_name != "windows" or len(s) < WINDOWS_MAX_PATH or s.startswith("\\\\?\\"):
        return s
    s = os.path.abspath(s) if os_name == current_os() else s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def is_hidden(path: Path, st: os.stat_result | None = None, os_name: str | None = None) -> bool:
    """Leading dot on every OS; also FILE_ATTRIBUTE_HIDDEN on Windows."""
    if path.name.startswith("."):
        return True
    os_name = os_name or current_os()
    if os_name == "windows":
        st = st or os.lstat(path)
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2))
    return False


def is_link(path: Path, st: os.stat_result | None = None) -> bool:
    """Symlinks everywhere, plus NTFS junctions / mount points on Windows."""
    st = st or os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return True
    tag = getattr(st, "st_reparse_tag", 0)
    return bool(tag) and tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def to_trash(path: Path) -> None:
    from send2trash import send2trash

    send2trash(long_path(path))


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if current_os() == "windows":  # pragma: no cover - exercised on the Windows CI job
        import ctypes
        from typing import Any, cast

        kernel32 = cast(Any, ctypes).windll.kernel32  # windll exists only on Windows
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_dangerous_root(path: Path) -> bool:
    """Filesystem roots (``/``, ``C:\\``) and the home directory itself."""
    p = path.resolve()
    if p == Path(p.anchor) or p.parent == p:
        return True
    try:
        return p == Path.home().resolve()
    except RuntimeError:  # pragma: no cover - no home directory
        return False


_SLUG_STRIP = re.compile(r"[^\w]+", re.UNICODE)


def slugify(text: str) -> str:
    """Lower-kebab slug; keeps Unicode word characters (e.g. Japanese). May be empty."""
    s = unicodedata.normalize("NFKC", text).lower()
    s = _SLUG_STRIP.sub("-", s).replace("_", "-")
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return safe_name(s) if s else ""
