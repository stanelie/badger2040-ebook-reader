"""Minimal proportional 1-bit bitmap font renderer for the Badger reader.

Loads a `.pf` font (see build_literata.py for the format) as one small bytes
blob and blits glyphs into an adafruit_framebuf.FrameBuffer. Also provides pixel
width metrics so the layout engine can wrap/justify/hyphenate in pixels instead
of character counts.
"""


class PropFont:
    def __init__(self, path, min_space_ratio=0.30):
        d = open(path, "rb").read()
        if d[:4] != b"PFN1":
            raise ValueError("bad font file")
        self.d = d
        self.box_h = d[4]
        self.baseline = d[5]
        self.first = d[6]
        self.count = d[7]
        # The baked space advance can be tiny at small sizes, which makes packed
        # and justified lines run together. Enforce a visible minimum scaled to
        # the font height so it holds across sizes.
        self.space_w = max(d[8], round(self.box_h * min_space_ratio))
        self.rec0 = 9
        self.bmp0 = self.rec0 + self.count * 4
        self._qmark = ord("?")
        self._space_idx = ord(" ") - self.first

    def _rec(self, ch):
        idx = ord(ch) - self.first
        if idx < 0 or idx >= self.count:
            idx = self._qmark - self.first
        r = self.rec0 + idx * 4
        d = self.d
        adv = self.space_w if idx == self._space_idx else d[r]
        return adv, d[r + 1], self.bmp0 + (d[r + 2] | (d[r + 3] << 8))

    def char_width(self, ch):
        return self._rec(ch)[0]

    def text_width(self, s):
        w = 0
        for ch in s:
            w += self._rec(ch)[0]
        return w

    def draw(self, fb, s, x, y, color=1, extra_each=0, extra_first=0):
        """Blit `s` at (x, y) top-left. `extra_each` px is added to every space
        advance and `extra_first` more spaces get one extra px (for justified
        line filling). Returns the final pen x.

        Writes bytes straight into the framebuffer's buffer (MHMSB) instead of
        calling fb.pixel() per lit pixel - the latter is far too slow on the
        RP2040. Falls back to fb.pixel() if the buffer isn't exposed."""
        buf = getattr(fb, "buf", None)
        if buf is None:
            return self._draw_slow(fb, s, x, y, color, extra_each, extra_first)
        d = self.d
        box_h = self.box_h
        W = fb.width
        H = fb.height
        stride = getattr(fb, "stride", W)  # bits per row; == width for MHMSB
        first_n = extra_first
        for ch in s:
            adv, bw, off = self._rec(ch)
            rb = (bw + 7) // 8
            for ry in range(box_h):
                yy = y + ry
                if yy < 0 or yy >= H:
                    continue
                rowbyte = (yy * stride) >> 3
                base = off + ry * rb
                for cx in range(bw):
                    if d[base + (cx >> 3)] & (0x80 >> (cx & 7)):
                        xx = x + cx
                        if 0 <= xx < W:
                            bi = rowbyte + (xx >> 3)
                            mask = 0x80 >> (xx & 7)
                            if color:
                                buf[bi] |= mask
                            else:
                                buf[bi] &= ~mask & 0xFF
            x += adv
            if ch == " ":
                x += extra_each
                if first_n > 0:
                    x += 1
                    first_n -= 1
        return x

    def _draw_slow(self, fb, s, x, y, color, extra_each, extra_first):
        d = self.d
        box_h = self.box_h
        first_n = extra_first
        for ch in s:
            adv, bw, off = self._rec(ch)
            rb = (bw + 7) // 8
            for ry in range(box_h):
                base = off + ry * rb
                yy = y + ry
                for cx in range(bw):
                    if d[base + (cx >> 3)] & (0x80 >> (cx & 7)):
                        fb.pixel(x + cx, yy, color)
            x += adv
            if ch == " ":
                x += extra_each
                if first_n > 0:
                    x += 1
                    first_n -= 1
        return x

    def draw_justified(self, fb, s, x, y, color, target_width):
        """Draw `s` stretched to `target_width` px by widening its spaces."""
        spaces = s.count(" ")
        extra = target_width - self.text_width(s)
        if spaces == 0 or extra <= 0:
            return self.draw(fb, s, x, y, color)
        base = extra // spaces
        rem = extra - base * spaces
        return self.draw(fb, s, x, y, color, extra_each=base, extra_first=rem)
