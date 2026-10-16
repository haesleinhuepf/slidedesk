import sys
from types import SimpleNamespace

import pytest
from PIL import Image
from pptx import Presentation

from slidedesk import images


@pytest.fixture
def deck(tmp_path):
    path = tmp_path / "deck.pptx"
    prs = Presentation()
    for _ in range(2):
        prs.slides.add_slide(prs.slide_layouts[6])
    prs.slides[1]._element.set("show", "0")
    prs.save(path)
    images.clear_cache()
    yield path
    images.clear_cache()


def fake_render(monkeypatch, calls):
    def render_slides(prs):
        calls.append([s._element.get("show") for s in prs.slides])
        return [Image.new("RGB", (4, 4), "red") for _ in prs.slides]

    monkeypatch.setitem(sys.modules, "render_pptx", SimpleNamespace(render_slides=render_slides))


def test_render_slide_image_renders_hidden_slides_and_caches_pngs(deck, monkeypatch):
    calls = []
    fake_render(monkeypatch, calls)

    first = images.render_slide_image(deck, 0, deck.parent / "cache")
    second = images.render_slide_image(deck, 1, deck.parent / "cache")

    assert first.size == second.size == (4, 4)
    assert first is not second
    assert calls == [[None, None]]
    assert len(list((deck.parent / "cache").glob("*.png"))) == 2
    assert not list(deck.parent.glob("*.pdf"))

    images.clear_cache()
    again = images.render_slide_image(deck, 0, deck.parent / "cache")
    assert again.size == (4, 4)
    assert calls == [[None, None]]


def test_render_slide_image_strip_is_grayscale(deck, monkeypatch):
    fake_render(monkeypatch, [])
    pixel = images.render_slide_image(deck, 0, deck.parent / "cache", strip=True).getpixel((0, 0))
    assert pixel[0] == pixel[1] == pixel[2]


def test_render_slide_image_rejects_missing_slide(deck, monkeypatch):
    fake_render(monkeypatch, [])
    with pytest.raises(ValueError):
        images.render_slide_image(deck, 5, deck.parent / "cache")
