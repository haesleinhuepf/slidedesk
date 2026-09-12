# SlideDesk

SlideDesk indexes a folder full of `.pptx` files (and sub-folders), 
allowing you to browse everything in a touch-friendly browser GUI.
It also has a similarity search, allowing you to rediscover slides 
and build slide deck efficiently.

## Install

```bash
pip install slidedesk
```

After updating from slidedeck, run this command again to install the `slidedesk`
command. Python imports now use `slidedesk`. Existing projects keep using
`slidedesk.db` and `.slidedesk_cache` so saved data remains available.

Requires Microsoft PowerPoint installed and only works on Windows.

Requires [Poppler](https://poppler.freedesktop.org/) (`pdftoppm`/`pdfinfo` on PATH,
used by `pdf2image` to render slide images from PDFs) and, on Windows, a local
install of Microsoft PowerPoint (used via COM automation/`pywin32` to export
`.pptx` to `.pdf`):

```
conda install poppler
```

## Usage

```bash
slidedesk /path/to/folder
```

This creates (or opens) a `slidedesk.db` SQLite project file inside the folder,
starts scanning it for `.pptx` files in the background, and opens a browser GUI at
`http://127.0.0.1:5000`. You can also make it use a different port using the 
`--port 8989` option.

To browse decks already stored in `slidedesk.db` without scanning folders for
changes or new decks:

```bash
slidedesk /path/to/folder --no-scan
```

This disables startup and recurring folder scans, including the scan API.
The background embedding worker is also stopped in this mode. You can still
explicitly refresh an individual indexed deck from the GUI.

## How it works under the hood 

SlideDesk downloads models from Hugging Face on first use, to run them locally
Hence, it does not need an API key or an OpenAI-compatible server.

Slide texts are embedded using the [intfloat/multilingual-e5-large-instruct](https://huggingface.co/intfloat/multilingual-e5-large-instruct) model. Thanks to this, you can search for terms 
such as "image filtering" and it may find slides about "image processsing", too.

Slide images are embedded locally with [openai/clip-vit-base-patch32](https://huggingface.co/openai/clip-vit-base-patch32). 

The background worker stores vectors in `slidedesk.db`. 
This includes hidden slides and slides without text once their PDF exports are
available. Changed slides or PDF exports are embedded again automatically.

“Show similar slides” presents one list: visual matches first, followed by
additional text matches, excluding duplicates. Either cache
can supply results while the other is still being built or unavailable.

Exports are saved in a temporary directory and served for download. 
They are later removed automatically.

## GUI features

- Grid of slide decks (one row per `.pptx`/`.pdf`/`.hidden.pdf` tuple), first slide
  shown as a thumbnail.
- Click a slide to reveal the rest of the deck's slides to the right.
- Search box: press Enter to find all slides containing the given text.
- Right-click or hold a slide for actions: zoom to slide, add to selection, show in deck, or show similar.
- "Export selection" button saves selected slides into a new `.pptx`.
- Toggle to show/hide hidden slides.
- Touch support: two-finger pinch to zoom, drag background to pan.
- Arrow keys or WASD pan the view; hold Shift to pan four times faster.
