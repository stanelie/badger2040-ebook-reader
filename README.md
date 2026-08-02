<img src="https://github.com/user-attachments/assets/fd823938-4715-4568-beb6-402af4e0cedd" width="300">

AI coded ebook reader for the badger2040.

Features :
- resume book where you left off
- fast display of the next page thanks to pre-buffering
- legible font better (to me) than the built-in fonts
- can convert an .epub file directly onboard to the .txt file format it can read, and saves the cover image alongside it
- ability to switch books (ebook file picker)
- displays battery status
- ebook progress bar

Installing (circuitpython version) :
- copy the contents of `circuitpython_version/` to the root of the CIRCUITPY drive, keeping
  `lib/` as `lib/`, and make a `/books` folder. Every dependency is in there - including
  `lib/adafruit_framebuf.mpy` and the `font5x8.bin` it needs - so nothing has to be downloaded
  separately.
- keep `adafruit_framebuf` in `/lib`, not at the drive root. CircuitPython searches the root
  first, so a `.py` copy there shadows the `.mpy` - and a `.py` is compiled into RAM at import
  while an `.mpy` is not, which on a board that is already a few KB short decides whether it
  boots. The unmodified upstream source is in `third_party/` for reference, deliberately not in
  the install.
- the board prints `boot: <n> bytes free` and names any missing data file at startup, which is
  the quickest way to spot a half-copied drive.

Usage :
- put .txt or .epub ebook file into /books folder of the badger2040
- selecting an .epub in the picker converts it on the spot, with a progress bar, then opens it
- button A brings up the file picker, up and down arrows to select book, button A again to choose book
- button UP for previous page, button DOWN for next page
- long press button A for full refresh (circuitpython version)

  Note : because there is very little space on the rp2040, not many .epub files can be stored on it, maybe just one, and the conversion will eat up more space for the extracted text

Converting an .epub (circuitpython version) :
- copy the .epub into /books over USB as usual
- connect to the REPL (Thonny works) and run :

  ```
  import epub_xtract
  epub_xtract.main()
  ```
- reset when it finishes, and the .txt is in the picker

  It frees the reader's memory before starting. Reaching the REPL stops code.py but does not
  release its globals - the page buffers, the 31KB of hyphenation patterns, the font, about
  60KB - and a chapter has to be decompressed whole (circuitpython has no streaming inflater),
  so that space is needed. Resetting afterwards rebuilds it all.

  Writing needs the filesystem, which the USB host normally owns - the reader never writes
  files, it keeps its position in NVRAM. On battery the converter takes it over by itself;
  while plugged in, hold button A while resetting so boot.py hands it over first.

I like it!

Case is here : https://cad.onshape.com/documents/814dd2a988145f0ed18b6efd/w/66507594fb6b5b6a70cee4f8/e/53ba6e3dcf1a4ae5db1cb3dd?renderMode=0&uiState=693158b53d70a686c43cbc0a
