"""Protection against malicious PDFs.

PDF Studio never runs a PDF's JavaScript, never performs its launch or automatic actions, and never opens the
files attached to it - so a hostile PDF cannot do anything inside PDF Studio. Such content would still fire in
other PDF readers once the file is passed on, though, so it is reported when a file is opened and can be
removed with one click.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf

EXECUTABLE_EXT = {".exe", ".com", ".scr", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
                  ".wsh", ".hta", ".msi", ".msp", ".dll", ".cpl", ".jar", ".lnk", ".reg", ".pif", ".application",
                  ".gadget", ".msc", ".docm", ".xlsm", ".pptm", ".iso", ".img", ".vhd"}

# action types that do something beyond moving around inside the document
_RISKY_ACTIONS = {
    "JavaScript": ("javascript", "JavaScript code"),
    "Launch": ("launch", "an action that starts a program or opens a file"),
    "SubmitForm": ("submit", "an action that sends form data to a web address"),
    "ImportData": ("import", "an action that loads data from another file"),
    "GoToR": ("remote", "a link that opens another PDF file"),
    "GoToE": ("remote", "a link into an embedded file"),
    "Rendition": ("media", "an action that plays media"),
}
_ACTION_RE = re.compile(r"/S\s*/(JavaScript|Launch|SubmitForm|ImportData|GoToR|GoToE|Rendition)\b")
_URI_RE = re.compile(r"/URI\s*\(\s*([^)]{0,200})")
_DANGEROUS_URI = re.compile(r"^(file:|javascript:|vbscript:|data:|smb:|\\\\)", re.I)


@dataclass
class Finding:
    kind: str           # javascript | launch | submit | import | remote | media | autorun | link | attachment | xfa | richmedia
    detail: str
    xref: int = 0
    severity: str = "high"


@dataclass
class SecurityReport:
    findings: list = field(default_factory=list)

    @property
    def risky(self) -> bool:
        return any(f.severity == "high" for f in self.findings)

    def count(self, kind: str) -> int:
        return sum(1 for f in self.findings if f.kind == kind)

    def summary(self) -> str:
        labels = {"javascript": "JavaScript", "launch": "program-launch action", "autorun": "automatic action",
                  "submit": "data-sending action", "import": "data-import action", "remote": "link to another file",
                  "media": "media action", "link": "suspicious link", "attachment": "attached program",
                  "xfa": "XFA form scripts", "richmedia": "embedded Flash/rich media"}
        parts = []
        for kind, label in labels.items():
            n = self.count(kind)
            if n:
                parts.append(label if n == 1 else f"{label} ×{n}")
        return ", ".join(parts)


def _obj(doc, xref) -> str:
    try:
        return doc.xref_object(xref, compressed=False) or ""
    except Exception:
        return ""


def scan(doc: pymupdf.Document) -> SecurityReport:
    """Find active or dangerous content. Reads object dictionaries only - no streams are decoded."""
    rep = SecurityReport()
    if not doc.is_pdf:
        return rep
    for xref in range(1, doc.xref_length()):
        text = _obj(doc, xref)
        if not text:
            continue
        for m in _ACTION_RE.finditer(text):
            kind, detail = _RISKY_ACTIONS[m.group(1)]
            sev = "high" if kind in ("javascript", "launch", "submit", "import") else "medium"
            rep.findings.append(Finding(kind, detail, xref, sev))
        if "/JS" in text and not _ACTION_RE.search(text) and re.search(r"/JS\s*[(<\d]", text):
            rep.findings.append(Finding("javascript", "JavaScript code", xref))
        for m in _URI_RE.finditer(text):
            if _DANGEROUS_URI.match(m.group(1).strip()):
                rep.findings.append(Finding("link", f"link to {m.group(1).strip()[:60]}", xref))
        if re.search(r"/(RichMedia|Flash)\b", text):
            rep.findings.append(Finding("richmedia", "embedded Flash or rich media", xref, "medium"))
    try:
        cat = doc.pdf_catalog()
        kind, val = doc.xref_get_key(cat, "OpenAction")
        if kind in ("xref", "dict") and (kind == "dict" and _ACTION_RE.search(val)
                                        or kind == "xref" and _ACTION_RE.search(_obj(doc, int(val.split()[0])))):
            rep.findings.append(Finding("autorun", "an action that runs when the file is opened", cat))
        if doc.xref_get_key(cat, "AA")[0] != "null":
            rep.findings.append(Finding("autorun", "actions triggered by document events", cat))
        if doc.xref_get_key(cat, "AcroForm/XFA")[0] != "null":
            rep.findings.append(Finding("xfa", "XFA form (can carry scripts)", cat, "medium"))
    except Exception:
        pass
    try:
        for name in doc.embfile_names():
            info = doc.embfile_info(name)
            fname = (info.get("ufilename") or info.get("filename") or name).lower()
            if any(fname.endswith(ext) for ext in EXECUTABLE_EXT):
                rep.findings.append(Finding("attachment", f"attached file “{fname}”"))
    except Exception:
        pass
    for page in doc:
        try:
            for annot in page.annots(types=[pymupdf.PDF_ANNOT_FILE_ATTACHMENT]) or []:
                fname = (annot.get_file().get("filename") if hasattr(annot, "get_file") else "") or ""
                info = annot.file_info if hasattr(annot, "file_info") else {}
                fname = (info.get("filename") or fname or "").lower()
                if any(fname.endswith(ext) for ext in EXECUTABLE_EXT):
                    rep.findings.append(Finding("attachment", f"attached file “{fname}” on page {page.number + 1}"))
        except Exception:
            continue
    return rep


def _risky_value(doc, kind: str, val: str) -> bool:
    if kind == "dict":
        return bool(_ACTION_RE.search(val) or "/JS" in val)
    if kind == "xref":
        try:
            body = _obj(doc, int(val.split()[0]))
        except Exception:
            return False
        return bool(_ACTION_RE.search(body) or "/JS" in body)
    return False


def neutralise(doc: pymupdf.Document, remove_attachments: bool = True) -> int:
    """Remove scripts, automatic and launch actions, dangerous links and (optionally) attached programs.
    Normal links, bookmarks and form fields keep working. Returns the number of things removed."""
    removed = 0
    if not doc.is_pdf:
        return 0
    cat = doc.pdf_catalog()
    for key in ("OpenAction", "AA"):
        kind, val = doc.xref_get_key(cat, key)
        if key == "AA" and kind != "null" or key == "OpenAction" and _risky_value(doc, kind, val):
            doc.xref_set_key(cat, key, "null")
            removed += 1
    kind, val = doc.xref_get_key(cat, "Names/JavaScript")
    if kind != "null":
        doc.xref_set_key(cat, "Names/JavaScript", "null")
        removed += 1
    if doc.xref_get_key(cat, "AcroForm/XFA")[0] != "null":
        doc.xref_set_key(cat, "AcroForm/XFA", "null")          # the regular form fields stay
        removed += 1

    for xref in range(1, doc.xref_length()):
        text = _obj(doc, xref)
        if not text:
            continue
        # triggers attached to pages, annotations and form fields
        if re.search(r"/AA\b", text):
            try:
                if doc.xref_get_key(xref, "AA")[0] != "null":
                    doc.xref_set_key(xref, "AA", "null")
                    removed += 1
            except Exception:
                pass
        # an action held inline by a link or widget
        try:
            k, v = doc.xref_get_key(xref, "A")
            if _risky_value(doc, k, v) or k == "dict" and _URI_RE.search(v) and \
                    _DANGEROUS_URI.match(_URI_RE.search(v).group(1).strip()):
                doc.xref_set_key(xref, "A", "null")
                removed += 1
        except Exception:
            pass
        # an action that is an object of its own: make it an empty (inert) dictionary
        text = _obj(doc, xref)
        if _ACTION_RE.search(text) and "/Type" not in text.split("/S", 1)[0][-40:]:
            try:
                doc.update_object(xref, "<<>>")
                removed += 1
            except Exception:
                pass
        elif re.search(r"/JS\s*[(<\d]", text):
            try:
                doc.xref_set_key(xref, "JS", "null")
                removed += 1
            except Exception:
                pass
        if re.search(r"/(RichMedia|Flash)\b", text):
            try:
                doc.xref_set_key(xref, "RichMediaContent", "null")
            except Exception:
                pass

    if remove_attachments:
        try:
            for name in list(doc.embfile_names()):
                info = doc.embfile_info(name)
                fname = (info.get("ufilename") or info.get("filename") or name).lower()
                if any(fname.endswith(ext) for ext in EXECUTABLE_EXT):
                    doc.embfile_del(name)
                    removed += 1
        except Exception:
            pass
        for page in doc:
            try:
                doomed = []
                for annot in page.annots(types=[pymupdf.PDF_ANNOT_FILE_ATTACHMENT]) or []:
                    info = annot.file_info if hasattr(annot, "file_info") else {}
                    fname = (info.get("filename") or "").lower()
                    if any(fname.endswith(ext) for ext in EXECUTABLE_EXT):
                        doomed.append(annot.xref)
                for x in doomed:
                    page.delete_annot(page.load_annot(x))
                    removed += 1
            except Exception:
                continue
    return removed


def harden_process():
    """Close the classic DLL/program-planting hole: when a PDF is double-clicked, Windows starts the app in
    the PDF's folder - a Downloads folder, a USB stick - and a malicious DLL or program placed next to the PDF
    could be picked up from there. Work from the application's own folder instead, and make program lookups
    search the system folders before the current one. (The DLL search path the packaging set up is left
    alone: resetting it could stop the app's own libraries from loading.)
    File paths from the command line must be made absolute before calling this."""
    import os
    import sys
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetSearchPathMode(0x00000001 | 0x00008000)   # SAFE_SEARCHMODE | PERMANENT
    except Exception:
        pass
    try:
        packaged = getattr(sys, "frozen", False) or "__compiled__" in globals()
        base = os.path.dirname(os.path.abspath(sys.executable if packaged else __file__))
        os.chdir(base)
    except Exception:
        pass
