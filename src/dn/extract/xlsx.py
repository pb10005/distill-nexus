# @covers AC-021
"""Spreadsheets: each sheet's first 200 rows as a GFM table, remaining row count noted."""

from __future__ import annotations

from pathlib import Path

from dn.extract.base import Extracted, gfm_table

MAX_ROWS = 200


def extract(path: Path) -> Extracted:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    out: list[str] = []
    sheets = len(wb.worksheets)
    try:
        for ws in wb.worksheets:
            rows: list[list[object]] = []
            total = 0
            for row in ws.iter_rows(values_only=True):
                if all(v is None for v in row):
                    continue
                total += 1
                if len(rows) < MAX_ROWS:
                    rows.append(list(row))
            out.append(f"## Sheet: {ws.title}")
            out.append(gfm_table(rows) if rows else "(empty)")
            if total > MAX_ROWS:
                out.append(f"({total - MAX_ROWS} more rows not shown; {total} rows in total)")
    finally:
        wb.close()
    return Extracted(text="\n\n".join(out) + "\n", type="xlsx", pages=sheets)
