import json

import pytest

from slidedesk import SlideProject
from slidedesk.server.app import create_app


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


def test_advanced_search_generation_endpoint(indexed_project, monkeypatch):
    from slidedesk import text_generation

    generate = monkeypatch.setattr(
        text_generation, "generate_list", lambda prompt: f"- {prompt}"
    )
    client = create_app(indexed_project).test_client()

    response = client.post(
        "/api/search/advanced/generate-list", json={"prompt": "List slides about AI"}
    )

    assert response.status_code == 200
    assert response.get_json() == {"list": "- List slides about AI"}
    assert client.post("/api/search/advanced/generate-list", json={}).status_code == 400


def test_advanced_search_generation_stream_endpoint(indexed_project, monkeypatch):
    from slidedesk import text_generation

    def generate(prompt, cancel_event):
        assert prompt == "List slides about AI"
        assert not cancel_event.is_set()
        yield "- Intro"
        yield "\n- Methods"

    monkeypatch.setattr(text_generation, "generate_list_stream", generate)
    client = create_app(indexed_project).test_client()

    response = client.post(
        "/api/search/advanced/generate-list/stream",
        json={"prompt": "List slides about AI", "generation_id": "test-generation"},
        buffered=False,
    )
    first_event = json.loads(next(response.response))
    cancel_response = client.post(
        "/api/search/advanced/generate-list/cancel", json={"generation_id": "test-generation"}
    )
    remaining_events = [json.loads(line) for line in response.response]
    assert response.status_code == 200
    assert first_event == {"text": "- Intro"}
    assert remaining_events == [{"cancelled": True, "done": True}]
    assert cancel_response.get_json() == {"ok": True, "cancelled": True}
    assert client.post("/api/search/advanced/generate-list/stream", json={"prompt": "test"}).status_code == 400


def test_advanced_semantic_search_preserves_rank_and_validates_limit(indexed_project, monkeypatch):
    project = indexed_project
    slides = project.search("match")
    (project.folder / "missing.pptx").touch()
    (project.folder / "directory.pptx").rmdir()
    (project.folder / "directory.pptx").touch()
    requested = []

    def search(query, top_k):
        requested.append((query, top_k))
        return list(reversed(slides))[:top_k]

    monkeypatch.setattr(project, "search_semantic", search)
    client = create_app(project).test_client()

    response = client.post(
        "/api/search/advanced/semantic", json={"query": "intro", "top_k": 2}
    )

    assert response.status_code == 200
    assert [slide["id"] for slide in response.get_json()] == [s.id for s in reversed(slides)][:2]
    assert requested == [("intro", 2)]
    assert client.post(
        "/api/search/advanced/semantic", json={"query": "intro", "top_k": True}
    ).status_code == 400
