"""Application bootstrap."""
from __future__ import annotations

import os
import sys
import threading
import traceback

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox

from . import APP_NAME
from .fonts import FONT_INDEX
from .style import STYLESHEET, icon


_CRASH_FILE = None


def _enable_crash_log():
    """Native crashes (access violations inside Qt/MuPDF/OpenCV) close the app without any dialog;
    faulthandler writes their Python stack to crash.log so they can be diagnosed."""
    global _CRASH_FILE
    try:
        import faulthandler
        from .paths import user_data_dir
        _CRASH_FILE = open(user_data_dir() / "crash.log", "a", encoding="utf-8")
        import datetime
        _CRASH_FILE.write(f"\n===== session {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        _CRASH_FILE.flush()
        faulthandler.enable(_CRASH_FILE, all_threads=True)
    except Exception:
        pass


def _excepthook(exc_type, exc, tb):
    msg = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        if sys.stderr:
            sys.stderr.write(msg)
    except Exception:
        pass
    if _CRASH_FILE:
        try:
            _CRASH_FILE.write(msg)
            _CRASH_FILE.flush()
        except Exception:
            pass
    app = QApplication.instance()
    if app is not None:
        QMessageBox.critical(None, APP_NAME, f"Unexpected error:\n\n{exc}\n\nDetails were written to the console.")


def selftest() -> int:
    """Headless check used by the installer build: every subsystem imports and works."""
    from .paths import user_data_dir
    log = user_data_dir() / "selftest.log"
    lines = []
    try:
        import pymupdf
        from . import forms, ocr, scanedit, signing, textops  # noqa: F401
        from . import cad, imagepdf, officeconvert, repair, security, splitpdf  # noqa: F401
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Self test")
        assert "Self test" in page.get_text()
        lines.append("pymupdf ok")
        FONT_INDEX.ensure()
        lines.append(f"fonts ok ({len(FONT_INDEX.entries)})")
        lines.append(f"tesseract: {ocr.find_tesseract()}")
        lines.append(f"signing available: {signing.AVAILABLE}")
        import numpy  # noqa: F401
        from . import cvsafe
        lines.append(f"opencv: {'ok' if cvsafe.AVAILABLE else 'NOT AVAILABLE - ' + cvsafe.ERROR}")
        import ezdxf  # noqa: F401
        lines.append("ezdxf ok")
        import pikepdf
        with pikepdf.new() as pdf:
            pdf.add_blank_page()
        lines.append("pikepdf ok")
        rep = security.scan(doc)
        assert not rep.findings
        lines.append(f"office automation: word={officeconvert.office_installed('word')}")
        log.write_text("\n".join(lines) + "\nPASS\n", "utf-8")
        return 0
    except Exception:
        log.write_text("\n".join(lines) + "\n" + traceback.format_exc(), "utf-8")
        return 1


def diagnose() -> int:
    """Write a report about this PC and what PDF Studio can load, for sorting out install problems."""
    import ctypes
    import platform
    import time
    from .paths import bundled_tesseract, user_data_dir
    lines = [APP_NAME + " diagnostic report", time.strftime("%Y-%m-%d %H:%M:%S"), "",
             f"Windows      : {platform.platform()} ({platform.machine()})"]
    try:
        lines.append(f"edition      : {platform.win32_edition()}  build {platform.win32_ver()[1]}")
    except Exception:
        pass
    lines += [f"program      : {sys.executable}", f"python       : {sys.version.split()[0]}", "", "Components:"]
    for name in ("PyQt6.QtWidgets", "pymupdf", "numpy", "cv2", "PIL", "ezdxf", "pikepdf", "docx",
                 "openpyxl", "pyhanko", "fontTools"):
        try:
            mod = __import__(name)
            lines.append(f"   OK      {name} {getattr(mod, '__version__', '')}".rstrip())
        except Exception as ex:
            lines.append(f"   FAILED  {name}: {type(ex).__name__}: {ex}")
    from . import cvsafe
    lines += ["", "Image features (scan editing, OCR, table export, drawing tracing): "
              + ("available" if cvsafe.AVAILABLE else "UNAVAILABLE - " + cvsafe.ERROR)]
    lines += ["", "Windows libraries:"]
    for dll in ("msvcp140.dll", "vcruntime140.dll", "vcomp140.dll", "ucrtbase.dll",
                "MF.dll", "MFPlat.dll", "MFReadWrite.dll", "d3d11.dll"):
        try:
            ctypes.WinDLL(dll)
            lines.append("   present  " + dll)
        except Exception:
            lines.append("   MISSING  " + dll)
    lines += ["", f"Tesseract OCR: {bundled_tesseract() or 'not bundled'}"]
    text = "\n".join(lines)
    out = user_data_dir() / "diagnostic.txt"
    try:
        out.write_text(text, encoding="utf-8")
    except Exception:
        pass
    # Show it: a dialog when the program is already running (File > Diagnose Problems), otherwise just
    # open the report. Starting Qt only to show a message box is not safe in the compiled build.
    from PyQt6.QtWidgets import QApplication, QMessageBox
    running = QApplication.instance()
    if running is not None:
        box = QMessageBox(QMessageBox.Icon.Information, APP_NAME,
                          "Diagnostic report written to: " + str(out))
        box.setDetailedText(text)
        box.exec()
    elif os.environ.get("PDFSTUDIO_NO_OPEN") != "1":
        try:
            os.startfile(str(out))
        except Exception:
            if sys.stderr:
                print(text, file=sys.stderr)
    return 0


def _report_startup_failure(ex: Exception):
    """A library failed to load - tell the user what is missing and how to fix it, and log the details."""
    missing = str(ex)
    hint = ""
    if "cv2" in missing.lower() or "dll load failed" in missing.lower():
        hint = ("A Windows component this program needs could not be loaded.\n\n"
                "Please install the Microsoft Visual C++ Redistributable (x64) from microsoft.com. On a Windows N "
                "or KN edition, also install the Media Feature Pack from Windows Update.\n\n"
                "If you upgraded from an older version, uninstall PDF Studio first and install it again.")
    text = (APP_NAME + " could not start.\n\n" + missing + ("\n\n" + hint if hint else "")).strip()
    try:
        from .paths import user_data_dir
        with (user_data_dir() / "crash.log").open("a", encoding="utf-8") as fh:
            fh.write("\nstartup failure: " + missing + "\n" + traceback.format_exc())
    except Exception:
        pass
    try:
        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(None, APP_NAME, text)
    except Exception:
        if sys.stderr:
            print(text, file=sys.stderr)


def run(argv) -> int:
    if "--selftest" in argv:
        return selftest()
    if "--diagnose" in argv:
        return diagnose()
    _enable_crash_log()
    dev_script = os.environ.get("PDFSTUDIO_DEV_SCRIPT")
    if dev_script and os.environ.get("PDFSTUDIO_DEV") == "1" and os.path.isfile(dev_script):
        # developer diagnostics: run a GUI test script inside the packaged app
        import runpy
        sys.excepthook = _excepthook
        runpy.run_path(dev_script, run_name="__main__")
        return 0
    # file arguments become absolute first: the process is about to leave the folder it was started in
    argv = [argv[0]] + [a if a.startswith("-") else os.path.abspath(a) for a in argv[1:]]
    from .security import harden_process
    harden_process()            # never pick up DLLs or programs planted next to a double-clicked PDF
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("PDFStudio")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    from .paths import resource
    ico = resource("assets", "icon.ico")
    app.setWindowIcon(QIcon(str(ico)) if ico.is_file() else icon("pages", "#d7282f", 64))
    if sys.platform == "win32":        # own taskbar identity/icon instead of python.exe's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PDFStudio.Editor")
        except Exception:
            pass
    sys.excepthook = _excepthook

    threading.Thread(target=FONT_INDEX.ensure, daemon=True).start()   # warm the font index

    try:
        from .mainwindow import MainWindow
    except ImportError as ex:                       # a library did not load (missing system DLL, broken install)
        _report_startup_failure(ex)
        return 1
    win = MainWindow()
    win.show()
    files = [a for a in argv[1:] if not a.startswith("-")]
    if files:
        win.open_file(files[0])
    return app.exec()
