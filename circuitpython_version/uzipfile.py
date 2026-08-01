# ------------------------------------------------------------
# uzipfile.py  -  pure-Python ZIP reader (CircuitPython port)
# ------------------------------------------------------------
# Ported from the MicroPython version in the repository root. The only real
# difference is decompression: MicroPython has `deflate.DeflateIO`, a streaming
# inflater, which CircuitPython does not - its `zlib` module offers only
# `zlib.decompress(data, wbits)`. So a DEFLATE member has to be decompressed
# whole into memory instead of being streamed.
#
# That matters for memory, and shapes how this is used:
#   * stored members (method 0) are still streamed, so extracting a cover image
#     - which EPUBs almost always store uncompressed, being JPEG already -
#     costs only the copy buffer
#   * deflated members are held in RAM while they are read, so the converter is
#     meant to be run standalone, without the reader's buffers loaded
import struct
import zlib
from io import BytesIO


class FileSliceReader:
    """Streaming reader over a slice of the archive (for stored members)."""

    def __init__(self, fp, start, size):
        self.fp = fp
        self.pos = start
        self.end = start + size
        self.fp.seek(start)

    def read(self, size=-1):
        if size < 0:
            size = self.end - self.pos
        remaining = self.end - self.pos
        if size > remaining:
            size = remaining
        if size <= 0:
            return b""
        data = self.fp.read(size)
        self.pos += len(data)
        return data

    def close(self):
        pass  # fp is shared, don't close it


class UZipFile:
    """Read-only ZIP archive supporting stored (0) and DEFLATE (8) members."""

    def __init__(self, filename):
        self.fp = open(filename, "rb")
        self.filelist = self._read_central_directory()

    # -----------------------------------------------------------------
    def _read_central_directory(self):
        self.fp.seek(0, 2)
        file_size = self.fp.tell()

        # ---- find End Of Central Directory (EOCD) -----------------
        SEARCH = 65535 + 22
        start = max(0, file_size - SEARCH)
        self.fp.seek(start)
        tail = self.fp.read(file_size - start)

        pos = tail.rfind(b"\x50\x4b\x05\x06")
        if pos == -1:
            raise OSError("Not a valid ZIP file (EOCD missing)")

        eocd_start = start + pos
        self.fp.seek(eocd_start + 16)
        cd_offset = struct.unpack("<I", self.fp.read(4))[0]

        # ---- read Central Directory entries -----------------------
        self.fp.seek(cd_offset)
        files = []

        while True:
            header = self.fp.read(46)
            if len(header) < 46 or header[:4] != b"\x50\x4b\x01\x02":
                break

            comp_method, = struct.unpack("<H", header[10:12])
            comp_size, = struct.unpack("<I", header[20:24])
            uncomp_size, = struct.unpack("<I", header[24:28])
            name_len, = struct.unpack("<H", header[28:30])
            extra_len, = struct.unpack("<H", header[30:32])
            comment_len, = struct.unpack("<H", header[32:34])
            lfh_offset, = struct.unpack("<I", header[42:46])

            name = self.fp.read(name_len).decode("utf-8")
            self.fp.seek(extra_len + comment_len, 1)   # skip

            files.append({
                "filename": name,
                "compression_method": comp_method,
                "compressed_size": comp_size,
                "uncompressed_size": uncomp_size,
                "lfl_offset": lfh_offset,
            })

        return files

    # -----------------------------------------------------------------
    def namelist(self):
        return [f["filename"] for f in self.filelist]

    def entry_for(self, member):
        for f in self.filelist:
            if f["filename"] == member:
                return f
        return None

    # -----------------------------------------------------------------
    def _get_entry(self, member):
        entry = self.entry_for(member)
        if entry is None:
            raise KeyError(member)

        # ---- go to Local File Header -------------------------------
        self.fp.seek(entry["lfl_offset"])
        lfh = self.fp.read(30)
        name_len, = struct.unpack("<H", lfh[26:28])
        extra_len, = struct.unpack("<H", lfh[28:30])

        data_start = entry["lfl_offset"] + 30 + name_len + extra_len
        return entry, data_start

    # -----------------------------------------------------------------
    def read(self, member):
        """Whole member as bytes. Only for small files - the OPF, container.xml."""
        entry, data_start = self._get_entry(member)
        self.fp.seek(data_start)
        compressed = self.fp.read(entry["compressed_size"])

        if entry["compression_method"] == 0:
            return compressed

        if entry["compression_method"] == 8:
            # negative wbits selects raw DEFLATE (no zlib header), which is
            # what ZIP stores
            return zlib.decompress(compressed, -15)

        raise NotImplementedError(
            "Compression method %d not supported" % entry["compression_method"])

    # -----------------------------------------------------------------
    def get_reader(self, member):
        """A reader with .read(size)/.close() for the member.

        Stored members stream straight off the archive. Deflated ones are
        decompressed in full first, because CircuitPython has no streaming
        inflater - so the caller still reads in chunks, but the memory has
        already been spent.
        """
        entry, data_start = self._get_entry(member)

        if entry["compression_method"] == 0:
            return FileSliceReader(self.fp, data_start, entry["compressed_size"])

        if entry["compression_method"] == 8:
            self.fp.seek(data_start)
            compressed = self.fp.read(entry["compressed_size"])
            return BytesIO(zlib.decompress(compressed, -15))

        raise NotImplementedError(
            "Compression method %d not supported" % entry["compression_method"])

    # -----------------------------------------------------------------
    def extract_to(self, member, dest_path, chunk=1024):
        """Write a member out to `dest_path`.

        Stored members are copied a chunk at a time and never held whole in
        memory, which is the case that matters: cover images are already
        compressed, so EPUBs normally store them rather than deflate them.
        """
        entry, data_start = self._get_entry(member)

        if entry["compression_method"] == 0:
            self.fp.seek(data_start)
            remaining = entry["compressed_size"]
            with open(dest_path, "wb") as out:
                while remaining > 0:
                    buf = self.fp.read(chunk if chunk < remaining else remaining)
                    if not buf:
                        break
                    out.write(buf)
                    remaining -= len(buf)
        else:
            data = self.read(member)
            with open(dest_path, "wb") as out:
                out.write(data)
        return True

    # -----------------------------------------------------------------
    def close(self):
        self.fp.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
