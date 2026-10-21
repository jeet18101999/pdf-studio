"""Images -> PDF: one page per image - every frame of a multi-page TIFF or GIF - in the order given.

Quality is preserved: a JPEG that needs no rotation or cropping is embedded byte for byte (no re-compression);
everything else is embedded losslessly, except rotated photos, which are re-saved as high-quality JPEG
instead of ballooning into huge PNGs. Phone photos are turned upright using their EXIF orientation.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from PIL import Image, ImageOps, ImageSequence

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".jfif"}
PAGE_SIZES = {"A4": (595.28, 841.89), "A3": (841.89, 1190.55), "A5": (419.53, 595.28),
              "Letter": (612.0, 792.0), "Legal": (612.0, 1008.0)}
MM = 72.0 / 25.4


@dataclass
class ImagePdfOptions:
    page_size: str = "fit"        # "fit": each page takes its image's own size; or a key of PAGE_SIZES
    orientation: str = "auto"     # auto | portrait | landscape (fixed page sizes only)
    margin_mm: float = 0.0
    placement: str = "fit"        # fit: whole image visible | fill: cover the page, cropping | actual: true size
    default_dpi: int = 96         # for images that don't say how big they are


def is_image_file(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXT


def _dpi(img: Image.Image, default: int) -> float:
    try:
        d = float(img.info.get("dpi", (0, 0))[0])
        if 30 <= d <= 2400:
            return d
    except Exception:
        pass
    return float(default)


def _frames(path: str):
    """(PIL image, original bytes usable as-is or None, dpi) for every frame."""
    raw = Path(path).read_bytes()
    with Image.open(io.BytesIO(raw)) as im:
        n = getattr(im, "n_frames", 1)
        fmt = (im.format or "").upper()
        for frame in ImageSequence.Iterator(im):
            orientation = frame.getexif().get(0x0112, 1) if hasattr(frame, "getexif") else 1
            upright = ImageOps.exif_transpose(frame.copy())
            as_is = raw if (fmt == "JPEG" and n == 1 and orientation in (1, None)) else None
            yield upright, as_is, _dpi(im, 0) or 0.0, fmt


def _encode(img: Image.Image, was_jpeg: bool) -> bytes:
    buf = io.BytesIO()
    if was_jpeg and img.mode in ("RGB", "L", "CMYK"):
        img.save(buf, "JPEG", quality=95, subsampling=0)
        return buf.getvalue()
    if img.mode not in ("RGB", "RGBA", "L", "LA"):
        img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
    img.save(buf, "PNG", optimize=False)
    return buf.getvalue()


def _page_rect(w_pt: float, h_pt: float, opt: ImagePdfOptions) -> pymupdf.Rect:
    margin = opt.margin_mm * MM
    if opt.page_size == "fit" or opt.page_size not in PAGE_SIZES:
        return pymupdf.Rect(0, 0, w_pt + 2 * margin, h_pt + 2 * margin)
    pw, ph = PAGE_SIZES[opt.page_size]
    landscape = (opt.orientation == "landscape") or (opt.orientation == "auto" and w_pt > h_pt)
    if landscape:
        pw, ph = ph, pw
    return pymupdf.Rect(0, 0, pw, ph)


def _centered(w: float, h: float, box: pymupdf.Rect) -> pymupdf.Rect:
    x = box.x0 + (box.width - w) / 2
    y = box.y0 + (box.height - h) / 2
    return pymupdf.Rect(x, y, x + w, y + h)


def images_to_pdf(paths: list[str], options: ImagePdfOptions | None = None, progress=None) -> pymupdf.Document:
    """Build a new PDF from the images. Raises ValueError naming any file that cannot be read."""
    opt = options or ImagePdfOptions()
    doc = pymupdf.open()
    for n, path in enumerate(paths, 1):
        if progress:
            progress(n, len(paths), Path(path).name)
        try:
            frames = list(_frames(path))
        except Exception as ex:
            doc.close()
            raise ValueError(f"Cannot read {Path(path).name}: {ex}") from ex
        for img, as_is, dpi, fmt in frames:
            dpi = dpi or float(opt.default_dpi)
            w_pt, h_pt = img.width / dpi * 72.0, img.height / dpi * 72.0
            page_rect = _page_rect(w_pt, h_pt, opt)
            page = doc.new_page(width=page_rect.width, height=page_rect.height)
            margin = opt.margin_mm * MM
            box = page_rect + (margin, margin, -margin, -margin)
            was_jpeg = fmt == "JPEG"

            if opt.placement == "fill" and opt.page_size != "fit":
                # cover the whole area, cropping the overhang evenly from both sides
                target = box.width / box.height
                src = img.width / img.height
                if abs(src - target) > 1e-3:
                    if src > target:
                        cw = int(round(img.height * target))
                        x0 = (img.width - cw) // 2
                        img = img.crop((x0, 0, x0 + cw, img.height))
                    else:
                        ch = int(round(img.width / target))
                        y0 = (img.height - ch) // 2
                        img = img.crop((0, y0, img.width, y0 + ch))
                    as_is = None
                rect = box
            else:
                if opt.placement == "actual" and opt.page_size != "fit":
                    w, h = w_pt, h_pt
                else:
                    w, h = box.width, box.width * img.height / img.width
                if w > box.width or h > box.height or opt.placement == "fit":
                    scale = min(box.width / w_pt, box.height / h_pt)
                    if opt.placement == "actual":
                        scale = min(1.0, scale)            # true size, shrunk only if it would not fit
                    w, h = w_pt * scale, h_pt * scale
                rect = _centered(w, h, box)
            stream = as_is if as_is is not None else _encode(img, was_jpeg)
            page.insert_image(rect, stream=stream, keep_proportion=False)
    if doc.page_count == 0:
        doc.close()
        raise ValueError("No images to convert")
    return doc
