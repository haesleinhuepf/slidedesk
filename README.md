# SlideDesk

SlideDesk indexes a folder full of `.pptx` files (and sub-folders), 
allowing you to browse everything in a touch-friendly browser GUI.
It also has a similarity search, allowing you to rediscover slides 
and build slide decks efficiently.

![]()

## Install

It is recommended to install SlideDesk in a [conda-forge](https://conda-forge.org/download/) environment.

```bash
pip install slidedesk
```

### Additional requirements

* Works on Windows only
* Requires Microsoft PowerPoint installed
* Requires [Poppler](https://poppler.freedesktop.org/). You can install poppler like this

```
conda install poppler
```

## Usage

```bash
slidedesk /path/to/folder
```

**Note:** If you run this for the first time on a folder that contains many slide decks, 
the initial scan may take hours depending on your computer. You will slide decks once they are scanned and a progress bar in the top right corner shows how far scanning and embedding are done.

Alternatively, navigate to the folder and run SlideDesk from there:
```bash
cd /path/to/folder
slidedesk .
```

This creates (or opens) a `.slidedesk/slidedesk.db` SQLite project file inside the folder,
starts scanning it for `.pptx` files in the background, and opens a browser GUI at
`http://127.0.0.1:5000`. You can also make it use a different port using the 
`--port 8989` option.

To browse decks already stored in `slidedesk.db` without scanning folders for
changes or new decks:

```bash
slidedesk /path/to/folder --no-scan
```

This disables startup and recurring folder scans.
The background embedding worker is also stopped in this mode. You can still
explicitly refresh an individual indexed deck from the GUI.

## How it works under the hood 

SlideDesk downloads models from Hugging Face on first use, to run them locally
Hence, it does not need an API key or an OpenAI-compatible server.

* Slide texts are embedded using the [intfloat/multilingual-e5-large-instruct](https://huggingface.co/intfloat/multilingual-e5-large-instruct) model. Thanks to this, you can search for terms 
such as "image filtering" and it may find slides about "image processsing", too.

* Slide images are embedded locally with [openai/clip-vit-base-patch32](https://huggingface.co/openai/clip-vit-base-patch32). 

* Advanced Search generates editable slide outlines locally with [ibm-granite/granite-4.1-3b](https://huggingface.co/ibm-granite/granite-4.1-3b). The model is downloaded on first use.

The background worker stores vectors in `slidedesk.db`. 

“Show similar slides” presents one list: visual matches first, followed by
additional text matches, excluding duplicates. Either cache
can supply results while the other is still being built or unavailable.

## Similar and related projects

* [SlideFlow](https://github.com/michaelseliger/slideflow)
* [SlideInsight](https://github.com/NFDI4BIOIMAGE/SlideInsight)
* [pptx-automizer](https://github.com/singerla/pptx-automizer)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. Note: Large parts of the code in this repository were vibe-coded using GitHub Copilot integration in Visual Studio Code. When modifying code here, consider using a similar tool.

## Acknowledgements

We acknowledge the financial support by the Federal Ministry of Research, Technology and Space of Germany and by Sächsische Staatsministerium für Wissenschaft, Kultur und Tourismus in the programme Center of Excellence for AI-research „Center for Scalable Data Analytics and Artificial Intelligence Dresden/Leipzig“, project identification number: ScaDS.AI

