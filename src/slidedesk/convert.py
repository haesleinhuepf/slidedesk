"""Helpers that drive Microsoft PowerPoint via COM automation (Windows-only):
repairing re-saved exports and copying slides to the clipboard.

Slide *images* are rendered directly from .pptx files via render-pptx (see images.py)
and have no PowerPoint dependency.
"""
from __future__ import annotations

import platform
import threading
from pathlib import Path

_apps = []
_apps_lock = threading.Lock()


class ConversionError(RuntimeError):
    pass


def _retain_app(app) -> None:
    with _apps_lock:
        _apps.append(app)


def repair_pptx(source: Path, repaired: Path) -> None:
    """Open and resave an export using a dedicated PowerPoint instance."""
    if platform.system() != "Windows":
        raise ConversionError("Repairing PowerPoint exports requires Microsoft PowerPoint on Windows.")
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise ConversionError("pywin32 is required to repair PowerPoint exports.") from exc

    source = source.resolve()
    repaired = repaired.resolve()
    repaired.parent.mkdir(parents=True, exist_ok=True)
    app = None
    presentation = None
    pythoncom.CoInitialize()
    print(f"Repairing PowerPoint export: {source} -> {repaired}")
    try:
        try:
            app = win32com.client.DispatchEx("PowerPoint.Application")
            _retain_app(app)
            app.DisplayAlerts = 2  # ppAlertsNone
            presentation = app.Presentations.Open(
                str(source), ReadOnly=False, Untitled=False, WithWindow=False
            )
            presentation.SaveAs(str(repaired), 24)  # ppSaveAsOpenXMLPresentation
        finally:
            if presentation is not None:
                presentation.Close()
    except Exception as exc:
        import warnings
        warnings.warn(f"PowerPoint failed to repair {source.name}: {exc}")
    pythoncom.CoUninitialize()


def _copy_to_clipboard(pptx_path: Path, do_copy) -> None:
    """Open `pptx_path` in a windowless PowerPoint and run `do_copy(presentation)`,
    which is expected to call `.Copy()` on some slide selection to populate the clipboard.
    """
    if platform.system() != "Windows":
        raise ConversionError("Copying slides to the clipboard requires Microsoft PowerPoint on Windows.")
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise ConversionError("pywin32 is required to copy slides to the clipboard.") from exc

    pptx_path = pptx_path.resolve()
    presentation = None
    pythoncom.CoInitialize()
    try:
        try:
            app = win32com.client.DispatchEx("PowerPoint.Application")
            _retain_app(app)
            app.DisplayAlerts = 2  # ppAlertsNone
            presentation = app.Presentations.Open(
                str(pptx_path), ReadOnly=True, Untitled=False, WithWindow=False
            )
            do_copy(presentation)
        finally:
            if presentation is not None:
                presentation.Close()
    except ConversionError:
        pythoncom.CoUninitialize()
        raise
    except Exception as exc:
        pythoncom.CoUninitialize()
        raise ConversionError(f"PowerPoint failed to copy slides from {pptx_path.name}: {exc}") from exc
    pythoncom.CoUninitialize()


def copy_slide_to_clipboard(pptx_path: Path, index_in_deck: int) -> None:
    """Copy one slide from `pptx_path` onto the OS clipboard via PowerPoint's
    native `Slide.Copy()`, so it can be pasted directly into another open
    PowerPoint presentation. Requires PowerPoint on Windows.
    """
    def do_copy(presentation):
        slide_number = index_in_deck + 1
        if not (1 <= slide_number <= presentation.Slides.Count):
            raise ConversionError(f"Slide index {index_in_deck} out of range for {pptx_path.name}")
        presentation.Slides(slide_number).Copy()

    _copy_to_clipboard(pptx_path, do_copy)


def copy_slides_to_clipboard(pptx_path: Path) -> None:
    """Copy every slide in `pptx_path` onto the OS clipboard as a single
    multi-slide `SlideRange.Copy()`, so the whole set can be pasted together.
    """
    def do_copy(presentation):
        if presentation.Slides.Count == 0:
            raise ConversionError("No slides to copy")
        presentation.Slides.Range().Copy()

    _copy_to_clipboard(pptx_path, do_copy)


def shutdown() -> None:
    """Compatibility hook; PowerPoint instances are intentionally left open."""
