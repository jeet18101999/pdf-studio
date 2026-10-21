"""Native text extraction (paragraph blocks with style) and in-place text replacement.

Every block carries a matrix from its *text space* (where lines run left-to-right along +x)
to unrotated page space, so rotated pages, rotated text and skewed scans edit correctly.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

from .fonts import FONT_INDEX, color_int_to_rgb, resolve_font, FLAG_BOLD, FLAG_ITALIC

ALIGN_LEFT, ALIGN_CENTER, ALIGN_RIGHT, ALIGN_JUSTIFY = 0, 1, 2, 3
IDENTITY = pymupdf.Matrix(1, 0, 0, 1, 0, 0)


@dataclass
class TextStyle:
    pdf_font: str = ""          # font name as found in PDF ("" for new / OCR text)
    family: str = "Arial"       # installed family used for display / fallback
    size: float = 11.0
    color: tuple = (0, 0, 0)
    bold: bool = False
    italic: bool = False
    underline: bool = False
    align: int = ALIGN_LEFT
    line_height: float = 1.2    # baseline distance / size
    family_changed: bool = False  # user picked another family -> don't reuse embedded font

    def copy(self, **kw):
        d = dict(self.__dict__)
        d.update(kw)
        return TextStyle(**d)


@dataclass
class TextBlock:
    page: int
    rect: pymupdf.Rect                      # in text space
    text: str
    style: TextStyle
    line_rects: list = field(default_factory=list)   # unrotated page space, for removal
    first_baseline: pymupdf.Point | None = None       # in text space
    editable: bool = True
    mixed_styles: bool = False
    matrix: pymupdf.Matrix = field(default_factory=lambda: pymupdf.Matrix(IDENTITY))   # text space -> page
    source: str = "native"                  # "native" | "scan"
    scan_id: int | None = None
    quads: list | None = None               # scan: word quads in page space
    baselines: list = field(default_factory=list)   # text-space y of each line

    def page_quad(self) -> pymupdf.Quad:
        return pymupdf.Rect(self.rect).quad * self.matrix


@dataclass
class _Unit:
    """One run of text that belongs together on a single line (a table cell, a label, a line of a paragraph)."""
    text: str
    rect_u: pymupdf.Rect          # unrotated page space
    rect: pymupdf.Rect            # text space
    baseline: float               # text space
    origin: pymupdf.Point         # text space, first glyph
    matrix: pymupdf.Matrix
    horizontal: bool
    styles: Counter
    spans: dict

    def __post_init__(self):
        # computed once: grouping lines into paragraphs compares these thousands of times per page
        self.mkey = tuple(round(v, 3) for v in self.matrix)
        self.skey = self.styles.most_common(1)[0][0]
        self.size = self.skey[1]


_IDENTITY_KEY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _union_rects(rects) -> pymupdf.Rect:
    """Union with exactly the rules of repeated `Rect |= Rect` - empty rectangles are skipped, and if all
    of them are empty the first is kept - but on plain floats, because this runs for every glyph."""
    first = None
    x0 = y0 = x1 = y1 = 0.0
    found = False
    for r in rects:
        a, b, c, d = r[0], r[1], r[2], r[3]
        if first is None:
            first = (a, b, c, d)
        if a >= c or b >= d:
            continue
        if not found:
            x0, y0, x1, y1, found = a, b, c, d, True
            continue
        if a < x0:
            x0 = a
        if b < y0:
            y0 = b
        if c > x1:
            x1 = c
        if d > y1:
            y1 = d
    return pymupdf.Rect(x0, y0, x1, y1) if found else pymupdf.Rect(first)


def _span_key(s):
    return (s["font"], round(s["size"], 1), s["color"], s["flags"] & (FLAG_BOLD | FLAG_ITALIC))


def page_rules(page: pymupdf.Page):
    """Horizontal and vertical segments of the page's vector graphics (table borders, frames, boxes)."""
    hs, vs = [], []
    try:
        drawings = page.get_drawings()
    except Exception:
        return hs, vs
    for dr in drawings:
        for it in dr["items"]:
            if it[0] == "l":
                segs = [(it[1], it[2])]
            elif it[0] == "re":
                r = pymupdf.Rect(it[1])
                segs = [(r.tl, r.tr), (r.bl, r.br), (r.tl, r.bl), (r.tr, r.br)]
            elif it[0] == "qu":
                q = it[1]
                segs = [(q.ul, q.ur), (q.ll, q.lr), (q.ul, q.ll), (q.ur, q.lr)]
            else:
                continue
            for a, b in segs:
                if abs(a.y - b.y) < 0.8 and abs(a.x - b.x) >= 3:
                    hs.append((min(a.x, b.x), max(a.x, b.x), (a.y + b.y) / 2))
                elif abs(a.x - b.x) < 0.8 and abs(a.y - b.y) >= 3:
                    vs.append(((a.x + b.x) / 2, min(a.y, b.y), max(a.y, b.y)))
    return hs, vs


def _line_units(line, vs) -> list[_Unit]:
    """Split one extracted line into units at wide gaps or where a vertical rule runs between characters
    (the PDF text engine happily puts separate table cells, bubbles and labels on one 'line')."""
    dx, dy = line["dir"]
    identity = abs(dx - 1) < 1e-3 and abs(dy) < 1e-3
    M = pymupdf.Matrix(IDENTITY) if identity else pymupdf.Matrix(math.degrees(math.atan2(dy, dx)))
    inv = ~M
    chars = [(ch, s) for s in line["spans"] if s.get("alpha", 255) > 0 for ch in s.get("chars", [])]
    groups: list[list] = [[]]
    prev = None                                  # last non-space (char, span, text-space bbox)
    for ch, s in chars:
        if ch["c"].isspace():
            if groups[-1]:
                groups[-1].append((ch, s, None))
            continue
        if identity:                             # ordinary horizontal text: the transform is a no-op
            a, b, c, d = ch["bbox"]
            cb = (min(a, c), min(b, d), max(a, c), max(b, d))
        else:
            r = (pymupdf.Rect(ch["bbox"]).quad * inv).rect
            cb = (r.x0, r.y0, r.x1, r.y1)
        if prev is not None:
            pb = prev[2]
            gap = cb[0] - pb[2]
            size = max(s["size"], prev[1]["size"])
            split = gap > 1.1 * size
            if not split and identity and gap > 0.1 * size:
                ymid = (max(pb[1], cb[1]) + min(pb[3], cb[3])) / 2
                split = any(pb[2] - 0.4 <= x <= cb[0] + 0.4 and y0 <= ymid <= y1 for x, y0, y1 in vs)
            if split:
                groups.append([])
        groups[-1].append((ch, s, cb))
        prev = (ch, s, cb)
    units = []
    for g in groups:
        solid = [(ch, s, cb) for ch, s, cb in g if cb is not None]
        if not solid:
            continue
        text = "".join(ch["c"] for ch, _, _ in g).strip()
        rect_u = _union_rects([solid[0][0]["bbox"]] + [ch["bbox"] for ch, _, _ in solid])
        rect = _union_rects([solid[0][2]] + [cb for _, _, cb in solid])
        styles, spans = Counter(), {}
        for ch, s, cb in solid:
            k = _span_key(s)
            styles[k] += 1
            spans.setdefault(k, s)
        origin = pymupdf.Point(solid[0][0]["origin"])
        if not identity:
            origin = origin * inv
        units.append(_Unit(text, rect_u, rect, origin.y, origin, M, abs(dx) >= abs(dy), styles, spans))
    return units


def _rule_between(hs, upper: _Unit, lower: _Unit) -> bool:
    """A horizontal rule separating two stacked units (table row border)."""
    if upper.mkey != _IDENTITY_KEY:
        return False
    lo = min(upper.rect_u.x1, lower.rect_u.x1)
    hi = max(upper.rect_u.x0, lower.rect_u.x0)
    x0, x1 = (hi, lo) if lo > hi else (min(upper.rect_u.x0, lower.rect_u.x0), max(upper.rect_u.x1, lower.rect_u.x1))
    span = max(1.0, x1 - x0)
    y_top = upper.baseline + 0.1 * upper.size
    y_bot = lower.baseline - 0.55 * lower.size
    for a, b, y in hs:
        if y_top <= y <= y_bot and min(b, x1) - max(a, x0) >= 0.5 * span:
            return True
    return False


def _continues(group: list[_Unit], u: _Unit, hs) -> bool:
    last = group[-1]
    if u.mkey != last.mkey or u.skey != last.skey:
        return False
    size = last.size
    gap = u.baseline - last.baseline
    if gap <= 0.6 * size or gap > 3.2 * size:
        return False                               # same row (side by side) or too far apart
    if len(group) > 1:
        own = (last.baseline - group[0].baseline) / (len(group) - 1)
        if abs(gap - own) > 0.3 * own:
            return False
    overlap = min(u.rect.x1, last.rect.x1) - max(u.rect.x0, last.rect.x0)
    if overlap < 0.25 * min(u.rect.width, last.rect.width):
        return False
    aligned = (abs(u.rect.x0 - last.rect.x0) <= 1.5 * size or abs(u.rect.x1 - last.rect.x1) <= 1.5 * size
               or abs((u.rect.x0 + u.rect.x1) - (last.rect.x0 + last.rect.x1)) / 2 <= 1.5 * size)
    if not aligned:
        return False
    return not _rule_between(hs, last, u)


def extract_blocks(page: pymupdf.Page) -> list[TextBlock]:
    """Editable text blocks: every table cell, label and bubble on its own; lines of a paragraph together."""
    FONT_INDEX.ensure()
    flags = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    d = page.get_text("rawdict", flags=flags)
    hs, vs = page_rules(page)
    units: list[_Unit] = []
    for b in d["blocks"]:
        if b.get("type", 0) != 0:
            continue
        for ln in b["lines"]:
            units.extend(_line_units(ln, vs))

    groups: list[list[_Unit]] = []
    for u in sorted(units, key=lambda x: (x.mkey, round(x.baseline, 1), x.rect.x0)):
        best, best_gap = None, None
        for g in reversed(groups[-60:]):
            if _continues(g, u, hs):
                gap = u.baseline - g[-1].baseline
                if best is None or gap < best_gap:
                    best, best_gap = g, gap
        if best is None:
            groups.append([u])
        else:
            best.append(u)

    blocks = []
    for g in groups:
        styles, spans = Counter(), {}
        for u in g:
            styles.update(u.styles)
            spans.update({k: v for k, v in u.spans.items() if k not in spans})
        key, _ = styles.most_common(1)[0]
        s = spans[key]
        entry, bold, italic = FONT_INDEX.match_pdf_font(s["font"], s["flags"])
        size = s["size"]
        baselines = [u.baseline for u in g]
        lh = 1.2
        if len(baselines) > 1:
            gaps = sorted(b2 - b1 for b1, b2 in zip(baselines, baselines[1:]) if b2 > b1)
            if gaps:
                lh = max(0.8, min(3.2, gaps[len(gaps) // 2] / size))
        rect = pymupdf.Rect(g[0].rect)
        for u in g[1:]:
            rect |= u.rect
        style = TextStyle(pdf_font=s["font"], family=entry.family if entry else "Arial", size=size,
                          color=color_int_to_rgb(s["color"]), bold=bold, italic=italic,
                          align=_guess_align([u.rect.x0 for u in g], [u.rect.x1 for u in g]), line_height=lh)
        blocks.append(TextBlock(page=page.number, rect=rect, text="\n".join(u.text for u in g), style=style,
                                line_rects=[(u.rect_u, u.horizontal) for u in g], first_baseline=g[0].origin,
                                mixed_styles=len(styles) > 1, matrix=pymupdf.Matrix(g[0].matrix),
                                baselines=baselines))
    return blocks


def _guess_align(lefts, rights):
    if len(lefts) < 2:
        return ALIGN_LEFT
    tol = 2.0
    left_same = max(lefts) - min(lefts) < tol
    right_same = (max(rights[:-1]) - min(rights[:-1]) < tol) if len(rights) > 2 else (max(rights) - min(rights) < tol)
    centers = [(a + b) / 2 for a, b in zip(lefts, rights)]
    if left_same and right_same and len(lefts) > 2:
        return ALIGN_JUSTIFY
    if left_same:
        return ALIGN_LEFT
    if max(centers) - min(centers) < tol:
        return ALIGN_CENTER
    if max(rights) - min(rights) < tol:
        return ALIGN_RIGHT
    return ALIGN_LEFT


# --------------------------------------------------------------------------- writing
def remove_text_rects(page: pymupdf.Page, rects, text_mode=pymupdf.PDF_REDACT_TEXT_REMOVE):
    """Remove text glyphs inside rects without touching images or vector graphics.

    rects: Rect, or (Rect, horizontal) tuples - only the central band across the text
    direction is used so neighbouring lines are never clipped."""
    for item in rects:
        r, horizontal = (item if isinstance(item, tuple) else (item, True))
        r = pymupdf.Rect(r)
        if horizontal:
            h = r.height
            band = pymupdf.Rect(r.x0 + 0.2, r.y0 + h * 0.3, r.x1 - 0.2, r.y1 - h * 0.3)
        else:
            w = r.width
            band = pymupdf.Rect(r.x0 + w * 0.3, r.y0 + 0.2, r.x1 - w * 0.3, r.y1 - 0.2)
        page.add_redact_annot(band if not band.is_empty else r, fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE, text=text_mode)


def page_font_xrefs(page: pymupdf.Page) -> set[int]:
    return {f[0] for f in page.get_fonts(full=True)}


_UNICODE_FIXES = {"00AD": "002D", "2010": "002D", "2011": "002D", "00A0": "0020", "2007": "0020", "202F": "0020"}
# a mapping's Unicode value is the token that follows the glyph code (bfchar) or the code range (bfrange)
_TOUNICODE_DEST = re.compile(rb"(<[0-9A-Fa-f]+>\s*)<(00[Aa][Dd]|2010|2011|00[Aa]0|2007|202[Ff])>")


def fix_tounicode(doc: pymupdf.Document, page: pymupdf.Page, before: set[int]):
    """Fonts often map one glyph to several code points ('-', soft hyphen, U+2010 hyphen; space and NBSP)
    and the generated ToUnicode CMap picks an exotic one, breaking search/copy in every PDF reader.
    Rewrite those Unicode destinations (never the glyph codes) for fonts just added to the page."""
    def repl(m):
        return m.group(1) + b"<" + _UNICODE_FIXES[m.group(2).decode().upper()].encode() + b">"

    for xref in page_font_xrefs(page) - before:
        kind, val = doc.xref_get_key(xref, "ToUnicode")
        if kind != "xref":
            continue
        tx = int(val.split()[0])
        try:
            data = doc.xref_stream(tx)
        except Exception:
            continue
        new = _TOUNICODE_DEST.sub(repl, data)
        if new != data:
            doc.update_stream(tx, new)


def _split_matrix(M: pymupdf.Matrix):
    L = pymupdf.Matrix(M.a, M.b, M.c, M.d, 0, 0)
    off = pymupdf.Point(M.e, M.f) * ~L
    is_identity = abs(M.a - 1) < 1e-9 and abs(M.d - 1) < 1e-9 and abs(M.b) < 1e-9 and abs(M.c) < 1e-9
    return off, L, is_identity


def write_text(doc: pymupdf.Document, page: pymupdf.Page, rect: pymupdf.Rect, text: str, style: TextStyle,
               first_baseline: pymupdf.Point | None = None, matrix: pymupdf.Matrix | None = None,
               collect: list | None = None, fixed_width: bool = False) -> str:
    """Write wrapped, aligned text into `rect` (text space) with the best matching font.

    collect: optional list receiving (line_index, word, x0, x1, baseline) in text space."""
    if not text.strip():
        return ""
    M = pymupdf.Matrix(matrix) if matrix is not None else pymupdf.Matrix(IDENTITY)
    font, desc = resolve_font(doc, page, pdf_name=None if style.family_changed else style.pdf_font,
                              family=style.family, bold=style.bold, italic=style.italic, text=text)
    size = style.size
    page_ts = ((page.rect * page.derotation_matrix).normalize().quad * ~M).rect
    widest = max(font.text_length(line, fontsize=size) for line in text.split("\n"))
    x0 = rect.x0
    x1 = max(rect.x1, x0 + widest + 1)
    if x1 > page_ts.x1 - 2 and style.align == ALIGN_LEFT:
        x1 = max(rect.x1, page_ts.x1 - 18)
    if fixed_width:                       # the user sized the box: wrap words to exactly this width
        x1 = max(rect.x1, x0 + 1)
    if first_baseline is None:
        first_baseline = pymupdf.Point(x0, rect.y0 + font.ascender * size)
    off, L, ident = _split_matrix(M)
    tw = pymupdf.TextWriter(page.rect)
    placed = []
    layout_text(tw, font, text, x0 + off.x, x1 + off.x, first_baseline.y + off.y, size, style.line_height,
                style.align, collect=placed)
    if collect is not None:
        collect.extend((li, w, a - off.x, b - off.x, y - off.y) for li, w, a, b, y in placed)
    fonts_before = page_font_xrefs(page)
    if ident:
        tw.write_text(page, color=style.color)
    else:
        tw.write_text(page, color=style.color, morph=(pymupdf.Point(0, 0), L))
    fix_tounicode(doc, page, fonts_before)
    if style.underline:
        _underline(page, font, text, x0, x1, first_baseline.y, style, M)
    return desc


def wrap_lines(font, text: str, width: float, size: float) -> list[tuple[str, bool]]:
    """Word-wrap text. Returns [(line, is_paragraph_end)]."""
    out = []
    space = font.text_length(" ", fontsize=size)
    for para in text.split("\n"):
        words = para.split(" ")
        line, lw = [], 0.0
        for w in words:
            ww = font.text_length(w, fontsize=size)
            add = ww + (space if line else 0)
            if line and lw + add > width:
                out.append((" ".join(line), False))
                line, lw = [w], ww
            else:
                line.append(w)
                lw += add
        out.append((" ".join(line), True))
    return out


def append_words(tw, x, y, line, font, size, gap=None, collect=None):
    """Write a line word by word. Embedded TrueType fonts often map the space glyph back to
    U+00A0 on extraction, so spaces are expressed as positioning instead of glyphs.
    collect: optional list receiving (word, x0, x1)."""
    space = font.text_length(" ", fontsize=size) if gap is None else gap
    for w in line.split(" "):
        if w:
            tw.append((x, y), w, font=font, fontsize=size)
            wl = font.text_length(w, fontsize=size)
            if collect is not None:
                collect.append((w, x, x + wl))
            x += wl
        x += space


def layout_text(tw: pymupdf.TextWriter, font, text, x0, x1, baseline, size, line_height, align, collect=None):
    """collect: optional list receiving (line_index, word, x0, x1, baseline) in writer coordinates."""
    width = x1 - x0
    y = baseline
    for li, (line, para_end) in enumerate(wrap_lines(font, text, width, size)):
        if line.strip():
            lw = font.text_length(line, fontsize=size)
            placed = []
            if align == ALIGN_CENTER:
                append_words(tw, x0 + (width - lw) / 2, y, line, font, size, collect=placed)
            elif align == ALIGN_RIGHT:
                append_words(tw, x1 - lw, y, line, font, size, collect=placed)
            elif align == ALIGN_JUSTIFY and not para_end and line.count(" ") > 0:
                words = [w for w in line.split(" ") if w]
                total = sum(font.text_length(w, fontsize=size) for w in words)
                gap = (width - total) / max(1, len(words) - 1)
                append_words(tw, x0, y, " ".join(words), font, size, gap, collect=placed)
            else:
                append_words(tw, x0, y, line, font, size, collect=placed)
            if collect is not None:
                collect.extend((li, w, a, b, y) for w, a, b in placed)
        y += size * line_height


def _underline(page, font, text, x0, x1, baseline, style, M):
    shape = page.new_shape()
    y = baseline + style.size * 0.12
    for line, _ in wrap_lines(font, text, x1 - x0, style.size):
        if line.strip():
            lw = font.text_length(line, fontsize=style.size)
            sx = {ALIGN_CENTER: x0 + (x1 - x0 - lw) / 2, ALIGN_RIGHT: x1 - lw}.get(style.align, x0)
            shape.draw_line(pymupdf.Point(sx, y) * M, pymupdf.Point(sx + lw, y) * M)
        y += style.size * style.line_height
    shape.finish(color=style.color, width=max(0.5, style.size / 16))
    shape.commit()


def replace_block(doc: pymupdf.Document, block: TextBlock, new_text: str, style: TextStyle) -> str:
    if block.source == "scan":
        from . import scanedit
        return scanedit.replace_block(doc, block, new_text, style)
    page = doc[block.page]
    remove_text_rects(page, block.line_rects)
    if not new_text.strip():
        return ""
    return write_text(doc, page, block.rect, new_text, style, block.first_baseline, matrix=block.matrix)


def delete_block(doc, block: TextBlock):
    replace_block(doc, block, "", block.style)


def reflow(text: str) -> str:
    """Join the visual lines of a paragraph so it can re-wrap to a new width (blank lines separate paragraphs)."""
    paras = text.split("\n\n")
    return "\n\n".join(" ".join(l.strip() for l in p.split("\n") if l.strip()) for p in paras)


def move_resize_block(doc: pymupdf.Document, block: TextBlock, new_rect: pymupdf.Rect) -> str:
    """Move and/or resize a text block (text-space rect).

    * moved only        -> same layout at the new position
    * width changed     -> the paragraph re-wraps to the new width
    * height changed    -> line spacing adapts so the lines fill the new height (single line: re-positioned)"""
    old = pymupdf.Rect(block.rect)
    new = pymupdf.Rect(new_rect).normalize()
    style = block.style.copy()
    size = style.size
    resized_w = abs(new.width - old.width) > 0.5
    resized_h = abs(new.height - old.height) > 0.5
    text = reflow(block.text) if resized_w else block.text
    fb = block.first_baseline or pymupdf.Point(old.x0, old.y0 + size * 0.8)
    top_pad = fb.y - old.y0
    baseline = pymupdf.Point(new.x0, new.y0 + top_pad)
    page = doc[block.page]
    if resized_h:
        if resized_w:
            font, _ = resolve_font(doc, page, pdf_name=None if style.family_changed else style.pdf_font,
                                   family=style.family, bold=style.bold, italic=style.italic, text=text)
            n = len(wrap_lines(font, text, max(1.0, new.width), size))
        else:
            n = len(text.split("\n"))
        n_old = len(block.text.split("\n"))
        bottom_pad = max(0.0, old.height - top_pad - (n_old - 1) * size * style.line_height)
        if n > 1:
            # capped so the lines are still recognised as one paragraph afterwards
            style.line_height = float(min(3.0, max(0.7, (new.height - top_pad - bottom_pad) / ((n - 1) * size))))
        else:
            baseline = pymupdf.Point(new.x0, new.y0 + new.height * (top_pad / max(old.height, 1e-3)))
    if block.source == "scan":
        from . import scanedit
        return scanedit.replace_block(doc, block, text, style, rect=new, first_baseline=baseline)
    remove_text_rects(page, block.line_rects)
    return write_text(doc, page, new, text, style, baseline, matrix=block.matrix, fixed_width=resized_w)


def page_is_scanned(page: pymupdf.Page) -> bool:
    from . import scanedit
    return scanedit.is_scanned_page(page)
