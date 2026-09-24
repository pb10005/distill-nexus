"""FEAT-002: scan and extract phases."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
from pathlib import Path

import pytest
from conftest import (
    SAMPLE_TREE,
    FakeClient,
    dn_json,
    make_llm,
    open_ws,
    run_dn,
    smart_handler,
    tool_of,
    with_taxonomy,
    write,
)

from dn.extract import extract_all, read_extracted, text_path
from dn.pipeline import Options, Pipeline
from dn.scan import load_inventory, scan


def _extract(root: Path, client: FakeClient | None = None, want_images: bool = True, mode: str = "dry"):  # type: ignore[no-untyped-def]
    ws = open_ws(root)
    res = scan(ws)
    llm = make_llm(root / ".dn", client=client, mode=mode if client is None else "live")
    stats = asyncio.run(extract_all(ws, res.entries, llm, want_images=want_images))
    return ws, res, stats


def _text_for(ws, rel: str) -> tuple[object, str]:  # type: ignore[no-untyped-def]
    entry = next(e for e in load_inventory(ws) if e.path == rel)
    return read_extracted(text_path(ws, entry.hash))


def test_scan_exclusions(target: Path):
    """AC-014: excluded dirs/files, hidden files, .dnignore, config exclude and dn outputs are not inventoried."""
    for rel in [
        ".git/config",
        "node_modules/x/index.js",
        "__pycache__/m.pyc",
        ".dn/cache/old.txt",
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
        "~$tmp.docx",
        ".hidden.md",
        "sub/.also-hidden",
        "ignored/skip.md",
        "scratch.tmp",
        "organized/_knowledge/INDEX.md",
        "organized/_duplicates/a.md",
        "organized/manifest.json",
    ]:
        write(target, rel, "x")
    write(target, ".dnignore", "ignored/\n")
    write(target, ".dn/config.yaml", "exclude:\n  - '*.tmp'\n")
    for rel in ["a.md", "docs/b.txt", "organized/specs/c.md"]:
        write(target, rel, rel)
    code, out = dn_json("scan", str(target))
    assert code == 0
    inv = sorted(e.path for e in load_inventory(open_ws(target)))
    assert inv == ["a.md", "docs/b.txt", "organized/specs/c.md"]
    assert out["scan"]["files"] == 3


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_scan_does_not_follow_symlinks(tmp_path: Path):
    """AC-015: without --follow-symlinks the link target is not inventoried."""
    outside = tmp_path / "outside"
    write(outside, "secret.md", "outside")
    root = tmp_path / "root"
    write(root, "a.md", "inside")
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "file-link.md").symlink_to(outside / "secret.md")
    ws = open_ws(root)
    assert [e.path for e in scan(ws).entries] == ["a.md"]
    assert sorted(e.path for e in scan(ws, follow_symlinks=True).entries) == [
        "a.md",
        "file-link.md",
        "link/secret.md",
    ]


def test_scan_duplicates(target: Path):
    """AC-016: identical content gets the same blake2b hash, is reported as duplicate, hash_mode recorded."""
    write(target, "one.md", "same content")
    write(target, "two.md", "same content")
    write(target, "other.md", "different")
    code, out = dn_json("scan", str(target))
    assert code == 0
    inv = {e.path: e for e in load_inventory(open_ws(target))}
    assert inv["one.md"].hash == inv["two.md"].hash != inv["other.md"].hash
    assert sorted(next(iter(out["scan"]["duplicates"].values()))) == ["one.md", "two.md"]
    assert all(e.hash_mode in ("head4m", "full") for e in inv.values())
    assert inv["one.md"].hash_mode == "full"  # <= 4MB is hashed completely
    import hashlib

    expected = hashlib.blake2b(b"same content" + b"12", digest_size=20).hexdigest()  # content, then size
    assert inv["one.md"].hash == expected


def test_scan_reuses_hashes_and_drops_deleted(target: Path):
    """AC-017: unchanged files are not re-hashed (hash_computed=0); deleted files leave the inventory."""
    write(target, "a.md", "a")
    write(target, "b.md", "b")
    first = dn_json("scan", str(target))[1]
    assert first["scan"]["hash_computed"] == 2
    second = dn_json("scan", str(target))[1]
    assert second["scan"]["hash_computed"] == 0
    (target / "b.md").unlink()
    third = dn_json("scan", str(target))[1]
    assert third["scan"]["removed"] == 1
    assert [e.path for e in load_inventory(open_ws(target))] == ["a.md"]


def test_scan_skips_too_large(target: Path):
    """AC-018: files over max_file_mb are recorded as skipped: too_large, not hashed or extracted."""
    write(target, ".dn/config.yaml", "max_file_mb: 0.001\n")
    write(target, "big.txt", "x" * 5000)
    write(target, "small.txt", "y")
    code, out = dn_json("scan", str(target))
    assert code == 0 and out["scan"]["skipped"] == ["big.txt"]
    ws, res, stats = _extract(target)
    big = next(e for e in load_inventory(ws) if e.path == "big.txt")
    assert big.skipped == "too_large" and big.hash is None
    assert "big.txt" in res.skipped
    assert len(list(ws.text_dir.glob("*.md"))) == 1


def test_extract_cp932_crlf(target: Path):
    """AC-019: CP932 + CRLF text is stored as UTF-8 without BOM with LF newlines, keeping the Japanese text."""
    write(target, "minutes.txt", "議事録\r\n決定事項: レート制限は毎秒100リクエスト\r\n".encode("cp932"))
    ws, _, _ = _extract(target)
    entry = load_inventory(ws)[0]
    raw = text_path(ws, entry.hash).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    text = raw.decode("utf-8")
    assert "決定事項: レート制限は毎秒100リクエスト" in text


def test_extract_frontmatter(tmp_path: Path):
    """AC-020: every extracted file starts with source/type/pages/chars/lang/quality frontmatter."""
    root = tmp_path / "t"
    shutil.copytree(SAMPLE_TREE, root)
    ws, _, stats = _extract(root)
    files = list(ws.text_dir.glob("*.md"))
    assert len(files) == stats.extracted >= 30
    for f in files:
        raw = f.read_text(encoding="utf-8")
        assert raw.startswith("---\n")
        head = raw.split("\n---\n", 1)[0]
        for key in ("source:", "type:", "pages:", "chars:", "lang:", "quality:"):
            assert key in head, (f, key)


def test_extract_office(tmp_path: Path):
    """AC-021: docx tables and xlsx's first 200 rows as GFM tables; xlsx remaining rows; pptx slide numbers and notes."""
    root = tmp_path / "t"
    shutil.copytree(SAMPLE_TREE, root)
    ws, _, _ = _extract(root)
    _, docx_text = _text_for(ws, "inbox/NDA_vendor-a.docx")
    assert (
        "| Party | Role |" in docx_text
        and "|---|---|" in docx_text
        and "| Vendor A | Recipient |" in docx_text
    )
    assert "# Mutual NDA - Vendor A" in docx_text
    _, xlsx_text = _text_for(ws, "data/sales.xlsx")
    table_rows = [ln for ln in xlsx_text.splitlines() if ln.startswith("| ") and "---" not in ln]
    assert len(table_rows) == 200  # header + 199 data rows
    assert "50 more rows not shown; 250 rows in total" in xlsx_text
    _, pptx_text = _text_for(ws, "meetings/weekly.pptx")
    assert "## Slide 1" in pptx_text and "## Slide 2" in pptx_text
    assert "Mention the incident" in pptx_text and "Owner: platform" in pptx_text
    assert "Notes (slide 1)" in pptx_text


def test_extract_pdf_mixed_ocr(tmp_path: Path):
    """AC-022: only pages with < 50 chars go to OCR; quality is mixed."""
    root = tmp_path / "t"
    write(root, "scan.pdf", (SAMPLE_TREE / "inbox/scanned-contract.pdf").read_bytes())
    client = FakeClient(lambda req, n: {"transcript": "OCR PAGE TWO TEXT", "description": "a signature page"})
    ws, _, stats = _extract(root, client=client)
    assert stats.failed == 0
    assert len(client.requests) == 1
    assert tool_of(client.requests[0]) == "submit_vision"
    blocks = client.requests[0]["messages"][0]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png"
    assert "(p2)" in blocks[1]["text"]
    meta, text = _text_for(ws, "scan.pdf")
    assert meta.quality == "mixed" and meta.pages == 2
    assert "OCR PAGE TWO TEXT" in text and "agreement term is 12 months" in text


def test_extract_html_and_code(target: Path):
    """AC-023: HTML without script/style; code with first 200 lines, docstrings and definition names."""
    write(target, "page.html", (SAMPLE_TREE / "specs/architecture.html").read_text(encoding="utf-8"))
    lines = [
        '"""Module doc: billing helpers."""',
        "",
        "def first():",
        '    """Compute the first invoice."""',
        "    return 1",
        "",
        "class Ledger:",
        '    """Holds entries."""',
    ]
    lines += [f"x_{i} = {i}" for i in range(len(lines), 300)]
    write(target, "big.py", "\n".join(lines) + "\n")
    ws, _, _ = _extract(target)
    _, html = _text_for(ws, "page.html")
    assert "The API gateway sits in front of the order and billing services." in html
    assert "var x" not in html and "body{}" not in html and "menu" not in html
    assert "| Service | Owner |" in html
    _, code = _text_for(ws, "big.py")
    assert "- `def first`" in code and "- `class Ledger`" in code
    assert "first: Compute the first invoice." in code and "module: Module doc: billing helpers." in code
    assert "x_199 = 199" in code and "x_200 = 200" not in code
    assert "100 more lines not shown" in code


def test_extract_image_vision_and_no_images(tmp_path: Path):
    """AC-024: with images enabled the image goes to the LLM and the transcript is stored; by default nothing is sent."""
    png = (SAMPLE_TREE / "specs/er-diagram.png").read_bytes()
    a = tmp_path / "a"
    write(a, "diagram.png", png)
    client = FakeClient(lambda req, n: {"transcript": "ORDERS -> BILLING", "description": "an ER diagram"})
    ws, _, _ = _extract(a, client=client)
    assert len(client.requests) == 1
    block = client.requests[0]["messages"][0]["content"][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/png"
    assert base64.b64decode(block["source"]["data"]) == png
    _, text = _text_for(ws, "diagram.png")
    assert "ORDERS -> BILLING" in text
    # --no-images through the command option (Options.no_images is what `dn plan --no-images` sets)
    b = tmp_path / "b"
    write(b, "diagram.png", png)
    with_taxonomy(b)
    client2 = FakeClient(smart_handler())
    ws2 = open_ws(b)
    report = asyncio.run(
        Pipeline(ws2, Options(llm_client=client2, progress=lambda m: None)).cmd_plan()  # default: images off
    )
    assert report.exit_code == 0
    assert [tool_of(r) for r in client2.requests if tool_of(r) == "submit_vision"] == []
    for r in client2.requests:
        assert all(
            not (isinstance(c, dict) and c.get("type") == "image")
            for c in r["messages"][0]["content"]
            if isinstance(r["messages"][0]["content"], list)
        )
    meta, text2 = _text_for(ws2, "diagram.png")
    assert meta.quality == "none" and text2.strip() == ""


def test_extract_truncates_large_text(target: Path):
    """AC-025: >200,000 chars keeps head/middle/tail 60,000 chars each and records truncated: true."""
    body = "".join(f"{i:07d}\n" for i in range(40000))  # 320,000 chars
    write(target, "huge.log", body)
    ws, _, _ = _extract(target)
    meta, text = _text_for(ws, "huge.log")
    assert meta.truncated is True
    head, middle, tail = text.split("\n\n[... truncated ...]\n\n")
    assert head == body[:60000]
    assert tail == body[-60000:]
    mid = len(body) // 2
    assert middle == body[mid - 30000 : mid + 30000]


def test_extract_error_isolated(target: Path):
    """AC-026: a broken docx is recorded in errors.jsonl (path/phase/error/traceback); other files are extracted."""
    write(target, "broken.docx", b"PK\x03\x04 this is not a real zip")
    write(target, "ok.md", "# fine\n")
    ws, _, stats = _extract(target)
    assert stats.failed == 1 and stats.extracted == 1
    errors = [json.loads(ln) for ln in ws.errors_path.read_text(encoding="utf-8").splitlines()]
    assert errors[0]["path"] == "broken.docx" and errors[0]["phase"] == "extract"
    assert errors[0]["error"] and "Traceback" in errors[0]["traceback"]
    _, ok = _text_for(ws, "ok.md")
    assert "# fine" in ok


def test_extract_magic_over_extension(target: Path, caplog: pytest.LogCaptureFixture):
    """AC-027: a .txt containing a PNG is treated as an image and a mismatch warning is logged."""
    write(target, "not-really.txt", (SAMPLE_TREE / "specs/er-diagram.png").read_bytes())
    client = FakeClient(lambda req, n: {"transcript": "", "description": "gray box"})
    with caplog.at_level(logging.WARNING, logger="dn"):
        ws, _, _ = _extract(target, client=client)
    meta, _ = _text_for(ws, "not-really.txt")
    assert meta.type == "image"
    assert len(client.requests) == 1
    assert any("type mismatch" in r.message and "not-really.txt" in r.message for r in caplog.records)


def test_scan_confirms_head_hash_collisions(target: Path):
    """AC-098: >4MB files equal in size and first 4MB get distinct full hashes (hash_mode full)."""
    head = os.urandom(4 * 1024 * 1024)
    write(target, "a.bin", head + b"A" * 1024)
    write(target, "b.bin", head + b"B" * 1024)
    write(target, "c.bin", os.urandom(5 * 1024 * 1024))
    code, out = dn_json("scan", str(target))
    assert code == 0 and out["scan"]["duplicates"] == {}
    ws = open_ws(target)
    res = scan(ws)
    inv = {e.path: e for e in res.entries}
    assert inv["a.bin"].hash != inv["b.bin"].hash
    assert inv["a.bin"].hash_mode == "full" and inv["b.bin"].hash_mode == "full"
    assert inv["c.bin"].hash_mode == "head4m"
    assert res.duplicates == {}


def test_images_cli_flag(tmp_path: Path):
    """AC-024: through the real CLI, `dn plan --images` sends the image while the default (and --no-images) does not."""
    png = (SAMPLE_TREE / "specs/er-diagram.png").read_bytes()
    results = {}
    for flag, extra in (("images", ["--images"]), ("default", []), ("no-images", ["--no-images"])):
        root = tmp_path / flag
        write(root, "diagram.png", png)
        write(root, "notes.md", "# specs specification\n\ntext\n")
        with_taxonomy(root)
        p = run_dn("plan", str(root), "--dry-llm", "--json", *extra)
        assert p.returncode == 0, p.stderr
        out = json.loads(p.stdout)
        meta, text = _text_for(open_ws(root), "diagram.png")
        results[flag] = (out, meta, text)
    out_on, meta_on, text_on = results["images"]
    # with --images the image goes to the (dry) vision call and its answer becomes the text
    assert meta_on.quality == "vision" and "(dry-llm) image diagram.png" in text_on
    for flag in ("default", "no-images"):  # nothing sent: no vision output, one LLM generation fewer
        out_off, meta_off, text_off = results[flag]
        assert meta_off.quality == "none" and text_off.strip() == ""
        assert out_on["llm"]["generated"] == out_off["llm"]["generated"] + 1
