# ------------------------------------------------------------
# coverimg.py  -  turn a book's cover into a sleep screen
# ------------------------------------------------------------
# Writes "<book>.sleep.bin": one 4736-byte frame, exactly the reader's
# framebuffer layout, that enter_sleep() can read straight in and push to the
# panel. The reader never decodes anything.
#
# That split is the point. Decoding a JPEG needs a 16-bit bitmap of the scaled
# image held whole - tens of KB in one piece - and the reader is the program
# with the least room and the most fragmented heap. Here the work happens once,
# in the converter or by hand, and every sleep afterwards costs one file read.
#
# Run by hand for books converted before this existed - their .epub may well be
# gone, but the .cover.jpeg beside the .txt is all this needs:
#
#     import coverimg
#     coverimg.main()
#
# Needs `jpegio`, which not every CircuitPython build has. Without it, or if the
# cover is too large to decode in the memory available, nothing is written and
# the reader simply leaves the last page on screen when it sleeps.
import os

WIDTH = 296
HEIGHT = 128
FRAME = WIDTH * HEIGHT // 8

TARGET_DIR = "books"

# Ordered 4x4 dithering. On a panel with two levels, thresholding flat tone
# makes a cover a silhouette; a dither matrix trades spatial detail for the
# shading that is actually there. Cheap, and unlike error diffusion it needs no
# second row buffer.
_BAYER = (0, 8, 2, 10,
          12, 4, 14, 6,
          3, 11, 1, 9,
          15, 7, 13, 5)


def sleep_path_for(txt_path):
    """Where the sleep frame for this book lives."""
    base = txt_path[:-4] if txt_path.endswith(".txt") else txt_path
    return base + ".sleep.bin"


def find_cover(txt_path):
    """The cover saved next to this book, or None."""
    base = txt_path[:-4] if txt_path.endswith(".txt") else txt_path
    for ext in ("jpeg", "jpg", "png"):
        path = "%s.cover.%s" % (base, ext)
        try:
            os.stat(path)
            return path
        except OSError:
            pass
    return None


def pack(get_pixel, src_w, src_h, out=None, width=WIDTH, height=HEIGHT):
    """Fit an RGB565 source into one 1-bit MHMSB frame, dithered.

    `get_pixel(x, y)` returns RGB565. Kept free of jpegio so the scaling,
    luminance and dithering can be exercised without a decoder or a device.

    The cover is portrait and the panel is landscape, so it is fitted by
    whichever axis runs out first and centred; the margins stay white.
    """
    if out is None:
        out = bytearray(width * height // 8)
    else:
        for i in range(len(out)):
            out[i] = 0
    if src_w <= 0 or src_h <= 0:
        return out

    # Largest whole-image fit, in 1/256ths to stay in integers.
    scale = min((width << 8) // src_w, (height << 8) // src_h)
    if scale < 1:
        scale = 1
    draw_w = (src_w * scale) >> 8
    draw_h = (src_h * scale) >> 8
    if draw_w < 1:
        draw_w = 1
    if draw_h < 1:
        draw_h = 1
    x0 = (width - draw_w) // 2
    y0 = (height - draw_h) // 2
    row_bytes = width >> 3

    for dy in range(draw_h):
        sy = (dy * src_h) // draw_h
        oy = y0 + dy
        if oy < 0 or oy >= height:
            continue
        rowbase = oy * row_bytes
        bayer_row = (oy & 3) << 2
        for dx in range(draw_w):
            ox = x0 + dx
            if ox < 0 or ox >= width:
                continue
            v = get_pixel((dx * src_w) // draw_w, sy)
            # RGB565 -> luma. Weights are the usual 77/151/28 over 256, with
            # each channel first stretched back to 0-255.
            lum = ((((v >> 11) & 0x1F) * 8 * 77
                    + ((v >> 5) & 0x3F) * 4 * 151
                    + (v & 0x1F) * 8 * 28) >> 8)
            if lum < (_BAYER[bayer_row + (ox & 3)] << 4) + 8:
                out[rowbase + (ox >> 3)] |= 0x80 >> (ox & 7)
    return out


def _sleep_message(reader):
    """Say so on the panel when there is no cover to show.

    Leaving the last page up was the first idea, and it reads as a board that
    has not gone to sleep yet - there is no way to tell the two apart by
    looking, which is the one thing this screen is for.
    """
    try:
        reader.show_message(("Sleeping...", 110, 30),
                            ("press any key to wake", 60, 90))
    except Exception as e:
        print("[COVER] could not draw the sleep message: %s" % e)
    return False


def show_sleep_screen():
    """Draw the sleep screen: the book's cover, or a message saying it slept.

    True if a cover was shown. The cover frame was prepared by this module -
    already 1-bit, already the right size, already in the framebuffer's own
    layout - so this allocates nothing and decodes nothing at sleep time.

    Reaches the reader through __main__ the way convert_ui does, rather than
    being handed its buffers: it needs several of them and it is only ever
    called from there.
    """
    try:
        import __main__ as reader
    except ImportError:
        import sys
        reader = sys.modules.get("__main__")
    if reader is None:
        return False
    book = getattr(reader, "text_file", "")
    buf = getattr(reader, "raw_working_buffer", None)
    if not book or buf is None:
        return _sleep_message(reader)
    try:
        with open(sleep_path_for(book), "rb") as f:
            got = f.readinto(buf)
    except Exception:
        return _sleep_message(reader)      # no cover for this book
    if got != len(buf):
        print("[COVER] sleep frame is %d bytes, expected %d - ignoring"
              % (got, len(buf)))
        return _sleep_message(reader)
    display = reader.display
    # A full flicker-free refresh: this image stays on the panel with the power
    # off, possibly for weeks, so drive every pixel properly rather than leave
    # a quick update's ghosting on it.
    display.set_speed(0, no_flickering=False)
    reader.update_display_fast(display._rotate_framebuffer(buf))
    display.set_speed(reader.ORIGINAL_SPEED, reader.ORIGINAL_NO_FLICKERING)
    return True


def render(cover_path, out_path, width=WIDTH, height=HEIGHT, budget=40000):
    """Decode `cover_path` and write a sleep frame. True if one was written.

    `budget` caps the decoded bitmap, which has to exist whole and in one
    piece: two bytes a pixel, so 40000 is about 20000 pixels. jpegio can only
    scale by halves, so a cover far above that simply cannot be decoded here -
    it says so and writes nothing rather than raising.
    """
    try:
        import jpegio
        import displayio
    except ImportError as e:
        print("[COVER] no jpegio in this build (%s)" % e)
        return False

    try:
        decoder = jpegio.JpegDecoder()
        src_w, src_h = decoder.open(cover_path)
    except Exception as e:
        print("[COVER] cannot open %s: %s" % (cover_path, e))
        return False

    # Prefer the smallest decode that still has more detail than the panel.
    chosen = None
    for s in (3, 2, 1, 0):
        sw, sh = src_w >> s, src_h >> s
        if sw < 1 or sh < 1:
            continue
        if sw * sh * 2 > budget:
            continue
        chosen = (s, sw, sh)
        if sw >= width or sh >= height:
            break
    if chosen is None:
        print("[COVER] %dx%d will not decode within %d bytes"
              % (src_w, src_h, budget))
        return False

    scale, sw, sh = chosen
    try:
        bitmap = displayio.Bitmap(sw, sh, 65535)
        decoder.decode(bitmap, scale=scale)
    except Exception as e:
        print("[COVER] decode failed at 1/%d (%dx%d): %s"
              % (1 << scale, sw, sh, e))
        return False

    frame = pack(lambda x, y: bitmap[x, y], sw, sh, width=width, height=height)
    bitmap = None

    try:
        with open(out_path, "wb") as f:
            f.write(frame)
    except Exception as e:
        print("[COVER] cannot write %s: %s" % (out_path, e))
        return False
    print("[COVER] %s -> %s (decoded 1/%d, %dx%d)"
          % (cover_path, out_path, 1 << scale, sw, sh))
    return True


def render_for_book(txt_path, force=False):
    """Make the sleep frame for one book, if it has a cover and needs one."""
    out = sleep_path_for(txt_path)
    if not force:
        try:
            os.stat(out)
            return False              # already has one
        except OSError:
            pass
    cover = find_cover(txt_path)
    if not cover:
        return False
    return render(cover, out)


def main(force=False):
    """Render sleep frames for every converted book that has a cover."""
    made = 0
    # Writing needs the filesystem, which the USB host holds by default. Hold A
    # while resetting and boot.py hands it over - that keeps the serial console,
    # so this can still be run from the REPL, unlike a conversion on battery.
    probe = "/.covertest"
    try:
        with open(probe, "wb") as f:
            f.write(b"x")
        os.remove(probe)
    except Exception:
        print("[COVER] filesystem is read-only - hold A while resetting so")
        print("[COVER] boot.py hands it to the board, then try again.")
        return 0
    try:
        names = os.listdir("/" + TARGET_DIR)
    except OSError as e:
        print("[COVER] cannot read /%s: %s" % (TARGET_DIR, e))
        return 0
    for name in names:
        if not name.endswith(".txt"):
            continue
        if render_for_book("/%s/%s" % (TARGET_DIR, name), force=force):
            made += 1
    print("[COVER] %d sleep screen(s) written" % made)
    return made
