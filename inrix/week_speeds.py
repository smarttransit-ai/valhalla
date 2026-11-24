"""Turn one week of INRIX speed readings into the per-tile CSV files that Valhalla's predicted-traffic tool reads.

    python inrix/week_speeds.py --readings readings/ --segment-edges edges.csv --sunday 2026-07-12 --out traffic

Valhalla holds one week of speeds per edge, in five-minute steps from Sunday 00:00 local time. For every Valhalla edge
in the segment-edges file, this writes the week that starts on `--sunday`: the mean of the speeds of the INRIX segments
on that edge, in km/h. A segment's missing five-minute readings take that segment's median speed of the week, and a
segment with no reading at all in the week is left out. The output is one CSV file per Valhalla tile under `--out`,
in the layout `valhalla_add_predicted_traffic` reads. `--out` has to be empty or new, so that no file of another
week is loaded with this one. `--profile` also saves the speeds that were packed, for checks.

The readings are Parquet or CSV files with INRIX's own column names: `xd_id` (the segment), `measurement_tstamp`,
`speed` and `reference_speed` (INRIX's empty-road speed), both in mph. `measurement_tstamp` has to be the local
wall-clock time of the roads, with no time zone. Every reading counts, whatever its `confidence_score`.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import shlex
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
from speed_profile import BUCKET_MINUTES, BUCKETS_PER_WEEK, COEFFICIENTS, compress, decompress, encode

KMH_PER_MPH = 1.609344
SLOWEST_KMH, FASTEST_KMH = 5.0, 140.0  # Valhalla uses a predicted speed only when it lies between these
MIN_SPEED_KMH = 6.0  # the slowest speed an edge's week starts from
LIFT_KMH = 1.0  # the step by which pack() raises an edge's slowest speeds

# A Valhalla edge id packs three numbers: the level (3 bits), the tile (22 bits) and the edge's index in the tile.
LEVEL_BITS, TILE_BITS = 3, 22
TILE_DIGITS = {0: 6, 1: 6, 2: 9}  # digits of the tile number in a tile's file path, by level
DIGITS_PER_FOLDER = 3

# One row per (segment, five-minute bucket of the week) that has readings: their mean speed, and the segment's mean
# reference speed over the week. A reading whose speed or reference speed is missing or not a number is skipped.
WEEK_SQL = """
    SELECT segment_row,
           CAST(floor(epoch(measurement_tstamp - $sunday) / $bucket_seconds) AS INTEGER) AS week_bucket,
           avg(speed) AS mph,
           avg(avg(reference_speed)) OVER (PARTITION BY segment_row) AS free_mph
    FROM {reader}($readings, union_by_name=true)
    JOIN (SELECT unnest($segments::BIGINT[]) AS xd_id, unnest(range(len($segments))) AS segment_row) USING (xd_id)
    WHERE measurement_tstamp >= $sunday AND measurement_tstamp < $end
      AND speed IS NOT NULL AND reference_speed IS NOT NULL AND NOT isnan(speed) AND NOT isnan(reference_speed)
    GROUP BY segment_row, week_bucket
"""


def split_edge_id(edge: int) -> tuple[int, int, int]:
    """(level, tile, index within the tile) of a Valhalla edge id."""
    level = edge & ((1 << LEVEL_BITS) - 1)
    tile = (edge >> LEVEL_BITS) & ((1 << TILE_BITS) - 1)
    return level, tile, edge >> (LEVEL_BITS + TILE_BITS)


def tile_file(level: int, tile: int) -> str:
    """Valhalla's path of a tile's file, e.g. level 2 tile 756425 -> 2/000/756/425.csv."""
    digits = f"{tile:0{TILE_DIGITS[level]}d}"
    folders = (digits[start : start + DIGITS_PER_FOLDER] for start in range(0, len(digits), DIGITS_PER_FOLDER))
    return "/".join([str(level), *folders]) + ".csv"


def read_segment_edges(path: Path) -> dict[int, list[int]]:
    """The Valhalla edge ids of each INRIX segment, from a CSV file with the columns XDSegID,valhalla_edge."""
    edges: dict[int, list[int]] = defaultdict(list)
    with path.open(newline="") as rows:
        for row in csv.DictReader(rows):
            edges[int(row["XDSegID"])].append(int(row["valhalla_edge"]))
    return dict(edges)


def read_week(readings: str, segments: list[int], sunday: date, threads: int) -> tuple[np.ndarray, np.ndarray]:
    """Each segment's speeds (mph) in the week's five-minute buckets, NaN for no reading, and its mean reference speed.

    `readings` is a Parquet or CSV file, a glob of such files, or a folder that holds Parquet files. Both arrays have
    one row per segment, in the order of `segments`.
    """
    if Path(readings).is_dir():
        readings = str(Path(readings) / "**" / "*.parquet")
    reader = "read_csv" if readings.lower().endswith((".csv", ".csv.gz")) else "read_parquet"
    bucket_seconds = timedelta(minutes=BUCKET_MINUTES).total_seconds()
    week = {"sunday": sunday, "end": sunday + timedelta(weeks=1)}
    values = {"readings": readings, "segments": segments, "bucket_seconds": bucket_seconds, **week}
    with duckdb.connect(config={"threads": threads}) as db:
        times = db.sql(f"SELECT measurement_tstamp FROM {reader}($readings)", params={"readings": readings})
        if str(times.types[0]) == "TIMESTAMP WITH TIME ZONE":
            # DuckDB would turn such times into this machine's local time, not the roads'.
            sys.exit("measurement_tstamp carries a time zone: save it as the roads' local time, with no zone")
        found = db.execute(WEEK_SQL.format(reader=reader), values).fetchnumpy()
    mph = np.full((len(segments), BUCKETS_PER_WEEK), np.nan)
    mph[found["segment_row"], found["week_bucket"]] = found["mph"]
    free_mph = np.full(len(segments), np.nan)
    free_mph[found["segment_row"]] = found["free_mph"]
    return mph, free_mph


def fill_missing(mph: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Give each segment's missing readings its median speed, and flag the segments that have no reading at all."""
    missing = np.isnan(mph)
    silent = missing.all(axis=1)
    filled = mph.copy()
    medians = np.nanmedian(mph[~silent], axis=1, keepdims=True)
    filled[~silent] = np.where(missing[~silent], medians, mph[~silent])
    return filled, silent


def edge_weeks(
    filled: np.ndarray,
    free_mph: np.ndarray,
    silent: np.ndarray,
    segments: list[int],
    edges_of_segment: dict[int, list[int]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Edge ids in rising order, each edge's week (km/h) and its free-flow speed (km/h).

    An edge gets the mean of the segments on it that have readings, and its week never goes below MIN_SPEED_KMH.
    """
    rows_on_edge: dict[int, list[int]] = defaultdict(list)
    for row, segment in enumerate(segments):
        if not silent[row]:
            for edge in edges_of_segment[segment]:
                rows_on_edge[edge].append(row)
    edge_ids = sorted(rows_on_edge)
    kmh = [np.maximum(filled[rows_on_edge[edge]].mean(axis=0) * KMH_PER_MPH, MIN_SPEED_KMH) for edge in edge_ids]
    free_kmh = [free_mph[rows_on_edge[edge]].mean() * KMH_PER_MPH for edge in edge_ids]
    return np.array(edge_ids, dtype=np.int64), np.array(kmh), np.array(free_kmh)


def pack(kmh: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Each edge's week as it is packed, and its packed form.

    Packing keeps 200 numbers of a week, so a short deep jam comes back with dips under the speeds put in. Valhalla
    does not use a speed at or under SLOWEST_KMH: it takes the edge's mean speed of the week instead, and the worst
    minutes of the jam would be driven at a normal speed. The slowest speeds of such an edge are raised, LIFT_KMH at
    a time, until its packed week stays above that limit.
    """
    kmh = kmh.copy()
    packed = compress(kmh)
    floor = MIN_SPEED_KMH
    rows = np.flatnonzero((decompress(packed) <= SLOWEST_KMH).any(axis=1))
    while rows.size:
        floor += LIFT_KMH
        kmh[rows] = np.maximum(kmh[rows], floor)
        packed[rows] = compress(kmh[rows])
        rows = rows[(decompress(packed[rows]) <= SLOWEST_KMH).any(axis=1)]
    return kmh, packed


def tile_lines(edge_ids: np.ndarray, kmh: np.ndarray, free_kmh: np.ndarray, packed: np.ndarray) -> dict[str, list[str]]:
    """The lines of each tile's file, one per edge: level/tile/index,free_flow_kmh,constrained_kmh,encoded_week."""
    lines: dict[str, list[str]] = defaultdict(list)
    for edge, week, free, coefficients in zip(edge_ids, kmh, free_kmh, packed, strict=True):
        level, tile, index = split_edge_id(int(edge))
        line = f"{level}/{tile}/{index},{round(free)},{round(week.mean())},{encode(coefficients)}"
        lines[tile_file(level, tile)].append(line)
    return dict(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--readings", required=True, help="INRIX readings: a Parquet or CSV file, a folder or a glob")
    parser.add_argument("--segment-edges", type=Path, required=True, help="CSV with the columns XDSegID,valhalla_edge")
    parser.add_argument("--sunday", type=date.fromisoformat, required=True, help="first day of the week, a Sunday")
    parser.add_argument("--out", type=Path, required=True, help="empty or new folder to write the tile CSV files into")
    parser.add_argument("--profile", type=Path, help=".npz file to also save the unpacked speeds into, for checks")
    parser.add_argument("--threads", type=int, default=4, help="DuckDB threads (%(default)s)")
    args = parser.parse_args()
    print("command:", shlex.join(sys.argv))
    if args.sunday.weekday() != calendar.SUNDAY:
        sys.exit(f"{args.sunday} is not a Sunday")
    if args.out.exists() and any(args.out.iterdir()):
        sys.exit(f"{args.out} is not empty: the files of another week in it would be loaded with this one")
    week = f"week that starts on {args.sunday}"

    edges_of_segment = read_segment_edges(args.segment_edges)
    segments = sorted(edges_of_segment)
    if not segments:
        sys.exit(f"{args.segment_edges} lists no segment")
    mph, free_mph = read_week(args.readings, segments, args.sunday, args.threads)
    missing = np.isnan(mph)
    if missing.all():
        sys.exit(f"no reading of the segments in {args.segment_edges} falls in the {week}")
    filled, silent = fill_missing(mph)
    print(f"\n{week}: {len(segments)} segments have Valhalla edges, {(~missing).sum()} five-minute readings")
    print(f"five-minute readings missing: {missing.mean():.2%}; segments with no reading (left out): {silent.sum()}")
    days = timedelta(weeks=1).days
    day_read = (~missing).reshape(len(segments), days, -1).any(axis=(0, 2))
    unread = [str(args.sunday + timedelta(days=day)) for day in range(days) if not day_read[day]]
    print(f"days with no reading: {', '.join(unread) or 'none'}")

    edge_ids, built, free_kmh = edge_weeks(filled, free_mph, silent, segments, edges_of_segment)
    print(f"Valhalla edges with a week of speeds: {len(edge_ids)}")
    print(f"speed, km/h: mean {built.mean():.1f}; INRIX reference (empty road) mean {free_kmh.mean():.1f}")
    kmh, packed = pack(built)
    unpacked = decompress(packed)
    lost = np.abs(unpacked - kmh)
    print(f"edges whose slowest speeds were lifted: {(kmh != built).any(axis=1).sum()}")
    loss = f"median {np.median(lost):.1f}, largest {lost.max():.1f}"
    print(f"km/h lost by packing a week into {COEFFICIENTS} numbers: {loss}")
    unused = (unpacked >= FASTEST_KMH).sum()
    print(f"five-minute speeds at or over {FASTEST_KMH:g} km/h after packing, which Valhalla does not use: {unused}")

    lines = tile_lines(edge_ids, kmh, free_kmh, packed)
    for name, rows in lines.items():
        path = args.out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(rows) + "\n")
    print(f"wrote {len(lines)} tile files under {args.out}")
    if args.profile:
        args.profile.parent.mkdir(parents=True, exist_ok=True)
        speeds = {"kmh": kmh.astype(np.float32), "free_kmh": free_kmh.astype(np.float32)}
        np.savez_compressed(args.profile, edge=edge_ids, **speeds)
        print(f"wrote the unpacked speeds to {args.profile}")


if __name__ == "__main__":
    main()
