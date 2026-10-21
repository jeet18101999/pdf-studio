"""Main window: menus, toolbars, tool rail, thumbnails, properties panel, search."""
from __future__ import annotations

import os
from pathlib import Path

import pymupdf
from PyQt6.QtCore import QSettings, QSize, Qt, QTimer
from PyQt6.QtGui import QAction, QActionGroup, QFont, QIcon, QImage, QKeySequence, QPainter
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox, QFileDialog,
                             QFontComboBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSpinBox,
                             QStackedWidget, QToolBar, QToolButton, QVBoxLayout, QWidget, QGroupBox)

from . import APP_NAME, dialogs, forms, imagetools, ocr, pageimages, signing, textops
from .document import PdfDocument
from .fonts import FONT_INDEX, clear_embedded_cache
from .style import icon
from .textops import TextStyle
from .viewer import (DocumentView, TOOL_ADD_TEXT, TOOL_ARROW, TOOL_EDIT, TOOL_ELLIPSE, TOOL_HAND,
                     TOOL_HIGHLIGHT, TOOL_INK, TOOL_LINE, TOOL_NOTE, TOOL_RECT, TOOL_REDACT, TOOL_SELECT,
                     TOOL_STRIKE, TOOL_UNDERLINE, TOOL_FORM_EDIT, TOOL_OBJECT)
from PyQt6.QtWidgets import QApplication, QGridLayout, QPlainTextEdit

FILE_FILTER = ("All documents (*.pdf *.docx *.doc *.rtf *.odt *.xlsx *.xls *.csv *.pptx *.ppt *.png *.jpg *.jpeg "
               "*.tif *.tiff *.bmp *.gif *.webp *.xps *.epub);;PDF files (*.pdf);;"
               "Word, Excel, PowerPoint (*.docx *.doc *.docm *.rtf *.odt *.xlsx *.xls *.xlsm *.csv *.ods *.pptx *.ppt *.pptm *.odp);;"
               "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp)")


# =============================================================================== thumbnails
class ThumbnailPanel(QListWidget):
    def __init__(self, pdoc: PdfDocument, view: DocumentView):
        super().__init__()
        self.pdoc, self.view = pdoc, view
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFlow(QListWidget.Flow.TopToBottom)
        self.setWrapping(False)
        self.setIconSize(QSize(120, 160))
        self.setSpacing(6)
        self.setMovement(QListWidget.Movement.Static)
        self.setMinimumWidth(170)
        self._queue: list[int] = []
        self._timer = QTimer(self, interval=15, timeout=self._render_some)
        self.currentRowChanged.connect(lambda r: r >= 0 and self.view.current_page() != r and self.view.go_to_page(r))
        pdoc.loaded.connect(self.rebuild)
        pdoc.structure_changed.connect(self.rebuild)
        pdoc.changed.connect(self.update_pages)
        view.page_changed.connect(self._sync)

    def rebuild(self):
        self.blockSignals(True)
        self.clear()
        if self.pdoc.is_open:
            for i in range(self.pdoc.page_count):
                it = QListWidgetItem(str(i + 1))
                it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
                it.setSizeHint(QSize(140, 190))
                self.addItem(it)
        self.blockSignals(False)
        self._queue = list(range(self.pdoc.page_count))
        self._timer.start()

    def update_pages(self, pages):
        if not self.pdoc.is_open or self.count() != self.pdoc.page_count:
            return
        for p in (pages if pages is not None else range(self.pdoc.page_count)):
            if p not in self._queue:
                self._queue.append(p)
        self._timer.start()

    def _render_some(self):
        for _ in range(3):
            if not self._queue or not self.pdoc.is_open:
                self._timer.stop()
                return
            i = self._queue.pop(0)
            if i < self.count():
                self.item(i).setIcon(QIcon(dialogs.page_thumb(self.pdoc.page(i), 120)))

    def _sync(self, page):
        if self.currentRow() != page and page < self.count():
            self.blockSignals(True)
            self.setCurrentRow(page)
            self.blockSignals(False)


# =============================================================================== properties panel
class PropertiesPanel(QStackedWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.view = win.view
        self.setMinimumWidth(270)
        self._building = False
        self.addWidget(self._tools_page())       # 0
        self.addWidget(self._text_page())        # 1
        self.addWidget(self._image_page())       # 2
        self.addWidget(self._comment_page())     # 3
        self.addWidget(self._forms_page())       # 4

    # ------------------------------------------------------------------ pages
    def _title(self, text, hint=None):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        t = QLabel(text)
        t.setObjectName("panelTitle")
        v.addWidget(t)
        if hint:
            h = QLabel(hint)
            h.setObjectName("hint")
            h.setWordWrap(True)
            v.addWidget(h)
        return w

    def _tools_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self._title("Tools", "Choose a tool on the left rail, or start here."))
        for text, ic, fn in (("Edit PDF text && images", "edit", lambda: self.win.set_tool(TOOL_EDIT)),
                             ("Recognize text (OCR)", "ocr", self.win.run_ocr),
                             ("Organize pages", "organize", self.win.organize),
                             ("Combine files", "merge", self.win.merge),
                             ("Create PDF from Word, Excel or images", "open", self.win.create_pdf_from_file),
                             ("Split PDF", "split", self.win.split_pdf),
                             ("Repair a damaged PDF", "apply", lambda: self.win.repair_pdf()),
                             ("Add image", "image", self.win.insert_image),
                             ("Fill && Sign", "sign", self.win.sign),
                             ("Prepare form", "check", lambda: self.win.set_tool(TOOL_FORM_EDIT)),
                             ("Sign with Digital ID", "protect", lambda: self.win.sign_digital()),
                             ("Comment", "note", lambda: self.win.set_tool(TOOL_HIGHLIGHT)),
                             ("Redact", "redact", lambda: self.win.set_tool(TOOL_REDACT)),
                             ("Protect", "protect", self.win.protect),
                             ("Compress PDF", "compress", self.win.compress),
                             ("Watermark / page numbers", "watermark", self.win.watermark)):
            b = QPushButton(icon(ic, "#1473e6"), "  " + text)
            b.setStyleSheet("text-align:left; padding:8px 12px; border-radius:6px;")
            b.clicked.connect(fn)
            v.addWidget(b)
        v.addStretch()
        return w

    def _text_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self._title("Format", "Click any paragraph to edit. Ctrl+Enter or click outside to apply, Esc to cancel."))
        g = QGroupBox("Font")
        f = QFormLayout(g)
        self.font_combo = QFontComboBox()
        self.font_combo.currentFontChanged.connect(self._style_changed)
        f.addRow(self.font_combo)
        row = QHBoxLayout()
        self.size = QDoubleSpinBox()
        self.size.setRange(2, 400)
        self.size.setDecimals(1)
        self.size.setSuffix(" pt")
        self.size.valueChanged.connect(self._style_changed)
        self.color_btn = dialogs.color_button((0, 0, 0), self._color_changed)
        self._color = (0, 0, 0)
        row.addWidget(self.size)
        row.addWidget(self.color_btn)
        f.addRow(row)
        row = QHBoxLayout()
        self.b_bold = self._toggle("bold", "Bold")
        self.b_italic = self._toggle("italic", "Italic")
        self.b_under = self._toggle("underline", "Underline")
        for b in (self.b_bold, self.b_italic, self.b_under):
            row.addWidget(b)
        row.addStretch()
        f.addRow(row)
        v.addWidget(g)

        g = QGroupBox("Paragraph")
        f = QFormLayout(g)
        row = QHBoxLayout()
        self.align_group = QButtonGroup(self)
        for i, (ic, tip) in enumerate((("alignl", "Left"), ("alignc", "Center"), ("alignr", "Right"), ("justify", "Justify"))):
            b = self._toggle(ic, tip)
            self.align_group.addButton(b, i)
            row.addWidget(b)
        self.align_group.setExclusive(True)
        self.align_group.idClicked.connect(self._style_changed)
        row.addStretch()
        f.addRow(row)
        self.line_h = QDoubleSpinBox()
        self.line_h.setRange(0.6, 4.0)
        self.line_h.setSingleStep(0.05)
        self.line_h.valueChanged.connect(self._style_changed)
        f.addRow("Line spacing", self.line_h)
        v.addWidget(g)
        self.font_info = QLabel()
        self.font_info.setObjectName("hint")
        self.font_info.setWordWrap(True)
        v.addWidget(self.font_info)
        row = QHBoxLayout()
        ok = QPushButton("Apply")
        ok.setObjectName("primary")
        ok.clicked.connect(lambda: self.view.finish_edit(True))
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(lambda: self.view.finish_edit(False))
        row.addWidget(ok)
        row.addWidget(cancel)
        v.addLayout(row)
        v.addStretch()
        return w

    def _toggle(self, ic, tip):
        b = QToolButton()
        b.setIcon(icon(ic))
        b.setToolTip(tip)
        b.setCheckable(True)
        b.setIconSize(QSize(18, 18))
        if ic in ("bold", "italic", "underline"):
            b.toggled.connect(self._style_changed)
        return b

    def _image_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self._title("Image", "Drag to move • handles resize • double-click for settings"))
        g = QGroupBox("Size & position (pt)")
        f = QFormLayout(g)
        self.img_x, self.img_y, self.img_w, self.img_h = (QDoubleSpinBox() for _ in range(4))
        for sp in (self.img_x, self.img_y, self.img_w, self.img_h):
            sp.setRange(-5000, 5000)
            sp.setDecimals(1)
        row = QHBoxLayout()
        row.addWidget(QLabel("X"))
        row.addWidget(self.img_x)
        row.addWidget(QLabel("Y"))
        row.addWidget(self.img_y)
        f.addRow(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("W"))
        row.addWidget(self.img_w)
        row.addWidget(QLabel("H"))
        row.addWidget(self.img_h)
        f.addRow(row)
        self.keep = QCheckBox("Lock aspect ratio")
        self.keep.setChecked(True)
        self.keep.toggled.connect(lambda on: setattr(self.view, "keep_aspect", on))
        f.addRow(self.keep)
        self._img_ar = 1.0
        self.img_w.editingFinished.connect(lambda: self._geom_edit("w"))
        self.img_h.editingFinished.connect(lambda: self._geom_edit("h"))
        self.img_x.editingFinished.connect(lambda: self._geom_edit("x"))
        self.img_y.editingFinished.connect(lambda: self._geom_edit("y"))
        v.addWidget(g)

        g = QGroupBox("Arrange & transform")
        grid = QVBoxLayout(g)
        for text, ic, fn in (("Image settings (background, opacity…)", "settings", self.win.image_settings_selected),
                             ("Replace image…", "replace", self.win.replace_selected_image),
                             ("Bring in front of text", "front", lambda: self.view.set_selected_image_order(False)),
                             ("Send behind text", "back", lambda: self.view.set_selected_image_order(True)),
                             ("Rotate right 90°", "rotr", lambda: self.win.transform_selected_image(rotate=90)),
                             ("Flip horizontal", "flip", lambda: self.win.transform_selected_image(flip_h=True)),
                             ("Remove background", "image", lambda: self.win.transform_selected_image(bg_mode=imagetools.BG_AUTO)),
                             ("Delete image", "delete", self.view.delete_selected_image)):
            b = QPushButton(icon(ic), "  " + text)
            b.setStyleSheet("text-align:left; padding:6px 10px; border-radius:6px;")
            b.clicked.connect(fn)
            grid.addWidget(b)
        v.addWidget(g)
        v.addStretch()
        return w

    def _comment_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self._title("Comment & Markup", "Drag over text to highlight / underline / strike. "
                                                    "Draw shapes and ink directly on the page."))
        f = QFormLayout()
        f.addRow("Highlight colour", dialogs.color_button(self.view.annot_color, lambda c: setattr(self.view, "annot_color", c)))
        f.addRow("Stroke colour", dialogs.color_button(self.view.stroke_color, lambda c: setattr(self.view, "stroke_color", c)))
        sw = QDoubleSpinBox()
        sw.setRange(0.25, 20)
        sw.setValue(self.view.stroke_width)
        sw.valueChanged.connect(lambda x: setattr(self.view, "stroke_width", x))
        f.addRow("Line width", sw)
        v.addLayout(f)
        red = QPushButton(icon("apply"), "  Apply all redactions")
        red.clicked.connect(self.win.apply_redactions)
        v.addWidget(red)
        v.addStretch()
        return w

    def _forms_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self._title("Prepare Form", "Pick a field type, then click or drag on the page "
                                                 "(hold Ctrl to add several). Select a field to edit it."))
        g = QGroupBox("Add field")
        grid = QGridLayout(g)
        types = [("Text", forms.TEXT, "addtext"), ("Check box", forms.CHECKBOX, "check"),
                 ("Radio button", forms.RADIO, "ellipse"), ("Dropdown", forms.COMBO, "next"),
                 ("List box", forms.LISTBOX, "pages"), ("Signature", forms.SIGNATURE, "sign")]
        for i, (label, t, ic) in enumerate(types):
            b = QToolButton()
            b.setText(label)
            b.setIcon(icon(ic))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setSizePolicy(b.sizePolicy().horizontalPolicy().Expanding, b.sizePolicy().verticalPolicy())
            b.clicked.connect(lambda _=False, t=t, label=label: self.win.arm_field(t, label))
            grid.addWidget(b, i // 2, i % 2)
        auto = QPushButton(icon("ocr", "#1473e6"), "  Auto-detect fields")
        auto.clicked.connect(self.win.auto_detect_fields)
        grid.addWidget(auto, 3, 0, 1, 2)
        v.addWidget(g)

        g = QGroupBox("Field properties")
        f = QFormLayout(g)
        self.f_type = QLabel("—")
        self.f_name = QLineEdit()
        self.f_tip = QLineEdit()
        self.f_required = QCheckBox("Required")
        self.f_readonly = QCheckBox("Read only")
        self.f_multi = QCheckBox("Multi-line")
        self.f_size = QDoubleSpinBox()
        self.f_size.setRange(0, 96)
        self.f_size.setSpecialValueText("Auto")
        self.f_size.setSuffix(" pt")
        self.f_choices = QPlainTextEdit()
        self.f_choices.setMaximumHeight(80)
        self.f_choices.setPlaceholderText("One option per line")
        apply_choices = QPushButton("Apply options")
        f.addRow("Type", self.f_type)
        f.addRow("Name", self.f_name)
        f.addRow("Tooltip", self.f_tip)
        row = QHBoxLayout()
        for cb in (self.f_required, self.f_readonly, self.f_multi):
            row.addWidget(cb)
        f.addRow(row)
        f.addRow("Font size", self.f_size)
        f.addRow("Options", self.f_choices)
        f.addRow("", apply_choices)
        row = QHBoxLayout()
        dup = QPushButton(icon("copy"), " Duplicate")
        dele = QPushButton(icon("delete"), " Delete")
        dup.clicked.connect(self.view.duplicate_selected_field)
        dele.clicked.connect(self.view.delete_selected_field)
        row.addWidget(dup)
        row.addWidget(dele)
        f.addRow(row)
        self.f_name.editingFinished.connect(lambda: self._field_changed("name"))
        self.f_tip.editingFinished.connect(lambda: self._field_changed("tooltip"))
        self.f_required.toggled.connect(lambda _: self._field_changed("required"))
        self.f_readonly.toggled.connect(lambda _: self._field_changed("readonly"))
        self.f_multi.toggled.connect(lambda _: self._field_changed("multiline"))
        self.f_size.editingFinished.connect(lambda: self._field_changed("fontsize"))
        apply_choices.clicked.connect(lambda: self._field_changed("choices"))
        self.field_group = g
        g.setEnabled(False)
        v.addWidget(g)

        g = QGroupBox("Form")
        grid = QGridLayout(g)
        for i, (text, ic, fn) in enumerate((("Clear form", "undo", self.win.clear_form),
                                             ("Flatten form", "apply", self.win.flatten_form),
                                             ("Export data…", "export", self.win.export_form_data),
                                             ("Import data…", "open", self.win.import_form_data))):
            b = QPushButton(icon(ic), " " + text)
            b.clicked.connect(fn)
            grid.addWidget(b, i // 2, i % 2)
        v.addWidget(g)
        v.addStretch()
        return w

    def show_field(self, f):
        if f is None:
            self.field_group.setEnabled(False)
            self.f_type.setText("—")
            return
        self._building = True
        self.field_group.setEnabled(True)
        self.f_type.setText(f.type_name)
        self.f_name.setText(f.name)
        self.f_tip.setText(f.tooltip)
        self.f_required.setChecked(f.required)
        self.f_readonly.setChecked(f.readonly)
        self.f_multi.setChecked(f.multiline)
        self.f_multi.setEnabled(f.type == forms.TEXT)
        self.f_size.setValue(f.fontsize or 0)
        self.f_choices.setPlainText("\n".join(str(c) for c in f.choices))
        self.f_choices.setEnabled(f.type in (forms.COMBO, forms.LISTBOX))
        self._building = False
        self.setCurrentIndex(4)

    def _field_changed(self, which):
        if self._building or self.view.selected_field is None:
            return
        f = self.view.selected_field.field
        props = {}
        if which == "name" and self.f_name.text().strip() and self.f_name.text().strip() != f.name:
            props["name"] = self.f_name.text().strip()
        elif which == "tooltip" and self.f_tip.text() != f.tooltip:
            props["tooltip"] = self.f_tip.text()
        elif which == "required":
            props["required"] = self.f_required.isChecked()
        elif which == "readonly":
            props["readonly"] = self.f_readonly.isChecked()
        elif which == "multiline" and f.type == forms.TEXT:
            props["multiline"] = self.f_multi.isChecked()
        elif which == "fontsize" and abs(self.f_size.value() - (f.fontsize or 0)) > 1e-3:
            props["fontsize"] = self.f_size.value()
        elif which == "choices" and f.type in (forms.COMBO, forms.LISTBOX):
            opts = [l.strip() for l in self.f_choices.toPlainText().splitlines() if l.strip()]
            if opts:
                props["choices"] = opts
        if props:
            self.view.update_selected_field(**props)

    # ------------------------------------------------------------------ state sync
    def show_text_style(self, style: TextStyle, info: str = ""):
        self._building = True
        self.font_combo.setCurrentFont(QFont(style.family))
        self.size.setValue(style.size)
        self._color = style.color
        self.color_btn.set_rgb(style.color)
        self.b_bold.setChecked(style.bold)
        self.b_italic.setChecked(style.italic)
        self.b_under.setChecked(style.underline)
        self.align_group.button(style.align).setChecked(True)
        self.line_h.setValue(style.line_height)
        blk = self.view.edit_block
        if style.pdf_font:
            src = style.pdf_font.split("+")[-1]
        elif blk is not None and blk.source == "scan":
            src = "detected from scan (OCR)"
        else:
            src = "new text"
        self.font_info.setText(f"Original font: {src}\nMatched: {style.family} {style.size:.1f} pt" + (f"\n{info}" if info else ""))
        self._building = False
        self.setCurrentIndex(1)

    def _color_changed(self, rgb):
        self._color = rgb
        self._style_changed()

    def current_style(self) -> TextStyle:
        base = self.view.edit_style or self.view.new_text_style
        return base.copy(family=self.font_combo.currentFont().family(), size=self.size.value(), color=self._color,
                         bold=self.b_bold.isChecked(), italic=self.b_italic.isChecked(),
                         underline=self.b_under.isChecked(), align=max(0, self.align_group.checkedId()),
                         line_height=self.line_h.value())

    def _style_changed(self, *_):
        if self._building:
            return
        style = self.current_style()
        if self.view.editor is not None:
            self.view.apply_style_to_editor(style)
        else:
            self.view.new_text_style = style.copy(pdf_font="", family_changed=False)

    def show_image(self, img):
        if img is None:
            self.setCurrentIndex(0 if self.view.tool not in (TOOL_HIGHLIGHT, TOOL_UNDERLINE, TOOL_STRIKE, TOOL_INK,
                                                             TOOL_RECT, TOOL_ELLIPSE, TOOL_LINE, TOOL_ARROW,
                                                             TOOL_NOTE, TOOL_REDACT) else 3)
            return
        r = img.rect
        for sp, val in ((self.img_x, r.x0), (self.img_y, r.y0), (self.img_w, r.width), (self.img_h, r.height)):
            sp.blockSignals(True)
            sp.setValue(val)
            sp.blockSignals(False)
        self._img_ar = r.width / max(0.01, r.height)
        self.setCurrentIndex(2)

    def _geom_edit(self, which):
        box = self.view.selected_image
        if not box:
            return
        x, y, w, h = self.img_x.value(), self.img_y.value(), self.img_w.value(), self.img_h.value()
        if self.keep.isChecked():
            if which == "w":
                h = w / self._img_ar
            elif which == "h":
                w = h * self._img_ar
        new = pymupdf.Rect(x, y, x + max(1, w), y + max(1, h))
        if new == box.img.rect:
            return
        box.setRect(self.view.page_rect_to_scene(box.img.page, new))
        self.view.commit_image_geometry(box)


# =============================================================================== main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings()
        self.pdoc = PdfDocument()
        self.view = DocumentView(self.pdoc)
        self.search_hits: list = []
        self.search_index = -1
        self._scanned_prompted: set[int] = set()
        self._signed = False

        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(self._search_bar())
        cv.addWidget(self.view, 1)
        self.setCentralWidget(central)

        self.thumbs = ThumbnailPanel(self.pdoc, self.view)
        dock = QDockWidget("Pages", self)
        dock.setObjectName("pagesDock")
        dock.setWidget(self.thumbs)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.pages_dock = dock

        self.props = PropertiesPanel(self)
        pd = QDockWidget("Properties", self)
        pd.setObjectName("propsDock")
        pd.setWidget(self.props)
        pd.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, pd)
        self.props_dock = pd

        self._actions()
        self._menus()
        self._toolbar()
        self._tool_rail()
        self._statusbar()

        self.view.status.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.view.edit_started.connect(lambda st: self.props.show_text_style(st))
        self.view.edit_finished.connect(lambda: self.props.setCurrentIndex(0 if self.view.selected_image is None else 2))
        self.view.image_selected.connect(self.props.show_image)
        self.view.image_settings_requested.connect(lambda img: self.image_settings_selected())
        self.view.scanned_page_edit.connect(lambda idx: self._scanned_pages([idx]))
        self.view.scanned_pages_detected.connect(self._scanned_pages)
        self.view.field_selected.connect(self.props.show_field)
        self.view.sign_field_requested.connect(self.sign_field)
        self.view.page_changed.connect(self._page_changed)
        self.view.zoom_changed.connect(lambda z: self.zoom_box.setEditText(f"{z * 100:.0f}%"))
        self.view.tool_changed.connect(self._tool_changed)
        self.pdoc.dirty_changed.connect(lambda _: self._update_title())
        self.pdoc.loaded.connect(self._on_loaded)
        self.pdoc.changed.connect(lambda _: (self._update_undo(), clear_embedded_cache()))
        self.pdoc.structure_changed.connect(self._on_structure)

        self.setAcceptDrops(True)
        self.resize(1400, 900)
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self._on_loaded()

    # ------------------------------------------------------------------ actions
    def _act(self, text, ic=None, shortcut=None, slot=None, checkable=False, tip=None):
        a = QAction(text, self)
        if ic:
            a.setIcon(icon(ic, mirror=(ic == "rotr")))
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if slot:
            a.triggered.connect(slot)
        a.setCheckable(checkable)
        a.setToolTip(tip or (f"{text} ({shortcut})" if shortcut else text))
        return a

    def _actions(self):
        A = self._act
        self.a_open = A("Open…", "open", QKeySequence.StandardKey.Open, self.open_dialog)
        self.a_save = A("Save", "save", QKeySequence.StandardKey.Save, self.save)
        self.a_saveas = A("Save As…", "saveas", "Ctrl+Shift+S", self.save_as)
        self.a_close = A("Close", "close", "Ctrl+W", self.close_doc)
        self.a_print = A("Print…", "print", QKeySequence.StandardKey.Print, self.print_doc)
        self.a_export_img = A("Export pages as images…", "export", None, self.export_images)
        self.a_export_txt = A("Export text…", "export", None, self.export_text)
        self.a_export_docx = A("Export to Word (.docx)…", "export", None, self.export_word)
        self.a_export_xlsx = A("Export tables to Excel (.xlsx)…", "export", None, self.export_excel)
        self.a_export_dxf = A("Export drawing to CAD (.dxf)…", "export", None, self.export_cad)
        self.a_undo = A("Undo", "undo", QKeySequence.StandardKey.Undo, self.undo)
        self.a_redo = A("Redo", "redo", "Ctrl+Y", self.redo)
        self.a_find = A("Find…", "search", QKeySequence.StandardKey.Find, self.show_search)
        self.a_zoom_in = A("Zoom in", "zoomin", "Ctrl+=", lambda: self.view.set_zoom(self.view.zoom * 1.2, False))
        self.a_zoom_out = A("Zoom out", "zoomout", "Ctrl+-", lambda: self.view.set_zoom(self.view.zoom / 1.2, False))
        self.a_fit_w = A("Fit width", "fitwidth", "Ctrl+2", self.view.fit_width)
        self.a_fit_p = A("Fit page", "fitpage", "Ctrl+0", self.view.fit_page)
        self.a_actual = A("Actual size", None, "Ctrl+1", lambda: self.view.set_zoom(1.0, False))
        self.a_rot_l = A("Rotate page left", "rotl", "Ctrl+Shift+-", lambda: self.rotate_current(-90))
        self.a_rot_r = A("Rotate page right", "rotr", "Ctrl+Shift+=", lambda: self.rotate_current(90))
        self.a_del_page = A("Delete page", "delete", None, self.delete_current_page)
        self.a_organize = A("Organize Pages", "organize", "Ctrl+Shift+O", self.organize)
        self.a_merge = A("Combine Files…", "merge", None, self.merge)
        self.a_create_pdf = A("From Word, Excel or PowerPoint…", "open", None, self.create_pdf_from_file)
        self.a_images_pdf = A("From Images…", "image", None, lambda: self.images_to_pdf())
        self.a_split = A("Split PDF…", "split", None, self.split_pdf)
        self.a_repair = A("Repair PDF…", "apply", None, lambda: self.repair_pdf())
        self.a_diagnose = A("Diagnose Problems…", "info", None, self.diagnose_problems)
        self.a_active = A("Remove Scripts && Hidden Actions…", "protect", None, self.remove_active_content)
        self.a_ocr = A("Recognize Text (OCR)…", "ocr", "Ctrl+Shift+R", self.run_ocr)
        self.a_image = A("Add Image…", "image", "Ctrl+Shift+I", self.insert_image)
        self.a_sign = A("Fill && Sign…", "sign", None, self.sign)
        self.a_protect = A("Protect with Password…", "protect", None, self.protect)
        self.a_compress = A("Compress PDF…", "compress", None, self.compress)
        self.a_watermark = A("Watermark / Page Numbers…", "watermark", None, self.watermark)
        self.a_redact_apply = A("Apply Redactions", "apply", None, self.apply_redactions)
        self.a_copy = A("Copy", "copy", None, self.view.copy_selection)
        self.a_auto_fields = A("Auto-detect Form Fields", "ocr", None, self.auto_detect_fields)
        self.a_clear_form = A("Clear Form", "undo", None, self.clear_form)
        self.a_flatten_form = A("Flatten Form", "apply", None, self.flatten_form)
        self.a_export_form = A("Export Form Data…", "export", None, self.export_form_data)
        self.a_import_form = A("Import Form Data…", "open", None, self.import_form_data)
        self.a_create_id = A("Create Digital ID…", "protect", None, self.create_digital_id)
        self.a_compare = A("Compare Files…", "copy", None, self.compare_files)
        self.a_batch = A("Batch Process…", "settings", None, self.batch_process)
        self.a_sign_digital = A("Sign with Digital ID…", "sign", None, lambda: self.sign_digital())
        self.a_verify = A("Verify Signatures…", "check", None, self.verify_signatures)

        self.tool_group = QActionGroup(self)
        self.tool_actions = {}
        for tool, text, ic, sc in ((TOOL_SELECT, "Select", "select", "V"), (TOOL_HAND, "Hand", "hand", "H"),
                                   (TOOL_EDIT, "Edit", "edit", "E"), (TOOL_ADD_TEXT, "Add Text", "addtext", "T"),
                                   (TOOL_HIGHLIGHT, "Highlight", "highlight", None),
                                   (TOOL_UNDERLINE, "Underline", "underline", None),
                                   (TOOL_STRIKE, "Strike", "strike", None), (TOOL_NOTE, "Note", "note", None),
                                   (TOOL_INK, "Draw", "ink", None), (TOOL_RECT, "Rectangle", "rect", None),
                                   (TOOL_ELLIPSE, "Ellipse", "ellipse", None), (TOOL_LINE, "Line", "line", None),
                                   (TOOL_ARROW, "Arrow", "arrow", None), (TOOL_REDACT, "Redact", "redact", None),
                                   (TOOL_FORM_EDIT, "Prepare Form", "check", None),
                                   (TOOL_OBJECT, "Select Objects", "crop", "O")):
            a = A(text, ic, sc, lambda _=False, t=tool: self.toggle_tool(t), checkable=True)
            self.tool_group.addAction(a)
            self.tool_actions[tool] = a
        self.tool_actions[TOOL_SELECT].setChecked(True)
        self.doc_actions = [self.a_save, self.a_saveas, self.a_close, self.a_print, self.a_export_img, self.a_export_txt,
                            self.a_export_docx, self.a_export_xlsx,
                            self.a_find, self.a_zoom_in, self.a_zoom_out, self.a_fit_w, self.a_fit_p, self.a_actual,
                            self.a_rot_l, self.a_rot_r, self.a_del_page, self.a_organize, self.a_ocr, self.a_image,
                            self.a_sign, self.a_protect, self.a_compress, self.a_watermark, self.a_redact_apply,
                            self.a_auto_fields, self.a_clear_form, self.a_flatten_form, self.a_export_form,
                            self.a_import_form, self.a_sign_digital, self.a_verify,
                            *self.tool_actions.values()]

    def _menus(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addActions([self.a_open])
        self.recent_menu = m.addMenu("Open Recent")
        m.addActions([self.a_save, self.a_saveas])
        m.addSeparator()
        create = m.addMenu(icon("open"), "Create PDF")
        create.addActions([self.a_create_pdf, self.a_images_pdf])
        m.addAction(self.a_merge)
        m.addActions([self.a_split, self.a_repair])
        exp = m.addMenu("Export")
        exp.addActions([self.a_export_docx, self.a_export_xlsx, self.a_export_dxf, self.a_export_txt,
                        self.a_export_img])
        m.addSeparator()
        m.addActions([self.a_protect, self.a_active, self.a_compress, self.a_print])
        m.addSeparator()
        m.addAction(self.a_diagnose)
        m.addAction(self.a_close)
        m.addAction("Exit", self.close)
        m = mb.addMenu("&Edit")
        m.addActions([self.a_undo, self.a_redo])
        m.addSeparator()
        m.addActions([self.a_copy, self.a_find])
        m.addSeparator()
        m.addActions([self.tool_actions[TOOL_EDIT], self.tool_actions[TOOL_OBJECT], self.tool_actions[TOOL_ADD_TEXT],
                      self.a_image])
        m = mb.addMenu("&View")
        m.addActions([self.a_zoom_in, self.a_zoom_out, self.a_actual, self.a_fit_w, self.a_fit_p])
        m.addSeparator()
        m.addAction(self.pages_dock.toggleViewAction())
        m.addAction(self.props_dock.toggleViewAction())
        m = mb.addMenu("&Document")
        m.addActions([self.a_organize, self.a_rot_l, self.a_rot_r, self.a_del_page])
        m.addSeparator()
        m.addActions([self.a_ocr, self.a_watermark])
        m = mb.addMenu("&Comment")
        for t in (TOOL_HIGHLIGHT, TOOL_UNDERLINE, TOOL_STRIKE, TOOL_NOTE, TOOL_INK, TOOL_RECT, TOOL_ELLIPSE,
                  TOOL_LINE, TOOL_ARROW):
            m.addAction(self.tool_actions[t])
        m = mb.addMenu("&Tools")
        m.addActions([self.a_compare, self.a_batch])
        m.addSeparator()
        m.addActions([self.a_ocr, self.a_organize, self.a_merge, self.a_image, self.a_sign,
                      self.tool_actions[TOOL_REDACT], self.a_redact_apply, self.a_protect, self.a_compress,
                      self.a_watermark])
        m = mb.addMenu("F&orms")
        m.addAction(self.tool_actions[TOOL_FORM_EDIT])
        m.addActions([self.a_auto_fields, self.a_clear_form, self.a_flatten_form])
        m.addSeparator()
        m.addActions([self.a_export_form, self.a_import_form])
        m.addSeparator()
        m.addActions([self.a_sign_digital, self.a_verify, self.a_create_id])
        m = mb.addMenu("&Help")
        m.addAction("About", lambda: QMessageBox.about(
            self, APP_NAME, f"<b>{APP_NAME}</b><br>PDF editor with OCR, font matching, page organisation and "
                            f"image editing.<br><br>PyMuPDF {pymupdf.VersionBind} • Tesseract: "
                            f"{'found' if ocr.find_tesseract() else 'not found'}"))
        self._refresh_recent()

    def _toolbar(self):
        tb = QToolBar("Main")
        tb.setObjectName("mainToolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        self.addToolBar(tb)
        tb.addActions([self.a_open, self.a_save, self.a_print])
        tb.addSeparator()
        tb.addActions([self.a_undo, self.a_redo])
        tb.addSeparator()
        tb.addActions([self.tool_actions[TOOL_SELECT], self.tool_actions[TOOL_HAND]])
        tb.addSeparator()
        prev = self._act("Previous page", "prev", None, lambda: self.view.go_to_page(self.view.current_page() - 1))
        nxt = self._act("Next page", "next", None, lambda: self.view.go_to_page(self.view.current_page() + 1))
        tb.addAction(prev)
        self.page_box = QSpinBox()
        self.page_box.setRange(1, 1)
        self.page_box.setFixedWidth(70)
        self.page_box.editingFinished.connect(lambda: self.view.go_to_page(self.page_box.value() - 1))
        tb.addWidget(self.page_box)
        self.page_total = QLabel(" / 0 ")
        tb.addWidget(self.page_total)
        tb.addAction(nxt)
        tb.addSeparator()
        tb.addActions([self.a_zoom_out])
        self.zoom_box = QComboBox()
        self.zoom_box.setEditable(True)
        self.zoom_box.setFixedWidth(90)
        for z in ("50%", "75%", "100%", "125%", "150%", "200%", "300%", "400%"):
            self.zoom_box.addItem(z)
        self.zoom_box.setEditText("100%")
        self.zoom_box.textActivated.connect(self._zoom_text)
        self.zoom_box.lineEdit().returnPressed.connect(lambda: self._zoom_text(self.zoom_box.currentText()))
        tb.addWidget(self.zoom_box)
        tb.addActions([self.a_zoom_in, self.a_fit_w, self.a_fit_p])
        tb.addSeparator()
        tb.addAction(self.a_find)
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding, spacer.sizePolicy().verticalPolicy())
        tb.addWidget(spacer)
        for a in (self.a_ocr, self.a_organize, self.a_merge):
            tb.addAction(a)
        for btn in tb.findChildren(QToolButton):
            if btn.defaultAction() in (self.a_ocr, self.a_organize, self.a_merge):
                btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

    def _tool_rail(self):
        rail = QToolBar("Tools")
        rail.setObjectName("toolRail")
        rail.setMovable(False)
        rail.setOrientation(Qt.Orientation.Vertical)
        rail.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        rail.setIconSize(QSize(22, 22))
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, rail)
        rail.addAction(self.tool_actions[TOOL_EDIT])
        rail.addAction(self.tool_actions[TOOL_OBJECT])
        rail.addAction(self.tool_actions[TOOL_ADD_TEXT])
        rail.addAction(self.a_image)
        rail.addSeparator()
        for t in (TOOL_HIGHLIGHT, TOOL_UNDERLINE, TOOL_STRIKE, TOOL_NOTE, TOOL_INK):
            rail.addAction(self.tool_actions[t])
        shapes = QToolButton()
        shapes.setText("Shapes")
        shapes.setIcon(icon("rect"))
        shapes.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        shapes.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        from PyQt6.QtWidgets import QMenu
        sm = QMenu(shapes)
        for t in (TOOL_RECT, TOOL_ELLIPSE, TOOL_LINE, TOOL_ARROW):
            sm.addAction(self.tool_actions[t])
        shapes.setMenu(sm)
        rail.addWidget(shapes)
        rail.addSeparator()
        rail.addAction(self.a_sign)
        rail.addAction(self.tool_actions[TOOL_FORM_EDIT])
        rail.addAction(self.tool_actions[TOOL_REDACT])
        rail.addAction(self.a_protect)
        a = self._act("Tools", "settings", None, lambda: (self.props.setCurrentIndex(0), self.props_dock.show()))
        rail.addAction(a)
        # shorten labels on the rail
        for btn in rail.findChildren(QToolButton):
            act = btn.defaultAction()
            if act is self.a_image:
                btn.setText("Image")
            elif act is self.a_sign:
                btn.setText("Sign")
            elif act is self.a_protect:
                btn.setText("Protect")
            elif act is self.tool_actions[TOOL_FORM_EDIT]:
                btn.setText("Forms")
            elif act is self.tool_actions[TOOL_OBJECT]:
                btn.setText("Objects")

    def _statusbar(self):
        sb = self.statusBar()
        self.font_status = QLabel()
        sb.addPermanentWidget(self.font_status)
        self.sig_status = QLabel()
        sb.addPermanentWidget(self.sig_status)
        self.tess_status = QLabel("OCR ready" if ocr.find_tesseract() else "OCR: Tesseract not found")
        sb.addPermanentWidget(self.tess_status)

    def _search_bar(self):
        w = QWidget()
        w.setStyleSheet("background:#fff; border-bottom:1px solid #e1e1e1;")
        h = QHBoxLayout(w)
        h.setContentsMargins(8, 4, 8, 4)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Find text in document")
        self.search_edit.returnPressed.connect(self.search_next)
        self.search_edit.textChanged.connect(lambda _: setattr(self, "search_hits", []))
        self.search_case = QCheckBox("Match case")
        self.search_label = QLabel()
        prev = QToolButton()
        prev.setIcon(icon("prev"))
        prev.clicked.connect(lambda: self.search_next(-1))
        nxt = QToolButton()
        nxt.setIcon(icon("next"))
        nxt.clicked.connect(lambda: self.search_next(1))
        close = QToolButton()
        close.setIcon(icon("close"))
        close.clicked.connect(self.hide_search)
        h.addWidget(QLabel("Find:"))
        h.addWidget(self.search_edit, 1)
        h.addWidget(self.search_case)
        h.addWidget(prev)
        h.addWidget(nxt)
        h.addWidget(self.search_label)
        h.addWidget(close)
        w.setVisible(False)
        self.search_widget = w
        return w

    # ------------------------------------------------------------------ state
    def _on_loaded(self):
        has = self.pdoc.is_open
        for a in self.doc_actions:
            a.setEnabled(has)
        self.page_box.setRange(1, max(1, self.pdoc.page_count))
        self.page_total.setText(f" / {self.pdoc.page_count} ")
        self._update_title()
        self._update_undo()
        self._scanned_prompted.clear()
        self.search_hits = []
        self._signed = False
        self.sig_status.setText("")
        if has:
            QTimer.singleShot(0, self.view.fit_width)
            QTimer.singleShot(250, self._signature_status)

    def _on_structure(self):
        self.page_box.setRange(1, max(1, self.pdoc.page_count))
        self.page_total.setText(f" / {self.pdoc.page_count} ")
        self._update_undo()

    def _update_title(self):
        if self.pdoc.is_open:
            self.setWindowTitle(f"{'• ' if self.pdoc.dirty else ''}{self.pdoc.title} — {APP_NAME}")
        else:
            self.setWindowTitle(APP_NAME)

    def _update_undo(self):
        self.a_undo.setEnabled(self.pdoc.can_undo())
        self.a_redo.setEnabled(self.pdoc.can_redo())
        self.a_undo.setText(f"Undo {self.pdoc.undo_label()}".strip())
        self.a_redo.setText(f"Redo {self.pdoc.redo_label()}".strip())

    def _page_changed(self, p):
        self.page_box.blockSignals(True)
        self.page_box.setValue(p + 1)
        self.page_box.blockSignals(False)

    def _zoom_text(self, t):
        try:
            self.view.set_zoom(float(t.strip().rstrip("%")) / 100, anchor_mouse=False)
        except ValueError:
            pass

    def set_tool(self, tool):
        if not self.pdoc.is_open:
            return
        self.view.set_tool(tool)

    def toggle_tool(self, tool):
        """Toolbar/rail clicks: clicking the active tool again (e.g. Edit) turns it off."""
        if not self.pdoc.is_open:
            return
        if self.view.tool == tool and tool != TOOL_SELECT:
            self.view.finish_edit(True)
            self.set_tool(TOOL_SELECT)
        else:
            self.set_tool(tool)

    def _tool_changed(self, tool):
        a = self.tool_actions.get(tool)
        if a:
            a.setChecked(True)
        if tool in (TOOL_HIGHLIGHT, TOOL_UNDERLINE, TOOL_STRIKE, TOOL_INK, TOOL_RECT, TOOL_ELLIPSE, TOOL_LINE,
                    TOOL_ARROW, TOOL_NOTE, TOOL_REDACT):
            self.props.setCurrentIndex(3)
        elif tool == TOOL_ADD_TEXT:
            self.props.show_text_style(self.view.new_text_style)
        elif tool == TOOL_FORM_EDIT:
            self.props.show_field(None)
            self.props.setCurrentIndex(4)
            self.props_dock.show()
            self.statusBar().showMessage("Prepare Form: pick a field type on the right, then click or drag on the page.", 8000)
        elif tool != TOOL_EDIT:
            self.props.setCurrentIndex(0)
        hints = {TOOL_OBJECT: "Select Objects: click a drawing to select the whole figure, or drag a box around "
                              "what you want. Then drag to move, handles to resize, Ctrl+C / Ctrl+V to copy.",
                 TOOL_EDIT: "Edit: click text to edit it in its original font; click images to move/resize.",
                 TOOL_ADD_TEXT: "Add Text: click where the new text should go.",
                 TOOL_HIGHLIGHT: "Highlight: drag across text.", TOOL_REDACT: "Redact: drag over content, then Apply Redactions.",
                 TOOL_SELECT: "Select: drag to select text, Ctrl+C to copy, right-click for markup."}
        if tool in hints:
            self.statusBar().showMessage(hints[tool], 8000)

    # ------------------------------------------------------------------ file ops
    def open_dialog(self):
        if not self._maybe_save():
            return
        start = self.settings.value("lastDir", str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(self, "Open", start, FILE_FILTER)
        if path:
            self.open_file(path)

    def open_file(self, path):
        if not self._maybe_save():
            return
        from . import imagepdf, officeconvert
        path = str(path)
        ext = Path(path).suffix.lower()
        if ext in officeconvert.OFFICE_EXT:              # Word, Excel, PowerPoint: convert to PDF
            self._open_office_document(path)
            return
        if imagepdf.is_image_file(path):
            try:
                doc = imagepdf.images_to_pdf([path])
            except ValueError as ex:
                QMessageBox.critical(self, APP_NAME, str(ex))
                return
            self._open_new_document(doc, Path(path).stem + ".pdf")
            return
        password = None
        try:
            if PdfDocument.needs_password(path):
                password, ok = QInputDialog.getText(self, "Password required", f"{Path(path).name} is protected.\nPassword:",
                                                    QLineEdit.EchoMode.Password)
                if not ok:
                    return
            self.pdoc.open(path, password)
        except PermissionError:
            QMessageBox.warning(self, APP_NAME, "Incorrect password.")
            return
        except Exception as ex:
            if ext == ".pdf" and Path(path).is_file():
                if QMessageBox.question(self, APP_NAME, f"{Path(path).name} is damaged and cannot be opened.\n\n"
                                                        f"({ex})\n\nTry to repair it?") == QMessageBox.StandardButton.Yes:
                    self.repair_pdf(path, ask_save=False)
            else:
                QMessageBox.critical(self, APP_NAME, f"Cannot open file:\n{ex}")
            return
        self.settings.setValue("lastDir", str(Path(path).parent))
        self._add_recent(path)
        self.view.set_tool(TOOL_SELECT)
        if self.pdoc.doc.is_repaired:
            self.statusBar().showMessage("This file was damaged; PDF Studio repaired it while opening. "
                                         "Save it to keep the repaired version.", 15000)
        QTimer.singleShot(0, self._check_active_content)

    # ------------------------------------------------------------------ create, split, repair, safety
    def _open_new_document(self, doc, suggested_name: str):
        """Show a document made in memory (converted, created from images, repaired). It has no file yet, so
        Save asks where to put it - the source file (a .docx, a damaged PDF) is never overwritten."""
        self.view.finish_edit(False)
        self.pdoc.new_from(doc)
        self.pdoc.suggested_name = suggested_name
        self._update_title()
        self.view.set_tool(TOOL_SELECT)

    def _open_office_document(self, path: str):
        import shutil
        import tempfile
        import threading
        from . import officeconvert
        name = Path(path).name
        app = officeconvert.office_app_for(path)
        use_office = bool(app) and officeconvert.office_installed(app)
        if not use_office and Path(path).suffix.lower() != ".docx":
            QMessageBox.warning(self, APP_NAME, f"Converting {name} to PDF needs Microsoft {app.title()} installed "
                                                "on this PC.")
            return
        work_dir = tempfile.mkdtemp(prefix="pdfstudio_")
        out = os.path.join(work_dir, "converted.pdf")
        result = {}

        def work():
            try:
                result["value"] = officeconvert.document_to_pdf(path, out)
            except Exception as ex:
                result["error"] = ex

        if use_office:
            # Office runs as a separate program: wait for it without freezing the window
            prog = dialogs.ProgressDialog("Create PDF", self)
            prog.bar.setRange(0, 0)
            prog.label.setText(f"Converting {name} with Microsoft {app.title()}…")
            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            timer = QTimer(prog)
            timer.timeout.connect(lambda: None if worker.is_alive() else prog.accept())
            timer.start(100)
            prog.cancelled.connect(prog.reject)
            prog.exec()
            if worker.is_alive():
                self.statusBar().showMessage("Conversion cancelled", 5000)
                return
        else:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                work()
            finally:
                QApplication.restoreOverrideCursor()
        try:
            if "error" in result:
                raise result["error"]
            _, how = result["value"]
            doc = pymupdf.open("pdf", Path(out).read_bytes())
        except Exception as ex:
            QMessageBox.critical(self, APP_NAME, f"Could not convert {name}:\n{ex}")
            return
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        self.settings.setValue("lastDir", str(Path(path).parent))
        self._open_new_document(doc, Path(path).stem + ".pdf")
        self.statusBar().showMessage(f"Converted {name} using {how}. Save to keep the PDF.", 12000)

    def create_pdf_from_file(self):
        from . import imagepdf
        start = self.settings.value("lastDir", str(Path.home()))
        flt = ("Word, Excel, PowerPoint and images (*.docx *.doc *.docm *.rtf *.odt *.xlsx *.xls *.xlsm *.csv *.ods "
               "*.pptx *.ppt *.pptm *.odp *.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp);;"
               "Word documents (*.docx *.doc *.docm *.rtf *.odt);;Excel workbooks (*.xlsx *.xls *.xlsm *.csv *.ods);;"
               "PowerPoint presentations (*.pptx *.ppt *.pptm *.odp);;"
               "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp)")
        paths, _ = QFileDialog.getOpenFileNames(self, "Create PDF from file", start, flt)
        if not paths:
            return
        if len(paths) > 1 and all(imagepdf.is_image_file(p) for p in paths):
            self.images_to_pdf(paths)
        else:
            self.open_file(paths[0])

    def images_to_pdf(self, paths=None):
        dlg = dialogs.ImagesToPdfDialog(paths or [], self)
        if not paths:
            QTimer.singleShot(0, dlg.add_dialog)         # start by choosing the pictures
        if not dlg.exec() or dlg.result_doc is None:
            return
        if not self._maybe_save():
            dlg.result_doc.close()
            return
        chosen = dlg.paths()
        doc = dlg.result_doc
        self._open_new_document(doc, Path(chosen[0]).stem + ".pdf" if len(chosen) == 1 else "Images.pdf")
        self.statusBar().showMessage(f"Created a {doc.page_count}-page PDF from {len(chosen)} image(s). "
                                     "Save to keep it.", 12000)

    def split_pdf(self):
        if not self.pdoc.is_open:
            self.open_dialog()
            if not self.pdoc.is_open:
                return
        self.view.finish_edit(True)
        dlg = dialogs.SplitDialog(self.pdoc, self)
        if dlg.exec() and dlg.written:
            folder = str(Path(dlg.written[0]).parent)
            if QMessageBox.question(self, "Split PDF", f"Created {len(dlg.written)} files in:\n{folder}\n\n"
                                                       "Open the folder?") == QMessageBox.StandardButton.Yes:
                os.startfile(folder)

    def repair_pdf(self, path=None, ask_save=True):
        from . import repair
        if not path:
            start = self.settings.value("lastDir", str(Path.home()))
            path, _ = QFileDialog.getOpenFileName(self, "Repair PDF", start, "PDF files (*.pdf);;All files (*)")
            if not path:
                return
        if ask_save and not self._maybe_save():
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report, doc = repair.repair(path)
        except Exception as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Repair PDF", f"Repair failed:\n{ex}")
            return
        QApplication.restoreOverrideCursor()
        if not report.ok or doc is None:
            QMessageBox.critical(self, "Repair PDF", f"{Path(path).name}\n\n{report.summary()}")
            return
        self._open_new_document(doc, Path(path).stem + " (repaired).pdf")
        QMessageBox.information(self, "Repair PDF", f"{Path(path).name}\n\n{report.summary()}\n\n"
                                                    "The repaired document is open. Save it to keep it — "
                                                    "your original file has not been changed.")

    def _check_active_content(self):
        """Warn about scripts, launch/automatic actions and attached programs in the file just opened."""
        from . import security
        if not self.pdoc.is_open:
            return
        try:
            report = security.scan(self.pdoc.doc)
        except Exception:
            return
        if not report.findings:
            return
        if not report.risky:
            self.statusBar().showMessage(f"This PDF contains {report.summary()}. PDF Studio does not run them.", 12000)
            return
        if QMessageBox.question(
                self, "Potentially harmful content",
                f"This PDF contains {report.summary()}.\n\n"
                "PDF Studio never runs scripts, launch actions or attached files, so it cannot harm you here — "
                "but other PDF readers might run them if this file is passed on.\n\n"
                "Remove them from this document? Normal links, bookmarks and form fields keep working. "
                "Save afterwards to keep the cleaned file.") == QMessageBox.StandardButton.Yes:
            self._neutralise_active_content()

    def remove_active_content(self):
        from . import security
        if not self.pdoc.is_open:
            return
        report = security.scan(self.pdoc.doc)
        if not report.findings:
            QMessageBox.information(self, "Scripts and hidden actions",
                                    "This PDF contains no JavaScript, automatic or launch actions, dangerous links "
                                    "or attached programs.")
            return
        if QMessageBox.question(self, "Scripts and hidden actions",
                                f"Remove {report.summary()} from this document?") == QMessageBox.StandardButton.Yes:
            self._neutralise_active_content()

    def _neutralise_active_content(self):
        from . import security
        self.view.finish_edit(True)
        self.pdoc.begin_change("Remove scripts and hidden actions")
        try:
            removed = security.neutralise(self.pdoc.doc)
        except Exception as ex:
            self.pdoc.cancel_change()
            QMessageBox.critical(self, APP_NAME, f"Could not remove them:\n{ex}")
            return
        self.pdoc.end_change(None)
        left = security.scan(self.pdoc.doc)
        self.statusBar().showMessage(f"Removed {removed} script(s) and hidden action(s). Save to keep the cleaned file."
                                     + (f" Still present: {left.summary()}." if left.findings else ""), 15000)

    def _add_recent(self, path):
        rec = [p for p in (self.settings.value("recent", []) or []) if p != path]
        rec.insert(0, path)
        self.settings.setValue("recent", rec[:10])
        self._refresh_recent()

    def _refresh_recent(self):
        self.recent_menu.clear()
        for p in self.settings.value("recent", []) or []:
            self.recent_menu.addAction(Path(p).name, lambda p=p: self.open_file(p)).setToolTip(p)
        self.recent_menu.setEnabled(not self.recent_menu.isEmpty())

    def save(self):
        if not self.pdoc.is_open:
            return False
        self.view.finish_edit(True)
        if self._signed and QMessageBox.question(
                self, "Signed document", "This PDF is digitally signed. Saving changes will invalidate the "
                                         "existing signatures.\n\nSave anyway?") != QMessageBox.StandardButton.Yes:
            return False
        if not self.pdoc.path or Path(self.pdoc.path).suffix.lower() != ".pdf":
            return self.save_as()
        return self._save_to(self.pdoc.path)

    def save_as(self):
        if not self.pdoc.is_open:
            return False
        self.view.finish_edit(True)
        suggested = (str(Path(self.pdoc.path).with_suffix(".pdf")) if self.pdoc.path
                     else str(Path(self.settings.value("lastDir", str(Path.home()))) / self.pdoc.title))
        path, _ = QFileDialog.getSaveFileName(self, "Save As", suggested, "PDF files (*.pdf)")
        if not path:
            return False
        ok = self._save_to(path)
        if ok:
            self._add_recent(path)
        return ok

    def _save_to(self, path, **kw):
        try:
            self.pdoc.save(path, **kw)
            self.statusBar().showMessage(f"Saved {path}", 5000)
            self._update_title()
            return True
        except Exception as ex:
            QMessageBox.critical(self, APP_NAME, f"Could not save:\n{ex}")
            return False

    def _maybe_save(self) -> bool:
        if not self.pdoc.is_open or not self.pdoc.dirty:
            return True
        r = QMessageBox.question(self, APP_NAME, f"Save changes to {self.pdoc.title}?",
                                 QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                 | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Save:
            return self.save()
        return r == QMessageBox.StandardButton.Discard

    def close_doc(self):
        if self._maybe_save():
            self.view.finish_edit(False)
            self.pdoc.close()

    def closeEvent(self, e):
        self.view.finish_edit(True)
        if self._maybe_save():
            self.settings.setValue("geometry", self.saveGeometry())
            e.accept()
        else:
            e.ignore()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        from . import imagepdf
        if len(paths) > 1 and all(imagepdf.is_image_file(p) for p in paths):
            self.images_to_pdf(paths)                   # several pictures: make one PDF of them
        elif len(paths) > 1:
            self.merge(paths)
        elif self.pdoc.is_open and Path(paths[0]).suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff"):
            self.insert_image(paths[0])
        else:
            self.open_file(paths[0])

    def print_doc(self):
        from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dlg = QPrintDialog(printer, self)
        dlg.setMinMax(1, self.pdoc.page_count)
        if dlg.exec() != QPrintDialog.DialogCode.Accepted:
            return
        first = max(1, printer.fromPage() or 1) - 1
        last = (printer.toPage() or self.pdoc.page_count) - 1
        painter = QPainter(printer)
        dpi = min(300, printer.resolution())
        for i in range(first, last + 1):
            if i > first:
                printer.newPage()
            pix = self.pdoc.page(i).get_pixmap(dpi=dpi, alpha=False)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
            area = painter.viewport()
            size = img.size().scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio)
            painter.drawImage(area.x() + (area.width() - size.width()) // 2, area.y(),
                              img.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        painter.end()

    def export_cad(self):
        """Read the drawing on this page and write it out as CAD geometry (DXF)."""
        if not self.pdoc.is_open:
            return
        from PyQt6.QtGui import QCursor
        from . import cad
        from .dialogs import CadExportDialog
        idx = self.view.current_page()
        try:
            info = cad.analyse(self.pdoc.doc, idx)
        except Exception as ex:
            QMessageBox.critical(self, "Export to CAD", f"Could not read this page:\n{ex}")
            return
        if not info["drawing"] and QMessageBox.question(
                self, "Export to CAD",
                f"Page {idx + 1} does not look like an engineering drawing.\n\nExport it anyway?") \
                != QMessageBox.StandardButton.Yes:
            return
        if info.get("scanned") and not self._opencv_ready("Export drawing to CAD"):
            return                       # tracing a scanned drawing needs the image library
        dlg = CadExportDialog(info, self)
        if not dlg.exec():
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export drawing to CAD", self._suggest(".dxf"),
                                              "CAD drawing (*.dxf)")
        if not path:
            return
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            rep = cad.export_dxf(self.pdoc.doc, idx, path, dlg.options())
        except Exception as ex:
            QMessageBox.critical(self, "Export to CAD", f"Export failed:\n{ex}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.statusBar().showMessage(f"CAD drawing saved: {rep.describe()}", 12000)
        QMessageBox.information(self, "Export complete",
                                f"Page {idx + 1} saved as a CAD drawing:\n{path}\n\n{rep.describe()}")

    def export_images(self):
        folder = QFileDialog.getExistingDirectory(self, "Export pages to folder")
        if not folder:
            return
        dpi, ok = QInputDialog.getInt(self, "Export images", "Resolution (dpi):", 150, 36, 600)
        if not ok:
            return
        stem = Path(self.pdoc.title).stem
        for i in range(self.pdoc.page_count):
            self.pdoc.page(i).get_pixmap(dpi=dpi, alpha=False).save(str(Path(folder) / f"{stem}_page{i + 1}.png"))
        self.statusBar().showMessage(f"Exported {self.pdoc.page_count} images to {folder}", 6000)

    def export_text(self):
        from . import convert
        path, _ = QFileDialog.getSaveFileName(self, "Export text", str(Path(self.pdoc.title).with_suffix(".txt")), "Text (*.txt)")
        if path:
            convert.to_text(self.pdoc.doc, path)
            self.statusBar().showMessage(f"Text exported to {path}", 6000)

    def _suggest(self, suffix):
        base = Path(self.pdoc.path) if self.pdoc.path else Path(self.pdoc.title)
        return str(base.with_suffix(suffix))

    def _scanned_without_text(self) -> bool:
        from . import scanedit
        return any(scanedit.is_scanned_page(p) and scanedit.visible_text_chars(p) == 0 and not p.get_text().strip()
                   for p in self.pdoc.doc)

    def export_word(self):
        from . import convert
        if self._scanned_without_text() and QMessageBox.question(
                self, "Export", "Some pages are scans without recognised text, so they will export as empty pages."
                                "\n\nRun Recognize Text (OCR) first?") == QMessageBox.StandardButton.Yes:
            self.run_ocr()
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export to Word", self._suggest(".docx"), "Word document (*.docx)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            convert.to_docx(self.pdoc.doc, path)
        except Exception as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Export", f"Export failed:\n{ex}")
            return
        QApplication.restoreOverrideCursor()
        self._offer_open(path, "Word document")

    def export_excel(self):
        from . import convert
        if self._scanned_without_text() and QMessageBox.question(
                self, "Export", "Some pages are scans without recognised text.\n\nRun Recognize Text (OCR) first?") \
                == QMessageBox.StandardButton.Yes:
            self.run_ocr()
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export tables to Excel", self._suggest(".xlsx"), "Excel workbook (*.xlsx)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            n = convert.to_xlsx(self.pdoc.doc, path)
        except Exception as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Export", f"Export failed:\n{ex}")
            return
        QApplication.restoreOverrideCursor()
        if n == 0:
            self.statusBar().showMessage("No tables were detected - the text was exported line by line.", 8000)
        self._offer_open(path, f"Excel workbook ({n} table{'s' if n != 1 else ''})")

    def _offer_open(self, path, what):
        if QMessageBox.question(self, "Export complete", f"{what} saved:\n{path}\n\nOpen it now?") \
                == QMessageBox.StandardButton.Yes:
            os.startfile(path)

    # ------------------------------------------------------------------ edit ops
    def undo(self):
        self.view.finish_edit(False)
        self.pdoc.undo()
        self._update_undo()

    def redo(self):
        self.view.finish_edit(False)
        self.pdoc.redo()
        self._update_undo()

    def rotate_current(self, d):
        i = self.view.current_page()
        self.pdoc.begin_change("Rotate page")
        p = self.pdoc.page(i)
        p.set_rotation((p.rotation + d) % 360)
        self.pdoc.end_change(structure=True)
        self.view.go_to_page(i)

    def delete_current_page(self):
        if self.pdoc.page_count <= 1:
            return
        i = self.view.current_page()
        if QMessageBox.question(self, "Delete page", f"Delete page {i + 1}?") != QMessageBox.StandardButton.Yes:
            return
        self.pdoc.begin_change("Delete page")
        self.pdoc.doc.delete_page(i)
        self.pdoc.end_change(structure=True)

    def organize(self):
        if not self.pdoc.is_open:
            return
        self.view.finish_edit(True)
        dialogs.OrganizeDialog(self.pdoc, self).exec()
        self._update_undo()

    def merge(self, paths=None):
        dlg = dialogs.MergeDialog(self, paths if isinstance(paths, list) else None)
        if self.pdoc.is_open and self.pdoc.path and not paths:
            dlg._add_path(self.pdoc.path) if not self.pdoc.dirty else None
        if dlg.exec() != dialogs.QDialog.DialogCode.Accepted:
            return
        try:
            out = dlg.build()
        except Exception as ex:
            QMessageBox.critical(self, "Combine", str(ex))
            return
        if not self._maybe_save():
            return
        self.pdoc.new_from(out, None)
        self.statusBar().showMessage(f"Combined {dlg.list.count()} files into {out.page_count} pages — use Save As", 8000)

    # ------------------------------------------------------------------ search
    def show_search(self):
        self.search_widget.setVisible(True)
        sel = self.view.selected_text()
        if sel and len(sel) < 80:
            self.search_edit.setText(sel)
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def hide_search(self):
        self.search_widget.setVisible(False)
        self.search_hits = []
        self.view.show_search_results([])

    def search_next(self, direction=1):
        text = self.search_edit.text()
        if not text or not self.pdoc.is_open:
            return
        if not self.search_hits:
            hits = []
            case = self.search_case.isChecked()
            for i in range(self.pdoc.page_count):
                page = self.pdoc.page(i)
                for r in page.search_for(text):
                    if case:
                        got = page.get_textbox(r + (-0.5, -0.5, 0.5, 0.5)).strip()
                        if text not in got:
                            continue
                    hits.append((i, r))
            self.search_hits = hits
            self.search_index = -1
            if not hits:
                self.search_label.setText("No matches")
                self.view.show_search_results([])
                return
        self.search_index = (self.search_index + (direction if isinstance(direction, int) else 1)) % len(self.search_hits)
        self.search_label.setText(f"{self.search_index + 1} of {len(self.search_hits)}")
        self.view.show_search_results(self.search_hits, self.search_index)

    # ------------------------------------------------------------------ OCR
    def _scanned_pages(self, pages):
        pages = [p for p in pages if p not in self._scanned_prompted]
        if not pages:
            return
        todo = self.view.scanned_pages_without_text()
        self._scanned_prompted.update(todo)
        if not todo:
            return
        n = len(todo)
        r = QMessageBox.question(
            self, "Scanned document",
            f"This document has {n} scanned page{'s' if n > 1 else ''}.\n\n"
            "Recognize the text so it can be edited? The original scan is kept exactly as it is — only the "
            "text you change is re-typeset in the detected font, size and colour.")
        if r == QMessageBox.StandardButton.Yes:
            self._start_ocr(todo, "eng", 300, ocr.MODE_EDITABLE)

    def _opencv_ready(self, what: str) -> bool:
        """OpenCV is optional: tell the user what is missing instead of failing halfway."""
        from . import cvsafe
        if cvsafe.AVAILABLE:
            return True
        QMessageBox.warning(self, what, cvsafe.MESSAGE)
        return False

    def diagnose_problems(self):
        from .app import diagnose
        diagnose()

    def run_ocr(self):
        if not self._opencv_ready("Recognize Text (OCR)"):
            return
        if not self.pdoc.is_open:
            return
        self.view.finish_edit(True)
        dlg = dialogs.OcrDialog(self.pdoc.page_count, self.view.current_page(), self)
        if dlg.exec() != dialogs.QDialog.DialogCode.Accepted:
            return
        pages = dlg.pages()
        if not pages:
            return
        self._start_ocr(pages, dlg.langs(), dlg.dpi.currentData(), dlg.mode())

    def _start_ocr(self, pages, langs, dpi, mode):
        if not ocr.find_tesseract():
            QMessageBox.warning(self, "OCR", "Tesseract OCR is not installed.\n"
                                             "Download: https://github.com/UB-Mannheim/tesseract/wiki")
            return
        prog = dialogs.ProgressDialog("Recognizing text", self)
        results = []
        worker = dialogs.OcrWorker(self.pdoc.doc.tobytes(garbage=0), pages, langs, dpi, mode)
        worker.progress.connect(prog.set)
        worker.page_done.connect(results.append)
        errors = []
        worker.failed.connect(errors.append)
        prog.cancelled.connect(worker.stop)
        worker.finished.connect(prog.accept)
        worker.start()
        prog.exec()
        worker.stop()
        worker.wait()
        if errors:
            QMessageBox.critical(self, "OCR", errors[0])
        if not results:
            return
        self.pdoc.begin_change("Recognize text")
        changed = set()
        for res in results:
            try:
                ocr.apply_ocr(self.pdoc.doc, res, mode)
                changed.add(res.page)
            except Exception as ex:
                QMessageBox.warning(self, "OCR", f"Page {res.page + 1}: {ex}")
        self.pdoc.end_change(changed)
        words = sum(len(r.words) for r in results)
        fonts = {}
        for r in results:
            for (fam, bold, italic), n in r.fonts.items():
                key = fam + (" Bold" if bold else "") + (" Italic" if italic else "")
                fonts[key] = fonts.get(key, 0) + n
        top = ", ".join(f for f, _ in sorted(fonts.items(), key=lambda kv: -kv[1])[:3])
        msg = f"Recognized {words} words on {len(results)} page(s)."
        if mode == ocr.MODE_EDITABLE:
            if top:
                msg += f" Fonts detected: {top}. Click any text to edit it."
            self.set_tool(TOOL_EDIT)
        self.statusBar().showMessage(msg, 12000)

    # ------------------------------------------------------------------ images
    def insert_image(self, path=None):
        if not self.pdoc.is_open:
            return
        if not isinstance(path, str):
            path, _ = QFileDialog.getOpenFileName(self, "Add image", self.settings.value("imgDir", ""),
                                                  "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp)")
            if not path:
                return
            self.settings.setValue("imgDir", str(Path(path).parent))
        try:
            pil = imagetools.load_image(Path(path).read_bytes())
        except Exception as ex:
            QMessageBox.critical(self, "Add image", f"Cannot read image:\n{ex}")
            return
        dlg = dialogs.ImageSettingsDialog(pil, parent=self, title="Add image — settings")
        if dlg.exec() != dialogs.QDialog.DialogCode.Accepted:
            return
        self.view.begin_place_image(pil, dlg.settings())

    def sign(self):
        if not self.pdoc.is_open:
            return
        dlg = dialogs.SignatureDialog(self)
        if dlg.exec() != dialogs.QDialog.DialogCode.Accepted:
            return
        img = dlg.result_image()
        if img is None:
            return
        self.view.begin_place_image(img, imagetools.ImageSettings())

    def image_settings_selected(self):
        box = self.view.selected_image
        if not box:
            return
        doc = self.pdoc.doc
        try:
            pil = pageimages.extract_pil(doc, box.img.xref)
        except Exception as ex:
            QMessageBox.warning(self, "Image", f"Cannot read this image: {ex}")
            return
        page = self.pdoc.page(box.img.page)
        st = imagetools.ImageSettings(behind_text=pageimages.is_behind_text(page, box.img))
        dlg = dialogs.ImageSettingsDialog(pil, st, self)
        if dlg.exec() != dialogs.QDialog.DialogCode.Accepted:
            return
        img = box.img
        self.view._mutate("Image settings", img.page,
                          lambda: pageimages.replace_with_settings(doc, page, img, dlg.settings(), pil))

    def transform_selected_image(self, **kw):
        box = self.view.selected_image
        if not box:
            return
        doc = self.pdoc.doc
        page = self.pdoc.page(box.img.page)
        img = box.img
        st = imagetools.ImageSettings(behind_text=pageimages.is_behind_text(page, img), **kw)
        rect = [None]

        def do():
            rect[0] = pageimages.replace_with_settings(doc, page, img, st)
        self.view._mutate("Transform image", img.page, do)
        if rect[0] is not None:
            self.view._reselect(img.page, rect[0])

    def replace_selected_image(self):
        box = self.view.selected_image
        if not box:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Replace image", "", "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff)")
        if not path:
            return
        pil = imagetools.load_image(Path(path).read_bytes())
        dlg = dialogs.ImageSettingsDialog(pil, parent=self, title="Replace image — settings")
        if dlg.exec() != dialogs.QDialog.DialogCode.Accepted:
            return
        doc, page, img = self.pdoc.doc, self.pdoc.page(box.img.page), box.img
        self.view._mutate("Replace image", img.page,
                          lambda: pageimages.replace_with_settings(doc, page, img, dlg.settings(), pil))

    # ------------------------------------------------------------------ security / output
    def apply_redactions(self):
        n = 0
        for p in self.pdoc.doc:
            n += sum(1 for a in p.annots() if a.type[0] == pymupdf.PDF_ANNOT_REDACT)
        if not n:
            QMessageBox.information(self, "Redact", "No areas are marked for redaction.\nUse the Redact tool first.")
            return
        if QMessageBox.question(self, "Apply redactions",
                                f"Permanently remove content under {n} marked area(s)?") != QMessageBox.StandardButton.Yes:
            return
        self.pdoc.begin_change("Apply redactions")
        for p in self.pdoc.doc:
            p.apply_redactions()
        self.pdoc.end_change()

    def protect(self):
        if not self.pdoc.is_open:
            return
        dlg = dialogs.ProtectDialog(self)
        if dlg.exec() != dialogs.QDialog.DialogCode.Accepted:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save protected PDF",
                                              str(Path(self.pdoc.path or "document.pdf").with_name(Path(self.pdoc.title).stem + "_protected.pdf")),
                                              "PDF (*.pdf)")
        if not path:
            return
        data = self.pdoc.doc.tobytes(garbage=3, deflate=True, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                                     user_pw=dlg.encryption()["user"], owner_pw=dlg.encryption()["owner"],
                                     permissions=dlg.encryption()["permissions"])
        Path(path).write_bytes(data)
        self.statusBar().showMessage(f"Protected copy saved: {path}", 8000)

    def compress(self):
        if not self.pdoc.is_open:
            return
        levels = ["High quality (150 dpi images)", "Balanced (110 dpi)", "Smallest (72 dpi)"]
        choice, ok = QInputDialog.getItem(self, "Compress PDF", "Image quality:", levels, 1, False)
        if not ok:
            return
        dpi, quality = {0: (150, 85), 1: (110, 70), 2: (72, 55)}[levels.index(choice)]
        path, _ = QFileDialog.getSaveFileName(self, "Save compressed PDF",
                                              str(Path(self.pdoc.path or "document.pdf").with_name(Path(self.pdoc.title).stem + "_compressed.pdf")),
                                              "PDF (*.pdf)")
        if not path:
            return
        before = len(self.pdoc.doc.tobytes(garbage=0))
        tmp = pymupdf.open("pdf", self.pdoc.doc.tobytes(garbage=0))
        try:
            tmp.rewrite_images(dpi_threshold=dpi + 20, dpi_target=dpi, quality=quality)
        except Exception:
            pass
        tmp.subset_fonts()
        data = tmp.tobytes(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True)
        Path(path).write_bytes(data)
        self.statusBar().showMessage(f"Compressed: {before / 1024:.0f} KB → {len(data) / 1024:.0f} KB  ({path})", 12000)

    def watermark(self):
        if not self.pdoc.is_open:
            return
        dlg = dialogs.WatermarkDialog(self.pdoc.page_count, self)
        if dlg.exec() != dialogs.QDialog.DialogCode.Accepted:
            return
        self.pdoc.begin_change("Watermark")
        pages = dlg.apply(self.pdoc.doc)
        self.pdoc.end_change(set(pages))

    # ------------------------------------------------------------------ compare / batch
    def compare_files(self):
        from . import compare
        dlg = dialogs.CompareDialog(self.pdoc.path if self.pdoc.is_open and not self.pdoc.dirty else None, self)
        if dlg.exec() != dialogs.QDialog.DialogCode.Accepted:
            return
        a_path, b_path = dlg.a.text(), dlg.b.text()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            doc_a, doc_b = pymupdf.open(a_path), pymupdf.open(b_path)
            if doc_a.needs_pass or doc_b.needs_pass:
                raise PermissionError("password-protected files must be unlocked first")
            changes = compare.compare(doc_a, doc_b, visual=dlg.visual.isChecked())
            from .paths import user_data_dir
            out = str(user_data_dir() / f"Compare - {Path(a_path).stem} vs {Path(b_path).stem}.pdf")
            compare.build_report(doc_a, doc_b, changes, out, Path(a_path).name, Path(b_path).name)
        except Exception as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Compare", f"Could not compare:\n{ex}")
            return
        QApplication.restoreOverrideCursor()
        s = compare.summary(changes)
        if not self._maybe_save():
            return
        self.open_file(out)
        self.statusBar().showMessage(f"Changed {s['changed']} • inserted {s['inserted']} • deleted {s['deleted']} • "
                                     f"graphics {s['visual']}   (hover a highlight to see the change)", 15000)

    def batch_process(self):
        initial = [self.pdoc.path] if self.pdoc.is_open and self.pdoc.path else []
        dialogs.BatchDialog(self, initial).exec()

    # ------------------------------------------------------------------ forms
    def arm_field(self, ftype, label):
        if not self.pdoc.is_open:
            return
        if self.view.tool != TOOL_FORM_EDIT:
            self.set_tool(TOOL_FORM_EDIT)
        self.view.form_field_type = ftype
        self.statusBar().showMessage(f"Click or drag on the page to add a {label} field (hold Ctrl to add several)", 8000)

    def auto_detect_fields(self):
        if not self.pdoc.is_open:
            return
        doc = self.pdoc.doc
        found = [(p.number, forms.auto_detect(p)) for p in doc]
        n = sum(len(d) for _, d in found)
        if not n:
            QMessageBox.information(self, "Auto-detect fields", "No new form fields were found.")
            return
        if QMessageBox.question(self, "Auto-detect fields",
                                f"Found {n} likely form field{'s' if n > 1 else ''} (fill-in lines, boxes, check boxes)."
                                f"\n\nAdd them?") != QMessageBox.StandardButton.Yes:
            return
        self.pdoc.begin_change("Auto-detect fields")
        for pno, det in found:
            forms.apply_detected(doc, pno, det)
        self.pdoc.end_change({p for p, d in found if d})
        if self.view.tool != TOOL_FORM_EDIT:
            self.set_tool(TOOL_FORM_EDIT)
        self.statusBar().showMessage(f"Added {n} fields. Select one to rename it or change its options.", 8000)

    def clear_form(self):
        if not self.pdoc.is_open or not forms.has_form(self.pdoc.doc):
            return
        self.pdoc.begin_change("Clear form")
        forms.reset_form(self.pdoc.doc)
        self.pdoc.end_change()

    def flatten_form(self):
        if not self.pdoc.is_open or not forms.has_form(self.pdoc.doc):
            return
        if QMessageBox.question(self, "Flatten form", "Burn the current field values into the pages?\n\n"
                                "The form will no longer be fillable (you can undo).") != QMessageBox.StandardButton.Yes:
            return
        self.pdoc.begin_change("Flatten form")
        forms.flatten(self.pdoc.doc)
        self.pdoc.end_change()

    def export_form_data(self):
        if not self.pdoc.is_open or not forms.has_form(self.pdoc.doc):
            QMessageBox.information(self, "Form data", "This document has no form fields.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export form data", str(Path(self.pdoc.title).with_suffix(".json")),
                                              "Form data (*.json)")
        if path:
            forms.save_data_json(self.pdoc.doc, path)
            self.statusBar().showMessage(f"Form data exported to {path}", 6000)

    def import_form_data(self):
        if not self.pdoc.is_open or not forms.has_form(self.pdoc.doc):
            QMessageBox.information(self, "Form data", "This document has no form fields.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import form data", "", "Form data (*.json)")
        if not path:
            return
        self.pdoc.begin_change("Import form data")
        try:
            n = forms.load_data_json(self.pdoc.doc, path)
        except Exception as ex:
            self.pdoc.cancel_change()
            QMessageBox.critical(self, "Form data", f"Could not import:\n{ex}")
            return
        self.pdoc.end_change()
        self.statusBar().showMessage(f"Filled {n} fields", 6000)

    # ------------------------------------------------------------------ signatures
    def sign_field(self, f):
        box = QMessageBox(self)
        box.setWindowTitle("Sign")
        box.setText(f"How do you want to sign “{f.name}”?")
        hand = box.addButton("Handwritten signature", QMessageBox.ButtonRole.ActionRole)
        cert = box.addButton("Digital ID (certificate)", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is hand:
            dlg = dialogs.SignatureDialog(self)
            if dlg.exec() != dialogs.QDialog.DialogCode.Accepted:
                return
            img = dlg.result_image()
            if img is None:
                return
            page = self.pdoc.page(f.page)
            r = f.rect
            ar = img.width / max(1, img.height)
            w = min(r.width, r.height * ar)
            h = w / ar
            rect = pymupdf.Rect(r.x0 + (r.width - w) / 2, r.y0 + (r.height - h) / 2,
                                r.x0 + (r.width + w) / 2, r.y0 + (r.height + h) / 2)
            self.view._mutate("Sign", f.page, lambda: pageimages.insert_pil(page, rect, img))
        elif clicked is cert:
            self.sign_digital(field=f.name)

    def create_digital_id(self):
        dlg = dialogs.CreateDigitalIdDialog(self)
        if dlg.exec() == dialogs.QDialog.DialogCode.Accepted:
            QMessageBox.information(self, "Digital ID", f"Digital ID created:\n{dlg.path.text()}\n\n"
                                                        "Keep this file and its password safe.")

    def sign_digital(self, field=None):
        if not self.pdoc.is_open:
            return
        if not signing.AVAILABLE:
            QMessageBox.warning(self, "Sign", "Digital signatures need the pyHanko component, which is not installed.")
            return
        self.view.finish_edit(True)
        data = self.pdoc.doc.tobytes(garbage=3, deflate=True)
        names = [n for n, signed in signing.signature_fields(data) if not signed]
        dlg = dialogs.DigitalSignDialog(names, self, default_field=field)
        if dlg.exec() != dialogs.QDialog.DialogCode.Accepted:
            return
        target = dlg.field.currentData()
        stem = Path(self.pdoc.title).stem
        start = str(Path(self.pdoc.path).with_name(stem + "_signed.pdf")) if self.pdoc.path else stem + "_signed.pdf"
        out, _ = QFileDialog.getSaveFileName(self, "Save signed PDF", start, "PDF (*.pdf)")
        if not out:
            return
        page = self.view.current_page()
        pg = self.pdoc.page(page)
        ur = (pg.rect * pg.derotation_matrix).normalize()
        rect = pymupdf.Rect(ur.x1 - 236, ur.y1 - 110, ur.x1 - 36, ur.y1 - 50)
        try:
            signing.sign_pdf(data, out, dlg.pfx_path(), dlg.pw.text(), field_name=target, page=page, rect=rect,
                             reason=dlg.reason.currentText(), location=dlg.location.text(),
                             signature_image=dlg.signature_image, certify=dlg.certify.isChecked())
        except Exception as ex:
            QMessageBox.critical(self, "Sign", f"Signing failed:\n{ex}")
            return
        self.pdoc.set_dirty(False)       # the signed copy contains every edit made so far
        self.open_file(out)
        self.statusBar().showMessage(f"Signed and saved: {out}", 10000)

    def verify_signatures(self):
        if not self.pdoc.is_open:
            return
        if not signing.AVAILABLE:
            QMessageBox.warning(self, "Signatures", "Signature verification needs the pyHanko component.")
            return
        p = Path(self.pdoc.path) if self.pdoc.path else None
        data = p.read_bytes() if p and p.is_file() and not self.pdoc.dirty else self.pdoc.doc.tobytes()
        try:
            reports = signing.verify(data, signing.trusted_certificates())
        except Exception as ex:
            QMessageBox.critical(self, "Signatures", f"Could not verify:\n{ex}")
            return
        dialogs.SignaturesDialog(reports, self).exec()

    def _signature_status(self):
        self._signed = False
        self.sig_status.setText("")
        if not self.pdoc.is_open or not signing.AVAILABLE or not self.pdoc.path:
            return
        try:
            data = Path(self.pdoc.path).read_bytes()
        except OSError:
            return
        if b"/ByteRange" not in data:
            return
        try:
            reports = signing.verify(data, signing.trusted_certificates())
        except Exception:
            return
        if not reports:
            return
        self._signed = True
        ok = all(r.intact and r.valid for r in reports)
        trusted = all(r.trusted for r in reports)
        if ok and trusted:
            msg, col = "✔ Signed – all signatures valid", "#1b7f3b"
        elif ok:
            msg, col = "⚠ Signed – signer identity not trusted", "#b26a00"
        else:
            msg, col = "✖ Signature INVALID – document changed after signing", "#c62828"
        self.sig_status.setText(msg)
        self.sig_status.setStyleSheet(f"color:{col}; font-weight:600; padding:0 8px;")
