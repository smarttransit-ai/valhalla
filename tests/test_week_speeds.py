"""Tests of inrix/week_speeds.py.

    python -m pytest tests/test_week_speeds.py -q

Every segment, speed and edge here is made up.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "inrix"))

import week_speeds  # noqa: E402
from speed_profile import BUCKETS_PER_WEEK, compress, decode, decompress  # noqa: E402

KMH_PER_MPH = 1.609344
BUCKETS_PER_HOUR = 12
SUNDAY = date(2026, 7, 12)

# (segment, time, speed in mph, reference speed in mph)
READINGS = [
    (101, "2026-07-12 00:05:00", 30.0, 40.0),  # Sunday 00:05 is bucket 1
    (101, "2026-07-12 00:10:00", 20.0, 40.0),  # two readings in bucket 2 ...
    (101, "2026-07-12 00:14:00", 40.0, 40.0),  # ... that average 30
    (101, "2026-07-18 23:55:00", 50.0, 40.0),  # the last bucket of Saturday
    (101, "2026-07-11 23:55:00", 99.0, 40.0),  # the Saturday before the week
    (101, "2026-07-19 00:00:00", 99.0, 40.0),  # the Sunday after the week
    (102, "2026-07-12 00:00:00", 45.0, 55.0),  # bucket 0
    (999, "2026-07-12 00:05:00", 77.0, 70.0),  # a segment that is not asked for
]
SEGMENTS = [101, 102, 103]  # segment 103 has no reading


def edge_id(level: int, tile: int, index: int) -> int:
    """A Valhalla edge id built by hand: 3 bits of level, 22 bits of tile, then the edge's index."""
    return (index << 25) | (tile << 3) | level


EDGE_OF_TWO = edge_id(2, 756425, 7)  # segments 101 and 102 run on it
EDGE_OF_ONE = edge_id(1, 47701, 3)  # segment 103 runs on it
EDGE_UNREAD = edge_id(2, 756426, 9)  # only segment 104 runs on it, and 104 has no reading


def write_readings(rows: list[tuple], path: Path, time_type: str = "TIMESTAMP") -> None:
    """Write readings with INRIX's column names, plus one column the module does not use."""
    frame = pd.DataFrame(rows, columns=["xd_id", "measurement_tstamp", "speed", "reference_speed"])
    frame["confidence_score"] = 30
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".csv":
        frame.to_csv(path, index=False)
    else:
        typed = f"SELECT * REPLACE (measurement_tstamp::{time_type} AS measurement_tstamp) FROM frame"
        duckdb.sql(f"COPY ({typed}) TO '{path}' (FORMAT parquet)")


def test_split_edge_id_unpacks_level_tile_and_index() -> None:
    assert week_speeds.split_edge_id((7 << 25) | (756425 << 3) | 2) == (2, 756425, 7)


def test_split_edge_id_keeps_the_largest_level_and_tile_apart() -> None:
    assert week_speeds.split_edge_id((1 << 25) | (4194303 << 3) | 7) == (7, 4194303, 1)


@pytest.mark.parametrize(
    ("level", "tile", "path"),
    [(0, 2140, "0/002/140.csv"), (1, 47701, "1/047/701.csv"), (2, 756425, "2/000/756/425.csv")],
)
def test_tile_file_follows_valhallas_folder_layout(level: int, tile: int, path: str) -> None:
    assert week_speeds.tile_file(level, tile) == path


def test_read_segment_edges_lists_every_edge_of_a_segment(tmp_path: Path) -> None:
    path = tmp_path / "segment_edges.csv"
    path.write_text("XDSegID,valhalla_edge\n101,900\n102,900\n101,700\n")
    assert week_speeds.read_segment_edges(path) == {101: [900, 700], 102: [900]}


def test_fill_missing_gives_gaps_the_segments_median() -> None:
    mph = np.array([[10.0, np.nan, 30.0, 50.0], [20.0, 20.0, np.nan, 60.0]])
    filled, silent = week_speeds.fill_missing(mph)
    np.testing.assert_array_equal(filled, [[10.0, 30.0, 30.0, 50.0], [20.0, 20.0, 20.0, 60.0]])
    assert not silent.any()


def test_fill_missing_flags_a_segment_with_no_reading_and_leaves_it_empty() -> None:
    filled, silent = week_speeds.fill_missing(np.array([[10.0, np.nan], [np.nan, np.nan]]))
    assert silent.tolist() == [False, True]
    np.testing.assert_array_equal(filled[0], [10.0, 10.0])
    assert np.isnan(filled[1]).all()


def test_fill_missing_does_not_change_its_input() -> None:
    mph = np.array([[10.0, np.nan]])
    week_speeds.fill_missing(mph)
    assert np.isnan(mph[0, 1])


def made_up_edge_weeks() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Edge 900 has segments 11, 12 and the silent 14, edge 700 a crawling segment, edge 800 only the silent one."""
    filled = np.array([[30.0, 40.0], [50.0, 60.0], [2.0, 10.0], [np.nan, np.nan]])
    free_mph = np.array([40.0, 60.0, 25.0, np.nan])
    silent = np.array([False, False, False, True])
    edges_of_segment = {11: [900], 12: [900], 13: [700], 14: [900, 800]}
    return week_speeds.edge_weeks(filled, free_mph, silent, [11, 12, 13, 14], edges_of_segment)


def test_edge_weeks_are_sorted_by_edge_id() -> None:
    assert made_up_edge_weeks()[0].tolist() == [700, 900]


def test_edge_weeks_average_the_segments_on_an_edge_in_kmh() -> None:
    _, kmh, free_kmh = made_up_edge_weeks()
    np.testing.assert_allclose(kmh[1], [40.0 * KMH_PER_MPH, 50.0 * KMH_PER_MPH])
    assert free_kmh[1] == pytest.approx(50.0 * KMH_PER_MPH)


def test_edge_weeks_never_go_below_six_kmh() -> None:
    _, kmh, _ = made_up_edge_weeks()
    np.testing.assert_allclose(kmh[0], [6.0, 10.0 * KMH_PER_MPH])


def test_edge_weeks_ignore_a_segment_with_no_reading() -> None:
    _, kmh, free_kmh = made_up_edge_weeks()
    assert not np.isnan(kmh).any()
    assert not np.isnan(free_kmh).any()


def test_edge_weeks_leave_out_an_edge_that_has_only_segments_with_no_reading() -> None:
    assert 800 not in made_up_edge_weeks()[0]


def made_up_tile_lines() -> tuple[dict[str, list[str]], np.ndarray, np.ndarray]:
    """Two edges in one level 2 tile and one in a level 1 tile, the first with a speed that moves over the week."""
    wave = 50.0 + 20.0 * np.sin(np.linspace(0.0, 14.0 * np.pi, BUCKETS_PER_WEEK))
    kmh = np.vstack([wave, np.full(BUCKETS_PER_WEEK, 30.4), np.full(BUCKETS_PER_WEEK, 6.0)])
    edge_ids = np.array([edge_id(2, 756425, 7), edge_id(2, 756425, 8), edge_id(1, 47701, 3)])
    packed = compress(kmh)
    return week_speeds.tile_lines(edge_ids, kmh, np.array([96.6, 48.3, 40.2]), packed), kmh, packed


def test_tile_lines_are_grouped_by_tile_file() -> None:
    lines, _, _ = made_up_tile_lines()
    assert {name: len(rows) for name, rows in lines.items()} == {"2/000/756/425.csv": 2, "1/047/701.csv": 1}


def test_tile_lines_give_edge_free_flow_and_mean_speed_as_whole_kmh() -> None:
    lines, _, _ = made_up_tile_lines()
    assert lines["2/000/756/425.csv"][1].split(",")[:3] == ["2/756425/8", "48", "30"]
    assert lines["1/047/701.csv"][0].split(",")[:3] == ["1/47701/3", "40", "6"]


def test_tile_lines_week_decodes_back_to_the_packed_speeds() -> None:
    lines, kmh, packed = made_up_tile_lines()
    fields = lines["2/000/756/425.csv"][0].split(",")
    assert len(fields) == 4
    np.testing.assert_array_equal(decode(fields[3]), packed[0])
    np.testing.assert_allclose(decompress(decode(fields[3])), kmh[0], atol=1.0)


@pytest.fixture(params=["readings.parquet", "readings.csv"])
def week(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """What read_week returns for READINGS, once from a Parquet file and once from a CSV file."""
    path = tmp_path / request.param
    write_readings(READINGS, path)
    return week_speeds.read_week(str(path), SEGMENTS, SUNDAY, threads=1)


def test_read_week_has_one_row_per_segment_and_one_column_per_bucket(week: tuple[np.ndarray, np.ndarray]) -> None:
    mph, free_mph = week
    assert mph.shape == (len(SEGMENTS), BUCKETS_PER_WEEK)
    assert free_mph.shape == (len(SEGMENTS),)


def test_read_week_puts_sunday_0005_in_bucket_1(week: tuple[np.ndarray, np.ndarray]) -> None:
    assert week[0][0, 1] == 30.0


def test_read_week_puts_the_end_of_saturday_in_the_last_bucket(week: tuple[np.ndarray, np.ndarray]) -> None:
    assert week[0][0, BUCKETS_PER_WEEK - 1] == 50.0


def test_read_week_averages_two_readings_in_one_bucket(week: tuple[np.ndarray, np.ndarray]) -> None:
    assert week[0][0, 2] == 30.0


def test_read_week_ignores_readings_outside_the_week(week: tuple[np.ndarray, np.ndarray]) -> None:
    assert np.flatnonzero(~np.isnan(week[0][0])).tolist() == [1, 2, BUCKETS_PER_WEEK - 1]


def test_read_week_ignores_segments_that_are_not_asked_for(week: tuple[np.ndarray, np.ndarray]) -> None:
    mph, _ = week
    assert np.flatnonzero(~np.isnan(mph[1])).tolist() == [0]
    assert mph[1, 0] == 45.0
    assert 77.0 not in mph


def test_read_week_leaves_a_segment_with_no_reading_empty(week: tuple[np.ndarray, np.ndarray]) -> None:
    mph, free_mph = week
    assert np.isnan(mph[2]).all()
    assert np.isnan(free_mph[2])


def test_read_week_gives_each_segments_reference_speed(week: tuple[np.ndarray, np.ndarray]) -> None:
    assert week[1][:2].tolist() == [40.0, 55.0]


def test_read_week_reads_every_parquet_file_under_a_folder(tmp_path: Path) -> None:
    write_readings(READINGS[:1], tmp_path / "readings" / "july" / "a.parquet")
    write_readings(READINGS[6:7], tmp_path / "readings" / "b.parquet")
    mph, _ = week_speeds.read_week(str(tmp_path / "readings"), SEGMENTS, SUNDAY, threads=1)
    assert (mph[0, 1], mph[1, 0]) == (30.0, 45.0)


def test_read_week_skips_a_reading_with_no_speed(tmp_path: Path) -> None:
    rows = [(101, "2026-07-12 00:00:00", None, 40.0), (101, "2026-07-12 00:05:00", 30.0, 40.0)]
    write_readings(rows, tmp_path / "readings.parquet")
    mph, _ = week_speeds.read_week(str(tmp_path / "readings.parquet"), SEGMENTS, SUNDAY, threads=1)
    assert np.isnan(mph[0, 0])
    assert mph[0, 1] == 30.0


def test_read_week_skips_a_reading_whose_reference_speed_is_not_a_number(tmp_path: Path) -> None:
    rows = "(101, TIMESTAMP '2026-07-12 00:00', 30.0, 'nan'::DOUBLE), (101, TIMESTAMP '2026-07-12 00:05', 50.0, 40.0)"
    names = "readings(xd_id, measurement_tstamp, speed, reference_speed)"
    duckdb.sql(f"COPY (SELECT * FROM (VALUES {rows}) {names}) TO '{tmp_path / 'readings.parquet'}' (FORMAT parquet)")
    mph, free_mph = week_speeds.read_week(str(tmp_path / "readings.parquet"), [101], SUNDAY, 1)
    assert np.isnan(mph[0, 0])
    assert (mph[0, 1], free_mph[0]) == (50.0, 40.0)


def test_read_week_reads_whole_number_speeds(tmp_path: Path) -> None:
    (tmp_path / "readings.csv").write_text(
        "xd_id,measurement_tstamp,speed,reference_speed\n101,2026-07-12 00:05:00,30,40\n"
    )
    mph, free_mph = week_speeds.read_week(str(tmp_path / "readings.csv"), [101], SUNDAY, 1)
    assert (mph[0, 1], free_mph[0]) == (30.0, 40.0)


@pytest.mark.parametrize("name", ["readings.parquet", "readings.csv"])
def test_read_week_refuses_times_that_carry_a_time_zone(tmp_path: Path, name: str) -> None:
    write_readings([(101, "2026-07-12 13:00:00+00", 30.0, 40.0)], tmp_path / name, time_type="TIMESTAMPTZ")
    with pytest.raises(SystemExit, match="time zone"):
        week_speeds.read_week(str(tmp_path / name), [101], SUNDAY, 1)


def jam_week() -> tuple[np.ndarray, np.ndarray]:
    """One edge at 100 km/h with a jam at the slowest speed on weekday evenings, and where the jam is."""
    bucket = np.arange(BUCKETS_PER_WEEK)
    hour, day = bucket / BUCKETS_PER_HOUR % 24, bucket // (24 * BUCKETS_PER_HOUR)
    jam = (hour >= 16.5) & (hour < 18) & (day >= 1) & (day <= 5)
    return np.where(jam, week_speeds.MIN_SPEED_KMH, 100.0)[None, :], jam


def test_packing_alone_pulls_a_jam_under_the_speed_valhalla_uses() -> None:
    kmh, _ = jam_week()
    assert decompress(compress(kmh)).min() <= week_speeds.SLOWEST_KMH


def test_pack_lifts_a_jam_until_valhalla_can_use_every_speed() -> None:
    kmh, jam = jam_week()
    lifted, packed = week_speeds.pack(kmh)
    assert decompress(packed).min() > week_speeds.SLOWEST_KMH
    assert np.array_equal(packed, compress(lifted))


def test_pack_lifts_only_the_slow_speeds_and_keeps_the_jam_slow() -> None:
    kmh, jam = jam_week()
    lifted, _ = week_speeds.pack(kmh)
    assert (lifted[0, ~jam] == 100.0).all()
    assert week_speeds.MIN_SPEED_KMH < lifted[0, jam].max() < 30.0


def test_pack_leaves_a_week_alone_when_valhalla_can_use_it() -> None:
    kmh = np.array([np.full(BUCKETS_PER_WEEK, 40.0), np.linspace(30.0, 90.0, BUCKETS_PER_WEEK)])
    lifted, packed = week_speeds.pack(kmh)
    assert np.array_equal(lifted, kmh)
    assert np.array_equal(packed, compress(kmh))


def test_pack_does_not_change_its_input() -> None:
    kmh, _ = jam_week()
    before = kmh.copy()
    week_speeds.pack(kmh)
    assert np.array_equal(kmh, before)


@pytest.fixture
def tiny_week(tmp_path: Path) -> list[str]:
    """The arguments of a run on a tiny made-up week: two segments on one edge, one crawling, one with no reading."""
    rows = [
        (101, "2026-07-12 00:00:00", 30.0, 55.0),
        (101, "2026-07-15 08:00:00", 30.0, 55.0),
        (102, "2026-07-13 12:00:00", 50.0, 65.0),
        (103, "2026-07-18 23:55:00", 2.0, 25.0),
    ]
    write_readings(rows, tmp_path / "readings.parquet")
    edges = [(101, EDGE_OF_TWO), (102, EDGE_OF_TWO), (103, EDGE_OF_ONE), (104, EDGE_UNREAD)]
    (tmp_path / "segment_edges.csv").write_text("XDSegID,valhalla_edge\n" + "".join(f"{s},{e}\n" for s, e in edges))
    return [
        "week_speeds.py",
        *("--readings", str(tmp_path / "readings.parquet")),
        *("--segment-edges", str(tmp_path / "segment_edges.csv")),
        *("--out", str(tmp_path / "traffic")),
        "--threads=1",
    ]


def run_main(monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", arguments)
    week_speeds.main()


def files_under(folder: Path) -> set[str]:
    return {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}


def test_main_writes_one_file_per_tile_that_has_an_edge_with_readings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tiny_week: list[str]
) -> None:
    run_main(monkeypatch, [*tiny_week, "--sunday", "2026-07-12"])
    assert files_under(tmp_path / "traffic") == {"2/000/756/425.csv", "1/047/701.csv"}


def test_main_writes_one_line_per_edge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tiny_week: list[str]) -> None:
    run_main(monkeypatch, [*tiny_week, "--sunday", "2026-07-12"])
    both, crawling = ((tmp_path / "traffic" / name).read_text() for name in ("2/000/756/425.csv", "1/047/701.csv"))
    assert both.endswith("\n")
    assert crawling.endswith("\n")
    edge, free, mean, encoded = both.splitlines()[0].split(",")
    assert (len(both.splitlines()), edge, free, mean) == (1, "2/756425/7", "97", "64")
    np.testing.assert_allclose(decompress(decode(encoded)), 40.0 * KMH_PER_MPH, atol=0.05)
    assert crawling.splitlines()[0].split(",")[:3] == ["1/47701/3", "40", "6"]


def test_main_saves_the_unpacked_speeds_when_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tiny_week: list[str]
) -> None:
    run_main(monkeypatch, [*tiny_week, "--sunday", "2026-07-12", "--profile", str(tmp_path / "profile.npz")])
    profile = np.load(tmp_path / "profile.npz")
    assert profile["edge"].tolist() == [EDGE_OF_ONE, EDGE_OF_TWO]
    assert profile["kmh"].shape == (2, BUCKETS_PER_WEEK)
    np.testing.assert_allclose(profile["kmh"][0], 6.0)
    np.testing.assert_allclose(profile["free_kmh"], [25.0 * KMH_PER_MPH, 60.0 * KMH_PER_MPH], rtol=1e-6)


def test_main_writes_no_profile_unless_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tiny_week: list[str]
) -> None:
    run_main(monkeypatch, [*tiny_week, "--sunday", "2026-07-12"])
    written = files_under(tmp_path) - {"readings.parquet", "segment_edges.csv"}
    assert written == {"traffic/2/000/756/425.csv", "traffic/1/047/701.csv"}


def test_main_prints_the_command_line_first_and_then_the_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tiny_week: list[str]
) -> None:
    run_main(monkeypatch, [*tiny_week, "--sunday", "2026-07-12"])
    printed = capsys.readouterr().out
    assert printed.startswith("command: week_speeds.py --readings ")
    assert "4 segments" in printed
    assert "(left out): 1" in printed
    assert "edges with a week of speeds: 2" in printed
    assert "days with no reading: 2026-07-14, 2026-07-16, 2026-07-17" in printed
    assert "edges whose slowest speeds were lifted: 0" in printed
    assert "wrote 2 tile files" in printed


def test_main_exits_when_the_out_folder_already_holds_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tiny_week: list[str]
) -> None:
    stale = tmp_path / "traffic" / "2" / "000" / "727" / "567.csv"
    stale.parent.mkdir(parents=True)
    stale.write_text("2/727567/4337,10,10,another week\n")
    with pytest.raises(SystemExit, match="not empty"):
        run_main(monkeypatch, [*tiny_week, "--sunday", "2026-07-12"])
    assert files_under(tmp_path / "traffic") == {"2/000/727/567.csv"}


def test_main_exits_when_the_table_lists_no_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tiny_week: list[str]
) -> None:
    (tmp_path / "segment_edges.csv").write_text("XDSegID,valhalla_edge\n")
    with pytest.raises(SystemExit, match="lists no segment"):
        run_main(monkeypatch, [*tiny_week, "--sunday", "2026-07-12"])


def test_main_exits_when_the_day_is_not_a_sunday(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tiny_week: list[str]
) -> None:
    with pytest.raises(SystemExit, match="2026-07-13 is not a Sunday"):
        run_main(monkeypatch, [*tiny_week, "--sunday", "2026-07-13"])
    assert not (tmp_path / "traffic").exists()


def test_main_exits_when_no_reading_falls_in_the_week(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tiny_week: list[str]
) -> None:
    with pytest.raises(SystemExit, match="no reading"):
        run_main(monkeypatch, [*tiny_week, "--sunday", "2026-08-02"])
    assert not (tmp_path / "traffic").exists()
