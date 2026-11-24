"""Tests for inrix/segment_edges.py. No server is contacted: a fake Valhalla stands in for `requests.post`."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "inrix"))

import segment_edges  # noqa: E402

URL = "http://valhalla.test:8002"
TIMEOUT = 5.0
SHAPE = [{"lat": 50.0, "lon": 10.0}, {"lat": 50.002, "lon": 10.001}]
WHOLE_EDGE = {"id": 7001}
REFUSED = 400  # Valhalla's HTTP status for a line it cannot match

# Three made-up road pieces for the end-to-end run, told apart by the longitude their line starts at.
WALKABLE = "LINESTRING (10 50, 10.001 50.002)"
SNAPPABLE = "LINESTRING (10.1 50, 10.101 50.002, 10.102 50.004)"
UNKNOWN = "LINESTRING (10.2 50, 10.201 50.002)"
MATCHES = pd.DataFrame(
    {
        "u": [1, 1, 2, 3],
        "v": [2, 2, 3, 4],
        "key": [0, 0, 0, 0],
        "XDSegID": [101, 102, 101, 103],
        "osm_geom": [WALKABLE, WALKABLE, SNAPPABLE, UNKNOWN],
        "overlap_m": [80.5, 40.25, 120.0, 15.0],
    }
)
# Edge 7009 lies just past the end of the walkable line: the walk only touches its start.
WALK_ANSWER = [WHOLE_EDGE, {"id": 7002, "source_percent_along": 0.05}, {"id": 7009, "target_percent_along": 0.02}]
SNAP_ANSWER = [{"id": 7002, "target_percent_along": 0.9}, {"id": 7003}]
ANSWERS = {(10.0, "edge_walk"): WALK_ANSWER, (10.1, "map_snap"): SNAP_ANSWER}
EXPECTED_CSV = "XDSegID,valhalla_edge\n101,7001\n101,7002\n101,7003\n102,7001\n102,7002\n"

Reply = list | int | Exception  # the edges of a 200 answer, another HTTP status, or an error to raise


class FakeValhalla:
    """Stands in for `requests.post`: keeps every request and answers with what `reply` gives for its JSON body.

    Like Valhalla 3.5, it says where the line enters and leaves an edge only when the edge's length is asked for.
    """

    def __init__(self, reply: Callable[[dict], Reply]) -> None:
        self.reply = reply
        self.asked: list[tuple[str, dict]] = []
        self.timeouts: list[float] = []

    def __call__(self, url: str, json: dict, timeout: float) -> requests.Response:
        self.asked.append((url, json))
        self.timeouts.append(timeout)
        reply = self.reply(json)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return response(reply, {"error": "made up"})
        if "edge.length" not in json["filters"]["attributes"]:
            reply = [{"id": edge["id"]} for edge in reply]
        return response(requests.codes.ok, {"edges": reply})

    @property
    def matched_by(self) -> list[str]:
        """The shape_match of every request, in order."""
        return [body["shape_match"] for _, body in self.asked]


def response(status: int, body: dict) -> requests.Response:
    answer = requests.Response()
    answer.status_code = status
    answer._content = json.dumps(body).encode()
    return answer


def serve(monkeypatch: pytest.MonkeyPatch, reply: Callable[[dict], Reply]) -> FakeValhalla:
    """Replace `requests.post`, as the module sees it, with a fake Valhalla."""
    fake = FakeValhalla(reply)
    monkeypatch.setattr(segment_edges.requests, "post", fake)
    return fake


Run = Callable[[], tuple[FakeValhalla, Path]]  # the `run` fixture: main on the made-up matches


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Run:
    """Run `main` on the made-up matches against the fake server; gives back the server and the CSV written."""

    def run_main() -> tuple[FakeValhalla, Path]:
        matches, out = tmp_path / "matches.csv", tmp_path / "segment_edges.csv"
        MATCHES.to_csv(matches)
        fake = serve(monkeypatch, lambda body: ANSWERS.get((body["shape"][0]["lon"], body["shape_match"]), REFUSED))
        argv = ["segment_edges.py", "--matches", str(matches), "--out", str(out)]
        argv += ["--valhalla", URL, "--timeout", str(TIMEOUT)]
        monkeypatch.setattr(sys, "argv", argv)
        segment_edges.main()
        return fake, out

    return run_main


def test_piece_shapes_gives_one_lat_lon_shape_per_distinct_piece() -> None:
    matches = pd.DataFrame(
        {
            "u": [1, 1, 1, 2],
            "v": [2, 2, 2, 3],
            "key": [0, 0, 1, 0],
            "XDSegID": [101, 102, 101, 101],
            "osm_geom": [WALKABLE, WALKABLE, "LINESTRING (10 50, 10.0005 50.003, 10.001 50.002)", SNAPPABLE],
        }
    )
    assert segment_edges.piece_shapes(matches) == {
        (1, 2, 0): [{"lat": 50.0, "lon": 10.0}, {"lat": 50.002, "lon": 10.001}],
        (1, 2, 1): [{"lat": 50.0, "lon": 10.0}, {"lat": 50.003, "lon": 10.0005}, {"lat": 50.002, "lon": 10.001}],
        (2, 3, 0): [{"lat": 50.0, "lon": 10.1}, {"lat": 50.002, "lon": 10.101}, {"lat": 50.004, "lon": 10.102}],
    }


@pytest.mark.parametrize(
    ("edge", "kept"),
    [
        pytest.param({"id": 7, "source_percent_along": 0.0, "target_percent_along": 1.0}, True, id="covered 1.0"),
        pytest.param({"id": 7, "source_percent_along": 0.05, "target_percent_along": 1.0}, True, id="covered 0.95"),
        pytest.param({"id": 7, "source_percent_along": 0.25, "target_percent_along": 0.75}, True, id="covered 0.5"),
        pytest.param({"id": 7, "source_percent_along": 0.5, "target_percent_along": 0.75}, False, id="covered 0.25"),
        pytest.param({"id": 7, "source_percent_along": 0.0, "target_percent_along": 0.0}, False, id="covered 0.0"),
        pytest.param({"id": 7}, True, id="no percents: the whole edge"),
        pytest.param({"id": 7, "target_percent_along": 0.25}, False, id="no source: from the start"),
        pytest.param({"id": 7, "source_percent_along": 0.75}, False, id="no target: to the end"),
    ],
)
def test_covered_edges_keeps_an_edge_only_when_at_least_half_is_covered(edge: dict, kept: bool) -> None:
    assert segment_edges.covered_edges([edge]) == ([edge["id"]] if kept else [])


def test_covered_edges_keeps_the_order_walked() -> None:
    edges = [{"id": 9, "source_percent_along": 0.1}, {"id": 3}, {"id": 5, "target_percent_along": 0.02}, {"id": 4}]
    assert segment_edges.covered_edges(edges) == [9, 3, 4]


def test_ask_posts_the_shape_to_trace_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = serve(monkeypatch, lambda body: [WHOLE_EDGE])
    assert segment_edges.ask(URL, SHAPE, segment_edges.WALKED, TIMEOUT) == [WHOLE_EDGE]
    ((url, body),) = fake.asked
    assert url == URL + "/trace_attributes"
    assert (body["shape"], body["costing"], body["shape_match"]) == (SHAPE, "auto", "edge_walk")
    assert fake.timeouts == [TIMEOUT]


def test_ask_gets_where_the_line_enters_and_leaves_each_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, lambda body: WALK_ANSWER)
    assert segment_edges.ask(URL, SHAPE, segment_edges.WALKED, TIMEOUT) == WALK_ANSWER


def test_ask_accepts_a_base_url_ending_in_a_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = serve(monkeypatch, lambda body: [WHOLE_EDGE])
    segment_edges.ask(URL + "/", SHAPE, segment_edges.WALKED, TIMEOUT)
    assert fake.asked[0][0] == URL + "/trace_attributes"


def test_ask_gives_none_when_valhalla_refuses_the_line(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, lambda body: REFUSED)
    assert segment_edges.ask(URL, SHAPE, segment_edges.WALKED, TIMEOUT) is None


def test_edges_of_piece_walks_the_line_and_asks_once_when_the_walk_works(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = serve(monkeypatch, lambda body: [WHOLE_EDGE])
    assert segment_edges.edges_of_piece(URL, SHAPE, TIMEOUT) == (segment_edges.WALKED, [WHOLE_EDGE])
    assert fake.matched_by == ["edge_walk"]


def test_edges_of_piece_falls_back_to_map_snap_when_the_walk_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    snapped = [{"id": 7002, "target_percent_along": 0.9}]
    fake = serve(monkeypatch, lambda body: snapped if body["shape_match"] == "map_snap" else REFUSED)
    assert segment_edges.edges_of_piece(URL, SHAPE, TIMEOUT) == (segment_edges.SNAPPED, snapped)
    assert fake.matched_by == ["edge_walk", "map_snap"]


def test_edges_of_piece_gives_no_edges_and_failed_when_both_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = serve(monkeypatch, lambda body: REFUSED)
    assert segment_edges.edges_of_piece(URL, SHAPE, TIMEOUT) == (segment_edges.FAILED, [])
    assert fake.matched_by == ["edge_walk", "map_snap"]


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        pytest.param(404, requests.HTTPError, id="not found"),
        pytest.param(500, requests.HTTPError, id="server error"),
        pytest.param(requests.ConnectionError("refused"), requests.ConnectionError, id="no server"),
        pytest.param(requests.Timeout("timed out"), requests.Timeout, id="timeout"),
    ],
)
def test_a_server_that_does_not_answer_properly_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, reply: Reply, error: type[Exception]
) -> None:
    fake = serve(monkeypatch, lambda body: reply)
    with pytest.raises(error):
        segment_edges.edges_of_piece(URL, SHAPE, TIMEOUT)
    assert fake.matched_by == ["edge_walk"]


def test_segment_edge_pairs_are_sorted_and_without_repeats() -> None:
    edges_of = {(1, 2, 0): [7002, 7001], (2, 3, 0): [7002, 7003], (3, 4, 0): [7001]}
    pairs = segment_edges.segment_edge_pairs(MATCHES.iloc[::-1], edges_of)
    assert pairs == [(101, 7001), (101, 7002), (101, 7003), (102, 7001), (102, 7002), (103, 7001)]


def test_segment_edge_pairs_give_each_segment_on_a_piece_that_pieces_edges() -> None:
    edges_of = {(1, 2, 0): [7001, 7002], (2, 3, 0): [], (3, 4, 0): []}
    pairs = segment_edges.segment_edge_pairs(MATCHES, edges_of)
    assert pairs == [(101, 7001), (101, 7002), (102, 7001), (102, 7002)]


def test_segment_edge_pairs_leave_out_a_segment_whose_pieces_gave_no_edge() -> None:
    edges_of = {(1, 2, 0): [7001], (2, 3, 0): [7003], (3, 4, 0): []}
    segments = {segment for segment, _ in segment_edges.segment_edge_pairs(MATCHES, edges_of)}
    assert segments == {101, 102}


def test_main_writes_one_row_per_segment_and_edge_the_line_covers(run: Run) -> None:
    _, out = run()
    assert out.read_text() == EXPECTED_CSV


def test_main_asks_once_per_distinct_piece_with_the_given_timeout(run: Run) -> None:
    fake, _ = run()
    assert [(body["shape"][0]["lon"], body["shape_match"]) for _, body in fake.asked] == [
        (10.0, "edge_walk"),
        (10.1, "edge_walk"),
        (10.1, "map_snap"),
        (10.2, "edge_walk"),
        (10.2, "map_snap"),
    ]
    assert {url for url, _ in fake.asked} == {URL + "/trace_attributes"}
    assert set(fake.timeouts) == {TIMEOUT}


def test_main_prints_the_command_line_then_the_summary(run: Run, capsys: pytest.CaptureFixture[str]) -> None:
    run()
    command, *summary = capsys.readouterr().out.splitlines()
    assert "--matches" in command
    assert "--valhalla " + URL in command
    assert dict(line.split(": ") for line in summary) == {
        "matches read": "4",
        "distinct pieces": "3",
        "pieces walked": "1",
        "pieces snapped": "1",
        "pieces failed": "1",
        "edges returned": "5",
        "edges dropped (covered under 0.5)": "1",
        "pieces left with no edge": "1",
        "pairs written": "5",
        "distinct segments": "2",
        "distinct edges": "3",
        "edges carrying more than one segment": "2",
    }


def test_main_prints_a_progress_line_to_stderr_every_progress_every_pieces(
    monkeypatch: pytest.MonkeyPatch, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(segment_edges, "PROGRESS_EVERY", 2)
    run()
    assert capsys.readouterr().err.splitlines() == ["2 of 3 pieces asked"]
