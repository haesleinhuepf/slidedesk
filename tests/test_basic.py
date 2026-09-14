"""Basic smoke tests for the slidedesk DB layer and public API (no PowerPoint
files or LibreOffice/Poppler required for these).
"""
from pathlib import Path

from slidedesk import SlideProject


def test_project_creates_db(tmp_path):
    project = SlideProject(tmp_path)
    assert project.db_path == tmp_path / ".slidedesk" / "slidedesk.db"
    assert project.db_path.exists()
    assert project.decks() == []
    project.close()


def test_project_moves_legacy_db(tmp_path):
    project = SlideProject(tmp_path)
    project.conn.execute("INSERT INTO meta VALUES ('test', 'saved')")
    project.conn.commit()
    project.close()
    project.db_path.rename(tmp_path / "slidedesk.db")

    project = SlideProject(tmp_path)
    assert not (tmp_path / "slidedesk.db").exists()
    assert project.conn.execute("SELECT value FROM meta WHERE key = 'test'").fetchone()[0] == "saved"
    project.close()


def test_scan_empty_folder(tmp_path):
    project = SlideProject(tmp_path)
    project.scan()
    assert project.decks() == []
    project.close()


def test_search_no_results(tmp_path):
    project = SlideProject(tmp_path)
    assert project.search("nothing") == []
    project.close()
