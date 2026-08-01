"""Offline checks for the EPUB -> text converter.

    python3 tools/test_epub.py

Builds real EPUB files with the standard library's zipfile and runs the
converter's own code over them, so the ZIP parsing, the HTML stripping and the
cover discovery are all exercised end to end. Only the filesystem is different
from the device.

CircuitPython's zlib takes the same negative-wbits argument for raw DEFLATE as
CPython's, so the decompression path is genuinely the one that will run.
"""
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import CPDIR

sys.path.insert(0, CPDIR)
import epub_xtract
import uzipfile

epub_xtract.log_status = lambda msg: None      # quiet


CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head>
<title>Chapter</title>
<style>body {{ font-family: serif; }}</style>
<script>var x = 1 < 2;</script>
</head><body>
<h1>{title}</h1>
<p>First paragraph with an &amp; entity and a &quot;quote&quot;.</p>
<p>Second   paragraph   with    collapsing whitespace.<br/>After a break.</p>
<div>A div, <em>with</em> <strong>inline</strong> tags.</div>
</body></html>
"""


def build_epub(path, chapters, cover=None, cover_style="epub2",
               compress_cover=False):
    """Write a small but structurally real EPUB."""
    opf_items, spine = [], []
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles>'
                   '</container>', zipfile.ZIP_DEFLATED)

        for i, (fname, title) in enumerate(chapters):
            z.writestr("OEBPS/" + fname, CHAPTER.format(title=title),
                       zipfile.ZIP_DEFLATED)
            opf_items.append(f'<item id="c{i}" href="{fname}" '
                             f'media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{i}"/>')

        cover_meta = ""
        if cover:
            data = b"\xff\xd8\xff\xe0" + b"COVERDATA" * 40   # stand-in JPEG
            z.writestr("OEBPS/images/" + cover, data,
                       zipfile.ZIP_DEFLATED if compress_cover else zipfile.ZIP_STORED)
            if cover_style == "epub3":
                opf_items.append(f'<item id="cov" href="images/{cover}" '
                                 f'media-type="image/jpeg" properties="cover-image"/>')
            elif cover_style == "epub2":
                opf_items.append(f'<item id="cov" href="images/{cover}" '
                                 f'media-type="image/jpeg"/>')
                cover_meta = '<meta name="cover" content="cov"/>'
            else:   # only discoverable by filename
                opf_items.append(f'<item id="cov" href="images/{cover}" '
                                 f'media-type="image/jpeg"/>')

        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="2.0" unique-identifier="id"><metadata>'
                   f'{cover_meta}</metadata>'
                   f'<manifest>{"".join(opf_items)}</manifest>'
                   f'<spine>{"".join(spine)}</spine></package>',
                   zipfile.ZIP_DEFLATED)
    return path


def convert(tmp, epub_path):
    """Run the converter with TARGET_DIR pointed at a temp folder."""
    books = os.path.join(tmp, "books")
    os.makedirs(books, exist_ok=True)
    epub_xtract.TARGET_DIR = books.lstrip("/")

    # the converter builds paths as "/<TARGET_DIR>/<name>"; on a desktop the
    # temp dir is already absolute, so strip the leading slash it adds
    real_run = epub_xtract.run_extraction

    class _P(str):
        pass

    name = os.path.basename(epub_path)
    base = name[:-5]
    out_txt = os.path.join(books, base + ".txt")

    with uzipfile.UZipFile(epub_path) as uzf:
        epub_xtract.extract_cover(uzf, os.path.join(books, base))
        members = [m for m in uzf.namelist()
                   if m.lower().endswith((".html", ".htm", ".xhtml"))]
        numbered, plain = [], []
        for m in members:
            is_num, num = epub_xtract._is_numbered_html(m)
            (numbered if is_num else plain).append((num, m) if is_num else m)
        numbered.sort(key=lambda x: x[0])
        ordered = plain + [m for _, m in numbered]
        with open(out_txt, "wb") as out:
            for m in ordered:
                r = uzf.get_reader(m)
                s = epub_xtract.HtmlToTextStreamer(r)
                while True:
                    c = s.read(512)
                    if not c:
                        break
                    out.write(c)
                s.close()
                out.write(b"\n\n")
    return books, out_txt


def test_zip_reader_handles_both_methods():
    tmp = tempfile.mkdtemp()
    p = build_epub(os.path.join(tmp, "Book.epub"),
                   [("ch1.xhtml", "One")], cover="cover.jpg")
    with uzipfile.UZipFile(p) as z:
        names = z.namelist()
        assert "OEBPS/content.opf" in names, "central directory not parsed"
        stored = z.entry_for("mimetype")
        deflated = z.entry_for("OEBPS/ch1.xhtml")
        assert stored["compression_method"] == 0
        assert deflated["compression_method"] == 8
        assert z.read("mimetype") == b"application/epub+zip", "stored read failed"
        assert b"<h1>One</h1>" in z.read("OEBPS/ch1.xhtml"), "deflate read failed"
    print("  [ok] reads both stored and DEFLATE members")


def test_html_becomes_readable_text():
    tmp = tempfile.mkdtemp()
    p = build_epub(os.path.join(tmp, "Book.epub"), [("ch1.xhtml", "One")])
    _, txt = convert(tmp, p)
    text = open(txt, "rb").read().decode("utf-8")

    assert "<" not in text and ">" not in text.replace("&gt;", ""), \
        f"tags survived: {text[:120]!r}"
    assert "font-family" not in text, "<style> content leaked into the text"
    assert "var x" not in text, "<script> content leaked into the text"
    assert "&" in text and "&amp;" not in text, "entities were not decoded"
    assert '"quote"' in text, "quot entity not decoded"
    assert "Second paragraph with collapsing whitespace." in text, \
        f"whitespace not collapsed: {text!r}"
    assert "\n\n" in text, "no paragraph separation - the reader needs blank lines"
    print("  [ok] tags stripped, entities decoded, paragraphs separated")


def test_split_chapters_are_ordered_numerically():
    tmp = tempfile.mkdtemp()
    chapters = [(f"book_split_{i:03d}.xhtml", f"Part{i}") for i in (10, 2, 1, 21, 3)]
    p = build_epub(os.path.join(tmp, "Book.epub"), chapters)
    _, txt = convert(tmp, p)
    text = open(txt, "rb").read().decode("utf-8")
    order = [text.index(f"Part{i}") for i in (1, 2, 3, 10, 21)]
    assert order == sorted(order), (
        "Calibre _split_ chapters came out in the wrong order - they sort "
        "numerically, not as strings (10 before 2)")
    print("  [ok] _split_NNN chapters ordered numerically, not lexically")


def test_cover_found_all_three_ways():
    for style, label in (("epub3", "EPUB3 properties=cover-image"),
                         ("epub2", 'EPUB2 <meta name="cover">'),
                         ("none", "filename fallback")):
        tmp = tempfile.mkdtemp()
        p = build_epub(os.path.join(tmp, "Book.epub"), [("ch1.xhtml", "One")],
                       cover="cover.jpg", cover_style=style)
        books, _ = convert(tmp, p)
        dest = os.path.join(books, "Book.cover.jpg")
        assert os.path.exists(dest), f"cover not extracted via {label}"
        assert open(dest, "rb").read().startswith(b"\xff\xd8"), \
            f"cover bytes wrong via {label}"
    print("  [ok] cover found via EPUB3, EPUB2 and filename fallback")


def test_cover_extraction_streams_when_stored():
    """Cover images are already compressed, so EPUBs normally STORE them -
    that is the case that can be copied without holding it in memory."""
    tmp = tempfile.mkdtemp()
    for compressed, label in ((False, "stored"), (True, "deflated")):
        p = build_epub(os.path.join(tmp, f"B{compressed}.epub"),
                       [("ch1.xhtml", "One")], cover="cover.jpg",
                       compress_cover=compressed)
        books, _ = convert(tmp, p)
        dest = os.path.join(books, f"B{compressed}.cover.jpg")
        assert os.path.exists(dest), f"{label} cover not extracted"
        assert len(open(dest, "rb").read()) == 4 + 9 * 40, f"{label} cover truncated"
    print("  [ok] cover extracted whether stored or deflated")


def test_missing_cover_is_not_fatal():
    tmp = tempfile.mkdtemp()
    p = build_epub(os.path.join(tmp, "Book.epub"), [("ch1.xhtml", "One")], cover=None)
    books, txt = convert(tmp, p)
    assert os.path.exists(txt) and os.path.getsize(txt) > 0, \
        "text conversion should still succeed without a cover"
    assert not any(f.endswith((".jpg", ".png")) for f in os.listdir(books))
    print("  [ok] an EPUB with no cover still converts")


def test_streamer_does_not_reallocate_per_character():
    """The streamer must accumulate into bytearrays.

    It originally built its output as an immutable bytes object one character
    at a time - a fresh allocation per character, plus another for the
    bytes([byte]) wrapper. On a 75-chapter book that churn fragmented the heap
    badly enough that whole chapters failed to allocate:

        MemoryError: memory allocation failed, allocating 47184 bytes

    Roughly one allocation per 512-byte chunk now, instead of two per byte.
    """
    import ast
    src = open(os.path.join(CPDIR, "epub_xtract.py")).read()
    cls = None
    for node in ast.parse(src).body:
        if isinstance(node, ast.ClassDef) and node.name == "HtmlToTextStreamer":
            cls = node
    assert cls, "HtmlToTextStreamer not found"

    # Inspect the code, not the text - the docstring names the old pattern in
    # order to explain it, and matching on source text flagged that instead.
    for call in (n for n in ast.walk(cls) if isinstance(n, ast.Call)):
        if isinstance(call.func, ast.Name) and call.func.id == "bytes":
            assert not (call.args and isinstance(call.args[0], ast.List)), (
                "streamer builds a bytes object from a list per byte again - "
                "one allocation per character")

    read = next((f for f in cls.body
                 if isinstance(f, ast.FunctionDef) and f.name == "read"), None)
    assert read, "read() not found"
    seg = ast.get_source_segment(src, read)
    assert "result = bytearray()" in seg, (
        "read() accumulates into an immutable bytes again; that is quadratic "
        "and fragments the heap")
    assert "self.buffer = self.buffer[i:]" not in seg, (
        "read() re-slices its input buffer each time, allocating a copy; "
        "track a position instead")
    print("  [ok] streamer accumulates in bytearrays (no per-character allocation)")


def test_entry_point_runs_without_a_name_guard():
    """convert.py has to run whatever CircuitPython sets __name__ to.

    Whether the file CircuitPython runs gets __name__ == "__main__" is not
    documented, and when it does not, a guarded file just defines its functions
    and exits - which looks exactly like nothing happening. convert.py
    therefore calls main() at module level.
    """
    import ast
    path = os.path.join(CPDIR, "convert.py")
    assert os.path.exists(path), "convert.py entry point is missing"
    src = open(path).read()
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = ast.dump(node.test)
            assert "__name__" not in test, (
                "convert.py guards its work behind __name__ - it will silently "
                "do nothing if CircuitPython does not set it to '__main__'")

    calls = [n for n in tree.body
             if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    assert any(isinstance(c.value.func, ast.Attribute) and c.value.func.attr == "main"
               for c in calls), "convert.py never calls main() at module level"
    print("  [ok] convert.py runs regardless of __name__")


def test_streaming_fallback_matches_zlib():
    """When no block big enough for zlib's output is free, uzipfile inflates in
    a stream instead. Both paths must give the same bytes.

    This is what lets a chapter larger than the biggest free block convert at
    all - on a real book the largest block had fallen to ~30KB while chapters
    ran to 49KB.
    """
    import hashlib, zlib as _zlib
    tmp = tempfile.mkdtemp()
    chapters = [(f"ch{i}.xhtml", f"Part{i}") for i in range(4)]
    p = build_epub(os.path.join(tmp, "Book.epub"), chapters)

    def digests(force_stream):
        real = uzipfile.zlib.decompress
        if force_stream:
            def boom(*a, **k):
                raise MemoryError("forced")
            uzipfile.zlib.decompress = boom
        try:
            z = uzipfile.UZipFile(p)
            z.ensure_window()
            out = {}
            for m in z.namelist():
                if not m.lower().endswith((".xhtml", ".html")):
                    continue
                r = z.get_reader(m)
                if force_stream:
                    assert type(r).__name__ == "RawInflater", \
                        "did not take the streaming path"
                d = b""
                while True:
                    c = r.read(512)
                    if not c:
                        break
                    d += c
                out[m] = hashlib.sha256(d).hexdigest()
            z.close()
            return out
        finally:
            uzipfile.zlib.decompress = real

    fast, slow = digests(False), digests(True)
    assert fast and fast == slow, "streaming fallback differs from zlib"
    print(f"  [ok] streaming fallback byte-identical to zlib ({len(fast)} members)")


def test_inflater_handles_every_deflate_block_type():
    """Stored, fixed-Huffman and dynamic-Huffman blocks, plus back-references
    that overlap and that reach across the window."""
    import io, zlib as _zlib, random
    from inflate import RawInflater

    def roundtrip(data, level):
        c = _zlib.compressobj(level, _zlib.DEFLATED, -15)
        comp = c.compress(data) + c.flush()
        r = RawInflater(io.BytesIO(comp), chunk=64)
        out = bytearray()
        while True:
            b = r.read(97)          # awkward size on purpose
            if not b:
                break
            out += b
        return bytes(out)

    rnd = random.Random(5)
    cases = [b"", b"x", b"ab" * 20000,
             bytes(rnd.getrandbits(8) for _ in range(9000)),
             b"a" * 40000,
             bytes(rnd.getrandbits(8) for _ in range(4000)) * 10]
    for data in cases:
        for level in (0, 1, 9):     # 0 gives stored blocks
            assert roundtrip(data, level) == data, \
                f"inflate mismatch, {len(data)} bytes at level {level}"
    print("  [ok] inflater matches zlib on stored/fixed/dynamic blocks")


def test_inflater_imported_up_front():
    """uzipfile must import the streaming inflater at module level.

    Importing a module allocates - inflate.py costs about 14KB for its code
    objects and tables - and the fallback is reached exactly when memory is
    short. A lazy import therefore fails at the one moment it is needed, which
    is what stopped a 47KB cover being extracted:

        Cover extraction failed: memory allocation failed, allocating 13848 bytes
    """
    import ast
    src = open(os.path.join(CPDIR, "uzipfile.py")).read()
    tree = ast.parse(src)

    top = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                top.add(a.asname or a.name)
    assert "RawInflater" in top or "inflate" in top, (
        "uzipfile does not import the inflater at module level")

    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef,)):
            for node in ast.walk(fn):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names]
                    assert not any("inflate" in n for n in names), (
                        f"{fn.name}() imports the inflater lazily; it will fail "
                        f"when memory is tight, which is when it is used")
    print("  [ok] streaming inflater imported up front, not under pressure")


def test_big_deflated_member_streams_to_file():
    """A large DEFLATED member written to a file must not be decompressed whole.

    Doing so needs the compressed AND uncompressed sizes at once, in two big
    blocks - about 207KB for a 105KB cover. Covers are barely compressible, so
    this is the realistic case, and it is what kept failing on the device:

        Cover extraction failed (cover.jpeg -> 47308 bytes, method 8):
            memory allocation failed
    """
    tmp = tempfile.mkdtemp()
    blob = bytes((i * 7 + (i >> 3)) & 0xFF for i in range(60000))   # ~incompressible
    p = os.path.join(tmp, "Big.epub")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container><rootfiles><rootfile '
                   'full-path="OEBPS/content.opf"/></rootfiles></container>',
                   zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/ch1.xhtml", CHAPTER.format(title="One"), zipfile.ZIP_DEFLATED)
        # DEFLATED on purpose - the case that needs streaming
        z.writestr("OEBPS/images/cover.jpg", blob, zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package><metadata>'
                   '<meta name="cover" content="cov"/></metadata><manifest>'
                   '<item id="c0" href="ch1.xhtml"/>'
                   '<item id="cov" href="images/cover.jpg" media-type="image/jpeg"/>'
                   '</manifest><spine><itemref idref="c0"/></spine></package>',
                   zipfile.ZIP_DEFLATED)

    z = uzipfile.UZipFile(p, window=32768)
    member = epub_xtract.find_cover_member(z)
    assert member, "cover not found"
    entry = z.entry_for(member)
    assert entry["compression_method"] == 8, "test needs a deflated cover"

    # zlib must not be reached at all for this one
    real = uzipfile.zlib.decompress
    used = []
    uzipfile.zlib.decompress = lambda *a, **k: (used.append(1), real(*a, **k))[1]
    try:
        dest = os.path.join(tmp, "out.jpg")
        z.extract_to(member, dest)
    finally:
        uzipfile.zlib.decompress = real
    z.close()

    assert open(dest, "rb").read() == blob, "streamed cover does not match"
    assert not used, ("a big deflated member was decompressed whole instead of "
                      "streamed - that needs two large contiguous blocks")
    print("  [ok] big deflated members stream to file (no whole-member block)")


def test_eocd_scan_does_not_grab_64k():
    """Finding the end-of-central-directory must not read the 64KB worst case.

    A ZIP comment can be 64KB, so the naive scan reads that much - the largest
    single allocation the converter made, bigger than any chapter. Once the
    streaming window was allocated first there was no longer room for it:

        Error: memory allocation failed, allocating 65558 bytes

    EPUBs have no comment, so a small scan finds it; it only grows if it must.
    """
    import ast
    src = open(os.path.join(CPDIR, "uzipfile.py")).read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.ClassDef) and node.name == "UZipFile":
            for m in node.body:
                if isinstance(m, ast.FunctionDef) and m.name == "_read_central_directory":
                    seg = ast.get_source_segment(src, m)
    assert "65535 + 22" not in seg, (
        "central directory scan reads the 64KB worst case up front again")

    # and it must still work whatever the comment size
    tmp = tempfile.mkdtemp()
    for clen in (0, 3000, 65000):
        p = os.path.join(tmp, f"c{clen}.zip")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("a.txt", b"payload" * 200, zipfile.ZIP_DEFLATED)
            zf.comment = b"x" * clen
        z = uzipfile.UZipFile(p)
        assert z.namelist() == ["a.txt"], f"comment {clen}: members lost"
        assert z.read("a.txt") == b"payload" * 200, f"comment {clen}: bad read"
        z.close()

    bad = os.path.join(tmp, "bad.bin")
    open(bad, "wb").write(b"not a zip" * 500)
    try:
        uzipfile.UZipFile(bad)
        raise AssertionError("a non-zip was accepted")
    except OSError:
        pass
    print("  [ok] EOCD scan starts small, grows only if needed, still validates")


def test_output_paginates_in_the_reader():
    """The point of the converter: its output has to feed the reader's engine."""
    from _harness import load_engine, walk_pages
    tmp = tempfile.mkdtemp()
    chapters = [(f"book_split_{i:03d}.xhtml", f"Part{i}") for i in range(1, 6)]
    p = build_epub(os.path.join(tmp, "Book.epub"), chapters)
    _, txt = convert(tmp, p)

    ns, font = load_engine("literata.pf")
    ns["text_file"] = txt
    pages = walk_pages(ns["paginate_text"], txt)
    real = [p for p in pages if p[2]]
    assert real, "converted text produced no pages"
    for off, rem, lines, _ in real:
        for ln in lines:
            assert font.text_width(ln.decode("utf-8")) <= 292, \
                f"converted text overflows the display at offset {off}"
    print(f"  [ok] converted text paginates cleanly ({len(real)} pages)")


def main():
    print("EPUB converter:")
    test_zip_reader_handles_both_methods()
    test_html_becomes_readable_text()
    test_split_chapters_are_ordered_numerically()
    test_cover_found_all_three_ways()
    test_cover_extraction_streams_when_stored()
    test_missing_cover_is_not_fatal()
    test_streamer_does_not_reallocate_per_character()
    test_entry_point_runs_without_a_name_guard()
    test_streaming_fallback_matches_zlib()
    test_inflater_handles_every_deflate_block_type()
    test_inflater_imported_up_front()
    test_big_deflated_member_streams_to_file()
    test_eocd_scan_does_not_grab_64k()
    test_output_paginates_in_the_reader()
    print("\nALL EPUB CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
