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

# Turn the cover a quarter-turn counter-clockwise before fitting it.
# A cover is portrait and the panel is landscape, so upright it fits by height
# and uses barely a third of the width - an 800x1104 cover lands at 93x125 in a
# 296x128 screen. Turned, its long side runs along the panel's long side and it
# lands at 176x128, close to twice the area. The reader is held sideways to
# look at it, which for a sleeping device is no hardship.
ROTATE_COVER = True

# Scale until the cover covers the whole panel and crop what hangs off, rather
# than fitting it whole inside white margins. Fitted whole and turned it still
# reached only 56% of the screen. Centred, so it is the outside edges that go.
FILL_SCREEN = True

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


def pack(get_pixel, src_w, src_h, out=None, width=WIDTH, height=HEIGHT,
         rotate=False, fill=None):
    """Fit an RGB565 source into one 1-bit MHMSB frame, dithered.

    `get_pixel(x, y)` returns RGB565. Kept free of jpegio so the scaling,
    rotation, luminance and dithering can be exercised without a decoder or a
    device.

    `fill` scales until the panel is covered and crops the overflow, centred;
    without it the whole image fits inside white margins instead. `rotate`
    turns the source a quarter-turn counter-clockwise first, which is how a
    portrait cover gets to use the length of a landscape panel.
    """
    if fill is None:
        fill = FILL_SCREEN
    if rotate:
        # A quarter-turn counter-clockwise: the source's right-hand column
        # becomes the top row, so reading across the turned image reads down
        # the original. Sampled on the way past rather than copied - there is
        # no room here for a second image.
        original = get_pixel
        last_x = src_w - 1
        get_pixel = lambda x, y, _g=original, _l=last_x: _g(_l - y, x)
        src_w, src_h = src_h, src_w
    if out is None:
        out = bytearray(width * height // 8)
    else:
        for i in range(len(out)):
            out[i] = 0
    if src_w <= 0 or src_h <= 0:
        return out

    # In 1/256ths to stay in integers. Filling takes the larger ratio, so the
    # short side reaches the edge and the long one runs past it; fitting takes
    # the smaller, so the long side stops at the edge and margins remain.
    if fill:
        # Rounded UP, both here and after scaling. Rounding down leaves the
        # panel a few columns short of covered - 98% of it, with a white strip
        # down one edge, which is the one thing filling is supposed to avoid.
        scale = max(((width << 8) + src_w - 1) // src_w,
                    ((height << 8) + src_h - 1) // src_h)
    else:
        scale = min((width << 8) // src_w, (height << 8) // src_h)
    if scale < 1:
        scale = 1
    draw_w = (src_w * scale) >> 8
    draw_h = (src_h * scale) >> 8
    if fill:
        # A pixel or two, when the shift above truncates. Cheaper than carrying
        # more precision, and the aspect error is well under a percent.
        if draw_w < width:
            draw_w = width
        if draw_h < height:
            draw_h = height
    if draw_w < 1:
        draw_w = 1
    if draw_h < 1:
        draw_h = 1
    # Negative when filling: that is the crop, half of it off each side.
    x0 = (width - draw_w) // 2
    y0 = (height - draw_h) // 2
    row_bytes = width >> 3

    # Walked over the OUTPUT, not over the scaled image. Filling makes the
    # scaled image bigger than the panel, and iterating that would spend most
    # of its time on pixels that are then thrown away.
    for oy in range(height):
        dy = oy - y0
        if dy < 0 or dy >= draw_h:
            continue
        sy = (dy * src_h) // draw_h
        rowbase = oy * row_bytes
        bayer_row = (oy & 3) << 2
        for ox in range(width):
            dx = ox - x0
            if dx < 0 or dx >= draw_w:
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


def render(cover_path, out_path, width=WIDTH, height=HEIGHT, budget=40000,
           rotate=None):
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
    # Turned, it is the cover's height that has to cover the panel's width.
    need_w, need_h = (height, width) if (
        ROTATE_COVER if rotate is None else rotate) else (width, height)
    chosen = None
    for s in (3, 2, 1, 0):
        sw, sh = src_w >> s, src_h >> s
        if sw < 1 or sh < 1:
            continue
        if sw * sh * 2 > budget:
            continue
        chosen = (s, sw, sh)
        # Filling scales up until both axes are covered, so both need the
        # resolution; fitting only stretches until the first one reaches.
        if (sw >= need_w and sh >= need_h) if FILL_SCREEN else (
                sw >= need_w or sh >= need_h):
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

    if rotate is None:
        rotate = ROTATE_COVER
    frame = pack(lambda x, y: bitmap[x, y], sw, sh, width=width, height=height,
                 rotate=rotate, fill=FILL_SCREEN)
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
