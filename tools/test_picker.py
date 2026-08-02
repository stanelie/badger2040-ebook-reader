"""Offline checks for the book picker's input loop and paging.

    python3 tools/test_picker.py

The picker draws through the display, so most of it needs hardware. Two things
can still be checked without it:

  * the paging arithmetic - the refactor replaced a stateful `offset` (only
    recomputed when the selection left the visible page) with deriving it from
    the selection, so those two must agree for every book count
  * the structure of the polling loop - the select button has to be checked
    before up/down, and the per-row page buffers must stay gone
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import CPDIR, FONTDIR, SYSDIR

PER_PAGE = 6


def picker_source():
    src = open(os.path.join(CPDIR, "code.py")).read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "file_picker":
            return node, ast.get_source_segment(src, node)
    raise AssertionError("file_picker not found")


def test_paging_matches_old_behaviour():
    """The old code carried `offset` in a variable and only moved it when the
    selection left the page. The new code derives it. Same answer, always."""
    for n_books in range(1, 26):
        selected, offset = 0, 0
        for step in range(n_books * 3):
            # old (stateful)
            if selected < offset or selected >= offset + PER_PAGE:
                offset = (selected // PER_PAGE) * PER_PAGE
            # new (derived)
            derived = (selected // PER_PAGE) * PER_PAGE
            assert offset == derived, (
                f"{n_books} books, selection {selected}: old page starts at "
                f"{offset}, new at {derived}")
            # the highlighted row must be on screen either way
            assert 0 <= selected - derived < PER_PAGE, (
                f"{n_books} books: selection {selected} is off the visible page")
            selected = (selected + 1) % n_books
    print("  [ok] derived paging matches the old stateful paging (1-25 books)")


def test_selection_wraps_both_ways():
    for n_books in (1, 5, 6, 7, 13):
        selected = 0
        selected = (selected - 1) % n_books
        assert selected == n_books - 1, f"up from the first book should wrap to the last"
        assert 0 <= selected - (selected // PER_PAGE) * PER_PAGE < PER_PAGE
        selected = (selected + 1) % n_books
        assert selected == 0, "down from the last book should wrap to the first"
    print("  [ok] selection wraps in both directions and stays on screen")


def test_select_button_checked_first():
    """A shared if/elif chain meant the select button was only read when
    neither up nor down was pressed, so a button reading as held would stop
    selection working entirely."""
    node, _ = picker_source()

    order = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                and sub.func.id == "button_pressed" and sub.args:
            arg = sub.args[0]
            if isinstance(arg, ast.Subscript) and isinstance(arg.slice, ast.Constant):
                order.append((arg.slice.value, sub.lineno))

    first_seen = {}
    for name, lineno in order:
        first_seen.setdefault(name, lineno)
    assert "a" in first_seen, "picker never checks the select button"
    for other in ("down", "up"):
        assert other in first_seen, f"picker never checks {other}"
        assert first_seen["a"] < first_seen[other], (
            f"select button is checked after {other} - a held {other} would "
            f"block selection")
    print("  [ok] select button is checked before up/down")


def test_no_page_buffer_cache():
    """The picker must not cache a rendered screen per row.

    It used to, so that moving the selection was a buffer swap. Each screen is
    4736 bytes and up to six were held at once - about 28KB - which ran the
    device out of memory as soon as there were a few books:

        MemoryError: memory allocation failed, allocating 4736 bytes

    It renders on demand instead, which became affordable once the glyph
    blitter and the rotation were sped up and partial refresh was added.
    """
    _, seg = picker_source()
    assert "selection_buffers" not in seg, (
        "picker caches a screen buffer per row again - this is what caused "
        "MemoryError once more than a couple of books were installed")
    assert "bytearray(" not in seg, (
        "picker allocates a screen-sized buffer; it should draw into the "
        "shared scratch frame")
    print("  [ok] no per-row screen cache (the ~28KB that caused MemoryError)")


def test_selection_moves_use_partial_refresh():
    """Moving the selection should refresh only the band holding the two
    highlight bars, not the whole panel."""
    node, seg = picker_source()
    calls = {c.func.attr for c in ast.walk(node)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    assert "update_partial" in calls, (
        "picker never asks for a partial refresh, so every selection move "
        "repaints the whole screen")
    assert "_draw_book_list" in seg, "picker never renders the list"
    # and it must still be able to fall back
    assert "update(" in seg, "picker has no full-refresh fallback"
    print("  [ok] selection moves use a partial refresh, with a full fallback")


def test_shared_font_buffer_loads_identically():
    """Every installed font must load byte-identically through the shared buffer.

    Fonts are read into one buffer claimed at startup, so switching allocates
    nothing - which is the whole point: a fresh ~4KB in one piece is exactly
    what a heap fragmented by a session of paginating cannot supply. It worked
    every time it was tested right after launching from the IDE, and failed
    on battery after actually reading for a while:

        font switch error: memory allocation failed, allocating 4352 bytes

    Reusing one buffer is only safe if a reload leaves no trace of the font
    before it, so each font is checked against a standalone load.
    """
    import propfont
    fonts = [f for f in os.listdir(FONTDIR) if f.endswith(".pf")]
    assert fonts, "no .pf fonts installed"
    biggest = max(os.path.getsize(os.path.join(FONTDIR, f)) for f in fonts)
    buf = bytearray(biggest)

    sample = "The quick brown fox; ijl WM 1234 -- Hamburgefonstiv"
    # cycle through more than once, so each load has to overwrite a different
    # previous font rather than a blank buffer
    for round_ in range(2):
        for name in fonts:
            path = os.path.join(FONTDIR, name)
            shared = propfont.PropFont(path, buf=buf)
            alone = propfont.PropFont(path)
            assert shared.text_width(sample) == alone.text_width(sample), (
                f"{name}: width differs when loaded through the shared buffer")
            assert (shared.box_h, shared.baseline, shared.first,
                    shared.count, shared.space_w) == (
                   alone.box_h, alone.baseline, alone.first,
                   alone.count, alone.space_w), f"{name}: header differs"
            for ch in sample:
                assert shared._rec(ch) == alone._rec(ch), (
                    f"{name}: glyph {ch!r} differs through the shared buffer")

    # the buffer must be big enough for every font, or a switch silently
    # truncates the largest one
    for name in fonts:
        size = os.path.getsize(os.path.join(FONTDIR, name))
        assert size <= len(buf), f"{name} ({size}) exceeds the buffer ({len(buf)})"

    # a short buffer must be rejected, not quietly produce a broken font
    try:
        propfont.PropFont(os.path.join(FONTDIR, fonts[0]), buf=bytearray(2))
        raise AssertionError("a 2-byte buffer was accepted as a font")
    except ValueError:
        pass

    # An unreadable font read into a buffer that still holds the PREVIOUS font
    # is the dangerous case: the read returns nothing, the old font's header is
    # still sitting there, and the magic check passes. Without a length check
    # the switch silently "succeeds" with the previous font's glyphs.
    import tempfile
    empty = os.path.join(tempfile.mkdtemp(), "empty.pf")
    open(empty, "wb").close()
    propfont.PropFont(os.path.join(FONTDIR, fonts[0]), buf=buf)   # prime it
    try:
        propfont.PropFont(empty, buf=buf)
        raise AssertionError(
            "an empty font file was accepted - the buffer still held the "
            "previous font, so the switch silently kept the old glyphs")
    except ValueError:
        pass
    print(f"  [ok] {len(fonts)} fonts load identically through one "
          f"{biggest}-byte buffer")

def test_file_backed_font_renders_identically():
    """A file-backed .pf must draw exactly what the in-memory one draws.

    The interface font is opened this way so it holds ~1KB instead of the whole
    file, replacing vga2_8x16 and its 4KB of resident glyph data. That is only
    worth anything if the pixels are the same, so every installed font is
    rendered both ways and compared byte for byte - including the justified and
    the pixel-at-a-time paths, which fetch glyphs through the same call.
    """
    import propfont
    import adafruit_framebuf as afb

    W, H = 296, 128
    fonts = sorted(f for f in os.listdir(FONTDIR) if f.endswith(".pf"))
    assert fonts, "no .pf fonts installed"

    sample = [("Select Book:  (.epub converts)", 5, 5, 1),
              ("The Last Town.txt", 5, 25, 1),
              ("inverted row", 5, 41, 0),          # colour 0 takes the slow path
              ("012345 !@#$%^&*()_+-=[]{};'", 5, 57, 1),
              ("edge", 0, 73, 1),                  # x == 0
              ("clipped at the right margin", 250, 89, 1),
              ("bottom", 2, 120, 1)]               # clipped vertically

    def render(font):
        buf = bytearray(W * H // 8)
        fb = afb.FrameBuffer(buf, W, H, afb.MHMSB)
        for s, x, y, c in sample:
            font.draw(fb, s, x, y, c)
        font.draw_justified(fb, "the quick brown fox jumps", 2, 105, 1, 290)
        return bytes(buf)

    for name in fonts:
        path = os.path.join(FONTDIR, name)
        mem = propfont.PropFont(path)
        disk = propfont.PropFont(path, file_backed=True)
        try:
            assert render(mem) == render(disk), (
                f"{name}: file-backed rendering differs from in-memory")
            for c in range(32, 127):
                assert mem._rec(chr(c)) == disk._rec(chr(c)), (
                    f"{name}: glyph record for {chr(c)!r} differs")
            assert (mem.box_h, mem.baseline, mem.space_w, mem.count) == (
                disk.box_h, disk.baseline, disk.space_w, disk.count), (
                f"{name}: metrics differ")

            # and it must actually hold less, or there is no point
            held = len(disk.d) + len(disk._gbuf)
            assert held < len(mem.d), (
                f"{name}: file-backed holds {held} bytes against {len(mem.d)} "
                "in memory - it is not saving anything")
        finally:
            disk.deinit()

    # the reading font must NOT be file-backed: the page renderer's speed comes
    # from shifting bytes out of a resident blob
    code = open(os.path.join(CPDIR, "code.py")).read()
    for line in code.splitlines():
        if "PropFont(AVAILABLE_FONTS" in line:
            assert "file_backed" not in line, (
                "the reading font is opened file-backed; page rendering would "
                "seek the flash for every glyph")
    assert "file_backed=True" in code, "the interface font is not file-backed"

    # ...and the driver has to actually be given it. Passing None is silent:
    # text() falls back to font5x8, so the chrome renders in a 5x8 cell inside
    # a layout built for 8x16 rather than failing.
    tree = ast.parse(code)
    handed = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "UC8151":
            for kw in node.keywords:
                if kw.arg == "ui_font":
                    handed = kw.value
    assert handed is not None, "the display is constructed without ui_font"
    assert not (isinstance(handed, ast.Constant) and handed.value is None), (
        "ui_font=None is passed to the display; interface text would quietly "
        "fall back to font5x8 in a layout built for 8px cells")
    print(f"  [ok] file-backed rendering is identical for {len(fonts)} fonts")

def main():
    print("book picker:")
    test_paging_matches_old_behaviour()
    test_selection_wraps_both_ways()
    test_select_button_checked_first()
    test_no_page_buffer_cache()
    test_selection_moves_use_partial_refresh()
    test_font_switch_failure_keeps_index_and_font_in_step()
    test_shared_font_buffer_loads_identically()
    test_file_backed_font_renders_identically()
    print("\nALL PICKER CHECKS PASSED")
    return 0


def test_font_switch_failure_keeps_index_and_font_in_step():
    """A font that will not load must leave the reader exactly as it was.

    cycle_font advances font_index before loading, so a failed load used to
    leave the index on the font that failed while FONT still held the previous
    one. The reported name was wrong, and the next press cycled from the wrong
    place, skipping a font entirely. Loading also held both fonts live at once,
    asking for room for two on a heap the reader has been rendering into:

        font switch error: memory allocation failed, allocating 3840 bytes
    """
    src = open(os.path.join(CPDIR, "code.py")).read()

    class Font:
        fail = set()

        def __init__(self, path, buf=None):
            if path in Font.fail:
                raise MemoryError("memory allocation failed, allocating 3840 bytes")
            self.path = path

    ns = {
        "AVAILABLE_FONTS": [("a.pf", "A"), ("b.pf", "B"), ("c.pf", "C")],
        "propfont": type("P", (), {"PropFont": Font}),
        "gc": type("G", (), {"collect": staticmethod(lambda: None)})(),
        "print": lambda *a, **k: None,
        "save_font_index": lambda i: None,
        "render_page_to_buffer": lambda *a: None,
        "update_display_fast": lambda *a, **k: None,
        "prerender_next": lambda: None,
        "prerender_prev": lambda: None,
        "current_offset": 0, "current_remainder": b"",
        "current_rotated_buffer": None,
        "font_index": 0, "FONT": Font("a.pf"),
        # shared font buffer, and the on-screen notice shown when a switch
        # fails - there is no serial on battery, so the screen is the only
        # place a failure can be reported
        "_font_buf": bytearray(8),
        "show_message": lambda *a, **k: None,
        "time": type("T", (), {"sleep": staticmethod(lambda s: None)})(),
    }
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "cycle_font":
            exec(ast.get_source_segment(src, node), ns)
    cycle_font = ns["cycle_font"]

    cycle_font()
    assert ns["font_index"] == 1 and ns["FONT"].path == "b.pf", "normal switch"

    Font.fail = {"c.pf"}
    cycle_font()
    assert ns["font_index"] == 1, (
        f"index moved to {ns['font_index']} after a failed load - it no longer "
        "matches FONT")
    assert ns["FONT"] is not None, "FONT left as None after a failed switch"
    assert ns["FONT"].path == "b.pf", f"FONT became {ns['FONT'].path}"

    Font.fail = set()
    cycle_font()
    assert ns["font_index"] == 2 and ns["FONT"].path == "c.pf", (
        "the font that failed was skipped on the next press")

    # Memory behaviour cannot be observed here - the stub allocates nothing -
    # but the ORDER decides whether a failed switch can leave the reader with
    # no font. Releasing the old font before the first attempt is cheaper and
    # was tried; it turns "the switch failed" into "there is no font", which is
    # unrecoverable. So the drop must happen only inside a MemoryError handler,
    # after the ordinary attempt has already failed.
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "cycle_font":
            body = ast.get_source_segment(src, node)
    first_load = body.find("FONT = propfont.PropFont")
    drop = body.find("FONT = None")
    assert 0 <= first_load < drop, (
        "cycle_font releases the old font before its first load attempt; a "
        "failure then leaves the reader with no font at all")

    # Whether the load reuses the shared buffer is not observable by running
    # the stub - it allocates nothing either way - but it is the entire fix.
    assert "buf=_font_buf" in body, (
        "cycle_font loads a font without the shared buffer, so a switch "
        "allocates ~4KB in one piece on a heap fragmented by reading")
    handler = body.find("except MemoryError:")
    assert 0 <= handler < drop, (
        "the old font is released outside the MemoryError fallback")
    print("  [ok] a failed font switch leaves index and FONT in step")

if __name__ == "__main__":
    sys.exit(main())
