# ------------------------------------------------------------
# uzipfile.py  –  pure-Python ZIP reader for MicroPython
# ------------------------------------------------------------
import deflate
import struct
from io import BytesIO


class FileSliceReader:
    """Simple streaming reader for a file slice (for stored files). No base class."""
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
        pass  # fp is shared, don't close


class UZipFile:
    """Read-only ZIP archive that only needs DEFLATE (method 8) or stored (0)."""

    def __init__(self, filename: str):
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

        eocd_sig = b"\x50\x4b\x05\x06"
        pos = tail.rfind(eocd_sig)
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

    # -----------------------------------------------------------------
    def _get_entry(self, member: str):
        entry = next((f for f in self.filelist if f["filename"] == member), None)
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
    def read(self, member: str):
        # Legacy: full read (for small files if needed)
        entry, data_start = self._get_entry(member)
        self.fp.seek(data_start)
        compressed = self.fp.read(entry["compressed_size"])

        if entry["compression_method"] == 0:
            return compressed

        if entry["compression_method"] == 8:
            stream = BytesIO(compressed)
            try:
                d = deflate.DeflateIO(stream, deflate.RAW, 15)
                data = d.read()
                d.close()
                return data
            except Exception as e:
                print(f"[UZIP ERROR] {member}: {e}")
                return b""

        raise NotImplementedError(
            f"Compression method {entry['compression_method']} not supported"
        )

    # -----------------------------------------------------------------
    def get_reader(self, member: str):
        """Return a streaming reader for the member (DeflateIO or file slice)."""
        entry, data_start = self._get_entry(member)

        if entry["compression_method"] == 0:  # stored: stream from fp slice
            return FileSliceReader(self.fp, data_start, entry["compressed_size"])

        if entry["compression_method"] == 8:  # DEFLATE: load compressed (small), stream decompress
            self.fp.seek(data_start)
            compressed = self.fp.read(entry["compressed_size"])
            stream = BytesIO(compressed)
            try:
                return deflate.DeflateIO(stream, deflate.RAW, 15)
            except Exception as e:
                print(f"[UZIP ERROR] {member}: {e}")
                raise  # re-raise to handle in caller

        raise NotImplementedError(
            f"Compression method {entry['compression_method']} not supported"
        )

    # -----------------------------------------------------------------
    def close(self):
        self.fp.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
