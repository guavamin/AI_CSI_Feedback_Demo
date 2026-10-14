"""Build the window / taskbar icon (assets/icon/) from the two brand marks in assets/logos.

    ../ID_project/bin/python -m tools.make_app_icon [--dark]      # needs Pillow + SciPy (build time only)

The InterDigital mark and the Wireless Intelligence Lab mark are cut out of the official logo files, upscaled
smoothly and re-cut from a signed distance field, so the edges are clean at any size and the strokes can be
made a little finer than in the logo. Composition: the InterDigital mark over the Lab mark (as the earlier
pyoaicsinet icon), separated by a thin gap in the tile's colour, on a rounded tile (ivory as the GUI header,
or the GUI's dark background with --dark). Writes app_icon.png (256 px, what the GUIs load), app_icon_512.png
and app_icon_64.png, plus a preview sheet.
"""

from __future__ import annotations

import argparse

import numpy as np
from PIL import Image
from scipy import ndimage

from common.style import REPO

S = 2048  # working resolution; downsampled at the end (anti-aliasing)
ID_BLUE = (0, 173, 237)
LAB_PURPLE = (111, 50, 94)


def logo_alpha(path, box, pad=6):
    """Alpha of one mark from a logo file, with a transparent margin (no flat cuts at the crop edge)."""
    a = np.asarray(Image.open(REPO / path).convert("RGBA").crop(box), dtype=np.float32)[..., 3] / 255.0
    return np.pad(a, pad)


def sdf(mask_lo, scale, thin_px=0.0):
    """Signed distance (px at working scale, >0 inside) of a low-res coverage map, upscaled smoothly."""
    big = ndimage.zoom(ndimage.gaussian_filter(mask_lo, 0.6), scale, order=3) > 0.5
    return (ndimage.distance_transform_edt(big) - ndimage.distance_transform_edt(~big)) - thin_px


def coverage(sdf):
    return np.clip(sdf + 0.5, 0.0, 1.0)  # 1-px linear edge at working scale (8x downsample -> smooth)


def place(sdf, size, x, y, fill=-1e6):
    out = np.full((size, size), fill, dtype=np.float32)
    h, w = sdf.shape
    out[y:y + h, x:x + w] = sdf[: size - y, : size - x]
    return out


def rounded_rect_sdf(size, x0, y0, x1, y1, r):
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2 - r, (y1 - y0) / 2 - r
    dx, dy = np.abs(xx - cx) - hx, np.abs(yy - cy) - hy
    outside = np.hypot(np.maximum(dx, 0), np.maximum(dy, 0))
    inside = np.minimum(np.maximum(dx, dy), 0)
    return -(outside + inside - r)  # >0 inside


# Lab mark measured on logo_lab.png (logo px, sub-pixel edges): a rounded box with the "WI" cut out of it
LAB_BOX = (0.0, 0.3, 87.8, 57.0, 6.0)
LAB_LETTERS = ((9.45, 10.67, 17.8, 46.7), (29.1, 10.67, 37.45, 46.7), (49.3, 10.67, 57.7, 46.7),  # three stems
               (9.45, 38.32, 57.7, 46.7),  # their foot
               (69.6, 10.67, 78.0, 46.7))  # the I


def lab_mark(x, y, k):
    """Box and letters of the Lab mark as exact shapes at (x, y), k working px per logo px."""
    x0, y0, x1, y1, r = LAB_BOX
    box = rounded_rect_sdf(S, x + k * x0, y + k * y0, x + k * x1, y + k * y1, k * r)
    letters = np.max([rounded_rect_sdf(S, x + k * a, y + k * b, x + k * c, y + k * d, 0.6 * k)
                      for a, b, c, d in LAB_LETTERS], axis=0)
    return box, letters


def build(dark: bool) -> Image.Image:
    # ID mark: 143 x 125 px in the logo; Lab mark (purple box, the "WI" is cut out of it): 88 x 57 px
    pad = 6
    id_scale = 940 / 143
    id_sdf = sdf(logo_alpha("assets/logos/logo_id.png", (0, 0, 143, 125), pad), id_scale, thin_px=9)
    lab_scale = 820 / 88
    tile_bg = (27, 27, 26) if dark else (250, 249, 245)
    tile_edge = (58, 57, 54) if dark else (226, 223, 214)
    m = 120  # tile margin (a small shadow-free inset, as desktop icons have)
    tile = rounded_rect_sdf(S, m, m, S - m, S - m, 380)

    id_x, id_y = 330 - int(pad * id_scale), 300 - int(pad * id_scale)
    lab_x, lab_y = 880, 1090
    lab, lab_white = lab_mark(lab_x, lab_y, lab_scale)
    ident = place(id_sdf, S, id_x, id_y)

    # centre the pair in the tile (by its bounding box)
    ys, xs = np.nonzero((ident > 0) | (lab > 0))
    dy, dx = int(round(S / 2 - (ys.min() + ys.max()) / 2)), int(round(S / 2 - (xs.min() + xs.max()) / 2))
    ident, lab, lab_white = (np.roll(a, (dy, dx), axis=(0, 1)) for a in (ident, lab, lab_white))
    img = np.zeros((S, S, 4), dtype=np.float32)

    def paint(cov, rgb, alpha=1.0):
        a = cov[..., None] * alpha
        img[..., :3] = img[..., :3] * (1 - a) + np.array(rgb, dtype=np.float32) / 255.0 * a
        img[..., 3:] = img[..., 3:] * (1 - a) + a

    paint(coverage(tile), tile_edge)  # hairline edge
    paint(coverage(tile - 6), tile_bg)
    # a soft vertical light on the tile
    yy = np.linspace(0, 1, S, dtype=np.float32)[:, None]
    shade = (0.035 if not dark else 0.05) * (yy - 0.5)
    img[..., :3] = np.clip(img[..., :3] - shade[..., None] * coverage(tile - 6)[..., None], 0, 1)
    paint(coverage(lab), LAB_PURPLE)
    paint(coverage(lab_white), (255, 255, 255) if not dark else (250, 249, 245))
    paint(coverage(np.minimum(ident + 34, lab)), tile_bg)  # gap where the blue mark crosses the purple one
    paint(coverage(ident), ID_BLUE)
    img[..., 3] *= coverage(tile)  # nothing outside the tile
    return Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8), "RGBA")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dark", action="store_true", help="dark tile (the GUI's background) instead of ivory")
    ap.add_argument("--out", default=str(REPO / "assets/icon"))
    args = ap.parse_args()
    out = __import__("pathlib").Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    big = build(args.dark)
    for size, name in ((512, "app_icon_512.png"), (256, "app_icon.png"), (64, "app_icon_64.png")):
        big.resize((size, size), Image.LANCZOS).save(out / name)
    sheet = Image.new("RGBA", (512 + 256 + 64 + 48 + 32 + 5 * 24, 560), (230, 228, 222, 255))
    x = 24
    for size in (512, 256, 64, 48, 32):
        sheet.alpha_composite(big.resize((size, size), Image.LANCZOS), (x, 24))
        x += size + 24
    sheet.save(out / "preview.png")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
