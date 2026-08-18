#!/usr/bin/env python3
"""Assemble the newest reviewed map cohorts into one evidence-backed portfolio.

The portfolio is deliberately written below ``review-output``.  It is a review
and handoff package, not finished artwork.  Every distinct plate is copied as
an SVG/PNG/plot-manifest triplet; pen-separated machine jobs are omitted because
they are not additional designs.  Source contracts, release reports, relevant
generator entry points, handoff documents, and checksums travel with the art.

Run from the repository root after the frozen university v2.1.4 build exists::

    .venv/bin/python tools/build_latest_map_portfolio.py --check-only
    .venv/bin/python tools/build_latest_map_portfolio.py

The destination must not already exist.  Assembly happens in an adjacent
temporary directory and is promoted atomically only after every expected cohort
and file pairing has passed its gate.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Sequence

from externalize_portfolio_map_attribution import (
    AttributionTransformError,
    TRANSFORM_ID,
    audit_svgs,
    externalize_triplet,
)


ROOT = Path(__file__).resolve().parent.parent
GENERATED_AT = "2026-08-18T00:00:00Z"
DEFAULT_OUTPUT = ROOT / "review-output/latest-map-portfolio-2026-08-18-v3"

UNIVERSITY_RELEASE = ROOT / "review-output/university-memorabilia-ranked-2026-v2.1.4"
HIKING_RELEASE = ROOT / "output/hiking-series-paired-v4.2-2026-08-06"
MARATHON_RELEASE = (
    ROOT / "review-output/marathon-course-plates-verified-2026-08-16-v1"
)
F1_CURRENT_RELEASE = (
    ROOT / "review-output/f1-circuit-atlas-2026-v2.3-format-v1-2026-08-16"
)
F1_LEGACY_RELEASE = (
    ROOT / "review-output/f1-circuit-atlas-legacy-v2.3-format-v1-2026-08-16-r2"
)
GOLF_RELEASE = ROOT / "output/golf-course-series-v4"
ROWING_A3_RELEASE = ROOT / "review-output/rowing-heads-a3"
ROWING_A5_RELEASE = ROOT / "review-output/rowing-heads-a5"

UK_UNIVERSITY_COLLECTION = "uk-times-good-university-guide-2026-top-30"
US_UNIVERSITY_COLLECTION = "us-qs-world-university-rankings-2027-top-20"

FORMAT_IDS = (
    "a5-portrait",
    "a5-landscape",
    "a4-portrait",
    "a4-landscape",
    "a3-portrait",
    "a3-landscape",
)
ROWING_COURSE_IDS = frozenset(
    {
        "head-of-the-charles",
        "henley-royal",
        "horr-london",
        "pairs-head-london",
    }
)


class PortfolioError(RuntimeError):
    """Raised when a source cohort or destination is unsafe or incomplete."""


@dataclass(frozen=True)
class CopyItem:
    source: Path
    destination: Path


@dataclass(frozen=True)
class Cohort:
    cohort_id: str
    release_root: Path
    artwork_root: Path
    expected_count: int
    artwork_prefix: Path = Path()
    contact_sheets: tuple[Path, ...] = ()
    metadata_directories: tuple[str, ...] = ()
    recursive: bool = True


@dataclass(frozen=True)
class Domain:
    directory: str
    title: str
    selection: str
    status: str
    caveat: str
    reproduction: str
    cohorts: tuple[Cohort, ...]
    contracts: tuple[CopyItem, ...]
    code: tuple[CopyItem, ...]
    docs: tuple[CopyItem, ...]
    generate_contact_sheet: bool = False
    regenerate_contact_sheets: bool = False


def _item(path: str, destination: str | None = None) -> CopyItem:
    source = ROOT / path
    return CopyItem(source, Path(destination or path))


def _hiking_contracts() -> tuple[CopyItem, ...]:
    data_dir = ROOT / "src/city_map_plotter/data"
    return tuple(
        CopyItem(path, Path("hiking-data") / path.name)
        for path in sorted(data_dir.glob("hike*"))
    )


def _domains() -> tuple[Domain, ...]:
    university_contract = _item(
        "contracts/university-memorabilia-v2.1",
        "university-memorabilia-v2.1",
    )
    university_code = (
        _item("tools/build_ranked_university_catalog.py"),
        _item("tools/build_ranked_university_series.py"),
        _item("tools/build_university_source_contract.py"),
        _item("tools/qa_ranked_university_series.py"),
        _item("tools/finalize_ranked_university_series.py"),
        _item("tools/check_map_reproducibility.py"),
        _item("src/city_map_plotter/data/ranked-universities-2026-v1.json"),
        _item("styles/university-memorabilia-v2.json"),
    )
    university_docs = (
        _item("contracts/university-memorabilia-v2.1/README.md", "CONTRACT.md"),
        _item(
            "docs/reproducibility/REPRODUCING_MAPS.md",
            "REPRODUCING_MAPS.md",
        ),
        _item("docs/THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"),
    )
    return (
        Domain(
            directory="01-university-cities-uk",
            title="UK university cities — ranked v2.1.4",
            selection=(
                "The Times and Sunday Times Good University Guide 2026 top 30, "
                "as frozen in the ranked-university catalog."
            ),
            status="Frozen exact-source digital review cohort.",
            caveat=(
                "Review-only until physical pen calibration and the documented "
                "external attribution/rights checks are complete."
            ),
            reproduction=(
                ".venv/bin/python tools/check_map_reproducibility.py\n"
                ".venv/bin/python tools/build_ranked_university_series.py \\\n"
                "  --output-dir review-output/"
                "university-memorabilia-ranked-2026-v2.1.4\n"
                ".venv/bin/python tools/finalize_ranked_university_series.py \\\n"
                "  review-output/university-memorabilia-ranked-2026-v2.1.4/"
                "ranked-universities.batch.json\n"
                ".venv/bin/python tools/qa_ranked_university_series.py \\\n"
                "  review-output/university-memorabilia-ranked-2026-v2.1.4/"
                "ranked-universities.batch.json --release-mode review"
            ),
            cohorts=(
                Cohort(
                    cohort_id="uk-ranked-v2.1.4",
                    release_root=UNIVERSITY_RELEASE,
                    artwork_root=UNIVERSITY_RELEASE / UK_UNIVERSITY_COLLECTION,
                    expected_count=30,
                    contact_sheets=(
                        UNIVERSITY_RELEASE / "uk-ranked-universities-contact-sheet.png",
                    ),
                    metadata_directories=("release-metadata",),
                ),
            ),
            contracts=(university_contract,),
            code=university_code,
            docs=university_docs,
            regenerate_contact_sheets=True,
        ),
        Domain(
            directory="02-university-cities-us",
            title="US university cities — ranked v2.1.4",
            selection=(
                "QS World University Rankings 2027 top 20 US institutions, "
                "as frozen in the ranked-university catalog."
            ),
            status="Frozen exact-source digital review cohort.",
            caveat=(
                "Review-only until physical pen calibration and the documented "
                "external attribution/rights checks are complete."
            ),
            reproduction=(
                ".venv/bin/python tools/check_map_reproducibility.py\n"
                ".venv/bin/python tools/build_ranked_university_series.py \\\n"
                "  --output-dir review-output/"
                "university-memorabilia-ranked-2026-v2.1.4\n"
                ".venv/bin/python tools/finalize_ranked_university_series.py \\\n"
                "  review-output/university-memorabilia-ranked-2026-v2.1.4/"
                "ranked-universities.batch.json\n"
                ".venv/bin/python tools/qa_ranked_university_series.py \\\n"
                "  review-output/university-memorabilia-ranked-2026-v2.1.4/"
                "ranked-universities.batch.json --release-mode review"
            ),
            cohorts=(
                Cohort(
                    cohort_id="us-ranked-v2.1.4",
                    release_root=UNIVERSITY_RELEASE,
                    artwork_root=UNIVERSITY_RELEASE / US_UNIVERSITY_COLLECTION,
                    expected_count=20,
                    contact_sheets=(
                        UNIVERSITY_RELEASE / "us-ranked-universities-contact-sheet.png",
                    ),
                    metadata_directories=("release-metadata",),
                ),
            ),
            contracts=(university_contract,),
            code=university_code,
            docs=university_docs,
            regenerate_contact_sheets=True,
        ),
        Domain(
            directory="03-hiking-maps",
            title="Hiking maps — paired contour release v4.2",
            selection=(
                "All 40 frozen routes, each as detailed-map and terrain-relief "
                "variants: 80 distinct A5 plates."
            ),
            status="Digital QA passed; review-only.",
            caveat=(
                "Not a navigation product. Physical proof, per-source rights, "
                "and intended-stock pen calibration remain required."
            ),
            reproduction=(
                "mapplot-hike build --all --format catalog \\\n"
                "  --output-dir output/hiking-series-paired-v4.2-2026-08-06\n"
                "PYTHONPATH=src .venv/bin/python tools/qa_niche_series.py \\\n"
                "  output/hiking-series-paired-v4.2-2026-08-06\n"
                "PYTHONPATH=src .venv/bin/python tools/audit_hiking_composition.py \\\n"
                "  output/hiking-series-paired-v4.2-2026-08-06"
            ),
            cohorts=(
                Cohort(
                    cohort_id="hiking-paired-v4.2",
                    release_root=HIKING_RELEASE,
                    artwork_root=HIKING_RELEASE / "hikes",
                    expected_count=80,
                    contact_sheets=(
                        HIKING_RELEASE / "hikes/hikes-detailed-map-contact-sheet.png",
                        HIKING_RELEASE / "hikes/hikes-terrain-relief-contact-sheet.png",
                    ),
                ),
            ),
            contracts=_hiking_contracts(),
            code=(
                _item("src/city_map_plotter/hike_plates.py"),
                _item("src/city_map_plotter/niche_cli.py"),
                _item("src/city_map_plotter/niche_common.py"),
                _item("tools/build_hiking_expansion_catalog.py"),
                _item("tools/apply_hiking_factual_enrichment.py"),
                _item("tools/apply_hiking_source_precedence.py"),
                _item("tools/assemble_hiking_release_catalog.py"),
                _item("tools/audit_hiking_composition.py"),
                _item("tools/derive_hiking_cnig_terrain_context.py"),
                _item("tools/derive_hiking_global_terrain.py"),
                _item("tools/derive_hiking_ign_contours.py"),
                _item("tools/derive_hiking_osm_context.py"),
                _item("tools/derive_hiking_raster_terrain_context.py"),
                _item("tools/derive_hiking_terrain_context.py"),
                _item("tools/derive_hiking_water_context.py"),
                _item("tools/enrich_hiking_natural_earth_hydro.py"),
                _item("tools/extract_hiking_context_pbf.py"),
                _item("tools/extract_hiking_context_pbf_union.py"),
                _item("tools/qa_niche_series.py"),
            ),
            docs=(
                _item(
                    "docs/audit-2026-08-06-hiking-contours-v4.2.md",
                    "RELEASE-AUDIT-v4.2.md",
                ),
                _item("docs/hiking-qa-acceptance.md", "QA-ACCEPTANCE.md"),
                _item("docs/research/hiking-context-provenance-audit-2026-08-03.md"),
                _item("docs/research/hiking-plates-audit-2026-08-02.md"),
            ),
            regenerate_contact_sheets=True,
        ),
        Domain(
            directory="04-marathon-courses",
            title="Verified full-course marathon plates — 2026-08-16 v1",
            selection=(
                "Fourteen sourced full marathon courses: current organiser vectors "
                "where available, plus Boston's checked OSM course relation. Every "
                "route passes the distance and connected-geometry gates."
            ),
            status="Pinned-source, verified-course digital review cohort.",
            caveat=(
                "Course geometry is included and source-bound. Review-only until "
                "route redistribution/event rights, race-day change checks, "
                "external attribution placement, and physical proof are complete."
            ),
            reproduction=(
                ".venv/bin/python tools/build_verified_marathon_course_series.py \\\n"
                "  --reuse-sources review-output/"
                "marathon-course-plates-verified-2026-08-16-v1/source-contract \\\n"
                "  --output-dir review-output/marathon-course-plates-reproduction"
            ),
            cohorts=(
                Cohort(
                    cohort_id="verified-marathon-courses-2026-08-16-v1",
                    release_root=MARATHON_RELEASE,
                    artwork_root=MARATHON_RELEASE / "plates",
                    expected_count=14,
                    contact_sheets=(
                        MARATHON_RELEASE
                        / "contact-sheets/marathon-course-contact-sheet.png",
                        MARATHON_RELEASE
                        / "contact-sheets/marathon-course-portrait-contact-sheet.png",
                        MARATHON_RELEASE
                        / "contact-sheets/marathon-course-landscape-contact-sheet.png",
                    ),
                ),
            ),
            contracts=(
                CopyItem(
                    MARATHON_RELEASE / "source-contract",
                    Path("verified-marathon-course-v1"),
                ),
            ),
            code=(
                _item("tools/build_verified_marathon_course_series.py"),
                _item("src/city_map_plotter/cli.py"),
                _item("src/city_map_plotter/course.py"),
                _item("src/city_map_plotter/cartography.py"),
                _item("src/city_map_plotter/svg.py"),
            ),
            docs=(
                _item("CODEX_MAP_HANDOFF.md", "CODEX_MAP_HANDOFF.md"),
                _item("docs/reproducibility/REPRODUCING_MAPS.md"),
            ),
            regenerate_contact_sheets=True,
        ),
        Domain(
            directory="05-rowing-races",
            title="Rowing race courses — A5 and A3, 8 August 2026",
            selection=(
                "The latest complete sourced rowing-course release: Head of the "
                "River Race, Pairs Head, Henley Royal Regatta, and Head of the "
                "Charles Regatta, each rendered as A5 and A3 portrait plates."
            ),
            status="Pinned-source rowing-course digital review cohort.",
            caveat=(
                "Each course line is the measured river centre-line between "
                "organiser-described named endpoints, not a survey of the raced "
                "line. Event/source rights, race-day change checks, and physical "
                "proof remain required."
            ),
            reproduction=(
                "for preset in a5-balanced-poster a3-balanced-poster; do\n"
                "  for course in horr-london pairs-head-london henley-royal "
                "head-of-the-charles; do\n"
                "    mapplot export --rowing-course \"$course\" "
                "--course-margin 0.15 \\\n"
                "      --preset \"$preset\" --poster-layout rowing-course \\\n"
                "      --layers roads,water,railways,parks,buildings \\\n"
                "      --style styles/rowing-course-v1.json "
                "--landmark-buildings \\\n"
                "      --water-fill dots --detail-profile plotter-faithful \\\n"
                "      --simplify-mm 0.04 --road-style centreline "
                "--no-scale-bar \\\n"
                "      --optimise --split-by-pen --attribution-mode external \\\n"
                "      --external-attribution-placement "
                "\"Product page, packaging, or caption adjacent to each artwork\" \\\n"
                "      --output \"review-output/rowing-reproduction/"
                "${preset%%-*}/$course.svg\"\n"
                "  done\n"
                "done"
            ),
            cohorts=(
                Cohort(
                    cohort_id="rowing-heads-a5-2026-08-08",
                    release_root=ROWING_A5_RELEASE,
                    artwork_root=ROWING_A5_RELEASE,
                    expected_count=4,
                    artwork_prefix=Path("a5-portrait"),
                    contact_sheets=(
                        ROWING_A5_RELEASE / "series-contact-sheet.png",
                    ),
                    recursive=False,
                ),
                Cohort(
                    cohort_id="rowing-heads-a3-2026-08-08",
                    release_root=ROWING_A3_RELEASE,
                    artwork_root=ROWING_A3_RELEASE,
                    expected_count=4,
                    artwork_prefix=Path("a3-portrait"),
                    contact_sheets=(
                        ROWING_A3_RELEASE / "series-contact-sheet.png",
                    ),
                    recursive=False,
                ),
            ),
            contracts=(
                _item(
                    "src/city_map_plotter/data/rowing-courses-v1.json",
                    "rowing-courses-v1.json",
                ),
                _item("styles/rowing-course-v1.json", "rowing-course-v1.json"),
            ),
            code=(
                _item("tools/build_course_geometry.py"),
                _item("src/city_map_plotter/rowing.py"),
                _item("src/city_map_plotter/cli.py"),
                _item("src/city_map_plotter/svg.py"),
                _item("src/city_map_plotter/furniture.py"),
            ),
            docs=(
                CopyItem(ROWING_A5_RELEASE / "SERIES.md", Path("A5-SERIES.md")),
                CopyItem(ROWING_A3_RELEASE / "SERIES.md", Path("A3-SERIES.md")),
                _item("CODEX_MAP_HANDOFF.md", "CODEX_MAP_HANDOFF.md"),
                _item("docs/reproducibility/REPRODUCING_MAPS.md"),
            ),
            regenerate_contact_sheets=True,
        ),
        Domain(
            directory="06-f1-courses",
            title="Formula 1 circuit atlases — current-format v2.3",
            selection=(
                "Both promoted technical-review packages: 22 eligible current "
                "events and 19 curated former-event configurations, each in all "
                "six binding formats."
            ),
            status="Digital technical review passed; rights/physical holds remain.",
            caveat=(
                "Independent artwork, not an official F1/FIA product. Madrid is "
                "held from the current matrix; current OSM context is not an "
                "event-configuration or period reconstruction."
            ),
            reproduction=(
                ".venv/bin/python tools/build_f1_circuit_series.py \\\n"
                "  --all-renderable \\\n"
                "  --catalog src/city_map_plotter/data/f1-circuits-2026.json \\\n"
                "  --format all --dpi 254 \\\n"
                "  --output-dir review-output/"
                "f1-circuit-atlas-2026-v2.3-format-v1-2026-08-16 \\\n"
                "  --qa-profile review --generated-at 2026-08-16T00:00:00Z\n"
                ".venv/bin/python tools/build_f1_circuit_series.py \\\n"
                "  --all-renderable \\\n"
                "  --catalog src/city_map_plotter/data/"
                "f1-circuits-legacy-v1.json \\\n"
                "  --format all --dpi 254 \\\n"
                "  --output-dir review-output/"
                "f1-circuit-atlas-legacy-v2.3-format-v1-2026-08-16-r2 \\\n"
                "  --qa-profile review --generated-at 2026-08-16T00:00:00Z"
            ),
            cohorts=(
                Cohort(
                    cohort_id="f1-current-2026-v2.3",
                    release_root=F1_CURRENT_RELEASE,
                    artwork_root=F1_CURRENT_RELEASE / "plates",
                    expected_count=132,
                    artwork_prefix=Path("current-2026"),
                    contact_sheets=tuple(
                        F1_CURRENT_RELEASE / "contact-sheets" / f"{format_id}.png"
                        for format_id in FORMAT_IDS
                    ),
                ),
                Cohort(
                    cohort_id="f1-former-v2.3",
                    release_root=F1_LEGACY_RELEASE,
                    artwork_root=F1_LEGACY_RELEASE / "plates",
                    expected_count=114,
                    artwork_prefix=Path("former-events"),
                    contact_sheets=tuple(
                        F1_LEGACY_RELEASE / "contact-sheets" / f"{format_id}.png"
                        for format_id in FORMAT_IDS
                    ),
                ),
            ),
            contracts=(
                _item("contracts/f1-circuits-2026", "f1-circuits-2026"),
                _item(
                    "contracts/f1-circuits-legacy-v1",
                    "f1-circuits-legacy-v1",
                ),
            ),
            code=(
                _item("src/city_map_plotter/f1_circuits.py"),
                _item("src/city_map_plotter/f1_cli.py"),
                _item("src/city_map_plotter/f1_legacy.py"),
                _item("tools/acquire_f1_circuit_sources.py"),
                _item("tools/acquire_f1_legacy_sources.py"),
                _item("tools/build_f1_circuit_catalog.py"),
                _item("tools/build_f1_legacy_catalog.py"),
                _item("tools/build_f1_circuit_series.py"),
                _item("tools/merge_f1_source_manifests.py"),
                _item("tools/qa_f1_circuit_series.py"),
                _item("src/city_map_plotter/data/f1-circuits-2026.json"),
                _item("src/city_map_plotter/data/f1-circuits-legacy-v1.json"),
            ),
            docs=(
                _item(
                    "docs/f1-circuits/F1_CIRCUIT_ATLAS.md",
                    "F1_CIRCUIT_ATLAS.md",
                ),
                _item(
                    "docs/f1-circuits/SOURCE_AUDIT_2026-08-10.md",
                    "SOURCE_AUDIT_2026-08-10.md",
                ),
                _item(
                    "docs/audit-2026-08-09-f1-circuit-atlas-v1.md",
                    "HISTORICAL-v1-AUDIT.md",
                ),
            ),
            regenerate_contact_sheets=True,
        ),
        Domain(
            directory="07-golf-courses",
            title="Twenty-Five Icons of Golf — v4",
            selection=(
                "All 25 curated courses that pass the exact 18-hole source gate, "
                "rendered with golf-clarity-course-a3-v4."
            ),
            status="Digital QA passed; review-only nominal unmeasured pens.",
            caveat=(
                "Not an objective ranking or official course product. Commercial "
                "rights/non-endorsement review and physical calibration remain."
            ),
            reproduction=(
                "mapplot-golf build --all \\\n"
                "  --output-dir output/golf-course-series-v4 --dpi 180\n"
                "PYTHONPATH=src .venv/bin/python tools/qa_golf_series.py \\\n"
                "  output/golf-course-series-v4"
            ),
            cohorts=(
                Cohort(
                    cohort_id="golf-course-series-v4",
                    release_root=GOLF_RELEASE,
                    artwork_root=GOLF_RELEASE,
                    expected_count=25,
                    contact_sheets=(GOLF_RELEASE / "golf-course-contact-sheet.png",),
                ),
            ),
            contracts=(_item("contracts/golf-courses-v2", "golf-courses-v2"),),
            code=(
                _item("src/city_map_plotter/golf.py"),
                _item("src/city_map_plotter/golf_cli.py"),
                _item("tools/acquire_golf_geometry.py"),
                _item("tools/build_golf_catalog.py"),
                _item("tools/qa_golf_series.py"),
                _item("src/city_map_plotter/data/golf-courses-v1.json"),
            ),
            docs=(_item("docs/golf/GOLF.md", "GOLF.md"),),
            regenerate_contact_sheets=True,
        ),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file(path: Path) -> None:
    if path.is_symlink():
        raise PortfolioError(f"Refusing symbolic-link source: {path}")
    if not path.is_file():
        raise PortfolioError(f"Required file is missing: {path}")


def _require_directory(path: Path) -> None:
    if path.is_symlink():
        raise PortfolioError(f"Refusing symbolic-link source directory: {path}")
    if not path.is_dir():
        raise PortfolioError(f"Required directory is missing: {path}")


def _copy_file(source: Path, destination: Path) -> str:
    _require_regular_file(source)
    if destination.exists():
        raise PortfolioError(f"Portfolio path collision: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_hash = _sha256(source)
    shutil.copy2(source, destination)
    destination_hash = _sha256(destination)
    if destination_hash != source_hash:
        raise PortfolioError(f"Copy verification failed for {source} -> {destination}.")
    return source_hash


def _copy_tree(source: Path, destination: Path) -> None:
    _require_directory(source)
    if destination.exists():
        raise PortfolioError(f"Portfolio path collision: {destination}")
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise PortfolioError(f"Refusing symbolic-link source: {path}")
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            _copy_file(path, target)
        else:
            raise PortfolioError(f"Refusing non-file source: {path}")
    destination.mkdir(parents=True, exist_ok=True)


def _copy_item(item: CopyItem, destination_root: Path) -> None:
    destination = destination_root / item.destination
    if item.source.is_symlink():
        raise PortfolioError(f"Refusing symbolic-link source: {item.source}")
    if item.source.is_file():
        _copy_file(item.source, destination)
    elif item.source.is_dir():
        _copy_tree(item.source, destination)
    else:
        raise PortfolioError(f"Required source is missing: {item.source}")


def _is_pen_job(path: Path) -> bool:
    return ".pen-" in path.name


def _is_contact_sheet(path: Path) -> bool:
    lowered = path.name.lower()
    return "contact-sheet" in lowered or "contact_sheet" in lowered


def _triplets(cohort: Cohort) -> list[tuple[Path, Path, Path]]:
    _require_directory(cohort.release_root)
    _require_directory(cohort.artwork_root)
    svg_candidates = (
        cohort.artwork_root.rglob("*.svg")
        if cohort.recursive
        else cohort.artwork_root.glob("*.svg")
    )
    svg_files = sorted(path for path in svg_candidates if not _is_pen_job(path))
    if len(svg_files) != cohort.expected_count:
        raise PortfolioError(
            f"{cohort.cohort_id}: expected {cohort.expected_count} master SVGs, "
            f"found {len(svg_files)} in {cohort.artwork_root}."
        )

    triplets: list[tuple[Path, Path, Path]] = []
    for svg_path in svg_files:
        png_path = svg_path.with_suffix(".png")
        manifest_path = svg_path.with_suffix(".plot.json")
        _require_regular_file(png_path)
        _require_regular_file(manifest_path)
        triplets.append((svg_path, png_path, manifest_path))

    expected_pngs = {png for _, png, _ in triplets}
    png_candidates = (
        cohort.artwork_root.rglob("*.png")
        if cohort.recursive
        else cohort.artwork_root.glob("*.png")
    )
    actual_pngs = {path for path in png_candidates if not _is_contact_sheet(path)}
    if actual_pngs != expected_pngs:
        missing = sorted(str(path) for path in expected_pngs - actual_pngs)
        extra = sorted(str(path) for path in actual_pngs - expected_pngs)
        raise PortfolioError(
            f"{cohort.cohort_id}: PNG inventory mismatch; "
            f"missing={missing[:3]}, extra={extra[:3]}."
        )

    expected_manifests = {manifest for _, _, manifest in triplets}
    manifest_candidates = (
        cohort.artwork_root.rglob("*.plot.json")
        if cohort.recursive
        else cohort.artwork_root.glob("*.plot.json")
    )
    actual_manifests = set(manifest_candidates)
    if actual_manifests != expected_manifests:
        missing = sorted(str(path) for path in expected_manifests - actual_manifests)
        extra = sorted(str(path) for path in actual_manifests - expected_manifests)
        raise PortfolioError(
            f"{cohort.cohort_id}: manifest inventory mismatch; "
            f"missing={missing[:3]}, extra={extra[:3]}."
        )
    return triplets


_JSON_STRING_FIELDS = {
    field: re.compile(rb'"' + field.encode("ascii") + rb'"\s*:\s*("(?:[^"\\]|\\.)*")')
    for field in ("title", "subtitle", "subject_id", "format_id", "generated_at")
}


def _manifest_summary(path: Path) -> dict[str, str]:
    with path.open("rb") as handle:
        head = handle.read(512 * 1024)
    summary: dict[str, str] = {}
    for field, pattern in _JSON_STRING_FIELDS.items():
        match = pattern.search(head)
        if match is None:
            continue
        try:
            value = json.loads(match.group(1).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, str):
            summary[field] = value
    return summary


def _copy_artwork(
    domain: Domain,
    domain_root: Path,
    cohort: Cohort,
    triplets: Sequence[tuple[Path, Path, Path]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    destination_root = domain_root / "artwork" / cohort.artwork_prefix
    for svg_path, png_path, manifest_path in triplets:
        relative_svg = svg_path.relative_to(cohort.artwork_root)
        relative_png = png_path.relative_to(cohort.artwork_root)
        relative_manifest = manifest_path.relative_to(cohort.artwork_root)
        destination_svg = destination_root / relative_svg
        destination_png = destination_root / relative_png
        destination_manifest = destination_root / relative_manifest
        _copy_file(svg_path, destination_svg)
        _copy_file(png_path, destination_png)
        _copy_file(manifest_path, destination_manifest)
        transform = externalize_triplet(
            destination_svg,
            destination_png,
            destination_manifest,
            external_placement="Portfolio root ATTRIBUTION.md",
        )
        svg_hash = _sha256(destination_svg)
        png_hash = _sha256(destination_png)
        manifest_hash = _sha256(destination_manifest)
        summary = _manifest_summary(destination_manifest)
        record: dict[str, Any] = {
                "domain": domain.directory,
                "cohort": cohort.cohort_id,
                "source_release": (
                    cohort.release_root.relative_to(ROOT).as_posix()
                    if cohort.release_root.is_relative_to(ROOT)
                    else cohort.release_root.name
                ),
                "source_artifact": svg_path.relative_to(
                    cohort.release_root
                ).as_posix(),
                "artifact_id": svg_path.stem,
                "subject_id": summary.get("subject_id", ""),
                "title": summary.get("title", svg_path.stem),
                "subtitle": summary.get("subtitle", ""),
                "format_id": (
                    summary.get("format_id")
                    or _format_from_path(svg_path)
                    or _format_from_svg_page(svg_path)
                ),
                "source_generated_at": summary.get("generated_at", ""),
                "svg": _file_record(
                    destination_svg,
                    svg_hash,
                    Path(domain.directory) / destination_svg.relative_to(domain_root),
                ),
                "png": _file_record(
                    destination_png,
                    png_hash,
                    Path(domain.directory) / destination_png.relative_to(domain_root),
                ),
                "manifest": _file_record(
                    destination_manifest,
                    manifest_hash,
                    Path(domain.directory)
                    / destination_manifest.relative_to(domain_root),
                ),
            }
        if transform is not None:
            record["presentation_transform"] = {
                "id": TRANSFORM_ID,
                "removed_visible_path_count": transform["removed_path_count"],
                "removed_visible_copy_values": transform["removed_copy_values"],
                "external_attribution_placement": "Portfolio root ATTRIBUTION.md",
            }
        records.append(record)
    return records


def _format_from_path(path: Path) -> str:
    for format_id in FORMAT_IDS:
        if format_id in path.parts or path.stem.endswith(format_id):
            return format_id
    return ""


def _format_from_svg_page(path: Path) -> str:
    with path.open("rb") as handle:
        head = handle.read(4096)
    width_match = re.search(rb'\bwidth="([0-9.]+)mm"', head)
    height_match = re.search(rb'\bheight="([0-9.]+)mm"', head)
    if width_match is None or height_match is None:
        return ""
    dimensions = (float(width_match.group(1)), float(height_match.group(1)))
    formats = {
        (148.0, 210.0): "a5-portrait",
        (210.0, 148.0): "a5-landscape",
        (210.0, 297.0): "a4-portrait",
        (297.0, 210.0): "a4-landscape",
        (297.0, 420.0): "a3-portrait",
        (420.0, 297.0): "a3-landscape",
    }
    return formats.get(dimensions, "")


def _file_record(path: Path, digest: str, published_path: Path) -> dict[str, Any]:
    return {
        "path": published_path.as_posix(),
        "sha256": digest,
        "bytes": path.stat().st_size,
    }


def _copy_release_metadata(cohort: Cohort, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(cohort.release_root.iterdir()):
        if path.is_symlink():
            raise PortfolioError(f"Refusing symbolic-link release member: {path}")
        if path.is_file():
            if path.suffix.lower() in {".md", ".json", ".txt", ".html", ".sha256"}:
                if path.name.endswith(".plot.json"):
                    continue
                _copy_file(path, destination / path.name)
        elif path.is_dir() and path.name in cohort.metadata_directories:
            _copy_tree(path, destination / path.name)


def _copy_contact_sheets(cohort: Cohort, destination: Path) -> int:
    count = 0
    for source in cohort.contact_sheets:
        _require_regular_file(source)
        target = destination / cohort.cohort_id / source.name
        _copy_file(source, target)
        svg_companion = source.with_suffix(".svg")
        if svg_companion.is_file() and not svg_companion.is_symlink():
            _copy_file(svg_companion, target.with_suffix(".svg"))
        count += 1
    return count


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _domain_readme(
    domain: Domain, artifact_count: int, contact_sheet_count: int
) -> str:
    return f"""# {domain.title}

## What is here

{domain.selection}

- Distinct examples: {artifact_count}
- Master artwork: {artifact_count} SVG files and {artifact_count} PNG previews
- Plot manifests: {artifact_count}
- Contact sheets: {contact_sheet_count}
- Status: {domain.status}

**Required caveat:** {domain.caveat}

`artwork/` contains only master examples. The source release's `.pen-NN-*.svg`
files are physical machine jobs for those same designs, not additional
examples, so they are intentionally not duplicated here. Rebuild them with the
included generator when preparing a calibrated plot.

`contracts/` contains the frozen or best-available source evidence. `code/`
contains the domain entry points. Shared renderer code and the binding plate
contract live at `../shared/`. `release-metadata/` preserves the source
package's reports, indexes, licences, and original checksums.

## Reproduce

Run from the repository root after reading `AGENTS.md`, `CODEX_MAP_HANDOFF.md`,
and `docs/reproducibility/REPRODUCING_MAPS.md`:

```bash
{domain.reproduction}
```

Then validate the master SVGs and simulate the plot order:

```bash
find <release-directory> -name '*.svg' ! -name '*.pen-*' -print0 | \\
  xargs -0 .venv/bin/python tools/validate_format.py
python3 tools/plotsim.py <one-master.svg> --compare
```

The bundle's `CHECKSUMS.sha256`, `catalog.json`, and `BUILD-VALIDATION.json`
record the handoff copy itself.

Portfolio review copies print no OpenStreetMap/OSM wording on the plate.
Required map-data credit is retained in the portfolio-root `ATTRIBUTION.md`,
source metadata, and source contracts. Contact sheets are regenerated from the
portfolio PNGs and supplied as both PNG and SVG.
"""


def _domain_handoff(domain: Domain) -> str:
    return f"""# LLM handoff — {domain.title}

Treat this folder as a review/evidence package, never as permission to publish
or sell the artwork. The binding paper rules are in
`../shared/plate-contract/format-v1.json`; do not hand-edit that generated JSON.

Current selection: {domain.selection}

Status: {domain.status}

Non-negotiable disclosure: {domain.caveat}

When changing a plate, return to the included request/catalog and source
contract, rebuild with the included entry points, validate every master SVG,
regenerate contact sheets, inspect PlotSim, and issue a new versioned cohort.
Never repair factual geometry by sketching, tracing a raster, or silently
substituting a fresh live source for a pinned snapshot.
"""


def _render_contact_sheet(
    destination: Path, pngs: Sequence[Path], *, columns: int
) -> tuple[Path, Path]:
    if not pngs:
        raise PortfolioError("Cannot generate a contact sheet without PNGs.")
    command = [
        sys.executable,
        str(ROOT / "tools/build_contact_sheet.py"),
        *map(str, pngs),
        "--out",
        str(destination),
        "--columns",
        str(columns),
        "--keep-svg",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise PortfolioError(
            "Generated domain contact sheet failed:\n"
            + result.stdout
            + result.stderr
        )
    _require_regular_file(destination)
    svg = destination.with_suffix(".svg")
    _require_regular_file(svg)
    return destination, svg


def _generate_contact_sheet(domain_root: Path, pngs: Sequence[Path]) -> Path:
    destination = domain_root / "contact-sheets/all-examples-contact-sheet.png"
    _render_contact_sheet(destination, pngs, columns=5)
    return destination


def _regenerate_externalized_contact_sheets(
    staging: Path,
    domain: Domain,
    domain_root: Path,
    records: Sequence[dict[str, Any]],
) -> int:
    """Rebuild sheets from cleaned PNGs so source-sheet lettering cannot leak in."""

    groups: list[tuple[str, list[dict[str, Any]], int]] = []
    if domain.directory in {"01-university-cities-uk", "02-university-cities-us"}:
        cohort_id = domain.cohorts[0].cohort_id
        groups.append(
            (f"{cohort_id}/ranked-universities-contact-sheet.png", list(records), 5)
        )
    elif domain.directory == "03-hiking-maps":
        for variant in ("detailed-map", "terrain-relief"):
            selected = [
                record
                for record in records
                if record["artifact_id"].endswith(f"--{variant}")
            ]
            groups.append(
                (
                    f"hiking-paired-v4.2/hikes-{variant}-contact-sheet.png",
                    selected,
                    5,
                )
            )
    elif domain.directory == "04-marathon-courses":
        groups.append(
            (
                "verified-marathon-courses-2026-08-16-v1/"
                "marathon-course-contact-sheet.png",
                list(records),
                4,
            )
        )
        for orientation in ("portrait", "landscape"):
            selected = [
                record
                for record in records
                if record["format_id"].endswith(orientation)
            ]
            groups.append(
                (
                    "verified-marathon-courses-2026-08-16-v1/"
                    f"marathon-course-{orientation}-contact-sheet.png",
                    selected,
                    3 if len(selected) < 8 else 4,
                )
            )
    elif domain.directory == "05-rowing-races":
        for cohort in domain.cohorts:
            selected = [
                record
                for record in records
                if record["cohort"] == cohort.cohort_id
            ]
            groups.append(
                (f"{cohort.cohort_id}/series-contact-sheet.png", selected, 2)
            )
    elif domain.directory == "06-f1-courses":
        for cohort in domain.cohorts:
            for format_id in FORMAT_IDS:
                selected = [
                    record
                    for record in records
                    if record["cohort"] == cohort.cohort_id
                    and record["format_id"] == format_id
                ]
                groups.append(
                    (f"{cohort.cohort_id}/{format_id}.png", selected, 5)
                )
    elif domain.directory == "07-golf-courses":
        groups.append(
            (
                "golf-course-series-v4/golf-course-contact-sheet.png",
                list(records),
                5,
            )
        )
    else:
        raise PortfolioError(
            f"No regenerated contact-sheet grouping for {domain.directory}."
        )

    for relative, selected, columns in groups:
        if not selected:
            raise PortfolioError(
                f"No portfolio records selected for contact sheet {relative}."
            )
        pngs = [staging / record["png"]["path"] for record in selected]
        _render_contact_sheet(
            domain_root / "contact-sheets" / relative,
            pngs,
            columns=columns,
        )
    return len(groups)


def _copy_shared(staging: Path) -> None:
    shared = staging / "shared"
    handoff_items = (
        _item("AGENTS.md", "AGENTS.md"),
        _item("CODEX_MAP_HANDOFF.md", "CODEX_MAP_HANDOFF.md"),
        _item(
            "docs/THIRD_PARTY_NOTICES.md",
            "THIRD_PARTY_NOTICES.md",
        ),
        _item(
            "docs/reproducibility/REPRODUCING_MAPS.md",
            "REPRODUCING_MAPS.md",
        ),
    )
    for item in handoff_items:
        _copy_item(item, shared / "handoff")

    plate_contract_items = (
        _item("docs/format/FORMAT.md", "FORMAT.md"),
        _item("docs/format/format-v1.json", "format-v1.json"),
        _item("tools/build_format_spec.py", "build_format_spec.py"),
        _item("tools/validate_format.py", "validate_format.py"),
    )
    for item in plate_contract_items:
        _copy_item(item, shared / "plate-contract")

    source_destination = shared / "code/src/city_map_plotter"
    source_destination.mkdir(parents=True, exist_ok=True)
    for path in sorted((ROOT / "src/city_map_plotter").glob("*.py")):
        _copy_file(path, source_destination / path.name)
    for item in (
        _item("pyproject.toml", "pyproject.toml"),
        _item("README.md", "README.md"),
        _item("requirements", "requirements"),
        _item("tests/fixtures", "tests/fixtures"),
        _item("tests/test_batch.py", "tests/test_batch.py"),
        _item(
            "tests/test_build_latest_map_portfolio.py",
            "tests/test_build_latest_map_portfolio.py",
        ),
        _item(
            "tests/test_externalize_portfolio_map_attribution.py",
            "tests/test_externalize_portfolio_map_attribution.py",
        ),
        _item(
            "tests/test_build_verified_marathon_course_series.py",
            "tests/test_build_verified_marathon_course_series.py",
        ),
        _item(
            "tests/test_finalize_ranked_university_series.py",
            "tests/test_finalize_ranked_university_series.py",
        ),
        _item("tests/test_format_conformance.py", "tests/test_format_conformance.py"),
        _item("tests/test_build_f1_circuit_series.py", "tests/test_build_f1_circuit_series.py"),
        _item("tests/test_f1_circuit_series_qa.py", "tests/test_f1_circuit_series_qa.py"),
        _item("tests/test_golf_cli.py", "tests/test_golf_cli.py"),
        _item("tests/test_hiking_series_qa.py", "tests/test_hiking_series_qa.py"),
        _item("tests/test_rowing.py", "tests/test_rowing.py"),
        _item(
            "tests/test_qa_ranked_university_series.py",
            "tests/test_qa_ranked_university_series.py",
        ),
        _item(
            "tests/test_ranked_university_series_builder.py",
            "tests/test_ranked_university_series_builder.py",
        ),
        _item("tools/plotsim.py", "tools/plotsim.py"),
        _item("tools/build_plotsim_viewer.py", "tools/build_plotsim_viewer.py"),
        _item("tools/build_contact_sheet.py", "tools/build_contact_sheet.py"),
        _item(
            "tools/externalize_portfolio_map_attribution.py",
            "tools/externalize_portfolio_map_attribution.py",
        ),
        _item(
            "tools/build_latest_map_portfolio.py",
            "tools/build_latest_map_portfolio.py",
        ),
    ):
        _copy_item(item, shared / "code")


def _write_top_level(
    staging: Path,
    domains: Sequence[Domain],
    records: Sequence[dict[str, Any]],
    contact_sheet_count: int,
) -> None:
    counts = {
        domain.directory: sum(
            record["domain"] == domain.directory for record in records
        )
        for domain in domains
    }
    total = len(records)
    table_rows = "\n".join(
        f"| [{domain.title}]({domain.directory}/README.md) | "
        f"{counts[domain.directory]} | {domain.status} |"
        for domain in domains
    )
    readme = f"""# Latest map portfolio — 18 August 2026

This review-only handoff collects the newest authoritative or best-available
local examples across the seven requested map families. It contains **{total}
distinct plates**, each as editable SVG, PNG preview, and plot manifest, plus
**{contact_sheet_count} contact sheets**. The complete file inventory is bound
by `CHECKSUMS.sha256` and the artwork index is `catalog.json`.

| Family | Plates | Status |
|---|---:|---|
{table_rows}

Open `index.html` for the complete visual gallery. Each family folder has the
same predictable layout: `artwork/`, `contact-sheets/`, `contracts/`, `code/`,
`docs/`, `release-metadata/`, `README.md`, and `LLM_HANDOFF.md`.

## Rebuild this handoff index

After rebuilding the seven source releases with the commands in their family
READMEs, run from the repository root:

```bash
.venv/bin/python tools/build_latest_map_portfolio.py --check-only
.venv/bin/python tools/build_latest_map_portfolio.py \\
  --output review-output/latest-map-portfolio-2026-08-18-v3
```

The compiler stages atomically, refuses to overwrite an existing destination,
requires exact SVG/PNG/manifest pairing, validates every copied master against
the binding format, and writes a complete checksum inventory.

## Important boundaries

- The university v2.1.4 cohort is rebuilt from its frozen 50-subject source and
  renderer contracts, split here into UK 30 and US 20.
- Hiking v4.2, F1 v2.3, and golf v4 passed their documented digital gates but
  remain review-only pending the stated rights and physical-proof work.
- The marathon folder contains 14 verified full-course plates using the exact
  established `output/marathon-series` visual recipe. Organiser vectors, the
  normalized geometry, course checks, and matching basemap snapshots are pinned.
- The rowing folder contains all four latest sourced race courses in both A5
  and A3. Each line is the measured river centre-line between named endpoints;
  the included contract records the organiser descriptions and source geometry.
- F1 includes both promoted v2.3 packages: 132 current-calendar plates and 114
  former-event plates. Madrid remains a declared hold, not an approximated map.
- `.pen-NN-*.svg` jobs are deliberately excluded because they duplicate the
  master design once per physical pen. The included code regenerates them.
- No portfolio plate or regenerated contact sheet prints OpenStreetMap/OSM
  wording. The legally required map-data credit is externalized to
  `ATTRIBUTION.md`; source/licence metadata and evidence remain intact.

## Safe rebuild order

1. Read `shared/handoff/AGENTS.md`, `CODEX_MAP_HANDOFF.md`, and
   `REPRODUCING_MAPS.md`.
2. Treat `shared/plate-contract/format-v1.json` as binding; edit its builder,
   never the JSON, if the global format contract changes.
3. Rebuild a source cohort to a new output directory using its family README.
4. Run `tools/validate_format.py` on every master SVG and inspect PlotSim plus
   the full contact sheets.
5. Build a new portfolio path; this compiler refuses to overwrite an existing
   package.

Generated output remains ignored by Git and must not be committed as finished
artwork.
"""
    _write_text(staging / "README.md", readme)
    handoff = """# LLM handoff — latest map portfolio

Start with `README.md` and `catalog.json`. Every artwork record has relative
SVG, PNG, and manifest paths plus SHA-256 digests. Family-specific claims and
rebuild commands are in each numbered folder's README and LLM handoff.

Do not erase or weaken the status labels. In particular: marathon course
geometry is source-bound and verified; rowing lines are sourced river
centre-lines rather than surveyed raced lines; F1 Madrid is held; all families
remain subject to the documented rights/physical gates.
Never invent, trace, or visually repair sourced course or map geometry.

No OpenStreetMap/OSM credit is drawn on the portfolio pages. Keep the external
credit in `ATTRIBUTION.md` and never remove the embedded source/licence metadata.

This package intentionally excludes historical superseded cohorts and
pen-separated machine jobs. `SOURCE-SELECTION.md` explains every choice. The
compiler that reproduced this exact structure is copied under
`shared/code/tools/build_latest_map_portfolio.py`.
"""
    _write_text(staging / "LLM_HANDOFF.md", handoff)
    selection = """# Source selection ledger

The word “newest” means the latest documented release/generator state in this
workspace on 2026-08-18, not the directory with the newest filesystem mtime.

- Universities: v2.1.4 replaces v2.1.3 and earlier pilots; exact frozen recipe.
- Hiking: paired v4.2 supersedes paired v4.1 and earlier contour releases.
- Marathon: the 14 preview-only city basemaps are superseded here by the
  verified full-course v1 release. It uses the established
  `output/marathon-series` sheet design and full-route framing. Every route is
  bound to retained organiser/vector evidence, normalized geometry, an accepted
  distance/topology check, and a pinned matching basemap.
- Rowing: the complete 8 August 2026 A5 and A3 course releases are included:
  four sourced races at each size. Crew-personalisation studies are excluded
  because this family is the race-course portfolio.
- F1: current and former-event v2.3 contracts were both regenerated under the
  same current renderer/QA revision; they supersede v2.2 and the v1 baseline.
- Golf: v4 supersedes v1/v2 and uses the golf-clarity-course-a3-v4 contract.
"""
    _write_text(staging / "SOURCE-SELECTION.md", selection)
    _write_text(
        staging / "ATTRIBUTION.md",
        """# External map-data attribution

Map data © OpenStreetMap contributors, licensed under the Open Data Commons
Open Database License (ODbL) 1.0.

- https://www.openstreetmap.org/copyright
- https://opendatacommons.org/licenses/odbl/1-0/

This credit is deliberately placed in the portfolio companion documentation
instead of being drawn on each review plate. The presentation transform removes
only explicit OpenStreetMap/OSM lettering paths. It does not remove source URLs,
licence records, publisher names, hashes, or other provenance from embedded SVG
metadata, plot manifests, source contracts, and family documentation.

Other data providers and organiser/course sources remain credited in the
family-specific `docs/`, `contracts/`, `release-metadata/`, and manifests.
`ON-PAGE-ATTRIBUTION-AUDIT.json` records the page-level audit and derivative
chain for every SVG, PNG, and regenerated contact sheet.
""",
    )
    catalog = {
        "schema_version": 1,
        "generated_at": GENERATED_AT,
        "release_status": "review-only-handoff",
        "artifact_count": total,
        "svg_count": total,
        "png_count": total,
        "plot_manifest_count": total,
        "contact_sheet_count": contact_sheet_count,
        "counts_by_domain": counts,
        "artifacts": list(records),
    }
    _write_json(staging / "catalog.json", catalog)
    _write_gallery(staging / "index.html", domains, records)


def _write_gallery(
    destination: Path,
    domains: Sequence[Domain],
    records: Sequence[dict[str, Any]],
) -> None:
    domain_titles = {domain.directory: domain.title for domain in domains}
    sections: list[str] = []
    for domain in domains:
        cards: list[str] = []
        for record in records:
            if record["domain"] != domain.directory:
                continue
            png = record["png"]["path"]
            svg = record["svg"]["path"]
            title = html.escape(record["title"])
            subtitle = html.escape(record["subtitle"])
            cohort = html.escape(record["cohort"])
            cards.append(
                "<figure>"
                f'<a href="{html.escape(svg)}"><img loading="lazy" '
                f'src="{html.escape(png)}" alt="{title}"></a>'
                f"<figcaption><strong>{title}</strong><br>{subtitle}<br>"
                f"<small>{cohort}</small></figcaption></figure>"
            )
        sections.append(
            f'<section id="{html.escape(domain.directory)}"><h2>'
            f"{html.escape(domain_titles[domain.directory])}</h2>"
            '<div class="grid">' + "\n".join(cards) + "</div></section>"
        )
    document = (
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Latest map portfolio — 2026-08-18</title>
<style>
:root{color-scheme:light;background:#ecebe7;color:#171918;font:15px/1.45 system-ui,sans-serif}
body{margin:0}header{padding:2rem clamp(1rem,4vw,4rem);background:#11221d;color:#f4f0e7}
main{padding:1rem clamp(1rem,4vw,4rem) 4rem}h1,h2{font-weight:500;letter-spacing:.01em}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:1rem}
figure{margin:0;padding:.75rem;background:#fff;box-shadow:0 2px 12px #0002}
img{width:100%;height:270px;object-fit:contain;background:#fafafa;display:block}
figcaption{padding-top:.65rem}small{color:#58625e}a{color:inherit}section{scroll-margin-top:1rem}
</style></head><body><header><h1>Latest map portfolio</h1>
<p>423 review plates · SVG + PNG + manifest · 18 August 2026</p></header><main>
"""
        + "\n".join(sections)
        + "\n</main></body></html>\n"
    )
    _write_text(destination, document)


def _write_checksums(root: Path) -> tuple[int, str]:
    checksum_path = root / "CHECKSUMS.sha256"
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        if path == checksum_path or not path.is_file():
            continue
        if path.is_symlink():
            raise PortfolioError(f"Refusing symbolic link in portfolio: {path}")
        lines.append(f"{_sha256(path)}  {path.relative_to(root).as_posix()}")
    _write_text(checksum_path, "\n".join(lines))
    return len(lines), _sha256(checksum_path)


def _validate_copied_svgs(
    staging: Path, records: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    svg_paths = [staging / record["svg"]["path"] for record in records]
    if len(svg_paths) != 423 or len(set(svg_paths)) != 423:
        raise PortfolioError("Format-validation inventory is not 423 unique SVGs.")
    command = [
        sys.executable,
        str(ROOT / "tools/validate_format.py"),
        "--quiet",
        *map(str, svg_paths),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        details = (result.stdout + result.stderr).strip()
        raise PortfolioError(
            "Binding format validation failed for the copied portfolio"
            + (f":\n{details}" if details else ".")
        )
    report = {
        "schema_version": 1,
        "validator": "tools/validate_format.py",
        "format_contract": "docs/format/format-v1.json",
        "status": "passed",
        "svg_count": len(svg_paths),
        "exit_status": result.returncode,
        "warnings_as_errors": False,
    }
    _write_json(staging / "FORMAT-VALIDATION.json", report)
    return report


def _audit_on_page_attribution(
    staging: Path, records: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    artwork_svgs = [staging / record["svg"]["path"] for record in records]
    contact_svgs = sorted(staging.glob("*/contact-sheets/**/*.svg"))
    report = audit_svgs([*artwork_svgs, *contact_svgs])
    transformed = [record for record in records if "presentation_transform" in record]
    report.update(
        {
            "artwork_svg_count": len(artwork_svgs),
            "contact_sheet_svg_count": len(contact_svgs),
            "transformed_artifact_count": len(transformed),
            "removed_visible_path_count": sum(
                int(record["presentation_transform"]["removed_visible_path_count"])
                for record in transformed
            ),
            "png_derivation": (
                "Every artwork PNG was copied with its SVG; transformed PNGs were "
                "rerasterised from the audited SVG at the original pixel dimensions. "
                "Every contact sheet was regenerated from those portfolio PNGs."
            ),
            "external_attribution_placement": "ATTRIBUTION.md",
        }
    )
    if report["status"] != "passed":
        first = report["failures"][:3]
        raise PortfolioError(
            "Visible OpenStreetMap/OSM copy remains on portfolio pages: "
            + json.dumps(first, ensure_ascii=False)
        )
    _write_json(staging / "ON-PAGE-ATTRIBUTION-AUDIT.json", report)
    return report


def _validate_rowing_course_triplets(
    cohort: Cohort,
    triplets: Sequence[tuple[Path, Path, Path]],
) -> None:
    expected_paper = "A5" if cohort.artwork_prefix == Path("a5-portrait") else "A3"
    seen: set[str] = set()
    for svg_path, _png_path, manifest_path in triplets:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PortfolioError(
                f"Invalid rowing manifest {manifest_path}: {exc}"
            ) from exc
        race_course = manifest.get("race_course")
        page = manifest.get("page")
        rendering = manifest.get("rendering")
        if not isinstance(race_course, dict):
            raise PortfolioError(f"Rowing plate has no race_course: {manifest_path}")
        if not isinstance(page, dict) or not isinstance(rendering, dict):
            raise PortfolioError(
                f"Rowing plate lacks page/rendering evidence: {manifest_path}"
            )
        course_id = race_course.get("course_id")
        if course_id != svg_path.stem or course_id not in ROWING_COURSE_IDS:
            raise PortfolioError(
                f"Unexpected rowing course identity in {manifest_path}: {course_id!r}"
            )
        official_m = race_course.get("official_distance_m")
        measured_m = race_course.get("measured_centreline_m")
        source_urls = race_course.get("source_urls")
        waypoint_count = race_course.get("waypoint_count")
        drawn_parts = race_course.get("drawn_parts")
        drawn_paths = race_course.get("drawn_paths")
        if (
            not isinstance(official_m, (int, float))
            or official_m <= 0
            or not isinstance(measured_m, (int, float))
            or measured_m <= 0
            or abs(measured_m - official_m) / official_m > 0.12
            or not isinstance(source_urls, list)
            or not source_urls
            or not isinstance(waypoint_count, int)
            or waypoint_count < 2
            or not isinstance(drawn_parts, int)
            or drawn_parts < 1
            or not isinstance(drawn_paths, int)
            or drawn_paths < 1
        ):
            raise PortfolioError(
                f"Rowing plate has incomplete or unverified course geometry: "
                f"{manifest_path}"
            )
        if page.get("paper") != expected_paper or page.get("orientation") != "portrait":
            raise PortfolioError(
                f"Rowing plate has the wrong paper format: {manifest_path}"
            )
        if rendering.get("attribution_mode") != "external":
            raise PortfolioError(
                f"Rowing plate does not declare external attribution: {manifest_path}"
            )
        seen.add(course_id)
    if seen != ROWING_COURSE_IDS:
        raise PortfolioError(
            f"{cohort.cohort_id}: rowing course set mismatch; found {sorted(seen)}."
        )


def _validate_sources(domains: Sequence[Domain]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for domain in domains:
        domain_count = 0
        for cohort in domain.cohorts:
            triplets = _triplets(cohort)
            if domain.directory == "05-rowing-races":
                _validate_rowing_course_triplets(cohort, triplets)
            domain_count += len(triplets)
            for sheet in cohort.contact_sheets:
                _require_regular_file(sheet)
        for item in (*domain.contracts, *domain.code, *domain.docs):
            if item.source.is_symlink() or not item.source.exists():
                raise PortfolioError(
                    f"Required handoff source is missing: {item.source}"
                )
        counts[domain.directory] = domain_count
    total = sum(counts.values())
    if total != 423:
        raise PortfolioError(f"Expected 423 distinct plates, found {total}.")
    return {"artifact_count": total, "counts_by_domain": counts}


def _assemble(output: Path, domains: Sequence[Domain]) -> None:
    if output.exists() or output.is_symlink():
        raise PortfolioError(
            f"Destination already exists; refusing overwrite: {output}"
        )
    output_parent = output.parent.resolve()
    output_parent.mkdir(parents=True, exist_ok=True)
    if output_parent == ROOT.resolve() or output.resolve() == ROOT.resolve():
        raise PortfolioError("Portfolio output must be a dedicated child directory.")

    source_audit = _validate_sources(domains)
    staging_path = Path(
        tempfile.mkdtemp(prefix=".latest-map-portfolio-", dir=output_parent)
    )
    records: list[dict[str, Any]] = []
    contact_sheet_count = 0
    try:
        _copy_shared(staging_path)
        for domain in domains:
            print(f"assembling {domain.directory}: {domain.title}", flush=True)
            domain_root = staging_path / domain.directory
            domain_records: list[dict[str, Any]] = []
            domain_contact_count = 0
            for cohort in domain.cohorts:
                triplets = _triplets(cohort)
                domain_records.extend(
                    _copy_artwork(domain, domain_root, cohort, triplets)
                )
                _copy_release_metadata(
                    cohort, domain_root / "release-metadata" / cohort.cohort_id
                )
                if not domain.regenerate_contact_sheets:
                    copied_sheets = _copy_contact_sheets(
                        cohort, domain_root / "contact-sheets"
                    )
                    domain_contact_count += copied_sheets

            for item in domain.contracts:
                _copy_item(item, domain_root / "contracts")
            for item in domain.code:
                _copy_item(item, domain_root / "code")
            for item in domain.docs:
                _copy_item(item, domain_root / "docs")

            if domain.generate_contact_sheet:
                # Catalog paths are staging-relative, so they remain valid after
                # the staging directory is atomically promoted.
                pngs = [
                    staging_path / record["png"]["path"] for record in domain_records
                ]
                _generate_contact_sheet(domain_root, pngs)
                domain_contact_count += 1
            if domain.regenerate_contact_sheets:
                domain_contact_count += _regenerate_externalized_contact_sheets(
                    staging_path, domain, domain_root, domain_records
                )

            _write_text(
                domain_root / "README.md",
                _domain_readme(domain, len(domain_records), domain_contact_count),
            )
            _write_text(domain_root / "LLM_HANDOFF.md", _domain_handoff(domain))
            records.extend(domain_records)
            contact_sheet_count += domain_contact_count

        records.sort(
            key=lambda record: (
                record["domain"],
                record["cohort"],
                record["artifact_id"],
            )
        )
        if len(records) != source_audit["artifact_count"]:
            raise PortfolioError("Copied artwork count changed during assembly.")
        attribution_audit = _audit_on_page_attribution(staging_path, records)
        _write_top_level(staging_path, domains, records, contact_sheet_count)
        expected_contact_sheets = 22
        if contact_sheet_count != expected_contact_sheets:
            raise PortfolioError(
                f"Expected {expected_contact_sheets} contact sheets, "
                f"copied/generated {contact_sheet_count}."
            )
        format_validation = _validate_copied_svgs(staging_path, records)
        validation = {
            "schema_version": 1,
            "generated_at": GENERATED_AT,
            "status": "assembled-and-format-validated",
            "artifact_count": len(records),
            "svg_count": len(records),
            "png_count": len(records),
            "plot_manifest_count": len(records),
            "contact_sheet_count": contact_sheet_count,
            "source_audit": source_audit,
            "format_validation": format_validation,
            "on_page_attribution_audit": attribution_audit,
            "pen_job_policy": "excluded-not-distinct-artwork",
        }
        _write_json(staging_path / "BUILD-VALIDATION.json", validation)
        checksum_count, _ = _write_checksums(staging_path)
        validation["checksum_entry_count"] = checksum_count
        # BUILD-VALIDATION is rewritten before the final checksum pass so the
        # checksum set describes the exact promoted bytes.
        _write_json(staging_path / "BUILD-VALIDATION.json", validation)
        _write_checksums(staging_path)
        staging_path.replace(output)
    except BaseException:
        if staging_path.exists() and staging_path.name.startswith(
            ".latest-map-portfolio-"
        ):
            shutil.rmtree(staging_path)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Verify source roots, exact counts, pairings, contracts, code, and docs.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    domains = _domains()
    try:
        audit = _validate_sources(domains)
        if args.check_only:
            print(json.dumps(audit, indent=2, sort_keys=True))
            return 0
        _assemble(args.output.resolve(), domains)
    except (
        OSError,
        PortfolioError,
        AttributionTransformError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"build_latest_map_portfolio: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.output.resolve()} ({audit['artifact_count']} plates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
