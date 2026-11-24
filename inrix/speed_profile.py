"""Valhalla's packed form of one edge's week of speeds.

Valhalla stores a week as 2,016 five-minute speeds (km/h), starting Sunday 00:00 local time. It keeps the first
200 terms of their cosine transform as 16-bit integers, written as base64 text, and it does not use a speed of
5 km/h or less. This follows `compress_speed_buckets`, `decompress_speed_bucket` and `encode_compressed_speeds`
in Valhalla's baldr/predictedspeeds.cc. The three numbers below are Valhalla's own constants.
"""

from __future__ import annotations

import base64

import numpy as np
from scipy.fft import dct, idct

BUCKETS_PER_WEEK = 2016  # kBucketsPerWeek
COEFFICIENTS = 200  # kCoefficientCount
MIN_KMH = 5  # kMinSpeedKph


def compress(speeds: np.ndarray) -> np.ndarray:
    """Rows of 2,016 speeds -> rows of 200 integer coefficients."""
    return np.rint(dct(speeds, norm="ortho")[..., :COEFFICIENTS]).astype(np.int16)


def decompress(coefficients: np.ndarray) -> np.ndarray:
    """Rows of 200 coefficients -> rows of the 2,016 speeds Valhalla will use."""
    return idct(coefficients.astype(float), n=BUCKETS_PER_WEEK, norm="ortho")


def encode(coefficients: np.ndarray) -> str:
    return base64.b64encode(coefficients.astype(">i2").tobytes()).decode()  # big-endian 16-bit integers


def decode(text: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(text), dtype=">i2").astype(np.int16)


def pack(speeds: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Each row's week as it is packed, and its coefficients.

    Packing smooths a week, so a short deep jam comes back with dips under the speeds put in. Where a dip reaches
    MIN_KMH, Valhalla takes the edge's mean speed of the week instead, and the worst minutes of the jam would be
    driven at a normal speed. The slowest speeds of such a row are raised, one km/h at a time, until its packed
    week stays above MIN_KMH.
    """
    speeds = speeds.copy()
    packed = compress(speeds)
    floor = MIN_KMH
    rows = np.flatnonzero((decompress(packed) <= MIN_KMH).any(axis=1))
    while rows.size:
        floor += 1
        speeds[rows] = np.maximum(speeds[rows], floor)
        packed[rows] = compress(speeds[rows])
        rows = rows[(decompress(packed[rows]) <= MIN_KMH).any(axis=1)]
    return speeds, packed
