"""Convert .pptx files to .pdf using Microsoft PowerPoint via COM automation.

Windows-only: requires Microsoft PowerPoint and pywin32 to be installed.
Rendering slide *images* from the resulting PDFs is done separately via
pdf2image/poppler (see images.py) and has no PowerPoint dependency.
"""
from __future__ import annotations

import platform
import threading
from pathlib import Path

_PP_SAVE_AS_PDF = 32  # PowerPoint's ppSaveAsPDF file-format constant

_local = threading.local()
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
        pythoncom.CoUninitialize()
        raise ConversionError(f"PowerPoint failed to repair {source.name}: {exc}") from exc
    pythoncom.CoUninitialize()

    if not repaired.is_file() or repaired.stat().st_size == 0:
        raise ConversionError(f"PowerPoint did not produce {repaired.name}")


def _get_powerpoint_app():
    if platform.system() != "Windows":
        raise ConversionError(
            "Converting .pptx to .pdf requires Microsoft PowerPoint via COM "
            "automation, which is only available on Windows."
        )
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise ConversionError(
            "pywin32 is required for .pptx -> .pdf conversion. Install it with "
            "'pip install pywin32'."
        ) from exc

    app = getattr(_local, "app", None)
    if app is not None:
        return app

    pythoncom.CoInitialize()
    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")
    except Exception as exc:
        raise ConversionError(f"Could not start PowerPoint: {exc}") from exc
    _retain_app(app)
    _local.app = app
    return app


def convert_pptx_to_pdf(pptx_path: Path, out_pdf_path: Path, timeout: int = 180) -> None:
    """Convert `pptx_path` to a PDF at `out_pdf_path` using PowerPoint COM automation."""
    app = _get_powerpoint_app()
    pptx_path = pptx_path.resolve()
    out_pdf_path = out_pdf_path.resolve()
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    presentation = None
    try:
        presentation = app.Presentations.Open(
            str(pptx_path), ReadOnly=True, WithWindow=False
        )
        presentation.SaveAs(str(out_pdf_path), _PP_SAVE_AS_PDF)
    except Exception as exc:
        raise ConversionError(
            f"PowerPoint failed to convert {pptx_path} to PDF: {exc}"
        ) from exc
    finally:
        if presentation is not None:
            presentation.Close()

    if not out_pdf_path.exists():
        raise ConversionError(f"PowerPoint did not produce {out_pdf_path}")


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
