"""Valhalla's packed form of one edge's week of speeds.

Valhalla stores a week as 2,016 five-minute speeds (km/h), starting Sunday 00:00 local time. It keeps only
the first 200 terms of a cosine transform of them, as 16-bit integers, written as base64 text. This follows
`compress_speed_buckets`, `decompress_speed_bucket` and `encode_compressed_speeds` in Valhalla's
baldr/predictedspeeds.cc.
"""

from __future__ import annotations

import base64

import numpy as np

BUCKET_MINUTES = 5
BUCKETS_PER_WEEK = 7 * 24 * 60 // BUCKET_MINUTES
COEFFICIENTS = 200
NORMALIZATION = np.sqrt(2.0 / BUCKETS_PER_WEEK)
FIRST_TERM = 1.0 / np.sqrt(2.0)
STORED = ">i2"  # each coefficient is a big-endian 16-bit integer

# COSINES[k, i] = cos(pi / 2016 * (i + 1/2) * k)
COSINES = np.cos(np.pi / BUCKETS_PER_WEEK * np.outer(np.arange(COEFFICIENTS), np.arange(BUCKETS_PER_WEEK) + 0.5))
COSINES[0] *= FIRST_TERM


def compress(speeds: np.ndarray) -> np.ndarray:
    """Rows of 2,016 speeds -> rows of 200 integer coefficients."""
    return np.rint(NORMALIZATION * speeds @ COSINES.T).astype(np.int16)


def decompress(coefficients: np.ndarray) -> np.ndarray:
    """Rows of 200 coefficients -> rows of the 2,016 speeds Valhalla will use."""
    return NORMALIZATION * coefficients.astype(float) @ COSINES


def encode(coefficients: np.ndarray) -> str:
    return base64.b64encode(coefficients.astype(STORED).tobytes()).decode()


def decode(text: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(text), dtype=STORED).astype(np.int16)
