"""Render PNG images for individual slides from their PDF export, with caching."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pdf2image import convert_from_bytes
from PIL import Image


def _cache_key(pdf_path: Path, page: int) -> str:
    stat = pdf_path.stat()
    raw = f"{pdf_path}|{stat.st_mtime_ns}|{stat.st_size}|{page}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@lru_cache(maxsize=20)
def _read_pdf_bytes_cached(
    pdf_path: Path
) -> bytes:
    return pdf_path.read_bytes()


def read_pdf_bytes(pdf_path: Path) -> bytes:
    """Read a PDF from disk, caching up to 20 recent file versions."""
    return _read_pdf_bytes_cached(pdf_path)


def render_page(
    pdf_path: Path, page: int, cache_dir: Path
) -> Image.Image:
    """Return the given 1-based `page` of `pdf_path` as a PIL Image, using a
    PNG cache on disk keyed by pdf path/mtime/page so repeat requests are cheap.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(pdf_path, page)
    cached = cache_dir / f"{key}.png"
    if cached.exists():
        return Image.open(cached)

    pages = convert_from_bytes(
        read_pdf_bytes(pdf_path), dpi=80, first_page=page, last_page=page
    )
    if not pages:
        raise ValueError(f"Page {page} not found in {pdf_path}")
    image = pages[0]
    image.save(cached, format="PNG")
    return image


def cached_path(
    pdf_path: Path, page: int, cache_dir: Path
) -> Optional[Path]:
    key = _cache_key(pdf_path, page)
    candidate = cache_dir / f"{key}.png"
    return candidate if candidate.exists() else None
