"""Offline test of the reader's page-navigation state machine.

    python3 tools/test_quickback.py            # every installed font
    python3 tools/test_quickback.py literata.pf

Navigation keeps three screen buffers (previous / current / next) and rotates
which is which on every page turn, so both directions are instant. That
rotation is easy to get subtly wrong - an aliased buffer, or a ready-flag that
claims a buffer holds a page it doesn't - and the symptom on the device would
be the display silently showing the wrong page.

This drives the REAL nav_page_down / nav_fast_advance / nav_page_up from
code.py. Only the things that need hardware are stubbed: rendering a page
records which page went into which buffer, and "displaying" records which
buffer was pushed to the panel. Everything else - pagination, hyphenation,
history, the buffer rotation itself - is the shipping code.

Checked after EVERY simulated button press:
  1. the displayed buffer holds exactly the current page (the screen can't lie)
  2. next_page_ready implies the next buffer really holds the next page
  3. prev_page_ready implies the prev buffer really holds the previous page
  4. the buffers never alias into the same object
"""
import ast
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import READER, available_fonts, load_engine, make_corpus


class Buf:
    """Stands in for a rotated screen buffer. The navigation code only ever
    rebinds these names, never indexes them, so an opaque object is enough -
    and it lets us record which page was drawn into it."""
    __slots__ = ("name", "page")

    def __init__(self, name):
        self.name = name
        self.page = None

    def __repr__(self):
        return f"<{self.name} {self.page}>"


class Engine:
    """Wraps the extracted code.py namespace and drives its real nav functions."""

    def __init__(self, ns, path, quick_back=True):
        self.ns = ns
        self.quick_back = quick_back

        ns["text_file"] = path
        ns["QUICK_BACK_OK"] = quick_back
        ns["page_history"] = []

        ns["current_rotated_buffer"] = Buf("A")
        ns["next_rotated_buffer"] = Buf("B")
        ns["prev_rotated_buffer"] = Buf("C") if quick_back else None

        ns["current_offset"] = 0
        ns["current_remainder"] = b""
        ns["next_page_ready"] = False
        ns["next_page_offset"] = 0
        ns["next_page_remainder"] = b""
        ns["prev_page_ready"] = False
        ns["prev_page_offset"] = 0
        ns["prev_page_remainder"] = b""

        # --- hardware stubs -------------------------------------------------
        ns["render_page_to_buffer"] = self._render
        ns["update_display_fast"] = self._display
        ns["wait_for_display"] = lambda: None
        ns["maybe_save_state"] = lambda: None
        ns["force_save_state"] = lambda: None

        self.displayed = None
        self.render_count = 0
        self.instant_backs = 0
        self.rendered_backs = 0

        # startup: draw the first page and pre-render its neighbours, exactly
        # as the MAIN section of code.py does
        self._render(0, b"", ns["current_rotated_buffer"])
        self._display(ns["current_rotated_buffer"])
        ns["prerender_next"]()
        ns["prerender_prev"]()

    # --- stubs --------------------------------------------------------------
    def _render(self, offset, remainder, target):
        target.page = (offset, remainder)
        self.render_count += 1

    def _display(self, buf, blocking=True):
        self.displayed = buf
        return False   # mimic a blocking update, so callers don't wait

    # --- state accessors ----------------------------------------------------
    def __getitem__(self, key):
        return self.ns[key]

    @property
    def page(self):
        return (self.ns["current_offset"], self.ns["current_remainder"])

    # --- actions (call the real code) ---------------------------------------
    def page_down(self, long_press=False):
        advanced, prev_came_free = self.ns["nav_page_down"]()
        if not advanced:
            return False
        if long_press:
            self.ns["nav_fast_advance"]()
        else:
            self.ns["prerender_next"]()
            if not prev_came_free:
                self.ns["prerender_prev"]()
        return True

    def page_up(self):
        before = self.render_count
        moved = self.ns["nav_page_up"]()
        if moved:
            # a quick back re-renders only the new neighbour, never the page
            # itself, so it costs strictly fewer renders than the slow path
            if self.render_count - before <= 1:
                self.instant_backs += 1
            else:
                self.rendered_backs += 1
        return moved

    # --- invariants ---------------------------------------------------------
    def check(self, where):
        ns = self.ns
        cur = self.page
        cur_buf = ns["current_rotated_buffer"]
        next_buf = ns["next_rotated_buffer"]
        prev_buf = ns["prev_rotated_buffer"]

        assert self.displayed is cur_buf, f"{where}: displaying the wrong buffer"
        assert cur_buf.page == cur, (
            f"{where}: SCREEN MISMATCH - showing {cur_buf.page}, position is {cur}")
        if ns["next_page_ready"]:
            want = (ns["next_page_offset"], ns["next_page_remainder"])
            assert next_buf.page == want, (
                f"{where}: next buffer holds {next_buf.page}, flag claims {want}")
        if ns["prev_page_ready"]:
            want = (ns["prev_page_offset"], ns["prev_page_remainder"])
            assert prev_buf.page == want, (
                f"{where}: prev buffer holds {prev_buf.page}, flag claims {want}")
        bufs = [cur_buf, next_buf] + ([prev_buf] if self.quick_back else [])
        assert len({id(b) for b in bufs}) == len(bufs), f"{where}: BUFFER ALIASING {bufs}"


def check_font(font_file):
    books = make_corpus()
    print(f"\n=== {font_file} ===")

    # random button sequences, with quick-back both enabled and disabled
    steps = 0
    for name in ("prose", "wrapped", "large", "hyphenwords"):
        path = books[name]
        for seed in range(6):
            rng = random.Random(seed)
            actions = [rng.choice(["down", "down", "down", "up", "up", "long"])
                       for _ in range(40)]
            for quick_back in (True, False):
                ns, _ = load_engine(font_file)
                e = Engine(ns, path, quick_back)
                e.check("init")
                for i, act in enumerate(actions):
                    if act == "up":
                        e.page_up()
                    else:
                        e.page_down(act == "long")
                    e.check(f"{name} seed={seed} qb={quick_back} step {i} ({act})")
                steps += len(actions)
    print(f"  {steps} navigation steps: screen always matched position, "
          f"no aliasing, ready-flags honest")

    # forward then back returns to where it started
    ns, _ = load_engine(font_file)
    e = Engine(ns, books["large"])
    start = e.page
    for _ in range(6):
        e.page_down()
        e.check("roundtrip forward")
    for _ in range(6):
        e.page_up()
        e.check("roundtrip back")
    assert e.page == start, f"round trip ended at {e.page}, expected {start}"
    print(f"  6 forward + 6 back returns to the starting page {start}")

    # quick-back really engages rather than silently falling back
    ns, _ = load_engine(font_file)
    e = Engine(ns, books["large"])
    for _ in range(8):
        e.page_down()
    for _ in range(8):
        e.page_up()
    total = e.instant_backs + e.rendered_backs
    assert total > 0, "no back presses happened"
    assert e.rendered_backs == 0, f"{e.rendered_backs} back press(es) had to re-render"
    print(f"  quick-back engaged on {e.instant_backs}/{total} back presses")

    # and it costs no extra rendering
    seq = ["down"] * 10 + ["up"] * 10
    costs = {}
    for quick_back in (True, False):
        ns, _ = load_engine(font_file)
        e = Engine(ns, books["large"], quick_back)
        base = e.render_count
        for act in seq:
            e.page_up() if act == "up" else e.page_down()
        costs[quick_back] = e.render_count - base
    assert costs[True] <= costs[False], (
        f"quick-back rendered more: {costs[True]} vs {costs[False]}")
    print(f"  renders for {len(seq)} presses: with quick-back {costs[True]}, "
          f"without {costs[False]}")


def test_previous_page_is_not_rendered_speculatively_at_boot_or_after_a_skip():
    """Two places where rendering the page behind is wasted work.

    At startup it cost 2.55s of a 9.1s boot - the slowest single step, because
    find_previous_page scans backwards from a paragraph boundary - and nobody
    picking up a book they were reading presses back first.

    After a long-press skip the same applies: someone who just jumped forward
    is going forward, and the page they skipped past costs as much to render as
    the one they asked for.

    Pressing up still works in both cases. It renders then, which is the same
    work moved to where it is wanted.
    """
    src = open(READER).read()

    # boot: the module-level pre-render must ask for next only
    lines = src.splitlines()
    boot = [i for i, l in enumerate(lines) if l.startswith("prerender_")]
    called = [lines[i].strip() for i in boot]
    assert "prerender_next()" in called, "boot no longer pre-renders the next page"
    assert "prerender_prev()" not in called, (
        "boot pre-renders the previous page again; measured at 2.55s of a 9.1s "
        "startup, in front of the first page appearing")

    for name, why in (
            ("nav_fast_advance", "a long-press skip goes forward"),):
        for node in ast.parse(src).body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                body = ast.get_source_segment(src, node)
        assert "prerender_next()" in body, f"{name} no longer pre-renders ahead"
        assert "prerender_prev()" not in body, (
            f"{name} pre-renders the page behind - {why}, and that render "
            "costs about a second with the reader unresponsive for it")
        assert "prev_page_ready = False" in body, (
            f"{name} leaves prev_page_ready as it found it; the buffer it "
            "points at is no longer the page behind")

    # but ordinary back-navigation must still pre-render, or quick-back is only
    # ever the one press after reading forward
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "nav_page_up":
            body = ast.get_source_segment(src, node)
    assert "prerender_prev()" in body, (
        "nav_page_up no longer pre-renders the page before; consecutive back "
        "presses would each render on demand and quick-back would be pointless")
    print("  [ok] no speculative back-page at boot or after a skip; "
          "quick-back intact")

def test_fast_back_jumps_without_chaining_scans():
    """Long-press back has to estimate, not walk.

    Forward, a skip runs the offset on with paginate_text and no rendering,
    which is cheap. There is no cheap backwards equivalent: find_previous_page
    scans and re-paginates to guess a single page back - measured at ~1.4s on
    the board - so 49 chained would take over a minute. It is also a heuristic
    that leaves the true page chain on the first step, so 49 would compound a
    guess 49 times.

    So the jump is estimated from the current page's byte span and snapped to a
    paragraph start. Landing a page or two out is the nature of a skip. What it
    must not do is land mid-word, or anywhere paginate_text cannot resume from.
    """
    import ast as _ast
    import tempfile

    ns, _font = load_engine(available_fonts()[0])
    body = open(make_corpus()["prose"], "rb").read()
    big = os.path.join(tempfile.mkdtemp(), "big.txt")
    open(big, "wb").write(body * 80)
    ns["text_file"] = big

    src = open(READER).read()
    scans = {"n": 0}
    real_fpp = ns["find_previous_page"]

    def counting_fpp(target):
        scans["n"] += 1
        return real_fpp(target)

    for node in _ast.parse(src).body:
        if isinstance(node, _ast.FunctionDef) and node.name == "nav_fast_back":
            exec(_ast.get_source_segment(src, node), ns)
    ns.update({"render_page_to_buffer": lambda *a: None,
               "update_display_fast": lambda *a, **k: None,
               "prerender_next": lambda: None,
               "current_rotated_buffer": None, "prev_page_ready": False,
               "find_previous_page": counting_fpp})

    off, rem = 0, b""
    for _ in range(60):
        lines, off, rem = ns["paginate_text"](big, off, rem)
        if not lines:
            break
    ns["current_offset"], ns["current_remainder"] = off, rem
    # Positions from before the jump, as ordinary reading would have left.
    ns["page_history"] = [(100, b""), (450, b""), (800, b"")]

    assert ns["nav_fast_back"](49) is True, "a long press back did nothing"
    land, lrem = ns["current_offset"], ns["current_remainder"]
    assert 0 <= land < off, f"landed at {land}, not before {off}"
    assert scans["n"] <= 1, (
        f"nav_fast_back called find_previous_page {scans['n']} times; each is "
        "~1.4s on the board, so this must estimate rather than walk back")

    data = open(big, "rb").read()
    assert land == 0 or data[land - 2:land] == b"\n\n", (
        "did not land on a paragraph start, so the page may begin mid-sentence")
    assert lrem == b"", (
        "landed with a remainder; a paragraph start has none, and a bogus one "
        "would put words from elsewhere at the top of the page")

    # the real requirement: the position is one the engine can read on from
    walked, o, r = 0, land, lrem
    while o < off and walked < 300:
        lines, o, r = ns["paginate_text"](big, o, r)
        if not lines:
            break
        walked += 1
    assert walked > 0, "cannot paginate forward from where the jump landed"
    assert abs(walked - 49) <= 10, (
        f"asked to go back 49 pages and landed {walked} away - the estimate is "
        "not tracking the real page size")

    # The page history records the way back through pages we have just jumped
    # over. Left in place, the next back press pops one of them and moves
    # FORWARD, which is the opposite of what was asked for.
    assert ns["page_history"] == [], (
        f"history survived the jump ({len(ns['page_history'])} entries); a "
        "back press would pop a position from before the jump and move forward")

    # edges: near the start it clamps, at the start it declines
    ns["current_offset"], ns["current_remainder"] = 400, b""
    ns["nav_fast_back"](49)
    assert ns["current_offset"] == 0, "a jump past the beginning did not clamp"
    ns["current_offset"], ns["current_remainder"] = 0, b""
    assert ns["nav_fast_back"](49) is False, (
        "claimed to move when already at the start of the book")

    # and the button has to reach it
    assert "nav_fast_back()" in src, "no long press is wired to the fast back"
    print(f"  [ok] long-press back jumps {walked} pages in one scan, "
          "landing on a paragraph start")

def main():
    fonts = sys.argv[1:] or available_fonts()
    if not fonts:
        print("no .pf fonts found in circuitpython_version/")
        return 1
    for f in fonts:
        check_font(f)
    # font-independent: what the reader chooses to pre-render, and when
    test_previous_page_is_not_rendered_speculatively_at_boot_or_after_a_skip()
    test_fast_back_jumps_without_chaining_scans()
    print("\nALL NAVIGATION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
