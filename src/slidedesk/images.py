"""Render images for individual slides directly from .pptx files using render-pptx.

Rendering a deck writes all of its slides as PNGs to the on-disk cache; rendered decks
are also kept in a small in-memory LRU cache.
"""
from __future__ import annotations

import hashlib
import logging
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image
from pptx import Presentation

logger = logging.getLogger(__name__)

_render_lock = threading.RLock()
# Decks whose rendering raised; keyed by (path, mtime_ns, size, strip) so edits retry.
_failed_renders: dict = {}


def _render_presentation(prs) -> list:
    from render_pptx import render_slides

    return list(render_slides(prs))


def _render_visible_deck(pptx_path: Path) -> list:
    from . import pptx_tools

    prs = Presentation(str(pptx_path))
    for slide in prs.slides:
        pptx_tools.set_slide_hidden(slide, False)
    return _render_presentation(prs)


def _render_strip_deck(pptx_path: Path) -> list:
    from . import layout_strip

    with tempfile.TemporaryDirectory() as tmp:
        stripped = Path(tmp) / pptx_path.name
        layout_strip.make_strip_layout_copy(pptx_path, stripped)
        images = _render_presentation(Presentation(str(stripped)))
    return [image.convert("L").convert("RGB") for image in images]


@lru_cache(maxsize=4)
def _render_deck_cached(
    pptx_path: Path, mtime_ns: int, size: int, strip: bool
) -> Tuple[Image.Image, ...]:
    with _render_lock:
        images = _render_strip_deck(pptx_path) if strip else _render_visible_deck(pptx_path)
    return tuple(images)


def _cache_key(pptx_path: Path, index: int, strip: bool) -> str:
    stat = pptx_path.stat()
    raw = f"{pptx_path}|{stat.st_mtime_ns}|{stat.st_size}|{index}|{int(strip)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def cached_path(pptx_path: Path, index: int, cache_dir: Path, strip: bool = False) -> Optional[Path]:
    candidate = cache_dir / f"{_cache_key(pptx_path, index, strip)}.png"
    return candidate if candidate.exists() else None


def delete_cached(pptx_path: Path, slide_count: int, cache_dir: Path) -> None:
    """Remove cached PNGs of every slide of the current version of `pptx_path`."""
    for strip in (False, True):
        for index in range(slide_count):
            (cache_dir / f"{_cache_key(pptx_path, index, strip)}.png").unlink(missing_ok=True)
    clear_cache()


def render_slide_image(
    pptx_path: Path, index: int, cache_dir: Path, strip: bool = False
) -> Image.Image:
    """Return slide `index` (0-based) of `pptx_path` as a PIL Image.

    PNGs are cached on disk in `cache_dir`, keyed by deck path/mtime/size, slide
    index and layout mode. With `strip`, the grayscale strip-layout rendering is
    returned. Every slide is rendered as if visible, including hidden slides.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cached_path(pptx_path, index, cache_dir, strip)
    if cached is not None:
        return Image.open(cached)

    # Concurrent requests (e.g. browser retries) must not each re-render the same deck.
    with _render_lock:
        cached = cached_path(pptx_path, index, cache_dir, strip)
        if cached is not None:
            return Image.open(cached)
        return _render_and_cache(pptx_path, index, cache_dir, strip)


def _render_and_cache(pptx_path: Path, index: int, cache_dir: Path, strip: bool) -> Image.Image:
    stat = pptx_path.stat()
    failure_key = (pptx_path, stat.st_mtime_ns, stat.st_size, strip)
    if failure_key in _failed_renders:
        raise ValueError(f"Rendering {pptx_path.name} failed earlier: {_failed_renders[failure_key]}")
    logger.warning("Slide %d of %s is not cached; rendering the deck now", index + 1, pptx_path)
    started = time.monotonic()
    try:
        rendered = _render_deck_cached(pptx_path, stat.st_mtime_ns, stat.st_size, strip)
    except Exception as exc:
        logger.exception("Rendering %s failed after %.1fs", pptx_path, time.monotonic() - started)
        _failed_renders[failure_key] = f"{type(exc).__name__}: {exc}"
        raise ValueError(f"Rendering {pptx_path.name} failed: {exc}") from exc
    logger.warning("Rendered %s in %.1fs", pptx_path.name, time.monotonic() - started)
    if not 0 <= index < len(rendered):
        raise ValueError(f"Slide {index} not found in {pptx_path}")
    # Persist every slide of the deck so later requests never trigger a re-render.
    for i, slide_image in enumerate(rendered):
        target = cache_dir / f"{_cache_key(pptx_path, i, strip)}.png"
        if not target.exists():
            tmp_target = target.with_suffix(".tmp")
            slide_image.save(tmp_target, format="PNG")
            tmp_target.replace(target)
    return rendered[index].copy()


def cache_deck_images(pptx_path: Path, cache_dir: Path) -> None:
    """Render and persist every slide of the deck with and without strip layout."""
    for strip in (False, True):
        render_slide_image(pptx_path, 0, cache_dir, strip=strip).close()


def clear_cache() -> None:
    _failed_renders.clear()
    _render_deck_cached.cache_clear()