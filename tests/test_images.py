from pathlib import Path

from PIL import Image

from slidedesk import images


def test_read_pdf_bytes_caches_recent_file_version(tmp_path, monkeypatch):
    pdf_path = tmp_path / "deck.pdf"
    pdf_path.write_bytes(b"first PDF")
    reads = []
    original_read_bytes = Path.read_bytes

    def track_read_bytes(path):
        reads.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", track_read_bytes)

    assert images.read_pdf_bytes(pdf_path) == b"first PDF"
    assert images.read_pdf_bytes(pdf_path) == b"first PDF"
    assert reads == [pdf_path]

    pdf_path.write_bytes(b"updated PDF")
    assert images.read_pdf_bytes(pdf_path) == b"updated PDF"
    assert reads == [pdf_path, pdf_path]
    assert images._read_pdf_bytes_cached.cache_info().maxsize == 20


def test_render_page_converts_cached_pdf_bytes(tmp_path, monkeypatch):
    pdf_path = tmp_path / "deck.pdf"
    pdf_path.write_bytes(b"PDF content")
    converted = []
    rendered_image = Image.new("RGB", (2, 3))

    def convert_from_bytes(pdf_bytes, **kwargs):
        converted.append((pdf_bytes, kwargs))
        return [rendered_image]

    monkeypatch.setattr(images, "convert_from_bytes", convert_from_bytes)

    result = images.render_page(pdf_path, 2, tmp_path / "cache")

    assert result is rendered_image
    assert converted == [
        (b"PDF content", {"first_page": 2, "last_page": 2})
    ]
    assert list((tmp_path / "cache").glob("*.png"))
