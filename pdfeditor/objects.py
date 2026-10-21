"""Vector objects: select, move, resize, copy/paste and delete figures made of PDF line art
(e.g. CAD views), together with the images and text labels that sit inside them.

CAD exports often pack whole views - even several views - into a single PDF path with thousands of
segments, so the page's line art is first detected as figures (see PageFigures) and selection works
on whole figures, never on half a line:
  * click   -> the figure the clicked line belongs to, including its leader lines and bubbles
  * marquee -> every figure lying mostly inside the rectangle, each taken complete
    (whole=False falls back to exactly the items inside the rectangle)
Editing removes the affected paths (redaction, line art only), redraws the parts of those paths that
were not selected exactly where they were, and draws the selected items with the new geometry.
"""
from __future__ import annotations

import copy
import io
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import pymupdf
from PIL import Image

from . import pageimages, textops

EPS = 0.6
TOUCH = 0.8          # points closer than this are considered connected
CELL = 12.0          # spatial grid cell for connectivity search
FIGURE_GAP = 6.0     # pieces closer than this (about 2 mm) belong to the same figure


# ============================================================================ data
@dataclass
class TextItem:
    text: str
    rect: pymupdf.Rect
    style: textops.TextStyle
    baseline: float
    line_rects: list


@dataclass
class ImageItem:
    rect: pymupdf.Rect
    png: bytes
    xref: int = 0


@dataclass
class Selection:
    page: int
    pieces: list = field(default_factory=list)     # path dicts holding only the selected items
    images: list = field(default_factory=list)     # ImageItem
    texts: list = field(default_factory=list)      # TextItem
    figures: set = field(default_factory=set)      # ids of the detected figures it holds (before editing)

    @property
    def bbox(self) -> pymupdf.Rect:
        return _union([_piece_bounds(p) for p in self.pieces if p["items"]] +
                      [i.rect for i in self.images] + [t.rect for t in self.texts])

    @property
    def empty(self) -> bool:
        return not (self.pieces or self.images or self.texts)

    @property
    def item_count(self) -> int:
        return sum(len(p["items"]) for p in self.pieces)

    def describe(self) -> str:
        parts = []
        if self.pieces:
            parts.append(f"{self.item_count} line segment{'s' if self.item_count != 1 else ''}")
        if self.images:
            parts.append(f"{len(self.images)} image{'s' if len(self.images) != 1 else ''}")
        if self.texts:
            parts.append(f"{len(self.texts)} text label{'s' if len(self.texts) != 1 else ''}")
        return ", ".join(parts) or "nothing"


CLIPBOARD: dict | None = None


# ============================================================================ geometry helpers
def _item_points(it):
    k = it[0]
    if k == "l":
        return [pymupdf.Point(it[1]), pymupdf.Point(it[2])]
    if k == "c":
        return [pymupdf.Point(x) for x in it[1:5]]
    if k == "re":
        r = pymupdf.Rect(it[1])
        return [r.tl, r.tr, r.br, r.bl]
    if k == "qu":
        q = pymupdf.Quad(it[1])
        return [q.ul, q.ur, q.lr, q.ll]
    return []


def _item_segments(it):
    pts = _item_points(it)
    if it[0] in ("re", "qu"):
        return list(zip(pts, pts[1:] + pts[:1]))
    return list(zip(pts, pts[1:]))              # line, or bezier control polygon


def _item_ends(it):
    pts = _item_points(it)
    if it[0] in ("re", "qu"):
        return pts
    return [pts[0], pts[-1]] if pts else []


def _item_rect(it) -> pymupdf.Rect:
    """Bounding box of an item. Horizontal/vertical lines get a hair of thickness: PyMuPDF treats
    zero-width rectangles as empty, and empty rectangles are ignored by unions and intersections."""
    pts = _item_points(it)
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if x1 - x0 < 0.02:
        x0, x1 = x0 - 0.01, x1 + 0.01
    if y1 - y0 < 0.02:
        y0, y1 = y0 - 0.01, y1 + 0.01
    return pymupdf.Rect(x0, y0, x1, y1)


def _union(rects) -> pymupdf.Rect:
    """Union of rectangles that never pulls in the origin (unlike starting from an empty Rect())."""
    out = None
    for r in rects:
        if r is None:
            continue
        out = pymupdf.Rect(r) if out is None else (out | r)
    return out if out is not None else pymupdf.Rect()


def _key(it):
    return (it[0],) + tuple((round(p.x, 1), round(p.y, 1)) for p in _item_points(it))


def _dist_to_segment(p, a, b) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / L))
    return ((p.x - a.x - t * dx) ** 2 + (p.y - a.y - t * dy) ** 2) ** 0.5


def _piece_rect(p) -> pymupdf.Rect:
    return _union(_item_rect(it) for it in p["items"] if _item_points(it))


def _piece_bounds(p) -> pymupdf.Rect:
    w = (p.get("width") or 0) / 2
    return _piece_rect(p) + (-w, -w, w, w)


def _inside(inner: pymupdf.Rect, outer: pymupdf.Rect, tol=EPS) -> bool:
    return (inner.x0 >= outer.x0 - tol and inner.y0 >= outer.y0 - tol and
            inner.x1 <= outer.x1 + tol and inner.y1 <= outer.y1 + tol)


def _page_paths(page: pymupdf.Page) -> list:
    return [p for p in page.get_drawings() if p.get("items")]


def _style_sig(p):
    col = tuple(round(c, 3) for c in p["color"]) if p.get("color") else None
    fill = tuple(round(c, 3) for c in p["fill"]) if p.get("fill") else None
    return (col, fill, round(p.get("width") or 0, 3), p.get("dashes"), p.get("type"))


def _loose_sig(p):
    """Identity of a drawn path that survives the page being rewritten: style + number of items."""
    return _style_sig(p) + (len(p.get("items") or ()),)


def _piece(path, items):
    q = {k: v for k, v in path.items() if k != "items"}
    q["items"] = list(items)
    q["rect"] = _piece_rect(q)
    return q


# ============================================================================ selecting
def _user_images(page: pymupdf.Page):
    from . import scanedit
    background = scanedit.scan_background_xrefs(page)
    return [i for i in pageimages.list_images(page) if i.xref and i.xref not in background]


def _texts_inside(page: pymupdf.Page, rect: pymupdf.Rect) -> list[TextItem]:
    out = []
    for b in textops.extract_blocks(page):
        ident = abs(b.matrix.a - 1) < 1e-6 and abs(b.matrix.b) < 1e-6
        if ident and b.source == "native" and _inside(b.rect, rect, 1.0):
            out.append(TextItem(b.text, pymupdf.Rect(b.rect), b.style.copy(), b.first_baseline.y, list(b.line_rects)))
    return out


def _images_inside(doc, page: pymupdf.Page, rect: pymupdf.Rect) -> list[ImageItem]:
    out = []
    for img in _user_images(page):
        if _inside(img.rect, rect, 1.0):
            try:
                pil = pageimages.extract_pil(doc, img.xref)
            except Exception:
                continue
            buf = io.BytesIO()
            pil.save(buf, "PNG")
            out.append(ImageItem(pymupdf.Rect(img.rect), buf.getvalue(), img.xref))
    return out


# ============================================================================ figure detection
SMALL_COMP = 36.0        # bubbles, arrow heads and tick marks hanging off a leader line
_FIG_CACHE: dict = {}


def _grid_of(entries) -> dict:
    g = defaultdict(list)
    for i, (_, _, _, r) in enumerate(entries):
        for gx in range(int((r.x0 - TOUCH) // CELL), int((r.x1 + TOUCH) // CELL) + 1):
            for gy in range(int((r.y0 - TOUCH) // CELL), int((r.y1 + TOUCH) // CELL) + 1):
                g[(gx, gy)].append(i)
    return g


def _cells(r):
    for gx in range(int((r.x0 - TOUCH) // CELL), int((r.x1 + TOUCH) // CELL) + 1):
        for gy in range(int((r.y0 - TOUCH) // CELL), int((r.y1 + TOUCH) // CELL) + 1):
            yield (gx, gy)


def _touches(a_it, a_r, b_it, b_r, gap=TOUCH) -> bool:
    """Do two items meet, i.e. does an endpoint of one lie on the other?"""
    if not (a_r + (-gap, -gap, gap, gap)).intersects(b_r):
        return False
    return (any(_dist_to_segment(e, p, q) <= gap for e in _item_ends(a_it) for p, q in _item_segments(b_it)) or
            any(_dist_to_segment(e, p, q) <= gap for e in _item_ends(b_it) for p, q in _item_segments(a_it)))


class PageFigures:
    """The line art of a page grouped into figures - a CAD view, a detail, a symbol - so that selecting
    always takes a whole object and never cuts a line in half.

    Items are first joined into components (touching / shared endpoints); components lying within
    FIGURE_GAP of each other form one figure. Sheet-border items and long rules are held back, because
    they would chain every view on the sheet together - but such a line is pulled into a figure when it
    only ever touches that one figure (a bubble's leader line), and the small component at its far end
    (the bubble itself, an arrow head) comes along, so nothing is left dangling.

    Components are built once per page; growing them into a figure happens on demand and is cached, so
    the first click on a big drawing costs about a second and every one after it is instant."""

    def __init__(self, page: pymupdf.Page):
        self.paths = _page_paths(page)
        pr = (page.rect * page.derotation_matrix).normalize()
        big_w, big_h = 0.55 * pr.width, 0.55 * pr.height
        band = 0.045 * min(pr.width, pr.height)
        long_rule = 0.2 * max(pr.width, pr.height)
        self.flat, self.loose = [], []          # (path index, item index, item, rect)
        for pi, p in enumerate(self.paths):
            for ii, it in enumerate(p["items"]):
                if not _item_points(it):
                    continue
                r = _item_rect(it)
                if r.width > big_w or r.height > big_h:       # the sheet frame is never part of a figure
                    continue
                edge = (r.x1 < pr.x0 + band or r.x0 > pr.x1 - band or
                        r.y1 < pr.y0 + band or r.y0 > pr.y1 - band)
                rule = it[0] == "l" and (r.height < 0.5 and r.width > long_rule or
                                         r.width < 0.5 and r.height > long_rule)
                (self.loose if edge or rule else self.flat).append((pi, ii, it, r))
        self.grid = _grid_of(self.flat)
        self.loose_grid = _grid_of(self.loose)
        self._components()
        self._fig_of_comp: dict = {}
        self._figures: dict = {}
        self._next_fid = 0

    # -- components -------------------------------------------------------------
    def _neighbours(self, i):
        _, _, it, r = self.flat[i]
        seen = set()
        for cell in _cells(r):
            for j in self.grid.get(cell, ()):
                if j == i or j in seen:
                    continue
                seen.add(j)
                if _touches(it, r, self.flat[j][2], self.flat[j][3]):
                    yield j

    def _components(self):
        parent = list(range(len(self.flat)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for i in range(len(self.flat)):
            for j in self._neighbours(i):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
        self.comps = defaultdict(list)
        self.comp_of = [0] * len(self.flat)
        for i in range(len(self.flat)):
            c = find(i)
            self.comp_of[i] = c
            self.comps[c].append(i)
        self.comp_rect = {c: _union(self.flat[i][3] for i in idxs) for c, idxs in self.comps.items()}

    # -- figures ----------------------------------------------------------------
    def figure_of_comp(self, comp) -> int:
        if comp in self._fig_of_comp:
            return self._fig_of_comp[comp]
        chosen = {comp}
        stack = [comp]
        while stack:                            # CAD exports break arcs and hatching into many pieces
            cur = stack.pop()
            zone = self.comp_rect[cur] + (-FIGURE_GAP, -FIGURE_GAP, FIGURE_GAP, FIGURE_GAP)
            for c in self.comps:
                if c not in chosen and zone.intersects(self.comp_rect[c]) and \
                        _comps_close(self.comps[cur], self.comps[c], self.flat):
                    chosen.add(c)
                    stack.append(c)
        # loose details (centre marks, holes, hatching) inside ONE outline of the figure belong to it;
        # testing against the whole figure would also swallow neighbouring bubbles and labels
        hosts = [self.comp_rect[c] for c in chosen
                 if self.comp_rect[c].width > 2 * FIGURE_GAP and self.comp_rect[c].height > 2 * FIGURE_GAP]
        for c in self.comps:
            if c not in chosen and any(_inside(self.comp_rect[c], h, 0) for h in hosts):
                chosen.add(c)
        fid = self._next_fid
        self._next_fid += 1
        for c in chosen:
            self._fig_of_comp[c] = fid
        loose = self._attached_loose(chosen, fid)
        self._figures[fid] = (chosen, loose)
        return fid

    def _loose_touching(self, li):
        """(flat item indices, loose item indices) that the loose item `li` meets."""
        _, _, it, r = self.loose[li]
        flat_hits, loose_hits = set(), set()
        for cell in _cells(r):
            for j in self.grid.get(cell, ()):
                if j not in flat_hits and _touches(it, r, self.flat[j][2], self.flat[j][3]):
                    flat_hits.add(j)
            for j in self.loose_grid.get(cell, ()):
                if j != li and j not in loose_hits and _touches(it, r, self.loose[j][2], self.loose[j][3]):
                    loose_hits.add(j)
        return flat_hits, loose_hits

    def _attached_loose(self, chosen: set, fid: int) -> set:
        """Long lines and border-band items that belong to this figure alone - a leader line out to a
        bubble, a dimension line across the view. A line shared with another figure stays where it is."""
        out = set()
        for _ in range(3):                      # leader -> bubble -> tick chains
            added = False
            for li in range(len(self.loose)):
                if li in out:
                    continue
                flat_hits, loose_hits = self._loose_touching(li)
                comps_hit = {self.comp_of[j] for j in flat_hits}
                if not (comps_hit & chosen or loose_hits & out):
                    continue
                strangers = [c for c in comps_hit if c not in chosen]
                if any(c in self._fig_of_comp or self.comp_rect[c].width > SMALL_COMP or
                       self.comp_rect[c].height > SMALL_COMP for c in strangers):
                    continue                    # shared with another figure: never cut it loose
                out.add(li)
                for c in strangers:             # the bubble at the end of the leader comes along
                    chosen.add(c)
                    self._fig_of_comp[c] = fid
                added = True
            if not added:
                break
        return out

    def has_figure(self, fid) -> bool:
        return fid in self._figures

    def figure_items(self, fid) -> list:
        chosen, loose = self._figures[fid]
        return [self.flat[i] for c in chosen for i in self.comps[c]] + [self.loose[li] for li in loose]

    def comp_rects(self, fid) -> list:
        return [self.comp_rect[c] for c in self._figures[fid][0]]

    def figure_rect(self, fid) -> pymupdf.Rect:
        return _union(r for _, _, _, r in self.figure_items(fid))

    def figure_at(self, point: pymupdf.Point, tol=3.0) -> int | None:
        """The figure whose line lies under `point`, or None."""
        best, best_d = None, tol
        for kind, entries in (("flat", self.flat), ("loose", self.loose)):
            for i, (_, _, it, r) in enumerate(entries):
                if not (r + (-tol, -tol, tol, tol)).contains(point):
                    continue
                d = min(_dist_to_segment(point, a, b) for a, b in _item_segments(it))
                if d <= best_d:
                    best, best_d = (kind, i), d
        if best is None:
            return None
        if best[0] == "flat":
            return self.figure_of_comp(self.comp_of[best[1]])
        flat_hits, _ = self._loose_touching(best[1])        # a leader line: the figure it hangs off
        for j in flat_hits:
            fid = self.figure_of_comp(self.comp_of[j])
            if best[1] in self._figures[fid][1]:
                return fid
        return self.figure_of_comp(self.comp_of[min(flat_hits)]) if flat_hits else None

    def figures_in(self, rect: pymupdf.Rect, frac=0.6) -> list:
        """Figures lying mostly (>= frac of their items) inside `rect`, plus the figure that surrounds
        `rect` when the box was drawn inside one big figure: taking the whole object always beats
        cutting its lines in half."""
        out, seen = [], set()
        for i, (_, _, _, r) in enumerate(self.flat):
            if not rect.intersects(r):
                continue
            fid = self.figure_of_comp(self.comp_of[i])
            if fid in seen:
                continue
            seen.add(fid)
            items = self.figure_items(fid)
            inside = sum(1 for _, _, _, ir in items if _inside(ir, rect))
            if inside >= max(1, int(len(items) * frac)) or \
                    (inside >= 3 and _inside(rect, self.figure_rect(fid), FIGURE_GAP)):
                out.append(fid)
        return out

    def all_figures(self) -> list:
        return sorted({self.figure_of_comp(c) for c in list(self.comps)})


def page_figures(doc: pymupdf.Document, page_no: int) -> PageFigures:
    """Detected figures of a page, cached until the page's content streams change."""
    page = doc[page_no]
    try:
        token = (tuple(page.get_contents()), len(page.read_contents()))
    except Exception:
        token = None
    key = (id(doc), page_no)
    hit = _FIG_CACHE.get(key)
    if hit is not None and token is not None and hit[0] == token:
        return hit[1]
    idx = PageFigures(page)
    if len(_FIG_CACHE) > 6:
        _FIG_CACHE.clear()
    _FIG_CACHE[key] = (token, idx)
    return idx


def _figure_texts(page, box, chosen_items, comp_rects) -> list:
    """Labels belonging to a figure: inside one of its outlines (bubble numbers) or touching its lines."""
    out = []
    for t in _texts_inside(page, box + (-20, -20, 20, 20)):
        zone = t.rect + (-FIGURE_GAP, -FIGURE_GAP, FIGURE_GAP, FIGURE_GAP)
        inside_piece = any(_inside(t.rect, pr, 0.5) and pr.width < box.width * 0.6 for pr in comp_rects)
        touching = any(zone.intersects(r) and any(zone.contains(e) for e in _item_ends(it))
                       for _, _, it, r in chosen_items)
        if inside_piece or touching:
            out.append(t)
    return out


def _selection_for(doc, page, idx: PageFigures, fids, with_text=True) -> Selection:
    by_path = defaultdict(list)
    chosen_items, comp_rects = [], []
    for fid in fids:
        items = idx.figure_items(fid)
        chosen_items += items
        comp_rects += idx.comp_rects(fid)
        for pi, _, it, _ in items:
            by_path[pi].append(it)
    sel = Selection(page.number, pieces=[_piece(idx.paths[pi], items) for pi, items in by_path.items()])
    sel.figures = set(fids)
    if sel.empty:
        return sel
    box = sel.bbox
    sel.images = _images_inside(doc, page, box)
    if with_text:
        sel.texts = _figure_texts(page, box, chosen_items, comp_rects)
    return sel


def figure_at(doc: pymupdf.Document, page_no: int, point, tolerance=3.0) -> int | None:
    return page_figures(doc, page_no).figure_at(pymupdf.Point(point), tolerance)


def figure_rect(doc: pymupdf.Document, page_no: int, fid: int) -> pymupdf.Rect | None:
    idx = page_figures(doc, page_no)
    return idx.figure_rect(fid) if idx.has_figure(fid) else None


def select_figures(doc: pymupdf.Document, page_no: int, fids, with_text=True) -> Selection:
    """Selection holding the given detected figures, complete."""
    idx = page_figures(doc, page_no)
    return _selection_for(doc, doc[page_no], idx, [f for f in fids if idx.has_figure(f)], with_text)


def all_figures(doc: pymupdf.Document, page_no: int) -> list:
    return page_figures(doc, page_no).all_figures()


# ============================================================================ selecting
def select_rect(doc: pymupdf.Document, page_no: int, rect: pymupdf.Rect, with_text=True,
                whole=True) -> Selection:
    """Marquee selection.

    whole=True (the default) snaps to detected figures: every figure lying mostly inside `rect` is taken
    complete, so a line is never cut in two. When no figure qualifies - a box drawn inside a single big
    figure - it falls back to exactly the items inside, which is also what whole=False always does."""
    page = doc[page_no]
    rect = pymupdf.Rect(rect).normalize()
    if whole:
        idx = page_figures(doc, page_no)
        sel = _selection_for(doc, page, idx, idx.figures_in(rect), with_text)
        if not sel.empty:
            return sel
    sel = Selection(page_no)
    for p in _page_paths(page):
        items = [it for it in p["items"] if _item_points(it) and _inside(_item_rect(it), rect)]
        if items:
            sel.pieces.append(_piece(p, items))
    sel.images = _images_inside(doc, page, rect)
    if with_text:
        sel.texts = _texts_inside(page, rect)
    return sel


def select_at(doc: pymupdf.Document, page_no: int, point: pymupdf.Point, tolerance=3.0) -> Selection:
    """Click selection: the whole detected figure the clicked line belongs to."""
    idx = page_figures(doc, page_no)
    fid = idx.figure_at(pymupdf.Point(point), tolerance)
    if fid is None:
        return Selection(page_no)
    return _selection_for(doc, doc[page_no], idx, [fid])


def _comps_close(a_idx, b_idx, flat, gap=FIGURE_GAP) -> bool:
    """Are two components within `gap` of each other (endpoint-to-segment distance)?"""
    b_rect = pymupdf.Rect()
    for j in b_idx:
        b_rect |= flat[j][3]
    zone = b_rect + (-gap, -gap, gap, gap)
    a_items = [flat[i] for i in a_idx if zone.intersects(flat[i][3])]
    if not a_items:
        return False
    b_items = [flat[j] for j in b_idx]
    for _, _, ait, ar in a_items:
        az = ar + (-gap, -gap, gap, gap)
        a_ends, a_segs = _item_ends(ait), _item_segments(ait)
        for _, _, bit, br in b_items:
            if not az.intersects(br):
                continue
            if any(_dist_to_segment(e, p, q) <= gap for e in a_ends for p, q in _item_segments(bit)) or \
                    any(_dist_to_segment(e, p, q) <= gap for e in _item_ends(bit) for p, q in a_segs):
                return True
    return False


# ============================================================================ drawing
def draw_paths(page: pymupdf.Page, paths: list, m: pymupdf.Matrix | None = None, width_scale: float = 1.0,
               overlay: bool = True):
    """Draw path dicts (as returned by get_drawings, possibly with a subset of items) with their styling."""
    if not paths:
        return
    m = m or pymupdf.Matrix(1, 0, 0, 1, 0, 0)
    shape = page.new_shape()
    for p in paths:
        drew = False
        for it in p["items"]:
            k = it[0]
            if k == "l":
                shape.draw_line(pymupdf.Point(it[1]) * m, pymupdf.Point(it[2]) * m)
            elif k == "c":
                shape.draw_bezier(*(pymupdf.Point(x) * m for x in it[1:5]))
            elif k == "re":
                shape.draw_quad(pymupdf.Rect(it[1]).quad * m)
            elif k == "qu":
                shape.draw_quad(pymupdf.Quad(it[1]) * m)
            else:
                continue
            drew = True
        if not drew:
            continue
        kind = p.get("type") or "s"
        caps = p.get("lineCap") or (0,)
        width = p.get("width")
        shape.finish(color=p.get("color") if "s" in kind else None,
                     fill=p.get("fill") if "f" in kind else None,
                     width=(width if width is not None else 1.0) * width_scale,
                     lineCap=int(max(caps)) if isinstance(caps, (tuple, list)) else int(caps),
                     lineJoin=int(p.get("lineJoin") or 0),
                     dashes=p.get("dashes") or None,
                     closePath=bool(p.get("closePath")) and "f" in kind,
                     even_odd=bool(p.get("even_odd", False)),
                     stroke_opacity=p.get("stroke_opacity") if p.get("stroke_opacity") is not None else 1,
                     fill_opacity=p.get("fill_opacity") if p.get("fill_opacity") is not None else 1)
    shape.commit(overlay=overlay)


def _map_item(it, m):
    k = it[0]
    if k == "l":
        return ("l", pymupdf.Point(it[1]) * m, pymupdf.Point(it[2]) * m)
    if k == "c":
        return ("c",) + tuple(pymupdf.Point(x) * m for x in it[1:5])
    if k == "re":
        return ("qu", pymupdf.Rect(it[1]).quad * m)
    if k == "qu":
        return ("qu", pymupdf.Quad(it[1]) * m)
    return it


def _mapped_pieces(pieces, m, width_scale):
    out = []
    for p in pieces:
        q = dict(p)
        q["items"] = [_map_item(it, m) for it in p["items"]]
        if p.get("width") is not None:
            q["width"] = p["width"] * width_scale
        q["rect"] = _piece_rect(q)
        out.append(q)
    return out


def _mapping(old: pymupdf.Rect, new: pymupdf.Rect) -> tuple[pymupdf.Matrix, float]:
    sx = new.width / old.width if old.width > 1e-6 else 1.0
    sy = new.height / old.height if old.height > 1e-6 else 1.0
    m = pymupdf.Matrix(1, 0, 0, 1, -old.x0, -old.y0) * pymupdf.Matrix(sx, 0, 0, sy, 0, 0) * \
        pymupdf.Matrix(1, 0, 0, 1, new.x0, new.y0)
    return m, abs(sx * sy) ** 0.5


# ============================================================================ editing
def _match_selected(paths: list, pieces: list) -> dict:
    """Locate the selected pieces in the page's current paths: {path index: Counter of item keys}."""
    path_keys = [Counter(_key(it) for it in p["items"]) for p in paths]
    path_sigs = [_style_sig(p) for p in paths]
    involved = defaultdict(Counter)
    for piece in pieces:
        keys = Counter(_key(it) for it in piece["items"])
        sig = _style_sig(piece)
        best, best_hits = None, 0
        for pi in range(len(paths)):
            if path_sigs[pi] != sig:
                continue
            hits = sum((keys & path_keys[pi]).values())
            if hits > best_hits:
                best, best_hits = pi, hits
        if best is not None:
            involved[best] += keys & path_keys[best]
    return involved


def _rebuild_line_art(page: pymupdf.Page, snapshot: list, exclude: dict, extra: list) -> list:
    """Replace ALL line art on the page by: every snapshot path minus the excluded items (same position and
    drawing order), followed by the `extra` pieces. Redrawing everything from a snapshot keeps untouched
    graphics pixel-identical no matter how the PDF engine rewrites or splits paths during removal.
    Returns the pieces as they now exist on the page for `extra` (exact stored geometry)."""
    from . import scanedit
    ur = (page.rect * page.derotation_matrix).normalize()
    page.add_redact_annot(ur + (-5, -5, 5, 5), fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                          text=pymupdf.PDF_REDACT_TEXT_NONE)
    out = []
    for pi, p in enumerate(snapshot):
        if pi in exclude:
            left = Counter(exclude[pi])
            keep = []
            for it in p["items"]:
                k = _key(it)
                if left[k] > 0:
                    left[k] -= 1
                    continue
                keep.append(it)
            if keep:
                out.append(_piece(p, keep))
        else:
            out.append(p)
    drawable = [p for p in out + list(extra) if any(_item_points(it) for it in p["items"])]
    # graphics go underneath text/images, except over a scanned page image
    draw_paths(page, drawable, overlay=bool(scanedit.background_images(page)))
    after = _page_paths(page)
    n_out = len(drawable) - len(extra)
    if extra and len(after) == len(drawable):
        return after[n_out:]
    return list(extra)


def _remove_images_texts(page: pymupdf.Page, sel: Selection):
    for img in sel.images:
        info = next((i for i in pageimages.list_images(page) if i.xref == img.xref), None)
        if info is not None:
            pageimages.delete_image(page, info)
    if sel.texts:
        textops.remove_text_rects(page, [lr for t in sel.texts for lr in t.line_rects])


def _place(doc, page: pymupdf.Page, pieces, images, texts, m: pymupdf.Matrix, scale: float) -> Selection:
    """Paste: draw pieces on top of the page, plus images and text."""
    new = Selection(page.number, pieces=_mapped_pieces(pieces, m, scale))
    before = len(_page_paths(page))
    draw_paths(page, new.pieces)
    after = _page_paths(page)
    if len(after) == before + len(new.pieces):
        new.pieces = after[before:]
    _place_images_texts(doc, page, images, texts, m, scale, new)
    return new


def _place_images_texts(doc, page: pymupdf.Page, images, texts, m: pymupdf.Matrix, scale: float,
                        new: Selection, img_op=None):
    for img in images:
        r = (pymupdf.Rect(img.rect).quad * m).rect
        pil, png = Image.open(io.BytesIO(img.png)), img.png
        if img_op is not None:                 # rotate / mirror the picture itself, not just its box
            pil = img_op(pil)
            buf = io.BytesIO()
            pil.save(buf, "PNG")
            png = buf.getvalue()
        xref = pageimages.insert_pil(page, r, pil)
        new.images.append(ImageItem(r, png, xref))
    for t in texts:
        r = (pymupdf.Rect(t.rect).quad * m).rect
        base = (pymupdf.Point(t.rect.x0, t.baseline) * m).y
        style = t.style.copy(size=max(2.0, t.style.size * scale))
        textops.write_text(doc, page, r, t.text, style, pymupdf.Point(r.x0, base))
        # keep the exactly mapped geometry: a box re-measured from the new glyphs would differ slightly and
        # every further move/resize of the selection would then add a little unwanted scaling
        line_rects = [((pymupdf.Rect(lr).quad * m).rect, hz) for lr, hz in t.line_rects]
        new.texts.append(TextItem(t.text, r, style, base, line_rects))


def _apply(doc: pymupdf.Document, sel: Selection, m: pymupdf.Matrix, scale: float, img_op=None) -> Selection:
    """Redraw a selection through matrix `m`, leaving the rest of the page untouched."""
    page = doc[sel.page]
    pieces, images, texts = copy.deepcopy(sel.pieces), list(sel.images), copy.deepcopy(sel.texts)
    new = Selection(page.number, pieces=_mapped_pieces(pieces, m, scale))
    if pieces:
        snapshot = _page_paths(page)
        new.pieces = _rebuild_line_art(page, snapshot, _match_selected(snapshot, pieces), new.pieces)
    _remove_images_texts(page, sel)
    _place_images_texts(doc, page, images, texts, m, scale, new, img_op)
    return new


def transform(doc: pymupdf.Document, sel: Selection, new_rect: pymupdf.Rect) -> Selection:
    """Move / resize a selection so its bounding box becomes new_rect. Returns the updated selection."""
    m, scale = _mapping(sel.bbox, pymupdf.Rect(new_rect).normalize())
    return _apply(doc, sel, m, scale)


def _art_box(sel: Selection) -> pymupdf.Rect:
    """Box of the line art and images only. Text labels stay upright when a figure is turned, so their
    box wanders with every turn - pivoting on it would stop `rotate` from being its own inverse."""
    box = _union([_piece_bounds(p) for p in sel.pieces if p["items"]] + [i.rect for i in sel.images])
    return box if not box.is_empty else sel.bbox


def rotate(doc: pymupdf.Document, sel: Selection, degrees: int = 90) -> Selection:
    """Turn a figure about its centre in 90° steps. Labels move with it but stay upright and readable."""
    b = _art_box(sel)
    cx, cy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
    m = pymupdf.Matrix(1, 0, 0, 1, -cx, -cy) * pymupdf.Matrix(degrees) * pymupdf.Matrix(1, 0, 0, 1, cx, cy)
    turn = {90: Image.ROTATE_270, 180: Image.ROTATE_180, 270: Image.ROTATE_90}.get(degrees % 360)
    return _apply(doc, sel, m, 1.0, (lambda im: im.transpose(turn)) if turn is not None else None)


def flip(doc: pymupdf.Document, sel: Selection, horizontal: bool = True) -> Selection:
    """Mirror a figure inside its own box. Labels are moved but not mirrored, so they stay readable."""
    b = _art_box(sel)
    if horizontal:
        m = pymupdf.Matrix(-1, 0, 0, 1, b.x0 + b.x1, 0)
        op = lambda im: im.transpose(Image.FLIP_LEFT_RIGHT)      # noqa: E731
    else:
        m = pymupdf.Matrix(1, 0, 0, -1, 0, b.y0 + b.y1)
        op = lambda im: im.transpose(Image.FLIP_TOP_BOTTOM)      # noqa: E731
    return _apply(doc, sel, m, 1.0, op)


def delete(doc: pymupdf.Document, sel: Selection):
    page = doc[sel.page]
    if sel.pieces:
        snapshot = _page_paths(page)
        _rebuild_line_art(page, snapshot, _match_selected(snapshot, sel.pieces), [])
    _remove_images_texts(page, sel)


def copy_selection(sel: Selection):
    global CLIPBOARD
    CLIPBOARD = {"pieces": copy.deepcopy(sel.pieces), "images": list(sel.images),
                 "texts": copy.deepcopy(sel.texts), "bbox": pymupdf.Rect(sel.bbox)}


def has_clipboard() -> bool:
    return bool(CLIPBOARD and (CLIPBOARD["pieces"] or CLIPBOARD["images"] or CLIPBOARD["texts"]))


def clipboard_size() -> tuple[float, float]:
    b = CLIPBOARD["bbox"] if CLIPBOARD else pymupdf.Rect()
    return b.width, b.height


def paste(doc: pymupdf.Document, page_no: int, top_left: pymupdf.Point) -> Selection | None:
    if not has_clipboard():
        return None
    box = CLIPBOARD["bbox"]
    m = pymupdf.Matrix(1, 0, 0, 1, top_left.x - box.x0, top_left.y - box.y0)
    return _place(doc, doc[page_no], CLIPBOARD["pieces"], CLIPBOARD["images"], CLIPBOARD["texts"], m, 1.0)


def render_png(doc: pymupdf.Document, sel: Selection, dpi=200) -> bytes:
    page = doc[sel.page]
    rot = page.rotation
    page.set_rotation(0)
    try:
        return page.get_pixmap(clip=sel.bbox + (-2, -2, 2, 2), dpi=dpi, alpha=False).tobytes("png")
    finally:
        page.set_rotation(rot)
