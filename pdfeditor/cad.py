"""Engineering drawing -> CAD (DXF).

Reads a drawing page the way a draughtsman would and rebuilds it as real CAD geometry:

  * PDF drawings are flattened line soup (a circle arrives as a few hundred tiny segments), so the
    segments are chained back together and each chain is split into straight runs, arcs and full
    circles by fitting them - the output holds ARC/CIRCLE/LWPOLYLINE entities, not dashes of noise.
  * line style decides the layer: thick solid = VISIBLE, thin = THIN, dashed = HIDDEN,
    long-dash-dot = CENTRE, grey = DIMENSIONS, filled = HATCH, text = TEXT.
  * the real-world scale is read from the drawing's own dimension texts ("321 mm" next to a line
    321 mm long) and only trusted when several of them agree; otherwise the sheet is written at
    paper size in millimetres and the user can set the scale.
  * scanned drawings have no vectors at all, so the page image is traced first (adaptive threshold,
    Hough lines, circle detection) and the same pipeline turns that into CAD geometry.

The result opens in AutoCAD, BricsCAD, LibreCAD, FreeCAD or any DXF reader.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pymupdf

PT_MM = 25.4 / 72.0                 # one PDF point in millimetres (paper size)
FLATTEN = 16                        # samples per bezier
STRAIGHT_TOL = 0.25                 # pt: how far a point may sit off a straight run
ARC_TOL = 0.35                      # pt: ...off a fitted arc
MIN_R, MAX_R = 0.5, 5000.0          # pt: sane arc radii

LAYERS = {                          # name: (aci colour, linetype, lineweight 1/100 mm)
    "VISIBLE": (7, "Continuous", 50),
    "THIN": (8, "Continuous", 25),
    "HIDDEN": (5, "DASHED", 25),
    "CENTRE": (1, "CENTER", 18),
    "DIMENSIONS": (3, "Continuous", 18),
    "HATCH": (8, "Continuous", 13),
    "TEXT": (2, "Continuous", 18),
}


@dataclass
class CadOptions:
    scale_mm_per_pt: float | None = None    # None -> detect from dimensions, else paper scale
    include_text: bool = True
    blocks: bool = False                    # one BLOCK per detected figure (view)
    vectorise_scans: bool = True
    dpi: int = 300
    version: str = "R2010"


@dataclass
class CadReport:
    source: str = "vector"                  # "vector" | "traced from the scan"
    scale_mm_per_pt: float = PT_MM
    scale_source: str = "paper size"        # "the drawing's dimensions" | "paper size" | "your setting"
    lines: int = 0
    polylines: int = 0
    arcs: int = 0
    circles: int = 0
    texts: int = 0
    blocks: int = 0
    width_mm: float = 0.0
    height_mm: float = 0.0

    @property
    def entities(self) -> int:
        return self.lines + self.polylines + self.arcs + self.circles + self.texts

    def describe(self) -> str:
        bits = [f"{self.lines} lines", f"{self.polylines} polylines", f"{self.arcs} arcs",
                f"{self.circles} circles"]
        if self.texts:
            bits.append(f"{self.texts} text labels")
        if self.blocks:
            bits.append(f"{self.blocks} blocks")
        return (", ".join(bits) + f" — {self.width_mm:.0f} × {self.height_mm:.0f} mm, "
                f"scale from {self.scale_source} ({self.source})")


# ============================================================================ reading the page
def _bezier(p0, p1, p2, p3, n=FLATTEN):
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((u * u * u * p0.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t * t * t * p3.x,
                    u * u * u * p0.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t * t * t * p3.y))
    return out


def _dash_kind(dashes) -> str:
    """Solid, hidden (short dashes) or centre line (long dash + dot)?"""
    if not dashes or dashes in ("[] 0", "none", "None"):
        return "solid"
    nums = [float(x) for x in re.findall(r"[\d.]+", str(dashes))]
    nums = [x for x in nums if x > 0]
    if not nums:
        return "solid"
    # a centre line alternates a long dash with a very short one; a hidden line has even dashes
    return "centre" if max(nums) > 4 * min(nums) and len(nums) >= 3 else "hidden"


def _layer_for(width, dashes, color, filled) -> str:
    kind = _dash_kind(dashes)
    if kind == "centre":
        return "CENTRE"
    if kind == "hidden":
        return "HIDDEN"
    if filled:
        return "HATCH"
    if color and len(color) == 3:
        r, g, b = color
        if abs(r - g) < 0.06 and abs(g - b) < 0.06 and 0.25 < r < 0.85:
            return "DIMENSIONS"            # greyed-out construction / dimension work
    return "VISIBLE" if (width or 0) >= 0.6 else "THIN"


def page_segments(page: pymupdf.Page) -> list:
    """Every drawn segment of the page as (a, b, layer). Curves are flattened."""
    out = []
    for path in page.get_drawings():
        layer = _layer_for(path.get("width"), path.get("dashes"), path.get("color"), path.get("fill"))
        for it in path.get("items") or ():
            k = it[0]
            if k == "l":
                out.append((pymupdf.Point(it[1]), pymupdf.Point(it[2]), layer))
            elif k == "c":
                pts = [pymupdf.Point(it[1])] + [pymupdf.Point(q) for q in
                                                _bezier(*(pymupdf.Point(x) for x in it[1:5]))]
                out += [(pts[i], pts[i + 1], layer) for i in range(len(pts) - 1)]
            elif k in ("re", "qu"):
                r = pymupdf.Rect(it[1]).quad if k == "re" else pymupdf.Quad(it[1])
                c = [r.ul, r.ur, r.lr, r.ll]
                out += [(c[i], c[(i + 1) % 4], layer) for i in range(4)]
    return [(a, b, lay) for a, b, lay in out if abs(b - a) > 1e-6]


def _chains(segments) -> list:
    """Walk the segments end to end, per layer, so a circle becomes one chain of points."""
    by_layer = defaultdict(list)
    for a, b, lay in segments:
        by_layer[lay].append((a, b))
    key = lambda p: (round(p.x, 2), round(p.y, 2))          # noqa: E731
    chains = []
    for lay, segs in by_layer.items():
        ends = defaultdict(list)
        for i, (a, b) in enumerate(segs):
            ends[key(a)].append(i)
            ends[key(b)].append(i)
        used = set()
        for i in range(len(segs)):
            if i in used:
                continue
            used.add(i)
            a, b = segs[i]
            pts = [a, b]
            for forward in (True, False):
                while True:
                    end = pts[-1] if forward else pts[0]
                    nxt = [j for j in ends[key(end)] if j not in used]
                    if len(nxt) != 1:                        # a junction: stop, it is a new chain
                        break
                    j = nxt[0]
                    used.add(j)
                    q = segs[j][1] if key(segs[j][0]) == key(end) else segs[j][0]
                    pts.append(q) if forward else pts.insert(0, q)
            chains.append((pts, lay))
    return chains


# ============================================================================ fitting
def _fit_circle(P):
    A = np.c_[2 * P, np.ones(len(P))]
    try:
        sol, *_ = np.linalg.lstsq(A, (P ** 2).sum(1), rcond=None)
    except np.linalg.LinAlgError:
        return None
    cx, cy = float(sol[0]), float(sol[1])
    r2 = float(sol[2]) + cx * cx + cy * cy
    if r2 <= 0:
        return None
    r = math.sqrt(r2)
    res = float(np.abs(np.hypot(P[:, 0] - cx, P[:, 1] - cy) - r).max())
    return cx, cy, r, res


def _straight_to(P, i, tol):
    j = i + 1
    while j + 1 < len(P):
        v = P[j + 1] - P[i]
        L = float(np.hypot(*v))
        if L < 1e-9:
            break
        n = np.array([-v[1] / L, v[0] / L])
        if float(np.abs((P[i:j + 2] - P[i]) @ n).max()) > tol:
            break
        j += 1
    return j


def _arc_to(P, i, tol):
    best = None
    j = i + 3
    while j < len(P):
        f = _fit_circle(P[i:j + 1])
        if f is None or f[3] > tol or not (MIN_R <= f[2] <= MAX_R):
            break
        best = (j, f)
        j += 1
    return best


def _turns(P):
    """Signed turn at each interior point of a polyline, in radians."""
    d = np.diff(P, axis=0)
    ang = np.arctan2(d[:, 1], d[:, 0])
    t = np.diff(ang)
    return (t + math.pi) % (2 * math.pi) - math.pi


def _arc_sane(P, a0: float, a1: float) -> bool:
    """Is this run really an arc? A least-squares circle through nearly straight points has a huge
    radius and a tiny residual, so it passes the fit but bows away from the drawing. An arc must
    bulge off its chord, curve one way only, and sweep as far as the points actually turn."""
    chord = float(np.hypot(*(P[-1] - P[0])))
    if chord > 1e-6:
        v = P[-1] - P[0]
        n = np.array([-v[1], v[0]]) / chord
        if float(np.abs((P - P[0]) @ n).max()) < max(STRAIGHT_TOL, 0.01 * chord):
            return False                         # straight enough: it is a line
    t = _turns(P)
    if len(t) and t.min() < 0 < t.max():
        return False                             # an S-curve is not one arc
    sweep = math.radians((a1 - a0) % 360.0 or 360.0)
    return abs(abs(float(t.sum())) - sweep) <= math.radians(25.0)


def _arc_angles(c, p0, pm, p1):
    """DXF arcs run counter-clockwise: order the ends so the sweep passes through the middle point."""
    ang = lambda p: math.degrees(math.atan2(p[1] - c[1], p[0] - c[0])) % 360.0    # noqa: E731
    a0, am, a1 = ang(p0), ang(pm), ang(p1)
    return (a0, a1) if ((am - a0) % 360.0) <= ((a1 - a0) % 360.0) else (a1, a0)


def _max_dist_to_polyline(S, P) -> float:
    """Farthest any point of S sits from the polyline through P."""
    A, B = P[:-1], P[1:]
    AB = B - A
    L2 = (AB ** 2).sum(1)
    L2[L2 == 0] = 1e-12
    t = np.clip(((S[:, None, :] - A[None]) * AB[None]).sum(2) / L2[None], 0.0, 1.0)
    proj = A[None] + t[:, :, None] * AB[None]
    return float(np.hypot(*(S[:, None, :] - proj).transpose(2, 0, 1)).min(axis=1).max())


def _arc_matches(P, c, r, a0, a1, tol=0.5, step=0.3) -> bool:
    """Check a finished arc against the points it claims to replace, both ways: every point must sit on
    the arc, and the whole arc must sit on the run. The circle fit only measures distance from the
    centre, so on its own it happily accepts an arc that sweeps the wrong part of the circle."""
    sweep = (a1 - a0) % 360.0 or 360.0
    n = max(8, int(math.radians(sweep) * r / step))
    ang = np.radians(a0 + sweep * np.arange(n + 1) / n)
    S = np.c_[c[0] + r * np.cos(ang), c[1] + r * np.sin(ang)]
    if len(P) < 2:
        return False
    if float(np.hypot(*(P[:, None, :] - S[None, :, :]).transpose(2, 0, 1)).min(axis=1).max()) > tol:
        return False
    return _max_dist_to_polyline(S, P) <= tol


def primitives(points, tol=STRAIGHT_TOL, arc_tol=ARC_TOL) -> list:
    """Split one chain (already in DXF coordinates) into ('line'|'arc'|'circle', ...) primitives."""
    P = np.asarray([(float(p[0]), float(p[1])) for p in points], float)
    if len(P) < 2:
        return []
    closed = bool(np.hypot(*(P[0] - P[-1])) < 0.05)
    if closed and len(P) >= 8:                      # a whole circle in one go?
        f = _fit_circle(P)
        t = _turns(P)
        if f and f[3] <= arc_tol and MIN_R <= f[2] <= MAX_R and not (len(t) and t.min() < 0 < t.max()) \
                and abs(abs(float(t.sum())) - 2 * math.pi) <= math.radians(40.0) \
                and _arc_matches(P, (f[0], f[1]), f[2], 0.0, 360.0):
            return [("circle", (f[0], f[1]), f[2])]
    out, i = [], 0
    while i < len(P) - 1:
        js = _straight_to(P, i, tol)
        arc = _arc_to(P, i, arc_tol)
        ja = arc[0] if arc else i
        take_arc = False
        if arc and (ja - i) >= 3 and ja > js:
            cx, cy, r, _ = arc[1]
            a0, a1 = _arc_angles((cx, cy), P[i], P[(i + ja) // 2], P[ja])
            take_arc = (_arc_sane(P[i:ja + 1], a0, a1) and
                        _arc_matches(P[i:ja + 1], (cx, cy), r, a0, a1))
        if take_arc:
            out.append(("arc", (cx, cy), r, a0, a1))
            i = ja
        else:
            out.append(("line", tuple(P[i]), tuple(P[js]), P[i:js + 1]))
            i = js
    return out


# ============================================================================ scale
_DIM = re.compile(r"^[ØøR⌀]?\s*(\d+(?:\.\d+)?)\s*(mm|MM)?$")


def detect_scale(page: pymupdf.Page, segments) -> tuple[float | None, list]:
    """Read the drawing's own dimension texts: a number sitting on a line whose length it states gives
    millimetres per point. Only trusted when several dimensions agree - numbers in a parts table would
    otherwise 'measure' the table rules."""
    longs = [(a, b) for a, b, _ in segments if abs(b - a) > 15]
    if not longs:
        return None, []
    found = []
    for blk in page.get_text("dict")["blocks"]:
        if blk["type"] != 0:
            continue
        for line in blk["lines"]:
            dx, dy = line.get("dir", (1.0, 0.0))
            for span in line["spans"]:
                m = _DIM.fullmatch(span["text"].strip())
                if not m:
                    continue
                value = float(m.group(1))
                if value <= 0:
                    continue
                r = pymupdf.Rect(span["bbox"])
                c = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
                for a, b in longs:
                    seg = b - a
                    L = abs(seg)
                    cos = abs((seg.x * dx + seg.y * dy) / L)         # dimension text runs along its line
                    if cos < 0.94:
                        continue
                    mid = pymupdf.Point((a.x + b.x) / 2, (a.y + b.y) / 2)
                    if abs(mid - c) > max(12.0, r.height * 2):
                        continue
                    found.append(value / L)
                    break
    if len(found) < 2:
        return None, found
    med = float(np.median(found))
    agree = [x for x in found if abs(x - med) <= 0.04 * med]
    if len(agree) < 2 or len(agree) < len(found) / 2:
        return None, found
    return float(np.median(agree)), found


# ============================================================================ tracing a scan
def _raster_segments(page: pymupdf.Page, dpi: int) -> list:
    """Trace a scanned drawing: threshold the page image, find its straight strokes and circles."""
    from .cvsafe import cv2
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    grey = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if pix.n >= 3 else img[:, :, 0]
    bw = cv2.adaptiveThreshold(grey, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15)
    k = 72.0 / dpi
    raw = cv2.HoughLinesP(bw, 1, np.pi / 720, threshold=int(0.10 * dpi), minLineLength=int(0.06 * dpi),
                          maxLineGap=max(2, int(0.012 * dpi)))
    segs = []
    if raw is not None:
        for x1, y1, x2, y2 in raw[:, 0, :]:
            segs.append((pymupdf.Point(x1 * k, y1 * k), pymupdf.Point(x2 * k, y2 * k), "VISIBLE"))
    return _merge_collinear(segs)


def _merge_collinear(segs, ang_tol=2.0, off_tol=1.0, gap=2.0) -> list:
    """Hough reports the same stroke several times: fuse the pieces that lie on one straight line.

    Each segment is bucketed by its angle and its distance from the origin, then the pieces in a
    bucket are merged along their shared direction whenever they overlap or nearly touch."""
    buckets = defaultdict(list)
    for a, b, lay in segs:
        v = b - a
        ang = math.degrees(math.atan2(v.y, v.x)) % 180.0
        rad = math.radians(ang)
        nx, ny = -math.sin(rad), math.cos(rad)
        buckets[(lay, int(round(ang / ang_tol)))].append((a, b, a.x * nx + a.y * ny))
    out = []
    for (lay, angle_bucket), group in buckets.items():
        rad = math.radians(angle_bucket * ang_tol)
        dx, dy = math.cos(rad), math.sin(rad)
        by_offset = defaultdict(list)
        for a, b, off in group:
            by_offset[int(round(off / max(off_tol, 0.1)))].append((a, b, off))
        for _, line_group in by_offset.items():
            runs = []
            for a, b, off in line_group:
                t0, t1 = a.x * dx + a.y * dy, b.x * dx + b.y * dy
                runs.append((min(t0, t1), max(t0, t1), off))
            runs.sort()
            cur = list(runs[0])
            merged = []
            for t0, t1, off in runs[1:]:
                if t0 <= cur[1] + gap:
                    cur[1] = max(cur[1], t1)
                else:
                    merged.append(tuple(cur))
                    cur = [t0, t1, off]
            merged.append(tuple(cur))
            for t0, t1, off in merged:
                at = lambda t: pymupdf.Point(dx * t - dy * off, dy * t + dx * off)    # noqa: E731
                out.append((at(t0), at(t1), lay))
    return out


# ============================================================================ writing the DXF
def is_drawing(page: pymupdf.Page) -> bool:
    """Does this page look like an engineering drawing rather than a text document?"""
    segs = sum(len(p.get("items") or ()) for p in page.get_drawings())
    words = len(page.get_text("words"))
    return segs >= 150 and segs > words


def analyse(doc: pymupdf.Document, page_no: int) -> dict:
    """Quick facts for the export dialog."""
    from . import scanedit
    page = doc[page_no]
    segs = page_segments(page)
    scanned = scanedit.is_scanned_page(page)
    scale, samples = detect_scale(page, segs) if segs else (None, [])
    return {"segments": len(segs), "texts": len(page.get_text("words")), "scanned": scanned,
            "drawing": is_drawing(page) or scanned, "scale_mm_per_pt": scale,
            "dimension_samples": len(samples)}


def export_dxf(doc: pymupdf.Document, page_no: int, path: str, opts: CadOptions | None = None) -> CadReport:
    """Convert one drawing page into a DXF file. Returns what was written."""
    import ezdxf
    from ezdxf import units
    from . import scanedit

    opts = opts or CadOptions()
    page = doc[page_no]
    rep = CadReport()

    segs = page_segments(page)
    if len(segs) < 40 and opts.vectorise_scans and scanedit.is_scanned_page(page):
        segs = _raster_segments(page, opts.dpi)
        rep.source = "traced from the scan"

    if opts.scale_mm_per_pt:
        scale = opts.scale_mm_per_pt
        rep.scale_source = "paper size" if abs(scale - PT_MM) < 1e-9 else "your setting"
    else:
        found, _ = detect_scale(page, segs)
        if found:
            scale, rep.scale_source = found, "the drawing's dimensions"
        else:
            scale, rep.scale_source = PT_MM, "paper size"
    rep.scale_mm_per_pt = scale

    pr = (page.rect * page.derotation_matrix).normalize()
    H = pr.height
    to_dxf = lambda p: ((p.x - pr.x0) * scale, (H - (p.y - pr.y0)) * scale)      # noqa: E731
    rep.width_mm, rep.height_mm = pr.width * scale, H * scale

    cad = ezdxf.new(opts.version, setup=True)
    cad.units = units.MM
    for name, (color, ltype, lw) in LAYERS.items():
        if name not in cad.layers:
            cad.layers.add(name, color=color, linetype=ltype, lineweight=lw)
    msp = cad.modelspace()

    # one BLOCK per detected figure (view), when asked for
    owner_of = {}
    if opts.blocks:
        try:
            from . import objects
            idx = objects.page_figures(doc, page_no)
            for n, fid in enumerate(idx.all_figures(), 1):
                blk = cad.blocks.new(name=f"VIEW_{n}")
                msp.add_blockref(f"VIEW_{n}", (0, 0))
                rep.blocks += 1
                for _, _, it, _ in idx.figure_items(fid):
                    for q in objects._item_points(it):
                        owner_of[(round(q.x, 1), round(q.y, 1))] = blk
        except Exception:
            owner_of = {}

    def space_for(pts):
        if not owner_of:
            return msp
        for p in pts:
            blk = owner_of.get((round(p.x, 1), round(p.y, 1)))
            if blk is not None:
                return blk
        return msp

    for pts, layer in _chains(segs):
        where = space_for(pts)
        attribs = {"layer": layer}
        for prim in primitives([to_dxf(p) for p in pts]):
            if prim[0] == "circle":
                where.add_circle(prim[1], prim[2], dxfattribs=attribs)
                rep.circles += 1
            elif prim[0] == "arc":
                where.add_arc(center=prim[1], radius=prim[2], start_angle=prim[3], end_angle=prim[4],
                              dxfattribs=attribs)
                rep.arcs += 1
            else:
                run = prim[3]
                if len(run) > 2:
                    where.add_lwpolyline([tuple(q) for q in run], dxfattribs=attribs)
                    rep.polylines += 1
                else:
                    where.add_line(prim[1], prim[2], dxfattribs=attribs)
                    rep.lines += 1

    if opts.include_text:
        for blk in page.get_text("dict")["blocks"]:
            if blk["type"] != 0:
                continue
            for line in blk["lines"]:
                dx, dy = line.get("dir", (1.0, 0.0))
                rot = math.degrees(math.atan2(-dy, dx))          # PDF y runs down, DXF y runs up
                for span in line["spans"]:
                    text = span["text"].strip()
                    if not text:
                        continue
                    mt = msp.add_mtext(text, dxfattribs={"layer": "TEXT",
                                                         "char_height": max(0.5, span["size"] * scale)})
                    mt.set_location(to_dxf(pymupdf.Point(span["origin"])), rotation=rot)
                    rep.texts += 1

    cad.saveas(path)
    return rep
