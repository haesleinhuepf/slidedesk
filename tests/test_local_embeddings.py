from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from slidedeck import SlideProject, embeddings
from slidedeck.cli import main


def test_local_endpoint_without_kiara_key(monkeypatch):
    monkeypatch.delenv("KIARA_API_KEY", raising=False)
    monkeypatch.setenv("KIARA_BASE_URL", "https://unused.invalid")
    client = Mock()
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(embedding=[1.0, 0.0])]
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr("openai.OpenAI", factory)
    assert embeddings.is_available(local=True)
    assert embeddings.embed_local("hello") == [1.0, 0.0]
    factory.assert_called_with(base_url="http://localhost:11434/v1/", api_key="ollama")
    client.embeddings.create.assert_called_once_with(
        model="jeffh/intfloat-multilingual-e5-large-instruct:f32", input="hello"
    )
    assert not embeddings.is_available()


def test_provider_switch_recomputes_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "is_available", lambda local=False: True)
    remote = Mock(return_value=[1.0, 0.0])
    local = Mock(return_value=[0.0, 1.0])
    monkeypatch.setattr(embeddings, "embed_kiara", remote)
    monkeypatch.setattr(embeddings, "embed_local", local)
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
        remote.assert_called_once_with("hello", embedding_model=embeddings.DEFAULT_MODEL)
    finally:
        project.close()

    project = SlideProject(tmp_path, local=True)
    try:
        assert project.embedding_status()["embedded"] == 0
        assert project.search_semantic("query") == []
        assert project.similar_slides(slide_id) == []
        assert project.embed_pending() == 1
        assert project.embed_pending() == 0
        assert project.embedding_status()["embedded"] == 1
        assert [s.id for s in project.search_semantic("query")] == [slide_id]
        assert all(c.kwargs["embedding_model"] == embeddings.LOCAL_MODEL for c in local.call_args_list)
        assert remote.call_count == 1
    finally:
        project.close()


@pytest.mark.parametrize("args, local", [([], False), (["--local"], True)])
def test_cli_local_option(tmp_path, monkeypatch, args, local):
    factory = Mock()
    app = Mock()
    monkeypatch.setattr("slidedeck.cli.SlideProject", factory)
    monkeypatch.setattr("slidedeck.server.app.create_app", Mock(return_value=app))
    result = CliRunner().invoke(main, ["serve", str(tmp_path), "--no-browser", *args])
    assert result.exit_code == 0, result.output
    factory.assert_called_once_with(str(tmp_path), local=local)
    factory.return_value.close.assert_called_once()
