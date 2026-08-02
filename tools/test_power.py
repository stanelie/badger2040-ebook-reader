"""Offline test of the sleep / inactivity behaviour.

    python3 tools/test_power.py

Battery life depends on the device actually powering down when left alone, and
that is easy to get wrong: any loop that polls buttons on its own has to honour
the timeout itself. The book picker originally did not, so leaving the device
sitting in the picker kept it awake until the battery ran down.

These tests drive the real check_inactivity() and state_save_current() from
code.py with the clock, battery and display stubbed, plus a structural check
that the picker's polling loop still calls check_inactivity at all.
"""
import ast
import os
import tempfile
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import CPDIR, FONTDIR, SYSDIR, INACTIVITY_TIMEOUT_DEFAULT, load_engine


class FakeClock:
    """Stands in for the time module; monotonic() only moves when we say so."""
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, _seconds):
        pass


def make_ns(charging=False, timeout=300):
    ns, _ = load_engine("literata.pf")
    clock = FakeClock()
    events = []

    ns["time"] = clock
    ns["INACTIVITY_TIMEOUT"] = timeout
    ns["last_activity"] = clock.now
    ns["get_battery_status"] = lambda: (80, charging)
    ns["enter_sleep"] = lambda: events.append("sleep")
    ns["led_on"] = lambda: events.append("led_on")
    ns["led_off"] = lambda: events.append("led_off")
    return ns, clock, events


def test_stays_awake_before_timeout():
    ns, clock, events = make_ns()
    clock.now += 299          # just under the 300s timeout
    assert ns["check_inactivity"]() is False
    assert "sleep" not in events, "slept before the timeout elapsed"
    print("  [ok] stays awake before the timeout")


def test_sleeps_after_timeout():
    ns, clock, events = make_ns()
    clock.now += 301
    assert ns["check_inactivity"]() is True
    assert events.count("sleep") == 1, f"expected one sleep, got {events}"
    assert events[0] == "led_on" and events[-1] == "led_off", (
        f"LED not left off after sleeping: {events}")
    print("  [ok] sleeps once past the timeout, and leaves the LED off")


def test_charging_defers_sleep():
    ns, clock, events = make_ns(charging=True)
    clock.now += 400
    assert ns["check_inactivity"]() is False
    assert "sleep" not in events, "slept while charging"
    assert ns["last_activity"] == clock.now, (
        "charging should refresh last_activity so it doesn't sleep the moment "
        "the cable is unplugged")
    print("  [ok] stays awake while charging, and refreshes the idle timer")


def test_activity_defers_sleep():
    ns, clock, events = make_ns()
    clock.now += 250
    assert ns["check_inactivity"]() is False
    ns["last_activity"] = clock.now      # a button press
    clock.now += 250                     # 500s total, but only 250s idle
    assert ns["check_inactivity"]() is False
    assert "sleep" not in events, "slept despite recent activity"
    clock.now += 100                     # now 350s idle
    assert ns["check_inactivity"]() is True
    print("  [ok] a button press defers sleep by a full timeout")


def test_save_skipped_without_a_book():
    """Sleeping from the startup picker must not write a phantom NVRAM entry."""
    ns, _, _ = make_ns()
    saved = []
    ns["state_save"] = lambda off, rem, path: saved.append(path)

    ns["text_file"] = ""
    ns["current_offset"] = 0
    ns["current_remainder"] = b""
    ns["state_save_current"]()
    assert saved == [], f"saved a book entry with no book open: {saved}"

    ns["text_file"] = "/books/real.txt"
    ns["current_offset"] = 1234
    ns["state_save_current"]()
    assert saved == ["/books/real.txt"], f"did not save a real book: {saved}"
    print("  [ok] no phantom NVRAM entry when no book is open")


def test_picker_loop_checks_inactivity():
    """Structural: the picker polls in its own loop, so it must call
    check_inactivity itself or the device can never sleep while it is open."""
    src = open(os.path.join(CPDIR, "code.py")).read()
    picker = None
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "file_picker":
            picker = node
    assert picker is not None, "file_picker not found"

    called = set()
    for node in ast.walk(picker):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    assert "check_inactivity" in called, (
        "file_picker does not call check_inactivity - the device would stay "
        "awake indefinitely with the picker open")

    # and it must refresh last_activity, or picking a book after browsing for
    # longer than the timeout would sleep immediately on return
    assigns = set()
    for node in ast.walk(picker):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assigns.add(t.id)
    assert "last_activity" in assigns, (
        "file_picker never refreshes last_activity - selecting a book after a "
        "long browse would sleep immediately on return to the reader")
    print("  [ok] picker honours the timeout and refreshes the idle timer")


def test_sleep_shows_the_cover_or_leaves_the_page():
    """The sleep screen is a prepared frame, read straight in.

    coverimg.py does the decoding, in the converter or by hand, because the
    reader has the least free memory and the most fragmented heap of anything
    on the board - a JPEG needs its scaled bitmap held whole and in one piece.
    At sleep it is a file read into a buffer that already exists.

    A book with no cover, or a frame of the wrong size, must say it has gone to
    sleep instead. Leaving the last page up was tried first and is unreadable
    as a state: a sleeping board looks exactly like one that is still awake.
    """
    import tempfile
    sys.path.insert(0, CPDIR)
    import coverimg

    calls = {"refreshed": 0, "speeds": [], "messages": []}
    buf = bytearray(4736)

    class _Display:
        rotation = 270

        def _rotate_framebuffer(self, b):
            return b

        def set_speed(self, speed, no_flickering=False):
            calls["speeds"].append((speed, no_flickering))

    class _Reader:
        text_file = ""
        raw_working_buffer = buf
        display = _Display()
        ORIGINAL_SPEED = 4
        ORIGINAL_NO_FLICKERING = True

        @staticmethod
        def update_display_fast(b, blocking=True):
            calls["refreshed"] += 1

        @staticmethod
        def show_message(*items, **kw):
            calls["messages"].append(" ".join(t for t, _x, _y in items))

    reader = _Reader()
    saved = sys.modules.get("__main__")
    sys.modules["__main__"] = reader
    was_on = coverimg.USE_COVER_SLEEP_SCREEN
    try:
        # The cover is off by default - see the test below - so turn it on to
        # exercise the path at all.
        coverimg.USE_COVER_SLEEP_SCREEN = True
        tmp = tempfile.mkdtemp()
        assert coverimg.show_sleep_screen() is False, (
            "claimed a cover with no book open")
        assert calls["messages"], "said nothing at all with no book open"

        book = os.path.join(tmp, "Sway.txt")
        open(book, "wb").write(b"text")
        reader.text_file = book
        calls["messages"] = []
        assert coverimg.show_sleep_screen() is False, (
            "claimed a cover that does not exist")
        assert calls["refreshed"] == 0, "pushed a frame with nothing to show"
        assert any("Sleep" in m for m in calls["messages"]), (
            "a book with no cover left the page up; a sleeping board then "
            "looks exactly like one that is still awake")

        frame = os.path.join(tmp, "Sway.sleep.bin")
        open(frame, "wb").write(b"\xff" * 100)
        calls["messages"] = []
        assert coverimg.show_sleep_screen() is False, (
            "accepted a truncated sleep frame")
        assert calls["refreshed"] == 0, "pushed a truncated frame to the panel"
        assert any("Sleep" in m for m in calls["messages"]), (
            "a bad frame showed nothing at all")

        payload = bytes(range(256)) * (4736 // 256) + b"\x00" * (4736 % 256)
        open(frame, "wb").write(payload)
        calls["messages"] = []
        assert coverimg.show_sleep_screen() is True, (
            "did not show a valid sleep frame")
        assert not calls["messages"], (
            "drew the sleep message over a cover that was shown")
        assert calls["refreshed"] == 1, "did not refresh the panel"
        assert bytes(buf) == payload, "the frame on screen is not the one on disk"

        # a full flicker refresh, then the speed put back: this image sits on
        # the panel with the power off, possibly for weeks
        assert calls["speeds"][0] == (0, False), (
            "sleep screen drawn with a quick update; its ghosting would stay "
            "on the panel for as long as the board is asleep")
        assert calls["speeds"][-1] == (4, True), "display speed not restored"
    finally:
        coverimg.USE_COVER_SLEEP_SCREEN = was_on
        if saved is None:
            sys.modules.pop("__main__", None)
        else:
            sys.modules["__main__"] = saved

    # and enter_sleep has to actually call it, without the panel-drawing code
    # creeping back into the resident file
    src = open(os.path.join(CPDIR, "code.py")).read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "enter_sleep":
            body = ast.get_source_segment(src, node)
    assert "coverimg" in body and "show_sleep_screen" in body, (
        "enter_sleep no longer draws a sleep screen")
    assert "def show_sleep_screen" not in src, (
        "show_sleep_screen is back in code.py; it runs once per sleep and that "
        "file is resident for the whole session")
    print("  [ok] sleep shows a prepared cover, or leaves the page up")


def test_sleep_frame_is_exactly_one_screen():
    """coverimg writes what the reader reads: one framebuffer, no header."""
    sys.path.insert(0, CPDIR)
    import coverimg
    assert coverimg.FRAME == 296 * 128 // 8 == 4736, (
        f"coverimg builds a {coverimg.FRAME}-byte frame; the reader's buffer "
        "is 4736")
    frame = coverimg.pack(lambda x, y: 0x0000, 100, 138)
    assert len(frame) == 4736, f"pack() produced {len(frame)} bytes"

    assert coverimg.sleep_path_for("/books/Sway.txt") == "/books/Sway.sleep.bin"
    assert coverimg.sleep_path_for("/books/Sway") == "/books/Sway.sleep.bin", (
        "a book path without .txt gets a different name")

    # Writer and reader must derive that name the same way. They are both in
    # this module now, so check they go through the one helper rather than
    # spelling the suffix out twice.
    ui = open(os.path.join(SYSDIR, "coverimg.py")).read()
    for fn in ("show_sleep_screen", "render_for_book"):
        for node in ast.parse(ui).body:
            if isinstance(node, ast.FunctionDef) and node.name == fn:
                body = ast.get_source_segment(ui, node)
        assert "sleep_path_for(" in body, (
            f"{fn} builds the sleep-frame path itself instead of using "
            "sleep_path_for - the two sides can drift apart")
    print("  [ok] the sleep frame is one screen, named the same on both sides")

def test_cover_fits_the_panel_and_dithers():
    """The cover is fitted whole and dithered, not cropped and thresholded.

    A book cover is portrait and the panel is landscape, so it has to letterbox
    rather than fill. And on a two-level panel, thresholding turns a cover into
    a silhouette - the dither is what keeps any shading at all.
    """
    sys.path.insert(0, CPDIR)
    import coverimg

    def rgb565(r, g, b):
        return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)

    W, H, ROW = 296, 128, 296 >> 3

    def inked(frame):
        return [(x, y) for y in range(H) for x in range(W)
                if frame[y * ROW + (x >> 3)] & (0x80 >> (x & 7))]

    # fill=False explicitly: filling is the default now, and it crops rather
    # than preserving the whole image, so this is the other mode.
    for sw, sh in ((600, 800), (1200, 1600), (100, 138), (128, 296), (2000, 100)):
        px = inked(coverimg.pack(lambda x, y: rgb565(0, 0, 0), sw, sh, fill=False))
        xs = [p[0] for p in px]
        ys = [p[1] for p in px]
        w, h = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
        assert w <= W and h <= H, f"{sw}x{sh}: drawn area escapes the panel"
        assert abs(sw / sh - w / h) / (sw / sh) < 0.08, (
            f"{sw}x{sh}: fitted to {w}x{h}, aspect ratio not preserved")
        assert min(xs) >= 0 and min(ys) >= 0

    # tone
    assert sum(coverimg.pack(lambda x, y: rgb565(255, 255, 255), W, H,
                             fill=False)) == 0, (
        "a white cover put ink on the panel")
    black = coverimg.pack(lambda x, y: rgb565(0, 0, 0), W, H, fill=False)
    assert sum(bin(b).count("1") for b in black) == W * H, (
        "a black cover left gaps")

    # a grey ramp must ink monotonically, and land near the requested level -
    # that is the difference between a dither and a threshold
    for level, expect in ((64, 75), (128, 50), (192, 25)):
        f = coverimg.pack(lambda x, y, l=level: rgb565(l, l, l), W, H, fill=False)
        pct = 100 * sum(bin(b).count("1") for b in f) // (W * H)
        assert abs(pct - expect) <= 8, (
            f"grey {level} inked {pct}% of the panel, expected about "
            f"{expect}% - this looks like a threshold, not a dither")
    print("  [ok] covers letterbox to the panel and dither by tone")

def test_cover_is_turned_counter_clockwise_to_fill_the_panel():
    """A portrait cover upright uses a third of a landscape panel.

    Fitted whole and upright, an 800x1104 cover lands at 90x125 in a 296x128
    screen - correct, and too small to make out. Turned a quarter-turn its long
    side runs along the panel's long side and it lands at 172x125, close to
    twice the area.

    The direction is easy to get backwards, so each edge of the source is
    checked against where a counter-clockwise turn puts it.
    """
    sys.path.insert(0, CPDIR)
    import coverimg

    W, H, ROW = 296, 128, 296 >> 3

    def rgb565(r, g, b):
        return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)

    def bbox(frame):
        px = [(x, y) for y in range(H) for x in range(W)
              if frame[y * ROW + (x >> 3)] & (0x80 >> (x & 7))]
        assert px, "nothing was drawn"
        xs = [p[0] for p in px]
        ys = [p[1] for p in px]
        return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1

    def black(x, y):
        return rgb565(0, 0, 0)

    for sw, sh in ((800, 1104), (600, 800), (1200, 1600)):
        # fill=False: filling covers the whole panel either way, so the gain
        # from turning it only shows when the image is fitted whole.
        _, _, uw, uh = bbox(coverimg.pack(black, sw, sh, rotate=False, fill=False))
        _, _, tw, th = bbox(coverimg.pack(black, sw, sh, rotate=True, fill=False))
        assert tw * th > uw * uh * 1.5, (
            f"{sw}x{sh}: turning it covers {tw}x{th} against {uw}x{uh} "
            "upright - barely more than leaving it alone")
        assert tw <= W and th <= H, f"{sw}x{sh}: turned image escapes the panel"

    SW, SH = 200, 400
    ax, ay, aw, ah = bbox(coverimg.pack(black, SW, SH, rotate=True, fill=False))
    edges = (
        ("left", lambda x, y: rgb565(0, 0, 0) if x < 20 else rgb565(255, 255, 255),
         "bottom"),
        ("right", lambda x, y: rgb565(0, 0, 0) if x > SW - 21 else rgb565(255, 255, 255),
         "top"),
        ("top", lambda x, y: rgb565(0, 0, 0) if y < 40 else rgb565(255, 255, 255),
         "left"),
        ("bottom", lambda x, y: rgb565(0, 0, 0) if y > SH - 41 else rgb565(255, 255, 255),
         "right"),
    )
    for name, paint, expect in edges:
        x0, y0, w, h = bbox(coverimg.pack(paint, SW, SH, rotate=True, fill=False))
        cx, cy = x0 + w / 2, y0 + h / 2
        got = ("left" if cx < ax + aw / 3 else
               "right" if cx > ax + 2 * aw / 3 else
               "top" if cy < ay + ah / 3 else
               "bottom" if cy > ay + 2 * ah / 3 else "middle")
        assert got == expect, (
            f"the {name} edge of the cover ended up at the {got}; a "
            f"counter-clockwise quarter-turn puts it at the {expect}")

    _, _, uw, uh = bbox(coverimg.pack(black, 800, 1104, rotate=False, fill=False))
    assert uh > uw, "upright fitting no longer keeps the cover portrait"
    assert coverimg.ROTATE_COVER is True, (
        "covers are no longer turned by default, so they go back to using a "
        "third of the screen")
    print("  [ok] covers turn counter-clockwise and cover ~1.9x the panel")

def test_cover_fills_the_panel_and_crops_from_the_centre():
    """Filling must reach every pixel, and lose the outside evenly.

    Fitted whole, even turned, a cover reached about 56% of the screen and sat
    in white margins. Filling scales until both axes are covered and crops the
    overflow - roughly 58% of the cover survives, and all of the panel is used.

    Coverage has to be exact. Rounding the scale down leaves the panel 98%
    covered, with a white strip down one edge, which looks like a bug rather
    than a crop.
    """
    sys.path.insert(0, CPDIR)
    import coverimg

    W, H, ROW = 296, 128, 296 >> 3

    def rgb565(r, g, b):
        return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)

    def lit(frame):
        return sum(bin(b).count("1") for b in frame)

    def black(x, y):
        return rgb565(0, 0, 0)

    for sw, sh in ((800, 1104), (600, 800), (1200, 1600), (1000, 1000),
                   (400, 1600), (2000, 100)):
        filled = coverimg.pack(black, sw, sh, rotate=True, fill=True)
        assert lit(filled) == W * H, (
            f"{sw}x{sh}: filling left {W * H - lit(filled)} pixels of the "
            "panel blank")
        fitted = coverimg.pack(black, sw, sh, rotate=True, fill=False)
        assert lit(fitted) < W * H, (
            f"{sw}x{sh}: fit=False is filling too, so nothing is letterboxed "
            "any more")

    # the crop takes from the outside, not from one side
    def middle_block(x, y):
        return (rgb565(0, 0, 0)
                if (800 // 3 < x < 2 * 800 // 3 and 1104 // 3 < y < 2 * 1104 // 3)
                else rgb565(255, 255, 255))
    frame = coverimg.pack(middle_block, 800, 1104, rotate=True, fill=True)
    px = [(x, y) for y in range(H) for x in range(W)
          if frame[y * ROW + (x >> 3)] & (0x80 >> (x & 7))]
    xs = [p[0] for p in px]
    ys = [p[1] for p in px]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    assert abs(cx - W / 2) < 8 and abs(cy - H / 2) < 8, (
        f"the middle of the cover landed at ({cx:.0f},{cy:.0f}) instead of the "
        f"middle of the panel ({W // 2},{H // 2}) - the crop is lopsided")

    # and dithering still works on top of the crop
    for level, expect in ((64, 75), (128, 50), (192, 25)):
        pct = 100 * lit(coverimg.pack(
            lambda x, y, l=level: rgb565(l, l, l), 800, 1104,
            rotate=True, fill=True)) // (W * H)
        assert abs(pct - expect) <= 8, (
            f"grey {level} inked {pct}% once filling, expected about {expect}%")

    assert coverimg.FILL_SCREEN is True, (
        "covers no longer fill the screen by default, so they go back to "
        "sitting in white margins")
    print("  [ok] covers fill the panel completely, cropped from the centre")

def test_cover_sleep_screen_is_off_by_default():
    """The cover is built and stored, but not shown.

    At 296x128 and two levels a cover is not legible - recognisable across a
    room, not across a dithered 1-bit thumbnail. Everything that renders one
    still runs, so this is one flag away from coming back.

    What the sleep screen shows instead is which book, how far in, and that it
    is asleep.
    """
    sys.path.insert(0, CPDIR)
    import coverimg

    assert coverimg.USE_COVER_SLEEP_SCREEN is False, (
        "the cover sleep screen is on again; it was turned off because the "
        "image is not readable at this size")
    # the machinery must still be there, not deleted
    for name in ("render", "render_for_book", "pack", "main"):
        assert callable(getattr(coverimg, name, None)), (
            f"coverimg.{name} was removed; turning the cover back on should "
            "be a one-flag change")

    shown = []
    book = os.path.join(tempfile.mkdtemp(), "The Last Town.txt")
    open(book, "wb").write(b"x" * 200)

    class R:
        text_file = book
        current_offset = 0
        raw_working_buffer = bytearray(4736)

        @staticmethod
        def show_message(*items):
            shown.append([t for t, _x, _y in items])

    saved = sys.modules.get("__main__")
    sys.modules["__main__"] = R
    try:
        for offset, expect in ((0, "0%"), (50, "25%"), (200, "100%")):
            R.current_offset = offset
            shown.clear()
            assert coverimg.show_sleep_screen() is False
            lines = shown[0]
            assert any("Sleeping" in l for l in lines), (
                "the sleep screen no longer says it is asleep")
            assert any("The Last Town" == l for l in lines), (
                f"the book title is missing: {lines}")
            assert any(expect in l for l in lines), (
                f"at offset {offset} of 200 the screen should say {expect}, "
                f"got {lines}")

        # a position past the end must not read as more than 100%
        R.current_offset = 10_000
        shown.clear()
        coverimg.show_sleep_screen()
        assert any("100%" in l for l in shown[0]), (
            f"a position past the end reported {shown[0]}")

        # a long title has to be cut, not run off the panel
        long_book = os.path.join(tempfile.mkdtemp(),
                                 "A" * 80 + ".txt")
        open(long_book, "wb").write(b"x" * 10)
        R.text_file = long_book
        shown.clear()
        coverimg.show_sleep_screen()
        assert all(len(l) <= 36 for l in shown[0]), (
            f"a long title was not shortened: {shown[0]}")
    finally:
        if saved is None:
            sys.modules.pop("__main__", None)
        else:
            sys.modules["__main__"] = saved
    print("  [ok] sleep screen shows title and progress; cover stays off")

def main():
    print("sleep / inactivity behaviour:")
    test_stays_awake_before_timeout()
    test_sleeps_after_timeout()
    test_charging_defers_sleep()
    test_activity_defers_sleep()
    test_save_skipped_without_a_book()
    test_picker_loop_checks_inactivity()
    test_sleep_shows_the_cover_or_leaves_the_page()
    test_sleep_frame_is_exactly_one_screen()
    test_cover_fits_the_panel_and_dithers()
    test_cover_is_turned_counter_clockwise_to_fill_the_panel()
    test_cover_fills_the_panel_and_crops_from_the_centre()
    test_cover_sleep_screen_is_off_by_default()
    print("\nALL POWER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
