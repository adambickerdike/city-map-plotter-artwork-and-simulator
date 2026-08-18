#!/usr/bin/env python3
"""Derive rowing course centre-lines from OpenStreetMap and write courses-v1.json.

    python3 tools/build_course_geometry.py            # all courses
    python3 tools/build_course_geometry.py horr       # one course

This repository refuses to draw a route it cannot source. Catalog marathons
carry `COURSE NOT INCLUDED` for exactly that reason: nobody has imported an
official route, so none is claimed.

A rowing head course is a different problem, and a tractable one. It is not an
arbitrary path through a city; it is *the river*, between two named places the
organiser publishes. So both halves are sourced:

* the **start and finish** are named OSM features -- Chiswick Bridge, Temple
  Island, the DeWolfe Boathouse -- whose positions come from OSM, matched to the
  organiser's own published course description (recorded in `source_urls`);
* the **line between them** is the OSM `waterway=river` centre-line, merged,
  cut at the projection of each endpoint, and measured.

The measured length is written next to the official distance so the two can be
compared. They will not match exactly: a head course is rowed on the racing
line, not the centre-line, and the tideway stream moves. A course whose measured
length is more than 12% from the published figure is rejected rather than
shipped, because that means the wrong river reach was cut.

Nothing here runs at render time. This is an offline generator, like
`build_format_spec.py`, and its output is committed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge, substring

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "src" / "city_map_plotter" / "data" / "rowing-courses-v1.json"
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = "CityMapPlotter-CourseBuilder/0.2 (+https://www.openstreetmap.org/copyright)"

SCHEMA_VERSION = 1
CATALOG_ID = "city-map-plotter-rowing-courses-v1"

#: How far the measured centre-line may sit from the published distance before
#: the cut is treated as wrong rather than merely approximate.
LENGTH_TOLERANCE = 0.12

#: Waypoint spacing after resampling. 25 m is well under one plotted nib at any
#: sheet size and keeps the committed file to a few hundred points per course.
RESAMPLE_M = 25.0

#: Distance marks are drawn every this many metres along the course, labelled in
#: whichever unit the event itself uses.
MILE_M = 1609.344

#: Everything about a course that a person wrote down rather than measured.
DESCRIPTIVE_FIELDS = (
    "id",
    "name",
    "title",
    "event",
    "river",
    "reach",
    "city",
    "country",
    "start",
    "finish",
    "official_distance_m",
    "official_distance_label",
    "direction",
    "first_held",
    "boats",
    "held",
    "founder",
    "notes",
    "poster",
    "landmarks",
    "banks",
    "distance_marks",
    "source_urls",
)


# --------------------------------------------------------------------------
# Course definitions: everything a person had to look up, with its source.
# --------------------------------------------------------------------------

#: Landmarks to label on each course. Every one is looked up in OSM by name
#: inside the course bbox and projected onto the course line, so the label sits
#: where the course actually passes it rather than where it was guessed.
#: Banks are the station names a crew uses, offset to their own side.
COURSE_LANDMARKS: dict[str, list[dict[str, Any]]] = {
    "horr-london": [
        {"osm": "Chiswick Bridge", "label": "CHISWICK BR", "kind": "bridge"},
        {"osm": "Barnes Bridge", "label": "BARNES BR", "kind": "bridge"},
        {"osm": "Hammersmith Bridge", "label": "HAMMERSMITH BR", "kind": "bridge"},
        {"osm": "Putney Bridge", "label": "PUTNEY BR", "kind": "bridge"},
        {"osm": "Mortlake", "label": "MORTLAKE", "kind": "place"},
        {"osm": "Barnes", "label": "BARNES", "kind": "place"},
        {"osm": "Chiswick", "label": "CHISWICK", "kind": "place"},
        {"osm": "Hammersmith", "label": "HAMMERSMITH", "kind": "place"},
        {"osm": "Fulham", "label": "FULHAM", "kind": "place"},
        {"osm": "Putney", "label": "PUTNEY", "kind": "place"},
        {"osm": "London Rowing Club", "label": "LONDON RC", "kind": "club"},
        {"osm": "Thames Rowing Club", "label": "THAMES RC", "kind": "club"},
        {"osm": "Vesta Rowing Club", "label": "VESTA RC", "kind": "club"},
        {"osm": "Imperial College Boat Club", "label": "IMPERIAL BC", "kind": "club"},
        {"osm": "Fulham Reach Boat Club", "label": "FULHAM REACH", "kind": "club"},
        {"osm": "Auriol Kensington Rowing Club", "label": "AURIOL KENSINGTON", "kind": "club"},
        {"osm": "Furnivall Sculling Club", "label": "FURNIVALL SC", "kind": "club"},
        {"osm": "Tideway Scullers School", "label": "TIDEWAY SCULLERS", "kind": "club"},
        {"osm": "Quintin Boat Club", "label": "QUINTIN BC", "kind": "club"},
        {"osm": "Craven Cottage", "label": "CRAVEN COTTAGE", "kind": "landmark"},
        {"osm": "Harrods Wharf", "label": "HARRODS", "kind": "landmark"},
        {"osm": "Chiswick Eyot", "label": "CHISWICK EYOT", "kind": "landmark"},
        {"osm": "St Paul's School", "label": "ST PAUL'S", "kind": "landmark"},
        {"osm": "Dukes Meadows", "label": "DUKES MEADOWS", "kind": "landmark"},
    ],
    "pairs-head-london": [
        {"osm": "Chiswick Bridge", "label": "CHISWICK BR", "kind": "bridge"},
        {"osm": "Barnes Bridge", "label": "BARNES BR", "kind": "bridge"},
        {"osm": "Mortlake", "label": "MORTLAKE", "kind": "place"},
        {"osm": "Barnes", "label": "BARNES", "kind": "place"},
        {"osm": "Chiswick", "label": "CHISWICK", "kind": "place"},
        {"osm": "Hammersmith", "label": "HAMMERSMITH", "kind": "place"},
        {"osm": "Harrods Wharf", "label": "HARRODS", "kind": "landmark"},
        {"osm": "Chiswick Eyot", "label": "CHISWICK EYOT", "kind": "landmark"},
        {"osm": "Dukes Meadows", "label": "DUKES MEADOWS", "kind": "landmark"},
        {"osm": "Tideway Scullers School", "label": "TIDEWAY SCULLERS", "kind": "club"},
        {"osm": "Mortlake Anglian and Alpha Boat Club", "label": "MORTLAKE A&A", "kind": "club"},
        {"osm": "Barnes Bridge Ladies Rowing Club", "label": "BARNES BRIDGE LADIES", "kind": "club"},
        {"osm": "Auriol Kensington Rowing Club", "label": "AURIOL KENSINGTON", "kind": "club"},
    ],
    "henley-royal": [
        {"osm": "Temple Island", "label": "TEMPLE ISLAND", "kind": "landmark"},
        {"osm": "Phyllis Court Riverside Pavilion", "label": "PHYLLIS COURT", "kind": "landmark"},
        {"osm": "Stewards' Enclosure", "label": "STEWARDS' ENCLOSURE", "kind": "landmark"},
        {"osm": "Fawley Court", "label": "FAWLEY COURT", "kind": "landmark"},
        {"osm": "Remenham Club", "label": "REMENHAM CLUB", "kind": "club"},
        {"osm": "Leander Club", "label": "LEANDER CLUB", "kind": "club"},
        {"osm": "Upper Thames Rowing Club", "label": "UPPER THAMES RC", "kind": "club"},
        {"osm": "Henley Bridge", "label": "HENLEY BRIDGE", "kind": "bridge"},
        {"osm": "Henley-on-Thames", "label": "HENLEY", "kind": "place"},
        {"osm": "Remenham", "label": "REMENHAM", "kind": "place"},
    ],
    "head-of-the-charles": [
        {"osm": "Boston University Bridge", "label": "BU BRIDGE", "kind": "bridge"},
        {"osm": "River Street Bridge", "label": "RIVER ST BR", "kind": "bridge"},
        {"osm": "Western Avenue Bridge", "label": "WESTERN AVE BR", "kind": "bridge"},
        {"osm": "Weeks Footbridge", "label": "WEEKS BR", "kind": "bridge"},
        {"osm": "Anderson Memorial Bridge", "label": "ANDERSON BR", "kind": "bridge"},
        {"osm": "Eliot Bridge", "label": "ELIOT BR", "kind": "bridge"},
        {"osm": "DeWolfe Boathouse", "label": "BU BOATHOUSE", "kind": "club"},
        {"osm": "Weld Boathouse", "label": "WELD BOATHOUSE", "kind": "club"},
        {"osm": "Newell Boathouse", "label": "NEWELL BOATHOUSE", "kind": "club"},
        {"osm": "Henderson Boathouse", "label": "HENDERSON BOATHOUSE", "kind": "club"},
        {"osm": "Riverside Boat Club", "label": "RIVERSIDE BC", "kind": "club"},
        {"osm": "Cambridge Boat Club", "label": "CAMBRIDGE BC", "kind": "club"},
        {"osm": "Herter Park", "label": "HERTER PARK", "kind": "landmark"},
        {"osm": "Harvard Stadium", "label": "HARVARD STADIUM", "kind": "landmark"},
        {"osm": "Cambridgeport", "label": "CAMBRIDGEPORT", "kind": "place"},
        {"osm": "Allston", "label": "ALLSTON", "kind": "place"},
    ],
}


#: Station names, and which side of the course they lie. ``+1`` is to the left
#: of the direction of travel, ``-1`` to the right.
COURSE_BANKS: dict[str, list[dict[str, Any]]] = {
    "horr-london": [
        {"label": "MIDDLESEX", "side": 1, "at": 0.35},
        {"label": "SURREY", "side": -1, "at": 0.35},
    ],
    "pairs-head-london": [
        {"label": "MIDDLESEX", "side": 1, "at": 0.45},
        {"label": "SURREY", "side": -1, "at": 0.45},
    ],
    # Henley races upstream, Temple Island to Poplar Point, heading south-west.
    # Left of travel is therefore the SOUTH-EAST bank: Remenham, the towpath and
    # the Stewards' Enclosure — the Berkshire station. Fawley Court on the
    # north-west bank is the Buckinghamshire station.
    "henley-royal": [
        {"label": "BERKS", "side": 1, "at": 0.5},
        {"label": "BUCKS", "side": -1, "at": 0.5},
    ],
    # The Charles is raced upstream, BU Boathouse to Herter Park, heading
    # broadly west. Left of travel is the SOUTH bank — Allston/Brighton,
    # which is Boston. Weld Boathouse and Harvard Square sit on the north
    # bank, which is Cambridge.
    "head-of-the-charles": [
        {"label": "BOSTON", "side": 1, "at": 0.55},
        {"label": "CAMBRIDGE", "side": -1, "at": 0.55},
    ],
}


COURSES: list[dict[str, Any]] = [
    {
        "id": "horr-london",
        "poster": {"title": "HEAD OF THE RIVER", "subtitle": "RIVER THAMES / LONDON", "course_line": "MORTLAKE TO PUTNEY - THE BOAT RACE COURSE IN REVERSE", "fields": [["DISTANCE", "4 MILES 374 YDS"], ["FIRST ROWED", "1926"], ["BOATS", "EIGHTS"]]},

        "name": "Head of the River Race",
        "title": "HEAD OF THE RIVER",
        "event": "Head of the River Race",
        "river": "River Thames",
        "reach": "Championship Course",
        "city": "London",
        "country": "United Kingdom",
        "start": {"label": "Mortlake", "osm": "Chiswick Bridge", "lat": 51.473165, "lon": -0.269780},
        "finish": {"label": "Putney", "osm": "Putney Bridge", "lat": 51.466092, "lon": -0.213847},
        "official_distance_m": 6779,
        "official_distance_label": "4 MILES 374 YDS",
        "direction": "DOWNSTREAM ON THE EBB",
        "first_held": 1926,
        "boats": "EIGHTS",
        "held": "MARCH",
        "founder": "Steve Fairbairn",
        "notes": [
            "The Boat Race course rowed in reverse, Mortlake to Putney.",
            "Started in 1926 by Steve Fairbairn with 21 crews; now capped at 400.",
        ],
        "source_urls": [
            "https://en.wikipedia.org/wiki/Head_of_the_River_Race",
            "https://www.theboatrace.org/the-course",
        ],
        "river_query": {"name": "River Thames", "bbox": [51.4600, -0.2800, 51.4950, -0.2050]},
    },
    {
        "id": "pairs-head-london",
        "poster": {"title": "PAIRS HEAD", "subtitle": "RIVER THAMES / LONDON", "course_line": "CHISWICK BRIDGE TO HARRODS WALL - UPPER CHAMPIONSHIP COURSE", "fields": [["DISTANCE", "4.5 KM"], ["ROWED", "OCTOBER"], ["BOATS", "2- AND 2X"]]},

        "name": "Pairs Head of the River Race",
        "title": "PAIRS HEAD",
        "event": "Pairs Head of the River Race",
        "river": "River Thames",
        "reach": "Championship Course",
        "city": "London",
        "country": "United Kingdom",
        "start": {"label": "Chiswick Bridge", "osm": "Chiswick Bridge", "lat": 51.473165, "lon": -0.269780},
        "finish": {"label": "Harrods Wall", "osm": "Harrods Wharf", "lat": 51.484721, "lon": -0.227819},
        "official_distance_m": 4500,
        "official_distance_label": "APPROX 4.5 KM",
        "direction": "DOWNSTREAM ON THE EBB",
        "first_held": None,
        "boats": "PAIRS AND DOUBLES",
        "held": "OCTOBER",
        "founder": None,
        "notes": [
            "Chiswick Bridge to Harrods Wall, the upper Championship Course.",
            "Run each October by Barnes Bridge Ladies Rowing Club.",
        ],
        "source_urls": [
            "https://www.bblrc.co.uk/pairshead/",
        ],
        "river_query": {"name": "River Thames", "bbox": [51.4600, -0.2800, 51.4950, -0.2050]},
    },
    {
        "id": "henley-royal",
        "poster": {"title": "HENLEY ROYAL", "subtitle": "RIVER THAMES / HENLEY", "course_line": "TEMPLE ISLAND TO POPLAR POINT - BOOMED STRAIGHT SINCE 1924", "fields": [["DISTANCE", "1 MILE 550 YDS"], ["FIRST HELD", "1839"], ["RACING", "TWO ABREAST"]]},

        "name": "Henley Royal Regatta",
        "title": "HENLEY ROYAL",
        "event": "Henley Royal Regatta",
        "river": "River Thames",
        "reach": "Henley Reach",
        "city": "Henley-on-Thames",
        "country": "United Kingdom",
        "start": {"label": "Temple Island", "osm": "Temple Island", "lat": 51.556931, "lon": -0.888989},
        "finish": {"label": "Poplar Point", "osm": "Phyllis Court Riverside Pavilion", "lat": 51.540767, "lon": -0.900606},
        "official_distance_m": 2112,
        "official_distance_label": "1 MILE 550 YDS",
        "direction": "UPSTREAM, SIDE BY SIDE",
        "first_held": 1839,
        "boats": "ALL CLASSES",
        "held": "JULY",
        "founder": None,
        "notes": [
            "Temple Island to Poplar Point, boomed straight since 1924.",
            "Two crews only, match racing over six days from 1839.",
        ],
        "source_urls": [
            "https://en.wikipedia.org/wiki/Henley_Royal_Regatta",
        ],
        "river_query": {"name": "River Thames", "bbox": [51.5350, -0.9150, 51.5620, -0.8800]},
    },
    {
        "id": "head-of-the-charles",
        "poster": {"title": "HEAD OF THE CHARLES", "subtitle": "CHARLES RIVER / BOSTON", "course_line": "BU BOATHOUSE TO HERTER PARK - SIX BRIDGES AND THE WEEKS TURN", "fields": [["DISTANCE", "3 MILES"], ["FIRST ROWED", "1965"], ["ROWED", "OCTOBER"]]},

        "name": "Head of the Charles Regatta",
        "title": "HEAD OF THE CHARLES",
        "event": "Head of the Charles Regatta",
        "river": "Charles River",
        "reach": "Boston and Cambridge",
        "city": "Boston",
        "country": "United States",
        "start": {"label": "BU Boathouse", "osm": "DeWolfe Boathouse", "lat": 42.353253, "lon": -71.107770},
        "finish": {"label": "Herter Park", "osm": "Herter Park", "lat": 42.367683, "lon": -71.137259},
        "official_distance_m": 4800,
        "official_distance_label": "3 MILES / 4.8 KM",
        "direction": "UPSTREAM",
        "first_held": 1965,
        "boats": "ALL CLASSES",
        "held": "OCTOBER",
        "founder": None,
        "notes": [
            "BU Boathouse to Herter Park, six bridges and the Weeks turn.",
            "First rowed 1965; now some 11,000 athletes over three days.",
        ],
        "source_urls": [
            "https://en.wikipedia.org/wiki/Head_of_the_Charles_Regatta",
            "https://hocr.org/spectators/where-to-watch/",
        ],
        "river_query": {"name": "Charles River", "bbox": [42.3480, -71.1500, 42.3760, -71.1000]},
    },
]


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


def _metres_per_degree(latitude: float) -> tuple[float, float]:
    """Local scale factors, good enough over a few kilometres of river."""

    return 111_320.0, 111_320.0 * math.cos(math.radians(latitude))


def _to_local(points: list[tuple[float, float]], origin_lat: float) -> LineString:
    """Project lat/lon to a local equirectangular metre grid."""

    lat_m, lon_m = _metres_per_degree(origin_lat)
    return LineString([(lon * lon_m, lat * lat_m) for lat, lon in points])


def _to_wgs84(line: LineString, origin_lat: float) -> list[tuple[float, float]]:
    lat_m, lon_m = _metres_per_degree(origin_lat)
    return [(y / lat_m, x / lon_m) for x, y in line.coords]


def overpass(query: str) -> dict[str, Any]:
    """Ask Overpass, politely and with a fallback.

    The public instances rate-limit and time out under load. This is an offline
    generator run by hand, so it waits and tries a mirror rather than failing a
    whole build on one 429.
    """

    last_error: Exception | None = None
    for attempt, endpoint in enumerate(OVERPASS_ENDPOINTS * 3):
        request = urllib.request.Request(
            endpoint,
            data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
            headers={"User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            wait = min(60, 8 * (attempt + 1))
            print(f"    {endpoint}: {exc} — retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise SystemExit(f"Overpass request failed after retries: {last_error}")


def fetch_river(name: str, bbox: list[float]) -> list[list[tuple[float, float]]]:
    south, west, north, east = bbox
    query = (
        f"[out:json][timeout:120];"
        f'way["waterway"="river"]["name"="{name}"]'
        f"({south},{west},{north},{east});out geom;"
    )
    payload = overpass(query)
    ways = [
        [(point["lat"], point["lon"]) for point in element.get("geometry", [])]
        for element in payload.get("elements", [])
        if element.get("type") == "way" and element.get("geometry")
    ]
    if not ways:
        raise SystemExit(f"No {name!r} centre-line found in {bbox}.")
    return ways


def merged_centreline(
    ways: list[list[tuple[float, float]]], origin_lat: float
) -> LineString:
    lines = [_to_local(way, origin_lat) for way in ways if len(way) >= 2]
    merged = linemerge(MultiLineString(lines))
    if isinstance(merged, LineString):
        return merged
    # The reach is mapped as several ways that do not all connect (side
    # channels, islands). Keep the longest connected run: a head course is
    # rowed on the navigable channel, which is always the longest.
    parts = sorted(merged.geoms, key=lambda part: part.length, reverse=True)
    return parts[0]


def cut_course(
    centreline: LineString,
    start: tuple[float, float],
    finish: tuple[float, float],
    origin_lat: float,
) -> tuple[LineString, float, float]:
    lat_m, lon_m = _metres_per_degree(origin_lat)
    start_point = Point(start[1] * lon_m, start[0] * lat_m)
    finish_point = Point(finish[1] * lon_m, finish[0] * lat_m)
    start_distance = centreline.project(start_point)
    finish_distance = centreline.project(finish_point)
    low, high = sorted((start_distance, finish_distance))
    cut = substring(centreline, low, high)
    if start_distance > finish_distance:
        cut = LineString(list(cut.coords)[::-1])
    return (
        cut,
        start_point.distance(centreline),
        finish_point.distance(centreline),
    )


def resample(line: LineString, step_m: float) -> LineString:
    count = max(2, int(round(line.length / step_m)) + 1)
    return LineString(
        [line.interpolate(index / (count - 1), normalized=True) for index in range(count)]
    )


def fetch_named_features(
    names: list[str], bbox: list[float]
) -> dict[str, tuple[float, float]]:
    """Look up each named OSM feature inside the course bbox, by centre."""

    if not names:
        return {}
    south, west, north, east = bbox
    clauses = "".join(
        f'nwr["name"="{name}"]({south},{west},{north},{east});' for name in names
    )
    payload = overpass(f"[out:json][timeout:120];({clauses});out tags center;")
    found: dict[str, list[tuple[float, float]]] = {}
    for element in payload.get("elements", []):
        name = (element.get("tags") or {}).get("name")
        centre = element.get("center") or {
            "lat": element.get("lat"),
            "lon": element.get("lon"),
        }
        if name is None or centre.get("lat") is None:
            continue
        found.setdefault(name, []).append(
            (float(centre["lat"]), float(centre["lon"]))
        )
    # A bridge is mapped as several ways; take the mean so the label lands on
    # the crossing rather than on one carriageway.
    return {
        name: (
            sum(lat for lat, _ in points) / len(points),
            sum(lon for _, lon in points) / len(points),
        )
        for name, points in found.items()
    }


def _markers_for(
    course: dict[str, Any],
    cut: LineString,
    origin_lat: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Landmarks projected onto the course, banks, and distance marks."""

    lat_m, lon_m = _metres_per_degree(origin_lat)
    entries = COURSE_LANDMARKS.get(course["id"], [])
    positions = fetch_named_features(
        [entry["osm"] for entry in entries], course["river_query"]["bbox"]
    )
    landmarks: list[dict[str, Any]] = []
    for entry in entries:
        position = positions.get(entry["osm"])
        if position is None:
            print(f"    ! no OSM feature named {entry['osm']!r}", file=sys.stderr)
            continue
        point = Point(position[1] * lon_m, position[0] * lat_m)
        along = cut.project(point)
        on_course = cut.interpolate(along)
        landmarks.append(
            {
                "label": entry["label"],
                "kind": entry.get("kind", "landmark"),
                "osm_feature": entry["osm"],
                "along_m": round(along, 1),
                "offset_m": round(point.distance(cut), 1),
                "lat": round(on_course.y / lat_m, 6),
                "lon": round(on_course.x / lon_m, 6),
            }
        )
    landmarks.sort(key=lambda record: record["along_m"])

    banks = []
    for entry in COURSE_BANKS.get(course["id"], []):
        along = cut.length * float(entry["at"])
        point = cut.interpolate(along)
        banks.append(
            {
                "label": entry["label"],
                "side": int(entry["side"]),
                "along_m": round(along, 1),
                "lat": round(point.y / lat_m, 6),
                "lon": round(point.x / lon_m, 6),
            }
        )

    marks: list[dict[str, Any]] = []
    step = MILE_M
    distance = step
    while distance < cut.length - step * 0.15:
        point = cut.interpolate(distance)
        miles = distance / MILE_M
        marks.append(
            {
                "label": f"{miles:g} MILE" + ("S" if miles != 1 else ""),
                "along_m": round(distance, 1),
                "lat": round(point.y / lat_m, 6),
                "lon": round(point.x / lon_m, 6),
            }
        )
        distance += step
    return landmarks, banks, marks


def build(course: dict[str, Any]) -> dict[str, Any]:
    query = course["river_query"]
    origin_lat = (query["bbox"][0] + query["bbox"][2]) / 2
    ways = fetch_river(query["name"], query["bbox"])
    centreline = merged_centreline(ways, origin_lat)
    cut, start_offset, finish_offset = cut_course(
        centreline,
        (course["start"]["lat"], course["start"]["lon"]),
        (course["finish"]["lat"], course["finish"]["lon"]),
        origin_lat,
    )
    measured_m = cut.length
    official_m = float(course["official_distance_m"])
    error = abs(measured_m - official_m) / official_m
    print(
        f"  {course['id']:<22} {len(ways):>3} river ways  "
        f"measured {measured_m:7.0f} m  official {official_m:6.0f} m  "
        f"({100 * error:+5.1f}%)  endpoint offsets "
        f"{start_offset:.0f}/{finish_offset:.0f} m"
    )
    if error > LENGTH_TOLERANCE:
        raise SystemExit(
            f"{course['id']}: measured {measured_m:.0f} m against a published "
            f"{official_m:.0f} m is {100 * error:.1f}% out. That is the wrong "
            "reach, not river drift -- check the endpoints."
        )
    landmarks, banks, distance_marks = _markers_for(course, cut, origin_lat)
    print(
        f"       {len(landmarks)} landmark(s), {len(banks)} bank label(s), "
        f"{len(distance_marks)} distance mark(s)"
    )
    course = {
        **course,
        "landmarks": landmarks,
        "banks": banks,
        "distance_marks": distance_marks,
    }
    waypoints = _to_wgs84(resample(cut, RESAMPLE_M), origin_lat)
    latitudes = [lat for lat, _ in waypoints]
    longitudes = [lon for _, lon in waypoints]
    record = {key: course[key] for key in DESCRIPTIVE_FIELDS}
    unused = {
        key: course[key]
        for key in (
            "id",
            "name",
            "title",
            "event",
            "river",
            "reach",
            "city",
            "country",
            "start",
            "finish",
            "official_distance_m",
            "official_distance_label",
            "direction",
            "first_held",
            "boats",
            "held",
            "founder",
            "notes",
            "poster",
            "source_urls",
        )
    }
    del unused
    record["geometry"] = {
        "source": "openstreetmap",
        "licence": "ODbL 1.0",
        "derivation": (
            f"OSM waterway=river '{query['name']}' centre-line, merged and cut "
            "between the projections of the two named endpoints"
        ),
        "resample_step_m": RESAMPLE_M,
        "measured_length_m": round(measured_m, 1),
        "start_offset_from_centreline_m": round(start_offset, 1),
        "finish_offset_from_centreline_m": round(finish_offset, 1),
        "waypoints": [[round(lat, 6), round(lon, 6)] for lat, lon in waypoints],
    }
    record["extent_wgs84"] = {
        "south": round(min(latitudes), 6),
        "west": round(min(longitudes), 6),
        "north": round(max(latitudes), 6),
        "east": round(max(longitudes), 6),
    }
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("course", nargs="*", help="Course ids to rebuild; default all.")
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument(
        "--copy-only",
        action="store_true",
        help="Rewrite the descriptive fields from this file and keep the "
        "committed geometry, so editing poster copy costs no Overpass calls.",
    )
    args = parser.parse_args(argv)

    wanted = set(args.course) or {course["id"] for course in COURSES}
    unknown = wanted - {course["id"] for course in COURSES}
    if unknown:
        raise SystemExit(f"Unknown course id(s): {', '.join(sorted(unknown))}")

    existing: dict[str, Any] = {}
    if args.out.exists():
        document = json.loads(args.out.read_text(encoding="utf-8"))
        existing = {record["id"]: record for record in document.get("courses", [])}

    if args.copy_only:
        print(f"refreshing copy for {len(wanted)} course(s), keeping geometry")
        for course in COURSES:
            if course["id"] not in wanted:
                continue
            previous = existing.get(course["id"])
            if previous is None:
                raise SystemExit(
                    f"{course['id']}: --copy-only needs existing geometry to keep."
                )
            record = {key: course[key] for key in DESCRIPTIVE_FIELDS}
            record["geometry"] = previous["geometry"]
            record["extent_wgs84"] = previous["extent_wgs84"]
            existing[course["id"]] = record
    else:
        print(f"building {len(wanted)} course(s) from OpenStreetMap")
        for course in COURSES:
            if course["id"] in wanted:
                existing[course["id"]] = build(course)

    ordered = [existing[course["id"]] for course in COURSES if course["id"] in existing]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "id": CATALOG_ID,
                "attribution": (
                    "Course centre-lines derived from OpenStreetMap data, "
                    "© OpenStreetMap contributors, ODbL 1.0."
                ),
                "courses": ordered,
            },
            indent=1,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    total = sum(len(record["geometry"]["waypoints"]) for record in ordered)
    print(f"wrote {args.out} ({len(ordered)} courses, {total} waypoints)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
