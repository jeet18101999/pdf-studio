"""Batch processing: apply a chain of actions to many PDFs (no Qt; safe in a worker thread).

Actions (dicts with an "op" key):
  {"op": "ocr", "mode": "searchable"|"editable", "langs": "eng"}
  {"op": "compress", "dpi": 110, "quality": 70}
  {"op": "watermark", "text": "CONFIDENTIAL", "size": 60, "opacity": 0.2, "color": (0.8, 0.1, 0.1), "angle": 45}
  {"op": "page_numbers", "format": "Page {n} of {total}", "size": 9}
  {"op": "flatten_forms"}
  {"op": "rotate", "degrees": 90}
  {"op": "protect", "user": "pw", "owner": "owner-pw"}
  {"op": "to_docx"} | {"op": "to_xlsx"} | {"op": "to_text"}      (export alongside the PDF output)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import convert, forms, ocr

OP_LABELS = {"ocr": "Recognize text (OCR)", "compress": "Compress", "watermark": "Watermark",
             "page_numbers": "Page numbers", "flatten_forms": "Flatten forms", "rotate": "Rotate pages",
             "protect": "Password protect", "to_docx": "Export to Word", "to_xlsx": "Export tables to Excel",
             "to_text": "Export text"}


@dataclass
class BatchResult:
    source: str
    ok: bool
    outputs: list = field(default_factory=list)
    message: str = ""


def _open(path: str) -> pymupdf.Document:
    doc = pymupdf.open(path)
    if doc.needs_pass:
        raise PermissionError("password protected")
    if not doc.is_pdf:
        doc = pymupdf.open("pdf", doc.convert_to_pdf())
    return pymupdf.open("pdf", doc.tobytes())          # in-memory: never locks or modifies the source


def _watermark(doc, text, size=60, opacity=0.2, color=(0.8, 0.1, 0.1), angle=45):
    font = pymupdf.Font("helv")
    for page in doc:
        r = page.rect
        tw = pymupdf.TextWriter(r, opacity=opacity, color=color)
        w = font.text_length(text, fontsize=size)
        c = pymupdf.Point(r.width / 2, r.height / 2) * page.derotation_matrix
        tw.append((c.x - w / 2, c.y + size * 0.35), text, font=font, fontsize=size)
        tw.write_text(page, morph=(c, pymupdf.Matrix(-angle - page.rotation)))


def _page_numbers(doc, fmt="Page {n} of {total}", size=9, color=(0.2, 0.2, 0.2)):
    font = pymupdf.Font("helv")
    total = doc.page_count
    for i, page in enumerate(doc):
        text = fmt.replace("{n}", str(i + 1)).replace("{total}", str(total))
        r = page.rect
        w = font.text_length(text, fontsize=size)
        pt = pymupdf.Point(r.width / 2, r.height - 20) * page.derotation_matrix
        tw = pymupdf.TextWriter(r, color=color)
        tw.append((pt.x - w / 2, pt.y), text, font=font, fontsize=size)
        tw.write_text(page, morph=(pt, pymupdf.Matrix(-page.rotation)))


def process_file(path: str, actions: list[dict], out_dir: str, suffix: str = "_processed",
                 progress=None, cancelled=lambda: False) -> BatchResult:
    src = Path(path)
    res = BatchResult(str(src), False)
    try:
        doc = _open(path)
    except Exception as ex:
        res.message = f"cannot open: {ex}"
        return res
    out_base = Path(out_dir) / (src.stem + suffix)
    encryption = None
    compress = None
    exports = []
    try:
        for a in actions:
            if cancelled():
                res.message = "cancelled"
                return res
            op = a["op"]
            if progress:
                progress(f"{src.name}: {OP_LABELS.get(op, op)}")
            if op == "ocr":
                data = doc.tobytes()
                matcher = ocr.FontMatcher() if a.get("mode", "searchable") == ocr.MODE_EDITABLE else None
                for pno in range(doc.page_count):
                    if cancelled():
                        res.message = "cancelled"
                        return res
                    page = doc[pno]
                    if len(page.get_text().strip()) > 50 and not _looks_scanned(page):
                        continue                       # already has real text
                    r = ocr.ocr_page(data, pno, langs=a.get("langs", "eng"), mode=a.get("mode", "searchable"),
                                     matcher=matcher)
                    ocr.apply_ocr(doc, r, a.get("mode", "searchable"))
            elif op == "compress":
                compress = (a.get("dpi", 110), a.get("quality", 70))
            elif op == "watermark":
                _watermark(doc, a.get("text", "CONFIDENTIAL"), a.get("size", 60), a.get("opacity", 0.2),
                           tuple(a.get("color", (0.8, 0.1, 0.1))), a.get("angle", 45))
            elif op == "page_numbers":
                _page_numbers(doc, a.get("format", "Page {n} of {total}"), a.get("size", 9))
            elif op == "flatten_forms":
                if forms.has_form(doc):
                    forms.flatten(doc)
            elif op == "rotate":
                for page in doc:
                    page.set_rotation((page.rotation + int(a.get("degrees", 90))) % 360)
            elif op == "protect":
                encryption = a
            elif op in ("to_docx", "to_xlsx", "to_text"):
                exports.append(op)
            else:
                raise ValueError(f"unknown action {op}")

        pdf_changed = any(a["op"] not in ("to_docx", "to_xlsx", "to_text") for a in actions)
        if pdf_changed:
            if compress:
                try:
                    doc.rewrite_images(dpi_threshold=compress[0] + 20, dpi_target=compress[0], quality=compress[1])
                except Exception:
                    pass
                try:
                    doc.subset_fonts()
                except Exception:
                    pass
            kw = dict(garbage=4 if compress else 3, deflate=True)
            if compress:
                kw.update(deflate_images=True, deflate_fonts=True, clean=True)
            if encryption:
                kw.update(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=encryption.get("user", ""),
                          owner_pw=encryption.get("owner") or (encryption.get("user", "") + "_owner"),
                          permissions=pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_COPY | pymupdf.PDF_PERM_ACCESSIBILITY)
            out_pdf = out_base.with_suffix(".pdf")
            out_pdf.write_bytes(doc.tobytes(**kw))
            res.outputs.append(str(out_pdf))
        for op in exports:
            if progress:
                progress(f"{src.name}: {OP_LABELS[op]}")
            target = out_base.with_suffix({"to_docx": ".docx", "to_xlsx": ".xlsx", "to_text": ".txt"}[op])
            {"to_docx": convert.to_docx, "to_xlsx": convert.to_xlsx, "to_text": convert.to_text}[op](doc, str(target))
            res.outputs.append(str(target))
        res.ok = True
        res.message = "done"
    except Exception as ex:
        res.message = f"{type(ex).__name__}: {ex}"
    return res


def _looks_scanned(page) -> bool:
    from . import scanedit
    return scanedit.is_scanned_page(page) and not scanedit.visible_text_chars(page)


def run_batch(files: list[str], actions: list[dict], out_dir: str, suffix: str = "_processed",
              progress=None, cancelled=lambda: False) -> list[BatchResult]:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    results = []
    for i, f in enumerate(files):
        if cancelled():
            break
        if progress:
            progress(f"({i + 1}/{len(files)}) {Path(f).name}", int(i / max(1, len(files)) * 100))
        results.append(process_file(f, actions, out_dir, suffix,
                                    progress=(lambda m, i=i: progress(m, int(i / len(files) * 100))) if progress else None,
                                    cancelled=cancelled))
    if progress:
        progress("Finished", 100)
    return results
