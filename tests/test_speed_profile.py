"""The packed week of speeds matches what Valhalla stores and survives the packing."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "inrix"))

from speed_profile import BUCKETS_PER_WEEK, COEFFICIENTS, compress, decode, decompress, encode  # noqa: E402

ENCODED_CHARACTERS = 536  # base64 of 200 two-byte coefficients
BUCKETS_PER_HOUR = 12
BUCKETS_PER_DAY = 24 * BUCKETS_PER_HOUR

# The notebook's invented week, 10 km/h on Sunday and 100 km/h after, as `valhalla_encode_speed` packs it.
NOTEBOOK_WEEK = [10] * BUCKETS_PER_DAY + [100] * (BUCKETS_PER_WEEK - BUCKETS_PER_DAY)
NOTEBOOK_WEEK_PACKED_BY_VALHALLA = (
    "D0n86/05/bH+Rf7k/3wAAABjAJ4AsQChAHcAPQAA/8v/p/+Y/53/tf/ZAAAAJAA+AEoARwA3AB0AAP/l/9H/x//J/9X/6QAAABYA"
    "JgAvAC0AJAATAAD/7v/g/9n/2f/i//AAAAAQABwAIgAhABoADgAA//L/5//i/+L/6f/zAAAADAAWABsAGgAVAAsAAP/1/+z/6P/o"
    "/+3/9gAAAAoAEgAWABYAEQAKAAD/9//v/+z/7P/w//cAAAAJAA8AEwATAA8ACAAA//j/8v/u/+//8v/4AAAABwANABAAEAANAAcA"
    "AP/5//T/8f/x//T/+QAAAAcADAAPAA4ACwAGAAD/+v/1//L/8v/1//oAAAAGAAsADQANAAoABgAA//r/9v/0//T/9v/7AAAABQAK"
    "AAwADAAJAAUAAP/7//f/9f/1//f/+wAAAAUACQALAAsACQAFAAD/+//4//b/9v/4//sAAAAEAAgACgAKAAgABAAA//z/+P/2//b/"
    "+P/8AAAABAAHAAkACQAHAAQAAP/8//n/9w=="
)


def test_a_constant_week_survives() -> None:
    speeds = np.full((1, BUCKETS_PER_WEEK), 40.0)
    assert np.allclose(decompress(compress(speeds)), 40.0, atol=0.1)


def test_a_daily_rush_hour_survives() -> None:
    hours = np.arange(BUCKETS_PER_WEEK) / BUCKETS_PER_HOUR
    speeds = (50.0 - 15.0 * np.exp(-((hours % 24 - 17.0) ** 2) / 8.0))[None, :]
    assert np.abs(decompress(compress(speeds)) - speeds).max() < 2.0


def test_the_text_form_round_trips() -> None:
    coefficients = compress(np.random.default_rng(0).uniform(10, 100, (1, BUCKETS_PER_WEEK)))[0]
    text = encode(coefficients)
    assert len(coefficients) == COEFFICIENTS
    assert len(text) == ENCODED_CHARACTERS
    assert np.array_equal(decode(text), coefficients)


def test_it_packs_the_notebook_week_as_valhalla_does() -> None:
    assert encode(compress(np.array([NOTEBOOK_WEEK], dtype=float))[0]) == NOTEBOOK_WEEK_PACKED_BY_VALHALLA
