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

# Optional on-device hyphenation. If the module or its pattern file is missing,
# hyphenation is disabled gracefully (plain word-wrapping still works).
try:
    import hyphenator
    hyphenator._load()  # force-load the pattern blob now so per-word calls can't fail on I/O
    _HYPHEN_OK = True
except Exception as _e:
    print(f"hyphenator unavailable: {_e}")
    _HYPHEN_OK = False

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
LINES_PER_PAGE = 9
LINE_HEIGHT = vga2_8x16.HEIGHT - 2 
TEXT_PADDING = 2
WIDTH = 296
HEIGHT = 128
TEXT_WIDTH = WIDTH - TEXT_PADDING*2
MAX_CHARS = TEXT_WIDTH // vga2_8x16.WIDTH
BOOK_DIR = "/books"

# Full-justify wrapped lines so the right margin is flush (monospace: pad
# spaces between words). Purely a rendering choice - it does not affect
# pagination/offsets. Set False for a ragged right edge.
JUSTIFY_TEXT = True

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
    external_font=vga2_8x16, 
    use_framebuf_font=True,
    font_path="font5x8.bin",
    speed=ORIGINAL_SPEED,
    no_flickering=ORIGINAL_NO_FLICKERING,
    full_update_period=0
)

display.enable_quick_updates(True)

# --- BUFFERS ---
raw_working_buffer = bytearray(display.width * display.height // 8)
current_rotated_buffer = bytearray(display.physical_width * display.physical_height // 8)
next_rotated_buffer = bytearray(display.physical_width * display.physical_height // 8)

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

def justify_line(text, max_chars):
    """Full-justify a single monospace line by distributing extra spaces
    between words until it reaches max_chars. Extra spaces go to the left-most
    gaps first. Returns text unchanged if it can't/shouldn't be justified."""
    words = text.split(" ")
    if len(words) < 2:
        return text
    need = max_chars - len(text)
    if need <= 0:
        return text
    gaps = len(words) - 1
    base, extra = divmod(need, gaps)
    out = []
    for i, w in enumerate(words[:-1]):
        out.append(w)
        out.append(" " * (1 + base + (1 if i < extra else 0)))
    out.append(words[-1])
    return "".join(out)


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
            # Persists across source lines so consecutive non-blank lines flow
            # together as one paragraph (only a blank line ends a paragraph).
            current_clean_text = ""

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
                    if current_clean_text:
                        lines.append(current_clean_text.encode("utf-8", "ignore"))
                        line_count += 1
                        current_clean_text = ""
                    next_offset = f.tell()
                    break

                line = line_bytes.rstrip(b"\r\n")

                if not line:
                    # Blank line ends a paragraph: flush its last line first, then
                    # emit the blank separator.
                    if current_clean_text:
                        lines.append(current_clean_text.encode("utf-8", "ignore"))
                        line_count += 1
                        current_clean_text = ""
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
                        
                    word_clean = raw_word.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")\
                               .replace("\u2014", "-").replace("\u2013", "-").replace("…", "...")

                    # --- Over-long word (wider than a full line): hard-break it into
                    # MAX_CHARS-sized chunks instead of overflowing off the display.
                    # We only page-break at whole-word boundaries, so the byte
                    # offset/remainder accounting stays exact.
                    if len(word_clean) > MAX_CHARS:
                        # Flush any partial line first - the long word starts fresh.
                        if current_clean_text:
                            lines.append(current_clean_text.encode("utf-8", "ignore"))
                            line_count += 1
                            current_clean_text = ""
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

                        chunks = [word_clean[c:c + MAX_CHARS]
                                  for c in range(0, len(word_clean), MAX_CHARS)]

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
                                current_clean_text = chunks[-1]
                            else:
                                current_clean_text = ""
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

                    appended = current_clean_text + " " + word_clean if current_clean_text else word_clean

                    if len(appended) <= MAX_CHARS:
                        current_clean_text = appended
                        word_count += 1
                    else:
                        # Word doesn't fit. Try to hyphenate a prefix onto this line
                        # first - but never across a PAGE boundary (that would leave
                        # half a word in the saved remainder). So only when the prefix
                        # line won't be the page's last line; then the whole word is
                        # consumed on this page and the offset stays whole-word.
                        if (hyphenate and HYPHENATE and _HYPHEN_OK
                                and line_count < LINES_PER_PAGE - 1):
                            used = len(current_clean_text) + (1 if current_clean_text else 0)
                            prefix, rest = hyphenator.hyphenate_split(word_clean, MAX_CHARS - used)
                            if prefix:
                                if current_clean_text:
                                    line_out = current_clean_text + " " + prefix + "-"
                                else:
                                    line_out = prefix + "-"
                                lines.append(line_out.encode("utf-8", "ignore"))
                                line_count += 1
                                current_clean_text = rest
                                word_count += 1
                                continue

                        lines.append(current_clean_text.encode("utf-8", "ignore"))
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

                        current_clean_text = word_clean
                        word_count += 1
                
                if next_offset != -1:
                    break

                # End of this source line - do NOT flush current_clean_text; the
                # paragraph continues on the next line. It gets flushed at a blank
                # line, at EOF, or when the next line's words wrap it.

            if next_offset == -1:
                # Loop ended without an explicit page break (e.g. a pathological
                # over-long token filled the page): flush any trailing text.
                if current_clean_text:
                    lines.append(current_clean_text.encode("utf-8", "ignore"))
                    line_count += 1
                    current_clean_text = ""
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

def find_previous_page(target_offset):
    """Find the page position that leads to target_offset by scanning backwards."""
    global text_file
    
    if target_offset == 0:
        return 0, b""
    
    # Search from before the target
    search_start = max(0, target_offset - 3000)  # ~3KB back covers 2-3 pages
    
    offset = search_start
    remainder = b""
    
    prev_offset = 0
    prev_remainder = b""
    
    while offset < target_offset:
        lines, next_offset, next_remainder = paginate_text(text_file, offset, remainder)
        
        if not lines or next_offset <= offset:
            break
        
        if next_offset >= target_offset:
            # Current (offset, remainder) is the page that leads to target
            return offset, remainder
        
        prev_offset = offset
        prev_remainder = remainder
        offset = next_offset
        remainder = next_remainder
    
    # If search_start was already a valid page boundary
    if search_start == 0:
        return 0, b""
    
    return prev_offset, prev_remainder

# ---------------- RENDERING -----------------
def render_page_to_buffer(page_offset, page_remainder, target_rotated_buffer):
    """Render a page to the target buffer."""
    global text_file
    
    for i in range(len(raw_working_buffer)):
        raw_working_buffer[i] = 0
        
    temp_fb = adafruit_framebuf.FrameBuffer(
        raw_working_buffer, display.width, display.height, adafruit_framebuf.MHMSB
    )
    
    old_fb = display.fb
    old_raw_fb = display.raw_fb
    display.fb = temp_fb
    display.raw_fb = raw_working_buffer
    
    try:
        lines, _, _ = paginate_text(text_file, page_offset, page_remainder)
        
        y = TEXT_PADDING
        n_lines = len(lines)
        for i in range(n_lines):
            line = lines[i]
            if line:
                try:
                    text = line.decode("utf-8", "replace")
                    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")\
                               .replace("\u2014", "-").replace("\u2013", "-")
                    # Justify only interior lines that are followed by more text
                    # on this page. The last line of a paragraph (next line blank
                    # or end of page) stays ragged; line 0 is skipped so a full
                    # line never collides with the top-right battery indicator.
                    if JUSTIFY_TEXT and i > 0 and i + 1 < n_lines and lines[i + 1]:
                        text = justify_line(text, MAX_CHARS)
                    display.text(text, TEXT_PADDING, y, 1)
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
        display.fb = old_fb
        display.raw_fb = old_raw_fb
        gc.collect()

    rotated_data = display._rotate_framebuffer(raw_working_buffer)
    for i in range(len(target_rotated_buffer)):
        target_rotated_buffer[i] = rotated_data[i]

def update_display_fast(rotated_buffer, blocking=True):
    old_rot = display.rotation
    display.rotation = 0 
    result = display.update(blocking=blocking, fb=rotated_buffer)
    display.rotation = old_rot
    return result

def wait_for_display():
    display.wait_ready()

# ---------------- FILE PICKER -----------------
def list_books():
    books = []
    try:
        for f in os.listdir(BOOK_DIR):
            if f.endswith(".txt") and not f.startswith("."):
                books.append(BOOK_DIR + "/" + f)
    except OSError:
        pass
    return sorted(books)

def file_picker():
    global first_display_update
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
    offset = 0
    prev_offset = -1
    per_page = 6
    
    selection_buffers = []
    
    while True:
        if offset != prev_offset:
            selection_buffers = []
            
            for sel_idx in range(per_page):
                book_idx = offset + sel_idx
                if book_idx >= len(books):
                    break
                
                for i in range(len(raw_working_buffer)): 
                    raw_working_buffer[i] = 0
                temp_fb = adafruit_framebuf.FrameBuffer(raw_working_buffer, display.width, display.height, adafruit_framebuf.MHMSB)
                
                old_fb = display.fb
                old_raw_fb = display.raw_fb
                display.fb = temp_fb
                display.raw_fb = raw_working_buffer
                
                try:
                    temp_fb.fill(0)
                    display.text("Select Book:", 5, 5, 1)
                    
                    for i in range(per_page):
                        idx = offset + i
                        if idx >= len(books): break
                        name = books[idx].split("/")[-1]
                        if len(name) > 33: name = name[:30] + "..."
                        y = 25 + i * 16
                        
                        if i == sel_idx:
                            display.fb.fill_rect(2, y-2, WIDTH-4, 16, 1)
                            display.text(name, 5, y, 0)
                        else:
                            display.text(name, 5, y, 1)
                    
                    if len(books) > per_page:
                        page = offset // per_page + 1
                        total = (len(books) + per_page - 1) // per_page
                        display.text(f"{page}/{total}", WIDTH - (vga2_8x16.WIDTH * 5), HEIGHT - vga2_8x16.HEIGHT - 10, 1)
                        
                    storage_status = get_storage_status()
                    STATUS_X = WIDTH - (len(storage_status) * FONT_W_5X8) - TEXT_PADDING
                    STATUS_Y = HEIGHT - FONT_H_5X8 - TEXT_PADDING 
                    temp_fb.text(storage_status, STATUS_X, STATUS_Y, 1, font_name="font5x8.bin")
                    
                finally:
                    display.fb = old_fb
                    display.raw_fb = old_raw_fb
                
                rotated = display._rotate_framebuffer(raw_working_buffer)
                buffer_copy = bytearray(len(rotated))
                for i in range(len(rotated)):
                    buffer_copy[i] = rotated[i]
                selection_buffers.append(buffer_copy)
            
            prev_offset = offset
            
            if selected < offset:
                selected = offset
            elif selected >= offset + len(selection_buffers):
                selected = offset + len(selection_buffers) - 1
            
            sel_index_on_page = selected - offset
            if 0 <= sel_index_on_page < len(selection_buffers):
                old_rot = display.rotation
                display.rotation = 0
                
                # Full refresh if first display
                if first_display_update:
                    display.set_speed(0, no_flickering=False)
                    display.update(fb=selection_buffers[sel_index_on_page])
                    display.set_speed(ORIGINAL_SPEED, no_flickering=ORIGINAL_NO_FLICKERING)
                    first_display_update = False
                else:
                    display.update(fb=selection_buffers[sel_index_on_page])
                
                display.rotation = old_rot
            led_off()
        
        if button_pressed(buttons["down"]):
            selected = (selected + 1) % len(books)
            
            if selected < offset or selected >= offset + per_page:
                offset = (selected // per_page) * per_page
            else:
                sel_index_on_page = selected - offset
                if 0 <= sel_index_on_page < len(selection_buffers):
                    old_rot = display.rotation
                    display.rotation = 0
                    display.update(fb=selection_buffers[sel_index_on_page])
                    display.rotation = old_rot
            time.sleep(0.15)
            
        elif button_pressed(buttons["up"]):
            selected = (selected - 1) % len(books)
            
            if selected < offset or selected >= offset + per_page:
                offset = (selected // per_page) * per_page
            else:
                sel_index_on_page = selected - offset
                if 0 <= sel_index_on_page < len(selection_buffers):
                    old_rot = display.rotation
                    display.rotation = 0
                    display.update(fb=selection_buffers[sel_index_on_page])
                    display.rotation = old_rot
            time.sleep(0.15)
            
        elif button_pressed(buttons["a"]):
            while button_pressed(buttons["a"]):
                time.sleep(0.05)
            return books[selected]
        
        time.sleep(0.05)

# ---------------- MAIN -----------------

# Flag to force full refresh on first display update after wake-up
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
        if new_book:
            text_file = new_book
        else:
            text_file = books[0]
        # Load saved position for this book (may be 0 if new)
        current_offset, current_remainder = state_load_book(text_file)
    else:
        display.fb.fill(0)
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

# Pre-render next page
lines, next_off, next_rem = paginate_text(text_file, current_offset, current_remainder)
if lines and next_off > current_offset:
    next_page_offset = next_off
    next_page_remainder = next_rem
    render_page_to_buffer(next_page_offset, next_page_remainder, next_rotated_buffer)
    next_page_ready = True
else:
    next_page_ready = False

# Save initial state
force_save_state()

gc.collect()
led_off()

# --- MAIN LOOP ---
while True:
    if any(button_pressed(b) for b in buttons.values()):
        last_activity = time.monotonic()
        
    # PAGE DOWN
    if button_pressed(buttons["down"]):
        led_on()
        
        # IMMEDIATE VISUAL FEEDBACK: Advance and display first page instantly
        page_advanced = False
        if next_page_ready:
            # Save current position to history
            history_push(current_offset, current_remainder)
            
            # Swap buffers and update display immediately
            current_rotated_buffer, next_rotated_buffer = next_rotated_buffer, current_rotated_buffer
            current_offset = next_page_offset
            current_remainder = next_page_remainder
            update_display_fast(current_rotated_buffer)
            next_page_ready = False
            page_advanced = True
        elif current_offset >= 0:
            # No pre-rendered page, try to advance
            lines, next_off, next_rem = paginate_text(text_file, current_offset, current_remainder)
            if lines and next_off > current_offset:
                history_push(current_offset, current_remainder)
                current_offset = next_off
                current_remainder = next_rem
                render_page_to_buffer(current_offset, current_remainder, current_rotated_buffer)
                update_display_fast(current_rotated_buffer)
                page_advanced = True
        
        # Now check if button is still held for long-press detection
        if page_advanced:
            press_start = time.monotonic()
            while button_pressed(buttons["down"]):
                time.sleep(0.05)
            press_duration = time.monotonic() - press_start
            
            if press_duration > 0.7:  # Long press: continue advancing more pages
                FAST_ADVANCE_PAGES = 49  # Already advanced 1, so 49 more = 50 total
                
                for i in range(FAST_ADVANCE_PAGES):
                    # Skip hyphenation here: these pages are only used to advance
                    # the offset, never rendered, so we don't pay for hyphenating them.
                    lines, next_off, next_rem = paginate_text(text_file, current_offset, current_remainder, hyphenate=False)

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
                
                # Pre-render next
                lines, next_off, next_rem = paginate_text(text_file, current_offset, current_remainder)
                if lines and next_off > current_offset:
                    next_page_offset = next_off
                    next_page_remainder = next_rem
                    render_page_to_buffer(next_page_offset, next_page_remainder, next_rotated_buffer)
                    next_page_ready = True
                else:
                    next_page_ready = False
                
                force_save_state()  # Always save after fast advance
            else:
                # Short press: single page already advanced, just pre-render next
                if update_display_fast(current_rotated_buffer, blocking=False):
                    # Pre-render next page while display updates
                    lines, next_off, next_rem = paginate_text(text_file, current_offset, current_remainder)
                    if lines and next_off > current_offset:
                        next_page_offset = next_off
                        next_page_remainder = next_rem
                        render_page_to_buffer(next_page_offset, next_page_remainder, next_rotated_buffer)
                        next_page_ready = True
                    else:
                        next_page_ready = False
                    
                    wait_for_display()
                else:
                    # Display update was blocking, pre-render now
                    lines, next_off, next_rem = paginate_text(text_file, current_offset, current_remainder)
                    if lines and next_off > current_offset:
                        next_page_offset = next_off
                        next_page_remainder = next_rem
                        render_page_to_buffer(next_page_offset, next_page_remainder, next_rotated_buffer)
                        next_page_ready = True
                    else:
                        next_page_ready = False
                
                maybe_save_state()  # Periodic save only
        
        led_off()

    # PAGE UP
    if button_pressed(buttons["up"]):
        led_on()
        
        while button_pressed(buttons["up"]):
            time.sleep(0.05)
        
        if current_offset > 0:
            # Try to get previous page from history
            prev = history_pop()
            
            if prev:
                current_offset, current_remainder = prev
            else:
                # Calculate previous page
                current_offset, current_remainder = find_previous_page(current_offset)
            
            render_page_to_buffer(current_offset, current_remainder, current_rotated_buffer)
            update_display_fast(current_rotated_buffer)
            
            # Pre-render next page
            lines, next_off, next_rem = paginate_text(text_file, current_offset, current_remainder)
            if lines and next_off > current_offset:
                next_page_offset = next_off
                next_page_remainder = next_rem
                render_page_to_buffer(next_page_offset, next_page_remainder, next_rotated_buffer)
                next_page_ready = True
            else:
                next_page_ready = False
            
            maybe_save_state()  # Periodic save only
        
        gc.collect()
        led_off()

    # FILE PICKER (SHORT) / FULL REFRESH (LONG) / FACTORY RESET (VERY LONG)
    if button_pressed(buttons["a"]):
        led_on()
        
        # Measure press duration with visual feedback for reset
        press_start = time.monotonic()
        reset_warning_shown = False
        while button_pressed(buttons["a"]):
            press_duration = time.monotonic() - press_start
            # Show warning after 3 seconds that reset is coming
            if press_duration > 3.0 and not reset_warning_shown:
                # Render reset warning screen
                for i in range(len(raw_working_buffer)): 
                    raw_working_buffer[i] = 0
                temp_fb = adafruit_framebuf.FrameBuffer(
                    raw_working_buffer, display.width, display.height, adafruit_framebuf.MHMSB
                )
                old_fb, old_raw_fb = display.fb, display.raw_fb
                display.fb, display.raw_fb = temp_fb, raw_working_buffer
                try:
                    display.text("FACTORY RESET", 70, 30, 1)
                    display.text("Keep holding to reset...", 30, 55, 1)
                    display.text("Release to cancel", 55, 80, 1)
                finally:
                    display.fb, display.raw_fb = old_fb, old_raw_fb
                rotated = display._rotate_framebuffer(raw_working_buffer)
                update_display_fast(rotated, blocking=True)
                reset_warning_shown = True
            time.sleep(0.05)
        press_duration = time.monotonic() - press_start
        
        if press_duration >= 10.0:  # Very long press: Factory reset
            led_on()
            
            # Show resetting message
            for i in range(len(raw_working_buffer)): 
                raw_working_buffer[i] = 0
            temp_fb = adafruit_framebuf.FrameBuffer(
                raw_working_buffer, display.width, display.height, adafruit_framebuf.MHMSB
            )
            old_fb, old_raw_fb = display.fb, display.raw_fb
            display.fb, display.raw_fb = temp_fb, raw_working_buffer
            try:
                display.text("RESETTING...", 80, 55, 1)
            finally:
                display.fb, display.raw_fb = old_fb, old_raw_fb
            rotated = display._rotate_framebuffer(raw_working_buffer)
            update_display_fast(rotated, blocking=True)
            
            # Clear NVRAM
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
            
            # Show complete message
            for i in range(len(raw_working_buffer)): 
                raw_working_buffer[i] = 0
            temp_fb = adafruit_framebuf.FrameBuffer(
                raw_working_buffer, display.width, display.height, adafruit_framebuf.MHMSB
            )
            old_fb, old_raw_fb = display.fb, display.raw_fb
            display.fb, display.raw_fb = temp_fb, raw_working_buffer
            try:
                display.text("RESET COMPLETE", 65, 45, 1)
                display.text("Restarting...", 80, 70, 1)
            finally:
                display.fb, display.raw_fb = old_fb, old_raw_fb
            rotated = display._rotate_framebuffer(raw_working_buffer)
            update_display_fast(rotated, blocking=True)
            
            time.sleep(1.5)
            microcontroller.reset()
            
        elif press_duration > 0.7:  # Long press: Full refresh
            if reset_warning_shown:
                render_page_to_buffer(current_offset, current_remainder, current_rotated_buffer)
            display.set_speed(0, no_flickering=False)
            update_display_fast(current_rotated_buffer, blocking=True)
            display.set_speed(ORIGINAL_SPEED, no_flickering=ORIGINAL_NO_FLICKERING)
            led_off()
        else:  # Short press: File picker
            force_save_state()  # Save before potentially switching books
            
            saved_offset = current_offset
            saved_remainder = current_remainder
            saved_next_ready = next_page_ready
            saved_next_offset = next_page_offset
            saved_next_remainder = next_page_remainder
            
            new_book = file_picker()
            
            if new_book:
                led_on()
                
                if text_file != new_book:
                    # Switching books - load saved position for new book
                    text_file = new_book
                    current_offset, current_remainder = state_load_book(text_file)
                    history_clear()
                    
                    render_page_to_buffer(current_offset, current_remainder, current_rotated_buffer)
                    update_display_fast(current_rotated_buffer)
                    
                    # Pre-render next
                    lines, next_off, next_rem = paginate_text(text_file, current_offset, current_remainder)
                    if lines and next_off > current_offset:
                        next_page_offset = next_off
                        next_page_remainder = next_rem
                        render_page_to_buffer(next_page_offset, next_page_remainder, next_rotated_buffer)
                        next_page_ready = True
                    else:
                        next_page_ready = False
                    
                    force_save_state()  # Save new book position
                    gc.collect()
                else:
                    # Same book - restore
                    current_offset = saved_offset
                    current_remainder = saved_remainder
                    next_page_ready = saved_next_ready
                    next_page_offset = saved_next_offset
                    next_page_remainder = saved_next_remainder
                    
                    render_page_to_buffer(current_offset, current_remainder, current_rotated_buffer)
                    update_display_fast(current_rotated_buffer)
                    gc.collect()
                
                led_off()
             
    # INACTIVITY TIMEOUT
    if time.monotonic() - last_activity > INACTIVITY_TIMEOUT:
        _, is_charging = get_battery_status()
        
        if is_charging:
            last_activity = time.monotonic()
        else:
            led_on()
            force_save_state()  # Critical: save before power down
            
            # Display sleep message
            for i in range(len(raw_working_buffer)): 
                raw_working_buffer[i] = 0
            temp_fb = adafruit_framebuf.FrameBuffer(
                raw_working_buffer, display.width, display.height, adafruit_framebuf.MHMSB
            )
            old_fb, old_raw_fb = display.fb, display.raw_fb
            display.fb, display.raw_fb = temp_fb, raw_working_buffer
            try:
                display.text("Sleeping...", 110, 30, 1)
                display.text("press any key to wake", 60, 90, 1)
            finally:
                display.fb, display.raw_fb = old_fb, old_raw_fb
            
            rotated = display._rotate_framebuffer(raw_working_buffer)
            old_rot = display.rotation
            display.rotation = 0
            # display.set_speed(0, no_flickering=False)  # Full refresh for sleep message
            display.update(fb=rotated)
            board.ENABLE_DIO.value = False
            led_off()
