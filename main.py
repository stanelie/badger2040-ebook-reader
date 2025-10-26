import badger2040
import gc
import badger_os
import vga2_8x16
import time
import os
import json
from machine import ADC, Pin

# ---------------- CONFIG -----------------
text_file = "/books/Wayward-Pines-02.txt"
gc.collect()

ENABLE_SLEEP = True
LINES_PER_PAGE = 9
LINE_HEIGHT = vga2_8x16.HEIGHT - 2
TEXT_PADDING = 2
WIDTH = badger2040.WIDTH
TEXT_WIDTH = WIDTH - TEXT_PADDING * 2
MAX_CHARS = TEXT_WIDTH // vga2_8x16.WIDTH

INDEX_FILE = text_file.replace("/", "_").replace(".", "_") + ".idx"

# ---------------- DISPLAY -----------------
display = badger2040.Badger2040()
display.set_update_speed(badger2040.UPDATE_TURBO)
display.led(0)

# ---------------- STATE -----------------
state = {"current_page": 0}
badger_os.state_load("ebook", state)

page_offsets = [0]
page_remainders = {}

# ---------------- FONT -----------------
def character(asci, x, y):
    if asci < vga2_8x16.FIRST or asci > vga2_8x16.LAST:
        asci = ord('?')
    idx = (asci - vga2_8x16.FIRST) * vga2_8x16.HEIGHT
    display.set_pen(0)
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

def prnt(text, x, y):
    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")\
               .replace("\u2014", "-").replace("\u2013", "-")
    for c in text:
        if vga2_8x16.FIRST <= ord(c) <= vga2_8x16.LAST:
            character(ord(c), x, y)
        else:
            character(ord('?'), x, y)
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
def save_index():
    try:
        data = {
            "offsets": page_offsets,
            "remainders": {str(k): v.decode("latin-1") for k, v in page_remainders.items()}
        }
        with open(INDEX_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass

def load_index():
    global page_offsets, page_remainders
    try:
        if os.stat(INDEX_FILE)[6] > 0:
            with open(INDEX_FILE, "r") as f:
                data = json.load(f)
                page_offsets = data.get("offsets", [0])
                page_remainders = {int(k): v.encode("latin-1") for k,v in data.get("remainders", {}).items()}
                return True
    except:
        pass
    return False

def load_or_build():
    global page_offsets
    if not load_index():
        page_offsets = [0]
    if state["current_page"] >= len(page_offsets):
        state["current_page"] = len(page_offsets)-1

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

                # If we have a remainder, use it as the first "line" and
                # advance the file pointer so we don't re-read the same bytes.
                if remainder:
                    line_bytes = remainder
                    # move file pointer past the remainder so subsequent f.readline()
                    # reads from the correct position
                    try:
                        f.seek(start_offset + len(remainder))
                    except:
                        # if seek fails for some reason, leave pointer as-is;
                        # we'll still proceed but avoid duplication in normal cases
                        pass
                    remainder = b""
                else:
                    line_bytes = f.readline()

                if not line_bytes:
                    next_offset = f.tell()
                    break

                line = line_bytes.rstrip(b"\r\n")
                if not line:
                    if draw:
                        y += LINE_HEIGHT
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
                        if draw:
                            prnt(current, TEXT_PADDING, y)
                            y += LINE_HEIGHT
                        lines += 1
                        if lines >= LINES_PER_PAGE:
                            remainder = line_bytes[byte_idx:]
                            next_offset = pos + byte_idx
                            break
                        current = word
                        byte_idx += len(word.encode("utf-8")) + (1 if i < len(words)-1 else 0)
                if next_offset != -1:
                    break
                if current:
                    if draw:
                        prnt(current, TEXT_PADDING, y)
                        y += LINE_HEIGHT
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
    # Delete index file if it exists
    try:
        if os.stat(INDEX_FILE)[6] > 0:
            os.remove(INDEX_FILE)
    except:
        pass
    # Reset in-memory state
    state["current_page"] = 0
    page_offsets = [0]
    page_remainders = {}
    badger_os.state_save("ebook", state)
    # Render first page
    remainder = b""
    next_offset, remainder = render_page(page_offsets[0], draw=True, remainder=remainder)
    page_remainders[0] = remainder
    # Pre-buffer next page
    next_page = 1
    if next_page == len(page_offsets):
        next_offset, remainder_next = render_page(page_offsets[0], draw=False, remainder=remainder)
        if next_offset > page_offsets[0]:
            page_offsets.append(next_offset)
            page_remainders[next_page] = remainder_next
            save_index()
    if next_page < len(page_offsets):
        render_page(page_offsets[next_page], draw=True, remainder=page_remainders.get(next_page, b""))
    display.update()
    display.update()


# ---------------- END MESSAGE -----------------
def show_end():
    display.set_pen(15)
    display.clear()
    display.set_pen(0)
    prnt("--- END OF BOOK ---", TEXT_PADDING, 50)
    prnt(f"Total Pages: {len(page_offsets)}", TEXT_PADDING, 70)
    prnt("Press UP to go back.", TEXT_PADDING, 90)

# ---------------- INIT -----------------
load_or_build()
current = state["current_page"]
remainder = page_remainders.get(current, b"")
next_offset, remainder = render_page(page_offsets[current], draw=True, remainder=remainder)
page_remainders[current] = remainder

# Pre-buffer next
next_page = current+1
if next_page == len(page_offsets):
    next_offset, remainder_next = render_page(page_offsets[current], draw=False, remainder=remainder)
    if next_offset>page_offsets[current]:
        page_offsets.append(next_offset)
        page_remainders[next_page]=remainder_next
        save_index()
if next_page < len(page_offsets):
    render_page(page_offsets[next_page], draw=True, remainder=page_remainders.get(next_page,b""))

# ---------------- MAIN LOOP -----------------
INACTIVITY_TIMEOUT=20*1000
last=time.ticks_ms()
while True:
    display.keepalive()
    
    # NEXT PAGE
    if display.pressed(badger2040.BUTTON_DOWN):
        last=time.ticks_ms()
        display.led(50)
        current = state["current_page"]+1
        if current < len(page_offsets):
            state["current_page"]=current
            display.update(); display.update()
            badger_os.state_save("ebook", state)
            # Discover next
            next_page = current+1
            remainder=page_remainders.get(current,b"")
            if next_page==len(page_offsets):
                next_offset, rem_next = render_page(page_offsets[current], draw=False, remainder=remainder)
                if next_offset>page_offsets[current]:
                    page_offsets.append(next_offset)
                    page_remainders[next_page]=rem_next
                    save_index()
            if next_page<len(page_offsets):
                render_page(page_offsets[next_page], draw=True, remainder=page_remainders.get(next_page,b""))
        else:
            show_end(); display.update(); display.update()
        display.led(0)

    # PREVIOUS PAGE
    if display.pressed(badger2040.BUTTON_UP):
        last=time.ticks_ms()
        display.led(50)
        current=max(0,state["current_page"]-1)
        state["current_page"]=current
        remainder=page_remainders.get(current,b"")
        render_page(page_offsets[current], draw=True, remainder=remainder)
        display.update(); display.update()
        next_page=current+1
        if next_page<len(page_offsets):
            render_page(page_offsets[next_page], draw=True, remainder=page_remainders.get(next_page,b""))
        badger_os.state_save("ebook", state)
        display.led(0)

    # BUTTON A: short = refresh, long = full reset
    if display.pressed(badger2040.BUTTON_A):
        press_start = time.ticks_ms()
        # Wait while button is held
        while display.pressed(badger2040.BUTTON_A):
            display.keepalive()
            time.sleep(0.05)
            if time.ticks_diff(time.ticks_ms(), press_start) > 1000:
                # Long press detected
                display.led(50)
                reset_book()
                display.led(0)
                break
        else:
            # Short press: refresh current page
            last = time.ticks_ms()
            display.led(20)
            current = state["current_page"]
            remainder = page_remainders.get(current, b"")
            render_page(page_offsets[current], draw=True, remainder=remainder)
            display.update()
            display.update()
            display.led(0)

    # SLEEP
    if ENABLE_SLEEP and time.ticks_diff(time.ticks_ms(), last)>INACTIVITY_TIMEOUT:
        display.led(50)
        display.halt()

