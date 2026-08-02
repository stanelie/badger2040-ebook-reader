# ------------------------------------------------------------
# memcheck.py  -  where the reader's memory actually goes
# ------------------------------------------------------------
# Run from a FRESH REPL (Ctrl+D first, so code.py's globals are gone):
#
#     import memcheck
#
# Prints what each part of the reader costs, in order, so the answer to "why
# is there no room for another 4736-byte buffer" comes from measurement rather
# than from reading the source and guessing. Source size is a bad proxy: the
# compiler drops comments, so a heavily-commented file can be large on disk and
# small in RAM.
#
# Nothing here is imported by the reader; it exists only to be run by hand.
import gc


def _free():
    gc.collect()
    return gc.mem_free()


def _largest():
    """Biggest single block available - the number that decides a failure.

    Free memory says how much is left; this says whether any of it is in one
    piece. A 4736-byte buffer refused with 70160 free is the difference between
    the two, and only this number shows it.
    """
    gc.collect()
    best, size = 0, 1024
    while size <= 131072:
        try:
            b = bytearray(size)
            del b
            best = size
            size *= 2
        except MemoryError:
            break
    return best


def report():
    steps = []
    base = _free()
    start = base
    print("\n--- MEMORY BREAKDOWN ---")
    print("baseline (fresh REPL): %d free" % base)

    def step(label, fn):
        nonlocal base
        try:
            fn()
        except Exception as e:
            print("  %-28s FAILED: %s" % (label, e))
            return
        now = _free()
        cost = base - now
        steps.append((label, cost))
        print("  %-28s %7d bytes   (%d free, largest >=%d)"
              % (label, cost, now, _largest()))
        base = now

    # Imported in the order code.py pulls them in, so each cost is the marginal
    # cost of that module on top of the ones before it.
    def _imp(name):
        def go():
            __import__(name)
        return go

    step("import adafruit_framebuf", _imp("adafruit_framebuf"))
    step("import propfont", _imp("propfont"))
    step("import uc8151_circuitpython", _imp("uc8151_circuitpython"))
    step("import hyphenator", _imp("hyphenator"))

    holder = {}

    def load_blob():
        import hyphenator
        hyphenator._load()
    step("hyphenation patterns", load_blob)

    def load_font():
        import propfont
        for name in ("oldmono.pf", "literata.pf", "lexenddeca.pf"):
            try:
                holder["font"] = propfont.PropFont(name)
                holder["font_name"] = name
                return
            except Exception:
                continue
        raise OSError("no .pf font found")
    step("one reading font", load_font)

    def buffers():
        holder["bufs"] = [bytearray(296 * 128 // 8) for _ in range(3)]
    step("3 page buffers + working", buffers)

    def rotate():
        holder["rot"] = bytearray(128 * 296 // 8)
    step("driver rotation scratch", rotate)

    def quickback():
        holder["qb"] = bytearray(128 * 296 // 8)
    step("quick-back 3rd buffer", quickback)

    print("\n  %-28s %7d bytes" % ("TOTAL ACCOUNTED", start - base))
    print("  %-28s %7d bytes" % ("STILL FREE", base))
    print("\n  font used: %s" % holder.get("font_name", "none"))
    print("\nLargest costs first:")
    for label, cost in sorted(steps, key=lambda s: -s[1])[:5]:
        print("  %7d  %s" % (cost, label))
    print("\nNote: code.py itself is NOT included - it is not imported here.")
    print("Run this before loading the reader to see the floor it starts from.")


report()
