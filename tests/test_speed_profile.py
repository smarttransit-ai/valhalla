"""The packed week of speeds matches what Valhalla stores and survives the packing."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "inrix"))

from speed_profile import (  # noqa: E402
    BUCKETS_PER_WEEK,
    COEFFICIENTS,
    MIN_KMH,
    compress,
    decode,
    decompress,
    encode,
    pack,
)

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


def jam_week() -> tuple[np.ndarray, np.ndarray]:
    """One edge at 100 km/h that crawls at 6 km/h on weekday evenings, and where that jam is."""
    bucket = np.arange(BUCKETS_PER_WEEK)
    hour, day = bucket / BUCKETS_PER_HOUR % 24, bucket // BUCKETS_PER_DAY
    jam = (hour >= 16.5) & (hour < 18) & (day >= 1) & (day <= 5)
    return np.where(jam, 6.0, 100.0)[None, :], jam


def test_packing_alone_pulls_a_jam_under_the_speed_valhalla_uses() -> None:
    kmh, _ = jam_week()
    assert decompress(compress(kmh)).min() <= MIN_KMH


def test_pack_lifts_a_jam_until_valhalla_can_use_every_speed() -> None:
    kmh, _ = jam_week()
    lifted, packed = pack(kmh)
    assert decompress(packed).min() > MIN_KMH
    assert np.array_equal(packed, compress(lifted))


def test_pack_lifts_only_the_slow_speeds_and_keeps_the_jam_slow() -> None:
    kmh, jam = jam_week()
    lifted, _ = pack(kmh)
    assert (lifted[0, ~jam] == 100.0).all()
    assert 6.0 < lifted[0, jam].max() < 30.0


def test_pack_lifts_a_road_that_crawls_all_week_just_over_the_limit() -> None:
    lifted, packed = pack(np.full((1, BUCKETS_PER_WEEK), 3.0))
    assert (lifted == MIN_KMH + 1).all()
    assert decompress(packed).min() > MIN_KMH


def test_pack_leaves_a_week_alone_when_valhalla_can_use_it() -> None:
    kmh = np.array([np.full(BUCKETS_PER_WEEK, 40.0), np.linspace(30.0, 90.0, BUCKETS_PER_WEEK)])
    lifted, packed = pack(kmh)
    assert np.array_equal(lifted, kmh)
    assert np.array_equal(packed, compress(kmh))


def test_pack_does_not_change_its_input() -> None:
    kmh, _ = jam_week()
    before = kmh.copy()
    pack(kmh)
    assert np.array_equal(kmh, before)
