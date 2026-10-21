"""OCR engine for scanned pages (Tesseract hOCR) with typography recognition.

Pipeline per page
  1. render the page as displayed, detect orientation (OSD) and skew, straighten
  2. Tesseract hOCR -> words; strip table-rule artefacts; re-read doubtful words zoomed-in
  3. typeface of the page by glyph-shape matching; per word: weight (rendered-stroke comparison),
     slant, ink colour (matched by ink mass against a crisp rendering)
  4. split lines at column gaps / table rules / style changes, group into editable blocks
  5. per block: typeface, exact size from glyph metrics (snapped to the page's sizes),
     alignment inferred from paragraph shape or the table cell it sits in
Result: an invisible, searchable text layer plus block metadata (stored in the PDF) that
`scanedit` uses to edit text in place while leaving the rest of the scan untouched.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFont

from .fonts import FONT_INDEX

MODE_SEARCHABLE = "searchable"
MODE_EDITABLE = "editable"
OCR_DPI = 300

CANDIDATE_FAMILIES = [
    "Arial", "Times New Roman", "Calibri", "Cambria", "Georgia", "Verdana", "Tahoma",
    "Trebuchet MS", "Segoe UI", "Courier New", "Garamond", "Book Antiqua", "Century Gothic",
    "Palatino Linotype", "Bookman Old Style", "Franklin Gothic Medium", "Consolas",
    "Arial Narrow", "Century Schoolbook", "Lucida Sans", "Candara", "Constantia", "Corbel",
]
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

from .cvsafe import cv2


# ============================================================================ tesseract
def find_tesseract() -> str | None:
    from .paths import bundled_tesseract
    bundled = bundled_tesseract()
    if bundled:
        os.environ.setdefault("TESSDATA_PREFIX", str(bundled.parent / "tessdata"))
        return str(bundled)
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"), "/usr/bin/tesseract",
              "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
        if os.path.isfile(p):
            return p
    return None


def available_languages() -> list[str]:
    exe = find_tesseract()
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=20,
                             creationflags=_NO_WINDOW).stdout
        return [l.strip() for l in out.splitlines()[1:] if l.strip() and l.strip() != "osd"]
    except Exception:
        return ["eng"]


def _tess(img: np.ndarray, args: list[str], ext: str, dpi: int) -> str:
    exe = find_tesseract()
    if not exe:
        raise RuntimeError("Tesseract OCR is not installed. Install it from "
                           "https://github.com/UB-Mannheim/tesseract/wiki and restart.")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "page.png"
        cv2.imwrite(str(src), img)
        base = Path(td) / "out"
        cmd = [exe, str(src), str(base), "--dpi", str(dpi), *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, creationflags=_NO_WINDOW)
        out = base.with_suffix(ext)
        if not out.exists():
            if ext == ".osd":
                return ""
            raise RuntimeError("Tesseract failed: " + (r.stderr or "")[-400:])
        return out.read_text("utf-8", errors="replace")


@dataclass
class Word:
    text: str
    box: tuple            # px in corrected image (x0, y0, x1, y1)
    conf: float
    line: int = 0
    ink: np.ndarray | None = None
    slant: float = 0.0
    bold: bool = False
    italic: bool = False
    color: tuple | None = None
    ibox: tuple | None = None   # tight ink bounds (px), table rules excluded


def parse_hocr(text: str) -> list[list[Word]]:
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text)
    root = ET.fromstring(text)
    lines = []
    for el in root.iter():
        if el.get("class", "") not in ("ocr_line", "ocr_header", "ocr_caption", "ocr_textfloat"):
            continue
        words = []
        for w in el.iter():
            if w.get("class") != "ocrx_word":
                continue
            t = "".join(w.itertext()).strip()
            if not t:
                continue
            props = dict(p.strip().split(" ", 1) for p in w.get("title", "").split(";") if " " in p.strip())
            box = tuple(int(v) for v in props["bbox"].split())
            words.append(Word(t, box, float(props.get("x_wconf", 0)), line=len(lines)))
        if words:
            lines.append(words)
    return lines


# ============================================================================ geometry
def detect_orientation(img: np.ndarray) -> int:
    """Clockwise rotation (0/90/180/270) needed to make the text upright."""
    h, w = img.shape[:2]
    scale = min(1.0, 1800 / max(h, w))
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else img
    try:
        osd = _tess(small, ["--psm", "0"], ".osd", 150)
    except Exception:
        return 0
    m = re.search(r"Rotate:\s*(\d+)", osd)
    c = re.search(r"Orientation confidence:\s*([\d.]+)", osd)
    rot = int(m.group(1)) if m else 0
    conf = float(c.group(1)) if c else 0.0
    if rot == 0:
        return 0
    if conf >= 3.0:
        return rot
    base = _mean_conf(small)
    turned = _mean_conf(rotate_cw(small, rot))
    return rot if turned > base + 8 else 0


def _mean_conf(img) -> float:
    try:
        tsv = _tess(img, ["--psm", "3", "tsv"], ".tsv", 150)
    except Exception:
        return 0.0
    confs = []
    for row in tsv.splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) == 12 and parts[11].strip():
            try:
                confs.append(float(parts[10]))
            except ValueError:
                pass
    return float(np.mean(confs)) if confs else 0.0


def rotate_cw(img, deg):
    return np.ascontiguousarray(np.rot90(img, k=-(deg // 90)))


def rot_matrix_px(deg, w, h) -> pymupdf.Matrix:
    """Point mapping for a clockwise image rotation of a w x h image."""
    return {0: pymupdf.Matrix(1, 0, 0, 1, 0, 0), 90: pymupdf.Matrix(0, 1, -1, 0, h, 0),
            180: pymupdf.Matrix(-1, 0, 0, -1, w, h), 270: pymupdf.Matrix(0, -1, 1, 0, 0, w)}[deg]


def estimate_skew(gray: np.ndarray) -> float:
    """Skew angle in degrees (projection-profile search)."""
    h, w = gray.shape
    scale = 1000 / w
    small = cv2.resize(gray, (1000, int(h * scale)), interpolation=cv2.INTER_AREA)
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    bw = bw.astype(np.float32)
    center = (500, bw.shape[0] / 2)

    def score(a):
        M = cv2.getRotationMatrix2D(center, a, 1.0)
        r = cv2.warpAffine(bw, M, (bw.shape[1], bw.shape[0]), flags=cv2.INTER_NEAREST)
        p = r.sum(axis=1)
        return float(np.sum((p[1:] - p[:-1]) ** 2))

    coarse = max(np.arange(-5, 5.01, 0.25), key=score)
    fine = max(np.arange(coarse - 0.25, coarse + 0.26, 0.05), key=score)
    return float(fine) if abs(fine) >= 0.1 else 0.0


def rules_masks(gray: np.ndarray, dpi: int):
    """(all rules, vertical rules) - long horizontal / vertical strokes such as table borders."""
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, int(dpi * 0.3)), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, int(dpi * 0.35))))
    v = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk)
    return cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk) | v, v


# ============================================================================ word helpers
JUNK_EDGE = "|[]{}_¦"


def clean_word(w: Word) -> bool:
    """Strip table-rule artefacts ('|Rupture', 'No.|') from OCR tokens. False if nothing is left."""
    t = w.text
    n = len(t)
    left = len(t) - len(t.lstrip(JUNK_EDGE))
    t2 = t.strip(JUNK_EDGE)
    if not t2:
        return False
    right = n - left - len(t2)
    if left or right:
        cw = (w.box[2] - w.box[0]) / max(1, n)
        w.box = (int(w.box[0] + left * cw), w.box[1], int(w.box[2] - right * cw), w.box[3])
        w.text = t2
    return w.box[2] - w.box[0] >= 2


def _alnum_len(t):
    return sum(ch.isalnum() for ch in t)


def _word_ink(gray, box):
    x0, y0, x1, y1 = box
    g = gray[y0:y1, x0:x1]
    if g.size == 0:
        return None, 0.0
    thr, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink = (g < thr) & ((thr - g.astype(np.int16)) > 12)
    return ink, float(ink.mean())


def _stroke(ink: np.ndarray) -> float:
    if ink is None or ink.sum() < 6:
        return 0.0
    d = cv2.distanceTransform(ink.astype(np.uint8), cv2.DIST_L2, 3)
    return float(2 * d[ink].mean())


def _slant(ink: np.ndarray) -> float:
    """Shear (tan) that makes vertical strokes sharpest; ~0.2 for italics."""
    if ink is None or ink.shape[0] < 10 or ink.shape[1] < ink.shape[0] * 1.2:
        return 0.0
    src = ink.astype(np.float32)
    h, w = src.shape
    best, best_s, base = 0.0, -1, None
    for s in (0.0, 0.12, 0.2, 0.28):
        M = np.float32([[1, s, -s * h / 2], [0, 1, 0]])
        sh = cv2.warpAffine(src, M, (w, h))
        sc = float(np.sum(sh.sum(axis=0) ** 2))
        if s == 0.0:
            base = sc
        if sc > best_s:
            best_s, best = sc, s
    return best if base and best_s > base * 1.10 else 0.0


def refine_words(gray: np.ndarray, words: list[Word], langs: str, dpi: int):
    """Re-read doubtful words individually, enlarged, in one Tesseract call."""
    todo = [w for w in words if w.conf < 60 and (w.box[3] - w.box[1]) >= 8 and _alnum_len(w.text) >= 1]
    if not todo:
        return
    crops = []
    for w in todo:
        h = w.box[3] - w.box[1]
        py, px = int(0.35 * h) + 2, int(0.3 * h) + 2
        x0, y0 = max(0, w.box[0] - px), max(0, w.box[1] - py)
        x1, y1 = min(gray.shape[1], w.box[2] + px), min(gray.shape[0], w.box[3] + py)
        scale = float(np.clip(48.0 / h, 1.0, 3.0))
        c = cv2.resize(gray[y0:y1, x0:x1], None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        crops.append(c)
    gap = 50
    width = max(c.shape[1] for c in crops) + 2 * gap
    height = sum(c.shape[0] + gap for c in crops) + gap
    canvas = np.full((height, width), 255, np.uint8)
    rows, y = [], gap
    for c in crops:
        canvas[y:y + c.shape[0], gap:gap + c.shape[1]] = c
        rows.append((y, y + c.shape[0]))
        y += c.shape[0] + gap
    try:
        lines = parse_hocr(_tess(canvas, ["-l", langs, "--psm", "6", "-c", "hocr_font_info=0", "hocr"], ".hocr", dpi))
    except Exception:
        return
    found = defaultdict(list)
    for ln in lines:
        for rw in ln:
            cy = (rw.box[1] + rw.box[3]) / 2
            for i, (a, b) in enumerate(rows):
                if a - gap / 2 <= cy <= b + gap / 2:
                    found[i].append(rw)
                    break
    for i, w in enumerate(todo):
        got = sorted(found.get(i, []), key=lambda r: r.box[0])
        if not got:
            continue
        text = " ".join(r.text for r in got)
        if " " not in w.text:
            text = text.replace(" ", "")
        conf = float(np.mean([r.conf for r in got]))
        text = text.strip(JUNK_EDGE)
        if text and conf > w.conf + 10 and abs(len(text) - len(w.text)) <= max(2, len(w.text) // 3):
            w.text, w.conf = text, conf


class FontMatcher:
    """Recognises typefaces by rendering candidate fonts and comparing glyph shapes."""

    def __init__(self):
        FONT_INDEX.ensure()
        self.variants = {}      # (family, bold, italic) -> path
        for fam in CANDIDATE_FAMILIES:
            for bold in (False, True):
                for italic in (False, True):
                    e = FONT_INDEX.find(fam, bold, italic)
                    if e and e.bold == bold and e.italic == italic and e.path.lower().endswith((".ttf", ".otf")):
                        self.variants[(e.family, bold, italic)] = e.path
        self.families = sorted({k[0] for k in self.variants})
        self._pil = {}

    def path(self, family, bold, italic):
        for key in ((family, bold, italic), (family, bold, False), (family, False, italic), (family, False, False)):
            if key in self.variants:
                return self.variants[key]
        e = FONT_INDEX.find(family, bold, italic) or FONT_INDEX.find("Arial", bold, italic)
        return e.path if e else ""

    def has_bold(self, family):
        return (family, True, False) in self.variants

    def pil(self, path, px):
        px = int(max(4, px))
        key = (path, px)
        if key not in self._pil:
            self._pil[key] = ImageFont.truetype(path, px)
        return self._pil[key]

    @staticmethod
    def _tight(m):
        ys, xs = np.nonzero(m)
        if len(xs) == 0:
            return None
        return m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    def render(self, path, text, px) -> np.ndarray:
        """Antialiased coverage (0..1) of text at px size, tightly cropped."""
        font = self.pil(path, px)
        l, t, r, b = font.getbbox(text)
        if r - l <= 0 or b - t <= 0:
            return np.zeros((1, 1), np.float32)
        im = Image.new("L", (r - l + 4, b - t + 4), 0)
        ImageDraw.Draw(im).text((2 - l, 2 - t), text, font=font, fill=255)
        return np.array(im, np.float32) / 255.0

    def score(self, ink, text, path) -> float:
        target = self._tight(ink)
        if target is None or target.shape[0] < 6 or target.shape[1] < 6:
            return -1.0
        th, tw = target.shape
        rend = self._tight(self.render(path, text, th * 1.2) > 0.5)
        if rend is None:
            return -1.0
        ar_t, ar_r = tw / th, rend.shape[1] / rend.shape[0]
        ar = min(ar_t, ar_r) / max(ar_t, ar_r)
        rr = cv2.resize(rend.astype(np.uint8) * 255, (tw, th), interpolation=cv2.INTER_AREA) > 127
        tg = target > 0
        k = np.ones((3, 3), np.uint8)
        inter = (tg & (cv2.dilate(rr.astype(np.uint8), k) > 0)).sum() + (rr & (cv2.dilate(tg.astype(np.uint8), k) > 0)).sum()
        iou = inter / max(1, tg.sum() + rr.sum())
        dens = abs(tg.mean() - rr.mean()) / max(0.05, tg.mean())
        return iou * 0.55 + ar * 0.45 - dens * 0.25

    def rank_families(self, samples, bold=False, italic=False, families=None):
        totals = defaultdict(float)
        for ink, text in samples:
            for fam in families or self.families:
                totals[fam] += max(0.0, self.score(ink, text, self.path(fam, bold, italic)))
        n = max(1, len(samples))
        return sorted(((v / n, f) for f, v in totals.items()), reverse=True)

    def metrics(self, path, text):
        """Ink bbox of `text` relative to the baseline, in em units: (left, top, right, bottom)."""
        f = self.pil(path, 200)
        l, t, r, b = f.getbbox(text, anchor="ls")
        return l / 200, t / 200, r / 200, b / 200

    def size_px(self, path, word: Word) -> float:
        l, t, r, b = self.metrics(path, word.text)
        box = word.ibox or word.box
        h = box[3] - box[1]
        return h / (b - t) if b - t > 0.05 else h / 0.72

    def rendered_stroke(self, path, text, px, blur) -> float:
        cov = self.render(path, text, px)
        if blur > 0:
            cov = cv2.GaussianBlur(cov, (0, 0), blur)
        g = (255 - cov * 255).astype(np.uint8)
        if g.size < 16:
            return 0.0
        thr, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return _stroke(g < thr)


# ============================================================================ grouping
@dataclass
class Segment:
    words: list
    box: tuple = (0, 0, 0, 0)
    bold: bool = False
    italic: bool = False

    def update(self):
        xs0, ys0, xs1, ys1 = zip(*(w.box for w in self.words))
        self.box = (min(xs0), min(ys0), max(xs1), max(ys1))

    @property
    def text(self):
        return " ".join(w.text for w in self.words)

    @property
    def height(self):
        return float(np.median([w.box[3] - w.box[1] for w in self.words]))


def _smooth_styles(words):
    """Short tokens (':', '-', '1') inherit the style of their neighbours."""
    for i, w in enumerate(words):
        if _alnum_len(w.text) >= 3:
            continue
        ref = None
        for j in list(range(i - 1, -1, -1)) + list(range(i + 1, len(words))):
            if _alnum_len(words[j].text) >= 3:
                ref = words[j]
                break
        if ref is not None:
            w.bold, w.italic = ref.bold, ref.italic


def _vertical_rule_between(rules, a: Word, b: Word) -> bool:
    x0, x1 = (a.box[0] + a.box[2]) // 2, (b.box[0] + b.box[2]) // 2
    if x1 <= x0:
        return False
    y0, y1 = max(a.box[1], b.box[1]), min(a.box[3], b.box[3])
    if y1 - y0 < 4:
        y0, y1 = min(a.box[1], b.box[1]), max(a.box[3], b.box[3])
    band = rules[y0:y1, x0:x1]
    return band.size > 0 and bool((band.sum(axis=0) > 0.7 * 255 * (y1 - y0)).any())


def _horizontal_rule_between(rules, top: Segment, bottom: Segment) -> bool:
    y0, y1 = (top.box[1] + top.box[3]) // 2, (bottom.box[1] + bottom.box[3]) // 2
    if y1 <= y0:
        return False
    x0, x1 = max(top.box[0], bottom.box[0]), min(top.box[2], bottom.box[2])
    if x1 - x0 < 4:
        x0, x1 = min(top.box[0], bottom.box[0]), max(top.box[2], bottom.box[2])
    band = rules[y0:y1, x0:x1]
    return band.size > 0 and bool((band.sum(axis=1) > 0.6 * 255 * (x1 - x0)).any())


def split_segments(line: list[Word], rules) -> list[Segment]:
    words = sorted(line, key=lambda w: w.box[0])
    h = float(np.median([w.box[3] - w.box[1] for w in words]))
    segs = [Segment([words[0]])]
    for prev, w in zip(words, words[1:]):
        gap = w.box[0] - prev.box[2]
        if gap > 1.6 * h or (prev.bold, prev.italic) != (w.bold, w.italic) or _vertical_rule_between(rules, prev, w):
            segs.append(Segment([w]))
        else:
            segs[-1].words.append(w)
    for s in segs:
        s.update()
        s.bold = Counter(w.bold for w in s.words).most_common(1)[0][0]
        s.italic = Counter(w.italic for w in s.words).most_common(1)[0][0]
    return segs


def group_blocks(segments: list[Segment], rules) -> list[list[Segment]]:
    segments = sorted(segments, key=lambda s: (s.box[1], s.box[0]))
    blocks: list[list[Segment]] = []
    for s in segments:
        best = None
        for b in blocks:
            last = b[-1]
            h = max(last.height, s.height)
            if (last.bold, last.italic) != (s.bold, s.italic):
                continue
            if abs(last.height - s.height) > 0.25 * h:
                continue
            vgap = s.box[1] - last.box[3]
            if vgap < -0.3 * h or vgap > 0.9 * h:
                continue
            left_ok = abs(s.box[0] - last.box[0]) < 0.9 * h
            center_ok = abs((s.box[0] + s.box[2]) - (last.box[0] + last.box[2])) / 2 < 0.9 * h
            if not (left_ok or center_ok):
                continue
            if _horizontal_rule_between(rules, last, s):
                continue
            best = b
            break
        if best is None:
            blocks.append([s])
        else:
            best.append(s)
    return blocks


# ============================================================================ page driver
@dataclass
class PageOcr:
    page: int
    ts2u: tuple                      # matrix text space (pt) -> unrotated page space
    words: list = field(default_factory=list)       # (text, TS rect) for the invisible layer
    blocks: list = field(default_factory=list)      # metadata blocks (editable mode)
    orientation: int = 0
    skew: float = 0.0
    fonts: Counter = field(default_factory=Counter)


def _ocr_worker_init():
    # several pages are recognised at once: one thread per Tesseract, so the workers don't fight for cores
    os.environ["OMP_THREAD_LIMIT"] = "1"


def _ocr_page_task(args):
    data, page_no, langs, dpi, mode = args
    return ocr_page(data, page_no, langs=langs, dpi=dpi, mode=mode)


def ocr_workers(n_pages: int) -> int:
    """Pages to recognise at once: one per spare CPU core, at most 6, never more than there are pages."""
    return max(1, min(n_pages, (os.cpu_count() or 2) - 1, 6))


def ocr_pages_parallel(pdf_bytes: bytes, pages, *, langs="eng", dpi=OCR_DPI, mode=MODE_EDITABLE,
                       workers: int | None = None, should_stop=None):
    """Recognise several pages at once, each in its own process (PyMuPDF must not be shared between
    threads). Pages are independent, so the results are identical to recognising them one after another.
    Yields each PageOcr as soon as it is ready - not necessarily in page order."""
    import multiprocessing
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
    pages = list(pages)
    pool = ProcessPoolExecutor(max_workers=workers or ocr_workers(len(pages)),
                               mp_context=multiprocessing.get_context("spawn"), initializer=_ocr_worker_init)
    pending = {pool.submit(_ocr_page_task, (pdf_bytes, p, langs, dpi, mode)) for p in pages}
    try:
        while pending:
            if should_stop and should_stop():
                return
            done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for fut in done:
                yield fut.result()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def ocr_page(pdf_bytes: bytes, page_no: int, *, langs="eng", dpi=OCR_DPI, mode=MODE_EDITABLE,
             matcher: FontMatcher | None = None, progress=None) -> PageOcr:
    """Heavy lifting; safe to run in a worker thread (uses its own document)."""
    def prog(msg, pct):
        if progress:
            progress(msg, pct)

    with pymupdf.open("pdf", pdf_bytes) as doc:
        page = doc[page_no]
        pix = page.get_pixmap(dpi=dpi, alpha=False, annots=False)       # as displayed (rotation applied)
        disp_w, disp_h = page.rect.width, page.rect.height
        derot = pymupdf.Matrix(page.derotation_matrix)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR) if pix.n == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    H0, W0 = img.shape[:2]
    sx, sy = W0 / disp_w, H0 / disp_h

    prog("Detecting orientation…", 2)
    orient = detect_orientation(img)
    R = rot_matrix_px(orient, W0, H0)
    img = rotate_cw(img, orient)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    prog("Straightening…", 6)
    skew = estimate_skew(gray)
    D = pymupdf.Matrix(1, 0, 0, 1, 0, 0)
    if skew:
        h, w = gray.shape
        M = cv2.getRotationMatrix2D((w / 2, h / 2), skew, 1.0)
        bg = int(np.percentile(gray, 90))
        img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=(bg, bg, bg))
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        D = pymupdf.Matrix(M[0, 0], M[1, 0], M[0, 1], M[1, 1], M[0, 2], M[1, 2])
    s = (sx + sy) / 2          # px per pt
    ts2u = pymupdf.Matrix(s, s) * ~D * ~R * pymupdf.Matrix(1 / sx, 1 / sy) * derot

    prog("Recognizing text…", 12)
    lines = parse_hocr(_tess(img, ["-l", langs, "--psm", "3", "-c", "hocr_font_info=0", "hocr"], ".hocr", dpi))
    lines = [[w for w in ln if clean_word(w)] for ln in lines]
    lines = [ln for ln in lines if ln]
    res = PageOcr(page=page_no, ts2u=tuple(ts2u), orientation=orient, skew=skew)
    if not lines:
        return res

    prog("Re-reading uncertain words…", 30)
    page_h, page_w = gray.shape
    rules, vrules = rules_masks(gray, dpi)
    words = [w for ln in lines for w in ln]
    refine_words(gray, words, langs, dpi)
    for w in words:
        if w.conf >= 30 or _alnum_len(w.text) >= 3:
            res.words.append((w.text, [v / s for v in w.box]))
    if mode != MODE_EDITABLE:
        return res

    prog("Analysing typography…", 45)
    matcher = matcher or FontMatcher()
    good_lines = []
    for ln in lines:
        keep = []
        for w in ln:
            x0, y0, x1, y1 = w.box
            if (y1 - y0) > page_h * 0.08 or (x1 - x0) > page_w * 0.9:
                continue
            an = _alnum_len(w.text)
            if an == 0 and w.conf < 75:
                continue
            if w.conf < 40 and (an < 3 or an / max(1, len(w.text)) < 0.6):
                continue
            w.ink, density = _word_ink(gray, w.box)
            if w.ink is None or density > 0.6 or w.ink.sum() < 4:        # QR codes, logos, solid marks
                continue
            clean = w.ink & ~(rules[y0:y1, x0:x1] > 0)
            ys, xs = np.nonzero(clean)
            if len(xs) >= 4:
                w.ink = clean
                w.ibox = (x0 + int(xs.min()), y0 + int(ys.min()), x0 + int(xs.max()) + 1, y0 + int(ys.max()) + 1)
            else:
                w.ibox = w.box
            w.slant = _slant(w.ink) if an >= 3 else 0.0
            w.italic = w.slant >= 0.12
            keep.append(w)
        if keep:
            good_lines.append(keep)
    all_words = [w for ln in good_lines for w in ln]
    if not all_words:
        return res

    # page typeface from the longest runs of adjacent words (never across column gaps)
    runs = []
    for ln in good_lines:
        ws = sorted(ln, key=lambda w: w.box[0])
        h = float(np.median([w.box[3] - w.box[1] for w in ws]))
        cur = [ws[0]]
        for prev, w in zip(ws, ws[1:]):
            if w.box[0] - prev.box[2] > 1.2 * h or _vertical_rule_between(rules, prev, w):
                runs.append(cur)
                cur = [w]
            else:
                cur.append(w)
        runs.append(cur)
    samples = []
    for run in sorted(runs, key=lambda r: -sum(len(w.text) for w in r)):
        run = run[:6]
        if sum(_alnum_len(w.text) for w in run) >= 8 and not any(w.italic for w in run) \
                and min(w.conf for w in run) >= 80:
            samples.append(_sample(gray, run))
        if len(samples) >= 12:
            break
    page_rank = matcher.rank_families(samples) if samples else [(1.0, "Arial")]
    page_family = page_rank[0][1]
    shortlist = [f for _, f in page_rank[:4]]

    # weight: compare the scan's stroke with the same word rendered regular vs bold.
    # Word size comes from its line (robust to '/', brackets...), and the rendering blur is
    # calibrated so typical (regular) text on this scan measures like the regular rendering.
    reg_path = matcher.path(page_family, False, False)
    bold_path = matcher.path(page_family, True, False)
    word_px = {}
    for ln in good_lines:
        est = [matcher.size_px(reg_path, w) for w in ln if _alnum_len(w.text) >= 2]
        med = float(np.median(est)) if est else 0.0
        for w in ln:
            px = matcher.size_px(reg_path, w)
            word_px[id(w)] = float(np.clip(px, 0.85 * med, 1.15 * med)) if med else px
    cands = [w for w in all_words if _alnum_len(w.text) >= 3]
    calib = cands[:: max(1, len(cands) // 60)]
    blur = 1.0 * dpi / 300
    if calib and reg_path != bold_path:
        strokes = [_stroke(w.ink) for w in calib]

        def offset(bl):
            d = [sw - matcher.rendered_stroke(reg_path, w.text, word_px[id(w)], bl) for sw, w in zip(strokes, calib)]
            return abs(float(np.percentile(d, 35)))
        blur = min((0.4, 0.7, 1.0, 1.3, 1.6, 2.0), key=offset)
    for w in all_words:
        if _alnum_len(w.text) < 2 or reg_path == bold_path:
            continue
        px = word_px[id(w)]
        sw = _stroke(w.ink)
        sr = matcher.rendered_stroke(reg_path, w.text, px, blur)
        sb = matcher.rendered_stroke(bold_path, w.text, px, blur)
        if sr > 0 and sb > sr:
            w.bold = sw > (sr + sb) / 2
    for ln in good_lines:
        _smooth_styles(sorted(ln, key=lambda w: w.box[0]))

    segments = [seg for ln in good_lines for seg in split_segments(ln, rules)]
    blocks = group_blocks(segments, rules)

    prog("Matching fonts…", 65)
    described = []
    for bi, blk in enumerate(blocks):
        d = _describe_block(bi, blk, img, gray, vrules, s, matcher, page_family, shortlist)
        if d is not None:
            described.append(d)
        if progress and bi % 10 == 0:
            prog("Matching fonts…", 65 + int(30 * bi / max(1, len(blocks))))
    _snap_sizes(described)
    _unify_ink(described)
    for d in described:
        d.pop("_chars", None)
        res.fonts[(d["family"], d["bold"], d["italic"])] += 1
    res.blocks = described
    return res


def _sample(gray, words):
    x0 = min(w.box[0] for w in words)
    x1 = max(w.box[2] for w in words)
    y0 = min(w.box[1] for w in words)
    y1 = max(w.box[3] for w in words)
    g = gray[y0:y1, x0:x1]
    thr, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return (g < thr), " ".join(w.text for w in words)


def _ink_color(img, gray, w: Word, matcher, path, px):
    """Text colour whose crisp rendering has the same ink mass as the scanned word."""
    x0, y0, x1, y1 = w.box
    crop = img[y0:y1, x0:x1].astype(np.float32)
    g = gray[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    paper = np.percentile(crop.reshape(-1, 3), 92, axis=0)
    diff = np.clip(paper - crop, 0, None)
    signal = (paper.mean() - g.astype(np.float32)) > 10
    mass = diff[signal].sum(axis=0)
    coverage = matcher.render(path, w.text, px).sum()
    if coverage < 4:
        return None
    ink = np.clip(paper - mass / coverage, 0, 255)
    return ink[2] / 255, ink[1] / 255, ink[0] / 255


def _cell_alignment(vrules, blk: list[Segment], size_px: float) -> int | None:
    """Right/centre alignment inferred from the table cell a single-line block sits in."""
    x0 = min(sg.box[0] for sg in blk)
    x1 = max(sg.box[2] for sg in blk)
    y0 = min(sg.box[1] for sg in blk)
    y1 = max(sg.box[3] for sg in blk)
    band = vrules[y0:y1, :]
    if band.size == 0:
        return None
    cols = np.nonzero(band.sum(axis=0) > 0.6 * 255 * (y1 - y0))[0]
    left = cols[cols < x0]
    right = cols[cols > x1]
    if len(left) == 0 or len(right) == 0:
        return None
    dl, dr = x0 - left.max(), right.min() - x1
    if dr < 1.5 * size_px and dl > 2.5 * size_px:
        return 2
    narrow_cell = (dl + dr) < 2.5 * (x1 - x0) + 4 * size_px
    if narrow_cell and dl > 0.8 * size_px and dr > 0.8 * size_px and abs(dl - dr) < 0.6 * size_px:
        return 1
    return None


def _describe_block(bid, blk: list[Segment], img, gray, vrules, s, matcher: FontMatcher, page_family, shortlist):
    words = [w for sg in blk for w in sg.words]
    chars = sum(len(w.text) for w in words)
    alnum = sum(_alnum_len(w.text) for w in words)
    conf = sum(w.conf * len(w.text) for w in words) / max(1, chars)
    ratio = alnum / max(1, chars)
    # drop only what looks like noise; codes such as 'SI/POR/001' legitimately get low confidence
    if alnum == 0 or (alnum <= 2 and conf < 70) or (conf < 45 and ratio < 0.6) or (conf < 20 and alnum < 5):
        return None
    bold = Counter(sg.bold for sg in blk).most_common(1)[0][0]
    italic = Counter(sg.italic for sg in blk).most_common(1)[0][0]
    longest = max(blk, key=lambda sg: len(sg.text))
    family = page_family
    if len(longest.text) >= 5:
        sample = [_sample(gray, longest.words[:8])]
        ranked = matcher.rank_families(sample, bold, italic, shortlist)
        page_score = next((sc for sc, f in ranked if f == page_family), 0.0)
        if ranked and ranked[0][0] > page_score + 0.06:
            family = ranked[0][1]
    path = matcher.path(family, bold, italic)

    # size from exact glyph ink extents: per-word height and width, plus whole-segment width
    # (which includes the real inter-word spacing and is the most reliable measure)
    sizes = []
    for w in words:
        box = w.ibox or w.box
        l, t, r, b = matcher.metrics(path, w.text)
        hh, ww = (box[3] - box[1]) / s, (box[2] - box[0]) / s
        if b - t > 0.05:
            sizes.append(hh / (b - t))
        if r - l > 0.05 and _alnum_len(w.text) >= 3:
            sizes.append(ww / (r - l))
    for sg in blk:
        if len(sg.words) >= 2:
            first, last = sg.words[0], sg.words[-1]
            span = ((last.ibox or last.box)[2] - (first.ibox or first.box)[0]) / s
            l, t, r, b = matcher.metrics(path, sg.text)
            if r - l > 0.2:
                sizes.extend([span / (r - l)] * 3)
    size = float(np.median(sizes)) if sizes else blk[0].height / s
    size = max(3.0, round(size * 2) / 2)

    colors = []
    for w in words:
        if _alnum_len(w.text) >= 2:
            c = _ink_color(img, gray, w, matcher, path, size * s)
            if c is not None:
                colors.extend([c] * len(w.text))
    color = np.median(colors, axis=0) if colors else np.array([0.1, 0.1, 0.1])
    if float(np.ptp(color)) < 0.08:            # neutral ink -> pure grey
        color = np.full(3, float(color.mean()))

    lines_meta, baselines = [], []
    for sg in blk:
        bl = []
        for w in sg.words:
            _, _, _, b = matcher.metrics(path, w.text)
            bl.append((w.ibox or w.box)[3] / s - b * size)
        baseline = float(np.median(bl))
        baselines.append(baseline)
        lines_meta.append({"words": [{"t": w.text, "r": [round(v / s, 2) for v in (w.ibox or w.box)]} for w in sg.words],
                           "baseline": round(baseline, 2)})
    lh = 1.2
    if len(baselines) > 1:
        gaps = [b2 - b1 for b1, b2 in zip(baselines, baselines[1:]) if b2 > b1]
        if gaps:
            lh = max(0.9, min(2.5, float(np.median(gaps)) / size))
    lefts = [sg.box[0] / s for sg in blk]
    rights = [sg.box[2] / s for sg in blk]
    align = 0
    if len(blk) > 1:
        tol = size * 0.6
        centers = [(a + b) / 2 for a, b in zip(lefts, rights)]
        if max(lefts) - min(lefts) > tol:
            if max(centers) - min(centers) < tol:
                align = 1
            elif max(rights) - min(rights) < tol:
                align = 2
    else:
        cell = _cell_alignment(vrules, blk, size * s)
        if cell is not None:
            align = cell
    return {"id": bid, "family": family, "bold": bool(bold), "italic": bool(italic), "size": size,
            "color": [round(float(c), 3) for c in color], "align": align, "lh": round(lh, 3),
            "lines": lines_meta, "_chars": chars}


def _unify_ink(blocks):
    """A printed page uses one black ink: give all neutral (grey/black) text the same, robust tone."""
    neutral = [(b["color"][0], b["_chars"]) for b in blocks if max(b["color"]) - min(b["color"]) < 0.08]
    if len(neutral) < 3:
        return
    vals = np.repeat([v for v, _ in neutral], [max(1, n) for _, n in neutral])
    ink = float(np.clip(np.percentile(vals, 30), 0.0, 0.35))
    for b in blocks:
        if max(b["color"]) - min(b["color"]) < 0.08:
            b["color"] = [round(ink, 3)] * 3


def _snap_sizes(blocks):
    """Pull slightly-off sizes onto the sizes the page predominantly uses."""
    weight = Counter()
    for b in blocks:
        weight[(b["size"], b["bold"])] += b["_chars"]
    for b in blocks:
        own = weight[(b["size"], b["bold"])]
        cands = [(sz, bd) for (sz, bd) in weight if bd == b["bold"] and abs(sz - b["size"]) <= 0.13 * b["size"]]
        if cands:
            best = max(cands, key=lambda k: weight[k])
            if weight[best] >= 2 * own:
                b["size"] = best[0]


# ============================================================================ apply
def apply_ocr(doc: pymupdf.Document, res: PageOcr, mode: str):
    """Write OCR results into the live document (main thread)."""
    from . import scanedit
    page = doc[res.page]
    urect = scanedit.unrotated_rect(page)
    # drop any previous invisible text layer (e.g. from the scanner) - visible text is untouched
    page.add_redact_annot(urect, fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE_INVISIBLE)
    M = pymupdf.Matrix(res.ts2u)
    L = pymupdf.Matrix(M.a, M.b, M.c, M.d, 0, 0)
    off = pymupdf.Point(M.e, M.f) * ~L
    font = pymupdf.Font("helv")
    tw = pymupdf.TextWriter(page.rect)
    n = 0
    for text, (x0, y0, x1, y1) in res.words:
        h = y1 - y0
        tl = font.text_length(text, fontsize=1)
        if tl <= 0 or h <= 0:
            continue
        size = max(2.0, min((x1 - x0) / tl, h * 1.6))
        tw.append((x0 + off.x, y1 - h * 0.18 + off.y), text, font=font, fontsize=size)
        n += 1
    if n:
        tw.write_text(page, render_mode=3, morph=(pymupdf.Point(0, 0), L))
    if mode == MODE_EDITABLE:
        scanedit.store_meta(doc, page, {"v": 1, "ts2u": list(res.ts2u), "blocks": res.blocks,
                                        "orientation": res.orientation, "skew": res.skew})
