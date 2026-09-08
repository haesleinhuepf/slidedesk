import pytest

from slidedeck import SlideProject
from slidedeck.server.app import create_app


@pytest.fixture
def indexed_project(tmp_path):
    project = SlideProject(tmp_path)
    for name in ("available.pptx", "missing.pptx", "directory.pptx"):
        deck_id = project.conn.execute(
            "INSERT INTO decks (pptx_path, pptx_mtime) VALUES (?, 0)", (name,)
        ).lastrowid
        project.conn.execute(
            "INSERT INTO slides (deck_id, index_in_deck, text) VALUES (?, 0, 'match')",
            (deck_id,),
        )
    project.conn.commit()
    (tmp_path / "available.pptx").touch()
    (tmp_path / "directory.pptx").mkdir()
    try:
        yield project
    finally:
        project.close()


@pytest.mark.parametrize("url", ["/api/decks", "/api/search?q=match", "/api/search/semantic?q=match"])
def test_deck_lists_exclude_missing_files(indexed_project, monkeypatch, url):
    project = indexed_project
    monkeypatch.setattr(project, "search_semantic", lambda query: project.search(query))
    client = create_app(project).test_client()

    response = client.get(url)
    assert response.status_code == 200
    decks = response.get_json()
    assert [d["name"] for d in decks] == ["available.pptx"]
    assert len(decks[0]["slides"]) == 1
    if "search" in url:
        assert decks[0]["matched_slides"] == decks[0]["slides"]

    (project.folder / "available.pptx").unlink()
    assert client.get(url).get_json() == []
    (project.folder / "available.pptx").touch()
    assert len(client.get(url).get_json()) == 1
    assert len(project.decks()) == 3


def test_slide_lists_exclude_missing_files(indexed_project):
    project = indexed_project
    client = create_app(project).test_client()
    for deck in project.decks():
        response = client.get(f"/api/decks/{deck.id}/slides")
        assert response.status_code == 200
        assert len(response.get_json()) == (1 if deck.name == "available.pptx" else 0)
    assert client.get("/api/decks/999/slides").get_json() == []


def test_similar_slides_exclude_missing_files(indexed_project, monkeypatch):
    project = indexed_project
    slides = project.search("match")
    monkeypatch.setattr(project, "similar_slides", lambda slide_id: slides)
    client = create_app(project).test_client()
    available = next(d for d in project.decks() if d.name == "available.pptx")
    source = project.slides(available.id)[0]

    response = client.get(f"/api/slides/{source.id}/similar")
    assert response.status_code == 200
    assert [s["id"] for s in response.get_json()] == [source.id]
    (project.folder / available.pptx_path).unlink()
    assert client.get(f"/api/slides/{source.id}/similar").status_code == 404
