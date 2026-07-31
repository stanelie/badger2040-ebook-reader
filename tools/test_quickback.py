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
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import available_fonts, load_engine, make_corpus


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


def main():
    fonts = sys.argv[1:] or available_fonts()
    if not fonts:
        print("no .pf fonts found in circuitpython_version/")
        return 1
    for f in fonts:
        check_font(f)
    print("\nALL NAVIGATION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
