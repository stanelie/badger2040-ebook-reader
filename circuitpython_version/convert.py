# ------------------------------------------------------------
# convert.py  -  entry point for the EPUB converter
# ------------------------------------------------------------
# Runs the converter over the first .epub it finds in /books.
#
# Deliberately module-level, with no `if __name__ == "__main__"` guard: whether
# CircuitPython sets __name__ to "__main__" for the file it runs is not
# documented, and when it does not, a guarded file simply defines its functions
# and exits - looking exactly like nothing happened. Code at module level runs
# either way.
#
# From the REPL, with a clean heap (the reader stays unloaded):
#
#     import supervisor
#     supervisor.set_next_code_file("convert.py")
#     supervisor.reload()
#
# Or, if that does not run: copy this file over code.py, reset, and copy the
# reader back afterwards.
#
# Writing needs the filesystem, which the USB host normally owns. On battery
# the converter takes it over itself; while plugged in, hold A while resetting
# so boot.py hands it over first.
print("convert.py starting")

import epub_xtract

epub_xtract.main()

print("convert.py finished - reset to go back to the reader")
