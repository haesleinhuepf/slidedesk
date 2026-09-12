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

Embeddings use KIARA by default (requires `KIARA_API_KEY`). To use local Ollama
embeddings, start Ollama and download the model, then pass `--local`:

```bash
ollama pull jeffh/intfloat-multilingual-e5-large-instruct:f32
slidedeck serve /path/to/folder --local
```

Local mode uses `http://localhost:11434/v1/` and does not need a KIARA API key.
In Python, use `SlideProject("/path/to/folder", local=True)`.
Switching providers recomputes cached slide embeddings for the selected model.

Slide images are also embedded locally with `openai/clip-vit-base-patch32`.
The background worker downloads CLIP on first use (internet access required),
then reuses the downloaded model and stores vectors in `slidedeck.db`.
This includes hidden slides and slides without text once their PDF exports are
available. Changed slides or PDF exports are embedded again automatically.
Upgrade an existing installation with `pip install -e .` to install PyTorch and
Transformers. Image embeddings require no KIARA key or Ollama server.

“Show similar slides” presents one list: visual matches first, followed by
additional text matches, without duplicates or separate labels. Either cache
can supply results while the other is still being built or unavailable.
In Python, `project.embed_images_pending()` processes a batch synchronously;
`project.scan_in_background()` maintains both caches automatically.

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
export_path = project.export_selection([s.id for s in results])
```

Exports are saved in a temporary directory outside the source deck folder and
served from there for download. They are removed when `project.close()` is called.
Downloads are automatically named `export_1.pptx`, `export_2.pptx`, and so on
within each project session, without a filename prompt.

## GUI features

- Grid of slide decks (one row per `.pptx`/`.pdf`/`.hidden.pdf` tuple), first slide
  shown as a thumbnail.
- Click a slide to reveal the rest of the deck's slides to the right.
- Search box: press Enter to find all slides containing the given text.
- Right-click or hold a slide for actions: zoom to slide, add to selection, show in deck, or show similar.
- "Export selection" button saves selected slides into a new `.pptx`.
- Toggle to show/hide hidden slides.
- Touch support: two-finger pinch to zoom, drag background to pan.
