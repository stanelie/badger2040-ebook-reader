"""Offline checks for wiring the EPUB converter into the picker.

Covers the two pieces that decide what the user sees: which files the picker
offers, and how often the progress bar spends a panel refresh. Both are pulled
out of the real code.py with `ast`, so these exercise the shipping code.

Run: python3 tools/test_convert.py
"""
import ast
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CPDIR = os.path.normpath(os.path.join(HERE, "..", "circuitpython_version"))
sys.path.insert(0, CPDIR)
sys.path.insert(0, HERE)

import epub_xtract  # noqa: E402


def _load(names, extra=None):
    """Exec the named top-level functions from code.py into a namespace."""
    src = open(os.path.join(CPDIR, "code.py")).read()
    ns = {"os": os, "print": lambda *a, **k: None}
    ns.update(extra or {})
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(ast.get_source_segment(src, node), ns)
    missing = [n for n in names if n not in ns]
    if missing:
        raise RuntimeError(f"could not extract {missing} from code.py")
    return ns, src


def _const(src, name, env=None):
    """Read a module-level constant out of code.py.

    eval rather than literal_eval because these are written in terms of the
    layout constants (_CONV_BAR is sized from WIDTH), and hard-coding the
    resulting numbers here would let the test keep passing after code.py
    changed them.
    """
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return eval(ast.get_source_segment(src, node.value),
                                dict(env or {}))
    raise RuntimeError(f"{name} not found in code.py")


# -----------------------------------------------------------------
def test_picker_lists_unconverted_epubs_only():
    """EPUBs appear only until their .txt exists.

    Both at once would show the same title twice, and choosing the EPUB would
    redo a conversion already sitting on disk.
    """
    tmp = tempfile.mkdtemp()
    ns, _ = _load(("list_books", "is_epub"), {"BOOK_DIR": tmp})

    for n in ("Dune.txt", "Sway.epub", "Both.txt", "Both.epub",
              "Upper.EPUB", ".hidden.txt", "notes.md"):
        open(os.path.join(tmp, n), "w").close()

    got = [b.split("/")[-1] for b in ns["list_books"]()]
    assert got == ["Both.txt", "Dune.txt", "Sway.epub", "Upper.EPUB"], got
    assert "Both.epub" not in got, "converted EPUB still offered"
    assert ".hidden.txt" not in got and "notes.md" not in got, got

    assert ns["is_epub"]("/books/A.epub") and ns["is_epub"]("/books/A.EPUB")
    assert not ns["is_epub"]("/books/A.txt")
    print("  [ok] picker lists texts, plus only the unconverted EPUBs")


def test_epub_and_its_text_agree_on_the_name():
    """The .txt the converter writes is the one list_books() hides the EPUB for.

    These are computed in different files - code.py strips ".epub" to match, the
    converter builds the path from TARGET_DIR - so a mismatch would silently
    leave the EPUB in the list forever, re-converting on every pick.
    """
    ns, _ = _load(("list_books", "is_epub"), {"BOOK_DIR": "/books"})
    for name in ("Sway.epub", "The Last Town.epub", "a.b.c.epub", "X.EPUB"):
        produced = epub_xtract.txt_path_for("/books/" + name)
        hidden_when = "/books/" + name[:-5] + ".txt"
        assert produced == hidden_when, (
            f"{name}: converter writes {produced}, picker hides on {hidden_when}")
    print("  [ok] converter output path matches what the picker suppresses")


# -----------------------------------------------------------------
class _Panel:
    """Counts refreshes, and records the bar width drawn each time."""

    def __init__(self):
        self.full = 0
        self.partial = 0
        self.bars = []


def _progress_ns():
    """_convert_progress and its helpers, with the panel and reader stubbed.

    These live in convert_ui.py rather than code.py: they run only during a
    conversion, and code.py is compiled into RAM for the whole session.
    """
    src = open(os.path.join(CPDIR, "convert_ui.py")).read()
    width = _const(open(os.path.join(CPDIR, "code.py")).read(), "WIDTH")
    env = {"WIDTH": width}
    bar = _const(src, "_CONV_BAR", env)
    panel = _Panel()

    class _Scratch:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FB:
        def fill_rect(self, x, y, w, h, c):
            # the filled part of the bar, not the four outline bars
            if x == bar[0] + 2 and y == bar[1] + 2:
                panel.cur = w

    class _Display:
        fb = _FB()
        physical_width, physical_height = 128, 296

        def text(self, *a, **k):
            pass

        def _rotate_framebuffer(self, buf):
            return "rotated"

        def update_partial(self, *a, **k):
            panel.partial += 1
            panel.bars.append(getattr(panel, "cur", 0))
            return True

    disp = _Display()

    class _Reader:
        """Stands in for code.py, which convert_ui reaches through __main__."""
        PARTIAL_UPDATES = True
        _ScratchFrame = _Scratch
        raw_working_buffer = bytearray(4736)
        display = disp
        QUICK_BACK_OK = True

        @staticmethod
        def update_display_fast(buf, blocking=True):
            panel.full += 1
            panel.bars.append(getattr(panel, "cur", 0))

        @staticmethod
        def show_message(*a, **k):
            pass

    ns = {
        "_CONV_BAR": bar,
        "_CONV_BAND_Y": _const(src, "_CONV_BAND_Y", env),
        "_CONV_BAND_H": _const(src, "_CONV_BAND_H", env),
        "_CONV_STEP": _const(src, "_CONV_STEP", env),
        "_CONV_NOTES": _const(src, "_CONV_NOTES", env),
        "_conv": dict(_const(src, "_conv", env)),
        "WIDTH": width,
        "display": disp,
        "reader": _Reader,
        "gc": type("G", (), {"collect": staticmethod(lambda: None)})(),
        "print": lambda *a, **k: None,
    }

    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in (
                "_draw_convert_screen", "_push_convert_screen",
                "_convert_progress"):
            exec(ast.get_source_segment(src, node), ns)
    return ns, panel


def test_progress_bar_refreshes_are_throttled():
    """Every chapter must not cost a panel refresh.

    A 75-chapter book moves the bar ~3px per chapter. Refreshing on each one
    spends 75 e-ink updates to show what a fraction of that shows just as well,
    and the conversion waits for every one of them.
    """
    ns, panel = _progress_ns()
    total = 75
    ns["_convert_progress"]("open", 0, 0, "The Last Town.epub")
    ns["_convert_progress"]("cover", 0, 0, "The Last Town.epub")
    ns["_convert_progress"]("start", 0, total, "The Last Town.epub")
    for i in range(total):
        ns["_convert_progress"]("chapter", i, total, "The Last Town.epub")
    ns["_convert_progress"]("chapter", total, total, "The Last Town.epub")
    ns["_convert_progress"]("done", 0, 0, "")

    refreshes = panel.full + panel.partial
    assert panel.partial < total // 2, (
        f"{panel.partial} partial refreshes for {total} chapters - throttling "
        "is not working")
    assert panel.partial > 10, (
        f"only {panel.partial} refreshes over {total} chapters - the bar would "
        "look stuck")
    # stage changes are rare and get a full refresh; chapters must not
    assert panel.full <= 6, f"{panel.full} full refreshes, expected only stages"
    print(f"  [ok] {total} chapters -> {panel.partial} partial + {panel.full} "
          f"full refreshes (not {total})")


def test_progress_bar_fills_monotonically_and_completely():
    """The bar only ever grows, and reaches the end of its track."""
    ns, panel = _progress_ns()
    bar_w = ns["_CONV_BAR"][2]
    inner = bar_w - 4

    for total in (1, 2, 14, 75, 300):
        ns2, panel2 = _progress_ns()
        ns2["_convert_progress"]("start", 0, total, "Book.epub")
        for i in range(total + 1):
            ns2["_convert_progress"]("chapter", i, total, "Book.epub")
        widths = [w for w in panel2.bars if w]
        assert widths == sorted(widths), f"total={total}: bar went backwards"
        assert max(widths) == inner, (
            f"total={total}: bar stops at {max(widths)}, track is {inner}")
        assert all(0 <= w <= inner for w in widths), f"total={total}: overflow"
    print(f"  [ok] bar grows monotonically and fills its {inner}px track")


def test_zero_chapters_does_not_divide_by_zero():
    """An EPUB with no HTML must not crash the progress screen."""
    ns, panel = _progress_ns()
    ns["_convert_progress"]("open", 0, 0, "Empty.epub")
    ns["_convert_progress"]("start", 0, 0, "Empty.epub")
    ns["_convert_progress"]("failed", 0, 0, "")
    print("  [ok] zero-chapter book draws without dividing by zero")


def test_reader_memory_is_freed_but_the_panel_survives():
    """keep_display must spare exactly what the progress bar draws through.

    Without raw_working_buffer and the driver scratches there is nothing to
    draw the bar into; without dropping the hyphenation blob and the page
    buffers there is not enough room to convert.
    """
    class FakeDisplay:
        _rotate_scratch = bytearray(8)
        _partial_scratch = bytearray(8)

    class FakeReader:
        raw_working_buffer = bytearray(8)
        current_rotated_buffer = bytearray(8)
        next_rotated_buffer = bytearray(8)
        prev_rotated_buffer = bytearray(8)
        _scratch_fb = object()
        FONT = object()
        display = FakeDisplay()

    for keep, must_survive, must_go in (
        (True,  ("raw_working_buffer", "_scratch_fb"),
                ("current_rotated_buffer", "next_rotated_buffer", "FONT")),
        (False, (),
                ("raw_working_buffer", "_scratch_fb", "FONT")),
    ):
        reader = FakeReader()
        reader.display = FakeDisplay()
        saved = sys.modules.get("__main__")
        sys.modules["__main__"] = reader
        try:
            epub_xtract.free_reader_memory(keep_display=keep)
        finally:
            if saved is not None:
                sys.modules["__main__"] = saved
        for nm in must_survive:
            assert getattr(reader, nm) is not None, (
                f"keep_display={keep}: {nm} was freed, nothing left to draw into")
        for nm in must_go:
            assert getattr(reader, nm) is None, (
                f"keep_display={keep}: {nm} survived, wasting room")
        if keep:
            assert reader.display._rotate_scratch is not None, (
                "rotation scratch freed - the progress screen cannot rotate")
        else:
            assert reader.display._rotate_scratch is None
    print("  [ok] keep_display spares the drawing buffers, frees the rest")


def test_code_py_stays_out_of_the_readers_way():
    """code.py is compiled into RAM at boot and stays there all session.

    Every byte in it is memory the reader never gets back, so work that runs
    rarely belongs in a module imported when it is needed. Adding the EPUB
    conversion UI here grew code.py by 8KB and cost this board its quick-back
    buffer - the failure surfaced as an unrelated allocation error at startup:

        quick-back disabled: not enough memory for a third page buffer
        font switch error: memory allocation failed, allocating 4352 bytes

    The budget is not a fixed truth, just a line that has to be argued with
    rather than crossed by accident. Raise it only with a reason.
    """
    # Measured as compiled bytecode, not source bytes. Source is a poor proxy:
    # the compiler drops comments, so moving 5KB of heavily-commented source
    # out of this file once changed the board's free memory by nothing at all.
    # CPython's bytecode is not CircuitPython's, but it tracks what actually
    # costs RAM - code and constants - instead of what merely reads long.
    import marshal
    src = open(os.path.join(CPDIR, "code.py")).read()
    size = len(marshal.dumps(compile(src, "code.py", "exec")))
    # 63000 -> 64500: shared font buffer, and the on-screen notice when a font
    # switch fails (no serial on battery, so a failure was otherwise a button
    # that did nothing).
    # 64500 -> 67000: queueing a conversion across a restart. The code has to
    # live here because it runs before anything else is built - that is the
    # point of it - and it buys back far more than it costs: a conversion boot
    # now skips the 31.5KB pattern blob, the font and three screen buffers.
    #
    # A ratchet against drift, not a hardware limit. Move it deliberately, and
    # write down why - two raises in a row is worth noticing.
    budget = 67000
    assert size <= budget, (
        f"code.py compiles to {size} bytes, over the {budget} budget by "
        f"{size - budget}. It is resident for the whole session - move "
        "rarely-run code into a module imported at the point of use, as "
        "convert_ui.py does.")

    # and the conversion UI must not have crept back in
    src = open(os.path.join(CPDIR, "code.py")).read()
    for leaked in ("_draw_convert_screen", "_convert_progress", "_CONV_BAR"):
        assert leaked not in src, (
            f"{leaked} is back in code.py; it belongs in convert_ui.py")
    print(f"  [ok] code.py is {size} bytes, within the {budget} budget")

def test_pending_conversion_round_trips_through_nvram():
    """The queued path must survive a restart, and never boot-loop.

    The picker records the EPUB and restarts so the conversion runs before the
    reader allocates anything - freeing memory afterwards is not equivalent,
    because it returns the bytes without closing the gaps, and the archive
    needs a 32KB window in one piece.
    """
    src = open(os.path.join(CPDIR, "code.py")).read()
    nvm = bytearray(4096)
    ns, _ = _load(("load_pending", "save_pending", "clear_pending"),
                  {"NVM": nvm,
                   "NVM_O_PENDING": _const(src, "NVM_O_PENDING"),
                   "PENDING_MAGIC": _const(src, "PENDING_MAGIC"),
                   "PENDING_MAX": _const(src, "PENDING_MAX")})

    assert ns["load_pending"]() == "", "blank NVRAM must not look like a job"

    for path in ("/books/The Last Town.epub", "/books/a.epub",
                 "/books/" + "x" * 100 + ".epub"):
        ns["save_pending"](path)
        got = ns["load_pending"]()
        assert got == path[:_const(src, "PENDING_MAX")], (
            f"queued {path!r}, read back {got!r}")
        ns["clear_pending"]()
        assert ns["load_pending"]() == "", "clear_pending left a job behind"

    # must not collide with the book entries or the font byte
    start = _const(src, "NVM_O_PENDING")
    entries_end = _const(src, "NVM_O_ENTRIES") + \
        _const(src, "MAX_BOOKS") * _const(src, "ENTRY_SIZE")
    assert start >= entries_end, (
        f"pending region at {start} overlaps book entries ending at {entries_end}")
    assert start > _const(src, "NVM_O_FONT_INDEX"), "overlaps the font byte"
    assert start + 2 + _const(src, "PENDING_MAX") <= 4096, "runs past NVRAM"

    # the flag must be cleared BEFORE the work, or a conversion that resets the
    # board is retried forever with no way to reach the picker and cancel it
    ui = open(os.path.join(CPDIR, "convert_ui.py")).read()
    for node in ast.parse(ui).body:
        if isinstance(node, ast.FunctionDef) and node.name == "run_pending":
            body = ast.get_source_segment(ui, node)
    cleared = body.find("clear_pending()")
    worked = body.find("convert_book(")
    assert 0 <= cleared < worked, (
        "run_pending converts before clearing the queued job; a conversion "
        "that resets the board would then repeat on every boot")
    print("  [ok] queued conversion round-trips, and is cleared before it runs")


def test_conversion_boot_skips_the_readers_allocations():
    """A conversion boot must not build what it will never use.

    Skipping is not the same as freeing and then converting: the 31.5KB pattern
    blob, the font and the page buffers each leave a hole behind when released,
    and it is a contiguous 32KB window the archive needs.
    """
    src = open(os.path.join(CPDIR, "code.py")).read()
    for guarded in ("hyphenator._load()",
                    "propfont.PropFont(AVAILABLE_FONTS[0][0]",
                    "current_rotated_buffer = bytearray(_BUF_SIZE)"):
        idx = src.find(guarded)
        assert idx > 0, f"{guarded} not found in code.py"
        window = src[max(0, idx - 400):idx]
        assert "PENDING_CONVERT" in window, (
            f"{guarded} is not skipped on a conversion boot - it will be "
            "allocated and then freed, leaving the hole that made the "
            "conversion fail")
    print("  [ok] a conversion boot skips the blob, the font and the page buffers")

if __name__ == "__main__":
    test_picker_lists_unconverted_epubs_only()
    test_epub_and_its_text_agree_on_the_name()
    test_progress_bar_refreshes_are_throttled()
    test_progress_bar_fills_monotonically_and_completely()
    test_zero_chapters_does_not_divide_by_zero()
    test_reader_memory_is_freed_but_the_panel_survives()
    test_pending_conversion_round_trips_through_nvram()
    test_conversion_boot_skips_the_readers_allocations()
    test_code_py_stays_out_of_the_readers_way()
    print("\nALL CONVERT CHECKS PASSED")
