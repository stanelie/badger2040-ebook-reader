"""Offline validation of the reader's text layout and pagination engine.

    python tools/test_reflow.py            # every installed font
    python tools/test_reflow.py literata.pf

Why these checks matter: paginate_text's (next_offset, remainder) pair is saved
to NVRAM as the reading position and re-run by find_previous_page for back
navigation, so it has to be exactly reproducible. Several of these assertions
correspond to bugs that actually shipped and were caught here:

  * lines overflowing the display (over-long words weren't broken)
  * a word split across a PAGE boundary, which would leave half a word in the
    saved remainder and corrupt resume
  * back-navigation rewinding only a few lines instead of a full page
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (LINES_PER_PAGE, TEXT_WIDTH, available_fonts, load_engine,
                      make_corpus, walk_pages)

def source_words(path, clean_word):
    """The book's words as paginate_text sees them.

    Uses the engine's own clean_word() rather than a copy of the substitution
    rules - keeping a duplicate list here went stale the moment the French
    mappings were added, and reported a false failure.
    """
    raw = open(path, "rb").read().decode("utf-8", "ignore")
    words = []
    for w in raw.split():
        words.extend(clean_word(w).split())
    return words


def is_hyphen_break(line):
    """True if the line ends in a hyphenation break - a '-' fused to the end of
    a word, which continues on the next line.

    A standalone dash is not one: French text turns em dashes into ' - ', so a
    line can legitimately end with a space-separated '-' without anything being
    split. Only the fused kind is unsafe at a page boundary.
    """
    return len(line) >= 2 and line.endswith(b"-") and line[-2:-1] != b" "


def visible_words(pages):
    """Every word actually drawn, with hyphenation breaks re-joined."""
    toks = []
    for _, _, lines, _ in pages:
        for ln in lines:
            if ln:
                toks.extend(ln.decode("utf-8").split())
    merged, i = [], 0
    while i < len(toks):
        t = toks[i]
        while t.endswith("-") and len(t) > 1 and i + 1 < len(toks):
            t = t[:-1] + toks[i + 1]
            i += 1
        merged.append(t)
        i += 1
    return merged


def check_book(ns, font, name, path, strict=True):
    paginate_text = ns["paginate_text"]
    find_previous_page = ns["find_previous_page"]
    ns["text_file"] = path
    px = font.text_width

    pages = walk_pages(paginate_text, path)
    real = [p for p in pages if p[2]]
    print(f"  {name:12} {os.stat(path)[6]:6} bytes  {len(real):3} pages", end="")

    # 1. nothing overflows the text area, horizontally or vertically. A page
    #    with more than LINES_PER_PAGE lines draws off the bottom of the screen,
    #    so that text is invisible even though pagination "kept" it.
    for off, _, lines, _ in pages:
        assert len(lines) <= LINES_PER_PAGE, (
            f"{name}: page at offset {off} has {len(lines)} lines, "
            f"only {LINES_PER_PAGE} fit on screen")
        for ln in lines:
            w = px(ln.decode("utf-8"))
            assert w <= TEXT_WIDTH, (
                f"{name}: line {w}px > {TEXT_WIDTH}px at offset {off}: {ln!r}")

    # 1b. every drawn character must exist in the font. Anything outside its
    #     range renders as '?', which is how accented text used to look.
    for off, _, lines, _ in pages:
        for ln in lines:
            for ch in ln.decode("utf-8"):
                idx = ord(ch) - font.first
                assert 0 <= idx < font.count, (
                    f"{name}: {ch!r} (U+{ord(ch):04X}) is outside the font and "
                    f"would render as '?' - it needs a glyph, or a mapping in "
                    f"clean_word()")

    # 2. hyphenation never splits a word across a PAGE boundary (offset safety)
    hyph = 0
    for off, _, lines, _ in pages:
        nonempty = [l for l in lines if l]
        hyph += sum(1 for l in nonempty if is_hyphen_break(l))
        if nonempty:
            assert not is_hyphen_break(nonempty[-1]), (
                f"{name}: word split across page boundary at offset {off}: "
                f"{nonempty[-1]!r}")

    # 3. every page redraws identically from its own (offset, remainder).
    #    This is exactly what NVRAM resume relies on.
    for off, rem, lines, next_off in real:
        l2, no2, _ = paginate_text(path, off, rem)
        assert l2 == lines and no2 == next_off, (
            f"{name}: page at offset {off} not reproducible from its own state")

    # 4. the text itself survives: same words, same order, nothing lost or
    #    duplicated. Compared letters-only because a break may add a hyphen.
    src = [w for w in (x.replace("-", "") for x in source_words(path, ns["clean_word"])) if w]
    vis = [w for w in (x.replace("-", "") for x in visible_words(pages)) if w]
    si = vi = 0
    while si < len(src) and vi < len(vis):
        if vis[vi] == src[si]:
            si += 1
            vi += 1
        elif px(src[si]) > TEXT_WIDTH and src[si].startswith(vis[vi]):
            acc = ""
            while vi < len(vis) and len(acc) < len(src[si]):
                acc += vis[vi]
                vi += 1
            assert acc == src[si], f"{name}: long word mangled: {acc!r} != {src[si]!r}"
            si += 1
        else:
            raise AssertionError(
                f"{name}: text diverged at source word {si} ({src[si]!r}) "
                f"vs drawn ({vis[vi]!r})")
    if si < len(src):
        leftover = src[si:]
        assert all(px(w) > TEXT_WIDTH * LINES_PER_PAGE for w in leftover), (
            f"{name}: {len(leftover)} word(s) never drawn: {leftover[:3]}")

    # 5. back navigation always moves about a page - never a few lines.
    #    (find_previous_page is the fallback used when the RAM history is empty.)
    backs = ""
    if strict and len(real) >= 4:
        avg = (real[-1][0] - real[0][0]) / (len(real) - 1)
        dists, exact = [], 0
        for i in range(1, len(real)):
            off = real[i][0]
            p_off, p_rem = find_previous_page(off)
            assert p_off < off, f"{name}: back did not move at offset {off}"
            dists.append(off - p_off)
            if paginate_text(path, p_off, p_rem)[1] == off:
                exact += 1
        tiny = sum(1 for d in dists if d < 0.5 * avg)
        assert tiny == 0, f"{name}: {tiny} back press(es) rewound under half a page"
        lo, hi = min(dists) / avg, max(dists) / avg
        backs = f"  back {lo:.2f}-{hi:.2f} pages ({exact}/{len(dists)} exact)"

    print(f"  {hyph:3} hyphen breaks{backs}")


def check_font(font_file):
    ns, font = load_engine(font_file)
    print(f"\n=== {font_file}  box_h={font.box_h} space_w={font.space_w} "
          f"lines/page={LINES_PER_PAGE} width={TEXT_WIDTH}px ===")

    # font metrics must be additive, or wrap/justify measurements drift
    assert font.text_width("") == 0
    assert font.text_width("ab") == font.char_width("a") + font.char_width("b")
    assert font.text_width("a b") == (font.char_width("a") + font.space_w
                                      + font.char_width("b"))

    books = make_corpus()
    for name in ("prose", "wrapped", "longwords", "large", "hyphenwords", "french"):
        check_book(ns, font, name, books[name])

    # the pathological token can't be drawn in full; we only require that
    # pagination terminates, stays in bounds, and recovers afterwards
    ns["text_file"] = books["monster"]
    pages = walk_pages(ns["paginate_text"], books["monster"])
    for _, _, lines, _ in pages:
        for ln in lines:
            assert font.text_width(ln.decode("utf-8")) <= TEXT_WIDTH
    assert "Recovery line after the monster." in " ".join(visible_words(pages)), \
        "did not recover after the over-long token"
    print(f"  {'monster':12} terminates in {len([p for p in pages if p[2]])} pages, "
          f"recovers afterwards")


def main():
    fonts = sys.argv[1:] or available_fonts()
    if not fonts:
        print("no .pf fonts found in circuitpython_version/")
        return 1
    for f in fonts:
        check_font(f)
    print("\nALL LAYOUT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
