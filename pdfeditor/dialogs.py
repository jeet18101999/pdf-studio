"""Dialogs: image settings, OCR, organize pages, merge/combine, signature, protect, watermark."""
from __future__ import annotations

import io
from pathlib import Path

import pymupdf
from PIL import Image
from PyQt6.QtCore import QPointF, QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog,
                             QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMessageBox, QProgressBar, QPushButton, QRadioButton, QSlider, QSpinBox,
                             QToolButton, QVBoxLayout, QWidget, QInputDialog)

from . import imagetools, ocr
from .imagetools import ImageSettings
from .style import icon


def pil_to_qpixmap(img: Image.Image, max_side=None) -> QPixmap:
    img = img.convert("RGBA")
    if max_side and max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side))
    q = QImage(img.tobytes("raw", "RGBA"), img.width, img.height, QImage.Format.Format_RGBA8888).copy()
    return QPixmap.fromImage(q)


def checker_pixmap(w, h, s=10) -> QPixmap:
    pm = QPixmap(w, h)
    pm.fill(QColor("#ffffff"))
    p = QPainter(pm)
    for y in range(0, h, s):
        for x in range(0, w, s):
            if (x // s + y // s) % 2:
                p.fillRect(x, y, s, s, QColor("#e4e4e4"))
    p.end()
    return pm


def page_thumb(page: pymupdf.Page, width=150) -> QPixmap:
    zoom = width / max(1, page.rect.width)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return QPixmap.fromImage(QImage(pix.samples, pix.width, pix.height, pix.stride,
                                    QImage.Format.Format_RGB888).copy())


def color_button(rgb, on_change) -> QPushButton:
    b = QPushButton()
    b.setFixedSize(34, 24)

    def paint(c):
        b.setStyleSheet(f"background:{c.name()}; border:1px solid #999; border-radius:4px;")
    col = QColor.fromRgbF(*rgb[:3])
    paint(col)

    def pick():
        c = QColorDialog.getColor(QColor(b.palette().button().color()), b, "Colour")
        if c.isValid():
            paint(c)
            on_change((c.redF(), c.greenF(), c.blueF()))
    b.clicked.connect(pick)
    b.set_rgb = lambda rgb: paint(QColor.fromRgbF(*rgb[:3]))
    return b


# ============================================================================ image settings
class ImageSettingsDialog(QDialog):
    """Preview + settings: background removal, opacity, crop, rotate/flip, colour, text wrapping."""

    def __init__(self, img: Image.Image, settings: ImageSettings | None = None, parent=None, title="Image settings"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.src = img.convert("RGBA")
        preview_src = self.src.copy()
        preview_src.thumbnail((700, 700))
        self.preview_src = preview_src
        self.s = settings or ImageSettings()
        self.resize(980, 640)

        root = QHBoxLayout(self)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(520, 520)
        self.preview.setStyleSheet("background:#fff; border:1px solid #ddd; border-radius:6px;")
        root.addWidget(self.preview, 1)

        side = QVBoxLayout()
        root.addLayout(side)

        # background
        g = QGroupBox("Background")
        f = QFormLayout(g)
        self.bg_mode = QComboBox()
        self.bg_mode.addItem("Keep background", imagetools.BG_NONE)
        self.bg_mode.addItem("Remove white background", imagetools.BG_WHITE)
        self.bg_mode.addItem("Remove colour (edge fill)", imagetools.BG_COLOR)
        self.bg_mode.addItem("Remove background automatically (AI cut-out)", imagetools.BG_AUTO)
        self.bg_mode.setCurrentIndex(max(0, self.bg_mode.findData(self.s.bg_mode)))
        f.addRow("Mode", self.bg_mode)
        row = QHBoxLayout()
        self.bg_color_btn = color_button(tuple(c / 255 for c in self.s.bg_color), self._set_bg_color)
        detect = QPushButton("Detect")
        detect.clicked.connect(self._detect_bg)
        row.addWidget(self.bg_color_btn)
        row.addWidget(detect)
        row.addStretch()
        f.addRow("Colour", row)
        self.tol = self._slider(0, 150, self.s.tolerance)
        f.addRow("Tolerance", self.tol)
        self.feather = self._slider(0, 8, self.s.feather)
        f.addRow("Edge softness", self.feather)
        side.addWidget(g)

        # appearance
        g = QGroupBox("Appearance")
        f = QFormLayout(g)
        self.opacity = self._slider(0, 100, int(self.s.opacity * 100))
        f.addRow("Opacity", self.opacity)
        self.bright = self._slider(20, 200, int(self.s.brightness * 100))
        f.addRow("Brightness", self.bright)
        self.contrast = self._slider(20, 200, int(self.s.contrast * 100))
        f.addRow("Contrast", self.contrast)
        self.sat = self._slider(0, 200, int(self.s.saturation * 100))
        f.addRow("Saturation", self.sat)
        self.gray = QCheckBox("Grayscale")
        self.gray.setChecked(self.s.grayscale)
        self.gray.toggled.connect(self._update)
        f.addRow("", self.gray)
        side.addWidget(g)

        # transform
        g = QGroupBox("Transform")
        lay = QGridLayout(g)
        for i, (name, ic, fn) in enumerate([("Rotate left", "rotl", lambda: self._rot(-90)),
                                             ("Rotate right", "rotr", lambda: self._rot(90)),
                                             ("Flip horizontal", "flip", self._fliph),
                                             ("Flip vertical", "flip", self._flipv)]):
            b = QToolButton()
            b.setText(name)
            b.setIcon(icon(ic, mirror=(name == "Rotate right")))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.clicked.connect(fn)
            lay.addWidget(b, i // 2, i % 2)
        crop_row = QHBoxLayout()
        self.crops = []
        for lbl, v in zip("LTRB", self.s.crop):
            sp = QSpinBox()
            sp.setRange(0, 45)
            sp.setSuffix("%")
            sp.setValue(int(v * 100))
            sp.setToolTip({"L": "Crop left", "T": "Crop top", "R": "Crop right", "B": "Crop bottom"}[lbl])
            sp.valueChanged.connect(self._update)
            crop_row.addWidget(QLabel(lbl))
            crop_row.addWidget(sp)
            self.crops.append(sp)
        lay.addWidget(QLabel("Crop"), 2, 0, 1, 2)
        lay.addLayout(crop_row, 3, 0, 1, 2)
        side.addWidget(g)

        # layout
        g = QGroupBox("Position with text")
        v = QVBoxLayout(g)
        self.front = QRadioButton("In front of text")
        self.behind = QRadioButton("Behind text (text stays readable on top)")
        (self.behind if self.s.behind_text else self.front).setChecked(True)
        self.keep = QCheckBox("Lock aspect ratio when resizing")
        self.keep.setChecked(self.s.keep_aspect)
        v.addWidget(self.front)
        v.addWidget(self.behind)
        v.addWidget(self.keep)
        side.addWidget(g)
        side.addStretch()

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        reset = bb.addButton("Reset", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(self._reset)
        side.addWidget(bb)
        self.bg_mode.currentIndexChanged.connect(self._update)
        self._update()

    def _slider(self, lo, hi, val):
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        s.setValue(val)
        s.valueChanged.connect(self._update)
        return s

    def _set_bg_color(self, rgb):
        self.s.bg_color = tuple(int(c * 255) for c in rgb)
        if self.bg_mode.currentData() == imagetools.BG_NONE:
            self.bg_mode.setCurrentIndex(self.bg_mode.findData(imagetools.BG_COLOR))
        self._update()

    def _detect_bg(self):
        c = imagetools.dominant_border_color(self.src)
        self.s.bg_color = c
        self.bg_color_btn.set_rgb(tuple(v / 255 for v in c))
        self.bg_mode.setCurrentIndex(self.bg_mode.findData(imagetools.BG_COLOR))
        self._update()

    def _rot(self, d):
        self.s.rotate = (self.s.rotate + d) % 360
        self._update()

    def _fliph(self):
        self.s.flip_h = not self.s.flip_h
        self._update()

    def _flipv(self):
        self.s.flip_v = not self.s.flip_v
        self._update()

    def _reset(self):
        self.s = ImageSettings()
        for w, v in ((self.opacity, 100), (self.bright, 100), (self.contrast, 100), (self.sat, 100),
                     (self.tol, 30), (self.feather, 1)):
            w.setValue(v)
        for c in self.crops:
            c.setValue(0)
        self.gray.setChecked(False)
        self.bg_mode.setCurrentIndex(0)
        self._update()

    def settings(self) -> ImageSettings:
        s = self.s
        s.bg_mode = self.bg_mode.currentData()
        s.tolerance = self.tol.value()
        s.feather = self.feather.value()
        s.opacity = self.opacity.value() / 100
        s.brightness = self.bright.value() / 100
        s.contrast = self.contrast.value() / 100
        s.saturation = self.sat.value() / 100
        s.grayscale = self.gray.isChecked()
        s.crop = tuple(c.value() / 100 for c in self.crops)
        s.behind_text = self.behind.isChecked()
        s.keep_aspect = self.keep.isChecked()
        return s

    def _update(self, *_):
        s = self.settings()
        try:
            out = imagetools.apply_settings(self.preview_src, s)
        except Exception as ex:
            self.preview.setText(str(ex))
            return
        pm = pil_to_qpixmap(out, 500)
        canvas = checker_pixmap(pm.width(), pm.height())
        p = QPainter(canvas)
        p.drawPixmap(0, 0, pm)
        p.end()
        self.preview.setPixmap(canvas)

    def result_image(self) -> Image.Image:
        return imagetools.apply_settings(self.src, self.settings())


# ============================================================================ OCR
class OcrWorker(QThread):
    progress = pyqtSignal(int, str)            # percent, message
    page_done = pyqtSignal(object)             # PageOcr
    failed = pyqtSignal(str)

    def __init__(self, pdf_bytes, pages, langs, dpi, mode):
        super().__init__()
        self.data, self.pages, self.langs, self.dpi, self.mode = pdf_bytes, pages, langs, dpi, mode
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            if len(self.pages) >= 2:
                self._run_parallel()
            else:
                self._run_sequential()
            self.progress.emit(100, "Done")
        except Exception as ex:
            self.failed.emit(str(ex))

    def _run_parallel(self):
        """Several pages: recognise them side by side on the spare CPU cores."""
        n = len(self.pages)
        workers = ocr.ocr_workers(n)
        self.progress.emit(1, f"Recognizing {n} pages, {workers} at a time…")
        done = 0
        for res in ocr.ocr_pages_parallel(self.data, self.pages, langs=self.langs, dpi=self.dpi, mode=self.mode,
                                          workers=workers, should_stop=lambda: self._stop):
            done += 1
            self.page_done.emit(res)
            self.progress.emit(int(done / n * 100), f"Page {res.page + 1} done ({done} of {n})")

    def _run_sequential(self):
        """One page: no worker processes to start, and step-by-step progress."""
        matcher = ocr.FontMatcher() if self.mode == ocr.MODE_EDITABLE else None
        n = len(self.pages)
        for i, pno in enumerate(self.pages):
            if self._stop:
                break
            base = int(i / n * 100)

            def prog(msg, pct, base=base, pno=pno):
                self.progress.emit(base + int(pct / n), f"Page {pno + 1}: {msg}")
            prog("Rendering…", 0)
            res = ocr.ocr_page(self.data, pno, langs=self.langs, dpi=self.dpi, mode=self.mode,
                               matcher=matcher, progress=prog)
            self.page_done.emit(res)


class OcrDialog(QDialog):
    def __init__(self, page_count, current_page, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recognize Text (OCR)")
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        if not ocr.find_tesseract():
            warn = QLabel("⚠ Tesseract OCR was not found. Install it from "
                          "<a href='https://github.com/UB-Mannheim/tesseract/wiki'>UB-Mannheim/tesseract</a>.")
            warn.setOpenExternalLinks(True)
            warn.setWordWrap(True)
            v.addWidget(warn)

        g = QGroupBox("Pages")
        f = QVBoxLayout(g)
        self.r_cur = QRadioButton(f"Current page ({current_page + 1})")
        self.r_all = QRadioButton(f"All pages (1–{page_count})")
        self.r_range = QRadioButton("Pages:")
        self.range_edit = QLineEdit(f"1-{page_count}")
        self.r_cur.setChecked(True)
        row = QHBoxLayout()
        row.addWidget(self.r_range)
        row.addWidget(self.range_edit)
        f.addWidget(self.r_cur)
        f.addWidget(self.r_all)
        f.addLayout(row)
        v.addWidget(g)

        g = QGroupBox("Output")
        f = QVBoxLayout(g)
        self.m_edit = QRadioButton("Editable text — keep the scan, detect fonts so any text can be edited in place")
        self.m_search = QRadioButton("Searchable only — add an invisible text layer (search / copy)")
        self.m_edit.setChecked(True)
        f.addWidget(self.m_edit)
        f.addWidget(self.m_search)
        v.addWidget(g)

        g = QGroupBox("Settings")
        f = QFormLayout(g)
        self.lang = QListWidget()
        self.lang.setMaximumHeight(120)
        for code in ocr.available_languages() or ["eng"]:
            it = QListWidgetItem(code)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if code == "eng" else Qt.CheckState.Unchecked)
            self.lang.addItem(it)
        f.addRow("Languages", self.lang)
        self.dpi = QComboBox()
        for d in (200, 300, 400):
            self.dpi.addItem(f"{d} dpi", d)
        self.dpi.setCurrentIndex(1)
        f.addRow("Resolution", self.dpi)
        v.addWidget(g)

        self.page_count, self.current_page = page_count, current_page
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Recognize Text")
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def pages(self) -> list[int]:
        if self.r_cur.isChecked():
            return [self.current_page]
        if self.r_all.isChecked():
            return list(range(self.page_count))
        return parse_ranges(self.range_edit.text(), self.page_count)

    def langs(self) -> str:
        codes = [self.lang.item(i).text() for i in range(self.lang.count())
                 if self.lang.item(i).checkState() == Qt.CheckState.Checked]
        return "+".join(codes) or "eng"

    def mode(self):
        return ocr.MODE_EDITABLE if self.m_edit.isChecked() else ocr.MODE_SEARCHABLE


class ProgressDialog(QDialog):
    cancelled = pyqtSignal()

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        v = QVBoxLayout(self)
        self.label = QLabel("Starting…")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        v.addWidget(self.label)
        v.addWidget(self.bar)
        b = QPushButton("Cancel")
        b.clicked.connect(self.cancelled.emit)
        v.addWidget(b, alignment=Qt.AlignmentFlag.AlignRight)

    def set(self, pct, msg):
        self.bar.setValue(pct)
        self.label.setText(msg)


def parse_ranges(text: str, count: int) -> list[int]:
    out = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            a = int(a) if a else 1
            b = int(b) if b else count
            out.extend(range(max(1, a) - 1, min(count, b)))
        else:
            n = int(part)
            if 1 <= n <= count:
                out.append(n - 1)
    return out


# ============================================================================ organize
class OrganizeDialog(QDialog):
    """Page grid: drag to reorder, rotate, delete, insert, duplicate, extract, split."""

    def __init__(self, pdoc, parent=None):
        super().__init__(parent)
        self.pdoc = pdoc
        self.setWindowTitle("Organize Pages")
        self.resize(1100, 720)
        v = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(90, 320)
        self.zoom.setValue(160)
        self.zoom.setMaximumWidth(160)
        buttons = [("Rotate left", "rotl", lambda: self.rotate(-90)), ("Rotate right", "rotr", lambda: self.rotate(90)),
                   ("Delete", "delete", self.delete), ("Duplicate", "copy", self.duplicate),
                   ("Insert blank", "insert", self.insert_blank), ("Insert from file", "open", self.insert_file),
                   ("Extract", "export", self.extract), ("Split", "split", self.split),
                   ("Move to start", "prev", lambda: self.move_to(0)), ("Move to end", "next", lambda: self.move_to(-1))]
        for text, ic, fn in buttons:
            b = QToolButton()
            b.setText(text)
            b.setIcon(icon(ic, mirror=(text == "Rotate right")))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setIconSize(QSize(22, 22))
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch()
        bar.addWidget(QLabel("Size"))
        bar.addWidget(self.zoom)
        v.addLayout(bar)

        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Snap)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setSpacing(12)
        self.list.setStyleSheet("QListWidget{background:#ececec;}")
        self.list.model().rowsMoved.connect(self._on_moved)
        v.addWidget(self.list, 1)
        self.zoom.valueChanged.connect(self.reload)

        hint = QLabel("Drag pages to reorder • Ctrl/Shift-click to select several • Changes can be undone")
        hint.setObjectName("hint")
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Done")
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(hint)
        row.addStretch()
        row.addWidget(bb)
        v.addLayout(row)

        self.pdoc.begin_change("Organize pages")
        self.touched = False
        self.reload()

    def reject(self):
        if self.touched:
            self.pdoc.cancel_change()
            self.pdoc.structure_changed.emit()
            self.pdoc.changed.emit(None)
        else:
            self.pdoc.cancel_change()
        super().reject()

    def accept(self):
        if not self.touched:
            self.pdoc._undo.pop() if self.pdoc._undo else None
        super().accept()

    def reload(self, *_):
        sel = {i.row() for i in self.list.selectedIndexes()}
        w = self.zoom.value()
        self.list.clear()
        self.list.setIconSize(QSize(w, int(w * 1.35)))
        for i, page in enumerate(self.pdoc.doc):
            it = QListWidgetItem(QIcon(page_thumb(page, w)), str(i + 1))
            it.setData(Qt.ItemDataRole.UserRole, i)
            it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.list.addItem(it)
            if i in sel:
                it.setSelected(True)

    def selected(self) -> list[int]:
        return sorted(i.row() for i in self.list.selectedIndexes())

    def _changed(self):
        self.touched = True
        self.pdoc.set_dirty(True)
        self.pdoc.structure_changed.emit()
        self.pdoc.changed.emit(None)
        self.reload()

    def _on_moved(self, *_):
        order = [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]
        if order != sorted(order):
            self.pdoc.doc.select(order)
            self._changed()

    def rotate(self, d):
        for i in self.selected():
            p = self.pdoc.doc[i]
            p.set_rotation((p.rotation + d) % 360)
        self._changed()

    def delete(self):
        sel = self.selected()
        if not sel:
            return
        if len(sel) == self.pdoc.page_count:
            QMessageBox.warning(self, "Delete", "A document must keep at least one page.")
            return
        self.pdoc.doc.delete_pages(sel)
        self._changed()

    def duplicate(self):
        sel = self.selected()
        if not sel:
            return
        # fullcopy_page makes an independent page (select() with repeats would share one page object)
        for i in reversed(sel):
            self.pdoc.doc.fullcopy_page(i, i + 1)
        self._changed()

    def insert_blank(self):
        sel = self.selected()
        at = (sel[-1] + 1) if sel else self.pdoc.page_count
        ref = self.pdoc.doc[max(0, at - 1)].rect
        self.pdoc.doc.new_page(at, width=ref.width, height=ref.height)
        self._changed()

    def insert_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Insert pages from", "", "PDF and images (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp)")
        if not path:
            return
        sel = self.selected()
        at = (sel[-1] + 1) if sel else self.pdoc.page_count
        src = open_as_pdf(path)
        self.pdoc.doc.insert_pdf(src, start_at=at)
        self._changed()

    def move_to(self, pos):
        sel = self.selected()
        if not sel:
            return
        rest = [i for i in range(self.pdoc.page_count) if i not in sel]
        order = sel + rest if pos == 0 else rest + sel
        self.pdoc.doc.select(order)
        self._changed()

    def extract(self):
        sel = self.selected()
        if not sel:
            QMessageBox.information(self, "Extract", "Select the pages to extract first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save extracted pages", "extracted.pdf", "PDF (*.pdf)")
        if not path:
            return
        out = pymupdf.open()
        for i in sel:
            out.insert_pdf(self.pdoc.doc, from_page=i, to_page=i)
        out.save(path, garbage=3, deflate=True)
        if QMessageBox.question(self, "Extract", "Delete the extracted pages from this document?") \
                == QMessageBox.StandardButton.Yes and len(sel) < self.pdoc.page_count:
            self.pdoc.doc.delete_pages(sel)
            self._changed()

    def split(self):
        n, ok = QInputDialog.getInt(self, "Split document", "Pages per file:", 1, 1, max(1, self.pdoc.page_count))
        if not ok:
            return
        folder = QFileDialog.getExistingDirectory(self, "Output folder")
        if not folder:
            return
        stem = Path(self.pdoc.path or "document.pdf").stem
        files = 0
        for start in range(0, self.pdoc.page_count, n):
            out = pymupdf.open()
            out.insert_pdf(self.pdoc.doc, from_page=start, to_page=min(start + n, self.pdoc.page_count) - 1)
            out.save(str(Path(folder) / f"{stem}_part{files + 1}.pdf"), garbage=3, deflate=True)
            files += 1
        QMessageBox.information(self, "Split", f"Created {files} files in {folder}")


def open_as_pdf(path: str) -> pymupdf.Document:
    d = pymupdf.open(path)
    if d.is_pdf:
        return d
    return pymupdf.open("pdf", d.convert_to_pdf())


# ============================================================================ merge
class MergeDialog(QDialog):
    """Combine PDFs / images into one PDF with optional page ranges."""

    def __init__(self, parent=None, initial: list[str] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Combine Files")
        self.resize(760, 520)
        v = QVBoxLayout(self)
        t = QLabel("Combine files into a single PDF")
        t.setObjectName("panelTitle")
        v.addWidget(t)
        row = QHBoxLayout()
        for text, fn in (("Add files…", self.add), ("Remove", self.remove), ("Move up", lambda: self.shift(-1)),
                         ("Move down", lambda: self.shift(1)), ("Page range…", self.set_range),
                         ("Sort by name", self.sort)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        v.addLayout(row)
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setIconSize(QSize(48, 64))
        self.list.setAcceptDrops(True)
        v.addWidget(self.list, 1)
        self.bookmarks = QCheckBox("Add a bookmark for each file")
        self.bookmarks.setChecked(True)
        v.addWidget(self.bookmarks)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Combine")
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.setAcceptDrops(True)
        for p in initial or []:
            self._add_path(p)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            self._add_path(u.toLocalFile())

    def _add_path(self, path):
        try:
            d = open_as_pdf(path)
            thumb = page_thumb(d[0], 48)
            n = d.page_count
        except Exception as ex:
            QMessageBox.warning(self, "Combine", f"Cannot open {path}:\n{ex}")
            return
        it = QListWidgetItem(QIcon(thumb), "")
        it.setData(Qt.ItemDataRole.UserRole, {"path": path, "pages": n, "range": ""})
        self._label(it)
        self.list.addItem(it)

    def _label(self, it):
        d = it.data(Qt.ItemDataRole.UserRole)
        rng = d["range"] or "all pages"
        it.setText(f"{Path(d['path']).name}\n{d['pages']} page(s) — {rng}")

    def add(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add files", "",
                                                "PDF and images (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.xps *.epub)")
        for p in paths:
            self._add_path(p)

    def remove(self):
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))

    def shift(self, d):
        r = self.list.currentRow()
        if r < 0 or not (0 <= r + d < self.list.count()):
            return
        it = self.list.takeItem(r)
        self.list.insertItem(r + d, it)
        self.list.setCurrentRow(r + d)

    def sort(self):
        items = [self.list.takeItem(0) for _ in range(self.list.count())]
        items.sort(key=lambda it: Path(it.data(Qt.ItemDataRole.UserRole)["path"]).name.lower())
        for it in items:
            self.list.addItem(it)

    def set_range(self):
        it = self.list.currentItem()
        if not it:
            return
        d = it.data(Qt.ItemDataRole.UserRole)
        text, ok = QInputDialog.getText(self, "Page range", f"Pages of {Path(d['path']).name} (e.g. 1-3,5):", text=d["range"])
        if ok:
            d["range"] = text.strip()
            it.setData(Qt.ItemDataRole.UserRole, d)
            self._label(it)

    def _accept(self):
        if self.list.count() == 0:
            return
        self.accept()

    def build(self) -> pymupdf.Document:
        out = pymupdf.open()
        toc = []
        for i in range(self.list.count()):
            d = self.list.item(i).data(Qt.ItemDataRole.UserRole)
            src = open_as_pdf(d["path"])
            start = out.page_count
            pages = parse_ranges(d["range"], src.page_count) if d["range"] else list(range(src.page_count))
            for p in pages:
                out.insert_pdf(src, from_page=p, to_page=p)
            if self.bookmarks.isChecked() and out.page_count > start:
                toc.append([1, Path(d["path"]).stem, start + 1])
        if toc:
            out.set_toc(toc)
        return out


# ============================================================================ signature
class SignaturePad(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(560, 220)
        self.strokes: list[list[tuple[int, int]]] = []
        self.pen_width = 3
        self.color = (0, 0, 80)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("white"))
        p.setPen(QPen(QColor("#cccccc"), 1, Qt.PenStyle.DashLine))
        y = int(self.height() * 0.75)
        p.drawLine(20, y, self.width() - 20, y)
        pen = QPen(QColor(*self.color), self.pen_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                   Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        for s in self.strokes:
            if len(s) == 1:
                p.drawPoint(*s[0])
            path = QPainterPath(QPointF(*s[0]))
            for pt in s[1:]:
                path.lineTo(QPointF(*pt))
            p.drawPath(path)

    def mousePressEvent(self, e):
        self.strokes.append([(int(e.position().x()), int(e.position().y()))])
        self.update()

    def mouseMoveEvent(self, e):
        if self.strokes:
            self.strokes[-1].append((int(e.position().x()), int(e.position().y())))
            self.update()

    def clear(self):
        self.strokes.clear()
        self.update()


class SignatureDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fill & Sign — create signature")
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Draw your signature, type it, or use an image:"))
        self.pad = SignaturePad()
        v.addWidget(self.pad)
        row = QHBoxLayout()
        self.typed = QLineEdit()
        self.typed.setPlaceholderText("…or type your name")
        self.font = QComboBox()
        for f in ("Segoe Script", "Lucida Handwriting", "Brush Script MT", "Ink Free", "Gabriola"):
            self.font.addItem(f)
        row.addWidget(self.typed, 1)
        row.addWidget(self.font)
        v.addLayout(row)
        row = QHBoxLayout()
        clear = QPushButton("Clear")
        clear.clicked.connect(self.pad.clear)
        img_btn = QPushButton("Use image…")
        img_btn.clicked.connect(self._use_image)
        col = QComboBox()
        col.addItems(["Dark blue", "Black", "Blue"])
        col.currentIndexChanged.connect(lambda i: (setattr(self.pad, "color", [(0, 0, 80), (0, 0, 0), (20, 60, 200)][i]),
                                                   self.pad.update()))
        row.addWidget(clear)
        row.addWidget(img_btn)
        row.addWidget(col)
        row.addStretch()
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Place signature")
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.image_path = None

    def _use_image(self):
        p, _ = QFileDialog.getOpenFileName(self, "Signature image", "", "Images (*.png *.jpg *.jpeg *.bmp)")
        if p:
            self.image_path = p
            self.accept()

    def result_image(self) -> Image.Image | None:
        if self.image_path:
            img = imagetools.load_image(Path(self.image_path).read_bytes())
            return imagetools.remove_color(img, (255, 255, 255), 40, 1, flood=False)
        if self.typed.text().strip():
            from PyQt6.QtGui import QFont, QFontMetrics
            f = QFont(self.font.currentText())
            f.setPixelSize(96)
            fm = QFontMetrics(f)
            w, h = fm.horizontalAdvance(self.typed.text()) + 40, fm.height() + 20
            qi = QImage(w, h, QImage.Format.Format_RGBA8888)
            qi.fill(Qt.GlobalColor.transparent)
            p = QPainter(qi)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setFont(f)
            p.setPen(QColor(*self.pad.color))
            p.drawText(20, fm.ascent() + 10, self.typed.text())
            p.end()
            buf = bytes(qi.constBits().asarray(qi.sizeInBytes()))
            return Image.frombytes("RGBA", (w, h), buf)
        if self.pad.strokes:
            return imagetools.signature_to_image(self.pad.strokes, (self.pad.width(), self.pad.height()),
                                                 self.pad.pen_width, self.pad.color)
        return None


# ============================================================================ protect / watermark
class CadExportDialog(QDialog):
    """How to turn an engineering drawing into a CAD (DXF) file."""

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export to CAD (DXF)")
        detected = info.get("scale_mm_per_pt")
        f = QFormLayout(self)
        if info.get("scanned"):
            head = "This page is a scan — the drawing will be traced from the image."
        else:
            head = f"{info.get('segments', 0):,} drawn lines found on this page."
            if not info.get("drawing"):
                head += " This does not look like an engineering drawing."
        f.addRow(QLabel(head))

        self.auto = QRadioButton(f"From the drawing's own dimensions ({detected:.4f} mm per point)"
                                 if detected else "From the drawing's own dimensions (none found here)")
        self.auto.setEnabled(detected is not None)
        self.paper = QRadioButton("Paper size — the sheet as printed (1 point = 0.353 mm)")
        self.custom = QRadioButton("Set the scale myself")
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(4)
        self.spin.setRange(0.0001, 1000.0)
        self.spin.setValue(0.3528)
        self.spin.setSuffix(" mm per point")
        self.spin.setEnabled(False)
        self.custom.toggled.connect(self.spin.setEnabled)
        (self.auto if detected else self.paper).setChecked(True)
        box = QGroupBox("Scale")
        v = QVBoxLayout(box)
        for widget in (self.auto, self.paper, self.custom, self.spin):
            v.addWidget(widget)
        f.addRow(box)

        self.text = QCheckBox("Keep the text labels")
        self.text.setChecked(True)
        self.blocks = QCheckBox("Group each view into its own block")
        self.trace = QCheckBox("Trace scanned drawings from the page image")
        self.trace.setChecked(True)
        self.version = QComboBox()
        self.version.addItems(["AutoCAD 2010", "AutoCAD R12 (opens in the oldest software)"])
        f.addRow(self.text)
        f.addRow(self.blocks)
        f.addRow(self.trace)
        f.addRow("DXF version", self.version)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def options(self):
        from .cad import PT_MM, CadOptions
        scale = None if self.auto.isChecked() else (PT_MM if self.paper.isChecked() else self.spin.value())
        return CadOptions(scale_mm_per_pt=scale, include_text=self.text.isChecked(),
                          blocks=self.blocks.isChecked(), vectorise_scans=self.trace.isChecked(),
                          version="R2010" if self.version.currentIndex() == 0 else "R12")


class ImagesToPdfDialog(QDialog):
    """Create a PDF from images: put them in order, choose the page layout."""

    def __init__(self, paths=None, parent=None):
        super().__init__(parent)
        from . import imagepdf
        self._imagepdf = imagepdf
        self.setWindowTitle("Create PDF from Images")
        self.resize(760, 600)
        self.result_doc = None
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Each image becomes a page, in this order (multi-page TIFFs add every page). "
                           "Drag the pictures to reorder them."))
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setIconSize(QSize(110, 110))
        self.list.setGridSize(QSize(140, 150))
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Snap)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setWordWrap(True)
        v.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, fn in (("Add images…", self.add_dialog), ("Remove", self.remove_selected),
                         ("◀ Move", lambda: self.move(-1)), ("Move ▶", lambda: self.move(1))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        self.count = QLabel()
        row.addWidget(self.count)
        v.addLayout(row)

        form = QFormLayout()
        self.size = QComboBox()
        self.size.addItem("Same as each image", "fit")
        for key in imagepdf.PAGE_SIZES:
            self.size.addItem(key, key)
        self.orient = QComboBox()
        for text, key in (("Automatic (follow each image)", "auto"), ("Portrait", "portrait"), ("Landscape", "landscape")):
            self.orient.addItem(text, key)
        self.margin = QDoubleSpinBox()
        self.margin.setRange(0, 50)
        self.margin.setDecimals(0)
        self.margin.setSuffix(" mm")
        self.place = QComboBox()
        for text, key in (("Fit the whole image", "fit"), ("Fill the page (edges cropped)", "fill"),
                          ("Actual size (shrunk only if too big)", "actual")):
            self.place.addItem(text, key)
        form.addRow("Page size", self.size)
        form.addRow("Orientation", self.orient)
        form.addRow("Margin", self.margin)
        form.addRow("Image on page", self.place)
        v.addLayout(form)
        self.size.currentIndexChanged.connect(self._sync)
        self.list.model().rowsInserted.connect(self._sync)
        self.list.model().rowsRemoved.connect(self._sync)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Create PDF")
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.ok_button = bb.button(QDialogButtonBox.StandardButton.Ok)
        v.addWidget(bb)
        self.add_paths(paths or [])
        self._sync()

    def _sync(self, *_):
        fixed = self.size.currentData() != "fit"
        self.orient.setEnabled(fixed)
        self.place.setEnabled(fixed)
        n = self.list.count()
        self.count.setText(f"{n} image{'s' if n != 1 else ''}")
        self.ok_button.setEnabled(n > 0)

    def add_paths(self, paths):
        skipped = []
        for p in paths:
            if not self._imagepdf.is_image_file(p):
                skipped.append(Path(p).name)
                continue
            item = QListWidgetItem(Path(p).name)
            item.setData(Qt.ItemDataRole.UserRole, p)
            item.setToolTip(p)
            try:
                with Image.open(p) as img:
                    img.draft("RGB", (220, 220))
                    thumb = img.copy()
                    thumb.thumbnail((220, 220))
                item.setIcon(QIcon(pil_to_qpixmap(thumb)))
            except Exception:
                item.setIcon(icon("image"))
            self.list.addItem(item)
        if skipped:
            QMessageBox.information(self, "Create PDF from Images", "These are not images and were skipped:\n" +
                                    "\n".join(skipped[:10]))
        self._sync()

    def add_dialog(self):
        exts = " ".join("*" + e for e in sorted(self._imagepdf.IMAGE_EXT))
        paths, _ = QFileDialog.getOpenFileNames(self, "Add images", "", f"Images ({exts})")
        self.add_paths(paths)

    def remove_selected(self):
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self._sync()

    def move(self, step):
        row = self.list.currentRow()
        if row < 0:
            return
        new = max(0, min(self.list.count() - 1, row + step))
        if new == row:
            return
        item = self.list.takeItem(row)
        self.list.insertItem(new, item)
        self.list.setCurrentRow(new)

    def paths(self) -> list[str]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]

    def options(self):
        return self._imagepdf.ImagePdfOptions(page_size=self.size.currentData(), orientation=self.orient.currentData(),
                                              margin_mm=self.margin.value(), placement=self.place.currentData())

    def accept(self):
        from PyQt6.QtWidgets import QApplication
        paths = self.paths()
        if not paths:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.result_doc = self._imagepdf.images_to_pdf(paths, self.options())
        except ValueError as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Create PDF from Images", str(ex))
            return
        QApplication.restoreOverrideCursor()
        super().accept()


class SplitDialog(QDialog):
    """Split the open document into several PDF files."""

    def __init__(self, pdoc, parent=None):
        super().__init__(parent)
        from . import splitpdf
        self._split = splitpdf
        self.pdoc = pdoc
        self.written: list[str] = []
        doc = pdoc.doc
        n = doc.page_count
        self.setWindowTitle("Split PDF")
        self.resize(640, 600)
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"<b>{pdoc.title}</b> — {n} page{'s' if n != 1 else ''}"))

        box = QGroupBox("How to split")
        g = QGridLayout(box)
        self.m_every = QRadioButton("Every")
        self.every = QSpinBox()
        self.every.setRange(1, max(1, n))
        self.every.setSuffix(" page(s) per file")
        self.m_ranges = QRadioButton("By page ranges")
        self.ranges = QLineEdit()
        self.ranges.setPlaceholderText("e.g. 1-3, 4-10, 11-   (each range becomes one file)")
        self.m_bookmarks = QRadioButton("At each top-level bookmark (chapter)")
        has_bookmarks = any(level == 1 for level, _, _ in doc.get_toc(simple=True))
        self.m_bookmarks.setEnabled(has_bookmarks)
        if not has_bookmarks:
            self.m_bookmarks.setText(self.m_bookmarks.text() + " — this document has no bookmarks")
        self.m_size = QRadioButton("Files no larger than")
        self.size_mb = QDoubleSpinBox()
        self.size_mb.setRange(0.1, 5000)
        self.size_mb.setValue(10)
        self.size_mb.setSuffix(" MB")
        g.addWidget(self.m_every, 0, 0)
        g.addWidget(self.every, 0, 1)
        g.addWidget(self.m_ranges, 1, 0)
        g.addWidget(self.ranges, 1, 1)
        g.addWidget(self.m_bookmarks, 2, 0, 1, 2)
        g.addWidget(self.m_size, 3, 0)
        g.addWidget(self.size_mb, 3, 1)
        v.addWidget(box)

        v.addWidget(QLabel("Files that will be created:"))
        self.preview = QListWidget()
        v.addWidget(self.preview, 1)

        form = QFormLayout()
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(str(Path(pdoc.path).parent) if pdoc.path else str(Path.home() / "Documents"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        self.pattern = QLineEdit("{name}_part{n}")
        self.pattern.setToolTip("{name} = this document's name, {n} = part number, {title} = bookmark title")
        form.addRow("Save to folder", folder_row)
        form.addRow("File names", self.pattern)
        v.addLayout(form)
        v.addWidget(QLabel("<small>Use {name}, {n} and {title} (the bookmark's title) in file names.</small>"))

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = bb.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("Split")
        self.ok_button.setObjectName("primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        self.m_every.setChecked(True)
        for w in (self.m_every, self.m_ranges, self.m_bookmarks, self.m_size):
            w.toggled.connect(self.refresh)
        self.every.valueChanged.connect(lambda _: (self.m_every.setChecked(True), self.refresh()))
        self.ranges.textEdited.connect(lambda _: (self.m_ranges.setChecked(True), self.refresh()))
        self.size_mb.valueChanged.connect(lambda _: (self.m_size.setChecked(True), self.refresh()))
        self.pattern.textEdited.connect(lambda _: self.refresh())
        self.refresh()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Save the parts to", self.folder.text())
        if d:
            self.folder.setText(d)

    def plan(self, measure=False):
        doc = self.pdoc.doc
        if self.m_every.isChecked():
            return self._split.plan_every(doc.page_count, self.every.value())
        if self.m_ranges.isChecked():
            return self._split.plan_ranges(doc.page_count, self.ranges.text())
        if self.m_bookmarks.isChecked():
            return self._split.plan_bookmarks(doc)
        if not measure:
            return None
        return self._split.plan_size(doc, int(self.size_mb.value() * 1024 * 1024))

    def _names(self, parts):
        stem = Path(self.pdoc.path or self.pdoc.title).stem
        width = len(str(len(parts)))
        out = []
        for n, p in enumerate(parts, 1):
            name = self.pattern.text().format(name=stem, n=str(n).zfill(width),
                                              title=self._split._safe_name(p.title or f"part{n}"))
            out.append(self._split._safe_name(name) + ".pdf")
        return out

    def refresh(self, *_):
        self.preview.clear()
        try:
            parts = self.plan()
            if parts is None:
                self.preview.addItem(f"Pages are added to each file while it stays under {self.size_mb.value():g} MB "
                                     "— the sizes are measured when you split.")
                self.ok_button.setEnabled(True)
                return
            names = self._names(parts)
        except (ValueError, KeyError, IndexError) as ex:
            msg = str(ex) if isinstance(ex, ValueError) else "File names may only use {name}, {n} and {title}"
            item = QListWidgetItem("⚠  " + msg)
            item.setForeground(QColor(190, 30, 30))
            self.preview.addItem(item)
            self.ok_button.setEnabled(False)
            return
        for name, part in zip(names, parts):
            self.preview.addItem(f"{name}  —  {part.describe()}")
        self.ok_button.setEnabled(bool(parts))

    def accept(self):
        from PyQt6.QtWidgets import QApplication
        folder = self.folder.text().strip()
        if not folder:
            QMessageBox.warning(self, "Split PDF", "Choose a folder for the new files.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            parts = self.plan(measure=True)
            self._names(parts)                                     # validates the name pattern
            stem = Path(self.pdoc.path or self.pdoc.title).stem
            self.written = self._split.write_parts(self.pdoc.doc, parts, folder, stem, self.pattern.text())
        except (ValueError, KeyError, IndexError, OSError) as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Split PDF", f"Could not split the document:\n{ex}")
            return
        QApplication.restoreOverrideCursor()
        super().accept()


class ProtectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Protect — encrypt with password")
        f = QFormLayout(self)
        self.user = QLineEdit()
        self.user.setEchoMode(QLineEdit.EchoMode.Password)
        self.user2 = QLineEdit()
        self.user2.setEchoMode(QLineEdit.EchoMode.Password)
        self.owner = QLineEdit()
        self.owner.setEchoMode(QLineEdit.EchoMode.Password)
        self.print_ = QCheckBox("Allow printing")
        self.print_.setChecked(True)
        self.copy = QCheckBox("Allow copying text and images")
        self.copy.setChecked(True)
        self.edit = QCheckBox("Allow editing")
        f.addRow("Open password", self.user)
        f.addRow("Confirm", self.user2)
        f.addRow("Permissions password (optional)", self.owner)
        f.addRow(self.print_)
        f.addRow(self.copy)
        f.addRow(self.edit)
        f.addRow(QLabel("Encryption: AES-256"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _ok(self):
        if self.user.text() != self.user2.text():
            QMessageBox.warning(self, "Protect", "Passwords do not match.")
            return
        if not self.user.text() and not self.owner.text():
            QMessageBox.warning(self, "Protect", "Enter a password.")
            return
        self.accept()

    def encryption(self) -> dict:
        perm = pymupdf.PDF_PERM_ACCESSIBILITY
        if self.print_.isChecked():
            perm |= pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_PRINT_HQ
        if self.copy.isChecked():
            perm |= pymupdf.PDF_PERM_COPY
        if self.edit.isChecked():
            perm |= pymupdf.PDF_PERM_MODIFY | pymupdf.PDF_PERM_ANNOTATE | pymupdf.PDF_PERM_FORM | pymupdf.PDF_PERM_ASSEMBLE
        return {"user": self.user.text(), "owner": self.owner.text() or self.user.text() + "_owner", "permissions": perm}


class CreateDigitalIdDialog(QDialog):
    """Create a self-signed Digital ID (.pfx) for signing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Digital ID")
        self.setMinimumWidth(460)
        f = QFormLayout(self)
        info = QLabel("A Digital ID lets you sign PDFs with a certificate. Signatures show who signed and "
                      "detect any change made afterwards. Keep the file and password safe.")
        info.setWordWrap(True)
        info.setObjectName("hint")
        f.addRow(info)
        self.name = QLineEdit()
        self.email = QLineEdit()
        self.org = QLineEdit()
        self.country = QLineEdit("IN")
        self.country.setMaxLength(2)
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.EchoMode.Password)
        self.years = QSpinBox()
        self.years.setRange(1, 20)
        self.years.setValue(5)
        from .paths import user_data_dir
        self.path = QLineEdit(str(user_data_dir() / "digital_ids" / "MyDigitalID.pfx"))
        browse = QPushButton("…")
        browse.setFixedWidth(36)
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.path)
        row.addWidget(browse)
        f.addRow("Name*", self.name)
        f.addRow("Email", self.email)
        f.addRow("Organization", self.org)
        f.addRow("Country (2 letters)", self.country)
        f.addRow("Password*", self.pw)
        f.addRow("Confirm password*", self.pw2)
        f.addRow("Valid for (years)", self.years)
        f.addRow("Save as", row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _browse(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save Digital ID", self.path.text(), "Digital ID (*.pfx)")
        if p:
            self.path.setText(p)

    def _ok(self):
        if not self.name.text().strip():
            QMessageBox.warning(self, "Digital ID", "Enter your name.")
            return
        if len(self.pw.text()) < 6 or self.pw.text() != self.pw2.text():
            QMessageBox.warning(self, "Digital ID", "Passwords must match and be at least 6 characters.")
            return
        from . import digitalid, signing
        try:
            Path(self.path.text()).parent.mkdir(parents=True, exist_ok=True)
            digitalid.create_digital_id(self.path.text(), self.name.text().strip(), self.pw.text(),
                                        email=self.email.text().strip(), organization=self.org.text().strip(),
                                        country=self.country.text().strip(), years=self.years.value())
            signing.register_identity(self.path.text(), self.pw.text())
        except Exception as ex:
            QMessageBox.critical(self, "Digital ID", f"Could not create the Digital ID:\n{ex}")
            return
        self.accept()


class DigitalSignDialog(QDialog):
    """Choose a Digital ID, password, reason/location and appearance, then sign."""

    def __init__(self, field_names: list[str], parent=None, default_field: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Sign with Digital ID")
        self.setMinimumWidth(500)
        f = QFormLayout(self)
        from .paths import user_data_dir
        ids = sorted((user_data_dir() / "digital_ids").glob("*.pfx")) if (user_data_dir() / "digital_ids").is_dir() else []
        self.id_combo = QComboBox()
        self.id_combo.setEditable(True)
        for p in ids:
            self.id_combo.addItem(p.stem, str(p))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        create = QPushButton("Create new…")
        create.clicked.connect(self._create)
        row = QHBoxLayout()
        row.addWidget(self.id_combo, 1)
        row.addWidget(browse)
        row.addWidget(create)
        f.addRow("Digital ID", row)
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Password", self.pw)
        self.field = QComboBox()
        self.field.addItem("New signature field (placed on the current page)", None)
        for n in field_names:
            self.field.addItem(f"Field: {n}", n)
        if default_field:
            self.field.setCurrentIndex(max(0, self.field.findData(default_field)))
        f.addRow("Sign in", self.field)
        self.reason = QComboBox()
        self.reason.setEditable(True)
        self.reason.addItems(["I approve this document", "I am the author of this document",
                              "I have reviewed this document", "I agree to the terms"])
        self.location = QLineEdit()
        f.addRow("Reason", self.reason)
        f.addRow("Location", self.location)
        self.draw_sig = QCheckBox("Include my handwritten signature in the appearance")
        f.addRow(self.draw_sig)
        self.certify = QCheckBox("Certify (lock the document; only form filling allowed afterwards)")
        f.addRow(self.certify)
        note = QLabel("The document is saved as a new signed file. Any change after signing breaks the signature.")
        note.setWordWrap(True)
        note.setObjectName("hint")
        f.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Sign…")
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.signature_image = None

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "Digital ID", "", "Digital ID (*.pfx *.p12)")
        if p:
            self.id_combo.addItem(Path(p).stem, p)
            self.id_combo.setCurrentIndex(self.id_combo.count() - 1)

    def _create(self):
        dlg = CreateDigitalIdDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            p = dlg.path.text()
            self.id_combo.addItem(Path(p).stem, p)
            self.id_combo.setCurrentIndex(self.id_combo.count() - 1)

    def pfx_path(self) -> str:
        return self.id_combo.currentData() or self.id_combo.currentText()

    def _ok(self):
        if not self.pfx_path() or not Path(self.pfx_path()).is_file():
            QMessageBox.warning(self, "Sign", "Choose a Digital ID file.")
            return
        from . import digitalid
        try:
            digitalid.load_digital_id(self.pfx_path(), self.pw.text())
        except Exception:
            QMessageBox.warning(self, "Sign", "Wrong password for this Digital ID.")
            return
        if self.draw_sig.isChecked():
            sd = SignatureDialog(self)
            if sd.exec() != QDialog.DialogCode.Accepted:
                return
            self.signature_image = sd.result_image()
        self.accept()


class SignaturesDialog(QDialog):
    """Signature verification results."""

    def __init__(self, reports, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Signatures")
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        if not reports:
            v.addWidget(QLabel("This document has no digital signatures."))
        for r in reports:
            ok = r.intact and r.valid
            g = QGroupBox(f"{'✔' if ok and r.trusted else ('⚠' if ok else '✖')}  {r.field}")
            lay = QFormLayout(g)
            status = QLabel(r.summary)
            status.setStyleSheet(f"font-weight:600; color:{'#1b7f3b' if ok and r.trusted else ('#b26a00' if ok else '#c62828')};")
            lay.addRow("Status", status)
            lay.addRow("Signed by", QLabel(r.signer))
            lay.addRow("Signed at", QLabel(r.signed_at or "—"))
            lay.addRow("Document unchanged", QLabel("Yes" if r.intact else "No"))
            lay.addRow("Identity trusted", QLabel("Yes" if r.trusted else "No (add the signer's certificate to trust it)"))
            v.addWidget(g)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        v.addWidget(bb)


class CompareDialog(QDialog):
    """Pick two PDFs to compare."""

    def __init__(self, current_path: str | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Compare Files")
        self.setMinimumWidth(560)
        f = QFormLayout(self)
        info = QLabel("Finds text that was changed, inserted or deleted (also in scanned PDFs with recognised text) "
                      "and graphics that changed, then creates a side-by-side report.")
        info.setWordWrap(True)
        info.setObjectName("hint")
        f.addRow(info)
        self.a = QLineEdit(current_path or "")
        self.b = QLineEdit()
        for label, edit in (("Original (older)", self.a), ("Revised (newer)", self.b)):
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            btn = QPushButton("Browse…")
            btn.clicked.connect(lambda _=False, e=edit: self._browse(e))
            row.addWidget(btn)
            f.addRow(label, row)
        self.visual = QCheckBox("Also compare images and graphics")
        self.visual.setChecked(True)
        f.addRow(self.visual)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Compare")
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _browse(self, edit):
        p, _ = QFileDialog.getOpenFileName(self, "Choose PDF", edit.text(), "PDF files (*.pdf)")
        if p:
            edit.setText(p)

    def _ok(self):
        for e in (self.a, self.b):
            if not Path(e.text()).is_file():
                QMessageBox.warning(self, "Compare", "Choose both files.")
                return
        self.accept()


class BatchWorker(QThread):
    progress = pyqtSignal(int, str)
    finished_results = pyqtSignal(list)

    def __init__(self, files, actions, out_dir, suffix):
        super().__init__()
        self.files, self.actions, self.out_dir, self.suffix = files, actions, out_dir, suffix
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        from . import batch
        last = [0]

        def prog(msg, pct=None):
            if pct is not None:
                last[0] = pct
            self.progress.emit(last[0], msg)
        results = batch.run_batch(self.files, self.actions, self.out_dir, self.suffix, progress=prog,
                                  cancelled=lambda: self._stop)
        self.finished_results.emit(results)


class BatchDialog(QDialog):
    """Apply actions to many PDFs at once."""

    def __init__(self, parent=None, initial: list[str] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Batch Process")
        self.resize(820, 640)
        v = QVBoxLayout(self)
        t = QLabel("Process many files at once")
        t.setObjectName("panelTitle")
        v.addWidget(t)
        top = QHBoxLayout()
        v.addLayout(top, 1)

        left = QVBoxLayout()
        top.addLayout(left, 3)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, fn in (("Add files…", self.add_files), ("Add folder…", self.add_folder), ("Remove", self.remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        left.addLayout(row)

        g = QGroupBox("Actions (applied in this order)")
        right = QFormLayout(g)
        top.addWidget(g, 2)
        self.c_ocr = QCheckBox("Recognize text (OCR)")
        self.ocr_mode = QComboBox()
        self.ocr_mode.addItem("Searchable", "searchable")
        self.ocr_mode.addItem("Editable (slower)", "editable")
        right.addRow(self.c_ocr, self.ocr_mode)
        self.c_wm = QCheckBox("Watermark")
        self.wm_text = QLineEdit("CONFIDENTIAL")
        right.addRow(self.c_wm, self.wm_text)
        self.c_pn = QCheckBox("Page numbers")
        self.pn_fmt = QLineEdit("Page {n} of {total}")
        right.addRow(self.c_pn, self.pn_fmt)
        self.c_flat = QCheckBox("Flatten forms")
        right.addRow(self.c_flat)
        self.c_rot = QCheckBox("Rotate pages")
        self.rot = QComboBox()
        self.rot.addItems(["90° right", "180°", "90° left"])
        right.addRow(self.c_rot, self.rot)
        self.c_comp = QCheckBox("Compress")
        self.comp = QComboBox()
        self.comp.addItems(["High quality", "Balanced", "Smallest"])
        self.comp.setCurrentIndex(1)
        right.addRow(self.c_comp, self.comp)
        self.c_prot = QCheckBox("Password protect")
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw.setPlaceholderText("open password")
        right.addRow(self.c_prot, self.pw)
        self.c_docx = QCheckBox("Also export to Word")
        self.c_xlsx = QCheckBox("Also export tables to Excel")
        self.c_txt = QCheckBox("Also export text")
        right.addRow(self.c_docx)
        right.addRow(self.c_xlsx)
        right.addRow(self.c_txt)

        g = QGroupBox("Output")
        f = QFormLayout(g)
        self.out_dir = QLineEdit(str(Path.home() / "Documents" / "PDF Studio Batch"))
        b = QPushButton("…")
        b.setFixedWidth(36)
        b.clicked.connect(self._choose_out)
        row = QHBoxLayout()
        row.addWidget(self.out_dir, 1)
        row.addWidget(b)
        f.addRow("Folder", row)
        self.suffix = QLineEdit("_processed")
        f.addRow("File name suffix", self.suffix)
        v.addWidget(g)

        self.bar = QProgressBar()
        self.status = QLabel("")
        self.status.setObjectName("hint")
        v.addWidget(self.bar)
        v.addWidget(self.status)
        bb = QDialogButtonBox()
        self.run_btn = bb.addButton("Run", QDialogButtonBox.ButtonRole.AcceptRole)
        self.run_btn.setObjectName("primary")
        self.stop_btn = bb.addButton("Stop", QDialogButtonBox.ButtonRole.ActionRole)
        self.stop_btn.setEnabled(False)
        close = bb.addButton(QDialogButtonBox.StandardButton.Close)
        self.run_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(lambda: self.worker and self.worker.stop())
        close.clicked.connect(self.reject)
        v.addWidget(bb)
        self.worker = None
        self.results = []
        for p in initial or []:
            self._add(p)

    def _add(self, path):
        existing = {self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())}
        if path in existing:
            return
        it = QListWidgetItem(Path(path).name)
        it.setToolTip(path)
        it.setData(Qt.ItemDataRole.UserRole, path)
        self.list.addItem(it)

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add files", "", "PDF and images (*.pdf *.png *.jpg *.jpeg *.tif *.tiff)")
        for p in paths:
            self._add(p)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add all PDFs in folder")
        if folder:
            for p in sorted(Path(folder).rglob("*.pdf")):
                self._add(str(p))

    def remove(self):
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))

    def _choose_out(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def actions(self) -> list[dict]:
        acts = []
        if self.c_ocr.isChecked():
            acts.append({"op": "ocr", "mode": self.ocr_mode.currentData()})
        if self.c_rot.isChecked():
            acts.append({"op": "rotate", "degrees": [90, 180, 270][self.rot.currentIndex()]})
        if self.c_flat.isChecked():
            acts.append({"op": "flatten_forms"})
        if self.c_wm.isChecked() and self.wm_text.text().strip():
            acts.append({"op": "watermark", "text": self.wm_text.text().strip()})
        if self.c_pn.isChecked():
            acts.append({"op": "page_numbers", "format": self.pn_fmt.text() or "Page {n} of {total}"})
        if self.c_comp.isChecked():
            acts.append({"op": "compress", **[{"dpi": 150, "quality": 85}, {"dpi": 110, "quality": 70},
                                               {"dpi": 72, "quality": 55}][self.comp.currentIndex()]})
        if self.c_prot.isChecked() and self.pw.text():
            acts.append({"op": "protect", "user": self.pw.text()})
        for cb, op in ((self.c_docx, "to_docx"), (self.c_xlsx, "to_xlsx"), (self.c_txt, "to_text")):
            if cb.isChecked():
                acts.append({"op": op})
        return acts

    def start(self):
        files = [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]
        acts = self.actions()
        if not files or not acts:
            QMessageBox.information(self, "Batch", "Add at least one file and choose at least one action.")
            return
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.worker = BatchWorker(files, acts, self.out_dir.text(), self.suffix.text())
        self.worker.progress.connect(lambda pct, msg: (self.bar.setValue(pct), self.status.setText(msg)))
        self.worker.finished_results.connect(self._done)
        self.worker.start()

    def _done(self, results):
        self.results = results
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        ok = sum(1 for r in results if r.ok)
        failed = [r for r in results if not r.ok]
        msg = f"Processed {ok} of {len(results)} file(s). Output folder:\n{self.out_dir.text()}"
        if failed:
            msg += "\n\nProblems:\n" + "\n".join(f"• {Path(r.source).name}: {r.message}" for r in failed[:10])
        self.status.setText(f"Done – {ok} OK, {len(failed)} failed")
        QMessageBox.information(self, "Batch finished", msg)

    def reject(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(5000)
        super().reject()


class WatermarkDialog(QDialog):
    def __init__(self, page_count, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Watermark / Page Numbers")
        f = QFormLayout(self)
        self.kind = QComboBox()
        self.kind.addItems(["Text watermark", "Page numbers (footer)"])
        self.text = QLineEdit("CONFIDENTIAL")
        self.size = QSpinBox()
        self.size.setRange(6, 200)
        self.size.setValue(60)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(5, 100)
        self.opacity.setValue(25)
        self.angle = QComboBox()
        self.angle.addItems(["45°", "0°", "90°"])
        self.color = (0.8, 0.1, 0.1)
        self.col_btn = color_button(self.color, lambda c: setattr(self, "color", c))
        self.pages = QLineEdit(f"1-{page_count}")
        self.fmt = QLineEdit("Page {n} of {total}")
        f.addRow("Type", self.kind)
        f.addRow("Text", self.text)
        f.addRow("Number format", self.fmt)
        f.addRow("Font size", self.size)
        f.addRow("Opacity", self.opacity)
        f.addRow("Angle", self.angle)
        f.addRow("Colour", self.col_btn)
        f.addRow("Pages", self.pages)
        self.page_count = page_count
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def apply(self, doc: pymupdf.Document) -> list[int]:
        pages = parse_ranges(self.pages.text(), doc.page_count)
        font = pymupdf.Font("helv")
        for i in pages:
            page = doc[i]
            r = page.rect
            if self.kind.currentIndex() == 0:
                text = self.text.text()
                size = self.size.value()
                tw = pymupdf.TextWriter(r, opacity=self.opacity.value() / 100, color=self.color)
                w = font.text_length(text, fontsize=size)
                c = pymupdf.Point(r.width / 2, r.height / 2) * page.derotation_matrix
                tw.append((c.x - w / 2, c.y + size * 0.35), text, font=font, fontsize=size)
                ang = [45, 0, 90][self.angle.currentIndex()]
                tw.write_text(page, morph=(c, pymupdf.Matrix(-ang - page.rotation)))
            else:
                text = self.fmt.text().replace("{n}", str(i + 1)).replace("{total}", str(doc.page_count))
                size = min(self.size.value(), 12) if self.size.value() > 30 else self.size.value()
                w = font.text_length(text, fontsize=size)
                tw = pymupdf.TextWriter(r, color=self.color)
                bottom = pymupdf.Point(r.width / 2, r.height - 24) * page.derotation_matrix
                tw.append((bottom.x - w / 2, bottom.y), text, font=font, fontsize=size)
                tw.write_text(page, morph=(bottom, pymupdf.Matrix(-page.rotation)))
        return pages
