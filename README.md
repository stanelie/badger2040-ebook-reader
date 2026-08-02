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
  the folders as they are, and make a `/books` folder. Every dependency is in there, so
  nothing has to be downloaded separately. Copy the hidden folders too - `cp -r` on macOS and
  Linux will not pick up `.system` and `.fonts` from a `*` glob.
- only `code.py` and `boot.py` sit at the root, because CircuitPython insists on finding them
  there. Everything else is in `/.system`, and the fonts in `/.fonts`, so a mounted CIRCUITPY
  shows your books rather than the machinery. macOS and Linux hide dot-folders; Windows uses a
  FAT attribute instead and will show them.
- `firmware/` holds the CircuitPython build and its patches. Those are for flashing the board,
  not for copying onto it.
- the unmodified upstream `adafruit_framebuf` source is in `third_party/` for reference,
  deliberately not in the install - a `.py` copy of it at the drive root would be compiled into
  RAM at every boot where the `.mpy` is not.
- the board prints `boot: <n> bytes free` and names any missing data file at startup, which is
  the quickest way to spot a half-copied drive.

Usage :
- put .txt or .epub ebook file into /books folder of the badger2040
- selecting an .epub in the picker converts it and opens the result. The board restarts into
  the converter, shows a progress bar, then restarts back into the reader
- the .epub is deleted once it has converted cleanly - the .txt and the cover replace it, and
  it is the largest file on the board. A conversion that failed part-way keeps its source, so
  it can be retried; set `DELETE_SOURCE_AFTER_CONVERT = False` in epub_xtract.py to always
  keep it
- button A brings up the file picker, up and down arrows to select book, button A again to choose book
- button UP for previous page, button DOWN for next page
- long press button A for full refresh (circuitpython version)
- when it sleeps it shows the book's cover, if that book has one, and says "Sleeping..."
  otherwise. The cover is turned a quarter-turn and scaled to fill the whole screen, so its
  outer edges are cropped - hold the reader sideways to look at it. `ROTATE_COVER = False`
  in coverimg.py leaves it upright, `FILL_SCREEN = False` fits it whole inside white margins
  instead of cropping. Covers are turned into a sleep frame at conversion time - for books converted
  before that existed, hold A while resetting and run `import coverimg; coverimg.main()`

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

  Choosing an .epub while plugged in therefore cannot convert it. The reader says so and
  leaves the book queued rather than restarting into a converter that could only refuse;
  unplugging restarts the board and runs it then.

I like it!

Case is here : https://cad.onshape.com/documents/814dd2a988145f0ed18b6efd/w/66507594fb6b5b6a70cee4f8/e/53ba6e3dcf1a4ae5db1cb3dd?renderMode=0&uiState=693158b53d70a686c43cbc0a
