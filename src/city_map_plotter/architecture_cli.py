"""Command-line builder for standalone architecture study plates."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .models import MapPlotterError
from .niche_common import PlateArtwork, write_plate


CANONICAL_FORMAT_ID = "a3-portrait"
DEFAULT_OUTPUT_DIR = Path("output/architecture-technical-v2.1")

CatalogLoader = Callable[[Path | None], list[dict[str, Any]]]
PlateBuilder = Callable[[dict[str, Any], str | None], PlateArtwork]


def _architecture_api() -> tuple[CatalogLoader, PlateBuilder]:
    """Import the architecture renderer lazily so CLI help remains lightweight."""

    from .architecture import build_architecture_plate, load_architecture_catalog

    return load_architecture_catalog, build_architecture_plate


def _absolute_path(path: Path) -> Path:
    """Return a lexical absolute path without following a target symlink."""

    return Path(os.path.abspath(path))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_records(catalog_file: Path | None) -> list[dict[str, Any]]:
    loader, _ = _architecture_api()
    records = loader(catalog_file)
    if not records:
        raise MapPlotterError("The architecture catalog contains no subjects.")
    ids = [str(record.get("id", "")) for record in records]
    if any(not subject_id for subject_id in ids):
        raise MapPlotterError("Every architecture catalog record needs an ID.")
    duplicates = sorted(
        subject_id for subject_id in set(ids) if ids.count(subject_id) > 1
    )
    if duplicates:
        raise MapPlotterError(
            "Architecture catalog repeats subject IDs: " + ", ".join(duplicates) + "."
        )
    for record in records:
        format_id = record.get("format_id")
        if format_id != CANONICAL_FORMAT_ID:
            raise MapPlotterError(
                f"Architecture subject {record['id']!r} requests {format_id!r}; "
                f"this series is binding to {CANONICAL_FORMAT_ID!r} only."
            )
    return records


def _select_records(
    records: list[dict[str, Any]], *, build_all: bool, subject_ids: list[str] | None
) -> list[dict[str, Any]]:
    if build_all:
        return records
    wanted = set(subject_ids or [])
    selected = [record for record in records if str(record["id"]) in wanted]
    missing = sorted(wanted - {str(record["id"]) for record in selected})
    if missing:
        raise MapPlotterError(
            "Unknown architecture subject(s): " + ", ".join(missing) + "."
        )
    if not selected:
        raise MapPlotterError("Choose --all or at least one --subject ID.")
    return selected


def _assert_safe_target(target: Path) -> None:
    if target.is_symlink():
        raise MapPlotterError(
            f"Output directory {target} is a symlink; choose a real directory."
        )
    if not target.exists():
        return
    if not target.is_dir():
        raise MapPlotterError(f"Output target {target} exists and is not a directory.")
    if any(target.iterdir()):
        raise MapPlotterError(
            f"Output directory {target} already exists and is not empty; "
            "choose a new or empty directory."
        )


def _replace_stage_paths(value: Any, staging_root: Path, final_root: Path) -> Any:
    """Deep-copy an output structure, replacing only staged ``path`` values."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "path" and isinstance(item, str):
                source = Path(item)
                try:
                    relative = source.relative_to(staging_root)
                except ValueError as exc:
                    raise MapPlotterError(
                        f"Renderer returned an output outside its staging directory: {source}."
                    ) from exc
                result[key] = str((final_root / relative).absolute())
            else:
                result[key] = _replace_stage_paths(item, staging_root, final_root)
        return result
    if isinstance(value, list):
        return [
            _replace_stage_paths(item, staging_root, final_root) for item in value
        ]
    return copy.deepcopy(value)


def _finalize_written_outputs(
    outputs: dict[str, Any], staging_root: Path, final_root: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        manifest_path = Path(str(outputs["manifest"]["path"]))
    except (KeyError, TypeError) as exc:
        raise MapPlotterError("Architecture renderer omitted its plot manifest path.") from exc
    try:
        manifest_path.relative_to(staging_root)
    except ValueError as exc:
        raise MapPlotterError(
            f"Renderer returned a manifest outside its staging directory: {manifest_path}."
        ) from exc
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sequence = manifest.get("pen_sequence")
    if not isinstance(sequence, list) or not sequence:
        raise MapPlotterError(
            f"Architecture manifest {manifest_path.name} has no physical pen sequence."
        )
    manifest["outputs"] = _replace_stage_paths(
        manifest.get("outputs", outputs), staging_root, final_root
    )
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    finalized = _replace_stage_paths(outputs, staging_root, final_root)
    finalized["manifest"]["sha256"] = _sha256(manifest_path)
    return finalized, copy.deepcopy(sequence)


def _relative_output(path: str, output_dir: Path) -> Path:
    try:
        return Path(path).relative_to(output_dir)
    except ValueError as exc:
        raise MapPlotterError(
            f"Artifact path {path!r} is outside the requested output directory."
        ) from exc


def _write_contact_sheet(
    staging_root: Path, final_root: Path, pngs: list[Path]
) -> dict[str, Any] | None:
    montage = shutil.which("montage")
    if montage is None or not pngs:
        return None
    destination = staging_root / "architecture-contact-sheet.png"
    columns = min(3, max(1, math.ceil(math.sqrt(len(pngs)))))
    result = subprocess.run(
        [
            montage,
            *map(str, pngs),
            "-thumbnail",
            "420x594",
            "-tile",
            f"{columns}x",
            "-geometry",
            "+18+18",
            "-background",
            "white",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not destination.is_file():
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise MapPlotterError(f"ImageMagick contact-sheet build failed{suffix}.")
    return {
        "path": str((final_root / destination.name).absolute()),
        "sha256": _sha256(destination),
    }


def _write_pen_guide(output_dir: Path, entries: list[dict[str, Any]]) -> Path:
    lines = [
        "# Architecture plate pen-change guide",
        "",
        "Each master SVG is ordered by physical pen. Plot the numbered Inkscape "
        "layers in order, or use the matching `.pen-NN-<pen-id>.svg` jobs when "
        "the build includes split-pen files.",
        "",
        "Calibrate the exact pens, paper stock, speed, and pressure before a "
        "production run.",
        "",
    ]
    for entry in entries:
        lines.extend([f"## {entry['title']}", ""])
        for step in entry["pen_sequence"]:
            lines.append(
                f"{step['step']}. **{step['pen']}** (`{step['pen_id']}`) — "
                f"{step['path_count']} paths, "
                f"{float(step['pen_down_distance_mm']):.1f} mm pen-down."
            )
        lines.append("")
    path = output_dir / "PEN-CHANGE-GUIDE.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_artifact_catalog(
    output_dir: Path,
    final_root: Path,
    entries: list[dict[str, Any]],
    contact_sheet: dict[str, Any] | None,
) -> Path:
    lines = [
        "# Standalone architecture study plates",
        "",
        "These A3 portrait drawings are source-qualified architectural studies. "
        "They are not surveys, construction drawings, as-built records, or claims "
        "of affiliation with the depicted venues or institutions.",
        "",
    ]
    if contact_sheet is not None:
        relative_contact = _relative_output(contact_sheet["path"], final_root)
        lines.extend([f"[Open the series contact sheet]({relative_contact})", ""])
    lines.extend(
        [
            "| Subject | Kind | Evidence | Rights | Files |",
            "|---|---|---|---|---|",
        ]
    )
    for entry in entries:
        outputs = entry["outputs"]
        links = [
            f"[SVG]({_relative_output(outputs['svg']['path'], final_root)})",
            f"[manifest]({_relative_output(outputs['manifest']['path'], final_root)})",
        ]
        if "png" in outputs:
            links.insert(
                1,
                f"[PNG]({_relative_output(outputs['png']['path'], final_root)})",
            )
        evidence = entry["evidence"]
        evidence_label = str(evidence.get("statement") or evidence.get("status") or "-")
        lines.append(
            f"| {entry['title']} (`{entry['id']}`) | {entry['subject_kind']} | "
            f"{evidence_label} | {entry['rights_status']} | {' / '.join(links)} |"
        )
    lines.extend(
        [
            "",
            "## Operator files",
            "",
            "- [Pen-change guide](PEN-CHANGE-GUIDE.md)",
            "- [Machine-readable index](index.json)",
            "- [SHA-256 checksums](CHECKSUMS.sha256)",
            "",
        ]
    )
    path = output_dir / "ARTIFACTS.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_checksums(output_dir: Path) -> Path:
    destination = output_dir / "CHECKSUMS.sha256"
    candidates = sorted(
        path
        for path in output_dir.rglob("*")
        if path.is_file() and path != destination
    )
    destination.write_text(
        "".join(
            f"{_sha256(path)}  {path.relative_to(output_dir)}\n"
            for path in candidates
        ),
        encoding="ascii",
    )
    return destination


def _entry_for(
    record: dict[str, Any],
    artwork: PlateArtwork,
    outputs: dict[str, Any],
    pen_sequence: list[dict[str, Any]],
    position: int,
) -> dict[str, Any]:
    return {
        "position": position,
        "id": artwork.subject_id,
        "title": artwork.title,
        "subtitle": artwork.subtitle,
        "subject_kind": record.get("subject_kind", artwork.subject_kind),
        "format_id": artwork.context.format_id,
        "location": copy.deepcopy(record.get("location", {})),
        "sources": copy.deepcopy(record.get("sources", [])),
        "evidence": copy.deepcopy(record.get("evidence", {})),
        "rights_status": record.get("rights_status", artwork.rights_status),
        "notes": copy.deepcopy(record.get("notes", [])),
        "pen_sequence": pen_sequence,
        "outputs": outputs,
    }


def build_series(args: argparse.Namespace) -> int:
    if not math.isfinite(args.dpi) or args.dpi <= 0:
        raise MapPlotterError("--dpi must be a positive finite number.")
    records = _load_records(args.catalog_file)
    selected = _select_records(
        records, build_all=bool(args.all), subject_ids=args.subject
    )
    _, builder = _architecture_api()
    output_dir = _absolute_path(args.output_dir)
    _assert_safe_target(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC).isoformat()

    prefix = f".{output_dir.name}.staging-"
    with tempfile.TemporaryDirectory(prefix=prefix, dir=output_dir.parent) as temporary:
        staging_root = Path(temporary)
        plates_dir = staging_root / "plates"
        entries: list[dict[str, Any]] = []
        staged_pngs: list[Path] = []
        for position, record in enumerate(selected, start=1):
            artwork = builder(record, CANONICAL_FORMAT_ID)
            if artwork.subject_id != record["id"]:
                raise MapPlotterError(
                    f"Renderer returned subject {artwork.subject_id!r} for "
                    f"catalog record {record['id']!r}."
                )
            if artwork.context.format_id != CANONICAL_FORMAT_ID:
                raise MapPlotterError(
                    f"Renderer returned {artwork.context.format_id!r}; standalone "
                    f"architecture plates require {CANONICAL_FORMAT_ID!r}."
                )
            staged_outputs = write_plate(
                artwork,
                plates_dir,
                png=not args.no_png,
                png_dpi=args.dpi,
                split_pens=not args.no_split_pens,
                generated_at=generated_at,
            )
            if "png" in staged_outputs:
                staged_pngs.append(Path(staged_outputs["png"]["path"]))
            outputs, pen_sequence = _finalize_written_outputs(
                staged_outputs, staging_root, output_dir
            )
            entries.append(
                _entry_for(record, artwork, outputs, pen_sequence, position)
            )
            print(f"[{position:02d}/{len(selected):02d}] staged {artwork.subject_id}")

        contact_sheet = _write_contact_sheet(
            staging_root, output_dir, staged_pngs
        )
        index_path = staging_root / "index.json"
        index_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog_id": "architecture-plates-v1",
                    "artifact_kind": "standalone-architecture-study-series",
                    "generated_at": generated_at,
                    "format_id": CANONICAL_FORMAT_ID,
                    "count": len(entries),
                    "contact_sheet": contact_sheet,
                    "entries": entries,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        _write_pen_guide(staging_root, entries)
        _write_artifact_catalog(staging_root, output_dir, entries, contact_sheet)
        _write_checksums(staging_root)

        # Re-check immediately before the atomic same-filesystem promotion in
        # case another process populated the requested target during rendering.
        _assert_safe_target(output_dir)
        os.replace(staging_root, output_dir)

    print(f"Built {len(entries)} architecture plates in {output_dir}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapplot-architecture",
        description=(
            "List or build source-qualified standalone architecture study plates."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="List the architecture catalog.")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--catalog-file", type=Path)

    build = commands.add_parser("build", help="Build canonical A3 portrait plates.")
    selection = build.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--subject", action="append")
    build.add_argument("--catalog-file", type=Path)
    build.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    build.add_argument("--dpi", type=float, default=300.0)
    build.add_argument("--no-png", action="store_true")
    build.add_argument("--no-split-pens", action="store_true")
    return parser


def _list_records(args: argparse.Namespace) -> int:
    records = _load_records(args.catalog_file)
    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return 0
    for record in records:
        evidence = record.get("evidence")
        status = evidence.get("status", "-") if isinstance(evidence, dict) else "-"
        print(
            f"{record['id']:<28} {record['subject_kind']:<12} "
            f"{status:<39} {record['rights_status']:<27} {record['title']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "list":
            return _list_records(args)
        return build_series(args)
    except (MapPlotterError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"mapplot-architecture: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
