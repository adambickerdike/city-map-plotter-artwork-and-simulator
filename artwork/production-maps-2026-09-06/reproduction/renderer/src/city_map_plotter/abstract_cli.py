"""Build the pinned 25-piece abstract pen-art collection."""

from __future__ import annotations

import argparse
import base64
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

from .abstract_art import (
    build_abstract_artwork,
    geometry_sha256,
    load_abstract_catalog,
)
from .models import MapPlotterError
from .niche_common import write_plate


DEFAULT_OUTPUT_DIR = Path("output/abstract-art-v1-review")


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
        stage_text = str(stage.resolve())
        if value == stage_text or value.startswith(stage_text + os.sep):
            relative = Path(value).relative_to(stage.resolve())
            return str(final.resolve() / relative)
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


def _gallery_html(artifacts: Sequence[dict[str, Any]], generated_at: str) -> str:
    cards: list[str] = []
    for artifact in artifacts:
        title = html.escape(str(artifact["title"]))
        subtitle = html.escape(str(artifact["subtitle"]))
        algorithm = html.escape(str(artifact["algorithm"]))
        svg = html.escape(str(artifact["svg"]), quote=True)
        image = artifact.get("png")
        visual = (
            f'<img src="{html.escape(str(image), quote=True)}" alt="{title}">'
            if image
            else f'<object data="{svg}" type="image/svg+xml" aria-label="{title}"></object>'
        )
        palette = "".join(
            f'<span class="pen">{html.escape(str(pen))}</span>'
            for pen in artifact["palette"]
        )
        cards.append(
            "<article>"
            f'<a class="visual" href="{svg}">{visual}</a>'
            f"<div class=copy><div class=index>{int(artifact['catalog_index']):02d} / 25</div>"
            f"<h2>{title}</h2><p>{subtitle}</p><div class=algorithm>{algorithm}</div>"
            f"<div class=palette>{palette}</div></div></article>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fields, Voids and Unknown Faces</title>
<style>
:root{{--paper:#f4f0e8;--ink:#171717;--muted:#76716a}}*{{box-sizing:border-box}}
body{{margin:0;background:#111;color:#eee;font:14px/1.45 system-ui,sans-serif}}
header{{max-width:1600px;margin:auto;padding:64px 32px 36px}}h1{{font:500 clamp(34px,6vw,86px)/.95 Georgia,serif;max-width:1100px;margin:0 0 24px}}
header p{{max-width:760px;color:#bbb;font-size:16px}}main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:2px;background:#333}}
article{{background:#171717;padding:22px}}.visual{{display:block;background:var(--paper);aspect-ratio:1/1.28;overflow:hidden}}.visual img,.visual object{{display:block;width:100%;height:100%;object-fit:contain}}
.copy{{padding:18px 2px 8px}}.index,.algorithm{{font:11px/1.2 ui-monospace,monospace;letter-spacing:.12em;text-transform:uppercase;color:#96918a}}h2{{font:400 28px/1.05 Georgia,serif;margin:8px 0}}p{{color:#bbb;margin:0 0 12px}}.palette{{display:flex;flex-wrap:wrap;gap:5px;margin-top:12px}}.pen{{font:10px ui-monospace,monospace;border:1px solid #4b4b4b;padding:3px 6px;border-radius:10px;color:#aaa}}
footer{{padding:36px 32px 70px;color:#777}}@media(max-width:500px){{main{{grid-template-columns:1fr}}header{{padding-top:40px}}}}
</style></head><body><header><h1>Fields, Voids and Unknown Faces</h1>
<p>Twenty-five original deterministic compositions built for real physical pens. Five studies use invented, anonymous facial reliefs that are not sourced from or intended to identify a real person. Every image links to its editable, millimetre-scale SVG master.</p></header>
<main>{"".join(cards)}</main><footer>Generated {html.escape(generated_at)} / review output / calibrate exact pens and stock before plotting.</footer></body></html>
"""


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise MapPlotterError(f"Contact-sheet input {path} is not a PNG.")
    return struct.unpack(">II", header[16:24])


def _write_contact_sheet(
    stage: Path, artifacts: Sequence[dict[str, Any]]
) -> list[Path]:
    plates = [stage / str(artifact["png"]) for artifact in artifacts]
    if not plates or any(not path.is_file() for path in plates):
        raise MapPlotterError("The contact sheet requires every selected PNG preview.")
    columns, gap, margin = 5, 24, 30
    sizes = [_png_size(path) for path in plates]
    cell_width = max(width for width, _ in sizes)
    cell_height = max(height for _, height in sizes)
    rows = (len(plates) + columns - 1) // columns
    page_width = margin * 2 + columns * cell_width + (columns - 1) * gap
    page_height = margin * 2 + rows * cell_height + (rows - 1) * gap
    chunks = [
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{page_width}" height="{page_height}" '
        f'viewBox="0 0 {page_width} {page_height}">',
        f'<rect width="{page_width}" height="{page_height}" fill="#111111"/>',
    ]
    for index, (path, (width, height)) in enumerate(zip(plates, sizes)):
        column, row = index % columns, index // columns
        x = margin + column * (cell_width + gap) + (cell_width - width) // 2
        y = margin + row * (cell_height + gap) + (cell_height - height) // 2
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        chunks.append(
            f'<image x="{x}" y="{y}" width="{width}" height="{height}" '
            f'xlink:href="data:image/png;base64,{encoded}"/>'
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
            "--export-type=png",
            f"--export-filename={png_path}",
            str(svg_path),
        ],
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


def _selected_pieces(args: argparse.Namespace):
    catalog = load_abstract_catalog()
    if args.all:
        if args.piece:
            raise MapPlotterError("Use either --all or --piece, not both.")
        return list(catalog)
    if not args.piece:
        raise MapPlotterError("Choose --all or repeat --piece PIECE_ID.")
    requested = list(args.piece)
    if len(requested) != len(set(requested)):
        raise MapPlotterError("A piece ID cannot be repeated in one build.")
    by_id = {piece.id: piece for piece in catalog}
    unknown = sorted(set(requested) - set(by_id))
    if unknown:
        raise MapPlotterError(f"Unknown abstract-art piece(s): {', '.join(unknown)}.")
    return [by_id[piece_id] for piece_id in requested]


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
            artwork = build_abstract_artwork(piece.id)
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
            artifacts.append(
                {
                    "subject_id": piece.id,
                    "catalog_index": piece.catalog_index,
                    "title": piece.title,
                    "subtitle": piece.subtitle,
                    "algorithm": piece.algorithm,
                    "format_id": piece.format_id,
                    "palette": list(piece.palette),
                    "svg": f"{artwork.artifact_id}.svg",
                    "plot_manifest": f"{artwork.artifact_id}.plot.json",
                    "png": f"{artwork.artifact_id}.png" if "png" in outputs else None,
                    "physical_pen_steps": manifest["plot_summary"][
                        "physical_pen_steps"
                    ],
                    "path_count": manifest["plot_summary"]["pen_down_path_count"],
                    "field_ink_coverage": manifest["plot_summary"][
                        "field_ink_coverage_upper_bound"
                    ],
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
        catalog_ids = [piece.id for piece in load_abstract_catalog()]
        selected_ids = [str(artifact["subject_id"]) for artifact in artifacts]
        series = {
            "schema_version": 1,
            "series_id": "abstract-art-v1",
            "collection_title": "Fields, Voids and Unknown Faces",
            "generated_at": generated_at,
            "production_ready": False,
            "production_blockers": [
                "the built-in physical pen widths remain nominal until the exact pens, stock and speed are calibrated",
                "the collection requires visual and physical pilot-plot approval before sale",
            ],
            "artifact_count": len(artifacts),
            "complete_catalog": selected_ids == catalog_ids,
            "checksums": "CHECKSUMS.sha256",
            "supplementary_artifacts": supplementary,
            "artifacts": artifacts,
        }
        (stage / "abstract-art-series.json").write_text(
            json.dumps(series, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _write_checksums(stage)
        os.replace(stage, output_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    print(f"Built {len(artifacts)} abstract-art plate(s) in {output_dir}.")
    print("REVIEW OUTPUT ONLY — validate, simulate and pilot-plot before production.")
    return 0


def _list(args: argparse.Namespace) -> int:
    pieces = load_abstract_catalog()
    if args.json:
        print(
            json.dumps(
                [piece.as_dict() for piece in pieces], indent=2, ensure_ascii=False
            )
        )
    else:
        for piece in pieces:
            face = " / anonymous face" if piece.is_face else ""
            print(
                f"{piece.catalog_index:02d}  {piece.id:<31} {piece.title} / {piece.format_id}{face}"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapplot-abstract",
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List the pinned 25-piece catalog.")
    listing.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    build = commands.add_parser(
        "build",
        help="Build selected catalog editions atomically.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    selection = build.add_mutually_exclusive_group(required=False)
    selection.add_argument(
        "--all", action="store_true", help="Build all 25 catalog pieces."
    )
    selection.add_argument(
        "--piece",
        action="append",
        help="Catalog piece ID; repeat to build a selection.",
    )
    build.add_argument(
        "--output-dir", "--out", dest="output_dir", default=DEFAULT_OUTPUT_DIR
    )
    build.add_argument("--no-png", action="store_true")
    build.add_argument("--no-split-pens", action="store_true")
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
