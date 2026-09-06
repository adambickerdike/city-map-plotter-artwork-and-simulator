#!/usr/bin/env python3
"""Verify the immutable artwork handoff and simulator repository inventory."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO = ROOT / "artwork/latest-map-portfolio-2026-08-18-v3"
SEATON_RELEASE = ROOT / "artwork/seaton-sluice-holywell-dene-2026-08-29-v8"
CARLISLE_RELEASE = (
    ROOT / "artwork/carlisle-university-fusehill-personalised-2026-08-31-v4"
)
COBRA_RELEASE = ROOT / "artwork/shelby-cobra-427-technical-blueprint-v1"
PORSCHE_RELEASE = ROOT / "artwork/porsche-911-2-0-targa-technical-blueprint-v1"
PRODUCTION_RELEASE = ROOT / "artwork/production-maps-2026-09-06"
EXPECTED_DOMAINS = {
    "01-university-cities-uk": 30,
    "02-university-cities-us": 20,
    "03-hiking-maps": 80,
    "04-marathon-courses": 14,
    "05-rowing-races": 8,
    "06-f1-courses": 246,
    "07-golf-courses": 25,
}
REQUIRED_SIMULATOR_FILES = (
    "tools/plotter_studio.py",
    "tools/plotter_control.py",
    "tools/plotjob.py",
    "tools/plotsim.py",
    "tools/build_plotsim_viewer.py",
    "tools/plotsim_viewer.tmpl",
    "docs/plotter/PLOTTER_SOFTWARE.md",
    "docs/plotter/device-profile-v1.schema.json",
    "docs/plotter/plot-job-v1.schema.json",
    "plotter-profiles/axidraw-class-simulation-v1.json",
    "plotter-profiles/grbl-servo-template-v1.json",
    "tests/test_plotter_system.py",
    "tests/test_paper_and_pens.py",
    "tests/test_repository_verification.py",
    "examples/augusta-national/augusta-national.svg",
    "examples/augusta-national/augusta-national.png",
    "examples/generated-viewers/augusta-national.html",
    "scripts/run_seaton_sluice_studio.sh",
    "scripts/run_carlisle_university_studio.sh",
    "scripts/run_shelby_cobra_studio.sh",
    "scripts/run_porsche_911_targa_studio.sh",
)
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
DRAWN_SVG_TAG = re.compile(r"<(?:path|text|tspan)\b[^>]*>", re.IGNORECASE)
DATA_COPY_ATTRIBUTE = re.compile(r'\bdata-copy="([^"]*)"', re.IGNORECASE)
VISIBLE_MAP_PROVIDER_REFERENCE = re.compile(
    r"(?:open\s*street\s*map|\bopenstreetmap\b|\bosm\b|\bodbl(?:[-\s]?1\.0)?\b|"
    r"open\s+map\s+data)",
    re.IGNORECASE,
)


class VerificationError(RuntimeError):
    """Raised when the repository handoff is incomplete or changed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"Could not read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"Expected a JSON object in {path}.")
    return value


def _require_real_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise VerificationError(f"Required regular file is missing: {path}")
    with path.open("rb") as handle:
        if handle.read(len(LFS_POINTER_PREFIX)) == LFS_POINTER_PREFIX:
            raise VerificationError(
                f"{path} is an unresolved Git LFS pointer; run `git lfs pull`."
            )


def _visible_map_provider_copy(path: Path) -> set[str]:
    """Return provider/licence copy attached to drawable SVG elements.

    Source contracts, SVG metadata and ``data-osm-*`` geometry lineage remain
    intentionally inspectable.  This gate covers only copy that a plotter or
    renderer can place on the finished sheet.
    """

    copies: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            for tag in DRAWN_SVG_TAG.findall(line):
                copy_match = DATA_COPY_ATTRIBUTE.search(tag)
                if copy_match is None:
                    continue
                copy = html.unescape(copy_match.group(1))
                if VISIBLE_MAP_PROVIDER_REFERENCE.search(copy):
                    copies.add(copy)
            # Generated plotter lettering carries ``data-copy``.  Retain this
            # fallback for a literal SVG text element introduced by hand.
            if (
                "<text" in line or "<tspan" in line
            ) and VISIBLE_MAP_PROVIDER_REFERENCE.search(html.unescape(line)):
                copies.add("literal SVG text provider/licence reference")
    return copies


def _html_map_provider_copy(path: Path) -> set[str]:
    """Return forbidden provider/licence references anywhere in a public page."""

    text = html.unescape(path.read_text(encoding="utf-8"))
    return {match.group(0) for match in VISIBLE_MAP_PROVIDER_REFERENCE.finditer(text)}


def _production_html_pages() -> list[Path]:
    pages = set((ROOT / "examples/generated-viewers").glob("*.html"))
    pages.add(PORTFOLIO / "index.html")
    pages.add(PRODUCTION_RELEASE / "index.html")
    pages.update((ROOT / "artwork").rglob("simulation/*.html"))
    pages.update(PORTFOLIO.rglob("gallery.html"))
    return sorted(pages)


def _verify_visible_map_provider_copy() -> dict[str, Any]:
    failures: list[str] = []
    inspected = 0
    for path in sorted(ROOT.rglob("*.svg")):
        _require_real_file(path)
        inspected += 1
        copies = _visible_map_provider_copy(path)
        if copies:
            failures.append(
                f"{path.relative_to(ROOT).as_posix()}: {', '.join(sorted(copies))}"
            )
    html_pages = _production_html_pages()
    for path in html_pages:
        _require_real_file(path)
        copies = _html_map_provider_copy(path)
        if copies:
            failures.append(
                f"{path.relative_to(ROOT).as_posix()}: {', '.join(sorted(copies))}"
            )
    if failures:
        raise VerificationError(
            "Visible map-provider/licence copy remains on production artwork: "
            + "; ".join(failures)
        )
    return {
        "production_visible_provider_copy_audit": "passed",
        "production_visible_provider_copy_svg_count": inspected,
        "production_visible_provider_copy_html_count": len(html_pages),
    }


def _verify_catalog(*, full: bool) -> tuple[int, int]:
    catalog = _load_json(PORTFOLIO / "catalog.json")
    records = catalog.get("artifacts")
    if not isinstance(records, list) or len(records) != 423:
        raise VerificationError("The catalog must contain exactly 423 artifacts.")
    counts = Counter(record.get("domain") for record in records)
    if dict(counts) != EXPECTED_DOMAINS:
        raise VerificationError(f"Unexpected catalog domain counts: {dict(counts)}")
    for record in records:
        if not isinstance(record, dict):
            raise VerificationError("Catalog artifact records must be objects.")
        for key in ("svg", "png", "manifest"):
            item = record.get(key)
            if not isinstance(item, dict):
                raise VerificationError(f"Catalog record lacks {key}: {record}")
            relative = item.get("path")
            expected_digest = item.get("sha256")
            if not isinstance(relative, str) or not isinstance(expected_digest, str):
                raise VerificationError(f"Invalid {key} record: {item}")
            path = PORTFOLIO / relative
            _require_real_file(path)
            if full and _sha256(path) != expected_digest:
                raise VerificationError(f"Catalog digest mismatch: {path}")
    return len(records), sum(EXPECTED_DOMAINS.values())


def _verify_release_checksums() -> int:
    checksum_path = PORTFOLIO / "CHECKSUMS.sha256"
    _require_real_file(checksum_path)
    checked = 0
    for line_number, raw_line in enumerate(
        checksum_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line:
            continue
        try:
            expected, relative = raw_line.split("  ", maxsplit=1)
        except ValueError as exc:
            raise VerificationError(
                f"Malformed checksum line {line_number}: {raw_line!r}"
            ) from exc
        path = PORTFOLIO / relative
        _require_real_file(path)
        if _sha256(path) != expected:
            raise VerificationError(f"Release checksum mismatch: {path}")
        checked += 1
    if checked != 2463:
        raise VerificationError(f"Expected 2463 release checksums, found {checked}.")
    return checked


def _verify_seaton_release() -> dict[str, Any]:
    import_manifest = _load_json(SEATON_RELEASE / "SOFTWARE_IMPORT.json")
    source_contract = _load_json(SEATON_RELEASE / "SOURCE-CONTRACT.json")
    qa_report = _load_json(SEATON_RELEASE / "qa/QA-REPORT.json")
    plot_job = _load_json(
        SEATON_RELEASE / "simulation/seaton-sluice-holywell-dene.plotjob.json"
    )
    if import_manifest.get("package_id") != SEATON_RELEASE.name:
        raise VerificationError("Seaton software import package ID does not match.")
    if source_contract.get("contract_id") != SEATON_RELEASE.name:
        raise VerificationError("Seaton source contract ID does not match.")
    if qa_report.get("status") != "pass":
        raise VerificationError("Seaton release QA report did not pass.")

    entrypoints = import_manifest.get("entrypoints")
    integrity = import_manifest.get("integrity")
    if not isinstance(entrypoints, dict) or not isinstance(integrity, dict):
        raise VerificationError(
            "Seaton import manifest lacks entrypoints or integrity."
        )
    digest_bindings = {
        "master_svg": "master_svg_sha256",
        "preview_png": "preview_png_sha256",
        "plot_manifest": "plot_manifest_sha256",
        "plot_job": "plot_job_file_sha256",
        "portable_viewer": "portable_viewer_sha256",
    }
    for entrypoint, digest_key in digest_bindings.items():
        relative = entrypoints.get(entrypoint)
        expected = integrity.get(digest_key)
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise VerificationError(f"Invalid Seaton import binding: {entrypoint}.")
        path = SEATON_RELEASE / relative
        _require_real_file(path)
        if _sha256(path) != expected:
            raise VerificationError(f"Seaton import digest mismatch: {path}.")

    profile = ROOT / "plotter-profiles/axidraw-class-simulation-v1.json"
    _require_real_file(profile)
    if _sha256(profile) != integrity.get("machine_profile_file_sha256"):
        raise VerificationError("Seaton import machine-profile digest mismatch.")

    pen_records = import_manifest.get("per_pen_svgs")
    if not isinstance(pen_records, list) or len(pen_records) != 11:
        raise VerificationError("Seaton import must declare exactly 11 per-pen SVGs.")
    if any(not isinstance(record, dict) for record in pen_records):
        raise VerificationError("Seaton per-pen records must be objects.")
    if [record.get("load") for record in pen_records] != list(range(1, 12)):
        raise VerificationError("Seaton per-pen load order is not contiguous.")
    for record in pen_records:
        relative = record.get("path")
        if not isinstance(relative, str):
            raise VerificationError("Seaton per-pen record lacks a path.")
        _require_real_file(SEATON_RELEASE / relative)

    motion_software = import_manifest.get("motion_software")
    if not isinstance(motion_software, dict):
        raise VerificationError("Seaton import lacks motion-software bindings.")
    for role, relative in motion_software.items():
        if not isinstance(relative, str):
            raise VerificationError(f"Invalid Seaton software binding: {role}.")
        path = (SEATON_RELEASE / relative).resolve()
        try:
            path.relative_to(ROOT.resolve())
        except ValueError as exc:
            raise VerificationError(
                f"Seaton software binding leaves the repository: {role}."
            ) from exc
        _require_real_file(path)

    master_digest = integrity["master_svg_sha256"]
    if plot_job.get("source", {}).get("sha256") != master_digest:
        raise VerificationError("Seaton plot job is not bound to its master SVG.")
    if plot_job.get("preflight", {}).get("path_count") != 2742:
        raise VerificationError("Seaton plot-job path count changed.")
    if plot_job.get("safety", {}).get("execution_allowed") is not False:
        raise VerificationError("Seaton physical execution must remain blocked.")

    checksum_path = SEATON_RELEASE / "CHECKSUMS.sha256"
    _require_real_file(checksum_path)
    checked = 0
    for line_number, raw_line in enumerate(
        checksum_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line:
            continue
        try:
            expected, relative = raw_line.split("  ", maxsplit=1)
        except ValueError as exc:
            raise VerificationError(
                f"Malformed Seaton checksum line {line_number}: {raw_line!r}"
            ) from exc
        path = SEATON_RELEASE / relative
        _require_real_file(path)
        if _sha256(path) != expected:
            raise VerificationError(f"Seaton release checksum mismatch: {path}")
        checked += 1
    if checked != 32:
        raise VerificationError(f"Expected 32 Seaton checksums, found {checked}.")
    return {
        "seaton_checksum_count": checked,
        "seaton_per_pen_svg_count": len(pen_records),
        "seaton_plot_path_count": plot_job["preflight"]["path_count"],
        "seaton_physical_execution_allowed": False,
    }


def _verify_carlisle_release() -> dict[str, Any]:
    import_manifest = _load_json(CARLISLE_RELEASE / "SOFTWARE_IMPORT.json")
    package_manifest = _load_json(CARLISLE_RELEASE / "PACKAGE.json")
    source_contract = _load_json(CARLISLE_RELEASE / "SOURCE-CONTRACT.json")
    qa_report = _load_json(CARLISLE_RELEASE / "qa/QA-REPORT.json")
    plot_job = _load_json(
        CARLISLE_RELEASE
        / "simulation/carlisle-university-fusehill-personalised.plotjob.json"
    )
    if import_manifest.get("package_id") != CARLISLE_RELEASE.name:
        raise VerificationError("Carlisle software import package ID does not match.")
    if package_manifest.get("package_id") != CARLISLE_RELEASE.name:
        raise VerificationError("Carlisle package manifest ID does not match.")
    if source_contract.get("contract_id") != CARLISLE_RELEASE.name:
        raise VerificationError("Carlisle source contract ID does not match.")
    if import_manifest.get("status") != "review-only":
        raise VerificationError("Carlisle package must remain review-only.")
    if qa_report.get("status") != "pass":
        raise VerificationError("Carlisle release QA report did not pass.")

    entrypoints = import_manifest.get("entrypoints")
    integrity = import_manifest.get("integrity")
    if not isinstance(entrypoints, dict) or not isinstance(integrity, dict):
        raise VerificationError(
            "Carlisle import manifest lacks entrypoints or integrity."
        )
    digest_bindings = {
        "master_svg": "master_svg_sha256",
        "preview_png": "preview_png_sha256",
        "plot_manifest": "plot_manifest_sha256",
        "plot_job": "plot_job_file_sha256",
        "portable_viewer": "portable_viewer_sha256",
        "machine_profile": "machine_profile_file_sha256",
        "source_snapshot": "source_snapshot_sha256",
        "source_query": "source_query_sha256",
        "source_contract": "source_contract_sha256",
        "qa_report": "qa_report_sha256",
    }
    for entrypoint, digest_key in digest_bindings.items():
        relative = entrypoints.get(entrypoint)
        expected = integrity.get(digest_key)
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise VerificationError(f"Invalid Carlisle import binding: {entrypoint}.")
        path = CARLISLE_RELEASE / relative
        _require_real_file(path)
        if _sha256(path) != expected:
            raise VerificationError(f"Carlisle import digest mismatch: {path}.")

    profile = ROOT / "plotter-profiles/axidraw-class-simulation-v1.json"
    _require_real_file(profile)
    if _sha256(profile) != integrity.get("machine_profile_file_sha256"):
        raise VerificationError("Carlisle shared machine-profile digest mismatch.")

    pen_records = import_manifest.get("per_pen_svgs")
    expected_pens = [
        "blue-0-4",
        "blue-0-25",
        "green-0-25",
        "purple-0-25",
        "grey-0-25",
        "red-0-4",
        "red-0-25",
        "black-0-6",
        "black-1",
        "black-0-4",
        "black-0-25",
    ]
    if not isinstance(pen_records, list) or len(pen_records) != 11:
        raise VerificationError("Carlisle import must declare exactly 11 per-pen SVGs.")
    if any(not isinstance(record, dict) for record in pen_records):
        raise VerificationError("Carlisle per-pen records must be objects.")
    if [record.get("load") for record in pen_records] != list(range(1, 12)):
        raise VerificationError("Carlisle per-pen load order is not contiguous.")
    if [record.get("pen_id") for record in pen_records] != expected_pens:
        raise VerificationError("Carlisle physical pen order changed.")
    for record in pen_records:
        relative = record.get("path")
        if not isinstance(relative, str):
            raise VerificationError("Carlisle per-pen record lacks a path.")
        _require_real_file(CARLISLE_RELEASE / relative)

    motion_software = import_manifest.get("motion_software")
    if not isinstance(motion_software, dict):
        raise VerificationError("Carlisle import lacks motion-software bindings.")
    for role, relative in motion_software.items():
        if not isinstance(relative, str):
            raise VerificationError(f"Invalid Carlisle software binding: {role}.")
        path = (CARLISLE_RELEASE / relative).resolve()
        try:
            path.relative_to(ROOT.resolve())
        except ValueError as exc:
            raise VerificationError(
                f"Carlisle software binding leaves the repository: {role}."
            ) from exc
        _require_real_file(path)

    master_digest = integrity["master_svg_sha256"]
    if plot_job.get("source", {}).get("sha256") != master_digest:
        raise VerificationError("Carlisle plot job is not bound to its master SVG.")
    if plot_job.get("preflight", {}).get("path_count") != 2828:
        raise VerificationError("Carlisle plot-job path count changed.")
    geometry = plot_job.get("geometry", {})
    if geometry.get("stroke_count") != 2828 or geometry.get("vertex_count") != 45130:
        raise VerificationError("Carlisle plot-job geometry counts changed.")
    if len(plot_job.get("pen_groups", [])) != 11:
        raise VerificationError("Carlisle plot job must contain eleven pen groups.")
    if plot_job.get("job_sha256") != import_manifest.get("plot_job", {}).get(
        "job_sha256"
    ):
        raise VerificationError("Carlisle internal plot-job digest changed.")
    safety = plot_job.get("safety", {})
    if safety.get("execution_allowed") is not False:
        raise VerificationError("Carlisle physical execution must remain blocked.")
    blocker_codes = {
        finding.get("code")
        for finding in safety.get("findings", [])
        if isinstance(finding, dict) and finding.get("severity") == "blocker"
    }
    if blocker_codes != {"unmeasured-pens", "uncalibrated-machine-timing"}:
        raise VerificationError("Carlisle physical safety blockers changed.")

    station_pen = CARLISLE_RELEASE / pen_records[3]["path"]
    if 'data-osm-id="566812584"' not in station_pen.read_text(encoding="utf-8"):
        raise VerificationError("Carlisle station is missing from pen load 04.")
    coverage = source_contract.get("coverage_evidence", {})
    if "way/566812584" not in coverage.get("required_landmark_refs", []):
        raise VerificationError(
            "Carlisle source contract no longer requires the station."
        )
    subject_qa = qa_report.get("subject", {})
    if (
        subject_qa.get("required_station_building_count") != 1
        or subject_qa.get("required_university_building_count") != 17
    ):
        raise VerificationError("Carlisle station or university-building QA changed.")

    checksum_path = CARLISLE_RELEASE / "CHECKSUMS.sha256"
    _require_real_file(checksum_path)
    checked = 0
    for line_number, raw_line in enumerate(
        checksum_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line:
            continue
        try:
            expected, relative = raw_line.split("  ", maxsplit=1)
        except ValueError as exc:
            raise VerificationError(
                f"Malformed Carlisle checksum line {line_number}: {raw_line!r}"
            ) from exc
        path = CARLISLE_RELEASE / relative
        _require_real_file(path)
        if _sha256(path) != expected:
            raise VerificationError(f"Carlisle release checksum mismatch: {path}")
        checked += 1
    if checked != 32:
        raise VerificationError(f"Expected 32 Carlisle checksums, found {checked}.")
    return {
        "carlisle_checksum_count": checked,
        "carlisle_plot_path_count": plot_job["preflight"]["path_count"],
        "carlisle_plot_vertex_count": geometry["vertex_count"],
        "carlisle_per_pen_svg_count": len(pen_records),
        "carlisle_station_path_count": 1,
        "carlisle_physical_execution_allowed": False,
    }


def _verify_cobra_release() -> dict[str, Any]:
    import_manifest = _load_json(COBRA_RELEASE / "SOFTWARE_IMPORT.json")
    package_manifest = _load_json(COBRA_RELEASE / "PACKAGE.json")
    plot_job = _load_json(
        COBRA_RELEASE / "plot/shelby-cobra-427.optimised.plotjob.json"
    )
    if import_manifest.get("package_id") != COBRA_RELEASE.name:
        raise VerificationError("Cobra software import package ID does not match.")
    if package_manifest.get("package_id") != COBRA_RELEASE.name:
        raise VerificationError("Cobra package manifest ID does not match.")
    if import_manifest.get("status") != "review-only":
        raise VerificationError("Cobra package must remain review-only.")

    entrypoints = import_manifest.get("entrypoints")
    integrity = import_manifest.get("integrity")
    if not isinstance(entrypoints, dict) or not isinstance(integrity, dict):
        raise VerificationError("Cobra import manifest lacks entrypoints or integrity.")
    digest_bindings = {
        "master_svg": "master_svg_sha256",
        "preview_png": "preview_png_sha256",
        "plot_manifest": "plot_manifest_sha256",
        "plot_job": "plot_job_file_sha256",
        "portable_viewer": "portable_viewer_sha256",
        "fact_ledger": "fact_ledger_sha256",
        "geometry_source": "geometry_source_sha256",
        "source_record": "source_record_sha256",
        "vector_provenance": "vector_provenance_sha256",
    }
    for entrypoint, digest_key in digest_bindings.items():
        relative = entrypoints.get(entrypoint)
        expected = integrity.get(digest_key)
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise VerificationError(f"Invalid Cobra import binding: {entrypoint}.")
        path = COBRA_RELEASE / relative
        _require_real_file(path)
        if _sha256(path) != expected:
            raise VerificationError(f"Cobra import digest mismatch: {path}.")

    profile = ROOT / "plotter-profiles/axidraw-class-simulation-v1.json"
    _require_real_file(profile)
    if _sha256(profile) != integrity.get("machine_profile_file_sha256"):
        raise VerificationError("Cobra import machine-profile digest mismatch.")

    motion_software = import_manifest.get("motion_software")
    if not isinstance(motion_software, dict):
        raise VerificationError("Cobra import lacks motion-software bindings.")
    for role, relative in motion_software.items():
        if not isinstance(relative, str):
            raise VerificationError(f"Invalid Cobra software binding: {role}.")
        path = (COBRA_RELEASE / relative).resolve()
        try:
            path.relative_to(ROOT.resolve())
        except ValueError as exc:
            raise VerificationError(
                f"Cobra software binding leaves the repository: {role}."
            ) from exc
        _require_real_file(path)

    pen_plan = import_manifest.get("pen_plan")
    expected_pens = ["white-0-3", "white-0-4", "white-0-5"]
    if not isinstance(pen_plan, list) or len(pen_plan) != 3:
        raise VerificationError("Cobra import must declare exactly three pen loads.")
    if any(not isinstance(record, dict) for record in pen_plan):
        raise VerificationError("Cobra pen-plan records must be objects.")
    if [record.get("load") for record in pen_plan] != [1, 2, 3]:
        raise VerificationError("Cobra pen load order is not contiguous.")
    if [record.get("pen_id") for record in pen_plan] != expected_pens:
        raise VerificationError("Cobra physical pen order changed.")

    master_digest = integrity["master_svg_sha256"]
    if plot_job.get("source", {}).get("sha256") != master_digest:
        raise VerificationError("Cobra plot job is not bound to its master SVG.")
    if plot_job.get("preflight", {}).get("path_count") != 542:
        raise VerificationError("Cobra plot-job path count changed.")
    geometry = plot_job.get("geometry", {})
    if geometry.get("stroke_count") != 542 or geometry.get("vertex_count") != 7924:
        raise VerificationError("Cobra plot-job geometry counts changed.")
    if len(plot_job.get("pen_groups", [])) != 3:
        raise VerificationError("Cobra plot job must contain three pen groups.")
    if plot_job.get("job_sha256") != import_manifest.get("plot_job", {}).get(
        "job_sha256"
    ):
        raise VerificationError("Cobra internal plot-job digest changed.")
    safety = plot_job.get("safety", {})
    if safety.get("execution_allowed") is not False:
        raise VerificationError("Cobra physical execution must remain blocked.")
    blocker_codes = {
        finding.get("code")
        for finding in safety.get("findings", [])
        if isinstance(finding, dict) and finding.get("severity") == "blocker"
    }
    if blocker_codes != {"unmeasured-pens", "uncalibrated-machine-timing"}:
        raise VerificationError("Cobra physical safety blockers changed.")

    master_path = COBRA_RELEASE / entrypoints["master_svg"]
    master_text = master_path.read_text(encoding="utf-8")
    disallowed_svg_fragments = ("stroke-dasharray", "<text", "<image")
    if any(fragment in master_text for fragment in disallowed_svg_fragments):
        raise VerificationError("Cobra master is not solid, path-only vector artwork.")

    checksum_path = COBRA_RELEASE / "CHECKSUMS.sha256"
    _require_real_file(checksum_path)
    checked = 0
    for line_number, raw_line in enumerate(
        checksum_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line:
            continue
        try:
            expected, relative = raw_line.split("  ", maxsplit=1)
        except ValueError as exc:
            raise VerificationError(
                f"Malformed Cobra checksum line {line_number}: {raw_line!r}"
            ) from exc
        path = COBRA_RELEASE / relative
        _require_real_file(path)
        if _sha256(path) != expected:
            raise VerificationError(f"Cobra release checksum mismatch: {path}")
        checked += 1
    if checked != 17:
        raise VerificationError(f"Expected 17 Cobra checksums, found {checked}.")
    return {
        "cobra_checksum_count": checked,
        "cobra_plot_path_count": plot_job["preflight"]["path_count"],
        "cobra_plot_vertex_count": geometry["vertex_count"],
        "cobra_pen_load_count": len(pen_plan),
        "cobra_physical_execution_allowed": False,
    }


def _verify_porsche_release() -> dict[str, Any]:
    import_manifest = _load_json(PORSCHE_RELEASE / "SOFTWARE_IMPORT.json")
    package_manifest = _load_json(PORSCHE_RELEASE / "PACKAGE.json")
    plot_job = _load_json(
        PORSCHE_RELEASE / "plot/porsche-911-2-0-targa.optimised.plotjob.json"
    )
    if import_manifest.get("package_id") != PORSCHE_RELEASE.name:
        raise VerificationError("Porsche software import package ID does not match.")
    if package_manifest.get("package_id") != PORSCHE_RELEASE.name:
        raise VerificationError("Porsche package manifest ID does not match.")
    if import_manifest.get("status") != "review-only":
        raise VerificationError("Porsche package must remain review-only.")

    entrypoints = import_manifest.get("entrypoints")
    integrity = import_manifest.get("integrity")
    if not isinstance(entrypoints, dict) or not isinstance(integrity, dict):
        raise VerificationError(
            "Porsche import manifest lacks entrypoints or integrity."
        )
    digest_bindings = {
        "master_svg": "master_svg_sha256",
        "preview_png": "preview_png_sha256",
        "plot_manifest": "plot_manifest_sha256",
        "plot_job": "plot_job_file_sha256",
        "portable_viewer": "portable_viewer_sha256",
        "fact_ledger": "fact_ledger_sha256",
        "geometry_source": "geometry_source_sha256",
        "source_record": "source_record_sha256",
        "vector_provenance": "vector_provenance_sha256",
    }
    for entrypoint, digest_key in digest_bindings.items():
        relative = entrypoints.get(entrypoint)
        expected = integrity.get(digest_key)
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise VerificationError(f"Invalid Porsche import binding: {entrypoint}.")
        path = PORSCHE_RELEASE / relative
        _require_real_file(path)
        if _sha256(path) != expected:
            raise VerificationError(f"Porsche import digest mismatch: {path}.")

    profile = ROOT / "plotter-profiles/axidraw-class-simulation-v1.json"
    _require_real_file(profile)
    if _sha256(profile) != integrity.get("machine_profile_file_sha256"):
        raise VerificationError("Porsche import machine-profile digest mismatch.")

    motion_software = import_manifest.get("motion_software")
    if not isinstance(motion_software, dict):
        raise VerificationError("Porsche import lacks motion-software bindings.")
    for role, relative in motion_software.items():
        if not isinstance(relative, str):
            raise VerificationError(f"Invalid Porsche software binding: {role}.")
        path = (PORSCHE_RELEASE / relative).resolve()
        try:
            path.relative_to(ROOT.resolve())
        except ValueError as exc:
            raise VerificationError(
                f"Porsche software binding leaves the repository: {role}."
            ) from exc
        _require_real_file(path)

    pen_plan = import_manifest.get("pen_plan")
    expected_pens = ["white-0-3", "white-0-4", "white-0-5"]
    if not isinstance(pen_plan, list) or len(pen_plan) != 3:
        raise VerificationError("Porsche import must declare exactly three pen loads.")
    if any(not isinstance(record, dict) for record in pen_plan):
        raise VerificationError("Porsche pen-plan records must be objects.")
    if [record.get("load") for record in pen_plan] != [1, 2, 3]:
        raise VerificationError("Porsche pen load order is not contiguous.")
    if [record.get("pen_id") for record in pen_plan] != expected_pens:
        raise VerificationError("Porsche physical pen order changed.")

    master_digest = integrity["master_svg_sha256"]
    if plot_job.get("source", {}).get("sha256") != master_digest:
        raise VerificationError("Porsche plot job is not bound to its master SVG.")
    if plot_job.get("preflight", {}).get("path_count") != 478:
        raise VerificationError("Porsche plot-job path count changed.")
    geometry = plot_job.get("geometry", {})
    if geometry.get("stroke_count") != 478 or geometry.get("vertex_count") != 4251:
        raise VerificationError("Porsche plot-job geometry counts changed.")
    if len(plot_job.get("pen_groups", [])) != 3:
        raise VerificationError("Porsche plot job must contain three pen groups.")
    if plot_job.get("job_sha256") != import_manifest.get("plot_job", {}).get(
        "job_sha256"
    ):
        raise VerificationError("Porsche internal plot-job digest changed.")
    safety = plot_job.get("safety", {})
    if safety.get("execution_allowed") is not False:
        raise VerificationError("Porsche physical execution must remain blocked.")
    blocker_codes = {
        finding.get("code")
        for finding in safety.get("findings", [])
        if isinstance(finding, dict) and finding.get("severity") == "blocker"
    }
    if blocker_codes != {"unmeasured-pens", "uncalibrated-machine-timing"}:
        raise VerificationError("Porsche physical safety blockers changed.")

    source_record = _load_json(PORSCHE_RELEASE / entrypoints["source_record"])
    if (
        source_record.get("native_svg_original") is not True
        or source_record.get("raster_conversion_used") is not False
        or source_record.get("source_sha256") != integrity.get("geometry_source_sha256")
    ):
        raise VerificationError("Porsche native-SVG source record changed.")
    fact_ledger = _load_json(PORSCHE_RELEASE / entrypoints["fact_ledger"])
    fact_subject = fact_ledger.get("subjects", {}).get(
        "car-native-035-porsche-911-targa"
    )
    if not isinstance(fact_subject, dict):
        raise VerificationError("Porsche technical fact subject is missing.")
    cards = fact_subject.get("fact_cards")
    if not isinstance(cards, list) or [card.get("label") for card in cards] != [
        "ENGINE",
        "DISPLACEMENT",
        "POWER",
        "WEIGHT",
        "0-100 KM/H",
        "TOP SPEED",
    ]:
        raise VerificationError("Porsche technical fact cards changed.")

    master_path = PORSCHE_RELEASE / entrypoints["master_svg"]
    master_text = master_path.read_text(encoding="utf-8")
    disallowed_svg_fragments = (
        "stroke-dasharray",
        "stroke-dashoffset",
        "<text",
        "<image",
    )
    if any(fragment in master_text for fragment in disallowed_svg_fragments):
        raise VerificationError(
            "Porsche master is not solid, path-only vector artwork."
        )
    forbidden_visible_copy = (
        'data-copy="SOURCE',
        'data-copy="CREATOR',
        'data-copy="LICENCE',
        'data-copy="LICENSE',
        'data-copy="PEN',
        'data-copy="VIEW',
        'data-copy="NATIVE SVG',
        'data-copy="BLUE STOCK',
        'data-copy="SOURCE-FAITHFUL',
    )
    if any(fragment in master_text for fragment in forbidden_visible_copy):
        raise VerificationError("Porsche master contains visible process copy.")

    checksum_path = PORSCHE_RELEASE / "CHECKSUMS.sha256"
    _require_real_file(checksum_path)
    checked = 0
    for line_number, raw_line in enumerate(
        checksum_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line:
            continue
        try:
            expected, relative = raw_line.split("  ", maxsplit=1)
        except ValueError as exc:
            raise VerificationError(
                f"Malformed Porsche checksum line {line_number}: {raw_line!r}"
            ) from exc
        path = PORSCHE_RELEASE / relative
        _require_real_file(path)
        if _sha256(path) != expected:
            raise VerificationError(f"Porsche release checksum mismatch: {path}")
        checked += 1
    if checked != 17:
        raise VerificationError(f"Expected 17 Porsche checksums, found {checked}.")
    return {
        "porsche_checksum_count": checked,
        "porsche_plot_path_count": plot_job["preflight"]["path_count"],
        "porsche_plot_vertex_count": geometry["vertex_count"],
        "porsche_pen_load_count": len(pen_plan),
        "porsche_physical_execution_allowed": False,
    }


def _verify_augusta_release() -> dict[str, Any]:
    release = ROOT / "examples/augusta-national"
    master_path = release / "augusta-national.svg"
    png_path = release / "augusta-national.png"
    manifest_path = release / "augusta-national.plot.json"
    plot_job_path = ROOT / "examples/generated-viewers/augusta-national.plotjob.json"
    viewer_path = ROOT / "examples/generated-viewers/augusta-national.html"
    for path in (master_path, png_path, manifest_path, plot_job_path, viewer_path):
        _require_real_file(path)

    manifest = _load_json(manifest_path)
    plot_job = _load_json(plot_job_path)
    master_digest = _sha256(master_path)
    rendering = manifest.get("rendering", {})
    if (
        rendering.get("visible_attribution") is not False
        or rendering.get("on_page_openstreetmap_reference") is not False
        or rendering.get("openstreetmap_attribution_mode") != "external"
        or rendering.get("external_openstreetmap_attribution_placement")
        != "Repository ARTWORK_AND_DATA_NOTICE.md and product listing or packaging"
    ):
        raise VerificationError("Augusta visible/external attribution state changed.")

    transform = manifest.get("presentation_transform", {})
    if (
        transform.get("removed_visible_path_count") != 46
        or transform.get("pen_files_regenerated") is not True
        or transform.get("machine_metrics_regenerated") is not True
        or transform.get("source_provenance_retained") is not True
        or transform.get("source_licence_metadata_retained") is not True
    ):
        raise VerificationError("Augusta production presentation transform changed.")

    outputs = manifest.get("outputs", {})
    output_bindings = (
        (outputs.get("svg"), master_path),
        (outputs.get("png"), png_path),
    )
    for record, path in output_bindings:
        if not isinstance(record, dict) or record.get("sha256") != _sha256(path):
            raise VerificationError(f"Augusta output digest mismatch: {path}.")
    pen_files = outputs.get("pen_files")
    if not isinstance(pen_files, list) or len(pen_files) != 9:
        raise VerificationError("Augusta must contain exactly nine per-pen SVGs.")
    if [record.get("step") for record in pen_files] != list(range(1, 10)):
        raise VerificationError("Augusta per-pen load order changed.")
    for record in pen_files:
        relative = record.get("path")
        expected = record.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise VerificationError("Invalid Augusta per-pen output binding.")
        path = release / relative
        _require_real_file(path)
        if _sha256(path) != expected:
            raise VerificationError(f"Augusta per-pen digest mismatch: {path}.")

    master_text = master_path.read_text(encoding="utf-8")
    black_pen_path = release / "augusta-national.pen-07-black-0-4.svg"
    black_pen_text = black_pen_path.read_text(encoding="utf-8")
    if len(re.findall(r"<path\b", master_text)) != 1255:
        raise VerificationError("Augusta cleaned master path count changed.")
    if len(re.findall(r"<path\b", black_pen_text)) != 295:
        raise VerificationError("Augusta cleaned black-0.4 path count changed.")
    if 'data-logical-layer="plate_attribution"' in master_text or (
        'data-logical-layer="plate_attribution"' in black_pen_text
    ):
        raise VerificationError("Augusta plotted attribution geometry reappeared.")

    summary = manifest.get("plot_summary", {})
    if (
        summary.get("pen_down_path_count") != 1255
        or summary.get("pen_down_distance_mm") != 19349.4
        or summary.get("pen_up_travel_mm") != 14447.0
    ):
        raise VerificationError("Augusta cleaned manifest motion metrics changed.")

    if plot_job.get("source", {}).get("sha256") != master_digest:
        raise VerificationError(
            "Augusta plot job is not bound to its cleaned master SVG."
        )
    if plot_job.get("preflight", {}).get("path_count") != 1255:
        raise VerificationError("Augusta plot-job path count changed.")
    geometry = plot_job.get("geometry", {})
    if geometry.get("stroke_count") != 1255 or geometry.get("vertex_count") != 9588:
        raise VerificationError("Augusta plot-job geometry counts changed.")
    if len(plot_job.get("pen_groups", [])) != 9:
        raise VerificationError("Augusta plot job must contain nine pen groups.")
    safety = plot_job.get("safety", {})
    if safety.get("execution_allowed") is not False:
        raise VerificationError("Augusta physical execution must remain blocked.")
    blocker_codes = {
        finding.get("code")
        for finding in safety.get("findings", [])
        if isinstance(finding, dict) and finding.get("severity") == "blocker"
    }
    if blocker_codes != {"unmeasured-pens", "uncalibrated-machine-timing"}:
        raise VerificationError("Augusta physical safety blockers changed.")

    if transform.get("optimised_plot_job_sha256") != _sha256(plot_job_path):
        raise VerificationError("Augusta transformed plot-job digest changed.")
    if transform.get("portable_viewer_sha256") != _sha256(viewer_path):
        raise VerificationError("Augusta transformed viewer digest changed.")
    if master_digest not in viewer_path.read_text(encoding="utf-8"):
        raise VerificationError(
            "Augusta viewer is not bound to its cleaned master SVG."
        )

    repository_manifest = _load_json(ROOT / "REPOSITORY_MANIFEST.json")
    promoted = repository_manifest.get("simulator", {}).get(
        "promoted_augusta_example", {}
    )
    promoted_bindings = {
        "master_svg_sha256": master_digest,
        "preview_png_sha256": _sha256(png_path),
        "plot_manifest_sha256": _sha256(manifest_path),
        "plot_job_file_sha256": _sha256(plot_job_path),
        "portable_viewer_sha256": _sha256(viewer_path),
    }
    if any(promoted.get(key) != value for key, value in promoted_bindings.items()):
        raise VerificationError("Repository manifest Augusta digest bindings changed.")
    if (
        promoted.get("plot_job_sha256") != plot_job.get("job_sha256")
        or promoted.get("stroke_count") != 1255
        or promoted.get("vertex_count") != 9588
        or promoted.get("removed_visible_provider_path_count") != 46
    ):
        raise VerificationError("Repository manifest Augusta evidence changed.")

    return {
        "augusta_plot_path_count": 1255,
        "augusta_plot_vertex_count": 9588,
        "augusta_pen_load_count": 9,
        "augusta_removed_visible_provider_path_count": 46,
        "augusta_plotjob_source_bound": True,
        "augusta_physical_execution_allowed": False,
    }


def _verify_structure() -> dict[str, Any]:
    for relative in REQUIRED_SIMULATOR_FILES:
        _require_real_file(ROOT / relative)
    validation = _load_json(PORTFOLIO / "BUILD-VALIDATION.json")
    if (
        validation.get("status") != "assembled-and-format-validated"
        or validation.get("artifact_count") != 423
        or validation.get("contact_sheet_count") != 22
    ):
        raise VerificationError("The stored portfolio build validation did not pass.")
    attribution = validation.get("on_page_attribution_audit")
    if not isinstance(attribution, dict) or attribution.get("status") != "passed":
        raise VerificationError("The stored visible-attribution audit did not pass.")
    portfolio_pngs = list(PORTFOLIO.rglob("*.png"))
    portfolio_svgs = list(PORTFOLIO.rglob("*.svg"))
    if len(portfolio_pngs) != 445 or len(portfolio_svgs) != 445:
        raise VerificationError(
            "Expected 445 portfolio PNGs and 445 portfolio SVGs, found "
            f"{len(portfolio_pngs)} and {len(portfolio_svgs)}."
        )
    repository_pngs = list(ROOT.rglob("*.png"))
    repository_svgs = list(ROOT.rglob("*.svg"))
    original_pngs = [p for p in repository_pngs if not p.is_relative_to(PRODUCTION_RELEASE)]
    original_svgs = [p for p in repository_svgs if not p.is_relative_to(PRODUCTION_RELEASE)]
    if len(original_pngs) != 451 or len(original_svgs) != 483:
        raise VerificationError(
            "Expected 451 original-edition PNGs and 483 original-edition SVGs, found "
            f"{len(original_pngs)} and {len(original_svgs)}."
        )
    for path in (*portfolio_pngs, *portfolio_svgs, *repository_pngs, *repository_svgs):
        _require_real_file(path)
    repository_manifest = _load_json(ROOT / "REPOSITORY_MANIFEST.json")
    repository_assets = repository_manifest.get("repository_assets", {})
    expected_asset_inventory = {
        "png_count": len(repository_pngs),
        "png_bytes": sum(path.stat().st_size for path in repository_pngs),
        "svg_count": len(repository_svgs),
        "svg_bytes": sum(path.stat().st_size for path in repository_svgs),
        "production_visible_provider_copy_audit": "passed",
        "production_visible_provider_copy_svg_count": len(repository_svgs),
        "production_visible_provider_copy_html_count": len(_production_html_pages()),
    }
    if any(
        repository_assets.get(key) != value
        for key, value in expected_asset_inventory.items()
    ):
        raise VerificationError("Repository asset inventory is stale.")
    return {
        "portfolio_png_count": len(portfolio_pngs),
        "portfolio_svg_count": len(portfolio_svgs),
        "repository_png_count": len(repository_pngs),
        "repository_svg_count": len(repository_svgs),
        "visible_attribution_audit": attribution.get("status"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Hash every catalog artifact and every release checksum entry.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        catalog_count, expected_count = _verify_catalog(full=args.full)
        structure = _verify_structure()
        seaton = _verify_seaton_release()
        carlisle = _verify_carlisle_release()
        cobra = _verify_cobra_release()
        porsche = _verify_porsche_release()
        augusta = _verify_augusta_release()
        visible_provider_copy = _verify_visible_map_provider_copy()
        checksum_count = _verify_release_checksums() if args.full else None
        from verify_production_maps import verify
        try:
            production = verify(PRODUCTION_RELEASE, full=args.full)
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            raise VerificationError(f"Production collection: {exc}") from exc
    except VerificationError as exc:
        print(f"verify_repository: {exc}", file=sys.stderr)
        return 2
    report = {
        "status": "passed",
        "full_hash_verification": args.full,
        "catalog_artifact_count": catalog_count,
        "expected_artifact_count": expected_count,
        "release_checksum_count": checksum_count,
        "production_collection": production,
        **structure,
        **seaton,
        **carlisle,
        **cobra,
        **porsche,
        **augusta,
        **visible_provider_copy,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
