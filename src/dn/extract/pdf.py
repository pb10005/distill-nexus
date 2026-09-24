# @covers AC-022
"""PDF: text per page; pages with < 50 characters are rendered at 150 dpi for OCR."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dn.extract.base import Extracted, ImageRequest, normalize_newlines

MIN_PAGE_CHARS = 50
OCR_DPI = 150


# @assumption AS-025 - with --no-images low-text pages stay as text
def extract(path: Path, want_images: bool = True) -> Extracted:
    import pymupdf

    doc: Any = pymupdf.open(str(path))  # type: ignore[no-untyped-call]
    pages: list[str] = []
    images: list[ImageRequest] = []
    try:
        for i, page in enumerate(doc, start=1):
            text = normalize_newlines(page.get_text("text")).strip()
            if len(text) < MIN_PAGE_CHARS and want_images:
                pix = page.get_pixmap(dpi=OCR_DPI)
                images.append(ImageRequest(locator=f"p{i}", data=pix.tobytes("png"), media_type="image/png"))
            pages.append(text)
        n = doc.page_count
    finally:
        doc.close()
    if not images:
        quality = "text"
    elif len(images) == n:
        quality = "ocr"
    else:
        quality = "mixed"
    body = "\n\n".join(f"## Page {i}\n\n{t}" for i, t in enumerate(pages, start=1))
    return Extracted(text=body, type="pdf", pages=n, quality=quality, images=images, page_texts=pages)
