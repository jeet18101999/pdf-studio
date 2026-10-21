"""Repair damaged PDFs.

Two independent repair engines are tried: MuPDF's (the engine PDF Studio renders with) and qpdf's (through
pikepdf). Each candidate is rebuilt page by page into a fresh, clean file, so one broken page cannot take the
rest of the document down with it, and the candidate that recovers more readable pages wins.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf


@dataclass
class RepairReport:
    source: str
    ok: bool = False
    engine: str = ""
    pages_found: int = 0
    pages_recovered: int = 0
    damaged_pages: list = field(default_factory=list)     # 1-based: recovered, but with errors while reading
    lost_pages: list = field(default_factory=list)        # 1-based: could not be recovered at all
    picture_pages: list = field(default_factory=list)     # 1-based: only their appearance survived
    problems: list = field(default_factory=list)          # what was wrong with the file
    output: str = ""

    def summary(self) -> str:
        if not self.ok:
            return "The file is too badly damaged: no pages could be recovered."
        lines = [f"Recovered {self.pages_recovered} of {self.pages_found} page(s) using the {self.engine} repair engine."]
        if self.problems:
            lines.append("Problems found: " + "; ".join(self.problems) + ".")
        if self.damaged_pages:
            lines.append("Pages that were partly damaged (check them): " + _pages(self.damaged_pages) + ".")
        if self.picture_pages:
            lines.append("Pages kept as a picture of what survived (their text can't be selected; run Recognize "
                         "Text to make it editable again): " + _pages(self.picture_pages) + ".")
        if self.lost_pages:
            lines.append("Pages that could not be recovered: " + _pages(self.lost_pages) + ".")
        if not self.problems and not self.damaged_pages and not self.lost_pages:
            lines.append("No damage was found; the file has been rewritten cleanly.")
        return "\n".join(lines)


def _pages(nums) -> str:
    nums = list(nums)
    return ", ".join(map(str, nums[:20])) + (f" and {len(nums) - 20} more" if len(nums) > 20 else "")


def _warnings_reset():
    try:
        pymupdf.TOOLS.mupdf_warnings(reset=True)
    except Exception:
        pass


def _warnings() -> str:
    try:
        return pymupdf.TOOLS.mupdf_warnings(reset=True) or ""
    except Exception:
        return ""


def inspect_bytes(data: bytes) -> list[str]:
    """Problems visible without parsing: header and end-of-file marker."""
    problems = []
    head = data[:1024]
    if not head.startswith(b"%PDF-"):
        problems.append("the file does not start like a PDF" + (" (extra bytes before the PDF data)"
                                                               if b"%PDF-" in data[:65536] else ""))
    if b"%%EOF" not in data[-2048:]:
        problems.append("the end of the file is missing (incomplete download or copy?)")
    if b"startxref" not in data[-4096:]:
        problems.append("the index of objects (cross-reference table) is missing")
    return problems


def _salvage(doc: pymupdf.Document):
    """Copy page by page into a new document.
    Returns (new doc, recovered, damaged 1-based, lost 1-based, kept-as-picture 1-based)."""
    out = pymupdf.open()
    damaged, lost, pictures = [], [], []
    for i in range(doc.page_count):
        _warnings_reset()
        try:
            page = doc[i]
            page.get_text("text")
            page.get_pixmap(dpi=10, annots=True)
        except Exception:
            lost.append(i + 1)
            continue
        before = out.page_count
        try:
            out.insert_pdf(doc, from_page=i, to_page=i)
            if out.page_count == before:
                raise RuntimeError("page not copied")
            if _warnings().strip():
                damaged.append(i + 1)
        except Exception:
            # the page's objects are too broken to copy (typically a file cut off part-way), but MuPDF can
            # still draw whatever survived: keep that as a picture of the page instead of losing the page
            try:
                while out.page_count > before:
                    out.delete_page(out.page_count - 1)
                src = doc[i]
                pix = src.get_pixmap(dpi=150, annots=True)
                new = out.new_page(width=src.rect.width, height=src.rect.height)
                new.insert_image(new.rect, pixmap=pix)
                damaged.append(i + 1)
                pictures.append(i + 1)
            except Exception:
                lost.append(i + 1)
    return out, out.page_count, damaged, lost, pictures


def _candidate_mupdf(path: str):
    _warnings_reset()
    doc = pymupdf.open(path)
    notes = []
    if doc.is_repaired:
        notes.append("the index of objects was broken and has been rebuilt")
    if _warnings().strip():
        notes.append("some objects could not be read")
    return doc, notes


def _candidate_qpdf(path: str):
    import pikepdf
    with pikepdf.open(path, attempt_recovery=True) as pdf:
        buf = io.BytesIO()
        pdf.save(buf)
    return pymupdf.open("pdf", buf.getvalue()), []


def repair(path: str, out_path: str | None = None) -> tuple[RepairReport, pymupdf.Document | None]:
    """Repair `path`. Returns the report and the repaired document (also saved to out_path if given)."""
    report = RepairReport(source=path)
    try:
        data = Path(path).read_bytes()
    except OSError as ex:
        report.problems.append(f"the file cannot be read: {ex}")
        return report, None
    report.problems.extend(inspect_bytes(data))

    best = None
    for engine, make in (("MuPDF", _candidate_mupdf), ("qpdf", _candidate_qpdf)):
        try:
            src, notes = make(path)
        except Exception:
            continue
        try:
            found = src.page_count
            doc, recovered, damaged, lost, pictures = _salvage(src)
            toc = []
            try:
                toc = src.get_toc(simple=True)
                meta = src.metadata
            except Exception:
                meta = None
        except Exception:
            continue
        finally:
            try:
                src.close()
            except Exception:
                pass
        # most pages recovered, then most recovered as real content (not pictures), then least damage
        score = (recovered, -len(pictures), -len(damaged))
        if best is None or score > best[0]:
            if best is not None:
                best[1].close()
            best = (score, doc, engine, found, recovered, damaged, lost, pictures, notes, toc, meta)
        else:
            doc.close()

    if best is None or best[4] == 0:
        if best is not None:
            best[1].close()
        return report, None
    _, doc, engine, found, recovered, damaged, lost, pictures, notes, toc, meta = best
    report.ok = True
    report.engine = engine
    report.pages_found = found
    report.pages_recovered = recovered
    report.damaged_pages = damaged
    report.lost_pages = lost
    report.picture_pages = pictures
    for n in notes:
        if n not in report.problems:
            report.problems.append(n)
    try:
        if meta:
            doc.set_metadata({k: v for k, v in meta.items() if v and k in ("title", "author", "subject", "keywords", "creator")})
        kept = {p for p in range(1, found + 1) if p not in lost}
        mapping = {old: new for new, old in enumerate(sorted(kept), 1)}
        clean_toc, prev = [], 0
        for level, title, page in toc or []:
            if page in mapping:
                level = max(1, min(level, prev + 1))
                clean_toc.append([level, title, mapping[page]])
                prev = level
        if clean_toc:
            doc.set_toc(clean_toc)
    except Exception:
        pass
    if out_path:
        doc.save(out_path, garbage=4, deflate=True, clean=True)
        report.output = out_path
        with pymupdf.open(out_path) as check:                  # prove the written file is sound
            if check.page_count != recovered:
                raise RuntimeError("the repaired file did not verify")
            for page in check:
                page.get_pixmap(dpi=10)
    return report, doc


def looks_damaged(path: str) -> bool:
    """Quick check used when opening a file: does it need repairing?"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    if inspect_bytes(data):
        return True
    try:
        _warnings_reset()
        with pymupdf.open("pdf", data) as d:
            return bool(d.is_repaired)
    except Exception:
        return True
