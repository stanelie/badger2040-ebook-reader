"""Host-side tool: repackage the old monospace vga2_8x16 bitmap font into the
reader's proportional `.pf` format, so it can be selected with the font toggle
and drawn by propfont (the current render path).

vga2_8x16 stores 256 glyphs, 16 bytes each (one byte per row, MSB = leftmost
pixel) - already the same layout the .pf bitmap section uses for an 8px-wide
glyph. We just slice the printable range and trim to the common ink rows so it
sits at the current line pitch.

    python tools/build_mono.py    ->  circuitpython_version/oldmono.pf
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "circuitpython_version", "vga2_8x16.py")
OUT = os.path.join(HERE, "..", "circuitpython_version", "oldmono.pf")

ns = {}
exec(open(SRC).read(), ns)
FONT = bytes(ns["_FONT"])
W, H = ns["WIDTH"], ns["HEIGHT"]  # 8, 16
FIRST, LAST = 0x20, 0x7E
CHARS = range(FIRST, LAST + 1)


def glyph_rows(c):
    return FONT[c * H:(c + 1) * H]  # H bytes, one row each


# trim to the union of inked rows across the printable glyphs
top, bot = H, 0
for c in CHARS:
    rows = glyph_rows(c)
    for r in range(H):
        if rows[r]:
            top = min(top, r)
            bot = max(bot, r + 1)
box_h = bot - top
baseline = box_h - (H - 12)  # informational only; draw doesn't use it

records = bytearray()
bitmap = bytearray()
for c in CHARS:
    rows = glyph_rows(c)[top:bot]
    off = len(bitmap)
    bitmap += rows                       # box_h bytes (1 byte/row, width 8)
    records += bytes([W, W, off & 0xFF, (off >> 8) & 0xFF])  # advance=8, box_w=8

header = bytes(b"PFN1") + bytes([box_h, baseline, FIRST, len(CHARS), W])
open(OUT, "wb").write(header + bytes(records) + bytes(bitmap))
print(f"box_h={box_h} (trimmed rows {top}..{bot} of {H}) advance={W} space={W}  "
      f"-> {os.path.normpath(OUT)} ({len(header)+len(records)+len(bitmap)} bytes)")
