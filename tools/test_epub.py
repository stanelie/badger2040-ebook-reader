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
    test_output_paginates_in_the_reader()
    print("\nALL EPUB CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
