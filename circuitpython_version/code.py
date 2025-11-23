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
import gc # REQUIRED for manual garbage collection
import adafruit_framebuf
import analogio   
import microcontroller # Used for checking pins and safe idling
import alarm

displayio.release_displays()

from uc8151_circuitpython import UC8151

# ---------------- STATE -----------------
STATE_FILE = "/state/ebook_state.bin"

# Create directories if they don't exist
for d in ["/books", "/state"]:
    try:
        os.mkdir(d)
    except OSError:
        pass

def state_save(state):
    try:
        with open(STATE_FILE, "wb") as f:
            file_path = state.get("last_book", "")
            data = struct.pack("<I", state.get("current_page", 0))
            f.write(data)
            f.write(struct.pack("<H", len(file_path)))
            f.write(file_path.encode("utf-8"))
    except OSError:
        pass
    except Exception as e:
        print("Error saving state:", e)

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
    except OSError:
        pass
    except Exception as e:
        print("state_load failed:", e)
    return state

# ---------------- CONFIG -----------------
LINES_PER_PAGE = 9
LINE_HEIGHT = vga2_8x16.HEIGHT - 2 
TEXT_PADDING = 2
WIDTH = 296
HEIGHT = 128
TEXT_WIDTH = WIDTH - TEXT_PADDING*2
MAX_CHARS = TEXT_WIDTH // vga2_8x16.WIDTH # ~35 characters per line
INACTIVITY_TIMEOUT = 300 # in seconds
BOOK_DIR = "/books"

# 5x8 Font Dimensions (used for status text)
FONT_W_5X8 = 5 
FONT_H_5X8 = 8

last_activity = time.monotonic()

# ---------------- GLOBAL INDEX STATE -----------------
# Flag to prevent *background* pre-rendering when we know we're stuck at the end/loop.
end_of_index_reached = False 
# -----------------------------------------------------

# ---------------- DISPLAY -----------------
spi = board.SPI()
display = UC8151(
    spi,
    cs=board.INKY_CS,
    dc=board.INKY_DC,
    rst=board.INKY_RST,
    busy=board.INKY_BUSY,
    rotation=270,
    external_font=vga2_8x16, 
    use_framebuf_font=True,  # ENABLED for 5x8 FONT
    font_path="font5x8.bin", # PATH TO SMALLEST FONT
    speed=4,
    no_flickering=False,
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

# ---------------- LED (Global Definition) -----------------
led = None
try:
    # Use the pre-imported microcontroller module
    led = digitalio.DigitalInOut(microcontroller.pin.GPIO25)
    led.direction = digitalio.Direction.OUTPUT
    led.value = False
except:
    try:
        # Fallback for other boards
        led = digitalio.DigitalInOut(board.LED)
        led.direction = digitalio.Direction.OUTPUT
        led.value = False
    except:
        pass

def led_on():
    if led: led.value = True

def led_off():
    if led: led.value = False

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
        return -1, False # Percentage, Is_Charging

    is_charging = vbus_sense.value

    # Get battery percentage
    vref.value = True
    time.sleep(0.02)
    
    raw_sum = 0
    for _ in range(5):
        raw_sum += adc.value
    reading = raw_sum / 5
    
    vref.value = False
    
    # Voltage calculation (3.3V / 65535 * 3)
    voltage = reading * (3.3 / 65535) * 3 
    
    # Percentage calculation (3.2V empty to 4.1V full)
    percent = (voltage - 3.2) / (4.1 - 3.2) * 100
    
    return int(max(0, min(100, percent))), is_charging

# ---------------- STORAGE STATUS -----------------
def get_storage_status():
    """Calculates and returns the free/total storage space as a formatted string."""
    try:
        stat = os.statvfs('/')
        # Calculate raw bytes
        total_bytes = stat[0] * stat[2]
        free_bytes = stat[0] * stat[3]
        
        # Convert to Megabytes (1 MB = 1024 * 1024 bytes)
        mb_divisor = 1024 * 1024
        
        total_mb = total_bytes / mb_divisor
        free_mb = free_bytes / mb_divisor
        
        # Format: Free: X.X/Y.Y MB (using one decimal place for brevity)
        return f"Free: {free_mb:.1f}/{total_mb:.1f} MB" 
        
    except Exception:
        return "Storage N/A"

# ---------------- TEXT PROCESSING (Memory Optimized) -----------------

def paginate_text(file_path, start_offset, remainder=b""):
    """
    Reads from the file stream and word-wraps the text into LINES_PER_PAGE lines.
    """
    print(f"DEBUG: Paginate called. Start_offset={start_offset}, Remainder_len={len(remainder)}")
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
                
                # Handle empty lines
                if not line:
                    lines.append(b"")
                    line_count += 1
                    if line_count >= LINES_PER_PAGE:
                        next_offset = f.tell()
                        break
                    continue
                
                # Decode - match MicroPython exactly
                try:
                    line_str = line.decode("utf-8", "ignore")
                except:
                    try:
                        line_str = line.decode("latin-1", "ignore")
                    except:
                        # Last resort: treat as ASCII, skip non-ASCII bytes
                        line_str = ''.join(chr(b) if b < 128 else '?' for b in line)
                
                # Clean unicode
                line_str = line_str.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")\
                           .replace("\u2014", "-").replace("\u2013", "-").replace("…", "...")
                
                words = line_str.split(" ")
                current = ""
                byte_idx = 0
                
                for i, word in enumerate(words):
                    if not word:
                        byte_idx += 1
                        continue
                    
                    appended = current + " " + word if current else word
                    
                    if len(appended) <= MAX_CHARS:
                        current = appended
                        byte_idx += len(word.encode("utf-8", "ignore")) + (1 if i < len(words)-1 else 0)
                    else:
                        # Line break
                        lines.append(current.encode("utf-8", "ignore"))
                        line_count += 1
                        
                        if line_count >= LINES_PER_PAGE:
                            remainder = line_bytes[byte_idx:]
                            next_offset = pos + byte_idx
                            break
                        
                        current = word
                        byte_idx += len(word.encode("utf-8", "ignore")) + (1 if i < len(words)-1 else 0)
                
                # Break check
                if next_offset != -1:
                    break
                
                # Add final line content
                if current:
                    lines.append(current.encode("utf-8", "ignore"))
                    line_count += 1
                
                if line_count >= LINES_PER_PAGE:
                    next_offset = f.tell()
                    break
            
            if next_offset == -1:
                next_offset = f.tell()
            
            print(f"DEBUG: Paginate finished. next_offset={next_offset}, final_rem_len={len(remainder)}")
            gc.collect()
            return lines, next_offset, remainder
            
    except Exception as e:
        print(f"ERROR: Paginate failure: {e}")
        import traceback
        traceback.print_exception(e)
        gc.collect()
        return [], start_offset, b""

# ---------------- RENDERING CORE -----------------
def render_page_and_rotate(page_num, target_rotated_buffer):
    """
    1. Renders text, battery status, and progress bar to raw_working_buffer (Landscape)
    2. Rotates it into target_rotated_buffer (Portrait)
    """
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
                    # Decode bytes to string for display
                    text = line.decode("utf-8", "replace")
                    # Clean up unicode quotes
                    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")\
                               .replace("\u2014", "-").replace("\u2013", "-")
                    # Draw using vga2_8x16 (the default external font)
                    display.text(text, TEXT_PADDING, y, 1)
                except:
                    pass
            # Increment Y even if line is empty (creates blank line)
            y += LINE_HEIGHT
            
        # ----------------- Battery Indicator (USING 5x8 FONT) -----------------
        pct, charging = get_battery_status()
        
        if charging:
             status_text = "USB"
        elif pct >= 0:
             status_text = f"{pct}%"
        else:
             status_text = ""
        
        if status_text:
            STATUS_X = WIDTH - (len(status_text) * FONT_W_5X8) - TEXT_PADDING
            STATUS_Y = TEXT_PADDING 
            
            # Draw using the adafruit_framebuf's 5x8 font
            temp_fb.text(status_text, STATUS_X, STATUS_Y, 1, font_name="font5x8.bin")
        # ----------------------------------------------------------------------
        
        # ----------------- Progress Bar (FIXED FILE POSITION IMPLEMENTATION) ------------------
        try:
            current_offset = page_offsets[page_num] 
            file_stats = os.stat(text_file)
            total_size = file_stats[6] # Index 6 is the file size in bytes
            
            if total_size > 0:
                progress_ratio = current_offset / total_size
                progress_ratio = max(0.0, min(1.0, progress_ratio)) 
                
                progress_width = int(progress_ratio * WIDTH)
                
                # Draw a 1-pixel high progress line at the very bottom (HEIGHT - 1)
                progress_width = max(1, min(WIDTH, progress_width)) 
                temp_fb.fill_rect(0, HEIGHT - 1, progress_width, 1, 1)

        except Exception as e:
            # Handle potential file errors during stat or index lookup
            print("Progress bar error:", e)
            pass 
        # ----------------------------------------------------------------------
            
    finally:
        display.fb = old_fb
        display.raw_fb = old_raw_fb
        gc.collect() # Force garbage collection after framebuffer/text objects are finished

    # ROTATION STEP:
    rotated_data = display._rotate_framebuffer(raw_working_buffer)
    for i in range(len(target_rotated_buffer)):
        target_rotated_buffer[i] = rotated_data[i]

def update_display_fast(rotated_buffer):
    """Sends an already rotated buffer to the display."""
    old_rot = display.rotation
    display.rotation = 0 
    display.update(blocking=True, fb=rotated_buffer)
    display.rotation = old_rot

# ---------------- INDEX STORAGE -----------------
def save_index(file_path, current_page=0):
    global page_offsets, page_remainders
    try:
        with open(file_path, "wb") as f:
            # Save current page first
            f.write(struct.pack("<I", current_page))
            # Save offsets
            f.write(struct.pack("<I", len(page_offsets)))
            for offset in page_offsets:
                f.write(struct.pack("<I", offset))
            # Save remainders
            keys = list(page_remainders.keys())
            f.write(struct.pack("<I", len(keys)))
            for k in keys:
                rem = page_remainders[k]
                f.write(struct.pack("<I", k))
                f.write(struct.pack("<I", len(rem)))
                f.write(rem)
    except OSError:
        pass

def load_index(file_path):
    global page_offsets, page_remainders
    try:
        with open(file_path, "rb") as f:
            # Load current page first
            saved_page = struct.unpack("<I", f.read(4))[0]
            # Load offsets
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
    except Exception:
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
    
    # Pre-rendered buffers for each selection position
    selection_buffers = []
    
    while True:
        # Pre-render all selection positions when page changes
        if offset != prev_offset:
            t_start = time.monotonic()
            print(f"[PICKER] Pre-rendering all {per_page} selection states...")
            
            selection_buffers = []
            
            # Render each possible selection position
            for sel_idx in range(per_page):
                book_idx = offset + sel_idx
                if book_idx >= len(books):
                    break
                
                # Clear and render
                for i in range(len(raw_working_buffer)): raw_working_buffer[i] = 0
                temp_fb = adafruit_framebuf.FrameBuffer(raw_working_buffer, display.width, display.height, adafruit_framebuf.MHMSB)
                
                old_fb = display.fb
                old_raw_fb = display.raw_fb
                display.fb = temp_fb
                display.raw_fb = raw_working_buffer
                
                try:
                    temp_fb.fill(0)
                    display.text("Select Book:", 5, 5, 1)
                    
                    # Render all books, highlighting the current selection
                    for i in range(per_page):
                        idx = offset + i
                        if idx >= len(books): break
                        name = books[idx].split("/")[-1]
                        if len(name) > 25: name = name[:22] + "..."
                        y = 25 + i * 16
                        
                        if i == sel_idx:  # This is the selected item for this buffer
                            display.fb.fill_rect(2, y-2, WIDTH-4, 16, 1)
                            display.text(name, 5, y, 0)
                        else:
                            display.text(name, 5, y, 1)
                    
                    # Status bar
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
                
                # Rotate and store
                rotated = display._rotate_framebuffer(raw_working_buffer)
                buffer_copy = bytearray(len(rotated))
                for i in range(len(rotated)):
                    buffer_copy[i] = rotated[i]
                selection_buffers.append(buffer_copy)
            
            prev_offset = offset
            
            # Adjust selected to be within the current page
            if selected < offset:
                selected = offset
            elif selected >= offset + len(selection_buffers):
                selected = offset + len(selection_buffers) - 1
            
            print(f"[PICKER] Pre-render complete: {(time.monotonic() - t_start)*1000:.1f}ms for {len(selection_buffers)} buffers")
            
            # Initial display
            sel_index_on_page = selected - offset
            if 0 <= sel_index_on_page < len(selection_buffers):
                t_start = time.monotonic()
                old_rot = display.rotation
                display.rotation = 0
                display.update(fb=selection_buffers[sel_index_on_page])
                display.rotation = old_rot
                print(f"[PICKER] Initial display update: {(time.monotonic() - t_start)*1000:.1f}ms")
        
        # Button handling - just swap buffers!
        if button_pressed(buttons["down"]):
            print(f"[PICKER] DOWN pressed")
            t_start = time.monotonic()
            
            selected = (selected + 1) % len(books)
            
            # Check if we need to change page
            if selected < offset or selected >= offset + per_page:
                offset = (selected // per_page) * per_page
                # This will trigger pre-render on next loop iteration
            else:
                # Same page - just swap buffer
                sel_index_on_page = selected - offset
                if 0 <= sel_index_on_page < len(selection_buffers):
                    old_rot = display.rotation
                    display.rotation = 0
                    display.update(fb=selection_buffers[sel_index_on_page])
                    display.rotation = old_rot
                    print(f"[PICKER] Display update: {(time.monotonic() - t_start)*1000:.1f}ms")
            
            time.sleep(0.15)
            
        elif button_pressed(buttons["up"]):
            print(f"[PICKER] UP pressed")
            t_start = time.monotonic()
            
            selected = (selected - 1) % len(books)
            
            # Check if we need to change page
            if selected < offset or selected >= offset + per_page:
                offset = (selected // per_page) * per_page
                # This will trigger pre-render on next loop iteration
            else:
                # Same page - just swap buffer
                sel_index_on_page = selected - offset
                if 0 <= sel_index_on_page < len(selection_buffers):
                    old_rot = display.rotation
                    display.rotation = 0
                    display.update(fb=selection_buffers[sel_index_on_page])
                    display.rotation = old_rot
                    print(f"[PICKER] Display update: {(time.monotonic() - t_start)*1000:.1f}ms")
            
            time.sleep(0.15)
            
        elif button_pressed(buttons["a"]) or button_pressed(buttons["c"]):
            while button_pressed(buttons["a"]) or button_pressed(buttons["c"]):
                time.sleep(0.05)
            end_of_index_reached = False
            return books[selected]
            
        elif button_pressed(buttons["b"]):
            while button_pressed(buttons["b"]):
                time.sleep(0.05)
            return None
        
        time.sleep(0.05)

# ---------------- MAIN -----------------
# Re-enable power on boot/wake
board.ENABLE_DIO.value = True
time.sleep(0.1)  # Allow power rails to stabilize

led_on()
state = state_load()
text_file = state.get("last_book", "")

if not text_file:
    books = list_books()
    if books:
        text_file = books[0]
        state["last_book"] = text_file
    else:
        # Show "No books" message
        display.fb.fill(0)
        display.text("No books in /books", 10, 50, 1)
        display.update()
        while True: time.sleep(1)

INDEX_FILE = "/state/" + text_file.replace("/", "_").replace(".", "_") + ".idx"

try:
    os.stat(INDEX_FILE)
    current = load_index(INDEX_FILE)  # Get saved page from index
except OSError:
    page_offsets = [0]
    page_remainders = {}
    current = 0
    
end_of_index_reached = False # Initial reset of flag

# Enforce boundaries
current = min(current, max(0, len(page_offsets)-1))
state["current_page"] = current

print("Initial render...")
render_page_and_rotate(current, current_rotated_buffer)
update_display_fast(current_rotated_buffer)

# Index Page 1 if needed for initial pre-render
if current + 1 >= len(page_offsets):
     curr_rem = page_remainders.get(current, b"")
     lines, next_off, next_rem = paginate_text(text_file, page_offsets[current], curr_rem)
     
     # Check for Advancement: File offset MUST move OR the remainder MUST change.
     advanced = (next_off > page_offsets[current]) or (next_rem and next_rem != curr_rem)
     
     # Advance index if the page produced lines AND advanced content OR left a NEW remainder
     if lines and advanced:
         page_offsets.append(next_off)
         page_remainders[current+1] = next_rem
         save_index(INDEX_FILE, current) # Save newly created index
     else:
         end_of_index_reached = True

if current + 1 < len(page_offsets):
    render_page_and_rotate(current + 1, next_rotated_buffer)
    next_page_ready = True
led_off()

print("Ready!")

# --- MAIN LOOP ---
while True:
    if any(button_pressed(b) for b in buttons.values()):
        last_activity = time.monotonic()
        
    # PAGE DOWN
    if button_pressed(buttons["down"]) or button_pressed(buttons["c"]):
        # Detect long press
        press_start = time.monotonic()
        while button_pressed(buttons["down"]) or button_pressed(buttons["c"]):
            time.sleep(0.05)
        press_duration = time.monotonic() - press_start
        
        led_on() # IMMEDIATE FEEDBACK
        
        if press_duration > 0.7:  # Long press: 50-page fast advance
            print("DEBUG: Long press detected - advancing 50 pages")
            
            FAST_ADVANCE_PAGES = 50
            target_page = current + FAST_ADVANCE_PAGES
            
            # Track remainder through the loop (CRITICAL!)
            last_remainder = page_remainders.get(current, b"")
            
            # Fast-forward through pages without rendering
            for _ in range(FAST_ADVANCE_PAGES):
                next_page = current + 1
                
                if next_page >= len(page_offsets):
                    # Need to create new index
                    lines, next_off, next_rem = paginate_text(text_file, page_offsets[current], last_remainder)
                    
                    # Check for advancement
                    if next_off <= page_offsets[current]:
                        # Can't advance, end of book
                        break
                    
                    page_offsets.append(next_off)
                    page_remainders[next_page] = next_rem
                    last_remainder = next_rem
                else:
                    # Page already indexed, but still need to paginate to get remainder
                    _, next_off, next_rem = paginate_text(text_file, page_offsets[current], last_remainder)
                    page_remainders[next_page] = next_rem
                    last_remainder = next_rem
                
                current = next_page
                gc.collect()
                
                if current >= target_page:
                    break
            
            # Ensure we didn't overshoot
            current = min(current, len(page_offsets) - 1)
            
            # Now render and display the final page
            print(f"DEBUG: Fast-advance complete. Rendering page {current}")
            render_page_and_rotate(current, current_rotated_buffer)
            update_display_fast(current_rotated_buffer)
            
            state["current_page"] = current
            save_index(INDEX_FILE, current)
            gc.collect()
            
            # Background Pre-render next page
            next_page_ready = False
            if current + 1 < len(page_offsets):
                render_page_and_rotate(current + 1, next_rotated_buffer)
                next_page_ready = True
            
        else:  # Short press: normal single page advance
            if current + 1 < len(page_offsets) and next_page_ready:
                current_rotated_buffer, next_rotated_buffer = next_rotated_buffer, current_rotated_buffer
                update_display_fast(current_rotated_buffer)
                current += 1
                next_page_ready = False
                print(f"DEBUG: Page Down to P{current}. Used pre-indexed page.")
                end_of_index_reached = False
                
            elif current + 1 < len(page_offsets):
                current += 1
                render_page_and_rotate(current, current_rotated_buffer)
                update_display_fast(current_rotated_buffer)
                print(f"DEBUG: Page Down to P{current}. Demand-rendered indexed page.")
                end_of_index_reached = False
                
            elif len(page_offsets) > 0:
                current_offset = page_offsets[current]
                curr_rem = page_remainders.get(current, b"")
                
                lines, next_off, next_rem = paginate_text(text_file, current_offset, curr_rem)
                
                print(f"DEBUG: Indexing check P{current}->P{current+1}. Next_off={next_off}, Current_off={current_offset}, Next_rem_len={len(next_rem)}")
                
                advanced = (next_off > current_offset) or (next_rem and next_rem != curr_rem)
                is_loop_stuck = (next_off == current_offset) and (0 < len(next_rem) < MAX_CHARS * 2)
                
                if is_loop_stuck:
                    print("DEBUG: Infinite loop detected. Attempting forced skip.")
                    lines_skipped, skip_off, skip_rem = paginate_text(text_file, current_offset, b"")
                    
                    if skip_off > current_offset:
                        next_off, next_rem = skip_off, skip_rem
                        print(f"DEBUG: Forced skip SUCCESS. New index offset: {next_off}")
                        lines, next_off, next_rem = paginate_text(text_file, next_off, skip_rem)
                        advanced = True
                    else:
                        print("DEBUG: Forced skip failed to advance file pointer.")
                        advanced = False
                
                if lines and advanced:
                    print(f"DEBUG: INDEX CREATED for P{current+1} at offset {next_off}")
                    page_offsets.append(next_off)
                    page_remainders[current+1] = next_rem
                    current += 1
                    render_page_and_rotate(current, current_rotated_buffer)
                    update_display_fast(current_rotated_buffer)
                    save_index(INDEX_FILE, current) 
                    end_of_index_reached = False
                else:
                    print("DEBUG: Page advancement stopped. End of book/indexed region reached.")
                    end_of_index_reached = True

            state["current_page"] = current
            
            # Background Pre-render
            next_page_ready = False
            if current + 1 < len(page_offsets):
                render_page_and_rotate(current + 1, next_rotated_buffer)
                next_page_ready = True
            elif not end_of_index_reached:
                current_offset = page_offsets[current]
                curr_rem = page_remainders.get(current, b"")
                
                lines, next_off, next_rem = paginate_text(text_file, current_offset, curr_rem)
                
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
                    render_page_and_rotate(current + 1, next_rotated_buffer)
                    next_page_ready = True
                    save_index(INDEX_FILE, current)
                else:
                    end_of_index_reached = True
        
        led_off()
        time.sleep(0.05)

    # PAGE UP
    if button_pressed(buttons["up"]):
        led_on() # IMMEDIATE FEEDBACK
        if current > 0:
            current -= 1
            render_page_and_rotate(current, current_rotated_buffer)
            update_display_fast(current_rotated_buffer)
            
            state["current_page"] = current
            print(f"DEBUG: Page Up to P{current}.")
            
            # Reset the 'stuck' flag when moving backward
            end_of_index_reached = False
            
            # Background Pre-render
            if current + 1 < len(page_offsets):
                render_page_and_rotate(current + 1, next_rotated_buffer)
                next_page_ready = True
        led_off() # TURN OFF AT END
        time.sleep(0.2)

    # FILE PICKER
    if button_pressed(buttons["a"]):
        led_on()
        # --- SAVE PROGRESS BEFORE ENTERING FILE PICKER ---
        print(f"DEBUG: Saving state and index before entering file picker. Current Page: {current}")
        save_index(INDEX_FILE, current)
        state_save(state)
        
        # LED is OFF during interactive file_picker()
        new_book = file_picker()
        
        if new_book:
            led_on() # TURN ON for slow process of loading/rendering
            
            # Set the current book to the one selected
            text_file = new_book
            state["last_book"] = text_file
            
            INDEX_FILE = "/state/" + text_file.replace("/", "_").replace(".", "_") + ".idx"
            
            # Load the index (offsets) for the selected book.
            try:
                os.stat(INDEX_FILE)
                current = load_index(INDEX_FILE)  # Get saved page from index
                print(f"DEBUG: Index loaded for {text_file}. Total pages indexed: {len(page_offsets)}")
            except OSError:
                page_offsets = [0]
                page_remainders = {}
                current = 0
                print(f"DEBUG: Index not found for {text_file}. Starting fresh.")

            # Enforce boundaries and update in-memory state
            current = min(current, max(0, len(page_offsets)-1))
            state["current_page"] = current
            print(f"DEBUG: Resuming/Starting {text_file} at Page {current}. Offset: {page_offsets[current]}")
            
            # Reset flag
            end_of_index_reached = False 

            # Render current page 
            render_page_and_rotate(current, current_rotated_buffer)
            update_display_fast(current_rotated_buffer)
            
            # Background Pre-render (Generate next page index if needed)
            next_page_ready = False
            
            # Check if we need to index the next page
            if current + 1 >= len(page_offsets):
                 # Try to calculate and append the next page index
                 current_offset = page_offsets[current]
                 curr_rem = page_remainders.get(current, b"")
                 
                 lines, next_off, next_rem = paginate_text(text_file, current_offset, curr_rem)
                 
                 print(f"DEBUG: Initial pre-render check P{current}->P{current+1}. Next_off={next_off}, Current_off={current_offset}, Next_rem_len={len(next_rem)}")
                 
                 advanced = (next_off > current_offset) or (next_rem and next_rem != curr_rem)
                 is_loop_stuck = (next_off == current_offset) and (next_rem == curr_rem) and (len(next_rem) > 0)
                 
                 if is_loop_stuck:
                      print("DEBUG: Initial pre-render loop detected. Attempting forced skip for index.")
                      lines_skipped, skip_off, skip_rem = paginate_text(text_file, current_offset, b"")
                 
                      if skip_off > current_offset:
                          next_off, next_rem = skip_off, skip_rem
                          print(f"DEBUG: Initial pre-render skip SUCCESS. New index offset: {next_off}")
                          advanced = True 
                      else:
                          advanced = False # Skip failed, stop pre-indexing

                 # Advance index if the page produced lines AND advanced content OR left a NEW remainder
                 if lines and advanced: 
                     page_offsets.append(next_off)
                     page_remainders[current+1] = next_rem
                     print(f"DEBUG: Initial INDEX CREATED for P{current+1} at offset {next_off}")
                 else:
                     end_of_index_reached = True

            if current + 1 < len(page_offsets):
                 # Now it's safe to pre-render the next page
                 render_page_and_rotate(current + 1, next_rotated_buffer)
                 next_page_ready = True
                 print(f"DEBUG: P{current+1} pre-rendered.")
            
            # Save final state/index (Index may have changed due to pre-render)
            save_index(INDEX_FILE, current) 
            state_save(state)
            
            led_off() # TURN OFF after loading/rendering
        else:
             # If no book was selected, just redraw the previous content
             update_display_fast(current_rotated_buffer) 
             
    # INACTIVITY TIMEOUT
    if time.monotonic() - last_activity > INACTIVITY_TIMEOUT:
        # Check if we're on USB power
        _, is_charging = get_battery_status()
        
        if is_charging:
            # On USB: Don't sleep at all, just reset timer
            print("On USB power - staying awake")
            last_activity = time.monotonic()
        else:
            # On battery: Hardware sleep
            led_on()
            
            print(f"DEBUG: Inactivity timeout. Saving state. Current Page: {current}")
            state["current_page"] = current
            save_index(INDEX_FILE, current)
            state_save(state)
            time.sleep(0.5)
            led_off()
            
            print("Entering low-power idle. Press any button to wake...")
            
            board.ENABLE_DIO.value = False
            print("ENABLE_DIO set to low - powered down")

    time.sleep(0.01)
