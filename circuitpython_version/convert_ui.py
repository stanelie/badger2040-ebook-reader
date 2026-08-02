# ------------------------------------------------------------
# convert_ui.py  -  progress screen for an on-device EPUB conversion
# ------------------------------------------------------------
# Split out of code.py deliberately. code.py is compiled into RAM when the
# board boots and stays there for the whole session, so every line in it costs
# memory the reader never gets back - and this code runs only while converting
# a book, which happens once per title. Keeping it here means it is compiled
# when a conversion starts and collected when the board reloads afterwards.
#
# The reader's own globals (the panel, its buffers, the drawing helpers) are
# reached through __main__, the same way epub_xtract does it, rather than being
# passed in: this needs a dozen of them, and _rebuild_reader_state has to write
# some back.
import gc
import time

import __main__ as reader

display = reader.display
WIDTH = reader.WIDTH

_CONV_BAR = (14, 56, WIDTH - 28, 20)   # x, y, w, h of the bar, logical coords
_CONV_BAND_Y = 48                      # region redrawn on the panel per step
_CONV_BAND_H = 64
# Only refresh once the bar has actually moved this far. A 75-chapter book
# advances the bar ~3px per chapter, so refreshing per chapter would spend 75
# panel updates to show what 30 show just as well - and each one costs time the
# conversion could be using.
_CONV_STEP = 8

_CONV_NOTES = {
    "open":     "Reading archive...",
    "cover":    "Saving cover...",
    "start":    "",
    "readonly": "Read-only! Unplug USB, retry",
    "failed":   "Conversion failed",
    "partial":  "Done - some chapters failed",
    "done":     "Done",
}

_conv = {"px": -1, "name": "", "total": 0, "done": 0, "full_drawn": False}


def _draw_convert_screen(name, done, total, note=""):
    """Draw the progress screen and return it rotated, ready for the panel."""
    bx, by, bw, bh = _CONV_BAR
    with reader._ScratchFrame():
        display.text("Converting EPUB", 8, 6, 1)
        display.text(name[:36], 8, 26, 1)

        # Outline drawn as four bars rather than framebuf's rect(): fill_rect
        # is the primitive the rest of this file uses, and it needs no
        # keyword-only fill argument to mean "outline".
        display.fb.fill_rect(bx, by, bw, 1, 1)
        display.fb.fill_rect(bx, by + bh - 1, bw, 1, 1)
        display.fb.fill_rect(bx, by, 1, bh, 1)
        display.fb.fill_rect(bx + bw - 1, by, 1, bh, 1)

        if total > 0:
            fill = ((bw - 4) * done) // total
            if fill > 0:
                display.fb.fill_rect(bx + 2, by + 2, fill, bh - 4, 1)
            display.text("%d / %d chapters" % (done, total), 8, 86, 1)
        if note:
            display.text(note[:36], 8, 104, 1)
    return display._rotate_framebuffer(reader.raw_working_buffer)


def _push_convert_screen(rotated, partial):
    if partial and reader.PARTIAL_UPDATES:
        try:
            if display.update_partial(0, _CONV_BAND_Y, WIDTH, _CONV_BAND_H,
                                      fb=rotated, pre_rotated=True):
                return
        except Exception as e:
            print(f"convert progress partial update failed, using full: {e}")
    reader.update_display_fast(rotated)


def _convert_progress(stage, done, total, name):
    """Progress callback handed to epub_xtract.convert_book()."""
    if name:
        _conv["name"] = name
    if total:
        _conv["total"] = total
    if stage == "chapter":
        _conv["done"] = done

    total = _conv["total"]
    done = _conv["done"]

    if stage == "chapter" and total and done < total:
        px = ((_CONV_BAR[2] - 4) * done) // total
        if px - _conv["px"] < _CONV_STEP:
            return                      # too small a move to spend a refresh on
        _conv["px"] = px

    rotated = _draw_convert_screen(_conv["name"], done, total,
                                   _CONV_NOTES.get(stage, ""))
    # Only the bar and its counter change between chapters, so those go out as
    # a partial refresh; the handful of stage changes get a full one.
    _push_convert_screen(rotated, _conv["full_drawn"] and stage == "chapter")
    _conv["full_drawn"] = True


def _rebuild_reader_state():
    """Reallocate what the converter released, for when a reload cannot happen.

    convert_book() drops the font and the page buffers to make room. Normally
    the reload right after makes that moot, but under an IDE holding the serial
    connection the board goes to the REPL instead of restarting, and the reader
    would carry on with FONT set to None.
    """
    # setattr on the reader module, not `global`: these belong to code.py, and
    # a global here would only ever rebind names inside this module while the
    # reader carried on with its own set to None.
    gc.collect()
    if getattr(reader, "FONT", None) is None:
        reader.FONT = reader.propfont.PropFont(
            reader.AVAILABLE_FONTS[reader.load_font_index()][0],
            buf=getattr(reader, "_font_buf", None))
    size = display.physical_width * display.physical_height // 8
    for name in ("current_rotated_buffer", "next_rotated_buffer"):
        if getattr(reader, name, None) is None:
            setattr(reader, name, bytearray(size))
    if reader.QUICK_BACK_OK and getattr(reader, "prev_rotated_buffer", None) is None:
        try:
            reader.prev_rotated_buffer = bytearray(size)
        except MemoryError:
            reader.prev_rotated_buffer = None
            reader.QUICK_BACK_OK = False
    gc.collect()


def run_pending(epub_path):
    """Convert a queued EPUB during startup, then restart into the result.

    Called from code.py before the reader has built anything, so the converter
    has the heap essentially to itself. Does not return: it restarts either
    way, because at this point the board has no page buffers, no font and no
    hyphenation patterns and is in no state to read a book.
    """
    # Cleared BEFORE the work, not after. A conversion that fails hard enough
    # to reset the board would otherwise be retried on the next boot, and the
    # next, with no way to reach the picker and cancel it.
    reader.clear_pending()

    _conv.update({"px": -1, "name": epub_path.split("/")[-1], "total": 0,
                  "done": 0, "full_drawn": False})
    reader.led_on()
    _convert_progress("open", 0, 0, _conv["name"])

    txt = None
    try:
        import epub_xtract
        txt = epub_xtract.convert_book(epub_path, progress=_convert_progress)
    except MemoryError as e:
        print(f"conversion ran out of memory: {e}")
        reader.show_message(("Out of memory", 90, 40), (str(e)[:36], 8, 70))
        time.sleep(4)
    except Exception as e:
        print(f"conversion error: {e}")
        reader.show_message(("Conversion failed", 75, 40), (str(e)[:36], 8, 70))
        time.sleep(4)
    reader.led_off()

    if txt:
        reader.state_save(0, b"", txt)
        reader.show_message(("Converted!", 100, 40), ("Opening book...", 80, 70))
    else:
        time.sleep(2)       # the reason is already on the panel

    try:
        import supervisor
        supervisor.reload()
    except Exception as e:
        print(f"reload unavailable after converting: {e}")
        reader.show_message(("Please reset the board", 55, 55))
    while True:
        time.sleep(1)


def convert_epub(epub_path):
    """Convert the chosen EPUB and open the result.

    On success the board reloads: the converter has just spent two minutes
    churning the heap, and starting over rebuilds the font and page buffers on
    a clean one rather than fitting them into the holes it left. Returns the
    .txt path if a reload did not happen, so the caller can still open it.
    """
    _conv.update({"px": -1, "name": epub_path.split("/")[-1], "total": 0,
                  "done": 0, "full_drawn": False})

    reader.led_on()
    _convert_progress("open", 0, 0, _conv["name"])

    txt = None
    try:
        import epub_xtract
        txt = epub_xtract.convert_book(epub_path, progress=_convert_progress)
    except MemoryError as e:
        print(f"conversion ran out of memory: {e}")
        reader.show_message(("Out of memory", 90, 40),
                     ("Convert over USB instead", 40, 70))
        time.sleep(4)
    except Exception as e:
        print(f"conversion error: {e}")
        reader.show_message(("Conversion failed", 75, 40), (str(e)[:36], 8, 70))
        time.sleep(4)
    reader.led_off()

    if not txt:
        time.sleep(2)           # the callback has the reason on screen already
        _rebuild_reader_state()
        return None

    # Make it the active book before restarting, so the reader comes back up
    # already showing it.
    reader.state_save(0, b"", txt)
    reader.show_message(("Converted!", 100, 40), ("Opening book...", 80, 70))
    try:
        import supervisor
        supervisor.reload()          # does not return
    except Exception as e:
        print(f"reload unavailable, opening in place: {e}")
    _rebuild_reader_state()
    return txt


