"""Compare two PDFs: word-level text changes, visual (image/graphics) changes, and a side-by-side report.

Works on native and OCR'd scanned PDFs (the text layer is compared)."""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field

import numpy as np
import pymupdf

INSERT, DELETE, REPLACE, VISUAL = "inserted", "deleted", "changed", "visual"
COLORS = {INSERT: (0.13, 0.65, 0.3), DELETE: (0.85, 0.15, 0.15), REPLACE: (0.95, 0.55, 0.0), VISUAL: (0.25, 0.4, 0.95)}

from .cvsafe import cv2


@dataclass
class Change:
    kind: str
    page_a: int | None
    page_b: int | None
    rects_a: list = field(default_factory=list)
    rects_b: list = field(default_factory=list)
    text_a: str = ""
    text_b: str = ""

    def describe(self) -> str:
        if self.kind == INSERT:
            return f"Inserted “{self.text_b[:80]}”"
        if self.kind == DELETE:
            return f"Deleted “{self.text_a[:80]}”"
        if self.kind == REPLACE:
            return f"Changed “{self.text_a[:60]}” → “{self.text_b[:60]}”"
        return "Graphics / image changed"


def _words(page: pymupdf.Page):
    ws = page.get_text("words", sort=True)
    return [(w[4], pymupdf.Rect(w[:4])) for w in ws if w[4].strip()]


def _norm(t: str) -> str:
    return t.replace(" ", " ").replace("­", "-").strip()


def _merge_rects(rects, gap=2.0):
    """Merge word rects on the same line into continuous highlight bands."""
    out = []
    for r in sorted(rects, key=lambda r: (round(r.y0), r.x0)):
        if out and abs(out[-1].y0 - r.y0) < r.height * 0.5 and r.x0 - out[-1].x1 < max(gap, r.height):
            out[-1] |= r
        else:
            out.append(pymupdf.Rect(r))
    return out


def compare_text(doc_a: pymupdf.Document, doc_b: pymupdf.Document) -> list[Change]:
    """Document-wide word diff (text that moves to another page is still matched)."""
    seq_a = [(p.number, t, r) for p in doc_a for t, r in _words(p)]
    seq_b = [(p.number, t, r) for p in doc_b for t, r in _words(p)]
    ta = [_norm(t) for _, t, _ in seq_a]
    tb = [_norm(t) for _, t, _ in seq_b]
    changes = []
    sm = difflib.SequenceMatcher(a=ta, b=tb, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a_items, b_items = seq_a[i1:i2], seq_b[j1:j2]
        kind = {"insert": INSERT, "delete": DELETE, "replace": REPLACE}[tag]
        if tag == "replace":
            text_a = " ".join(t for _, t, _ in a_items)
            text_b = " ".join(t for _, t, _ in b_items)
            same_page = {p for p, _, _ in a_items} == {p for p, _, _ in b_items}
            similar = difflib.SequenceMatcher(a=text_a, b=text_b, autojunk=False).ratio() >= 0.25
            if not same_page or (not similar and min(len(a_items), len(b_items)) > 2):
                # unrelated removal + addition (e.g. a clause removed here, new text elsewhere)
                changes.append(Change(DELETE, a_items[0][0], None, _merge_rects([r for _, _, r in a_items]), [], text_a, ""))
                changes.append(Change(INSERT, None, b_items[0][0], [], _merge_rects([r for _, _, r in b_items]), "", text_b))
                continue
        # split long runs per page so highlights land on the right pages
        pages = sorted({p for p, _, _ in a_items} | {p for p, _, _ in b_items}) or [None]
        pa = a_items[0][0] if a_items else (seq_a[i1 - 1][0] if i1 > 0 and seq_a else (0 if doc_a.page_count else None))
        pb = b_items[0][0] if b_items else (seq_b[j1 - 1][0] if j1 > 0 and seq_b else (0 if doc_b.page_count else None))
        changes.append(Change(kind, pa, pb,
                              _merge_rects([r for _, _, r in a_items]), _merge_rects([r for _, _, r in b_items]),
                              " ".join(t for _, t, _ in a_items), " ".join(t for _, t, _ in b_items)))
    return changes


def compare_visual(page_a: pymupdf.Page, page_b: pymupdf.Page, dpi: int = 60, ignore_text: bool = True) -> list[pymupdf.Rect]:
    """Regions whose rendering differs (images, drawings). Text regions are masked when ignore_text,
    because text changes are reported by the text diff."""
    def render(page):
        pix = page.get_pixmap(dpi=dpi, alpha=False, annots=False)
        img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
        return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY), pix.width / page.rect.width
    ga, ka = render(page_a)
    gb, kb = render(page_b)
    h, w = min(ga.shape[0], gb.shape[0]), min(ga.shape[1], gb.shape[1])
    ga, gb = ga[:h, :w], gb[:h, :w]
    diff = cv2.absdiff(cv2.GaussianBlur(ga, (3, 3), 0), cv2.GaussianBlur(gb, (3, 3), 0))
    mask = (diff > 40).astype(np.uint8)
    if ignore_text:
        for page, k in ((page_a, ka), (page_b, kb)):
            for _, r in _words(page):
                rr = (r * page.rotation_matrix) * k
                x0, y0 = max(0, int(rr.x0) - 2), max(0, int(rr.y0) - 2)
                mask[y0:int(rr.y1) + 3, x0:int(rr.x1) + 3] = 0
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    out = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < 30:
            continue
        r = pymupdf.Rect(x / ka, y / ka, (x + bw) / ka, (y + bh) / ka)
        out.append(r * page_b.derotation_matrix)
    return out


def compare(doc_a: pymupdf.Document, doc_b: pymupdf.Document, visual: bool = True) -> list[Change]:
    changes = compare_text(doc_a, doc_b)
    if visual:
        for i in range(min(doc_a.page_count, doc_b.page_count)):
            for r in compare_visual(doc_a[i], doc_b[i]):
                changes.append(Change(VISUAL, i, i, [r], [r]))
    for i in range(doc_b.page_count, doc_a.page_count):
        changes.append(Change(DELETE, i, None, [doc_a[i].rect], [], text_a=f"[page {i + 1} removed]"))
    for i in range(doc_a.page_count, doc_b.page_count):
        changes.append(Change(INSERT, None, i, [], [doc_b[i].rect], text_b=f"[page {i + 1} added]"))
    return changes


def summary(changes: list[Change]) -> dict:
    out = {INSERT: 0, DELETE: 0, REPLACE: 0, VISUAL: 0}
    for c in changes:
        out[c.kind] += 1
    return out


def build_report(doc_a: pymupdf.Document, doc_b: pymupdf.Document, changes: list[Change], path: str,
                 name_a: str = "Original", name_b: str = "Revised") -> str:
    """Side-by-side report: summary page, then each page pair with colour-coded highlights."""
    report = pymupdf.open()
    W, H = 842, 595                        # A4 landscape
    font = pymupdf.Font("helv")
    bold = pymupdf.Font("hebo")

    # ---- summary page
    s = summary(changes)
    page = report.new_page(width=W, height=H)
    tw = pymupdf.TextWriter(page.rect)
    tw.append((40, 60), "Document comparison", font=bold, fontsize=22)
    tw.append((40, 88), f"{name_a}  →  {name_b}", font=font, fontsize=11)
    y = 130
    for kind, label in ((REPLACE, "Changed"), (INSERT, "Inserted"), (DELETE, "Deleted"), (VISUAL, "Graphics changed")):
        page.draw_rect(pymupdf.Rect(40, y - 10, 52, y + 2), color=None, fill=COLORS[kind])
        tw.append((60, y), f"{label}: {s[kind]}", font=font, fontsize=12)
        y += 22
    y += 14
    tw.append((40, y), "Changes", font=bold, fontsize=13)
    y += 20
    for c in changes:
        if c.kind == VISUAL:
            continue
        if y > H - 40:
            tw.write_text(page)
            page = report.new_page(width=W, height=H)
            tw = pymupdf.TextWriter(page.rect)
            y = 50
        pg = c.page_b if c.page_b is not None else c.page_a
        line = f"p.{(pg or 0) + 1}  {c.describe()}"
        tw.append((50, y), line[:140], font=font, fontsize=9, )
        y += 14
    tw.write_text(page)

    # ---- side-by-side pages
    pages = max(doc_a.page_count, doc_b.page_count)
    half = (W - 60) / 2
    for i in range(pages):
        page = report.new_page(width=W, height=H)
        page.insert_text((20, 24), f"Page {i + 1}", fontname="hebo", fontsize=11)
        page.insert_text((20 + half / 2, 24), name_a, fontsize=9)
        page.insert_text((40 + half + half / 2, 24), name_b, fontsize=9)
        for side, doc, x0, key in ((0, doc_a, 20, "a"), (1, doc_b, 40 + half, "b")):
            box = pymupdf.Rect(x0, 34, x0 + half, H - 16)
            page.draw_rect(box, color=(0.8, 0.8, 0.85), width=0.5)
            if i >= doc.page_count:
                page.insert_text((box.x0 + 20, box.y0 + 40), "(no page)", fontsize=12, color=(0.5, 0.5, 0.5))
                continue
            src = doc[i]
            sr = src.rect
            scale = min(box.width / sr.width, box.height / sr.height)
            target = pymupdf.Rect(box.x0, box.y0, box.x0 + sr.width * scale, box.y0 + sr.height * scale)
            page.show_pdf_page(target, doc, i)
            for c in changes:
                pno = c.page_a if key == "a" else c.page_b
                rects = c.rects_a if key == "a" else c.rects_b
                if pno != i or not rects:
                    continue
                if (key == "a" and c.kind == INSERT) or (key == "b" and c.kind == DELETE):
                    continue
                for r in rects:
                    rr = (pymupdf.Rect(r) * src.rotation_matrix)
                    hr = pymupdf.Rect(target.x0 + rr.x0 * scale, target.y0 + rr.y0 * scale,
                                      target.x0 + rr.x1 * scale, target.y0 + rr.y1 * scale) + (-1, -1, 1, 1)
                    annot = page.add_rect_annot(hr)
                    annot.set_colors(stroke=COLORS[c.kind], fill=COLORS[c.kind])
                    annot.set_opacity(0.35)
                    annot.set_border(width=0.6)
                    annot.set_info(content=c.describe())
                    annot.update()
    report.save(path, garbage=3, deflate=True)
    return path
