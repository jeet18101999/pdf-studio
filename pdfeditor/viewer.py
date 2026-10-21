"""Document canvas: continuous page view, tools, inline text editing and image manipulation."""
from __future__ import annotations

import pymupdf
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QCursor, QFont, QImage, QKeySequence, QPainter, QPainterPath,
                         QPen, QPixmap, QTextCursor, QTextOption, QTextBlockFormat, QGuiApplication)
from PyQt6.QtWidgets import (QApplication, QGraphicsItem, QGraphicsPathItem, QGraphicsPixmapItem,
                             QGraphicsRectItem, QGraphicsScene, QGraphicsTextItem, QGraphicsView,
                             QInputDialog, QMenu, QGraphicsEllipseItem, QGraphicsLineItem, QGraphicsProxyWidget,
                             QGraphicsWidget, QLineEdit, QPlainTextEdit, QCheckBox, QComboBox, QListWidget,
                             QPushButton, QAbstractItemView)

from PyQt6 import sip

from . import annots, forms, objects, pagecache, pageimages, scanedit, textops


def later(fn):
    """Run fn after the current event has been fully handled.

    Actions triggered from inside a QGraphicsItem's own event handler (release, double-click, context menu,
    key press) often change the PDF, which rebuilds the overlay items - deleting the item whose handler is
    still on the stack. Returning into a deleted item crashes Qt (access violation), so such actions are
    always deferred to the event loop."""
    QTimer.singleShot(0, fn)


def alive(item) -> bool:
    return item is not None and not sip.isdeleted(item) and item.scene() is not None
from .document import PdfDocument
from .textops import TextBlock, TextStyle

GAP = 14.0
SCREEN_DPI = 96.0

TOOL_SELECT = "select"
TOOL_HAND = "hand"
TOOL_EDIT = "edit"
TOOL_ADD_TEXT = "addtext"
TOOL_HIGHLIGHT = "highlight"
TOOL_UNDERLINE = "underline"
TOOL_STRIKE = "strike"
TOOL_NOTE = "note"
TOOL_INK = "ink"
TOOL_RECT = "rect"
TOOL_ELLIPSE = "ellipse"
TOOL_LINE = "line"
TOOL_ARROW = "arrow"
TOOL_REDACT = "redact"
TOOL_PLACE_IMAGE = "placeimage"
TOOL_FORM_EDIT = "formedit"
TOOL_OBJECT = "objects"

DRAG_TOOLS = {TOOL_RECT, TOOL_ELLIPSE, TOOL_LINE, TOOL_ARROW, TOOL_REDACT, TOOL_PLACE_IMAGE, TOOL_FORM_EDIT, TOOL_OBJECT}

FIELD_DEFAULT_SIZE = {forms.CHECKBOX: (12, 12), forms.RADIO: (12, 12), forms.LISTBOX: (120, 60),
                      forms.SIGNATURE: (160, 40), forms.COMBO: (130, 18), forms.TEXT: (150, 18)}
MARKUP_TOOLS = {TOOL_HIGHLIGHT, TOOL_UNDERLINE, TOOL_STRIKE}


def qcolor(rgb) -> QColor:
    return QColor.fromRgbF(*[max(0.0, min(1.0, c)) for c in rgb[:3]])


# =============================================================================== items
class PageItem(QGraphicsRectItem):
    def __init__(self, index: int, w: float, h: float):
        super().__init__(0, 0, w, h)
        self.index = index
        self.setBrush(QBrush(Qt.GlobalColor.white))
        self.setPen(QPen(QColor(0, 0, 0, 40), 0))
        self.pix_item = QGraphicsPixmapItem(self)
        self.pix_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.rendered_scale = 0.0
        self.setZValue(0)


HANDLE = 7.0

RESIZE_CURSORS = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
                  "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
                  "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
                  "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor}


class ImageBoxItem(QGraphicsRectItem):
    """Selection frame for an image with move + 8 resize handles. Commits on mouse release."""
    lock_aspect = True

    def __init__(self, view: "DocumentView", img: pageimages.PageImage, rect: QRectF):
        super().__init__(rect)
        self.view, self.img = view, img
        self.orig = QRectF(rect)
        self.setAcceptHoverEvents(True)
        self.setZValue(20)
        self.selected = False
        self._drag = None
        self._apply_pen()

    def _apply_pen(self):
        pen = QPen(QColor(20, 115, 230), 0, Qt.PenStyle.SolidLine if self.selected else Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QBrush(QColor(20, 115, 230, 25 if self.selected else 0)))

    def set_selected(self, on):
        self.selected = on
        self._apply_pen()
        self.update()

    def _handles(self):
        r = self.rect()
        s = HANDLE / max(0.01, self.view.transform().m11())
        pts = {"tl": r.topLeft(), "t": QPointF(r.center().x(), r.top()), "tr": r.topRight(),
               "r": QPointF(r.right(), r.center().y()), "br": r.bottomRight(),
               "b": QPointF(r.center().x(), r.bottom()), "bl": r.bottomLeft(), "l": QPointF(r.left(), r.center().y())}
        return {k: QRectF(p.x() - s / 2, p.y() - s / 2, s, s) for k, p in pts.items()}

    def boundingRect(self):
        s = HANDLE / max(0.01, self.view.transform().m11())
        return self.rect().adjusted(-s, -s, s, s)

    def shape(self):
        p = QPainterPath()
        p.addRect(self.boundingRect())
        return p

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if self.selected:
            painter.setPen(QPen(QColor(20, 115, 230), 0))
            painter.setBrush(Qt.GlobalColor.white)
            for r in self._handles().values():
                painter.drawRect(r)

    def _handle_at(self, pos):
        if not self.selected:
            return None
        for k, r in self._handles().items():
            if r.adjusted(-2, -2, 2, 2).contains(pos):
                return k
        return None

    def hoverMoveEvent(self, e):
        h = self._handle_at(e.pos())
        self.setCursor(RESIZE_CURSORS.get(h, Qt.CursorShape.SizeAllCursor))

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(e)
        self.view.select_image(self)
        self._drag = (self._handle_at(e.pos()) or "move", e.pos(), QRectF(self.rect()))
        e.accept()

    def mouseMoveEvent(self, e):
        if not self._drag:
            return
        kind, start, r0 = self._drag
        d = e.pos() - start
        r = QRectF(r0)
        keep = (self.view.keep_aspect and self.lock_aspect) != bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if kind == "move":
            r.translate(d)
        else:
            if "l" in kind:
                r.setLeft(min(r0.right() - 4, r0.left() + d.x()))
            if "r" in kind:
                r.setRight(max(r0.left() + 4, r0.right() + d.x()))
            if "t" in kind:
                r.setTop(min(r0.bottom() - 4, r0.top() + d.y()))
            if "b" in kind:
                r.setBottom(max(r0.top() + 4, r0.bottom() + d.y()))
            if keep and kind in ("tl", "tr", "bl", "br") and r0.height() > 0:
                ar = r0.width() / r0.height()
                w = r.width()
                h = w / ar
                if "t" in kind:
                    r.setTop(r.bottom() - h)
                else:
                    r.setBottom(r.top() + h)
        self.prepareGeometryChange()
        self.setRect(r.normalized())
        self.view.status.emit(f"{r.width():.0f} × {r.height():.0f} pt")

    def mouseReleaseEvent(self, e):
        if not self._drag:
            return
        self._drag = None
        if self.rect() != self.orig:
            view = self.view
            later(lambda: view.commit_image_geometry(self))

    def mouseDoubleClickEvent(self, e):
        view, img = self.view, self.img
        later(lambda: view.image_settings_requested.emit(img))

    def contextMenuEvent(self, e):
        self.view.select_image(self)
        self.view.image_context_menu(self, e.screenPos())


class FieldBoxItem(ImageBoxItem):
    """A form field in Prepare Form mode: select, move, resize (commits on release)."""
    lock_aspect = False

    def __init__(self, view: "DocumentView", field: forms.FieldInfo, rect: QRectF):
        QGraphicsRectItem.__init__(self, rect)
        self.view, self.field, self.img = view, field, None
        self.orig = QRectF(rect)
        self.setAcceptHoverEvents(True)
        self.setZValue(20)
        self.selected = False
        self._drag = None
        self._apply_pen()
        self.setToolTip(f"{field.type_name}: {field.name}")

    def _apply_pen(self):
        pen = QPen(QColor(20, 115, 230) if self.selected else QColor(40, 90, 200, 170), 0)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QBrush(QColor(20, 115, 230, 70) if self.selected else QColor(120, 160, 255, 50)))

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        r = self.rect()
        f = QFont("Segoe UI")
        f.setPixelSize(max(1, int(min(8, r.height() * 0.62))))
        painter.setFont(f)
        painter.setPen(QColor(10, 60, 160))
        label = self.field.name if self.field.type not in (forms.CHECKBOX, forms.RADIO) else ""
        if label:
            painter.drawText(r.adjusted(2, 0, -2, 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), label)

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return QGraphicsRectItem.mousePressEvent(self, e)
        self.view.select_field(self)
        self._drag = (self._handle_at(e.pos()) or "move", e.pos(), QRectF(self.rect()))
        e.accept()

    def mouseReleaseEvent(self, e):
        if not self._drag:
            return
        self._drag = None
        if self.rect() != self.orig:
            view = self.view
            later(lambda: view.commit_field_geometry(self))

    def mouseDoubleClickEvent(self, e):
        view, field = self.view, self.field
        later(lambda: view.field_selected.emit(field))

    def contextMenuEvent(self, e):
        self.view.select_field(self)
        view = self.view
        m = QMenu()
        m.addAction("Duplicate", lambda: later(view.duplicate_selected_field))
        m.addAction("Delete", lambda: later(view.delete_selected_field))
        m.exec(e.screenPos())


class TextBlockItem(ImageBoxItem):
    """Editable paragraph frame (Edit tool).

    click -> edit the text   |   drag inside -> move the whole box
    corner handles -> width and height   |   left/right handles -> width only (text re-wraps)
    top/bottom handles -> height only (line spacing adapts)"""
    lock_aspect = False
    DRAG_THRESHOLD = 4        # screen pixels before a press becomes a drag

    def __init__(self, view: "DocumentView", block: TextBlock, rect: QRectF):
        QGraphicsRectItem.__init__(self, rect)
        self.view, self.block, self.img = view, block, None
        self.orig = QRectF(rect)
        self.setAcceptHoverEvents(True)
        self.setZValue(10)
        self.selected = False
        self._drag = None
        self._moved = False
        self._hovered = False
        self._press_screen = None
        self._apply_pen()
        self.setCursor(Qt.CursorShape.IBeamCursor if block.editable else Qt.CursorShape.ForbiddenCursor)
        if block.editable:
            self.setToolTip("Click to edit • drag to move • drag a handle to resize")

    def _active(self):
        return self._hovered or self._drag is not None

    def _apply_pen(self):
        on = self._active()
        pen = QPen(QColor(20, 115, 230, 230 if on else 90), 0,
                   Qt.PenStyle.SolidLine if on else Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QBrush(QColor(20, 115, 230, 18 if on else 0)))

    def _handle_at(self, pos):
        if not self.block.editable:
            return None
        for k, r in self._handles().items():
            if r.adjusted(-2, -2, 2, 2).contains(pos):
                return k
        return None

    def paint(self, painter, option, widget=None):
        QGraphicsRectItem.paint(self, painter, option, widget)
        if self._active() and self.block.editable:
            painter.setPen(QPen(QColor(20, 115, 230), 0))
            painter.setBrush(Qt.GlobalColor.white)
            for r in self._handles().values():
                painter.drawRect(r)

    def hoverEnterEvent(self, e):
        self._hovered = True
        self._apply_pen()
        self.update()

    def hoverLeaveEvent(self, e):
        self._hovered = False
        self._apply_pen()
        self.update()

    def hoverMoveEvent(self, e):
        h = self._handle_at(e.pos())
        if h:
            self.setCursor(RESIZE_CURSORS[h])
        else:
            self.setCursor(Qt.CursorShape.IBeamCursor if self.block.editable else Qt.CursorShape.ForbiddenCursor)

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or not self.block.editable:
            return QGraphicsRectItem.mousePressEvent(self, e)
        self._drag = (self._handle_at(e.pos()) or "move", e.pos(), QRectF(self.rect()))
        self._press_screen = e.screenPos()
        self._moved = False
        self._apply_pen()
        e.accept()

    def mouseMoveEvent(self, e):
        if not self._drag:
            return
        if not self._moved:
            d = e.screenPos() - self._press_screen
            if abs(d.x()) + abs(d.y()) < self.DRAG_THRESHOLD:
                return
            self._moved = True
            if self._drag[0] == "move":
                self.setCursor(Qt.CursorShape.SizeAllCursor)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if not self._drag:
            return
        self._drag = None
        self._apply_pen()
        view, block = self.view, self.block
        if not self._moved:
            later(lambda: view.start_block_edit(block, self))
        elif self.rect() != self.orig:
            later(lambda: view.commit_block_geometry(self))

    def mouseDoubleClickEvent(self, e):
        if self.block.editable:
            view, block = self.view, self.block
            later(lambda: view.start_block_edit(block, self))

    def contextMenuEvent(self, e):
        view, block = self.view, self.block
        m = QMenu()
        a_edit = m.addAction("Edit text")
        a_del = m.addAction("Delete text block")
        a_copy = m.addAction("Copy text")
        act = m.exec(e.screenPos())
        if act == a_edit:
            later(lambda: view.start_block_edit(block, self))
        elif act == a_del:
            later(lambda: view.delete_block(block))
        elif act == a_copy:
            QGuiApplication.clipboard().setText(block.text)


class AnnotBoxItem(ImageBoxItem):
    """A drawn shape / pen stroke / note: click to select, drag to move, handles to resize
    (Shift keeps proportions), Delete to remove."""
    lock_aspect = False

    def __init__(self, view: "DocumentView", info: annots.AnnotInfo, rect: QRectF):
        QGraphicsRectItem.__init__(self, rect)
        self.view, self.info, self.img = view, info, None
        self.orig = QRectF(rect)
        self.setAcceptHoverEvents(True)
        self.setZValue(18)
        self.selected = False
        self._hovered = False
        self._drag = None
        self._apply_pen()
        self.setToolTip(f"{info.type_name} — click to select, drag to move, Delete to remove")

    def _apply_pen(self):
        if self.selected:
            pen = QPen(QColor(20, 115, 230), 0, Qt.PenStyle.SolidLine)
        elif self._hovered:
            pen = QPen(QColor(20, 115, 230, 170), 0, Qt.PenStyle.DashLine)
        else:
            pen = QPen(Qt.PenStyle.NoPen)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QBrush(QColor(20, 115, 230, 22 if self.selected else 0)))

    def _handle_at(self, pos):
        if not self.selected or not self.info.resizable:
            return None
        for k, r in self._handles().items():
            if r.adjusted(-2, -2, 2, 2).contains(pos):
                return k
        return None

    def paint(self, painter, option, widget=None):
        QGraphicsRectItem.paint(self, painter, option, widget)
        if self.selected and self.info.resizable:
            painter.setPen(QPen(QColor(20, 115, 230), 0))
            painter.setBrush(Qt.GlobalColor.white)
            for r in self._handles().values():
                painter.drawRect(r)

    def hoverEnterEvent(self, e):
        self._hovered = True
        self._apply_pen()
        self.update()

    def hoverLeaveEvent(self, e):
        self._hovered = False
        self._apply_pen()
        self.update()

    def hoverMoveEvent(self, e):
        h = self._handle_at(e.pos())
        self.setCursor(RESIZE_CURSORS.get(h, Qt.CursorShape.SizeAllCursor))

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return QGraphicsRectItem.mousePressEvent(self, e)
        self.view.select_annot(self)
        self._drag = (self._handle_at(e.pos()) or "move", e.pos(), QRectF(self.rect()))
        e.accept()

    def mouseReleaseEvent(self, e):
        if not self._drag:
            return
        self._drag = None
        if self.rect() != self.orig:
            view = self.view
            later(lambda: view.commit_annot_geometry(self))

    def mouseDoubleClickEvent(self, e):
        e.accept()

    def contextMenuEvent(self, e):
        self.view.select_annot(self)
        view = self.view
        m = QMenu()
        m.addAction("Delete", lambda: later(view.delete_selected_annot))
        m.exec(e.screenPos())


class ObjectBoxItem(ImageBoxItem):
    """Frame around a selected figure (Select Objects tool): drag to move, handles to resize
    (proportional; Shift for free resizing)."""
    lock_aspect = True

    def __init__(self, view: "DocumentView", sel: objects.Selection, rect: QRectF):
        QGraphicsRectItem.__init__(self, rect)
        self.view, self.sel, self.img = view, sel, None
        self.orig = QRectF(rect)
        self.setAcceptHoverEvents(True)
        self.setZValue(22)
        self.selected = True
        self._drag = None
        pen = QPen(QColor(20, 115, 230), 0, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QBrush(QColor(20, 115, 230, 20)))
        self.setToolTip(f"{sel.describe()} — drag to move, handles to resize, Ctrl+C/Ctrl+V to copy, Delete to remove")

    def _apply_pen(self):
        pass

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return QGraphicsRectItem.mousePressEvent(self, e)
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            e.ignore()          # Ctrl+click reaches the page through the frame, to add/remove a figure
            return
        self._drag = (self._handle_at(e.pos()) or "move", e.pos(), QRectF(self.rect()))
        self.view.setFocus()
        e.accept()

    def mouseReleaseEvent(self, e):
        if not self._drag:
            return
        self._drag = None
        if self.rect() != self.orig:
            view = self.view
            later(lambda: view.commit_object_geometry(self))

    def mouseMoveEvent(self, e):
        ImageBoxItem.mouseMoveEvent(self, e)
        if self._drag and self._drag[0] == "move" and e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            _, start, r0 = self._drag           # Shift keeps the move on one axis
            d = e.pos() - start
            r = QRectF(r0)
            r.translate(d.x(), 0) if abs(d.x()) >= abs(d.y()) else r.translate(0, d.y())
            self.prepareGeometryChange()
            self.setRect(r)

    def mouseDoubleClickEvent(self, e):
        e.accept()

    def contextMenuEvent(self, e):
        view = self.view
        m = QMenu()
        m.addAction("Copy\tCtrl+C", lambda: later(view.copy_objects))
        m.addAction("Cut\tCtrl+X", lambda: later(view.cut_objects))
        m.addAction("Duplicate\tCtrl+D", lambda: later(view.duplicate_objects))
        m.addSeparator()
        m.addAction("Rotate left\t[", lambda: later(lambda: view.rotate_objects(-90)))
        m.addAction("Rotate right\t]", lambda: later(lambda: view.rotate_objects(90)))
        m.addAction("Flip horizontally\tM", lambda: later(lambda: view.flip_objects(True)))
        m.addAction("Flip vertically\tShift+M", lambda: later(lambda: view.flip_objects(False)))
        m.addSeparator()
        m.addAction("Save as image…", lambda: later(view.save_objects_image))
        m.addAction("Delete\tDel", lambda: later(view.delete_objects))
        m.exec(e.screenPos())


class _PlainTextField(QPlainTextEdit):
    def __init__(self, text, on_commit):
        super().__init__(text)
        self._on_commit = on_commit

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self._on_commit(self.toPlainText())


class InlineTextEditor(QGraphicsTextItem):
    """Editable text overlay matching the original font, size and colour."""
    SCALE = 10.0     # item works in 10x units so fractional point sizes are exact

    def __init__(self, view: "DocumentView", text: str, style: TextStyle, width: float):
        super().__init__()
        self.view = view
        self.setScale(1 / self.SCALE)
        self.setZValue(50)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setPlainText(text)
        self.min_width = width
        self.apply_style(style)
        self.document().contentsChanged.connect(self._grow)

    def apply_style(self, style: TextStyle):
        f = QFont(style.family)
        f.setPixelSize(max(1, round(style.size * self.SCALE)))
        f.setBold(style.bold)
        f.setItalic(style.italic)
        f.setUnderline(style.underline)
        self.setFont(f)
        self.setDefaultTextColor(qcolor(style.color))
        self.document().setDocumentMargin(0)
        opt = QTextOption()
        opt.setAlignment({textops.ALIGN_LEFT: Qt.AlignmentFlag.AlignLeft, textops.ALIGN_CENTER: Qt.AlignmentFlag.AlignHCenter,
                          textops.ALIGN_RIGHT: Qt.AlignmentFlag.AlignRight,
                          textops.ALIGN_JUSTIFY: Qt.AlignmentFlag.AlignJustify}[style.align])
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(opt)
        # line height
        cur = QTextCursor(self.document())
        cur.select(QTextCursor.SelectionType.Document)
        fmt = QTextBlockFormat()
        fmt.setLineHeight(style.line_height * style.size * self.SCALE, 2)   # 2 = FixedHeight
        cur.mergeBlockFormat(fmt)
        self.setTextWidth(self.min_width * self.SCALE)
        self.update()

    def _grow(self):
        self.view.editor_changed()

    def keyPressEvent(self, e):
        view = self.view
        if e.key() == Qt.Key.Key_Escape:
            later(lambda: view.finish_edit(commit=False))      # finishing removes this very item
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            later(lambda: view.finish_edit(commit=True))
            return
        super().keyPressEvent(e)


# =============================================================================== view
class DocumentView(QGraphicsView):
    page_changed = pyqtSignal(int)
    zoom_changed = pyqtSignal(float)
    status = pyqtSignal(str)
    edit_started = pyqtSignal(object)          # TextStyle
    edit_finished = pyqtSignal()
    image_selected = pyqtSignal(object)        # PageImage or None
    image_settings_requested = pyqtSignal(object)
    scanned_page_edit = pyqtSignal(int)        # user tried to edit text on a scanned page
    scanned_pages_detected = pyqtSignal(list)  # scanned pages without recognised text (Edit tool)
    tool_changed = pyqtSignal(str)
    image_placed = pyqtSignal()
    field_selected = pyqtSignal(object)        # forms.FieldInfo or None (Prepare Form)
    sign_field_requested = pyqtSignal(object)  # forms.FieldInfo (signature field clicked while filling)

    def __init__(self, pdoc: PdfDocument, parent=None):
        super().__init__(parent)
        self.pdoc = pdoc
        self.scene_ = QGraphicsScene(self)
        self.setScene(self.scene_)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QColor("#e9e9e9"))
        self.pages: list[PageItem] = []
        self.zoom = 1.0
        self.tool = TOOL_SELECT
        self.annot_color = (1.0, 0.85, 0.0)
        self.stroke_color = (0.85, 0.1, 0.1)
        self.stroke_width = 1.5
        self.keep_aspect = True
        self.overlays: list[QGraphicsItem] = []
        self._page_overlays: dict[int, list] = {}      # Edit mode: boxes built so far, per page
        self._overlay_restore = (None, None)           # (image, shape) to re-select when boxes are rebuilt
        self.search_items: list[QGraphicsItem] = []
        self.editor: InlineTextEditor | None = None
        self.editor_bg: QGraphicsRectItem | None = None
        self.edit_block: TextBlock | None = None
        self.edit_new_at: tuple | None = None       # (page, Point) for new text
        self.edit_style: TextStyle | None = None
        self.selected_image: ImageBoxItem | None = None
        self.pending_image = None                   # (PIL image, ImageSettings) for placement
        self.new_text_style = TextStyle(family="Arial", size=12)
        self.selected_annot: AnnotBoxItem | None = None
        self.object_sel: objects.Selection | None = None
        self.object_box: ObjectBoxItem | None = None
        self.object_fids: set = set()
        self.object_hover: QGraphicsRectItem | None = None
        self._hover_key = None
        self._object_commit = False
        self._last_scene_pos: QPointF | None = None
        self.form_items: list[QGraphicsProxyWidget] = []
        self.selected_field: FieldBoxItem | None = None
        self.form_field_type: int | None = None     # armed field type in Prepare Form
        self._form_commit = False
        self._press = None
        self._rubber: QGraphicsItem | None = None
        self._ink_path: list[QPointF] = []
        self._sel_items: list[QGraphicsItem] = []
        self._sel_page_rect = None

        self._render_timer = QTimer(self, singleShot=True, interval=40, timeout=self._render_visible)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.horizontalScrollBar().valueChanged.connect(lambda _: self._render_timer.start())
        pdoc.loaded.connect(self.rebuild)
        pdoc.structure_changed.connect(self.rebuild)
        pdoc.changed.connect(self.refresh_pages)

    # ------------------------------------------------------------------ layout
    def rebuild(self):
        cur = self.current_page() if self.pages else 0
        self.finish_edit(commit=False)
        self.scene_.clear()
        self.pages.clear()
        self.overlays.clear()
        self.search_items.clear()
        self._sel_items.clear()
        self.form_items.clear()
        self.selected_image = None
        self.selected_field = None
        self.selected_annot = None
        if not self.pdoc.is_open:
            self.scene_.setSceneRect(QRectF())
            return
        y = GAP
        maxw = 0
        for i in range(self.pdoc.page_count):
            r = self.pdoc.page(i).rect
            item = PageItem(i, r.width, r.height)
            item.setPos(-r.width / 2, y)
            self.scene_.addItem(item)
            self.pages.append(item)
            y += r.height + GAP
            maxw = max(maxw, r.width)
        self.scene_.setSceneRect(QRectF(-maxw / 2 - GAP * 3, 0, maxw + GAP * 6, y))
        self._apply_zoom()
        self._build_tool_items()
        self.go_to_page(min(cur, len(self.pages) - 1))

    def refresh_pages(self, pages=None):
        if not self.pages:
            return
        if len(self.pages) != self.pdoc.page_count:
            self.rebuild()
            return
        if pages is None:
            self.finish_edit(commit=False)          # the whole document was replaced (undo, redo, ...)
        for item in self.pages:
            if pages is None or item.index in pages:
                r = self.pdoc.page(item.index).rect
                if abs(item.rect().width() - r.width) > 0.1 or abs(item.rect().height() - r.height) > 0.1:
                    self.rebuild()
                    return
                item.rendered_scale = 0
        self._render_visible()
        if not self._form_commit:
            if self.tool == TOOL_EDIT and pages is not None:
                self._refresh_overlay_pages(pages)  # only the pages that changed
            else:
                self._build_tool_items()
        self.clear_selection()

    def _apply_zoom(self):
        s = self.zoom * SCREEN_DPI / 72.0
        self.setTransform(self.transform().fromScale(s, s))
        self.zoom_changed.emit(self.zoom)
        self._render_timer.start()

    def set_zoom(self, z: float, anchor_mouse=True):
        z = max(0.1, min(8.0, z))
        if abs(z - self.zoom) < 1e-4:
            return
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse if anchor_mouse
                                     else QGraphicsView.ViewportAnchor.AnchorViewCenter)
        factor = z / self.zoom
        self.zoom = z
        self.scale(factor, factor)
        self.zoom_changed.emit(self.zoom)
        self.page_changed.emit(self.current_page())
        self._render_timer.start()

    def fit_width(self):
        if not self.pages:
            return
        w = self.pages[self.current_page()].rect().width()
        avail = self.viewport().width() - 40
        self.set_zoom(avail / (w * SCREEN_DPI / 72.0), anchor_mouse=False)

    def fit_page(self):
        if not self.pages:
            return
        r = self.pages[self.current_page()].rect()
        s = min((self.viewport().width() - 40) / r.width(), (self.viewport().height() - 30) / r.height())
        self.set_zoom(s * 72.0 / SCREEN_DPI, anchor_mouse=False)
        self.go_to_page(self.current_page())

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            step = 1.12 if e.angleDelta().y() > 0 else 1 / 1.12
            self.set_zoom(self.zoom * step)
            e.accept()
        else:
            super().wheelEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._render_timer.start()

    def _on_scroll(self, _):
        self._render_timer.start()
        self.page_changed.emit(self.current_page())

    def current_page(self) -> int:
        if not self.pages:
            return 0
        center = self.mapToScene(self.viewport().rect().center())
        for item in self.pages:
            top = item.pos().y()
            if center.y() <= top + item.rect().height() + GAP / 2:
                return item.index
        return len(self.pages) - 1

    def go_to_page(self, i: int):
        if 0 <= i < len(self.pages):
            item = self.pages[i]
            self.verticalScrollBar().setValue(int(self.mapFromScene(QPointF(0, item.pos().y() - GAP / 2)).y()
                                                  + self.verticalScrollBar().value()))
            self.page_changed.emit(i)

    # ------------------------------------------------------------------ rendering
    def _render_visible(self):
        if not self.pages or not self.pdoc.is_open:
            return
        vis = self.mapToScene(self.viewport().rect()).boundingRect()
        margin = vis.height()
        vis.adjust(0, -margin, 0, margin)
        dpr = self.devicePixelRatioF()
        scale = self.zoom * SCREEN_DPI / 72.0 * dpr
        for item in self.pages:
            r = item.sceneBoundingRect()
            if r.intersects(vis):
                if abs(item.rendered_scale - scale) > 1e-3:
                    self._render_page(item, scale)
            elif item.rendered_scale and (r.bottom() < vis.top() - margin * 3 or r.top() > vis.bottom() + margin * 3):
                item.pix_item.setPixmap(QPixmap())      # free memory far away
                item.rendered_scale = 0
        self._ensure_overlays()                         # Edit mode: boxes for pages scrolled into view

    def _render_page(self, item: PageItem, scale: float):
        scale = min(scale, 6.0)
        page = self.pdoc.page(item.index)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=True)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        item.pix_item.setPixmap(QPixmap.fromImage(img))
        item.pix_item.setScale(1 / scale)
        item.rendered_scale = scale

    # ------------------------------------------------------------------ coordinates
    def scene_to_page(self, sp: QPointF):
        for item in self.pages:
            local = sp - item.pos()
            if item.rect().adjusted(-GAP / 2, -GAP / 2, GAP / 2, GAP / 2).contains(local):
                page = self.pdoc.page(item.index)
                p = pymupdf.Point(local.x(), local.y()) * page.derotation_matrix
                return item.index, p
        return None, None

    def page_rect_to_scene(self, index: int, rect) -> QRectF:
        page = self.pdoc.page(index)
        r = pymupdf.Rect(rect) * page.rotation_matrix
        origin = self.pages[index].pos()
        return QRectF(origin.x() + r.x0, origin.y() + r.y0, r.width, r.height)

    def scene_rect_to_page(self, index: int, qr: QRectF) -> pymupdf.Rect:
        page = self.pdoc.page(index)
        o = self.pages[index].pos()
        r = pymupdf.Rect(qr.left() - o.x(), qr.top() - o.y(), qr.right() - o.x(), qr.bottom() - o.y())
        return (r * page.derotation_matrix).normalize()

    # ------------------------------------------------------------------ tools
    def set_tool(self, tool: str):
        if tool == self.tool and tool != TOOL_EDIT:
            return
        self.finish_edit(commit=True)
        self.clear_selection()
        if tool != TOOL_OBJECT:
            self.object_sel = None
            self.object_fids = set()
            self._clear_object_hover()
        self.tool = tool
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag if tool == TOOL_HAND else QGraphicsView.DragMode.NoDrag)
        cursors = {TOOL_SELECT: Qt.CursorShape.IBeamCursor, TOOL_HAND: Qt.CursorShape.OpenHandCursor,
                   TOOL_EDIT: Qt.CursorShape.ArrowCursor, TOOL_ADD_TEXT: Qt.CursorShape.IBeamCursor,
                   TOOL_NOTE: Qt.CursorShape.PointingHandCursor}
        self.viewport().setCursor(cursors.get(tool, Qt.CursorShape.CrossCursor))
        self._build_tool_items()
        self.tool_changed.emit(tool)
        if tool == TOOL_EDIT:
            pending = self.scanned_pages_without_text()
            if pending:
                self.scanned_pages_detected.emit(pending)

    def scanned_pages_without_text(self) -> list[int]:
        doc = self.pdoc.doc
        return [p.number for p in doc if scanedit.is_scanned_page(p) and not scanedit.has_meta(doc, p)]

    def block_scene_rect(self, block) -> QRectF:
        return self.page_rect_to_scene(block.page, block.page_quad().rect)

    def clear_overlays(self):
        for it in self.overlays:
            if it.scene():
                self.scene_.removeItem(it)
        self.overlays.clear()
        self._page_overlays.clear()
        self.selected_annot = None
        if self.selected_image is not None:
            self.selected_image = None
            self.image_selected.emit(None)

    def build_overlays(self, select_annot: tuple | None = None):
        """Edit mode: text, image and shape boxes. Only pages on or near the screen get them - building
        boxes for every page up front took over a minute on a long scanned file - and further pages get
        theirs as they scroll into view."""
        sel = self.selected_image
        sel_img = (sel.img.xref, sel.img.page) if alive(sel) and getattr(sel, "img", None) is not None else None
        self.clear_overlays()
        self._overlay_restore = (sel_img, select_annot)
        if not self.pdoc.is_open:
            return
        self._ensure_overlays()

    def _pages_near_view(self) -> list[int]:
        """Pages on screen, plus one screen-height above and below."""
        if not self.pages:
            return []
        vis = self.mapToScene(self.viewport().rect()).boundingRect()
        margin = vis.height()
        vis.adjust(0, -margin, 0, margin)
        return [item.index for item in self.pages if item.sceneBoundingRect().intersects(vis)]

    def _ensure_overlays(self):
        if self.tool != TOOL_EDIT or not self.pdoc.is_open:
            return
        built = False
        for idx in self._pages_near_view():
            if idx not in self._page_overlays:
                self._build_page_overlays(idx)
                built = True
        if built:
            self._order_overlays()

    def _ensure_page_overlays(self, idx: int):
        if self.tool == TOOL_EDIT and self.pdoc.is_open and 0 <= idx < len(self.pages) \
                and idx not in self._page_overlays:
            self._build_page_overlays(idx)
            self._order_overlays()

    def _order_overlays(self):
        """Keep `overlays` in page order however the pages were (re)built, so it stays predictable."""
        paged = [it for idx in sorted(self._page_overlays) for it in self._page_overlays[idx]]
        ids = {id(it) for it in paged}
        self.overlays[:] = paged + [it for it in self.overlays if id(it) not in ids]

    def _page_blocks(self, page) -> list:
        doc = self.pdoc.doc
        return list(pagecache.get(page, "edit_blocks",
                                  lambda: textops.extract_blocks(page) + scanedit.blocks_for_page(doc, page)))

    def _build_page_overlays(self, idx: int):
        items = self._page_overlays.setdefault(idx, [])
        page = self.pdoc.page(idx)
        sel_img, sel_annot = self._overlay_restore
        background = scanedit.scan_background_xrefs(page)     # the scan and edit patches are not user images
        for img in pageimages.list_images(page):
            if img.xref in background:
                continue
            box = ImageBoxItem(self, img, self.page_rect_to_scene(idx, img.rect))
            self.scene_.addItem(box)
            self.overlays.append(box)
            items.append(box)
            if sel_img == (img.xref, img.page):
                self.select_image(box)
        for block in self._page_blocks(page):
            tb = TextBlockItem(self, block, self.block_scene_rect(block).adjusted(-1, -1, 1, 1))
            self.scene_.addItem(tb)
            self.overlays.append(tb)
            items.append(tb)
        for info in annots.list_annots(page):
            box = AnnotBoxItem(self, info, self.page_rect_to_scene(idx, info.rect))
            self.scene_.addItem(box)
            self.overlays.append(box)
            items.append(box)
            if sel_annot == (info.page, info.xref):
                self.select_annot(box)

    def _drop_page_overlays(self, idx: int):
        items = self._page_overlays.pop(idx, [])
        if not items:
            return
        gone = {id(it) for it in items}
        if self.selected_image is not None and id(self.selected_image) in gone:
            self.select_image(None)
        if self.selected_annot is not None and id(self.selected_annot) in gone:
            self.selected_annot = None
        for it in items:
            if alive(it) and it.scene() is self.scene_:
                self.scene_.removeItem(it)
        self.overlays[:] = [it for it in self.overlays if id(it) not in gone]

    def _refresh_overlay_pages(self, pages):
        """After an edit on some pages, rebuild only those pages' boxes."""
        sel = self.selected_image
        sel_img = (sel.img.xref, sel.img.page) if alive(sel) and getattr(sel, "img", None) is not None else None
        sel_annot = (self.selected_annot.info.page, self.selected_annot.info.xref) \
            if alive(self.selected_annot) else None
        self._overlay_restore = (sel_img, sel_annot)
        near = set(self._pages_near_view())
        for idx in sorted(pages):
            self._drop_page_overlays(idx)
            if idx in near:
                self._build_page_overlays(idx)
        self._order_overlays()

    # ------------------------------------------------------------------ mouse
    def mousePressEvent(self, e):
        sp = self.mapToScene(e.position().toPoint())
        hit0 = self.itemAt(e.position().toPoint())
        if isinstance(hit0, (QGraphicsProxyWidget, AnnotBoxItem)) or \
                (self.tool == TOOL_FORM_EDIT and isinstance(hit0, FieldBoxItem)) or \
                (self.tool == TOOL_OBJECT and isinstance(hit0, ObjectBoxItem) and
                 not e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            return super().mousePressEvent(e)          # form widgets / shapes / field frames handle their own clicks
        if self.selected_annot is not None and e.button() == Qt.MouseButton.LeftButton:
            self.select_annot(None)
        if self.editor is not None and e.button() == Qt.MouseButton.LeftButton:
            hit = self.itemAt(e.position().toPoint())
            if hit is not self.editor and hit is not self.editor_bg:
                # the commit rebuilds the page: never tear the scene down inside a mouse handler
                self.finish_edit(commit=True, defer_commit=True)
                if self.tool == TOOL_ADD_TEXT:
                    return
        if self.tool in (TOOL_HAND, TOOL_EDIT) or e.button() != Qt.MouseButton.LeftButton:
            if self.tool == TOOL_EDIT and e.button() == Qt.MouseButton.LeftButton:
                hit = self.itemAt(e.position().toPoint())
                if not isinstance(hit, (ImageBoxItem, TextBlockItem, InlineTextEditor)):
                    self.select_image(None)
                    idx, p = self.scene_to_page(sp)
                    if idx is not None:
                        page = self.pdoc.page(idx)
                        if scanedit.is_scanned_page(page) and not scanedit.has_meta(self.pdoc.doc, page):
                            self.scanned_page_edit.emit(idx)
            return super().mousePressEvent(e)
        idx, p = self.scene_to_page(sp)
        if idx is None:
            return super().mousePressEvent(e)
        self._press = (idx, sp)
        if self.tool == TOOL_SELECT or self.tool in MARKUP_TOOLS:
            self.clear_selection()
        elif self.tool == TOOL_INK:
            self._ink_path = [sp]
            self._rubber = QGraphicsPathItem()
            pen = QPen(qcolor(self.stroke_color), self.stroke_width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            self._rubber.setPen(pen)
            self._rubber.setZValue(30)
            self.scene_.addItem(self._rubber)
        elif self.tool in DRAG_TOOLS:
            pen = QPen(QColor(20, 115, 230) if self.tool != TOOL_REDACT else QColor(200, 0, 0), 0, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            if self.tool in (TOOL_LINE, TOOL_ARROW):
                self._rubber = QGraphicsLineItem()
            elif self.tool == TOOL_ELLIPSE:
                self._rubber = QGraphicsEllipseItem()
            else:
                self._rubber = QGraphicsRectItem()
            self._rubber.setPen(pen)
            self._rubber.setZValue(30)
            self.scene_.addItem(self._rubber)
        e.accept()

    def mouseMoveEvent(self, e):
        sp = self.mapToScene(e.position().toPoint())
        self._last_scene_pos = sp
        if self._press is None:
            if self.tool == TOOL_OBJECT:
                QTimer.singleShot(120, self._hover_objects)      # only once the pointer settles
            return super().mouseMoveEvent(e)
        idx, start = self._press
        page_box = self.pages[idx].sceneBoundingRect()
        sp = QPointF(min(max(sp.x(), page_box.left()), page_box.right()),
                     min(max(sp.y(), page_box.top()), page_box.bottom()))
        if self.tool == TOOL_SELECT or self.tool in MARKUP_TOOLS:
            self._show_text_selection(idx, start, sp)
        elif self.tool == TOOL_INK and self._rubber is not None:
            self._ink_path.append(sp)
            path = QPainterPath(self._ink_path[0])
            for pt in self._ink_path[1:]:
                path.lineTo(pt)
            self._rubber.setPath(path)
        elif self._rubber is not None:
            if isinstance(self._rubber, QGraphicsLineItem):
                self._rubber.setLine(start.x(), start.y(), sp.x(), sp.y())
            else:
                self._rubber.setRect(QRectF(start, sp).normalized())
        e.accept()

    def mouseReleaseEvent(self, e):
        if self._press is None:
            return super().mouseReleaseEvent(e)
        idx, start = self._press
        self._press = None
        sp = self.mapToScene(e.position().toPoint())
        page = self.pdoc.page(idx)
        drag = QRectF(start, sp).normalized()
        tiny = drag.width() < 3 and drag.height() < 3
        rubber, self._rubber = self._rubber, None
        if rubber is not None:
            self.scene_.removeItem(rubber)
        p0 = self.scene_to_page(start)[1]
        p1 = self.scene_to_page(sp)[1] or p0

        if self.tool == TOOL_SELECT:
            if tiny:
                self.clear_selection()
        elif self.tool in MARKUP_TOOLS:
            if not tiny:
                self.markup_selection(self.tool)
        elif self.tool == TOOL_ADD_TEXT:
            self.start_new_text(idx, p0)
        elif self.tool == TOOL_NOTE:
            text, ok = QInputDialog.getMultiLineText(self, "Sticky note", "Comment:")
            if ok and text.strip():
                self._mutate("Add note", idx, lambda: self._add_note(page, p0, text))
        elif self.tool == TOOL_INK and len(self._ink_path) > 1:
            pts = [self.scene_to_page(q)[1] for q in self._ink_path]
            pts = [(q.x, q.y) for q in pts if q is not None]
            self._ink_path = []

            def ink():
                a = page.add_ink_annot([pts])
                a.set_border(width=self.stroke_width)
                a.set_colors(stroke=self.stroke_color)
                a.update()
            self._mutate("Draw", idx, ink)
        elif self.tool in DRAG_TOOLS:
            prect = self.scene_rect_to_page(idx, drag)
            if self.tool == TOOL_OBJECT:
                mods = e.modifiers()
                self.select_objects(idx, None if tiny else prect, p0,
                                    add=bool(mods & Qt.KeyboardModifier.ControlModifier),
                                    exact=bool(mods & Qt.KeyboardModifier.AltModifier))
            elif self.tool == TOOL_FORM_EDIT:
                if self.form_field_type is None:
                    self.select_field(None)
                else:
                    self.create_field_at(idx, None if tiny else prect, p0,
                                         keep_armed=bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier))
            elif self.tool == TOOL_PLACE_IMAGE:
                self.place_pending_image(idx, prect if not tiny else None, p0)
            elif not tiny:
                self._shape(idx, page, prect, p0, p1)
        e.accept()

    def mouseDoubleClickEvent(self, e):
        if self.tool == TOOL_SELECT:
            sp = self.mapToScene(e.position().toPoint())
            idx, p = self.scene_to_page(sp)
            if idx is not None:
                for w in self.pdoc.page(idx).get_text("words"):
                    if pymupdf.Rect(w[:4]).contains(p):
                        self._set_selection(idx, [pymupdf.Rect(w[:4])], w[4])
                        return
        super().mouseDoubleClickEvent(e)

    def keyPressEvent(self, e):
        if self.editor is None and self.tool == TOOL_OBJECT:
            ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
            k = e.key()
            if ctrl and k == Qt.Key.Key_V:
                self.paste_objects()
                return
            if ctrl and k == Qt.Key.Key_A:
                self.select_all_objects()
                return
            if self.object_sel is not None:
                if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                    self.delete_objects()
                    return
                if ctrl and k == Qt.Key.Key_C:
                    self.copy_objects()
                    return
                if ctrl and k == Qt.Key.Key_X:
                    self.cut_objects()
                    return
                if ctrl and k == Qt.Key.Key_D:
                    self.duplicate_objects()
                    return
                if k == Qt.Key.Key_Escape:
                    self.object_sel = None
                    self._show_object_box()
                    return
                if k == Qt.Key.Key_BracketLeft:
                    self.rotate_objects(-90)
                    return
                if k == Qt.Key.Key_BracketRight:
                    self.rotate_objects(90)
                    return
                if k == Qt.Key.Key_M and not ctrl:      # H and V belong to the Hand / Select tools
                    self.flip_objects(not e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                    return
                if k in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
                    step = 10 if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
                    dx = {Qt.Key.Key_Left: -step, Qt.Key.Key_Right: step}.get(k, 0)
                    dy = {Qt.Key.Key_Up: -step, Qt.Key.Key_Down: step}.get(k, 0)
                    b = self.object_sel.bbox
                    self._apply_object_rect(b + (dx, dy, dx, dy), "Move figure")
                    return
        if self.editor is None:
            if e.matches(QKeySequence.StandardKey.Copy) and self._sel_page_rect:
                self.copy_selection()
                return
            if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected_field:
                self.delete_selected_field()
                return
            if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected_annot:
                self.delete_selected_annot()
                return
            if e.key() == Qt.Key.Key_Escape and self.selected_annot:
                self.select_annot(None)
                return
            if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected_image:
                self.delete_selected_image()
                return
            if self.selected_image and e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
                step = 10 if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
                dx = {Qt.Key.Key_Left: -step, Qt.Key.Key_Right: step}.get(e.key(), 0)
                dy = {Qt.Key.Key_Up: -step, Qt.Key.Key_Down: step}.get(e.key(), 0)
                box = self.selected_image
                box.setRect(box.rect().translated(dx, dy))
                self.commit_image_geometry(box)
                return
            if e.key() == Qt.Key.Key_Escape:
                self.clear_selection()
                self.select_image(None)
                if self.tool == TOOL_PLACE_IMAGE:
                    self.set_tool(TOOL_EDIT)
                return
        super().keyPressEvent(e)

    def contextMenuEvent(self, e):
        item = self.itemAt(e.pos())
        if isinstance(item, (TextBlockItem, ImageBoxItem, InlineTextEditor)):
            return super().contextMenuEvent(e)
        if self._sel_page_rect:
            m = QMenu(self)
            m.addAction("Copy", self.copy_selection)
            m.addSeparator()
            m.addAction("Highlight", lambda: self.markup_selection(TOOL_HIGHLIGHT))
            m.addAction("Underline", lambda: self.markup_selection(TOOL_UNDERLINE))
            m.addAction("Strikethrough", lambda: self.markup_selection(TOOL_STRIKE))
            m.addAction("Mark for redaction", self._redact_selection)
            m.exec(e.globalPos())
            return
        super().contextMenuEvent(e)

    # ------------------------------------------------------------------ text selection
    def _show_text_selection(self, idx, a: QPointF, b: QPointF):
        page = self.pdoc.page(idx)
        pa = self.scene_to_page(a)[1]
        pb = self.scene_to_page(b)[1]
        if pa is None or pb is None:
            return
        # reading-order selection between the two points (like Acrobat)
        words = page.get_text("words", sort=True)
        start_i = end_i = None
        top, bottom = (pa, pb) if (pa.y, pa.x) <= (pb.y, pb.x) else (pb, pa)
        sel = []
        for w in words:
            r = pymupdf.Rect(w[:4])
            cy = (r.y0 + r.y1) / 2
            if r.y1 < top.y or r.y0 > bottom.y:
                continue
            same_top = r.y0 <= top.y <= r.y1
            same_bottom = r.y0 <= bottom.y <= r.y1
            if same_top and same_bottom:
                if r.x1 >= min(top.x, bottom.x) and r.x0 <= max(top.x, bottom.x):
                    sel.append(w)
            elif same_top:
                if r.x1 >= top.x:
                    sel.append(w)
            elif same_bottom:
                if r.x0 <= bottom.x:
                    sel.append(w)
            elif top.y < cy < bottom.y:
                sel.append(w)
        rects = [pymupdf.Rect(w[:4]) for w in sel]
        text = " ".join(w[4] for w in sel)
        self._set_selection(idx, rects, text, (top, bottom))

    def _set_selection(self, idx, rects, text, points=None):
        for it in self._sel_items:
            self.scene_.removeItem(it)
        self._sel_items.clear()
        for r in rects:
            it = QGraphicsRectItem(self.page_rect_to_scene(idx, r))
            it.setBrush(QColor(20, 115, 230, 70))
            it.setPen(QPen(Qt.PenStyle.NoPen))
            it.setZValue(25)
            self.scene_.addItem(it)
            self._sel_items.append(it)
        if rects:
            u = pymupdf.Rect(rects[0])
            for r in rects[1:]:
                u |= r
            self._sel_page_rect = (idx, u, text, rects, points)
            self.status.emit(f"{len(text)} characters selected")
        else:
            self._sel_page_rect = None

    def clear_selection(self):
        for it in self._sel_items:
            if it.scene():
                self.scene_.removeItem(it)
        self._sel_items.clear()
        self._sel_page_rect = None

    def copy_selection(self):
        if self._sel_page_rect:
            QGuiApplication.clipboard().setText(self._sel_page_rect[2])
            self.status.emit("Copied to clipboard")

    def selected_text(self):
        return self._sel_page_rect[2] if self._sel_page_rect else ""

    def markup_selection(self, kind):
        if not self._sel_page_rect:
            return
        idx, _, _, rects, _ = self._sel_page_rect
        page = self.pdoc.page(idx)

        def do():
            fn = {TOOL_HIGHLIGHT: page.add_highlight_annot, TOOL_UNDERLINE: page.add_underline_annot,
                  TOOL_STRIKE: page.add_strikeout_annot}[kind]
            a = fn(rects)
            color = self.annot_color if kind == TOOL_HIGHLIGHT else self.stroke_color
            a.set_colors(stroke=color)
            a.update()
        self._mutate(kind.title(), idx, do)

    def _redact_selection(self):
        if not self._sel_page_rect:
            return
        idx, _, _, rects, _ = self._sel_page_rect
        page = self.pdoc.page(idx)

        def do():
            for r in rects:
                page.add_redact_annot(r, fill=(0, 0, 0))
        self._mutate("Mark redaction", idx, do)

    # ------------------------------------------------------------------ annotations
    def _mutate(self, label, idx, fn):
        self.pdoc.begin_change(label)
        try:
            fn()
        except Exception as ex:
            self.pdoc.cancel_change()
            self.status.emit(f"{label} failed: {ex}")
            raise
        self.pdoc.end_change({idx} if idx is not None else None)

    @staticmethod
    def _add_note(page, p, text):
        a = page.add_text_annot(p, text, icon="Comment")
        a.set_colors(stroke=(1, 0.8, 0))
        a.update()

    def _shape(self, idx, page, prect, p0, p1):
        tool = self.tool
        created = []

        def do():
            if tool == TOOL_REDACT:
                page.add_redact_annot(prect, fill=(0, 0, 0))
                return
            if tool == TOOL_RECT:
                a = page.add_rect_annot(prect)
            elif tool == TOOL_ELLIPSE:
                a = page.add_circle_annot(prect)
            else:
                a = page.add_line_annot(p0, p1)
                if tool == TOOL_ARROW:
                    a.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_CLOSED_ARROW)
            a.set_border(width=self.stroke_width)
            a.set_colors(stroke=self.stroke_color)
            a.update()
            created.append(a.xref)

        self._mutate(tool.title(), idx, do)
        if created:
            # like Acrobat: return to Select with the new shape selected, ready to move / resize
            self.set_tool(TOOL_SELECT)
            self._select_annot_xref(idx, created[0])

    # ------------------------------------------------------------------ text editing
    def _find_block_item(self, block: TextBlock) -> "TextBlockItem | None":
        """The text box that holds `block` on the page as it stands now, matched by position and text.
        Overlay items are thrown away and rebuilt whenever the document changes, so a deferred action
        cannot rely on the item it was created with."""
        self._ensure_page_overlays(block.page)
        want = pymupdf.Rect(block.rect)
        best, best_score = None, 0.0
        for it in self.overlays:
            if not isinstance(it, TextBlockItem) or not alive(it) or it.scene() is not self.scene_:
                continue
            other = it.block
            if other.page != block.page:
                continue
            overlap = pymupdf.Rect(other.rect) & want
            if overlap.is_empty or overlap.is_infinite:
                continue
            score = overlap.get_area() / max(want.get_area(), pymupdf.Rect(other.rect).get_area(), 1e-6)
            if other.text.strip() == block.text.strip():
                score += 0.5
            if score > best_score:
                best, best_score = it, score
        return best if best_score >= 0.4 else None

    def start_block_edit(self, block: TextBlock, box: TextBlockItem | None):
        # this arrives deferred (later()), so the world may have moved on in the meantime: the user may
        # have left Edit mode, or the page may have been rebuilt. Opening an editor on a torn-down page
        # leaves it orphaned - still pointing at items that no longer exist - which crashes later.
        if self.tool != TOOL_EDIT or block.page >= len(self.pages):
            return
        if box is not None and (not alive(box) or box.scene() is not self.scene_):
            # the page was rebuilt under us (usually by committing the edit the user just left):
            # the old overlay item is gone, so carry on with the box that now holds this text
            box = self._find_block_item(block)
            if box is None:
                return
            block = box.block
        if self.editor is not None:
            self.finish_edit(commit=True)
            if box is not None and not alive(box):
                return          # committing the previous edit rebuilt the page; the old box is gone
        if box is not None and not alive(box):
            box = None
        if block.mixed_styles:
            self.status.emit("Paragraph has mixed formatting — editing applies the dominant style")
        rect = self.block_scene_rect(block)
        self.edit_block = block
        self.edit_new_at = None
        self.edit_style = block.style.copy()
        self._open_editor(block.page, rect, block.text, self.edit_style, box)

    def start_new_text(self, idx, p: pymupdf.Point):
        self.finish_edit(commit=True)
        style = self.new_text_style.copy()
        page = self.pdoc.page(idx)
        width = max(120.0, min(300.0, page.rect.width - p.x - 20))
        top = pymupdf.Point(p.x, p.y - style.size * 0.8)
        rect = self.page_rect_to_scene(idx, pymupdf.Rect(top.x, top.y, top.x + width, top.y + style.size * 1.3))
        self.edit_block = None
        self.edit_new_at = (idx, top, width)
        self.edit_style = style
        self._open_editor(idx, rect, "", style, None)

    def _open_editor(self, idx, rect: QRectF, text, style, box):
        bg = self._sample_background(idx, rect)
        self.editor_bg = QGraphicsRectItem(rect.adjusted(-1.5, -1.5, 1.5, 1.5))
        self.editor_bg.setBrush(QBrush(bg))
        pen = QPen(QColor(20, 115, 230), 0)
        pen.setCosmetic(True)
        self.editor_bg.setPen(pen)
        self.editor_bg.setZValue(45)
        self.scene_.addItem(self.editor_bg)
        if box is not None and alive(box):
            box.setVisible(False)
        self.editor = InlineTextEditor(self, text, style, rect.width() + 0.5)
        # align first baseline: Qt places ascent at top of line box
        self.editor.setPos(rect.left(), rect.top() - self._leading_offset(style))
        self.scene_.addItem(self.editor)
        self.editor.setFocus()
        cur = self.editor.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.editor.setTextCursor(cur)
        self.setFocus()
        self.editor.setFocus()
        self.editor_changed()
        self.edit_started.emit(style)

    def _leading_offset(self, style: TextStyle) -> float:
        from PyQt6.QtGui import QFontMetricsF
        f = QFont(style.family)
        f.setPixelSize(max(1, round(style.size * 10)))
        f.setBold(style.bold)
        fm = QFontMetricsF(f)
        # PDF glyph boxes start roughly at the font ascender; Qt's line box adds leading above it
        return max(0.0, (style.line_height * style.size - fm.height() / 10) / 2)

    def _sample_background(self, idx, rect: QRectF) -> QColor:
        """Sample the page colour around a text box so the editing backdrop blends in."""
        try:
            page = self.pdoc.page(idx)
            pr = self.scene_rect_to_page(idx, rect).irect
            clip = pymupdf.Rect(pr) + (-4, -4, 4, 4)
            pix = page.get_pixmap(clip=clip & page.rect, dpi=36, alpha=False)
            data = pix.samples
            n = pix.n
            w, h = pix.width, pix.height
            samples = []
            for x in range(w):
                for y in (0, h - 1):
                    o = (y * w + x) * n
                    samples.append(tuple(data[o:o + 3]))
            from collections import Counter
            c = Counter(samples).most_common(1)[0][0]
            return QColor(*c)
        except Exception:
            return QColor(Qt.GlobalColor.white)

    def editor_changed(self):
        if self.editor is None or self.editor_bg is None:
            return
        br = self.editor.mapRectToScene(self.editor.boundingRect())
        r = self.editor_bg.rect()
        self.editor_bg.setRect(QRectF(r.left(), r.top(), max(r.width(), br.width() + 3),
                                      max(br.bottom() - r.top() + 1.5, r.height())))

    def apply_style_to_editor(self, style: TextStyle):
        if self.editor is None:
            return
        old = self.edit_style
        if old and style.family != old.family:
            style.family_changed = True
        self.edit_style = style
        self.editor.apply_style(style)
        self.editor_changed()
        self.editor.setFocus()

    def finish_edit(self, commit=True, defer_commit=False):
        """Close the inline editor. defer_commit=True applies the text on the next event loop turn -
        use it from inside a mouse or key handler, because committing rebuilds the page and destroys
        the very items the handler is still working with."""
        if self.editor is None:
            return
        editor, bg = self.editor, self.editor_bg
        text = editor.toPlainText()
        block, new_at, style = self.edit_block, self.edit_new_at, self.edit_style
        self.editor = self.editor_bg = None
        self.edit_block = self.edit_new_at = None
        for it in (editor, bg):
            if it is not None and alive(it) and it.scene() is self.scene_:
                self.scene_.removeItem(it)
        self.edit_finished.emit()
        for it in self.overlays:
            if alive(it):
                it.setVisible(True)
        if not commit:
            return
        if defer_commit:
            later(lambda: self._apply_edit(block, new_at, style, text))
        else:
            self._apply_edit(block, new_at, style, text)

    def _apply_edit(self, block, new_at, style, text):
        try:
            if block is not None:
                unchanged = (text == block.text and style.__dict__ == block.style.__dict__)
                if unchanged:
                    return
                doc = self.pdoc.doc
                if not text.strip():
                    self._mutate("Delete text", block.page, lambda: textops.delete_block(doc, block))
                else:
                    self._mutate("Edit text", block.page,
                                 lambda: self.status.emit("Font: " + textops.replace_block(doc, block, text, style)))
            elif new_at is not None and text.strip():
                idx, top, width = new_at
                page = self.pdoc.page(idx)
                rect = pymupdf.Rect(top.x, top.y, top.x + width, top.y + style.size)
                baseline = pymupdf.Point(top.x, top.y + style.size * 0.8)
                self.new_text_style = style.copy(family_changed=False, pdf_font="")
                self._mutate("Add text", idx, lambda: textops.write_text(self.pdoc.doc, page, rect, text,
                                                                         style.copy(pdf_font=""), baseline))
        except Exception as ex:
            self.status.emit(f"Could not apply text edit: {ex}")

    def delete_block(self, block: TextBlock):
        self._mutate("Delete text", block.page, lambda: textops.delete_block(self.pdoc.doc, block))

    # ------------------------------------------------------------------ images
    def select_image(self, box: ImageBoxItem | None):
        if self.selected_image is box:
            return
        if self.selected_image is not None and self.selected_image.scene():
            self.selected_image.set_selected(False)
        self.selected_image = box
        if box is not None:
            box.set_selected(True)
            self.status.emit(f"Image {box.img.width}×{box.img.height}px — drag to move, handles to resize "
                             f"(Shift toggles aspect lock), Delete to remove")
        self.image_selected.emit(box.img if box else None)

    def commit_image_geometry(self, box: ImageBoxItem):
        if not alive(box):
            return
        idx = box.img.page
        new_rect = self.scene_rect_to_page(idx, box.rect())
        page = self.pdoc.page(idx)
        img = box.img
        if not img.xref:
            self.status.emit("Inline images cannot be moved")
            box.setRect(box.orig)
            return
        behind = pageimages.is_behind_text(page, img)
        self._mutate("Move image", idx, lambda: pageimages.move_image(self.pdoc.doc, page, img, new_rect, behind))
        self._reselect(idx, new_rect)

    def commit_block_geometry(self, item: "TextBlockItem"):
        """Apply a moved/resized text box to the PDF."""
        if not alive(item):
            return
        block = item.block
        page = self.pdoc.page(block.page)
        r = item.rect().adjusted(1, 1, -1, -1)            # the overlay is drawn 1pt outside the text
        o = self.pages[block.page].pos()
        disp = pymupdf.Rect(r.left() - o.x(), r.top() - o.y(), r.right() - o.x(), r.bottom() - o.y())
        ts = ((disp.quad * page.derotation_matrix) * ~block.matrix).rect
        orig = item.orig.adjusted(1, 1, -1, -1)
        moved_only = abs(r.width() - orig.width()) < 0.05 and abs(r.height() - orig.height()) < 0.05
        if moved_only:          # keep the exact size (no rounding drift)
            ts = pymupdf.Rect(ts.x0, ts.y0, ts.x0 + block.rect.width, ts.y0 + block.rect.height)
        doc = self.pdoc.doc

        def do():
            desc = textops.move_resize_block(doc, block, ts)
            self.status.emit(("Moved text" if moved_only else "Resized text") + (f" • {desc}" if desc else ""))
        self._mutate("Move text" if moved_only else "Resize text", block.page, do)

    def _reselect(self, idx, rect):
        self._ensure_page_overlays(idx)
        for it in self.overlays:
            if isinstance(it, ImageBoxItem) and it.img is not None and it.img.page == idx and \
                    abs(it.img.rect.x0 - rect.x0) < 1 and abs(it.img.rect.y0 - rect.y0) < 1:
                self.select_image(it)
                return

    def delete_selected_image(self):
        box = self.selected_image
        if not box:
            return
        page = self.pdoc.page(box.img.page)
        self.select_image(None)
        self._mutate("Delete image", box.img.page, lambda: pageimages.delete_image(page, box.img))

    def set_selected_image_order(self, behind: bool):
        box = self.selected_image
        if not box:
            return
        page = self.pdoc.page(box.img.page)
        img = box.img
        self._mutate("Arrange image", img.page,
                     lambda: pageimages.move_image(self.pdoc.doc, page, img, img.rect, behind))
        self._reselect(img.page, img.rect)

    def image_context_menu(self, box: ImageBoxItem, pos):
        img = box.img
        m = QMenu(self)
        m.addAction("Image settings…", lambda: later(lambda: self.image_settings_requested.emit(img)))
        m.addAction("Bring in front of text", lambda: later(lambda: self.set_selected_image_order(False)))
        m.addAction("Send behind text", lambda: later(lambda: self.set_selected_image_order(True)))
        m.addSeparator()
        m.addAction("Copy image", lambda: self._copy_image(img))
        m.addAction("Delete", lambda: later(self.delete_selected_image))
        m.exec(pos)

    def _copy_image(self, img):
        pil = pageimages.extract_pil(self.pdoc.doc, img.xref)
        qimg = QImage(pil.tobytes("raw", "RGBA"), pil.width, pil.height, QImage.Format.Format_RGBA8888).copy()
        QGuiApplication.clipboard().setImage(qimg)

    def begin_place_image(self, pil, settings):
        self.pending_image = (pil, settings)
        self.set_tool(TOOL_PLACE_IMAGE)
        self.status.emit("Click to place the image, or drag to draw its size. Esc to cancel.")

    def place_pending_image(self, idx, prect: pymupdf.Rect | None, p0: pymupdf.Point):
        if not self.pending_image:
            return
        pil, settings = self.pending_image
        from .imagetools import apply_settings
        out = apply_settings(pil, settings)
        page = self.pdoc.page(idx)
        ar = out.width / max(1, out.height)
        if prect is None or prect.width < 4:
            w = min(out.width * 72 / 96, page.rect.width * 0.5)
            h = w / ar
            prect = pymupdf.Rect(p0.x - w / 2, p0.y - h / 2, p0.x + w / 2, p0.y + h / 2)
        elif settings.keep_aspect:
            prect = pymupdf.Rect(prect.x0, prect.y0, prect.x0 + prect.width, prect.y0 + prect.width / ar)
        self.pending_image = None
        self._mutate("Insert image", idx, lambda: pageimages.insert_pil(page, prect, out, settings.behind_text))
        self.set_tool(TOOL_EDIT)
        self._reselect(idx, prect)
        self.image_placed.emit()

    # ------------------------------------------------------------------ search
    def show_search_results(self, hits, current=-1):
        for it in self.search_items:
            if it.scene():
                self.scene_.removeItem(it)
        self.search_items.clear()
        for i, (idx, r) in enumerate(hits):
            it = QGraphicsRectItem(self.page_rect_to_scene(idx, r))
            it.setBrush(QColor(255, 150, 0, 140) if i == current else QColor(255, 220, 0, 90))
            it.setPen(QPen(Qt.PenStyle.NoPen))
            it.setZValue(26)
            self.scene_.addItem(it)
            self.search_items.append(it)
        if 0 <= current < len(hits):
            idx, r = hits[current]
            self.centerOn(self.page_rect_to_scene(idx, r).center())

    # ------------------------------------------------------------------ forms: tool items
    def _build_tool_items(self):
        if not self.pdoc.is_open:
            return
        sel_annot = (self.selected_annot.info.page, self.selected_annot.info.xref) \
            if alive(self.selected_annot) else None
        if self.tool == TOOL_EDIT:
            self.clear_form_items()
            self.build_overlays(sel_annot)          # text, image and shape boxes, page by page
        elif self.tool == TOOL_OBJECT:
            self.clear_form_items()
            self.clear_overlays()
            if not self._object_commit:
                self.object_sel = None          # the page changed underneath (undo, other edits)
                self.object_fids = set()
            self._show_object_box()
            QTimer.singleShot(0, self._hover_objects)    # the rebuild dropped the hover outline
        elif self.tool == TOOL_FORM_EDIT:
            self.clear_form_items()
            self.build_field_boxes()
        else:
            self.clear_overlays()
            self.build_form_items()
            if self.tool in (TOOL_SELECT, TOOL_HAND):
                self.build_annot_boxes(sel_annot)

    # ------------------------------------------------------------------ shapes & pen drawings
    def build_annot_boxes(self, select: tuple | None = None):
        for item in self.pages:
            page = self.pdoc.page(item.index)
            for info in annots.list_annots(page):
                box = AnnotBoxItem(self, info, self.page_rect_to_scene(item.index, info.rect))
                self.scene_.addItem(box)
                self.overlays.append(box)
                if select == (info.page, info.xref):
                    self.select_annot(box)

    def select_annot(self, box: "AnnotBoxItem | None"):
        if box is not None:
            if self.selected_image is not None:
                self.select_image(None)
            self.clear_selection()
        if self.selected_annot is box:
            return
        if alive(self.selected_annot):
            self.selected_annot.selected = False
            self.selected_annot._apply_pen()
            self.selected_annot.update()
        self.selected_annot = box
        if box is not None:
            box.selected = True
            box._apply_pen()
            box.prepareGeometryChange()
            box.update()
            self.setFocus()
            hint = "drag to move, handles to resize (Shift keeps proportions), " if box.info.resizable else "drag to move, "
            self.status.emit(f"{box.info.type_name} selected — {hint}Delete to remove")

    def _select_annot_xref(self, page: int, xref: int):
        self._ensure_page_overlays(page)
        for it in self.overlays:
            if isinstance(it, AnnotBoxItem) and it.info.page == page and it.info.xref == xref:
                self.select_annot(it)
                return

    def commit_annot_geometry(self, box: "AnnotBoxItem"):
        if not alive(box):
            return
        info = box.info
        new_rect = self.scene_rect_to_page(info.page, box.rect())
        result = []
        self._mutate(f"Move {info.type_name.lower()}", info.page,
                     lambda: result.append(annots.transform_annot(self.pdoc.doc, info.page, info.xref, new_rect)))
        if result:
            self._select_annot_xref(info.page, result[0])

    def delete_selected_annot(self):
        box = self.selected_annot
        if not alive(box):
            self.selected_annot = None
            return
        info = box.info
        self.select_annot(None)
        self._mutate(f"Delete {info.type_name.lower()}", info.page,
                     lambda: annots.delete_annot(self.pdoc.doc, info.page, info.xref))

    # ------------------------------------------------------------------ figures (vector objects)
    def _show_object_box(self):
        if alive(self.object_box):
            self.scene_.removeItem(self.object_box)
            if self.object_box in self.overlays:
                self.overlays.remove(self.object_box)
        self.object_box = None
        self._clear_object_hover()
        sel = self.object_sel
        if sel is None or sel.empty or self.tool != TOOL_OBJECT or sel.page >= len(self.pages):
            return
        rect = self.page_rect_to_scene(sel.page, sel.bbox).adjusted(-2, -2, 2, 2)
        self.object_box = ObjectBoxItem(self, sel, rect)
        self.scene_.addItem(self.object_box)
        self.overlays.append(self.object_box)

    def _object_tol(self) -> float:
        """Click tolerance in page points: a few screen pixels, whatever the zoom."""
        return max(2.0, 6.0 / max(0.05, self.transform().m11()))

    def _clear_object_hover(self):
        if self.object_hover is not None and alive(self.object_hover):
            if self.object_hover.scene():
                self.scene_.removeItem(self.object_hover)
            if self.object_hover in self.overlays:
                self.overlays.remove(self.object_hover)
        self.object_hover = None
        self._hover_key = None

    def _hover_objects(self):
        """Outline the figure under the pointer, so the user sees what a click would pick up."""
        if self.tool != TOOL_OBJECT or self._press is not None or self._last_scene_pos is None:
            return self._clear_object_hover()
        idx, p = self.scene_to_page(self._last_scene_pos)
        fid = r = None
        if idx is not None and p is not None:
            try:
                fid = objects.figure_at(self.pdoc.doc, idx, p, self._object_tol())
                r = objects.figure_rect(self.pdoc.doc, idx, fid) if fid is not None else None
            except Exception:
                fid = r = None
        if r is None:
            return self._clear_object_hover()
        if self._hover_key == (idx, fid):
            return
        self._clear_object_hover()
        self._hover_key = (idx, fid)
        item = QGraphicsRectItem(self.page_rect_to_scene(idx, r).adjusted(-2, -2, 2, 2))
        pen = QPen(QColor(20, 115, 230, 170), 0, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        item.setPen(pen)
        item.setBrush(QBrush(QColor(20, 115, 230, 12)))
        item.setZValue(19)
        item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.scene_.addItem(item)
        self.overlays.append(item)
        self.object_hover = item

    def select_objects(self, idx: int, prect: pymupdf.Rect | None, point: pymupdf.Point,
                       add: bool = False, exact: bool = False):
        """Click: the whole figure under the cursor, detected from the drawing itself (Ctrl adds or
        removes one). Drag: every figure lying mostly inside the box, each taken complete - so no line
        is ever cut in half. Hold Alt while dragging to take exactly the lines inside the box instead."""
        doc = self.pdoc.doc
        keep = set(self.object_fids) if add and self.object_sel is not None and self.object_sel.page == idx else set()
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            if exact and prect is not None:
                sel, fids = objects.select_rect(doc, idx, prect, whole=False), set()
            elif prect is None:
                fid = objects.figure_at(doc, idx, point, self._object_tol())
                fids = set(keep)
                if fid is not None:
                    fids.symmetric_difference_update({fid})      # Ctrl+click a selected figure drops it
                sel = objects.select_figures(doc, idx, fids) if fids else None
            else:
                marquee = objects.select_rect(doc, idx, prect)
                fids = keep | set(marquee.figures)
                sel = objects.select_figures(doc, idx, fids) if fids else marquee
        except Exception as ex:
            self.status.emit(f"Could not select: {ex}")
            sel, fids = None, set()
        finally:
            QApplication.restoreOverrideCursor()
        self.object_sel = sel if sel is not None and not sel.empty else None
        self.object_fids = fids if self.object_sel is not None else set()
        self._show_object_box()
        if self.object_sel:
            n = len(self.object_fids)
            self.status.emit(f"Selected {self.object_sel.describe()}" + (f" in {n} objects" if n > 1 else "") +
                             " — drag to move, handles to resize, [ ] to rotate, H / V to flip, "
                             "Ctrl+C to copy, Delete to remove")
        else:
            self.status.emit("Nothing there — click a line of the drawing, or drag a box around what you want")
        self.setFocus()

    def select_all_objects(self):
        idx = self.object_sel.page if self.object_sel else self.current_page()
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            fids = objects.all_figures(self.pdoc.doc, idx)
            sel = objects.select_figures(self.pdoc.doc, idx, fids)
        except Exception as ex:
            self.status.emit(f"Could not select: {ex}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.object_sel = sel if not sel.empty else None
        self.object_fids = set(fids) if self.object_sel else set()
        self._show_object_box()
        self.status.emit(f"Selected every drawing on this page: {sel.describe()}" if self.object_sel
                         else "This page has no drawings to select")

    def _object_edit(self, label: str, fn):
        sel = self.object_sel
        if sel is None:
            return
        result = []
        self._object_commit = True
        try:
            self._mutate(label, sel.page, lambda: result.append(fn()))
        finally:
            self._object_commit = False
        if result:
            self.object_sel = result[0]
        self.object_fids = set()          # the page changed: detected figure ids are stale
        self._show_object_box()

    def _apply_object_rect(self, new_rect: pymupdf.Rect, label: str):
        sel = self.object_sel
        if sel is None:
            return
        self._object_edit(label, lambda: objects.transform(self.pdoc.doc, sel, new_rect))

    def rotate_objects(self, degrees: int = 90):
        sel = self.object_sel
        if sel is None:
            return
        self._object_edit("Rotate figure", lambda: objects.rotate(self.pdoc.doc, sel, degrees))
        self.status.emit(f"Rotated {abs(degrees)}° {'right' if degrees > 0 else 'left'}")

    def flip_objects(self, horizontal: bool = True):
        sel = self.object_sel
        if sel is None:
            return
        self._object_edit("Flip figure", lambda: objects.flip(self.pdoc.doc, sel, horizontal))
        self.status.emit(f"Flipped {'horizontally' if horizontal else 'vertically'} (labels stay readable)")

    def save_objects_image(self):
        sel = self.object_sel
        if sel is None:
            return
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(self, "Save figure as image", "figure.png",
                                              "PNG image (*.png)")
        if not path:
            return
        try:
            with open(path, "wb") as fh:
                fh.write(objects.render_png(self.pdoc.doc, sel, dpi=300))
        except Exception as ex:
            self.status.emit(f"Could not save the image: {ex}")
            return
        self.status.emit(f"Saved {path}")

    def commit_object_geometry(self, box: ObjectBoxItem):
        if not alive(box) or self.object_sel is None:
            return
        sel = self.object_sel
        target = self.scene_rect_to_page(sel.page, box.rect().adjusted(2, 2, -2, -2))
        orig = self.scene_rect_to_page(sel.page, box.orig.adjusted(2, 2, -2, -2))
        moved_only = abs(target.width - orig.width) < 0.05 and abs(target.height - orig.height) < 0.05
        b = sel.bbox
        if moved_only:                       # exact translation, no rounding of the size
            dx, dy = target.x0 - orig.x0, target.y0 - orig.y0
            target = b + (dx, dy, dx, dy)
        self._apply_object_rect(target, "Move figure" if moved_only else "Resize figure")

    def copy_objects(self):
        if self.object_sel is None:
            return
        objects.copy_selection(self.object_sel)
        try:
            png = objects.render_png(self.pdoc.doc, self.object_sel)
            img = QImage.fromData(png, "PNG")
            QGuiApplication.clipboard().setImage(img)      # also paste-able into other applications
        except Exception:
            pass
        self.status.emit(f"Copied {self.object_sel.describe()}")

    def delete_objects(self):
        sel = self.object_sel
        if sel is None:
            return
        self.object_sel = None
        self.object_fids = set()
        self._mutate("Delete figure", sel.page, lambda: objects.delete(self.pdoc.doc, sel))
        self._show_object_box()

    def cut_objects(self):
        self.copy_objects()
        self.delete_objects()

    def paste_objects(self, top_left: pymupdf.Point | None = None, page: int | None = None):
        if not objects.has_clipboard():
            self.status.emit("Nothing to paste — select a figure and press Ctrl+C first")
            return
        if page is None or top_left is None:
            idx, p = (self.scene_to_page(self._last_scene_pos) if self._last_scene_pos is not None else (None, None))
            if idx is not None:
                page, top_left = idx, p
            else:
                page = self.current_page()
                box = objects.CLIPBOARD["bbox"]
                top_left = pymupdf.Point(box.x0 + 20, box.y0 + 20)
        result = []
        self._object_commit = True
        try:
            self._mutate("Paste figure", page, lambda: result.append(objects.paste(self.pdoc.doc, page, top_left)))
        finally:
            self._object_commit = False
        if result and result[0] is not None:
            self.object_sel = result[0]
            self.object_fids = set()
            if self.tool != TOOL_OBJECT:
                self.set_tool(TOOL_OBJECT)
                self.object_sel = result[0]
            self._show_object_box()
            self.status.emit(f"Pasted {result[0].describe()}")

    def duplicate_objects(self):
        sel = self.object_sel
        if sel is None:
            return
        objects.copy_selection(sel)
        b = sel.bbox
        self.paste_objects(pymupdf.Point(b.x0 + 20, b.y0 + 20), sel.page)

    def clear_form_items(self):
        for it in self.form_items:
            if it.scene():
                self.scene_.removeItem(it)
            it.deleteLater()
        self.form_items.clear()

    def build_form_items(self):
        """Live Qt widgets over every form field, so forms can be filled directly on the page."""
        self.clear_form_items()
        prev = None
        for item in self.pages:
            page = self.pdoc.page(item.index)
            fields = sorted(forms.list_fields(page), key=lambda f: (round(f.rect.y0 / 4), f.rect.x0))
            for f in fields:
                proxy = self._make_field_proxy(f)
                if proxy is None:
                    continue
                self.form_items.append(proxy)
                if prev is not None:
                    QGraphicsWidget.setTabOrder(prev, proxy)
                prev = proxy

    def _make_field_proxy(self, f: forms.FieldInfo):
        r = self.page_rect_to_scene(f.page, f.rect)
        if r.width() < 2 or r.height() < 2:
            return None
        h = r.height()
        bg = "#dde5ff"      # opaque: hides the field's own printed appearance underneath
        border = "1px solid #d9534f" if f.required else "none"
        base_css = f"background:{bg}; border:{border}; padding:0px; margin:0px; color:black; border-radius:0px;"
        size_px = f.fontsize or max(6.0, min(12.0, h * (0.62 if not f.multiline else 0.3)))
        font = QFont("Arial")
        font.setPixelSize(max(4, round(size_px)))

        def commit(v):
            self.commit_field_value(proxy, v)

        if f.type == forms.TEXT:
            if f.multiline:
                wdg = _PlainTextField(str(f.value or ""), commit)
                wdg.setStyleSheet(f"QPlainTextEdit{{{base_css}}}")
            else:
                wdg = QLineEdit(str(f.value or ""))
                if f.maxlen:
                    wdg.setMaxLength(f.maxlen)
                if f.flags & forms.FLAG_PASSWORD:
                    wdg.setEchoMode(QLineEdit.EchoMode.Password)
                wdg.setStyleSheet(f"QLineEdit{{{base_css}}}")
                wdg.editingFinished.connect(lambda: commit(wdg.text()))
            wdg.setReadOnly(f.readonly)
        elif f.type in (forms.CHECKBOX, forms.RADIO):
            wdg = QCheckBox()
            wdg.setChecked(f.checked)
            ind = max(4, int(min(h, r.width()) * 0.8))
            radius = ind // 2 if f.type == forms.RADIO else 2
            wdg.setStyleSheet(
                f"QCheckBox{{{base_css}}} QCheckBox::indicator{{width:{ind}px; height:{ind}px; "
                f"margin-left:{max(0, int((r.width() - ind) / 2))}px; border:1px solid #555; border-radius:{radius}px; background:white;}}"
                f"QCheckBox::indicator:checked{{background:#1473e6; border:1px solid #0d5bb8;}}")
            wdg.toggled.connect(commit)
            wdg.setEnabled(not f.readonly)
        elif f.type == forms.COMBO:
            wdg = QComboBox()
            wdg.addItems([str(c) for c in f.choices])
            wdg.setCurrentText(str(f.value or ""))
            wdg.setStyleSheet(f"QComboBox{{{base_css} min-height:0px; padding-left:2px;}}")
            wdg.textActivated.connect(commit)
            wdg.setEnabled(not f.readonly)
        elif f.type == forms.LISTBOX:
            wdg = QListWidget()
            wdg.addItems([str(c) for c in f.choices])
            wdg.setStyleSheet(f"QListWidget{{{base_css}}}")
            wdg.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            for i in range(wdg.count()):
                if wdg.item(i).text() == str(f.value):
                    wdg.item(i).setSelected(True)
            wdg.itemClicked.connect(lambda it: commit(it.text()))
            wdg.setEnabled(not f.readonly)
        elif f.type == forms.SIGNATURE:
            wdg = QPushButton("✍ Click to sign")
            wdg.setStyleSheet(f"QPushButton{{{base_css} color:#0d47a1; text-align:left; padding-left:3px;}}")
            wdg.clicked.connect(lambda: self.sign_field_requested.emit(f))
        else:
            return None
        wdg.setFont(font)
        wdg.setToolTip(f.tooltip or f.name)
        wdg.setMinimumSize(0, 0)
        wdg.setContentsMargins(0, 0, 0, 0)
        wdg.setFixedSize(max(1, round(r.width())), max(1, round(r.height())))   # never larger than the field
        if isinstance(wdg, (QPlainTextEdit, QListWidget)):
            wdg.setFrameShape(QPlainTextEdit.Shape.NoFrame)
            wdg.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            wdg.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        proxy = QGraphicsProxyWidget()
        proxy.setWidget(wdg)
        proxy.setGeometry(r)
        proxy.setZValue(15)
        proxy.field = f
        self.scene_.addItem(proxy)
        return proxy

    def commit_field_value(self, proxy, value):
        f: forms.FieldInfo = proxy.field
        current = f.checked if f.type in (forms.CHECKBOX, forms.RADIO) else f.value
        if (f.type in (forms.CHECKBOX, forms.RADIO) and bool(value) == bool(current)) or \
                (f.type not in (forms.CHECKBOX, forms.RADIO) and str(value) == str(current or "")):
            return

        def do():
            self._form_commit = True
            try:
                self._mutate("Fill form", f.page, lambda: forms.set_value(self.pdoc.doc, f.page, f.xref, value))
            finally:
                self._form_commit = False
            f.value = (f.on_state if value else "Off") if f.type in (forms.CHECKBOX, forms.RADIO) else value
            if f.type == forms.RADIO and value:
                for other in self.form_items:
                    of = other.field
                    if of.type == forms.RADIO and of.name == f.name and of.xref != f.xref:
                        of.value = "Off"
                        w = other.widget()
                        w.blockSignals(True)
                        w.setChecked(False)
                        w.blockSignals(False)
        QTimer.singleShot(0, do)

    # ------------------------------------------------------------------ forms: prepare
    def build_field_boxes(self):
        sel = self.selected_field.field.xref if self.selected_field else None
        self.clear_overlays()
        for item in self.pages:
            for f in forms.list_fields(self.pdoc.page(item.index)):
                box = FieldBoxItem(self, f, self.page_rect_to_scene(item.index, f.rect))
                self.scene_.addItem(box)
                self.overlays.append(box)
                if f.xref == sel:
                    self.select_field(box)

    def select_field(self, box: FieldBoxItem | None):
        if box is not None and self.selected_image is not None:
            self.select_image(None)
        if self.selected_field is box:
            if box is not None:
                self.field_selected.emit(box.field)
            return
        if self.selected_field is not None and self.selected_field.scene():
            self.selected_field.set_selected(False)
        self.selected_field = box
        if box is not None:
            box.set_selected(True)
            self.status.emit(f"{box.field.type_name} “{box.field.name}” — drag to move, handles to resize, Delete to remove")
        self.field_selected.emit(box.field if box else None)

    def _select_field_xref(self, page: int, xref: int):
        for it in self.overlays:
            if isinstance(it, FieldBoxItem) and it.field.xref == xref and it.field.page == page:
                self.select_field(it)
                return

    def commit_field_geometry(self, box: FieldBoxItem):
        if not alive(box):
            return
        f = box.field
        new = self.scene_rect_to_page(f.page, box.rect())
        self._mutate("Move field", f.page, lambda: forms.update_field(self.pdoc.doc, f.page, f.xref, rect=new))
        self._select_field_xref(f.page, f.xref)

    def create_field_at(self, idx, prect, p0, keep_armed=False):
        ftype = self.form_field_type
        if prect is None or prect.width < 4 or prect.height < 4:
            w, h = FIELD_DEFAULT_SIZE.get(ftype, (150, 18))
            prect = pymupdf.Rect(p0.x, p0.y - h / 2, p0.x + w, p0.y + h / 2)
        created = []
        self._mutate("Add field", idx, lambda: created.append(forms.create_field(self.pdoc.doc, idx, ftype, prect)))
        if not keep_armed:
            self.form_field_type = None
        if created:
            self._select_field_xref(idx, created[0])

    def delete_selected_field(self):
        box = self.selected_field
        if not box:
            return
        f = box.field
        self.select_field(None)
        self._mutate("Delete field", f.page, lambda: forms.delete_field(self.pdoc.doc, f.page, f.xref))

    def duplicate_selected_field(self):
        box = self.selected_field
        if not box:
            return
        f = box.field
        r = pymupdf.Rect(f.rect) + (0, f.rect.height + 6, 0, f.rect.height + 6)
        created = []
        name = f.name if f.type == forms.RADIO else f.name
        self._mutate("Duplicate field", f.page, lambda: created.append(
            forms.create_field(self.pdoc.doc, f.page, f.type, r, name=name, choices=f.choices or None,
                               tooltip=f.tooltip, fontsize=f.fontsize)))
        if created:
            self._select_field_xref(f.page, created[0])

    def update_selected_field(self, **props):
        box = self.selected_field
        if not box:
            return
        f = box.field
        self._mutate("Field properties", f.page, lambda: forms.update_field(self.pdoc.doc, f.page, f.xref, **props))
        self._select_field_xref(f.page, f.xref)
