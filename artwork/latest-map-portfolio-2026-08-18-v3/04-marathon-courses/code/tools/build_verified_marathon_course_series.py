#!/usr/bin/env python3
"""Build a pinned, source-audited series of real marathon course plates.

This is intentionally separate from ``build_marathon_city_preview_series.py``.
That earlier migration proves only city basemap coverage and must continue to
say ``COURSE NOT INCLUDED``.  This builder admits a route only when:

* an organiser page (or the explicitly documented Boston OSM relation) binds
  the downloaded vector to the named event;
* the raw response and any deterministic normalization are retained;
* ``city_map_plotter.course`` accepts the line against 42.195 km; and
* the final plate contains a non-empty ``race_course`` layer and a pinned OSM
  basemap source.

The default output is review material, never finished artwork.  It is staged
beside the requested destination, validated, and promoted only when the full
14-course cohort passes.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gzip
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from city_map_plotter.course import (  # noqa: E402
    TRACE_LENGTH_TOLERANCE_UNDER,
    RaceCourse,
    course_from_overpass,
    course_from_track_file,
)
from city_map_plotter.geometry import (  # noqa: E402
    expand_bbox_to_aspect,
    load_plate_format,
    pad_bbox,
    poster_plate_for_extent,
)
from city_map_plotter.models import BoundingBox, MapPlotterError  # noqa: E402
from city_map_plotter.osm import (  # noqa: E402
    DEFAULT_OVERPASS_URL,
    fetch_course_relation,
    fetch_overpass,
)


GENERATED_AT = "2026-08-16T00:00:00Z"
DEFAULT_OUTPUT = ROOT / "review-output/marathon-course-plates-verified-2026-08-16-v1"
LEGACY_RELEASE = ROOT / "output/marathon-series"
COURSE_DISTANCE_M = 42_195.0
PNG_DPI = 254.0
FAMILIES = ("roads", "water", "railways", "parks")
USER_AGENT = (
    "city-map-plotter-marathon-source-audit/1.0 "
    "(+https://www.openstreetmap.org/copyright)"
)
OVERPASS_ENDPOINTS = (
    DEFAULT_OVERPASS_URL,
    "https://overpass.kumi.systems/api/interpreter",
)
DOWNLOAD_CACHE_ROOT: Path | None = None


class MarathonCourseBuildError(RuntimeError):
    """Raised when any provenance, geometry, render, or release gate fails."""


@dataclass(frozen=True)
class EvidencePage:
    label: str
    url: str
    filename: str
    required_markers: tuple[str, ...]
    allow_unavailable: bool = False


@dataclass(frozen=True)
class CourseSpec:
    id: str
    display_title: str
    event_name: str
    city: str
    country: str
    organiser: str
    edition: str
    vector_status: str
    landing_page_url: str
    geometry_url: str
    acquisition_kind: str
    raw_filename: str
    normalized_filename: str
    evidence_pages: tuple[EvidencePage, ...]
    geometry_markers: tuple[str, ...] = ()
    legacy_subject_id: str | None = None
    relation_id: int | None = None
    zip_member: str | None = None
    kml_document_marker: str | None = None
    kml_placemark: str | None = None
    source_note: str = ""

    @property
    def source_label(self) -> str:
        labels = {
            "direct-gpx": "ORGANISER GPX",
            "zip-gpx": "ORGANISER GPX",
            "google-mymaps-kml": "ORGANISER KML",
            "organiser-strava-embed": "ORGANISER STRAVA",
            "osm-relation": "VERIFIED OSM",
        }
        return labels[self.acquisition_kind]


def _page(
    label: str,
    url: str,
    filename: str,
    *required_markers: str,
    allow_unavailable: bool = False,
) -> EvidencePage:
    return EvidencePage(
        label,
        url,
        filename,
        tuple(required_markers),
        allow_unavailable=allow_unavailable,
    )


COURSES: tuple[CourseSpec, ...] = (
    CourseSpec(
        id="london-marathon-2026",
        display_title="LONDON MARATHON",
        event_name="2026 TCS London Marathon",
        city="London",
        country="United Kingdom",
        organiser="London Marathon Events",
        edition="2026",
        vector_status="current organiser-published 2026 vector",
        landing_page_url="https://www.londonmarathonevents.co.uk/london-marathon/course",
        geometry_url="https://strava-embeds.com/route/3477685973392799106",
        acquisition_kind="organiser-strava-embed",
        raw_filename="strava-route-3477685973392799106.html",
        normalized_filename="london-marathon-2026.geojson",
        evidence_pages=(
            _page(
                "organiser-course-page",
                "https://www.londonmarathonevents.co.uk/london-marathon/course",
                "organiser-course-page.html",
                "3477685973392799106",
            ),
        ),
        geometry_markers=("__ROUTE_DATA__", "2026 TCS London Marathon"),
        legacy_subject_id="marathon-london",
        source_note="The organiser page embeds this exact Strava route id.",
    ),
    CourseSpec(
        id="tokyo-marathon-2026",
        display_title="TOKYO MARATHON",
        event_name="Tokyo Marathon 2026",
        city="Tokyo",
        country="Japan",
        organiser="Tokyo Marathon Foundation",
        edition="2026",
        vector_status="current organiser-published 2026 vector",
        landing_page_url="https://www.marathon.tokyo/en/about/course/",
        geometry_url=(
            "https://www.google.com/maps/d/kml?"
            "mid=1jGYjkrF_m5K3rWgYJu1XqyMbjHPq2pQ&forcekml=1"
        ),
        acquisition_kind="google-mymaps-kml",
        raw_filename="tokyo-marathon-2026.kml",
        normalized_filename="tokyo-marathon-2026.kml",
        evidence_pages=(
            _page(
                "organiser-course-page",
                "https://www.marathon.tokyo/en/about/course/",
                "organiser-course-page.html",
                "1jGYjkrF_m5K3rWgYJu1XqyMbjHPq2pQ",
                "2026",
            ),
        ),
        geometry_markers=("Tokyo Marathon 2026",),
        legacy_subject_id="marathon-tokyo",
        kml_document_marker="Tokyo Marathon 2026",
    ),
    CourseSpec(
        id="valencia-marathon-2026",
        display_title="VALENCIA MARATHON",
        event_name="Valencia Marathon Trinidad Alfonso Zurich 2026",
        city="Valencia",
        country="Spain",
        organiser="Valencia Ciudad del Running",
        edition="2026",
        vector_status="current organiser-published 2026 vector",
        landing_page_url=(
            "https://www.valenciaciudaddelrunning.com/en/marathon/"
            "official-route-marathon/"
        ),
        geometry_url=(
            "https://www.google.com/maps/d/kml?"
            "mid=1BEeuxU3kRDyxewR7Pp4Rqu_7Knajvxo&forcekml=1"
        ),
        acquisition_kind="google-mymaps-kml",
        raw_filename="valencia-marathon-2026-all-layers.kml",
        normalized_filename="valencia-marathon-2026-race-line.geojson",
        evidence_pages=(
            _page(
                "organiser-course-page",
                "https://www.valenciaciudaddelrunning.com/en/marathon/"
                "official-route-marathon/",
                "organiser-course-page.html",
                "1BEeuxU3kRDyxewR7Pp4Rqu_7Knajvxo",
                "2026",
            ),
        ),
        geometry_markers=("RECORRIDO · RACE LINE",),
        legacy_subject_id="marathon-valencia",
        kml_placemark="RECORRIDO · RACE LINE",
        source_note=(
            "The raw map has starts, access lines and annotations; only the "
            "Placemark named RECORRIDO · RACE LINE is admitted."
        ),
    ),
    CourseSpec(
        id="boston-marathon-2026",
        display_title="BOSTON MARATHON",
        event_name="130th Boston Marathon",
        city="Boston",
        country="United States",
        organiser="Boston Athletic Association",
        edition="2026",
        vector_status="verified OSM course relation checked against official 2026 map",
        landing_page_url="https://www.baa.org/races/boston-marathon/the-course/",
        geometry_url="https://www.openstreetmap.org/relation/11680552",
        acquisition_kind="osm-relation",
        raw_filename="relation-11680552.json.gz",
        normalized_filename="boston-marathon-relation-11680552.geojson",
        evidence_pages=(
            _page(
                "organiser-course-page",
                "https://www.baa.org/races/boston-marathon/the-course/",
                "organiser-course-page.html",
                "2026-Course-Map.pdf",
            ),
            _page(
                "official-course-map",
                "https://www.baa.org/wp-content/uploads/2026/03/2026-Course-Map.pdf",
                "official-course-map.pdf",
                "%PDF",
            ),
        ),
        legacy_subject_id="marathon-boston",
        relation_id=11_680_552,
        source_note=(
            "The vector is OSM relation/11680552; the organiser's 2026 map is "
            "retained as the independent event/course identity check."
        ),
    ),
    CourseSpec(
        id="berlin-marathon-2025",
        display_title="BERLIN MARATHON",
        event_name="BMW Berlin Marathon",
        city="Berlin",
        country="Germany",
        organiser="SCC EVENTS",
        edition="2025",
        vector_status="latest vector currently published by organiser (2025)",
        landing_page_url="https://www.bmw-berlin-marathon.com/das-rennen/strecke",
        geometry_url=(
            "https://www.bmw-berlin-marathon.com/fileadmin/media/events/"
            "berlinmarathon/gpx/BM25_Marathon-Strecke.gpx"
        ),
        acquisition_kind="direct-gpx",
        raw_filename="BM25_Marathon-Strecke.gpx",
        normalized_filename="berlin-marathon-2025.gpx",
        evidence_pages=(
            _page(
                "organiser-course-page",
                "https://www.bmw-berlin-marathon.com/das-rennen/strecke",
                "organiser-course-page.html",
                "BM25_Marathon-Strecke.gpx",
            ),
        ),
        legacy_subject_id="marathon-berlin",
    ),
    CourseSpec(
        id="sydney-marathon-2025",
        display_title="SYDNEY MARATHON",
        event_name="TCS Sydney Marathon",
        city="Sydney",
        country="Australia",
        organiser="Pont3 / TCS Sydney Marathon",
        edition="2025",
        vector_status="latest vector currently published by organiser (2025)",
        landing_page_url="https://www.tcssydneymarathon.com/course",
        geometry_url=(
            "https://b6bf2f2b-ace0-4e08-b630-3e45a19de9b9.filesusr.com/ugd/"
            "cbc827_3c7f6f8e7cd54e4a8f570891c1cabff9.gpx?"
            "dn=2025%20TCS%20Sydney%20Marathon%20Course%20with%20KM%20markers.gpx"
        ),
        acquisition_kind="direct-gpx",
        raw_filename="2025-tcs-sydney-marathon-course.gpx",
        normalized_filename="sydney-marathon-2025.gpx",
        evidence_pages=(
            _page(
                "organiser-course-page",
                "https://www.tcssydneymarathon.com/course",
                "organiser-course-page.html",
                "cbc827_3c7f6f8e7cd54e4a8f570891c1cabff9",
                allow_unavailable=True,
            ),
        ),
        legacy_subject_id="marathon-sydney",
    ),
    CourseSpec(
        id="marrakech-marathon-2026",
        display_title="MARRAKECH MARATHON",
        event_name="Marathon International de Marrakech 2026",
        city="Marrakech",
        country="Morocco",
        organiser="Marathon International de Marrakech",
        edition="2026",
        vector_status="current organiser-linked 2026 vector",
        landing_page_url="https://marathonmarrakech.ma/parcours/?lang=en",
        geometry_url=(
            "https://drive.usercontent.google.com/download?"
            "id=1YtrJEfA9F7DrLXVgDHMG4e0LMXyiq1an&export=download&confirm=t"
        ),
        acquisition_kind="direct-gpx",
        raw_filename="marrakech-full-marathon-2026.gpx",
        normalized_filename="marrakech-marathon-2026.gpx",
        evidence_pages=(
            _page(
                "organiser-home-page",
                "https://marathonmarrakech.ma/",
                "organiser-home-page.html",
                "https://marathonmarrakech.ma/parcours/",
            ),
            _page(
                "organiser-linked-drive-folder",
                "https://drive.google.com/drive/folders/"
                "1pJ-5wmleDUIA2N4r1ApGiai1X7hUDKoc?usp=sharing",
                "organiser-linked-drive-folder.html",
                "1YtrJEfA9F7DrLXVgDHMG4e0LMXyiq1an",
            ),
            _page(
                "route-file-record",
                "https://drive.google.com/file/d/"
                "1YtrJEfA9F7DrLXVgDHMG4e0LMXyiq1an/view",
                "route-file-record.html",
                "1YtrJEfA9F7DrLXVgDHMG4e0LMXyiq1an",
            ),
        ),
        source_note=(
            "The organiser course page returned HTTP 500 at build time; its "
            "working official home-page link to /parcours/ and the linked "
            "Drive folder/file record are pinned with the GPX."
        ),
    ),
    CourseSpec(
        id="geneva-marathon-2026",
        display_title="GENEVA MARATHON",
        event_name="Generali Geneve Marathon 2026",
        city="Geneva",
        country="Switzerland",
        organiser="Generali Geneve Marathon",
        edition="2026",
        vector_status="latest organiser-published vector (2026)",
        landing_page_url="https://www.generaligenevemarathon.com/marathon",
        geometry_url=(
            "https://512763f9-e0e1-4531-92c1-a627ccca8323.filesusr.com/ugd/"
            "a32e51_1851e34d983d4d3ca2569fba05324ff3.gpx?dn=Marathon_ggm2026.gpx"
        ),
        acquisition_kind="direct-gpx",
        raw_filename="Marathon_ggm2026.gpx",
        normalized_filename="geneva-marathon-2026.gpx",
        evidence_pages=(
            _page(
                "organiser-course-page",
                "https://www.generaligenevemarathon.com/marathon",
                "organiser-course-page.html",
                "1851e34d983d4d3ca2569fba05324ff3",
            ),
        ),
    ),
    CourseSpec(
        id="rotterdam-marathon-2026",
        display_title="ROTTERDAM MARATHON",
        event_name="NN Marathon Rotterdam 2026",
        city="Rotterdam",
        country="Netherlands",
        organiser="Golazo / NN Marathon Rotterdam",
        edition="2026",
        vector_status="current organiser-published 2026 vector",
        landing_page_url="https://nnmarathonrotterdam.nl/parcoursen/",
        geometry_url=(
            "https://nnmarathonrotterdam.nl/wp-content/uploads/sites/52/2026/03/"
            "NN-Marathon-Rotterdam-2026-Marathon-DEF.gpx"
        ),
        acquisition_kind="direct-gpx",
        raw_filename="NN-Marathon-Rotterdam-2026-Marathon-DEF.gpx",
        normalized_filename="rotterdam-marathon-2026.gpx",
        evidence_pages=(
            _page(
                "organiser-course-page",
                "https://nnmarathonrotterdam.nl/parcoursen/",
                "organiser-course-page.html",
                "NN-Marathon-Rotterdam-2026-Marathon-DEF.gpx",
            ),
        ),
    ),
    CourseSpec(
        id="frankfurt-marathon-2026",
        display_title="FRANKFURT MARATHON",
        event_name="Mainova Frankfurt Marathon 2026",
        city="Frankfurt",
        country="Germany",
        organiser="motion events / Frankfurt Marathon",
        edition="2026",
        vector_status="current organiser-published 2026 vector",
        landing_page_url=(
            "https://www.frankfurt-marathon.com/dein-lauf/marathon/strecke/"
        ),
        geometry_url=(
            "https://deploy.frankfurt-marathon.com/wp-content/uploads/"
            "mainova-frankfurt-marathon_strecke_marathonstrecke_-01.gpx"
        ),
        acquisition_kind="direct-gpx",
        raw_filename="mainova-frankfurt-marathon-2026.gpx",
        normalized_filename="frankfurt-marathon-2026.gpx",
        evidence_pages=(
            _page(
                "organiser-course-page",
                "https://www.frankfurt-marathon.com/dein-lauf/marathon/strecke/",
                "organiser-course-page.html",
                "mainova-frankfurt-marathon_strecke_marathonstrecke_-01.gpx",
                "2026",
            ),
        ),
    ),
    CourseSpec(
        id="stockholm-marathon-2026",
        display_title="STOCKHOLM MARATHON",
        event_name="adidas Stockholm Marathon 2026",
        city="Stockholm",
        country="Sweden",
        organiser="Marathongruppen / Stockholm Marathon",
        edition="2026",
        vector_status="current organiser-published 2026 vector",
        landing_page_url="https://stockholmmarathon.se/start/banan/",
        geometry_url=(
            "https://stockholmmarathon.se/wp-content/uploads/2026/05/SM26h.gpx_.zip"
        ),
        acquisition_kind="zip-gpx",
        raw_filename="SM26h.gpx_.zip",
        normalized_filename="stockholm-marathon-2026.gpx",
        evidence_pages=(
            _page(
                "organiser-course-page",
                "https://stockholmmarathon.se/start/banan/",
                "organiser-course-page.html",
                "SM26h.gpx_.zip",
                "2026",
            ),
        ),
        zip_member="SM26h.gpx",
    ),
    CourseSpec(
        id="jakarta-marathon-2026",
        display_title="JAKARTA MARATHON",
        event_name="BTN Jakarta International Marathon 2026",
        city="Jakarta",
        country="Indonesia",
        organiser="BTN Jakarta International Marathon",
        edition="2026",
        vector_status="current organiser-published 2026 vector",
        landing_page_url="https://jakim.id/race",
        geometry_url=(
            "https://jakim.id/assets/file/"
            "Rute%20Marathon%20-%20BTN%20Jakim%202026.gpx"
        ),
        acquisition_kind="direct-gpx",
        raw_filename="Rute-Marathon-BTN-Jakim-2026.gpx",
        normalized_filename="jakarta-marathon-2026.gpx",
        evidence_pages=(
            _page(
                "organiser-course-page",
                "https://jakim.id/race",
                "organiser-course-page.html",
                "Rute Marathon - BTN Jakim 2026.gpx",
            ),
        ),
    ),
    CourseSpec(
        id="zermatt-marathon-2026",
        display_title="ZERMATT MARATHON",
        event_name="Gornergrat Zermatt Marathon 2026",
        city="Zermatt",
        country="Switzerland",
        organiser="Gornergrat Zermatt Marathon",
        edition="2026",
        vector_status="current organiser-published 2026 vector",
        landing_page_url="https://www.zermattmarathon.ch/en/our-courses/marathon",
        geometry_url=(
            "https://www.zermattmarathon.ch?"
            "action=get_file&id=30&resource_link_id=4a"
        ),
        acquisition_kind="direct-gpx",
        raw_filename="zermatt-marathon-2026.gpx",
        normalized_filename="zermatt-marathon-2026.gpx",
        evidence_pages=(
            _page(
                "organiser-course-page",
                "https://www.zermattmarathon.ch/en/our-courses/marathon",
                "organiser-course-page.html",
                "resource_link_id=4a",
            ),
        ),
    ),
    CourseSpec(
        id="cote-d-amour-marathon-2026",
        display_title="CÔTE D'AMOUR",
        event_name="Marathon International de la Cote d'Amour Amarris 2026",
        city="La Baule / Guerande",
        country="France",
        organiser="Marathon de la Cote d'Amour",
        edition="2026",
        vector_status="current organiser-published 2026 vector",
        landing_page_url="https://www.marathondelacotedamour.com/marathon",
        geometry_url=(
            "https://804ed606-f2eb-4923-b4d7-3c62fbd26f13.filesusr.com/ugd/"
            "d20372_a4ef2f4465ad49d5a4961dae3b3d44ba.gpx?dn=Marathon+International+"
            "de+la+C%C3%B4te+d%27Amour+Amarris.gpx"
        ),
        acquisition_kind="direct-gpx",
        raw_filename="marathon-cote-d-amour-amarris-2026.gpx",
        normalized_filename="cote-d-amour-marathon-2026.gpx",
        evidence_pages=(
            _page(
                "organiser-course-page",
                "https://www.marathondelacotedamour.com/marathon",
                "organiser-course-page.html",
                "a4ef2f4465ad49d5a4961dae3b3d44ba",
                "2026",
            ),
        ),
    ),
)


_BBOX_PATTERN = re.compile(
    r"\((-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),"
    r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)\);"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stable_digest(value: object) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def _require_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise MarathonCourseBuildError(f"Required regular file is missing: {path}")


def _copy_exact(source: Path, destination: Path) -> str:
    _require_file(source)
    if destination.exists():
        raise MarathonCourseBuildError(f"Release path collision: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = _sha256(source)
    shutil.copy2(source, destination)
    if _sha256(destination) != digest:
        raise MarathonCourseBuildError(f"Copy verification failed: {source}")
    return digest


def _relative_record(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _bbox_dict(bbox: BoundingBox) -> dict[str, float]:
    return {
        "west": bbox.west,
        "south": bbox.south,
        "east": bbox.east,
        "north": bbox.north,
    }


def _bbox_from_dict(value: dict[str, Any]) -> BoundingBox:
    return BoundingBox(
        float(value["west"]),
        float(value["south"]),
        float(value["east"]),
        float(value["north"]),
    )


def _contains(outer: BoundingBox, inner: BoundingBox, *, epsilon: float = 1e-9) -> bool:
    return (
        outer.west <= inner.west + epsilon
        and outer.south <= inner.south + epsilon
        and outer.east >= inner.east - epsilon
        and outer.north >= inner.north - epsilon
    )


def _query_extent(query: str) -> BoundingBox:
    match = _BBOX_PATTERN.search(query)
    if match is None:
        raise MarathonCourseBuildError("Overpass query contains no readable bbox.")
    south, west, north, east = map(float, match.groups())
    return BoundingBox(west, south, east, north)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _download(
    url: str,
    *,
    required_markers: Sequence[str] = (),
    attempts: int = 5,
    max_bytes: int = 50 * 1024 * 1024,
) -> tuple[bytes, dict[str, Any]]:
    """Download one source with bounded retries and marker evidence."""

    cache_data: Path | None = None
    cache_metadata: Path | None = None
    if DOWNLOAD_CACHE_ROOT is not None:
        key = _sha256_bytes(url.encode("utf-8"))
        cache_data = DOWNLOAD_CACHE_ROOT / f"{key}.bin"
        cache_metadata = DOWNLOAD_CACHE_ROOT / f"{key}.json"
        if cache_data.is_file() and cache_metadata.is_file():
            data = cache_data.read_bytes()
            lower = data.lower()
            missing = [
                marker
                for marker in required_markers
                if marker.encode("utf-8").lower() not in lower
            ]
            if not missing and len(data) <= max_bytes:
                metadata = json.loads(cache_metadata.read_text(encoding="utf-8"))
                metadata["from_download_cache"] = True
                metadata["required_markers"] = list(required_markers)
                return data, metadata

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "en-GB,en;q=0.8",
            },
        )
        try:
            with urlopen(request, timeout=90) as response:
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise MarathonCourseBuildError(
                        f"Source exceeds {max_bytes} bytes: {url}"
                    )
                lower = data.lower()
                missing = [
                    marker
                    for marker in required_markers
                    if marker.encode("utf-8").lower() not in lower
                ]
                if missing:
                    raise MarathonCourseBuildError(
                        f"Source {url} is missing required marker(s): {missing}"
                    )
                metadata = {
                    "requested_url": url,
                    "final_url": response.geturl(),
                    "http_status": int(response.status),
                    "content_type": response.headers.get("Content-Type"),
                    "retrieved_at": _utc_now(),
                    "required_markers": list(required_markers),
                    "attempt": attempt,
                }
                if cache_data is not None and cache_metadata is not None:
                    cache_data.parent.mkdir(parents=True, exist_ok=True)
                    cache_data.write_bytes(data)
                    _write_json(cache_metadata, metadata)
                return data, metadata
        except MarathonCourseBuildError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            retryable = not isinstance(exc, HTTPError) or exc.code in {
                408,
                425,
                429,
                500,
                502,
                503,
                504,
            }
            if not retryable or attempt == attempts:
                break
            delay = min(20.0, 2.0**attempt)
            print(
                f"  retrying {url} after {type(exc).__name__}: {exc} "
                f"({delay:g}s)",
                flush=True,
            )
            time.sleep(delay)
    raise MarathonCourseBuildError(f"Could not download {url}: {last_error}")


class _RouteDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._capture = False
        self.fragments: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.casefold() == "script" and dict(attrs).get("id") == "__ROUTE_DATA__":
            self._capture = True

    def handle_data(self, data: str) -> None:
        if self._capture:
            self.fragments.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._capture:
            self._capture = False


def _normalise_london(raw: Path, destination: Path, spec: CourseSpec) -> dict[str, Any]:
    parser = _RouteDataParser()
    parser.feed(raw.read_text(encoding="utf-8"))
    if not parser.fragments:
        raise MarathonCourseBuildError("London Strava embed has no __ROUTE_DATA__.")
    try:
        route = json.loads("".join(parser.fragments))
        coordinates = route["coordinates"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise MarathonCourseBuildError(f"London route data is malformed: {exc}") from exc
    line: list[list[float]] = []
    for coordinate in coordinates:
        if not isinstance(coordinate, list) or len(coordinate) < 2:
            raise MarathonCourseBuildError("London route has an invalid coordinate.")
        line.append([float(coordinate[0]), float(coordinate[1])])
    if len(line) < 2:
        raise MarathonCourseBuildError("London route has fewer than two coordinates.")
    document = {
        "type": "FeatureCollection",
        "name": spec.event_name,
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": spec.event_name,
                    "source_route_id": "3477685973392799106",
                },
                "geometry": {"type": "LineString", "coordinates": line},
            }
        ],
    }
    _write_json(destination, document)
    return {
        "operation": "extract-embedded-route-data-coordinates",
        "selector": "script#__ROUTE_DATA__.coordinates",
        "input_coordinate_count": len(coordinates),
        "output_coordinate_count": len(line),
        "elevation_discarded": True,
    }


def _normalise_valencia(
    raw: Path, destination: Path, spec: CourseSpec
) -> dict[str, Any]:
    try:
        root = ET.fromstring(raw.read_bytes())
    except ET.ParseError as exc:
        raise MarathonCourseBuildError(f"Valencia KML is malformed: {exc}") from exc
    selected: list[list[list[float]]] = []
    names: list[str] = []
    for placemark in root.findall(".//{*}Placemark"):
        name_node = placemark.find("./{*}name")
        name = "" if name_node is None else "".join(name_node.itertext()).strip()
        names.append(name)
        if name != spec.kml_placemark:
            continue
        for node in placemark.findall(".//{*}LineString/{*}coordinates"):
            points: list[list[float]] = []
            for token in (node.text or "").split():
                fields = token.split(",")
                if len(fields) >= 2:
                    points.append([float(fields[0]), float(fields[1])])
            if len(points) >= 2:
                selected.append(points)
    if not selected:
        raise MarathonCourseBuildError(
            f"Valencia KML has no Placemark named {spec.kml_placemark!r}."
        )
    features = [
        {
            "type": "Feature",
            "properties": {"name": spec.kml_placemark, "part": index},
            "geometry": {"type": "LineString", "coordinates": points},
        }
        for index, points in enumerate(selected, 1)
    ]
    _write_json(
        destination,
        {"type": "FeatureCollection", "name": spec.event_name, "features": features},
    )
    return {
        "operation": "select-exact-kml-placemark",
        "selector": spec.kml_placemark,
        "raw_placemark_count": len(names),
        "selected_line_count": len(selected),
        "output_coordinate_count": sum(len(line) for line in selected),
        "excluded_placemark_count": sum(name != spec.kml_placemark for name in names),
    }


def _normalise_stockholm(
    raw: Path, destination: Path, spec: CourseSpec
) -> dict[str, Any]:
    if spec.zip_member is None:
        raise MarathonCourseBuildError("Stockholm ZIP member is not declared.")
    try:
        with zipfile.ZipFile(raw) as archive:
            names = archive.namelist()
            if spec.zip_member not in names:
                raise MarathonCourseBuildError(
                    f"Stockholm ZIP has {names!r}, not {spec.zip_member!r}."
                )
            info = archive.getinfo(spec.zip_member)
            if info.is_dir() or Path(info.filename).name != info.filename:
                raise MarathonCourseBuildError("Stockholm ZIP member path is unsafe.")
            data = archive.read(info)
    except zipfile.BadZipFile as exc:
        raise MarathonCourseBuildError(f"Stockholm source is not a ZIP: {exc}") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return {
        "operation": "extract-exact-zip-member",
        "member": spec.zip_member,
        "archive_members": names,
        "uncompressed_size_bytes": len(data),
    }


def _course_geojson(course: RaceCourse, spec: CourseSpec) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "name": spec.event_name,
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": spec.event_name,
                    "source_ref": course.source_ref,
                    "part": index,
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[lon, lat] for lat, lon in part],
                },
            }
            for index, part in enumerate(course.parts, 1)
        ],
    }


def _canonical_json_sha256(path: Path) -> str:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    return _stable_digest(value)


def _normalise_downloaded_route(
    spec: CourseSpec, raw: Path, destination: Path
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if spec.acquisition_kind == "organiser-strava-embed":
        return _normalise_london(raw, destination, spec)
    if spec.kml_placemark:
        return _normalise_valencia(raw, destination, spec)
    if spec.acquisition_kind == "zip-gpx":
        return _normalise_stockholm(raw, destination, spec)
    if spec.kml_document_marker:
        data = raw.read_bytes()
        if spec.kml_document_marker.encode("utf-8") not in data:
            raise MarathonCourseBuildError(
                f"{spec.id}: KML document marker is missing."
            )
        destination.write_bytes(data)
        return {
            "operation": "identity-copy-after-kml-document-marker-check",
            "document_marker": spec.kml_document_marker,
        }
    destination.write_bytes(raw.read_bytes())
    return {"operation": "identity-copy-organiser-gpx"}


def _verify_course(spec: CourseSpec, path: Path) -> RaceCourse:
    try:
        course = course_from_track_file(
            path,
            course_id=spec.id,
            source_ref=spec.geometry_url,
            name=spec.event_name,
            official_distance_m=COURSE_DISTANCE_M,
            tolerance=TRACE_LENGTH_TOLERANCE_UNDER,
        )
    except MapPlotterError as exc:
        raise MarathonCourseBuildError(f"{spec.id}: {exc}") from exc
    if not course.verification.accepted:
        raise MarathonCourseBuildError(f"{spec.id}: course verification rejected.")
    return course


def _fetch_relation_source(
    spec: CourseSpec, raw: Path, normalized: Path, cache_dir: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    if spec.relation_id is None:
        raise MarathonCourseBuildError(f"{spec.id}: no relation id declared.")
    last_error: Exception | None = None
    acquisition = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            acquisition = fetch_course_relation(
                spec.relation_id,
                endpoint=endpoint,
                user_agent=USER_AGENT,
                cache_dir=cache_dir,
                timeout_s=600,
            )
            break
        except (MapPlotterError, OSError) as exc:
            last_error = exc
            print(f"  relation endpoint failed: {endpoint}: {exc}", flush=True)
    if acquisition is None:
        raise MarathonCourseBuildError(
            f"{spec.id}: could not fetch relation: {last_error}"
        )
    cache_path = Path(str(acquisition.cache_path))
    _copy_exact(cache_path, raw)
    course = course_from_overpass(
        acquisition.data,
        course_id=spec.id,
        source_ref=f"relation/{spec.relation_id}",
        official_distance_m=COURSE_DISTANCE_M,
        tolerance=TRACE_LENGTH_TOLERANCE_UNDER,
    )
    _write_json(normalized, _course_geojson(course, spec))
    transformation = {
        "operation": "assemble-osm-route-relation-and-emit-geojson",
        "source_ref": f"relation/{spec.relation_id}",
        "query": acquisition.query,
        "query_sha256": _sha256_bytes((acquisition.query or "").encode("utf-8")),
        "endpoint": acquisition.endpoint,
        "from_cache": acquisition.from_cache,
        "part_count": len(course.parts),
        "output_coordinate_count": sum(len(part) for part in course.parts),
    }
    raw_meta = {
        "requested_url": spec.geometry_url,
        "final_url": spec.geometry_url,
        "http_status": 200,
        "content_type": "application/json+gzip",
        "retrieved_at": _utc_now(),
        "acquisition_endpoint": acquisition.endpoint,
        "from_cache": acquisition.from_cache,
    }
    return raw_meta, transformation


def _acquire_routes(contract_root: Path, cache_dir: Path) -> list[RaceCourse]:
    raw_root = contract_root / "routes/raw"
    normalized_root = contract_root / "routes/normalized"
    route_records: list[dict[str, Any]] = []
    verifications: list[dict[str, Any]] = []
    courses: list[RaceCourse] = []

    for position, spec in enumerate(COURSES, 1):
        print(f"[{position:02d}/14] acquiring route evidence: {spec.id}", flush=True)
        spec_raw_root = raw_root / spec.id
        evidence_records: list[dict[str, Any]] = []
        for page in spec.evidence_pages:
            try:
                data, response = _download(
                    page.url,
                    required_markers=page.required_markers,
                    max_bytes=20 * 1024 * 1024,
                )
            except MarathonCourseBuildError as exc:
                if not page.allow_unavailable:
                    raise
                print(f"  evidence page unavailable; recording hold: {exc}", flush=True)
                evidence_records.append(
                    {
                        "label": page.label,
                        "source": {
                            "requested_url": page.url,
                            "retrieved_at": _utc_now(),
                            "required_markers": list(page.required_markers),
                            "access_error": str(exc),
                        },
                        "file": None,
                        "status": "remote-page-unavailable-raw-vector-still-acquired",
                    }
                )
                continue
            destination = spec_raw_root / page.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            evidence_records.append(
                {
                    "label": page.label,
                    "source": response,
                    "file": _relative_record(destination, contract_root),
                }
            )
            time.sleep(0.35)

        raw = spec_raw_root / spec.raw_filename
        normalized = normalized_root / spec.normalized_filename
        if spec.acquisition_kind == "osm-relation":
            raw_meta, transformation = _fetch_relation_source(
                spec, raw, normalized, cache_dir
            )
        else:
            data, raw_meta = _download(
                spec.geometry_url,
                required_markers=spec.geometry_markers,
                max_bytes=30 * 1024 * 1024,
            )
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(data)
            transformation = _normalise_downloaded_route(spec, raw, normalized)
        course = _verify_course(spec, normalized)
        courses.append(course)
        verification = course.verification.as_dict()
        verification["course_id"] = spec.id
        verification["normalized_path"] = normalized.relative_to(
            contract_root
        ).as_posix()
        verifications.append(verification)
        print(
            f"  accepted {course.measured_length_m / 1000:.3f} km, "
            f"{course.verification.coordinate_count} coordinates",
            flush=True,
        )
        route_records.append(
            {
                "position": position,
                "course": {
                    key: value
                    for key, value in asdict(spec).items()
                    if key != "evidence_pages"
                },
                "official_evidence": evidence_records,
                "raw_geometry_source": {
                    "source": raw_meta,
                    "file": _relative_record(raw, contract_root),
                },
                "normalization": transformation,
                "normalized_geometry": _relative_record(normalized, contract_root),
                "verification": verification,
            }
        )

    logical = {
        "schema_version": 1,
        "id": "verified-marathon-route-sources-2026-08-16-v1",
        "generated_at": GENERATED_AT,
        "status": "review-only-source-evidence",
        "policy": {
            "certified_distance_m": COURSE_DISTANCE_M,
            "short_tolerance_fraction": TRACE_LENGTH_TOLERANCE_UNDER,
            "long_tolerance_fraction": 0.03,
            "minimum_largest_component_fraction": 0.8,
            "no_raster_tracing": True,
            "no_hand_drawn_geometry": True,
        },
        "course_count": len(route_records),
        "courses": route_records,
    }
    _write_json(
        contract_root / "route-source-manifest.json",
        {**logical, "contract_sha256": _stable_digest(logical)},
    )
    _write_json(
        contract_root / "route-verification.json",
        {
            "schema_version": 1,
            "generated_at": GENERATED_AT,
            "accepted_count": sum(item["accepted"] for item in verifications),
            "rejected_count": sum(not item["accepted"] for item in verifications),
            "courses": verifications,
        },
    )
    return courses


def _load_reused_routes(contract_root: Path) -> list[RaceCourse]:
    manifest_path = contract_root / "route-source-manifest.json"
    _require_file(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("courses")
    if not isinstance(records, list) or len(records) != len(COURSES):
        raise MarathonCourseBuildError("Reused route manifest has the wrong cohort.")
    by_id = {
        item.get("course", {}).get("id"): item
        for item in records
        if isinstance(item, dict) and isinstance(item.get("course"), dict)
    }
    courses: list[RaceCourse] = []
    for spec in COURSES:
        record = by_id.get(spec.id)
        if not isinstance(record, dict):
            raise MarathonCourseBuildError(f"Reused route is missing: {spec.id}")
        normalized_record = record.get("normalized_geometry")
        if not isinstance(normalized_record, dict):
            raise MarathonCourseBuildError(f"{spec.id}: normalized record missing.")
        path = contract_root / str(normalized_record.get("path"))
        _require_file(path)
        if _sha256(path) != normalized_record.get("sha256"):
            raise MarathonCourseBuildError(f"{spec.id}: normalized hash changed.")
        for evidence in record.get("official_evidence", []):
            file_record = evidence.get("file", {})
            if file_record is None:
                continue
            evidence_path = contract_root / str(file_record.get("path"))
            _require_file(evidence_path)
            if _sha256(evidence_path) != file_record.get("sha256"):
                raise MarathonCourseBuildError(f"{spec.id}: evidence hash changed.")
        raw_record = record.get("raw_geometry_source", {}).get("file", {})
        raw_path = contract_root / str(raw_record.get("path"))
        _require_file(raw_path)
        if _sha256(raw_path) != raw_record.get("sha256"):
            raise MarathonCourseBuildError(f"{spec.id}: raw source hash changed.")
        courses.append(_verify_course(spec, path))
    return courses


def _render_bbox(course: RaceCourse) -> tuple[BoundingBox, str]:
    padded_course = pad_bbox(course.bbox(), fraction=0.04)
    format_id = poster_plate_for_extent(padded_course, preset="a4-balanced-poster")
    aspect = float(load_plate_format(format_id)["map_field_aspect"])
    return expand_bbox_to_aspect(padded_course, aspect), format_id


def _legacy_basemap(
    spec: CourseSpec, required_bbox: BoundingBox
) -> tuple[Path, dict[str, Any]] | None:
    if not spec.legacy_subject_id:
        return None
    manifest_path = LEGACY_RELEASE / f"{spec.legacy_subject_id}.plot.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source = manifest["source"]
        provenance = source["provenance"]
        query = provenance["overpass_query"]
        cache_path = Path(source["cache_path"])
        source_extent = _query_extent(query)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if not cache_path.is_file() or not _contains(source_extent, required_bbox):
        return None
    expected_hash = provenance.get("source_file_sha256")
    actual_hash = _sha256(cache_path)
    if expected_hash and expected_hash != actual_hash:
        raise MarathonCourseBuildError(
            f"{spec.id}: legacy basemap cache no longer matches its manifest."
        )
    return cache_path, {
        "acquisition_mode": "reused-exact-legacy-overpass-response",
        "legacy_manifest": manifest_path.relative_to(ROOT).as_posix(),
        "endpoint": source.get("endpoint"),
        "query": query,
        "query_sha256": _sha256_bytes(query.encode("utf-8")),
        "query_extent_wgs84": _bbox_dict(source_extent),
        "osm_base_timestamp": source.get("timestamp"),
        "canonical_json_sha256": provenance.get("canonical_source_data_sha256"),
        "from_cache": True,
    }


def _live_basemap(
    spec: CourseSpec, render_bbox: BoundingBox, cache_dir: Path
) -> tuple[Path, dict[str, Any]]:
    acquisition_bbox = pad_bbox(render_bbox)
    errors: list[str] = []
    for endpoint in OVERPASS_ENDPOINTS:
        print(f"  querying basemap: {endpoint}", flush=True)
        try:
            acquisition = fetch_overpass(
                acquisition_bbox,
                FAMILIES,
                endpoint=endpoint,
                user_agent=USER_AGENT,
                cache_dir=cache_dir,
                timeout_s=600,
                max_response_mb=512,
            )
            path = Path(str(acquisition.cache_path))
            _require_file(path)
            data = acquisition.data
            osm3s = data.get("osm3s") if isinstance(data, dict) else None
            timestamp = osm3s.get("timestamp_osm_base") if isinstance(osm3s, dict) else None
            query = acquisition.query or ""
            return path, {
                "acquisition_mode": "live-overpass-then-pinned",
                "endpoint": acquisition.endpoint,
                "query": query,
                "query_sha256": _sha256_bytes(query.encode("utf-8")),
                "query_extent_wgs84": _bbox_dict(acquisition_bbox),
                "osm_base_timestamp": timestamp,
                "canonical_json_sha256": _stable_digest(data),
                "from_cache": acquisition.from_cache,
            }
        except (MapPlotterError, OSError) as exc:
            errors.append(f"{endpoint}: {exc}")
            print(f"  basemap endpoint failed: {endpoint}: {exc}", flush=True)
    raise MarathonCourseBuildError(
        f"{spec.id}: all basemap endpoints failed: {'; '.join(errors)}"
    )


def _acquire_basemaps(
    contract_root: Path, courses: Sequence[RaceCourse], cache_dir: Path
) -> dict[str, dict[str, Any]]:
    osm_root = contract_root / "osm"
    entries: list[dict[str, Any]] = []
    bindings: dict[str, dict[str, Any]] = {}
    for position, (spec, course) in enumerate(zip(COURSES, courses), 1):
        print(f"[{position:02d}/14] pinning OSM basemap: {spec.id}", flush=True)
        render_bbox, format_id = _render_bbox(course)
        legacy = _legacy_basemap(spec, render_bbox)
        if legacy is not None:
            source_path, acquisition = legacy
            print("  using covered exact legacy OSM response", flush=True)
        else:
            source_path, acquisition = _live_basemap(spec, render_bbox, cache_dir)
        destination = osm_root / f"{spec.id}.json.gz"
        _copy_exact(source_path, destination)
        file_record = _relative_record(destination, contract_root)
        canonical = _canonical_json_sha256(destination)
        declared_canonical = acquisition.get("canonical_json_sha256")
        if declared_canonical and canonical != declared_canonical:
            raise MarathonCourseBuildError(
                f"{spec.id}: canonical basemap digest changed during pinning."
            )
        query_extent = _bbox_from_dict(acquisition["query_extent_wgs84"])
        if not _contains(query_extent, render_bbox):
            raise MarathonCourseBuildError(
                f"{spec.id}: pinned basemap does not cover the render extent."
            )
        entry = {
            "position": position,
            "course_id": spec.id,
            "file": file_record,
            "canonical_json_sha256": canonical,
            "render_extent_wgs84": _bbox_dict(render_bbox),
            "format_id": format_id,
            **acquisition,
        }
        entries.append(entry)
        bindings[spec.id] = entry

    logical = {
        "schema_version": 1,
        "id": "verified-marathon-osm-basemaps-2026-08-16-v1",
        "generated_at": GENERATED_AT,
        "status": "review-only-pinned-overpass-json",
        "families": list(FAMILIES),
        "license": {
            "data": "Open Database License (ODbL) 1.0",
            "attribution": "© OpenStreetMap contributors",
            "copyright_url": "https://www.openstreetmap.org/copyright",
            "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        },
        "entry_count": len(entries),
        "entries": entries,
    }
    _write_json(
        contract_root / "osm-source-manifest.json",
        {**logical, "contract_sha256": _stable_digest(logical)},
    )
    return bindings


def _load_reused_basemaps(contract_root: Path) -> dict[str, dict[str, Any]]:
    path = contract_root / "osm-source-manifest.json"
    _require_file(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != len(COURSES):
        raise MarathonCourseBuildError("Reused OSM manifest has the wrong cohort.")
    bindings: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("course_id"), str):
            raise MarathonCourseBuildError("Reused OSM manifest entry is malformed.")
        source = contract_root / str(entry.get("file", {}).get("path"))
        _require_file(source)
        if _sha256(source) != entry.get("file", {}).get("sha256"):
            raise MarathonCourseBuildError(
                f"{entry['course_id']}: reused OSM source hash changed."
            )
        if _canonical_json_sha256(source) != entry.get("canonical_json_sha256"):
            raise MarathonCourseBuildError(
                f"{entry['course_id']}: reused canonical OSM hash changed."
            )
        bindings[entry["course_id"]] = entry
    if set(bindings) != {spec.id for spec in COURSES}:
        raise MarathonCourseBuildError("Reused OSM source ids differ from the catalog.")
    return bindings


def _route_manifest_by_id(contract_root: Path) -> dict[str, dict[str, Any]]:
    manifest = json.loads(
        (contract_root / "route-source-manifest.json").read_text(encoding="utf-8")
    )
    return {
        item["course"]["id"]: item
        for item in manifest["courses"]
        if isinstance(item, dict) and isinstance(item.get("course"), dict)
    }


def _rebase_text_paths(path: Path, staging: Path, final: Path) -> int:
    data = path.read_bytes()
    old = str(staging.resolve()).encode("utf-8")
    new = str(final.resolve()).encode("utf-8")
    count = data.count(old)
    if count:
        path.write_bytes(data.replace(old, new))
    return count


def _png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise MarathonCourseBuildError(f"Not a PNG: {path}")
    return struct.unpack(">II", header[16:24])


def _rasterize(svg: Path, png: Path, *, final_svg: Path, final_png: Path) -> dict[str, Any]:
    inkscape = shutil.which("inkscape")
    if inkscape is None:
        raise MarathonCourseBuildError("Inkscape is required for PNG output.")
    result = subprocess.run(
        [
            inkscape,
            str(svg),
            "--export-type=png",
            "--export-area-page",
            f"--export-dpi={PNG_DPI:g}",
            "--export-background=white",
            "--export-background-opacity=255",
            f"--export-filename={png}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MarathonCourseBuildError(
            f"Inkscape failed for {svg}: {(result.stderr or result.stdout).strip()}"
        )
    width, height = _png_dimensions(png)
    version = subprocess.run(
        [inkscape, "--version"], capture_output=True, text=True, check=False
    ).stdout.strip()
    return {
        "format": "PNG",
        "path": str(final_png.resolve()),
        "dpi": PNG_DPI,
        "width_px": width,
        "height_px": height,
        "background": "opaque white",
        "renderer": version or "Inkscape",
        "source_svg": str(final_svg.resolve()),
        "source_svg_sha256": _sha256(svg),
        "png_sha256": _sha256(png),
    }


def _run(command: Sequence[str], *, label: str) -> str:
    result = subprocess.run(
        list(command),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MarathonCourseBuildError(
            f"{label} failed ({result.returncode}):\n"
            + (result.stderr or result.stdout).strip()
        )
    return result.stdout


def _render_plates(
    staging: Path,
    final: Path,
    contract_root: Path,
    courses: Sequence[RaceCourse],
    basemaps: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    route_records = _route_manifest_by_id(contract_root)
    plates_root = staging / "plates"
    final_plates_root = final / "plates"
    artifacts: list[dict[str, Any]] = []
    qa_records: list[dict[str, Any]] = []
    checkpoints_root = final.parent / f".{final.name}.checkpoints"
    checkpoints_root.mkdir(parents=True, exist_ok=True)
    legacy_checkpoints_root = staging / ".build-checkpoints"
    recovery_artifacts: dict[str, dict[str, Any]] = {}
    recovery_qa: dict[str, dict[str, Any]] = {}
    try:
        previous_release = json.loads(
            (staging / "release-manifest.json").read_text(encoding="utf-8")
        )
        recovery_artifacts = {
            str(item["course_id"]): item
            for item in previous_release.get("artifacts", [])
            if isinstance(item, dict) and isinstance(item.get("course_id"), str)
        }
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    try:
        previous_qa = json.loads(
            (staging / "release-qa.json").read_text(encoding="utf-8")
        )
        recovery_qa = {
            str(item["course_id"]): item
            for item in previous_qa.get("plates", [])
            if isinstance(item, dict) and isinstance(item.get("course_id"), str)
        }
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    mapplot = ROOT / ".venv/bin/mapplot"
    if not mapplot.is_file():
        raise MarathonCourseBuildError(f"mapplot executable is missing: {mapplot}")

    indexed_courses = list(enumerate(zip(COURSES, courses), 1))
    # Render the final catalog entry first. It has the longest display-name history,
    # so furniture-fit regressions fail before any of the expensive city compiles.
    render_order = indexed_courses[-1:] + indexed_courses[:-1]
    for position, (spec, verified) in render_order:
        print(f"[{position:02d}/14] rendering course plate: {spec.id}", flush=True)
        stem = f"{position:03d}-{spec.id}"
        svg = plates_root / f"{stem}.svg"
        manifest_path = plates_root / f"{stem}.plot.json"
        png = plates_root / f"{stem}.png"
        checkpoint_path = checkpoints_root / f"{stem}.json"
        checkpoint_candidates: list[dict[str, Any]] = []
        for candidate_path in (
            checkpoint_path,
            legacy_checkpoints_root / f"{stem}.json",
        ):
            if not candidate_path.is_file():
                continue
            try:
                checkpoint_candidates.append(
                    json.loads(candidate_path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError):
                pass
        if spec.id in recovery_artifacts and spec.id in recovery_qa:
            checkpoint_candidates.append(
                {
                    "schema_version": 1,
                    "artifact": recovery_artifacts[spec.id],
                    "qa": recovery_qa[spec.id],
                }
            )
        for checkpoint in checkpoint_candidates:
            artifact = checkpoint.get("artifact")
            qa_record = checkpoint.get("qa")
            output_records = (
                artifact.get("svg") if isinstance(artifact, dict) else None,
                artifact.get("png") if isinstance(artifact, dict) else None,
                artifact.get("plot_manifest") if isinstance(artifact, dict) else None,
            )
            if (
                isinstance(artifact, dict)
                and isinstance(qa_record, dict)
                and artifact.get("course_id") == spec.id
                and qa_record.get("course_id") == spec.id
                and all(
                    isinstance(record, dict)
                    and isinstance(record.get("path"), str)
                    and isinstance(record.get("sha256"), str)
                    and (staging / record["path"]).is_file()
                    and _sha256(staging / record["path"]) == record["sha256"]
                    for record in output_records
                )
            ):
                print(f"[{position:02d}/14] verified checkpoint: {spec.id}", flush=True)
                _write_json(checkpoint_path, checkpoint)
                artifacts.append(artifact)
                qa_records.append(qa_record)
                break
        else:
            artifact = None
        if isinstance(artifact, dict):
            continue
        final_svg = final_plates_root / svg.name
        final_png = final_plates_root / png.name
        route_record = route_records[spec.id]
        route_path = contract_root / route_record["normalized_geometry"]["path"]
        basemap = basemaps[spec.id]
        basemap_path = contract_root / basemap["file"]["path"]
        style_path = contract_root / "marathon-style-lean.json"
        command = [
            str(mapplot),
            "export",
            "--course-file",
            str(route_path),
            "--input-json",
            str(basemap_path),
            "--output",
            str(svg),
            "--manifest",
            str(manifest_path),
            "--preset",
            "a4-balanced-poster",
            "--layers",
            ",".join(FAMILIES),
            "--detail-profile",
            "plotter-faithful",
            "--simplify-mm",
            "0.04",
            "--road-style",
            "centreline",
            "--style",
            str(style_path),
            "--no-optimise",
            "--attribution-mode",
            "external",
            "--external-attribution-placement",
            "Companion release ATTRIBUTION.md",
            "--scale-bar",
            "--no-physical-audit",
            "--course-distance-km",
            "42.195",
            "--course-tolerance",
            f"{TRACE_LENGTH_TOLERANCE_UNDER:g}",
            "--title",
            spec.display_title,
            "--subtitle",
            "42.195 KM",
        ]
        output = _run(command, label=f"render {spec.id}")
        if not svg.is_file() or not manifest_path.is_file():
            raise MarathonCourseBuildError(f"{spec.id}: renderer omitted artifacts.")
        _rebase_text_paths(svg, staging, final)
        _rebase_text_paths(manifest_path, staging, final)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MarathonCourseBuildError(
                f"{spec.id}: invalid plot manifest: {exc}"
            ) from exc
        layer = next(
            (
                item
                for item in manifest.get("layers", [])
                if isinstance(item, dict) and item.get("id") == "race_course"
            ),
            None,
        )
        if not isinstance(layer, dict) or int(layer.get("path_count", 0)) <= 0:
            raise MarathonCourseBuildError(
                f"{spec.id}: final manifest has no emitted race_course paths."
            )
        if manifest.get("race_course", {}).get("verification", {}).get("accepted") is not True:
            raise MarathonCourseBuildError(
                f"{spec.id}: final manifest lacks accepted course verification."
            )
        provenance = manifest.get("source", {}).get("provenance", {})
        if provenance.get("source_pinned") is not True:
            raise MarathonCourseBuildError(f"{spec.id}: basemap is not pinned.")
        if provenance.get("source_file_sha256") != basemap["file"]["sha256"]:
            raise MarathonCourseBuildError(
                f"{spec.id}: rendered basemap hash differs from the contract."
            )
        rendering = manifest.get("rendering", {})
        if (
            rendering.get("attribution_mode") != "external"
            or rendering.get("visible_attribution") is not False
        ):
            raise MarathonCourseBuildError(
                f"{spec.id}: map-data attribution must be external, not on-page."
            )
        svg_text = svg.read_text(encoding="utf-8")
        if 'id="layer-race_course"' not in svg_text:
            raise MarathonCourseBuildError(f"{spec.id}: race course SVG group missing.")
        if "COURSE NOT INCLUDED" in svg_text:
            raise MarathonCourseBuildError(
                f"{spec.id}: stale city-preview disclosure reached the course plate."
            )
        manifest["release_source_binding"] = {
            "schema_version": 1,
            "course_id": spec.id,
            "route_source_manifest": str(
                (final / "source-contract/route-source-manifest.json").resolve()
            ),
            "route_source_manifest_sha256": _sha256(
                contract_root / "route-source-manifest.json"
            ),
            "normalized_route_path": str(
                (final / "source-contract" / route_record["normalized_geometry"]["path"]).resolve()
            ),
            "normalized_route_sha256": route_record["normalized_geometry"]["sha256"],
            "official_page_url": spec.landing_page_url,
            "geometry_source_url": spec.geometry_url,
            "vector_status": spec.vector_status,
            "osm_source_manifest": str(
                (final / "source-contract/osm-source-manifest.json").resolve()
            ),
            "osm_source_sha256": basemap["file"]["sha256"],
        }
        _write_json(manifest_path, manifest)
        raster = _rasterize(
            svg,
            png,
            final_svg=final_svg,
            final_png=final_png,
        )
        manifest["raster_exports"] = [raster]
        _write_json(manifest_path, manifest)
        plotsim = _run(
            [
                sys.executable,
                str(ROOT / "tools/plotsim.py"),
                str(svg),
                "--order",
                "document",
                "--strict-svg",
            ],
            label=f"plotsim {spec.id}",
        )
        page = manifest.get("page", {})
        orientation = page.get("orientation")
        paper = str(page.get("paper", "")).casefold()
        format_id = f"{paper}-{orientation}" if paper and orientation else None
        artifact = {
            "position": position,
            "course_id": spec.id,
            "title": spec.display_title,
            "edition": spec.edition,
            "format_id": format_id,
            "orientation": orientation,
            "measured_length_m": round(verified.measured_length_m, 3),
            "svg": _relative_record(svg, staging),
            "png": _relative_record(png, staging),
            "plot_manifest": _relative_record(manifest_path, staging),
        }
        qa_record = {
            "course_id": spec.id,
            "format_id": artifact["format_id"],
            "race_course_path_count": int(layer["path_count"]),
            "course_verification_accepted": True,
            "pinned_basemap": True,
            "on_page_map_data_attribution": False,
            "route_source_bound": True,
            "png_dimensions": list(_png_dimensions(png)),
            "plotsim_document_order": plotsim.strip().splitlines(),
            "renderer_summary": output.strip().splitlines()[:20],
        }
        artifacts.append(artifact)
        qa_records.append(qa_record)
        _write_json(
            checkpoint_path,
            {"schema_version": 1, "artifact": artifact, "qa": qa_record},
        )
    artifacts.sort(key=lambda item: int(item["position"]))
    positions = {spec.id: position for position, spec in enumerate(COURSES, 1)}
    qa_records.sort(key=lambda item: positions[str(item["course_id"])])
    shutil.rmtree(legacy_checkpoints_root, ignore_errors=True)
    return artifacts, qa_records


def _build_contact_sheets(
    staging: Path, artifacts: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    contact_root = staging / "contact-sheets"
    groups: list[tuple[str, list[Path]]] = [
        (
            "marathon-course-contact-sheet",
            [staging / item["png"]["path"] for item in artifacts],
        )
    ]
    for orientation in ("portrait", "landscape"):
        paths = [
            staging / item["png"]["path"]
            for item in artifacts
            if item.get("orientation") == orientation
        ]
        if paths:
            groups.append((f"marathon-course-{orientation}-contact-sheet", paths))
    records: list[dict[str, Any]] = []
    for stem, paths in groups:
        output = contact_root / f"{stem}.png"
        columns = 4 if len(paths) >= 8 else 3
        _run(
            [
                sys.executable,
                str(ROOT / "tools/build_contact_sheet.py"),
                *map(str, paths),
                "--out",
                str(output),
                "--columns",
                str(columns),
                "--keep-svg",
            ],
            label=f"contact sheet {stem}",
        )
        svg = output.with_suffix(".svg")
        records.append(
            {
                "id": stem,
                "plate_count": len(paths),
                "png": _relative_record(output, staging),
                "svg": _relative_record(svg, staging),
            }
        )
    return records


def _validate_formats(artifacts: Sequence[dict[str, Any]], staging: Path) -> str:
    svgs = [str(staging / item["svg"]["path"]) for item in artifacts]
    return _run(
        [sys.executable, str(ROOT / "tools/validate_format.py"), *svgs],
        label="binding format validation",
    )


def _copy_code_and_contract_docs(staging: Path) -> list[dict[str, Any]]:
    files = (
        "tools/build_verified_marathon_course_series.py",
        "tools/build_contact_sheet.py",
        "tools/validate_format.py",
        "tools/plotsim.py",
        "src/city_map_plotter/course.py",
        "src/city_map_plotter/cli.py",
        "src/city_map_plotter/geometry.py",
        "src/city_map_plotter/osm.py",
        "src/city_map_plotter/cartography.py",
        "src/city_map_plotter/svg.py",
        "src/city_map_plotter/furniture.py",
        "src/city_map_plotter/svgkit.py",
        "src/city_map_plotter/styles.py",
        "docs/format/format-v1.json",
        "docs/format/FORMAT.md",
        "docs/reproducibility/REPRODUCING_MAPS.md",
        "CODEX_MAP_HANDOFF.md",
    )
    records: list[dict[str, Any]] = []
    for relative in files:
        source = ROOT / relative
        destination = staging / "code-and-contract" / relative
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                raise MarathonCourseBuildError(
                    f"Generated code snapshot path is not a regular file: {destination}"
                )
            destination.unlink()
        _copy_exact(source, destination)
        records.append(_relative_record(destination, staging))
    return records


def _write_release_docs(staging: Path, artifacts: Sequence[dict[str, Any]]) -> None:
    table = "\n".join(
        "| {position} | {title} | {edition} | {format_id} | {measured:.3f} |".format(
            position=item["position"],
            title=item["title"],
            edition=item["edition"],
            format_id=item["format_id"],
            measured=item["measured_length_m"] / 1000.0,
        )
        for item in artifacts
    )
    _write_text(
        staging / "README.md",
        f"""# Verified marathon course plates — 2026-08-16 v1

This is the corrected marathon cohort: **14 real vector course plates**, not
marathon-labelled city previews. Every example has a master SVG, a 254 DPI PNG,
a plot manifest, an accepted 42.195 km verification, a pinned OpenStreetMap
basemap, and retained organiser/route evidence.

No course was traced from a raster, converted from a photograph, or drawn by
the generator. London is extracted from the organiser-embedded Strava route;
Tokyo and Valencia come from organiser-linked KML; the GPX courses are exact
organiser downloads; Boston is verified OSM relation/11680552 checked against
the organiser's current course page/map.

Berlin and Sydney are labelled 2025 because those are the latest vector files
their organiser pages currently publish. The other organiser vectors are 2026.
This package is review-only: route-file redistribution rights, external map-data
attribution placement, physical pen calibration, and event-day change checks
remain release gates. The quadratic close-pair physical audit is also deferred
to the selected physical pen/stock proof; format, nib gating, PlotSim and SVG
structure are still checked in this digital cohort. The visual recipe is the
exact `output/marathon-series/marathon-style-lean.json` design the earlier good
course sheets used: full course framing, full qualifying city linework, the
same pen colours and the same `RACE COURSE` / measured distance / scale copy.
The course overprints the basemap without an optional clearance halo. Travel
ordering is deferred to the selected plotter/machine job rather than frozen
into these review masters.

| # | Plate | edition | binding format | measured km |
|---:|---|---:|---|---:|
{table}

## Contents

- `plates/`: 14 SVG/PNG/plot-manifest triplets.
- `contact-sheets/`: complete and orientation-specific PNG/SVG sheets.
- `source-contract/routes/raw/`: exact downloaded evidence and route responses.
- `source-contract/routes/normalized/`: the only route files admitted to rendering.
- `source-contract/route-source-manifest.json`: source chain, hashes and transforms.
- `source-contract/route-verification.json`: measured-length/topology evidence.
- `source-contract/osm/`: exact pinned basemap responses.
- `source-contract/osm-source-manifest.json`: bbox/query/hash provenance.
- `code-and-contract/`: generator, course gate, renderer and binding plate spec.
- `LLM_HANDOFF.md`, `REPRODUCE.md`, `ATTRIBUTION.md`, `release-qa.json`, checksums.

The superseded `marathon-city-previews-2026-08-16-v3` cohort remains a valid
city-basemap study but is not a course-map source and is not used here.
""",
    )
    _write_text(
        staging / "REPRODUCE.md",
        """# Reproduce from pinned source evidence

Run from the city-map-plotter repository root. This mode performs no route or
basemap download; it copies and verifies the existing source contract, then
reruns the course gate, renderer, PNG export, PlotSim, format validator and
contact-sheet builder.

```bash
.venv/bin/python tools/build_verified_marathon_course_series.py \
  --reuse-sources review-output/marathon-course-plates-verified-2026-08-16-v1/source-contract \
  --output-dir review-output/marathon-course-plates-reproduction
```

For a fresh current-source audit, omit `--reuse-sources`. A fresh run may yield
different hashes if an organiser has replaced its official vector or page.
Those changes must be reviewed; they are not silently treated as the same
course edition.
""",
    )
    _write_text(
        staging / "LLM_HANDOFF.md",
        """# LLM handoff — marathon course plates

Do not merge this cohort with the older marathon city previews. The defining
gate is `source-contract/route-source-manifest.json`: each red line must resolve
to its normalized route, raw bytes, organiser evidence and accepted measurement.

Order of operations:

1. Verify all hashes in `CHECKSUMS.sha256`.
2. Read `CODEX_MAP_HANDOFF.md` and `docs/reproducibility/REPRODUCING_MAPS.md`.
3. Read the route and OSM source manifests before changing a course or extent.
4. Never trace a PDF/image or repair a route by drawing missing links.
5. Re-run the 42.195 km and largest-component gates after any source change.
6. Render only from `routes/normalized/` plus the matching pinned OSM JSON.
7. Require a non-empty `race_course` layer and embedded verification evidence.
8. Run `tools/validate_format.py`, PlotSim, PNG pairing and contact sheets.
9. Keep outputs under `review-output`; they are not finished physical artwork.

Valencia's normalization is intentionally selective: only the exact Placemark
`RECORRIDO · RACE LINE` is used. London extracts only `__ROUTE_DATA__.coordinates`.
Stockholm extracts only `SM26h.gpx` from the organiser ZIP. Boston assembles
only OSM relation/11680552. These transformations are evidence, not optional
cleanup.

Before calling any route “current,” re-check the organiser page. Berlin and
Sydney are consciously retained as the latest organiser-published 2025 vectors;
do not relabel them 2026 without a new official vector.
""",
    )
    _write_text(
        staging / "ATTRIBUTION.md",
        """# Attribution and source status

Map data © OpenStreetMap contributors, licensed under ODbL 1.0.

- https://www.openstreetmap.org/copyright
- https://opendatacommons.org/licenses/odbl/1-0/

Course vectors remain attributed to the event organisers and source services
named in `source-contract/route-source-manifest.json`. They are retained here
as review/reproducibility evidence. This package does not assert permission to
redistribute or sell those route files or derived plates. Resolve those rights,
event marks, and the external OSM credit placement before publication.
""",
    )
    _write_text(
        staging / "SUPERSEDES.md",
        """# Supersession

For marathon **course-map** review, this release supersedes:

`review-output/marathon-city-previews-2026-08-16-v3`

That older release remains truthful on its own terms because its plates say
`COURSE NOT INCLUDED`; it is a city-basemap cohort, not a course cohort. None of
its route-shaped research files or SVG artwork was used by this release.
""",
    )


def _write_checksums(root: Path) -> tuple[int, str]:
    destination = root / "CHECKSUMS.sha256"
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path != destination and not path.is_symlink()
    )
    _write_text(
        destination,
        "\n".join(f"{_sha256(path)}  {path.relative_to(root).as_posix()}" for path in files),
    )
    return len(files), _sha256(destination)


def _validate_no_staging_paths(staging: Path, final: Path) -> None:
    needle = str(staging.resolve()).encode("utf-8")
    stale: list[str] = []
    for path in staging.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".json", ".svg", ".md", ".txt"}:
            if needle in path.read_bytes():
                stale.append(path.relative_to(staging).as_posix())
    if stale:
        raise MarathonCourseBuildError(
            "Ephemeral staging paths remain: " + ", ".join(stale[:10])
        )
    if staging.resolve() == final.resolve():
        raise MarathonCourseBuildError("Staging and final paths must differ.")


def _catalog_document() -> dict[str, Any]:
    records = []
    for position, spec in enumerate(COURSES, 1):
        value = asdict(spec)
        value["position"] = position
        value["evidence_pages"] = [asdict(page) for page in spec.evidence_pages]
        records.append(value)
    logical = {
        "schema_version": 1,
        "id": "verified-marathon-course-plates-2026-08-16-v1",
        "as_of": "2026-08-16",
        "selection_method": (
            "Fourteen newest defensible organiser-published vectors available "
            "to this audit, plus Boston's verified OSM relation checked against "
            "the official current course map. No raster tracing."
        ),
        "course_count": len(records),
        "courses": records,
    }
    return {**logical, "catalog_sha256": _stable_digest(logical)}


def build(output_dir: Path, *, reuse_sources: Path | None, cache_dir: Path) -> Path:
    global DOWNLOAD_CACHE_ROOT
    DOWNLOAD_CACHE_ROOT = cache_dir / "marathon-source-audit"
    final = output_dir.expanduser().resolve()
    if final.exists():
        raise MarathonCourseBuildError(f"Destination already exists: {final}")
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = (final.parent / f".{final.name}.work").resolve()
    if staging.exists() and not staging.is_dir():
        raise MarathonCourseBuildError(f"Resume path is not a directory: {staging}")
    if staging.is_dir():
        print(f"resuming staged build: {staging}", flush=True)
    else:
        staging.mkdir()
    try:
        contract_root = staging / "source-contract"
        if contract_root.is_dir():
            print(f"reusing staged source contract: {contract_root}", flush=True)
            courses = _load_reused_routes(contract_root)
            basemaps = _load_reused_basemaps(contract_root)
        elif reuse_sources is not None:
            source = reuse_sources.expanduser().resolve()
            if source == contract_root or not source.is_dir():
                raise MarathonCourseBuildError(
                    f"Reusable source contract is not a directory: {source}"
                )
            print(f"copying pinned source contract: {source}", flush=True)
            shutil.copytree(source, contract_root)
            courses = _load_reused_routes(contract_root)
            basemaps = _load_reused_basemaps(contract_root)
        else:
            contract_root.mkdir(parents=True)
            _write_json(contract_root / "course-catalog.json", _catalog_document())
            courses = _acquire_routes(contract_root, cache_dir)
            basemaps = _acquire_basemaps(contract_root, courses, cache_dir)

        if not (contract_root / "course-catalog.json").is_file():
            _write_json(contract_root / "course-catalog.json", _catalog_document())
        render_contract = {
            "schema_version": 1,
            "id": "verified-marathon-course-render-v1",
            "generated_at": GENERATED_AT,
            "preset": "a4-balanced-poster",
            "orientation_policy": "course-extent-selects-binding-format",
            "families": list(FAMILIES),
            "detail_profile": "plotter-faithful",
            "simplify_mm": 0.04,
            "road_style": "centreline",
            "adaptive_detail": False,
            "style_contract": "source-contract/marathon-style-lean.json",
            "style_source": "output/marathon-series/marathon-style-lean.json",
            "travel_optimisation": False,
            "travel_optimisation_status": "deferred-to-selected-machine-job",
            "course_clearance_mm": 0.0,
            "course_clearance_policy": (
                "no optional route halo; contracted red race-course strokes "
                "overprint the basemap"
            ),
            "physical_audit": False,
            "physical_audit_status": (
                "deferred-to-physical-pen-stock-proof; quadratic close-pair scan "
                "is not a digital-release gate"
            ),
            "course_distance_km": 42.195,
            "short_tolerance_fraction": TRACE_LENGTH_TOLERANCE_UNDER,
            "long_tolerance_fraction": 0.03,
            "attribution_mode": "external",
            "png_dpi": PNG_DPI,
            "review_only": True,
        }
        legacy_style = LEGACY_RELEASE / "marathon-style-lean.json"
        style_destination = contract_root / "marathon-style-lean.json"
        if style_destination.exists():
            if _sha256(style_destination) != _sha256(legacy_style):
                raise MarathonCourseBuildError(
                    "Reused marathon style differs from the good legacy course sheets."
                )
        else:
            _copy_exact(legacy_style, style_destination)
        _write_json(contract_root / "render-contract.json", render_contract)
        artifacts, qa_records = _render_plates(
            staging, final, contract_root, courses, basemaps
        )
        format_output = _validate_formats(artifacts, staging)
        contacts = _build_contact_sheets(staging, artifacts)
        _write_release_docs(staging, artifacts)
        code_records = _copy_code_and_contract_docs(staging)
        qa = {
            "schema_version": 1,
            "generated_at": GENERATED_AT,
            "status": "passed",
            "expected_course_count": len(COURSES),
            "actual_course_count": len(artifacts),
            "all_routes_verified": all(
                item["course_verification_accepted"] for item in qa_records
            ),
            "all_basemaps_pinned": all(item["pinned_basemap"] for item in qa_records),
            "all_race_course_layers_nonempty": all(
                item["race_course_path_count"] > 0 for item in qa_records
            ),
            "format_validator_output": format_output.strip().splitlines(),
            "plates": qa_records,
        }
        release_qa_path = staging / "release-qa.json"
        _write_json(release_qa_path, qa)
        _rebase_text_paths(release_qa_path, staging, final)
        release_logical = {
            "schema_version": 1,
            "id": "marathon-course-plates-verified-2026-08-16-v1",
            "generated_at": GENERATED_AT,
            "status": "review-only-complete",
            "course_count": len(artifacts),
            "artifact_triplet_count": len(artifacts),
            "contact_sheet_count": len(contacts),
            "artifacts": artifacts,
            "contact_sheets": contacts,
            "code_and_contract_files": code_records,
            "source_contracts": {
                "catalog": "source-contract/course-catalog.json",
                "routes": "source-contract/route-source-manifest.json",
                "route_verification": "source-contract/route-verification.json",
                "osm": "source-contract/osm-source-manifest.json",
                "render": "source-contract/render-contract.json",
                "style": "source-contract/marathon-style-lean.json",
            },
            "qa": "release-qa.json",
            "supersedes_for_course_maps": (
                "review-output/marathon-city-previews-2026-08-16-v3"
            ),
        }
        _write_json(
            staging / "release-manifest.json",
            {**release_logical, "release_contract_sha256": _stable_digest(release_logical)},
        )
        _validate_no_staging_paths(staging, final)
        file_count, checksum_hash = _write_checksums(staging)
        os.replace(staging, final)
        shutil.rmtree(
            final.parent / f".{final.name}.checkpoints", ignore_errors=True
        )
        print(
            json.dumps(
                {
                    "release": str(final),
                    "course_plates": len(artifacts),
                    "contact_sheets": len(contacts),
                    "files_checksummed": file_count,
                    "checksums_sha256": checksum_hash,
                    "status": "complete-review-release",
                },
                indent=2,
            ),
            flush=True,
        )
        return final
    except Exception:
        print(f"resumable staging retained: {staging}", file=sys.stderr, flush=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--reuse-sources",
        type=Path,
        help=(
            "Existing source-contract directory to verify and reuse offline. "
            "Omit for a fresh organiser/Overpass acquisition."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".cache/city-map-plotter",
        help="Download cache used only during fresh acquisition.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        build(
            args.output_dir,
            reuse_sources=args.reuse_sources,
            cache_dir=args.cache_dir.expanduser().resolve(),
        )
    except (MarathonCourseBuildError, MapPlotterError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
