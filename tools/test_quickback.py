"""Offline simulation of the reader's page-navigation state machine.

    python tools/test_quickback.py            # every installed font
    python tools/test_quickback.py literata.pf

Navigation keeps three screen buffers (previous / current / next) and rotates
which is which on every page turn, so that both directions are instant. That
rotation is easy to get subtly wrong - an aliased buffer, or a ready-flag that
claims a buffer holds a page it doesn't - and the symptom would be the display
silently showing the wrong page. paginate_text and find_previous_page are the
real ones from code.py; the buffer bookkeeping in the main loop is mirrored
here (it lives inline in the `while True:` loop, so it can't be imported).

Checked after EVERY simulated button press:
  1. the displayed buffer holds exactly the current page (the screen can't lie)
  2. next_page_ready implies the next buffer really holds the next page
  3. prev_page_ready implies the prev buffer really holds the previous page
  4. the buffers never alias into the same object

If you change the navigation logic in code.py, mirror it in Reader below.
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import available_fonts, load_engine, make_corpus

PAGE_HISTORY_SIZE = 10  # mirrors code.py


class Buf:
    """A screen buffer; .page records which page is currently drawn in it."""
    __slots__ = ("name", "page")

    def __init__(self, name):
        self.name = name
        self.page = None

    def __repr__(self):
        return f"<{self.name} {self.page}>"


class Reader:
    """Mirror of the navigation state machine in code.py's main loop."""

    def __init__(self, ns, path, quick_back=True):
        self.ns = ns
        self.paginate_text = ns["paginate_text"]
        self.find_previous_page = ns["find_previous_page"]
        self.path = path
        ns["text_file"] = path
        self.QUICK_BACK_OK = quick_back

        self.cur_buf = Buf("A")
        self.next_buf = Buf("B")
        self.prev_buf = Buf("C") if quick_back else None

        self.current_offset = 0
        self.current_remainder = b""
        self.next_page_ready = False
        self.next_page_offset = 0
        self.next_page_remainder = b""
        self.prev_page_ready = False
        self.prev_page_offset = 0
        self.prev_page_remainder = b""
        self.history = []

        self.displayed = None
        self.render_count = 0
        self.instant_backs = 0
        self.rendered_backs = 0

        self.render(self.cur_buf, self.current_offset, self.current_remainder)
        self.display(self.cur_buf)
        self.prerender_next()
        self.prerender_prev()

    # --- primitives ------------------------------------------------------
    def render(self, buf, off, rem):
        buf.page = (off, rem)
        self.render_count += 1

    def display(self, buf):
        self.displayed = buf

    def history_push(self, off, rem):
        self.history.append((off, rem))
        if len(self.history) > PAGE_HISTORY_SIZE:
            self.history.pop(0)

    def history_pop(self):
        return self.history.pop() if self.history else None

    def history_peek(self):
        return self.history[-1] if self.history else None

    # --- mirrors of prerender_next / prerender_prev ----------------------
    def prerender_next(self):
        lines, next_off, next_rem = self.paginate_text(
            self.path, self.current_offset, self.current_remainder)
        if lines and next_off > self.current_offset:
            self.next_page_offset = next_off
            self.next_page_remainder = next_rem
            self.render(self.next_buf, next_off, next_rem)
            self.next_page_ready = True
        else:
            self.next_page_ready = False

    def prerender_prev(self):
        self.prev_page_ready = False
        if not self.QUICK_BACK_OK or self.current_offset <= 0:
            return
        pos = self.history_peek()
        if pos is None:
            pos = self.find_previous_page(self.current_offset)
        if not pos:
            return
        p_off, p_rem = pos
        if p_off == self.current_offset and p_rem == self.current_remainder:
            return
        self.prev_page_offset = p_off
        self.prev_page_remainder = p_rem
        self.render(self.prev_buf, p_off, p_rem)
        self.prev_page_ready = True

    # --- mirrors of the PAGE DOWN / PAGE UP handlers ---------------------
    def page_down(self, long_press=False):
        page_advanced = False
        prev_came_free = False

        if self.next_page_ready:
            self.history_push(self.current_offset, self.current_remainder)
            leaving = (self.current_offset, self.current_remainder)
            if self.QUICK_BACK_OK:
                self.prev_buf, self.cur_buf, self.next_buf = (
                    self.cur_buf, self.next_buf, self.prev_buf)
                self.prev_page_offset, self.prev_page_remainder = leaving
                self.prev_page_ready = True
                prev_came_free = True
            else:
                self.cur_buf, self.next_buf = self.next_buf, self.cur_buf
            self.current_offset = self.next_page_offset
            self.current_remainder = self.next_page_remainder
            self.display(self.cur_buf)
            self.next_page_ready = False
            page_advanced = True
        elif self.current_offset >= 0:
            lines, next_off, next_rem = self.paginate_text(
                self.path, self.current_offset, self.current_remainder)
            if lines and next_off > self.current_offset:
                self.history_push(self.current_offset, self.current_remainder)
                leaving = (self.current_offset, self.current_remainder)
                if self.QUICK_BACK_OK:
                    self.prev_buf, self.cur_buf = self.cur_buf, self.prev_buf
                    self.prev_page_offset, self.prev_page_remainder = leaving
                    self.prev_page_ready = True
                    prev_came_free = True
                self.current_offset = next_off
                self.current_remainder = next_rem
                self.render(self.cur_buf, self.current_offset, self.current_remainder)
                self.display(self.cur_buf)
                page_advanced = True

        if not page_advanced:
            return False

        if long_press:
            for i in range(49):
                lines, next_off, next_rem = self.paginate_text(
                    self.path, self.current_offset, self.current_remainder, False)
                if not lines or next_off <= self.current_offset:
                    break
                if i % 10 == 0:
                    self.history_push(self.current_offset, self.current_remainder)
                self.current_offset = next_off
                self.current_remainder = next_rem
            self.render(self.cur_buf, self.current_offset, self.current_remainder)
            self.display(self.cur_buf)
            self.prerender_next()
            self.prerender_prev()
        else:
            self.prerender_next()
            if not prev_came_free:
                self.prerender_prev()
        return True

    def page_up(self):
        if self.current_offset <= 0:
            return False
        prev = self.history_pop()
        if not prev:
            prev = self.find_previous_page(self.current_offset)

        next_came_free = False
        if (self.QUICK_BACK_OK and self.prev_page_ready
                and self.prev_page_offset == prev[0]
                and self.prev_page_remainder == prev[1]):
            self.next_buf, self.cur_buf, self.prev_buf = (
                self.cur_buf, self.prev_buf, self.next_buf)
            self.next_page_offset = self.current_offset
            self.next_page_remainder = self.current_remainder
            self.next_page_ready = True
            next_came_free = True
            self.current_offset, self.current_remainder = prev
            self.prev_page_ready = False
            self.display(self.cur_buf)
            self.instant_backs += 1
        else:
            self.current_offset, self.current_remainder = prev
            self.render(self.cur_buf, self.current_offset, self.current_remainder)
            self.display(self.cur_buf)
            self.rendered_backs += 1

        if not next_came_free:
            self.prerender_next()
        self.prerender_prev()
        return True

    # --- invariants ------------------------------------------------------
    def check(self, where):
        cur = (self.current_offset, self.current_remainder)
        assert self.displayed is self.cur_buf, f"{where}: displaying the wrong buffer"
        assert self.cur_buf.page == cur, (
            f"{where}: SCREEN MISMATCH - showing {self.cur_buf.page}, position is {cur}")
        if self.next_page_ready:
            assert self.next_buf.page == (self.next_page_offset, self.next_page_remainder), (
                f"{where}: next buffer holds {self.next_buf.page}, flag claims "
                f"{(self.next_page_offset, self.next_page_remainder)}")
        if self.prev_page_ready:
            assert self.prev_buf.page == (self.prev_page_offset, self.prev_page_remainder), (
                f"{where}: prev buffer holds {self.prev_buf.page}, flag claims "
                f"{(self.prev_page_offset, self.prev_page_remainder)}")
        bufs = [self.cur_buf, self.next_buf]
        if self.QUICK_BACK_OK:
            bufs.append(self.prev_buf)
        assert len({id(b) for b in bufs}) == len(bufs), f"{where}: BUFFER ALIASING {bufs}"


def check_font(font_file):
    ns, font = load_engine(font_file)
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
                r = Reader(ns, path, quick_back)
                r.check("init")
                for i, act in enumerate(actions):
                    if act == "up":
                        r.page_up()
                    else:
                        r.page_down(act == "long")
                    r.check(f"{name} seed={seed} qb={quick_back} step {i} ({act})")
                steps += len(actions)
    print(f"  {steps} navigation steps: screen always matched position, "
          f"no aliasing, ready-flags honest")

    # forward then back returns to where it started
    r = Reader(ns, books["large"])
    start = r.cur_buf.page
    for _ in range(6):
        r.page_down()
        r.check("roundtrip forward")
    for _ in range(6):
        r.page_up()
        r.check("roundtrip back")
    assert r.cur_buf.page == start, f"round trip ended at {r.cur_buf.page}, expected {start}"
    print(f"  6 forward + 6 back returns to the starting page {start}")

    # quick-back really engages rather than silently falling back
    r = Reader(ns, books["large"])
    for _ in range(8):
        r.page_down()
    for _ in range(8):
        r.page_up()
    total = r.instant_backs + r.rendered_backs
    assert total > 0, "no back presses happened"
    assert r.rendered_backs == 0, f"{r.rendered_backs} back press(es) had to re-render"
    print(f"  quick-back engaged on {r.instant_backs}/{total} back presses")

    # and it costs no extra rendering
    seq = ["down"] * 10 + ["up"] * 10
    costs = {}
    for quick_back in (True, False):
        r = Reader(ns, books["large"], quick_back)
        base = r.render_count
        for act in seq:
            r.page_up() if act == "up" else r.page_down()
        costs[quick_back] = r.render_count - base
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
