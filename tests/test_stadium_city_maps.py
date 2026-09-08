# ruff: noqa: E402
"""Geographic stadium preservation and release integration regressions."""

import json
import sys
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_stadium_city_maps import (
    verify,
    verify_native_geometry,
    verify_club_header,
    PACKAGE,
    ORIGINAL,
    SVG,
)
from verify_repository import _repository_artwork_files


@pytest.fixture
def stadium():
    row = next(
        r
        for r in json.loads((ORIGINAL / "catalog.json").read_text())["stadiums"]
        if r["id"] == "newcastle_united"
    )
    p = PACKAGE / "premier_league/newcastle_united"
    return (
        ET.parse(p / "newcastle_united.svg").getroot(),
        json.loads((ORIGINAL / row["files"]["stadium_overlay"]["path"]).read_text()),
        json.loads((p / "newcastle_united.plot.json").read_text()),
    )


def test_all_stadium_city_artifacts():
    report = verify()
    assert report["stadiums"] == 44
    assert report["native_stadium_paths"] > 1800
    assert report["physical_execution_allowed"] is False
    assert report["club_headers"] == 44
    assert report["stadium_linear_enlargement_vs_previous"] == 1.25


def test_missing_club_header_is_rejected(stadium):
    root, _, manifest = stadium
    root.remove(root.find(f"{SVG}g[@id='layer-poster_club']"))
    with pytest.raises(ValueError, match="Football club header is missing"):
        verify_club_header(root, manifest, "Newcastle United FC")


def test_wrong_club_header_is_rejected(stadium):
    root, _, manifest = stadium
    with pytest.raises(ValueError, match="header copy differs"):
        verify_club_header(root, manifest, "Arsenal FC")


def test_club_header_overlap_is_rejected(stadium):
    root, _, manifest = stadium
    club = root.find(f"{SVG}g[@id='layer-poster_club']")
    for path in club:
        path.set("d", "M 27.353,40 L 100,40 L 100,44 L 27.353,44")
    with pytest.raises(ValueError, match="sit clearly between"):
        verify_club_header(root, manifest, "Newcastle United FC")


def test_missing_stadium_path_is_rejected(stadium):
    root, overlay, manifest = stadium
    group = root.find(f"{SVG}g[@id='layer-stadium_detail']")
    group.remove(next(group.iter(SVG + "path")))
    with pytest.raises(ValueError, match="identity/coverage"):
        verify_native_geometry(root, overlay, manifest)


def test_cubic_control_point_movement_is_rejected(stadium):
    root, overlay, manifest = stadium
    path = next(
        p
        for p in root.iter(SVG + "path")
        if p.get("data-stadium-path-id") and " C " in p.get("d")
    )
    d = path.get("d").split()
    index = d.index("C") + 1
    d[index] = str(float(d[index]) + 0.05)
    path.set("d", " ".join(d))
    with pytest.raises(ValueError, match="control point moved"):
        verify_native_geometry(root, overlay, manifest)


def test_ignored_previews_do_not_enter_published_inventory():
    assert all(
        p.relative_to(ROOT).parts[0] != "build"
        for p in _repository_artwork_files("*.svg")
    )
