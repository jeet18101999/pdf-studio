"""PDF Studio - a full-featured PDF editor.

Run:  python main.py [file.pdf]
"""
import multiprocessing
import sys

if __name__ == "__main__":
    # OCR recognises pages in worker processes. In the packaged app each worker starts this same exe, and
    # freeze_support() runs the worker right here - before the GUI is imported - instead of a second window.
    multiprocessing.freeze_support()
    from pdfeditor.app import run
    sys.exit(run(sys.argv))
