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
    if len(repository_pngs) != 447 or len(repository_svgs) != 455:
        raise VerificationError(
            "Expected 447 repository PNGs and 455 repository SVGs, found "
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
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
