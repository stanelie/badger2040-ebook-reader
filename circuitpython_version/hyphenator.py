"""On-device hyphenation (Frank Liang's algorithm, as used by TeX).

Memory-conscious for the RP2040: the ~4900 Knuth-Liang / ushyphmax patterns
live in `hyphen_patterns.txt` as a single sorted, newline-delimited blob that
is loaded once into one bytes object (~31 KB) and binary-searched in place -
no per-pattern Python objects, no big dict.

Patterns and exceptions are public domain (Knuth & Liang; ushyphmax by
Gerard D.C. Kuiken). Algorithm after Ned Batchelder's public-domain
hyphenate.py; this implementation reproduces its output exactly.
"""

_PATTERNS_PATH = "hyphen_patterns.txt"
_LETTERS_MAX = 9  # longest pattern key (letters incl. boundary dots)

# Words Knuth listed as exceptions (hyphen points precomputed).
EXCEPTIONS = {
    'associate': [0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0],
    'associates': [0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0],
    'declination': [0, 0, 0, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0],
    'obligatory': [0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0],
    'philanthropic': [0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0],
    'present': [0, 0, 0, 0, 0, 0, 0, 0, 0],
    'presents': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    'project': [0, 0, 0, 0, 0, 0, 0, 0, 0],
    'projects': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    'reciprocity': [0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0],
    'recognizance': [0, 0, 0, 1, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0],
    'reformation': [0, 0, 0, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0],
    'retribution': [0, 0, 0, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0],
    'table': [0, 0, 0, 1, 0, 0, 0],
}

_BLOB = None


def _load():
    global _BLOB
    if _BLOB is None:
        with open(_PATTERNS_PATH, "rb") as f:
            _BLOB = f.read()
    return _BLOB


def _cmp_key(blob, ls, le, key):
    """Compare the letters-only key of pattern line blob[ls:le] against `key`
    (bytes), lexicographically, without allocating. Returns -1, 0 or 1."""
    ki = 0
    klen = len(key)
    p = ls
    while p < le:
        c = blob[p]
        if 48 <= c <= 57:   # skip digits - they aren't part of the key
            p += 1
            continue
        if ki >= klen:
            return 1        # line key longer, key is a prefix of it
        kc = key[ki]
        if c != kc:
            return -1 if c < kc else 1
        ki += 1
        p += 1
    return 0 if ki == klen else -1


def _points_at(blob, ls, le):
    # digit vector of pattern blob[ls:le], e.g. b".ach4" -> [0, 0, 0, 0, 4]
    pts = [0]
    p = ls
    while p < le:
        c = blob[p]
        if 48 <= c <= 57:
            pts[-1] = c - 48
        else:
            pts.append(0)
        p += 1
    return pts


def _lookup(key):
    """Binary-search the sorted, newline-delimited blob for a pattern whose
    letters-only key equals `key` (bytes). Return its digit vector or None."""
    blob = _BLOB
    lo = 0
    hi = len(blob)
    while lo < hi:
        mid = (lo + hi) // 2
        ls = blob.rfind(b"\n", 0, mid) + 1
        le = blob.find(b"\n", mid)
        if le < 0:
            le = len(blob)
        c = _cmp_key(blob, ls, le, key)
        if c == 0:
            return _points_at(blob, ls, le)
        if c < 0:
            lo = le + 1
        else:
            hi = ls
    return None


def hyphenate(word):
    """Return `word` split into pieces at legal hyphenation points."""
    if len(word) <= 4:
        return [word]
    lw = word.lower()
    # only plain ASCII letters are handled; anything else is left whole
    for ch in lw:
        if not ("a" <= ch <= "z"):
            return [word]

    if lw in EXCEPTIONS:
        points = EXCEPTIONS[lw]
    else:
        _load()
        work = "." + lw + "."
        wb = work.encode("ascii")
        n = len(wb)
        points = [0] * (n + 1)
        for i in range(n):
            top = min(i + _LETTERS_MAX, n)
            for j in range(i + 1, top + 1):
                pts = _lookup(wb[i:j])
                if pts:
                    for k in range(len(pts)):
                        if points[i + k] < pts[k]:
                            points[i + k] = pts[k]
        # never break in the first two or last two letters
        points[1] = points[2] = points[-2] = points[-3] = 0

    pieces = [""]
    for c, p in zip(word, points[2:]):
        pieces[-1] += c
        if p % 2:
            pieces.append("")
    return pieces


def hyphenate_split(word, space_left):
    """Split `word` for line-wrapping: return (prefix, rest) where prefix + "-"
    fits within `space_left` characters and is the longest such legal break, or
    (None, None) if the word can't/shouldn't be hyphenated here."""
    if space_left < 3 or len(word) < 5:
        return None, None
    pieces = hyphenate(word)
    if len(pieces) < 2:
        return None, None
    best = None
    acc = ""
    for idx in range(len(pieces) - 1):
        acc += pieces[idx]
        if len(acc) + 1 <= space_left:  # acc + trailing "-"
            best = acc
        else:
            break
    if best is None:
        return None, None
    return best, word[len(best):]
