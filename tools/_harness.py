"""Shared plumbing for the offline test harnesses.

The reader runs on CircuitPython and code.py does hardware setup at import time
(board, displayio, the UC8151 panel), so it can't simply be imported on a
desktop. Instead we pull the pure-logic functions out of the real code.py with
`ast` and exec them in a namespace holding the handful of globals they use.
That keeps code.py the single source of truth - these tests always exercise the
code that actually ships, not a copy.

Requires only the standard library plus the project's own pure-Python modules
(propfont, hyphenator). No Pillow, no hardware.
"""
import ast
import os
import random
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
CPDIR = os.path.normpath(os.path.join(HERE, "..", "circuitpython_version"))
sys.path.insert(0, CPDIR)
# adafruit_framebuf is vendored here rather than in the install folder, so a
# copy of it never lands at the drive root and shadows lib/'s .mpy.
THIRD_PARTY = os.path.normpath(os.path.join(HERE, "..", "third_party"))
sys.path.insert(0, THIRD_PARTY)

import hyphenator  # noqa: E402  (needs CPDIR on the path first)
import propfont  # noqa: E402

hyphenator._PATTERNS_PATH = os.path.join(CPDIR, "hyphen_patterns.txt")

# Layout constants, mirroring code.py's CONFIG block.
TEXT_PADDING = 2
TEXT_TOP = 0
WIDTH = 296
HEIGHT = 128
TEXT_WIDTH = WIDTH - TEXT_PADDING * 2
LINES_PER_PAGE = 9

PAGE_HISTORY_SIZE = 10
INACTIVITY_TIMEOUT_DEFAULT = 300

# The functions worth testing offline: everything that decides what text lands
# on a page, plus the navigation state machine (which buffer holds which page).
# Anything touching the panel or the framebuffer is stubbed by the caller.
EXTRACT = (
    "paginate_text", "find_previous_page", "_pixel_chunks", "_paragraph_start",
    "clean_word",
    "history_push", "history_pop", "history_peek", "history_clear",
    "prerender_next", "prerender_prev",
    "nav_page_down", "nav_fast_advance", "nav_page_up",
    "check_inactivity", "state_save_current",
)


class _GC:
    """Stand-in for CircuitPython's gc module."""
    def collect(self):
        pass


def available_fonts():
    """Every .pf font shipped in circuitpython_version, in the order code.py
    offers them via the B button."""
    preferred = ["oldmono.pf", "literata.pf", "lexenddeca.pf"]
    present = [f for f in preferred if os.path.exists(os.path.join(CPDIR, f))]
    extra = sorted(f for f in os.listdir(CPDIR)
                   if f.endswith(".pf") and f not in preferred)
    return present + extra


def load_engine(font_file):
    """Extract the layout engine from the real code.py, bound to `font_file`.

    Returns (namespace, FONT). The namespace holds the live functions, so
    ns["paginate_text"] is literally the shipping implementation.
    """
    hyphenator._load()
    font = propfont.PropFont(os.path.join(CPDIR, font_file))

    src = open(os.path.join(CPDIR, "code.py")).read()
    ns = {
        "LINES_PER_PAGE": LINES_PER_PAGE,
        "TEXT_WIDTH": TEXT_WIDTH,
        "FONT": font,
        "gc": _GC(),
        "open": open,
        "text_file": None,          # set per-book by the caller
        "print": lambda *a, **k: None,   # silence the engine's error prints
        "hyphenator": hyphenator,
        "HYPHENATE": True,
        "_HYPHEN_OK": True,
        "PAGE_HISTORY_SIZE": PAGE_HISTORY_SIZE,
        "page_history": [],
    }
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in EXTRACT:
            exec(ast.get_source_segment(src, node), ns)

    missing = [n for n in EXTRACT if n not in ns]
    if missing:
        raise RuntimeError(f"could not extract {missing} from code.py")
    return ns, font


# ----------------------------------------------------------------- corpora
def make_corpus():
    """Write the test books to a fresh temp directory and return {name: path}.

    Self-contained on purpose: earlier versions of these harnesses read
    leftover files from a scratch directory and broke when the OS cleaned it.
    """
    d = tempfile.mkdtemp(prefix="badger-harness-")
    books = {}

    def write(name, text):
        p = os.path.join(d, name + ".txt")
        with open(p, "w") as f:
            f.write(text)
        books[name] = p

    # A: ordinary prose, each paragraph one long source line
    write("prose", (
        "It was the best of times, it was the worst of times, it was the age of "
        "wisdom, it was the age of foolishness, it was the epoch of belief, it was "
        "the epoch of incredulity, it was the season of Light, it was the season of "
        "Darkness, it was the spring of hope, it was the winter of despair.\n\n"
        "We had everything before us, we had nothing before us, we were all going "
        "direct to Heaven, we were all going direct the other way.\n"
    ) * 6)

    # B: hard-wrapped source (Gutenberg style) with smart quotes and dashes -
    # exercises paragraph joining and the unicode cleanup
    para = ("The quick brown fox jumps over the lazy dog while the "
            "“clever” cat—quietly—watches from the windowsill, "
            "thinking its own thoughts about mice and men and the passage of time.")
    write("wrapped", "\n".join(textwrap.fill(para, 68) for _ in range(8)) + "\n")

    # C: long-but-placeable tokens (URL, 50-char, 200-char) - must reconstruct
    write("longwords",
          "Visit https://example.com/some/really/long/path/that/keeps/going/and/going "
          "for details. Also " + "a" * 50 + " and " + "b" * 200 + " end.\n"
          "Normal sentence follows to ensure recovery after the long token works.\n")

    # D: pathological token longer than a whole page - must not hang or corrupt
    write("monster",
          "prefix " + "z" * 500 + " suffix\nRecovery line after the monster.\n")

    # E: large book, long hard-wrapped paragraphs - stresses back-navigation
    rng = random.Random(42)
    vocab = ("the quick brown fox jumps over a lazy dog while clever cats quietly "
             "watch mice and men consider time memory light darkness hope despair "
             "wisdom folly belief doubt spring winter heaven otherwise").split()
    paras = []
    for _ in range(40):
        words = [rng.choice(vocab) for _ in range(rng.randint(40, 160))]
        words[0] = words[0].capitalize()
        paras.append(textwrap.fill(" ".join(words) + ".", 72))
    write("large", "\n\n".join(paras) + "\n")

    # G: French - accented lowercase, folded accented capitals, guillemets, the
    # oe ligature and a non-breaking space, all of which have to survive
    # pagination and end up as characters the fonts can actually draw.
    write("french", (
        "Il était une fois, à Noël, une élève naïve qui rêvait d'écrire un "
        "chef-d'œuvre. « Où çà ? » demanda-t-elle, le cœur battant, déjà "
        "âgée. ÉCOLE, ÊTRE et ÎLE en majuscules. La forêt française était "
        "préférée des aînés — voilà ! Ça suffit : où ça ?\n\n"
        "Les naïfs élèves préféraient goûter des crêpes brûlées près du "
        "château, où l'aïeul räconte drôlement ses vieilles histoires.\n"
    ) * 4)

    # F: already-hyphenated words, packed so many land at line ends
    write("hyphenwords", (
        "The low-ceilinged mother-in-law suite had a well-worn state-of-the-art "
        "self-cleaning coffee-maker and an over-engineered twenty-three-year-old "
        "air-conditioning unit near the north-facing window-sill. ") * 6 + "\n")

    return books


def walk_pages(paginate_text, path, hyphenate=True, guard=100000):
    """Page through a whole book. Returns [(offset, remainder, lines, next_off)]."""
    pages = []
    offset, remainder = 0, b""
    size = os.stat(path)[6]
    steps = 0
    while True:
        steps += 1
        if steps > guard:
            raise AssertionError(f"pagination did not terminate in {path}")
        lines, next_off, next_rem = paginate_text(path, offset, remainder, hyphenate)
        pages.append((offset, remainder, lines, next_off))
        if not lines:
            break
        if next_off >= size and not next_rem:
            break
        if next_off == offset and next_rem == remainder:
            raise AssertionError(f"no forward progress at offset {offset} in {path}")
        if next_off < offset and not remainder:
            raise AssertionError(f"went backwards at offset {offset} in {path}")
        offset, remainder = next_off, next_rem
    return pages
