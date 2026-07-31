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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import CPDIR, INACTIVITY_TIMEOUT_DEFAULT, load_engine


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


def main():
    print("sleep / inactivity behaviour:")
    test_stays_awake_before_timeout()
    test_sleeps_after_timeout()
    test_charging_defers_sleep()
    test_activity_defers_sleep()
    test_save_skipped_without_a_book()
    test_picker_loop_checks_inactivity()
    print("\nALL POWER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
