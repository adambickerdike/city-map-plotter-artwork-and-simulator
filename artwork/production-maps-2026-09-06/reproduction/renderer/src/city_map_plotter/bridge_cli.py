"""Command-line builder for standalone bridge elevation study plates."""

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


CANONICAL_FORMAT_ID = "a3-landscape"
CATALOG_ID = "bridge-plates-v1"
ARTIFACT_KIND = "standalone-bridge-elevation-study-series"
DEVELOPMENT_ARTIFACT_KIND = "bridge-dimension-schematic-preview-series"
DEFAULT_OUTPUT_DIR = Path("output/bridge-engineering-v1")

CatalogLoader = Callable[[Path | None], list[dict[str, Any]]]
PlateBuilder = Callable[[dict[str, Any], str | None], PlateArtwork]


def _bridge_api() -> tuple[CatalogLoader, PlateBuilder]:
    """Import the bridge renderer lazily so CLI help remains lightweight."""

    from .bridges import build_bridge_plate, load_bridge_catalog

    return load_bridge_catalog, build_bridge_plate


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
    loader, _ = _bridge_api()
    records = loader(catalog_file)
    if not records:
        raise MapPlotterError("The bridge catalog contains no subjects.")
    ids = [str(record.get("id", "")) for record in records]
    if any(not subject_id for subject_id in ids):
        raise MapPlotterError("Every bridge catalog record needs an ID.")
    duplicates = sorted(
        subject_id for subject_id in set(ids) if ids.count(subject_id) > 1
    )
    if duplicates:
        raise MapPlotterError(
            "Bridge catalog repeats subject IDs: " + ", ".join(duplicates) + "."
        )
    for record in records:
        format_id = record.get("format_id")
        if format_id != CANONICAL_FORMAT_ID:
            raise MapPlotterError(
                f"Bridge subject {record['id']!r} requests {format_id!r}; "
                f"this series is binding to {CANONICAL_FORMAT_ID!r} only."
            )
    return records


def _select_records(
    records: list[dict[str, Any]],
    *,
    build_all: bool,
    subject_ids: list[str] | None,
    include_schematics: bool = False,
) -> list[dict[str, Any]]:
    def release_eligible(record: dict[str, Any]) -> bool:
        fidelity = record.get("fidelity")
        return (
            isinstance(fidelity, dict)
            and fidelity.get("status") == "source-profile"
            and fidelity.get("release_eligible") is True
        )

    if build_all:
        selected = (
            records
            if include_schematics
            else [record for record in records if release_eligible(record)]
        )
        if not selected:
            raise MapPlotterError(
                "The bridge catalog contains no release-eligible source profiles."
            )
        return selected
    wanted = set(subject_ids or [])
    selected = [record for record in records if str(record["id"]) in wanted]
    missing = sorted(wanted - {str(record["id"]) for record in selected})
    if missing:
        raise MapPlotterError("Unknown bridge subject(s): " + ", ".join(missing) + ".")
    if not selected:
        raise MapPlotterError("Choose --all or at least one --subject ID.")
    schematic = [
        str(record["id"]) for record in selected if not release_eligible(record)
    ]
    if schematic and not include_schematics:
        raise MapPlotterError(
            "Bridge subject(s) are dimension-only schematic previews, not "
            "source-faithful artwork: "
            + ", ".join(schematic)
            + ". Use --include-schematics only for development review."
        )
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
        return [_replace_stage_paths(item, staging_root, final_root) for item in value]
    return copy.deepcopy(value)


def _finalize_written_outputs(
    outputs: dict[str, Any], staging_root: Path, final_root: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        manifest_path = Path(str(outputs["manifest"]["path"]))
    except (KeyError, TypeError) as exc:
        raise MapPlotterError(
            "Bridge renderer omitted its plot manifest path."
        ) from exc
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
            f"Bridge manifest {manifest_path.name} has no physical pen sequence."
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
    destination = staging_root / "bridge-contact-sheet.png"
    columns = min(3, max(1, math.ceil(math.sqrt(len(pngs)))))
    result = subprocess.run(
        [
            montage,
            *map(str, pngs),
            "-thumbnail",
            "594x420",
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
        "# Bridge plate pen-change guide",
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
    *,
    development_bundle: bool,
) -> Path:
    from .bridges import SOURCE_PROFILE_RECOGNITION_MINIMUM_DRAWING_FRACTION

    bundle_label = (
        "Development bridge schematic previews"
        if development_bundle
        else "Source-profile bridge elevation artwork"
    )
    lines = [
        f"# {bundle_label}",
        "",
        "These A3 landscape drawings are orthographic bridge elevation studies. "
        "Only records marked `source-profile` are fidelity-qualified. "
        "Dimension-only schematic previews are development artifacts and cannot "
        "pass release QA. They are not surveys, structural calculations, "
        "construction drawings, as-built records, or claims of affiliation with "
        "the depicted bridges, their owners, operators, designers, or authorities.",
        "",
        "Source-profile views retain equal horizontal and vertical axes. Their "
        "named horizontal recognition span occupies at least "
        f"{SOURCE_PROFILE_RECOGNITION_MINIMUM_DRAWING_FRACTION:.0%} of the "
        "drawing band; complete multi-span evidence remains pinned outside that "
        "focused artwork view.",
        "",
    ]
    if contact_sheet is not None:
        relative_contact = _relative_output(contact_sheet["path"], final_root)
        lines.extend([f"[Open the series contact sheet]({relative_contact})", ""])
    lines.extend(
        [
            "| Bridge | Kind | Fidelity | Evidence | Rights | Files |",
            "|---|---|---|---|---|---|",
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
        fidelity = entry.get("fidelity", {})
        fidelity_label = str(fidelity.get("status", "unclassified"))
        lines.append(
            f"| {entry['title']} (`{entry['id']}`) | {entry['subject_kind']} | "
            f"{fidelity_label} | {evidence_label} | {entry['rights_status']} | "
            f"{' / '.join(links)} |"
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
        path for path in output_dir.rglob("*") if path.is_file() and path != destination
    )
    destination.write_text(
        "".join(
            f"{_sha256(path)}  {path.relative_to(output_dir)}\n" for path in candidates
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
        "fidelity": copy.deepcopy(record.get("fidelity", {})),
        "rights_status": record.get("rights_status", artwork.rights_status),
        "claim_scope": copy.deepcopy(record.get("claim_scope", "")),
        "notes": copy.deepcopy(record.get("notes", [])),
        "pen_sequence": pen_sequence,
        "outputs": outputs,
    }


def build_series(args: argparse.Namespace) -> int:
    if not math.isfinite(args.dpi) or args.dpi <= 0:
        raise MapPlotterError("--dpi must be a positive finite number.")
    records = _load_records(args.catalog_file)
    selected = _select_records(
        records,
        build_all=bool(args.all),
        subject_ids=args.subject,
        include_schematics=bool(args.include_schematics),
    )
    development_bundle = any(
        not (
            isinstance(record.get("fidelity"), dict)
            and record["fidelity"].get("status") == "source-profile"
            and record["fidelity"].get("release_eligible") is True
        )
        for record in selected
    )
    _, builder = _bridge_api()
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
                    f"bridge elevation plates require {CANONICAL_FORMAT_ID!r}."
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
            entries.append(_entry_for(record, artwork, outputs, pen_sequence, position))
            print(f"[{position:02d}/{len(selected):02d}] staged {artwork.subject_id}")

        contact_sheet = _write_contact_sheet(staging_root, output_dir, staged_pngs)
        index_path = staging_root / "index.json"
        index_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog_id": CATALOG_ID,
                    "artifact_kind": (
                        DEVELOPMENT_ARTIFACT_KIND
                        if development_bundle
                        else ARTIFACT_KIND
                    ),
                    "fidelity_policy": (
                        "development-includes-dimension-schematics"
                        if development_bundle
                        else "release-source-profiles-only"
                    ),
                    "catalog_count": len(records),
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
        _write_artifact_catalog(
            staging_root,
            output_dir,
            entries,
            contact_sheet,
            development_bundle=development_bundle,
        )
        _write_checksums(staging_root)

        # Re-check immediately before the atomic same-filesystem promotion in
        # case another process populated the requested target during rendering.
        _assert_safe_target(output_dir)
        os.replace(staging_root, output_dir)

    print(f"Built {len(entries)} bridge plates in {output_dir}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapplot-bridges",
        description=(
            "List or build source-qualified standalone bridge elevation study plates."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="List the bridge catalog.")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--catalog-file", type=Path)

    build = commands.add_parser("build", help="Build canonical A3 landscape plates.")
    selection = build.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--subject", action="append")
    build.add_argument("--catalog-file", type=Path)
    build.add_argument(
        "--include-schematics",
        action="store_true",
        help=(
            "Development only: allow dimension-generated schematic previews. "
            "Bundles containing them cannot pass bridge release QA."
        ),
    )
    build.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    build.add_argument("--dpi", type=float, default=300.0)
    build.add_argument(
        "--no-png",
        action="store_true",
        help="Development only; the resulting bundle cannot pass bridge series review QA.",
    )
    build.add_argument(
        "--no-split-pens",
        action="store_true",
        help="Development only; the resulting bundle cannot pass bridge series review QA.",
    )
    return parser


def _list_records(args: argparse.Namespace) -> int:
    records = _load_records(args.catalog_file)
    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return 0
    for record in records:
        evidence = record.get("evidence")
        status = evidence.get("status", "-") if isinstance(evidence, dict) else "-"
        fidelity = record.get("fidelity")
        fidelity_status = (
            str(fidelity.get("status", "unclassified"))
            if isinstance(fidelity, dict)
            else "unclassified"
        )
        print(
            f"{record['id']:<28} {record['subject_kind']:<18} "
            f"{fidelity_status:<20} {status:<39} "
            f"{record['rights_status']:<27} {record['title']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "list":
            return _list_records(args)
        return build_series(args)
    except (MapPlotterError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"mapplot-bridges: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
