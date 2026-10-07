"""自動生成（tools/publish/live/build_live_repo.py）：tools/pipeline/make_earth_tiles.py から雲のタイルに要る部分だけを抜き出した版。手で直さない"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

TILE, BORDER = 256, 2


FACES = [  # (n, U, V)
    ((1, 0, 0), (0, 1, 0), (0, 0, -1)),
    ((-1, 0, 0), (0, -1, 0), (0, 0, -1)),
    ((0, 1, 0), (-1, 0, 0), (0, 0, -1)),
    ((0, -1, 0), (1, 0, 0), (0, 0, -1)),
    ((0, 0, 1), (0, 1, 0), (1, 0, 0)),
    ((0, 0, -1), (0, 1, 0), (-1, 0, 0)),
]


def tile_dirs(face: int, level: int, x: int, y: int) -> np.ndarray:
    n, U, V = (np.array(a, float) for a in FACES[face])
    k = (np.arange(TILE) - BORDER + 0.5) / (TILE - 2 * BORDER)
    u = (x + k) / 2 ** level
    v = (y + k) / 2 ** level
    s = np.tan((2 * u - 1) * np.pi / 4)[None, :, None]
    t = np.tan((2 * v - 1) * np.pi / 4)[:, None, None]
    d = n + s * U + t * V
    return d / np.linalg.norm(d, axis=2, keepdims=True)


def write_bundle(path: Path, tiles: list[tuple[int, int, bytes]]) -> None:
    head = bytearray(b"UTB1") + struct.pack("<I", len(tiles))
    off = 8 + 12 * len(tiles)
    body = bytearray()
    for x, y, data in tiles:
        head += struct.pack("<HHII", x, y, off + len(body), len(data))
        body += data
    path.write_bytes(bytes(head + body))
