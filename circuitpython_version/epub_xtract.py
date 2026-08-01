# ------------------------------------------------------------
# epub_xtract.py  -  EPUB -> plain text converter (CircuitPython port)
# ------------------------------------------------------------
# Converts an EPUB in /books into a .txt the reader can open, and saves the
# cover image next to it.
#
# Run it from the REPL - this works with Thonny, which holds the serial
# connection and interrupts the board back to the prompt, so tricks like
# supervisor.set_next_code_file() never get to run:
#
#     import epub_xtract
#     epub_xtract.main()
#
# main() frees the reader's memory first. Interrupting code.py to reach the
# REPL does NOT release its globals - the page buffers, the 31KB of
# hyphenation patterns, the font, about 60KB in total - and without that space
# a chapter cannot be decompressed (CircuitPython has no streaming inflater, so
# each one is inflated whole). Reset afterwards and the reader rebuilds it all.
#
# Writing needs the filesystem, which the USB host normally owns. On battery
# the converter takes it over itself; while plugged in, hold A while resetting
# so boot.py hands it over first.
#
# Output for "/books/Sway.epub":
#   /books/Sway.txt         the text, blank line between paragraphs
#   /books/Sway.cover.jpg   the cover, if the EPUB declares one
import os
import time

from uzipfile import UZipFile

# --- Configuration ------------------------------------------------
TARGET_DIR = "books"
# Print memory and member sizes as it goes. The interesting number is not how
# much is free but how big the largest single block is: zlib.decompress has to
# return one contiguous object, so a chapter fails when the largest block is
# smaller than it needs, even with plenty free overall.
VERBOSE = True
MAX_STATUS_LINES = 6
STATUS_HISTORY = []


def log_status(msg):
    """Append to history and print to the REPL."""
    global STATUS_HISTORY
    STATUS_HISTORY.append(msg)
    if len(STATUS_HISTORY) > MAX_STATUS_LINES:
        STATUS_HISTORY = STATUS_HISTORY[-MAX_STATUS_LINES:]
    print("[EXTRACTOR] %s" % msg)


# -----------------------------------------------------------------
def largest_block(limit=200000):
    """Biggest bytearray that can be allocated right now.

    CircuitPython's collector does not move objects, so free memory gets split
    into pieces. What matters for a big allocation is the largest single piece,
    which mem_free() does not tell you - hence probing for it.
    """
    try:
        import gc
        gc.collect()
    except Exception:
        pass
    best = 0
    size = 1024
    while size <= limit:                       # grow until it fails
        try:
            b = bytearray(size)
            del b
            best = size
            size *= 2
        except MemoryError:
            break
    lo, hi = best, min(best * 2, limit)        # then narrow down
    while lo + 2048 < hi:
        mid = (lo + hi) // 2
        try:
            b = bytearray(mid)
            del b
            lo = mid
        except MemoryError:
            hi = mid
    try:
        import gc
        gc.collect()
    except Exception:
        pass
    return lo


def mem_note(label):
    """Log free memory and the largest allocatable block."""
    if not VERBOSE:
        return
    try:
        import gc
        gc.collect()
        log_status("%s: %d free, largest block %d"
                   % (label, gc.mem_free(), largest_block()))
    except Exception:
        pass


# -----------------------------------------------------------------
def free_reader_memory():
    """Release whatever the reader is still holding.

    Running this from the REPL leaves code.py's globals alive - the three page
    buffers, the 31KB of hyphenation patterns, the font, the driver's rotation
    scratch. None of it is needed here, and roughly 60KB is the difference
    between chapters converting and failing to allocate.

    Nothing is lost: reset afterwards and the reader rebuilds all of it.
    """
    try:
        import gc
    except ImportError:
        gc = None
    before = None
    if gc is not None:
        try:
            before = gc.mem_free()      # CircuitPython only; absent on desktop
        except Exception:
            pass

    dropped = 0

    # The hyphenation pattern blob is the single biggest item.
    try:
        import hyphenator
        if getattr(hyphenator, "_BLOB", None) is not None:
            hyphenator._BLOB = None
            dropped += 1
    except Exception:
        pass

    # The reader's own buffers, if code.py has run.
    try:
        try:
            import __main__ as reader
        except ImportError:
            import sys
            reader = sys.modules.get("__main__")
        if reader is None:
            raise ImportError("no __main__")
        for nm in ("raw_working_buffer", "current_rotated_buffer",
                   "next_rotated_buffer", "prev_rotated_buffer",
                   "_scratch_fb", "FONT"):
            if getattr(reader, nm, None) is not None:
                setattr(reader, nm, None)
                dropped += 1
        disp = getattr(reader, "display", None)
        if disp is not None:
            for nm in ("_rotate_scratch", "_partial_scratch"):
                if getattr(disp, nm, None) is not None:
                    setattr(disp, nm, None)
                    dropped += 1
    except Exception:
        pass

    if gc is not None:
        gc.collect()                    # always collect, even without mem_free
        if before is not None:
            try:
                after = gc.mem_free()
                log_status("Freed %d bytes from the reader, %d now free"
                           % (after - before, after))
            except Exception:
                pass
    return dropped


# -----------------------------------------------------------------
def _writable():
    """Can CircuitPython actually write to the filesystem right now?"""
    probe = "/.epubtest"
    try:
        with open(probe, "wb") as f:
            f.write(b"x")
        os.remove(probe)
        return True
    except Exception:
        return False


def ensure_writable():
    """Get write access to the filesystem, if it can be had.

    The reader itself never writes files - it keeps its state in NVRAM - so the
    board normally leaves the filesystem to the USB host, which is why this is
    only ever a converter problem.

    storage.remount() can hand it over at runtime, but only while the host does
    not have write access. So on battery, or with the drive ejected, this just
    works. Plugged in, the host holds it and boot.py has to do it instead
    (hold A while resetting).
    """
    if _writable():
        return True

    try:
        import storage
        storage.remount("/", readonly=False)
    except Exception as e:
        log_status("remount failed: %s" % e)

    if _writable():
        log_status("Filesystem remounted read-write")
        return True

    log_status("Filesystem is read-only - cannot write the converted book.")
    log_status("Either unplug USB and run on battery, or hold A while")
    log_status("resetting so boot.py hands the filesystem to the board.")
    return False


# -----------------------------------------------------------------
def find_epub_file():
    """First .epub in /books, else in the root."""
    for folder in ("/" + TARGET_DIR, "/"):
        try:
            for f in os.listdir(folder):
                if f.lower().endswith(".epub"):
                    path = folder.rstrip("/") + "/" + f
                    log_status("Found: %s" % path)
                    return path
        except Exception as e:
            log_status("FS error on %s: %s" % (folder, e))
    log_status("No .epub found.")
    return None


# -----------------------------------------------------------------
def _is_numbered_html(member):
    """(True, n) for a Calibre-style ..._split_NNN.html, else (False, -1)."""
    if not member.lower().endswith((".html", ".htm", ".xhtml")):
        return False, -1
    basename = member.split("/")[-1]
    if "_split_" not in basename:
        return False, -1
    try:
        num_str = basename.split("_split_")[1].split(".")[0]
        return True, int(num_str)
    except (IndexError, ValueError):
        return False, -1


# ---------------- cover discovery --------------------------------
def _attr(text, name):
    """Value of attribute `name` in a tag fragment, or None.

    Works on bytes. Decoding the OPF to str would hold a second full-size copy
    of it, and transient allocations that big are what leave the heap in
    pieces.
    """
    for quote in (b'"', b"'"):
        key = name + b"=" + quote
        i = text.find(key)
        if i >= 0:
            i += len(key)
            j = text.find(quote, i)
            if j > i:
                return text[i:j]
    return None


def _tags(xml, tag):
    """Each <tag ...> occurrence, as bytes."""
    out = []
    needle = b"<" + tag
    i = 0
    while True:
        i = xml.find(needle, i)
        if i < 0:
            break
        j = xml.find(b">", i)
        if j < 0:
            break
        out.append(xml[i:j])
        i = j
    return out


def _resolve(base_member, href):
    """Resolve an href that is relative to the file it appeared in (str)."""
    if href.startswith("/"):
        return href.lstrip("/")
    base = base_member.rsplit("/", 1)[0] if "/" in base_member else ""
    while href.startswith("../"):
        href = href[3:]
        base = base.rsplit("/", 1)[0] if "/" in base else ""
    return (base + "/" + href) if base else href


def find_cover_member(uzf):
    """Path of the cover image inside the EPUB, or None.

    Tries what the EPUB actually declares first - the OPF names the cover
    either through <meta name="cover" content="ID"> (EPUB 2) or an item with
    properties="cover-image" (EPUB 3) - and only then guesses by filename.
    """
    names = uzf.namelist()

    opf_name = None
    try:
        container = uzf.read("META-INF/container.xml")
        for tag in _tags(container, b"rootfile"):
            path = _attr(tag, b"full-path")
            if path:
                opf_name = path.decode("utf-8", "ignore")
                break
    except Exception:
        pass
    if opf_name is None:
        for n in names:
            if n.lower().endswith(".opf"):
                opf_name = n
                break

    if opf_name:
        try:
            opf = uzf.read(opf_name)          # bytes; never decoded whole
            items = _tags(opf, b"item")

            # EPUB 3: an item flagged as the cover image
            for tag in items:
                props = _attr(tag, b"properties") or b""
                if b"cover-image" in props:
                    href = _attr(tag, b"href")
                    if href:
                        return _resolve(opf_name, href.decode("utf-8", "ignore"))

            # EPUB 2: <meta name="cover" content="some-id">
            cover_id = None
            for tag in _tags(opf, b"meta"):
                if (_attr(tag, b"name") or b"").lower() == b"cover":
                    cover_id = _attr(tag, b"content")
                    if cover_id:
                        break
            if cover_id:
                for tag in items:
                    if _attr(tag, b"id") == cover_id:
                        href = _attr(tag, b"href")
                        if href:
                            return _resolve(opf_name, href.decode("utf-8", "ignore"))
        except Exception as e:
            log_status("OPF parse failed: %s" % e)

    # Fall back to an obvious filename
    for want in ("cover.jpg", "cover.jpeg", "cover.png"):
        for n in names:
            if n.split("/")[-1].lower() == want:
                return n
    for n in names:
        low = n.lower()
        if "cover" in low and low.endswith((".jpg", ".jpeg", ".png")):
            return n
    return None


def extract_cover(uzf, base_path):
    """Save the cover next to the text. Returns the path written, or None."""
    member = find_cover_member(uzf)
    if not member:
        log_status("No cover image found")
        return None

    ext = member.rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png"):
        ext = "jpg"
    dest = "%s.cover.%s" % (base_path, ext)
    try:
        entry = uzf.entry_for(member)
        size = entry["uncompressed_size"] if entry else 0
        uzf.extract_to(member, dest)
        log_status("Cover: %s -> %s (%d bytes)" % (member, dest, size))
        return dest
    except Exception as e:
        ent = uzf.entry_for(member)
        log_status("Cover extraction failed (%s -> %d bytes, method %d): %s"
                   % (member[-24:],
                      ent["uncompressed_size"] if ent else -1,
                      ent["compression_method"] if ent else -1, e))
        mem_note("  at cover failure")
        return None


# -----------------------------------------------------------------
class HtmlToTextStreamer:
    """Streams HTML out as plain text: strips tags, decodes the common
    entities, collapses whitespace, and puts a blank line between block
    elements - which is exactly the paragraph separation the reader's
    pagination expects.

    Everything accumulates into bytearrays. The original built its output as an
    immutable bytes object one character at a time, which allocates a fresh
    object per character (and another for the bytes([byte]) wrapper); on a
    75-chapter book that churn was enough to fragment the heap into failed
    allocations, quite apart from being slow.
    """

    def __init__(self, underlying_reader):
        self.reader = underlying_reader
        self.in_tag = False
        self.tag_buffer = bytearray()
        self.in_skip = False
        self.in_entity = False
        self.entity_buffer = bytearray()
        self.last_was_space = False
        self.buffer = b''
        self.pos = 0

        self.entities = {
            b'lt': b'<',
            b'gt': b'>',
            b'amp': b'&',
            b'quot': b'"',
            b'nbsp': b' ',
            b'apos': b"'",
            b'#39': b"'",
            b'mdash': b'-',
            b'ndash': b'-',
            b'hellip': b'...',
            b'rsquo': b"'",
            b'lsquo': b"'",
            b'ldquo': b'"',
            b'rdquo': b'"',
        }

    BLOCK_TAGS = (b'p', b'div', b'h1', b'h2', b'h3', b'h4', b'h5', b'h6',
                  b'li', b'td', b'tr', b'blockquote', b'section')

    def read(self, size=512):
        result = bytearray()
        while len(result) < size:
            if self.pos >= len(self.buffer):
                chunk = self.reader.read(size)
                if not chunk:
                    break
                self.buffer = chunk
                self.pos = 0

            buf = self.buffer
            n = len(buf)
            i = self.pos
            while i < n and len(result) < size:
                byte = buf[i]

                if self.in_skip:
                    if byte == 0x3C:            # '<'
                        self.in_tag = True
                        self.tag_buffer = bytearray()
                    elif self.in_tag:
                        if byte == 0x3E:        # '>'
                            self.in_tag = False
                            tag = self.tag_buffer.lower()
                            if tag == b'/script' or tag == b'/style':
                                self.in_skip = False
                        else:
                            self.tag_buffer.append(byte)
                else:
                    if byte == 0x3C:
                        self.in_tag = True
                        self.tag_buffer = bytearray()
                        self.last_was_space = True   # a tag separates words
                    elif self.in_tag:
                        if byte == 0x3E:
                            self.in_tag = False
                            tag = self.tag_buffer.lower()
                            if tag.startswith(b'script') or tag.startswith(b'style'):
                                self.in_skip = True
                            elif tag.startswith(b'/'):
                                name = tag[1:].split(b' ')[0]
                                if name in self.BLOCK_TAGS:
                                    result += b'\n\n'
                                    self.last_was_space = True
                            elif tag.startswith(b'br'):
                                result += b'\n'
                                self.last_was_space = True
                            self.tag_buffer = bytearray()
                        else:
                            self.tag_buffer.append(byte)
                    else:
                        if self.in_entity:
                            if byte == 0x3B:    # ';'
                                # bytes() because a bytearray cannot be a dict key
                                entity = bytes(self.entity_buffer).lower()
                                repl = self.entities.get(entity)
                                if repl is None:
                                    result += b'&'
                                    result += self.entity_buffer
                                    result += b';'
                                    self.last_was_space = False
                                elif repl != b' ' or not self.last_was_space:
                                    result += repl
                                    self.last_was_space = (repl == b' ')
                                self.in_entity = False
                                self.entity_buffer = bytearray()
                            elif len(self.entity_buffer) > 10:
                                # a bare '&' in the text, not an entity
                                result += b'&'
                                result += self.entity_buffer
                                result.append(byte)
                                self.in_entity = False
                                self.entity_buffer = bytearray()
                                self.last_was_space = False
                            else:
                                self.entity_buffer.append(byte)
                        elif byte == 0x26:      # '&'
                            self.in_entity = True
                            self.entity_buffer = bytearray()
                        elif byte in (32, 9, 10, 13):
                            if not self.last_was_space:
                                result.append(32)
                                self.last_was_space = True
                        else:
                            result.append(byte)
                            self.last_was_space = False
                i += 1

            self.pos = i

        return result

    def close(self):
        try:
            self.reader.close()
        except Exception:
            pass


# -----------------------------------------------------------------
def run_extraction(epub_path):
    """Convert an EPUB to /books/<name>.txt (+ .cover.<ext>).

    `epub_path` may be a full path or just a filename inside /books.
    Returns True if everything converted cleanly.
    """
    if epub_path.startswith("/"):
        epub_full_path = epub_path
        name = epub_path.split("/")[-1]
    else:
        epub_full_path = "/%s/%s" % (TARGET_DIR, epub_path)
        name = epub_path
    base_name = name[:-5] if name.lower().endswith(".epub") else name

    log_status("Processing: %s" % epub_full_path)

    try:
        os.stat("/" + TARGET_DIR)
    except OSError:
        os.mkdir("/" + TARGET_DIR)
        log_status("Created /%s" % TARGET_DIR)

    base_path = "/%s/%s" % (TARGET_DIR, base_name)
    concat_path = base_path + ".txt"
    success = True

    try:
        # Ask for the streaming window as the archive opens, so it is placed
        # while the heap is whole rather than carved out of it later.
        with UZipFile(epub_full_path, window=32768) as uzf:
            biggest = 0
            for e in uzf.filelist:
                if e["compression_method"] == 8 and e["uncompressed_size"] > biggest:
                    biggest = e["uncompressed_size"]
            log_status("Largest deflated member: %d bytes; window %s"
                       % (biggest, "ready" if uzf._window else "MISSING"))
            mem_note("After opening")

            # Cover next: if the text conversion runs into trouble later, at
            # least the cover is already saved.
            extract_cover(uzf, base_path)

            numbered = []
            plain = []
            for member in uzf.namelist():
                if member.endswith("/"):
                    continue
                if member.lower().endswith((".html", ".htm", ".xhtml")):
                    is_num, num = _is_numbered_html(member)
                    if is_num:
                        numbered.append((num, member))
                    else:
                        plain.append(member)

            numbered.sort(key=lambda x: x[0])
            ordered = plain + [m for _, m in numbered]
            total = len(ordered)
            log_status("Files to process: %d" % total)


            if not total:
                log_status("No HTML files found")
                return False

            extracted = 0
            failures = []
            with open(concat_path, "wb") as out:
                for idx, member in enumerate(ordered, 1):
                    # Reclaim the previous chapter before asking for the next.
                    try:
                        import gc
                        gc.collect()
                    except Exception:
                        pass

                    ent = uzf.entry_for(member)
                    csize = ent["compressed_size"] if ent else 0
                    usize = ent["uncompressed_size"] if ent else 0
                    if VERBOSE:
                        try:
                            import gc
                            log_status("[%d/%d] %s  %d->%d bytes, %d free, "
                                       "largest %d"
                                       % (idx, total, member[-20:], csize, usize,
                                          gc.mem_free(), largest_block()))
                        except Exception:
                            log_status("[%d/%d] %s  %d->%d bytes"
                                       % (idx, total, member[-20:], csize, usize))
                    else:
                        log_status("[%d/%d] %s" % (idx, total, member[-24:]))
                    try:
                        reader = uzf.get_reader(member)
                        stripper = HtmlToTextStreamer(reader)
                        while True:
                            chunk = stripper.read(512)
                            if not chunk:
                                break
                            out.write(chunk)
                        stripper.close()
                        out.write(b"\n\n")
                        extracted += 1
                    except MemoryError:
                        failures.append((member, csize, usize))
                        log_status("  OUT OF MEMORY needing ~%d contiguous "
                                   "(largest block was %d)"
                                   % (usize, largest_block()))
                        success = False
                    except Exception as e:
                        log_status("Failed %s: %s" % (member, e))
                        success = False

            log_status("--- EXTRACTION COMPLETE ---")
            log_status("Combined %d/%d files -> %s" % (extracted, total, concat_path))
            if failures:
                log_status("%d chapter(s) could not be decompressed:" % len(failures))
                for m, c, u in failures:
                    log_status("   %s  %d compressed -> %d uncompressed" % (m, c, u))
            return success

    except Exception as e:
        log_status("--- EXTRACTION FAILED ---")
        log_status("Error: %s" % e)
        return False


# -----------------------------------------------------------------
def main():
    print("\n--- EPUB EXTRACTOR ---")
    print("[EXTRACTOR] running from epub_xtract.main()")
    free_reader_memory()
    mem_note("At start")
    if not ensure_writable():
        return False
    epub = find_epub_file()
    if not epub:
        return False
    t0 = time.monotonic()
    ok = run_extraction(epub)
    log_status("Took %.1fs" % (time.monotonic() - t0))
    log_status("Done. Reset to read." if ok else "Finished with errors.")
    return ok


if __name__ == "__main__":
    main()
