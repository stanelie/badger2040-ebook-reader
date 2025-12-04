<img src="https://github.com/user-attachments/assets/7a14b9e0-4949-4c9f-9d27-3b670a396616" width="300">

AI coded ebook reader for the badger2040.

Features :
- resume book where you left off
- fast display of the next page thanks to pre-buffering
- legible font better (to me) than the built-in fonts
- can convert an .epub file directly onboard to the .txt file format it can read (micropython version only)
- ability to switch books (ebook file picker)
- displays battery status
- ebook progress bar

Usage :
- put .txt of .epub (micropython version only) ebook file into /books folder of the badger2040
- button A brings up the file picker, up and down arrows to select book, button A again to choose book
- button UP for previous page, button DOWN for next page
- long press button A for full refresh (circuitpython version)

  Note : because there is very little space on the rp2040, not many .epub files can be stored on it, maybe just one, and the conversion will eat up more space for the extracted text

I like it!
