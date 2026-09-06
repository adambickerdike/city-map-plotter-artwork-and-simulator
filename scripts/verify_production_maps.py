#!/usr/bin/env python3
"""Verify the portable customer map release after building or cloning it."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
from city_map_plotter.production_copy import FORBIDDEN_COPY, geometry_digest, visible_copies  # noqa: E402
from city_map_plotter.production_header import HEADER_IDS, is_city_map, verify_city_header  # noqa: E402
from plotjob import verify_plot_job  # noqa: E402

RELEASE = ROOT / "artwork/production-maps-2026-09-06"
EXPECTED = {
    "01-university-cities-uk": 32, "02-university-cities-us": 20,
    "03-hiking-maps": 81, "04-marathon-courses": 14, "05-rowing-races": 8,
    "06-f1-courses": 246, "07-golf-courses": 25, "08-city-maps-uk": 31,
}


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def relative_file(release: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Nonportable release path: {value}")
    path = release / relative
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(release.resolve()):
        raise ValueError(f"Missing or unsafe release file: {value}")
    with path.open("rb") as handle:
        if handle.read(42).startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise ValueError(f"Unresolved Git LFS pointer: {value}; run git lfs pull")
    return path


def verify(release: Path, *, full: bool = False) -> dict:
    catalog = json.loads((release / "catalog.json").read_text())
    rows = catalog["artifacts"]
    counts = dict(Counter(r["domain"] for r in rows))
    if counts != EXPECTED or catalog["artifact_count"] != sum(EXPECTED.values()):
        raise ValueError(f"Incomplete collection: {counts}")
    keys = [(r["domain"], r["id"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate map IDs")
    files_checked, jobs_checked, headers_checked = 0, 0, 0
    for row in rows:
        for file in [row[k] for k in ("svg", "png", "thumbnail", "manifest", "job")] + row["pen_files"]:
            path = relative_file(release, file["path"])
            if path.stat().st_size != file["bytes"] or (full and sha(path) != file["sha256"]):
                raise ValueError(f"Changed artifact: {file['path']}")
            files_checked += 1
        master = release / row["svg"]["path"]
        manifest = json.loads((release / row["manifest"]["path"]).read_text())
        root = ET.parse(master).getroot()
        copies = visible_copies(root, manifest)
        if copies != row["visible_copy"] or any(FORBIDDEN_COPY.search(s) for s in copies):
            raise ValueError(f"Customer copy changed: {row['id']}")
        if not manifest["digital_release"]["software_import_ready"]:
            raise ValueError(f"Map is not import ready: {row['id']}")
        if not row["qa"]["map_geometry_unchanged"]:
            raise ValueError(f"Unexplained map geometry change: {row['id']}")
        if is_city_map(manifest):
            header = verify_city_header(root, manifest)
            if header != {key: manifest["header_layout"][key] for key in header}:
                raise ValueError(f"City header evidence does not match the SVG: {row['id']}")
            if geometry_digest(root, exclude_furniture=True, exclude_group_ids=HEADER_IDS) != manifest["header_layout"]["map_geometry_sha256"]:
                raise ValueError(f"City map linework changed: {row['id']}")
            headers_checked += 1
        if full:
            job = json.loads((release / row["job"]["path"]).read_text())
            verify_plot_job(job)
            if job["source"]["sha256"] != row["svg"]["sha256"] or job["source"]["bytes"] != row["svg"]["bytes"]:
                raise ValueError(f"Plot job does not match the final master: {row['id']}")
            if len(job["pen_groups"]) != len(row["pen_files"]) or row["stroke_count"] != job["geometry"]["stroke_count"]:
                raise ValueError(f"Pen/job inventory mismatch: {row['id']}")
            if any(issue["severity"] == "error" for issue in job["preflight"]["issues"]):
                raise ValueError(f"SVG preflight failed: {row['id']}")
            jobs_checked += 1
    if headers_checked != 84:
        raise ValueError(f"Incomplete city header update: {headers_checked}/84")
    for required in ("newcastle-city-a3-portrait", "newcastle-university-a3-portrait"):
        row = next(r for r in rows if r["id"] == required)
        manifest = json.loads((release / row["manifest"]["path"]).read_text())
        rendering = manifest["rendering"]
        if rendering["detail_profile"] != "plotter-faithful" or rendering["simplify_tolerance_mm"] != 0.04:
            raise ValueError("Newcastle has lost its full-detail rendering recipe")
        if "Armstrong Building" not in (release / row["svg"]["path"]).read_text():
            raise ValueError("Newcastle's Armstrong Building is missing")
    checksums = 0
    if full:
        indexed = set()
        for line in (release / "CHECKSUMS.sha256").read_text().splitlines():
            digest, path_text = line.split("  ", 1)
            path = relative_file(release, path_text)
            if path_text in indexed or sha(path) != digest:
                raise ValueError(f"Checksum ledger mismatch: {path_text}")
            indexed.add(path_text)
            checksums += 1
        actual = {p.relative_to(release).as_posix() for p in release.rglob("*") if p.is_file() and p.name != "CHECKSUMS.sha256" and "__pycache__" not in p.parts}
        if actual != indexed:
            raise ValueError(f"Checksum inventory incomplete: {sorted(actual ^ indexed)[:5]}")
    return {"status": "passed", "maps": len(rows), "counts_by_domain": counts,
            "artifact_files": files_checked, "verified_jobs": jobs_checked,
            "checksum_files": checksums, "visible_copy_failures": 0,
            "city_headers_verified": headers_checked}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, default=RELEASE)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.release.resolve(), full=args.full), indent=2))
        return 0
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        print(f"production-map-verification: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
