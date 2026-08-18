#!/usr/bin/env python3
"""Build the packaged golf-course catalog from pinned OSM extracts.

This is intentionally network-free.  Run ``acquire_golf_geometry.py`` only
when refreshing sources, review the resulting contract, then run this tool to
project the frozen WGS84 geometry into one shared local-metre model per course.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from shapely.geometry import LineString


ROOT = Path(__file__).resolve().parent.parent
CONTRACT_ROOT = ROOT / "contracts" / "golf-courses-v2"
MANIFEST_PATH = CONTRACT_ROOT / "source-manifest.json"
OUTPUT_PATH = ROOT / "src" / "city_map_plotter" / "data" / "golf-courses-v2.json"
CATALOG_ID = "golf-courses-v2"
SCHEMA_VERSION = 1
EARTH_RADIUS_M = 6_371_008.8
FORMAT_ID = "a3-portrait"
CATALOG_SIMPLIFY_M = 0.5


COURSE_METADATA: tuple[dict[str, Any], ...] = (
    {
        "id": "augusta-national",
        "title": "AUGUSTA NATIONAL",
        "subtitle": "AUGUSTA, GEORGIA / UNITED STATES",
        "location": {"label": "Augusta, Georgia, United States", "country_code": "US"},
        "championship_context": "MASTERS TOURNAMENT / ANNUAL HOST",
        "profile_publisher": "Masters Tournament",
        "profile_url": "https://www.masters.com/en_US/course/index.html",
        "selection_note": "The permanent home of the Masters Tournament.",
    },
    {
        "id": "old-course-st-andrews",
        "title": "THE OLD COURSE",
        "subtitle": "ST ANDREWS, FIFE / SCOTLAND",
        "location": {"label": "St Andrews, Fife, Scotland", "country_code": "GB"},
        "championship_context": "THE OPEN / HOME OF GOLF",
        "profile_publisher": "St Andrews Links Trust",
        "profile_url": "https://www.standrews.com/Play/Courses/Old-course",
        "selection_note": "The Old Course at St Andrews, an Open Championship venue.",
    },
    {
        "id": "pebble-beach",
        "title": "PEBBLE BEACH GOLF LINKS",
        "subtitle": "MONTEREY PENINSULA, CALIFORNIA / UNITED STATES",
        "location": {
            "label": "Pebble Beach, California, United States",
            "country_code": "US",
        },
        "championship_context": "U.S. OPEN VENUE / PACIFIC LINKS",
        "profile_publisher": "Pebble Beach Resorts",
        "profile_url": "https://www.pebblebeach.com/golf/pebble-beach-golf-links/",
        "selection_note": "Pebble Beach Golf Links, a recurring U.S. Open venue.",
    },
    {
        "id": "pinehurst-no-2",
        "title": "PINEHURST NO. 2",
        "subtitle": "PINEHURST, NORTH CAROLINA / UNITED STATES",
        "location": {
            "label": "Pinehurst, North Carolina, United States",
            "country_code": "US",
        },
        "championship_context": "U.S. OPEN VENUE / DONALD ROSS",
        "profile_publisher": "Pinehurst Resort",
        "profile_url": "https://www.pinehurst.com/golf/courses/no-2/",
        "selection_note": "Pinehurst No. 2, the resort's Donald Ross championship course.",
    },
    {
        "id": "oakmont",
        "title": "OAKMONT COUNTRY CLUB",
        "subtitle": "OAKMONT, PENNSYLVANIA / UNITED STATES",
        "location": {
            "label": "Oakmont, Pennsylvania, United States",
            "country_code": "US",
        },
        "championship_context": "U.S. OPEN VENUE / HENRY FOWNES",
        "profile_publisher": "Oakmont Country Club",
        "profile_url": "https://www.oakmont-countryclub.org/club/scripts/section/section.asp?NS=HP",
        "selection_note": "Oakmont Country Club, a historic U.S. Open venue.",
    },
    {
        "id": "shinnecock-hills",
        "title": "SHINNECOCK HILLS",
        "subtitle": "SOUTHAMPTON, NEW YORK / UNITED STATES",
        "location": {
            "label": "Southampton, New York, United States",
            "country_code": "US",
        },
        "championship_context": "U.S. OPEN VENUE / LONG ISLAND LINKS",
        "profile_publisher": "Shinnecock Hills Golf Club",
        "profile_url": "https://www.shinnecockhillsgolfclub.org/",
        "selection_note": "Shinnecock Hills Golf Club, a recurring U.S. Open venue.",
    },
    {
        "id": "muirfield",
        "title": "MUIRFIELD",
        "subtitle": "GULLANE, EAST LOTHIAN / SCOTLAND",
        "location": {"label": "Gullane, East Lothian, Scotland", "country_code": "GB"},
        "championship_context": "THE OPEN VENUE / LINKS COURSE",
        "profile_publisher": "The Honourable Company of Edinburgh Golfers",
        "profile_url": "https://www.muirfield.org.uk/the-course/",
        "selection_note": "Muirfield, the links of the Honourable Company of Edinburgh Golfers.",
    },
    {
        "id": "carnoustie-championship",
        "title": "CARNOUSTIE",
        "subtitle": "CARNOUSTIE, ANGUS / SCOTLAND",
        "location": {"label": "Carnoustie, Angus, Scotland", "country_code": "GB"},
        "championship_context": "THE OPEN VENUE / CHAMPIONSHIP COURSE",
        "profile_publisher": "Carnoustie Golf Links",
        "profile_url": "https://www.carnoustiegolflinks.com/course/championship-course/",
        "selection_note": "The Championship Course at Carnoustie Golf Links.",
    },
    {
        "id": "royal-county-down",
        "title": "ROYAL COUNTY DOWN",
        "subtitle": "NEWCASTLE, COUNTY DOWN / NORTHERN IRELAND",
        "location": {
            "label": "Newcastle, County Down, Northern Ireland",
            "country_code": "GB",
        },
        "championship_context": "CHAMPIONSHIP LINKS / MOURNE MOUNTAINS",
        "profile_publisher": "Royal County Down Golf Club",
        "profile_url": "https://www.royalcountydown.org/championship_links",
        "selection_note": "Royal County Down's Championship Links beside Dundrum Bay.",
    },
    {
        "id": "royal-portrush-dunluce",
        "title": "DUNLUCE LINKS",
        "subtitle": "ROYAL PORTRUSH, COUNTY ANTRIM / NORTHERN IRELAND",
        "location": {
            "label": "Portrush, County Antrim, Northern Ireland",
            "country_code": "GB",
        },
        "championship_context": "THE OPEN VENUE / DUNLUCE LINKS",
        "profile_publisher": "The R&A",
        "profile_url": "https://www.theopen.com/previous-opens/royal-portrush-153rd-open/course-guide",
        "selection_note": "The Dunluce Links at Royal Portrush, an Open Championship venue.",
    },
    {
        "id": "royal-melbourne-west",
        "title": "ROYAL MELBOURNE WEST",
        "subtitle": "BLACK ROCK, VICTORIA / AUSTRALIA",
        "location": {"label": "Black Rock, Victoria, Australia", "country_code": "AU"},
        "championship_context": "WEST COURSE / MELBOURNE SANDBELT",
        "profile_publisher": "The Royal Melbourne Golf Club",
        "profile_url": "https://www.royalmelbourne.com.au/courses/the-west-course/",
        "selection_note": "The West Course at Royal Melbourne Golf Club.",
    },
    {
        "id": "cypress-point",
        "title": "CYPRESS POINT CLUB",
        "subtitle": "PEBBLE BEACH, CALIFORNIA / UNITED STATES",
        "location": {
            "label": "Pebble Beach, California, United States",
            "country_code": "US",
        },
        "championship_context": "MONTEREY PENINSULA / COASTAL COURSE",
        "profile_publisher": "Cypress Point Club",
        "profile_url": "https://www.cpcmack.org/",
        "selection_note": "Cypress Point Club on California's Monterey Peninsula.",
    },
    {
        "id": "royal-st-georges",
        "title": "ROYAL ST GEORGE'S",
        "subtitle": "SANDWICH, KENT / ENGLAND",
        "location": {"label": "Sandwich, Kent, England", "country_code": "GB"},
        "championship_context": "THE OPEN VENUE / SANDWICH LINKS",
        "profile_publisher": "The Royal St George's Golf Club",
        "profile_url": "https://www.royalstgeorges.com/",
        "selection_note": "Royal St George's, the first Open venue outside Scotland and a fifteen-time host.",
    },
    {
        "id": "royal-birkdale",
        "title": "ROYAL BIRKDALE",
        "subtitle": "SOUTHPORT, MERSEYSIDE / ENGLAND",
        "location": {
            "label": "Southport, Merseyside, England",
            "country_code": "GB",
        },
        "championship_context": "THE OPEN VENUE / CHAMPIONSHIP LINKS",
        "profile_publisher": "Royal Birkdale Golf Club",
        "profile_url": "https://royalbirkdale.com/the-course/",
        "selection_note": "Royal Birkdale, an Open Championship and Ryder Cup venue on the Southport links.",
    },
    {
        "id": "royal-troon-old",
        "title": "ROYAL TROON OLD COURSE",
        "subtitle": "TROON, SOUTH AYRSHIRE / SCOTLAND",
        "location": {
            "label": "Troon, South Ayrshire, Scotland",
            "country_code": "GB",
        },
        "championship_context": "THE OPEN VENUE / POSTAGE STAMP",
        "profile_publisher": "Royal Troon Golf Club",
        "profile_url": "https://www.royaltroon.co.uk/the-courses/old-course/",
        "selection_note": "Royal Troon's Old Course, a ten-time Open host and home of the Postage Stamp.",
    },
    {
        "id": "turnberry-ailsa",
        "title": "TURNBERRY AILSA",
        "subtitle": "TURNBERRY, SOUTH AYRSHIRE / SCOTLAND",
        "location": {
            "label": "Turnberry, South Ayrshire, Scotland",
            "country_code": "GB",
        },
        "championship_context": "THE OPEN VENUE / DUEL IN THE SUN",
        "profile_publisher": "Trump Turnberry",
        "profile_url": "https://www.turnberry.co.uk/Default.aspx?p=dynamicmodule&pageid=100013&ssid=100035&vnf=1",
        "selection_note": "The Ailsa Course at Turnberry, a four-time Open host and setting of the 1977 Duel in the Sun.",
    },
    {
        "id": "royal-dornoch-championship",
        "title": "ROYAL DORNOCH",
        "subtitle": "DORNOCH, HIGHLAND / SCOTLAND",
        "location": {"label": "Dornoch, Highland, Scotland", "country_code": "GB"},
        "championship_context": "OLD TOM MORRIS / HIGHLAND LINKS",
        "profile_publisher": "Royal Dornoch Golf Club",
        "profile_url": "https://royaldornoch.com/championship-course-2/",
        "selection_note": "Royal Dornoch's Championship Course, the Highland links extended to eighteen holes by Old Tom Morris.",
    },
    {
        "id": "sunningdale-old",
        "title": "SUNNINGDALE OLD",
        "subtitle": "SUNNINGDALE, BERKSHIRE / ENGLAND",
        "location": {
            "label": "Sunningdale, Berkshire, England",
            "country_code": "GB",
        },
        "championship_context": "WILLIE PARK JR / HEATHLAND CLASSIC",
        "profile_publisher": "Sunningdale Golf Club",
        "profile_url": "https://www.sunningdale.com/old_course",
        "selection_note": "Sunningdale's Willie Park Jr-designed Old Course, opened in 1901.",
    },
    {
        "id": "ballybunion-old",
        "title": "BALLYBUNION OLD",
        "subtitle": "BALLYBUNION, COUNTY KERRY / IRELAND",
        "location": {
            "label": "Ballybunion, County Kerry, Ireland",
            "country_code": "IE",
        },
        "championship_context": "OLD COURSE / ATLANTIC LINKS",
        "profile_publisher": "Ballybunion Golf Club",
        "profile_url": "https://www.ballybuniongolfclub.com/courses/",
        "selection_note": "Ballybunion's Old Course, the historic Atlantic links founded in 1893.",
    },
    {
        "id": "winged-foot-west",
        "title": "WINGED FOOT WEST",
        "subtitle": "MAMARONECK, NEW YORK / UNITED STATES",
        "location": {
            "label": "Mamaroneck, New York, United States",
            "country_code": "US",
        },
        "championship_context": "U.S. OPEN VENUE / A. W. TILLINGHAST",
        "profile_publisher": "Winged Foot Golf Club",
        "profile_url": "https://wfgc.org/club-history",
        "selection_note": "Winged Foot's A. W. Tillinghast-designed West Course, host of six U.S. Opens.",
    },
    {
        "id": "national-golf-links",
        "title": "NATIONAL GOLF LINKS",
        "subtitle": "SOUTHAMPTON, NEW YORK / UNITED STATES",
        "location": {
            "label": "Southampton, New York, United States",
            "country_code": "US",
        },
        "championship_context": "INAUGURAL WALKER CUP / C. B. MACDONALD",
        "profile_publisher": "National Golf Links of America",
        "profile_url": "https://www.ngla.us/default.aspx?PageId=1&p=DynamicModule&ssid=100001&vnf=1",
        "selection_note": "C. B. Macdonald's National Golf Links of America, host of the inaugural Walker Cup.",
    },
    {
        "id": "seminole",
        "title": "SEMINOLE GOLF CLUB",
        "subtitle": "JUNO BEACH, FLORIDA / UNITED STATES",
        "location": {
            "label": "Juno Beach, Florida, United States",
            "country_code": "US",
        },
        "championship_context": "WALKER CUP VENUE / DONALD ROSS",
        "profile_publisher": "United States Golf Association",
        "profile_url": "https://www.usga.org/content/usga/home-page/championships/2021/2021-walker-cup--home/articles/venue-spotlight-seminole-2021-walker-cup-florida.html",
        "selection_note": "Seminole's Donald Ross-designed course, venue for the 2021 Walker Cup.",
    },
    {
        "id": "whistling-straits",
        "title": "WHISTLING STRAITS",
        "subtitle": "SHEBOYGAN, WISCONSIN / UNITED STATES",
        "location": {
            "label": "Sheboygan, Wisconsin, United States",
            "country_code": "US",
        },
        "championship_context": "PGA CHAMPIONSHIP / RYDER CUP VENUE",
        "profile_publisher": "Kohler Wisconsin",
        "profile_url": "https://www.kohlerwisconsin.com/golf/whistling-straits/the-straits",
        "selection_note": "The Straits Course at Whistling Straits, a three-time PGA Championship and 2021 Ryder Cup venue.",
    },
    {
        "id": "hirono",
        "title": "HIRONO GOLF CLUB",
        "subtitle": "MIKI, HYOGO / JAPAN",
        "location": {"label": "Miki, Hyogo, Japan", "country_code": "JP"},
        "championship_context": "C. H. ALISON / JAPANESE CLASSIC",
        "profile_publisher": "Hirono Golf Club",
        "profile_url": "https://hironogolfclub.jp/en/",
        "selection_note": "Hirono Golf Club, C. H. Alison's landmark Japanese course.",
    },
    {
        "id": "cabot-cliffs",
        "title": "CABOT CLIFFS",
        "subtitle": "INVERNESS, NOVA SCOTIA / CANADA",
        "location": {
            "label": "Inverness, Nova Scotia, Canada",
            "country_code": "CA",
        },
        "championship_context": "COORE & CRENSHAW / ATLANTIC CLIFFS",
        "profile_publisher": "Cabot Cape Breton",
        "profile_url": "https://cabot.com/capebreton/golf/cabot-cliffs/",
        "selection_note": "Cabot Cliffs, Bill Coore and Ben Crenshaw's Atlantic clifftop course in Cape Breton.",
    },
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_extract(record: dict[str, Any]) -> dict[str, Any]:
    path = ROOT / str(record["extract_path"])
    compressed = path.read_bytes()
    if _sha256(compressed) != record["extract_sha256"]:
        raise SystemExit(f"Pinned extract hash mismatch: {path.relative_to(ROOT)}")
    return json.loads(gzip.decompress(compressed).decode("utf-8"))


def _all_coordinates(extract: dict[str, Any]) -> list[tuple[float, float]]:
    coordinates: list[tuple[float, float]] = []
    for path in extract["root"]["paths"]:
        coordinates.extend((float(lon), float(lat)) for lon, lat in path["coordinates"])
    for feature in extract["features"]:
        for path in feature["paths"]:
            coordinates.extend(
                (float(lon), float(lat)) for lon, lat in path["coordinates"]
            )
    if not coordinates:
        raise SystemExit(f"Extract {extract['course_id']} contains no geometry.")
    return coordinates


def _project_paths(
    paths: list[dict[str, Any]],
    *,
    origin_lat: float,
    origin_lon: float,
) -> list[dict[str, Any]]:
    cos_latitude = math.cos(math.radians(origin_lat))

    def point(raw: list[float]) -> list[float]:
        lon, lat = float(raw[0]), float(raw[1])
        x = math.radians(lon - origin_lon) * EARTH_RADIUS_M * cos_latitude
        y = math.radians(lat - origin_lat) * EARTH_RADIUS_M
        return [round(x, 2), round(y, 2)]

    result = []
    for path in paths:
        projected = [point(raw) for raw in path["coordinates"]]
        if not projected:
            continue
        if len(projected) >= 2:
            simplified = LineString(projected).simplify(
                CATALOG_SIMPLIFY_M, preserve_topology=True
            )
            projected = [
                [round(float(x), 2), round(float(y), 2)] for x, y in simplified.coords
            ]
        record = {"role": str(path.get("role", "line")), "points": projected}
        if path.get("source_ref"):
            record["source_ref"] = str(path["source_ref"])
        result.append(record)
    return result


def _geometry_digest(model: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "coordinate_system": model["coordinate_system"],
            "origin_wgs84": model["origin_wgs84"],
            "boundary": model["boundary"],
            "features": model["features"],
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(payload)


def _build_record(
    metadata: dict[str, Any],
    source_record: dict[str, Any],
    extract: dict[str, Any],
) -> dict[str, Any]:
    if (
        metadata["id"] != source_record["course_id"]
        or metadata["id"] != extract["course_id"]
    ):
        raise SystemExit(f"Course identity mismatch for {metadata['id']}.")
    coordinates = _all_coordinates(extract)
    origin_lon = sum(lon for lon, _ in coordinates) / len(coordinates)
    origin_lat = sum(lat for _, lat in coordinates) / len(coordinates)
    features: list[dict[str, Any]] = []
    for feature in extract["features"]:
        paths = _project_paths(
            feature["paths"], origin_lat=origin_lat, origin_lon=origin_lon
        )
        if not paths:
            continue
        features.append(
            {
                "source_ref": feature["source_ref"],
                "source_version": feature["source_version"],
                "source_timestamp": feature["source_timestamp"],
                "tags": feature["tags"],
                "paths": paths,
            }
        )
    model = {
        "coordinate_system": "local-equirectangular-metre",
        "origin_wgs84": [round(origin_lat, 8), round(origin_lon, 8)],
        "projection": {
            "method": "local-equirectangular-v1",
            "earth_radius_m": EARTH_RADIUS_M,
            "coordinates_rounded_m": 0.01,
            "catalog_simplification_tolerance_m": CATALOG_SIMPLIFY_M,
        },
        "boundary": _project_paths(
            extract["root"]["paths"], origin_lat=origin_lat, origin_lon=origin_lon
        ),
        "features": features,
    }
    model["geometry_sha256"] = _geometry_digest(model)
    osm_source = {
        "id": f"osm-{metadata['id']}-v1",
        "kind": "openstreetmap",
        "publisher": "OpenStreetMap contributors",
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
        "url": f"https://www.openstreetmap.org/{source_record['root_ref']}",
        "snapshot_date": source_record["snapshot_date"],
        "snapshot_path": source_record["extract_path"],
        "snapshot_sha256": source_record["extract_sha256"],
        "root_source_ref": source_record["root_ref"],
        "root_source_timestamp": source_record["root_source_timestamp"],
        "geometry_sha256": model["geometry_sha256"],
        "use": "Boundary, holes, playing surfaces, hazards, water, paths, vegetation, and buildings where mapped.",
        "method": source_record["selection_method"],
    }
    profile_source = {
        "id": f"profile-{metadata['id']}",
        "kind": "official-course-profile",
        "publisher": metadata["profile_publisher"],
        "license": "reference-only",
        "url": metadata["profile_url"],
        "use": "Course name and concise championship or geographic context only; no profile geometry traced.",
    }
    return {
        "id": metadata["id"],
        "title": metadata["title"],
        "subtitle": metadata["subtitle"],
        "subject_kind": "map",
        "format_id": FORMAT_ID,
        "location": metadata["location"],
        "championship_context": metadata["championship_context"],
        "selection_note": metadata["selection_note"],
        "sources": [osm_source, profile_source],
        "evidence": {
            "status": "source-derived-openstreetmap-course-study",
            "hole_inventory": "exactly-18-numbered-source-centrelines",
            "selection_method": extract["selection"]["method"],
            "feature_counts": extract["feature_counts"],
            "statement": (
                "All plotted course geometry is retained from the pinned OpenStreetMap extract; "
                "unmapped detail is omitted and the drawing is not a survey or scorecard."
            ),
        },
        "rights_status": "odbl-attribution-required",
        "notes": [
            "Course names identify geographic subjects and do not imply endorsement.",
            "Built-in pen widths are nominal and require stock/speed calibration before plotting.",
        ],
        "model": model,
    }


def main() -> int:
    manifest_bytes = MANIFEST_PATH.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("contract_id") != CATALOG_ID or manifest.get("schema_version") != 1:
        raise SystemExit(
            "Golf source manifest has the wrong contract or schema version."
        )
    source_by_id = {record["course_id"]: record for record in manifest["sources"]}
    expected = [metadata["id"] for metadata in COURSE_METADATA]
    if set(source_by_id) != set(expected):
        raise SystemExit(
            "Golf source manifest must contain exactly the curated 25 courses."
        )
    subjects = [
        _build_record(
            metadata,
            source_by_id[metadata["id"]],
            _load_extract(source_by_id[metadata["id"]]),
        )
        for metadata in COURSE_METADATA
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "catalog_id": CATALOG_ID,
        "data_snapshot": manifest["snapshot_date"],
        "source_contract": {
            "path": MANIFEST_PATH.relative_to(ROOT).as_posix(),
            "sha256": _sha256(manifest_bytes),
        },
        "series": {
            "title": "TWENTY-FIVE ICONS OF GOLF",
            "edition": "full-course-source-study-v2",
            "format_policy": "A3 portrait dense map edition",
            "selection_policy": (
                "A curated cross-section of globally famous major venues and highly regarded "
                "links, chosen only where the pinned source resolves exactly 18 numbered holes."
            ),
        },
        "subjects": subjects,
    }
    OUTPUT_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT_PATH.relative_to(ROOT)} ({len(subjects)} courses).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
