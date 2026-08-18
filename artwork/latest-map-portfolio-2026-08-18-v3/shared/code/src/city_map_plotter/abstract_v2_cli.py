"""Build the curated twenty-five-piece three-dimensional abstract collection."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import html
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from typing import Any, Sequence

from .abstract_art_v2 import (
    Abstract3DPiece,
    build_abstract_3d_artwork,
    geometry_sha256,
    load_abstract_3d_catalog,
)
from .models import MapPlotterError
from .niche_common import write_plate


SERIES_ID = "abstract-art-v2"
COLLECTION_TITLE = "Pressure Systems / 170"
TARGET_COLLECTION_SIZE = 25
DEFAULT_OUTPUT_DIR = Path("output/abstract-art-v2-review")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_paths(value: Any, stage: Path, final: Path) -> Any:
    if isinstance(value, dict):
        return {key: _replace_paths(item, stage, final) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_paths(item, stage, final) for item in value]
    if isinstance(value, str):
        stage_root = stage.resolve()
        stage_text = str(stage_root)
        if value == stage_text or value.startswith(stage_text + os.sep):
            return str(final.resolve() / Path(value).relative_to(stage_root))
    return value


def _rewrite_manifest(path: Path, stage: Path, final: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MapPlotterError(
            f"Could not finalize plot manifest {path}: {exc}"
        ) from exc
    manifest = _replace_paths(manifest, stage, final)
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def _selected_pieces(args: argparse.Namespace) -> list[Abstract3DPiece]:
    catalog = load_abstract_3d_catalog()
    if args.all:
        return list(catalog)
    requested = list(args.piece or ())
    if len(requested) != len(set(requested)):
        raise MapPlotterError("A piece ID cannot be repeated in one build.")
    by_id = {piece.id: piece for piece in catalog}
    unknown = sorted(set(requested) - set(by_id))
    if unknown:
        raise MapPlotterError(f"Unknown 3D piece(s): {', '.join(unknown)}.")
    return [by_id[piece_id] for piece_id in requested]


def _gallery_html(artifacts: Sequence[dict[str, Any]], generated_at: str) -> str:
    total = len(artifacts)
    cards: list[str] = []
    for artifact in artifacts:
        title = html.escape(str(artifact["title"]))
        subtitle = html.escape(str(artifact["subtitle"]))
        category = html.escape(str(artifact["category"]))
        svg = html.escape(str(artifact["svg"]), quote=True)
        manifest = html.escape(str(artifact["plot_manifest"]), quote=True)
        preview = artifact.get("png")
        visual = (
            f'<img src="{html.escape(str(preview), quote=True)}" alt="{title}">'
            if preview
            else f'<object data="{svg}" type="image/svg+xml" aria-label="{title}"></object>'
        )
        palette = "".join(
            f'<span class="pen">{html.escape(str(pen))}</span>'
            for pen in artifact["palette"]
        )
        visibility = artifact["visibility"]
        facts = (
            f"{int(visibility['object_count'])} OBJECTS / "
            f"{int(visibility['triangle_count']):,} TRIANGLES / "
            f"{int(visibility['occluded_sample_count']):,} HIDDEN SAMPLES"
        )
        cards.append(
            "<article>"
            f'<a class="visual" href="{svg}">{visual}</a>'
            '<div class="copy">'
            f'<div class="index">{int(artifact["catalog_index"]):02d} / {total:02d}</div>'
            f"<h2>{title}</h2><p>{subtitle}</p>"
            f'<div class="category">{category}</div>'
            f'<div class="facts">{facts}</div>'
            f'<div class="palette">{palette}</div>'
            f'<div class="links"><a href="{svg}">SVG MASTER</a><a href="{manifest}">PLOT MANIFEST</a></div>'
            "</div></article>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{COLLECTION_TITLE}</title>
<style>
:root{{--paper:#f2f0e8;--panel:#15171b;--line:#343941;--muted:#9aa0aa;--signal:#82aaff}}
*{{box-sizing:border-box}}body{{margin:0;background:#090a0c;color:#f2f2ef;font:14px/1.45 Inter,system-ui,sans-serif}}
header{{max-width:1680px;margin:auto;padding:72px 34px 42px;border-bottom:1px solid var(--line)}}
.eyebrow,.index,.category,.facts,.pen,.links{{font:11px/1.3 ui-monospace,SFMono-Regular,monospace;letter-spacing:.12em;text-transform:uppercase}}
.eyebrow,.index,.category{{color:var(--signal)}}h1{{font:600 clamp(42px,7vw,108px)/.86 Arial,sans-serif;letter-spacing:-.065em;text-transform:uppercase;max-width:1150px;margin:16px 0 30px}}
header p{{max-width:790px;color:#bbc0c8;font-size:16px}}main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:1px;background:var(--line)}}
article{{background:var(--panel);padding:20px}}.visual{{display:block;background:var(--paper);aspect-ratio:1/1.18;overflow:hidden}}
.visual img,.visual object{{display:block;width:100%;height:100%;object-fit:contain}}.copy{{padding:20px 3px 8px}}
h2{{font:600 30px/1 Arial,sans-serif;letter-spacing:-.035em;text-transform:uppercase;margin:9px 0 7px}}p{{color:#c3c6cc;margin:0 0 14px}}
.facts{{color:#747c87;margin-top:7px}}.palette{{display:flex;flex-wrap:wrap;gap:5px;margin-top:16px}}.pen{{border:1px solid #414751;padding:4px 7px;color:#aab0b8}}
.links{{display:flex;gap:18px;margin-top:18px}}a{{color:inherit}}.links a{{color:#d1d5db;text-decoration-color:#59616d}}
footer{{padding:40px 34px 76px;color:#747b84}}@media(max-width:520px){{main{{grid-template-columns:1fr}}header{{padding-top:44px}}}}
</style></head><body><header><div class="eyebrow">PS170 / THREE-DIMENSIONAL PEN PLOT COLLECTION</div>
<h1>{COLLECTION_TITLE}</h1><p>Project-authored triangle meshes, native 3D curves and perspective cameras reduced to depth-tested physical pen paths. The raster depth pass establishes visibility only; no preview pixels are traced into the SVG masters.</p></header>
<main>{"".join(cards)}</main><footer>Generated {html.escape(generated_at)} / review edition / validate, simulate and pilot-plot before production.</footer></body></html>
"""


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise MapPlotterError(f"Contact-sheet input {path} is not a PNG.")
    return struct.unpack(">II", header[16:24])


def _write_contact_sheet(
    stage: Path, artifacts: Sequence[dict[str, Any]]
) -> list[Path]:
    if not artifacts or any(not artifact.get("png") for artifact in artifacts):
        raise MapPlotterError("The contact sheet requires every selected PNG preview.")
    for artifact in artifacts:
        preview = stage / str(artifact["png"])
        if not preview.is_file():
            raise MapPlotterError(f"Missing contact-sheet preview {preview}.")
        _png_size(preview)

    columns = min(5, max(1, math.ceil(math.sqrt(len(artifacts)))))
    rows = math.ceil(len(artifacts) / columns)
    cell_width, cell_height = 430, 600
    preview_height = 520
    gap, margin, heading = 24, 36, 106
    page_width = margin * 2 + columns * cell_width + (columns - 1) * gap
    page_height = heading + margin + rows * cell_height + (rows - 1) * gap + margin
    chunks = [
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" ',
        f'width="{page_width}" height="{page_height}" viewBox="0 0 {page_width} {page_height}">',
        '<rect width="100%" height="100%" fill="#090a0c"/>',
        "<style>text{font-family:Arial,sans-serif;fill:#f2f2ef}.meta{font-family:monospace;fill:#82aaff;letter-spacing:2px}</style>",
        f'<text x="{margin}" y="45" font-size="30" font-weight="700">PRESSURE SYSTEMS / 170</text>',
        f'<text class="meta" x="{margin}" y="75" font-size="13">3D VECTOR COLLECTION / {len(artifacts)} SELECTED SCENES / REVIEW OUTPUT</text>',
    ]
    for index, artifact in enumerate(artifacts):
        column, row = index % columns, index // columns
        x = margin + column * (cell_width + gap)
        y = heading + row * (cell_height + gap)
        preview = html.escape(str(artifact["png"]), quote=True)
        title = html.escape(str(artifact["title"]))
        chunks.extend(
            (
                f'<rect x="{x}" y="{y}" width="{cell_width}" height="{cell_height}" fill="#15171b"/>',
                f'<rect x="{x + 10}" y="{y + 10}" width="{cell_width - 20}" height="{preview_height}" fill="#f2f0e8"/>',
                f'<image x="{x + 10}" y="{y + 10}" width="{cell_width - 20}" height="{preview_height}" preserveAspectRatio="xMidYMid meet" xlink:href="{preview}"/>',
                f'<text class="meta" x="{x + 14}" y="{y + 555}" font-size="12">{int(artifact["catalog_index"]):02d}</text>',
                f'<text x="{x + 52}" y="{y + 555}" font-size="18" font-weight="700">{title}</text>',
            )
        )
    chunks.append("</svg>")
    svg_path = stage / "collection-contact-sheet.svg"
    png_path = stage / "collection-contact-sheet.png"
    svg_path.write_text("".join(chunks), encoding="utf-8")
    inkscape = shutil.which("inkscape")
    if inkscape is None:
        raise MapPlotterError("Contact-sheet export requires Inkscape on PATH.")
    result = subprocess.run(
        [
            inkscape,
            str(svg_path),
            "--export-type=png",
            "--export-area-page",
            "--export-background=#090a0c",
            "--export-background-opacity=255",
            f"--export-filename={png_path}",
        ],
        cwd=stage,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise MapPlotterError(f"Contact-sheet export failed: {detail}.")
    _png_size(png_path)
    return [svg_path, png_path]


def _write_checksums(stage: Path) -> Path:
    checksum_path = stage / "CHECKSUMS.sha256"
    files = sorted(
        path for path in stage.iterdir() if path.is_file() and path != checksum_path
    )
    checksum_path.write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in files),
        encoding="utf-8",
    )
    return checksum_path


def _build(args: argparse.Namespace) -> int:
    if not math.isfinite(args.dpi) or args.dpi <= 0:
        raise MapPlotterError("--dpi must be a positive finite number.")
    pieces = _selected_pieces(args)
    output_dir = Path(args.output_dir).absolute()
    if output_dir.exists():
        raise MapPlotterError(
            f"Output directory {output_dir} already exists; choose a new path so no artwork is overwritten."
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.stage-", dir=output_dir.parent)
    )
    generated_at = args.generated_at or datetime.now(UTC).isoformat()
    artifacts: list[dict[str, Any]] = []
    try:
        for position, piece in enumerate(pieces, start=1):
            print(f"[{position:02d}/{len(pieces):02d}] {piece.title}", flush=True)
            artwork = build_abstract_3d_artwork(piece.id)
            outputs = write_plate(
                artwork,
                stage,
                png=not args.no_png,
                png_dpi=args.dpi,
                split_pens=not args.no_split_pens,
                generated_at=generated_at,
            )
            manifest_path = Path(str(outputs["manifest"]["path"]))
            manifest = _rewrite_manifest(manifest_path, stage, output_dir)
            metadata = manifest["rendering"]["abstract_3d"]
            pen_files = manifest["outputs"]["pen_files"]
            artifacts.append(
                {
                    "subject_id": piece.id,
                    "catalog_index": piece.catalog_index,
                    "title": piece.title,
                    "subtitle": piece.subtitle,
                    "scene": piece.scene,
                    "category": piece.category,
                    "composition": piece.composition,
                    "format_id": piece.format_id,
                    "palette": list(piece.palette),
                    "svg": f"{artwork.artifact_id}.svg",
                    "plot_manifest": f"{artwork.artifact_id}.plot.json",
                    "png": f"{artwork.artifact_id}.png" if "png" in outputs else None,
                    "split_pen_files": [
                        Path(str(record["path"])).name for record in pen_files
                    ],
                    "physical_pen_steps": manifest["plot_summary"][
                        "physical_pen_steps"
                    ],
                    "path_count": manifest["plot_summary"]["pen_down_path_count"],
                    "estimated_plot_seconds": manifest["plot_summary"][
                        "estimated_plot_seconds_including_pen_up"
                    ],
                    "field_ink_coverage": manifest["plot_summary"][
                        "field_ink_coverage_upper_bound"
                    ],
                    "visibility": metadata["visibility"],
                    "scene_sha256": metadata["scene_sha256"],
                    "geometry_sha256": geometry_sha256(artwork),
                    "svg_sha256": _sha256(stage / f"{artwork.artifact_id}.svg"),
                    "manifest_sha256": _sha256(manifest_path),
                }
            )

        gallery_path = stage / "index.html"
        gallery_path.write_text(
            _gallery_html(artifacts, generated_at), encoding="utf-8"
        )
        supplementary = [
            {
                "kind": "browser-gallery",
                "path": gallery_path.name,
                "sha256": _sha256(gallery_path),
            }
        ]
        if not args.no_png:
            for path in _write_contact_sheet(stage, artifacts):
                supplementary.append(
                    {
                        "kind": (
                            "collection-contact-sheet-svg"
                            if path.suffix == ".svg"
                            else "collection-contact-sheet-png"
                        ),
                        "path": path.name,
                        "sha256": _sha256(path),
                    }
                )

        catalog = load_abstract_3d_catalog()
        catalog_ids = [piece.id for piece in catalog]
        selected_ids = [str(artifact["subject_id"]) for artifact in artifacts]
        series = {
            "schema_version": 1,
            "series_id": SERIES_ID,
            "collection_title": COLLECTION_TITLE,
            "generated_at": generated_at,
            "catalog_size": len(catalog),
            "target_collection_size": TARGET_COLLECTION_SIZE,
            "artifact_count": len(artifacts),
            "complete_catalog": selected_ids == catalog_ids,
            "catalog_status": "curated-25-scene-review",
            "renderer": "perspective-depth-buffer-v2",
            "production_ready": False,
            "production_blockers": [
                "the exact pens, stock and plot speed require calibration and physical pilot approval",
                "each promoted composition requires final visual review at true paper scale",
            ],
            "workflow_document": "docs/abstract-art/ABSTRACT_3D_V2.md",
            "checksums": "CHECKSUMS.sha256",
            "supplementary_artifacts": supplementary,
            "artifacts": artifacts,
        }
        (stage / "abstract-art-v2-series.json").write_text(
            json.dumps(series, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _write_checksums(stage)
        os.replace(stage, output_dir)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    print(f"Built {len(artifacts)} 3D abstract plate(s) in {output_dir}.")
    print("CURATION REVIEW — validate, simulate and pilot-plot before production.")
    return 0


def _list(args: argparse.Namespace) -> int:
    pieces = load_abstract_3d_catalog()
    if args.json:
        print(
            json.dumps(
                [
                    {"catalog_index": piece.catalog_index, **piece.as_dict()}
                    for piece in pieces
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        total = len(pieces)
        for piece in pieces:
            print(
                f"{piece.catalog_index:02d}/{total:02d}  {piece.id:<19} "
                f"{piece.title} / {piece.format_id} / {piece.category}"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapplot-abstract-v2",
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List the curated v2 collection.")
    listing.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    build = commands.add_parser(
        "build",
        help="Build selected scenes into a new atomic review directory.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    selection = build.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true", help="Build the full catalog.")
    selection.add_argument(
        "--piece",
        action="append",
        help="Piece ID; repeat to build a selected set.",
    )
    build.add_argument(
        "--output-dir", "--out", dest="output_dir", default=DEFAULT_OUTPUT_DIR
    )
    build.add_argument(
        "--no-png",
        action="store_true",
        help="Skip PNG previews and the PNG-dependent contact sheet.",
    )
    build.add_argument(
        "--no-split-pens", action="store_true", help="Skip one-file-per-pen jobs."
    )
    build.add_argument("--dpi", type=float, default=180.0)
    build.add_argument(
        "--generated-at", help="Fixed ISO timestamp for repeatable manifests."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
        if args.command == "list":
            return _list(args)
        if args.command == "build":
            return _build(args)
    except MapPlotterError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
