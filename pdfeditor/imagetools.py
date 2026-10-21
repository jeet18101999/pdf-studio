"""Image processing for inserted images: background removal, opacity, flips, adjustments."""
from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageEnhance, ImageOps

BG_NONE, BG_WHITE, BG_COLOR, BG_AUTO = "none", "white", "color", "auto"

from .cvsafe import cv2


@dataclass
class ImageSettings:
    bg_mode: str = BG_NONE
    bg_color: tuple = (255, 255, 255)
    tolerance: int = 30          # 0..255 colour distance for colour-key modes
    feather: int = 1             # edge softening radius (px)
    opacity: float = 1.0
    rotate: int = 0              # 0/90/180/270 clockwise
    flip_h: bool = False
    flip_v: bool = False
    brightness: float = 1.0
    contrast: float = 1.0
    saturation: float = 1.0
    grayscale: bool = False
    crop: tuple = (0.0, 0.0, 0.0, 0.0)   # fractions trimmed from left, top, right, bottom
    behind_text: bool = False
    keep_aspect: bool = True


def load_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGBA")


def apply_settings(img: Image.Image, s: ImageSettings) -> Image.Image:
    img = img.convert("RGBA")
    # crop
    l, t, r, b = s.crop
    if any((l, t, r, b)):
        w, h = img.size
        box = (int(w * l), int(h * t), max(int(w * l) + 1, int(w * (1 - r))), max(int(h * t) + 1, int(h * (1 - b))))
        img = img.crop(box)
    # background removal
    if s.bg_mode == BG_WHITE:
        img = remove_color(img, (255, 255, 255), s.tolerance, s.feather, flood=False)
    elif s.bg_mode == BG_COLOR:
        img = remove_color(img, s.bg_color, s.tolerance, s.feather, flood=True)
    elif s.bg_mode == BG_AUTO:
        img = remove_background_auto(img, s.feather)
    # colour adjustments (keep alpha)
    alpha = img.getchannel("A")
    rgb = img.convert("RGB")
    if s.grayscale:
        rgb = ImageOps.grayscale(rgb).convert("RGB")
    if s.brightness != 1.0:
        rgb = ImageEnhance.Brightness(rgb).enhance(s.brightness)
    if s.contrast != 1.0:
        rgb = ImageEnhance.Contrast(rgb).enhance(s.contrast)
    if s.saturation != 1.0 and not s.grayscale:
        rgb = ImageEnhance.Color(rgb).enhance(s.saturation)
    if s.opacity < 1.0:
        alpha = alpha.point(lambda a: int(a * max(0.0, s.opacity)))
    rgb.putalpha(alpha)
    img = rgb
    if s.flip_h:
        img = ImageOps.mirror(img)
    if s.flip_v:
        img = ImageOps.flip(img)
    if s.rotate % 360:
        img = img.rotate(-s.rotate, expand=True)
    return img


def remove_color(img: Image.Image, color, tolerance: int, feather: int, flood: bool) -> Image.Image:
    """Make pixels close to `color` transparent. flood=True only removes regions touching the border."""
    arr = np.array(img.convert("RGBA"))
    rgb = arr[:, :, :3].astype(np.int32)
    dist = np.sqrt(((rgb - np.array(color[:3], dtype=np.int32)) ** 2).sum(axis=2))
    near = (dist <= tolerance).astype(np.uint8)
    if flood:
        near = _border_connected(near)
    alpha = arr[:, :, 3].astype(np.float32)
    # soft edge: partially transparent for distances just above tolerance
    soft = np.clip((dist - tolerance) / max(1.0, tolerance * 0.5 + 1), 0, 1)
    new_alpha = np.where(near > 0, 0, alpha * np.where(dist < tolerance * 1.5 + 1, soft, 1.0))
    if flood:
        new_alpha = np.where(near > 0, 0, alpha)
    new_alpha = _feather(new_alpha, feather)
    arr[:, :, 3] = np.clip(np.minimum(new_alpha, alpha), 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def remove_background_auto(img: Image.Image, feather: int = 1) -> Image.Image:
    """Automatic subject extraction with GrabCut seeded from the image border."""
    arr = np.array(img.convert("RGBA"))
    bgr = cv2.cvtColor(arr[:, :, :3], cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    scale = 1.0
    if max(h, w) > 800:                       # grabcut is slow; work on a smaller copy
        scale = 800 / max(h, w)
        small = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    else:
        small = bgr
    sh, sw = small.shape[:2]
    mask = np.full((sh, sw), cv2.GC_PR_FGD, np.uint8)
    m = max(2, int(min(sh, sw) * 0.02))
    mask[:m, :] = cv2.GC_BGD
    mask[-m:, :] = cv2.GC_BGD
    mask[:, :m] = cv2.GC_BGD
    mask[:, -m:] = cv2.GC_BGD
    # pixels matching the dominant border colour are probably background too
    border = np.concatenate([small[:m].reshape(-1, 3), small[-m:].reshape(-1, 3),
                             small[:, :m].reshape(-1, 3), small[:, -m:].reshape(-1, 3)])
    med = np.median(border, axis=0)
    close = np.sqrt(((small.astype(np.int32) - med) ** 2).sum(axis=2)) < 18
    mask[close & (mask == cv2.GC_PR_FGD)] = cv2.GC_PR_BGD
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(small, mask, None, bgd, fgd, 5, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return img
    fg = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    if scale != 1.0:
        fg = cv2.resize(fg, (w, h), interpolation=cv2.INTER_LINEAR)
    alpha = _feather(fg.astype(np.float32), feather)
    arr[:, :, 3] = np.minimum(arr[:, :, 3], alpha.astype(np.uint8))
    return Image.fromarray(arr, "RGBA")


def _border_connected(mask: np.ndarray) -> np.ndarray:
    n, labels = cv2.connectedComponents(mask, connectivity=4)
    border_labels = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])))
    border_labels.discard(0)
    out = np.isin(labels, list(border_labels)) & (mask > 0)
    return out.astype(np.uint8)


def _feather(alpha: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return alpha
    k = radius * 2 + 1
    return cv2.GaussianBlur(alpha, (k, k), 0)


def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    if img.mode == "RGBA" and img.getchannel("A").getextrema() == (255, 255):
        img = img.convert("RGB")          # no transparency -> smaller file, no SMask
    img.save(buf, "PNG", optimize=False)
    return buf.getvalue()


def dominant_border_color(img: Image.Image) -> tuple:
    arr = np.array(img.convert("RGB"))
    border = np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]])
    return tuple(int(v) for v in np.median(border, axis=0))


def signature_to_image(strokes, size, pen_width=3, color=(0, 0, 80)) -> Image.Image:
    """Render signature strokes (list of point lists) to a transparent image, tightly cropped."""
    w, h = size
    canvas = np.zeros((h, w, 4), np.uint8)
    for stroke in strokes:
        pts = np.array(stroke, np.int32).reshape(-1, 1, 2)
        if len(pts) == 1:
            cv2.circle(canvas, tuple(pts[0][0]), pen_width // 2, (*color, 255), -1, cv2.LINE_AA)
        else:
            cv2.polylines(canvas, [pts], False, (*color, 255), pen_width, cv2.LINE_AA)
    img = Image.fromarray(canvas, "RGBA")
    bbox = img.getchannel("A").getbbox()
    if bbox:
        pad = 4
        img = img.crop((max(0, bbox[0] - pad), max(0, bbox[1] - pad), min(w, bbox[2] + pad), min(h, bbox[3] + pad)))
    return img
