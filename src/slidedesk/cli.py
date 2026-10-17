"""Command-line entry point: `slidedesk <folder>`."""
from __future__ import annotations

import importlib
import logging
import os
import signal
import subprocess
import sys
import threading
import webbrowser

import click

from .project import SlideProject


def _start_scan_process(folder: str, scan_interval: float) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "slidedesk.cli", folder, "--scan-only",
           "--scan-interval", str(scan_interval)]
    kwargs = {}
    if sys.platform == "win32":
        # A separate group lets us deliver CTRL_BREAK without hitting this process.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(cmd, **kwargs)


def _stop_scan_process(proc: subprocess.Popen, timeout: float = 15.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except OSError:
        pass


def _preload_torch() -> None:
    """Import torch up front so the first similarity or semantic search doesn't pay for it."""
    print("Loading torch...")
    try:
        importlib.import_module("torch")
    except ImportError:
        logging.getLogger(__name__).warning("torch is not installed; embedding search is unavailable.")


def _run_scan_only(folder: str, scan_interval: float) -> None:
    stop = threading.Event()
    handler = lambda *_: stop.set()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, handler)
    project = SlideProject(folder)
    try:
        project.scan_in_background(interval=scan_interval)
        while not stop.wait(0.5):
            pass
    finally:
        # Workers may be inside a long model call; don't let that block exit.
        closer = threading.Thread(target=project.close, daemon=True)
        closer.start()
        closer.join(10.0)
        os._exit(0)


@click.command()
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
@click.option(
    "--scan-only",
    is_flag=True,
    help="Run only the background scan and embedding worker, without the web server.",
)
def main(folder: str, host: str, port: int, no_browser: bool, scan_interval: float,
         no_scan: bool = False, scan_only: bool = False) -> None:
    """Open (or create) a project in FOLDER and start the GUI server.

    Unless --no-scan is given, scanning and embedding run in a --scan-only subprocess.
    """
    if scan_only:
        _run_scan_only(folder, scan_interval)
        return

    from .server.app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    project = SlideProject(folder)
    scan_proc = None if no_scan else _start_scan_process(folder, scan_interval)
    # After starting the scan subprocess, so both start up in parallel.
    _preload_torch()

    app = create_app(
        project,
        scan_enabled=not no_scan,
        on_shutdown=(lambda: _stop_scan_process(scan_proc)) if scan_proc is not None else None,
    )

    if not no_browser:
        url = f"http://{host}:{port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    finally:
        if scan_proc is not None:
            _stop_scan_process(scan_proc)
        project.close()


if __name__ == "__main__":
    main()
