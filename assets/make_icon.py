"""Generates the PDF Studio icon (original design).

Concept: a sheet of paper whose folded corner is a fountain-pen nib, on an indigo-to-coral tile,
with a scan beam crossing the page and a signature stroke - editing, OCR and signing in one mark.
Outputs: icon_1024.png, icon.png (256), icon.ico (16-256), favicon.png (64), icon_preview.png
"""
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

OUT = 1024
SS = 3                      # supersampling for smooth edges
S = OUT * SS
HERE = Path(__file__).resolve().parent


def P(v):
    return int(round(v * SS))


def rounded_mask(radius):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, S - 1, S - 1), radius=P(radius), fill=255)
    return m


def gradient_tile():
    top = np.array((46, 40, 128), np.float32)       # indigo
    mid = np.array((150, 62, 140), np.float32)      # plum
    bottom = np.array((246, 104, 84), np.float32)   # coral
    y, x = np.mgrid[0:S, 0:S].astype(np.float32) / S
    t = np.clip(0.78 * y + 0.22 * x, 0, 1)[..., None]
    col = np.where(t < 0.5, top + (mid - top) * (t / 0.5), mid + (bottom - mid) * ((t - 0.5) / 0.5))
    # gentle radial light from the top-left
    light = np.exp(-(((x - 0.25) ** 2) / 0.18 + ((y - 0.1) ** 2) / 0.12))[..., None]
    col = np.clip(col + light * 38, 0, 255).astype(np.uint8)
    tile = Image.fromarray(col, "RGB").convert("RGBA")
    tile.putalpha(rounded_mask(228))
    return tile


def page_layer():
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    x0, y0, x1, y1 = 262, 168, 762, 856
    fold = 196
    r = 34
    page = [(x0, y0 + r), (x0 + r, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1 - r),
            (x1 - r, y1), (x0 + r, y1), (x0, y1 - r)]
    pg = [(P(a), P(b)) for a, b in page]

    sh = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sh).polygon([(a + P(16), b + P(28)) for a, b in pg], fill=110)
    sh = sh.filter(ImageFilter.GaussianBlur(P(30)))
    shadow = Image.new("RGBA", (S, S), (25, 10, 60, 0))
    shadow.putalpha(sh)
    layer = Image.alpha_composite(layer, shadow)
    d = ImageDraw.Draw(layer)
    d.polygon(pg, fill=(251, 249, 255, 255))

    # text lines (the editable content), kept clear of the folded corner
    for w, y in ((250, 318), (380, 396), (330, 474), (390, 552)):
        d.rounded_rectangle((P(x0 + 72), P(y), P(x0 + 72 + w), P(y + 32)), radius=P(16), fill=(196, 191, 230, 255))

    # scan beam with glow
    beam = Image.new("L", (S, S), 0)
    ImageDraw.Draw(beam).rounded_rectangle((P(x0 - 70), P(624), P(x1 + 70), P(642)), radius=P(9), fill=255)
    glow = Image.new("RGBA", (S, S), (255, 128, 96, 0))
    glow.putalpha(beam.filter(ImageFilter.GaussianBlur(P(26))).point(lambda v: int(v * 0.6)))
    layer = Image.alpha_composite(layer, glow)
    core = Image.new("RGBA", (S, S), (255, 98, 72, 0))
    core.putalpha(beam)
    layer = Image.alpha_composite(layer, core)

    # folded corner shaped as a pen nib
    d = ImageDraw.Draw(layer)
    fx, fy = x1 - fold, y0
    d.polygon([(P(fx), P(fy)), (P(x1), P(fy + fold)), (P(fx), P(fy + fold))], fill=(255, 201, 124, 255))
    d.polygon([(P(fx + 8), P(fy + 26)), (P(fx + 8), P(fy + fold - 8)), (P(x1 - 26), P(fy + fold - 8))],
              fill=(255, 150, 92, 255))
    cx, cy = fx + 66, fy + fold - 66
    d.line([(P(fx + 14), P(fy + fold - 14)), (P(cx), P(cy))], fill=(118, 38, 62, 255), width=P(11))
    d.ellipse((P(cx - 18), P(cy - 18), P(cx + 18), P(cy + 18)), fill=(118, 38, 62, 255))

    # signature stroke: smooth, round-capped, tapering
    pts = []
    for i in range(220):
        t = i / 219
        x = x0 + 78 + t * 330
        y = 752 - 36 * math.sin(t * math.pi * 2.2) * (1 - 0.35 * t) - 20 * t
        pts.append((x, y))
    for i in range(len(pts) - 1):
        t = i / (len(pts) - 1)
        wdt = 22 * (0.55 + 0.45 * math.sin(math.pi * min(1, t * 1.15)))
        (ax, ay), (bx, by) = pts[i], pts[i + 1]
        d.line([(P(ax), P(ay)), (P(bx), P(by))], fill=(60, 48, 156, 255), width=max(1, P(wdt)))
        d.ellipse((P(bx - wdt / 2), P(by - wdt / 2), P(bx + wdt / 2), P(by + wdt / 2)), fill=(60, 48, 156, 255))
    return layer


def build():
    icon = Image.alpha_composite(gradient_tile(), page_layer()).resize((OUT, OUT), Image.LANCZOS)
    icon.save(HERE / "icon_1024.png")
    icon.resize((256, 256), Image.LANCZOS).save(HERE / "icon.png")
    icon.resize((64, 64), Image.LANCZOS).save(HERE / "favicon.png")
    icon.save(HERE / "icon.ico", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64),
                                         (128, 128), (256, 256)])
    sizes = (1024, 256, 64, 32, 16)
    prev = Image.new("RGBA", (sum(sizes) + 20 * (len(sizes) + 1), 1064), (236, 236, 242, 255))
    x = 20
    for sz in sizes:
        prev.alpha_composite(icon.resize((sz, sz), Image.LANCZOS), (x, 20))
        x += sz + 20
    prev.save(HERE / "icon_preview.png")
    print("icon written to", HERE)


if __name__ == "__main__":
    build()
