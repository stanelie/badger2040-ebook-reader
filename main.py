import badger2040
import gc
import time
import os
import struct
import vga2_8x16
from machine import ADC, Pin

# ---------------- CONFIG -----------------
LINES_PER_PAGE = 9
LINE_HEIGHT = vga2_8x16.HEIGHT - 2
TEXT_PADDING = 2
WIDTH = badger2040.WIDTH
TEXT_WIDTH = WIDTH - TEXT_PADDING * 2
MAX_CHARS = TEXT_WIDTH // vga2_8x16.WIDTH
INACTIVITY_TIMEOUT = 60*1000
BOOK_DIR = "/books"
STATE_FILE = "/state/ebook.bin"
last = time.ticks_ms()

# ---------------- DISPLAY -----------------
display = badger2040.Badger2040()
display.set_update_speed(badger2040.UPDATE_TURBO)
display.led(0)

# ---------------- FONT -----------------
def character(asci, x, y, pen_color=0):
    if asci < vga2_8x16.FIRST or asci > vga2_8x16.LAST:
        asci = ord('?')
    idx = (asci - vga2_8x16.FIRST) * vga2_8x16.HEIGHT
    display.set_pen(pen_color)
    for y_off in range(vga2_8x16.HEIGHT):
        row = vga2_8x16.FONT[idx + y_off]
        start = -1
        for x_off in range(vga2_8x16.WIDTH):
            set_pixel = (row >> (7 - x_off)) & 1
            if set_pixel and start == -1:
                start = x_off
            elif not set_pixel and start != -1:
                display.rectangle(x + start, y + y_off, x_off - start, 1)
                start = -1
        if start != -1:
            display.rectangle(x + start, y + y_off, vga2_8x16.WIDTH - start, 1)

def prnt(text, x, y, pen_color=0):
    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")\
               .replace("\u2014", "-").replace("\u2013", "-")
    for c in text:
        character(ord(c) if vga2_8x16.FIRST <= ord(c) <= vga2_8x16.LAST else ord('?'),
                  x, y, pen_color=pen_color)
        x += vga2_8x16.WIDTH

# ---------------- STATE -----------------
def state_save(state):
    try:
        last_book_bytes = state.get("last_book", "").encode("utf-8")
        current_page = state.get("current_page", 0)
        with open(STATE_FILE, "wb") as f:
            f.write(struct.pack("<H", current_page))
            f.write(struct.pack("<H", len(last_book_bytes)))
            f.write(last_book_bytes)
    except Exception as e:
        print("state_save failed:", e)

def state_load():
    state = {"current_page": 0, "last_book": None}
    try:
        if os.stat(STATE_FILE)[6] > 0:
            with open(STATE_FILE, "rb") as f:
                current_page = struct.unpack("<H", f.read(2))[0]
                l = struct.unpack("<H", f.read(2))[0]
                last_book = f.read(l).decode("utf-8")
                state["current_page"] = current_page
                state["last_book"] = last_book
    except OSError:
        pass
    except Exception as e:
        print("state_load failed:", e)
    return state

# ---------------- BATTERY -----------------
def battery_percent():
    vref = Pin(27, Pin.OUT)
    vref.value(1)
    adc = ADC(29)
    time.sleep(0.2)
    reading = sum(adc.read_u16() for _ in range(5)) / 5
    vref.value(0)
    voltage = reading * (3.3 / 65535) * 3
    return int(max(0, min(100, (voltage - 3.2) / (4.1 - 3.2) * 100)))

# ---------------- INDEX -----------------
page_offsets = [0]
page_remainders = {}

def save_index(idx_file):
    try:
        with open(idx_file, "wb") as f:
            f.write(struct.pack("<H", len(page_offsets)))
            for off in page_offsets:
                f.write(struct.pack("<I", off))
            f.write(struct.pack("<H", len(page_remainders)))
            for k, v in page_remainders.items():
                f.write(struct.pack("<H", k))
                f.write(struct.pack("<H", len(v)))
                f.write(v)
    except Exception as e:
        print("save_index failed:", e)

def load_index(idx_file):
    global page_offsets, page_remainders
    try:
        if os.stat(idx_file)[6] > 0:
            with open(idx_file, "rb") as f:
                page_offsets = []
                n_offsets = struct.unpack("<H", f.read(2))[0]
                for _ in range(n_offsets):
                    page_offsets.append(struct.unpack("<I", f.read(4))[0])
                n_rem = struct.unpack("<H", f.read(2))[0]
                page_remainders = {}
                for _ in range(n_rem):
                    k = struct.unpack("<H", f.read(2))[0]
                    l = struct.unpack("<H", f.read(2))[0]
                    page_remainders[k] = f.read(l)
            return True
    except Exception as e:
        print("load_index failed:", e)
    return False

def index_exists(idx_file):
    try:
        return os.stat(idx_file)[6] > 0
    except OSError:
        return False

# ---------------- PAGE RENDERER -----------------
def render_page(start_offset, draw=True, remainder=b""):
    if draw: 
        display.set_pen(15)
        display.clear()
    y = 0
    lines = 0
    next_offset = -1
    try:
        with open(text_file, "rb") as f:
            f.seek(start_offset)
            while lines < LINES_PER_PAGE:
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
                    if draw: y += LINE_HEIGHT
                    lines += 1
                    if lines >= LINES_PER_PAGE: 
                        next_offset = f.tell()
                        break
                    continue
                try:
                    line_str = line.decode("utf-8", "ignore")
                except:
                    line_str = line.decode("latin-1", "ignore")
                words = line_str.replace("…", "...").split(" ")
                current = ""
                byte_idx = 0
                for i, word in enumerate(words):
                    if not word:
                        byte_idx += 1
                        continue
                    appended = current + " " + word if current else word
                    if len(appended) <= MAX_CHARS:
                        current = appended
                        byte_idx += len(word.encode("utf-8")) + (1 if i < len(words)-1 else 0)
                    else:
                        if draw: prnt(current, TEXT_PADDING, y)
                        if draw: y += LINE_HEIGHT
                        lines += 1
                        if lines >= LINES_PER_PAGE:
                            remainder = line_bytes[byte_idx:]
                            next_offset = pos + byte_idx
                            break
                        current = word
                        byte_idx += len(word.encode("utf-8")) + (1 if i < len(words)-1 else 0)
                if next_offset != -1: break
                if current:
                    if draw: prnt(current, TEXT_PADDING, y)
                    if draw: y += LINE_HEIGHT
                    lines += 1
                if lines >= LINES_PER_PAGE:
                    next_offset = f.tell()
                    break
            if next_offset == -1:
                next_offset = f.tell()
    except:
        return start_offset, b""
    if draw:
        percent = battery_percent()
        display.set_font("bitmap8")
        display.text(f"{percent}", 287, 0, WIDTH, 1.0)
        try:
            file_size = os.stat(text_file)[6]
            progress = (start_offset + 1)/file_size
            display.rectangle(0, 126, int(progress*WIDTH), 2)
        except:
            pass
    return next_offset, remainder

def reset_book():
    global page_offsets, page_remainders, state
    try:
        if os.stat(INDEX_FILE)[6] > 0:
            os.remove(INDEX_FILE)
    except:
        pass
    state["current_page"] = 0
    page_offsets = [0]
    page_remainders = {}
    state_save(state)
    remainder = b""
    next_offset, remainder = render_page(page_offsets[0], draw=True, remainder=remainder)
    page_remainders[0] = remainder
    next_page = 1
    if next_page == len(page_offsets):
        next_offset, remainder_next = render_page(page_offsets[0], draw=False, remainder=remainder)
        if next_offset > page_offsets[0]:
            page_offsets.append(next_offset)
            page_remainders[next_page] = remainder_next
            save_index(INDEX_FILE)
    if next_page < len(page_offsets):
        render_page(page_offsets[next_page], draw=True, remainder=page_remainders.get(next_page, b""))
    display.update(); display.update()

# ---------------- FILE PICKER -----------------
LIST_LINE_HEIGHT = LINE_HEIGHT
LIST_START_Y = 10 + 16 + 4
HEADER_TEXT = "choose book :"

def get_text_files(directory):
    try:
        all_files = os.listdir(directory)
        return sorted([f for f in all_files if f.endswith(".txt")])
    except OSError:
        return []

def draw_file_list(files, selected_index):
    display.set_pen(15)
    display.clear()
    prnt(HEADER_TEXT, 0, 0)
    display.line(0, 16, badger2040.WIDTH, 16)
    if not files:
        prnt(f"No .txt files", 5, LIST_START_Y)
        prnt(f"in {BOOK_DIR}", 5, LIST_START_Y+LINE_HEIGHT)
        display.update(); display.update()
        return
    list_area_height = badger2040.HEIGHT - LIST_START_Y
    max_items = list_area_height // LINE_HEIGHT
    start_index = 0
    if selected_index >= max_items:
        start_index = (selected_index - max_items) + 1
    y = LIST_START_Y
    for i in range(start_index, len(files)):
        if i >= start_index + max_items: break
        file = files[i]
        if i == selected_index:
            display.set_pen(0)
            display.rectangle(0, y-1, badger2040.WIDTH, LINE_HEIGHT+2)
            prnt(file, 5, y, pen_color=15)
        else:
            prnt(file, 5, y)
        y += LINE_HEIGHT
    display.update(); display.update()

def file_picker():
    files = get_text_files(BOOK_DIR)
    if not files: return None
    idx = 0
    changed = True
    while True:
        if changed:
            draw_file_list(files, idx)
            changed = False
        if display.pressed(badger2040.BUTTON_UP):
            if idx > 0: idx -= 1; changed = True
        if display.pressed(badger2040.BUTTON_DOWN):
            if idx < len(files) - 1: idx += 1; changed = True
        if display.pressed(badger2040.BUTTON_C):
            return f"{BOOK_DIR}/{files[idx]}"
        time.sleep(0.05)

# ---------------- INIT -----------------
state = state_load()
try:
    text_file = state["last_book"] or None
except:
    text_file = None
if not text_file:
    text_file = "Error: Not Set"
INDEX_FILE = text_file.replace("/", "_").replace(".", "_") + ".idx"

if index_exists(INDEX_FILE):
    load_index(INDEX_FILE)
else:
    page_offsets = [0]
    page_remainders = {}
    save_index(INDEX_FILE)

current = state["current_page"]
remainder = page_remainders.get(current, b"")
next_offset, remainder = render_page(page_offsets[current], draw=True, remainder=remainder)
page_remainders[current] = remainder
next_page = current + 1
if next_page == len(page_offsets):
    next_offset, remainder_next = render_page(page_offsets[current], draw=False, remainder=remainder)
    if next_offset > page_offsets[current]:
        page_offsets.append(next_offset)
        page_remainders[next_page] = remainder_next
        save_index(INDEX_FILE)
if next_page < len(page_offsets):
    render_page(page_offsets[next_page], draw=True, remainder=page_remainders.get(next_page, b""))
display.update(); display.update()

# ---------------- MAIN LOOP -----------------
FAST_ADVANCE_PAGES = 200  # Number of pages to skip on long-press down

while True:
    display.keepalive()

    # NEXT PAGE (short press)
    if display.pressed(badger2040.BUTTON_DOWN):
        press_start = time.ticks_ms()
        while display.pressed(badger2040.BUTTON_DOWN):
            time.sleep(0.05)
        press_duration = time.ticks_diff(time.ticks_ms(), press_start)

        last = time.ticks_ms()
        display.led(50)

        if press_duration > 700:  # long press: fast advance
            print("Fast advancing...")
            target_page = state["current_page"] + FAST_ADVANCE_PAGES
            current = state["current_page"]

            last_remainder = page_remainders.get(current, b"")
            for _ in range(FAST_ADVANCE_PAGES):
                next_page = current + 1

                # If we reached or passed end of offsets, generate next offset
                if next_page >= len(page_offsets):
                    next_offset, rem_next = render_page(page_offsets[current], draw=False, remainder=last_remainder)
                    if next_offset <= page_offsets[current]:
                        break  # end of book
                    page_offsets.append(next_offset)
                    last_remainder = rem_next
                else:
                    # Precompute remainder if not cached
                    _, rem_next = render_page(page_offsets[current], draw=False, remainder=last_remainder)
                    last_remainder = rem_next

                current = next_page
                gc.collect()

                if current >= target_page:
                    break

            # Store remainder for the final page we land on
            page_remainders[current] = last_remainder

            # ✅ Precompute the *next* page offset so "down" works normally
            next_page = current + 1
            if next_page >= len(page_offsets):
                next_offset, rem_next = render_page(page_offsets[current], draw=False, remainder=last_remainder)
                if next_offset > page_offsets[current]:
                    page_offsets.append(next_offset)
                    page_remainders[next_page] = rem_next

            # Now render and display the final visible page
            render_page(page_offsets[current], draw=True, remainder=last_remainder)
            display.update(); display.update()

            # Save updated index and state
            save_index(INDEX_FILE)
            state["current_page"] = current
            state_save(state)
            gc.collect()




        else:
            # short press: normal next page
            current = state["current_page"] + 1
            if current < len(page_offsets):
                state["current_page"] = current
                display.update(); display.update()
                state_save(state)
                next_page = current + 1
                remainder = page_remainders.get(current, b"")
                if next_page == len(page_offsets):
                    next_offset, rem_next = render_page(page_offsets[current], draw=False, remainder=remainder)
                    if next_offset > page_offsets[current]:
                        page_offsets.append(next_offset)
                        page_remainders[next_page] = rem_next
                        save_index(INDEX_FILE)
                if next_page < len(page_offsets):
                    render_page(page_offsets[next_page], draw=True, remainder=page_remainders.get(next_page, b""))
            else:
                display.set_pen(15)
                display.clear()
                prnt("--- END OF BOOK ---", 5, 50)
                prnt(f"Total Pages: {len(page_offsets)}", 5, 70)
                prnt("Press UP to go back.", 5, 90)
                display.update(); display.update()

        display.led(0)

    # PREVIOUS PAGE
    if display.pressed(badger2040.BUTTON_UP):
        last = time.ticks_ms()
        display.led(50)
        current = max(0, state["current_page"] - 1)
        state["current_page"] = current
        remainder = page_remainders.get(current, b"")
        render_page(page_offsets[current], draw=True, remainder=remainder)
        display.update(); display.update()
        next_page = current + 1
        if next_page < len(page_offsets):
            render_page(page_offsets[next_page], draw=True, remainder=page_remainders.get(next_page, b""))
        state_save(state)
        display.led(0)

    # BUTTON_A: choose book
    if display.pressed(badger2040.BUTTON_A):
        new_book = file_picker()
        if not new_book:
            continue

        # If user picked the same book, do NOT reset current_page.
        # Instead ensure the index/remainders are loaded and present for the saved page,
        # then redraw that page and pre-buffer the next page.
        if new_book == text_file:
            # Make sure INDEX_FILE points to this book's index
            INDEX_FILE = text_file.replace("/", "_").replace(".", "_") + ".idx"

            # If an index exists on disk, load it (so page_offsets/page_remainders are filled)
            if index_exists(INDEX_FILE):
                load_index(INDEX_FILE)

            # Ensure current_page is valid (clamp)
            cp = state.get("current_page", 0)
            if cp >= len(page_offsets):
                cp = len(page_offsets) - 1
                if cp < 0:
                    cp = 0
                state["current_page"] = cp

            # Ensure we have remainder info for this page — compute silently if missing
            if cp not in page_remainders:
                # we need remainder for cp; try to compute from previous page if exists
                if cp == 0:
                    page_remainders[0] = b""
                else:
                    prev = cp - 1
                    prev_rem = page_remainders.get(prev, b"")
                    next_off, rem_next = render_page(page_offsets[prev], draw=False, remainder=prev_rem)
                    # If offset advanced, make sure page_offsets covers cp
                    if next_off > page_offsets[prev] and cp >= len(page_offsets):
                        page_offsets.append(next_off)
                    page_remainders[cp] = rem_next

            # Now render the current page visibly and store remainder
            remainder = page_remainders.get(cp, b"")
            next_off_vis, rem_vis = render_page(page_offsets[cp], draw=True, remainder=remainder)
            page_remainders[cp] = rem_vis

            # Pre-buffer next page so a normal DOWN press will work immediately
            next_page = cp + 1
            if next_page >= len(page_offsets):
                prev_rem = page_remainders.get(cp, b"")
                next_off2, rem_next2 = render_page(page_offsets[cp], draw=False, remainder=prev_rem)
                if next_off2 > page_offsets[cp]:
                    page_offsets.append(next_off2)
                    page_remainders[next_page] = rem_next2

            display.update(); display.update()
            # don't change state["current_page"] or reset it

        else:
            # Different book selected: switch to it (reset to page 0)
            text_file = new_book
            state["last_book"] = text_file
            state["current_page"] = 0
            state_save(state)
            INDEX_FILE = text_file.replace("/", "_").replace(".", "_") + ".idx"

            # if an index already exists, load it (resume where left off), otherwise create it
            if index_exists(INDEX_FILE):
                load_index(INDEX_FILE)
            else:
                page_offsets = [0]
                page_remainders = {}
                save_index(INDEX_FILE)

            # Render page 0 and pre-buffer next
            remainder = page_remainders.get(0, b"")
            next_offset, remainder = render_page(page_offsets[0], draw=True, remainder=remainder)
            page_remainders[0] = remainder
            next_page = 1
            if next_page == len(page_offsets):
                next_offset, rem_next = render_page(page_offsets[0], draw=False, remainder=remainder)
                if next_offset > page_offsets[0]:
                    page_offsets.append(next_offset)
                    page_remainders[next_page] = rem_next
                    save_index(INDEX_FILE)
            if next_page < len(page_offsets):
                render_page(page_offsets[next_page], draw=True, remainder=page_remainders.get(next_page, b""))
            display.update(); display.update()


    # BUTTON_B: reset / short press
    if display.pressed(badger2040.BUTTON_B):
        press_start = time.ticks_ms()
        display.update(); display.update()
        while display.pressed(badger2040.BUTTON_B):
            display.keepalive()
            time.sleep(0.05)
            if time.ticks_diff(time.ticks_ms(), press_start) > 1000:
                display.led(50)
                reset_book()
                display.led(0)
                break

    # SLEEP on inactivity
    if time.ticks_diff(time.ticks_ms(), last) > INACTIVITY_TIMEOUT:
        display.led(50)
        display.halt()

    time.sleep(0.05)


