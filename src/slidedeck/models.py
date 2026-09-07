"""Plain data objects returned by the slidedeck Python API."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Deck:
    """A `.pptx` file together with its rendered `.pdf` / `.hidden.pdf` exports."""

    id: int
    pptx_path: str
    pptx_mtime: float
    pdf_path: Optional[str]
    pdf_mtime: Optional[float]
    hidden_pdf_path: Optional[str]
    hidden_pdf_mtime: Optional[float]
    last_scanned: Optional[float]

    @property
    def name(self) -> str:
        return Path(self.pptx_path).name

    @classmethod
    def from_row(cls, row) -> "Deck":
        return cls(
            id=row["id"],
            pptx_path=row["pptx_path"],
            pptx_mtime=row["pptx_mtime"],
            pdf_path=row["pdf_path"],
            pdf_mtime=row["pdf_mtime"],
            hidden_pdf_path=row["hidden_pdf_path"],
            hidden_pdf_mtime=row["hidden_pdf_mtime"],
            last_scanned=row["last_scanned"],
        )


@dataclass(frozen=True)
class Slide:
    """A single slide belonging to a `Deck`."""

    id: int
    deck_id: int
    index_in_deck: int
    visible_pdf_page: Optional[int]
    hidden: bool
    text: str

    @classmethod
    def from_row(cls, row) -> "Slide":
        return cls(
            id=row["id"],
            deck_id=row["deck_id"],
            index_in_deck=row["index_in_deck"],
            visible_pdf_page=row["visible_pdf_page"],
            hidden=bool(row["hidden"]),
            text=row["text"],
        )
