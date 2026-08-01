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

Usage :
- put .txt or .epub ebook file into /books folder of the badger2040
- button A brings up the file picker, up and down arrows to select book, button A again to choose book
- button UP for previous page, button DOWN for next page
- long press button A for full refresh (circuitpython version)

  Note : because there is very little space on the rp2040, not many .epub files can be stored on it, maybe just one, and the conversion will eat up more space for the extracted text

Converting an .epub (circuitpython version) :
- copy the .epub into /books over USB as usual
- at the REPL, launch the converter as its own program, so the reader is not loaded :

  ```
  import supervisor
  supervisor.set_next_code_file("epub_xtract.py")
  supervisor.reload()
  ```
- it runs by itself and prints progress; reset afterwards and the .txt is in the picker

  It has to run with nothing else loaded: a chapter is decompressed whole (circuitpython has
  no streaming inflater) and the reader holds around 60KB of buffers, hyphenation patterns and
  fonts. Interrupting the reader to the REPL does not free that, so chapters fail to allocate.

  Writing needs the filesystem, which the USB host normally owns - the reader never writes
  files, it keeps its position in NVRAM. On battery the converter takes it over by itself;
  while plugged in, hold button A while resetting so boot.py hands it over first.

I like it!

Case is here : https://cad.onshape.com/documents/814dd2a988145f0ed18b6efd/w/66507594fb6b5b6a70cee4f8/e/53ba6e3dcf1a4ae5db1cb3dd?renderMode=0&uiState=693158b53d70a686c43cbc0a
