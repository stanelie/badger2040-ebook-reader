# ------------------------------------------------------------
# epub_xtract.py  -  EPUB -> plain text converter (CircuitPython port)
# ------------------------------------------------------------
# Converts an EPUB in /books into a .txt the reader can open, and saves the
# cover image next to it.
#
# Run it STANDALONE, not alongside the reader: a DEFLATE member has to be
# decompressed whole (CircuitPython has no streaming inflater), and the reader
# holds roughly 60KB in page buffers, hyphenation patterns and fonts. With the
# reader loaded there is not enough left for a chapter.
#
#   1. copy the .epub into /books over USB, as normal
#   2. hold A while resetting - boot.py then hands the filesystem to
#      CircuitPython, which is what lets this write to /books
#   3. at the REPL:  import epub_xtract; epub_xtract.main()
#   4. reset normally to read the .txt it produced
#
# Output for "/books/Sway.epub":
#   /books/Sway.txt         the text, blank line between paragraphs
#   /books/Sway.cover.jpg   the cover, if the EPUB declares one
import os
import time

from uzipfile import UZipFile

# --- Configuration ------------------------------------------------
TARGET_DIR = "books"
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
    """Value of attribute `name` in a tag fragment, or None."""
    for quote in ('"', "'"):
        key = name + "=" + quote
        i = text.find(key)
        if i >= 0:
            i += len(key)
            j = text.find(quote, i)
            if j > i:
                return text[i:j]
    return None


def _tags(xml, tag):
    """Yield the text of each <tag ...> occurrence."""
    out = []
    needle = "<" + tag
    i = 0
    while True:
        i = xml.find(needle, i)
        if i < 0:
            break
        j = xml.find(">", i)
        if j < 0:
            break
        out.append(xml[i:j])
        i = j
    return out


def _resolve(base_member, href):
    """Resolve an href that is relative to the file it appeared in."""
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
        container = uzf.read("META-INF/container.xml").decode("utf-8", "ignore")
        for tag in _tags(container, "rootfile"):
            path = _attr(tag, "full-path")
            if path:
                opf_name = path
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
            opf = uzf.read(opf_name).decode("utf-8", "ignore")
            items = _tags(opf, "item")

            # EPUB 3: an item flagged as the cover image
            for tag in items:
                props = _attr(tag, "properties") or ""
                if "cover-image" in props:
                    href = _attr(tag, "href")
                    if href:
                        return _resolve(opf_name, href)

            # EPUB 2: <meta name="cover" content="some-id">
            cover_id = None
            for tag in _tags(opf, "meta"):
                if (_attr(tag, "name") or "").lower() == "cover":
                    cover_id = _attr(tag, "content")
                    if cover_id:
                        break
            if cover_id:
                for tag in items:
                    if _attr(tag, "id") == cover_id:
                        href = _attr(tag, "href")
                        if href:
                            return _resolve(opf_name, href)
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
        log_status("Cover extraction failed: %s" % e)
        return None


# -----------------------------------------------------------------
class HtmlToTextStreamer:
    """Streams HTML out as plain text: strips tags, decodes the common
    entities, collapses whitespace, and puts a blank line between block
    elements - which is exactly the paragraph separation the reader's
    pagination expects."""

    def __init__(self, underlying_reader):
        self.reader = underlying_reader
        self.in_tag = False
        self.tag_buffer = b''
        self.in_skip = False
        self.in_entity = False
        self.entity_buffer = b''
        self.last_was_space = False
        self.buffer = b''

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
        result = b''
        while len(result) < size:
            if not self.buffer:
                chunk = self.reader.read(size)
                if not chunk:
                    break
                self.buffer = chunk

            i = 0
            while i < len(self.buffer) and len(result) < size:
                byte = self.buffer[i]
                char = bytes([byte])

                if self.in_skip:
                    if byte == 0x3C:            # '<'
                        self.in_tag = True
                        self.tag_buffer = b''
                    elif self.in_tag:
                        if byte == 0x3E:        # '>'
                            self.in_tag = False
                            tag_str = self.tag_buffer.lower()
                            if tag_str == b'/script' or tag_str == b'/style':
                                self.in_skip = False
                        else:
                            self.tag_buffer += char
                else:
                    if byte == 0x3C:
                        self.in_tag = True
                        self.tag_buffer = b''
                        self.last_was_space = True   # a tag separates words
                    elif self.in_tag:
                        if byte == 0x3E:
                            self.in_tag = False
                            tag_str = self.tag_buffer.lower()
                            if tag_str.startswith(b'script') or tag_str.startswith(b'style'):
                                self.in_skip = True
                            elif tag_str.startswith(b'/'):
                                tag_name = tag_str[1:].split(b' ')[0]
                                if tag_name in self.BLOCK_TAGS:
                                    result += b'\n\n'
                                    self.last_was_space = True
                            elif tag_str.startswith(b'br'):
                                result += b'\n'
                                self.last_was_space = True
                            self.tag_buffer = b''
                        else:
                            self.tag_buffer += char
                    else:
                        if self.in_entity:
                            if byte == 0x3B:    # ';'
                                entity = self.entity_buffer.lower()
                                repl = self.entities.get(
                                    entity, b'&' + self.entity_buffer + b';')
                                if repl != b' ' or not self.last_was_space:
                                    result += repl
                                    self.last_was_space = (repl == b' ')
                                self.in_entity = False
                                self.entity_buffer = b''
                            elif len(self.entity_buffer) > 10:
                                # not an entity after all - flush it as text
                                result += b'&' + self.entity_buffer + char
                                self.in_entity = False
                                self.entity_buffer = b''
                                self.last_was_space = False
                            else:
                                self.entity_buffer += char
                        elif byte == 0x26:      # '&'
                            self.in_entity = True
                            self.entity_buffer = b''
                        elif byte in (32, 9, 10, 13):
                            if not self.last_was_space:
                                result += b' '
                                self.last_was_space = True
                        else:
                            result += char
                            self.last_was_space = False
                i += 1

            self.buffer = self.buffer[i:]

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
        with UZipFile(epub_full_path) as uzf:
            # Cover first: it is cheap, and if the text conversion runs out of
            # memory later at least the cover is already saved.
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
            with open(concat_path, "wb") as out:
                for idx, member in enumerate(ordered, 1):
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
                        log_status("Out of memory on %s - run this standalone, "
                                   "without the reader loaded" % member)
                        success = False
                    except Exception as e:
                        log_status("Failed %s: %s" % (member, e))
                        success = False

            log_status("--- EXTRACTION COMPLETE ---")
            log_status("Combined %d/%d files -> %s" % (extracted, total, concat_path))
            return success

    except Exception as e:
        log_status("--- EXTRACTION FAILED ---")
        log_status("Error: %s" % e)
        return False


# -----------------------------------------------------------------
def main():
    print("\n--- EPUB EXTRACTOR ---")
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
