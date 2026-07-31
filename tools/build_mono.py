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
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "circuitpython_version", "vga2_8x16.py")
OUT = os.path.join(HERE, "..", "circuitpython_version", "oldmono.pf")

ns = {}
exec(open(SRC).read(), ns)
FONT = bytes(ns["_FONT"])
W, H = ns["WIDTH"], ns["HEIGHT"]  # 8, 16

# The .pf index is Unicode (U+0020..U+00FF) so accented text renders, but this
# font's upper half is CP437, not Latin-1 - 0x82 is 'é', 0xE9 is a Greek theta.
# So each Unicode codepoint is looked up through the cp437 codec. CP437 is
# missing ten uppercase accented letters (À Â È Ê Ë Î Ï Ô Ù Û); those fall back
# to the unaccented letter, which reads far better than '?'.
FIRST, LAST = 0x20, 0xFF
CONTROL = range(0x7F, 0xA0)
CHARS = range(FIRST, LAST + 1)


def strip_accent(ch):
    stripped = "".join(c for c in unicodedata.normalize("NFD", ch)
                       if not unicodedata.combining(c))
    return stripped if len(stripped) == 1 else ch


def source_index(code):
    """Index into this font's CP437 glyph table for a Unicode codepoint."""
    if code in CONTROL:
        return None
    ch = chr(code)

    # Capitals are stored unaccented, matching build_font.py: their diacritics
    # would sit above cap height, and this keeps all three fonts consistent.
    if ch.isupper():
        ch = strip_accent(ch)

    try:
        return ch.encode("cp437")[0]
    except UnicodeEncodeError:
        pass
    # Not in CP437 - fall back to the base letter if we can
    base = strip_accent(ch)
    if base != ch:
        try:
            return base.encode("cp437")[0]
        except UnicodeEncodeError:
            pass
    return None


def glyph_rows(c):
    if c is None:
        return bytes(H)  # blank
    return FONT[c * H:(c + 1) * H]  # H bytes, one row each


# trim to the union of inked rows across the printable glyphs
top, bot = H, 0
for code in CHARS:
    rows = glyph_rows(source_index(code))
    for r in range(H):
        if rows[r]:
            top = min(top, r)
            bot = max(bot, r + 1)
box_h = bot - top
baseline = box_h - (H - 12)  # informational only; draw doesn't use it

records = bytearray()
bitmap = bytearray()
n_fallback = 0
for code in CHARS:
    src = source_index(code)
    if src is not None and code not in CONTROL:
        try:
            if chr(code).encode("cp437")[0] != src:
                n_fallback += 1
        except UnicodeEncodeError:
            n_fallback += 1
    rows = glyph_rows(src)[top:bot]
    off = len(bitmap)
    bitmap += rows                       # box_h bytes (1 byte/row, width 8)
    records += bytes([W, W, off & 0xFF, (off >> 8) & 0xFF])  # advance=8, box_w=8

header = bytes(b"PFN1") + bytes([box_h, baseline, FIRST, len(CHARS), W])
open(OUT, "wb").write(header + bytes(records) + bytes(bitmap))
print(f"box_h={box_h} (trimmed rows {top}..{bot} of {H}) advance={W} space={W} "
      f"glyphs={len(CHARS)} accent-stripped fallbacks={n_fallback}  "
      f"-> {os.path.normpath(OUT)} ({len(header)+len(records)+len(bitmap)} bytes)")
