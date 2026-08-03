# ------------------------------------------------------------
# code.py  -  the shim CircuitPython insists on
# ------------------------------------------------------------
# The reader itself is /.system/reader.py. It lives there so it can be shipped
# as a .mpy: CircuitPython compiles every .py it imports at every boot, and it
# will only accept source for the file it runs at startup - so the one thing
# that cannot be precompiled is whichever file is called code.py.
#
# Measured on this board, precompiling the modules imported at boot took the
# time before the first page from 1.19s to 0.35s and gave back 3.5KB of RAM.
# reader.py is 83KB of source, the largest single thing left being compiled.
#
# Keep this file small. Everything in it is compiled on every boot, for ever.
import sys

for _p in ("/.system", "/lib"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import reader        # noqa: F401  - importing it runs the reader
