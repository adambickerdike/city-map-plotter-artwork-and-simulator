#!/usr/bin/env python3
"""Verify the immutable artwork handoff and simulator repository inventory."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO = ROOT / "artwork/latest-map-portfolio-2026-08-18-v3"
SEATON_RELEASE = ROOT / "artwork/seaton-sluice-holywell-dene-2026-08-29-v8"
COBRA_RELEASE = ROOT / "artwork/shelby-cobra-427-technical-blueprint-v1"
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
    "examples/augusta-national/augusta-national.svg",
    "examples/augusta-national/augusta-national.png",
    "examples/generated-viewers/augusta-national.html",
    "scripts/run_seaton_sluice_studio.sh",
    "scripts/run_shelby_cobra_studio.sh",
)
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"


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
        raise VerificationError("Seaton import manifest lacks entrypoints or integrity.")
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
    if len(repository_pngs) != 449 or len(repository_svgs) != 469:
        raise VerificationError(
            "Expected 449 repository PNGs and 469 repository SVGs, found "
            f"{len(repository_pngs)} and {len(repository_svgs)}."
        )
    for path in (*portfolio_pngs, *portfolio_svgs, *repository_pngs, *repository_svgs):
        _require_real_file(path)
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
        cobra = _verify_cobra_release()
        checksum_count = _verify_release_checksums() if args.full else None
    except VerificationError as exc:
        print(f"verify_repository: {exc}", file=sys.stderr)
        return 2
    report = {
        "status": "passed",
        "full_hash_verification": args.full,
        "catalog_artifact_count": catalog_count,
        "expected_artifact_count": expected_count,
        "release_checksum_count": checksum_count,
        **structure,
        **seaton,
        **cobra,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
