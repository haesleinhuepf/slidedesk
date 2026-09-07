"""Render PNG images for individual slides from their PDF export, with caching."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from pdf2image import convert_from_path
from PIL import Image


def _cache_key(pdf_path: Path, page: int, dpi: int) -> str:
    stat = pdf_path.stat()
    raw = f"{pdf_path}|{stat.st_mtime}|{page}|{dpi}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def render_page(
    pdf_path: Path, page: int, cache_dir: Path, dpi: int = 110
) -> Image.Image:
    """Return the given 1-based `page` of `pdf_path` as a PIL Image, using a
    PNG cache on disk keyed by pdf path/mtime/page/dpi so repeat requests are cheap.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(pdf_path, page, dpi)
    cached = cache_dir / f"{key}.png"
    if cached.exists():
        return Image.open(cached)

    pages = convert_from_path(
        str(pdf_path), dpi=dpi, first_page=page, last_page=page
    )
    if not pages:
        raise ValueError(f"Page {page} not found in {pdf_path}")
    image = pages[0]
    image.save(cached, format="PNG")
    return image


def cached_path(
    pdf_path: Path, page: int, cache_dir: Path, dpi: int = 110
) -> Optional[Path]:
    key = _cache_key(pdf_path, page, dpi)
    candidate = cache_dir / f"{key}.png"
    return candidate if candidate.exists() else None
