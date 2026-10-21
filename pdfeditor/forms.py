"""PDF forms: read/fill fields, create and edit fields (Prepare Form), auto-detect fields,
flatten, reset, and import/export form data."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import pymupdf

TEXT = pymupdf.PDF_WIDGET_TYPE_TEXT
CHECKBOX = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
RADIO = pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON
COMBO = pymupdf.PDF_WIDGET_TYPE_COMBOBOX
LISTBOX = pymupdf.PDF_WIDGET_TYPE_LISTBOX
SIGNATURE = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
BUTTON = pymupdf.PDF_WIDGET_TYPE_BUTTON

TYPE_NAMES = {TEXT: "Text field", CHECKBOX: "Check box", RADIO: "Radio button", COMBO: "Dropdown",
              LISTBOX: "List box", SIGNATURE: "Signature", BUTTON: "Button"}
TYPE_PREFIX = {TEXT: "Text", CHECKBOX: "Check", RADIO: "Group", COMBO: "Dropdown", LISTBOX: "List",
               SIGNATURE: "Signature", BUTTON: "Button"}

FLAG_READONLY = pymupdf.PDF_FIELD_IS_READ_ONLY
FLAG_REQUIRED = pymupdf.PDF_FIELD_IS_REQUIRED
FLAG_MULTILINE = pymupdf.PDF_TX_FIELD_IS_MULTILINE
FLAG_PASSWORD = pymupdf.PDF_TX_FIELD_IS_PASSWORD
FLAG_COMB = pymupdf.PDF_TX_FIELD_IS_COMB


@dataclass
class FieldInfo:
    page: int
    xref: int
    name: str
    type: int
    rect: pymupdf.Rect          # unrotated page coordinates
    value: object = ""
    choices: list = field(default_factory=list)
    flags: int = 0
    tooltip: str = ""
    fontsize: float = 0.0
    maxlen: int = 0
    on_state: str = "Yes"

    @property
    def type_name(self):
        return TYPE_NAMES.get(self.type, "Field")

    @property
    def readonly(self):
        return bool(self.flags & FLAG_READONLY)

    @property
    def required(self):
        return bool(self.flags & FLAG_REQUIRED)

    @property
    def multiline(self):
        return bool(self.flags & FLAG_MULTILINE)

    @property
    def checked(self):
        return self.value not in ("Off", "", False, None)


def _info(page: pymupdf.Page, w: pymupdf.Widget) -> FieldInfo:
    on = "Yes"
    if w.field_type in (CHECKBOX, RADIO):
        try:
            on = w.on_state() or "Yes"
        except Exception:
            pass
    return FieldInfo(page=page.number, xref=w.xref, name=w.field_name or "", type=w.field_type,
                     rect=pymupdf.Rect(w.rect), value=w.field_value, choices=list(w.choice_values or []),
                     flags=w.field_flags or 0, tooltip=w.field_label or "", fontsize=w.text_fontsize or 0,
                     maxlen=w.text_maxlen or 0, on_state=on)


def list_fields(page: pymupdf.Page) -> list[FieldInfo]:
    return [_info(page, w) for w in page.widgets()]


def all_fields(doc: pymupdf.Document) -> list[FieldInfo]:
    return [f for page in doc for f in list_fields(page)]


def has_form(doc: pymupdf.Document) -> bool:
    return any(True for page in doc for _ in page.widgets())


def get_widget(page: pymupdf.Page, xref: int) -> pymupdf.Widget | None:
    for w in page.widgets():
        if w.xref == xref:
            return w
    return None


# ------------------------------------------------------------------ filling
def set_value(doc: pymupdf.Document, page_no: int, xref: int, value) -> None:
    page = doc[page_no]
    w = get_widget(page, xref)
    if w is None:
        raise KeyError(f"field {xref} not found")
    t = w.field_type
    if t == CHECKBOX:
        w.field_value = (w.on_state() or True) if value else "Off"
    elif t == RADIO:
        if value:
            w.field_value = True
            w.update()
            # make the group exclusive on every page
            for p in doc:
                for other in p.widgets():
                    if other.field_type == RADIO and other.field_name == w.field_name and other.xref != xref:
                        if other.field_value not in ("Off", False):
                            other.field_value = False
                            other.update()
            return
        w.field_value = False
    elif t == LISTBOX:
        w.field_value = value if isinstance(value, (list, tuple)) else str(value)
    elif t in (TEXT, COMBO):
        w.field_value = "" if value is None else str(value)
    else:
        return
    w.update()


def reset_form(doc: pymupdf.Document) -> int:
    n = 0
    for page in doc:
        for w in page.widgets():
            if w.field_type == TEXT:
                w.field_value = ""
            elif w.field_type in (CHECKBOX, RADIO):
                w.field_value = False
            elif w.field_type in (COMBO, LISTBOX):
                w.field_value = w.choice_values[0] if w.choice_values else ""
            else:
                continue
            w.update()
            n += 1
    return n


def export_data(doc: pymupdf.Document) -> dict:
    data = {}
    for f in all_fields(doc):
        if f.type in (SIGNATURE, BUTTON) or not f.name:
            continue
        if f.type == RADIO:
            if f.checked:
                data[f.name] = f.on_state
            else:
                data.setdefault(f.name, None)
        elif f.type == CHECKBOX:
            data[f.name] = f.checked
        else:
            data[f.name] = f.value
    return data


def import_data(doc: pymupdf.Document, data: dict) -> int:
    n = 0
    for f in all_fields(doc):
        if f.name not in data:
            continue
        v = data[f.name]
        if f.type == RADIO:
            set_value(doc, f.page, f.xref, v is not None and str(v) == str(f.on_state))
        elif f.type == CHECKBOX:
            set_value(doc, f.page, f.xref, bool(v) and str(v).lower() not in ("off", "false", "0", "no"))
        else:
            set_value(doc, f.page, f.xref, v)
        n += 1
    return n


def save_data_json(doc, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(export_data(doc), fh, indent=2, ensure_ascii=False)


def load_data_json(doc, path) -> int:
    with open(path, encoding="utf-8") as fh:
        return import_data(doc, json.load(fh))


# ------------------------------------------------------------------ prepare form
def field_names(doc) -> set[str]:
    return {f.name for f in all_fields(doc)}


def unique_name(doc, base: str) -> str:
    base = re.sub(r"[.\s]+", "_", base.strip()) or "Field"
    names = field_names(doc)
    if base not in names:
        return base
    i = 1
    while f"{base}_{i}" in names:
        i += 1
    return f"{base}_{i}"


def create_field(doc: pymupdf.Document, page_no: int, ftype: int, rect, name: str | None = None,
                 choices: list | None = None, tooltip: str = "", fontsize: float = 0, value=None,
                 on_state: str | None = None) -> int:
    page = doc[page_no]
    rect = pymupdf.Rect(rect).normalize()
    w = pymupdf.Widget()
    w.field_type = ftype
    w.rect = rect
    if ftype == RADIO:
        w.field_name = name or unique_name(doc, "Group")      # radios share the group name
    else:
        w.field_name = unique_name(doc, name or TYPE_PREFIX.get(ftype, "Field"))
    w.field_label = tooltip
    w.text_font = "Helv"
    w.text_fontsize = fontsize
    w.text_color = (0, 0, 0)
    if ftype in (COMBO, LISTBOX):
        w.choice_values = list(choices or ["Option 1", "Option 2"])
        w.field_value = value if value is not None else w.choice_values[0]
    elif ftype in (CHECKBOX, RADIO):
        w.field_value = bool(value)
        if on_state and ftype == RADIO:
            w.button_caption = on_state
    elif ftype == TEXT:
        w.field_value = "" if value is None else str(value)
        if rect.height > 2.2 * max(10, fontsize or 10):
            w.field_flags = FLAG_MULTILINE
    elif ftype == BUTTON:
        w.button_caption = name or "Button"
    annot = page.add_widget(w)
    return annot.xref


def update_field(doc: pymupdf.Document, page_no: int, xref: int, *, name=None, tooltip=None, readonly=None,
                 required=None, multiline=None, password=None, maxlen=None, fontsize=None, choices=None,
                 rect=None, text_color=None, fill_color=None, border_color=None) -> int:
    page = doc[page_no]
    w = get_widget(page, xref)
    if w is None:
        raise KeyError(f"field {xref} not found")
    if name is not None and name != w.field_name:
        w.field_name = name if w.field_type == RADIO else unique_name(doc, name)
    if tooltip is not None:
        w.field_label = tooltip
    flags = w.field_flags or 0
    for on, bit in ((readonly, FLAG_READONLY), (required, FLAG_REQUIRED), (multiline, FLAG_MULTILINE),
                    (password, FLAG_PASSWORD)):
        if on is not None:
            flags = (flags | bit) if on else (flags & ~bit)
    w.field_flags = flags
    if maxlen is not None:
        w.text_maxlen = int(maxlen)
    if fontsize is not None:
        w.text_fontsize = float(fontsize)
    if choices is not None and w.field_type in (COMBO, LISTBOX):
        w.choice_values = list(choices)
        if w.field_value not in choices:
            w.field_value = choices[0] if choices else ""
    if rect is not None:
        w.rect = pymupdf.Rect(rect).normalize()
    if text_color is not None:
        w.text_color = text_color
    if fill_color is not None:
        w.fill_color = fill_color or None
    if border_color is not None:
        w.border_color = border_color or None
        w.border_width = 1 if border_color else 0
    w.update()
    return w.xref


def delete_field(doc: pymupdf.Document, page_no: int, xref: int):
    page = doc[page_no]
    w = get_widget(page, xref)
    if w is not None:
        page.delete_widget(w)


def flatten(doc: pymupdf.Document):
    """Burn field appearances into the page content; the form is no longer editable."""
    doc.bake(annots=False, widgets=True)


# ------------------------------------------------------------------ auto-detect
@dataclass
class Detected:
    type: int
    rect: pymupdf.Rect
    name: str


CHECK_GLYPHS = set("☐□❑❒▢◻⬜")


def _label_left(words, rect: pymupdf.Rect, max_gap=160) -> str:
    cy = (rect.y0 + rect.y1) / 2
    cands = [w for w in words if w[2] <= rect.x0 + 2 and rect.x0 - w[2] < max_gap and w[1] - 3 <= cy <= w[3] + 6]
    cands.sort(key=lambda w: w[0])
    label = []
    for w in reversed(cands):
        if label and w[2] < label[0][0] - 12:
            break
        label.insert(0, w)
        if len(label) >= 4:
            break
    return " ".join(w[4] for w in label).strip(" :_.")


def _label_above(words, rect: pymupdf.Rect) -> str:
    cands = [w for w in words if w[3] <= rect.y0 + 2 and rect.y0 - w[3] < 14 and w[0] < rect.x1 and w[2] > rect.x0]
    cands.sort(key=lambda w: (w[1], w[0]))
    return " ".join(w[4] for w in cands[:4]).strip(" :_.")


def _clean_name(label: str, fallback: str) -> str:
    label = re.sub(r"[^\w\s/-]", "", label).strip()
    return label[:40] if label else fallback


def _type_for_label(label: str, default: int) -> int:
    l = label.lower()
    if default == TEXT and ("signature" in l or l.startswith("sign") or "signed" in l):
        return SIGNATURE
    return default


def auto_detect(page: pymupdf.Page) -> list[Detected]:
    """Find likely form fields: fill-in lines, underscore runs, check boxes and empty labelled boxes."""
    words = page.get_text("words")
    existing = [pymupdf.Rect(w.rect) for w in page.widgets()]
    found: list[Detected] = []

    def free(r: pymupdf.Rect) -> bool:
        if any(abs(r & e) > 0.3 * min(abs(r), abs(e)) for e in existing):
            return False
        if any(abs(r & d.rect) > 0.3 * min(abs(r), abs(d.rect)) for d in found):
            return False
        return True

    def text_inside(r: pymupdf.Rect) -> bool:
        for w in words:
            wr = pymupdf.Rect(w[:4])
            c = (wr.tl + wr.br) * 0.5
            if r.contains(c) and w[4].strip("_ .") != "":
                return True
        return False

    # underscore runs typed as text: "Name: ________"
    for w in words:
        if re.fullmatch(r"_{4,}[.:]?", w[4]):
            wr = pymupdf.Rect(w[:4])
            r = pymupdf.Rect(wr.x0, wr.y1 - 16, wr.x1, wr.y1 - 1)
            if free(r):
                label = _label_left(words, wr) or _label_above(words, r)
                found.append(Detected(_type_for_label(label, TEXT), r, _clean_name(label, "Text")))

    segments, boxes = [], []
    for d in page.get_drawings():
        for item in d["items"]:
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 0.8 and 36 <= abs(p2.x - p1.x) <= 480:
                    segments.append(pymupdf.Rect(min(p1.x, p2.x), p1.y, max(p1.x, p2.x), p1.y))
            elif item[0] == "re":
                r = pymupdf.Rect(item[1])
                if r.height < 1.6 and 36 <= r.width <= 480:
                    segments.append(pymupdf.Rect(r.x0, r.y1, r.x1, r.y1))
                elif 6 <= r.width <= 18 and 6 <= r.height <= 18 and abs(r.width - r.height) < 3:
                    boxes.append(r)
                elif 60 <= r.width <= 480 and 14 <= r.height <= 40:
                    boxes.append(r)

    # fill-in lines with nothing written on them
    for s in segments:
        r = pymupdf.Rect(s.x0, s.y0 - 16, s.x1, s.y0 - 0.5)
        if r.y0 < 0 or text_inside(r) or not free(r):
            continue
        label = _label_left(words, r) or _label_above(words, pymupdf.Rect(s.x0, s.y0 - 30, s.x1, s.y0 - 16))
        if not label:
            continue           # table rules and decorative lines have no label next to them
        found.append(Detected(_type_for_label(label, TEXT), r, _clean_name(label, "Text")))

    # square boxes -> check boxes; wider empty boxes with a label -> text fields
    for b in boxes:
        if text_inside(b) or not free(b):
            continue
        if b.width <= 18:
            label = _label_right(words, b)
            found.append(Detected(CHECKBOX, b, _clean_name(label, "Check")))
        else:
            label = _label_left(words, b, 40)
            if label.endswith(":") or label:
                found.append(Detected(_type_for_label(label, TEXT), b + (1, 1, -1, -1), _clean_name(label, "Text")))

    # check-box glyphs in text
    for w in words:
        if w[4] and w[4][0] in CHECK_GLYPHS:
            wr = pymupdf.Rect(w[:4])
            side = min(wr.height, 12)
            r = pymupdf.Rect(wr.x0, wr.y0 + (wr.height - side) / 2, wr.x0 + side, wr.y0 + (wr.height + side) / 2)
            if free(r):
                label = w[4][1:].strip() or _label_right(words, r)
                found.append(Detected(CHECKBOX, r, _clean_name(label, "Check")))
    return found


def _label_right(words, rect: pymupdf.Rect) -> str:
    cy = (rect.y0 + rect.y1) / 2
    cands = sorted((w for w in words if w[0] >= rect.x1 - 1 and w[0] - rect.x1 < 12 + 200 and w[1] - 3 <= cy <= w[3] + 3),
                   key=lambda w: w[0])
    label = []
    last = rect.x1
    for w in cands:
        if w[0] - last > 14:
            break
        label.append(w[4])
        last = w[2]
        if len(label) >= 4:
            break
    return " ".join(label)


def apply_detected(doc, page_no: int, detected: list[Detected]) -> list[int]:
    return [create_field(doc, page_no, d.type, d.rect, d.name) for d in detected]
