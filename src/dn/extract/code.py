# @covers AC-023
"""Source code: first 200 lines, docstrings / comments, and definition names."""

from __future__ import annotations

import ast
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


def _python_docs(src: str) -> tuple[list[str], list[str]]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [], []
    docs: list[str] = []
    names: list[str] = []
    mod_doc = ast.get_docstring(tree)
    if mod_doc:
        docs.append(f"module: {mod_doc}")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            kind = "class" if isinstance(node, ast.ClassDef) else "def"
            names.append(f"{kind} {node.name}")
            d = ast.get_docstring(node)
            if d:
                docs.append(f"{node.name}: {d}")
    return docs, names


def extract(path: Path) -> Extracted:
    src = normalize_newlines(decode(path.read_bytes()))
    lines = src.split("\n")
    if path.suffix.lower() == ".py":
        docs, names = _python_docs(src)
    else:
        docs, names = [], []
    if not names:
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
