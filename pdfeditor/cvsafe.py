"""OpenCV, made optional.

PDF Studio uses OpenCV only for image work: cleaning up scans, finding table rules, tracing drawings and
removing photo backgrounds. If it cannot load on a particular PC - a missing Windows component, a damaged
install - the program must still start and everything else must keep working, so the import is wrapped here
and the problem is reported when such a feature is actually used.

Set PDFSTUDIO_NO_OPENCV=1 to pretend it is missing (used by the tests).
"""
from __future__ import annotations

import os

MESSAGE = ("The image library (OpenCV) could not be loaded on this PC, so scanned-page editing, text "
           "recognition (OCR), table export and drawing tracing are unavailable. Everything else works.\n\n"
           "To fix it: install the Microsoft Visual C++ Redistributable (x64) from microsoft.com, then start "
           "PDF Studio again. On a Windows N or KN edition, also install the Media Feature Pack from Windows "
           "Update.\n\nFile ▸ Diagnose Problems… writes a report that shows exactly what is missing.")


class MissingOpenCV(RuntimeError):
    """Raised when a feature that needs OpenCV is used on a PC where OpenCV did not load."""


class _Absent:
    """Stands in for the cv2 module: importing keeps working, using it explains what is missing."""

    def __getattr__(self, name):
        raise MissingOpenCV(MESSAGE)


if os.environ.get("PDFSTUDIO_NO_OPENCV") == "1":
    cv2, AVAILABLE, ERROR = _Absent(), False, "disabled for testing (PDFSTUDIO_NO_OPENCV=1)"
else:
    try:
        import cv2                                  # noqa: F401
        AVAILABLE, ERROR = True, ""
    except Exception as ex:                         # missing system DLL, broken install, unsupported CPU
        cv2, AVAILABLE, ERROR = _Absent(), False, f"{type(ex).__name__}: {ex}"


def require():
    """Raise MissingOpenCV (with the reason) unless OpenCV is available."""
    if not AVAILABLE:
        raise MissingOpenCV(MESSAGE + (f"\n\nDetails: {ERROR}" if ERROR else ""))
