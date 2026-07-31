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

## Font builders

These need Pillow (`pip install Pillow`) and are only run when changing fonts.
The generated `.pf` files are committed, so you don't need to run these to build
the project.

```
# any TTF/OTF -> the reader's 1-bit bitmap format
python3 tools/build_font.py <font.ttf> <out.pf> [size=13] [threshold=108] [weight=400]

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
