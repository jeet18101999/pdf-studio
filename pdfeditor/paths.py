"""Locations of bundled resources, both when running from source and from the installed build."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def app_root() -> Path:
    """Folder containing bundled resources (PyInstaller sets sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent.parent


def resource(*parts) -> Path:
    return app_root().joinpath(*parts)


def bundled_tesseract() -> Path | None:
    """Tesseract shipped with the installer: <app>/tesseract/tesseract.exe."""
    candidates = [resource("tesseract", "tesseract.exe"),
                  Path(sys.executable).resolve().parent / "tesseract" / "tesseract.exe"]
    for c in candidates:
        if c.is_file():
            return c
    return None


def user_data_dir() -> Path:
    d = Path(os.environ.get("APPDATA", Path.home())) / "PDFStudio"
    d.mkdir(parents=True, exist_ok=True)
    return d
