# @covers AC-021
"""Word documents: headings kept, tables as GFM tables, body order preserved."""

from __future__ import annotations

from pathlib import Path

from dn.extract.base import Extracted, gfm_table


def extract(path: Path) -> Extracted:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = docx.Document(str(path))
    out: list[str] = []
    for block in doc.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(block, doc)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            if style.startswith("Heading"):
                try:
                    level = min(int(style.split()[-1]), 3)
                except ValueError:
                    level = 2
                out.append("#" * level + " " + text)
            elif style == "Title":
                out.append("# " + text)
            elif "List" in style:
                out.append("- " + text)
            else:
                out.append(text)
        elif tag == "tbl":
            t = Table(block, doc)
            rows = [[cell.text for cell in row.cells] for row in t.rows]
            out.append(gfm_table(rows))
    return Extracted(text="\n\n".join(out) + "\n", type="docx")
