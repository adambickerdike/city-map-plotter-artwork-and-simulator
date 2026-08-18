#!/usr/bin/env python3
"""Extract one bbox-complete PBF context shared by several hiking plates.

This avoids scanning a large regional PBF once per overlapping plate.  The
output keeps the canonical metadata returned by ``load_pbf`` for the union
extent; downstream derivation must verify that the union contains its plate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from city_map_plotter.models import BoundingBox, MapFeature  # type: ignore[import-untyped]
from city_map_plotter.pbf import load_pbf  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = ROOT / "src" / "city_map_plotter" / "data" / "hike-plates-v1.json"
DEFAULT_LAYERS = {"green_space", "water_areas", "waterways"}


def _feature_payload(feature: MapFeature) -> dict[str, Any]:
    return {
        "layer": feature.layer,
        "points": [list(point) for point in feature.points],
        "osm_type": feature.osm_type,
        "osm_id": feature.osm_id,
        "part": feature.part,
        "tags": feature.tags,
        "geometry_type": feature.geometry_type,
        "ring_role": feature.ring_role,
        "outer_ring_part": feature.outer_ring_part,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, required=True)
    parser.add_argument("--subject", action="append", required=True)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--layers",
        default=",".join(sorted(DEFAULT_LAYERS)),
        help="Comma-separated canonical feature layers.",
    )
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    records = {
        str(record["id"]): record for record in catalog.get("plates", [])
    }
    selected_records: list[dict[str, Any]] = []
    for subject_id in args.subject:
        record = records.get(subject_id)
        if record is None:
            parser.error(f"unknown subject {subject_id!r}")
        selected_records.append(record)
    layers = {layer.strip() for layer in args.layers.split(",") if layer.strip()}
    if not layers:
        parser.error("--layers must not be empty")
    extents = [record["context"]["extent"] for record in selected_records]
    west = min(float(extent[0]) for extent in extents)
    south = min(float(extent[1]) for extent in extents)
    east = max(float(extent[2]) for extent in extents)
    north = max(float(extent[3]) for extent in extents)
    acquisition = load_pbf(
        args.pbf,
        BoundingBox(west=west, south=south, east=east, north=north),
        layers,
    )
    features = acquisition.features or []
    payload = {
        "subjects": [str(record["id"]) for record in selected_records],
        "union_extent_wgs84": [west, south, east, north],
        "source_metadata": acquisition.source_metadata,
        "features": [_feature_payload(feature) for feature in features],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"{len(selected_records)} subjects / {len(features)} features -> {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
