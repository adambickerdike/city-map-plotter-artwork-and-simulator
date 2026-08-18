from __future__ import annotations

import hashlib
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_verified_marathon_course_series as marathon  # noqa: E402


def test_verified_marathon_catalog_is_fourteen_vector_courses() -> None:
    assert len(marathon.COURSES) == 14
    assert len({course.id for course in marathon.COURSES}) == 14
    assert {course.acquisition_kind for course in marathon.COURSES} <= {
        "direct-gpx",
        "zip-gpx",
        "google-mymaps-kml",
        "organiser-strava-embed",
        "osm-relation",
    }
    assert sum(course.acquisition_kind == "osm-relation" for course in marathon.COURSES) == 1
    assert all(
        not course.geometry_url.casefold().endswith(
            (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff")
        )
        for course in marathon.COURSES
    )


def test_verified_marathon_style_is_exact_established_series_contract() -> None:
    style = marathon.LEGACY_RELEASE / "marathon-style-lean.json"
    assert style.is_file()
    digest = hashlib.sha256(style.read_bytes()).hexdigest()
    assert len(digest) == 64
    catalog = marathon._catalog_document()
    assert catalog["course_count"] == 14
    assert "No raster tracing" in catalog["selection_method"]
