"""Font discovery and matching.

* Indexes installed system fonts (name tables via fontTools, cached on disk).
* Maps PDF font names ("ABCDEF+TimesNewRomanPS-BoldMT") to installed fonts.
* Resolves a usable pymupdf.Font for writing text: embedded font first (if it
  has every glyph needed), otherwise the best matching system font.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path

import pymupdf

FONT_DIRS = [
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
    Path("/usr/share/fonts"), Path.home() / ".fonts", Path("/Library/Fonts"),
    Path.home() / "Library" / "Fonts", Path("/System/Library/Fonts"),
]
CACHE_FILE = Path(os.environ.get("APPDATA", Path.home())) / "PDFStudio" / "fontindex.json"

# PDF base-14 / common aliases -> installed family
ALIASES = {
    "helvetica": "Arial", "helv": "Arial", "arialmt": "Arial", "swiss": "Arial",
    "times": "Times New Roman", "timesroman": "Times New Roman", "timesnewroman": "Times New Roman",
    "tiro": "Times New Roman", "courier": "Courier New", "cour": "Courier New",
    "couriernew": "Courier New", "symbol": "Symbol", "zapfdingbats": "Wingdings",
    "garamond": "Garamond", "palatino": "Palatino Linotype", "bookman": "Bookman Old Style",
    "centuryschoolbook": "Century Schoolbook", "avantgarde": "Century Gothic",
    "helveticaneue": "Arial", "minionpro": "Times New Roman", "myriadpro": "Segoe UI",
    "sourcesanspro": "Segoe UI", "dejavusans": "Verdana", "liberationsans": "Arial",
    "liberationserif": "Times New Roman", "liberationmono": "Courier New",
}

BOLD_TOKENS = ("bold", "black", "heavy", "semibold", "demibold", "extrabold", "ultrabold")
ITALIC_TOKENS = ("italic", "oblique", "slanted")

# span flag bits from PyMuPDF
FLAG_ITALIC = 2
FLAG_SERIF = 4
FLAG_MONO = 8
FLAG_BOLD = 16


@dataclass
class FontEntry:
    path: str
    family: str
    style: str
    fullname: str
    psname: str
    bold: bool
    italic: bool
    weight: int = 400


def _norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"^[a-z]{6}\+", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


import threading
_INDEX_LOCK = threading.Lock()


class FontIndex:
    def __init__(self):
        self.entries: list[FontEntry] = []
        self._by_family: dict[str, list[FontEntry]] = {}
        self._by_ps: dict[str, FontEntry] = {}
        self._loaded = False

    # -------------------------------------------------------------- build
    def ensure(self):
        if self._loaded:
            return
        with _INDEX_LOCK:
            if not self._loaded:
                self._build()

    def _build(self):
        files = []
        for d in FONT_DIRS:
            try:
                if d.is_dir():
                    for p in d.rglob("*"):
                        if p.suffix.lower() in (".ttf", ".otf", ".ttc"):
                            files.append(p)
            except OSError:
                pass
        sig = {str(p): int(p.stat().st_mtime) for p in files}
        cached = self._read_cache()
        if cached and cached.get("sig") == sig:
            self.entries = [FontEntry(**e) for e in cached["entries"]]
        else:
            self.entries = []
            for p in files:
                self.entries.extend(_read_font(p))
            self._write_cache(sig)
        for e in self.entries:
            self._by_family.setdefault(e.family.lower(), []).append(e)
            self._by_ps.setdefault(_norm(e.psname), e)
            self._by_ps.setdefault(_norm(e.fullname), e)
        self._loaded = True

    def _read_cache(self):
        try:
            return json.loads(CACHE_FILE.read_text("utf-8"))
        except Exception:
            return None

    def _write_cache(self, sig):
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps({"sig": sig, "entries": [asdict(e) for e in self.entries]}), "utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------- lookup
    def families(self) -> list[str]:
        self.ensure()
        return sorted({e.family for e in self.entries}, key=str.lower)

    def find(self, family: str, bold=False, italic=False) -> FontEntry | None:
        self.ensure()
        cands = self._by_family.get(family.lower())
        if not cands:
            return None

        def score(e: FontEntry):
            s = 0
            s += 0 if e.bold == bold else 10
            s += 0 if e.italic == italic else 10
            s += abs(e.weight - (700 if bold else 400)) / 100
            s += 0 if e.path.lower().endswith((".ttf", ".otf")) else 3
            return s

        return min(cands, key=score)

    def match_pdf_font(self, pdf_name: str, flags: int = 0) -> tuple[FontEntry, bool, bool]:
        """Map a PDF font name + span flags to the closest installed font.

        Returns (entry, bold, italic)."""
        self.ensure()
        raw = re.sub(r"^[A-Z]{6}\+", "", pdf_name or "")
        low = raw.lower()
        bold = bool(flags & FLAG_BOLD) or any(t in low for t in BOLD_TOKENS)
        italic = bool(flags & FLAG_ITALIC) or any(t in low for t in ITALIC_TOKENS)

        # 1. exact postscript / full name
        e = self._by_ps.get(_norm(raw))
        if e:
            return e, e.bold or bold, e.italic or italic

        # 2. family part before '-' or ','
        fam = re.split(r"[-,]", raw)[0]
        fam_n = _norm(fam)
        for suffix in ("psmt", "mt", "ps", "std", "pro", "lt", "com"):
            if fam_n.endswith(suffix) and len(fam_n) > len(suffix) + 2:
                fam_n = fam_n[: -len(suffix)]
        fam_n = re.sub("(" + "|".join(BOLD_TOKENS + ITALIC_TOKENS + ("regular", "roman", "light", "medium")) + ")$", "", fam_n)
        target = ALIASES.get(fam_n)
        if not target:
            for f in self._by_family:
                if _norm(f) == fam_n:
                    target = self._by_family[f][0].family
                    break
        if not target:   # prefix match e.g. "SegoeUISemibold" -> "Segoe UI"
            best = None
            for f in self._by_family:
                nf = _norm(f)
                if len(nf) >= 4 and fam_n.startswith(nf) and (best is None or len(nf) > len(_norm(best))):
                    best = f
            if best:
                target = self._by_family[best][0].family
        if not target:
            if flags & FLAG_MONO or "mono" in low or "courier" in low:
                target = "Courier New"
            elif flags & FLAG_SERIF or "serif" in low and "sans" not in low:
                target = "Times New Roman"
            else:
                target = "Arial"
        e = self.find(target, bold, italic) or self.find("Arial", bold, italic)
        if e is None and self.entries:
            e = self.entries[0]
        return e, bold, italic


def _read_font(path: Path) -> list[FontEntry]:
    import logging
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    from fontTools.ttLib import TTFont, TTCollection
    out = []
    try:
        if path.suffix.lower() == ".ttc":
            fonts = TTCollection(str(path), lazy=True).fonts[:1]    # MuPDF loads face 0 only
        else:
            fonts = [TTFont(str(path), lazy=True, fontNumber=0)]
        for f in fonts:
            name = f["name"]

            def g(*ids):
                for i in ids:
                    r = name.getDebugName(i)
                    if r:
                        return r
                return ""

            family = g(16, 1)
            style = g(17, 2) or "Regular"
            weight = 400
            italic = False
            try:
                os2 = f["OS/2"]
                weight = int(os2.usWeightClass)
                italic = bool(os2.fsSelection & 1)
            except Exception:
                pass
            sl = style.lower()
            bold = weight >= 600 or "bold" in sl or "black" in sl
            italic = italic or "italic" in sl or "oblique" in sl
            if family:
                out.append(FontEntry(str(path), family, style, g(4), g(6), bold, italic, weight))
    except Exception:
        pass
    return out


FONT_INDEX = FontIndex()


# ------------------------------------------------------------------ pymupdf fonts
@lru_cache(maxsize=128)
def font_from_file(path: str) -> pymupdf.Font:
    return pymupdf.Font(fontfile=path)


def embedded_font(doc: pymupdf.Document, page: pymupdf.Page, pdf_name: str) -> pymupdf.Font | None:
    """Return the font embedded in the PDF under `pdf_name`, if it can be loaded."""
    key = (id(doc), pdf_name)
    if key in _embedded_cache:
        return _embedded_cache[key]
    font = None
    try:
        for xref, ext, ftype, basefont, name, enc, *_ in page.get_fonts(full=True):
            if basefont == pdf_name or basefont.split("+")[-1] == pdf_name.split("+")[-1]:
                if ext in ("n/a", "") or ftype == "Type3":
                    continue
                _, _, _, content = doc.extract_font(xref)
                if content:
                    font = pymupdf.Font(fontbuffer=content)
                    break
    except Exception:
        font = None
    _embedded_cache[key] = font
    return font


_embedded_cache: dict = {}


def clear_embedded_cache():
    _embedded_cache.clear()


def has_all_glyphs(font: pymupdf.Font, text: str) -> bool:
    for ch in set(text):
        if ch.isspace():
            continue
        if not font.has_glyph(ord(ch)):
            return False
    return True


def resolve_font(doc, page, *, pdf_name: str | None, family: str | None, bold: bool, italic: bool,
                 text: str, prefer_embedded=True) -> tuple[pymupdf.Font, str]:
    """Pick the font to write `text` with. Returns (font, description)."""
    if prefer_embedded and pdf_name:
        f = embedded_font(doc, page, pdf_name)
        if f is not None and has_all_glyphs(f, text):
            return f, f"embedded {pdf_name.split('+')[-1]}"
    entry = None
    if family:
        entry = FONT_INDEX.find(family, bold, italic)
    if entry is None and pdf_name:
        entry, bold, italic = FONT_INDEX.match_pdf_font(pdf_name, (FLAG_BOLD if bold else 0) | (FLAG_ITALIC if italic else 0))
    if entry is not None:
        try:
            f = font_from_file(entry.path)
            if has_all_glyphs(f, text):
                return f, entry.fullname or entry.family
        except Exception:
            pass
    # Unicode fallback
    for fam in ("Arial", "Segoe UI", "Arial Unicode MS", "Nirmala UI"):
        e = FONT_INDEX.find(fam, bold, italic)
        if e:
            try:
                f = font_from_file(e.path)
                if has_all_glyphs(f, text):
                    return f, e.fullname
            except Exception:
                pass
    return pymupdf.Font("helv"), "Helvetica"


def resolve_font_file(family: str, bold: bool, italic: bool, text: str) -> tuple[pymupdf.Font, str | None]:
    """Like resolve_font for installed fonts, but also returns the font file path (for pixel rendering)."""
    FONT_INDEX.ensure()
    tried = []
    for fam in (family, "Arial", "Segoe UI", "Arial Unicode MS", "Nirmala UI"):
        e = FONT_INDEX.find(fam, bold, italic) if fam else None
        if e is None or e.path in tried:
            continue
        tried.append(e.path)
        try:
            f = font_from_file(e.path)
        except Exception:
            continue
        if has_all_glyphs(f, text):
            return f, e.path
    return pymupdf.Font("helv"), None


def color_int_to_rgb(c: int) -> tuple[float, float, float]:
    return ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255
