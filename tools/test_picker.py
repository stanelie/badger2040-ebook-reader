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


def test_no_per_row_page_buffers():
    """The old picker pre-rendered a full-page copy per selectable row - up to
    six 4,736-byte buffers held at once."""
    _, seg = picker_source()
    assert "selection_buffers" not in seg, (
        "picker still builds a list of per-row page buffers (~28KB)")
    assert "bytearray(" not in seg, (
        "picker allocates a bytearray per redraw; it should reuse the shared "
        "scratch frame and the driver's rotation buffer")
    print("  [ok] no per-row page buffers (~28KB of allocations gone)")


def main():
    print("book picker:")
    test_paging_matches_old_behaviour()
    test_selection_wraps_both_ways()
    test_select_button_checked_first()
    test_no_per_row_page_buffers()
    print("\nALL PICKER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
