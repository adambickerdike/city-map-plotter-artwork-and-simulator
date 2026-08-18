"""Command-line batch builder for source-backed hiking pen plates."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .models import MapPlotterError
from .niche_common import PlateArtwork, write_plate


DOMAIN_ORDER = ("hikes",)
HikeBuilder = Callable[..., PlateArtwork]


def _domain_api() -> dict[
    str,
    tuple[
        Callable[[], list[dict[str, Any]]],
        HikeBuilder,
    ],
]:
    from .hike_plates import build_hike_plate, load_hike_release_catalog

    return {
        "hikes": (load_hike_release_catalog, build_hike_plate),
    }


def _hike_variants() -> tuple[str, ...]:
    """Load the renderer's binding variant order without eager catalog imports."""

    from .hike_plates import HIKE_VARIANTS

    variants = tuple(HIKE_VARIANTS)
    if not variants or len(variants) != len(set(variants)):
        raise MapPlotterError("Hiking variants must be a non-empty unique sequence.")
    return variants


def catalog_records(domain: str | None = None) -> list[dict[str, Any]]:
    api = _domain_api()
    domains = DOMAIN_ORDER if domain is None else (domain,)
    records: list[dict[str, Any]] = []
    for domain_id in domains:
        if domain_id not in api:
            raise MapPlotterError(
                f"Unknown domain {domain_id!r}; choose {', '.join(DOMAIN_ORDER)}."
            )
        loader, _ = api[domain_id]
        for record in loader():
            tagged = dict(record)
            tagged.setdefault("domain", domain_id)
            records.append(tagged)
    ids = [str(record.get("id")) for record in records]
    if len(ids) != len(set(ids)):
        repeated = sorted(
            {subject_id for subject_id in ids if ids.count(subject_id) > 1}
        )
        raise MapPlotterError(f"Hiking catalog repeats subject IDs: {repeated}.")
    return records


def _select_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    records = catalog_records(args.domain)
    if args.all:
        return records
    wanted = set(args.subject or [])
    selected = [record for record in records if record["id"] in wanted]
    missing = sorted(wanted - {record["id"] for record in selected})
    if missing:
        raise MapPlotterError(f"Unknown hiking subject(s): {', '.join(missing)}.")
    if not selected:
        raise MapPlotterError("Choose --all or at least one --subject ID.")
    return selected


def _catalog_format(record: dict[str, Any]) -> str:
    composition = record.get("composition")
    composition_format = (
        composition.get("format_id") if isinstance(composition, dict) else None
    )
    return str(
        record.get("format_id")
        or record.get("preferred_format")
        or composition_format
        or "a5-portrait"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_contact_sheet(
    domain_dir: Path,
    domain: str,
    variant_id: str,
    entries: list[dict[str, Any]],
) -> Path | None:
    montage = shutil.which("montage")
    pngs = [
        Path(record["outputs"]["png"]["path"])
        for record in entries
        if record["domain"] == domain
        and record["variant_id"] == variant_id
        and "png" in record["outputs"]
    ]
    if montage is None or not pngs:
        return None
    destination = domain_dir / f"{domain}-{variant_id}-contact-sheet.png"
    columns = min(5, len(pngs))
    rows = math.ceil(len(pngs) / columns)
    result = subprocess.run(
        [
            montage,
            *map(str, pngs),
            "-thumbnail",
            "420x600",
            "-gravity",
            "center",
            "-tile",
            f"{columns}x{rows}",
            "-geometry",
            "420x600+18+18",
            "-background",
            "white",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return destination


def _write_gallery(output_dir: Path, index: list[dict[str, Any]]) -> Path:
    cards: list[str] = []
    for record in index:
        png = record.get("outputs", {}).get("png", {}).get("path")
        if not png:
            continue
        relative = Path(png).relative_to(output_dir.resolve())
        cards.append(
            "<figure>"
            f'<a href="{html.escape(str(relative.with_suffix(".svg")))}">'
            f'<img src="{html.escape(str(relative))}" '
            f'alt="{html.escape(record["title"])} — '
            f'{html.escape(record["variant_id"])}"></a>'
            f"<figcaption>{html.escape(record['title'])}<br>"
            f"{html.escape(record['subject_id'])} / "
            f"{html.escape(record['variant_id'])}<br>"
            f"{html.escape(record['artifact_id'])}</figcaption>"
            "</figure>"
        )
    document = (
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Hiking pen-map collection</title><style>
body{font:15px system-ui;margin:2rem;background:#eee;color:#171717}h1{font-weight:500}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:1.5rem}
figure{margin:0;background:white;padding:1rem;box-shadow:0 2px 12px #0002}img{display:block;width:100%;height:auto}
figcaption{line-height:1.45;padding-top:.7rem}a{color:inherit}
</style></head><body><h1>Hiking pen-map collection</h1><p>Click a preview to open its editable SVG.</p>
<main>"""
        + "\n".join(cards)
        + "</main></body></html>\n"
    )
    path = output_dir / "gallery.html"
    path.write_text(document, encoding="utf-8")
    return path


def _write_pen_guide(output_dir: Path, index: list[dict[str, Any]]) -> Path:
    lines = [
        "# Pen-change guide",
        "",
        "Each master SVG is already ordered by physical pen. Plot the numbered "
        "Inkscape layers in order, or send the matching `.pen-NN-<pen-id>.svg` files.",
        "",
        "All built-in pen widths are nominal review values. Calibrate the exact pens, "
        "paper stock and speed before a production run.",
        "",
    ]
    for record in index:
        manifest = json.loads(Path(record["outputs"]["manifest"]["path"]).read_text())
        lines.extend(
            [
                f"## {record['title']} — {record['variant_id']}",
                "",
                f"Artifact: `{record['artifact_id']}`",
                "",
            ]
        )
        for step in manifest["pen_sequence"]:
            lines.append(
                f"{step['step']}. **{step['pen']}** (`{step['pen_id']}`) — "
                f"{step['path_count']} paths, {step['pen_down_distance_mm']:.1f} mm pen-down."
            )
        lines.append("")
    path = output_dir / "PEN-CHANGE-GUIDE.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _single_line(value: object) -> str:
    """Return a source-register field without embedded layout control."""

    return " ".join(str(value or "").split())


def _write_source_register(
    output_dir: Path,
    index: list[dict[str, Any]],
) -> tuple[Path, Path]:
    """Write one exact source/licence register for the paired hiking release."""

    release_root = output_dir.resolve()
    subjects: dict[str, dict[str, Any]] = {}
    generated_at = ""
    for entry in index:
        subject_id = str(entry["subject_id"])
        manifest_path = Path(entry["outputs"]["manifest"]["path"]).resolve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_sources = manifest.get("sources")
        if not isinstance(manifest_sources, list) or not all(
            isinstance(source, dict) for source in manifest_sources
        ):
            raise MapPlotterError(
                f"{entry['artifact_id']}: manifest has no valid source list."
            )
        generated_at = generated_at or str(manifest.get("generated_at") or "")
        sources = [dict(source) for source in manifest_sources]
        source_digest = hashlib.sha256(
            json.dumps(
                sources,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        subject = subjects.get(subject_id)
        if subject is None:
            subject = {
                "subject_id": subject_id,
                "title": str(entry["title"]),
                "source_records_sha256": source_digest,
                "sources": sources,
                "artifacts": [],
            }
            subjects[subject_id] = subject
        elif subject["source_records_sha256"] != source_digest:
            raise MapPlotterError(
                f"{subject_id}: paired variants do not share identical source records."
            )
        subject["artifacts"].append(
            {
                "artifact_id": str(entry["artifact_id"]),
                "variant_id": str(entry["variant_id"]),
                "manifest": str(manifest_path.relative_to(release_root)),
            }
        )

    source_record_count = sum(len(subject["sources"]) for subject in subjects.values())
    review_required = any(
        bool(source.get("provider_attribution_review_required"))
        for subject in subjects.values()
        for source in subject["sources"]
    )
    payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "release_status": "review-only",
        "commercial_clearance_status": "incomplete",
        "subject_count": len(subjects),
        "artifact_count": len(index),
        "source_record_count": source_record_count,
        "provider_attribution_review_required": review_required,
        "subjects": list(subjects.values()),
    }
    sources_path = output_dir / "SOURCES.json"
    sources_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    lines = [
        "HIKING SERIES SOURCE AND LICENCE REGISTER",
        "",
        "RELEASE STATUS: REVIEW ONLY — NOT COMMERCIALLY CLEARED.",
        "",
        "This register identifies each factual input and its declared terms. It is ",
        "not a substitute for the full licence text or a route-by-route rights review.",
        "Follow the source URLs and resolve all attribution obligations before sale.",
        "The AWS-hosted Mapzen terrain product has mixed underlying source terms; ",
        "its location-specific provider attribution remains an explicit blocker.",
        "",
    ]
    for subject in subjects.values():
        lines.extend(
            [
                f"{subject['title']} ({subject['subject_id']})",
                "-" * (len(subject["title"]) + len(subject["subject_id"]) + 3),
            ]
        )
        for source in subject["sources"]:
            lines.extend(
                [
                    f"Source ID: {_single_line(source.get('id'))}",
                    f"Publisher: {_single_line(source.get('publisher'))}",
                    f"Use: {_single_line(source.get('use'))}",
                    f"Declared licence: {_single_line(source.get('license'))}",
                    f"Attribution: {_single_line(source.get('attribution'))}",
                    f"URL: {_single_line(source.get('url'))}",
                    "Provider attribution review required: "
                    + (
                        "yes"
                        if source.get("provider_attribution_review_required")
                        else "no"
                    ),
                    "",
                ]
            )
    licences_path = output_dir / "LICENSES.txt"
    licences_path.write_text("\n".join(lines), encoding="utf-8")
    return sources_path, licences_path


def _write_artifact_catalog(
    output_dir: Path,
    index: list[dict[str, Any]],
    contact_sheets: dict[str, dict[str, str]],
) -> Path:
    subject_count = len({record["subject_id"] for record in index})
    variants = tuple(dict.fromkeys(record["variant_id"] for record in index))
    lines = [
        "# Hiking pen-map artifact catalog",
        "",
        f"{len(index)} review-only hiking map artifacts across {subject_count} route(s) "
        f"and {len(variants)} variant(s). SVGs use physical millimetres; each master "
        "has a PNG preview, plot manifest, and one SVG job per active pen.",
        "",
        "> These are design and engineering examples, not commercial clearance or "
        "navigation documents. Calibrate the exact pens, stock, and speed before plotting.",
        "",
    ]
    for domain in DOMAIN_ORDER:
        domain_entries = [record for record in index if record["domain"] == domain]
        lines.extend([f"## {domain.title()}", ""])
        for variant_id, contact in contact_sheets.get(domain, {}).items():
            lines.extend(
                [
                    f"[Open {variant_id} contact sheet]"
                    f"({Path(contact).relative_to(output_dir.resolve())})",
                    "",
                ]
            )
        lines.extend(
            [
                "| Route | Variant | Artifact | Format | Evidence / scale | Files |",
                "|---|---|---|---|---|---|",
            ]
        )
        for record in domain_entries:
            outputs = record["outputs"]
            svg = Path(outputs["svg"]["path"]).relative_to(output_dir.resolve())
            manifest = Path(outputs["manifest"]["path"]).relative_to(
                output_dir.resolve()
            )
            links = [f"[SVG]({svg})"]
            if "png" in outputs:
                png = Path(outputs["png"]["path"]).relative_to(output_dir.resolve())
                links.append(f"[PNG]({png})")
            links.append(f"[manifest]({manifest})")
            lines.append(
                f"| {record['title']} (`{record['subject_id']}`) | "
                f"{record['variant_id']} | `{record['artifact_id']}` | "
                f"{record['format_id']} | "
                f"{record['evidence_status']} / {record['scale_status']} | "
                f"{' / '.join(links)} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Operator files",
            "",
            "- [Pen-change guide](PEN-CHANGE-GUIDE.md)",
            "- [Machine-readable index](index.json)",
            "- [Machine-readable source register](SOURCES.json)",
            "- [Source and licence register](LICENSES.txt)",
            "- [Visual gallery](gallery.html)",
            "- [QA report](qa-report.md) (written by the separate QA pass)",
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
        if path.is_file() and path != destination and path.name != "CHECKSUMS.sha256"
    )
    destination.write_text(
        "".join(
            f"{_sha256(path)}  {path.relative_to(output_dir)}\n" for path in candidates
        ),
        encoding="ascii",
    )
    return destination


def build_series(args: argparse.Namespace) -> int:
    if not math.isfinite(args.dpi) or args.dpi <= 0:
        raise MapPlotterError("--dpi must be a positive finite number.")
    records = _select_records(args)
    api = _domain_api()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC).isoformat()
    index: list[dict[str, Any]] = []
    variants = _hike_variants()
    artifact_total = len(records) * len(variants)
    artifact_position = 0
    for subject_position, record in enumerate(records, start=1):
        domain = str(record["domain"])
        _, builder = api[domain]
        selected_format = (
            _catalog_format(record) if args.format == "catalog" else args.format
        )
        base_subject_id = str(record["id"])
        for variant_position, variant_id in enumerate(variants, start=1):
            artifact_position += 1
            artwork = builder(
                record,
                selected_format,
                variant_id=variant_id,
            )
            expected_artifact_id = f"{base_subject_id}--{variant_id}"
            if (
                artwork.subject_id != base_subject_id
                or artwork.variant_id != variant_id
                or artwork.artifact_id != expected_artifact_id
            ):
                raise MapPlotterError(
                    "Hiking builder returned mismatched subject/variant identity: "
                    f"expected {expected_artifact_id!r}."
                )
            domain_dir = output_dir / domain
            outputs = write_plate(
                artwork,
                domain_dir,
                png=not args.no_png,
                png_dpi=args.dpi,
                split_pens=not args.no_split_pens,
                generated_at=generated_at,
            )
            index.append(
                {
                    "position": artifact_position,
                    "subject_position": subject_position,
                    "variant_position": variant_position,
                    # Keep ``id`` as a compatibility alias, now unambiguously
                    # identifying the rendered artifact rather than its route.
                    "id": artwork.artifact_id,
                    "subject_id": artwork.subject_id,
                    "variant_id": variant_id,
                    "artifact_id": artwork.artifact_id,
                    "domain": domain,
                    "title": artwork.title,
                    "subtitle": artwork.subtitle,
                    "format_id": artwork.context.format_id,
                    "scale_status": artwork.scale_status,
                    "evidence_status": artwork.evidence_status,
                    "rights_status": artwork.rights_status,
                    "outputs": outputs,
                }
            )
            print(
                f"[{artifact_position:02d}/{artifact_total:02d}] "
                f"{domain}/{artwork.artifact_id}"
            )

    contact_sheets: dict[str, dict[str, str]] = {}
    if not args.no_png:
        for domain in DOMAIN_ORDER:
            for variant_id in variants:
                path = _write_contact_sheet(
                    output_dir / domain,
                    domain,
                    variant_id,
                    index,
                )
                if path is not None:
                    contact_sheets.setdefault(domain, {})[variant_id] = str(
                        path.resolve()
                    )
    index_path = output_dir / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "generated_at": generated_at,
                "format_id": args.format,
                "count": len(index),
                "subject_count": len(records),
                "artifact_count": len(index),
                "variants": list(variants),
                "counts_by_domain": {
                    domain: sum(item["domain"] == domain for item in index)
                    for domain in DOMAIN_ORDER
                },
                "counts_by_variant": {
                    variant_id: sum(item["variant_id"] == variant_id for item in index)
                    for variant_id in variants
                },
                "contact_sheets": contact_sheets,
                "entries": index,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_gallery(output_dir, index)
    _write_pen_guide(output_dir, index)
    _write_source_register(output_dir, index)
    _write_artifact_catalog(output_dir, index, contact_sheets)
    _write_checksums(output_dir)
    print(f"Built {len(index)} plates in {output_dir}")
    return 0


def build_gpx(args: argparse.Namespace) -> int:
    """Build a private, local hiking plate from one caller-owned GPX file."""

    if not args.confirm_rights:
        raise MapPlotterError(
            "GPX import requires --confirm-rights to confirm that you may use the track."
        )
    if not args.privacy_reviewed:
        raise MapPlotterError(
            "GPX import requires --privacy-reviewed after checking home/start/end exposure."
        )
    if not math.isfinite(args.dpi) or args.dpi <= 0:
        raise MapPlotterError("--dpi must be a positive finite number.")
    from .hike_plates import build_hike_from_gpx

    artwork = build_hike_from_gpx(
        args.gpx.resolve(),
        title=args.title,
        subtitle=args.subtitle,
        format_id=args.format,
    )
    output_dir = args.output_dir.resolve()
    outputs = write_plate(
        artwork,
        output_dir,
        png=True,
        png_dpi=args.dpi,
        split_pens=True,
    )
    print(f"Built private GPX plate: {outputs['svg']['path']}")
    print("Decorative output only; do not use it for navigation.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapplot-hike",
        description="Build geographic, pen-physical hiking maps.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List the curated hiking catalog.")
    listing.add_argument("--domain", choices=DOMAIN_ORDER)
    listing.add_argument("--json", action="store_true")

    build = commands.add_parser("build", help="Build SVG/PNG plate examples.")
    selection = build.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--subject", action="append")
    build.add_argument("--domain", choices=DOMAIN_ORDER)
    build.add_argument(
        "--format",
        default="catalog",
        choices=(
            "catalog",
            "a5-portrait",
            "a5-landscape",
            "a4-portrait",
            "a4-landscape",
            "a3-portrait",
            "a3-landscape",
        ),
        help="Use each catalog composition or force one binding plate format.",
    )
    build.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/hiking-series-paired-2026-08-04"),
    )
    build.add_argument("--dpi", type=float, default=300.0)
    build.add_argument("--no-png", action="store_true")
    build.add_argument("--no-split-pens", action="store_true")

    gpx = commands.add_parser(
        "gpx",
        help="Build a local decorative hiking plate from a caller-owned GPX file.",
    )
    gpx.add_argument("gpx", type=Path)
    gpx.add_argument("--title", required=True)
    gpx.add_argument("--subtitle", default="PERSONAL ROUTE / NOT FOR NAVIGATION")
    gpx.add_argument(
        "--format",
        default="a5-portrait",
        choices=(
            "a5-portrait",
            "a5-landscape",
            "a4-portrait",
            "a4-landscape",
            "a3-portrait",
            "a3-landscape",
        ),
    )
    gpx.add_argument("--output-dir", type=Path, default=Path("output/personal-hike"))
    gpx.add_argument("--dpi", type=float, default=300.0)
    gpx.add_argument(
        "--confirm-rights",
        action="store_true",
        help="Confirm that you own or have permission to use the GPX track.",
    )
    gpx.add_argument(
        "--privacy-reviewed",
        action="store_true",
        help="Confirm that start/end/home-location exposure has been reviewed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "list":
            records = catalog_records(args.domain)
            if args.json:
                print(json.dumps(records, indent=2, ensure_ascii=False))
            else:
                for record in records:
                    print(f"{record['id']:<34} {record['domain']:<7} {record['title']}")
            return 0
        if args.command == "build":
            return build_series(args)
        return build_gpx(args)
    except (MapPlotterError, OSError, ValueError) as exc:
        print(f"mapplot-hike: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
