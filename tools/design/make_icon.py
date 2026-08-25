#!/usr/bin/env python3
"""
tools/design/make_icon.py — build the Trellis app icon.

The mark is Ryan's (2026-08-25): a trellis of cream slats on a rounded square.
The detail that makes it a trellis rather than a window grid is that the slats
STOP SHORT of the edge and float in the field — keep that.

Recoloured into the app's own palette the same day: near-black ground, copper
slats, straight out of main.css. Switching scheme is the PALETTE constant
below and nothing else.

Two densities, on purpose. macOS carries separate artwork per size, and the
full three-slat mark turns to grey mush at 16px — the size it wears in the
Finder sidebar and the window title bar. Small sizes get a two-slat drawing
that is still legibly a lattice. A retina variant always uses the drawing for
its POINT size, or the icon appears to change shape when a display does.

    python3 tools/design/make_icon.py [outdir]

Writes Trellis.icns (macOS), Trellis.ico (Windows, for later), and a 1024 PNG.
Pure Pillow — no macOS tooling, so it runs anywhere including CI.
"""
import struct
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

# --accent-lit on --bg-0, from app/static/css/main.css
PALETTE = {"ground": (20, 18, 16, 255), "slat": (212, 170, 130, 255)}

# Big Sur geometry: the body fills ~80% of the canvas and carries its own
# rounded corners. macOS neither rounds nor insets anything for you.
INSET_FRAC, RADIUS_FRAC = 0.0977, 0.1807


def mark(size=1024, n=3, slat_frac=0.030, end_inset=0.085, palette=PALETTE):
    inset = int(size * INSET_FRAC)
    body = (inset, inset, size - inset, size - inset)
    span = size - 2 * inset

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(body, int(size * RADIUS_FRAC), fill=palette["ground"])

    w = max(2, round(slat_frac * span))
    step = span / (n + 1)
    a, b = inset + end_inset * span, inset + span - end_inset * span
    for i in range(1, n + 1):
        p = inset + step * i
        d.rectangle([round(p - w / 2), round(a), round(p + w / 2), round(b)], fill=palette["slat"])
        d.rectangle([round(a), round(p - w / 2), round(b), round(p + w / 2)], fill=palette["slat"])
    return img


FULL  = dict(n=3, slat_frac=0.030, end_inset=0.085)   # 128pt and up
SMALL = dict(n=2, slat_frac=0.075, end_inset=0.12)    # 16pt and 32pt

# (icns type code, pixel size, the POINT size it represents)
ENTRIES = [
    (b"icp4",   16,  16), (b"ic11",   32,  16),
    (b"icp5",   32,  32), (b"ic12",   64,  32),
    (b"ic07",  128, 128), (b"ic13",  256, 128),
    (b"ic08",  256, 256), (b"ic14",  512, 256),
    (b"ic09",  512, 512), (b"ic10", 1024, 512),
]


def art(px, pt):
    return mark(1024, **(SMALL if pt <= 32 else FULL)).resize((px, px), Image.LANCZOS)


def main(outdir="."):
    out = Path(outdir); out.mkdir(parents=True, exist_ok=True)

    blocks = b""
    for code, px, pt in ENTRIES:
        buf = BytesIO(); art(px, pt).save(buf, format="PNG"); data = buf.getvalue()
        blocks += code + struct.pack(">I", len(data) + 8) + data
    (out / "Trellis.icns").write_bytes(b"icns" + struct.pack(">I", len(blocks) + 8) + blocks)

    art(256, 256).save(out / "Trellis.ico",
                       sizes=[(s, s) for s in (16, 32, 48, 64, 128, 256)])
    mark(1024, **FULL).save(out / "trellis-1024.png")
    print(f"wrote {out}/Trellis.icns, Trellis.ico, trellis-1024.png")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
