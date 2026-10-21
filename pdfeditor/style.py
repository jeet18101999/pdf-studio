"""Look & feel: Acrobat-like light theme and glyph icons from the Windows icon fonts."""
from __future__ import annotations

from functools import lru_cache

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap

ACCENT = "#1473e6"

ICONS = {
    "open": "", "save": "", "saveas": "", "print": "", "undo": "",
    "redo": "", "zoomin": "", "zoomout": "", "fitwidth": "", "fitpage": "",
    "select": "", "hand": "", "edit": "", "addtext": "", "image": "",
    "highlight": "", "underline": "", "strike": "", "note": "", "ink": "",
    "rect": "", "ellipse": "", "line": "", "arrow": "", "redact": "",
    "organize": "", "merge": "", "ocr": "", "protect": "", "compress": "",
    "sign": "", "search": "", "delete": "", "rotl": "", "rotr": "",
    "bold": "", "italic": "", "alignl": "", "alignc": "", "alignr": "",
    "justify": "", "front": "", "back": "", "replace": "", "crop": "",
    "export": "", "split": "", "insert": "", "prev": "", "next": "",
    "close": "", "settings": "", "watermark": "", "pages": "", "copy": "",
    "flip": "", "check": "", "apply": "", "info": "",
}


@lru_cache(maxsize=1)
def _icon_family() -> str | None:
    fams = set(QFontDatabase.families())
    for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if f in fams:
            return f
    return None


@lru_cache(maxsize=256)
def icon(name: str, color: str = "#2c2c2c", size: int = 32, mirror=False) -> QIcon:
    fam = _icon_family()
    glyph = ICONS.get(name, "")
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.setPen(QColor(color))
    if mirror:
        p.translate(size, 0)
        p.scale(-1, 1)
    if fam and glyph:
        f = QFont(fam)
        f.setPixelSize(int(size * 0.62))
        p.setFont(f)
        p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, glyph)
    else:
        f = QFont("Segoe UI")
        f.setPixelSize(int(size * 0.45))
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, name[:2].title())
    p.end()
    ic = QIcon(pm)
    # white variant for checked/active state
    return ic


STYLESHEET = f"""
* {{ font-family: 'Segoe UI', 'Helvetica Neue', Arial; font-size: 9.5pt; }}
QMainWindow, QDialog {{ background: #f4f4f4; }}
QMenuBar {{ background: #ffffff; border-bottom: 1px solid #e1e1e1; padding: 2px; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 4px; }}
QMenuBar::item:selected {{ background: #ececec; }}
QMenu {{ background: #ffffff; border: 1px solid #d6d6d6; padding: 4px; }}
QMenu::item {{ padding: 6px 28px 6px 24px; border-radius: 4px; }}
QMenu::item:selected {{ background: #e8f1fd; color: #000; }}
QMenu::separator {{ height: 1px; background: #e6e6e6; margin: 4px 8px; }}
QToolBar {{ background: #ffffff; border: none; border-bottom: 1px solid #e1e1e1; spacing: 2px; padding: 3px 6px; }}
QToolBar#toolRail {{ border-bottom: none; border-right: 1px solid #e1e1e1; padding: 6px 4px; spacing: 4px; }}
QToolButton {{ border: 1px solid transparent; border-radius: 6px; padding: 4px; color: #2c2c2c; }}
QToolButton:hover {{ background: #efefef; }}
QToolButton:checked {{ background: #e3edfb; border: 1px solid #b5d0f5; color: {ACCENT}; }}
QToolButton:pressed {{ background: #dcdcdc; }}
QToolBar#toolRail QToolButton {{ min-width: 64px; font-size: 8pt; }}
QPushButton {{ background: #ffffff; border: 1px solid #c8c8c8; border-radius: 14px; padding: 5px 16px; }}
QPushButton:hover {{ background: #f2f2f2; }}
QPushButton:default, QPushButton#primary {{ background: {ACCENT}; color: white; border: 1px solid {ACCENT}; }}
QPushButton#primary:hover {{ background: #0d66d0; }}
QPushButton:disabled {{ color: #aaa; border-color: #e0e0e0; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QFontComboBox, QPlainTextEdit, QTextEdit {{
    background: #ffffff; border: 1px solid #c8c8c8; border-radius: 4px; padding: 3px 6px; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
QDockWidget {{ titlebar-close-icon: none; }}
QDockWidget::title {{ background: #ffffff; padding: 6px; border-bottom: 1px solid #e1e1e1; font-weight: 600; }}
QListWidget {{ background: #fafafa; border: none; outline: none; }}
QListWidget::item {{ border-radius: 4px; padding: 4px; color: #333; }}
QListWidget::item:selected {{ background: #dbe8fb; border: 2px solid {ACCENT}; color: #000; }}
QGraphicsView {{ background: #e9e9e9; border: none; }}
QStatusBar {{ background: #ffffff; border-top: 1px solid #e1e1e1; }}
QGroupBox {{ border: 1px solid #dedede; border-radius: 6px; margin-top: 14px; padding: 8px; background: #fff; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; font-weight: 600; color: #444; }}
QLabel#panelTitle {{ font-size: 11pt; font-weight: 600; padding: 4px 0; }}
QLabel#hint {{ color: #6e6e6e; }}
QSlider::groove:horizontal {{ height: 4px; background: #d0d0d0; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {ACCENT}; width: 14px; margin: -6px 0; border-radius: 7px; }}
QTabWidget::pane {{ border: 1px solid #dedede; background: #fff; }}
QTabBar::tab {{ padding: 6px 14px; }}
QTabBar::tab:selected {{ color: {ACCENT}; border-bottom: 2px solid {ACCENT}; }}
QProgressBar {{ border: 1px solid #ccc; border-radius: 4px; text-align: center; background: #fff; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
"""
