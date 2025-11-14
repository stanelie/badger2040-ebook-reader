#############################################
# main.py  — updated with EPUB support
#############################################

import badger2040
import gc
import time
import os
import struct
import vga2_8x16
from machine import ADC, Pin

# --- NEW IMPORTS ---
import epub_xtract                      # ← ADD THIS
from epub_xtract import run_extraction  # ← ADD THIS
#############################################

STATE_FILE = "/state/ebook_state.bin"

# ---------------- STATE -----------------
def state_save(state):
    try:
        with open(STATE_FILE, "wb") as f:
            file_path = state.get("last_book", "")
            data = struct.pack("<I", state.get("current_page", 0))
            f.write(data)
            f.write(struct.pack("<H", len(file_path)))
            f.write(file_path.encode("utf-8"))
    except Exception as e:
        print("Error saving state:", e)

def state_load():
    state = {"current_page": 0, "last_book": ""}
    try:
        if os.stat(STATE_FILE)[6] > 0:
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
WIDTH = badger2040.WIDTH
TEXT_WIDTH = WIDTH - TEXT_PADDING*2
MAX_CHARS = TEXT_WIDTH // vga2_8x16.WIDTH
INACTIVITY_TIMEOUT = 60*1000
BOOK_DIR = "/books"
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

# ---- NEW: limit how many remainders we keep ----
MAX_REMAINDERS = 9

def prune_remainders(keep_page):
    half = MAX_REMAINDERS // 2
    keep_low = max(0, keep_page - half)
    keep_high = keep_page + half
    to_delete = [k for k in page_remainders.keys() if k < keep_low or k > keep_high]
    for k in to_delete:
        try:
            del page_remainders[k]
        except KeyError:
            pass

def save_index(idx_file):
    try:
        with open(idx_file, "wb") as f:
            f.write(struct.pack("<H", len(page_offsets)))
            for off in page_offsets:
                f.write(struct.pack("<I", off))
            f.write(struct.pack("<H", 0))
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
            display.rectangle(0, 127, int(progress*WIDTH), 1)
        except:
            pass
    return next_offset, remainder

# ---------------- FILE PICKER -----------------
LIST_LINE_HEIGHT = LINE_HEIGHT
LIST_START_Y = 10 + 16 + 4
HEADER_TEXT = "choose book :"

# ---- UPDATED: show .txt *and* .epub ----
def get_text_files(directory):
    try:
        all_files = os.listdir(directory)
        return sorted([f for f in all_files if f.endswith(".txt") or f.endswith(".epub")])
    except OSError:
        return []

def draw_file_list(files, selected_index):
    display.set_pen(15)
    display.clear()
    prnt(HEADER_TEXT, 0, 0)
    display.line(0, 16, badger2040.WIDTH, 16)
    stat = os.statvfs('/')
    free_bytes = stat[1] * stat[3] 
    total_bytes = stat[1] * stat[2]
    display.set_font("bitmap8")
    display.text(f"Free space : {free_bytes / 1024 / 1024:.2f}/{total_bytes / 1024 / 1024:.2f} MB", 180, 120, WIDTH, 1.0)
    if not files:
        prnt(f"No books found", 5, LIST_START_Y)
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
        if display.pressed(badger2040.BUTTON_A):
            return f"{BOOK_DIR}/{files[idx]}"
        time.sleep(0.05)

# ---------------- INIT -----------------
state = state_load()
text_file = state.get("last_book") or None
if not text_file:
    text_file = "Error: Not Set"
INDEX_FILE = "/state/" + text_file.replace("/", "_").replace(".", "_") + ".idx"

if index_exists(INDEX_FILE):
    load_index(INDEX_FILE)
else:
    page_offsets = [0]
    page_remainders = {}
    save_index(INDEX_FILE)

current = state.get("current_page", 0)
current = min(current, len(page_offsets)-1)
remainder = page_remainders.get(current, b"")
next_offset, remainder = render_page(page_offsets[current], draw=True, remainder=remainder)
page_remainders[current] = remainder
prune_remainders(current)

next_page = current + 1
if next_page == len(page_offsets):
    next_offset, rem_next = render_page(page_offsets[current], draw=False, remainder=remainder)
    if next_offset > page_offsets[current]:
        page_offsets.append(next_offset)
        page_remainders[next_page] = rem_next
        prune_remainders(current)
        save_index(INDEX_FILE)
if next_page < len(page_offsets):
    render_page(page_offsets[next_page], draw=True, remainder=page_remainders.get(next_page, b""))

# ---------------- MAIN LOOP -----------------
FAST_ADVANCE_PAGES = 50

while True:
    display.keepalive()

    # NEXT PAGE
    if display.pressed(badger2040.BUTTON_DOWN) or display.pressed(badger2040.BUTTON_C):
        press_start = time.ticks_ms()
        while display.pressed(badger2040.BUTTON_DOWN):
            time.sleep(0.05)
        press_duration = time.ticks_diff(time.ticks_ms(), press_start)
        last = time.ticks_ms()
        display.led(50)

        if press_duration > 700:
            target_page = state["current_page"] + FAST_ADVANCE_PAGES
            current = state["current_page"]
            last_remainder = page_remainders.get(current, b"")
            for _ in range(FAST_ADVANCE_PAGES):
                next_page = current + 1
                if next_page >= len(page_offsets):
                    next_offset, rem_next = render_page(page_offsets[current], draw=False, remainder=last_remainder)
                    if next_offset <= page_offsets[current]:
                        break
                    page_offsets.append(next_offset)
                    last_remainder = rem_next
                else:
                    _, rem_next = render_page(page_offsets[current], draw=False, remainder=last_remainder)
                    last_remainder = rem_next
                current = next_page
                gc.collect()
                prune_remainders(current)
                if current >= target_page:
                    break

            current = min(current, len(page_offsets)-1)
            page_remainders[current] = last_remainder
            prune_remainders(current)
            next_page = current + 1
            if next_page >= len(page_offsets):
                next_offset, rem_next = render_page(page_offsets[current], draw=False, remainder=last_remainder)
                if next_offset > page_offsets[current]:
                    page_offsets.append(next_offset)
                    page_remainders[next_page] = rem_next
                    prune_remainders(current)
            render_page(page_offsets[current], draw=True, remainder=last_remainder)
            display.update(); display.update()
            state["current_page"] = current
            gc.collect()

        else:
            display.update(); display.update()
            current = state["current_page"] + 1
            if current >= len(page_offsets):
                current = len(page_offsets)-1
            state["current_page"] = current
            remainder = page_remainders.get(current, b"")
            next_page = current + 1
            if next_page == len(page_offsets):
                next_offset, rem_next = render_page(page_offsets[current], draw=False, remainder=remainder)
                gc.collect()
                if next_offset > page_offsets[current]:
                    page_offsets.append(next_offset)
                    page_remainders[next_page] = rem_next
            if next_page < len(page_offsets):
                render_page(page_offsets[next_page], draw=True, remainder=page_remainders.get(next_page, b""))
            prune_remainders(current)

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
        prune_remainders(current)
        state_save(state)
        display.led(0)

    # ---------------- BUTTON_A = FILE PICKER -----------------
    if display.pressed(badger2040.BUTTON_A):

        save_index(INDEX_FILE)
        state_save(state)

        new_book = file_picker()
        if not new_book:
            continue

        # normalize
        def norm_path(p):
            if not p:
                return ""
            p = p.strip()
            if p.startswith("./"):
                p = p[2:]
            if p.startswith("/"):
                p = p[1:]
            return p.lower()

        nb = norm_path(new_book)
        tf = norm_path(text_file)
        same_book = nb == tf

        # ---- EPUB HANDLING ----
        if new_book.lower().endswith(".epub"):
            display.set_pen(15)
            display.clear()
            prnt("Extracting EPUB...", 10, 50)
            display.update(); display.update()

            # Turn on LED to indicate extraction is in progress
            display.led(50)
            
            # Pass the full path directly to the extractor
            ok = run_extraction(new_book)
            
            # Turn off LED when extraction is complete
            display.led(0)

            if not ok:
                display.set_pen(15)
                display.clear()
                prnt("Extraction failed!", 10, 50)
                display.update(); display.update()
                time.sleep(2)
                continue

            # The extracted .txt file will be in /books/
            txt_name = new_book[:-5] + ".txt"
            new_book = txt_name

        INDEX_FILE = "/state/" + new_book.replace("/", "_").replace(".", "_") + ".idx"

        if same_book:
            state = state_load()
            current = min(state.get("current_page", 0), len(page_offsets)-1)
            remainder = page_remainders.get(current, b"")
            render_page(page_offsets[current], draw=True, remainder=remainder)
            display.update()
            continue

        text_file = new_book
        state["last_book"] = text_file
        state_save(state)

        if index_exists(INDEX_FILE):
            load_index(INDEX_FILE)
            state = state_load()
            current = min(state.get("current_page", 0), len(page_offsets)-1)
            remainder = page_remainders.get(current, b"")
            render_page(page_offsets[current], draw=True, remainder=remainder)
            display.update()
        else:
            page_offsets = [0]
            page_remainders = {}
            state["current_page"] = 0
            state_save(state)

            remainder = b""
            next_offset, remainder = render_page(page_offsets[0], draw=True, remainder=remainder)
            page_remainders[0] = remainder
            prune_remainders(0)
            next_offset2, rem_next2 = render_page(page_offsets[0], draw=False, remainder=remainder)
            if next_offset2 > page_offsets[0]:
                page_offsets.append(next_offset2)
                page_remainders[1] = rem_next2
                prune_remainders(0)
                save_index(INDEX_FILE)
            display.update(); display.update()

    # BUTTON_B short press
    if display.pressed(badger2040.BUTTON_B):
        press_start = time.ticks_ms()
        display.update(); display.update()
        while display.pressed(badger2040.BUTTON_B):
            display.keepalive()
            time.sleep(0.05)
            if time.ticks_diff(time.ticks_ms(), press_start) > 1000:
                display.led(50)
                display.led(0)
                break

    # SLEEP
    if time.ticks_diff(time.ticks_ms(), last) > INACTIVITY_TIMEOUT:
        display.led(50)
        save_index(INDEX_FILE)
        state_save(state)
        display.halt()

    time.sleep(0.05)
