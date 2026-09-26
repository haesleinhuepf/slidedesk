"""The unified slidedesk Python API: `SlideProject` ties together indexing,
PDF conversion, slide-image rendering and search/export over one project folder.
"""
from __future__ import annotations

import logging
import tempfile
import threading
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional

from PIL import Image
from pptx import Presentation
from pptx.slide import Slide as PptxSlide

from . import convert, db, embeddings, image_embeddings, images, pptx_tools
from .models import Deck, Slide
from .scanner import BackgroundScanner, scan_once

log = logging.getLogger(__name__)

DB_FILENAME = "slidedesk.db"
CACHE_DIRNAME = ".slidedesk/_cache"


class _EmbeddingTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag in {"br", "div", "li", "p"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"div", "li", "p"}:
            self.parts.append(" ")

    def text(self) -> str:
        return " ".join("".join(self.parts).split())


def _text_for_embedding(text: str) -> str:
    parser = _EmbeddingTextParser()
    parser.feed(unescape(text))
    parser.close()
    return parser.text()



class SlideProject:
    """Opens or creates a slidedesk project rooted at `folder`."""

    EMBEDDING_MODEL = embeddings.DEFAULT_MODEL
    IMAGE_EMBEDDING_MODEL = image_embeddings.MODEL

    def __init__(self, folder: str | Path):
        self.folder = Path(folder).expanduser().resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db_path = self.folder / ".slidedesk" / DB_FILENAME
        self.cache_dir = self.folder / CACHE_DIRNAME
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        legacy_db_path = self.folder / DB_FILENAME
        if not self.db_path.exists() and legacy_db_path.is_file():
            legacy_db_path.rename(self.db_path)
        self.conn = db.connect(self.db_path)
        self._conn_lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._status = {
            "running": False,
            "current_file": None,
            "last_run_started": None,
            "last_run_finished": None,
            "error": None,
        }
        self._scanner: Optional[BackgroundScanner] = None
        self._export_tempdir = tempfile.TemporaryDirectory(prefix="slidedesk-exports-")
        self.export_dir = Path(self._export_tempdir.name)
        self._export_lock = threading.Lock()
        self._export_number = 0
        self._image_embedding_lock = threading.Lock()

    # -- indexing -----------------------------------------------------
    def scan(self, stop_event: Optional[threading.Event] = None) -> None:
        """Synchronously scan the folder once, updating the database."""
        scan_once(self, stop_event=stop_event)
    
    def refresh_deck(self, deck_id: int) -> None:
        """Force a fresh scan of one deck and its derived database contents."""
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
        print("Closing SlideDesk project...")
        print("Stopping background scan...")
        self.stop_background_scan()
        convert.shutdown()
        print("Closing database connection...")
        self.conn.close()
        print("Cleaning up export temporary directory...")
        self._export_tempdir.cleanup()
        print("SlideDesk project closed.")

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

    def set_slide_hidden(self, slide_id: int, hidden: bool) -> None:
        """Persist a user's visibility choice without modifying the source PPTX."""
        with self._conn_lock, self.conn:
            if self.slide(slide_id) is None:
                raise KeyError(f"No such slide: {slide_id}")
            self.conn.execute(
                "UPDATE slides SET hidden = ?, user_hidden = ? WHERE id = ?",
                (int(hidden), int(hidden), slide_id),
            )

    def set_deck_slides_hidden(self, deck_id: int, hidden: bool) -> None:
        """Set visibility individually for every current slide in a deck."""
        with self._conn_lock, self.conn:
            if self.deck(deck_id) is None:
                raise KeyError(f"No such deck: {deck_id}")
            self.conn.execute(
                "UPDATE slides SET hidden = ?, user_hidden = ? WHERE deck_id = ?",
                (int(hidden), int(hidden), deck_id),
            )

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
    def _embeddings_available(self) -> bool:
        return embeddings.is_available()

    def _embed(self, text: str) -> List[float]:
        return embeddings.embed_local(text, embedding_model=self.EMBEDDING_MODEL)

    def embedding_status(self) -> dict:
        """Report text and vision cache coverage without running inference."""
        with self._conn_lock:
            total = self.conn.execute(
                "SELECT COUNT(*) AS c FROM slides WHERE text != ''"
            ).fetchone()["c"]
            done = self.conn.execute(
                "SELECT COUNT(*) AS c FROM slide_embeddings WHERE model = ?",
                (self.EMBEDDING_MODEL,),
            ).fetchone()["c"]
            image_rows = self.conn.execute(
                "SELECT s.id, e.source_key FROM slides s "
                "LEFT JOIN slide_image_embeddings e ON e.slide_id = s.id AND e.model = ?",
                (self.IMAGE_EMBEDDING_MODEL,),
            ).fetchall()
            image_done = sum(
                row["source_key"] is not None
                and row["source_key"] == self._image_source_key(row["id"])
                for row in image_rows
            )
        return {
            "available": self._embeddings_available(), "total": total, "embedded": done,
            "vision": {"available": image_embeddings.is_available(),
                       "total": len(image_rows), "embedded": image_done},
        }

    def embed_pending(self, limit: int = 20, stop_event: Optional[threading.Event] = None) -> int:
        """Compute and cache embeddings for up to `limit` slides that don't yet
        have one for the current model. Returns the number of slides embedded.
        """
        if not self._embeddings_available():
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
            if stop_event and stop_event.is_set():
                break
            try:
                vector = self._embed(_text_for_embedding(row["text"])[:1024])
            except Exception as exc:
                log.warning("Embedding failed for slide %s: %s", row["id"], exc)
                continue
            with self._conn_lock:
                current = self.slide(row["id"])
                if current is None or current.text != row["text"]:
                    continue
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

    def _image_source_key(self, slide_id: int) -> Optional[str]:
        """Identify the rendered source, including changes to either PDF export."""
        slide = self.slide(slide_id)
        if slide is None:
            return None
        deck = self.deck(slide.deck_id)
        if deck is None or not (self.folder / deck.pptx_path).is_file():
            return None
        pdf = deck.hidden_pdf_path if slide.visible_pdf_page is None else deck.pdf_path
        if not pdf:
            return None
        try:
            stat = (self.folder / pdf).stat()
            pptx_stat = (self.folder / deck.pptx_path).stat()
        except OSError:
            return None
        return repr((pdf, stat.st_mtime_ns, stat.st_size, pptx_stat.st_mtime_ns,
                     slide.index_in_deck, slide.visible_pdf_page))

    def embed_images_pending(self, limit: int = 20, stop_event=None) -> int:
        """Cache all renderable slides, including hidden slides and empty text.

        Rendering and inference run outside the DB lock. Recheck the source before
        writing so a concurrent refresh cannot store an obsolete vector.
        """
        count = 0
        with self._image_embedding_lock:
            with self._conn_lock:
                rows = self.conn.execute(
                    "SELECT s.id, e.model, e.source_key FROM slides s "
                    "LEFT JOIN slide_image_embeddings e ON e.slide_id = s.id ORDER BY s.id"
                ).fetchall()
            for row in rows:
                print(f"Processing slide {row['id']} for image embedding.", stop_event)
                if count >= limit or (stop_event is not None and stop_event.is_set()):
                    break
                key = self._image_source_key(row["id"])
                if key is None or (row["model"] == self.IMAGE_EMBEDDING_MODEL
                                   and row["source_key"] == key):
                    continue
                try:
                    with self.slide_image(row["id"]) as image:
                        vector = image_embeddings.embed_image(image, self.IMAGE_EMBEDDING_MODEL)
                except embeddings.EmbeddingError:
                    # Model/dependency failures affect every slide; retry next pass.
                    raise
                except Exception as exc:
                    log.warning("Image rendering failed for slide %s: %s", row["id"], exc)
                    continue
                with self._conn_lock:
                    if stop_event is not None and stop_event.is_set():
                        break
                    if self._image_source_key(row["id"]) != key:
                        continue
                    self.conn.execute(
                        "INSERT INTO slide_image_embeddings "
                        "(slide_id, model, source_key, vector, updated_at) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(slide_id) DO UPDATE SET model=excluded.model, "
                        "source_key=excluded.source_key, vector=excluded.vector, updated_at=excluded.updated_at",
                        (row["id"], self.IMAGE_EMBEDDING_MODEL, key,
                         embeddings.pack_vector(vector), time.time()),
                    )
                    self.conn.commit()
                count += 1
        return count

    def _similar_cached(self, slide_id: int, image: bool = False) -> List[int]:
        table = "slide_image_embeddings" if image else "slide_embeddings"
        model = self.IMAGE_EMBEDDING_MODEL if image else self.EMBEDDING_MODEL
        with self._conn_lock:
            row = self.conn.execute(
                f"SELECT * FROM {table} WHERE slide_id = ? AND model = ?",
                (slide_id, model),
            ).fetchone()
            if row is None or (image and row["source_key"] != self._image_source_key(slide_id)):
                return []
            target = embeddings.unpack_vector(row["vector"])
            rows = self.conn.execute(
                f"SELECT * FROM {table} WHERE model = ? AND slide_id != ? ORDER BY slide_id",
                (model, slide_id),
            ).fetchall()
        scored = [
            (embeddings.cosine_similarity(target, embeddings.unpack_vector(row["vector"])), row["slide_id"])
            for row in rows
            if not image or row["source_key"] == self._image_source_key(row["slide_id"])
        ]
        scored.sort(key=lambda t: t[0], reverse=True)
        return [sid for _, sid in scored]

    def similar_slides(self, slide_id: int, top_k: int = 16) -> List[Slide]:
        """Image matches first, then additional text matches, in one deduplicated list.

        Reserve half the slots for visual matches. Fill unused slots from either
        cache without comparing scores from different embedding spaces.
        """
        if top_k <= 0:
            return []
        visual = self._similar_cached(slide_id, image=True)
        textual = self._similar_cached(slide_id)
        ids = list(dict.fromkeys(visual[:(top_k + 1) // 2] + textual + visual))[:top_k]
        slides = [self.slide(sid) for sid in ids]
        return [s for s in slides if s is not None]

    def search_semantic(self, query: str, top_k: int = 20) -> List[Slide]:
        """Embedding-based semantic search over cached slide vectors, best matches first."""
        query_vector = self._embed(query)
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

        if slide.visible_pdf_page is None:
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
    def export_selection(self, slide_ids: List[int], out_filename: Optional[str] = None) -> Path:
        """Copy the given slides (by id, in the given order) into a new .pptx
        saved in a temporary folder, returning its path. Exports remain available
        until the project is closed. By default, filenames are numbered starting
        at export_1.pptx for each project session.
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

        with self._export_lock:
            if out_filename is None:
                while True:
                    self._export_number += 1
                    out_path = self.export_dir / f"export_{self._export_number}.pptx"
                    if not out_path.exists():
                        break
            else:
                out_path = self.export_dir / Path(out_filename).name
                if out_path.suffix.lower() != ".pptx":
                    out_path = out_path.with_suffix(".pptx")
            # Only publish the completed, resaved presentation to the download directory.
            with tempfile.TemporaryDirectory(prefix="slidedesk-repair-") as staging:
                source = Path(staging) / "source.pptx"
                repaired = Path(staging) / "repaired.pptx"
                pptx_tools.save_selection_as_pptx(pairs, template_path, source)
                convert.repair_pptx(source, repaired)
                repaired.replace(out_path)
        return out_path
