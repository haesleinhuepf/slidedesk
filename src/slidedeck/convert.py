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


class ConversionError(RuntimeError):
    pass


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


def shutdown() -> None:
    """Quit the current thread's PowerPoint instance, if one was started."""
    app = getattr(_local, "app", None)
    if app is None:
        return
    try:
        app.Quit()
    except Exception:
        pass
    _local.app = None
    try:
        import pythoncom

        pythoncom.CoUninitialize()
    except Exception:
        pass
