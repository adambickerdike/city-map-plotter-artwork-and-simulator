#!/usr/bin/env python3
"""Extract frozen, bbox-complete hiking context from a regional OSM PBF."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from city_map_plotter.models import BoundingBox, MapFeature
from city_map_plotter.pbf import load_pbf


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
    parser.add_argument("--output-dir", type=Path, required=True)
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
    layers = {layer.strip() for layer in args.layers.split(",") if layer.strip()}
    if not layers:
        parser.error("--layers must not be empty")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for subject_id in args.subject:
        record = records.get(subject_id)
        if record is None:
            parser.error(f"unknown subject {subject_id!r}")
        west, south, east, north = (
            float(value) for value in record["context"]["extent"]
        )
        acquisition = load_pbf(
            args.pbf,
            BoundingBox(west=west, south=south, east=east, north=north),
            layers,
        )
        features = acquisition.features or []
        payload = {
            "source_metadata": acquisition.source_metadata,
            "features": [_feature_payload(feature) for feature in features],
        }
        output = args.output_dir / f"{subject_id}-osm-context.json"
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"{subject_id}: {len(features)} features -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
