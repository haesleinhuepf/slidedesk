# slidedeck

Index a folder full of `.pptx` files, automatically render `.pdf` / `.hidden.pdf`
exports, extract slide text, and browse/search everything in a touch-friendly
browser GUI.

## Install

```bash
pip install -e .
```

Requires [Poppler](https://poppler.freedesktop.org/) (`pdftoppm`/`pdfinfo` on PATH,
used by `pdf2image` to render slide images from PDFs) and, on Windows, a local
install of Microsoft PowerPoint (used via COM automation/`pywin32` to export
`.pptx` to `.pdf`):

```
conda install poppler
```

## Usage

```bash
slidedeck serve /path/to/folder
```

This creates (or opens) a `slidedeck.db` SQLite project file inside the folder,
starts scanning it for `.pptx` files in the background, and opens a browser GUI at
`http://127.0.0.1:5000`.

## Python API

```python
from slidedeck import SlideProject

project = SlideProject("/path/to/folder")
project.scan()  # synchronous scan; use scan_in_background() for async

for deck in project.decks():
    print(deck.pptx_path, len(project.slides(deck.id)))
    for slide in project.slides(deck.id):
        print(slide.index_in_deck, slide.hidden, slide.text)
        image = project.slide_image(slide.id)  # PIL.Image.Image

results = project.search("quarterly results")
project.export_selection([s.id for s in results], "export.pptx")
```

## GUI features

- Grid of slide decks (one row per `.pptx`/`.pdf`/`.hidden.pdf` tuple), first slide
  shown as a thumbnail.
- Click a slide to reveal the rest of the deck's slides to the right.
- Search box: press Enter to find all slides containing the given text.
- Double-click a slide to add/remove it from the export selection.
- "Export selection" button saves selected slides into a new `.pptx`.
- Toggle to show/hide hidden slides.
- Touch support: two-finger pinch to zoom, drag background to pan.
