from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import torch
from click.testing import CliRunner

from slidedesk import SlideProject, embeddings
from slidedesk.cli import main


def test_default_huggingface_model(monkeypatch):
    tokenizer = Mock(return_value={
        "input_ids": torch.tensor([[1, 2]]),
        "attention_mask": torch.tensor([[1, 1]]),
    })
    model = Mock()
    model.return_value = SimpleNamespace(
        last_hidden_state=torch.tensor([[[3.0, 0.0], [0.0, 4.0]]])
    )
    model_factory = Mock(from_pretrained=Mock(return_value=model))
    tokenizer_factory = Mock(from_pretrained=Mock(return_value=tokenizer))
    monkeypatch.setattr("transformers.AutoModel", model_factory)
    monkeypatch.setattr("transformers.AutoTokenizer", tokenizer_factory)
    embeddings._local_model_cache.clear()

    assert embeddings.is_available()
    assert embeddings.embed_local("hello") == pytest.approx([0.6, 0.8])
    tokenizer_factory.from_pretrained.assert_called_once_with(embeddings.DEFAULT_MODEL)
    model_factory.from_pretrained.assert_called_once_with(embeddings.DEFAULT_MODEL)
    tokenizer.assert_called_once_with("hello", return_tensors="pt", truncation=True, max_length=512)


def test_default_embedding_recomputes_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    embed = Mock(return_value=[0.0, 1.0])
    monkeypatch.setattr(embeddings, "embed_local", embed)
    project = SlideProject(tmp_path)
    try:
        deck_id = project.conn.execute(
            "INSERT INTO decks (pptx_path, pptx_mtime) VALUES (?, ?)", ("test.pptx", 0)
        ).lastrowid
        slide_id = project.conn.execute(
            "INSERT INTO slides (deck_id, index_in_deck, text) VALUES (?, ?, ?)",
            (deck_id, 0, "hello"),
        ).lastrowid
        project.conn.commit()
        assert project.embed_pending() == 1
        assert project.embed_pending() == 0
        assert project.embedding_status()["embedded"] == 1
        assert [s.id for s in project.search_semantic("query")] == [slide_id]
        assert embed.call_args_list[0].args == ("hello",)
        assert embed.call_args_list[0].kwargs == {"embedding_model": embeddings.DEFAULT_MODEL}
    finally:
        project.close()


def test_cli_uses_default_embeddings(tmp_path, monkeypatch):
    factory = Mock()
    app = Mock()
    monkeypatch.setattr("slidedesk.cli.SlideProject", factory)
    monkeypatch.setattr("slidedesk.server.app.create_app", Mock(return_value=app))
    result = CliRunner().invoke(main, ["serve", str(tmp_path), "--no-browser"])
    assert result.exit_code == 0, result.output
    factory.assert_called_once_with(str(tmp_path))
    factory.return_value.close.assert_called_once()
