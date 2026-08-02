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
# code.py and boot.py sit at the drive root; everything else is in dot-folders
# so a mounted CIRCUITPY shows books rather than machinery.
SYSDIR = os.path.join(CPDIR, ".system")
FONTDIR = os.path.join(CPDIR, ".fonts")
sys.path.insert(0, SYSDIR)
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
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == name:
                return eval(ast.get_source_segment(src, node.value),
                            dict(env or {}))
            # tuple unpacking, e.g. BAND_Y, BAND_H, STEP = 48, 64, 8
            if isinstance(t, ast.Tuple):
                for i, el in enumerate(t.elts):
                    if isinstance(el, ast.Name) and el.id == name:
                        return eval(ast.get_source_segment(src, node.value),
                                    dict(env or {}))[i]
    raise RuntimeError(f"{name} not found")


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
    src = open(os.path.join(SYSDIR, "convert_ui.py")).read()
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
    # 68000 -> 68600: boot timing prints, added to find where startup time
    # goes, plus the constant that answered them. Diagnostic and temporary -
    # BOOT_TIMING switches the prints off, and when the question is settled
    # they should come out and this should come back down.
    #
    # 67500 -> 68000: the interface font moved from vga2_8x16 to a file-backed
    # oldmono.pf. This budget counts only code.py's own bytecode, so it sees
    # 277 bytes added and none of the 17.8KB module deleted or the ~4KB of
    # resident glyph data that went with it. Measuring one file was always a
    # proxy; here it points the wrong way, so the deletion is asserted below
    # rather than trusted to this number.
    #
    # 67000 -> 67500: recovery when a conversion boot raises. Without it the
    # board stops dead with a blinking LED and no screen, having skipped the
    # page buffers it would need to carry on; 189 bytes to restart into a
    # working reader instead is worth paying.
    #
    # A ratchet against drift, not a hardware limit. Move it deliberately, and
    # write down why - this is the third raise, which is a trend.
    #
    # The identified next move is the picker: file_picker, _draw_book_list and
    # open_picker are ~8.4KB compiled, and run only while the A button is held.
    # Lazily importing them the way convert_ui is imported would take code.py
    # well below any of these numbers. It was not done here because the gain
    # needed was 189 bytes and the picker is the part of this codebase with the
    # most recent history of subtle breakage.
    budget = 68600
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

    # The interface font must stay a .pf. vga2_8x16 held 4KB of glyph data
    # resident for the whole session, and comparing the two showed all 95
    # printable ASCII glyphs byte-identical to oldmono.pf - the same typeface
    # shipped twice, in two formats.
    assert not os.path.exists(os.path.join(SYSDIR, "vga2_8x16.py")), (
        "vga2_8x16.py is back; oldmono.pf carries the same glyphs and the "
        "module costs ~4KB of RAM for as long as the reader runs")
    for f, where in (("code.py", CPDIR), ("convert.py", SYSDIR),
                     ("convert_ui.py", SYSDIR)):
        text = open(os.path.join(where, f)).read()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "vga2_8x16" in stripped:
                raise AssertionError(f"{f} imports vga2_8x16 again: {stripped}")
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
    ui = open(os.path.join(SYSDIR, "convert_ui.py")).read()
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

def test_conversion_trigger_runs_after_everything_it_calls():
    """convert_ui reaches back into code.py, so the trigger must come last.

    Placed beside the display setup, where it went first, the progress screen
    called reader._ScratchFrame, reader.update_display_fast and
    reader.show_message before those existed. A conversion boot raised
    AttributeError before drawing anything: a blank screen and a blinking LED,
    with no serial attached to say why.

    Nothing is lost by waiting - what matters for memory is that the buffers,
    font and pattern blob are skipped far earlier.
    """
    import re
    src = open(os.path.join(CPDIR, "code.py")).read()
    ui = open(os.path.join(SYSDIR, "convert_ui.py")).read()

    lines = src.splitlines()
    # Every occurrence, not just the last: an extra call added earlier would
    # otherwise hide behind the correct one and crash exactly as before.
    triggers = [i for i, line in enumerate(lines, 1)
                if "convert_ui.run_pending" in line
                and not line.strip().startswith("#")]
    assert triggers, "code.py never runs a queued conversion"
    assert len(triggers) == 1, (
        f"a queued conversion is started from {len(triggers)} places "
        f"(lines {triggers}); only the last one is safely placed")
    trigger = triggers[0]

    defined = {}
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defined[node.name] = node.lineno
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined.setdefault(t.id, node.lineno)

    late = [n for n in sorted(set(re.findall(r"reader\.(\w+)", ui)))
            if n in defined and defined[n] > trigger]
    assert not late, (
        f"convert_ui uses {late} from code.py, but they are defined after the "
        f"trigger on line {trigger} - a conversion boot will raise before it "
        "draws anything")

    # and a failure must restart rather than stop the board dead
    # The handler has to be the one wrapping run_pending, and it has to be a
    # catch-all. Matched through the AST rather than by reading nearby text:
    # the recovery block has a second, narrower `except Exception` guarding the
    # supervisor import, and a text search happily accepts that one instead.
    guard = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Try):
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if "run_pending" in body:
                guard = node
    assert guard is not None, (
        "the queued conversion is not wrapped in a try; an unhandled error "
        "leaves a board with no page buffers and a blinking LED")

    catch_all = any(h.type is None or getattr(h.type, "id", None) == "Exception"
                    for h in guard.handlers)
    assert catch_all, (
        "the conversion trigger catches only specific errors; anything else "
        "stops the board dead with a blinking LED and no screen")
    assert "reload" in ast.dump(ast.Module(body=guard.handlers, type_ignores=[])), (
        "the recovery path does not restart the board, so it would carry on "
        "as a reader with no page buffers")
    print(f"  [ok] conversion trigger (line {trigger}) runs after all it calls")

def test_failed_conversion_does_not_become_the_active_book():
    """A conversion that produced nothing must not be opened.

    Making it active sent the reader to a blank page with nothing to turn to,
    which reads as a broken reader rather than a conversion that wrote nothing.
    """
    ui = open(os.path.join(SYSDIR, "convert_ui.py")).read()
    for node in ast.parse(ui).body:
        if isinstance(node, ast.FunctionDef) and node.name == "run_pending":
            body = node

    # state_save must be reachable only when there is a txt to open
    saves = [n for n in ast.walk(body)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "state_save"]
    assert saves, "run_pending never records the converted book"

    guarded = []
    for node in ast.walk(body):
        if isinstance(node, ast.If):
            seg = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if "state_save" in seg:
                # The condition has to be the conversion result itself. Merely
                # being inside *an* if is not enough - `if True:` keeps the
                # shape and loses the meaning.
                assert isinstance(node.test, ast.Name), (
                    "the book is recorded under a condition that does not test "
                    f"the conversion result: {ast.dump(node.test)}")
                guarded.append(node)
    assert guarded, (
        "run_pending records the book unconditionally; a failed conversion "
        "would be opened as a blank page")
    for node in guarded:
        seg = ast.dump(ast.Module(body=node.orelse, type_ignores=[]))
        assert "state_save" not in seg, (
            "the failure branch also records the book")
    print("  [ok] a failed conversion is not made the active book")

def test_convert_py_writes_nvram_the_reader_can_read():
    """convert.py records the new book without importing code.py.

    It cannot import it: not loading code.py is the entire reason convert.py
    exists. A conversion boot that still ran code.py reached the archive with
    46272 bytes free and nothing over 1KB contiguous, and failed on a 525-byte
    chapter. So the NVRAM layout is written out in both files, and the two have
    to agree - a silent disagreement would convert the book and then open the
    wrong one, or none.
    """
    import struct
    src = open(os.path.join(SYSDIR, "convert.py")).read()
    code = open(os.path.join(CPDIR, "code.py")).read()

    nvm = bytearray(4096)
    fake = type("MC", (), {})()
    fake.nvm = nvm
    writer = {"microcontroller": fake, "struct": struct}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "set_active_book":
            exec(ast.get_source_segment(src, node), writer)
    assert "set_active_book" in writer, "convert.py cannot record the new book"

    reader = {"NVM": nvm, "struct": struct, "print": lambda *a, **k: None}
    for node in ast.parse(code).body:
        if isinstance(node, ast.Assign) and getattr(
                node.targets[0], "id", "").startswith(
                    ("NVM_O_", "ENTRY_", "MAX_BOOKS", "NVRAM_MAGIC")):
            try:
                reader[node.targets[0].id] = ast.literal_eval(
                    ast.get_source_segment(code, node.value))
            except Exception:
                pass
        elif isinstance(node, ast.FunctionDef) and node.name in (
                "_get_entry_base", "_read_entry", "_find_book_index",
                "state_load_last_book", "state_load_book"):
            exec(ast.get_source_segment(code, node), reader)

    nvm[0:4] = struct.pack("<I", reader["NVRAM_MAGIC"])
    nvm[4:6] = struct.pack("<H", 0)
    nvm[6:8] = struct.pack("<H", 0)

    # Poison every entry slot with a position from some earlier book. Starting
    # from zeroed NVRAM would let "never writes the offset" pass unnoticed, and
    # the reader would then open a brand new book part-way through it.
    for i in range(reader["MAX_BOOKS"]):
        base = 8 + i * reader["ENTRY_SIZE"]
        nvm[base:base + 4] = struct.pack("<I", 123456)
        nvm[base + 4:base + 6] = struct.pack("<H", 5)
        nvm[base + 6:base + 11] = b"stale"

    for path in ("/books/The Last Town.txt", "/books/Alice.txt",
                 "/books/The Last Town.txt"):
        writer["set_active_book"](path)
        assert reader["state_load_last_book"]() == path, (
            f"convert.py recorded {path!r} but code.py opens "
            f"{reader['state_load_last_book']()!r}")
        # state_load_book returns (0, b"") when it fails internally, which is
        # the same answer as success - so prove the lookup actually found the
        # entry before trusting what it says about the position.
        assert reader["_find_book_index"](path) >= 0, (
            f"code.py cannot find {path!r} in the entry convert.py wrote")
        offset, remainder = reader["state_load_book"](path)
        assert offset == 0 and remainder == b"", (
            "a freshly converted book must open at the start, not at a "
            "position left over from a previous book in that slot")

    count = struct.unpack("<H", bytes(nvm[4:6]))[0]
    assert count == 2, (
        f"converting the same book twice made {count} entries; it should "
        "reuse the one already there")

    # and the queued-job slot must be the same one on both sides
    def const(text, name):
        for node in ast.parse(text).body:
            if isinstance(node, ast.Assign) and getattr(
                    node.targets[0], "id", None) == name:
                return ast.literal_eval(ast.get_source_segment(text, node.value))
        raise AssertionError(f"{name} missing")
    for name in ("NVM_O_PENDING", "PENDING_MAGIC", "PENDING_MAX"):
        assert const(src, name) == const(code, name), (
            f"{name} differs between convert.py and code.py, so the queued "
            "book would not be found")
    print("  [ok] convert.py and code.py agree on the NVRAM layout")

def test_only_code_and_boot_sit_at_the_drive_root():
    """Everything else lives in dot-folders, hidden from a mounted CIRCUITPY.

    CircuitPython insists on finding code.py and boot.py at the root, so those
    two stay; the rest goes into /.system and the fonts into /.fonts. macOS and
    Linux hide dot-folders; Windows uses a FAT attribute instead and shows them
    regardless, so this is tidiness, not concealment.

    The paths are absolute on the device, which means a file moved without its
    references updated fails at boot rather than in a test - hence checking
    both that the layout holds and that nothing points at the old places.
    """
    root = sorted(f for f in os.listdir(CPDIR)
                  if not f.startswith(".") and f != "__pycache__")
    assert set(root) <= {"code.py", "boot.py", "lib"}, (
        f"unexpected files at the drive root: {root} - only code.py and "
        "boot.py have to be there")

    for name in ("propfont.py", "hyphenator.py", "uc8151_circuitpython.py",
                 "epub_xtract.py", "convert.py", "convert_ui.py",
                 "coverimg.py", "uzipfile.py", "inflate.py",
                 "hyphen_patterns.txt"):
        assert os.path.exists(os.path.join(SYSDIR, name)), (
            f"{name} is not in .system")
    for name in ("oldmono.pf", "literata.pf", "lexenddeca.pf", "font5x8.bin"):
        assert os.path.exists(os.path.join(FONTDIR, name)), (
            f"{name} is not in .fonts")

    code = open(os.path.join(CPDIR, "code.py")).read()
    # the hidden folders have to be on the import path before anything from
    # them is imported, or the board does not boot at all
    first_import = min(
        (i for i, l in enumerate(code.splitlines())
         if l.startswith("import ") and l.split()[1] in
         ("propfont", "hyphenator", "adafruit_framebuf")),
        default=None)
    setup = next((i for i, l in enumerate(code.splitlines())
                  if "/.system" in l and "sys.path" not in l), None)
    setup = next(i for i, l in enumerate(code.splitlines()) if '"/.system"' in l)
    assert first_import is None or setup < first_import, (
        "code.py imports from /.system before putting it on sys.path")

    # nothing may still reference a font or the patterns at the old top level
    import re
    for where, name in ([(CPDIR, "code.py")] +
                        [(SYSDIR, f) for f in os.listdir(SYSDIR)
                         if f.endswith(".py")]):
        text = open(os.path.join(where, name)).read()
        for stale in re.findall(r'"(?!/)[A-Za-z0-9_]+\.(?:pf|bin)"', text):
            raise AssertionError(
                f"{name} still opens {stale} at the top level; the fonts are "
                "in /.fonts now")
        assert '"hyphen_patterns.txt"' not in text, (
            f"{name} still opens hyphen_patterns.txt at the top level")
    print("  [ok] only code.py and boot.py at the root; paths follow")

def test_progress_forces_a_full_refresh_periodically():
    """Partial refreshes alone let the rest of the panel fade.

    A partial update only drives the pixels in its window, and the charge
    elsewhere drifts a little each time. Across the thirty-odd updates of a
    conversion the title and counter outside the band visibly lose contrast -
    "starts fine, gets fainter". A full refresh restores them.

    This is about the converter's own screen, in convert.py, which draws
    without the reader loaded.
    """
    src = open(os.path.join(SYSDIR, "convert.py")).read()
    every = _const(src, "FULL_EVERY")
    assert 1 < every <= 12, (
        f"FULL_EVERY is {every}; too high and the screen fades, too low and "
        "the conversion spends its time refreshing")

    calls = {"partial": 0, "full": 0}
    bar = _const(src, "BAR", {"WIDTH": 296})

    class _FB:
        def fill(self, c):
            pass

        def fill_rect(self, *a):
            pass

    class _Display:
        fb = _FB()
        raw_fb = None
        rotation = 270

        def set_speed(self, speed, no_flickering=False):
            calls.setdefault("speeds", []).append((speed, no_flickering))

        def text(self, *a, **k):
            pass

        def _rotate_framebuffer(self, buf):
            return buf

        def update_partial(self, *a, **k):
            calls["partial"] += 1
            return True

        def update(self, *a, **k):
            calls["full"] += 1

    ns = {
        "WIDTH": 296, "HEIGHT": 128, "BAR": bar,
        "BAND_Y": _const(src, "BAND_Y"), "BAND_H": _const(src, "BAND_H"),
        "STEP": _const(src, "STEP"), "FULL_EVERY": every,
        "SPEED": _const(src, "SPEED"), "NO_FLICKER": _const(src, "NO_FLICKER"),
        "NOTES": _const(src, "NOTES"),
        "_state": dict(_const(src, "_state")),
        "display": _Display(), "fb": _FB(), "working": bytearray(4736),
        "print": lambda *a, **k: None,
    }
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in ("draw", "progress"):
            exec(ast.get_source_segment(src, node), ns)

    total = 75
    ns["progress"]("start", 0, total, "Book.epub")
    for i in range(total + 1):
        ns["progress"]("chapter", i, total, "Book.epub")

    # The first update after the restart has to drive every pixel. The driver
    # has just started and does not know what is on the panel, and the quick
    # waveform only moves pixels it believes changed - so the screen the reader
    # left behind shows through the progress display.
    speeds = calls.get("speeds", [])
    assert speeds and speeds[0] == (0, False), (
        f"the converter's first draw is not a full flicker refresh ({speeds[:2]}); "
        "the previous screen will show through it")
    assert (0, False) not in speeds[1:], (
        "every draw is a full flicker refresh, which is far slower than it "
        "needs to be")

    assert calls["full"] >= 2, (
        f"only {calls['full']} full refreshes across a whole conversion; the "
        "screen outside the progress band will fade away")
    assert calls["partial"] > calls["full"], (
        "more full refreshes than partial ones - each costs about a second, "
        "which the conversion has to wait for")
    # never more than FULL_EVERY partials in a row
    run = 0
    ns2 = dict(ns)
    print(f"  [ok] {calls['partial']} partial + {calls['full']} full refreshes "
          f"(a full one at least every {every})")


def test_a_conversion_refused_for_usb_stays_queued():
    """Being plugged in is a fixable refusal, not a failure.

    The queued book is normally cleared before the work, so a conversion that
    resets the board cannot repeat forever. A read-only filesystem is different:
    it is detected, reported, and fixed by unplugging - and unplugging restarts
    the board, which is exactly when it should run.
    """
    src = open(os.path.join(SYSDIR, "convert.py")).read()
    marker = src.index('_state["why"] == "readonly"')
    # up to the next branch at the same level, or this reads the else: too
    end = src.index("\n    else:", marker)
    branch = src[marker:end]
    # An actual write into nvm, not just a mention of the offset: the offset
    # name appears on several lines, so a string search passes even when the
    # assignment that matters has been removed.
    writes = 0
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.If):
            continue
        if "readonly" not in (ast.get_source_segment(src, node.test) or ""):
            continue
        for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if (isinstance(t, ast.Subscript)
                            and "nvm" in (ast.get_source_segment(src, t) or "")):
                        writes += 1
    assert writes >= 2, (
        "a conversion refused because USB holds the disk is not written back "
        f"into the queue ({writes} nvm writes in that branch), so unplugging "
        "and rebooting will not resume it")
    assert "convert.log" not in branch, (
        "the readonly branch points at a log file that could not be written - "
        "the filesystem it would live on is the one that was refused")

    # and the reader has to restart when USB goes away
    code = open(os.path.join(CPDIR, "code.py")).read()
    assert "usb_connected" in code and "_usb_was_connected" in code, (
        "the reader does not notice USB being unplugged, so a queued "
        "conversion waits for a manual reset")
    for node in ast.parse(code).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) == "_usb_was_connected":
                    break
    assert "supervisor.reload()" in code
    print("  [ok] a USB-refused conversion stays queued; unplugging restarts")

def test_no_module_level_name_is_used_before_it_exists():
    """code.py runs top to bottom, so order is correctness, not style.

    A name used at module level before it is bound raises at boot, on the
    board, with nothing on screen:

        NameError: name '_usb_was_connected' isn't defined

    Function and class bodies are exempt - they run later, when everything
    exists. A compound statement is treated as a unit: anything it binds
    anywhere counts as bound for the whole of it, which is loose but avoids
    guessing at branch order.

    This exists because a test that merely searched for the name passed: the
    line that *used* it contained it, so the missing definition looked present.
    """
    src = open(os.path.join(CPDIR, "code.py")).read()
    tree = ast.parse(src)

    bound = set(dir(__builtins__)) | {
        "__name__", "__file__", "__doc__", "__builtins__"}

    def bindings(node):
        """Every name this statement binds, not descending into def/class."""
        out = set()
        stack = [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)):
                out.add(cur.name)
                continue                     # its body runs later
            if isinstance(cur, ast.Name) and isinstance(cur.ctx, ast.Store):
                out.add(cur.id)
            elif isinstance(cur, (ast.Import, ast.ImportFrom)):
                for a in cur.names:
                    out.add((a.asname or a.name).split(".")[0])
            elif isinstance(cur, ast.ExceptHandler) and cur.name:
                out.add(cur.name)
            elif isinstance(cur, ast.comprehension):
                for sub in ast.walk(cur.target):
                    if isinstance(sub, ast.Name):
                        out.add(sub.id)
            for child in ast.iter_child_nodes(cur):
                stack.append(child)
        return out

    def reads(node):
        """Names this statement loads, not descending into def/class bodies."""
        out = set()
        stack = [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(cur, ast.Name) and isinstance(cur.ctx, ast.Load):
                out.add(cur.id)
            for child in ast.iter_child_nodes(cur):
                stack.append(child)
        return out

    problems = []
    for node in tree.body:
        here = bindings(node)
        for name in sorted(reads(node) - bound - here):
            if not name.startswith("__"):
                problems.append((node.lineno, name))
        bound |= here

    assert not problems, (
        "used at module level before being defined, which is a NameError at "
        "boot: " + ", ".join(f"{n!r} near line {ln}" for ln, n in problems[:6]))
    print(f"  [ok] module-level order is sound ({len(bound)} names bound)")


def test_tethered_conversion_is_refused_before_restarting():
    """Plugged in, do not restart - say so and keep the book queued.

    Restarting achieves nothing while the host owns the filesystem: the
    converter can only refuse. Worse, if the restart does not take - which is
    what an IDE holding the serial port does - the screen is left reading
    "Restarting..." with nothing happening at all.
    """
    src = open(os.path.join(CPDIR, "code.py")).read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "convert_epub":
            body = ast.get_source_segment(src, node)
    assert body, "convert_epub is gone"

    usb = body.find("usb_connected")
    restart = body.find("supervisor.reload()")
    # the quoted string, not the bare word - it appears in a comment too,
    # which made this pass with the message deleted
    drawn = body.find('"Restarting..."')
    assert usb != -1, (
        "convert_epub does not check whether USB is attached, so it restarts "
        "into a converter that can only refuse")
    assert usb < restart, "the USB check comes after the restart"
    assert drawn != -1, "the restart is no longer announced on the panel"
    assert usb < drawn, (
        'it draws "Restarting..." before checking USB, so a refusal leaves '
        "that on the screen")

    # the book must stay queued, so unplugging picks it up
    queued = body.find("save_pending")
    assert queued != -1 and queued < usb, (
        "the book is not queued before the USB check, so unplugging would "
        "have nothing to resume")
    seg = body[usb:restart]
    assert "clear_pending" not in seg, (
        "the tethered path clears the queued book, so unplugging will not "
        "convert it")
    print("  [ok] tethered: queued and explained, no pointless restart")

def test_first_refresh_is_not_the_slowest_waveform():
    """Boot is dominated by one e-ink refresh, not by rendering.

    Measured on the board: 1.19s to load state, 0.96s to lay out and draw the
    page, then 3.45s for the panel. That refresh has to drive every pixel - the
    driver has just started and cannot know what is on the screen - but speed 0
    is the panel's own factory table, the most thorough and the slowest. The
    computed waveforms halve their period per step and still drive every pixel
    charge-neutrally below speed 4.

    The full refresh a long press on A asks for is deliberately left at 0: the
    user is waiting for that one on purpose.
    """
    src = open(os.path.join(CPDIR, "code.py")).read()
    speed = _const(src, "FIRST_REFRESH_SPEED")
    assert 1 <= speed <= 3, (
        f"FIRST_REFRESH_SPEED is {speed}; above 3 the waveform stops being "
        "charge-neutral and will not clear the previous image, and 0 is the "
        "slow factory table this exists to avoid")

    # every first-display refresh goes through the constant
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "set_speed"):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        flicker = any(k.arg == "no_flickering"
                      and isinstance(k.value, ast.Constant)
                      and k.value.value is False
                      for k in node.keywords)
        if not flicker:
            continue
        # a literal 0 is only allowed where the user asked for it and waits
        if isinstance(arg, ast.Constant) and arg.value == 0:
            around = src.splitlines()[max(0, node.lineno - 6):node.lineno]
            assert any("asked for" in l for l in around), (
                f"line {node.lineno} does a full-flicker refresh at speed 0 "
                "without saying why; on the boot path that is 3.45 seconds")
    print(f"  [ok] first refresh at speed {speed}, not the factory table")

if __name__ == "__main__":
    test_picker_lists_unconverted_epubs_only()
    test_epub_and_its_text_agree_on_the_name()
    test_progress_bar_refreshes_are_throttled()
    test_progress_bar_fills_monotonically_and_completely()
    test_zero_chapters_does_not_divide_by_zero()
    test_reader_memory_is_freed_but_the_panel_survives()
    test_pending_conversion_round_trips_through_nvram()
    test_conversion_boot_skips_the_readers_allocations()
    test_conversion_trigger_runs_after_everything_it_calls()
    test_failed_conversion_does_not_become_the_active_book()
    test_convert_py_writes_nvram_the_reader_can_read()
    test_code_py_stays_out_of_the_readers_way()
    test_only_code_and_boot_sit_at_the_drive_root()
    test_progress_forces_a_full_refresh_periodically()
    test_a_conversion_refused_for_usb_stays_queued()
    test_tethered_conversion_is_refused_before_restarting()
    test_first_refresh_is_not_the_slowest_waveform()
    test_no_module_level_name_is_used_before_it_exists()
    print("\nALL CONVERT CHECKS PASSED")
