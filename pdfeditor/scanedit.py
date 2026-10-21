"""In-place editing of scanned pages.

The scan image itself is never modified. Editing a recognised block:
  * compares old and new text word by word - only changed words (and what must reflow after
    them) are touched, so every unchanged word stays original scan pixels;
  * erases changed words with a glyph-shaped patch (table rules are never erased);
  * renders the replacement *to match the scan*: an ink model (ink colour/tint, blur, paper noise)
    is fitted against the block's real scanned glyphs, and the new words are drawn as pixels with
    that model, plus an invisible text layer so they stay searchable/copyable;
  * keeps block metadata current so the block can be edited again and again.
Metadata lives in a stream referenced from the page dictionary: it survives save and undo.
"""
from __future__ import annotations

import difflib
import io
import json
import math
from dataclasses import dataclass

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFont

from .cvsafe import cv2
from . import pagecache, textops
from .fonts import resolve_font_file
from .textops import TextBlock, TextStyle

META_KEY = "PDFStudioOCR"
PATCH_KEY = "PDFStudioPatch"


# ------------------------------------------------------------------ metadata
def _meta_xref(doc, page) -> int:
    kind, val = doc.xref_get_key(page.xref, META_KEY)
    if kind == "xref":
        return int(val.split()[0])
    return 0


def store_meta(doc: pymupdf.Document, page: pymupdf.Page, meta: dict):
    xref = _meta_xref(doc, page)
    if not xref:
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        doc.xref_set_key(page.xref, META_KEY, f"{xref} 0 R")
    doc.update_stream(xref, json.dumps(meta, separators=(",", ":")).encode("utf-8"), compress=True)


def load_meta(doc: pymupdf.Document, page: pymupdf.Page) -> dict | None:
    xref = _meta_xref(doc, page)
    if not xref:
        return None
    try:
        return json.loads(doc.xref_stream(xref).decode("utf-8"))
    except Exception:
        return None


def has_meta(doc, page) -> bool:
    return bool(_meta_xref(doc, page))


# ------------------------------------------------------------------ detection
def unrotated_rect(page: pymupdf.Page) -> pymupdf.Rect:
    return (page.rect * page.derotation_matrix).normalize()


def is_patch_image(doc, xref: int) -> bool:
    if not xref:
        return False
    try:
        return doc.xref_get_key(xref, PATCH_KEY)[0] != "null"
    except Exception:
        return False


def background_images(page: pymupdf.Page, coverage=0.6) -> list[dict]:
    ur = unrotated_rect(page)
    area = abs(ur)
    out = []
    for info in pagecache.image_infos(page):
        r = pymupdf.Rect(info["bbox"])
        if abs(r & ur) >= coverage * area and info.get("width", 0) > 1:
            out.append(info)
    return out


def visible_text_chars(page: pymupdf.Page) -> int:
    n = 0
    for b in page.get_text("dict", flags=0)["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                if s.get("alpha", 255) > 0:
                    n += len(s["text"].strip())
    return n


def is_scanned_page(page: pymupdf.Page) -> bool:
    return pagecache.get(page, "scanned", lambda: _is_scanned_page(page))


def _is_scanned_page(page: pymupdf.Page) -> bool:
    if not background_images(page):
        return False
    return has_meta(page.parent, page) or visible_text_chars(page) < 40


def _legacy_patch(doc, xref: int) -> bool:
    """Patches made before they were tagged: sparse, glyph-shaped transparency."""
    try:
        kind, val = doc.xref_get_key(xref, "SMask")
        if kind != "xref":
            return False
        mask = pymupdf.Pixmap(doc, int(val.split()[0]))
        a = np.frombuffer(mask.samples, np.uint8)
        return a.size > 0 and float((a > 127).mean()) < 0.5
    except Exception:
        return False


def scan_background_xrefs(page: pymupdf.Page) -> set[int]:
    """Images that are part of the scan (the page image and our edit patches) - not user images."""
    return set(pagecache.get(page, "scan_background", lambda: _scan_background_xrefs(page)))


def _scan_background_xrefs(page: pymupdf.Page) -> set[int]:
    from .pageimages import is_user_image
    doc = page.parent
    infos = pagecache.image_infos(page)
    out = {i.get("xref", 0) for i in infos if is_patch_image(doc, i.get("xref", 0))}
    if is_scanned_page(page):
        bg = {i.get("xref", 0) for i in background_images(page)}
        out |= bg
        meta = load_meta(doc, page)
        if meta:
            # untagged patches from older versions: sparse transparency lying on recognised text.
            # Images the user inserted (tagged, or not on text) always stay editable.
            M = pymupdf.Matrix(meta["ts2u"])
            word_rects = [(pymupdf.Rect(w["r"]).quad * M).rect
                          for b in meta.get("blocks", []) for ln in b["lines"] for w in ln["words"]]
            scan_dpi = native_scan_dpi(page)
            for i in infos:
                x = i.get("xref", 0)
                if not x or x in bg or x in out or is_user_image(doc, x):
                    continue
                r = pymupdf.Rect(i["bbox"])
                if r.width <= 0 or not _legacy_patch(doc, x):
                    continue
                covered = sum(abs(r & wr) for wr in word_rects)
                # old patches were rendered at exactly the scan's resolution; inserted pictures practically never are
                density = i.get("width", 0) / r.width * 72
                at_scan_res = abs(density - scan_dpi) <= 0.04 * scan_dpi
                if covered > 0.25 * max(abs(r), 1e-6) or at_scan_res:
                    out.add(x)
    return out


def native_scan_dpi(page) -> int:
    imgs = background_images(page)
    if not imgs:
        return 300
    info = imgs[0]
    r = pymupdf.Rect(info["bbox"])
    return int(max(150, min(400, info["width"] / max(1, r.width) * 72)))


def _insert_patch(doc, page, rect_u, rgba: np.ndarray):
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG")
    xref = page.insert_image(rect_u, stream=buf.getvalue(), keep_proportion=False, overlay=True)
    try:
        doc.xref_set_key(xref, PATCH_KEY, "true")
    except Exception:
        pass
    return xref


# ------------------------------------------------------------------ blocks
def _style_of(b) -> TextStyle:
    return TextStyle(pdf_font="", family=b["family"], size=b["size"], color=tuple(b["color"]),
                     bold=b["bold"], italic=b["italic"], align=b.get("align", 0), line_height=b.get("lh", 1.2))


def blocks_for_page(doc: pymupdf.Document, page: pymupdf.Page) -> list[TextBlock]:
    meta = load_meta(doc, page)
    if not meta:
        return []
    M = pymupdf.Matrix(meta["ts2u"])
    out = []
    for b in meta.get("blocks", []):
        lines = [ln for ln in b["lines"] if ln["words"]]
        if not lines:
            continue
        rect = pymupdf.Rect()
        quads = []
        for ln in lines:
            for w in ln["words"]:
                r = pymupdf.Rect(w["r"])
                rect |= r
                quads.append(r.quad * M)
        text = "\n".join(" ".join(w["t"] for w in ln["words"]) for ln in lines)
        out.append(TextBlock(page=page.number, rect=rect, text=text, style=_style_of(b),
                             line_rects=[q.rect for q in quads],
                             first_baseline=pymupdf.Point(rect.x0, lines[0]["baseline"]),
                             matrix=M, source="scan", scan_id=b["id"], quads=quads))
    return out


# ------------------------------------------------------------------ text-space rendering helpers
def _linear(M: pymupdf.Matrix) -> pymupdf.Matrix:
    return pymupdf.Matrix(M.a, M.b, M.c, M.d, 0, 0)


def render_ts(page: pymupdf.Page, M: pymupdf.Matrix, rect_ts: pymupdf.Rect, k: float):
    """Render the page (no annotations) with text-space axes as pixel axes.
    Returns (bgr image, function TS point -> pixel (x, y))."""
    L = _linear(M)
    inv = ~L
    region_u = (pymupdf.Rect(rect_ts).quad * M).rect & unrotated_rect(page)
    m = inv * pymupdf.Matrix(k, k)
    rot = page.rotation
    page.set_rotation(0)
    try:
        pix = page.get_pixmap(matrix=m, clip=region_u, alpha=False, annots=False)
    finally:
        page.set_rotation(rot)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR) if pix.n == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    off = pymupdf.Point(M.e, M.f) * inv
    ox, oy = pix.x, pix.y

    def to_px(x, y):
        return (x + off.x) * k - ox, (y + off.y) * k - oy
    return img, to_px


@dataclass
class InkModel:
    color: np.ndarray        # BGR 0..255 - ink at full coverage
    sigma: float             # blur (px at scale k)
    noise: float             # paper grain std (grey levels)
    k: float
    gain: float = 1.0        # ink spread (>1 thicker/darker strokes than the crisp font, <1 lighter)
    hscale: float = 1.0      # extra glyph width of the scanned type relative to the matched font
    size_scale: float = 1.0  # printed size relative to the OCR size estimate

    def to_meta(self):
        return {"bgr": [round(float(c), 2) for c in self.color], "sigma": round(self.sigma, 3),
                "noise": round(self.noise, 2), "k": round(self.k, 4), "gain": round(self.gain, 3),
                "hscale": round(self.hscale, 4), "size_scale": round(self.size_scale, 4)}

    @staticmethod
    def from_meta(d, k):
        return InkModel(np.array(d["bgr"], np.float32), d["sigma"] * k / d["k"], d.get("noise", 2.0), k,
                        d.get("gain", 1.0), d.get("hscale", 1.0), d.get("size_scale", 1.0))

    def shape(self, beta: np.ndarray) -> np.ndarray:
        """Ink spread applied to blurred coverage: gain > 1 fattens strokes, gain < 1 thins them by eroding
        the soft edges - stroke cores keep full ink density either way, as in real print."""
        if self.gain >= 1.0:
            return 1.0 - np.power(np.clip(1.0 - beta, 0, 1), self.gain)
        t = (1.0 - self.gain) * 0.6
        return np.clip((beta - t) / (1.0 - t), 0, 1)


def _coverage(path: str, text: str, px: float, ss: int = 4) -> np.ndarray:
    """Antialiased coverage (0..1) of text, tightly cropped, rendered supersampled."""
    font = ImageFont.truetype(path, max(4, int(round(px * ss))))
    l, t, r, b = font.getbbox(text)
    if r - l <= 0 or b - t <= 0:
        return np.zeros((1, 1), np.float32)
    im = Image.new("L", (r - l + 2 * ss, b - t + 2 * ss), 0)
    ImageDraw.Draw(im).text((ss - l, ss - t), text, font=font, fill=255)
    a = np.array(im, np.float32) / 255.0
    ys, xs = np.nonzero(a > 0.02)
    a = a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = max(1, round(a.shape[0] / ss)), max(1, round(a.shape[1] / ss))
    return cv2.resize(a, (w, h), interpolation=cv2.INTER_AREA)


def page_ink_color(page: pymupdf.Page, M: pymupdf.Matrix, page_words: list[dict], k: float):
    """True ink colour of the page (BGR) measured at the centres of thick strokes, which reach full
    ink density even after scanner blur. Returns None when the page has too few thick strokes."""
    boxes = [pymupdf.Rect(w["r"]) for w in page_words if not w.get("v")]
    if len(boxes) < 3:
        return None
    union = pymupdf.Rect()
    for r in boxes:
        union |= r
    img, to_px = render_ts(page, M, union + (-2, -2, 2, 2), k)
    imgf = img.astype(np.float32)
    gray = imgf.mean(axis=2)
    H, W = gray.shape
    inside = np.zeros((H, W), bool)
    for r in boxes:
        x0, y0 = to_px(r.x0, r.y0)
        x1, y1 = to_px(r.x1, r.y1)
        inside[max(0, int(y0)):min(H, int(math.ceil(y1))), max(0, int(x0)):min(W, int(math.ceil(x1)))] = True
    vals = gray[inside]
    if vals.size < 100:
        return None
    p_gray = float(np.percentile(vals, 92))
    ink = inside & (gray < p_gray - 40)
    if ink.sum() < 200:
        return None
    # the darkest few percent of ink pixels are where the print reached full density
    dark = ink & (gray <= np.percentile(gray[ink], 4))
    if dark.sum() < 50:
        return None
    return np.median(imgf[dark], axis=0).astype(np.float32)


def _solve_gain(beta: list[np.ndarray], depth: float, mass_scan: float) -> float:
    """Find the ink-spread gain whose *shaped* coverage carries the scanned ink mass."""
    def mass(g):
        m = InkModel(np.zeros(3), 1, 0, 1, g)
        return sum(float(m.shape(b).sum()) for b in beta) * depth
    lo, hi = 0.4, 4.0
    if mass(hi) <= mass_scan:
        return hi
    if mass(lo) >= mass_scan:
        return lo
    for _ in range(25):
        mid = (lo + hi) / 2
        if mass(mid) < mass_scan:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def calibrate_ink(page: pymupdf.Page, M: pymupdf.Matrix, words: list[dict], font_path: str, size_pt: float,
                  k: float, page_ink: np.ndarray | None = None) -> InkModel | None:
    """Measure how the scan renders this block's ink - no glyph registration needed.

    * paper colour & grain from the bright pixels around the words
    * stroke-centre colour and stroke width from the distance transform of the ink
    * blur from edge steepness: for a blurred step edge, max gradient = |ink - paper| / (sigma * sqrt(2 pi)),
      and the centre of a stroke of width w only reaches erf(w / (2 sqrt2 sigma)) of full ink
    * ink spread (gain) so the rendered words carry the same ink mass as the scanned words
    """
    samples = [w for w in words if not w.get("v") and sum(c.isalnum() for c in w["t"]) >= 2][:10]
    if not samples:
        return None
    union = pymupdf.Rect()
    for w in samples:
        union |= pymupdf.Rect(w["r"])
    img, to_px = render_ts(page, M, union + (-4, -4, 4, 4), k)
    imgf = img.astype(np.float32)
    gray = imgf.mean(axis=2)
    H, W = gray.shape

    boxes = np.zeros((H, W), np.uint8)
    word_boxes = []
    for w in samples:
        x0, y0 = to_px(w["r"][0], w["r"][1])
        x1, y1 = to_px(w["r"][2], w["r"][3])
        bx0, by0 = max(0, int(math.floor(x0)) - 2), max(0, int(math.floor(y0)) - 2)
        bx1, by1 = min(W, int(math.ceil(x1)) + 2), min(H, int(math.ceil(y1)) + 2)
        if bx1 - bx0 < 4 or by1 - by0 < 4:
            continue
        boxes[by0:by1, bx0:bx1] = 255
        word_boxes.append((w, bx0, by0, bx1, by1))
    if not word_boxes:
        return None
    inside = boxes > 0
    vals = gray[inside]
    p_gray = float(np.percentile(vals, 92))
    paper_px = inside & (gray >= np.percentile(vals, 75))
    paper = np.median(imgf[paper_px], axis=0) if paper_px.any() else np.full(3, p_gray, np.float32)
    noise = float(gray[paper_px].std()) if paper_px.sum() > 8 else 2.0

    ink = (inside & (gray < p_gray - max(25.0, 4.0 * noise))).astype(np.uint8)
    # table rules are not glyphs
    bw = ink * 255
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, int(k * 72 * 0.3)), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(15, int(k * 72 * 0.4))))
    rules = cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk) | cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk)
    ink[cv2.dilate(rules, np.ones((3, 3), np.uint8)) > 0] = 0
    if ink.sum() < 20:
        return None

    dist = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    peaks = (dist >= cv2.dilate(dist, np.ones((3, 3), np.uint8))) & (dist >= 0.9)
    if peaks.sum() < 8:
        peaks = dist >= max(0.5, float(np.percentile(dist[ink > 0], 80)))
    width = float(2.0 * np.median(dist[peaks]))
    thick = peaks & (dist >= np.percentile(dist[peaks], 50))
    core = np.median(imgf[thick], axis=0)
    core_gray = float(core.mean())

    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3) / 8.0
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3) / 8.0
    grad = np.sqrt(gx * gx + gy * gy)
    near = cv2.dilate(ink, np.ones((3, 3), np.uint8)) > 0
    gmax = float(np.percentile(grad[near], 95)) if near.any() else 30.0

    sigma = 0.8
    reach = 1.0
    for _ in range(6):
        reach = max(0.35, math.erf(width / (2 * math.sqrt(2) * sigma)))
        depth = (p_gray - core_gray) / reach
        sigma = float(np.clip(abs(depth) / (max(gmax, 1e-3) * math.sqrt(2 * math.pi)), 0.3, 2.5))
    color = np.clip(paper + (core - paper) / reach, 0, 255).astype(np.float32)
    if page_ink is not None:
        # a printed page uses one ink: prefer the page's full-density ink when it is darker
        # (thin strokes never reach full density, so the local estimate reads too light)
        if float(page_ink.mean()) < float(color.mean()) and abs(float(page_ink.mean()) - core_gray) < 150:
            color = page_ink.copy()
    model = InkModel(color=color, sigma=sigma, noise=noise, k=k, gain=1.0)
    if not font_path:
        return model

    # --- geometry: ink extents of the scanned words vs. the font (a size error scales both ways,
    #     a wider typeface only widens)
    wr_, hr_ = [], []
    for w, bx0, by0, bx1, by1 in word_boxes:
        if sum(c.isalnum() for c in w["t"]) < 3:
            continue
        cov = _coverage(font_path, w["t"], size_pt * k)
        wink = ink[by0:by1, bx0:bx1]
        rows_ = np.nonzero(wink.any(axis=1))[0]
        cols_ = np.nonzero(wink.any(axis=0))[0]
        if len(cols_) > 3 and len(rows_) > 3 and min(cov.shape) > 3:
            wr_.append((cols_.max() - cols_.min() + 1) / cov.shape[1])
            hr_.append((rows_.max() - rows_.min() + 1) / cov.shape[0])
    if wr_:
        # conservative width (30th percentile): replacement words must never be wider than the scanned
        # ones, or they run into the next word
        wmed, hmed = float(np.percentile(wr_, 30)), float(np.median(hr_))
        if abs(wmed - hmed) < 0.05:
            ss, hs = (wmed + hmed) / 2, 1.0
        else:
            ss, hs = hmed, wmed / hmed
        model.size_scale = 1.0 if abs(ss - 1) < 0.02 else float(np.clip(ss, 0.85, 1.2))
        model.hscale = 1.0 if abs(hs - 1) < 0.02 else float(np.clip(hs, 0.85, 1.2))

    covs = []
    for w, *_ in word_boxes:
        c = _coverage(font_path, w["t"], size_pt * model.size_scale * k)
        if c.size < 4:
            continue
        if model.hscale != 1.0:
            c = cv2.resize(c, (max(1, int(round(c.shape[1] * model.hscale))), c.shape[0]), interpolation=cv2.INTER_AREA)
        covs.append(np.pad(c, 8))
    if not covs:
        return model

    # --- appearance: match ink mass and edge energy of the scanned words (both translation invariant)
    thr_g = 3.0 * max(1.0, noise)
    depth = max(1.0, p_gray - float(color.mean()))
    core_level = 0.7 * depth                   # "full-density" pixels: at least 70% of the way to the ink
    mass_scan = edge_scan = core_scan = 0.0
    for w, bx0, by0, bx1, by1 in word_boxes:
        d = p_gray - gray[by0:by1, bx0:bx1]
        mass_scan += float(d[d > 2.5 * max(1.0, noise)].sum())
        core_scan += float((d >= core_level).sum())
        gb = grad[by0:by1, bx0:bx1]
        edge_scan += float(gb[gb > thr_g].sum())
    if mass_scan <= 0:
        return model
    best = None
    for s in (0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 1.0, 1.15, 1.3, 1.5):
        betas = [cv2.GaussianBlur(c, (0, 0), s) for c in covs]
        g = _solve_gain(betas, depth, mass_scan)
        trial = InkModel(color, s, noise, k, g)
        e = core = 0.0
        for b in betas:
            im = trial.shape(b) * depth
            core += float((im >= core_level).sum())
            gx_ = cv2.Sobel(im, cv2.CV_32F, 1, 0, ksize=3) / 8.0
            gy_ = cv2.Sobel(im, cv2.CV_32F, 0, 1, ksize=3) / 8.0
            gm = np.sqrt(gx_ * gx_ + gy_ * gy_)
            e += float(gm[gm > thr_g].sum())
        # same ink mass (solved), same edge sharpness, same amount of full-density ink
        err = abs(e - edge_scan) / max(1.0, edge_scan) + abs(core - core_scan) / max(20.0, core_scan)
        if best is None or err < best[0]:
            best = (err, s, g)
    model.sigma, model.gain = float(best[1]), float(best[2])
    return model


# ------------------------------------------------------------------ erasing
def erase_scan_words(page: pymupdf.Page, quads: list[pymupdf.Quad], heights_pt: list[float]):
    """Cover scanned (or scan-rendered) words, given as unrotated-space quads, with matching paper."""
    if not quads:
        return
    doc = page.parent
    ur = unrotated_rect(page)
    region = pymupdf.Rect()
    for q in quads:
        region |= q.rect
    region = (region + (-4, -4, 4, 4)) & ur
    if region.is_empty:
        return
    dpi = max(200, native_scan_dpi(page))
    rot = page.rotation
    page.set_rotation(0)
    try:
        pix = page.get_pixmap(clip=region, dpi=dpi, alpha=False, annots=False)
    finally:
        page.set_rotation(rot)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR) if pix.n == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    kx, ky = pix.width / region.width, pix.height / region.height
    boxes = np.zeros(gray.shape, np.uint8)
    for q, h in zip(quads, heights_pt):
        pad = max(1.0, 0.2 * h)
        c = (q.ul + q.lr) * 0.5
        pts = []
        for p in (q.ul, q.ur, q.lr, q.ll):
            v = p - c
            n = max(1e-6, abs(v))
            p2 = p + v * (pad / n)
            pts.append([(p2.x - region.x0) * kx, (p2.y - region.y0) * ky])
        cv2.fillConvexPoly(boxes, np.array(pts, np.int32), 255)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, int(dpi * 0.3)), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(15, int(dpi * 0.4))))
    rules = cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk) | cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk)
    rules_d = cv2.dilate(rules, np.ones((5, 5), np.uint8))
    inside = (boxes > 0) & (rules_d == 0)
    if not inside.any():
        return
    vals = gray[inside].astype(np.float32)
    paper = float(np.percentile(vals, 85))
    bright = vals[vals >= np.percentile(vals, 60)]
    std = float(bright.std()) if bright.size > 8 else 3.0
    ink = (inside & (gray.astype(np.float32) < paper - max(10.0, 3.0 * std))).astype(np.uint8) * 255
    grow = max(2, int(round(dpi / 100)))
    mask = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1)))
    mask[rules_d > 0] = 0
    if not mask.any():
        return
    fill = cv2.inpaint(img, mask, 4, cv2.INPAINT_TELEA).astype(np.float32)
    rng = np.random.default_rng(1)
    noise = rng.normal(0, max(0.5, std * 0.7), fill.shape[:2]).astype(np.float32)
    fill = np.clip(fill + noise[..., None] * (mask[..., None] > 0), 0, 255).astype(np.uint8)
    alpha = cv2.GaussianBlur(mask, (3, 3), 0)
    rgba = np.dstack([cv2.cvtColor(fill, cv2.COLOR_BGR2RGB), alpha])
    _insert_patch(doc, page, region, rgba)


def _redact_bands(page, quads, text_mode):
    if not quads:
        return
    for q in quads:
        r = q.rect
        h = r.height
        band = pymupdf.Rect(r.x0 + 0.3, r.y0 + h * 0.3, r.x1 - 0.3, r.y1 - h * 0.3)
        page.add_redact_annot(band if not band.is_empty else r, fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=text_mode)


def _erase_words(page, M, words):
    """Remove words: pixel words (scanned or scan-rendered) get a patch + their invisible text removed;
    vector words (older edits) are redacted."""
    pixel = [w for w in words if not w.get("v")]
    vec = [w for w in words if w.get("v")]
    if pixel:
        quads = [pymupdf.Rect(w["r"]).quad * M for w in pixel]
        erase_scan_words(page, quads, [pymupdf.Rect(w["r"]).height for w in pixel])
        _redact_bands(page, quads, pymupdf.PDF_REDACT_TEXT_REMOVE_INVISIBLE)
    if vec:
        _redact_bands(page, [pymupdf.Rect(w["r"]).quad * M for w in vec], pymupdf.PDF_REDACT_TEXT_REMOVE)


# ------------------------------------------------------------------ writing (scan-matched pixels)
def _draw_words(doc, page, M, style: TextStyle, font: pymupdf.Font, font_path: str, model: InkModel,
                placements: list[tuple], use_style_color: bool) -> list[dict]:
    """placements: [(word, x0, baseline)] in text space. Draws scan-like pixels + invisible text."""
    if not placements:
        return []
    size = style.size
    k = model.k
    hs = model.hscale or 1.0
    pil = ImageFont.truetype(font_path, max(4, int(round(size * k * 4))))
    items = []
    rect = pymupdf.Rect()
    for t, x, base in placements:
        l, tp, r, b = pil.getbbox(t, anchor="ls")
        wr = pymupdf.Rect(x + hs * l / (4 * k), base + tp / (4 * k), x + hs * r / (4 * k), base + b / (4 * k))
        items.append((t, x, base, wr))
        rect |= wr
    pad = max(2.0, 3 * model.sigma / k + 1)
    rect_ts = rect + (-pad, -pad, pad, pad)
    # coverage canvas in text-space pixels (4x supersampled); drawn horizontally compressed by the
    # glyph-width ratio, then stretched back so glyph shapes widen but word origins stay put
    W = int(math.ceil(rect_ts.width * k)) + 2
    H = int(math.ceil(rect_ts.height * k)) + 2
    Wu = max(1, int(math.ceil(W * 4 / hs)))
    big = Image.new("L", (Wu, H * 4), 0)
    draw = ImageDraw.Draw(big)
    for t, x, base, _ in items:
        draw.text(((x - rect_ts.x0) * k * 4 / hs, (base - rect_ts.y0) * k * 4), t, font=pil, fill=255, anchor="ls")
    cov = cv2.resize(np.array(big, np.float32) / 255.0, (W, H), interpolation=cv2.INTER_AREA)
    beta = model.shape(cv2.GaussianBlur(cov, (0, 0), max(0.2, model.sigma)))
    # scan grain on the ink
    rng = np.random.default_rng(abs(hash((round(rect.x0, 1), round(rect.y0, 1)))) % (2 ** 32))
    grain = rng.normal(0, 1, beta.shape).astype(np.float32)
    beta = np.clip(beta * (1 + 0.05 * grain), 0, 1)

    # warp text-space pixels into unrotated page pixels
    region_u = (rect_ts.quad * M).rect
    A = pymupdf.Matrix(1 / k, 1 / k) * pymupdf.Matrix(1, 0, 0, 1, rect_ts.x0, rect_ts.y0) * M \
        * pymupdf.Matrix(1, 0, 0, 1, -region_u.x0, -region_u.y0) * pymupdf.Matrix(k, k)
    Wu, Hu = int(math.ceil(region_u.width * k)), int(math.ceil(region_u.height * k))
    if abs(A.a - 1) < 1e-6 and abs(A.d - 1) < 1e-6 and abs(A.b) < 1e-6 and abs(A.c) < 1e-6 \
            and abs(A.e) < 1e-6 and abs(A.f) < 1e-6:
        alpha = beta[:Hu, :Wu] if beta.shape[0] >= Hu and beta.shape[1] >= Wu else cv2.resize(beta, (Wu, Hu))
    else:
        cvA = np.float32([[A.a, A.c, A.e], [A.b, A.d, A.f]])
        alpha = cv2.warpAffine(beta, cvA, (Wu, Hu), flags=cv2.INTER_LINEAR, borderValue=0)
    region_u = pymupdf.Rect(region_u.x0, region_u.y0, region_u.x0 + alpha.shape[1] / k, region_u.y0 + alpha.shape[0] / k)

    if use_style_color:
        bgr = np.array([style.color[2], style.color[1], style.color[0]], np.float32) * 255
    else:
        bgr = model.color
    rgb = np.empty(alpha.shape + (3,), np.uint8)
    rgb[..., 0], rgb[..., 1], rgb[..., 2] = bgr[2], bgr[1], bgr[0]
    rgba = np.dstack([rgb, np.clip(alpha * 255, 0, 255).astype(np.uint8)])
    _insert_patch(doc, page, region_u, rgba)

    # invisible text so the words remain searchable / selectable
    helv = pymupdf.Font("helv")
    L = _linear(M)
    off = pymupdf.Point(M.e, M.f) * ~L
    tw = pymupdf.TextWriter(page.rect)
    for t, x, base, wr in items:
        tl = helv.text_length(t, fontsize=1)
        if tl > 0:
            fs = max(2.0, min(wr.width / tl, size * 1.5))
            tw.append((wr.x0 + off.x, base + off.y), t, font=helv, fontsize=fs)
    tw.write_text(page, render_mode=3, morph=(pymupdf.Point(0, 0), L))
    return [{"t": t, "r": [round(v, 2) for v in (wr.x0, wr.y0, wr.x1, wr.y1)]} for t, x, base, wr in items]


def _run_placements(font, style, tokens, x, baseline, anchor, hscale=1.0):
    size = style.size
    space = font.text_length(" ", fontsize=size) * hscale
    width = font.text_length(" ".join(tokens), fontsize=size) * hscale
    if anchor == "right":
        x -= width
    elif anchor == "center":
        x -= width / 2
    out = []
    for t in tokens:
        out.append((t, x, baseline))
        x += font.text_length(t, fontsize=size) * hscale + space
    return out


def _vector_words(doc, page, M, style, font, placements):
    """Fallback when no font file is available: plain vector text."""
    L = _linear(M)
    off = pymupdf.Point(M.e, M.f) * ~L
    tw = pymupdf.TextWriter(page.rect)
    out = []
    for t, x, base in placements:
        tw.append((x + off.x, base + off.y), t, font=font, fontsize=style.size)
        w = font.text_length(t, fontsize=style.size)
        out.append({"t": t, "r": [round(x, 2), round(base - style.size * 0.78, 2), round(x + w, 2),
                                  round(base + style.size * 0.22, 2)], "v": 1})
    before = textops.page_font_xrefs(page)
    tw.write_text(page, color=style.color, morph=(pymupdf.Point(0, 0), L))
    textops.fix_tounicode(doc, page, before)
    return out


def _numeric(tokens) -> bool:
    """Amount-like text: digits with separators / currency (e.g. '1,59,500.00', '₹ 49,500', '-12.5%')."""
    text = "".join(tokens)
    digits = sum(c.isdigit() for c in text)
    return digits >= 1 and digits >= 0.6 * len([c for c in text if not c.isspace()]) and \
        all(c.isdigit() or c in ",.-+%₹$€£()/ " for c in text)


def _style_key(st: TextStyle):
    return (st.family, round(st.size, 2), tuple(round(c, 3) for c in st.color), st.bold, st.italic, st.underline)


def replace_block(doc: pymupdf.Document, block: TextBlock, new_text: str, style: TextStyle,
                  rect: pymupdf.Rect | None = None, first_baseline: pymupdf.Point | None = None) -> str:
    """rect / first_baseline: lay the block out at a new position/size (move & resize) - always re-typesets."""
    page = doc[block.page]
    meta = load_meta(doc, page)
    mblock = next((b for b in (meta or {}).get("blocks", []) if b["id"] == block.scan_id), None)
    if mblock is None:
        return ""
    M = pymupdf.Matrix(meta["ts2u"])
    old_lines = [ln for ln in mblock["lines"] if ln["words"]]
    new_lines = new_text.split("\n")
    same_style = _style_key(style) == _style_key(block.style)
    color_changed = tuple(round(c, 3) for c in style.color) != tuple(round(c, 3) for c in block.style.color)
    align = style.align
    font, path = resolve_font_file(style.family, style.bold, style.italic, new_text or "x")
    desc = f"{style.family}{' Bold' if style.bold else ''}{' Italic' if style.italic else ''}"

    # ink model from the block's own scanned glyphs (measured before anything is erased)
    k = max(200, native_scan_dpi(page)) / 72.0
    model = None
    all_words = [w for ln in old_lines for w in ln["words"]]
    if path:
        page_ink = None
        if meta.get("page_ink"):
            page_ink = np.array(meta["page_ink"], np.float32)
        else:
            page_words = [w for b in meta["blocks"] for ln in b["lines"] for w in ln["words"]]
            page_ink = page_ink_color(page, M, page_words, k)
            if page_ink is not None:
                meta["page_ink"] = [round(float(c), 2) for c in page_ink]
        model = calibrate_ink(page, M, all_words, path, block.style.size, k, page_ink)
        if model is not None:
            mblock["ink"] = model.to_meta()
        elif mblock.get("ink"):
            model = InkModel.from_meta(mblock["ink"], k)
        if model is None:
            c = np.array([block.style.color[2], block.style.color[1], block.style.color[0]], np.float32) * 255
            model = InkModel(color=c, sigma=0.8 * k / (300 / 72), noise=2.0, k=k)

    hs = model.hscale if model is not None else 1.0
    sd = style.copy(size=style.size * (model.size_scale if model is not None else 1.0))   # printed size

    def draw(placements):
        if not placements:
            return []
        if path and model is not None:
            return _draw_words(doc, page, M, sd, font, path, model, placements, color_changed)
        return _vector_words(doc, page, M, style, font, placements)

    if rect is None and same_style and len(new_lines) == len(old_lines):
        # ---- word-level diff per line: untouched words keep their original scan pixels
        for ln, new_line in zip(old_lines, new_lines):
            old_tokens = [w["t"] for w in ln["words"]]
            new_tokens = new_line.split()
            if old_tokens == new_tokens:
                continue
            ops = [op for op in difflib.SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=False).get_opcodes()
                   if op[0] != "equal"]
            first_old, first_new = ops[0][1], ops[0][3]
            last_old, last_new = ops[-1][2], ops[-1][4]
            words = ln["words"]
            space = font.text_length(" ", fontsize=style.size)
            line_align = align
            if line_align == textops.ALIGN_LEFT and _numeric(new_tokens) and _numeric(old_tokens) and words:
                # amounts in tables are right-aligned: a longer number must grow to the left, never across
                # the cell border on its right
                old_w = words[-1]["r"][2] - words[0]["r"][0]
                new_w = font.text_length(" ".join(new_tokens), fontsize=sd.size) * hs
                if new_w > old_w + 0.5:
                    line_align = textops.ALIGN_RIGHT
            if line_align == textops.ALIGN_RIGHT:
                erase, keep, tokens = words[:last_old], words[last_old:], new_tokens[:last_new]
                right = erase[-1]["r"][2] if erase else ((keep[0]["r"][0] - space) if keep else block.rect.x1)
                _erase_words(page, M, erase)
                ln["words"] = draw(_run_placements(font, sd,tokens, right, ln["baseline"], "right", hs)) + keep
            elif line_align == textops.ALIGN_CENTER:
                cx = (words[0]["r"][0] + words[-1]["r"][2]) / 2 if words else block.rect.x0
                _erase_words(page, M, words)
                ln["words"] = draw(_run_placements(font, sd,new_tokens, cx, ln["baseline"], "center", hs))
            else:
                keep, erase, tokens = words[:first_old], words[first_old:], new_tokens[first_new:]
                if erase:
                    x = erase[0]["r"][0]
                    if keep:
                        x = max(x, keep[-1]["r"][2] + 0.25 * space)
                else:
                    x = (keep[-1]["r"][2] + space) if keep else block.rect.x0
                _erase_words(page, M, erase)
                ln["words"] = keep + draw(_run_placements(font, sd,tokens, x, ln["baseline"], "left", hs))
        mblock["lines"] = old_lines
    else:
        # ---- structural or style change: re-typeset the whole block
        _erase_words(page, M, all_words)
        new_meta_lines = []
        if new_text.strip():
            placed = []
            dummy = pymupdf.TextWriter(page.rect)
            page_ts = ((unrotated_rect(page)).quad * ~M).rect
            widest = max(font.text_length(line, fontsize=style.size) for line in new_text.split("\n"))
            box = pymupdf.Rect(rect) if rect is not None else block.rect
            base_y = (first_baseline or block.first_baseline).y
            x0 = box.x0
            if rect is not None and abs(rect.width - block.rect.width) > 0.5:
                x1 = box.x1                       # width was set by the user: wrap to it
            else:
                x1 = max(box.x1, x0 + widest + 1)
                if x1 > page_ts.x1 - 2 and style.align == textops.ALIGN_LEFT:
                    x1 = max(box.x1, page_ts.x1 - 18)
            textops.layout_text(dummy, font, new_text, x0, x1, base_y, sd.size,
                                style.line_height, style.align, collect=placed)
            by_line = {}
            for li, t, a, b, y in placed:
                by_line.setdefault(li, []).append((t, a, y))
            if hs != 1.0:          # stretch advances from each line's start by the glyph-width ratio
                for li, items in by_line.items():
                    start = items[0][1]
                    by_line[li] = [(t, start + (a - start) * hs, y) for t, a, y in items]
            for li in sorted(by_line):
                words = draw(by_line[li])
                if words:
                    new_meta_lines.append({"words": words, "baseline": round(by_line[li][0][2], 2)})
        mblock["lines"] = new_meta_lines
        mblock.update(family=style.family, size=style.size, color=list(style.color), bold=style.bold,
                      italic=style.italic, align=style.align, lh=style.line_height)
    if not any(ln["words"] for ln in mblock["lines"]):
        meta["blocks"] = [b for b in meta["blocks"] if b["id"] != block.scan_id]
    store_meta(doc, page, meta)
    return desc


def delete_block(doc, block: TextBlock):
    replace_block(doc, block, "", block.style)
