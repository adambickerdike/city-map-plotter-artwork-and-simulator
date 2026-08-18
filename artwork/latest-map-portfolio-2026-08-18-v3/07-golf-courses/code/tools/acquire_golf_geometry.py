#!/usr/bin/env python3
"""Acquire small, pinned OpenStreetMap extracts for the golf-course series.

The renderer never calls the network.  This offline tool downloads an OSM API
``map`` response around one curated course boundary, retains only sourced
course features that intersect that boundary, and writes a deterministic gzip
extract under ``contracts/golf-courses-v2``.  No fairway, hazard, or hole is
invented when OpenStreetMap does not supply it.

Examples::

    .venv/bin/python tools/acquire_golf_geometry.py --all
    .venv/bin/python tools/acquire_golf_geometry.py --course old-course-st-andrews
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union


ROOT = Path(__file__).resolve().parent.parent
CONTRACT_ROOT = ROOT / "contracts" / "golf-courses-v2"
EXTRACT_ROOT = CONTRACT_ROOT / "source-extracts"
MANIFEST_PATH = CONTRACT_ROOT / "source-manifest.json"
USER_AGENT = (
    "city-map-plotter-golf-source-builder/1.0 "
    "(+https://www.openstreetmap.org/copyright)"
)
OSM_API = "https://api.openstreetmap.org/api/0.6/map"
SCHEMA_VERSION = 1
CONTRACT_ID = "golf-courses-v2"
EXTRACT_RECIPE = "osm-api-map-filtered-course-features-v1"
SOURCE_DATE = "2026-08-05"
EARTH_RADIUS_M = 6_371_008.8
REQUEST_INTERVAL_S = 5.0


# Bboxes are ordered south, west, north, east and are padded below before the
# API request.  The root object is the exact leisure=golf_course boundary used
# to constrain selection.  Royal Melbourne's club relation contains multiple
# courses; its explicit 1W..18W hole references define the West Course mask.
COURSES: tuple[dict[str, Any], ...] = (
    {
        "id": "augusta-national",
        "root_ref": "way/871993734",
        "bbox": [33.4930529, -82.0309711, 33.5088489, -82.0147293],
    },
    {
        "id": "old-course-st-andrews",
        "root_ref": "way/1019045811",
        "bbox": [56.3427969, -2.8259240, 56.3608568, -2.8026099],
    },
    {
        "id": "pebble-beach",
        "root_ref": "relation/3741806",
        "bbox": [36.5576470, -121.9499497, 36.5708711, -121.9289221],
    },
    {
        "id": "pinehurst-no-2",
        "root_ref": "way/1358696570",
        "bbox": [35.1843691, -79.4684522, 35.1990453, -79.4530096],
        "hole_ref_course": "#2",
    },
    {
        "id": "oakmont",
        "root_ref": "relation/6174192",
        "bbox": [40.5246454, -79.8338234, 40.5334496, -79.8161207],
    },
    {
        "id": "shinnecock-hills",
        "root_ref": "way/689056680",
        "bbox": [40.8914212, -72.4465433, 40.9021118, -72.4330589],
    },
    {
        "id": "muirfield",
        "root_ref": "way/101336384",
        "bbox": [56.0410958, -2.8319837, 56.0504513, -2.8116736],
    },
    {
        "id": "carnoustie-championship",
        "root_ref": "way/1459805268",
        "bbox": [56.4892789, -2.7379307, 56.4978242, -2.7164635],
    },
    {
        "id": "royal-county-down",
        "root_ref": "way/78134737",
        "bbox": [54.2162362, -5.8859906, 54.2302960, -5.8656705],
    },
    {
        "id": "royal-portrush-dunluce",
        "root_ref": "way/1413316756",
        "bbox": [55.1983956, -6.6364632, 55.2071119, -6.6140584],
    },
    {
        "id": "royal-melbourne-west",
        "root_ref": "relation/4180358",
        "bbox": [-37.9778458, 145.0202967, -37.9648603, 145.0466046],
        "hole_ref_suffix": "W",
    },
    {
        "id": "cypress-point",
        "root_ref": "way/36435651",
        "bbox": [36.5744374, -121.9779894, 36.5831775, -121.9574346],
    },
    {
        "id": "royal-st-georges",
        "root_ref": "way/24494194",
        "bbox": [51.2680849, 1.3643774, 51.2837206, 1.3856802],
    },
    {
        "id": "royal-birkdale",
        "root_ref": "way/25720345",
        "bbox": [53.6165115, -3.0493811, 53.6318212, -3.0299768],
    },
    {
        "id": "royal-troon-old",
        "root_ref": "way/18080206",
        "bbox": [55.5176100, -4.6531886, 55.5332096, -4.6214022],
    },
    {
        "id": "turnberry-ailsa",
        "root_ref": "way/46421398",
        "bbox": [55.3111088, -4.8467457, 55.3327294, -4.8255413],
        "hole_tag_filters": {"course:name": "Ailsa"},
    },
    {
        "id": "royal-dornoch-championship",
        "root_ref": "way/102270514",
        "bbox": [57.8782843, -4.0239447, 57.8987025, -4.0044939],
        "hole_tag_filters": {"golf:course:name": "Championship Course"},
    },
    {
        "id": "sunningdale-old",
        "root_ref": "relation/15410213",
        "bbox": [51.3742049, -0.6470121, 51.3896149, -0.6214537],
        "hole_tag_filters": {"golf:course:name": "Sunningdale Old Course"},
    },
    {
        "id": "ballybunion-old",
        "root_ref": "relation/19920899",
        "bbox": [52.4838140, -9.6864961, 52.5084606, -9.6709192],
        "hole_tag_filters": {"name": "Old Course"},
    },
    {
        "id": "winged-foot-west",
        "root_ref": "way/122734591",
        "bbox": [40.9543847, -73.7595799, 40.9705324, -73.7481334],
    },
    {
        "id": "national-golf-links",
        "root_ref": "way/28989103",
        "bbox": [40.8954014, -72.4567468, 40.9172489, -72.4417434],
    },
    {
        "id": "seminole",
        "root_ref": "way/125329140",
        "bbox": [26.8592289, -80.0558879, 26.8667299, -80.0466062],
    },
    {
        "id": "whistling-straits",
        "root_ref": "way/205111637",
        "bbox": [43.8349447, -87.7408445, 43.8647580, -87.7284452],
    },
    {
        "id": "hirono",
        "root_ref": "way/138736088",
        "bbox": [34.7622983, 135.0074576, 34.7720374, 135.0257639],
    },
    {
        "id": "cabot-cliffs",
        "root_ref": "way/676091202",
        "bbox": [46.2432391, -61.2956999, 46.2611185, -61.2768607],
    },
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tags(element: ET.Element) -> dict[str, str]:
    return {
        str(tag.get("k")): str(tag.get("v"))
        for tag in element.findall("tag")
        if tag.get("k") is not None and tag.get("v") is not None
    }


def _osm_ref(element: ET.Element) -> str:
    return f"{element.tag}/{element.get('id')}"


def _projector(latitude: float, longitude: float):
    cos_latitude = math.cos(math.radians(latitude))

    def project(lon: float, lat: float) -> tuple[float, float]:
        return (
            math.radians(lon - longitude) * EARTH_RADIUS_M * cos_latitude,
            math.radians(lat - latitude) * EARTH_RADIUS_M,
        )

    return project


def _relation_paths(
    relation: ET.Element,
    ways: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for member in relation.findall("member"):
        if member.get("type") != "way" or member.get("ref") is None:
            continue
        way = ways.get(str(member.get("ref")))
        if way is None or len(way["coordinates"]) < 2:
            continue
        result.append(
            {
                "role": member.get("role") or "outer",
                "coordinates": way["coordinates"],
                "source_ref": f"way/{member.get('ref')}",
            }
        )
    return result


def _polygon_from_paths(
    paths: Iterable[dict[str, Any]],
    project,
) -> BaseGeometry:
    outer_lines: list[LineString] = []
    inner_lines: list[LineString] = []
    for path in paths:
        coordinates = [
            project(float(lon), float(lat)) for lon, lat in path["coordinates"]
        ]
        if len(coordinates) < 3:
            continue
        line = LineString(coordinates)
        if str(path.get("role", "outer")) == "inner":
            inner_lines.append(line)
        else:
            outer_lines.append(line)
    if not outer_lines:
        raise RuntimeError("Course boundary has no usable outer paths.")
    outer_polygons = list(polygonize(unary_union(outer_lines)))
    if not outer_polygons:
        outer_polygons = [
            Polygon(line.coords)
            for line in outer_lines
            if len(line.coords) >= 4 and line.coords[0] == line.coords[-1]
        ]
    if not outer_polygons:
        raise RuntimeError("Course boundary outer paths do not form a polygon.")
    result: BaseGeometry = unary_union(outer_polygons)
    if inner_lines:
        inner_polygons = list(polygonize(unary_union(inner_lines)))
        if inner_polygons:
            result = result.difference(unary_union(inner_polygons))
    if result.is_empty:
        raise RuntimeError("Course boundary polygon is empty.")
    return result.buffer(0)


def _relevant(tags: dict[str, str]) -> bool:
    if "golf" in tags:
        return True
    if tags.get("natural") in {
        "water",
        "wood",
        "scrub",
        "heath",
        "wetland",
        "coastline",
    }:
        return True
    if tags.get("landuse") in {"forest", "grass", "meadow"}:
        return True
    if tags.get("waterway"):
        return True
    if tags.get("water"):
        return True
    if tags.get("highway") in {"path", "track", "service", "footway"}:
        return True
    if tags.get("building"):
        return True
    return False


def _feature_geometry(
    feature: dict[str, Any],
    project,
) -> BaseGeometry:
    paths = feature.get("paths") or []
    geometries: list[BaseGeometry] = []
    for path in paths:
        coordinates = [
            project(float(lon), float(lat)) for lon, lat in path["coordinates"]
        ]
        if len(coordinates) == 1:
            geometries.append(Point(coordinates[0]))
        elif len(coordinates) >= 4 and coordinates[0] == coordinates[-1]:
            polygon = Polygon(coordinates)
            geometries.append(polygon.buffer(0) if not polygon.is_valid else polygon)
        elif len(coordinates) >= 2:
            geometries.append(LineString(coordinates))
    if not geometries:
        return Point()
    return unary_union(geometries)


def _canonical_hole_number(
    value: str | None,
    suffix: str | None,
    course_token: str | None,
) -> int | None:
    if value is None:
        return None
    if course_token is not None:
        match = re.fullmatch(
            rf"\s*(\d{{1,2}})\s*-\s*{re.escape(course_token)}\s*",
            value,
        )
        if match is None:
            return None
        number = int(match.group(1))
        return number if 1 <= number <= 18 else None
    match = re.fullmatch(r"\s*(\d{1,2})([A-Za-z]?)\s*", value)
    if match is None:
        return None
    number = int(match.group(1))
    actual_suffix = match.group(2).upper()
    if not 1 <= number <= 18:
        return None
    if suffix is not None and actual_suffix != suffix.upper():
        return None
    if suffix is None and actual_suffix:
        return None
    return number


def _parse_osm(xml_bytes: bytes, course: dict[str, Any]) -> dict[str, Any]:
    document = ET.fromstring(xml_bytes)
    node_elements = {str(node.get("id")): node for node in document.findall("node")}
    nodes: dict[str, dict[str, Any]] = {}
    for node_id, node in node_elements.items():
        nodes[node_id] = {
            "coordinates": [float(node.get("lon")), float(node.get("lat"))],
            "tags": _tags(node),
            "version": int(node.get("version") or 0),
            "timestamp": node.get("timestamp"),
        }

    ways: dict[str, dict[str, Any]] = {}
    for way in document.findall("way"):
        coordinates = [
            nodes[str(nd.get("ref"))]["coordinates"]
            for nd in way.findall("nd")
            if str(nd.get("ref")) in nodes
        ]
        ways[str(way.get("id"))] = {
            "coordinates": coordinates,
            "tags": _tags(way),
            "version": int(way.get("version") or 0),
            "timestamp": way.get("timestamp"),
        }

    relations = {
        str(relation.get("id")): relation for relation in document.findall("relation")
    }
    root_type, root_id = str(course["root_ref"]).split("/", 1)
    if root_type == "way":
        root_way = ways.get(root_id)
        if root_way is None:
            raise RuntimeError(f"Missing curated boundary {course['root_ref']}.")
        root_tags = root_way["tags"]
        boundary_paths = [
            {
                "role": "outer",
                "coordinates": root_way["coordinates"],
                "source_ref": course["root_ref"],
            }
        ]
        root_version = root_way["version"]
        root_timestamp = root_way["timestamp"]
    elif root_type == "relation":
        relation = relations.get(root_id)
        if relation is None:
            raise RuntimeError(f"Missing curated boundary {course['root_ref']}.")
        root_tags = _tags(relation)
        boundary_paths = _relation_paths(relation, ways)
        root_version = int(relation.get("version") or 0)
        root_timestamp = relation.get("timestamp")
    else:
        raise RuntimeError(f"Unsupported boundary reference {course['root_ref']}.")
    if root_tags.get("leisure") != "golf_course":
        raise RuntimeError(
            f"Curated root {course['root_ref']} is not leisure=golf_course."
        )

    south, west, north, east = [float(value) for value in course["bbox"]]
    centre_lat = (south + north) / 2.0
    centre_lon = (west + east) / 2.0
    project = _projector(centre_lat, centre_lon)
    boundary_geometry = _polygon_from_paths(boundary_paths, project)

    candidates: list[dict[str, Any]] = []
    for node_id, node in nodes.items():
        if not _relevant(node["tags"]):
            continue
        candidates.append(
            {
                "source_ref": f"node/{node_id}",
                "source_version": node["version"],
                "source_timestamp": node["timestamp"],
                "tags": node["tags"],
                "paths": [
                    {
                        "role": "point",
                        "coordinates": [node["coordinates"]],
                    }
                ],
            }
        )
    for way_id, way in ways.items():
        if not _relevant(way["tags"]):
            continue
        candidates.append(
            {
                "source_ref": f"way/{way_id}",
                "source_version": way["version"],
                "source_timestamp": way["timestamp"],
                "tags": way["tags"],
                "paths": [
                    {
                        "role": "outer"
                        if len(way["coordinates"]) >= 4
                        and way["coordinates"][0] == way["coordinates"][-1]
                        else "line",
                        "coordinates": way["coordinates"],
                    }
                ],
            }
        )
    for relation_id, relation in relations.items():
        tags = _tags(relation)
        if not _relevant(tags):
            continue
        candidates.append(
            {
                "source_ref": f"relation/{relation_id}",
                "source_version": int(relation.get("version") or 0),
                "source_timestamp": relation.get("timestamp"),
                "tags": tags,
                "paths": _relation_paths(relation, ways),
            }
        )

    suffix = course.get("hole_ref_suffix")
    course_token = course.get("hole_ref_course")
    tag_filters = {
        str(key): str(value)
        for key, value in dict(course.get("hole_tag_filters") or {}).items()
    }
    holes: list[tuple[int, dict[str, Any], BaseGeometry]] = []
    for feature in candidates:
        if feature["tags"].get("golf") != "hole":
            continue
        if any(feature["tags"].get(key) != value for key, value in tag_filters.items()):
            continue
        number = _canonical_hole_number(
            feature["tags"].get("ref"), suffix, course_token
        )
        if number is None:
            continue
        geometry = _feature_geometry(feature, project)
        if geometry.is_empty or not geometry.intersects(boundary_geometry):
            continue
        holes.append((number, feature, geometry))
    by_number: dict[int, list[tuple[dict[str, Any], BaseGeometry]]] = {}
    for number, feature, geometry in holes:
        by_number.setdefault(number, []).append((feature, geometry))
    duplicates = {
        number: values for number, values in by_number.items() if len(values) != 1
    }
    missing = sorted(set(range(1, 19)) - set(by_number))
    if missing or (
        (suffix is not None or course_token is not None or tag_filters) and duplicates
    ):
        duplicate_copy = ", ".join(
            f"{number}({len(values)})" for number, values in sorted(duplicates.items())
        )
        raise RuntimeError(
            f"{course['id']} does not resolve to exactly holes 1..18; "
            f"missing={missing}, duplicates={duplicate_copy or 'none'}."
        )
    # A club boundary can legitimately contain a separate par-three course.
    # Augusta, for example, has two sourced ways numbered 1..9.  The
    # championship-course hole is unambiguously the longer sourced centreline;
    # retain that object and record the selection rule instead of discarding the
    # entire source or inventing a route.  A named suffix policy (Royal
    # Melbourne West) remains strict because those refs are already explicit.
    if duplicates:
        for number, values in by_number.items():
            values.sort(
                key=lambda item: (
                    item[1].length,
                    item[0]["source_ref"],
                ),
                reverse=True,
            )
    selected_holes = {values[0][0]["source_ref"] for values in by_number.values()}

    selection_geometry = boundary_geometry
    selection_method = "root-golf-course-boundary"
    if suffix is not None:
        hole_geometry = unary_union([values[0][1] for values in by_number.values()])
        selection_geometry = hole_geometry.buffer(130.0).intersection(boundary_geometry)
        selection_method = f"root-boundary-intersected-with-{suffix}-hole-buffer-130m"
    elif course_token is not None:
        hole_geometry = unary_union([values[0][1] for values in by_number.values()])
        selection_geometry = hole_geometry.buffer(130.0).intersection(boundary_geometry)
        selection_method = (
            f"root-boundary-intersected-with-{course_token}-hole-buffer-130m"
        )
    elif tag_filters:
        hole_geometry = unary_union([values[0][1] for values in by_number.values()])
        selection_geometry = hole_geometry.buffer(130.0).intersection(boundary_geometry)
        filter_label = ",".join(
            f"{key}={value}" for key, value in sorted(tag_filters.items())
        )
        selection_method = (
            "root-boundary-intersected-with-explicit-hole-tags-"
            f"{filter_label}-buffer-130m"
        )
    elif duplicates:
        selection_method += "-longest-source-hole-per-duplicate-number"

    selected: list[dict[str, Any]] = []
    for feature in candidates:
        if feature["source_ref"] == course["root_ref"]:
            continue
        if feature["tags"].get("leisure") == "golf_course":
            continue
        if feature["tags"].get("golf") == "hole":
            if feature["source_ref"] not in selected_holes:
                continue
        geometry = _feature_geometry(feature, project)
        if geometry.is_empty or not geometry.intersects(selection_geometry):
            continue
        selected.append(feature)
    selected.sort(
        key=lambda item: (
            item["tags"].get("golf", ""),
            item["source_ref"].split("/", 1)[0],
            int(item["source_ref"].split("/", 1)[1]),
        )
    )

    counts: dict[str, int] = {}
    for feature in selected:
        key = (
            f"golf:{feature['tags']['golf']}"
            if feature["tags"].get("golf")
            else (
                f"natural:{feature['tags']['natural']}"
                if feature["tags"].get("natural")
                else (
                    f"landuse:{feature['tags']['landuse']}"
                    if feature["tags"].get("landuse")
                    else (
                        "waterway"
                        if feature["tags"].get("waterway")
                        else (
                            "highway" if feature["tags"].get("highway") else "building"
                        )
                    )
                )
            )
        )
        counts[key] = counts.get(key, 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "openstreetmap-golf-course-extract",
        "course_id": course["id"],
        "source": {
            "provider": "OpenStreetMap contributors",
            "license": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors",
            "api": "OpenStreetMap API 0.6 map",
            "snapshot_date": SOURCE_DATE,
        },
        "root": {
            "source_ref": course["root_ref"],
            "source_version": root_version,
            "source_timestamp": root_timestamp,
            "tags": root_tags,
            "paths": boundary_paths,
        },
        "selection": {
            "method": selection_method,
            "hole_ref_suffix": suffix,
            "hole_ref_course": course_token,
            "hole_tag_filters": tag_filters,
            "duplicate_hole_numbers_resolved_by_length": sorted(duplicates),
            "hole_refs": [
                by_number[number][0][0]["source_ref"] for number in range(1, 19)
            ],
        },
        "feature_counts": dict(sorted(counts.items())),
        "features": selected,
    }


def _request_bbox(course: dict[str, Any]) -> tuple[float, float, float, float]:
    south, west, north, east = [float(value) for value in course["bbox"]]
    latitude_pad = max((north - south) * 0.08, 0.0005)
    longitude_pad = max((east - west) * 0.08, 0.0005)
    return (
        west - longitude_pad,
        south - latitude_pad,
        east + longitude_pad,
        north + latitude_pad,
    )


def _fetch(course: dict[str, Any]) -> tuple[bytes, str]:
    west, south, east, north = _request_bbox(course)
    query = urllib.parse.urlencode(
        {"bbox": f"{west:.7f},{south:.7f},{east:.7f},{north:.7f}"}
    )
    url = f"{OSM_API}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                payload = response.read()
            if not payload.startswith(b"<?xml"):
                raise RuntimeError("OSM API returned a non-XML payload.")
            return payload, url
        except Exception as exc:  # noqa: BLE001 - retry an offline acquisition.
            last_error = exc
            if attempt == 3:
                break
            delay = 4.0 * (attempt + 1)
            print(f"    retry after {exc!s} ({delay:g}s)", file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError(f"OSM API request failed: {last_error}")


def _write_extract(course: dict[str, Any]) -> dict[str, Any]:
    print(f"{course['id']}: fetching {course['root_ref']} ...", flush=True)
    raw, url = _fetch(course)
    extract = _parse_osm(raw, course)
    encoded = (
        json.dumps(
            extract,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    compressed = gzip.compress(encoded, compresslevel=9, mtime=0)
    EXTRACT_ROOT.mkdir(parents=True, exist_ok=True)
    relative = Path("contracts/golf-courses-v2/source-extracts") / (
        f"{course['id']}-osm-v1.json.gz"
    )
    output = ROOT / relative
    output.write_bytes(compressed)
    counts = extract["feature_counts"]
    print(
        f"    {len(extract['features'])} features; "
        f"18 holes; {len(compressed) / 1024:.1f} KiB compressed"
    )
    return {
        "course_id": course["id"],
        "root_ref": course["root_ref"],
        "api_url": url,
        "raw_payload_sha256": _sha256(raw),
        "extract_path": relative.as_posix(),
        "extract_sha256": _sha256(compressed),
        "snapshot_date": SOURCE_DATE,
        "root_source_timestamp": extract["root"]["source_timestamp"],
        "selection_method": extract["selection"]["method"],
        "feature_counts": counts,
    }


def _write_manifest(records: list[dict[str, Any]]) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "snapshot_date": SOURCE_DATE,
        "extract_recipe": EXTRACT_RECIPE,
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
        "generated_at": datetime.now(UTC).isoformat(),
        "sources": records,
    }
    CONTRACT_ROOT.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Acquire all 25 courses.")
    group.add_argument(
        "--course",
        choices=[str(course["id"]) for course in COURSES],
        help="Acquire one course (and replace its manifest record).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected = (
        list(COURSES)
        if args.all
        else [course for course in COURSES if course["id"] == args.course]
    )
    existing: dict[str, dict[str, Any]] = {}
    if MANIFEST_PATH.exists():
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        existing = {record["course_id"]: record for record in raw.get("sources", [])}
    for course in selected:
        prior = existing.get(str(course["id"]))
        if args.all and prior is not None and prior.get("snapshot_date") == SOURCE_DATE:
            prior_path = ROOT / str(prior.get("extract_path", ""))
            if prior_path.is_file() and _sha256(prior_path.read_bytes()) == prior.get(
                "extract_sha256"
            ):
                print(f"{course['id']}: reusing verified {SOURCE_DATE} extract")
                continue
        existing[str(course["id"])] = _write_extract(course)
        ordered = [
            existing[str(candidate["id"])]
            for candidate in COURSES
            if str(candidate["id"]) in existing
        ]
        _write_manifest(ordered)
        time.sleep(REQUEST_INTERVAL_S)
    ordered = [
        existing[str(course["id"])]
        for course in COURSES
        if str(course["id"]) in existing
    ]
    _write_manifest(ordered)
    print(f"Wrote {MANIFEST_PATH.relative_to(ROOT)} ({len(ordered)} source records).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
