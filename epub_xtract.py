# ------------------------------------------------------------
# epub_xtract.py  –  Badger 2040 EPUB → HTML extractor
# ------------------------------------------------------------
import os
import time
import machine
from uzipfile import UZipFile

# --- Configuration ------------------------------------------------
TARGET_DIR = "books"
MAX_STATUS_LINES = 6
STATUS_HISTORY = []


def log_status(msg: str) -> None:
    """Append to history and print to REPL."""
    global STATUS_HISTORY
    STATUS_HISTORY.append(msg)
    if len(STATUS_HISTORY) > MAX_STATUS_LINES:
        STATUS_HISTORY = STATUS_HISTORY[-MAX_STATUS_LINES:]
    print(f"[EXTRACTOR] {msg}")


# -----------------------------------------------------------------
def find_epub_file() -> str | None:
    log_status("Scanning root for .epub …")
    try:
        for f in os.listdir("/"):
            if f.lower().endswith(".epub"):
                log_status(f"Found: {f}")
                return f
    except Exception as e:
        log_status(f"FS error: {e}")
    log_status("No .epub found.")
    return None


# -----------------------------------------------------------------
def _ensure_path(path: str) -> None:
    """Create intermediate directories (relative to cwd)."""
    parts = path.split("/")
    cur = ""
    for p in parts:
        if not p:
            continue
        cur = cur + "/" + p if cur else p
        try:
            os.mkdir(cur)
        except OSError:               # already exists
            pass


# -----------------------------------------------------------------
def _is_numbered_html(member: str) -> tuple[bool, int]:
    """Check if member is a numbered _split_XXX.html/htm, return (True, num) or (False, -1)."""
    if not member.lower().endswith((".html", ".htm")):
        return False, -1
    basename = member.split("/")[-1]
    if "_split_" not in basename:
        return False, -1
    try:
        num_str = basename.split("_split_")[1].split(".")[0]
        num = int(num_str)
        return True, num
    except (IndexError, ValueError):
        return False, -1


# -----------------------------------------------------------------
class HtmlToTextStreamer:
    """Streaming HTML to plain text converter with tag stripping, whitespace normalization, and logical separations."""
    def __init__(self, underlying_reader):
        self.reader = underlying_reader
        self.in_tag = False
        self.tag_buffer = b''
        self.in_skip = False
        self.in_entity = False
        self.entity_buffer = b''
        self.last_was_space = False
        self.buffer = b''

        # Common entities
        self.entities = {
            b'lt': b'<',
            b'gt': b'>',
            b'amp': b'&',
            b'quot': b'"',
            b'nbsp': b' ',
            b'apos': b"'",
        }

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
                    if byte == ord('<'):
                        self.in_tag = True
                        self.tag_buffer = b''
                    elif self.in_tag:
                        if byte == ord('>'):
                            self.in_tag = False
                            tag_str = self.tag_buffer.lower()
                            if tag_str == b'/script' or tag_str == b'/style':
                                self.in_skip = False
                        else:
                            self.tag_buffer += char
                else:
                    if byte == ord('<'):
                        self.in_tag = True
                        self.tag_buffer = b''
                        self.last_was_space = True  # Treat tag as space separator
                    elif self.in_tag:
                        if byte == ord('>'):
                            self.in_tag = False
                            tag_str = self.tag_buffer.lower()
                            # Check for skip start
                            if tag_str.startswith(b'script') or tag_str.startswith(b'style'):
                                self.in_skip = True
                            # Insert newlines for block closes or br
                            elif tag_str.startswith(b'/'):
                                tag_name = tag_str[1:].split(b' ')[0]
                                if tag_name in [b'p', b'div', b'h1', b'h2', b'h3', b'h4', b'h5', b'h6', b'li', b'td', b'tr']:
                                    result += b'\n\n'
                                    self.last_was_space = True
                            elif tag_str.startswith(b'br'):
                                result += b'\n'
                                self.last_was_space = True
                            self.tag_buffer = b''
                        else:
                            self.tag_buffer += char
                    else:
                        # Text content
                        if self.in_entity:
                            if byte == ord(';'):
                                entity = self.entity_buffer.lower()
                                repl = self.entities.get(entity, b'&' + self.entity_buffer + b';')
                                if repl != b' ' or not self.last_was_space:
                                    result += repl
                                    self.last_was_space = (repl == b' ')
                                self.in_entity = False
                                self.entity_buffer = b''
                            else:
                                self.entity_buffer += char
                        elif byte == ord('&'):
                            self.in_entity = True
                            self.entity_buffer = b''
                        elif byte in (32, 9, 10, 13):  # space, tab, \n, \r
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
        self.reader.close()


# -----------------------------------------------------------------
def run_extraction(epub_path: str) -> bool:
    """
    Extract EPUB to text file in TARGET_DIR.
    
    Args:
        epub_path: Can be either:
                   - Full path like "/books/Sway.epub"
                   - Just filename like "Sway.epub" (will look in TARGET_DIR)
    
    Returns:
        True if successful, False otherwise
    """
    # Normalize the epub path
    if epub_path.startswith("/"):
        # Full path provided - use as-is
        epub_full_path = epub_path
        # Extract just the base name for output
        base_name = epub_path.split("/")[-1].rsplit('.epub', 1)[0] if epub_path.lower().endswith('.epub') else epub_path.split("/")[-1]
    else:
        # Just filename - look in TARGET_DIR
        epub_full_path = f"/{TARGET_DIR}/{epub_path}"
        base_name = epub_path.rsplit('.epub', 1)[0] if epub_path.lower().endswith('.epub') else epub_path
    
    log_status(f"Processing: {epub_full_path}")
    
    # Ensure target directory exists
    log_status(f"Preparing /{TARGET_DIR} …")
    try:
        os.stat(TARGET_DIR)
    except OSError:
        os.mkdir(TARGET_DIR)
        log_status(f"Created /{TARGET_DIR}")

    time.sleep(0.5)

    success = True
    try:
        with UZipFile(epub_full_path) as uzf:
            all_members = uzf.namelist()
            # Only include HTML files
            numbered = []
            non_numbered_html = []
            for member in all_members:
                if member.endswith("/"):
                    continue  # skip dirs
                if member.lower().endswith((".html", ".htm")):
                    is_num, num = _is_numbered_html(member)
                    if is_num:
                        numbered.append((num, member))
                    else:
                        non_numbered_html.append(member)

            total = len(non_numbered_html) + len(numbered)
            log_status(f"Files to process: {total}")

            # Sort numbered by num
            numbered.sort(key=lambda x: x[0])

            extracted_count = 0

            # Output path - always in TARGET_DIR
            concat_path = f"/{TARGET_DIR}/{base_name}.txt"
            
            has_combined = non_numbered_html or numbered
            if has_combined:
                try:
                    with open(concat_path, "wb") as out:
                        # First, non-numbered HTML in order encountered
                        for j, member in enumerate(non_numbered_html, 1):
                            disp = member[-20:]
                            log_status(f"[{j}/{total}] (stream) …{disp}")

                            try:
                                reader = uzf.get_reader(member)
                                stripper = HtmlToTextStreamer(reader)
                                while True:
                                    chunk = stripper.read(512)
                                    if not chunk:
                                        break
                                    out.write(chunk)
                                stripper.close()
                                extracted_count += 1
                            except Exception as e:
                                log_status(f"Failed {member}: {e}")
                                success = False

                        # Then, sorted numbered HTML
                        for k, (num, member) in enumerate(numbered, 1):
                            idx = len(non_numbered_html) + k
                            disp = member[-20:]
                            log_status(f"[{idx}/{total}] (stream) …{disp}")

                            try:
                                reader = uzf.get_reader(member)
                                stripper = HtmlToTextStreamer(reader)
                                while True:
                                    chunk = stripper.read(512)
                                    if not chunk:
                                        break
                                    out.write(chunk)
                                stripper.close()
                                extracted_count += 1
                            except Exception as e:
                                log_status(f"Failed {member}: {e}")
                                success = False
                except Exception as e:
                    log_status(f"Concat failed: {e}")
                    success = False

            log_status("--- EXTRACTION COMPLETE ---")
            if has_combined:
                log_status(f"Combined {extracted_count} HTMLs → {concat_path}")
            else:
                log_status("No HTML files found")
            return success

    except Exception as e:
        log_status("--- EXTRACTION FAILED ---")
        log_status(f"Error: {e}")
        return False


# -----------------------------------------------------------------
def main() -> None:
    print("\n--- EPUB EXTRACTOR STARTING ---")
    log_status("Init OK")

    epub = find_epub_file()
    if epub:
        run_extraction(epub)

    log_status("Idle loop – press RESET to stop")
    while True:
        machine.idle()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
