"""Office documents -> PDF.

When Microsoft Office is installed, Office itself does the conversion - Word, Excel or PowerPoint exporting
to PDF - so the result looks exactly like the document does in Office: fonts, tables, headers and footers,
page breaks. Office is driven by a small PowerShell script. File paths go in as separate arguments and are
never spliced into script text, and macros are force-disabled before the document is opened, so a hostile
document cannot run code during the conversion.

Without Office, Word files (.docx) are converted by PDF Studio itself: headings, text styles, alignment,
lists, tables and pictures come through, but elaborate layouts may differ from Word.
"""
from __future__ import annotations

import html
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf

WORD_EXT = {".doc", ".docx", ".docm", ".dotx", ".rtf", ".odt"}
EXCEL_EXT = {".xls", ".xlsx", ".xlsm", ".ods", ".csv"}
POWERPOINT_EXT = {".ppt", ".pptx", ".pptm", ".odp"}
OFFICE_EXT = WORD_EXT | EXCEL_EXT | POWERPOINT_EXT

_PROGIDS = {"word": "Word.Application", "excel": "Excel.Application", "powerpoint": "PowerPoint.Application"}
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_SCRIPT = r'''
param([string]$App, [string]$In, [string]$Out)
$ErrorActionPreference = "Stop"
$ForceDisableMacros = 3        # msoAutomationSecurityForceDisable
switch ($App) {
  "word" {
    $o = New-Object -ComObject Word.Application
    $o.Visible = $false
    $o.DisplayAlerts = 0
    $o.AutomationSecurity = $ForceDisableMacros
    try {
      $d = $o.Documents.Open($In, $false, $true, $false)      # ConfirmConversions, ReadOnly, AddToRecentFiles
      try { $d.ExportAsFixedFormat($Out, 17) } finally { $d.Close(0) }
    } finally { $o.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($o) }
  }
  "excel" {
    $o = New-Object -ComObject Excel.Application
    $o.Visible = $false
    $o.DisplayAlerts = $false
    $o.AutomationSecurity = $ForceDisableMacros
    try {
      $b = $o.Workbooks.Open($In, 0, $true)                     # UpdateLinks = never, ReadOnly
      try { $b.ExportAsFixedFormat(0, $Out) } finally { $b.Close($false) }
    } finally { $o.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($o) }
  }
  "powerpoint" {
    $o = New-Object -ComObject PowerPoint.Application
    $o.AutomationSecurity = $ForceDisableMacros
    try {
      $p = $o.Presentations.Open($In, -1, 0, 0)                 # ReadOnly, not Untitled, no window
      try { $p.SaveAs($Out, 32) } finally { $p.Close() }        # ppSaveAsPDF
    } finally { $o.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($o) }
  }
}
'''


def office_app_for(path: str) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in WORD_EXT:
        return "word"
    if ext in EXCEL_EXT:
        return "excel"
    if ext in POWERPOINT_EXT:
        return "powerpoint"
    return None


def office_installed(app: str = "word") -> bool:
    """Is this Office application registered for automation on this PC?"""
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, _PROGIDS[app] + r"\CLSID"))
        return True
    except Exception:
        return False


def _powershell() -> str:
    """Full path, so a planted powershell.exe in the current folder can never be picked up instead."""
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(exe) if exe.is_file() else "powershell.exe"


def office_to_pdf(src: str, out: str, timeout: int = 300) -> str:
    """Convert with Microsoft Office. Raises RuntimeError with a readable message on failure."""
    app = office_app_for(src)
    if app is None:
        raise RuntimeError(f"{Path(src).suffix} files are not Office documents")
    if not office_installed(app):
        raise RuntimeError(f"Microsoft {app.title()} is not installed")
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "convert.ps1"
        script.write_text(_SCRIPT, encoding="utf-8-sig")
        tmp_out = Path(td) / "converted.pdf"
        cmd = [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
               "-App", app, "-In", str(Path(src).resolve()), "-Out", str(tmp_out)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Microsoft {app.title()} did not finish within {timeout // 60} minutes")
        if not tmp_out.is_file() or tmp_out.stat().st_size == 0:
            msg = (r.stderr or r.stdout or "").strip()
            low = msg.lower()
            if "password" in low:
                raise RuntimeError("The document is password protected. Open it in Office, remove the password, "
                                   "and try again.")
            detail = next((ln.strip() for ln in msg.splitlines() if ln.strip() and not ln.startswith("At ")), "")
            raise RuntimeError(f"Microsoft {app.title()} could not convert the file" + (f": {detail[:300]}" if detail else "."))
        shutil.copyfile(tmp_out, out)
    return out


# ------------------------------------------------------------------ built-in .docx converter (no Office)
def _run_html(run) -> str:
    text = html.escape(run.text).replace("\n", "<br/>")
    if not text:
        return ""
    styles = []
    f = run.font
    if f.size is not None:
        styles.append(f"font-size:{f.size.pt:.1f}pt")
    if f.color is not None and f.color.type is not None and f.color.rgb is not None:
        styles.append(f"color:#{f.color.rgb}")
    if f.name:
        styles.append(f"font-family:'{html.escape(f.name)}'")
    if styles:
        text = f'<span style="{";".join(styles)}">{text}</span>'
    if run.bold:
        text = f"<b>{text}</b>"
    if run.italic:
        text = f"<i>{text}</i>"
    if run.underline:
        text = f"<u>{text}</u>"
    return text


def _paragraph_html(par, images: dict, archive) -> str:
    from docx.oxml.ns import qn
    parts = []
    for run in par.runs:
        parts.append(_run_html(run))
        for blip in run._element.iter(qn("a:blip")):          # pictures placed in this run
            rid = blip.get(qn("r:embed"))
            part = par.part.related_parts.get(rid) if rid else None
            if part is None:
                continue
            name = images.get(rid)
            if name is None:
                name = f"img{len(images) + 1}{Path(part.partname).suffix or '.png'}"
                images[rid] = name
                archive.add((part.blob, name))
            extent = next(run._element.iter(qn("wp:extent")), None)
            width = f' width="{int(extent.get("cx")) / 12700:.0f}"' if extent is not None else ""
            parts.append(f'<img src="{name}"{width}/>')
    inner = "".join(parts) or "&#160;"
    style_name = (par.style.name if par.style is not None else "") or ""
    align = {0: "left", 1: "center", 2: "right", 3: "justify"}.get(
        int(par.alignment) if par.alignment is not None else -1, "")
    css = f' style="text-align:{align}"' if align else ""
    if style_name == "Title":
        return f"<h1{css}>{inner}</h1>"
    if style_name.startswith("Heading "):
        level = min(6, max(1, int(style_name.split()[-1]) if style_name.split()[-1].isdigit() else 2))
        return f"<h{level}{css}>{inner}</h{level}>"
    if "List" in style_name:
        return f"<p{css}>&#8226;&#160;{inner}</p>"
    return f"<p{css}>{inner}</p>"


def docx_to_pdf_basic(src: str, out: str) -> str:
    """Convert a .docx without Office, keeping the document's order of paragraphs, tables and pictures."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(src)
    archive = pymupdf.Archive()
    images: dict = {}
    body = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            body.append(_paragraph_html(Paragraph(child, d), images, archive))
        elif tag == "tbl":
            rows = []
            for row in Table(child, d).rows:
                cells = []
                for cell in row.cells:
                    content = "".join(_paragraph_html(p, images, archive) for p in cell.paragraphs)
                    cells.append(f"<td>{content}</td>")
                rows.append("<tr>" + "".join(cells) + "</tr>")
            body.append("<table>" + "".join(rows) + "</table>")
    css = ("body {font-family: sans-serif; font-size: 11pt;} p {margin: 0 0 6pt 0;} "
           "table {border-collapse: collapse; margin: 4pt 0 8pt 0;} td {border: 0.6pt solid #444; padding: 3pt;}")
    story = pymupdf.Story(html="<body>" + "".join(body) + "</body>", user_css=css, archive=archive)

    sec = d.sections[0]
    pw = sec.page_width.pt if sec.page_width else 595.0
    ph = sec.page_height.pt if sec.page_height else 842.0
    ml = sec.left_margin.pt if sec.left_margin is not None else 72.0
    mr = sec.right_margin.pt if sec.right_margin is not None else 72.0
    mt = sec.top_margin.pt if sec.top_margin is not None else 72.0
    mb = sec.bottom_margin.pt if sec.bottom_margin is not None else 72.0
    mediabox = pymupdf.Rect(0, 0, pw, ph)
    where = pymupdf.Rect(ml, mt, pw - mr, ph - mb)

    writer = pymupdf.DocumentWriter(out)
    more = True
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    return out


def document_to_pdf(src: str, out: str) -> tuple[str, str]:
    """Best available conversion. Returns (output path, how it was converted)."""
    app = office_app_for(src)
    if app and office_installed(app):
        return office_to_pdf(src, out), f"Microsoft {app.title()}"
    if Path(src).suffix.lower() == ".docx":
        return docx_to_pdf_basic(src, out), "PDF Studio (Microsoft Word is not installed; layout may differ)"
    raise RuntimeError(f"Converting {Path(src).suffix} files needs Microsoft {app.title() if app else 'Office'} installed.")
