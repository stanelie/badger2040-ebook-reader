"""Host-side tool: convert Literata (variable TTF) into the compact 1-bit
proportional bitmap font `circuitpython_version/literata.pf` used by the reader.

This runs on a desktop (needs Pillow), NOT on the Badger. Re-run it if you want
a different size or a different font.

    pip install Pillow
    # Literata is SIL OFL - https://github.com/google/fonts/tree/main/ofl/literata
    curl -L -o Literata-var.ttf \
      "https://raw.githubusercontent.com/google/fonts/main/ofl/literata/Literata%5Bopsz%2Cwght%5D.ttf"
    python tools/build_literata.py 12 108   # size=12px, threshold=108 (9 lines/page)

`literata.pf` format:
  magic 4 = b"PFN1"; box_h; baseline; first_char; count; space_advance
  then `count` records of 4 bytes: advance, box_width, offset(uint16 LE)
  then the bitmap section: per glyph box_h rows x ceil(box_width/8) bytes, MSB first.
"""
import os
import sys
from PIL import Image, ImageFont, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
TTF = os.environ.get("LITERATA_TTF", os.path.join(HERE, "Literata-var.ttf"))
OUT = os.path.join(HERE, "..", "circuitpython_version", "literata.pf")

SIZE = int(sys.argv[1]) if len(sys.argv) > 1 else 12  # 12px -> box_h 13 -> 9 lines/page
THRESH = int(sys.argv[2]) if len(sys.argv) > 2 else 108
FIRST, LAST = 0x20, 0x7E
CHARS = [chr(c) for c in range(FIRST, LAST + 1)]

font = ImageFont.truetype(TTF, SIZE)
try:
    font.set_variation_by_axes([SIZE, 400])  # opsz=SIZE, wght=400 (Regular)
except Exception as e:
    print("axis set failed (static font?):", e)

BASE = 48
CANVAS_H = 96


def render_gray(ch, w):
    img = Image.new("L", (w, CANVAS_H), 0)
    ImageDraw.Draw(img).text((0, BASE), ch, font=font, fill=255, anchor="ls")
    return img


# pass 1: tight ink box shared by all glyphs
union_top, union_bot = CANVAS_H, 0
advances = {}
for ch in CHARS:
    advances[ch] = max(0, round(font.getlength(ch)))
    bbox = render_gray(ch, max(advances[ch] + 4, 8)).getbbox()
    if bbox:
        union_top = min(union_top, bbox[1])
        union_bot = max(union_bot, bbox[3])
box_h = union_bot - union_top
baseline = BASE - union_top

# pass 2: pack glyphs
records, bitmap = [], bytearray()
for ch in CHARS:
    adv = advances[ch]
    g = render_gray(ch, max(adv + 4, 8))
    ink = g.getbbox()
    box_w = max(adv, ink[2] if ink else 0, 1)
    row_bytes = (box_w + 7) // 8
    off = len(bitmap)
    px = g.load()
    for ry in range(box_h):
        sy = union_top + ry
        row = bytearray(row_bytes)
        for rx in range(box_w):
            if px[rx, sy] >= THRESH:
                row[rx >> 3] |= 0x80 >> (rx & 7)
        bitmap += row
    records.append((min(adv, 255), min(box_w, 255), off))

header = bytearray(b"PFN1") + bytes([box_h, baseline, FIRST, len(CHARS), min(advances[" "], 255)])
recs = bytearray()
for adv, bw, off in records:
    recs += bytes([adv, bw, off & 0xFF, (off >> 8) & 0xFF])
open(OUT, "wb").write(bytes(header) + bytes(recs) + bytes(bitmap))
print(f"SIZE={SIZE} THRESH={THRESH} box_h={box_h} baseline={baseline} "
      f"space={advances[' ']}  ->  {os.path.normpath(OUT)} "
      f"({len(header)+len(recs)+len(bitmap)} bytes)")
