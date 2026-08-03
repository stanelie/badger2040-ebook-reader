# tools/

Host-side scripts. None of these run on the Badger — they run on a desktop with
regular Python 3.

## Tests

The reader's layout and navigation logic is pure Python, so most of it can be
exercised without hardware. Both harnesses pull the real functions out of
`circuitpython_version/code.py` with `ast` and run them, so they always test the
code that actually ships rather than a copy.

If you add a function that these should cover, add its name to `EXTRACT` in
`_harness.py`. Keep hardware access (panel, framebuffer, NVRAM, buttons) out of
the logic functions so they stay testable — the harnesses stub those.

Only the standard library is needed (no Pillow, no hardware):

```
python3 tools/test_reflow.py          # text layout + pagination
python3 tools/test_quickback.py       # page-navigation state machine
python3 tools/test_power.py           # sleep / inactivity behaviour
python3 tools/test_picker.py          # book picker paging + input loop
python3 tools/test_display.py         # e-ink rotation + partial-update windows
python3 tools/test_epub.py            # EPUB -> text converter
python3 tools/test_convert.py         # converting from the picker + progress bar
```

Both run against every installed `.pf` font by default; pass a font filename to
narrow it (`python3 tools/test_reflow.py literata.pf`).

### test_reflow.py

Checks the invariants that keep reading position trustworthy. `paginate_text`
returns a `(next_offset, remainder)` pair that gets written to NVRAM as the
saved position and re-run by `find_previous_page` for back navigation, so it has
to be exactly reproducible.

- no line exceeds the text area, and no page exceeds `LINES_PER_PAGE`
  (extra lines would be drawn off the bottom of the screen and silently lost)
- hyphenation never splits a word across a *page* boundary, which would leave
  half a word in the saved remainder
- every page redraws identically from its own `(offset, remainder)` — this is
  exactly what resume-after-sleep depends on
- the book's words all survive, in order, nothing lost or duplicated
- back navigation always moves about a page, never just a few lines

### test_quickback.py

Drives the real `nav_page_down` / `nav_fast_advance` / `nav_page_up` from
`code.py` through random button sequences, checking after **every** press that
the displayed buffer really holds the current page, that the ready-flags never
lie about what a buffer contains, and that the three screen buffers never alias
into the same object.

Only the hardware is stubbed: "rendering" a page records which page went into
which buffer, and "displaying" records which buffer was pushed to the panel.
The pagination, hyphenation, history and buffer-rotation logic under test is
the shipping code.

This is why the navigation state transitions live in `nav_*` functions instead
of inline in the main loop — the main loop only polls buttons and times presses,
which is the part that genuinely needs hardware.

### test_power.py

Battery life depends on the device actually powering down when left alone, and
any loop that polls buttons on its own has to honour the timeout itself. Drives
the real `check_inactivity()` with the clock and battery stubbed: stays awake
before the timeout, sleeps once after it, defers while charging, and resets on a
button press. Also checks that sleeping with no book open doesn't write a
phantom NVRAM entry, and structurally that `file_picker` still calls
`check_inactivity` and refreshes `last_activity` — the picker runs its own
polling loop, and originally did neither.

### test_picker.py

Proves the picker's derived paging matches the stateful version it replaced for
every book count from 1 to 25, and structurally that the select button is read
before up/down (in a shared if/elif chain, a button reading as held would block
selection entirely) and that the per-row page buffers stay gone.

### test_display.py

The SPI conversation with the panel can only be verified on hardware; what this
checks is everything deciding *which* bytes get sent. The 270° rotation is
compared against a plain reference implementation, and for partial updates the
bytes gathered for a region must be exactly the bytes that region occupies in a
full-screen update, with the PTL registers describing the same rectangle.

Partial updates address the panel in 8-pixel banks, so a requested band is
snapped **outward**. The picker's highlight bands end at y=39/55/71 — snapping
inward instead would quietly clip the bottom of the bar, so regions with
unaligned ends are in the test set specifically.

### test_epub.py

Builds real EPUB files with the standard library's `zipfile` and runs the
converter over them, so the ZIP parsing, HTML stripping and cover discovery are
all exercised for real - CircuitPython's `zlib` takes the same negative-wbits
argument for raw DEFLATE as CPython's, so it is the same decompression path.

Covers the three ways an EPUB can declare its cover (EPUB3 `properties`, EPUB2
`<meta name="cover">`, and plain filename), that Calibre `_split_NNN` chapters
sort numerically rather than lexically, and that the converted text paginates
cleanly through the reader's own engine.

It also checks `inflate.py`, the pure-Python streaming DEFLATE decoder used
when no free block is big enough for `zlib.decompress`'s output, against zlib
across stored / fixed-Huffman / dynamic-Huffman blocks, overlapping copies and
window-crossing matches — and that forcing the fallback gives byte-identical
results to the fast path.

### test_power.py additions

Covers the sleep screen: that a prepared frame is shown, that a missing or
wrong-sized one falls back to the "Sleeping..." message rather than leaving the
page up - a sleeping board has to be distinguishable from an awake one - and
that it is drawn with a full flicker refresh - the image sits there with the
power off, so a quick update's ghosting would stay for as long as the board
sleeps. Also checks the cover fitting: letterboxed whole rather than cropped,
and dithered, since thresholding a cover on a two-level panel gives a
silhouette (a 25% grey must ink about 25% of the screen, not none of it).

### test_convert.py

Covers wiring the converter into the picker: that an EPUB is offered only until
its `.txt` exists, that the path the converter writes is exactly the one the
picker suppresses the EPUB on (they are computed in two different files, and a
mismatch would silently re-convert on every pick), and that the progress bar
throttles its refreshes - a 75-chapter book redraws about 26 times, not 75,
because each e-ink update is time the conversion is not using.

Also checks `free_reader_memory(keep_display=True)` spares exactly the buffers
the progress screen draws through while still releasing the rest, and that
`vga2_8x16.py` stays deleted - its 95 printable glyphs were byte-identical to
`oldmono.pf`, so the same typeface was shipping twice and costing ~4KB of RAM
for the copy nobody needed.

The reader itself is `circuitpython_version/.system/reader.py`; `code.py` is a
shim so the reader can ship precompiled. The harness exports `READER` for it,
and the tests read that rather than `code.py`.

It also walks the reader's module level in order and fails on any name read
before it is bound. That is a NameError at boot, on the board, with nothing on
screen - and it shipped once, because the test guarding it searched for the
name and found the line that *used* it.

And it holds `code.py` to a size budget. code.py is compiled into RAM when the
board boots and stays resident for the whole session, so every byte in it is
memory the reader never gets back - adding the conversion UI there once grew it
by 8KB and cost the board its quick-back buffer, surfacing as an unrelated
allocation failure at startup. Rarely-run code belongs in a module imported at
the point of use, which is what `convert_ui.py` is.

## Font builders

These need Pillow (`pip install Pillow`) and are only run when changing fonts.
The generated `.pf` files are committed, so you don't need to run these to build
the project.

```
# any TTF/OTF -> the reader's 1-bit bitmap format
python3 tools/build_font.py <font.ttf> <out.pf> [size=13] [threshold=108] [weight=400] [maxbox=15]

# repackage the original vga2_8x16 monospace font into the same format
python3 tools/build_mono.py
```

The fonts currently shipped were built with:

```
python3 tools/build_font.py Literata-var.ttf    ../circuitpython_version/literata.pf   13 108 400
python3 tools/build_font.py LexendDeca-var.ttf  ../circuitpython_version/lexenddeca.pf 13 108 500
python3 tools/build_mono.py
```

Literata and Lexend Deca are SIL OFL; their licenses are in
`circuitpython_version/`. A slightly heavier weight (500) is used for Lexend
because its thin stems rasterise unevenly to 1-bit at this size.

### Character coverage

The fonts cover U+0020–U+00FF, so accented text (French, Spanish, German, …)
renders directly instead of falling back to `?`. Two rules keep that from
disturbing the layout:

* **Accented capitals are stored as the plain letter** (É as E). Their
  diacritics sit above cap height and would force a 17px glyph box instead of
  15px, which at the 14px line pitch collides with descenders on the line
  above. French routinely drops accents on capitals anyway.
* **`maxbox` caps the glyph box.** If a font's natural extent still exceeds it,
  rows are dropped from the top — never the bottom, so descenders such as the
  cedilla French needs survive. Lexend loses one row this way, off the
  Scandinavian `å` ring.

Characters outside Latin-1 are mapped in `clean_word()` in `code.py`: the oe
ligature becomes `oe`, curly quotes and dashes become ASCII, and a
non-breaking space becomes a normal one.

The old monospace font is CP437, not Latin-1 (`0x82` is `é`), so
`build_mono.py` looks each codepoint up through the `cp437` codec. CP437 lacks
ten uppercase accented letters; those fall back to the unaccented letter.
