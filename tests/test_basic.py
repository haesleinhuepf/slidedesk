"""Basic smoke tests for the slidedeck DB layer and public API (no PowerPoint
files or LibreOffice/Poppler required for these).
"""
from pathlib import Path

from slidedeck import SlideProject


def test_project_creates_db(tmp_path):
    project = SlideProject(tmp_path)
    assert project.db_path.exists()
    assert project.decks() == []
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
