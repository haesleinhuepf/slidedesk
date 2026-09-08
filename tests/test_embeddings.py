import pytest

from slidedeck import SlideProject
from slidedeck import embeddings


@pytest.mark.parametrize(
    "source, expected",
    [
        ("<b>Hello</b><br>world", "Hello world"),
        ("&lt;b&gt;Hello&lt;/b&gt;&lt;br&gt;world", "Hello world"),
        ("&#60;b&#62;Hello&#60;/b&#62;", "Hello"),
        ("<p>Research &amp; development</p>", "Research & development"),
        ("2 < 3 and 5 > 4", "2 < 3 and 5 > 4"),
    ],
)
def test_embed_pending_strips_html(tmp_path, monkeypatch, source, expected):
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    calls = []

    def embed(text, embedding_model):
        calls.append((text, embedding_model))
        return [1.0, 0.0]

    monkeypatch.setattr(embeddings, "embed_kiara", embed)
    project = SlideProject(tmp_path)
    try:
        deck_id = project.conn.execute(
            "INSERT INTO decks (pptx_path, pptx_mtime) VALUES (?, ?)",
            ("example.pptx", 0),
        ).lastrowid
        project.conn.execute(
            "INSERT INTO slides (deck_id, index_in_deck, text) VALUES (?, ?, ?)",
            (deck_id, 0, source),
        )
        project.conn.commit()

        assert project.embed_pending() == 1
        assert calls == [(expected, project.EMBEDDING_MODEL)]
        assert project.embed_pending() == 0
    finally:
        project.close()
