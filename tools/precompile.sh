#!/bin/sh
# ------------------------------------------------------------
# precompile.sh  -  build an install tree with .mpy modules
# ------------------------------------------------------------
# CircuitPython compiles every .py it imports, at every boot. That is 138KB of
# source on this project, and the compiling happens before code.py's own timing
# marks can see it. An .mpy is compiled already, so it only has to be loaded.
#
#     tools/precompile.sh          build/ from circuitpython_version/
#     tools/precompile.sh --check  say what would be built, change nothing
#
# Then copy build/ to the board, exactly as you would copy the source tree:
#
#     cp -R build/. /Volumes/CIRCUITPY/
#
# The source tree is never modified. build/ holds .mpy in place of the .py it
# compiled, and everything else copied through - fonts, patterns, boot.py, and
# code.py itself, which CircuitPython will only accept as source.
#
# mpy-cross must match the firmware's mpy format, not its version string. Check
# with `tools/mpy-cross --version` against the first four bytes of an .mpy the
# board already loads: 43 06 00 1f here, for mpy v6.3.
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
SRC="$ROOT/circuitpython_version"
OUT="$ROOT/build"
MPY="$HERE/mpy-cross"

# Everything imported at boot or during a conversion. convert.py is absent for
# the same reason as code.py: it is run AS a code file, by set_next_code_file,
# and CircuitPython wants source for those.
#
# code.py is not here and cannot be: CircuitPython looks for code.py as source
# and will not run a code.mpy. That is why the reader itself is reader.py, in
# .system, with code.py a five-line shim that imports it - so the 83KB that
# used to be compiled at every boot is precompiled like everything else.
MODULES="reader uc8151_circuitpython propfont hyphenator inflate uzipfile epub_xtract convert_ui coverimg factory"

if [ ! -x "$MPY" ]; then
    echo "mpy-cross not found at $MPY" >&2
    echo "Download the build matching your firmware's mpy version from" >&2
    echo "  https://adafruit-circuit-python.s3.amazonaws.com/index.html?prefix=bin/mpy-cross/" >&2
    exit 1
fi

if [ "$1" = "--check" ]; then
    echo "would compile, into $OUT:"
    for m in $MODULES; do
        [ -f "$SRC/.system/$m.py" ] && printf "  %-28s %8s bytes\n" "$m.py" "$(wc -c < "$SRC/.system/$m.py")"
    done
    echo "would copy through: code.py, boot.py, .fonts/, lib/, and any .py not listed"
    exit 0
fi

rm -rf "$OUT"
mkdir -p "$OUT"
# Everything first, then replace what compiles. Copying first means a module
# that is not in the list, or that fails to compile, still ships as source.
cp -R "$SRC/." "$OUT/"
find "$OUT" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

saved=0
for m in $MODULES; do
    src="$SRC/.system/$m.py"
    [ -f "$src" ] || continue
    if "$MPY" -o "$OUT/.system/$m.mpy" "$src"; then
        rm -f "$OUT/.system/$m.py"
        before=$(wc -c < "$src")
        after=$(wc -c < "$OUT/.system/$m.mpy")
        saved=$((saved + before - after))
        printf "  %-28s %7s -> %7s\n" "$m" "$before" "$after"
    else
        echo "  $m: FAILED to compile, shipping as source" >&2
    fi
done

# A stamp, so "is the board running what I just edited" is answerable. Getting
# that wrong has cost this project a debugging cycle before, when a cached
# import meant the old code was still running and the logs looked identical.
{
    echo "built:  $(date '+%Y-%m-%d %H:%M:%S')"
    echo "commit: $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)$(git -C "$ROOT" diff --quiet 2>/dev/null || echo '+dirty')"
    newest=$(find "$SRC" -name '*.py' -print0 | xargs -0 ls -t 2>/dev/null | head -1)
    echo "newest source: $(basename "$newest") $(date -r "$newest" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)"
    echo "compiled: $MODULES"
} > "$OUT/.system/BUILD_STAMP.txt"

echo ""
echo "  stamped $(head -1 "$OUT/.system/BUILD_STAMP.txt" | cut -d' ' -f2-)"
echo "  $(($saved / 1024))KB less to compile at every boot"
echo "  build/ is ready:  cp -R $OUT/. /Volumes/CIRCUITPY/"
