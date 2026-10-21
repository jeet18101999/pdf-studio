"""Operations on images placed on PDF pages: list, extract, move/resize, replace, delete, z-order."""
from __future__ import annotations

import io
from dataclasses import dataclass

import pymupdf
from PIL import Image

from . import pagecache
from .imagetools import ImageSettings, apply_settings, to_png_bytes


@dataclass
class PageImage:
    page: int
    xref: int
    rect: pymupdf.Rect      # unrotated page coordinates
    width: int
    height: int
    number: int


def list_images(page: pymupdf.Page) -> list[PageImage]:
    out = []
    for info in pagecache.image_infos(page):
        r = pymupdf.Rect(info["bbox"])
        if r.is_empty or r.is_infinite:
            continue
        if info.get("width", 0) <= 1 and info.get("height", 0) <= 1:
            continue        # placeholder left behind by Page.delete_image()
        out.append(PageImage(page.number, info.get("xref", 0), r, info.get("width", 0),
                             info.get("height", 0), info.get("number", 0)))
    return out


def extract_pil(doc: pymupdf.Document, xref: int) -> Image.Image:
    """Extract an image including its soft mask (transparency)."""
    pix = pymupdf.Pixmap(doc, xref)
    smask = doc.xref_get_key(xref, "SMask")
    if smask[0] == "xref":
        try:
            mask = pymupdf.Pixmap(doc, int(smask[1].split()[0]))
            pix = pymupdf.Pixmap(pix, mask)
        except Exception:
            pass
    if pix.colorspace and pix.colorspace.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    return img.convert("RGBA")


def xref_is_shared(doc, xref: int) -> int:
    n = 0
    for p in doc:
        for it in p.get_images(full=True):
            if it[0] == xref:
                n += 1
    return n


USER_IMAGE_KEY = "PDFStudioUserImage"


def insert_pil(page: pymupdf.Page, rect: pymupdf.Rect, img: Image.Image, behind=False) -> int:
    rect = pymupdf.Rect(rect)
    xref = page.insert_image(rect, stream=to_png_bytes(img), keep_proportion=False, overlay=not behind)
    try:        # mark as a user image so it is never mistaken for part of a scan
        page.parent.xref_set_key(xref, USER_IMAGE_KEY, "true")
    except Exception:
        pass
    return xref


def is_user_image(doc, xref: int) -> bool:
    try:
        return bool(xref) and doc.xref_get_key(xref, USER_IMAGE_KEY)[0] != "null"
    except Exception:
        return False


def delete_image(page: pymupdf.Page, img: PageImage):
    if img.xref:
        page.delete_image(img.xref)
    else:   # inline image: cover by redacting its area (image pixels only)
        page.add_redact_annot(img.rect, fill=False, cross_out=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE, text=pymupdf.PDF_REDACT_TEXT_NONE)


def move_image(doc, page: pymupdf.Page, img: PageImage, new_rect: pymupdf.Rect, behind=None):
    """Move/resize by re-inserting the same pixels at a new rectangle."""
    pil = extract_pil(doc, img.xref)
    delete_image(page, img)
    return insert_pil(page, new_rect, pil, behind=bool(behind))


def is_behind_text(page: pymupdf.Page, img: PageImage) -> bool:
    """True if the image is drawn before any text in the content stream (i.e. behind text)."""
    try:
        for b in page.get_text("dict", clip=img.rect)["blocks"]:
            if b.get("type") == 0:
                break
        else:
            return False
        tr = page.get_texttrace()
        first_text_seq = min((t["seqno"] for t in tr), default=None)
        infos = [i for i in pagecache.image_infos(page) if i.get("xref") == img.xref]
        if first_text_seq is None or not infos:
            return False
        return infos[0].get("number", 0) < first_text_seq
    except Exception:
        return False


def replace_with_settings(doc, page, img: PageImage, settings: ImageSettings, source: Image.Image | None = None):
    pil = source if source is not None else extract_pil(doc, img.xref)
    out = apply_settings(pil, settings)
    rect = pymupdf.Rect(img.rect)
    # keep the centre, adapt aspect ratio if rotation/crop changed it
    if settings.keep_aspect and out.width and out.height:
        ar = out.width / out.height
        c = rect.tl + (rect.br - rect.tl) * 0.5
        w = rect.width
        h = w / ar
        if h > rect.height * 1.6:
            h = rect.height
            w = h * ar
        rect = pymupdf.Rect(c.x - w / 2, c.y - h / 2, c.x + w / 2, c.y + h / 2)
    delete_image(page, img)
    insert_pil(page, rect, out, behind=settings.behind_text)
    return rect
