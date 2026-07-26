"""Host-side tool: convert a TTF/OTF into the reader's compact 1-bit
proportional bitmap font (.pf). Needs Pillow. Runs on a desktop, not the Badger.

    python tools/build_font.py <font.ttf> <out.pf> [size=13] [threshold=108]

For a variable font it selects weight 400 (Regular) and, if the font has an
optical-size axis, sets it to the pixel size; other axes keep their default.

All bundled fonts are open-licensed (SIL OFL). Sources:
  Literata     https://github.com/google/fonts/tree/main/ofl/literata
  Lexend Deca  https://github.com/google/fonts/tree/main/ofl/lexenddeca

.pf format:
  magic 4 = b"PFN1"; box_h; baseline; first_char(0x20); count; space_advance
  then `count` records of 4 bytes: advance, box_width, offset(uint16 LE)
  then per glyph: box_h rows x ceil(box_width/8) bytes, MSB first.
"""
import sys
from PIL import Image, ImageFont, ImageDraw

TTF = sys.argv[1]
OUT = sys.argv[2]
SIZE = int(sys.argv[3]) if len(sys.argv) > 3 else 13
THRESH = int(sys.argv[4]) if len(sys.argv) > 4 else 108

FIRST, LAST = 0x20, 0x7E
CHARS = [chr(c) for c in range(FIRST, LAST + 1)]

font = ImageFont.truetype(TTF, SIZE)
try:
    axes = font.get_variation_axes()
    vals = []
    for ax in axes:
        nm = ax["name"]
        nm = nm.decode("latin-1") if isinstance(nm, bytes) else str(nm)
        nm = nm.lower()
        if "weight" in nm:
            vals.append(400)
        elif "optical" in nm:
            vals.append(SIZE)
        else:
            vals.append(ax.get("default", 0))
    font.set_variation_by_axes(vals)
except Exception as e:
    print("note: not a variable font / axis set skipped:", e)

BASE = 48
CANVAS_H = 96


def render_gray(ch, w):
    img = Image.new("L", (w, CANVAS_H), 0)
    ImageDraw.Draw(img).text((0, BASE), ch, font=font, fill=255, anchor="ls")
    return img


advances = {ch: max(0, round(font.getlength(ch))) for ch in CHARS}
union_top, union_bot = CANVAS_H, 0
for ch in CHARS:
    bb = render_gray(ch, max(advances[ch] + 4, 8)).getbbox()
    if bb:
        union_top = min(union_top, bb[1])
        union_bot = max(union_bot, bb[3])
box_h = union_bot - union_top
baseline = BASE - union_top

records, bitmap = bytearray(), bytearray()
for ch in CHARS:
    adv = advances[ch]
    g = render_gray(ch, max(adv + 4, 8))
    ink = g.getbbox()
    box_w = max(adv, ink[2] if ink else 0, 1)
    rb = (box_w + 7) // 8
    off = len(bitmap)
    px = g.load()
    for ry in range(box_h):
        sy = union_top + ry
        row = bytearray(rb)
        for rx in range(box_w):
            if px[rx, sy] >= THRESH:
                row[rx >> 3] |= 0x80 >> (rx & 7)
        bitmap += row
    records += bytes([min(adv, 255), min(box_w, 255), off & 0xFF, (off >> 8) & 0xFF])

header = bytes(b"PFN1") + bytes([box_h, baseline, FIRST, len(CHARS), min(advances[" "], 255)])
open(OUT, "wb").write(header + bytes(records) + bytes(bitmap))
print(f"{TTF} size={SIZE} thresh={THRESH} box_h={box_h} baseline={baseline} "
      f"space={advances[' ']} -> {OUT} ({len(header)+len(records)+len(bitmap)} bytes)")
