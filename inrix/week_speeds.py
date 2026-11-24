"""Turn one week of INRIX speed readings into the per-tile CSV files that Valhalla's predicted-traffic tool reads.

    python inrix/week_speeds.py --readings readings/ --segment-edges segment_edges.csv \
        --edge-paths way_edges_ids_paths.csv --sunday 2026-07-12 --out traffic

Valhalla holds one week of speeds per edge, in five-minute steps from Sunday 00:00 local time. For every Valhalla edge
in the segment-edges file, this writes the week that starts on `--sunday`: the mean of the speeds of the INRIX segments
on that edge, in km/h. A segment's missing five-minute readings take that segment's median speed of the week, and a
segment with no reading at all in the week is left out. The output is one CSV file per Valhalla tile under `--out`,
in the layout `valhalla_add_predicted_traffic` reads. `--out` has to be empty or new, so that no file of another
week is loaded with this one.

The readings are Parquet or CSV files with INRIX's own column names: `xd_id` (the segment), `measurement_tstamp`,
`speed` and `reference_speed` (INRIX's empty-road speed), both in mph. `measurement_tstamp` has to be the local
wall-clock time of the roads, with no time zone. Every reading counts, whatever its `confidence_score`.

`--edge-paths` is the table `valhalla_ways_to_ids` writes in the container, `way_edges_ids_paths.csv`. It gives each
edge's name in Valhalla and its tile's file, so no edge id is taken apart here.
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
from scipy import constants
from speed_profile import BUCKETS_PER_WEEK, COEFFICIENTS, decompress, encode, pack

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
    bucket_seconds = timedelta(weeks=1).total_seconds() / BUCKETS_PER_WEEK
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

    An edge gets the mean of the segments on it that have readings.
    """
    rows_on_edge: dict[int, list[int]] = defaultdict(list)
    for row, segment in enumerate(segments):
        if not silent[row]:
            for edge in edges_of_segment[segment]:
                rows_on_edge[edge].append(row)
    edge_ids = sorted(rows_on_edge)
    kmh_per_mph = constants.mph / constants.kmh
    kmh = [filled[rows_on_edge[edge]].mean(axis=0) * kmh_per_mph for edge in edge_ids]
    free_kmh = [free_mph[rows_on_edge[edge]].mean() * kmh_per_mph for edge in edge_ids]
    return np.array(edge_ids, dtype=np.int64), np.array(kmh), np.array(free_kmh)


def read_edge_paths(path: Path, edge_ids: np.ndarray) -> dict[int, tuple[str, str]]:
    """Each edge's name in Valhalla (level/tile/index) and its tile's traffic file, from way_edges_ids_paths.csv."""
    wanted = "SELECT edge_id, graph_id, path FROM read_csv($table) WHERE edge_id IN (SELECT unnest($edges::BIGINT[]))"
    rows = duckdb.execute(wanted, {"table": str(path), "edges": edge_ids.tolist()}).fetchall()
    return {edge: (name, tile_file) for edge, name, tile_file in rows}


def tile_lines(
    edge_ids: np.ndarray, kmh: np.ndarray, free_kmh: np.ndarray, packed: np.ndarray, paths: dict[int, tuple[str, str]]
) -> dict[str, list[str]]:
    """The lines of each tile's file, one per edge in `paths`: name,free_flow_kmh,constrained_kmh,encoded_week."""
    lines: dict[str, list[str]] = defaultdict(list)
    for edge, week, free, coefficients in zip(edge_ids.tolist(), kmh, free_kmh, packed, strict=True):
        if edge in paths:
            name, tile_file = paths[edge]
            lines[tile_file].append(f"{name},{round(free)},{round(week.mean())},{encode(coefficients)}")
    return dict(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--readings", required=True, help="INRIX readings: a Parquet or CSV file, a folder or a glob")
    parser.add_argument("--segment-edges", type=Path, required=True, help="CSV with the columns XDSegID,valhalla_edge")
    parser.add_argument("--edge-paths", type=Path, required=True, help="way_edges_ids_paths.csv from the container")
    parser.add_argument("--sunday", type=date.fromisoformat, required=True, help="first day of the week, a Sunday")
    parser.add_argument("--out", type=Path, required=True, help="empty or new folder to write the tile CSV files into")
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
    lost = np.abs(decompress(packed) - kmh)
    print(f"edges whose slowest speeds were lifted: {(kmh != built).any(axis=1).sum()}")
    loss = f"median {np.median(lost):.1f}, largest {lost.max():.1f}"
    print(f"km/h lost by packing a week into {COEFFICIENTS} numbers: {loss}")

    paths = read_edge_paths(args.edge_paths, edge_ids)
    print(f"edges that {args.edge_paths.name} does not list (left out): {len(edge_ids) - len(paths)}")
    lines = tile_lines(edge_ids, kmh, free_kmh, packed, paths)
    for tile_file, rows in lines.items():
        path = args.out / tile_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(rows) + "\n")
    print(f"wrote {len(lines)} tile files under {args.out}")


if __name__ == "__main__":
    main()
