# @covers AC-024
"""Images are transcribed and described by Claude vision (see extract.__init__)."""

from __future__ import annotations

from pathlib import Path

from dn.extract.base import Extracted, ImageRequest

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def extract(path: Path, want_images: bool = True, media_type: str | None = None) -> Extracted:
    mt = media_type or MEDIA_TYPES.get(path.suffix.lower(), "image/png")
    if not want_images:
        return Extracted(text="", type="image", quality="none")
    return Extracted(
        text="",
        type="image",
        quality="vision",
        images=[ImageRequest(locator="image", data=path.read_bytes(), media_type=mt)],
    )
