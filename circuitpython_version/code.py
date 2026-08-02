"""
CircuitPython E-book Reader for Badger 2040
Fully on-the-fly pagination - no filesystem state storage
State saved to NVRAM only
"""
import board
import displayio
import digitalio
import time
import os
import struct
import vga2_8x16
# import sans_serif_8x16 as vga2_8x16
import gc
import adafruit_framebuf
import analogio
import pwmio
import microcontroller
from uc8151_circuitpython import UC8151

# --- PENDING CONVERSION, READ BEFORE ANYTHING IS ALLOCATED ---
# Choosing an EPUB in the picker does not convert it there and then. It records
# the path here and restarts, so the conversion runs on a boot that has built
# none of the reader: no hyphenation patterns, no font, one screen buffer
# instead of five.
#
# Freeing all of that first is not the same thing and does not work. Opening an
# archive needs a 32KB streaming window in ONE piece, and after a session of
# reading there is no such piece left - releasing objects gives the bytes back
# but leaves the heap in fragments, so the converter failed where it had plenty
# of memory by the total:
#
#     Out of memory - convert over USB instead
#
# Offsets 3902-4023 are free: book entries end at 3848 and the font byte is at
# 3900-3901.
NVM = microcontroller.nvm
NVM_O_PENDING = 3902          # magic, length, then the path
PENDING_MAGIC = 0xC9
PENDING_MAX = 120


def load_pending():
    """Path of an EPUB queued for conversion, or "" if there is none."""
    try:
        head = bytes(NVM[NVM_O_PENDING:NVM_O_PENDING + 2])
        if head[0] != PENDING_MAGIC or not 0 < head[1] <= PENDING_MAX:
            return ""
        start = NVM_O_PENDING + 2
        return bytes(NVM[start:start + head[1]]).decode()
    except Exception:
        return ""


def save_pending(path):
    b = path.encode()[:PENDING_MAX]
    NVM[NVM_O_PENDING:NVM_O_PENDING + 2] = bytes([PENDING_MAGIC, len(b)])
    NVM[NVM_O_PENDING + 2:NVM_O_PENDING + 2 + len(b)] = b


def clear_pending():
    try:
        NVM[NVM_O_PENDING:NVM_O_PENDING + 1] = bytes([0])
    except Exception:
        pass


PENDING_CONVERT = load_pending()

# --- SCREEN BUFFERS, CLAIMED FIRST ---
# Every screen buffer is taken here, before the hyphenation patterns, the font
# and the display driver have allocated anything, because the collector does
# not move objects: whatever is asked for last has to fit in a gap left by
# everything before it. Allocated at the end instead, the last one failed on a
# board with fifteen times its size still free -
#
#     boot: 70160 bytes free, quick-back OFF
#
# which is not a shortage of memory but a shortage of anywhere to put it.
#
# The size is written out rather than read from the panel, which does not exist
# yet. Both orientations come to the same 4736 bytes on the Badger, and the
# driver section below checks the panel really is that size.
_BUF_SIZE = 296 * 128 // 8

raw_working_buffer = bytearray(_BUF_SIZE)
_early_rotate_scratch = bytearray(_BUF_SIZE)

# A conversion draws one progress screen and then restarts the board, so it
# needs no page buffers at all. Leaving them unallocated is most of the point
# of running it on its own boot: the converter gets an untouched heap rather
# than one with five screen buffers already carved out of it.
if PENDING_CONVERT:
    current_rotated_buffer = None
    next_rotated_buffer = None
else:
    current_rotated_buffer = bytearray(_BUF_SIZE)
    next_rotated_buffer = bytearray(_BUF_SIZE)

# Quick-back: a third page buffer holding the PREVIOUS page, so pressing up is
# instant like pressing down already is. Costs one more screen buffer (~4.7KB)
# but no extra rendering: on a page turn the page being left is already drawn,
# so it becomes the previous page just by rotating which buffer is which (and
# vice-versa when going back). Last of the five, so it is the one that loses if
# the board cannot hold them all - back-navigation then renders on demand.
QUICK_BACK = True and not PENDING_CONVERT
try:
    prev_rotated_buffer = bytearray(_BUF_SIZE) if QUICK_BACK else None
    QUICK_BACK_OK = QUICK_BACK
except MemoryError:
    print("quick-back disabled: not enough memory for a third page buffer")
    prev_rotated_buffer = None
    QUICK_BACK_OK = False

# Report missing data files once, before they fail somewhere less obvious. A
# missing module raises ImportError and names itself, so those need no check
# here; these do not. font5x8.bin only surfaces from inside a text() call, and
# a missing .pf font quietly falls back to another - so a half-copied drive can
# look like a rendering or memory bug rather than a missing file.
for _need in ("font5x8.bin", "hyphen_patterns.txt"):
    try:
        os.stat(_need)
    except OSError:
        print(f"MISSING FILE: {_need} - copy it from circuitpython_version/")

# A library at the drive root shadows the copy in /lib, and CircuitPython
# compiles a .py into RAM at import where an .mpy costs nothing. For
# adafruit_framebuf that is roughly 30KB - enough on this board to lose the
# quick-back buffer and fail a 3840-byte font load, neither of which points
# anywhere near the actual cause.
for _shadow in ("adafruit_framebuf.py",):
    try:
        os.stat(_shadow)
        os.stat("lib/" + _shadow[:-3] + ".mpy")
    except OSError:
        continue
    print(f"WASTING RAM: /{_shadow} shadows lib/{_shadow[:-3]}.mpy - "
          f"delete /{_shadow} to get its memory back")

# Optional on-device hyphenation. If the module or its pattern file is missing,
# hyphenation is disabled gracefully (plain word-wrapping still works).
try:
    import hyphenator
    if not PENDING_CONVERT:
        # 31.5KB, and a conversion never lays out a page. Skipped rather than
        # loaded and freed: freeing it back would leave the hole behind.
        hyphenator._load()  # force-load now so per-word calls can't fail on I/O
    _HYPHEN_OK = True
except Exception as _e:
    print(f"hyphenator unavailable: {_e}")
    _HYPHEN_OK = False

# Proportional reading fonts. The B button cycles through whichever of these
# are present on the device. The layout engine measures line fit in pixels via
# the active FONT. The picker and status bars still use the built-in monospace.
import propfont

FONT_FILES = [
    ("oldmono.pf", "Mono 8x16"),     # the original reader font (default)
    ("literata.pf", "Literata"),
    ("lexenddeca.pf", "Lexend Deca"),
]
AVAILABLE_FONTS = []
for _fp, _fn in FONT_FILES:
    try:
        open(_fp, "rb").close()
        AVAILABLE_FONTS.append((_fp, _fn))
    except Exception:
        pass
if not AVAILABLE_FONTS:
    AVAILABLE_FONTS = [("literata.pf", "Literata")]  # last resort; errors if truly missing

# One buffer, sized for the largest installed font and reused for every load.
# Claimed now, with the screen buffers, while the heap is whole. Switching
# fonts then allocates nothing at all - it used to need a fresh ~4KB in one
# piece, on a heap the reader had spent the session paginating into, so it
# worked when tested straight after launching and failed after actually
# reading for a while:
#
#     font switch error: memory allocation failed, allocating 4352 bytes
_font_buf = None
try:
    _biggest = 0
    for _fp, _fn in (() if PENDING_CONVERT else AVAILABLE_FONTS):
        try:
            _sz = os.stat(_fp)[6]
            if _sz > _biggest:
                _biggest = _sz
        except OSError:
            pass
    if _biggest:
        _font_buf = bytearray(_biggest)
except MemoryError:
    _font_buf = None      # fall back to allocating per load, as before

font_index = 0
# The progress screen draws through the driver's own text routine, so a
# conversion boot needs no reading font.
FONT = None if PENDING_CONVERT else propfont.PropFont(AVAILABLE_FONTS[0][0], buf=_font_buf)

LED_DUTY_CYCLE = 40
INACTIVITY_TIMEOUT = 300

# ---------------- LED -----------------
led = pwmio.PWMOut(board.USER_LED, frequency=1000, duty_cycle=0)
def led_on():
    duty_value = int((LED_DUTY_CYCLE / 100.0) * 65535)
    led.duty_cycle = duty_value
def led_off():
    led.duty_cycle = 0
led_on()

displayio.release_displays()

# Create books directory if needed
try:
    os.mkdir("/books")
except OSError:
    pass

# ---------------- NVRAM STATE STORAGE (Multi-book) -----------------
# NVRAM Layout (RP2040 has 4096 bytes):
# [0:4]     - Magic number 0xEB00C5A7
# [4:6]     - Number of book entries (uint16)
# [6:8]     - Currently active book index (uint16)
# [8:...]   - Book entries (variable, up to MAX_BOOKS)
#
# Each book entry (256 bytes fixed):
# [0:4]     - File offset (uint32)
# [4:6]     - Remainder length (uint16)
# [6:56]    - Remainder bytes (50 bytes max)
# [56:58]   - Path length (uint16)
# [58:256]  - Path string (198 bytes max)

NVM = microcontroller.nvm
NVRAM_MAGIC = 0xEB00C5A7

# Layout constants
NVM_O_MAGIC = 0
NVM_O_COUNT = 4
NVM_O_ACTIVE = 6
NVM_O_ENTRIES = 8

# Entry layout
ENTRY_SIZE = 256
ENTRY_O_OFFSET = 0
ENTRY_O_REM_LEN = 4
ENTRY_O_REM = 6
ENTRY_REM_MAX = 50
ENTRY_O_PATH_LEN = 56
ENTRY_O_PATH = 58
ENTRY_PATH_MAX = 198

# Max books we can store: (4096 - 8) / 256 = 15
MAX_BOOKS = 15

# Global settings live past the book-entry region (entries end at 8+15*256=3848).
NVM_O_FONT_MAGIC = 3900
NVM_O_FONT_INDEX = 3901
FONT_SETTINGS_MAGIC = 0x5A


def load_font_index():
    """Read the saved font choice from NVRAM (0 if unset)."""
    try:
        if bytes(NVM[NVM_O_FONT_MAGIC:NVM_O_FONT_MAGIC + 1])[0] == FONT_SETTINGS_MAGIC:
            return bytes(NVM[NVM_O_FONT_INDEX:NVM_O_FONT_INDEX + 1])[0]
    except Exception:
        pass
    return 0


def save_font_index(idx):
    """Persist the font choice to NVRAM."""
    try:
        NVM[NVM_O_FONT_MAGIC:NVM_O_FONT_MAGIC + 1] = bytes([FONT_SETTINGS_MAGIC])
        NVM[NVM_O_FONT_INDEX:NVM_O_FONT_INDEX + 1] = bytes([idx & 0xFF])
    except Exception as e:
        print(f"save_font_index error: {e}")

def _get_entry_base(index):
    """Get base offset for a book entry"""
    return NVM_O_ENTRIES + (index * ENTRY_SIZE)

def _read_entry(index):
    """Read a book entry from NVRAM"""
    base = _get_entry_base(index)
    try:
        offset = struct.unpack("<I", bytes(NVM[base+ENTRY_O_OFFSET:base+ENTRY_O_OFFSET+4]))[0]
        
        rem_len = struct.unpack("<H", bytes(NVM[base+ENTRY_O_REM_LEN:base+ENTRY_O_REM_LEN+2]))[0]
        remainder = b""
        if 0 < rem_len <= ENTRY_REM_MAX:
            remainder = bytes(NVM[base+ENTRY_O_REM:base+ENTRY_O_REM+rem_len])
        
        path_len = struct.unpack("<H", bytes(NVM[base+ENTRY_O_PATH_LEN:base+ENTRY_O_PATH_LEN+2]))[0]
        path = ""
        if 0 < path_len <= ENTRY_PATH_MAX:
            path = bytes(NVM[base+ENTRY_O_PATH:base+ENTRY_O_PATH+path_len]).decode("utf-8")
        
        return {"offset": offset, "remainder": remainder, "path": path}
    except Exception as e:
        print(f"_read_entry ERROR: {e}")
        return None

def _write_entry(index, offset, remainder, path):
    """Write a book entry to NVRAM"""
    base = _get_entry_base(index)
    try:
        NVM[base+ENTRY_O_OFFSET:base+ENTRY_O_OFFSET+4] = struct.pack("<I", offset)
        
        rem = remainder[:ENTRY_REM_MAX] if remainder else b""
        NVM[base+ENTRY_O_REM_LEN:base+ENTRY_O_REM_LEN+2] = struct.pack("<H", len(rem))
        if rem:
            NVM[base+ENTRY_O_REM:base+ENTRY_O_REM+len(rem)] = rem
        
        path_bytes = path.encode("utf-8")[:ENTRY_PATH_MAX]
        NVM[base+ENTRY_O_PATH_LEN:base+ENTRY_O_PATH_LEN+2] = struct.pack("<H", len(path_bytes))
        if path_bytes:
            NVM[base+ENTRY_O_PATH:base+ENTRY_O_PATH+len(path_bytes)] = path_bytes
    except Exception as e:
        print(f"_write_entry ERROR: {e}")

def _find_book_index(book_path):
    """Find index of a book in NVRAM, or -1 if not found"""
    try:
        count = struct.unpack("<H", bytes(NVM[NVM_O_COUNT:NVM_O_COUNT+2]))[0]
        for i in range(min(count, MAX_BOOKS)):
            entry = _read_entry(i)
            if entry and entry["path"] == book_path:
                return i
    except:
        pass
    return -1

def state_save(current_offset, current_remainder, book_path):
    """Save state for a book to NVRAM"""
    try:
        # Ensure magic is set
        magic = struct.unpack("<I", bytes(NVM[NVM_O_MAGIC:NVM_O_MAGIC+4]))[0]
        if magic != NVRAM_MAGIC:
            # Initialize fresh NVRAM
            NVM[NVM_O_MAGIC:NVM_O_MAGIC+4] = struct.pack("<I", NVRAM_MAGIC)
            NVM[NVM_O_COUNT:NVM_O_COUNT+2] = struct.pack("<H", 0)
            NVM[NVM_O_ACTIVE:NVM_O_ACTIVE+2] = struct.pack("<H", 0)
        
        count = struct.unpack("<H", bytes(NVM[NVM_O_COUNT:NVM_O_COUNT+2]))[0]
        
        # Find existing entry for this book
        book_index = _find_book_index(book_path)
        
        if book_index >= 0:
            # Update existing entry
            _write_entry(book_index, current_offset, current_remainder, book_path)
            NVM[NVM_O_ACTIVE:NVM_O_ACTIVE+2] = struct.pack("<H", book_index)
        else:
            # Add new entry
            if count < MAX_BOOKS:
                # Append new entry
                _write_entry(count, current_offset, current_remainder, book_path)
                NVM[NVM_O_ACTIVE:NVM_O_ACTIVE+2] = struct.pack("<H", count)
                NVM[NVM_O_COUNT:NVM_O_COUNT+2] = struct.pack("<H", count + 1)
            else:
                # Storage full - overwrite oldest (index 0), shift others
                for i in range(MAX_BOOKS - 1):
                    entry = _read_entry(i + 1)
                    if entry:
                        _write_entry(i, entry["offset"], entry["remainder"], entry["path"])
                # Write new entry at end
                _write_entry(MAX_BOOKS - 1, current_offset, current_remainder, book_path)
                NVM[NVM_O_ACTIVE:NVM_O_ACTIVE+2] = struct.pack("<H", MAX_BOOKS - 1)
                
    except Exception as e:
        print(f"state_save NVRAM ERROR: {e}")

def state_load_book(book_path):
    """Load state for a specific book from NVRAM"""
    try:
        magic = struct.unpack("<I", bytes(NVM[NVM_O_MAGIC:NVM_O_MAGIC+4]))[0]
        if magic != NVRAM_MAGIC:
            return 0, b""
        
        book_index = _find_book_index(book_path)
        if book_index >= 0:
            entry = _read_entry(book_index)
            if entry:
                return entry["offset"], entry["remainder"]
    except Exception as e:
        print(f"state_load_book ERROR: {e}")
    return 0, b""

def state_load_last_book():
    """Load the last active book path from NVRAM"""
    try:
        magic = struct.unpack("<I", bytes(NVM[NVM_O_MAGIC:NVM_O_MAGIC+4]))[0]
        if magic != NVRAM_MAGIC:
            return ""
        
        active = struct.unpack("<H", bytes(NVM[NVM_O_ACTIVE:NVM_O_ACTIVE+2]))[0]
        entry = _read_entry(active)
        if entry:
            return entry["path"]
    except Exception as e:
        print(f"state_load_last_book ERROR: {e}")
    return ""

def state_save_current():
    """Save current reading position to NVRAM"""
    global current_offset, current_remainder, text_file
    if not text_file:
        # No book open yet (e.g. the startup picker timed out and went to
        # sleep). Saving here would add an entry with an empty path, which
        # would then show up as a phantom book.
        return
    state_save(current_offset, current_remainder, text_file)

# ---------------- SAVE FREQUENCY CONTROL -----------------
# Only save to NVRAM periodically to extend flash life
# Flash typically rated for 100,000 erase cycles
PAGES_BETWEEN_SAVES = 10
pages_since_save = 0

def maybe_save_state():
    """Save state every N pages to reduce flash wear"""
    global pages_since_save
    pages_since_save += 1
    if pages_since_save >= PAGES_BETWEEN_SAVES:
        state_save_current()
        pages_since_save = 0

def force_save_state():
    """Force immediate save (for sleep, book switch, etc.)"""
    global pages_since_save
    state_save_current()
    pages_since_save = 0

# ---------------- CONFIG -----------------
TEXT_PADDING = 2
WIDTH = 296
HEIGHT = 128
TEXT_WIDTH = WIDTH - TEXT_PADDING*2
# Vertical text offset. The glyph box has a few empty rows above the caps, so
# starting at 0 (flush to the top) still looks like it has headroom, and it
# lets the bottom line's descenders clear the screen edge instead of clipping.
TEXT_TOP = 0
# Fit a fixed number of lines and distribute the height so the page fills the
# screen (leaving a little leading between lines) rather than packing lines
# tightly at box height and leaving a gap at the bottom. Pair the font size so
# its box height is about LINE_HEIGHT (the size-13 Literata box is 15px, ~1px
# taller than the 14px pitch, which only the tall glyphs like parens use).
LINES_PER_PAGE = 9
LINE_HEIGHT = (HEIGHT - TEXT_TOP) // LINES_PER_PAGE
BOOK_DIR = "/books"

# Full-justify wrapped lines so the right margin is flush (monospace: pad
# spaces between words). Purely a rendering choice - it does not affect
# pagination/offsets. Set False for a ragged right edge.
JUSTIFY_TEXT = True

# Refresh only the part of the screen that changed, where that is possible.
# Used by the book picker, where moving the selection changes just the two
# highlight bars. Partial refreshes leave a little more ghosting than full
# ones; set False to go back to full refreshes everywhere.
PARTIAL_UPDATES = True

# Hyphenate words that overflow a line (Frank Liang's algorithm, on-device).
# Fills lines more evenly and shrinks the gaps justification opens up. Only
# applied within a page - a word is never split across a page boundary, so the
# saved offset/remainder stays on whole-word boundaries. Requires hyphenator.py.
HYPHENATE = True

FONT_W_5X8 = 5
FONT_H_5X8 = 8

last_activity = time.monotonic()

# ---------------- PAGE HISTORY (RAM only) -----------------
# Rolling cache of recent page positions for backward navigation
PAGE_HISTORY_SIZE = 10
page_history = []  # List of (offset, remainder) tuples

def history_push(offset, remainder):
    """Push current position to history before advancing"""
    global page_history
    page_history.append((offset, remainder))
    if len(page_history) > PAGE_HISTORY_SIZE:
        page_history.pop(0)

def history_pop():
    """Pop previous position from history"""
    global page_history
    if page_history:
        return page_history.pop()
    return None

def history_peek():
    """Look at the previous position WITHOUT consuming it - used to pre-render
    the previous page. The actual back navigation still pops it, and because
    both read the same entry the pre-rendered page always matches where back
    will actually go."""
    if page_history:
        return page_history[-1]
    return None

def history_clear():
    """Clear page history"""
    global page_history
    page_history = []

# ---------------- DISPLAY -----------------
spi = board.SPI()

ORIGINAL_SPEED = 4
ORIGINAL_NO_FLICKERING = True
display = UC8151(
    spi,
    cs=board.INKY_CS,
    dc=board.INKY_DC,
    rst=board.INKY_RST,
    busy=board.INKY_BUSY,
    rotation=270,
    # external_font wins in the driver's text(), so vga2_8x16 draws every piece
    # of reader chrome and the two arguments below are never reached from
    # display.text(). font5x8.bin is still needed on the drive, though: the
    # status lines call temp_fb.text(font_name="font5x8.bin") directly, and
    # convert.py runs without an external font at all.
    external_font=vga2_8x16,
    use_framebuf_font=True,
    font_path="font5x8.bin",
    speed=ORIGINAL_SPEED,
    no_flickering=ORIGINAL_NO_FLICKERING,
    full_update_period=0
)

display.enable_quick_updates(True)

# --- BUFFERS ---
# Claimed at the very top of this file, before the hyphenation patterns, the
# font and the driver - see the block near the imports. All that is left here
# is to check the panel is the size they were sized for, and to hand the
# rotation buffer to the driver.
if display.width * display.height // 8 != _BUF_SIZE:
    # A different panel: the early guesses are the wrong size, so pay the
    # fragmentation cost rather than draw through buffers that do not fit.
    print(f"panel is not {WIDTH}x{HEIGHT}; reallocating buffers")
    _BUF_SIZE = display.width * display.height // 8
    raw_working_buffer = bytearray(_BUF_SIZE)
    current_rotated_buffer = bytearray(_BUF_SIZE)
    next_rotated_buffer = bytearray(_BUF_SIZE)
    _early_rotate_scratch = bytearray(_BUF_SIZE)
    if prev_rotated_buffer is not None:
        prev_rotated_buffer = bytearray(_BUF_SIZE)

display._rotate_scratch = _early_rotate_scratch
_early_rotate_scratch = None

prev_page_ready = False
prev_page_offset = 0
prev_page_remainder = b""

# One persistent FrameBuffer wrapping raw_working_buffer, reused for every
# screen render (reading pages, the book picker, reset/sleep messages) instead
# of constructing a fresh wrapper each time. Only one is ever needed live at
# once (the reader draws one screen, transmits it, then draws the next).
_scratch_fb = adafruit_framebuf.FrameBuffer(
    raw_working_buffer, display.width, display.height, adafruit_framebuf.MHMSB
)


# What is left once every buffer is placed - free memory AND the largest single
# block, because those answer different questions and only the second one
# explains a failure. The collector does not move objects, so free memory gets
# split into pieces; a 4736-byte buffer fails when no single piece is that big,
# however much is free in total. Seeing "70160 free" next to a failed 4736-byte
# allocation is what tells you the problem is fragmentation, not shortage.
gc.collect()
_probe = 1024
while _probe <= 65536:
    try:
        _b = bytearray(_probe)
        del _b
        _largest = _probe
        _probe *= 2
    except MemoryError:
        break
print(f"boot: {gc.mem_free()} bytes free, largest block >={_largest}, "
      f"quick-back {'on' if QUICK_BACK_OK else 'OFF (no contiguous block)'}")


class _ScratchFrame:
    """Context manager for drawing a screen: clears the shared scratch
    framebuffer (a single native .fill(0) call instead of a ~4700-iteration
    Python loop) and points display.fb/raw_fb at it so the existing
    display.text()/fill_rect()/etc. helpers draw into it, restoring the
    previous fb/raw_fb on exit.

        with _ScratchFrame() as temp_fb:
            display.text("Hello", 5, 5, 1)
            temp_fb.fill_rect(0, 0, 10, 10, 1)
    """
    def __enter__(self):
        self.old_fb = display.fb
        self.old_raw_fb = display.raw_fb
        _scratch_fb.fill(0)
        display.fb = _scratch_fb
        display.raw_fb = raw_working_buffer
        return _scratch_fb

    def __exit__(self, exc_type, exc_val, exc_tb):
        display.fb = self.old_fb
        display.raw_fb = self.old_raw_fb
        return False


next_page_ready = False
next_page_offset = 0
next_page_remainder = b""

# ---------------- BUTTONS -----------------
buttons = {}
for name, pin in [("up", board.SW_UP), ("down", board.SW_DOWN),
                  ("a", board.SW_A), ("b", board.SW_B), ("c", board.SW_C)]:
    b = digitalio.DigitalInOut(pin)
    b.direction = digitalio.Direction.INPUT
    b.pull = digitalio.Pull.DOWN
    buttons[name] = b

def button_pressed(btn):
    return btn.value

# ---------------- BATTERY SETUP -----------------
try:
    vref = digitalio.DigitalInOut(board.VREF_POWER)
    vref.direction = digitalio.Direction.OUTPUT
    vref.value = False
    vbus_sense = digitalio.DigitalInOut(board.VBUS_DETECT)
    vbus_sense.direction = digitalio.Direction.INPUT
    adc = analogio.AnalogIn(board.VBAT_SENSE)
except Exception as e:
    print(f"Battery pin setup failed: {e}")
    vref = None
    vbus_sense = None
    adc = None

def get_battery_status():
    global vref, vbus_sense, adc
    if not vref or not vbus_sense or not adc:
        return -1, False

    is_charging = vbus_sense.value
    vref.value = True
    time.sleep(0.02)
    
    raw_sum = 0
    for _ in range(5):
        raw_sum += adc.value
    reading = raw_sum / 5
    vref.value = False
    
    voltage = reading * (3.3 / 65535) * 3 
    percent = (voltage - 3.2) / (4.1 - 3.2) * 100
    
    return int(max(0, min(100, percent))), is_charging

def get_storage_status():
    try:
        stat = os.statvfs('/')
        total_bytes = stat[0] * stat[2]
        free_bytes = stat[0] * stat[3]
        
        mb_divisor = 1024 * 1024
        total_mb = total_bytes / mb_divisor
        free_mb = free_bytes / mb_divisor
        
        return f"Free: {free_mb:.1f}/{total_mb:.1f} MB" 
    except Exception:
        return "Storage N/A"

# ---------------- TEXT PROCESSING -----------------

def clean_word(word):
    """Map a word onto characters the fonts can actually draw.

    The fonts cover U+0020-U+00FF, so accented letters (French, Spanish,
    German, ...) render directly and only a few things need substituting:
    typographic quotes and dashes, and the handful of characters outside
    Latin-1 - notably the oe ligature, which is common in French.

    Accented CAPITALS need nothing here: the fonts store them as the plain
    letter, because their diacritics would sit above cap height and collide
    with the line above (see tools/build_font.py).

    The scan first means a pure-ASCII word - almost all of them - returns
    immediately instead of building ten intermediate strings.
    """
    for ch in word:
        if ch > "\x7e":
            break
    else:
        return word

    return (word.replace("“", '"').replace("”", '"')
                .replace("‘", "'").replace("’", "'")
                .replace("—", "-").replace("–", "-")
                .replace("…", "...")
                .replace("œ", "oe").replace("Œ", "OE")
                .replace("Ÿ", "Y")
                .replace(" ", " "))



def _pixel_chunks(word, max_px):
    """Break an over-long word (wider than a full line) into pieces that each
    fit within max_px pixels. Used only as a fallback for tokens with no
    hyphenation points that still overflow (e.g. long URLs).

    Tracks width with per-character increments and only joins at a chunk
    boundary - the old version called FONT.text_width(cur + ch) every
    character, which both re-measured the whole growing string each time
    (O(n^2)) and re-allocated a new concatenated string each time."""
    chunks = []
    cur_chars = []
    cur_w = 0
    for ch in word:
        cw = FONT.char_width(ch)
        if cur_chars and cur_w + cw > max_px:
            chunks.append("".join(cur_chars))
            cur_chars = [ch]
            cur_w = cw
        else:
            cur_chars.append(ch)
            cur_w += cw
    if cur_chars:
        chunks.append("".join(cur_chars))
    return chunks


def paginate_text(file_path, start_offset, remainder=b"", hyphenate=True):
    """Paginate one page of text starting from offset with optional remainder.

    hyphenate=False skips hyphenation for speed when the page's *lines* are
    thrown away and only next_offset is needed (e.g. the fast-advance skip loop).
    Page boundaries stay on whole words either way, so the offset is still a
    valid resume/render point."""
    try:
        with open(file_path, "rb") as f:
            f.seek(start_offset)
            lines = []
            line_count = 0
            next_offset = -1
            # The line currently being built, as a list of words plus its
            # running pixel width - NOT a string rebuilt via concatenation on
            # every word. Persists across source lines so consecutive non-blank
            # lines flow together as one paragraph (only a blank line ends a
            # paragraph). current_width always equals
            # FONT.text_width(" ".join(current_words)) exactly; the join only
            # happens once, when a line is flushed to `lines`. Rebuilding a
            # growing string on every word (the old approach) was a major
            # source of RP2040 heap fragmentation - see hyphenator.py's
            # _lookup docstring for the same issue in the hyphenation search.
            current_words = []
            current_width = 0

            while line_count < LINES_PER_PAGE:
                pos = f.tell()
                
                if remainder:
                    line_bytes = remainder
                    pos = start_offset  # The remainder conceptually "starts" at start_offset
                    f.seek(start_offset + len(remainder))  # File continues after remainder
                    remainder = b""
                else:
                    line_bytes = f.readline()
                
                if not line_bytes:
                    # EOF: flush the paragraph currently being built.
                    if current_words:
                        lines.append(" ".join(current_words).encode("utf-8", "ignore"))
                        line_count += 1
                        current_words = []
                        current_width = 0
                    next_offset = f.tell()
                    break

                line = line_bytes.rstrip(b"\r\n")

                if not line:
                    # Blank line ends a paragraph: flush its last line first, then
                    # emit the blank separator.
                    if current_words:
                        lines.append(" ".join(current_words).encode("utf-8", "ignore"))
                        line_count += 1
                        current_words = []
                        current_width = 0
                        if line_count >= LINES_PER_PAGE:
                            # Page filled by the paragraph tail; absorb this blank
                            # line into the break (next page continues after it).
                            next_offset = f.tell()
                            break
                    lines.append(b"")
                    line_count += 1
                    if line_count >= LINES_PER_PAGE:
                        next_offset = f.tell()
                        break
                    continue
                
                try:
                    line_str_raw = line.decode("utf-8", "ignore")
                except:
                    try:
                        line_str_raw = line.decode("latin-1", "ignore")
                    except:
                        line_str_raw = ''.join(chr(b) if b < 128 else '?' for b in line)
                
                words_raw = line_str_raw.split(" ")
                word_count = 0
                
                for raw_word in words_raw:
                    if not raw_word:
                        word_count += 1
                        continue
                        
                    word_clean = clean_word(raw_word)

                    # --- Over-long word (wider than a full line): hard-break it
                    # into pixel-sized chunks instead of overflowing off the
                    # display. We only page-break at whole-word boundaries, so the
                    # byte offset/remainder accounting stays exact.
                    if FONT.text_width(word_clean) > TEXT_WIDTH:
                        # Flush any partial line first - the long word starts fresh.
                        if current_words:
                            lines.append(" ".join(current_words).encode("utf-8", "ignore"))
                            line_count += 1
                            current_words = []
                            current_width = 0
                            if line_count >= LINES_PER_PAGE:
                                consumed_raw = words_raw[:word_count]
                                consumed_raw_str = " ".join(consumed_raw)
                                if len(consumed_raw) > 0 and word_count < len(words_raw):
                                    consumed_raw_str += " "
                                byte_idx = len(consumed_raw_str.encode("utf-8", "ignore"))
                                remainder = line_bytes[byte_idx:]
                                extra_skip = 0
                                while remainder.startswith(b' '):
                                    remainder = remainder[1:]
                                    extra_skip += 1
                                next_offset = pos + byte_idx + extra_skip
                                break

                        chunks = _pixel_chunks(word_clean, TEXT_WIDTH)

                        # Place the whole word only if it fits in the lines left on
                        # this page; otherwise defer it (unconsumed) to the next page.
                        # On a fresh page it cannot fit anywhere, so place what we can
                        # to guarantee forward progress.
                        if line_count + len(chunks) <= LINES_PER_PAGE or line_count == 0:
                            placed = 0
                            for chunk in chunks[:-1]:
                                if line_count >= LINES_PER_PAGE:
                                    break
                                lines.append(chunk.encode("utf-8", "ignore"))
                                line_count += 1
                                placed += 1
                            if line_count < LINES_PER_PAGE and placed == len(chunks) - 1:
                                current_words = [chunks[-1]]
                                current_width = FONT.text_width(chunks[-1])
                            else:
                                current_words = []
                                current_width = 0
                            word_count += 1
                            continue
                        else:
                            consumed_raw = words_raw[:word_count]
                            consumed_raw_str = " ".join(consumed_raw)
                            if len(consumed_raw) > 0 and word_count < len(words_raw):
                                consumed_raw_str += " "
                            byte_idx = len(consumed_raw_str.encode("utf-8", "ignore"))
                            remainder = line_bytes[byte_idx:]
                            extra_skip = 0
                            while remainder.startswith(b' '):
                                remainder = remainder[1:]
                                extra_skip += 1
                            next_offset = pos + byte_idx + extra_skip
                            break

                    word_w = FONT.text_width(word_clean)
                    prospective = current_width + (FONT.space_w + word_w if current_words else word_w)

                    if prospective <= TEXT_WIDTH:
                        current_words.append(word_clean)
                        current_width = prospective
                        word_count += 1
                    else:
                        # Word doesn't fit. Try to hyphenate a prefix onto this line
                        # first - but never across a PAGE boundary (that would leave
                        # half a word in the saved remainder). So only when the prefix
                        # line won't be the page's last line; then the whole word is
                        # consumed on this page and the offset stays whole-word.
                        if (hyphenate and HYPHENATE and _HYPHEN_OK
                                and line_count < LINES_PER_PAGE - 1):
                            used = current_width + (FONT.space_w if current_words else 0)
                            head, rest = hyphenator.hyphenate_split(word_clean, TEXT_WIDTH - used, FONT.text_width)
                            if head:
                                # head already includes its trailing hyphen (soft or existing)
                                if current_words:
                                    current_words.append(head)
                                    line_out = " ".join(current_words)
                                else:
                                    line_out = head
                                lines.append(line_out.encode("utf-8", "ignore"))
                                line_count += 1
                                current_words = [rest]
                                current_width = FONT.text_width(rest)
                                word_count += 1
                                continue

                        lines.append(" ".join(current_words).encode("utf-8", "ignore"))
                        line_count += 1

                        if line_count >= LINES_PER_PAGE:
                            consumed_raw = words_raw[:word_count]
                            consumed_raw_str = " ".join(consumed_raw)

                            if len(consumed_raw) > 0 and word_count < len(words_raw):
                                consumed_raw_str += " "

                            byte_idx = len(consumed_raw_str.encode("utf-8", "ignore"))
                            remainder = line_bytes[byte_idx:]

                            extra_skip = 0
                            while remainder.startswith(b' '):
                                remainder = remainder[1:]
                                extra_skip += 1

                            next_offset = pos + byte_idx + extra_skip
                            break

                        current_words = [word_clean]
                        current_width = word_w
                        word_count += 1
                
                if next_offset != -1:
                    break

                # End of this source line - do NOT flush current_words; the
                # paragraph continues on the next line. It gets flushed at a blank
                # line, at EOF, or when the next line's words wrap it.

            if next_offset == -1:
                # Loop ended without an explicit page break (e.g. a pathological
                # over-long token filled the page): flush any trailing text.
                if current_words:
                    lines.append(" ".join(current_words).encode("utf-8", "ignore"))
                    line_count += 1
                    current_words = []
                    current_width = 0
                next_offset = f.tell()
            
            gc.collect()
            return lines, next_offset, remainder
            
    except OSError as e:
        print(f"ERROR: File not found: {file_path}")
        gc.collect()
        return [], start_offset, b""
    except Exception as e:
        print(f"ERROR: Paginate failure: {e}")
        gc.collect()
        return [], start_offset, b""

def _paragraph_start(file_path, pos):
    """Return the start of the paragraph at/just before byte `pos` (the position
    after the previous blank line), or 0 / `pos` if none is found nearby. Used to
    align the back-scan so its re-paginated chain lines up with the real one."""
    if pos <= 0:
        return 0
    try:
        start = max(0, pos - 1200)
        with open(file_path, "rb") as f:
            f.seek(start)
            chunk = f.read(pos - start)
        i = chunk.rfind(b"\n\n")
        if i >= 0:
            return start + i + 2
        return 0 if start == 0 else pos
    except Exception:
        return pos


def find_previous_page(target_offset):
    """Find a page position roughly one page before target_offset by scanning
    backwards. This is only the fallback used when the RAM page-history is empty
    (e.g. right after opening/resuming a book); normal back-navigation pops the
    exact history. Because re-paginated page boundaries can be phase-shifted from
    the real chain, an exact match isn't guaranteed - so on a miss we return
    whichever nearby boundary is closest to a full page back, never a boundary
    only a few lines up."""
    global text_file

    if target_offset == 0:
        return 0, b""

    # Start at a paragraph boundary before the target for better alignment.
    search_start = _paragraph_start(text_file, max(0, target_offset - 3000))

    offset = search_start
    remainder = b""
    prev_offset = 0
    prev_remainder = b""

    while offset < target_offset:
        # hyphenate=False keeps this scan fast (no Liang calls). The boundaries
        # then differ slightly from the real (hyphenated) chain, so we rarely
        # land exactly on target - that's fine, we just want ~one page back.
        lines, next_offset, next_remainder = paginate_text(
            text_file, offset, remainder, hyphenate=False)

        if not lines or next_offset <= offset:
            break

        if next_offset >= target_offset:
            if next_offset == target_offset:
                return offset, remainder  # exact previous page
            # Overshoot: target sits inside this page. Return the boundary before
            # it so "back" always moves ~a full page, never just a few lines.
            return prev_offset, prev_remainder

        prev_offset = offset
        prev_remainder = remainder
        offset = next_offset
        remainder = next_remainder

    if search_start == 0:
        return 0, b""

    return prev_offset, prev_remainder

# ---------------- RENDERING -----------------
def render_page_to_buffer(page_offset, page_remainder, target_rotated_buffer):
    """Render a page to the target buffer."""
    global text_file
    
    try:
        with _ScratchFrame() as temp_fb:
            lines, _, _ = paginate_text(text_file, page_offset, page_remainder)

            y = TEXT_TOP
            n_lines = len(lines)
            for i in range(n_lines):
                line = lines[i]
                if line:
                    try:
                        # Already smart-quote/dash-cleaned in paginate_text (every
                        # byte here comes from word_clean, which went through that
                        # replace() chain once already) - no need to redo it per
                        # render, which just adds allocation churn for no effect.
                        text = line.decode("utf-8", "replace")
                        # Justify only interior lines that are followed by more text
                        # on this page (widen the spaces to the full text width). The
                        # last line of a paragraph (next line blank or end of page)
                        # stays ragged; line 0 is skipped so a full line never
                        # collides with the top-right battery indicator.
                        if JUSTIFY_TEXT and i > 0 and i + 1 < n_lines and lines[i + 1]:
                            FONT.draw_justified(temp_fb, text, TEXT_PADDING, y, 1,
                                                TEXT_WIDTH)
                        else:
                            FONT.draw(temp_fb, text, TEXT_PADDING, y, 1)
                    except:
                        pass
                y += LINE_HEIGHT

            # Battery indicator
            pct, charging = get_battery_status()
            if charging:
                 status_text = "USB"
            elif pct >= 0:
                 status_text = f"{pct}"
            else:
                 status_text = ""

            if status_text:
                STATUS_X = WIDTH - (len(status_text) * FONT_W_5X8) - TEXT_PADDING
                STATUS_Y = TEXT_PADDING
                temp_fb.text(status_text, STATUS_X, STATUS_Y, 1, font_name="font5x8.bin")

            # Progress bar
            try:
                file_stats = os.stat(text_file)
                total_size = file_stats[6]

                if total_size > 0:
                    progress_ratio = page_offset / total_size
                    progress_ratio = max(0.0, min(1.0, progress_ratio))
                    progress_width = int(progress_ratio * WIDTH)
                    progress_width = max(1, min(WIDTH, progress_width))
                    temp_fb.fill_rect(0, HEIGHT - 1, progress_width, 1, 1)
            except:
                pass
    finally:
        gc.collect()

    # Slice assignment copies at C speed; the byte-at-a-time Python loop this
    # replaces ran 4736 iterations on every page render.
    target_rotated_buffer[:] = display._rotate_framebuffer(raw_working_buffer)

def update_display_fast(rotated_buffer, blocking=True):
    old_rot = display.rotation
    display.rotation = 0 
    result = display.update(blocking=blocking, fb=rotated_buffer)
    display.rotation = old_rot
    return result

def wait_for_display():
    display.wait_ready()


def show_message(*items, blocking=True):
    """Draw a full-screen message and push it to the panel.

    Each item is (text, x, y) in the monospace UI font. Used for the reset,
    sleep and status screens, which otherwise all repeated the same
    clear / draw / rotate / update dance.
    """
    with _ScratchFrame():
        for text, x, y in items:
            display.text(text, x, y, 1)
    rotated = display._rotate_framebuffer(raw_working_buffer)
    update_display_fast(rotated, blocking=blocking)


def prerender_next():
    """Render the page AFTER the current one into next_rotated_buffer, so a
    forward press can swap to it instantly."""
    global next_page_ready, next_page_offset, next_page_remainder
    lines, next_off, next_rem = paginate_text(text_file, current_offset, current_remainder)
    if lines and next_off > current_offset:
        next_page_offset = next_off
        next_page_remainder = next_rem
        render_page_to_buffer(next_page_offset, next_page_remainder, next_rotated_buffer)
        next_page_ready = True
    else:
        next_page_ready = False


def prerender_prev():
    """Render the page BEFORE the current one into prev_rotated_buffer, so a
    back press can swap to it instantly. Uses the same source the back press
    will use (page history first, then the find_previous_page fallback), so
    the pre-rendered page always matches where back actually goes."""
    global prev_page_ready, prev_page_offset, prev_page_remainder
    prev_page_ready = False
    if not QUICK_BACK_OK or current_offset <= 0:
        return
    pos = history_peek()
    if pos is None:
        pos = find_previous_page(current_offset)
    if not pos:
        return
    p_off, p_rem = pos
    if p_off == current_offset and p_rem == current_remainder:
        return  # degenerate - would not move
    prev_page_offset = p_off
    prev_page_remainder = p_rem
    render_page_to_buffer(p_off, p_rem, prev_rotated_buffer)
    prev_page_ready = True

# ---------------- FILE PICKER -----------------
def list_books():
    """Readable books, plus any EPUB that has not been converted yet.

    An EPUB whose .txt already exists is left out: the two would sit next to
    each other in the list under the same title, and picking the EPUB would
    only redo work that is already on disk.
    """
    texts = []
    epubs = []
    try:
        for f in os.listdir(BOOK_DIR):
            if f.startswith("."):
                continue
            if f.endswith(".txt"):
                texts.append(BOOK_DIR + "/" + f)
            elif f.lower().endswith(".epub"):
                epubs.append(BOOK_DIR + "/" + f)
    except OSError:
        pass

    have = set(texts)
    unconverted = [e for e in epubs if e[:-5] + ".txt" not in have]
    return sorted(texts) + sorted(unconverted)


def is_epub(path):
    return path.lower().endswith(".epub")

def _draw_book_list(books, selected, per_page, highlight=True):
    """Draw the book list with `selected` highlighted and return the rotated
    buffer, ready to hand to display.update().

    Renders one screen on demand into the shared scratch frame. The previous
    version pre-rendered a full-page copy for every selectable row - up to six
    4,736-byte buffers, ~28KB held at once, which is far more than the
    allocations that were causing MemoryError crashes.
    """
    offset = (selected // per_page) * per_page

    with _ScratchFrame() as temp_fb:
        display.text("Select Book:  (.epub converts)", 5, 5, 1)

        for i in range(per_page):
            idx = offset + i
            if idx >= len(books):
                break
            name = books[idx].split("/")[-1]
            if len(name) > 33:
                name = name[:30] + "..."
            y = 25 + i * 16

            if highlight and idx == selected:
                # Bar starts one pixel BELOW the old y-2 so it covers exactly
                # the rows the text can ink (y..y+14; row 15 of the 8x16 cell is
                # always blank) and so consecutive bands tile without
                # overlapping. That makes a highlighted row the exact inverse of
                # an unhighlighted one, which is what _xor_row_band relies on.
                display.fb.fill_rect(2, y - 1, WIDTH - 4, 16, 1)
                display.text(name, 5, y, 0)
            else:
                display.text(name, 5, y, 1)

        if len(books) > per_page:
            page = offset // per_page + 1
            total = (len(books) + per_page - 1) // per_page
            display.text(f"{page}/{total}", WIDTH - (vga2_8x16.WIDTH * 5),
                         HEIGHT - vga2_8x16.HEIGHT - 10, 1)

        storage_status = get_storage_status()
        STATUS_X = WIDTH - (len(storage_status) * FONT_W_5X8) - TEXT_PADDING
        STATUS_Y = HEIGHT - FONT_H_5X8 - TEXT_PADDING
        temp_fb.text(storage_status, STATUS_X, STATUS_Y, 1, font_name="font5x8.bin")

    return display._rotate_framebuffer(raw_working_buffer)


def _xor_row_band(buf, row):
    """Invert the highlight band for `row` inside a ROTATED screen buffer.

    Highlighting a row is exactly inverting the pixels of its bar, so moving
    the selection is two inversions - no redraw and no rotation. That matters:
    on this device redrawing the list costs a few hundred milliseconds and
    rotating it a hundred more, while a partial refresh does NOT shorten the
    panel's refresh cycle (the waveform runs the same frames either way), so
    that work is pure added latency.

    The bands are laid out to make this cheap: row r covers logical rows
    25+16r-1 .. +16, which is exactly two whole 8-pixel banks starting at
    bank 3+2r, and consecutive rows never overlap. In the rotated buffer a
    logical column becomes a row, so this walks the bar's x range and flips
    two bytes each.
    """
    bank = 3 + 2 * row
    row_bytes = display.physical_width >> 3      # 16
    # The bar spans logical x 2..WIDTH-3; rotation maps x -> WIDTH-1-x, so that
    # is rotated rows 2..WIDTH-3 as well.
    for ny in range(2, WIDTH - 2):
        base = ny * row_bytes + bank
        buf[base] ^= 0xFF
        buf[base + 1] ^= 0xFF


# ---------------- EPUB CONVERSION -----------------
# The picker lists unconverted EPUBs alongside the books; choosing one lands
# here. The screen drawing and the reload live in convert_ui.py rather than in
# this file, because code.py is compiled into RAM at every boot and stays there
# for the whole session: 5KB of source that runs only while converting a book
# was 5KB permanently unavailable to the reader, and this board is close enough
# to the edge that it cost the quick-back buffer.


def convert_epub(epub_path):
    """Queue the EPUB and restart, so the conversion gets an untouched heap.

    Converting here instead fails on a heap the reader has been paginating
    into: the archive needs a 32KB streaming window in one piece, and freeing
    the reader's own buffers first returns the bytes without closing the gaps.

    Only returns if the restart could not happen - an IDE holding the serial
    port sends the board to the REPL instead - in which case it falls back to
    converting in place, which is what it used to do always.
    """
    try:
        save_pending(epub_path)
    except Exception as e:
        print(f"could not queue the conversion: {e}")
    show_message(("Converting EPUB", 75, 40), ("Restarting...", 90, 70))
    try:
        import supervisor
        # Run convert.py rather than this file. Restarting alone was not
        # enough: code.py and its modules stay resident while it is the running
        # program, and the converter reached the archive with 46KB free and no
        # piece of it over 1KB, failing on a 525-byte chapter. convert.py loads
        # the panel and the converter and nothing else.
        try:
            supervisor.set_next_code_file("convert.py")
        except Exception as e:
            print(f"set_next_code_file unavailable: {e}")
        supervisor.reload()          # does not return
    except Exception as e:
        print(f"reload unavailable, converting in place: {e}")
    clear_pending()
    import convert_ui
    return convert_ui.convert_epub(epub_path)


def file_picker():
    """Show the list of books and return the chosen path, or None if nothing
    was chosen (no books, or the picker timed out and went to sleep)."""
    global first_display_update, last_activity
    books = list_books()

    if not books:
        display.fb.fill(0)
        display.text("No books found!", 10, 40, 1)

        # Full refresh if first display
        if first_display_update:
            display.set_speed(0, no_flickering=False)
            display.update()
            display.set_speed(ORIGINAL_SPEED, no_flickering=ORIGINAL_NO_FLICKERING)
            first_display_update = False
        else:
            display.update()

        time.sleep(2)
        return None

    selected = 0
    per_page = 6
    page_shown = -1        # which page of books is on the panel
    row_shown = None       # which row is highlighted on the panel
    screen = None          # the page, rotated, ready for the panel

    while True:
        offset = (selected // per_page) * per_page
        row = selected - offset

        if offset != page_shown:
            # New page of books: draw it once WITHOUT a highlight and rotate it
            # once. `screen` is the driver's rotation buffer, so nothing else
            # may rotate while the picker owns it.
            _draw_book_list(books, selected, per_page, highlight=False)
            screen = display._rotate_framebuffer(raw_working_buffer)
            _xor_row_band(screen, row)          # apply the highlight

            old_rot = display.rotation
            display.rotation = 0
            if first_display_update:
                display.set_speed(0, no_flickering=False)
                display.update(fb=screen)
                display.set_speed(ORIGINAL_SPEED, no_flickering=ORIGINAL_NO_FLICKERING)
                first_display_update = False
            else:
                display.update(fb=screen)
            display.rotation = old_rot

            page_shown, row_shown = offset, row
            gc.collect()
            led_off()

        elif row != row_shown:
            # Same list, the highlight just moved. Flip the two bands - no
            # redraw and no rotation, which is what keeps this quick.
            _xor_row_band(screen, row_shown)    # remove the old highlight
            _xor_row_band(screen, row)          # draw the new one

            lo, hi = min(row_shown, row), max(row_shown, row)
            band_y = 25 + lo * 16 - 1
            band_h = (25 + hi * 16 - 1 + 16) - band_y

            done = False
            if PARTIAL_UPDATES:
                try:
                    done = display.update_partial(0, band_y, WIDTH, band_h,
                                                  fb=screen, pre_rotated=True)
                except Exception as e:
                    print(f"partial update failed, using full: {e}")
                    done = False
            if not done:
                old_rot = display.rotation
                display.rotation = 0
                display.update(fb=screen)
                display.rotation = old_rot

            row_shown = row
            led_off()

        # Check the select button FIRST. When it shared an if/elif chain with
        # up/down, a button reading as held would stop selection working at all.
        if button_pressed(buttons["a"]):
            last_activity = time.monotonic()
            while button_pressed(buttons["a"]):
                time.sleep(0.05)
            return books[selected]

        if button_pressed(buttons["down"]):
            last_activity = time.monotonic()
            selected = (selected + 1) % len(books)
            time.sleep(0.15)

        elif button_pressed(buttons["up"]):
            last_activity = time.monotonic()
            selected = (selected - 1) % len(books)
            time.sleep(0.15)

        # The picker runs its own polling loop, so it has to honour the
        # inactivity timeout itself - otherwise leaving the device sitting in
        # the picker would keep it awake until the battery ran down.
        if check_inactivity():
            return None

        time.sleep(0.05)

# ---------------- NAVIGATION -----------------
# All the page/buffer state transitions live here, deliberately free of button
# polling and press timing. That keeps the main loop a thin dispatcher, and it
# lets tools/test_quickback.py import and drive these directly (with the
# rendering and display calls stubbed) instead of re-implementing them.

def nav_page_down():
    """Advance one page, using the pre-rendered next page when available.

    Returns (advanced, prev_came_free): whether the position moved, and whether
    the previous-page buffer was filled for free by the rotation - if it was,
    the caller must NOT call prerender_prev() and re-render it needlessly.
    """
    global current_offset, current_remainder
    global current_rotated_buffer, next_rotated_buffer, prev_rotated_buffer
    global next_page_ready
    global prev_page_ready, prev_page_offset, prev_page_remainder

    if next_page_ready:
        history_push(current_offset, current_remainder)
        leaving_offset, leaving_remainder = current_offset, current_remainder
        prev_came_free = False
        if QUICK_BACK_OK:
            # Rotate all three: the page being left is already drawn, so it
            # becomes the previous page for free; the pre-rendered next becomes
            # current; the stale prev buffer is recycled as the new next.
            prev_rotated_buffer, current_rotated_buffer, next_rotated_buffer = (
                current_rotated_buffer, next_rotated_buffer, prev_rotated_buffer)
            prev_page_offset, prev_page_remainder = leaving_offset, leaving_remainder
            prev_page_ready = True
            prev_came_free = True
        else:
            current_rotated_buffer, next_rotated_buffer = (
                next_rotated_buffer, current_rotated_buffer)
        current_offset = next_page_offset
        current_remainder = next_page_remainder
        update_display_fast(current_rotated_buffer)
        next_page_ready = False
        return True, prev_came_free

    # Nothing pre-rendered - paginate and draw on demand.
    if current_offset >= 0:
        lines, next_off, next_rem = paginate_text(
            text_file, current_offset, current_remainder)
        if lines and next_off > current_offset:
            history_push(current_offset, current_remainder)
            leaving_offset, leaving_remainder = current_offset, current_remainder
            prev_came_free = False
            if QUICK_BACK_OK:
                # Keep the drawn page we're leaving as the previous page and
                # draw the new page into the recycled prev buffer.
                prev_rotated_buffer, current_rotated_buffer = (
                    current_rotated_buffer, prev_rotated_buffer)
                prev_page_offset, prev_page_remainder = leaving_offset, leaving_remainder
                prev_page_ready = True
                prev_came_free = True
            current_offset = next_off
            current_remainder = next_rem
            render_page_to_buffer(current_offset, current_remainder, current_rotated_buffer)
            update_display_fast(current_rotated_buffer)
            return True, prev_came_free

    return False, False


def nav_fast_advance(max_pages=49):
    """Long-press skip: run the offset forward without rendering the pages
    passed over, then draw where we land and rebuild both neighbours."""
    global current_offset, current_remainder

    for i in range(max_pages):
        # Skip hyphenation here: these pages are only used to advance the
        # offset, never rendered, so we don't pay for hyphenating them.
        lines, next_off, next_rem = paginate_text(
            text_file, current_offset, current_remainder, hyphenate=False)
        if not lines or next_off <= current_offset:
            break
        # Don't save every page to history during fast advance
        if i % 10 == 0:
            history_push(current_offset, current_remainder)
            gc.collect()
        current_offset = next_off
        current_remainder = next_rem

    render_page_to_buffer(current_offset, current_remainder, current_rotated_buffer)
    update_display_fast(current_rotated_buffer)
    # Jumped far, so the buffered neighbours are no longer adjacent
    prerender_next()
    prerender_prev()


def nav_page_up():
    """Go back one page, instantly when the previous page is already buffered.
    Returns True if the position moved."""
    global current_offset, current_remainder
    global current_rotated_buffer, next_rotated_buffer, prev_rotated_buffer
    global next_page_ready, next_page_offset, next_page_remainder
    global prev_page_ready

    if current_offset <= 0:
        return False

    # Try to get previous page from history, else calculate it
    prev = history_pop()
    if not prev:
        prev = find_previous_page(current_offset)

    next_came_free = False
    if (QUICK_BACK_OK and prev_page_ready
            and prev_page_offset == prev[0]
            and prev_page_remainder == prev[1]):
        # QUICK BACK: the previous page is already drawn, so just rotate. The
        # page being left is already drawn too, so it becomes the next page for
        # free; the stale next buffer is recycled as prev.
        next_rotated_buffer, current_rotated_buffer, prev_rotated_buffer = (
            current_rotated_buffer, prev_rotated_buffer, next_rotated_buffer)
        next_page_offset, next_page_remainder = current_offset, current_remainder
        next_page_ready = True
        next_came_free = True
        current_offset, current_remainder = prev
        prev_page_ready = False
        update_display_fast(current_rotated_buffer)
    else:
        current_offset, current_remainder = prev
        render_page_to_buffer(current_offset, current_remainder, current_rotated_buffer)
        update_display_fast(current_rotated_buffer)

    if not next_came_free:
        prerender_next()
    prerender_prev()
    return True


# ---------------- INPUT HANDLERS -----------------

def cycle_font():
    """B button: switch to the next installed reading font, persist the choice
    and redraw. The page reflows because the metrics differ, but the byte
    offset stays a valid starting point."""
    global font_index, FONT

    if len(AVAILABLE_FONTS) < 2:
        return
    previous = font_index
    font_index = (font_index + 1) % len(AVAILABLE_FONTS)
    try:
        try:
            # Reads into the buffer claimed at startup, so nothing is allocated
            # here and there is no block for a fragmented heap to refuse. The
            # old font's data is overwritten in place - fine, because FONT is
            # the only reference to it and is replaced on the same line.
            FONT = propfont.PropFont(AVAILABLE_FONTS[font_index][0], buf=_font_buf)
        except MemoryError:
            # Only reachable when there is no shared buffer (it could not be
            # claimed at startup) and a per-load allocation is being made after
            # all. Release the old font and retry - but only now, after the
            # ordinary attempt has failed, so that a switch which then fails
            # cannot leave the reader with no font at all.
            FONT = None
            gc.collect()
            FONT = propfont.PropFont(AVAILABLE_FONTS[font_index][0], buf=_font_buf)
        save_font_index(font_index)
        gc.collect()
        render_page_to_buffer(current_offset, current_remainder, current_rotated_buffer)
        update_display_fast(current_rotated_buffer)
        # The buffered neighbours were drawn in the old font; rebuild both.
        prerender_next()
        prerender_prev()
    except Exception as e:
        print(f"font switch error: {e}")
        # On battery there is no serial, so a failed press is otherwise just a
        # button that does nothing - which is how this went unexplained: it
        # worked every time it was tried straight after launching from the IDE,
        # and failed after actually reading for a while.
        try:
            show_message(("Font switch failed", 70, 40), (str(e)[:36], 8, 70))
            time.sleep(1.5)
        except Exception:
            pass
        # font_index and FONT have to agree: leaving the index on the font that
        # failed to load would report the wrong name and cycle from the wrong
        # place, and the next press would skip a font.
        font_index = previous
        if FONT is None:
            try:
                FONT = propfont.PropFont(AVAILABLE_FONTS[font_index][0],
                                         buf=_font_buf)
            except Exception as e2:
                print(f"could not reload the previous font: {e2}")
        # The error screen replaced the page; put the page back.
        if FONT is not None:
            try:
                render_page_to_buffer(current_offset, current_remainder,
                                      current_rotated_buffer)
                update_display_fast(current_rotated_buffer)
            except Exception as e3:
                print(f"could not redraw after a failed font switch: {e3}")


def factory_reset():
    """Wipe saved state and restart. Does not return."""
    show_message(("RESETTING...", 80, 55))

    try:
        NVM[0:256] = bytes(256)
        print("NVRAM cleared")
    except Exception as e:
        print(f"NVRAM clear error: {e}")

    # Delete any legacy .idx files if they exist
    try:
        for f in os.listdir("/state"):
            if f.endswith(".idx"):
                try:
                    os.remove("/state/" + f)
                    print(f"Deleted: /state/{f}")
                except:
                    pass
    except:
        pass

    show_message(("RESET COMPLETE", 65, 45), ("Restarting...", 80, 70))
    time.sleep(1.5)
    microcontroller.reset()


def open_picker():
    """Show the book picker and switch books, or restore the current one."""
    global text_file, current_offset, current_remainder
    global next_page_ready, next_page_offset, next_page_remainder
    global prev_page_ready, prev_page_offset, prev_page_remainder

    force_save_state()  # Save before potentially switching books
    saved = (current_offset, current_remainder,
             next_page_ready, next_page_offset, next_page_remainder,
             prev_page_ready, prev_page_offset, prev_page_remainder)

    new_book = file_picker()
    if not new_book:
        # Nothing chosen (no books, or the picker timed out) - make sure the
        # activity LED doesn't stay lit.
        led_off()
        return

    led_on()
    if is_epub(new_book):
        # An unconverted EPUB: build the .txt first. A successful conversion
        # reloads the board and never comes back here.
        new_book = convert_epub(new_book)
        if not new_book:
            # Nothing to open - put the reader back where it was.
            (current_offset, current_remainder,
             next_page_ready, next_page_offset, next_page_remainder,
             prev_page_ready, prev_page_offset, prev_page_remainder) = saved
            render_page_to_buffer(current_offset, current_remainder,
                                  current_rotated_buffer)
            update_display_fast(current_rotated_buffer)
            gc.collect()
            led_off()
            return

    if text_file != new_book:
        # Switching books - load saved position for the new one
        text_file = new_book
        current_offset, current_remainder = state_load_book(text_file)
        history_clear()
        render_page_to_buffer(current_offset, current_remainder, current_rotated_buffer)
        update_display_fast(current_rotated_buffer)
        prerender_next()
        prerender_prev()
        force_save_state()  # Save new book position
    else:
        # Same book - restore (the picker draws through its own buffers, so the
        # neighbour page images are still valid)
        (current_offset, current_remainder,
         next_page_ready, next_page_offset, next_page_remainder,
         prev_page_ready, prev_page_offset, prev_page_remainder) = saved
        render_page_to_buffer(current_offset, current_remainder, current_rotated_buffer)
        update_display_fast(current_rotated_buffer)

    gc.collect()
    led_off()


def handle_menu_button():
    """A button: short press opens the picker, long press forces a full
    refresh, holding for 10s triggers a factory reset."""
    press_start = time.monotonic()
    reset_warning_shown = False

    while button_pressed(buttons["a"]):
        # Warn after 3 seconds that a reset is coming
        if time.monotonic() - press_start > 3.0 and not reset_warning_shown:
            show_message(("FACTORY RESET", 70, 30),
                         ("Keep holding to reset...", 30, 55),
                         ("Release to cancel", 55, 80))
            reset_warning_shown = True
        time.sleep(0.05)
    press_duration = time.monotonic() - press_start

    if press_duration >= 10.0:
        led_on()
        factory_reset()  # does not return
    elif press_duration > 0.7:
        # Full refresh (clears e-ink ghosting)
        if reset_warning_shown:
            render_page_to_buffer(current_offset, current_remainder, current_rotated_buffer)
        display.set_speed(0, no_flickering=False)
        update_display_fast(current_rotated_buffer, blocking=True)
        display.set_speed(ORIGINAL_SPEED, no_flickering=ORIGINAL_NO_FLICKERING)
        led_off()
    else:
        open_picker()


def enter_sleep():
    """Save the reading position, show the sleep screen and cut the display
    rail. Waking runs code.py from the top again."""
    force_save_state()  # Critical: save before power down
    show_message(("Sleeping...", 110, 30), ("press any key to wake", 60, 90))
    board.ENABLE_DIO.value = False


def check_inactivity():
    """Power down if the user has been idle past the timeout.

    Called from every loop that can hold the device for a long time - the main
    reading loop and the book picker. The picker runs its own polling loop, so
    without this the device would stay awake indefinitely with the picker open.

    Returns True if it went to sleep. In practice cutting ENABLE_DIO powers the
    board down, so callers rarely see that return.
    """
    global last_activity

    if time.monotonic() - last_activity <= INACTIVITY_TIMEOUT:
        return False

    _, is_charging = get_battery_status()
    if is_charging:
        last_activity = time.monotonic()
        return False

    led_on()
    enter_sleep()
    led_off()
    return True


# ---------------- MAIN -----------------

# Flag to force full refresh on first display update after wake-up
# A conversion queued by the picker runs here and restarts the board, so the
# reader never starts. This sits after the definitions rather than beside the
# display setup, where it was first put: the progress screen calls back into
# _ScratchFrame, update_display_fast and show_message, and at that point they
# did not exist yet, so a conversion boot raised AttributeError before drawing
# anything - a dead screen and a blinking LED.
#
# Nothing is lost by waiting. What matters for memory is that the buffers, the
# font and the pattern blob were skipped much earlier; everything in between is
# only definitions.
if PENDING_CONVERT:
    try:
        import convert_ui
        convert_ui.run_pending(PENDING_CONVERT)  # restarts; does not return
    except Exception as _e:
        # Anything unhandled here would otherwise stop the board dead with a
        # blinking LED and no screen, which is what a conversion boot looked
        # like when it raised. The queued job has already been cleared, so
        # restarting comes straight back up as an ordinary reader. Carrying on
        # is not an option: this boot skipped the page buffers and the font.
        print(f"conversion boot failed, restarting as reader: {_e}")
        try:
            import supervisor
            supervisor.reload()
        except Exception:
            pass

first_display_update = True

# Load last active book from NVRAM
text_file = state_load_last_book()
current_offset = 0
current_remainder = b""

# Check if the last book still exists
if text_file:
    try:
        os.stat(text_file)
        # Load saved position for this book
        current_offset, current_remainder = state_load_book(text_file)
    except OSError:
        print(f"Last book '{text_file}' no longer exists")
        text_file = ""
        current_offset = 0
        current_remainder = b""

if not text_file:
    books = list_books()
    if books:
        new_book = file_picker()
        if new_book and is_epub(new_book):
            # First run with only EPUBs in /books - convert before reading.
            new_book = convert_epub(new_book)
        if new_book:
            text_file = new_book
        elif not is_epub(books[0]):
            text_file = books[0]
        # books[0] can still be an EPUB here - every book in /books was one and
        # the conversion did not produce a .txt. There is nothing to page
        # through, so fall through to the message below rather than trying.
        if text_file:
            # Load saved position for this book (may be 0 if new)
            current_offset, current_remainder = state_load_book(text_file)

    if not text_file:
        display.fb.fill(0)
        if books:
            display.text("Nothing to read yet", 10, 40, 1)
            display.text("Convert an EPUB from the menu", 10, 60, 1)
        else:
            display.text("No books in /books", 10, 50, 1)

        # Full refresh if first display
        if first_display_update:
            display.set_speed(0, no_flickering=False)
            display.update()
            display.set_speed(ORIGINAL_SPEED, no_flickering=ORIGINAL_NO_FLICKERING)
            first_display_update = False
        else:
            display.update()

        while True:
            time.sleep(1)

# Clear history for fresh start
history_clear()

# Apply the saved font choice
font_index = load_font_index() % len(AVAILABLE_FONTS)
if font_index != 0:
    try:
        FONT = propfont.PropFont(AVAILABLE_FONTS[font_index][0], buf=_font_buf)
    except Exception as e:
        print(f"font load failed: {e}")
        font_index = 0

# Render current page
render_page_to_buffer(current_offset, current_remainder, current_rotated_buffer)

# Full refresh for first display update (only if file_picker wasn't shown)
if first_display_update:
    display.set_speed(0, no_flickering=False)
    update_display_fast(current_rotated_buffer)
    display.set_speed(ORIGINAL_SPEED, no_flickering=ORIGINAL_NO_FLICKERING)
    first_display_update = False
else:
    update_display_fast(current_rotated_buffer)

# Pre-render the neighbouring pages so the first press either way is instant
prerender_next()
prerender_prev()

# Save initial state
force_save_state()

gc.collect()
led_off()

# --- MAIN LOOP ---
while True:
    if any(button_pressed(b) for b in buttons.values()):
        last_activity = time.monotonic()

    # PAGE DOWN - short press turns one page, long press skips ahead
    if button_pressed(buttons["down"]):
        led_on()

        # Turn one page immediately so the press feels instant, then look at
        # how long the button is actually held.
        page_advanced, prev_came_free = nav_page_down()

        if page_advanced:
            press_start = time.monotonic()
            while button_pressed(buttons["down"]):
                time.sleep(0.05)

            if time.monotonic() - press_start > 0.7:
                nav_fast_advance()
                force_save_state()  # Always save after fast advance
            else:
                # The previous page came free from the buffer rotation, so it
                # only needs re-rendering when it didn't.
                nonblocking = update_display_fast(current_rotated_buffer, blocking=False)
                prerender_next()
                if not prev_came_free:
                    prerender_prev()
                if nonblocking:
                    wait_for_display()
                maybe_save_state()  # Periodic save only

        led_off()

    # PAGE UP
    if button_pressed(buttons["up"]):
        led_on()
        while button_pressed(buttons["up"]):
            time.sleep(0.05)

        if nav_page_up():
            maybe_save_state()  # Periodic save only

        gc.collect()
        led_off()

    # FONT TOGGLE (B)
    if button_pressed(buttons["b"]):
        led_on()
        while button_pressed(buttons["b"]):
            time.sleep(0.05)
        last_activity = time.monotonic()

        cycle_font()

        gc.collect()
        led_off()

    # FILE PICKER (SHORT) / FULL REFRESH (LONG) / FACTORY RESET (VERY LONG)
    if button_pressed(buttons["a"]):
        led_on()
        handle_menu_button()

    # INACTIVITY TIMEOUT
    check_inactivity()
