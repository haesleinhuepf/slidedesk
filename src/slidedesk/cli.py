"""Command-line entry point: `slidedesk serve <folder>`."""
from __future__ import annotations

import threading
import webbrowser

import click

from .project import SlideProject


@click.group()
def main() -> None:
    """slidedesk: index and browse .pptx slide decks in a folder."""


@main.command()
@click.argument("folder", type=click.Path(file_okay=False, path_type=str))
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=5000, show_default=True, type=int)
@click.option("--no-browser", is_flag=True, help="Don't open a browser window.")
@click.option("--no-scan", is_flag=True, help="Use indexed decks without scanning folders for changes or new decks.")
@click.option(
    "--scan-interval",
    default=5.0,
    show_default=True,
    type=float,
    help="Seconds between background folder scans.",
)
def serve(folder: str, host: str, port: int, no_browser: bool, scan_interval: float, no_scan: bool = False) -> None:
    """Open (or create) a project in FOLDER and start the GUI server."""
    from .server.app import create_app

    project = SlideProject(folder)
    if not no_scan:
        project.scan_in_background(interval=scan_interval)

    app = create_app(project, scan_enabled=not no_scan)

    if not no_browser:
        url = f"http://{host}:{port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    finally:
        project.close()


if __name__ == "__main__":
    main()
