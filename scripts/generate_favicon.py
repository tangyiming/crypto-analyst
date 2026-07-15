#!/usr/bin/env python3
"""Generate favicon.ico (16/32px) from site palette — no third-party deps."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "analyst" / "web" / "static" / "favicon.ico"

BG = (11, 14, 17, 255)
BORDER = (30, 35, 41, 255)
GREEN = (14, 203, 129, 255)
YELLOW = (240, 185, 11, 255)
RED = (246, 70, 93, 255)
MUTED = (132, 142, 156, 255)


def _blend(fg: tuple[int, int, int, int], bg: tuple[int, int, int, int]) -> tuple[int, int, int]:
    fa = fg[3] / 255.0
    ba = bg[3] / 255.0
    a = fa + ba * (1 - fa)
    if a <= 0:
        return (0, 0, 0)
    return tuple(
        int((fg[i] * fa + bg[i] * ba * (1 - fa)) / a)
        for i in range(3)
    )


def _set(px: list[tuple[int, int, int]], size: int, x: int, y: int, color: tuple[int, int, int, int]) -> None:
    if 0 <= x < size and 0 <= y < size:
        px[y * size + x] = _blend(color, (*px[y * size + x], 255))


def _rect(px, size, x0, y0, w, h, color):
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            _set(px, size, x, y, color)


def _line(px, size, x0, y0, x1, y1, color):
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        _set(px, size, x0, y0, color)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def render_icon(size: int) -> list[tuple[int, int, int]]:
    px = [BG[:3]] * (size * size)
    m = size / 32.0

    def S(v: float) -> int:
        return int(round(v * m))

    # border
    for i in range(size):
        _set(px, size, i, 0, BORDER)
        _set(px, size, i, size - 1, BORDER)
        _set(px, size, 0, i, BORDER)
        _set(px, size, size - 1, i, BORDER)

    # trend
    pts = [(5, 22), (11, 17), (17, 19), (23, 11), (27, 9)]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        _line(px, size, S(x0), S(y0), S(x1), S(y1), MUTED)

    # candles
    _line(px, size, S(10), S(9), S(10), S(23), GREEN)
    _rect(px, size, S(9), S(13), max(1, S(2)), max(1, S(7)), GREEN)

    _line(px, size, S(16), S(11), S(16), S(25), YELLOW)
    _rect(px, size, S(15), S(15), max(1, S(3)), max(1, S(8)), YELLOW)

    _line(px, size, S(22), S(13), S(22), S(24), RED)
    _rect(px, size, S(21), S(16), max(1, S(2)), max(1, S(5)), RED)

    cx, cy, r = S(27), S(9), max(1, S(2))
    for y in range(cy - r, cy + r + 1):
        for x in range(cx - r, cx + r + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r ** 2:
                _set(px, size, x, y, YELLOW)

    return px


def _png_bytes(size: int, px: list[tuple[int, int, int]]) -> bytes:
    raw = bytearray()
    for y in range(size - 1, -1, -1):
        raw.append(0)
        for x in range(size):
            r, g, b = px[y * size + x]
            raw.extend((r, g, b))

    compressed = zlib.compress(bytes(raw), 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


def _ico_bytes(sizes: list[int]) -> bytes:
    images = [(s, _png_bytes(s, render_icon(s))) for s in sizes]
    count = len(images)
    offset = 6 + 16 * count
    out = bytearray()
    out += struct.pack("<HHH", 0, 1, count)
    entries = []
    for size, png in images:
        entries.append((size, png, offset))
        offset += len(png)
    for size, png, off in entries:
        out += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0,
            0,
            1,
            32,
            len(png),
            off,
        )
    for _, png, _ in entries:
        out += png
    return bytes(out)


def main() -> None:
    OUT.write_bytes(_ico_bytes([16, 32]))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
