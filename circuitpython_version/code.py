"""
CircuitPython E-book Reader for Badger 2040
Ported from MicroPython to use uc8151_circuitpython driver

FINAL VERSION 2.20 (FORCED SKIP FIX):
- FIXED: Infinite page loop caused by long lines that wrap perfectly, 
         which returned the same offset/remainder for the next page.
- Implements a targeted 'forced skip' logic: when the loop condition 
  (identical next_offset and next_remainder) is met, the code re-runs 
  pagination with an empty remainder to force the file stream to advance 
  past the problematic line, effectively breaking the index loop.
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
            
            current_buffer = remainder
            line_start_offset = start_offset
            
            for _ in range(LINES_PER_PAGE):
                
                # 1. Ensure we have raw bytes to process
                if not current_buffer:
                    line_start_offset = f.tell()
                    line_bytes = f.readline()
                    
                    if not line_bytes:
                        next_offset = f.tell()
                        print(f"DEBUG: Paginate END OF FILE. next_offset={next_offset}")
                        break
                        
                    current_buffer = line_bytes.rstrip(b"\r\n")
                    print(f"DEBUG: Read file. line_start_offset={line_start_offset}, line_len={len(line_bytes)}")
                    
                # 2. Word Wrapping - WORK WITH ORIGINAL BYTES
                original_bytes = current_buffer  # Keep original bytes!
                current_buffer = current_buffer.lstrip(b" \t")
                
                if not current_buffer:
                    lines.append(b"")
                    current_buffer = b""
                    continue
                    
                # Decode for display logic only
                try:
                    line_str = current_buffer.decode("utf-8", "ignore")
                except:
                    line_str = current_buffer.decode("latin-1", "ignore")
                    
                words = line_str.split(" ")
                
                line_to_display = ""
                byte_idx = 0  # Track position in ORIGINAL current_buffer bytes
                
                for i, word in enumerate(words):
                    if not word:
                        byte_idx += 1  # Skip the space
                        continue
                    
                    appended = line_to_display + " " + word if line_to_display else word
                    
                    if len(appended) <= MAX_CHARS:
                        line_to_display = appended
                        # Track bytes: word + space (if not last)
                        word_bytes = word.encode("utf-8", "ignore")
                        byte_idx += len(word_bytes)
                        if i < len(words) - 1:
                            byte_idx += 1  # Space after word
                    else:
                        # Line break needed!
                        # Remainder is the REST of the original bytes
                        current_buffer = current_buffer[byte_idx:]
                        break
                else:
                    # All words fit, no remainder from this line
                    current_buffer = b""
                
                # Encode the finished line for the current page
                lines.append(line_to_display.encode("utf-8", "ignore"))

            # 4. Calculate the final next_offset and remainder
            if current_buffer:
                # Page is full, broke mid-line
                # Calculate how many bytes we consumed from the original line
                bytes_consumed = len(original_bytes) - len(current_buffer)
                next_offset = line_start_offset + bytes_consumed
                final_remainder = current_buffer
            else:
                # Consumed the last file line fully
                next_offset = f.tell()
                final_remainder = b""

            print(f"DEBUG: Paginate finished. next_offset={next_offset}, final_rem_len={len(final_remainder)}")
            gc.collect()
            return lines, next_offset, final_remainder
            
    except OSError:
        gc.collect()
        return [], start_offset, b""
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
def save_index(file_path):
    global page_offsets, page_remainders
    try:
        with open(file_path, "wb") as f:
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
    except OSError:
        pass

def load_index(file_path):
    global page_offsets, page_remainders
    try:
        with open(file_path, "rb") as f:
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
    except Exception:
        page_offsets = [0]
        page_remainders = {}

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
        # Clear screen using large font
        display.fb.fill(0)
        display.text("No books found!", 10, 40, 1)
        display.update()
        time.sleep(2)
        return None
    
    selected = 0
    offset = 0
    per_page = 6
    
    while True:
        # Drawing to raw_working_buffer (Landscape)
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
                if len(name) > 25: name = name[:22] + "..."
                y = 25 + i * 16
                if idx == selected:
                    display.fb.fill_rect(2, y-2, WIDTH-4, 16, 1)
                    # Note: display.text here uses vga2_8x16 (the external_font)
                    display.text(name, 5, y, 0) 
                else:
                    display.text(name, 5, y, 1)
                    
            # ----------------- Bottom Status Bar -----------------
            
            # 1. Page Count (vga2_8x16 font)
            if len(books) > per_page:
                page = offset // per_page + 1
                total = (len(books) + per_page - 1) // per_page
                display.text(f"{page}/{total}", WIDTH - (vga2_8x16.WIDTH * 5), HEIGHT - vga2_8x16.HEIGHT - 10, 1)
                
            # 2. Storage Status (5x8 font) - Absolute bottom right
            storage_status = get_storage_status()
            
            STATUS_X = WIDTH - (len(storage_status) * FONT_W_5X8) - TEXT_PADDING
            STATUS_Y = HEIGHT - FONT_H_5X8 - TEXT_PADDING 
            
            # Draw using the adafruit_framebuf's 5x8 font
            temp_fb.text(storage_status, STATUS_X, STATUS_Y, 1, font_name="font5x8.bin")
            # -----------------------------------------------------
            
        finally:
            display.fb = old_fb
            display.raw_fb = old_raw_fb
        
        # Rotation and Update
        rotated = display._rotate_framebuffer(raw_working_buffer)
        old_rot = display.rotation
        display.rotation = 0
        display.update(fb=rotated)
        display.rotation = old_rot
        
        while True:
            if button_pressed(buttons["down"]):
                selected = (selected + 1) % len(books)
                if selected < offset: offset = selected
                elif selected >= offset + per_page: offset = selected - per_page + 1
                time.sleep(0.15)
                break
            if button_pressed(buttons["up"]):
                selected = (selected - 1) % len(books)
                if selected < offset: offset = selected
                elif selected >= offset + per_page: offset = selected - per_page + 1
                time.sleep(0.15)
                break
            if button_pressed(buttons["a"]) or button_pressed(buttons["c"]):
                time.sleep(0.2)
                end_of_index_reached = False # Reset flag on book change
                return books[selected]
            if button_pressed(buttons["b"]):
                time.sleep(0.2)
                return None
            time.sleep(0.05)

# ---------------- MAIN -----------------
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
    load_index(INDEX_FILE)
except OSError:
    page_offsets = [0]
    page_remainders = {}
    
end_of_index_reached = False # Initial reset of flag

current = min(state.get("current_page", 0), max(0, len(page_offsets)-1))
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
         save_index(INDEX_FILE) # Save newly created index
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
    if button_pressed(buttons["down"]):
        
        led_on() # IMMEDIATE FEEDBACK
        
        if current + 1 < len(page_offsets) and next_page_ready:
            # Instant swap (indexed page)
            current_rotated_buffer, next_rotated_buffer = next_rotated_buffer, current_rotated_buffer
            update_display_fast(current_rotated_buffer)
            current += 1
            next_page_ready = False
            print(f"DEBUG: Page Down to P{current}. Used pre-indexed page.")
            end_of_index_reached = False # Reset flag on successful advance
            
        elif current + 1 < len(page_offsets):
            # Demand render (indexed page)
            current += 1
            render_page_and_rotate(current, current_rotated_buffer)
            update_display_fast(current_rotated_buffer)
            print(f"DEBUG: Page Down to P{current}. Demand-rendered indexed page.")
            end_of_index_reached = False # Reset flag on successful advance
            
        elif len(page_offsets) > 0:
             # Try calc next and render (NEW INDEX PAGE CREATED)
             
             # Current state of the page we are trying to advance from
             current_offset = page_offsets[current]
             curr_rem = page_remainders.get(current, b"")
             
             # First attempt to paginate normally
             lines, next_off, next_rem = paginate_text(text_file, current_offset, curr_rem)
             
             print(f"DEBUG: Indexing check P{current}->P{current+1}. Next_off={next_off}, Current_off={current_offset}, Next_rem_len={len(next_rem)}")
             
             # Check for Advancement: File offset MUST move OR the remainder MUST change.
             advanced = (next_off > current_offset) or (next_rem and next_rem != curr_rem)
             
             # --- CRITICAL LOOP DETECTION AND FORCED SKIP ---
             is_loop_stuck = (next_off == current_offset) and (next_rem == curr_rem) and (len(next_rem) > 0)
             
             if is_loop_stuck:
                 print("DEBUG: Infinite loop detected (Next_off=Current_off and Next_rem=Curr_rem). Attempting forced skip.")
                 
                 # Force the file pointer past the long line by running pagination with NO remainder.
                 # This makes `paginate_text` consume the rest of the line starting at current_offset.
                 lines_skipped, skip_off, skip_rem = paginate_text(text_file, current_offset, b"")
                 
                 if skip_off > current_offset:
                     # Successfully advanced the file pointer to the start of the *next* file line.
                     next_off, next_rem = skip_off, skip_rem
                     print(f"DEBUG: Forced skip SUCCESS. New index offset: {next_off}")
                     # The page still needs to display lines (from the new next_off)
                     lines, next_off, next_rem = paginate_text(text_file, next_off, skip_rem)
                     advanced = True # Force success for indexing
                 else:
                     # Skip failed, likely end of book with a long line.
                     print("DEBUG: Forced skip failed to advance file pointer.")
                     advanced = False
             # ------------------------------------------------
             
             # Final check for indexing
             if lines and advanced:
                 print(f"DEBUG: INDEX CREATED for P{current+1} at offset {next_off}")
                 page_offsets.append(next_off)
                 page_remainders[current+1] = next_rem
                 current += 1
                 render_page_and_rotate(current, current_rotated_buffer)
                 update_display_fast(current_rotated_buffer)
                 save_index(INDEX_FILE) 
                 end_of_index_reached = False # SUCCESS: Reset flag
             else:
                 print("DEBUG: Page advancement stopped. Indexing condition failed. End of book/indexed region reached.")
                 # No current++ here. Page does not advance.
                 end_of_index_reached = True # FAILURE: Set flag to block background pre-render

        state["current_page"] = current # <-- UPDATE IN MEMORY ONLY
        
        # Background Pre-render
        next_page_ready = False
        if current + 1 < len(page_offsets):
            render_page_and_rotate(current + 1, next_rotated_buffer)
            next_page_ready = True
        elif not end_of_index_reached: # Only try to generate the next index if we haven't hit the end/loop
             # Pre-render logic for creating the next page index if one doesn't exist
             current_offset = page_offsets[current]
             curr_rem = page_remainders.get(current, b"")
             
             lines, next_off, next_rem = paginate_text(text_file, current_offset, curr_rem)
             
             print(f"DEBUG: Pre-render check P{current}->P{current+1}. Next_off={next_off}, Current_off={current_offset}, Next_rem_len={len(next_rem)}")
             
             advanced = (next_off > current_offset) or (next_rem and next_rem != curr_rem)
             is_loop_stuck = (next_off == current_offset) and (next_rem == curr_rem) and (len(next_rem) > 0)
             
             if is_loop_stuck:
                 print("DEBUG: Pre-render loop detected. Attempting forced skip for index.")
                 lines_skipped, skip_off, skip_rem = paginate_text(text_file, current_offset, b"")
                 
                 if skip_off > current_offset:
                     next_off, next_rem = skip_off, skip_rem
                     print(f"DEBUG: Pre-render skip SUCCESS. New index offset: {next_off}")
                     advanced = True 
                 else:
                     advanced = False # Skip failed, stop pre-indexing

             if lines and advanced:
                 print(f"DEBUG: INDEX CREATED for P{current+1} at offset {next_off}")
                 page_offsets.append(next_off)
                 page_remainders[current+1] = next_rem
                 render_page_and_rotate(current + 1, next_rotated_buffer)
                 next_page_ready = True
                 save_index(INDEX_FILE)
             else:
                 end_of_index_reached = True # Set flag to block future attempts in the main loop
        
        led_off() # TURN OFF AT END

        t_start = time.monotonic()
        while button_pressed(buttons["down"]) and (time.monotonic() - t_start < 0.3): pass
        time.sleep(0.05)

    # PAGE UP
    if button_pressed(buttons["up"]):
        led_on() # IMMEDIATE FEEDBACK
        if current > 0:
            current -= 1
            render_page_and_rotate(current, current_rotated_buffer)
            update_display_fast(current_rotated_buffer)
            
            state["current_page"] = current # <-- UPDATE IN MEMORY ONLY
            print(f"DEBUG: Page Up to P{current}.")
            
            # Reset the 'stuck' flag when moving backward
            end_of_index_reached = False
            
            # Background Pre-render
            if current + 1 < len(page_offsets):
                render_page_and_rotate(current + 1, next_rotated_buffer)
                next_page_ready = True
        led_off() # TURN OFF AT END
        time.sleep(0.2)

    # FILE PICKER (Progress save is REQUIRED here)
    if button_pressed(buttons["a"]):
        
        # --- SAVE PROGRESS BEFORE ENTERING FILE PICKER ---
        print(f"DEBUG: Saving state and index before entering file picker. Current Page: {current}")
        save_index(INDEX_FILE) 
        state_save(state)
        
        # LED is OFF during interactive file_picker()
        new_book = file_picker()
        
        if new_book:
            led_on() # TURN ON for slow process of loading/rendering
            
            # 1. Reload state from disk to ensure 'state' holds the latest saved page for the selected book.
            state = state_load() 
            
            # Get the name of the book that was saved in the state file
            saved_book = state.get("last_book", "")
            
            # Set the current book to the one selected
            text_file = new_book
            state["last_book"] = text_file
            
            INDEX_FILE = "/state/" + text_file.replace("/", "_").replace(".", "_") + ".idx"
            
            # 2. Load the index (offsets) for the selected book.
            try:
                os.stat(INDEX_FILE)
                load_index(INDEX_FILE) 
                print(f"DEBUG: Index loaded for {text_file}. Total pages indexed: {len(page_offsets)}")
            except OSError:
                page_offsets = [0]
                page_remainders = {}
                print(f"DEBUG: Index not found for {text_file}. Starting fresh.")

            # 3. Determine starting page
            if saved_book == text_file:
                 # The state file tracks this book, so load its saved page
                current = state.get("current_page", 0)
            else:
                # The state file tracks a different book, so start this one at page 0.
                current = 0
            
            # 4. Enforce boundaries and update in-memory state
            current = min(current, max(0, len(page_offsets)-1))
            state["current_page"] = current
            print(f"DEBUG: Resuming/Starting {text_file} at Page {current}. Offset: {page_offsets[current]}")
            
            # Reset flag
            end_of_index_reached = False 

            # 5. Render current page 
            render_page_and_rotate(current, current_rotated_buffer)
            update_display_fast(current_rotated_buffer)
            
            # 6. Background Pre-render (Generate next page index if needed)
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
            
            # 7. Save final state/index (Index may have changed due to pre-render)
            save_index(INDEX_FILE) 
            
            led_off() # TURN OFF after loading/rendering
        else:
             # If no book was selected, just redraw the previous content
             update_display_fast(current_rotated_buffer) 
             
    # INACTIVITY TIMEOUT (Progress save is REQUIRED here)
    if time.monotonic() - last_activity > INACTIVITY_TIMEOUT:
        # Check if we're on USB power
        _, is_charging = get_battery_status()
        
        if is_charging:
            # On USB: Don't sleep at all, just reset timer
            print("On USB power - staying awake")
            last_activity = time.monotonic()
        else:
            # On battery: Light sleep with button polling
            led_on()
            
            print(f"DEBUG: Inactivity timeout. Saving state. Current Page: {current}")
            state["current_page"] = current
            save_index(INDEX_FILE)
            state_save(state)
            time.sleep(0.5)
            led_off()
            
            # Turn off e-ink display to save power
            # (the display itself draws minimal power when not updating)
            
            print("Entering low-power idle. Press any button to wake...")
            
            board.ENABLE_DIO.value = False
            print("ENABLE_DIO set to low - powered down")



