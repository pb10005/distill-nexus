# @covers AC-023
"""HTML body extraction with BeautifulSoup (script/style/navigation removed)."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup, Tag

from dn.extract.base import Extracted, gfm_table, normalize_newlines
from dn.extract.text import decode

# @assumption AS-006
DROP = ["script", "style", "noscript", "nav", "footer", "template", "iframe", "svg"]


def _render(node: Tag, out: list[str]) -> None:
    for child in node.children:
        if not isinstance(child, Tag):
            text = str(child).strip()
            if text:
                out.append(text)
            continue
        name = child.name
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = min(int(name[1]), 3)
            out.append("\n" + "#" * level + " " + child.get_text(" ", strip=True) + "\n")
        elif name == "table":
            rows: list[list[object]] = [
                [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                for tr in child.find_all("tr")
            ]
            out.append("\n" + gfm_table([r for r in rows if r]) + "\n")
        elif name == "li":
            out.append("- " + child.get_text(" ", strip=True))
        elif name in ("p", "pre", "blockquote"):
            out.append("\n" + child.get_text(" ", strip=True) + "\n")
        elif name == "br":
            out.append("\n")
        else:
            _render(child, out)


def extract(path: Path) -> Extracted:
    soup = BeautifulSoup(decode(path.read_bytes()), "html.parser")
    for tag in soup.find_all(DROP):
        tag.decompose()
    body = soup.body or soup
    parts: list[str] = []
    title = soup.title.get_text(strip=True) if soup.title else ""
    if title:
        parts.append(f"# {title}\n")
    _render(body, parts)
    text = "\n".join(p for p in parts if p is not None)
    import re

    text = re.sub(r"\n{3,}", "\n\n", normalize_newlines(text)).strip() + "\n"
    return Extracted(text=text, type="html")
