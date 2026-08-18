#!/usr/bin/env python3
"""Assemble the ten legacy hikes and thirty verified additions for terrain freezing.

The legacy source bundle remains immutable and strictly validated.  This tool
loads it through the production validator, binds every subject north-up, then
appends the separately acquired expansion records.  Global DEM terrain is
frozen in the next derivation pass; the renderer itself stays network-free.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, NoReturn, Sequence

from city_map_plotter.hike_plates import (
    EXPECTED_IDS,
    HIKE_PENS,
    PEN_PLAN_ID,
    SUBJECT_KIND,
    _release_expected_ids,
    load_hike_catalog,
)

from hiking_map_extent import bind_aspect_expanded_map_extent


RELEASE_ID = "hike-plates-release-v1"
EXPECTED_SUBJECTS = 40
OSM_TERRAIN_CREDIT = (
    "© OpenStreetMap CONTRIBUTORS / MAPZEN AWS TERRAIN"
)


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"assemble_hiking_release_catalog: {message}")


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"could not read {path}: {exc}")
    if not isinstance(value, dict):
        _fail(f"{path} must contain an object")
    return value


def _legacy_source_credit(credit_line: str) -> str:
    """Retain the non-OSM provider copy from a legacy visible credit.

    The audited legacy plates deliberately name sources such as Ordnance
    Survey, swisstopo, IGN and ÍslandsDEM.  Their OSM clauses vary between
    ``OSM`` and ``OPENSTREETMAP`` abbreviations, so the release cannot safely
    replace the complete line with one generic terrain credit.
    """

    retained: list[str] = []
    for raw_line in credit_line.split(" | "):
        line = raw_line.strip()
        upper = line.upper()
        if "OPENSTREETMAP.ORG/COPYRIGHT" in upper:
            line = line[: upper.index("OPENSTREETMAP.ORG/COPYRIGHT")]
            upper = line.upper()
        osm_positions = [
            position
            for token in ("OPENSTREETMAP CONTRIBUTORS", "OSM CONTRIBUTORS")
            if (position := upper.find(token)) >= 0
        ]
        if osm_positions:
            line = line[: min(osm_positions)]
        line = line.rstrip(" /\u00a9").strip()
        if line and line not in retained:
            retained.append(line)
    if not retained:
        return "SOURCE-SPECIFIC CREDITS IN MANIFEST"
    return " / ".join(retained)


def _legacy_release_credit(record: dict[str, Any]) -> str:
    """Compose a maximum-two-line visible credit for the paired release."""

    subject_id = str(record.get("id", "<unknown>"))
    original = record.get("credit_line")
    if not isinstance(original, str) or not original.strip():
        _fail(f"{subject_id}: legacy visible attribution is missing")
    sources = record.get("sources")
    if not isinstance(sources, list):
        _fail(f"{subject_id}: legacy sources must be an array")
    has_odbl = any(
        isinstance(source, dict)
        and str(source.get("license", "")).upper().startswith("ODBL")
        for source in sources
    )
    if not has_odbl:
        _fail(f"{subject_id}: legacy route unexpectedly lacks an ODbL source")
    credit = f"{_legacy_source_credit(original)} | {OSM_TERRAIN_CREDIT}"
    if "OpenStreetMap" not in credit:
        _fail(f"{subject_id}: ODbL visible credit must literally name OpenStreetMap")
    return credit


def _literal_openstreetmap_credit(value: Any, *, subject_id: str) -> str:
    """Normalize casing while failing closed on an abbreviated ODbL credit."""

    if not isinstance(value, str) or not value.strip():
        _fail(f"{subject_id}: visible attribution is missing")
    folded = value.casefold()
    token = "openstreetmap"
    offset = folded.find(token)
    if offset < 0:
        _fail(f"{subject_id}: ODbL visible credit must literally name OpenStreetMap")
    return f"{value[:offset]}OpenStreetMap{value[offset + len(token):]}"


def assemble(
    expansions: Sequence[dict[str, Any]], *, retrieved_at: str
) -> dict[str, Any]:
    if not isinstance(retrieved_at, str) or not retrieved_at.strip():
        _fail("retrieved_at must be an explicit non-empty timestamp")
    additions: list[dict[str, Any]] = []
    for expansion in expansions:
        if (
            expansion.get("schema_version") != 1
            or expansion.get("id") != "hike-plates-expansion-v1"
        ):
            _fail("every expansion input must use schema 1 / hike-plates-expansion-v1")
        if expansion.get("subject_kind") != SUBJECT_KIND:
            _fail(f"every expansion input must use subject_kind={SUBJECT_KIND!r}")
        pen_plan = expansion.get("pen_plan")
        if (
            not isinstance(pen_plan, dict)
            or pen_plan.get("id") != PEN_PLAN_ID
            or tuple(pen_plan.get("pens", ())) != HIKE_PENS
        ):
            _fail("every expansion input must use the exact seven-pen hiking plan")
        plates = expansion.get("plates")
        if not isinstance(plates, list) or not plates:
            _fail("every expansion input must contain a non-empty plates array")
        if not all(isinstance(record, dict) for record in plates):
            _fail("expansion plates must be objects")
        for record in plates:
            subject_id = str(record.get("id", ""))
            context = record.get("context")
            composition = record.get("composition")
            if record.get("subject_kind") != SUBJECT_KIND:
                _fail(f"{subject_id}: expansion subject_kind is invalid")
            if (
                not isinstance(composition, dict)
                or composition.get("pen_plan") != PEN_PLAN_ID
            ):
                _fail(f"{subject_id}: expansion pen plan is invalid")
            if (
                not isinstance(context, dict)
                or context.get("rotation_deg") != 0.0
                or context.get("orientation_status") != "north-up"
            ):
                _fail(f"{subject_id}: expansion geometry must already be north-up")
            addition = copy.deepcopy(record)
            # Expansion plates already carry only the common OSM/terrain
            # credit.  Preserve its two-line layout while using the literal
            # project name required by the ODbL attribution gate.
            addition["credit_line"] = _literal_openstreetmap_credit(
                addition.get("credit_line"),
                subject_id=subject_id,
            )
            additions.append(addition)
    if len(additions) != 30:
        _fail("expansion inputs must contain exactly thirty plates in total")
    addition_ids = [str(record.get("id", "")) for record in additions]
    expected_addition_ids = _release_expected_ids() - EXPECTED_IDS
    if len(set(addition_ids)) != 30 or set(addition_ids) != expected_addition_ids:
        _fail("expansion IDs must exactly match the thirty canonical recipes")

    legacy = load_hike_catalog()
    for record in legacy:
        context = record["context"]
        context["rotation_deg"] = 0.0
        context["orientation_status"] = "north-up"
        # Keep the location-native OS/IGN/swisstopo/IslandsDEM terrain and any
        # route-source elevation profile intact.  The global DEM pass is a
        # fallback for records that lack those facts; a later source-precedence
        # gate reconciles its route samples without throwing superior source
        # geometry away.
        record["credit_line"] = _legacy_release_credit(record)
        record["notes"].append(
            "North-up release preserves location-native terrain and route-source "
            "elevation ahead of the global fallback pass."
        )

    records = [*legacy, *additions]
    identifiers = [str(record.get("id")) for record in records]
    if len(records) != EXPECTED_SUBJECTS or len(identifiers) != len(set(identifiers)):
        _fail("assembled release must contain forty unique subject IDs")
    for record in records:
        record["data_snapshot"] = retrieved_at
        context = record.get("context")
        if not isinstance(context, dict):
            _fail(f"{record.get('id')}: context must be an object")
        if context.get("rotation_deg") != 0.0:
            _fail(f"{record.get('id')}: release must be north-up")
        if context.get("orientation_status") != "north-up":
            _fail(f"{record.get('id')}: release orientation metadata must be north-up")
        # Minimal test fixtures predating geographic acquisition do not carry
        # an extent.  Every production catalog record does; bind those records
        # here so an existing frozen expansion can be reassembled without an
        # OSM/Waymarked re-query before the new terrain pass.
        if isinstance(context.get("route_extent", context.get("extent")), list):
            bind_aspect_expanded_map_extent(record, has_profile=True)

    return {
        "schema_version": 1,
        "id": RELEASE_ID,
        "subject_kind": "route_plate",
        "data_snapshot": retrieved_at,
        "pen_plan": {
            "id": PEN_PLAN_ID,
            "pens": list(HIKE_PENS),
            "order_note": (
                "Grey 0.25 minor relief/roads, Grey 0.40 factual index contours, "
                "blue water, green vegetation, black labels, black title/border, "
                "red hero route."
            ),
        },
        "plates": records,
    }


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        _fail(f"could not write {path}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expansion", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--retrieved-at", required=True)
    args = parser.parse_args()
    release = assemble(
        [_load(path) for path in args.expansion],
        retrieved_at=args.retrieved_at,
    )
    _write_atomic(args.output, release)
    print(f"Assembled {len(release['plates'])} north-up subjects -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
