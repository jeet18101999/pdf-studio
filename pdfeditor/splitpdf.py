"""Split a PDF into several files: every N pages, by page ranges, at top-level bookmarks, or by file size.

Each part keeps the bookmarks that point into it and the document's metadata.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf


@dataclass
class Part:
    pages: list[int]                      # 0-based page numbers, in output order
    title: str = ""
    size_hint: int = 0                    # estimated bytes (size mode)

    def describe(self) -> str:
        runs, start, prev = [], None, None
        for p in self.pages:
            if start is None:
                start = prev = p
            elif p == prev + 1:
                prev = p
            else:
                runs.append((start, prev))
                start = prev = p
        if start is not None:
            runs.append((start, prev))
        text = ", ".join(f"{a + 1}" if a == b else f"{a + 1}–{b + 1}" for a, b in runs)
        return ("page " if len(self.pages) == 1 else "pages ") + text


def plan_every(page_count: int, n: int) -> list[Part]:
    n = max(1, int(n))
    return [Part(list(range(s, min(s + n, page_count)))) for s in range(0, page_count, n)]


def parse_ranges(text: str, page_count: int) -> list[list[int]]:
    """'1-3, 5, 8-' -> [[0,1,2], [4], [7..last]]. Each comma-separated range becomes one file.
    Raises ValueError with a message fit for the user."""
    groups = []
    for chunk in [c.strip() for c in re.split(r"[,;]", text) if c.strip()]:
        m = re.fullmatch(r"(\d*)\s*[-–]\s*(\d*)|(\d+)", chunk)
        if not m:
            raise ValueError(f"“{chunk}” is not a page or a range like 3-7")
        if m.group(3):
            a = b = int(m.group(3))
        else:
            if not m.group(1) and not m.group(2):
                raise ValueError(f"“{chunk}” needs at least one page number")
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else page_count
        if a < 1 or b < 1 or a > page_count or b > page_count:
            raise ValueError(f"“{chunk}”: this document has pages 1 to {page_count}")
        if a > b:
            raise ValueError(f"“{chunk}”: the first page comes after the last")
        groups.append(list(range(a - 1, b)))
    if not groups:
        raise ValueError("Enter at least one page range, for example 1-3, 4-10")
    return groups


def plan_ranges(page_count: int, text: str) -> list[Part]:
    return [Part(g) for g in parse_ranges(text, page_count)]


def plan_bookmarks(doc: pymupdf.Document) -> list[Part]:
    """One file per top-level bookmark (chapter). Pages before the first bookmark become their own part."""
    starts = []
    for level, title, page in doc.get_toc(simple=True):
        if level == 1 and 1 <= page <= doc.page_count and (not starts or page - 1 > starts[-1][0]):
            starts.append((page - 1, title.strip()))
    if not starts:
        raise ValueError("This document has no bookmarks to split at")
    parts = []
    if starts[0][0] > 0:
        parts.append(Part(list(range(0, starts[0][0])), "Front matter"))
    for i, (first, title) in enumerate(starts):
        last = starts[i + 1][0] - 1 if i + 1 < len(starts) else doc.page_count - 1
        parts.append(Part(list(range(first, last + 1)), title))
    return parts


def _pages_bytes(doc: pymupdf.Document, pages: list[int]) -> int:
    out = pymupdf.open()
    for a, b in _runs(pages):
        out.insert_pdf(doc, from_page=a, to_page=b)
    n = len(out.tobytes(garbage=3, deflate=True))
    out.close()
    return n


def plan_size(doc: pymupdf.Document, max_bytes: int, progress=None) -> list[Part]:
    """Parts no larger than max_bytes (a single page that is larger on its own becomes its own part).
    Pages are added while the measured size of the part stays under the limit."""
    parts: list[Part] = []
    cur: list[int] = []
    cur_size = 0
    for p in range(doc.page_count):
        if progress:
            progress(p + 1, doc.page_count)
        trial = cur + [p]
        size = _pages_bytes(doc, trial)
        if cur and size > max_bytes:
            parts.append(Part(cur, size_hint=cur_size))
            cur, cur_size = [p], _pages_bytes(doc, [p])
        else:
            cur, cur_size = trial, size
    if cur:
        parts.append(Part(cur, size_hint=cur_size))
    return parts


def _runs(pages: list[int]):
    start = prev = None
    for p in pages:
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            yield start, prev
            start = prev = p
    if start is not None:
        yield start, prev


def _safe_name(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip().rstrip(".")
    return text[:80] or "part"


def _part_toc(doc_toc, pages: list[int]) -> list:
    """The bookmarks that point into this part, renumbered, with a valid level structure."""
    where = {p: i + 1 for i, p in enumerate(pages)}
    out, prev_level = [], 0
    for level, title, page in doc_toc:
        if page - 1 not in where:
            continue
        level = max(1, min(level, prev_level + 1))
        out.append([level, title, where[page - 1]])
        prev_level = level
    return out


def write_parts(doc: pymupdf.Document, parts: list[Part], out_dir: str, stem: str,
                pattern: str = "{name}_part{n}", progress=None) -> list[str]:
    """Write every part as its own PDF. `pattern` may use {name}, {n} and {title}."""
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    toc = doc.get_toc(simple=True)
    meta = dict(doc.metadata or {})
    written, used = [], set()
    width = len(str(len(parts)))
    for n, part in enumerate(parts, 1):
        if progress:
            progress(n, len(parts))
        out = pymupdf.open()
        for a, b in _runs(part.pages):
            out.insert_pdf(doc, from_page=a, to_page=b)
        try:
            out.set_toc(_part_toc(toc, part.pages))
        except Exception:
            pass
        m = {k: v for k, v in meta.items() if k in ("author", "subject", "keywords", "creator", "title") and v}
        if part.title:
            m["title"] = part.title
        try:
            out.set_metadata(m)
        except Exception:
            pass
        base = pattern.format(name=stem, n=str(n).zfill(width), title=_safe_name(part.title or f"part{n}"))
        base = _safe_name(base)
        name, k = base, 2
        while name.lower() in used or (folder / f"{name}.pdf").exists() and name.lower() not in used and n == 0:
            name = f"{base} ({k})"
            k += 1
        used.add(name.lower())
        path = folder / f"{name}.pdf"
        out.save(str(path), garbage=3, deflate=True)
        out.close()
        written.append(str(path))
    return written
