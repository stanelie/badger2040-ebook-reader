"""
CircuitPython E-book Reader for Badger 2040
Ported from MicroPython to use uc8151_circuitpython driver
"""
import board
import displayio
import digitalio
import time
import os
import struct
import vga2_8x16
import gc
import adafruit_framebuf
import analogio
import pwmio
from uc8151_circuitpython import UC8151

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

# DEBUG FLAG - set to False to disable timing output
DEBUG_TIMING = False

def debug_time(label, start_time):
    """Print debug timing info"""
    if DEBUG_TIMING:
        elapsed = (time.monotonic() - start_time) * 1000  # Convert to ms
        print(f"[DEBUG] {label}: {elapsed:.1f}ms")
    return time.monotonic()

displayio.release_displays()

# Create directories if they don't exist
for d in ["/books", "/state"]:
    try:
        os.mkdir(d)
    except OSError:
        pass

# ---------------- STATE -----------------
STATE_FILE = "/state/ebook_state.bin"

def state_save(state):
    try:
        page = state.get("current_page", 0)
        book = state.get("last_book", "")
        
        with open(STATE_FILE, "wb") as f:
            file_path = state.get("last_book", "")
            data = struct.pack("<I", state.get("current_page", 0))
            f.write(data)
            f.write(struct.pack("<H", len(file_path)))
            f.write(file_path.encode("utf-8"))
        
        # Explicit sync to ensure data is written to flash
        try:
            os.sync()
        except:
            pass
        
    except OSError as e:
        print(f"state_save ERROR: {e}")
    except Exception as e:
        print(f"state_save ERROR: {e}")

def state_load():
    state = {"current_page": 0, "last_book": ""}
    try:
        stat = os.stat(STATE_FILE)
        if stat[6] > 0:
            with open(STATE_FILE, "rb") as f:
                current_page = struct.unpack("<I", f.read(4))[0]
                l = struct.unpack("<H", f.read(2))[0]
                last_book = f.read(l).decode("utf-8")
                state["current_page"] = current_page
                state["last_book"] = last_book
    except OSError as e:
        print(f"state_load: No state file found ({e})")
    except Exception as e:
        print(f"state_load ERROR: {e}")
    return state

# ---------------- CONFIG -----------------
LINES_PER_PAGE = 9
LINE_HEIGHT = vga2_8x16.HEIGHT - 2 
TEXT_PADDING = 2
WIDTH = 296
HEIGHT = 128
TEXT_WIDTH = WIDTH - TEXT_PADDING*2
MAX_CHARS = TEXT_WIDTH // vga2_8x16.WIDTH
BOOK_DIR = "/books"

# 5x8 Font Dimensions (used for status text)
FONT_W_5X8 = 5 
FONT_H_5X8 = 8

last_activity = time.monotonic()

# ---------------- GLOBAL INDEX STATE -----------------
end_of_index_reached = False

# ---------------- DISPLAY -----------------
spi = board.SPI()

# Store original display settings
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

# ---------------- BATTERY STATUS -----------------
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

# ---------------- STORAGE STATUS -----------------
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

# ---------------- REMAINDER MANAGEMENT -----------------
def keep_only_needed_remainders(current_page):
    """Keep only remainders for current and next page"""
    global page_remainders
    keys_to_keep = {current_page, current_page + 1}
    keys_to_remove = [k for k in page_remainders.keys() if k not in keys_to_keep]
    for k in keys_to_remove:
        del page_remainders[k]

# ---------------- TEXT PROCESSING -----------------

def paginate_text(file_path, start_offset, remainder=b""):
    try:
        with open(file_path, "rb") as f:
            f.seek(start_offset)
            lines = []
            line_count = 0
            next_offset = -1
            
            while line_count < LINES_PER_PAGE:
                pos = f.tell()
                
                if remainder:
                    line_bytes = remainder
                    f.seek(start_offset + len(remainder))
                    remainder = b""
                else:
                    line_bytes = f.readline()
                
                if not line_bytes:
                    next_offset = f.tell()
                    break
                
                line = line_bytes.rstrip(b"\r\n")
                
                if not line:
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
                current_clean_text = ""
                word_count = 0
                
                for raw_word in words_raw:
                    if not raw_word:
                        word_count += 1
                        continue
                        
                    word_clean = raw_word.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")\
                               .replace("\u2014", "-").replace("\u2013", "-").replace("…", "...")

                    appended = current_clean_text + " " + word_clean if current_clean_text else word_clean
                    
                    if len(appended) <= MAX_CHARS:
                        current_clean_text = appended
                        word_count += 1
                    else:
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
                
                if current_clean_text:
                    lines.append(current_clean_text.encode("utf-8", "ignore"))
                    line_count += 1
                
                if line_count >= LINES_PER_PAGE:
                    next_offset = f.tell()
                    break
            
            if next_offset == -1:
                next_offset = f.tell()
            
            gc.collect()
            return lines, next_offset, remainder
            
    except OSError as e:
        print(f"ERROR: File not found or cannot be read: {file_path}")
        gc.collect()
        return [], start_offset, b""
    except Exception as e:
        print(f"ERROR: Paginate failure: {e}")
        gc.collect()
        return [], start_offset, b""

# ---------------- RENDERING CORE -----------------
def render_page_and_rotate(page_num, target_rotated_buffer):
    # Clear raw buffer
    for i in range(len(raw_working_buffer)):
        raw_working_buffer[i] = 0
        
    # Setup temp framebuffer
    temp_fb = adafruit_framebuf.FrameBuffer(
        raw_working_buffer, display.width, display.height, adafruit_framebuf.MHMSB
    )
    
    # Swap display context for text rendering
    old_fb = display.fb
    old_raw_fb = display.raw_fb
    
    display.fb = temp_fb
    display.raw_fb = raw_working_buffer
    
    try:
        # Get Text
        remainder = page_remainders.get(page_num, b"")
        lines, _, _ = paginate_text(text_file, page_offsets[page_num], remainder)
        
        y = TEXT_PADDING
        for line in lines:
            if line:
                try:
                    text = line.decode("utf-8", "replace")
                    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")\
                               .replace("\u2014", "-").replace("\u2013", "-")
                    display.text(text, TEXT_PADDING, y, 1)
                except:
                    pass
            y += LINE_HEIGHT
            
        # Battery Indicator
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
        
        # Progress Bar
        try:
            current_offset = page_offsets[page_num] 
            file_stats = os.stat(text_file)
            total_size = file_stats[6]
            
            if total_size > 0:
                progress_ratio = current_offset / total_size
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

    # ROTATION STEP
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

# ---------------- INDEX STORAGE -----------------
def save_index(file_path, current_page=0):
    global page_offsets, page_remainders
    try:
        with open(file_path, "wb") as f:
            f.write(struct.pack("<I", current_page))
            f.write(struct.pack("<I", len(page_offsets)))
            for offset in page_offsets:
                f.write(struct.pack("<I", offset))
            keys = list(page_remainders.keys())
            f.write(struct.pack("<I", len(keys)))
            for k in keys:
                rem = page_remainders[k]
                f.write(struct.pack("<I", k))
                f.write(struct.pack("<I", len(rem)))
                f.write(rem)
        
        # Explicit sync to ensure data is written to flash
        try:
            os.sync()
        except:
            pass
        
    except OSError as e:
        print(f"save_index ERROR: {e}")

def load_index(file_path):
    global page_offsets, page_remainders
    try:
        with open(file_path, "rb") as f:
            saved_page = struct.unpack("<I", f.read(4))[0]
            num_offsets = struct.unpack("<I", f.read(4))[0]
            page_offsets = []
            for _ in range(num_offsets):
                page_offsets.append(struct.unpack("<I", f.read(4))[0])
            page_remainders = {}
            try:
                num_remainders = struct.unpack("<I", f.read(4))[0]
                for _ in range(num_remainders):
                    k = struct.unpack("<I", f.read(4))[0]
                    rem_len = struct.unpack("<I", f.read(4))[0]
                    rem = f.read(rem_len)
                    page_remainders[k] = rem
            except:
                pass
        
        return saved_page
    except Exception as e:
        print(f"load_index ERROR: {e}")
        page_offsets = [0]
        page_remainders = {}
        return 0

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
    global end_of_index_reached
    
    books = list_books()
    
    if not books:
        display.fb.fill(0)
        display.text("No books found!", 10, 40, 1)
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
                
                for i in range(len(raw_working_buffer)): raw_working_buffer[i] = 0
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
            end_of_index_reached = False
            return books[selected]
        
        time.sleep(0.05)

# ---------------- MAIN -----------------

state = state_load()
text_file = state.get("last_book", "")

# Check if the last book still exists
if text_file:
    try:
        os.stat(text_file)
    except OSError:
        print(f"Last book '{text_file}' no longer exists, will show picker")
        text_file = ""
        state["last_book"] = ""
        state["current_page"] = 0

if not text_file:
    books = list_books()
    if books:
        # Show file picker immediately
        new_book = file_picker()
        if new_book:
            text_file = new_book
            state["last_book"] = text_file
            state["current_page"] = 0
            state_save(state)
        else:
            # User cancelled picker, just use first book
            text_file = books[0]
            state["last_book"] = text_file
    else:
        display.fb.fill(0)
        display.text("No books in /books", 10, 50, 1)
        display.update()
        while True: time.sleep(1)

INDEX_FILE = "/state/" + text_file.replace("/", "_").replace(".", "_") + ".idx"

try:
    os.stat(INDEX_FILE)
    current = load_index(INDEX_FILE)
except OSError:
    page_offsets = [0]
    page_remainders = {}
    current = 0
    
end_of_index_reached = False

current = min(current, max(0, len(page_offsets)-1))
state["current_page"] = current

# Clean up loaded remainders to only what we need
keep_only_needed_remainders(current)

render_page_and_rotate(current, current_rotated_buffer)
update_display_fast(current_rotated_buffer)

if current + 1 >= len(page_offsets):
     curr_rem = page_remainders.get(current, b"")
     lines, next_off, next_rem = paginate_text(text_file, page_offsets[current], curr_rem)
     
     advanced = (next_off > page_offsets[current]) or (next_rem and next_rem != curr_rem)
     
     if lines and advanced:
         page_offsets.append(next_off)
         page_remainders[current+1] = next_rem
         save_index(INDEX_FILE, current)
     else:
         end_of_index_reached = True

if current + 1 < len(page_offsets):
    render_page_and_rotate(current + 1, next_rotated_buffer)
    next_page_ready = True

keep_only_needed_remainders(current)
gc.collect()
led_off()

# --- MAIN LOOP ---
while True:
    if any(button_pressed(b) for b in buttons.values()):
        last_activity = time.monotonic()
        
    # PAGE DOWN
    if button_pressed(buttons["down"]):
        t_total_start = time.monotonic()
        if DEBUG_TIMING:
            print("\n" + "="*50)
            print("[DEBUG] DOWN BUTTON PRESSED")
            print("="*50)
        
        led_on()
        t0 = debug_time("LED on", t_total_start)
        
        press_start = time.monotonic()
        while button_pressed(buttons["down"]):
            time.sleep(0.05)
        press_duration = time.monotonic() - press_start
        t0 = debug_time(f"Button release wait (duration: {press_duration*1000:.0f}ms)", t0)
        
        if press_duration > 0.7:  # Long press: 50-page fast advance
            if DEBUG_TIMING:
                print("[DEBUG] LONG PRESS - Fast advance mode")
            
            FAST_ADVANCE_PAGES = 50
            start_page = current
            
            t_fast_start = time.monotonic()
            for i in range(FAST_ADVANCE_PAGES):
                next_page = current + 1
                
                if next_page >= len(page_offsets):
                    curr_rem = page_remainders.get(current, b"")
                    lines, next_off, next_rem = paginate_text(text_file, page_offsets[current], curr_rem)
                    
                    # Check if we've reached the end of the file
                    if next_off <= page_offsets[current] or not lines:
                        break
                    
                    page_offsets.append(next_off)
                    page_remainders[next_page] = next_rem
                
                current = next_page
                
                # Keep only current and next page remainders
                keep_only_needed_remainders(current)
                
                # Collect garbage every 10 pages
                if i % 10 == 0:
                    gc.collect()
            
            pages_advanced = current - start_page
            t0 = debug_time(f"Fast advance {pages_advanced} pages", t_fast_start)
            
            gc.collect()
            t0 = debug_time("gc.collect after fast advance", t0)
            
            current = min(current, len(page_offsets) - 1)
            
            # Save progress after fast advance
            t_save_start = time.monotonic()
            save_index(INDEX_FILE, current)
            t0 = debug_time("save_index", t_save_start)
            
            state["current_page"] = current
            
            t_render_start = time.monotonic()
            render_page_and_rotate(current, current_rotated_buffer)
            t0 = debug_time("render_page_and_rotate (current)", t_render_start)
            
            t_display_start = time.monotonic()
            update_display_fast(current_rotated_buffer)
            t0 = debug_time("update_display_fast", t_display_start)
            
            next_page_ready = False
            if current + 1 < len(page_offsets):
                t_prerender_start = time.monotonic()
                render_page_and_rotate(current + 1, next_rotated_buffer)
                t0 = debug_time("render_page_and_rotate (next, pre-render)", t_prerender_start)
                next_page_ready = True
            
        else:  # Short press: normal single page advance
            if DEBUG_TIMING:
                print("[DEBUG] SHORT PRESS - Normal page advance")
                print(f"[DEBUG] next_page_ready={next_page_ready}, current={current}, len(page_offsets)={len(page_offsets)}")
            
            if current + 1 < len(page_offsets) and next_page_ready:
                if DEBUG_TIMING:
                    print("[DEBUG] PATH: Pre-rendered next page available")
                
                t_swap_start = time.monotonic()
                current_rotated_buffer, next_rotated_buffer = next_rotated_buffer, current_rotated_buffer
                t0 = debug_time("Buffer swap", t_swap_start)
                
                t_display_start = time.monotonic()
                if update_display_fast(current_rotated_buffer, blocking=False):
                    t0 = debug_time("update_display_fast (non-blocking)", t_display_start)
                    
                    current += 1
                    t0 = debug_time("Increment current", t0)
                    
                    t_cleanup_start = time.monotonic()
                    keep_only_needed_remainders(current)
                    t0 = debug_time("keep_only_needed_remainders", t_cleanup_start)
                    
                    next_page_ready = False
                    end_of_index_reached = False
                    
                    if current + 1 < len(page_offsets):
                        if DEBUG_TIMING:
                            print("[DEBUG] Pre-rendering next page from existing index")
                        t_prerender_start = time.monotonic()
                        render_page_and_rotate(current + 1, next_rotated_buffer)
                        t0 = debug_time("render_page_and_rotate (next)", t_prerender_start)
                        next_page_ready = True
                    elif not end_of_index_reached:
                        if DEBUG_TIMING:
                            print("[DEBUG] Need to paginate for next page")
                        current_offset = page_offsets[current]
                        curr_rem = page_remainders.get(current, b"")
                        
                        t_paginate_start = time.monotonic()
                        lines, next_off, next_rem = paginate_text(text_file, current_offset, curr_rem)
                        t0 = debug_time("paginate_text", t_paginate_start)
                        
                        advanced = (next_off > current_offset) or (next_rem and next_rem != curr_rem)
                        is_loop_stuck = (next_off == current_offset) and (0 < len(next_rem) < MAX_CHARS * 2)
                        
                        if is_loop_stuck:
                            if DEBUG_TIMING:
                                print("[DEBUG] Loop stuck detected, attempting recovery")
                            lines_skipped, skip_off, skip_rem = paginate_text(text_file, current_offset, b"")
                            if skip_off > current_offset:
                                next_off, next_rem = skip_off, skip_rem
                                advanced = True
                            else:
                                advanced = False

                        if lines and advanced:
                            page_offsets.append(next_off)
                            page_remainders[current+1] = next_rem
                            t_prerender_start = time.monotonic()
                            render_page_and_rotate(current + 1, next_rotated_buffer)
                            t0 = debug_time("render_page_and_rotate (next, after paginate)", t_prerender_start)
                            next_page_ready = True
                        else:
                            end_of_index_reached = True
                    
                    t_wait_start = time.monotonic()
                    wait_for_display()
                    t0 = debug_time("wait_for_display", t_wait_start)
                else:
                    t0 = debug_time("update_display_fast (blocking fallback)", t_display_start)
                    current += 1
                    keep_only_needed_remainders(current)
                    next_page_ready = False
                    end_of_index_reached = False
                
            elif current + 1 < len(page_offsets):
                if DEBUG_TIMING:
                    print("[DEBUG] PATH: Next page in index but not pre-rendered")
                
                current += 1
                keep_only_needed_remainders(current)
                
                t_render_start = time.monotonic()
                render_page_and_rotate(current, current_rotated_buffer)
                t0 = debug_time("render_page_and_rotate (current)", t_render_start)
                
                t_display_start = time.monotonic()
                if update_display_fast(current_rotated_buffer, blocking=False):
                    t0 = debug_time("update_display_fast (non-blocking)", t_display_start)
                    end_of_index_reached = False
                    
                    if current + 1 < len(page_offsets):
                        t_prerender_start = time.monotonic()
                        render_page_and_rotate(current + 1, next_rotated_buffer)
                        t0 = debug_time("render_page_and_rotate (next)", t_prerender_start)
                        next_page_ready = True
                    elif not end_of_index_reached:
                        current_offset = page_offsets[current]
                        curr_rem = page_remainders.get(current, b"")
                        
                        t_paginate_start = time.monotonic()
                        lines, next_off, next_rem = paginate_text(text_file, current_offset, curr_rem)
                        t0 = debug_time("paginate_text", t_paginate_start)
                        
                        advanced = (next_off > current_offset) or (next_rem and next_rem != curr_rem)
                        is_loop_stuck = (next_off == current_offset) and (0 < len(next_rem) < MAX_CHARS * 2)
                        
                        if is_loop_stuck:
                            lines_skipped, skip_off, skip_rem = paginate_text(text_file, current_offset, b"")
                            if skip_off > current_offset:
                                next_off, next_rem = skip_off, skip_rem
                                advanced = True
                            else:
                                advanced = False

                        if lines and advanced:
                            page_offsets.append(next_off)
                            page_remainders[current+1] = next_rem
                            t_prerender_start = time.monotonic()
                            render_page_and_rotate(current + 1, next_rotated_buffer)
                            t0 = debug_time("render_page_and_rotate (next)", t_prerender_start)
                            next_page_ready = True
                        else:
                            end_of_index_reached = True
                    
                    t_wait_start = time.monotonic()
                    wait_for_display()
                    t0 = debug_time("wait_for_display", t_wait_start)
                
            elif len(page_offsets) > 0:
                if DEBUG_TIMING:
                    print("[DEBUG] PATH: Need to paginate new page")
                
                current_offset = page_offsets[current]
                curr_rem = page_remainders.get(current, b"")
                
                t_paginate_start = time.monotonic()
                lines, next_off, next_rem = paginate_text(text_file, current_offset, curr_rem)
                t0 = debug_time("paginate_text (initial)", t_paginate_start)
                
                advanced = (next_off > current_offset) or (next_rem and next_rem != curr_rem)
                is_loop_stuck = (next_off == current_offset) and (0 < len(next_rem) < MAX_CHARS * 2)
                
                if is_loop_stuck:
                    if DEBUG_TIMING:
                        print("[DEBUG] Loop stuck detected")
                    lines_skipped, skip_off, skip_rem = paginate_text(text_file, current_offset, b"")
                    
                    if skip_off > current_offset:
                        next_off, next_rem = skip_off, skip_rem
                        lines, next_off, next_rem = paginate_text(text_file, next_off, skip_rem)
                        advanced = True
                    else:
                        advanced = False
                
                if lines and advanced:
                    page_offsets.append(next_off)
                    page_remainders[current+1] = next_rem
                    current += 1
                    keep_only_needed_remainders(current)
                    
                    t_render_start = time.monotonic()
                    render_page_and_rotate(current, current_rotated_buffer)
                    t0 = debug_time("render_page_and_rotate (current)", t_render_start)
                    
                    t_display_start = time.monotonic()
                    if update_display_fast(current_rotated_buffer, blocking=False):
                        t0 = debug_time("update_display_fast (non-blocking)", t_display_start)
                        end_of_index_reached = False
                        
                        if current + 1 < len(page_offsets):
                            t_prerender_start = time.monotonic()
                            render_page_and_rotate(current + 1, next_rotated_buffer)
                            t0 = debug_time("render_page_and_rotate (next)", t_prerender_start)
                            next_page_ready = True
                        elif not end_of_index_reached:
                            current_offset = page_offsets[current]
                            curr_rem = page_remainders.get(current, b"")
                            
                            t_paginate_start = time.monotonic()
                            lines, next_off, next_rem = paginate_text(text_file, current_offset, curr_rem)
                            t0 = debug_time("paginate_text", t_paginate_start)
                            
                            advanced = (next_off > current_offset) or (next_rem and next_rem != curr_rem)
                            is_loop_stuck = (next_off == current_offset) and (0 < len(next_rem) < MAX_CHARS * 2)
                            
                            if is_loop_stuck:
                                lines_skipped, skip_off, skip_rem = paginate_text(text_file, current_offset, b"")
                                if skip_off > current_offset:
                                    next_off, next_rem = skip_off, skip_rem
                                    advanced = True
                                else:
                                    advanced = False

                            if lines and advanced:
                                page_offsets.append(next_off)
                                page_remainders[current+1] = next_rem
                                t_prerender_start = time.monotonic()
                                render_page_and_rotate(current + 1, next_rotated_buffer)
                                t0 = debug_time("render_page_and_rotate (next)", t_prerender_start)
                                next_page_ready = True
                            else:
                                end_of_index_reached = True
                        
                        t_wait_start = time.monotonic()
                        wait_for_display()
                        t0 = debug_time("wait_for_display", t_wait_start)
                else:
                    end_of_index_reached = True
                    if DEBUG_TIMING:
                        print("[DEBUG] End of file reached")

            state["current_page"] = current
        
        t_final_sleep = time.monotonic()
        time.sleep(0.003)
        debug_time("Final sleep", t_final_sleep)
        
        led_off()
        
        if DEBUG_TIMING:
            total_time = (time.monotonic() - t_total_start) * 1000
            print("-"*50)
            print(f"[DEBUG] TOTAL DOWN PRESS TIME: {total_time:.1f}ms")
            print("="*50 + "\n")

    # PAGE UP
    if button_pressed(buttons["up"]):
        led_on()
        if current > 0:
            current -= 1
            keep_only_needed_remainders(current)
            
            render_page_and_rotate(current, current_rotated_buffer)
            update_display_fast(current_rotated_buffer)
            
            state["current_page"] = current
            end_of_index_reached = False
            
            if current + 1 < len(page_offsets):
                render_page_and_rotate(current + 1, next_rotated_buffer)
                next_page_ready = True
        
        gc.collect()
        led_off()

    # FILE PICKER (SHORT PRESS) / FULL REFRESH (LONG PRESS)
    if button_pressed(buttons["a"]):
        led_on()
        
        # Measure press duration
        press_start = time.monotonic()
        while button_pressed(buttons["a"]):
            time.sleep(0.05)
        press_duration = time.monotonic() - press_start
        
        if press_duration > 0.7:  # Long press: Full refresh with speed 0
            display.set_speed(0, no_flickering=False)
            update_display_fast(current_rotated_buffer, blocking=True)
            display.set_speed(ORIGINAL_SPEED, no_flickering=ORIGINAL_NO_FLICKERING)
            led_off()
        else:  # Short press: File picker
            # Save current state before entering picker
            state["current_page"] = current
            save_index(INDEX_FILE, current)
            state_save(state)
            saved_current = current
            saved_next_page_ready = next_page_ready
            for i in range(3): # Multiple sync attempts to ensure data is flushed
                try:
                    os.sync()
                    time.sleep(0.2)
                except:
                    pass
            time.sleep(0.3)
            try:
                print(f"veryfing write to storage")
                verify_state = state_load()
                if verify_state.get("current_page") != current:
                    print(f"WARNING - State verification failed! Expected {current}, got {verify_state.get('current_page')}")
            except Exception as e:
                print(f"WARNING - Could not verify state: {e}")
            time.sleep(0.3)
            new_book = file_picker()
            
            if new_book:
                led_on()
                
                # Save OLD book's index before switching (only if actually switching)
                if text_file != new_book:
                    save_index(INDEX_FILE, current)
                    state_save(state)
                    
                    text_file = new_book
                    state["last_book"] = text_file
                    
                    INDEX_FILE = "/state/" + text_file.replace("/", "_").replace(".", "_") + ".idx"
                    
                    try:
                        os.stat(INDEX_FILE)
                        current = load_index(INDEX_FILE)
                    except OSError:
                        page_offsets = [0]
                        page_remainders = {}
                        current = 0

                    current = min(current, max(0, len(page_offsets)-1))
                    state["current_page"] = current
                    
                    # Clean up loaded remainders
                    keep_only_needed_remainders(current)
                    
                    end_of_index_reached = False 

                    render_page_and_rotate(current, current_rotated_buffer)
                    update_display_fast(current_rotated_buffer)
                    
                    next_page_ready = False
                    
                    if current + 1 >= len(page_offsets):
                         current_offset = page_offsets[current]
                         curr_rem = page_remainders.get(current, b"")
                         
                         lines, next_off, next_rem = paginate_text(text_file, current_offset, curr_rem)
                         
                         advanced = (next_off > current_offset) or (next_rem and next_rem != curr_rem)
                         is_loop_stuck = (next_off == current_offset) and (next_rem == curr_rem) and (len(next_rem) > 0)
                         
                         if is_loop_stuck:
                              lines_skipped, skip_off, skip_rem = paginate_text(text_file, current_offset, b"")
                         
                              if skip_off > current_offset:
                                  next_off, next_rem = skip_off, skip_rem
                                  advanced = True 
                              else:
                                  advanced = False

                         if lines and advanced: 
                             page_offsets.append(next_off)
                             page_remainders[current+1] = next_rem
                         else:
                             end_of_index_reached = True

                    if current + 1 < len(page_offsets):
                         render_page_and_rotate(current + 1, next_rotated_buffer)
                         next_page_ready = True
                    
                    keep_only_needed_remainders(current)
                    save_index(INDEX_FILE, current) 
                    state_save(state)
                    gc.collect()
                else:
                    # Same book selected - just restore display
                    current = saved_current
                    next_page_ready = saved_next_page_ready
                    render_page_and_rotate(current, current_rotated_buffer)
                    update_display_fast(current_rotated_buffer)
                    
                    if not next_page_ready and current + 1 < len(page_offsets):
                        render_page_and_rotate(current + 1, next_rotated_buffer)
                        next_page_ready = True
                    
                    gc.collect()
                
                led_off()
             
    # INACTIVITY TIMEOUT
    if time.monotonic() - last_activity > INACTIVITY_TIMEOUT:
        _, is_charging = get_battery_status()
        
        if is_charging:
            last_activity = time.monotonic()
        else:
            led_on()
            
            state["current_page"] = current

            save_index(INDEX_FILE, current)
            state_save(state)
            time.sleep(0.5)  # Give filesystem time to buffer
            
            for i in range(3): # Multiple sync attempts to ensure data is flushed
                try:
                    os.sync()
                    time.sleep(0.2)
                except:
                    pass
            
            time.sleep(0.3) # Wait a bit before verification
            
            try:
                verify_state = state_load()
                if verify_state.get("current_page") != current:
                    print(f"SLEEP: WARNING - State verification failed! Expected {current}, got {verify_state.get('current_page')}")
            except Exception as e:
                print(f"SLEEP: WARNING - Could not verify state: {e}")

            time.sleep(0.5) # Final delay before power down
                     
            board.ENABLE_DIO.value = False
            led_off()
