# ------------------------------------------------------------
# convert.py  -  EPUB conversion as its own program
# ------------------------------------------------------------
# The picker queues a book and asks CircuitPython to run THIS file instead of
# code.py on the next restart. That is the whole point of it: code.py, and every
# module it imports, stays resident for as long as it is the running program -
# on this board about 70KB the converter cannot have.
#
# Skipping the reader's buffers was not enough. A conversion boot that still ran
# code.py reached the archive with 46272 bytes free and no piece of it bigger
# than 1KB, so every chapter failed, including a 525-byte one:
#
#     After opening: 46272 free, largest block 1024
#     [1/75] text/part0000.html  300->525 bytes, 45072 free, largest 0
#       OUT OF MEMORY needing ~525 contiguous (largest block was 0)
#
# From the REPL, where code.py is not loaded, the same book converts. This file
# makes that condition repeatable: the panel, the converter, and nothing else.
#
# Deliberately module-level, with no `if __name__ == "__main__"` guard: whether
# CircuitPython sets __name__ to "__main__" for the file it runs is not
# documented, and a guarded file that does not match defines its functions and
# exits, looking exactly like a board that did nothing.
#
# Run by hand with nothing queued, it converts the first .epub in /books, which
# is what it did before.
import gc
import time

import board
import microcontroller
import adafruit_framebuf
from uc8151_circuitpython import UC8151

print("convert.py starting")

WIDTH, HEIGHT = 296, 128
BUF_SIZE = WIDTH * HEIGHT // 8

# The slot code.py writes the queued book into.
NVM_O_PENDING = 3902
PENDING_MAGIC = 0xC9
PENDING_MAX = 120


def load_pending():
    try:
        head = bytes(microcontroller.nvm[NVM_O_PENDING:NVM_O_PENDING + 2])
        if head[0] != PENDING_MAGIC or not 0 < head[1] <= PENDING_MAX:
            return ""
        start = NVM_O_PENDING + 2
        return bytes(microcontroller.nvm[start:start + head[1]]).decode()
    except Exception:
        return ""


def clear_pending():
    try:
        microcontroller.nvm[NVM_O_PENDING:NVM_O_PENDING + 1] = bytes([0])
    except Exception:
        pass


def set_active_book(path):
    """Point the reader's NVRAM at the freshly converted book.

    Written out here rather than imported from code.py, because importing
    code.py is exactly what this file exists to avoid. The layout is the one
    documented at the top of code.py's NVRAM section.
    """
    import struct
    nvm = microcontroller.nvm
    if struct.unpack("<I", bytes(nvm[0:4]))[0] != 0xEB00C5A7:
        return                       # no state yet; the reader will build it
    encoded = path.encode()[:198]
    count = struct.unpack("<H", bytes(nvm[4:6]))[0]
    idx = None
    for i in range(min(count, 15)):
        base = 8 + i * 256
        n = struct.unpack("<H", bytes(nvm[base + 56:base + 58]))[0]
        if bytes(nvm[base + 58:base + 58 + n]) == encoded:
            idx = i
            break
    if idx is None:
        if count >= 15:
            return                   # full; the picker can still reach it
        idx = count
        nvm[4:6] = struct.pack("<H", count + 1)
    base = 8 + idx * 256
    nvm[base:base + 4] = struct.pack("<I", 0)              # start of the book
    nvm[base + 4:base + 6] = struct.pack("<H", 0)          # no remainder
    nvm[base + 56:base + 58] = struct.pack("<H", len(encoded))
    nvm[base + 58:base + 58 + len(encoded)] = encoded
    nvm[6:8] = struct.pack("<H", idx)                      # make it active


def back_to_reader():
    """Hand the board back to the reader and restart. Does not return."""
    try:
        import supervisor
        try:
            supervisor.set_next_code_file("code.py")
        except Exception:
            pass                     # older builds run code.py anyway
        supervisor.reload()
    except Exception as e:
        print(f"could not restart into the reader: {e}")
    while True:
        time.sleep(1)


# --- panel ---------------------------------------------------------
# Buffers first, while the heap is whole. No vga2_8x16 either: the progress
# screen draws through framebuf's own small font, so the reader's 17KB text
# font is one more thing this program does not load.
working = bytearray(BUF_SIZE)
rotate_scratch = bytearray(BUF_SIZE)

display = UC8151(
    board.SPI(),
    cs=board.INKY_CS,
    dc=board.INKY_DC,
    rst=board.INKY_RST,
    busy=board.INKY_BUSY,
    rotation=270,
    use_framebuf_font=True,
    font_path="font5x8.bin",
    speed=4,
    no_flickering=True,
    full_update_period=0,
)
display.enable_quick_updates(True)
display._rotate_scratch = rotate_scratch
fb = adafruit_framebuf.FrameBuffer(working, WIDTH, HEIGHT, adafruit_framebuf.MHMSB)

BAR = (14, 56, WIDTH - 28, 20)
BAND_Y, BAND_H, STEP = 48, 64, 8
_state = {"px": -1, "drawn": False, "name": "", "done": 0, "total": 0}

NOTES = {"open": "Reading archive...", "cover": "Saving cover...",
         "readonly": "Filesystem is read-only!", "failed": "Conversion failed",
         "empty": "Nothing was written!", "partial": "Some chapters failed",
         "done": "Done"}


def draw(note=""):
    bx, by, bw, bh = BAR
    fb.fill(0)
    old_fb, old_raw = display.fb, display.raw_fb
    display.fb, display.raw_fb = fb, working
    try:
        display.text("Converting EPUB", 8, 6, 1)
        display.text(_state["name"][:36], 8, 26, 1)
        fb.fill_rect(bx, by, bw, 1, 1)
        fb.fill_rect(bx, by + bh - 1, bw, 1, 1)
        fb.fill_rect(bx, by, 1, bh, 1)
        fb.fill_rect(bx + bw - 1, by, 1, bh, 1)
        total, done = _state["total"], _state["done"]
        if total > 0:
            fill = ((bw - 4) * done) // total
            if fill > 0:
                fb.fill_rect(bx + 2, by + 2, fill, bh - 4, 1)
            display.text("%d / %d chapters" % (done, total), 8, 86, 1)
        if note:
            display.text(note[:36], 8, 104, 1)
    finally:
        display.fb, display.raw_fb = old_fb, old_raw

    rotated = display._rotate_framebuffer(working)
    if _state["drawn"] and not note:
        try:
            if display.update_partial(0, BAND_Y, WIDTH, BAND_H,
                                      fb=rotated, pre_rotated=True):
                return
        except Exception as e:
            print(f"partial update failed, using full: {e}")
    old_rot = display.rotation
    display.rotation = 0
    display.update(fb=rotated)
    display.rotation = old_rot
    _state["drawn"] = True


def progress(stage, done, total, name):
    if name:
        _state["name"] = name
    if total:
        _state["total"] = total
    if stage == "chapter":
        _state["done"] = done
        t = _state["total"]
        if t and done < t:
            px = ((BAR[2] - 4) * done) // t
            if px - _state["px"] < STEP:
                return               # too small a move to spend a refresh on
            _state["px"] = px
    draw(NOTES.get(stage, ""))


# --- convert -------------------------------------------------------
book = load_pending()
clear_pending()          # before the work: a crash must not repeat forever
gc.collect()

import epub_xtract       # last, so its cost lands on a settled heap

if not book:
    epub_xtract.main()   # run by hand: convert whatever is in /books
    print("convert.py finished - reset to go back to the reader")
else:
    _state["name"] = book.split("/")[-1]
    txt = None
    try:
        txt = epub_xtract.convert_book(book, progress=progress, keep_display=True)
    except Exception as e:
        print(f"conversion failed: {e}")
        _state["name"] = str(e)[:36]
        draw("Conversion failed")
        time.sleep(4)
    if txt:
        # Turn the cover into a sleep screen now, while this program still has
        # the board to itself. The reader could not: decoding needs the scaled
        # image held whole, and it has neither the free memory nor a heap in
        # one piece by the time it goes to sleep.
        draw("Making sleep screen...")
        try:
            import coverimg
            coverimg.render_for_book(txt)
        except Exception as e:
            print(f"no sleep screen: {e}")   # the reader keeps the last page
        try:
            set_active_book(txt)
        except Exception as e:
            print(f"could not record the new book: {e}")
        draw("Done - opening book")
        time.sleep(1)
    else:
        draw("FAILED - see .convert.log")
        time.sleep(6)
    back_to_reader()
