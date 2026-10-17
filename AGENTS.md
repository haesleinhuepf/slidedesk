# Working on SlideDesk

## Project overview

SlideDesk is a Windows desktop-oriented Python application that indexes PowerPoint
decks and serves a touch-friendly Flask browser interface. It provides full-text,
local text/image embedding search, slide outline generation, and slide export.
Microsoft PowerPoint is required for clipboard operations and re-saving exports.
Slide rendering uses `render-pptx` directly; it does not require PDF conversion or
Poppler.

## Repository map

- `src/slidedesk/project.py`: `SlideProject`, the main API coordinating indexing,
  search, rendering, background workers, and exports.
- `src/slidedesk/db.py` and `models.py`: SQLite schema, migrations, and data models.
- `src/slidedesk/scanner.py`: folder scanning and indexing.
- `src/slidedesk/convert.py`, `images.py`, `layout_strip.py`, and `pptx_tools.py`:
  slide rendering, image processing, and PowerPoint operations.
- `src/slidedesk/embeddings.py`, `image_embeddings.py`, and `text_generation.py`:
  local model loading and inference.
- `src/slidedesk/cli.py`: Click CLI and application startup/shutdown.
- `src/slidedesk/server/app.py`: Flask application factory and HTTP routes.
- `src/slidedesk/server/templates/index.html` and `server/static/`: browser UI,
  plain JavaScript, and CSS. No Node package installation or frontend build is
  configured.
- `tests/`: Python pytest tests and standalone Node.js JavaScript tests.
- `pyproject.toml`: package metadata, dependencies, and console entry point.

## Setup and commands

Run commands from the repository root. Use the existing virtual environment when
available; on Windows its interpreter is `.venv\Scripts\python.exe`.

```powershell
# Install editable package and test dependencies into the active environment.
python -m pip install -e ".[dev]"

# Run Python tests, or target the module relevant to a change.
python -m pytest
python -m pytest tests/test_server.py

# Run the JavaScript tests with Node's built-in test runner.
node --test tests/test_auto_zoom.cjs tests/test_viewport.cjs

# Start the application on a disposable folder or a chosen deck collection.
slidedesk <folder> --no-browser

# Browse an already indexed collection without background scanning/embedding.
slidedesk <folder> --no-browser --no-scan
```

The default address is `http://127.0.0.1:5000`; use `--port` to change it.
Model downloads occur on first use and can be large. Prefer mocked model loading
and temporary project folders for automated checks, following existing tests.
Do not require model downloads or an installed PowerPoint instance for unit tests
that can exercise the behavior with mocks.

## Implementation guidance

- Follow the surrounding Python and JavaScript style. Keep Python compatible with
  the declared minimum version in `pyproject.toml` (currently 3.9).
- Keep orchestration in `SlideProject`, HTTP handling in the Flask layer, and
  model-specific work in the corresponding model modules.
- Preserve thread synchronization and resource cleanup when changing scanning,
  embeddings, SQLite access, or export handling. The Flask server is threaded and
  background workers share project state.
- Projects persist data in `<folder>/.slidedesk/slidedesk.db` and cache images in
  `<folder>/.slidedesk/_cache`. Preserve migration support for existing projects
  when changing database schemas or storage paths.
- Preserve `--no-scan` behavior: startup and recurring scans and the background
  embedding worker are disabled, while explicit GUI refresh remains available.
- Keep inference local; the current application does not require an API key or
  an external inference server.
- Preserve source presentations. Use temporary directories for generated decks
  and test fixtures; do not commit user decks, databases, caches, model weights,
  or environment files.
- Update the README when changing installation, CLI options, prerequisites, or
  user-visible workflows. Avoid unrelated refactoring and dependency changes.

## Validation

Run the relevant pytest modules for Python changes and the Node tests for viewport
or auto-zoom changes. For broader changes, run the full applicable test suites.
Add regression coverage for behavior changes using existing test patterns.
For UI changes, also check the affected workflow in a browser when available;
the Node tests exercise extracted functions and do not replace visual checks.
Report which checks ran and any checks blocked by missing dependencies, models,
or Windows/PowerPoint integration. No formatter or linter is configured in
`pyproject.toml`; do not assume additional tooling is required.
