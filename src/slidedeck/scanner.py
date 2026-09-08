"""Folder scanning: find .pptx files, make sure .pdf / .hidden.pdf exports
exist and are up to date, and (re)index slide text/hidden flags into the DB.
"""
from __future__ import annotations

import logging
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from pptx import Presentation

from . import pptx_tools
from .convert import ConversionError, convert_pptx_to_pdf
from . import convert

if TYPE_CHECKING:
    from .project import SlideProject

log = logging.getLogger(__name__)

IGNORED_PREFIXES = ("~$",)
CACHE_DIRNAME = ".slidedeck_cache"


def _iter_pptx_files(root: Path):
    for path in root.rglob("*.pptx"):
        if path.name.startswith(IGNORED_PREFIXES):
            continue
        if CACHE_DIRNAME in path.parts:
            continue
        yield path


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _index_slides(conn, deck_id: int, pptx_path: Path) -> None:
    prs = Presentation(str(pptx_path))
    conn.execute("DELETE FROM slides WHERE deck_id = ?", (deck_id,))
    visible_counter = 0
    rows = []
    for idx, slide in enumerate(prs.slides):
        hidden = pptx_tools.is_slide_hidden(slide)
        text = pptx_tools.extract_text(slide)
        visible_page: Optional[int]
        if hidden:
            visible_page = None
        else:
            visible_counter += 1
            visible_page = visible_counter
        rows.append((deck_id, idx, visible_page, int(hidden), text))
    conn.executemany(
        "INSERT INTO slides (deck_id, index_in_deck, visible_pdf_page, hidden, text) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def scan_once(
    project: "SlideProject", deck_id: Optional[int] = None, force: bool = False
) -> None:
    """Scan the project, optionally forcing a single deck to be reindexed."""
    conn = project.conn
    root = project.folder
    seen_paths = set()

    target_path = None
    if deck_id is not None:
        target = conn.execute(
            "SELECT pptx_path FROM decks WHERE id = ?", (deck_id,)
        ).fetchone()
        if target is None:
            return
        target_path = target["pptx_path"]

    for pptx_path in _iter_pptx_files(root):
        rel_pptx = _rel(root, pptx_path)
        if target_path is not None and rel_pptx != target_path:
            continue
        seen_paths.add(rel_pptx)
        project._status["current_file"] = rel_pptx
        try:
            mtime = pptx_path.stat().st_mtime
        except FileNotFoundError:
            continue

        row = conn.execute(
            "SELECT * FROM decks WHERE pptx_path = ?", (rel_pptx,)
        ).fetchone()

        pdf_rel = str(Path(rel_pptx).with_suffix(".pdf"))
        hidden_pdf_rel = rel_pptx[: -len(".pptx")] + ".hidden.pdf"
        pdf_path = root / pdf_rel
        hidden_pdf_path = root / hidden_pdf_rel

        needs_reindex = force or row is None or row["pptx_mtime"] != mtime

        if not pdf_path.exists() or needs_reindex:
            try:
                convert_pptx_to_pdf(pptx_path, pdf_path)
            except ConversionError as exc:
                log.warning("Skipping PDF export for %s: %s", pptx_path, exc)

        if not hidden_pdf_path.exists() or needs_reindex:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_pptx = Path(tmp) / pptx_path.name
                    pptx_tools.make_all_visible_copy(pptx_path, tmp_pptx)
                    convert_pptx_to_pdf(tmp_pptx, hidden_pdf_path)
            except ConversionError as exc:
                log.warning("Skipping hidden-PDF export for %s: %s", pptx_path, exc)

        pdf_mtime = pdf_path.stat().st_mtime if pdf_path.exists() else None
        hidden_pdf_mtime = (
            hidden_pdf_path.stat().st_mtime if hidden_pdf_path.exists() else None
        )
        now = time.time()

        if row is None:
            cur = conn.execute(
                "INSERT INTO decks (pptx_path, pptx_mtime, pdf_path, pdf_mtime, "
                "hidden_pdf_path, hidden_pdf_mtime, last_scanned) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rel_pptx, mtime, pdf_rel, pdf_mtime, hidden_pdf_rel, hidden_pdf_mtime, now),
            )
            deck_id = cur.lastrowid
        else:
            deck_id = row["id"]
            conn.execute(
                "UPDATE decks SET pptx_mtime=?, pdf_path=?, pdf_mtime=?, "
                "hidden_pdf_path=?, hidden_pdf_mtime=?, last_scanned=? WHERE id=?",
                (mtime, pdf_rel, pdf_mtime, hidden_pdf_rel, hidden_pdf_mtime, now, deck_id),
            )

        if needs_reindex:
            _index_slides(conn, deck_id, pptx_path)
        conn.commit()

    if deck_id is not None:
        if target_path not in seen_paths:
            # The deck's source .pptx is gone; drop the stale record.
            conn.execute("DELETE FROM decks WHERE id = ?", (deck_id,))
            conn.commit()
        project._status["current_file"] = None
        return

    # Drop decks whose source .pptx has disappeared.
    existing = conn.execute("SELECT id, pptx_path FROM decks").fetchall()
    for row in existing:
        if row["pptx_path"] not in seen_paths:
            conn.execute("DELETE FROM decks WHERE id = ?", (row["id"],))
    conn.commit()
    project._status["current_file"] = None


class BackgroundScanner:
    """Runs `scan_once` repeatedly on a daemon thread until stopped."""

    def __init__(self, project: "SlideProject", interval: float = 5.0):
        self.project = project
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        status = self.project._status
        try:
            while not self._stop_event.is_set():
                status["running"] = True
                status["last_run_started"] = time.time()
                status["error"] = None
                try:
                    with self.project._conn_lock:
                        scan_once(self.project)
                    self.project.embed_pending()
                except Exception as exc:  # keep the loop alive across transient errors
                    log.exception("Scan failed")
                    status["error"] = str(exc)
                status["last_run_finished"] = time.time()
                status["running"] = False
                self._stop_event.wait(self.interval)
        finally:
            convert.shutdown()
