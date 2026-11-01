"""Read the preview image Blender saves inside every .blend (the TEST block). No Blender needed."""

from __future__ import annotations

import gzip
import struct
import zlib

import zstandard


def _open(path: str):
    f = open(path, "rb")
    magic = f.read(4)
    f.seek(0)
    if magic[:2] == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=f)
    if magic == b"\x28\xb5\x2f\xfd":
        return zstandard.ZstdDecompressor().stream_reader(f)
    return f


def _read_exact(r, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = r.read(n - len(out))
        if not chunk:
            break
        out += chunk
    return out


def read_thumbnail(path: str) -> tuple[int, int, bytes] | None:
    """(width, height, top-down RGBA bytes) or None if the file has no preview."""
    r = _open(path)
    try:
        head = _read_exact(r, 12)
        if not head.startswith(b"BLENDER"):
            return None
        if head[7:9].isdigit():
            # Blender 5+: "BLENDER17-01v0501": 17-byte header, 32-byte block headers.
            hsize = int(head[7:9])
            head += _read_exact(r, hsize - 12)
            endian = "<" if head[12:13] == b"v" else ">"
            bh_fmt, bh_size = endian + "4si Q q q", 32  # code, sdna, old ptr, len, nr
            len_index = 3
        else:
            # Older: "BLENDER-v300" ('-' = 64-bit pointers, '_' = 32-bit).
            endian = "<" if head[8:9] == b"v" else ">"
            if head[7:8] == b"-":
                bh_fmt, bh_size = endian + "4s i Q i i", 24  # code, len, old ptr, sdna, nr
            else:
                bh_fmt, bh_size = endian + "4s i I i i", 20
            len_index = 1
        for _ in range(64):  # the preview is near the start; don't walk the whole file
            bh = _read_exact(r, bh_size)
            if len(bh) < bh_size:
                return None
            fields = struct.unpack(bh_fmt, bh)
            code, length = fields[0], fields[len_index]
            if code == b"TEST":
                w, h = struct.unpack(endian + "ii", _read_exact(r, 8))
                if not (0 < w <= 1024 and 0 < h <= 1024):
                    return None
                px = _read_exact(r, w * h * 4)
                rows = [px[y * w * 4:(y + 1) * w * 4] for y in range(h)]
                return w, h, b"".join(reversed(rows))  # stored bottom-up
            if code == b"ENDB":
                return None
            _read_exact(r, length)
        return None
    finally:
        r.close()


def to_png(w: int, h: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
