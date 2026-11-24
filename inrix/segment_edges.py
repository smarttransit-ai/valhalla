"""Find the Valhalla edges under each INRIX XD segment and write the table XDSegID,valhalla_edge.

    python inrix/segment_edges.py --matches matches.csv --out segment_edges.csv --valhalla http://localhost:8002

The matches file comes from geo-mapper-api, which matches each XD segment to pieces of the OpenStreetMap road
network. Call `inrix_to_osm.serial(geojson, csv, counties, osm_xml=...)` on the same OSM file the Valhalla tiles
were built from, and save the table it returns with `to_csv`. Each row names one road piece (`u`, `v`, `key`),
one segment matched to it (`XDSegID`) and the piece's line (`osm_geom`, WKT with the longitude first).

The line of every distinct piece is sent to a running Valhalla server, which answers with its own edges under
the line. Valhalla also returns the edge just before the start and the edge just past the end of a line. They
belong to the next blocks, and the line touches almost none of their length, so an edge counts only when the
line covers at least `--min-covered` of it. Every segment matched to a piece gets the edges of that piece.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from collections import Counter

import pandas as pd
import requests
from shapely import wkt

Piece = tuple[int, int, int]  # (u, v, key)


def piece_shapes(matches: pd.DataFrame) -> dict[Piece, list[dict[str, float]]]:
    """Each distinct road piece -> its line as Valhalla takes it."""
    pieces = matches.drop_duplicates(["u", "v", "key"])
    return {
        (u, v, key): [{"lat": lat, "lon": lon} for lon, lat in wkt.loads(line).coords]
        for u, v, key, line in pieces[["u", "v", "key", "osm_geom"]].itertuples(index=False, name=None)
    }


def covered_edges(edges: list[dict], min_covered: float) -> list[int]:
    """The ids of the edges the line covers at least `min_covered` of, in the order walked.

    Valhalla says where the line enters an edge (`source_percent_along`, absent for its start) and where it
    leaves it (`target_percent_along`, absent for its end).
    """
    return [
        edge["id"]
        for edge in edges
        if edge.get("target_percent_along", 1.0) - edge.get("source_percent_along", 0.0) >= min_covered
    ]


def ask(url: str, shape: list[dict[str, float]], how: str, timeout: float) -> list[dict] | None:
    """Valhalla's edges under one line, in the order walked, or None when Valhalla cannot match the line."""
    body = {
        "shape": shape,
        "costing": "auto",
        "shape_match": how,
        # Asked for an edge's length, Valhalla 3.5 also says where the line enters and leaves the edge.
        "filters": {"attributes": ["edge.id", "edge.length"], "action": "include"},
    }
    answer = requests.post(url.rstrip("/") + "/trace_attributes", json=body, timeout=timeout)
    if answer.status_code == requests.codes.bad_request:  # Valhalla's status for a line it cannot match
        return None
    answer.raise_for_status()
    return answer.json()["edges"]


def edges_of_piece(url: str, shape: list[dict[str, float]], timeout: float) -> tuple[str, list[dict]]:
    """(how the line was matched, Valhalla's edges) for one piece: its exact line is walked, else snapped to the map."""
    for how in ("edge_walk", "map_snap"):  # Valhalla's shape_match values
        edges = ask(url, shape, how, timeout)
        if edges is not None:
            return how, edges
    return "nothing", []


def segment_edge_pairs(matches: pd.DataFrame, edges_of: dict[Piece, list[int]]) -> list[tuple[int, int]]:
    """Every (segment, Valhalla edge) pair, sorted: a segment gets the edges of each piece it is matched to."""
    rows = matches[["u", "v", "key", "XDSegID"]].itertuples(index=False, name=None)
    return sorted({(segment, edge) for u, v, key, segment in rows for edge in edges_of[(u, v, key)]})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--matches", required=True, help="the table geo-mapper-api returns, saved with to_csv")
    parser.add_argument("--out", required=True, help="CSV to write, with the columns XDSegID,valhalla_edge")
    parser.add_argument("--valhalla", default="http://localhost:8002", help="the Valhalla server (%(default)s)")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per request (%(default)s)")
    covered = "share of an edge a line has to cover for the edge to count (%(default)s)"
    parser.add_argument("--min-covered", type=float, default=0.5, help=covered)
    args = parser.parse_args()
    print("command:", shlex.join(sys.argv))

    matches = pd.read_csv(args.matches, usecols=["u", "v", "key", "XDSegID", "osm_geom"])
    shapes = piece_shapes(matches)
    print(f"matches read: {len(matches)}")
    print(f"distinct pieces, sent to Valhalla one at a time: {len(shapes)}", flush=True)
    edges_of: dict[Piece, list[int]] = {}
    matched_by: Counter[str] = Counter()
    returned = 0
    for piece, shape in shapes.items():
        how, edges = edges_of_piece(args.valhalla, shape, args.timeout)
        edges_of[piece] = covered_edges(edges, args.min_covered)
        matched_by[how] += 1
        returned += len(edges)

    table = pd.DataFrame(segment_edge_pairs(matches, edges_of), columns=["XDSegID", "valhalla_edge"])
    table.to_csv(args.out, index=False)
    for how, pieces in matched_by.most_common():
        print(f"pieces matched by {how}: {pieces}")
    print(f"edges returned: {returned}")
    print(f"edges dropped (covered under {args.min_covered}): {returned - sum(map(len, edges_of.values()))}")
    print(f"pieces left with no edge: {sum(not edges for edges in edges_of.values())}")
    print(f"pairs written: {len(table)}")
    print(f"distinct segments: {table['XDSegID'].nunique()}")
    print(f"distinct edges: {table['valhalla_edge'].nunique()}")
    print(f"edges carrying more than one segment: {(table['valhalla_edge'].value_counts() > 1).sum()}")


if __name__ == "__main__":
    main()
