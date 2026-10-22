import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from slidedesk import SlideProject, embeddings, mixed_embeddings


@pytest.fixture
def project(tmp_path, monkeypatch):
    project = SlideProject(tmp_path)
    (tmp_path / "deck.pptx").touch()
    deck = project.conn.execute(
        "INSERT INTO decks (pptx_path, pptx_mtime) VALUES ('deck.pptx', 0)"
    ).lastrowid
    for i in range(4):
        project.conn.execute(
            "INSERT INTO slides (deck_id, index_in_deck, text) VALUES (?, ?, 'text')", (deck, i)
        )
    project.conn.commit()
    layouts = []

    def slide_image(sid, layout=True):
        layouts.append(layout)
        return Image.new("RGB", (32, 32))
    project.layouts = layouts
    monkeypatch.setattr(project, "slide_image", slide_image)
    monkeypatch.setattr(mixed_embeddings, "embed_mixed",
                        Mock(return_value=("A slide about cells.", [1.0, 0.0])))
    try:
        yield project
    finally:
        project.close()


def test_mixed_cache_persists_and_invalidates(project):
    assert project.embed_mixed_pending(limit=3) == 3
    assert project.embed_mixed_pending() == 1
    assert project.embed_mixed_pending() == 0
    # The VLM sees the strip-layout rendering only.
    assert project.layouts == [False] * 4
    row = project.conn.execute(
        "SELECT model, description FROM slide_mixed_embeddings WHERE slide_id = 1"
    ).fetchone()
    assert row["model"] == f"{mixed_embeddings.VLM_MODEL}|{project.EMBEDDING_MODEL}"
    assert row["description"] == "A slide about cells."
    assert project.embedding_status()["mixed"]["embedded"] == 4
    (project.folder / "deck.pptx").write_bytes(b"changed deck")
    assert project.embedding_status()["mixed"]["embedded"] == 0
    assert project.embed_mixed_pending() == 4
    project.EMBEDDING_MODEL = "other-text-model"
    assert project.embed_mixed_pending() == 4
    project.conn.execute("DELETE FROM slides")
    project.conn.commit()
    assert project.conn.execute("SELECT COUNT(*) FROM slide_mixed_embeddings").fetchone()[0] == 0


def test_model_failure_is_raised_for_retry(project):
    mixed_embeddings.embed_mixed.side_effect = embeddings.EmbeddingError("gated model")
    with pytest.raises(embeddings.EmbeddingError):
        project.embed_mixed_pending()
    assert project.conn.execute("SELECT COUNT(*) FROM slide_mixed_embeddings").fetchone()[0] == 0


def seed(project, mixed_vectors):
    for sid, vector in enumerate(mixed_vectors, 1):
        project.conn.execute(
            "INSERT INTO slide_mixed_embeddings VALUES (?, ?, ?, '', ?, 0)",
            (sid, project._mixed_model_key(), project._image_source_key(sid, strip=True),
             embeddings.pack_vector(vector)),
        )
    for sid, vector in enumerate(([1, 0], [0, 1], [-1, 0], [0.9, 0.1]), 1):
        project.conn.execute(
            "INSERT INTO slide_embeddings VALUES (?, ?, ?, 0)",
            (sid, project.EMBEDDING_MODEL, embeddings.pack_vector(vector)),
        )
    project.conn.commit()


def test_mixed_matches_come_first(project):
    # Slide 2 is a "No content" slide (empty vector) and must not be matched.
    seed(project, ([1, 0], [], [0, 1], [1, 0]))
    assert [s.id for s in project.similar_slides(1, top_k=3)] == [4, 3, 2]
    # Without a mixed vector for the query slide, other embeddings are used.
    assert [s.id for s in project.similar_slides(2, top_k=3)] == [4, 1, 3]


def test_embed_mixed_skips_no_content(monkeypatch):
    embed_local = Mock(return_value=[0.5, 0.5])
    monkeypatch.setattr(embeddings, "embed_local", embed_local)
    monkeypatch.setattr(mixed_embeddings, "describe_image", Mock(return_value='"No content."'))
    with Image.new("RGB", (8, 8)) as image:
        assert mixed_embeddings.embed_mixed(image) == ('"No content."', [])
        embed_local.assert_not_called()
        mixed_embeddings.describe_image.return_value = "Cell division."
        assert mixed_embeddings.embed_mixed(image, embedding_model="m") == ("Cell division.", [0.5, 0.5])
    embed_local.assert_called_once_with("Cell division.", embedding_model="m")


def test_vlm_loads_once_and_decodes_new_tokens(monkeypatch):
    class Inputs(dict):
        def to(self, *args, **kwargs):
            return self
    output = Mock()
    output.__getitem__ = Mock(return_value="new tokens")
    vlm = Mock(dtype="float32")
    vlm.generate.return_value = output
    processor = Mock()
    processor.apply_chat_template.return_value = Inputs(
        input_ids=SimpleNamespace(shape=(1, 7)), pixel_values="pixels")
    processor.decode.return_value = "  An abstract.\n"
    model_class = Mock()
    model_class.from_pretrained.return_value = vlm
    processor_class = Mock()
    processor_class.from_pretrained.return_value = processor
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        inference_mode=nullcontext, bfloat16="bfloat16", float32="float32"))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoModelForImageTextToText=model_class, AutoProcessor=processor_class))
    monkeypatch.setattr(mixed_embeddings, "_models", {})
    with Image.new("RGBA", (16, 16)) as image:
        assert mixed_embeddings.describe_image(image) == "An abstract."
        assert mixed_embeddings.describe_image(image) == "An abstract."
    model_class.from_pretrained.assert_called_once_with(
        mixed_embeddings.VLM_MODEL, dtype="float32", low_cpu_mem_usage=False)
    vlm.eval.assert_called_once()
    content = processor.apply_chat_template.call_args.args[0][0]["content"]
    assert content[0]["image"].mode == "RGB"
    assert content[1]["text"] == mixed_embeddings.PROMPT
    output.__getitem__.assert_called_with((0, slice(7, None)))
    processor.decode.assert_called_with("new tokens", skip_special_tokens=True)
