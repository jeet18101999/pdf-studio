"""Selecting, moving, resizing and deleting drawn markup: shapes (rectangle, ellipse, line, arrow,
polygon), pen/ink strokes, sticky notes, stamps and free-text comments."""
from __future__ import annotations

from dataclasses import dataclass

import pymupdf

T = pymupdf
SHAPE_TYPES = {T.PDF_ANNOT_SQUARE: "Rectangle", T.PDF_ANNOT_CIRCLE: "Ellipse", T.PDF_ANNOT_LINE: "Line",
               T.PDF_ANNOT_POLYGON: "Polygon", T.PDF_ANNOT_POLY_LINE: "Polyline", T.PDF_ANNOT_INK: "Pen drawing",
               T.PDF_ANNOT_TEXT: "Note", T.PDF_ANNOT_FREE_TEXT: "Text comment", T.PDF_ANNOT_STAMP: "Stamp"}
RESIZABLE_BY_RECT = {T.PDF_ANNOT_SQUARE, T.PDF_ANNOT_CIRCLE, T.PDF_ANNOT_FREE_TEXT, T.PDF_ANNOT_STAMP}
POINT_BASED = {T.PDF_ANNOT_LINE, T.PDF_ANNOT_INK, T.PDF_ANNOT_POLYGON, T.PDF_ANNOT_POLY_LINE}


@dataclass
class AnnotInfo:
    page: int
    xref: int
    type: int
    rect: pymupdf.Rect          # unrotated page coordinates

    @property
    def type_name(self) -> str:
        return SHAPE_TYPES.get(self.type, "Markup")

    @property
    def resizable(self) -> bool:
        return self.type != T.PDF_ANNOT_TEXT       # sticky-note icons have a fixed size


def list_annots(page: pymupdf.Page) -> list[AnnotInfo]:
    out = []
    for a in page.annots():
        t = a.type[0]
        if t in SHAPE_TYPES:
            r = pymupdf.Rect(a.rect)
            if not r.is_empty:
                out.append(AnnotInfo(page.number, a.xref, t, r))
    return out


def _load(page: pymupdf.Page, xref: int):
    for a in page.annots():
        if a.xref == xref:
            return a
    return None


def _mapper(old: pymupdf.Rect, new: pymupdf.Rect):
    sx = new.width / old.width if old.width > 1e-6 else 1.0
    sy = new.height / old.height if old.height > 1e-6 else 1.0

    def m(p):
        p = pymupdf.Point(p)
        return pymupdf.Point(new.x0 + (p.x - old.x0) * sx, new.y0 + (p.y - old.y0) * sy)
    return m


def _stroke_pad(annot) -> float:
    try:
        return max(0.0, float(annot.border.get("width") or 0)) / 2 + 1.0
    except Exception:
        return 1.0


def transform_annot(doc: pymupdf.Document, page_no: int, xref: int, new_rect: pymupdf.Rect) -> int:
    """Move/resize an annotation so its bounding box becomes new_rect. Returns the (possibly new) xref."""
    page = doc[page_no]
    annot = _load(page, xref)
    if annot is None:
        raise KeyError(f"annotation {xref} not found")
    new_rect = pymupdf.Rect(new_rect).normalize()
    old = pymupdf.Rect(annot.rect)
    t = annot.type[0]
    if t == T.PDF_ANNOT_TEXT:                        # note icon: move only
        w, h = old.width, old.height
        annot.set_rect(pymupdf.Rect(new_rect.x0, new_rect.y0, new_rect.x0 + w, new_rect.y0 + h))
        annot.update()
        return annot.xref
    if t in RESIZABLE_BY_RECT:
        _set_rect_exact(annot, new_rect)
        return annot.xref
    if t in POINT_BASED:
        # map the geometry inside the stroke padding so line width does not scale the shape
        pad = _stroke_pad(annot)
        inner_old = old + (pad, pad, -pad, -pad)
        inner_new = new_rect + (pad, pad, -pad, -pad)
        if inner_old.is_empty or inner_new.is_empty:
            inner_old, inner_new = old, new_rect
        m = _mapper(inner_old, inner_new)
        colors = annot.colors or {}
        border = annot.border or {}
        opacity = annot.opacity
        info = annot.info or {}
        line_ends = annot.line_ends if t in (T.PDF_ANNOT_LINE, T.PDF_ANNOT_POLY_LINE) else None
        verts = annot.vertices
        page.delete_annot(annot)
        if t == T.PDF_ANNOT_INK:
            strokes = [[m(p) for p in stroke] for stroke in verts]
            new = page.add_ink_annot([[(p.x, p.y) for p in s] for s in strokes])
        elif t == T.PDF_ANNOT_LINE:
            pts = [m(p) for p in verts]
            new = page.add_line_annot(pts[0], pts[1])
        elif t == T.PDF_ANNOT_POLYGON:
            new = page.add_polygon_annot([m(p) for p in verts])
        else:
            new = page.add_polyline_annot([m(p) for p in verts])
        if border.get("width") is not None:
            new.set_border(width=border.get("width"), dashes=border.get("dashes") or None)
        new.set_colors(stroke=colors.get("stroke"), fill=colors.get("fill"))
        if line_ends and any(line_ends):
            new.set_line_ends(*line_ends)
        if opacity is not None and 0 <= opacity < 1:
            new.set_opacity(opacity)
        if info.get("content"):
            new.set_info(content=info.get("content"), title=info.get("title") or None)
        new.update()
        return new.xref
    annot.set_rect(new_rect)
    annot.update()
    return annot.xref


def _set_rect_exact(annot, target: pymupdf.Rect):
    """Rebuilding the appearance pads the rectangle by the stroke width; compensate so repeated moves
    never make a shape creep larger."""
    annot.set_rect(target)
    annot.update()
    got = pymupdf.Rect(annot.rect)
    d = (got.x0 - target.x0, got.y0 - target.y0, got.x1 - target.x1, got.y1 - target.y1)
    if any(abs(v) > 0.05 for v in d):
        adjusted = pymupdf.Rect(target.x0 - d[0], target.y0 - d[1], target.x1 - d[2], target.y1 - d[3])
        if adjusted.width > 1 and adjusted.height > 1:
            annot.set_rect(adjusted)
            annot.update()


def delete_annot(doc: pymupdf.Document, page_no: int, xref: int):
    page = doc[page_no]
    annot = _load(page, xref)
    if annot is not None:
        page.delete_annot(annot)
