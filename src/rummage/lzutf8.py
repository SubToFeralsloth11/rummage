"""LZUTF8 decoding: Blockbench saves compressed .bbmodel files (and backups) as "<lz>" + LZUTF8 data
in its "StorageBinaryString" text encoding (15 bits per character)."""

from __future__ import annotations


def _decode_binary_string(s: str) -> bytes:
    out = bytearray()
    remainder = 0
    state = 0
    for ch in s:
        value = ord(ch)
        if value >= 32768:  # end marker; low bit set = odd byte count, drop the padding byte
            if value == 32769 and out:
                out.pop()
            state = 0
            continue
        if state == 0:
            remainder = value
        else:
            v = (remainder << state) | (value >> (15 - state))
            out.append((v >> 8) & 255)
            out.append(v & 255)
            remainder = value & ((1 << (15 - state)) - 1)
        state = 0 if state == 15 else state + 1
    return bytes(out)


def _decompress(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b >> 6 != 3 or i == n - 1 or data[i + 1] >> 7 == 1:
            out.append(b)  # literal byte (including UTF-8 lead bytes followed by continuation bytes)
            i += 1
            continue
        length = b & 31
        if b >> 5 == 6:
            distance = data[i + 1]
            i += 2
        else:
            distance = (data[i + 1] << 8) | data[i + 2]
            i += 3
        start = len(out) - distance
        for k in range(length):
            out.append(out[start + k])
    return bytes(out)


def read_bbmodel_text(raw: str) -> str:
    """Plain JSON, or Blockbench's compressed "<lz>…" form."""
    if raw.startswith("<lz>"):
        return _decompress(_decode_binary_string(raw[4:])).decode("utf-8", errors="replace")
    return raw
