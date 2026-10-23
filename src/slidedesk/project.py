"""The unified slidedesk Python API: `SlideProject` ties together indexing,
slide-image rendering and search/export over one project folder.
"""
from __future__ import annotations

import logging
import os
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

from . import (convert, db, embeddings, image_embeddings, images, mixed_embeddings,
               pptx_tools)
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
    MIXED_VLM_MODEL = mixed_embeddings.VLM_MODEL

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
        self._mixed_embedding_lock = threading.Lock()
        self._renderer = images.BackgroundRenderer(self.cache_dir)

    # -- indexing -----------------------------------------------------
    def scan(self, stop_event: Optional[threading.Event] = None) -> None:
        """Synchronously scan the folder once, updating the database."""
        scan_once(self, stop_event=stop_event)
    
    def refresh_deck(self, deck_id: int) -> None:
        """Delete a deck's cached PNGs and embeddings, then rescan it from scratch."""
        deck = self.deck(deck_id)
        if deck is None:
            return
        with self._scan_lock:
            slide_ids = [s.id for s in self.slides(deck_id)]
            pptx_path = self.folder / deck.pptx_path
            if pptx_path.is_file():
                images.delete_cached(pptx_path, len(slide_ids), self.cache_dir)
            with self._conn_lock, self.conn:
                for table in ("slide_embeddings", "slide_image_embeddings",
                              "slide_strip_image_embeddings", "slide_mixed_embeddings"):
                    self.conn.executemany(
                        f"DELETE FROM {table} WHERE slide_id = ?", [(i,) for i in slide_ids]
                    )
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
        print("Stopping slide rendering...")
        self._renderer.stop()
        convert.shutdown()
        print("Closing database connection...")
        self.conn.close()
        print("Cleaning up export temporary directory...")
        self._export_tempdir.cleanup()
        print("SlideDesk project closed.")

    # -- reading data ---------------------------------------------------
    def decks(self) -> List[Deck]:
        with self._conn_lock:
            rows = self.conn.execute(
                "SELECT * FROM decks WHERE load_failed = 0 ORDER BY pptx_path"
            ).fetchall()
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

    def slides_by_deck(self, deck_ids: Optional[List[int]] = None) -> dict:
        """Slides grouped by deck id, in deck order, from a single query.

        All decks by default, or only those in `deck_ids`.
        """
        with self._conn_lock:
            if deck_ids is None:
                rows = self.conn.execute(
                    "SELECT * FROM slides ORDER BY deck_id, index_in_deck"
                ).fetchall()
            else:
                ids = [int(i) for i in deck_ids]
                rows = []
                # Chunk to stay below SQLite's bound-parameter limit on old versions.
                for start in range(0, len(ids), 500):
                    chunk = ids[start:start + 500]
                    rows += self.conn.execute(
                        f"SELECT * FROM slides WHERE deck_id IN ({','.join('?' * len(chunk))}) "
                        "ORDER BY deck_id, index_in_deck",
                        chunk,
                    ).fetchall()
        grouped = {}
        for row in rows:
            slide = Slide.from_row(row)
            grouped.setdefault(slide.deck_id, []).append(slide)
        return grouped

    def deck_revisions(self) -> dict:
        """Map each deck id to a string that changes whenever the deck's metadata
        or its slides (added, reindexed, hidden/unhidden) change.

        `last_scanned` is excluded because every scan pass updates it. Reindexing
        always creates new slide ids (AUTOINCREMENT), so slide text changes are
        covered by the ids without reading the text.
        """
        with self._conn_lock:
            rows = self.conn.execute(
                "SELECT d.id, d.pptx_path, d.pptx_mtime, d.pdf_path, d.pdf_mtime, "
                "d.hidden_pdf_path, d.hidden_pdf_mtime, s.slides FROM decks d "
                "LEFT JOIN (SELECT deck_id, group_concat(id || ':' || hidden) AS slides "
                "FROM (SELECT deck_id, id, hidden FROM slides ORDER BY deck_id, id) "
                "GROUP BY deck_id) s ON s.deck_id = d.id"
            ).fetchall()
        return {row["id"]: repr(tuple(row)[1:]) for row in rows}

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
            mixed_rows = self.conn.execute(
                "SELECT s.id, e.source_key FROM slides s "
                "LEFT JOIN slide_mixed_embeddings e ON e.slide_id = s.id AND e.model = ?",
                (self._mixed_model_key(),),
            ).fetchall()
        keys = self._image_source_keys()
        image_done = sum(
            row["source_key"] is not None and row["source_key"] == keys.get(row["id"])
            for row in image_rows
        )
        strip_keys = self._image_source_keys(strip=True)
        mixed_done = sum(
            row["source_key"] is not None and row["source_key"] == strip_keys.get(row["id"])
            for row in mixed_rows
        )
        return {
            "available": self._embeddings_available(), "total": total, "embedded": done,
            "vision": {"available": image_embeddings.is_available(),
                       "total": len(image_rows), "embedded": image_done},
            "mixed": {"available": mixed_embeddings.is_available(),
                      "total": len(mixed_rows), "embedded": mixed_done},
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
            slide = self.slide(row["id"])
            deck = self.deck(slide.deck_id) if slide else None
            if slide and deck:
                print(f"Computing text embedding: {Path(deck.pptx_path).name}, "
                      f"slide {slide.index_in_deck + 1}")
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

    def _image_source_key(self, slide_id: int, strip: bool = False) -> Optional[str]:
        """Identify the rendered source, including changes to the .pptx file."""
        slide = self.slide(slide_id)
        if slide is None:
            return None
        deck = self.deck(slide.deck_id)
        if deck is None:
            return None
        try:
            stat = (self.folder / deck.pptx_path).stat()
        except OSError:
            return None
        return repr((deck.pptx_path, stat.st_mtime_ns, stat.st_size,
                     slide.index_in_deck, strip))

    def _image_source_keys(self, strip: bool = False) -> dict:
        """Bulk `_image_source_key` for all slides: one query and one stat per deck.

        File system access happens outside the DB lock, so status polling does not
        block other requests on large collections.
        """
        with self._conn_lock:
            rows = self.conn.execute(
                "SELECT s.id, s.index_in_deck, d.pptx_path FROM slides s "
                "JOIN decks d ON d.id = s.deck_id"
            ).fetchall()
        stats = {}
        keys = {}
        for row in rows:
            pptx_path = row["pptx_path"]
            if pptx_path not in stats:
                try:
                    stats[pptx_path] = (self.folder / pptx_path).stat()
                except OSError:
                    stats[pptx_path] = None
            stat = stats[pptx_path]
            if stat is not None:
                keys[row["id"]] = repr((pptx_path, stat.st_mtime_ns, stat.st_size,
                                        row["index_in_deck"], strip))
        return keys

    def embed_images_pending(self, limit: int = 20, stop_event=None) -> int:
        """Cache all renderable slides, including hidden slides and empty text.

        Rendering and inference run outside the DB lock. Recheck the source before
        writing so a concurrent refresh cannot store an obsolete vector. Both the
        normal and the strip-layout renderings are embedded (separate tables).
        """
        count = 0
        with self._image_embedding_lock:
            for strip in (False, True):
                count += self._embed_images_pending(strip, limit - count, stop_event)
        return count

    def _embed_images_pending(self, strip: bool, limit: int, stop_event) -> int:
        table = "slide_strip_image_embeddings" if strip else "slide_image_embeddings"
        count = 0
        if True:
            with self._conn_lock:
                rows = self.conn.execute(
                    f"SELECT s.id, e.model, e.source_key FROM slides s "
                    f"LEFT JOIN {table} e ON e.slide_id = s.id ORDER BY s.id"
                ).fetchall()
            keys = self._image_source_keys(strip)
            for row in rows:
                if count >= limit or (stop_event is not None and stop_event.is_set()):
                    break
                key = keys.get(row["id"])
                if key is None or (row["model"] == self.IMAGE_EMBEDDING_MODEL
                                   and row["source_key"] == key):
                    continue
                slide = self.slide(row["id"])
                deck = self.deck(slide.deck_id) if slide else None
                if slide and deck:
                    print(f"Computing image embedding: {Path(deck.pptx_path).name}, "
                          f"slide {slide.index_in_deck + 1}"
                          f"{' (strip layout)' if strip else ''}")
                try:
                    with (self.slide_image(row["id"], layout=False) if strip
                          else self.slide_image(row["id"])) as image:
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
                    if self._image_source_key(row["id"], strip) != key:
                        continue
                    self.conn.execute(
                        f"INSERT INTO {table} "
                        "(slide_id, model, source_key, vector, updated_at) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(slide_id) DO UPDATE SET model=excluded.model, "
                        "source_key=excluded.source_key, vector=excluded.vector, updated_at=excluded.updated_at",
                        (row["id"], self.IMAGE_EMBEDDING_MODEL, key,
                         embeddings.pack_vector(vector), time.time()),
                    )
                    self.conn.commit()
                count += 1
        return count

    def _mixed_model_key(self) -> str:
        """Mixed vectors depend on both the VLM and the text-embedding model."""
        return f"{self.MIXED_VLM_MODEL}|{self.EMBEDDING_MODEL}"

    def embed_mixed_pending(self, limit: int = 20, stop_event=None) -> int:
        """Describe strip-layout slide images with a VLM and cache text embeddings
        of the descriptions in `slide_mixed_embeddings`.

        Like image embeddings, inference runs outside the DB lock and the source is
        rechecked before writing. "No content" slides are cached with an empty vector.
        """
        model = self._mixed_model_key()
        count = 0
        with self._mixed_embedding_lock:
            with self._conn_lock:
                rows = self.conn.execute(
                    "SELECT s.id, e.model, e.source_key FROM slides s "
                    "LEFT JOIN slide_mixed_embeddings e ON e.slide_id = s.id ORDER BY s.id"
                ).fetchall()
            keys = self._image_source_keys(strip=True)
            for row in rows:
                if count >= limit or (stop_event is not None and stop_event.is_set()):
                    break
                key = keys.get(row["id"])
                if key is None or (row["model"] == model and row["source_key"] == key):
                    continue
                slide = self.slide(row["id"])
                deck = self.deck(slide.deck_id) if slide else None
                if slide and deck:
                    print(f"Computing mixed embedding: {Path(deck.pptx_path).name}, "
                          f"slide {slide.index_in_deck + 1}")
                try:
                    with self.slide_image(row["id"], layout=False) as image:
                        description, vector = mixed_embeddings.embed_mixed(
                            image, self.MIXED_VLM_MODEL, self.EMBEDDING_MODEL)
                except embeddings.EmbeddingError:
                    # Model/dependency failures affect every slide; retry next pass.
                    raise
                except Exception as exc:
                    log.warning("Image rendering failed for slide %s: %s", row["id"], exc)
                    continue
                with self._conn_lock:
                    if stop_event is not None and stop_event.is_set():
                        break
                    if self._image_source_key(row["id"], strip=True) != key:
                        continue
                    self.conn.execute(
                        "INSERT INTO slide_mixed_embeddings "
                        "(slide_id, model, source_key, description, vector, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(slide_id) DO UPDATE SET model=excluded.model, "
                        "source_key=excluded.source_key, description=excluded.description, "
                        "vector=excluded.vector, updated_at=excluded.updated_at",
                        (row["id"], model, key, description,
                         embeddings.pack_vector(vector), time.time()),
                    )
                    self.conn.commit()
                count += 1
        return count

    def _similar_cached(self, slide_id: int, image: bool = False, strip: bool = False,
                        mixed: bool = False, limit: Optional[int] = None) -> List[int]:
        if mixed:
            # Keyed on the strip-layout rendering that the VLM described.
            table, model, image, strip = "slide_mixed_embeddings", self._mixed_model_key(), True, True
        else:
            image = image or strip
            if strip:
                table = "slide_strip_image_embeddings"
            else:
                table = "slide_image_embeddings" if image else "slide_embeddings"
            model = self.IMAGE_EMBEDDING_MODEL if image else self.EMBEDDING_MODEL
        # Bulk keys: one query and one stat per deck instead of per candidate slide.
        keys = self._image_source_keys(strip) if image else None
        with self._conn_lock:
            row = self.conn.execute(
                f"SELECT * FROM {table} WHERE slide_id = ? AND model = ?",
                (slide_id, model),
            ).fetchone()
            if row is None or (image and row["source_key"] != keys.get(slide_id)):
                return []
            target = embeddings.unpack_vector(row["vector"])
            rows = self.conn.execute(
                f"SELECT * FROM {table} WHERE model = ? AND slide_id != ? ORDER BY slide_id",
                (model, slide_id),
            ).fetchall()
        if image:
            rows = [r for r in rows if r["source_key"] == keys.get(r["slide_id"])]
        order = embeddings.rank_by_cosine(target, [r["vector"] for r in rows], limit)
        return [rows[i]["slide_id"] for i in order]

    def similar_slides(self, slide_id: int, top_k: int = 16) -> List[Slide]:
        """Mixed (VLM description) matches first, then strip-layout image matches,
        then image matches, then text matches.

        After the mixed matches, reserve half the slots for visual matches
        (strip-layout before regular). Fill unused slots from any cache without
        comparing scores from different embedding spaces.
        """
        if top_k <= 0:
            return []
        # The merge below never uses more than top_k entries of any list.
        mixed = self._similar_cached(slide_id, mixed=True, limit=top_k)
        stripped = self._similar_cached(slide_id, strip=True, limit=top_k)
        visual = self._similar_cached(slide_id, image=True, limit=top_k)
        textual = self._similar_cached(slide_id, limit=top_k)
        primary = list(dict.fromkeys(stripped + visual))
        ids = list(dict.fromkeys(
            mixed + primary[:(top_k + 1) // 2] + stripped + textual + visual))[:top_k]
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
        order = embeddings.rank_by_cosine(query_vector, [row["vector"] for row in rows], top_k)
        slides = [self.slide(rows[i]["slide_id"]) for i in order]
        return [s for s in slides if s is not None]

    # -- rendering --------------------------------------------------------
    def slide_image(self, slide_id: int, layout: bool = True) -> Image.Image:
        """Render the given slide directly from its .pptx to a PIL Image.

        With `layout=False`, the grayscale strip-layout rendering is used.
        """
        slide = self.slide(slide_id)
        if slide is None:
            raise KeyError(f"No such slide: {slide_id}")
        deck = self.deck(slide.deck_id)
        if deck is None:
            raise KeyError(f"No such deck: {slide.deck_id}")
        return images.render_slide_image(
            self.folder / deck.pptx_path, slide.index_in_deck, self.cache_dir, strip=not layout
        )

    def slide_image_path(self, slide_id: int, layout: bool = True) -> Optional[Path]:
        """Return the cached PNG of a slide without rendering it.

        If the slide is not rendered yet, its deck is queued for background rendering
        and None is returned. Raises KeyError or FileNotFoundError for unknown slides
        and missing decks, and images.RenderError if the deck cannot be rendered.
        """
        slide = self.slide(slide_id)
        if slide is None:
            raise KeyError(f"No such slide: {slide_id}")
        deck = self.deck(slide.deck_id)
        if deck is None:
            raise KeyError(f"No such deck: {slide.deck_id}")
        pptx_path = self.folder / deck.pptx_path
        if not pptx_path.is_file():
            raise FileNotFoundError(f"No such file: {deck.pptx_path}")
        cached = images.cached_path(pptx_path, slide.index_in_deck, self.cache_dir, strip=not layout)
        if cached is not None:
            return cached
        failure = images.render_failure(pptx_path, strip=not layout)
        if failure is not None:
            raise images.RenderError(f"Rendering {deck.pptx_path} failed: {failure}")
        self._renderer.request(pptx_path, strip=not layout)
        return None

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

    def copy_slide_to_clipboard(self, slide_id: int) -> None:
        """Copy one slide onto the OS clipboard via PowerPoint COM automation (Windows only)."""
        slide = self.slide(slide_id)
        if slide is None:
            raise KeyError(f"No such slide: {slide_id}")
        deck = self.deck(slide.deck_id)
        if deck is None:
            raise KeyError(f"No such deck: {slide.deck_id}")
        convert.copy_slide_to_clipboard(self.folder / deck.pptx_path, slide.index_in_deck)

    def _selection_pairs(self, slide_ids: List[int]) -> tuple:
        """Resolve slide ids to (pptx_path, index_in_deck) pairs, plus a template deck path."""
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
        return pairs, template_path

    def copy_selection_to_clipboard(self, slide_ids: List[int]) -> None:
        """Copy multiple slides (by id, in order) onto the OS clipboard as one
        multi-slide paste, via a temporary .pptx built the same way as export.
        """
        if not slide_ids:
            raise ValueError("No slides selected to copy")
        pairs, template_path = self._selection_pairs(slide_ids)
        with tempfile.TemporaryDirectory(prefix="slidedesk-clipboard-") as staging:
            source = Path(staging) / "source.pptx"
            repaired = Path(staging) / "repaired.pptx"
            pptx_tools.save_selection_as_pptx(pairs, template_path, source)
            convert.repair_pptx(source, repaired)
            if os.path.exists(repaired):
                convert.copy_slides_to_clipboard(repaired)
            else:
                convert.copy_slides_to_clipboard(source)

    # -- export -----------------------------------------------------------
    def export_selection(self, slide_ids: List[int], out_filename: Optional[str] = None) -> Path:
        """Copy the given slides (by id, in the given order) into a new .pptx
        saved in a temporary folder, returning its path. Exports remain available
        until the project is closed. By default, filenames are numbered starting
        at export_1.pptx for each project session.
        """
        if not slide_ids:
            raise ValueError("No slides selected for export")

        pairs, template_path = self._selection_pairs(slide_ids)

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
                if os.path.exists(repaired):
                    repaired.replace(out_path)
                else:
                    source.replace(out_path)
        return out_path
