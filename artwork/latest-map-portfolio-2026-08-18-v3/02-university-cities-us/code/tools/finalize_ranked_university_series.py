#!/usr/bin/env python3
"""Create contact sheets and an exact checksum inventory for the ranked series."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Sequence


UK_COLLECTION = "uk-times-good-university-guide-2026-top-30"
US_COLLECTION = "us-qs-world-university-rankings-2027-top-20"
EXPECTED_COUNTS = {UK_COLLECTION: 30, US_COLLECTION: 20}
CONTACT_NAMES = {
    UK_COLLECTION: "uk-ranked-universities-contact-sheet.png",
    US_COLLECTION: "us-ranked-universities-contact-sheet.png",
}
QA_REPORT_NAMES = frozenset(
    {
        "RANKED_UNIVERSITY_QA_REPORT.json",
        "RANKED_UNIVERSITY_QA_REPORT-pilot.json",
    }
)
CHECKSUM_POLICY = (
    "Every release file is declared except root CHECKSUMS.sha256 and the "
    "regenerated root QA reports RANKED_UNIVERSITY_QA_REPORT.json and "
    "RANKED_UNIVERSITY_QA_REPORT-pilot.json."
)


class FinalizeError(RuntimeError):
    """The batch is incomplete or its release paths are unsafe."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: object) -> None:
    _atomic_text(
        path,
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
    )


def _release_path(value: object, root: Path, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise FinalizeError(f"{label} is missing.")
    path = Path(value).expanduser()
    resolved = (path if path.is_absolute() else root / path).resolve()
    if not resolved.is_relative_to(root):
        raise FinalizeError(f"{label} escapes the release root: {resolved}")
    return resolved


def _source_inventory_root(root: Path) -> tuple[Path, str]:
    """Return the only directory allowed to supply completed source bytes.

    Older pilots copied live responses into ``source-cache``. The v2.1.4
    release instead renders directly from the exact JSON-set contract copied
    below ``release-metadata/source-snapshots``. Finalization must understand
    both layouts without allowing a manifest to point elsewhere in the release.
    """

    contract_path = root / "SERIES-CONTRACT.json"
    if not contract_path.exists():
        return (root / "source-cache").resolve(), "source-cache"
    if contract_path.is_symlink() or not contract_path.is_file():
        raise FinalizeError("SERIES-CONTRACT.json must be a regular file.")
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalizeError(f"Could not read SERIES-CONTRACT.json: {exc}") from exc
    if not isinstance(contract, dict):
        raise FinalizeError("SERIES-CONTRACT.json must contain a JSON object.")
    source_contract = contract.get("source_contract")
    if not isinstance(source_contract, dict) or source_contract.get("mode") != (
        "pinned-input-json-set"
    ):
        return (root / "source-cache").resolve(), "source-cache"

    source_manifest = _release_path(
        source_contract.get("path"), root, "series pinned-source manifest"
    )
    if source_manifest.is_symlink() or not source_manifest.is_file():
        raise FinalizeError(
            f"Series pinned-source manifest is missing: {source_manifest}"
        )
    inventory_root = source_manifest.parent / "overpass"
    if inventory_root.is_symlink() or not inventory_root.is_dir():
        raise FinalizeError(
            "Series pinned-source overpass directory is missing or unsafe: "
            f"{inventory_root}"
        )
    return inventory_root.resolve(), "pinned-source snapshot"


def _source_data_path(
    value: object,
    root: Path,
    inventory_root: Path,
    inventory_label: str,
    label: str,
) -> Path:
    if isinstance(value, str) and ".." in Path(value).parts:
        raise FinalizeError(f"{label} contains forbidden path traversal.")
    resolved = _release_path(value, root, label)
    if not resolved.is_relative_to(inventory_root):
        raise FinalizeError(
            f"{label} is outside the release {inventory_label}: {resolved}"
        )
    if not resolved.name.endswith(".json.gz"):
        raise FinalizeError(f"{label} is not a canonical .json.gz cache file.")
    return resolved


def _manifest_source_path(
    item: dict[str, Any],
    root: Path,
    inventory_root: Path,
    inventory_label: str,
) -> Path:
    subject_id = str(item.get("subject_id", "unknown subject"))
    manifest_path = _release_path(
        item.get("manifest"), root, f"{subject_id} manifest"
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalizeError(
            f"Could not read completed manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise FinalizeError(f"Completed manifest is not a JSON object: {manifest_path}")
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise FinalizeError(f"{subject_id} completed manifest source record is missing.")
    return _source_data_path(
        source.get("cache_path"),
        root,
        inventory_root,
        inventory_label,
        f"{subject_id} source cache",
    )


def _reject_source_cache_inventory(items: list[dict[str, Any]], root: Path) -> None:
    inventory_root, inventory_label = _source_inventory_root(root)
    referenced: dict[Path, str] = {}
    failures: list[str] = []
    for item in items:
        if item.get("status") != "completed":
            continue
        subject_id = str(item.get("subject_id", "unknown subject"))
        try:
            cache_path = _manifest_source_path(
                item, root, inventory_root, inventory_label
            )
        except FinalizeError as exc:
            failures.append(str(exc))
            continue
        previous = referenced.get(cache_path)
        if previous is not None:
            failures.append(
                f"duplicate {inventory_label} reference: {previous} and {subject_id} "
                f"both reference {cache_path.relative_to(root)}"
            )
        else:
            referenced[cache_path] = subject_id
        if not cache_path.is_file():
            failures.append(
                f"{subject_id} referenced source-cache file is missing: "
                f"{cache_path.relative_to(root)}"
            )

    observed: set[Path] = set()
    if inventory_root.is_symlink():
        failures.append(
            f"release {inventory_label} directory must not be a symbolic link"
        )
    if inventory_root.is_dir():
        for path in sorted(inventory_root.rglob("*.json.gz")):
            if path.is_symlink():
                failures.append(
                    f"{inventory_label} file must not be a symbolic link: "
                    f"{path.relative_to(root)}"
                )
                continue
            if path.is_file():
                observed.add(path.resolve())

    unreferenced = sorted(observed - set(referenced))
    if unreferenced:
        failures.append(
            f"unreferenced {inventory_label} .json.gz files: "
            + ", ".join(path.relative_to(root).as_posix() for path in unreferenced)
        )
    if failures:
        raise FinalizeError("Source-cache inventory is invalid: " + "; ".join(failures))


def _build_contact_sheet(plates: list[Path], output: Path) -> None:
    executable = shutil.which("magick")
    if executable is None:
        raise FinalizeError("Contact-sheet generation requires ImageMagick `magick`.")
    command = [
        executable,
        "montage",
        *map(str, plates),
        "-thumbnail",
        "296x420",
        "-tile",
        "5x",
        "-geometry",
        "296x420+12+12",
        "-background",
        "white",
        "-alpha",
        "remove",
        "-alpha",
        "off",
        "-depth",
        "8",
        "-units",
        "PixelsPerInch",
        "-density",
        "254",
        "-define",
        "png:color-type=2",
        str(output),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0 or not output.is_file():
        raise FinalizeError(
            f"Could not create {output.name}: {result.stderr.strip()}"
        )


def _transient_kind(path: Path, root: Path) -> str | None:
    relative = path.relative_to(root)
    if "__pycache__" in relative.parts:
        return "__pycache__"
    if path.name == ".lock" or path.suffix == ".lock":
        return ".lock"
    if path.name == ".tmp" or path.suffix == ".tmp":
        return "*.tmp"
    if path.suffix in {".pyc", ".pyo"}:
        return path.suffix
    return None


def _reject_forbidden_transients(root: Path) -> None:
    forbidden = [
        (path, kind)
        for path in root.rglob("*")
        if (kind := _transient_kind(path, root)) is not None
    ]
    if forbidden:
        details = ", ".join(
            f"{path.relative_to(root).as_posix()} ({kind})"
            for path, kind in sorted(forbidden)
        )
        raise FinalizeError(f"Release contains forbidden transient paths: {details}")


def _is_designated_qa_report(path: Path, root: Path) -> bool:
    return path.parent == root and path.name in QA_REPORT_NAMES


def _checksum_files(root: Path) -> list[Path]:
    _reject_forbidden_transients(root)
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path != root / "CHECKSUMS.sha256"
        and not _is_designated_qa_report(path, root)
    )


def finalize(
    report_path: Path,
    *,
    output_root: Path | None = None,
    allow_incomplete: bool = False,
) -> dict[str, Any]:
    report_path = report_path.expanduser().resolve()
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalizeError(f"Could not read batch report {report_path}: {exc}") from exc
    if not isinstance(report, dict):
        raise FinalizeError("Batch report must contain a JSON object.")
    root = (
        output_root.expanduser().resolve()
        if output_root is not None
        else Path(str(report.get("output_dir", ""))).expanduser().resolve()
    )
    if not root.is_dir() or not report_path.is_relative_to(root):
        raise FinalizeError("Batch report and output root do not describe one release.")
    _reject_forbidden_transients(root)
    items = report.get("items")
    if not isinstance(items, list) or not items:
        raise FinalizeError("Batch report has no items.")

    completed: dict[str, list[tuple[int, Path]]] = {}
    failed_subjects: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise FinalizeError("Batch report contains a malformed item.")
        if item.get("status") != "completed":
            failed_subjects.append(str(item.get("subject_id")))
            continue
        collection = str(item.get("collection_id"))
        position = item.get("position")
        if not isinstance(position, int):
            raise FinalizeError("Completed batch item has no integer position.")
        png = _release_path(item.get("png"), root, f"{item.get('subject_id')} PNG")
        if not png.is_file():
            raise FinalizeError(f"Completed PNG is missing: {png}")
        completed.setdefault(collection, []).append((position, png))

    if not completed:
        raise FinalizeError("No completed plate is available for finalization.")
    if not allow_incomplete:
        if failed_subjects:
            raise FinalizeError(
                "Cannot finalize an incomplete batch: " + ", ".join(failed_subjects)
            )
        for collection, expected in EXPECTED_COUNTS.items():
            actual = len(completed.get(collection, []))
            if actual != expected:
                raise FinalizeError(
                    f"{collection} has {actual} completed plates; expected {expected}."
                )

    _reject_source_cache_inventory(items, root)

    contacts: list[Path] = []
    for collection in (UK_COLLECTION, US_COLLECTION):
        records = sorted(completed.get(collection, []))
        if not records:
            continue
        positions = [position for position, _path in records]
        if positions != list(range(1, len(positions) + 1)):
            raise FinalizeError(f"{collection} completed positions are not contiguous.")
        output = root / CONTACT_NAMES[collection]
        _build_contact_sheet([path for _position, path in records], output)
        contacts.append(output)

    finalization = {
        "schema_version": 1,
        "status": "pilot" if allow_incomplete else "complete-review-series",
        "generated_at": datetime.now(UTC).isoformat(),
        "batch_report": report_path.relative_to(root).as_posix(),
        "completed_plate_count": sum(len(records) for records in completed.values()),
        "completed_by_collection": {
            collection: len(records) for collection, records in sorted(completed.items())
        },
        "contact_sheets": [path.relative_to(root).as_posix() for path in contacts],
        "checksum_policy": CHECKSUM_POLICY,
    }
    _atomic_json(root / "FINALIZATION.json", finalization)

    checksum_lines = [
        f"{_sha256(path)}  {path.relative_to(root).as_posix()}"
        for path in _checksum_files(root)
    ]
    _atomic_text(root / "CHECKSUMS.sha256", "\n".join(checksum_lines) + "\n")
    return finalization


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--allow-incomplete", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = finalize(
            args.report,
            output_root=args.output_root,
            allow_incomplete=args.allow_incomplete,
        )
    except FinalizeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
