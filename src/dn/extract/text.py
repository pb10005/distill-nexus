# @covers AC-019
"""Plain text family: charset detection (CP932 aware), newline normalization."""

from __future__ import annotations

from pathlib import Path

from charset_normalizer import from_bytes

from dn.extract.base import Extracted, normalize_newlines


def decode(data: bytes) -> str:
    if not data:
        return ""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    best = from_bytes(data).best()
    if best is not None:
        return str(best)
    return data.decode("cp932", errors="replace")


def extract(path: Path) -> Extracted:
    return Extracted(text=normalize_newlines(decode(path.read_bytes())), type="text")
