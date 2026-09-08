"""The unified slidedeck Python API: `SlideProject` ties together indexing,
PDF conversion, slide-image rendering and search/export over one project folder.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import List, Optional

from PIL import Image
from pptx import Presentation
from pptx.slide import Slide as PptxSlide

from . import convert, db, embeddings, images, pptx_tools
from .models import Deck, Slide
from .scanner import BackgroundScanner, scan_once

log = logging.getLogger(__name__)

DB_FILENAME = "slidedeck.db"
CACHE_DIRNAME = ".slidedeck_cache"


class SlideProject:
    """Opens or creates a slidedeck project rooted at `folder`."""

    EMBEDDING_MODEL = embeddings.DEFAULT_MODEL

    def __init__(self, folder: str | Path):
        self.folder = Path(folder).expanduser().resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db_path = self.folder / DB_FILENAME
        self.cache_dir = self.folder / CACHE_DIRNAME
        self.cache_dir.mkdir(exist_ok=True)
        self.conn = db.connect(self.db_path)
        self._conn_lock = threading.RLock()
        self._status = {
            "running": False,
            "current_file": None,
            "last_run_started": None,
            "last_run_finished": None,
            "error": None,
        }
        self._scanner: Optional[BackgroundScanner] = None

    # -- indexing -----------------------------------------------------
    def scan(self) -> None:
        """Synchronously scan the folder once, updating the database."""
        with self._conn_lock:
            scan_once(self)
    
    def refresh_deck(self, deck_id: int) -> None:
        """Force a fresh scan of one deck and its derived database contents."""
        with self._conn_lock:
            scan_once(self, deck_id=deck_id, force=True)

    def scan_in_background(self, interval: float = 5.0) -> BackgroundScanner:
        """Start (or return the existing) background scan loop."""
        if self._scanner is None:
            self._scanner = BackgroundScanner(self, interval=interval)
        self._scanner.start()
        return self._scanner

    def stop_background_scan(self) -> None:
        if self._scanner:
            self._scanner.stop()

    def scan_status(self) -> dict:
        return dict(self._status)

    def close(self) -> None:
        self.stop_background_scan()
        convert.shutdown()
        self.conn.close()

    # -- reading data ---------------------------------------------------
    def decks(self) -> List[Deck]:
        with self._conn_lock:
            rows = self.conn.execute("SELECT * FROM decks ORDER BY pptx_path").fetchall()
        return [Deck.from_row(r) for r in rows]

    def deck(self, deck_id: int) -> Optional[Deck]:
        with self._conn_lock:
            row = self.conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
        return Deck.from_row(row) if row else None

    def slides(self, deck_id: int, include_hidden: bool = True) -> List[Slide]:
        with self._conn_lock:
            if include_hidden:
                rows = self.conn.execute(
                    "SELECT * FROM slides WHERE deck_id = ? ORDER BY index_in_deck", (deck_id,)
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM slides WHERE deck_id = ? AND hidden = 0 "
                    "ORDER BY index_in_deck",
                    (deck_id,),
                ).fetchall()
        return [Slide.from_row(r) for r in rows]

    def slide(self, slide_id: int) -> Optional[Slide]:
        with self._conn_lock:
            row = self.conn.execute("SELECT * FROM slides WHERE id = ?", (slide_id,)).fetchone()
        return Slide.from_row(row) if row else None

    def search(self, query: str) -> List[Slide]:
        """Full-text search over slide text, newest decks first."""
        with self._conn_lock:
            rows = self.conn.execute(
                "SELECT slides.* FROM slides_fts "
                "JOIN slides ON slides.id = slides_fts.rowid "
                "WHERE slides_fts MATCH ? ORDER BY slides.deck_id, slides.index_in_deck",
                (query,),
            ).fetchall()
        return [Slide.from_row(r) for r in rows]

    # -- semantic search --------------------------------------------------
    def embedding_status(self) -> dict:
        """Report whether semantic search is usable, and cache coverage."""
        with self._conn_lock:
            total = self.conn.execute(
                "SELECT COUNT(*) AS c FROM slides WHERE text != ''"
            ).fetchone()["c"]
            done = self.conn.execute(
                "SELECT COUNT(*) AS c FROM slide_embeddings WHERE model = ?",
                (self.EMBEDDING_MODEL,),
            ).fetchone()["c"]
        return {"available": embeddings.is_available(), "total": total, "embedded": done}

    def embed_pending(self, limit: int = 20) -> int:
        """Compute and cache embeddings for up to `limit` slides that don't yet
        have one for the current model. Returns the number of slides embedded.
        """
        if not embeddings.is_available():
            return 0
        with self._conn_lock:
            rows = self.conn.execute(
                "SELECT slides.id, slides.text FROM slides "
                "LEFT JOIN slide_embeddings "
                "ON slide_embeddings.slide_id = slides.id AND slide_embeddings.model = ? "
                "WHERE slides.text != '' AND slide_embeddings.slide_id IS NULL "
                "LIMIT ?",
                (self.EMBEDDING_MODEL, limit),
            ).fetchall()
        count = 0
        for row in rows:
            try:
                vector = embeddings.embed_kiara(row["text"][:1024], embedding_model=self.EMBEDDING_MODEL)
            except Exception as exc:
                log.warning("Embedding failed for slide %s: %s", row["id"], exc, row["text"])
                continue
            with self._conn_lock:
                self.conn.execute(
                    "INSERT INTO slide_embeddings (slide_id, model, vector, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(slide_id) DO UPDATE SET "
                    "model=excluded.model, vector=excluded.vector, updated_at=excluded.updated_at",
                    (row["id"], self.EMBEDDING_MODEL, embeddings.pack_vector(vector), time.time()),
                )
                self.conn.commit()
            count += 1
        return count

    def similar_slides(self, slide_id: int, top_k: int = 16) -> List[Slide]:
        """Slides whose cached embedding is closest to the given slide's, best first.

        Returns an empty list if the slide has no cached embedding yet.
        """
        with self._conn_lock:
            row = self.conn.execute(
                "SELECT vector FROM slide_embeddings WHERE slide_id = ? AND model = ?",
                (slide_id, self.EMBEDDING_MODEL),
            ).fetchone()
            if row is None:
                return []
            target = embeddings.unpack_vector(row["vector"])
            rows = self.conn.execute(
                "SELECT slide_id, vector FROM slide_embeddings WHERE model = ? AND slide_id != ?",
                (self.EMBEDDING_MODEL, slide_id),
            ).fetchall()
        scored = [
            (embeddings.cosine_similarity(target, embeddings.unpack_vector(row["vector"])), row["slide_id"])
            for row in rows
        ]
        scored.sort(key=lambda t: t[0], reverse=True)
        slides = [self.slide(sid) for _, sid in scored[:top_k]]
        return [s for s in slides if s is not None]

    def search_semantic(self, query: str, top_k: int = 20) -> List[Slide]:
        """Embedding-based semantic search over cached slide vectors, best matches first."""
        query_vector = embeddings.embed_kiara(query, embedding_model=self.EMBEDDING_MODEL)
        with self._conn_lock:
            rows = self.conn.execute(
                "SELECT slide_id, vector FROM slide_embeddings WHERE model = ?",
                (self.EMBEDDING_MODEL,),
            ).fetchall()
        scored = [
            (embeddings.cosine_similarity(query_vector, embeddings.unpack_vector(row["vector"])), row["slide_id"])
            for row in rows
        ]
        scored.sort(key=lambda t: t[0], reverse=True)
        slides = [self.slide(slide_id) for _, slide_id in scored[:top_k]]
        return [s for s in slides if s is not None]

    # -- rendering --------------------------------------------------------
    def slide_image(self, slide_id: int, dpi: int = 110) -> Image.Image:
        """Render the given slide to a PIL Image, using the correct PDF export."""
        slide = self.slide(slide_id)
        if slide is None:
            raise KeyError(f"No such slide: {slide_id}")
        deck = self.deck(slide.deck_id)
        if deck is None:
            raise KeyError(f"No such deck: {slide.deck_id}")

        if slide.hidden:
            if not deck.hidden_pdf_path:
                raise FileNotFoundError("hidden.pdf export not available yet")
            pdf_path = self.folder / deck.hidden_pdf_path
            page = slide.index_in_deck + 1
        else:
            if not deck.pdf_path:
                raise FileNotFoundError("pdf export not available yet")
            pdf_path = self.folder / deck.pdf_path
            page = slide.visible_pdf_page

        return images.render_page(pdf_path, page, self.cache_dir, dpi=dpi)

    def slide_pptx_object(self, slide_id: int) -> PptxSlide:
        """Open the source .pptx and return the python-pptx Slide object."""
        slide = self.slide(slide_id)
        if slide is None:
            raise KeyError(f"No such slide: {slide_id}")
        deck = self.deck(slide.deck_id)
        prs = Presentation(str(self.folder / deck.pptx_path))
        return prs.slides[slide.index_in_deck]

    def slide_text(self, slide_id: int) -> str:
        slide = self.slide(slide_id)
        if slide is None:
            raise KeyError(f"No such slide: {slide_id}")
        return slide.text

    # -- export -----------------------------------------------------------
    def export_selection(self, slide_ids: List[int], out_filename: str) -> Path:
        """Copy the given slides (by id, in the given order) into a new .pptx
        saved inside the project folder, returning its path.
        """
        if not slide_ids:
            raise ValueError("No slides selected for export")

        pairs = []
        template_path: Optional[Path] = None
        for slide_id in slide_ids:
            slide = self.slide(slide_id)
            if slide is None:
                continue
            deck = self.deck(slide.deck_id)
            pptx_path = self.folder / deck.pptx_path
            if template_path is None:
                template_path = pptx_path
            pairs.append((pptx_path, slide.index_in_deck))

        if not pairs or template_path is None:
            raise ValueError("None of the given slide ids exist")

        out_path = self.folder / out_filename
        if out_path.suffix.lower() != ".pptx":
            out_path = out_path.with_suffix(".pptx")
        pptx_tools.save_selection_as_pptx(pairs, template_path, out_path)
        return out_path
