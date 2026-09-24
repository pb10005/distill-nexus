# @covers AC-023
# @assumption AS-032 - comments are listed too (instruction §5.2: "docstring/コメント")
"""Source code: first 200 lines, docstrings / comments, and definition names."""

from __future__ import annotations

import re
from pathlib import Path

from dn.extract.base import Extracted, normalize_newlines
from dn.extract.text import decode

HEAD_LINES = 200
_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:public\s+|private\s+|protected\s+|internal\s+|static\s+|async\s+|abstract\s+)*"
    r"(?:def|class|func|fn|function|interface|struct|enum|trait|type|impl)\s+([A-Za-z_][\w]*)",
    re.M,
)
_COMMENT = re.compile(r"^\s*(?:#|//|/\*+|\*|--)\s?(.*\S)", re.M)


_MODULE_DOC = re.compile(r'\A(?:[ \t]*(?:#[^\n]*)?\n)*[ \t]*[rRbBuU]?("""|\'\'\')(.*?)\1', re.S)
_DEF_DOC = re.compile(
    r'^[ \t]*(?:async[ \t]+)?(?:def|class)[ \t]+(\w+)[^\n]*:[ \t]*\n[ \t]*[rRbBuU]?("""|\'\'\')(.*?)\2',
    re.S | re.M,
)


def _python_docs(src: str) -> list[str]:
    """Docstrings found by regular expressions (no syntax tree; see FEAT-002 out_of_scope)."""
    docs: list[str] = []
    m = _MODULE_DOC.match(src)
    if m and m.group(2).strip():
        docs.append(f"module: {' '.join(m.group(2).split())}")
    for d in _DEF_DOC.finditer(src):
        if d.group(3).strip():
            docs.append(f"{d.group(1)}: {' '.join(d.group(3).split())}")
    return docs


def extract(path: Path) -> Extracted:
    src = normalize_newlines(decode(path.read_bytes()))
    lines = src.rstrip("\n").split("\n")
    docs = _python_docs(src) if path.suffix.lower() == ".py" else []
    names = [m.group(0).strip() for m in _DEF.finditer(src)]
    comments = [m.group(1) for m in _COMMENT.finditer(src)]
    parts = [f"# {path.name}", "", "## Definitions", ""]
    parts += [f"- `{n}`" for n in dict.fromkeys(names)] or ["- (none)"]
    if docs:
        parts += ["", "## Docstrings", ""] + [f"- {d}" for d in docs]
    if comments:
        parts += ["", "## Comments", ""] + [f"- {c}" for c in list(dict.fromkeys(comments))[:300]]
    lang = path.suffix.lstrip(".")
    parts += ["", f"## First {min(HEAD_LINES, len(lines))} lines", "", f"```{lang}"]
    parts += lines[:HEAD_LINES] + ["```"]
    if len(lines) > HEAD_LINES:
        parts.append(f"\n({len(lines) - HEAD_LINES} more lines not shown)")
    return Extracted(text="\n".join(parts) + "\n", type="code")
