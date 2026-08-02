"""Offline checks for the e-ink driver's framebuffer handling.

    python3 tools/test_display.py

The SPI conversation with the panel can only be verified on hardware. What can
be checked here is everything that decides WHICH bytes get sent:

  * the 270 degree rotation, against a straightforward reference implementation
  * the partial-update window: the bytes gathered for a region must be exactly
    the bytes that region occupies in a full-screen update, and the PTL window
    registers must describe that same region
  * the command order of a partial refresh

Getting the partial window wrong would put the right pixels in the wrong place
on the panel, which is hard to debug by eye, so it is pinned down here.
"""
import ast
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import CPDIR

W, H = 296, 128            # logical (rotated) size
PHYS_W, PHYS_H = 128, 296  # panel size
ROW_BYTES = PHYS_W >> 3

# command codes, mirroring the driver
CMD = {"PON": 0x04, "PTIN": 0x91, "PTL": 0x90, "DTM2": 0x13,
       "DSP": 0x11, "DRF": 0x12, "PTOU": 0x92, "POF": 0x02}


def make_driver():
    """A stub UC8151 carrying the real rotation and partial-update methods."""
    src = open(os.path.join(CPDIR, "uc8151_circuitpython.py")).read()
    wanted = ("update_partial", "_rotate_framebuffer", "ensure_scratch")
    methods = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.ClassDef) and node.name == "UC8151":
            for m in node.body:
                if isinstance(m, ast.FunctionDef) and m.name in wanted:
                    methods[m.name] = ast.get_source_segment(src, m)
    missing = [n for n in wanted if n not in methods]
    assert not missing, f"could not extract {missing}"

    ns = {f"CMD_{k}": v for k, v in CMD.items()}
    body = "\n".join(methods.values())
    exec("class D:\n" + "\n".join("    " + l for l in body.splitlines()), ns)

    d = ns["D"]()
    d.rotation = 270
    d.width, d.height = W, H
    d.physical_width, d.physical_height = PHYS_W, PHYS_H
    d._rotate_scratch = None
    d._partial_scratch = None
    d.quick_update_mode = True
    d.update_count = 0
    d.sent = []
    d.wait_ready = lambda: None
    d.write = lambda cmd, data=None: d.sent.append(
        (cmd, bytes(data) if data is not None else None))
    return d


def reference_rotate(fb):
    """Plain, obviously-correct 270 degree rotation."""
    out = bytearray(PHYS_W * PHYS_H // 8)
    for y in range(H):
        for x in range(W):
            if fb[(y * W + x) >> 3] & (0x80 >> (x & 7)):
                idx = (W - 1 - x) * PHYS_W + y
                out[idx >> 3] |= 0x80 >> (idx & 7)
    return bytes(out)


def test_rotation_matches_reference():
    d = make_driver()
    rnd = random.Random(4)
    cases = [bytearray(W * H // 8), bytearray([0xFF] * (W * H // 8))]
    for _ in range(4):
        cases.append(bytearray(rnd.getrandbits(8) for _ in range(W * H // 8)))
    for i, fb in enumerate(cases):
        d._rotate_scratch = None
        assert bytes(d._rotate_framebuffer(bytes(fb))) == reference_rotate(fb), (
            f"rotation differs from the reference on case {i}")
    print(f"  [ok] rotation matches a reference implementation ({len(cases)} buffers)")


def test_partial_window_bytes_and_registers():
    """The window must carry exactly the region's bytes, and say so in PTL."""
    d = make_driver()
    rnd = random.Random(11)
    fb = bytes(rnd.getrandbits(8) for _ in range(W * H // 8))
    full = reference_rotate(fb)

    # Includes bands whose end is NOT a multiple of 8 - the picker's highlight
    # bands end at y=39/55/71, so snapping the end inward instead of outward
    # would quietly clip the bottom rows of the bar.
    regions = [(0, 0, W, H), (0, 16, W, 24), (0, 24, W, 16), (0, 112, W, 16),
               (0, 48, W, 32), (10, 32, 100, 16), (0, 23, W, 17), (0, 0, W, 8),
               (0, 16, W, 40),
               (0, 23, W, 16), (0, 23, W, 32), (0, 39, W, 16), (0, 16, W, 20),
               (0, 55, W, 17), (0, 1, W, 3)]
    for (x, y, w, h) in regions:
        d.sent.clear()
        d._rotate_scratch = None
        assert d.update_partial(x, y, w, h, fb) is True, f"refused region {(x,y,w,h)}"

        by_cmd = dict(d.sent)
        window, data = by_cmd[CMD["PTL"]], by_cmd[CMD["DTM2"]]

        # what the region should map to
        y0 = y & ~7
        y1 = min(H, (y + h + 7) & ~7)
        x1 = min(W, x + w)
        x0 = max(0, x)
        px, pw, cols = PHYS_H - x1, x1 - x0, (y1 - y0) >> 3

        expect = bytearray()
        for dx in range(pw):
            s = (px + dx) * ROW_BYTES + (y0 >> 3)
            expect += full[s:s + cols]
        assert data == bytes(expect), (
            f"region {(x,y,w,h)}: window bytes are not the region's bytes")

        assert window[0] == y0 and window[1] == y1 - 1, (
            f"region {(x,y,w,h)}: PTL banked axis {window[0]}..{window[1]} "
            f"should be {y0}..{y1-1}")
        assert (window[2] << 8 | window[3]) == px, f"region {(x,y,w,h)}: PTL start"
        assert (window[4] << 8 | window[5]) == px + pw - 1, f"region {(x,y,w,h)}: PTL end"
        assert window[6] == 0x01, "PT_SCAN flag"
    print(f"  [ok] partial window carries exactly the region's bytes ({len(regions)} regions)")


def test_banded_and_pre_rotated_paths_agree():
    """update_partial can either rotate just the band it needs, or gather from
    a buffer the caller already rotated. Both must produce the same window."""
    d = make_driver()
    rnd = random.Random(21)
    fb = bytes(rnd.getrandbits(8) for _ in range(W * H // 8))
    rotated = reference_rotate(fb)

    for (x, y, w, h) in [(0, 0, W, H), (0, 23, W, 32), (0, 16, W, 24),
                         (0, 55, W, 17), (12, 40, 120, 16)]:
        d.sent.clear(); d._rotate_scratch = None
        assert d.update_partial(x, y, w, h, fb) is True
        banded = dict(d.sent)[CMD["DTM2"]]

        d.sent.clear()
        assert d.update_partial(x, y, w, h, rotated, pre_rotated=True) is True
        gathered = dict(d.sent)[CMD["DTM2"]]

        assert banded == gathered, (
            f"region {(x,y,w,h)}: rotating the band gives different bytes than "
            f"gathering from a pre-rotated buffer")
    print("  [ok] band-rotate and pre-rotated gather agree (5 regions)")


def test_full_region_equals_a_full_update():
    """Asking for the whole screen must produce the whole framebuffer."""
    d = make_driver()
    rnd = random.Random(7)
    fb = bytes(rnd.getrandbits(8) for _ in range(W * H // 8))
    d.sent.clear()
    d.update_partial(0, 0, W, H, fb)
    data = dict(d.sent)[CMD["DTM2"]]
    assert data == reference_rotate(fb), "full-screen partial != full update"
    assert len(data) == PHYS_W * PHYS_H // 8 == 4736
    print("  [ok] a full-screen partial equals a normal full update (4736 bytes)")


def test_xor_band_equals_drawing_the_highlight():
    """The picker highlights a row by inverting its band in the ROTATED buffer,
    instead of redrawing and re-rotating the screen.

    That is only valid if inverting the band in rotated space is the same as
    inverting the bar's rectangle in logical space before rotating. If the band
    or its x range were off, the highlight would land on the wrong pixels - so
    the two are compared here directly.
    """
    src = open(os.path.join(CPDIR, "code.py")).read()
    xor_src = None
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "_xor_row_band":
            xor_src = ast.get_source_segment(src, node)
    assert xor_src, "_xor_row_band not found"

    class FakeDisplay:
        physical_width = PHYS_W
    ns = {"display": FakeDisplay(), "WIDTH": W}
    exec(xor_src, ns)
    xor_row_band = ns["_xor_row_band"]

    rnd = random.Random(33)
    clean = bytearray(rnd.getrandbits(8) for _ in range(W * H // 8))

    for row in range(6):
        # logical: invert the bar rectangle, then rotate
        y0 = 25 + row * 16 - 1
        y1 = y0 + 16
        highlighted = bytearray(clean)
        for y in range(y0, y1):
            for x in range(2, W - 2):
                highlighted[(y * W + x) >> 3] ^= 0x80 >> (x & 7)
        want = reference_rotate(highlighted)

        # rotated: rotate the clean screen, then invert the band
        got = bytearray(reference_rotate(clean))
        xor_row_band(got, row)

        assert bytes(got) == want, (
            f"row {row}: inverting the band in rotated space does not match "
            f"inverting the bar before rotating")

        # and it must be reversible - moving away restores the clean screen
        xor_row_band(got, row)
        assert bytes(got) == reference_rotate(clean), (
            f"row {row}: inverting twice did not restore the original")
    print("  [ok] band inversion == drawing the highlight, and is reversible "
          "(6 rows)")


def test_command_order():
    d = make_driver()
    fb = bytes(W * H // 8)
    d.sent.clear()
    d.update_partial(0, 16, W, 24, fb)
    order = [c for c, _ in d.sent]
    expected = [CMD["PON"], CMD["PTIN"], CMD["PTL"], CMD["DTM2"],
                CMD["DSP"], CMD["DRF"], CMD["PTOU"]]
    assert order == expected, (
        f"command order {[hex(c) for c in order]} != {[hex(c) for c in expected]}")
    # PTOU matters: send_image() skips it while quick updates are on, so
    # without it here the next FULL refresh would stay confined to this window.
    assert order[-1] == CMD["PTOU"], "partial refresh must leave partial mode"
    print("  [ok] command order is PON, PTIN, PTL, DTM2, DSP, DRF, PTOU")


def test_full_update_after_partial_repaints_everything():
    """A partial refresh only drives the pixels in its window.

    In no-flickering mode the waveform only moves pixels it believes changed,
    so without intervention the next FULL update leaves the untouched parts of
    the previous screen showing through - opening a book from the picker left
    picker rows mixed into the page. The first full update after any partial
    one must use the flickering waveform, which drives every pixel, and then
    go straight back to the fast one.
    """
    src = open(os.path.join(CPDIR, "uc8151_circuitpython.py")).read()
    wanted = ("update", "update_partial", "_rotate_framebuffer",
              "send_image", "ensure_scratch")
    methods = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.ClassDef) and node.name == "UC8151":
            for m in node.body:
                if isinstance(m, ast.FunctionDef) and m.name in wanted:
                    methods[m.name] = ast.get_source_segment(src, m)

    ns = {f"CMD_{k}": v for k, v in CMD.items()}
    ns["CMD_DTM1"] = 0x10
    exec("class D:\n" + "\n".join("    " + l for l in
                                  "\n".join(methods.values()).splitlines()), ns)
    d = ns["D"]()
    d.rotation, d.width, d.height = 270, W, H
    d.physical_width, d.physical_height = PHYS_W, PHYS_H
    d._rotate_scratch = d._partial_scratch = None
    d._stale_after_partial = False
    d.quick_update_mode = True
    d.no_flickering = True
    d.full_update_period = 0        # how the reader configures it
    d.speed, d.update_count = 4, 0
    d.raw_fb = bytearray(W * H // 8)
    d.wait_ready = lambda: None
    d.is_busy = lambda: False
    d.write = lambda c, data=None: None
    luts = []
    d.set_waveform_lut = lambda speed=None, no_flickering=None: luts.append(
        (speed, no_flickering))

    fb = bytes(W * H // 8)

    luts.clear(); d.update(fb=fb)
    assert luts == [], "a normal page turn should not change the waveform"

    d.update_partial(0, 24, W, 32, fb)
    assert d._stale_after_partial, "partial refresh did not flag the screen stale"

    luts.clear(); d.update(fb=fb)
    assert luts and luts[0][1] is False, (
        "the full update after a partial one did not switch to the flickering "
        "waveform - the previous screen would bleed through")
    assert not d._stale_after_partial, "stale flag was not cleared"

    luts.clear(); d.update(fb=fb)
    assert luts == [], "the flickering waveform was not given back afterwards"
    print("  [ok] first full update after a partial repaints everything, "
          "then reverts to the fast waveform")


def test_refuses_what_it_cannot_map():
    d = make_driver()
    fb = bytes(W * H // 8)
    assert d.update_partial(0, 0, 0, 10, fb) is False, "empty width accepted"
    assert d.update_partial(0, 0, 10, 0, fb) is False, "empty height accepted"
    assert d.update_partial(W + 5, 0, 10, 10, fb) is False, "offscreen accepted"
    d.rotation = 0
    assert d.update_partial(0, 0, W, H, fb) is False, (
        "should refuse a rotation its mapping was not written for")
    print("  [ok] refuses empty, offscreen and unsupported-rotation requests "
          "(caller falls back to a full update)")


def main():
    print("e-ink driver:")
    test_rotation_matches_reference()
    test_partial_window_bytes_and_registers()
    test_banded_and_pre_rotated_paths_agree()
    test_full_region_equals_a_full_update()
    test_xor_band_equals_drawing_the_highlight()
    test_command_order()
    test_full_update_after_partial_repaints_everything()
    test_refuses_what_it_cannot_map()
    test_rotation_scratch_is_claimed_before_optional_buffers()
    print("\nALL DISPLAY CHECKS PASSED")
    print("\nNote: the SPI conversation itself is not verifiable off-device.")
    return 0


def test_rotation_scratch_is_claimed_before_optional_buffers():
    """The buffer the panel cannot work without must not be allocated last.

    _rotate_framebuffer used to create its scratch on first use, so the one
    mandatory buffer was requested only after every optional buffer had taken
    its share. A board a few KB short then booted, drew the picker, and died
    inside the rotation with nothing on screen:

        File "uc8151_circuitpython.py", line 839, in _rotate_framebuffer
        MemoryError: memory allocation failed, allocating 4736 bytes

    Claimed at startup instead, a shortage falls on quick-back, which is
    written to lose it gracefully.
    """
    src = open(os.path.join(CPDIR, "code.py")).read()
    lines = src.splitlines()

    def line_of(needle):
        for i, l in enumerate(lines):
            if needle in l and not l.strip().startswith("#"):
                return i
        raise AssertionError("not found in code.py: " + needle)

    assert line_of("display.ensure_scratch()") < line_of("prev_rotated_buffer = bytearray("), (
        "the rotation scratch is claimed after the optional quick-back buffer; "
        "a short board will crash in _rotate_framebuffer instead of simply "
        "losing quick-back")

    drv = open(os.path.join(CPDIR, "uc8151_circuitpython.py")).read()
    assert "def ensure_scratch(self)" in drv, "driver lost ensure_scratch()"

    # Moving the scratch earlier only helps if quick-back still absorbs the
    # shortage, so its allocation must stay inside a MemoryError guard.
    # Module level only: _rebuild_reader_state has its own guarded allocation,
    # and matching that one instead would let the boot-time guard be removed
    # without this noticing.
    guarded = False
    for node in ast.parse(src).body:
        if isinstance(node, ast.Try):
            seg = ast.get_source_segment(src, node) or ""
            if "prev_rotated_buffer = bytearray(" in seg and "MemoryError" in seg:
                guarded = True
    assert guarded, "the boot-time quick-back allocation is no longer guarded "\
                    "by MemoryError"
    print("  [ok] rotation scratch claimed before optional buffers")

if __name__ == "__main__":
    sys.exit(main())
