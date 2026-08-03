# CircuitPython driver for the UC8151/IL0373 e-paper display.
# This is the e-paper type used in the Badger 2040.
#
# Ported from MicroPython to CircuitPython
# Original Copyright(C) 2024 Salvatore Sanfilippo <antirez@gmail.com>
# MIT license.

import time
import digitalio
import adafruit_framebuf

### Commands list.
# Commands are executed putting the DC line in command mode
# and sending the command as first byte, followed if needed by
# the data arguments (but with DC in data mode).

CMD_PSR      = 0x00
CMD_PWR      = 0x01
CMD_POF      = 0x02
CMD_PFS      = 0x03
CMD_PON      = 0x04
CMD_PMES     = 0x05
CMD_BTST     = 0x06
CMD_DSLP     = 0x07
CMD_DTM1     = 0x10
CMD_DSP      = 0x11
CMD_DRF      = 0x12
CMD_DTM2     = 0x13
CMD_LUT_VCOM = 0x20
CMD_LUT_WW   = 0x21
CMD_LUT_BW   = 0x22
CMD_LUT_WB   = 0x23
CMD_LUT_BB   = 0x24
CMD_PLL      = 0x30
CMD_TSC      = 0x40
CMD_TSE      = 0x41
CMD_TSR      = 0x43
CMD_TSW      = 0x42
CMD_CDI      = 0x50
CMD_LPD      = 0x51
CMD_TCON     = 0x60
CMD_TRES     = 0x61
CMD_REV      = 0x70
CMD_FLG      = 0x71
CMD_AMV      = 0x80
CMD_VV       = 0x81
CMD_VDCS     = 0x82
CMD_PTL      = 0x90
CMD_PTIN     = 0x91
CMD_PTOU     = 0x92
CMD_PGM      = 0xa0
CMD_APG      = 0xa1
CMD_ROTP     = 0xa2
CMD_CCSET    = 0xe0
CMD_PWS      = 0xe3
CMD_TSSET    = 0xe5

### Register values

# PSR
RES_96x230   = 0b00000000
RES_96x252   = 0b01000000
RES_128x296  = 0b10000000
RES_160x296  = 0b11000000
LUT_OTP      = 0b00000000
LUT_REG      = 0b00100000
FORMAT_BWR   = 0b00000000
FORMAT_BW    = 0b00010000
SCAN_DOWN    = 0b00000000
SCAN_UP      = 0b00001000
SHIFT_LEFT   = 0b00000000
SHIFT_RIGHT  = 0b00000100
BOOSTER_OFF  = 0b00000000
BOOSTER_ON   = 0b00000010
RESET_SOFT   = 0b00000000
RESET_NONE   = 0b00000001

# PWR
VDS_EXTERNAL = 0b00000000
VDS_INTERNAL = 0b00000010
VDG_EXTERNAL = 0b00000000
VDG_INTERNAL = 0b00000001
VCOM_VD      = 0b00000000
VCOM_VG      = 0b00000100
VGHL_16V     = 0b00000000
VGHL_15V     = 0b00000001
VGHL_14V     = 0b00000010
VGHL_13V     = 0b00000011

# BOOSTER
START_10MS = 0b00000000
START_20MS = 0b01000000
START_30MS = 0b10000000
START_40MS = 0b11000000
STRENGTH_1 = 0b00000000
STRENGTH_2 = 0b00001000
STRENGTH_3 = 0b00010000
STRENGTH_4 = 0b00011000
STRENGTH_5 = 0b00100000
STRENGTH_6 = 0b00101000
STRENGTH_7 = 0b00110000
STRENGTH_8 = 0b00111000
OFF_0_27US = 0b00000000
OFF_0_34US = 0b00000001
OFF_0_40US = 0b00000010
OFF_0_54US = 0b00000011
OFF_0_80US = 0b00000100
OFF_1_54US = 0b00000101
OFF_3_34US = 0b00000110
OFF_6_58US = 0b00000111

# PFS
FRAMES_1   = 0b00000000
FRAMES_2   = 0b00010000
FRAMES_3   = 0b00100000
FRAMES_4   = 0b00110000

# TSE
TEMP_INTERNAL = 0b00000000
TEMP_EXTERNAL = 0b10000000
OFFSET_0      = 0b00000000
OFFSET_1      = 0b00000001
OFFSET_2      = 0b00000010
OFFSET_3      = 0b00000011
OFFSET_4      = 0b00000100
OFFSET_5      = 0b00000101
OFFSET_6      = 0b00000110
OFFSET_7      = 0b00000111
OFFSET_MIN_8  = 0b00001000
OFFSET_MIN_7  = 0b00001001
OFFSET_MIN_6  = 0b00001010
OFFSET_MIN_5  = 0b00001011
OFFSET_MIN_4  = 0b00001100
OFFSET_MIN_3  = 0b00001101
OFFSET_MIN_2  = 0b00001110
OFFSET_MIN_1  = 0b00001111

# PLL flags
HZ_29      = 0b00111111
HZ_33      = 0b00111110
HZ_40      = 0b00111101
HZ_50      = 0b00111100
HZ_67      = 0b00111011
HZ_100     = 0b00111010
HZ_200     = 0b00111001

class UC8151:
    def __init__(self, spi, *, cs, dc, rst, busy, width=128, height=296, speed=0, 
                 mirror_x=False, mirror_y=False, inverted=False, no_flickering=False, 
                 debug=False, full_update_period=50, dangerous_reaffirm_black=False,
                 use_framebuf_font=False, font_path="/.fonts/font5x8.bin", rotation=0,
                 ui_font=None, buf=None):
        """
        Initialize the UC8151 e-ink display driver.
        
        Args:
            rotation: Display rotation in degrees (0, 90, 180, 270).
                     For Badger 2040, use rotation=270 to correct orientation.
            use_framebuf_font: If True, use adafruit_framebuf's font (requires font5x8.bin).
            font_path: Path to font5x8.bin file (only used if use_framebuf_font=True).
            buf: a framebuffer to draw into, instead of allocating one. The
                 caller almost certainly has a screen-sized buffer already, and
                 a second one is 4736 bytes of duplicate - claimed here, after
                 everything optional, which is the worst place to need it.
            ui_font: a propfont.PropFont for interface text. Preferred over
                     use_framebuf_font. The reader passes oldmono.pf opened
                     file-backed, which is the same typeface the old vga2_8x16
                     module held as 4KB of resident glyph data.
        """
        
        # First, try to deinitialize any existing display that might be using the pins
        self._release_existing_display()
        
        self.spi = spi
        self.use_framebuf_font = use_framebuf_font
        self.font_path = font_path
        self.rotation = rotation
        self.ui_font = ui_font
        
        # Store the original dimensions as physical dimensions
        self.physical_width = width
        self.physical_height = height

        # Reused scratch buffer for _rotate_framebuffer, allocated lazily on
        # first use instead of a fresh bytearray every call - this method runs
        # on every page render (twice per page turn: current + pre-rendered
        # next), so re-allocating its ~4.7KB output buffer each time added
        # needless churn to an already memory-constrained device.
        self._rotate_scratch = None

        # Reused 1-byte scratch for write()'s command byte (and the rare
        # single-int-data case) - every command sent to the panel otherwise
        # allocated a fresh bytes([cmd]) object just to hold one byte.
        self._cmd_buf = bytearray(1)

        # Gather buffer for update_partial(), grown on demand.
        self._partial_scratch = None

        # Set by update_partial(). A partial refresh only drives the pixels
        # inside its window, so afterwards the panel's idea of the previous
        # frame no longer matches the rest of the screen. In no-flickering mode
        # the waveform only moves pixels it believes have changed, so the next
        # FULL update would leave the old content showing through. The next
        # full update therefore has to use the flickering waveform, which
        # drives every pixel.
        self._stale_after_partial = False

        # Swap width/height for user's framebuffer if rotation is 90 or 270
        if rotation in (90, 270):
            self.width = height
            self.height = width
        else:
            self.width = width
            self.height = height
        
        # Setup GPIO pins using digitalio
        self.cs = digitalio.DigitalInOut(cs) if cs is not None else None
        if self.cs:
            self.cs.direction = digitalio.Direction.OUTPUT
            self.cs.value = True
            
        self.dc = digitalio.DigitalInOut(dc) if dc is not None else None
        if self.dc:
            self.dc.direction = digitalio.Direction.OUTPUT
            
        self.rst = digitalio.DigitalInOut(rst) if rst is not None else None
        if self.rst:
            self.rst.direction = digitalio.Direction.OUTPUT
            
        self.busy = digitalio.DigitalInOut(busy) if busy is not None else None
        if self.busy:
            self.busy.direction = digitalio.Direction.INPUT
        
        self.speed = speed
        self.no_flickering = no_flickering
        self.dangerous_reaffirm_black = dangerous_reaffirm_black
        self.inverted = inverted
        self.mirror_x = mirror_x
        self.mirror_y = mirror_y
        self.debug = debug
        
        self.initialize_display()
        
        # The framebuffer is always in the rotated orientation.
        #
        # Taking the caller's buffer when offered matters more than it looks:
        # this runs after every optional allocation the program has made, so a
        # board a few KB short fails here, on a buffer it already owns a copy
        # of - the reader's raw_working_buffer is the same size and the same
        # thing:
        #
        #     File "uc8151_circuitpython.py", line 242, in __init__
        #     MemoryError: memory allocation failed, allocating 4736 bytes
        need = self.width * self.height // 8
        if buf is not None and len(buf) == need:
            self.raw_fb = buf
        else:
            if buf is not None:
                print("display buffer is %d bytes, need %d - allocating"
                      % (len(buf), need))
            self.raw_fb = bytearray(need)
        self.fb = adafruit_framebuf.FrameBuffer(
            self.raw_fb, self.width, self.height, adafruit_framebuf.MHMSB
        )

        # Updates done with the current speed settings.
        self.update_count = 0

        # From time to time, if partial updates or no-flickering updates
        # are used, we perform a full update regardless, to remove ghosting,
        # make the background color more even and so forth.
        self.full_update_period = full_update_period
        
        # Quick update mode for faster response
        self.quick_update_mode = False
    
    def enable_quick_updates(self, enable=True):
        """Keep power on between updates for faster response (<500ms)"""
        self.quick_update_mode = enable
        if enable:
            self.write(CMD_PON)  # Power stays on

    def text(self, string, x, y, color=1):
        """
        Draw text using external font, terminalio font, or adafruit_framebuf font.
        
        Args:
            string: Text to display
            x, y: Position (top-left corner)
            color: 1 for black, 0 for white
        """
        if self.ui_font is not None:
            # A .pf bitmap font, the same format the reader's text uses. The
            # interface font used to be a separate Python module holding 4KB of
            # glyphs, which was the same typeface as oldmono.pf twice over.
            self.ui_font.draw(self.fb, string, x, y, color)
        elif self.use_framebuf_font:
            # adafruit_framebuf's own text(), from font5x8.bin. convert.py uses
            # this: BitmapFont seeks the file per character and holds almost no
            # RAM, which matters more there than drawing speed does.
            try:
                self.fb.text(string, x, y, color, font_name=self.font_path)
            except Exception as e:
                print(f"Error loading framebuf font: {e}")
                print("Make sure font5x8.bin is in the correct location.")
        else:
            raise RuntimeError("no font: pass ui_font or use_framebuf_font")
    
        
    
    

    def _release_existing_display(self):
        """Try to release any existing display that might be using the pins."""
        try:
            import board
            if hasattr(board, 'DISPLAY') and board.DISPLAY is not None:
                try:
                    board.DISPLAY.deinit()
                except:
                    pass
        except:
            pass

    # Return true if the display is busy performing an update, or also
    # if for any other reason it is not able to accept commands right now.
    def is_busy(self):
        if self.busy is None:
            return False
        return not self.busy.value  # Low on busy condition.

    def wait_ready(self):
        if self.busy is None:
            return
        while self.is_busy():
            pass

    # Perform hardware reset.
    def reset(self):
        if self.rst is None:
            return
        self.rst.value = False
        time.sleep(0.01)
        self.rst.value = True
        time.sleep(0.01)
        self.wait_ready()

    # Send just a command, just data, or a command + data, depending
    # on cmd or data being both bytes() / bytearrays() or None.
    def write(self, cmd=None, data=None):
        self.wait_ready()
        
        # Lock SPI for this transaction
        while not self.spi.try_lock():
            pass
        
        try:
            if self.cs:
                self.cs.value = False
            if self.dc:
                self.dc.value = False  # Command mode
            
            # Write command (reused 1-byte buffer instead of a fresh bytes([cmd])
            # allocation - this runs for every single command sent to the panel)
            self._cmd_buf[0] = cmd
            self.spi.write(self._cmd_buf)

            if data:
                if isinstance(data, int):
                    self._cmd_buf[0] = data
                    data = self._cmd_buf
                if isinstance(data, list):
                    data = bytes(data)
                if self.dc:
                    self.dc.value = True  # Data mode
                self.spi.write(data)
            
            if self.cs:
                self.cs.value = True
        finally:
            # Always unlock SPI
            self.spi.unlock()

    # This function sets the PSR register, a key register to
    # set up the panel configuration. We call this function each
    # time a new speed / LUTs are configured, because when we
    # revert to the default LUTs (speed 0) the PSR register
    # must be set to look into the internal tables.
    def set_panel_configuration(self):
        # Panel configuration: resolution, format and so forth.
        psr_settings = FORMAT_BW | BOOSTER_ON | RESET_NONE

        # Use physical dimensions for the hardware configuration
        if self.physical_width == 96 and self.physical_height == 230:
            psr_settings |= RES_96x230
        elif self.physical_width == 96 and self.physical_height == 252:
            psr_settings |= RES_96x252
        elif self.physical_width == 128 and self.physical_height == 296:
            psr_settings |= RES_128x296
        elif self.physical_width == 160 and self.physical_height == 296:
            psr_settings |= RES_160x296
        else:
            raise ValueError("Unsupported display resolution specified")

        # Configure mirroring (same for all rotations)
        psr_settings |= SHIFT_LEFT if self.mirror_x else SHIFT_RIGHT
        psr_settings |= SCAN_DOWN if self.mirror_y else SCAN_UP

        # If we select the default update speed, we will use the
        # lookup tables defined by the device. Otherwise the values for
        # the lookup tables must be read from the registers we set.
        if self.speed == 0:
            psr_settings |= LUT_OTP
        else:
            psr_settings |= LUT_REG

        self.write(CMD_PSR, psr_settings)

    def initialize_display(self):
        self.reset()

        # Soft reset
        self.write(CMD_PSR, RESET_SOFT)

        # Here we set the voltage levels that are used for the low-high
        # transitions states, driven by the waveforms provided in the
        # lookup tables for refresh.
        self.write(CMD_PWR, [
            VDS_INTERNAL | VDG_INTERNAL,
            VCOM_VD | VGHL_16V,
            0b100110,  # +10v VDH
            0b100110,  # -10v VDL
            0b000011   # VDHR default (For red pixels, not used here)
        ])

        # Set the lookup tables depending on the speed.
        self.set_waveform_lut()

        # Booster soft start configuration.
        self.write(CMD_BTST, [
            START_10MS | STRENGTH_3 | OFF_6_58US,
            START_10MS | STRENGTH_3 | OFF_6_58US,
            START_10MS | STRENGTH_3 | OFF_6_58US
        ])

        # Power on
        self.write(CMD_PON)

        # Setup the panel configuration
        self.set_panel_configuration()

        # Setup the duration (in frames) for the discharge executed for
        # power-off.
        self.write(CMD_PFS, FRAMES_4)

        # Use the internal temperature sensor.
        self.write(CMD_TSE, TEMP_INTERNAL | OFFSET_0)

        # Set non overlapping period for Gate and Source lines.
        self.write(CMD_TCON, 0x22)

        # VCOM data and interval settings.
        self.write(CMD_CDI, 0b11_01_1100 if self.inverted else 0b11_00_1100)

        # PLL clock frequency.
        self.write(CMD_PLL, HZ_100)

        # Power off the display.
        self.write(CMD_POF)

    def set_waveform_lut(self, speed=None, no_flickering=None):
        if speed is None:
            speed = self.speed
        if no_flickering is None:
            no_flickering = self.no_flickering

        if speed < 1:
            return

        if speed > 6:
            raise ValueError("Speed must be set between 0 and 6")

        # Create the LUTs to fill with the computed values.
        VCOM = bytearray(44)
        BW = bytearray(42)
        WB = bytearray(42)
        WW = bytearray(42)
        BB = bytearray(42)

        # Those periods are powers of two
        period = 64
        hperiod = period // 2
        
        # Actual period is scaled by the speed factor
        period = max(int(period / (2 ** (speed - 1))), 1)
        hperiod = max(int(hperiod / (2 ** (speed - 1))), 1)

        if speed <= 3 and not no_flickering:
            # For low speed everything is charge-neutral

            # Phase 1: long go-inverted-color.
            self.set_lut_row(VCOM, 0, pat=0, dur=[period, 0, 0, 0], rep=2)
            self.set_lut_row(BW, 0, pat=0b01_000000, dur=[period, 0, 0, 0], rep=2)
            self.set_lut_row(WB, 0, pat=0b10_000000, dur=[period, 0, 0, 0], rep=2)

            # Phase 2: short ping/pong.
            self.set_lut_row(VCOM, 1, pat=0, dur=[hperiod, hperiod, 0, 0], rep=2)
            self.set_lut_row(BW, 1, pat=0b10_01_0000, dur=[hperiod, hperiod, 0, 0], rep=1)
            self.set_lut_row(WB, 1, pat=0b01_10_0000, dur=[hperiod, hperiod, 0, 0], rep=1)

            # Phase 3: long go-target-color.
            self.set_lut_row(VCOM, 2, pat=0, dur=[period, 0, 0, 0], rep=2)
            self.set_lut_row(BW, 2, pat=0b10_000000, dur=[period, 0, 0, 0], rep=2)
            self.set_lut_row(WB, 2, pat=0b01_000000, dur=[period, 0, 0, 0], rep=2)

            WW[:] = BW[:]
            BB[:] = WB[:]
        else:  # Speed > 3
            # Phase 1
            p = period
            self.set_lut_row(VCOM, 0, pat=0, dur=[p, p, p, p], rep=1)
            self.set_lut_row(BW, 0, pat=0b10_00_00_00, dur=[p * 4, 0, 0, 0], rep=1)
            self.set_lut_row(WB, 0, pat=0b01_00_00_00, dur=[p * 4, 0, 0, 0], rep=1)
            self.set_lut_row(WW, 0, pat=0b01_10_00_00, dur=[p * 2, p * 2, 0, 0], rep=1)
            self.set_lut_row(BB, 0, pat=0b10_01_00_00, dur=[p * 2, p * 2, 0, 0], rep=1)

        # If no flickering mode is enabled, use empty waveform for BB and WW
        if no_flickering:
            self.clear_lut(WW)
            self.clear_lut(BB)
            if self.dangerous_reaffirm_black:
                self.set_lut_row(BB, 0, pat=0b10_01_10_01, dur=[0, 2, 0, 0], rep=1)

        if self.debug:
            print(f"LUTs for speed {speed} no_flickering {no_flickering}:")
            self.show_lut(BW, "BW")
            self.show_lut(WB, "WB")
            self.show_lut(WW, "WW")
            self.show_lut(BB, "BB")

        # Set the LUTs into the display registers.
        self.write(CMD_LUT_VCOM, VCOM)
        self.write(CMD_LUT_BW, BW)
        self.write(CMD_LUT_WB, WB)
        self.write(CMD_LUT_WW, WW)
        self.write(CMD_LUT_BB, BB)

    def set_speed(self, new_speed, *, no_flickering=None, full_update_period=None):
        if no_flickering is not None:
            self.no_flickering = no_flickering
        if full_update_period is not None:
            self.full_update_period = full_update_period
        self.speed = new_speed
        self.set_panel_configuration()
        self.set_waveform_lut()
        self.update_count = 0

    def set_lut_row(self, lut, row, pat, dur, rep):
        if row > 6:
            raise ValueError("LUTs have 7 total rows (0-6)")
        off = 6 * row
        lut[off] = pat
        lut[off + 1] = dur[0]
        lut[off + 2] = dur[1]
        lut[off + 3] = dur[2]
        lut[off + 4] = dur[3]
        lut[off + 5] = rep

    def clear_lut(self, lut):
        for i in range(len(lut)):
            lut[i] = 0

    def show_lut(self, lut, name):
        print(name, ":")
        for i in range(7):
            if i > 0 and lut[i * 6] == 0:
                break
            print(bin(lut[i * 6] | 256)[3:], end=' ')
            for j in range(1, 6):
                print(hex(lut[i * 6 + j]), end=' ')
            print("")
        print("---")

    def wait_and_switch_off(self):
        self.wait_ready()
        self.write(CMD_POF)

    def update(self, blocking=True, fb=None):
        if fb is None:
            fb = self.raw_fb
        if not blocking and self.is_busy():
            return False

        # Periodic full refresh for no-flickering mode
        do_full_update = (self.full_update_period != 0 and
                         self.update_count % self.full_update_period == 0 and
                         self.no_flickering)

        # First full update after any partial one: the untouched pixels are not
        # where the panel thinks they are, so drive all of them or the previous
        # screen bleeds through (leaving the book picker visible under a page).
        if self._stale_after_partial:
            if self.no_flickering:
                do_full_update = True
            self._stale_after_partial = False

        if do_full_update:
            self.set_waveform_lut(min(2, self.speed), False)

        self.send_image(fb)
        self.write(CMD_DRF)  # Start refresh cycle.

        # Load back the no-flickering LUTs if we forced a flickered refresh.
        if do_full_update:
            self.set_waveform_lut()

        if blocking:
            self.wait_ready()
            # Only power off if NOT in quick mode
            if not self.quick_update_mode:
                self.write(CMD_POF)
        self.update_count += 1
        return True

    def update_partial(self, x, y, w, h, fb=None, pre_rotated=False):
        """Refresh only a rectangle of the display, in LOGICAL coordinates
        (the same 296x128 space text is drawn in).

        Only the pixels inside the window are driven, so this is much quicker
        than a full refresh and leaves the rest of the screen untouched.

        `y` and `h` are snapped outward to multiples of 8: the panel addresses
        that axis in 8-pixel banks. `x`/`w` are exact.

        The window is sent in the panel's own orientation. With rotation 270 a
        logical horizontal band becomes a vertical strip on the panel, and our
        rotated buffer is already laid out the way the panel wants it
        (index = position_along_296 * 16 + bank), so the region is gathered
        with plain slices.

        Returns False if the request is empty or the geometry is unsupported,
        in which case the caller should fall back to a full update.
        """
        if self.rotation != 270:
            return False            # mapping below assumes the Badger's rotation

        if fb is None:
            fb = self.raw_fb

        # Clip to the screen
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(self.width, x + w)
        y1 = min(self.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return False

        # Snap the banked axis outward to whole 8-pixel banks
        y0 &= ~7
        y1 = (y1 + 7) & ~7
        if y1 > self.height:
            y1 = self.height

        # Logical -> panel. The 128 axis is the logical y; the 296 axis runs
        # backwards from the logical x (new_y = 295 - x), so the window start
        # comes from the FAR edge of the logical rectangle.
        py = y0
        ph = y1 - y0
        px = self.physical_height - x1
        pw = x1 - x0

        cols = ph >> 3                      # bytes per strip (128 axis)
        bank = py >> 3
        row_bytes = self.physical_width >> 3   # 16

        # One contiguous buffer, so the window goes out as a single SPI
        # transfer instead of `pw` small ones.
        need = pw * cols
        if self._partial_scratch is None or len(self._partial_scratch) < need:
            self._partial_scratch = bytearray(need)
        out = self._partial_scratch

        if pre_rotated or self.rotation == 0:
            # Caller supplied a buffer already in panel orientation; the window
            # is a strided gather out of it.
            k = 0
            for dx in range(pw):
                start = (px + dx) * row_bytes + bank
                out[k:k + cols] = fb[start:start + cols]
                k += cols
        else:
            # Rotate ONLY this band, straight into the window buffer. Rotating
            # the whole screen just to copy a slice out of it is the dominant
            # cost of a small update - a band a third of the screen high costs
            # about a third as much.
            for i in range(need):
                out[i] = 0
            src_row_bytes = self.width >> 3
            for ly in range(y0, y1):
                row_base = ly * src_row_bytes
                bank_off = (ly >> 3) - bank
                mask = 0x80 >> (ly & 7)          # constant for the row
                for bx in range(x0 >> 3, (x1 + 7) >> 3):
                    byte_val = fb[row_base + bx]
                    if not byte_val:
                        continue                  # blank byte
                    base_x = bx << 3
                    # Stepping one pixel right in logical x steps one whole
                    # strip back in the window, so this is a subtraction.
                    idx = (x1 - 1 - base_x) * cols + bank_off
                    for bit in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01):
                        if byte_val & bit:
                            if 0 <= idx < need:
                                out[idx] |= mask
                        idx -= cols

        window = bytes([
            py & 0xFF,                      # bank axis start (pixels, x8)
            (y1 - 1) & 0xFF,                # bank axis end, inclusive
            (px >> 8) & 0xFF, px & 0xFF,    # long axis start
            ((px + pw - 1) >> 8) & 0xFF, (px + pw - 1) & 0xFF,
            0x01,                           # PT_SCAN
        ])

        self.wait_ready()
        self.write(CMD_PON)
        self.write(CMD_PTIN)                # enter partial mode
        self.write(CMD_PTL, window)
        self.write(CMD_DTM2, memoryview(out)[:need])
        self.write(CMD_DSP)
        self.write(CMD_DRF)
        self.wait_ready()

        # Leave partial mode. send_image() skips PTOU while quick updates are
        # enabled, so without this a later full refresh would still be confined
        # to this window.
        self.write(CMD_PTOU)
        if not self.quick_update_mode:
            self.write(CMD_POF)

        # Only this window was driven, so the next full update must repaint
        # everything rather than trust the panel's previous frame.
        self._stale_after_partial = True

        self.update_count += 1
        return True

    def ensure_scratch(self):
        """Allocate the rotation buffer now, and return it.

        Worth calling at startup, because this buffer is not optional: with a
        rotated display nothing reaches the panel without it. Left to allocate
        itself on first use, it is asked for after every optional buffer has
        already taken its share, and comes last in a queue it should be at the
        front of - a board a few KB short then boots, draws the picker, and dies
        inside the rotation with nothing drawn.
        """
        size = self.physical_width * self.physical_height // 8
        if self._rotate_scratch is None or len(self._rotate_scratch) != size:
            self._rotate_scratch = bytearray(size)
        return self._rotate_scratch

    def _rotate_framebuffer(self, fb):
        """
        Rotate the framebuffer - optimized for 270° rotation.
        For 270°: processes in chunks for better performance.

        Reuses a persistent scratch buffer (self._rotate_scratch) instead of
        allocating a fresh ~4.7KB bytearray every call. This method runs on
        every page render (twice per page turn: current + pre-rendered next),
        so the repeated allocation was a source of memory churn. Safe to
        share one buffer across calls: every caller synchronously copies out
        or transmits (via spi.write(), itself a blocking call) the returned
        buffer's contents before this method can be called again - this is a
        single-threaded, cooperative runtime with no background DMA left
        in flight after spi.write() returns.
        """
        if self.rotation == 0:
            return fb

        size = self.physical_width * self.physical_height // 8
        if self._rotate_scratch is None or len(self._rotate_scratch) != size:
            self._rotate_scratch = self.ensure_scratch()
        rotated = self._rotate_scratch
        for i in range(size):
            rotated[i] = 0

        if self.rotation == 270:
            # 270 degree rotation: (x, y) -> (y, width - 1 - x)
            #
            # This runs on every page render and every picker redraw, and it is
            # the slowest pure-Python loop in the project, so the per-pixel work
            # is kept to an OR and a subtraction. When both widths are a
            # multiple of 8 (they are on the Badger: 296 and 128) the addressing
            # collapses nicely:
            #
            #   dst_idx  = (width-1-src_x) * physical_width + src_y
            #   dst_byte = (width-1-src_x) * (physical_width//8) + src_y//8
            #   dst_bit  = 7 - (src_y & 7)
            #
            # physical_width*n is a multiple of 8, so the destination BIT
            # depends only on src_y - it is constant for a whole source row.
            # And stepping one pixel right in the source steps one whole row
            # down in the destination, so the destination byte index just
            # decreases by a fixed stride. That removes the per-pixel divide,
            # modulo and multiply the previous version did.
            width = self.width
            height = self.height
            phys_w = self.physical_width

            if width % 8 == 0 and phys_w % 8 == 0:
                src_row_bytes = width >> 3
                dst_stride = phys_w >> 3
                last_col = (width - 1) * dst_stride
                # Steps of one, two, three and four destination rows. Hoisted
                # because they are used once per inked byte and multiplying
                # inside the loop was measurable.
                ds1 = dst_stride
                ds2 = ds1 + ds1
                ds3 = ds2 + ds1
                ds4 = ds2 + ds2

                for src_y in range(height):
                    row_base = src_y * src_row_bytes
                    mask = 0x80 >> (src_y & 7)          # constant for the row
                    base0 = last_col + (src_y >> 3)

                    for bx in range(src_row_bytes):
                        byte_val = fb[row_base + bx]
                        if not byte_val:
                            continue                     # skip blank bytes
                        base = base0 - (bx << 3) * dst_stride
                        # Tested a nibble at a time rather than walking all
                        # eight bits. A page of text averages 2.6 lit pixels in
                        # the bytes that have any, so most half-bytes are empty
                        # and this skips them four at a time - a third off the
                        # rotation, which is the largest part of drawing a page.
                        # A 256-entry table of set-bit positions was marginally
                        # quicker still and cost thousands of bytes to hold.
                        if byte_val & 0xF0:
                            if byte_val & 0x80:
                                rotated[base] |= mask
                            if byte_val & 0x40:
                                rotated[base - ds1] |= mask
                            if byte_val & 0x20:
                                rotated[base - ds2] |= mask
                            if byte_val & 0x10:
                                rotated[base - ds3] |= mask
                        if byte_val & 0x0F:
                            base -= ds4
                            if byte_val & 0x08:
                                rotated[base] |= mask
                            if byte_val & 0x04:
                                rotated[base - ds1] |= mask
                            if byte_val & 0x02:
                                rotated[base - ds2] |= mask
                            if byte_val & 0x01:
                                rotated[base - ds3] |= mask
                return rotated

            # General case (dimensions not byte-aligned)
            for src_byte_idx in range(len(fb)):
                byte_val = fb[src_byte_idx]
                if byte_val == 0:
                    continue  # Skip empty bytes

                src_pixel_idx = src_byte_idx * 8
                src_y = src_pixel_idx // width
                src_x_start = src_pixel_idx % width

                for bit in range(8):
                    if byte_val & (1 << (7 - bit)):
                        src_x = src_x_start + bit
                        if src_x >= width:
                            continue

                        dst_idx = (width - 1 - src_x) * phys_w + src_y
                        rotated[dst_idx >> 3] |= (1 << (7 - (dst_idx & 7)))
            
            return rotated
        
        # Fallback for other rotations (90, 180) - `rotated` (the shared,
        # already-zeroed scratch buffer) is set up above.
        fb_view = memoryview(fb)
        rot_view = memoryview(rotated)
        
        if self.rotation == 90:
            for y in range(self.height):
                for x in range(self.width):
                    src_idx = y * self.width + x
                    src_byte = src_idx >> 3
                    src_bit = 7 - (src_idx & 7)
                    
                    if fb_view[src_byte] & (1 << src_bit):
                        new_x = self.height - 1 - y
                        new_y = x
                        dst_idx = new_y * self.physical_width + new_x
                        dst_byte = dst_idx >> 3
                        dst_bit = 7 - (dst_idx & 7)
                        rot_view[dst_byte] |= (1 << dst_bit)
        
        elif self.rotation == 180:
            for y in range(self.height):
                for x in range(self.width):
                    src_idx = y * self.width + x
                    src_byte = src_idx >> 3
                    src_bit = 7 - (src_idx & 7)
                    
                    if fb_view[src_byte] & (1 << src_bit):
                        new_x = self.width - 1 - x
                        new_y = self.height - 1 - y
                        dst_idx = new_y * self.physical_width + new_x
                        dst_byte = dst_idx >> 3
                        dst_bit = 7 - (dst_idx & 7)
                        rot_view[dst_byte] |= (1 << dst_bit)
        
        return rotated

    def send_image(self, fb, old=False):
        # Rotate the framebuffer if needed
        if self.rotation != 0:
            fb = self._rotate_framebuffer(fb)
        
        # Only power on if not in quick mode
        if not self.quick_update_mode:
            self.write(CMD_PON)
        
        # Skip PTOU in quick mode
        if not self.quick_update_mode:
            self.write(CMD_PTOU)
        
        if old:
            self.write(CMD_DTM1, fb)
        else:
            self.write(CMD_DTM2, fb)
        self.write(CMD_DSP)

    def set_pixels_for_greyscale(self, grey, fb1, fb2, width, height, shift, level):
        """Helper function to render greyscale images."""
        count = width * height
        anypixel = False
        
        for i in range(count // 8):
            fb1[i] = 0
            fb2[i] = 0

        for i in range(count):
            byte = i >> 3
            bit = 1 << (7 - (i & 7))

            # Invert and rescale
            converted = (255 - grey[i]) >> shift
            if converted == level:  # WW condition
                anypixel = True
            elif converted == level + 1:  # BB condition
                anypixel = True
                fb1[byte] |= bit
                fb2[byte] |= bit
            elif converted == level + 2:  # WB condition
                anypixel = True
                fb1[byte] |= bit
            else:  # BW condition, pixels not touched.
                fb2[byte] |= bit
        return anypixel

    def load_greyscale_image(self, filename, greyscale=16):
        """Load and render a greyscale image from file."""
        with open(filename, "rb") as f:
            f.read(4)  # Skip header
            imgdata = bytearray(self.width * self.height)
            f.readinto(imgdata)
            print("Image max luminance:", max(imgdata))
            self.update_greyscale(imgdata, greyscale)

    def update_greyscale(self, buffer, greyscale):
        """Update the display in greyscale mode."""
        greyscales = [32, 16, 8, 4]
        frames_to_black = 32

        if greyscale not in greyscales:
            raise ValueError("Unsupported greyscale")

        shift = 3 + greyscales.index(greyscale)

        # Prepare the display
        orig_speed = self.speed
        orig_no_flickering = self.no_flickering

        self.set_speed(2, no_flickering=True)
        self.fb.fill(0)
        self.update(blocking=True)  # All screen white

        # Setup LUTs
        LUT = bytearray(42)
        VCOM = bytearray(44)

        fb2 = bytearray(self.width * self.height // 8)
        for g in range(0, greyscale, 3):
            anypixel = self.set_pixels_for_greyscale(
                buffer, self.raw_fb, fb2, self.width, self.height, shift, g + 1
            )
            
            if anypixel:
                self.send_image(fb2, old=True)

                # Set LUTs for this grey level
                LUT[0] = 0x55
                LUT[5] = 1
                LUT[1] = int(frames_to_black / (greyscale - 1) * (g + 1))
                self.write(CMD_LUT_WW, LUT)
                LUT[1] = int(frames_to_black / (greyscale - 1) * (g + 2))
                self.write(CMD_LUT_BB, LUT)
                LUT[1] = int(frames_to_black / (greyscale - 1) * (g + 3))
                self.write(CMD_LUT_WB, LUT)
                LUT[1] = 0
                LUT[5] = 0
                self.write(CMD_LUT_BW, LUT)

                VCOM[0] = 0
                VCOM[1] = int(frames_to_black / greyscale * (g + 3))
                VCOM[5] = 1
                self.write(CMD_LUT_VCOM, VCOM)

                self.update(blocking=True)

        # Restore normal LUT
        self.set_speed(orig_speed, no_flickering=orig_no_flickering)
        self.wait_and_switch_off()


# Example usage for Badger2040
if __name__ == "__main__":
    import board
    import displayio
    
    # CRITICAL: Release the built-in display first!
    displayio.release_displays()
    
    # Use the board's built-in SPI - no need to configure if already initialized
    spi = board.SPI()
    
    # Create display instance using Badger 2040's predefined pins
    eink = UC8151(
        spi,
        cs=board.INKY_CS,
        dc=board.INKY_DC,
        rst=board.INKY_RST,
        busy=board.INKY_BUSY,
        speed=2,
        no_flickering=False
    )
    
    # Draw some test content
    eink.fb.fill(0)
    eink.text("Hello CircuitPython!", 10, 10, 1)  # Use eink.text() instead of eink.fb.text()
    eink.fb.rect(10, 30, 100, 50, 1)
    eink.update(blocking=True)
    
    print("Display updated!")
