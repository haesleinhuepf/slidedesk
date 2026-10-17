import pytest

from slidedesk import SlideProject
from slidedesk import embeddings


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

    monkeypatch.setattr(embeddings, "embed_local", embed)
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


def test_rank_by_cosine_matches_pairwise_similarity():
    target = [1.0, 0.5, 0.0]
    vectors = [[0.0, 1.0, 0.0], [2.0, 1.0, 0.0], [1.0, 0.5, 0.0], [-1.0, 0.0, 0.0],
               [0.0, 0.0, 0.0], [1.0, 0.0, 1.0]]
    blobs = [embeddings.pack_vector(v) for v in vectors]
    expected = sorted(range(len(vectors)),
                      key=lambda i: -embeddings.cosine_similarity(target, vectors[i]))
    # Equal vectors tie; the earlier input keeps precedence.
    assert embeddings.rank_by_cosine(target, blobs) == expected == [1, 2, 5, 0, 4, 3]
    assert embeddings.rank_by_cosine(target, blobs, limit=2) == [1, 2]


def test_rank_by_cosine_skips_mismatched_and_empty_vectors():
    blobs = [embeddings.pack_vector([1.0, 0.0]), embeddings.pack_vector([1.0, 0.0, 0.0])]
    assert embeddings.rank_by_cosine([1.0, 0.0, 0.0], blobs) == [1]
    assert embeddings.rank_by_cosine([1.0, 0.0, 0.0], []) == []
    assert embeddings.rank_by_cosine([], blobs) == []
