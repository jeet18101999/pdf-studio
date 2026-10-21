"""Document model: wraps a PyMuPDF document with undo/redo and change signals."""
from __future__ import annotations

import os
from pathlib import Path

import pymupdf
from PyQt6.QtCore import QObject, pyqtSignal

MAX_UNDO = 40


class PdfDocument(QObject):
    """Holds the open PDF. Every mutation goes through `begin_change()` so it can be undone."""

    changed = pyqtSignal(object)        # set of page indexes changed, or None = everything
    structure_changed = pyqtSignal()    # page count / order changed
    loaded = pyqtSignal()
    dirty_changed = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.doc: pymupdf.Document | None = None
        self.path: str | None = None
        self._undo: list[tuple[bytes, str]] = []
        self._redo: list[tuple[bytes, str]] = []
        self._dirty = False
        self.suggested_name: str | None = None      # file name offered by Save for documents that have no file yet

    # ------------------------------------------------------------------ io
    def open(self, path: str, password: str | None = None) -> bool:
        data = Path(path).read_bytes()          # load into memory so the file can be overwritten on save
        doc = pymupdf.open(stream=data, filetype=_filetype(path))
        if doc.needs_pass:
            if not password or not doc.authenticate(password):
                raise PermissionError("password")
        if not doc.is_pdf:                      # images, XPS, EPUB... -> convert to PDF
            doc = pymupdf.open("pdf", doc.convert_to_pdf())
        self._set(doc, path)
        return True

    @staticmethod
    def needs_password(path: str) -> bool:
        try:
            with pymupdf.open(stream=Path(path).read_bytes(), filetype=_filetype(path)) as d:
                return d.needs_pass
        except Exception:
            return False

    def new_from(self, doc: pymupdf.Document, path: str | None = None):
        self._set(doc, path)
        self.set_dirty(True)

    def _set(self, doc, path):
        if self.doc is not None:
            self.doc.close()
        self.doc = doc
        self.path = path
        self.suggested_name = None
        self._undo.clear()
        self._redo.clear()
        self.set_dirty(False)
        self.loaded.emit()

    def save(self, path: str | None = None, *, encryption: dict | None = None, compress=False) -> str:
        path = path or self.path
        if not path:
            raise ValueError("No path")
        kw = dict(garbage=3, deflate=True, clean=False)
        if compress:
            kw.update(garbage=4, deflate_images=True, deflate_fonts=True, clean=True)
        if encryption:
            kw.update(encryption=pymupdf.PDF_ENCRYPT_AES_256,
                      user_pw=encryption.get("user") or "",
                      owner_pw=encryption.get("owner") or encryption.get("user") or "",
                      permissions=encryption.get("permissions",
                                                 pymupdf.PDF_PERM_ACCESSIBILITY | pymupdf.PDF_PERM_PRINT
                                                 | pymupdf.PDF_PERM_COPY | pymupdf.PDF_PERM_ANNOTATE))
        data = self.doc.tobytes(**kw)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        self.path = path
        self.set_dirty(False)
        return path

    def close(self):
        if self.doc is not None:
            self.doc.close()
        self.doc = None
        self.path = None
        self._undo.clear()
        self._redo.clear()
        self.set_dirty(False)
        self.loaded.emit()

    # ----------------------------------------------------------- state
    @property
    def is_open(self) -> bool:
        return self.doc is not None

    @property
    def page_count(self) -> int:
        return self.doc.page_count if self.doc else 0

    def page(self, i: int) -> pymupdf.Page:
        return self.doc[i]

    @property
    def dirty(self):
        return self._dirty

    def set_dirty(self, v: bool):
        if v != self._dirty:
            self._dirty = v
            self.dirty_changed.emit(v)

    @property
    def title(self) -> str:
        return Path(self.path).name if self.path else (self.suggested_name or "Untitled.pdf")

    # ----------------------------------------------------------- undo
    def begin_change(self, label: str = "Edit"):
        """Snapshot the document before a mutation."""
        if not self.doc:
            return
        self._undo.append((self.doc.tobytes(garbage=0), label))
        if len(self._undo) > MAX_UNDO:
            self._undo.pop(0)
        self._redo.clear()

    def end_change(self, pages=None, structure=False):
        self.set_dirty(True)
        if structure:
            self.structure_changed.emit()
        self.changed.emit(set(pages) if pages is not None else None)

    def cancel_change(self):
        """Roll back to the snapshot taken by the last begin_change (used by Cancel buttons)."""
        if self._undo:
            data, _ = self._undo.pop()
            self._restore(data)

    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    def undo_label(self):
        return self._undo[-1][1] if self._undo else ""

    def redo_label(self):
        return self._redo[-1][1] if self._redo else ""

    def undo(self):
        if not self._undo:
            return
        data, label = self._undo.pop()
        self._redo.append((self.doc.tobytes(garbage=0), label))
        self._restore(data)

    def redo(self):
        if not self._redo:
            return
        data, label = self._redo.pop()
        self._undo.append((self.doc.tobytes(garbage=0), label))
        self._restore(data)

    def _restore(self, data: bytes):
        before = self._layout()
        self.doc.close()
        self.doc = pymupdf.open("pdf", data)
        self.set_dirty(True)
        if self._layout() != before:
            # pages added, removed, resized or rotated: the page frames must be laid out again.
            # Otherwise re-rendering is enough, which is far cheaper on long documents.
            self.structure_changed.emit()
        self.changed.emit(None)

    def _layout(self) -> list:
        return [(tuple(round(v, 2) for v in p.rect), p.rotation) for p in self.doc]


def _filetype(path: str) -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    return ext or "pdf"
