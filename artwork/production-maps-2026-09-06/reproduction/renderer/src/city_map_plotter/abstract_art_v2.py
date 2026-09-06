"""Plate adapter for the curated genuinely three-dimensional abstract-art v2."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
from importlib import resources
import json
import math
from typing import Any

from .abstract_3d_scenes import ALL_SCENES, PILOT_SCENES, build_scene
from .models import MapPlotterError
from .niche_common import (
    ArtworkLayer,
    PEN_ORDER,
    PENS_BY_ID,
    PlateArtwork,
    StrokeRecord,
    context_for,
    polyline_length_mm,
)
from .plot3d import RenderedCurve, render_hidden_line_scene


CATALOG_RESOURCE = "data/abstract-art-v2.json"
PILOT_CATALOG_RESOURCE = "data/abstract-art-v2-pilot.json"
CATALOG_ID = "abstract-art-v2"
PILOT_CATALOG_ID = "abstract-art-v2-pilot"
PRODUCTION_DEPTH_BUFFER_PX = 1800
RENDERER_VERSION = 2
_PIECE_KEYS = {
    "id",
    "title",
    "subtitle",
    "scene",
    "seed",
    "format_id",
    "category",
    "palette",
    "statement",
    "composition",
}


@dataclass(frozen=True)
class Abstract3DPiece:
    id: str
    title: str
    subtitle: str
    scene: str
    seed: int
    format_id: str
    category: str
    palette: tuple[str, ...]
    statement: str
    composition: str
    catalog_index: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "subtitle": self.subtitle,
            "scene": self.scene,
            "seed": self.seed,
            "format_id": self.format_id,
            "category": self.category,
            "palette": list(self.palette),
            "statement": self.statement,
            "composition": self.composition,
        }


def _catalog_error(catalog_id: str, message: str) -> MapPlotterError:
    return MapPlotterError(f"Abstract 3D catalog {catalog_id!r} is invalid: {message}")


@lru_cache(maxsize=2)
def _load_catalog(
    resource_name: str,
    catalog_id: str,
    expected_count: int,
    pilot_only: bool,
) -> tuple[Abstract3DPiece, ...]:
    resource = resources.files("city_map_plotter").joinpath(resource_name)
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _catalog_error(catalog_id, str(exc)) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise _catalog_error(catalog_id, "schema_version must equal 1")
    if payload.get("catalog_id") != catalog_id:
        raise _catalog_error(catalog_id, f"catalog_id must equal {catalog_id!r}")
    raw_pieces = payload.get("pieces")
    if not isinstance(raw_pieces, list) or len(raw_pieces) != expected_count:
        raise _catalog_error(
            catalog_id,
            f"pieces must contain exactly {expected_count} curated scenes",
        )
    allowed_scenes = PILOT_SCENES if pilot_only else ALL_SCENES
    pieces: list[Abstract3DPiece] = []
    ids: set[str] = set()
    scenes: set[str] = set()
    for index, record in enumerate(raw_pieces, start=1):
        if not isinstance(record, dict) or set(record) != _PIECE_KEYS:
            raise _catalog_error(
                catalog_id,
                f"piece {index} must contain exactly {sorted(_PIECE_KEYS)}"
            )
        piece_id = record["id"]
        scene = record["scene"]
        if (
            not isinstance(piece_id, str)
            or not piece_id
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
                for character in piece_id
            )
        ):
            raise _catalog_error(catalog_id, f"piece {index} has an invalid id")
        if piece_id in ids:
            raise _catalog_error(catalog_id, f"piece id {piece_id!r} is repeated")
        if scene not in allowed_scenes or scene in scenes:
            raise _catalog_error(
                catalog_id, f"piece {piece_id!r} has an invalid/repeated scene"
            )
        seed = record["seed"]
        if not isinstance(seed, int) or isinstance(seed, bool) or seed <= 0:
            raise _catalog_error(catalog_id, f"piece {piece_id!r} has an invalid seed")
        format_id = record["format_id"]
        if format_id not in {"a3-portrait", "a3-landscape"}:
            raise _catalog_error(
                catalog_id, f"piece {piece_id!r} must use a canonical A3 plate"
            )
        palette = record["palette"]
        if (
            not isinstance(palette, list)
            or not 3 <= len(palette) <= 7
            or len(palette) != len(set(palette))
            or any(pen_id not in PENS_BY_ID for pen_id in palette)
        ):
            raise _catalog_error(
                catalog_id, f"piece {piece_id!r} has an invalid physical palette"
            )
        text_values = {
            name: record[name]
            for name in ("title", "subtitle", "category", "statement", "composition")
        }
        if any(
            not isinstance(value, str) or not value.strip()
            for value in text_values.values()
        ):
            raise _catalog_error(catalog_id, f"piece {piece_id!r} has empty copy")
        pieces.append(
            Abstract3DPiece(
                piece_id,
                str(record["title"]),
                str(record["subtitle"]),
                str(scene),
                int(seed),
                str(format_id),
                str(record["category"]),
                tuple(str(value) for value in palette),
                str(record["statement"]),
                str(record["composition"]),
                index,
            )
        )
        ids.add(piece_id)
        scenes.add(str(scene))
    return tuple(pieces)


def load_abstract_3d_catalog() -> tuple[Abstract3DPiece, ...]:
    return _load_catalog(CATALOG_RESOURCE, CATALOG_ID, 25, False)


def load_abstract_3d_pilot() -> tuple[Abstract3DPiece, ...]:
    return _load_catalog(PILOT_CATALOG_RESOURCE, PILOT_CATALOG_ID, 6, True)


def _nearest_order(records: list[StrokeRecord]) -> list[StrokeRecord]:
    if len(records) < 2:
        return records
    remaining = records[:]
    current = min(
        remaining,
        key=lambda record: (
            min(point[1] for point in record.points),
            min(point[0] for point in record.points),
        ),
    )
    remaining.remove(current)
    ordered = [current]
    endpoint = current.points[-1]
    while remaining:
        best_index = 0
        best_reverse = False
        best_distance = math.inf
        for index, record in enumerate(remaining):
            for reverse, point in (
                (False, record.points[0]),
                (True, record.points[-1]),
            ):
                distance = math.hypot(point[0] - endpoint[0], point[1] - endpoint[1])
                if distance < best_distance:
                    best_index, best_reverse, best_distance = index, reverse, distance
        chosen = remaining.pop(best_index)
        if best_reverse:
            chosen.points.reverse()
        ordered.append(chosen)
        endpoint = chosen.points[-1]
    return ordered


def _geometry_digest(layers: list[ArtworkLayer]) -> str:
    payload = [
        {
            "pen_id": layer.pen_id,
            "records": [
                {
                    "points": [[round(x, 6), round(y, 6)] for x, y in record.points],
                    "role": record.role,
                    "attributes": record.attributes,
                }
                for record in layer.records
            ],
        }
        for layer in layers
    ]
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _layer_from_curves(
    pen_id: str, curves: list[RenderedCurve]
) -> tuple[ArtworkLayer, int]:
    layer = ArtworkLayer(
        f"scene_{pen_id.replace('-', '_')}",
        f"Visible 3D scene / {PENS_BY_ID[pen_id].label}",
        pen_id,
    )
    minimum = 3.0 * layer.pen.mark_width_mm
    rejected = 0
    for curve in curves:
        if polyline_length_mm(curve.points) + 1e-9 < minimum:
            rejected += 1
            continue
        layer.add(
            curve.points,
            source_ref="abstract-3d-engine-v2",
            role=curve.role,
            attributes={
                **curve.attributes,
                "data-mean-camera-depth": f"{curve.mean_depth:.6f}",
                "data-renderer": "perspective-depth-buffer-v2",
            },
        )
    layer.records = _nearest_order(layer.records)
    return layer, rejected


def geometry_sha256(artwork: PlateArtwork) -> str:
    return _geometry_digest(artwork.layers)


def build_abstract_3d_artwork(piece_id: str) -> PlateArtwork:
    by_id = {piece.id: piece for piece in load_abstract_3d_catalog()}
    try:
        piece = by_id[piece_id]
    except KeyError as exc:
        raise MapPlotterError(f"Unknown abstract 3D piece {piece_id!r}.") from exc
    context = context_for(piece.format_id)
    scene = build_scene(piece.scene, piece.seed)
    requested_depth_buffer_px = scene.depth_buffer_px
    scene.depth_buffer_px = max(PRODUCTION_DEPTH_BUFFER_PX, scene.depth_buffer_px)
    rendered = render_hidden_line_scene(scene, context.field)
    grouped: dict[str, list[RenderedCurve]] = {}
    for curve in rendered.curves:
        if curve.pen_id not in piece.palette:
            raise MapPlotterError(
                f"Scene {piece.id!r} emitted undeclared artwork pen {curve.pen_id!r}."
            )
        grouped.setdefault(curve.pen_id, []).append(curve)
    layers: list[ArtworkLayer] = []
    filtered = 0
    for pen_id in PEN_ORDER:
        if pen_id not in grouped:
            continue
        layer, rejected = _layer_from_curves(pen_id, grouped[pen_id])
        filtered += rejected
        if layer.records:
            layers.append(layer)
    if len(layers) < 3:
        raise MapPlotterError(
            f"Scene {piece.id!r} emitted too little material hierarchy."
        )
    geometry_digest = _geometry_digest(layers)
    stats = rendered.stats.as_dict()
    details = (
        f"PS170 / OBJECT {piece.catalog_index:02d}",
        f"{piece.category.upper()} / DEPTH PASS",
        f"EDITION 01 / {len(piece.palette)} PENS",
    )
    return PlateArtwork(
        subject_id=piece.id,
        domain="abstract-3d",
        subject_kind="project-authored-3d-abstract",
        title=piece.title.upper(),
        subtitle=piece.subtitle.upper(),
        details=details,
        credit_line="PS170 / PROJECT-AUTHORED",
        scale_status="not-to-scale",
        evidence_status="project-authored-perspective-mesh",
        rights_status="project-authored",
        sources=(
            {
                "id": "abstract-3d-engine-v2",
                "kind": "project-authored-3d-renderer",
                "title": "City Map Plotter perspective hidden-line engine",
                "license": "project-authored",
                "geometry_use": "triangle meshes, native 3D curves, camera projection and visibility",
            },
        ),
        context=context,
        layers=layers,
        pen_order=PEN_ORDER,
        artifact_kind="abstract-3d-pen-art",
        rendering_preset=f"abstract-3d-{piece.scene}-v2",
        format_subject_policy="abstract-pen-art",
        source_provider="project-authored 3D scene",
        source_license="project-authored",
        data_snapshot="2026-08-09",
        notes=(
            "Genuine 3D triangle meshes are rendered through a pinned look-at perspective camera.",
            "A transient depth buffer removes hidden curves; no raster image is traced into artwork.",
            "The complete named map field is the only artwork viewport; any crop is cataloged camera framing.",
        ),
        catalog_record=piece.as_dict(),
        rendering_metadata={
            "abstract_3d": {
                "catalog_id": CATALOG_ID,
                "catalog_index": piece.catalog_index,
                "renderer_version": RENDERER_VERSION,
                "scene": piece.scene,
                "scene_sha256": rendered.scene_sha256,
                "geometry_sha256": geometry_digest,
                "seed": piece.seed,
                "category": piece.category,
                "composition": piece.composition,
                "palette": list(piece.palette),
                "camera": {
                    "projection": "perspective",
                    "eye": list(scene.camera.eye),
                    "target": list(scene.camera.target),
                    "up": list(scene.camera.up),
                    "fov_y_deg": scene.camera.fov_y_deg,
                    "near": scene.camera.near,
                    "far": scene.camera.far,
                    "lens_shift": list(scene.camera.lens_shift),
                    "view_basis_matrix": [list(row) for row in rendered.camera_matrix],
                },
                "visibility": {
                    "method": "perspective-correct-transient-depth-buffer",
                    "scene_requested_depth_buffer_px": requested_depth_buffer_px,
                    "production_minimum_depth_buffer_px": PRODUCTION_DEPTH_BUFFER_PX,
                    "raster_is_review_or_visibility_source_only": True,
                    "raster_geometry_traced": False,
                    **stats,
                },
                "filtered_sub_three_nib_fragments": filtered,
                "plottable_strokes": sum(len(layer.records) for layer in layers),
                "solid_fills_used": False,
                "raster_sources_used": False,
                "reference_geometry_imported": False,
                "stroke_order": "nearest-endpoint-within-physical-pen",
            }
        },
        border_style="none",
    )


def build_all_abstract_3d_pilots() -> tuple[PlateArtwork, ...]:
    return tuple(
        build_abstract_3d_artwork(piece.id) for piece in load_abstract_3d_pilot()
    )


def build_all_abstract_3d_artworks() -> tuple[PlateArtwork, ...]:
    return tuple(
        build_abstract_3d_artwork(piece.id) for piece in load_abstract_3d_catalog()
    )
