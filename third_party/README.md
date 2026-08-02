# third_party

Upstream source, kept for reference and as a fallback. **Not** part of the
install - deliberately.

## adafruit_framebuf.py

Unmodified from
[Adafruit_CircuitPython_framebuf](https://github.com/adafruit/Adafruit_CircuitPython_framebuf)
(MIT, license alongside). What the reader installs is the compiled
`circuitpython_version/lib/adafruit_framebuf.mpy` instead, because a `.py` is
compiled into RAM at import and an `.mpy` is not - on a board that is already
a few KB short, that difference decides whether it boots.

Keeping this file out of `circuitpython_version/` is the point. CircuitPython
searches the drive root before `/lib`, so a copy of it at the root would
**shadow** the `.mpy` and quietly reinstate the RAM cost the `.mpy` avoids.

Use it only to read the source, or if you have no `.mpy` for your CircuitPython
version - in which case put it in `/lib`, not at the root.
