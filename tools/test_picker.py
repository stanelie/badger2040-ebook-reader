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
from _harness import CPDIR

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


def main():
    print("book picker:")
    test_paging_matches_old_behaviour()
    test_selection_wraps_both_ways()
    test_select_button_checked_first()
    test_no_page_buffer_cache()
    test_selection_moves_use_partial_refresh()
    test_font_switch_failure_keeps_index_and_font_in_step()
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

        def __init__(self, path):
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

    # Whether both fonts are live at once is a memory property, and the stub
    # above allocates nothing, so it cannot be observed by running the code -
    # but it decides whether a switch needs room for one font or two. Pinned
    # structurally instead: the old one must be dropped before the new load.
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "cycle_font":
            body = ast.get_source_segment(src, node)
    drop = body.find("FONT = None")
    load = body.find("FONT = propfont.PropFont")
    assert 0 <= drop < load, (
        "cycle_font loads the new font while the old one is still referenced, "
        "so a switch needs room for two fonts instead of one")
    print("  [ok] a failed font switch leaves index and FONT in step")

if __name__ == "__main__":
    sys.exit(main())
