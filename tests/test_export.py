from zipfile import ZipFile

from pptx import Presentation

from slidedeck import SlideProject
from slidedeck.pptx_tools import extract_text, is_slide_hidden, set_slide_hidden
from slidedeck.scanner import _index_slides
from slidedeck.server.app import create_app


def test_export_selected_slide_ids_in_order(tmp_path):
    project = SlideProject(tmp_path)
    try:
        decks = []
        for name in ("first", "second"):
            path = tmp_path / f"{name}.pptx"
            prs = Presentation()
            for index in range(4):
                slide = prs.slides.add_slide(prs.slide_layouts[0])
                slide.shapes.title.text = f"{name} slide {index + 1}"
                set_slide_hidden(slide, index == 1)
            prs.save(path)
            deck_id = project.conn.execute(
                "INSERT INTO decks (pptx_path, pptx_mtime) VALUES (?, ?)",
                (path.name, path.stat().st_mtime),
            ).lastrowid
            _index_slides(project.conn, deck_id, path)
            decks.append(project.slides(deck_id))
        project.conn.commit()
        # Nonconsecutive positions, a hidden slide, both decks, and a repeat.
        selected = [decks[0][3], decks[1][1], decks[0][0], decks[1][2], decks[0][3]]
        client = create_app(project).test_client()
        response = client.post("/api/export", json={
            "slide_ids": [slide.id for slide in selected], "filename": "chosen.pptx"
        })
        assert response.status_code == 200
        output = tmp_path / response.json["path"]
        with ZipFile(output) as archive:
            names = archive.namelist()
            assert len(names) == len(set(names)), "Duplicate PPTX package parts"
        exported = Presentation(output)
        assert [extract_text(slide) for slide in exported.slides] == [
            slide.text for slide in selected
        ]
        assert all(not is_slide_hidden(slide) for slide in exported.slides)
    finally:
        project.close()
