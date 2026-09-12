from unittest.mock import Mock

from click.testing import CliRunner

from slidedesk import SlideProject
from slidedesk.cli import main


def test_no_scan_serves_existing_database(tmp_path, monkeypatch):
    project = SlideProject(tmp_path)
    project.conn.execute(
        "INSERT INTO decks (pptx_path, pptx_mtime) VALUES ('indexed.pptx', 0)"
    )
    project.conn.commit()
    project.close()
    (tmp_path / "indexed.pptx").touch()
    (tmp_path / "new.pptx").touch()

    scanner = Mock(side_effect=AssertionError("Folder scan must not start"))
    monkeypatch.setattr(SlideProject, "scan_in_background", scanner)

    def run(app, **kwargs):
        client = app.test_client()
        assert client.get("/").status_code == 200
        assert [d["pptx_path"] for d in client.get("/api/decks").get_json()] == [
            "indexed.pptx"
        ]
        assert client.get("/api/scan/status").get_json()["running"] is False
        assert client.post("/api/scan").status_code == 403

    monkeypatch.setattr("flask.Flask.run", run)
    result = CliRunner().invoke(main, [str(tmp_path), "--no-browser", "--no-scan"])
    assert result.exit_code == 0, result.exception
    scanner.assert_not_called()


def test_serve_scans_by_default(tmp_path, monkeypatch):
    scanner = Mock()
    monkeypatch.setattr(SlideProject, "scan_in_background", scanner)
    monkeypatch.setattr("flask.Flask.run", Mock())
    result = CliRunner().invoke(
        main, [str(tmp_path), "--no-browser", "--scan-interval", "12"]
    )
    assert result.exit_code == 0, result.exception
    scanner.assert_called_once_with(interval=12.0)
