"""Draw the app icon: two cards and a green load badge.

    py tools/make_icon.py

Writes `assets/bml.ico` (16 to 256px) and `assets/bml.png`. Both are committed,
so Pillow is only needed to change the icon. Each size is drawn at its own
scale with 4x supersampling, and the smallest sizes drop the detail.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZES = (16, 24, 32, 48, 64, 128, 256)

# The window's palette, so the taskbar icon and the app agree.
PANEL = (43, 45, 49, 255)      # theme.PANEL
INK = (30, 31, 34, 255)        # theme.INK
GREEN = (35, 165, 90, 255)     # theme.GREEN
CARD = (219, 222, 225, 255)    # theme.TEXT, as the card face
CARD_BACK = (74, 77, 85, 255)  # theme.RAISED
WHITE = (255, 255, 255, 255)

SS = 4  # supersampling factor


def _rounded(draw, box, radius, fill=None, outline=None, width=1) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline,
                           width=max(1, int(width)))


def draw_icon(size: int):
    """One icon at one size. All geometry is a fraction of `size`."""
    from PIL import Image, ImageDraw

    # Below 32px: one upright card and a solid badge.
    tiny = size < 32
    scale = size * SS
    unit = scale / 100.0
    image = Image.new("RGBA", (scale, scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    border = 3.0 * unit if tiny else 3.5 * unit
    _rounded(draw, (border / 2, border / 2, scale - border / 2, scale - border / 2),
             radius=int((14 if tiny else 18) * unit), fill=PANEL,
             outline=GREEN, width=int(border))

    def card(colour, angle, offset_x, offset_y, w, h, pip=0.0):
        layer = Image.new("RGBA", (scale, scale), (0, 0, 0, 0))
        pen = ImageDraw.Draw(layer)
        left = (50 - w / 2 + offset_x) * unit
        top = (50 - h / 2 + offset_y) * unit
        _rounded(pen, (left, top, left + w * unit, top + h * unit),
                 radius=int(5 * unit), fill=colour, outline=INK,
                 width=max(1, int(1.6 * unit)))
        if pip:
            # A diamond survives being six pixels tall.
            cx, cy = left + w * unit / 2, top + h * unit / 2
            r = pip * unit
            pen.polygon([(cx, cy - r), (cx + r * 0.72, cy),
                         (cx, cy + r), (cx - r * 0.72, cy)], fill=GREEN)
        if not angle:
            return layer
        return layer.rotate(angle, resample=Image.BICUBIC,
                            center=(scale / 2, scale / 2))

    if tiny:
        image.alpha_composite(card(CARD, 0, -6, -4, 44, 58, pip=11))
    else:
        image.alpha_composite(card(CARD_BACK, 16, -7, -2, 38, 54))
        image.alpha_composite(card(CARD, -9, 4, 1, 38, 54, pip=9))

    # The load badge: green disc, bottom right, with a chevron pointing down.
    r = (20 if tiny else 17) * unit
    cx, cy = (74 if tiny else 72) * unit, (76 if tiny else 74) * unit
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=GREEN, outline=INK,
                 width=max(1, int((2.4 if tiny else 1.8) * unit)))
    if not tiny:
        arm = r * 0.46
        draw.line([(cx - arm, cy - arm * 0.45), (cx, cy + arm * 0.55),
                   (cx + arm, cy - arm * 0.45)],
                  fill=WHITE, width=max(1, int(2.6 * unit)), joint="curve")

    return image.resize((size, size), Image.LANCZOS)


def main() -> int:
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Pillow is needed to draw the icon:  py -m pip install pillow")
        return 1

    ASSETS.mkdir(parents=True, exist_ok=True)
    images = [draw_icon(size) for size in SIZES]
    ico = ASSETS / "bml.ico"
    images[-1].save(ico, format="ICO",
                    sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    png = ASSETS / "bml.png"
    images[-1].save(png, format="PNG")
    print(f"wrote {ico}  ({', '.join(str(s) for s in SIZES)})")
    print(f"wrote {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
