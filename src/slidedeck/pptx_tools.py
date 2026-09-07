"""Helpers built on top of python-pptx: hidden-slide flags, text extraction,
producing an "all slides visible" copy (for the .hidden.pdf export), and
copying slides between presentations (for "export selection").
"""
from __future__ import annotations

import copy
from io import BytesIO
from pathlib import Path
from typing import List

from pptx import Presentation
from pptx.oxml.ns import qn
from pptx.util import Emu


def is_slide_hidden(slide) -> bool:
    """A slide is hidden when its <p:sld show="0"/> attribute is present and 0."""
    show = slide._element.get("show")
    return show is not None and show.strip() == "0"


def set_slide_hidden(slide, hidden: bool) -> None:
    if hidden:
        slide._element.set("show", "0")
    else:
        if slide._element.get("show") is not None:
            del slide._element.attrib["show"]


def extract_text(slide) -> str:
    """Concatenate all text found in text frames and tables on a slide."""
    chunks: List[str] = []
    for shape in slide.shapes:
        try:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    chunks.append(text)
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        text = cell.text_frame.text.strip()
                        if text:
                            chunks.append(text)
            # shape_type raises NotImplementedError for some malformed/unrecognized shapes.
            if shape.shape_type == 6:  # GROUP
                for sub in shape.shapes:
                    if sub.has_text_frame:
                        text = sub.text_frame.text.strip()
                        if text:
                            chunks.append(text)
        except NotImplementedError:
            continue
    return "\n".join(chunks)


def make_all_visible_copy(pptx_path: Path, out_path: Path) -> None:
    """Save a copy of `pptx_path` to `out_path` with every slide's hidden flag cleared."""
    prs = Presentation(str(pptx_path))
    for slide in prs.slides:
        set_slide_hidden(slide, False)
    prs.save(str(out_path))


def _next_slide_id(prs: Presentation) -> int:
    sld_id_lst = prs.slides._sldIdLst
    ids = [sld_id.get("id") for sld_id in sld_id_lst]
    ids = [int(i) for i in ids if i is not None]
    return (max(ids) + 1) if ids else 256


def copy_slide_into(dest_prs: Presentation, src_slide) -> None:
    """Best-effort copy of `src_slide` (from another Presentation) into `dest_prs`.

    Uses a blank layout, then deep-copies the source slide's shape tree and
    duplicates image/media parts so pictures keep working. Complex features
    (embedded charts, OLE objects, some animations) may not be copied.
    """
    blank_layout = _best_layout(dest_prs, src_slide)
    new_slide = dest_prs.slides.add_slide(blank_layout)

    # Remove placeholder shapes that came from the blank layout so they don't
    # linger behind the copied content.
    for shape in list(new_slide.shapes):
        shape._element.getparent().remove(shape._element)

    src_part = src_slide.part
    dest_part = new_slide.part

    # Map source relationship ids -> new relationship ids for this slide.
    rel_map = {}
    for rel in src_part.rels.values():
        if rel.reltype.endswith("/image") or "image" in rel.reltype:
            image_part = rel.target_part
            new_image_part, _ = dest_part.get_or_add_image_part(BytesIO(image_part.image.blob))
            new_rId = dest_part.relate_to(new_image_part, rel.reltype)
            rel_map[rel.rId] = new_rId

    spTree = new_slide.shapes._spTree
    for child in list(src_slide.shapes._spTree):
        tag = child.tag
        if tag == qn("p:nvGrpSpPr") or tag == qn("p:grpSpPr"):
            continue
        new_child = copy.deepcopy(child)
        _remap_blips(new_child, rel_map)
        spTree.append(new_child)

    set_slide_hidden(new_slide, False)


def _best_layout(dest_prs: Presentation, src_slide):
    """Pick a layout on dest_prs: try to match by name, else use a mostly-blank one."""
    src_layout_name = None
    try:
        src_layout_name = src_slide.slide_layout.name
    except Exception:
        pass
    if src_layout_name:
        for layout in dest_prs.slide_layouts:
            if layout.name == src_layout_name:
                return layout
    return dest_prs.slide_layouts[6] if len(dest_prs.slide_layouts) > 6 else dest_prs.slide_layouts[-1]


def _remap_blips(element, rel_map) -> None:
    """Update r:embed / r:link attributes on <a:blip> elements to new rIds."""
    for blip in element.iter(qn("a:blip")):
        rid = blip.get(qn("r:embed"))
        if rid and rid in rel_map:
            blip.set(qn("r:embed"), rel_map[rid])


def save_selection_as_pptx(
    source_pptx_paths_and_indices: List[tuple], template_path: Path, out_path: Path
) -> None:
    """Build a new .pptx containing copies of the given (pptx_path, slide_index) slides.

    `template_path` is used as the base Presentation (its own slides are removed)
    so theme/master/size match one of the source decks.
    """
    dest = Presentation(str(template_path))
    while len(dest.slides._sldIdLst):
        dest.slides._sldIdLst.remove(dest.slides._sldIdLst[0])

    open_decks = {}
    for pptx_path, index in source_pptx_paths_and_indices:
        prs = open_decks.get(pptx_path)
        if prs is None:
            prs = Presentation(str(pptx_path))
            open_decks[pptx_path] = prs
        src_slide = prs.slides[index]
        copy_slide_into(dest, src_slide)

    dest.save(str(out_path))
