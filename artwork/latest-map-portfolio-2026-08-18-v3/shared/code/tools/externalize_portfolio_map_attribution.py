#!/usr/bin/env python3
"""Move OpenStreetMap credit off review plates and into companion documents.

This tool is deliberately a *presentation-copy* transform.  It removes only
vector paths whose explicit ``data-role="attribution"`` / ``data-copy`` pair
names OpenStreetMap or OSM.  Source metadata, licences, URLs, and factual
provenance remain intact.  The paired PNG is then rasterised from the changed
SVG, and the copied plot manifest records both the original hashes and the
scope of the transform.

The transformed masters are not silently promoted as machine-ready jobs.  The
manifest states that pen-separated files and exact machine metrics must be
regenerated from a renderer configured for external attribution before plotting.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
from typing import Any, Sequence


TRANSFORM_ID = "externalize-open-map-attribution-v1"
APPLIED_AT = "2026-08-16T00:00:00Z"
DEFAULT_EXTERNAL_PLACEMENT = "Companion portfolio ATTRIBUTION.md"

_PATH_TAG = re.compile(r"[ \t]*<path\b[^>]*?/>[ \t]*(?:\r?\n)?", re.IGNORECASE)
_DRAWN_TAG = re.compile(r"<(?:path|text|tspan)\b[^>]*>", re.IGNORECASE)
_DATA_COPY = re.compile(r'\bdata-copy="([^"]*)"', re.IGNORECASE)
_DATA_ROLE = re.compile(r'\bdata-role="([^"]*)"', re.IGNORECASE)
_PHYSICAL_GROUP = re.compile(r'<g\b[^>]*\bid="(layer-pen-[^"]+)"[^>]*>', re.IGNORECASE)
_EMPTY_ATTRIBUTION_GROUP = re.compile(
    r'[ \t]*<g\b[^>]*\bid="logical-plate_attribution"[^>]*>\s*</g>[ \t]*(?:\r?\n)?',
    re.IGNORECASE,
)
_OPEN_MAP_REFERENCE = re.compile(
    r"(?:open\s*street\s*map|open\s+street\s+map|\bosm\b)", re.IGNORECASE
)


class AttributionTransformError(RuntimeError):
    """Raised when a presentation-copy transform cannot be proven safe."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_value(tag: str) -> str | None:
    match = _DATA_COPY.search(tag)
    return html.unescape(match.group(1)) if match else None


def _is_drawn_open_map_attribution(tag: str) -> bool:
    role = _DATA_ROLE.search(tag)
    copy = _copy_value(tag)
    return bool(
        role
        and role.group(1).casefold() == "attribution"
        and copy
        and _OPEN_MAP_REFERENCE.search(copy)
    )


def visible_open_map_copies(svg_text: str) -> list[str]:
    """Return distinct OpenStreetMap/OSM copy attached to drawn SVG elements."""

    return sorted(
        {
            copy
            for tag in _DRAWN_TAG.findall(svg_text)
            if _is_drawn_open_map_attribution(tag)
            if (copy := _copy_value(tag)) is not None
        },
        key=str.casefold,
    )


def visible_attribution_copies(svg_text: str) -> list[str]:
    """Return all distinct copy strings still attached to attribution paths."""

    return sorted(
        {
            copy
            for tag in _DRAWN_TAG.findall(svg_text)
            if (role := _DATA_ROLE.search(tag)) is not None
            and role.group(1).casefold() == "attribution"
            if (copy := _copy_value(tag)) is not None
        },
        key=str.casefold,
    )


def _physical_group_for_position(svg_text: str, position: int) -> str | None:
    group_id: str | None = None
    for match in _PHYSICAL_GROUP.finditer(svg_text, 0, position):
        group_id = match.group(1)
    return group_id


def _remove_open_map_paths(svg_text: str) -> tuple[str, list[str], dict[str, int]]:
    removals: list[tuple[int, int, str]] = []
    group_counts: dict[str, int] = {}
    copies: list[str] = []
    for match in _PATH_TAG.finditer(svg_text):
        tag = match.group(0)
        if not _is_drawn_open_map_attribution(tag):
            continue
        copy = _copy_value(tag)
        if copy is None:
            raise AttributionTransformError("Attribution path has no data-copy value.")
        group_id = _physical_group_for_position(svg_text, match.start())
        if group_id is None:
            raise AttributionTransformError(
                "Open-map attribution path is outside a physical pen group."
            )
        removals.append((match.start(), match.end(), copy))
        copies.append(copy)
        group_counts[group_id] = group_counts.get(group_id, 0) + 1

    transformed = svg_text
    for start, end, _ in reversed(removals):
        transformed = transformed[:start] + transformed[end:]
    transformed = _EMPTY_ATTRIBUTION_GROUP.sub("", transformed)

    # Keep each physical-layer title truthful about its displayed path count.
    for group_id, removed_count in group_counts.items():
        title_pattern = re.compile(
            rf'(<g\b[^>]*\bid="{re.escape(group_id)}"[^>]*>\s*'
            rf'<title>[^<]*?plot\s+)(\d+)(\s+paths?\.</title>)',
            re.IGNORECASE,
        )

        def decrement(match: re.Match[str]) -> str:
            updated = int(match.group(2)) - removed_count
            if updated < 0:
                raise AttributionTransformError(
                    f"{group_id}: removed path count exceeds layer title count."
                )
            return match.group(1) + str(updated) + match.group(3)

        transformed, substitutions = title_pattern.subn(decrement, transformed, count=1)
        if substitutions != 1:
            raise AttributionTransformError(
                f"Could not update physical-layer title for {group_id}."
            )
    return transformed, copies, group_counts


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise AttributionTransformError(f"Not a PNG file: {path}")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise AttributionTransformError(f"Invalid PNG dimensions: {path}")
    return width, height


def _rasterise_matching_png(svg: Path, png: Path) -> None:
    width, height = _png_dimensions(png)
    inkscape = shutil.which("inkscape")
    if inkscape is None:
        raise AttributionTransformError("Inkscape is required to refresh portfolio PNGs.")
    with tempfile.NamedTemporaryFile(
        prefix=f".{png.stem}.", suffix=".png", dir=png.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        command = [
            inkscape,
            str(svg),
            "--export-type=png",
            f"--export-filename={temporary}",
            f"--export-width={width}",
            f"--export-height={height}",
            "--export-background=#ffffff",
            "--export-background-opacity=255",
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise AttributionTransformError(
                f"Inkscape failed for {svg}:\n{(result.stderr or result.stdout).strip()}"
            )
        if _png_dimensions(temporary) != (width, height):
            raise AttributionTransformError(
                f"Raster dimensions changed for {svg}: expected {width}x{height}."
            )
        temporary.replace(png)
    finally:
        temporary.unlink(missing_ok=True)


def _update_manifest(
    manifest_path: Path,
    *,
    original_hashes: dict[str, str],
    svg_hash: str,
    png_hash: str,
    removed_copies: Sequence[str],
    group_counts: dict[str, int],
    remaining_attribution_copies: Sequence[str],
    external_placement: str,
) -> None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AttributionTransformError(f"Invalid plot manifest {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise AttributionTransformError(f"Plot manifest is not an object: {manifest_path}")

    layers = manifest.get("layers", [])
    if not isinstance(layers, list):
        raise AttributionTransformError(f"Manifest layers are not a list: {manifest_path}")
    matched_groups: set[str] = set()
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        group_id = layer.get("svg_group_id")
        if group_id not in group_counts:
            continue
        matched_groups.add(group_id)
        old_count = int(layer.get("path_count", 0))
        new_count = old_count - group_counts[group_id]
        if new_count < 0:
            raise AttributionTransformError(
                f"{manifest_path}: removed count exceeds {group_id} manifest count."
            )
        layer["path_count"] = new_count
        if "plate_attribution" in layer.get("logical_layers", []):
            if not remaining_attribution_copies:
                layer["logical_layers"] = [
                    value
                    for value in layer["logical_layers"]
                    if value != "plate_attribution"
                ]
    if matched_groups != set(group_counts):
        missing = sorted(set(group_counts) - matched_groups)
        raise AttributionTransformError(
            f"{manifest_path}: no manifest layer for SVG groups {missing}."
        )

    rendering = manifest.setdefault("rendering", {})
    if not isinstance(rendering, dict):
        raise AttributionTransformError(f"Manifest rendering is not an object: {manifest_path}")
    rendering["visible_attribution"] = bool(remaining_attribution_copies)
    rendering["openstreetmap_attribution_mode"] = "external"
    rendering["on_page_openstreetmap_reference"] = False
    rendering["external_openstreetmap_attribution_placement"] = external_placement

    plot_summary = manifest.setdefault("plot_summary", {})
    if isinstance(plot_summary, dict):
        source_path_count = plot_summary.get("pen_down_path_count")
        if isinstance(source_path_count, int):
            plot_summary["pen_down_path_count"] = source_path_count - sum(group_counts.values())
        plot_summary["presentation_transform_metrics_status"] = (
            "path count updated; distances/timing remain source-release metrics and "
            "must be regenerated before plotting"
        )

    readiness = manifest.setdefault("production_readiness", {})
    if isinstance(readiness, dict):
        readiness["production_ready"] = False
        blockers = readiness.setdefault("blocking_reasons", [])
        blocker = (
            "portfolio presentation attribution transform requires regenerated "
            "pen jobs and machine metrics before plotting"
        )
        if isinstance(blockers, list) and blocker not in blockers:
            blockers.append(blocker)

    outputs = manifest.get("outputs")
    if isinstance(outputs, dict):
        if isinstance(outputs.get("svg"), dict):
            outputs["svg"]["sha256"] = svg_hash
        if isinstance(outputs.get("png"), dict):
            outputs["png"]["sha256"] = png_hash
        outputs["pen_files_status"] = (
            "source-release files not copied; regenerate after external attribution configuration"
        )

    manifest["presentation_transform"] = {
        "schema_version": 1,
        "id": TRANSFORM_ID,
        "applied_at": APPLIED_AT,
        "scope": "portfolio-review-display-copy",
        "original_artifact_sha256": original_hashes,
        "removed_visible_path_count": sum(group_counts.values()),
        "removed_visible_copy_values": sorted(set(removed_copies), key=str.casefold),
        "remaining_visible_attribution_copy_values": list(remaining_attribution_copies),
        "external_attribution_placement": external_placement,
        "source_provenance_retained": True,
        "source_licence_metadata_retained": True,
        "pen_files_regenerated": False,
        "machine_metrics_regenerated": False,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def externalize_triplet(
    svg: Path,
    png: Path,
    manifest: Path,
    *,
    external_placement: str = DEFAULT_EXTERNAL_PLACEMENT,
) -> dict[str, Any] | None:
    """Transform a copied SVG/PNG/manifest triplet, or return ``None`` if clean."""

    for path in (svg, png, manifest):
        if path.is_symlink() or not path.is_file():
            raise AttributionTransformError(f"Required regular file is missing: {path}")
    original_text = svg.read_text(encoding="utf-8")
    original_copies = visible_open_map_copies(original_text)
    if not original_copies:
        return None
    original_hashes = {
        "svg": _sha256(svg),
        "png": _sha256(png),
        "manifest": _sha256(manifest),
    }
    transformed, removed_copies, group_counts = _remove_open_map_paths(original_text)
    if visible_open_map_copies(transformed):
        raise AttributionTransformError(f"Visible open-map copy remains in {svg}.")
    remaining_copies = visible_attribution_copies(transformed)
    svg.write_text(transformed, encoding="utf-8")
    _rasterise_matching_png(svg, png)
    svg_hash = _sha256(svg)
    png_hash = _sha256(png)
    _update_manifest(
        manifest,
        original_hashes=original_hashes,
        svg_hash=svg_hash,
        png_hash=png_hash,
        removed_copies=removed_copies,
        group_counts=group_counts,
        remaining_attribution_copies=remaining_copies,
        external_placement=external_placement,
    )
    return {
        "svg": str(svg),
        "removed_path_count": len(removed_copies),
        "removed_copy_values": sorted(set(removed_copies), key=str.casefold),
        "remaining_attribution_copy_values": remaining_copies,
        "original_sha256": original_hashes,
        "transformed_sha256": {
            "svg": svg_hash,
            "png": png_hash,
            "manifest": _sha256(manifest),
        },
    }


def audit_svgs(paths: Sequence[Path]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    for path in paths:
        copies = visible_open_map_copies(path.read_text(encoding="utf-8"))
        if copies:
            failures.append({"svg": str(path), "visible_copy_values": copies})
    return {
        "schema_version": 1,
        "audit": "drawn-open-map-attribution",
        "inspected_svg_count": len(paths),
        "failure_count": len(failures),
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "scope_note": (
            "Source/licence metadata is intentionally retained; this audit covers "
            "copy attached to visible SVG path/text elements."
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("svg", type=Path)
    parser.add_argument("png", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--external-placement", default=DEFAULT_EXTERNAL_PLACEMENT
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = externalize_triplet(
            args.svg,
            args.png,
            args.manifest,
            external_placement=args.external_placement,
        )
    except (OSError, AttributionTransformError) as exc:
        print(f"externalize_portfolio_map_attribution: {exc}")
        return 2
    print(json.dumps(result or {"status": "already-external"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
