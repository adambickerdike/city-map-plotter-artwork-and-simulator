from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from math import floor, hypot, isfinite
from typing import Any

from .geometry import Layout, load_plate_format
from .models import LayerStyle, MapPlotterError, PlotStroke
from .stroke_font import stroke_text


INK_BALANCED_POLICY = "ink-balanced-v2"
INK_BUDGET_GATE_SCHEMA_VERSION = 2
DEFAULT_TARGET_COVERAGE = 0.2775
DEFAULT_CONTEXT_RESERVE_COVERAGE = 0.005
DEFAULT_ROAD_RESERVE_COVERAGE = 0.02
TILE_COLUMNS = 4
TILE_ROWS = 4

_RESERVE_LAYERS = (
    "roads_major",
    "roads_secondary",
    "roads_local",
    "water_areas",
    "rivers",
    "waterways",
    "railways",
    "paths",
    "green_space",
)
_ROAD_LAYERS = ("roads_major", "roads_secondary", "roads_local", "roads_other")
_FAIR_CONTEXT_LAYERS = (
    "water_areas",
    "rivers",
    "waterways",
    "railways",
    "paths",
    "green_space",
)
_SEMANTIC_ROLE_ORDER = (
    # The race course is the subject of its plate, not context for it. Ranking
    # it first means the ink budget can never cull the one line the sheet
    # exists to show -- it culls the city around it instead.
    "race_course",
    "arterial_through",
    "arterial_link",
    "secondary_through",
    "secondary_link",
    "local_street",
    "other_road",
    "principal_water_area",
    "principal_river",
    "principal_waterway",
    "minor_water_area",
    "river",
    "minor_waterway",
    "active_principal_rail",
    "subway_rail",
    "disused_rail",
    "service_rail",
    "path_context",
    "park_context",
    "building_context",
    "road_area",
    "supplemental",
)
_SEMANTIC_RANK = {role: rank for rank, role in enumerate(_SEMANTIC_ROLE_ORDER)}
_CENTRELINE_ROAD_LAYERS = frozenset(
    {"roads_major", "roads_secondary", "roads_local", "roads_other", "paths"}
)
_EPSILON = 1e-9


EffectiveNibResolver = Callable[[PlotStroke, LayerStyle], float]


def _rounded(value: float) -> float:
    return round(value, 9)


def _canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _serialized_points(stroke: PlotStroke) -> tuple[tuple[float, float], ...]:
    if len(stroke.points) < 2:
        raise MapPlotterError(
            "An ink-budget input stroke must contain at least two points."
        )
    points: list[tuple[float, float]] = []
    for point in stroke.points:
        if len(point) != 2 or not all(isfinite(value) for value in point):
            raise MapPlotterError(
                "An ink-budget input stroke contains a non-finite page coordinate."
            )
        points.append((float(f"{point[0]:.3f}"), float(f"{point[1]:.3f}")))
    return tuple(points)


def serialized_geometry_sha256(stroke: PlotStroke) -> str:
    """Fingerprint the coordinate tokens emitted at the SVG's 0.001 mm precision."""

    payload = ";".join(f"{x:.3f},{y:.3f}" for x, y in stroke.points).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _polyline_length(points: Sequence[tuple[float, float]]) -> float:
    return sum(
        hypot(end[0] - start[0], end[1] - start[1])
        for start, end in zip(points, points[1:])
    )


def _segment_length_in_rect(
    start: tuple[float, float],
    end: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> float:
    """Measure a straight segment inside a closed rectangle with Liang-Barsky."""

    left, top, right, bottom = rect
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = hypot(dx, dy)
    if length <= 0:
        return 0.0
    enter = 0.0
    leave = 1.0
    for direction, distance_to_edge in (
        (-dx, start[0] - left),
        (dx, right - start[0]),
        (-dy, start[1] - top),
        (dy, bottom - start[1]),
    ):
        if direction == 0:
            if distance_to_edge < 0:
                return 0.0
            continue
        parameter = distance_to_edge / direction
        if direction < 0:
            enter = max(enter, parameter)
        else:
            leave = min(leave, parameter)
        if enter > leave:
            return 0.0
    return length * max(0.0, leave - enter)


def _field_length(
    points: Sequence[tuple[float, float]],
    rect: tuple[float, float, float, float],
) -> float:
    return sum(
        _segment_length_in_rect(start, end, rect)
        for start, end in zip(points, points[1:])
    )


def _source_refs(stroke: PlotStroke) -> tuple[str, ...]:
    refs = tuple(
        sorted(
            {
                item.strip()
                for item in stroke.tags.get("source-refs", "").split(";")
                if item.strip()
            }
        )
    )
    if not refs:
        raise MapPlotterError(
            "Ink-balanced selection requires exact 'source-refs' provenance on "
            f"every cartographic stroke; layer {stroke.layer!r}, part "
            f"{stroke.part!r} has none."
        )
    return refs


def _canonical_source_ref(source_ref: str) -> str:
    parts = source_ref.split("/", 2)
    if len(parts) != 3 or not parts[0] or not parts[1] or not parts[2]:
        raise MapPlotterError(
            "Ink-balanced source references must have the canonical "
            f"'osm-type/osm-id/part' form; got {source_ref!r}."
        )
    return f"{parts[0]}/{parts[1]}"


def _positive_finite(value: object, *, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value <= 0
    ):
        raise MapPlotterError(f"{label} must be a positive finite number.")
    return float(value)


@dataclass(frozen=True)
class InkBudgetInputStrokeEvidence:
    index: int
    layer: str
    part: str
    source_refs: tuple[str, ...]
    canonical_source_refs: tuple[str, ...]
    serialized_length_mm: float
    field_serialized_length_mm: float
    serialized_geometry_sha256: str
    effective_nib_mm: float
    emitted_path_multiplier: int
    planned_effective_ink_mm2: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "layer": self.layer,
            "part": self.part,
            "source_refs": list(self.source_refs),
            "canonical_source_refs": list(self.canonical_source_refs),
            "serialized_length_mm": self.serialized_length_mm,
            "field_serialized_length_mm": self.field_serialized_length_mm,
            "serialized_geometry_sha256": self.serialized_geometry_sha256,
            "effective_nib_mm": self.effective_nib_mm,
            "emitted_path_multiplier": self.emitted_path_multiplier,
            "planned_effective_ink_mm2": self.planned_effective_ink_mm2,
        }


@dataclass(frozen=True)
class InkBudgetGateOmission:
    omission_id: str
    group_id: str
    layer: str
    tier: str
    stage: str
    semantic_role: str
    cullable: bool
    canonical_source_refs: tuple[str, ...]
    input_indexes: tuple[int, ...]
    input_strokes: tuple[InkBudgetInputStrokeEvidence, ...]
    serialized_length_mm: float
    field_serialized_length_mm: float
    serialized_geometry_sha256: str
    planned_effective_ink_mm2: float
    effective_nibs_mm: tuple[float, ...]
    priority: dict[str, Any]
    cutoff: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": INK_BUDGET_GATE_SCHEMA_VERSION,
            "policy": INK_BALANCED_POLICY,
            "omission_id": self.omission_id,
            "reason": "ink_budget_capacity",
            "group_id": self.group_id,
            "layer": self.layer,
            "tier": self.tier,
            "stage": self.stage,
            "semantic_role": self.semantic_role,
            "cullable": self.cullable,
            "canonical_source_refs": list(self.canonical_source_refs),
            "input_indexes": list(self.input_indexes),
            "input_strokes": [stroke.as_dict() for stroke in self.input_strokes],
            "serialized_length_mm": self.serialized_length_mm,
            "field_serialized_length_mm": self.field_serialized_length_mm,
            "serialized_geometry_sha256": self.serialized_geometry_sha256,
            "planned_effective_ink_mm2": self.planned_effective_ink_mm2,
            "effective_nibs_mm": list(self.effective_nibs_mm),
            "priority": self.priority,
            "cutoff": self.cutoff,
        }


def ink_budget_partition_sha256(
    *,
    input_count: int,
    retained_input_indexes: Sequence[int],
    omitted_groups: Sequence[tuple[str, Sequence[int]]],
) -> str:
    """Digest the complete retained/omitted input partition."""

    payload = {
        "input_count": input_count,
        "retained_input_indexes": sorted(set(retained_input_indexes)),
        "omitted_groups": [
            {
                "group_id": group_id,
                "input_indexes": sorted(set(indexes)),
            }
            for group_id, indexes in sorted(
                omitted_groups,
                key=lambda item: item[0],
            )
        ],
    }
    return _canonical_json_sha256(payload)


@dataclass(frozen=True)
class InkBudgetGateLedger:
    entries: tuple[InkBudgetGateOmission, ...]
    input_count: int
    retained_input_indexes: tuple[int, ...]
    omitted_input_indexes: tuple[int, ...]
    omitted_source_refs: tuple[str, ...]
    omitted_canonical_source_refs: tuple[str, ...]
    selection_partition_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": INK_BUDGET_GATE_SCHEMA_VERSION,
            "policy": INK_BALANCED_POLICY,
            "reason": "ink_budget_gate",
            "entries": [entry.as_dict() for entry in self.entries],
            "entry_count": len(self.entries),
            "omitted_group_count": len(self.entries),
            "input_count": self.input_count,
            "retained_input_indexes": list(self.retained_input_indexes),
            "omitted_input_indexes": list(self.omitted_input_indexes),
            "omitted_input_stroke_count": len(self.omitted_input_indexes),
            "omitted_source_refs": list(self.omitted_source_refs),
            "omitted_canonical_source_refs": list(self.omitted_canonical_source_refs),
            "uncullable_source_group_count": 0,
            "selection_partition_sha256": self.selection_partition_sha256,
        }


@dataclass(frozen=True)
class InkBudgetResult:
    strokes: list[PlotStroke]
    diagnostics: dict[str, Any]
    ink_budget_gate: InkBudgetGateLedger


@dataclass(frozen=True)
class _StrokeRecord:
    index: int
    stroke: PlotStroke
    evidence: InkBudgetInputStrokeEvidence
    points: tuple[tuple[float, float], ...]
    max_road_rank: int
    named: bool
    bridge_or_tunnel: bool


@dataclass(frozen=True)
class _StrokeGroup:
    group_id: str
    layer: str
    tier: str
    records: tuple[_StrokeRecord, ...]
    canonical_source_refs: tuple[str, ...]
    serialized_length_mm: float
    field_serialized_length_mm: float
    serialized_geometry_sha256: str
    planned_effective_ink_mm2: float
    effective_nibs_mm: tuple[float, ...]
    tile_x: int
    tile_y: int
    tile_order: int
    max_road_rank: int
    named: bool
    bridge_or_tunnel: bool
    semantic_role: str
    semantic_rank: int
    link: bool
    active_rail: bool
    rail_service: bool
    principal_water: bool
    topology_nodes: tuple[str, ...]

    @property
    def input_indexes(self) -> tuple[int, ...]:
        return tuple(record.index for record in self.records)


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, item: int) -> int:
        parent = self._parent[item]
        if parent != item:
            self._parent[item] = self.find(parent)
        return self._parent[item]

    def union(self, first: int, second: int) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root == second_root:
            return
        if first_root < second_root:
            self._parent[second_root] = first_root
        else:
            self._parent[first_root] = second_root


def _tier_for_layer(layer: str) -> str:
    return {
        "water_areas": "water_areas",
        "rivers": "rivers",
        "waterways": "waterways",
        "roads_major": "major_roads",
        "roads_secondary": "secondary_roads",
        "roads_local": "local_roads",
        "roads_other": "other_roads",
        "road_areas": "road_areas",
        "railways": "railways",
        "paths": "path_context",
        "green_space": "park_context",
    }.get(layer, "supplemental")


def _group_tag_values(records: Sequence[_StrokeRecord], key: str) -> frozenset[str]:
    return frozenset(
        value.strip().casefold()
        for record in records
        if (value := record.stroke.tags.get(key)) and value.strip()
    )


def _semantic_properties(
    layer: str,
    records: Sequence[_StrokeRecord],
) -> tuple[str, int, bool, bool, bool, bool]:
    named = any(record.named for record in records)
    highways = _group_tag_values(records, "highway")
    link = any(value.endswith("_link") for value in highways)
    role = "supplemental"
    active_rail = False
    rail_service = False
    principal_water = False

    if layer == "race_course":
        role = "race_course"
    elif layer == "buildings":
        role = "building_context"
    elif layer == "roads_major":
        role = "arterial_link" if link else "arterial_through"
    elif layer == "roads_secondary":
        role = "secondary_link" if link else "secondary_through"
    elif layer == "roads_local":
        role = "local_street"
    elif layer == "roads_other":
        role = "other_road"
    elif layer == "road_areas":
        role = "road_area"
    elif layer == "railways":
        services = _group_tag_values(records, "service")
        railways = _group_tag_values(records, "railway")
        rail_service = bool(services & {"yard", "siding", "spur", "crossover"})
        if rail_service:
            role = "service_rail"
        elif railways & {"disused", "abandoned", "razed", "construction", "proposed"}:
            role = "disused_rail"
        elif "subway" in railways:
            role = "subway_rail"
        else:
            role = "active_principal_rail"
            active_rail = True
    elif layer in {"water_areas", "rivers", "waterways"}:
        waterways = _group_tag_values(records, "waterway")
        waters = _group_tag_values(records, "water")
        naturals = _group_tag_values(records, "natural")
        explicitly_minor = bool(
            waterways & {"drain", "ditch", "stream"} or waters & {"pond", "basin"}
        )
        if layer == "water_areas":
            principal_water = not explicitly_minor and (
                named
                or bool(waterways & {"river", "riverbank"})
                or bool(waters & {"river", "reservoir", "lake"})
                or "bay" in naturals
            )
            role = "principal_water_area" if principal_water else "minor_water_area"
        elif layer == "rivers":
            principal_water = named or "river" in waterways
            role = "principal_river" if principal_water else "river"
        else:
            principal_water = not explicitly_minor and (
                named or bool(waterways & {"river", "canal"})
            )
            role = "principal_waterway" if principal_water else "minor_waterway"
    elif layer == "paths":
        role = "path_context"
    elif layer == "green_space":
        role = "park_context"

    return (
        role,
        _SEMANTIC_RANK[role],
        link,
        active_rail,
        rail_service,
        principal_water,
    )


def _selection_stage(layer: str) -> str:
    if layer in _ROAD_LAYERS:
        return "road_fill"
    if layer in _FAIR_CONTEXT_LAYERS:
        return "fair_context_fill"
    return "late_fill"


def _road_rank(stroke: PlotStroke) -> int:
    value = stroke.tags.get("road-rank", "0")
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _truthy_osm_tag(value: str | None) -> bool:
    return bool(value and value.casefold() not in {"no", "false", "0"})


def _tile_order_lookup() -> dict[tuple[int, int], int]:
    centre_x = (TILE_COLUMNS - 1) / 2
    centre_y = (TILE_ROWS - 1) / 2
    ordered = sorted(
        ((x, y) for y in range(TILE_ROWS) for x in range(TILE_COLUMNS)),
        key=lambda tile: (
            (tile[0] - centre_x) ** 2 + (tile[1] - centre_y) ** 2,
            tile[1],
            tile[0],
        ),
    )
    return {tile: index for index, tile in enumerate(ordered)}


_TILE_ORDER = _tile_order_lookup()


def _group_tile(
    records: Sequence[_StrokeRecord], layout: Layout
) -> tuple[int, int, int]:
    xs = [point[0] for record in records for point in record.points]
    ys = [point[1] for record in records for point in record.points]
    centre_x = (min(xs) + max(xs)) / 2
    centre_y = (min(ys) + max(ys)) / 2
    relative_x = (centre_x - layout.map_x_mm) / layout.map_width_mm
    relative_y = (centre_y - layout.map_y_mm) / layout.map_height_mm
    tile_x = min(TILE_COLUMNS - 1, max(0, floor(relative_x * TILE_COLUMNS)))
    tile_y = min(TILE_ROWS - 1, max(0, floor(relative_y * TILE_ROWS)))
    return tile_x, tile_y, _TILE_ORDER[(tile_x, tile_y)]


def _group_geometry_sha256(records: Sequence[_StrokeRecord]) -> str:
    return _canonical_json_sha256(
        sorted(
            (
                {
                    "layer": record.stroke.layer,
                    "part": str(record.stroke.part),
                    "source_refs": list(record.evidence.source_refs),
                    "serialized_geometry_sha256": (
                        record.evidence.serialized_geometry_sha256
                    ),
                }
                for record in records
            ),
            key=lambda item: (
                str(item["layer"]),
                str(item["part"]),
                json.dumps(item["source_refs"], separators=(",", ":")),
                str(item["serialized_geometry_sha256"]),
            ),
        )
    )


def _build_groups(
    records: Sequence[_StrokeRecord], layout: Layout
) -> list[_StrokeGroup]:
    disjoint = _DisjointSet(len(records))
    first_by_source: dict[tuple[str, str], int] = {}
    for position, record in enumerate(records):
        for source_ref in record.evidence.canonical_source_refs:
            key = (record.stroke.layer, source_ref)
            previous = first_by_source.setdefault(key, position)
            disjoint.union(previous, position)

    grouped: dict[int, list[_StrokeRecord]] = defaultdict(list)
    for position, record in enumerate(records):
        grouped[disjoint.find(position)].append(record)

    groups: list[_StrokeGroup] = []
    for grouped_records in grouped.values():
        ordered_records = tuple(sorted(grouped_records, key=lambda item: item.index))
        layers = {record.stroke.layer for record in ordered_records}
        if len(layers) != 1:
            raise MapPlotterError(
                "An atomic ink-budget source group crossed logical map layers."
            )
        layer = next(iter(layers))
        canonical_refs = tuple(
            sorted(
                {
                    source_ref
                    for record in ordered_records
                    for source_ref in record.evidence.canonical_source_refs
                }
            )
        )
        stable_identity = {
            "layer": layer,
            "canonical_source_refs": canonical_refs,
        }
        group_id = f"ink-group-{_canonical_json_sha256(stable_identity)[:20]}"
        tile_x, tile_y, tile_order = _group_tile(ordered_records, layout)
        (
            semantic_role,
            semantic_rank,
            link,
            active_rail,
            rail_service,
            principal_water,
        ) = _semantic_properties(layer, ordered_records)
        groups.append(
            _StrokeGroup(
                group_id=group_id,
                layer=layer,
                tier=_tier_for_layer(layer),
                records=ordered_records,
                canonical_source_refs=canonical_refs,
                serialized_length_mm=_rounded(
                    sum(
                        record.evidence.serialized_length_mm
                        for record in ordered_records
                    )
                ),
                field_serialized_length_mm=_rounded(
                    sum(
                        record.evidence.field_serialized_length_mm
                        for record in ordered_records
                    )
                ),
                serialized_geometry_sha256=_group_geometry_sha256(ordered_records),
                planned_effective_ink_mm2=_rounded(
                    sum(
                        record.evidence.planned_effective_ink_mm2
                        for record in ordered_records
                    )
                ),
                effective_nibs_mm=tuple(
                    sorted(
                        {record.evidence.effective_nib_mm for record in ordered_records}
                    )
                ),
                tile_x=tile_x,
                tile_y=tile_y,
                tile_order=tile_order,
                max_road_rank=max(record.max_road_rank for record in ordered_records),
                named=any(record.named for record in ordered_records),
                bridge_or_tunnel=any(
                    record.bridge_or_tunnel for record in ordered_records
                ),
                semantic_role=semantic_role,
                semantic_rank=semantic_rank,
                link=link,
                active_rail=active_rail,
                rail_service=rail_service,
                principal_water=principal_water,
                topology_nodes=tuple(
                    sorted(
                        {
                            node
                            for record in ordered_records
                            for node in (
                                record.stroke.tags.get("topology:start-node", ""),
                                record.stroke.tags.get("topology:end-node", ""),
                            )
                            if node
                        }
                    )
                ),
            )
        )
    return sorted(groups, key=lambda group: group.group_id)


def _ordered_tile_first(
    groups: Sequence[_StrokeGroup],
) -> list[tuple[_StrokeGroup, int]]:
    ordered: list[tuple[_StrokeGroup, int]] = []
    for semantic_rank in sorted({group.semantic_rank for group in groups}):
        by_tile: dict[tuple[int, int], list[_StrokeGroup]] = defaultdict(list)
        for group in groups:
            if group.semantic_rank == semantic_rank:
                by_tile[(group.tile_x, group.tile_y)].append(group)
        for tile_groups in by_tile.values():
            tile_groups.sort(
                key=lambda group: (
                    -group.max_road_rank,
                    -int(group.named),
                    -int(group.bridge_or_tunnel),
                    group.group_id,
                    group.serialized_geometry_sha256,
                )
            )

        round_index = 0
        while True:
            appended = False
            for tile in sorted(by_tile, key=lambda item: _TILE_ORDER[item]):
                tile_groups = by_tile[tile]
                if round_index < len(tile_groups):
                    ordered.append((tile_groups[round_index], round_index))
                    appended = True
            if not appended:
                break
            round_index += 1
    return ordered


def _decoration_nib(
    mapping: Mapping[str, float] | None,
    *keys: str,
    default: float,
) -> float:
    if mapping is not None:
        for key in keys:
            if key in mapping:
                return _positive_finite(
                    mapping[key], label=f"Effective nib for {key!r}"
                )
    return default


def _fixed_reservation(
    layout: Layout,
    *,
    include_frame: bool,
    include_north: bool,
    effective_nib_by_layer: Mapping[str, float] | None,
) -> tuple[float, dict[str, Any]]:
    rect = layout.clip_rect
    left, top, right, bottom = rect
    fixed: dict[str, Any] = {}
    total = 0.0

    frame_nib = _decoration_nib(
        effective_nib_by_layer,
        "frame",
        default=0.40,
    )
    frame_points = (
        (left, top),
        (right, top),
        (right, bottom),
        (left, bottom),
        (left, top),
    )
    serialized_frame = tuple(
        (float(f"{x:.3f}"), float(f"{y:.3f}")) for x, y in frame_points
    )
    frame_length = _field_length(serialized_frame, rect) if include_frame else 0.0
    frame_ink = frame_length * frame_nib
    fixed["frame"] = {
        "included": include_frame,
        "field_serialized_length_mm": _rounded(frame_length),
        "effective_nib_mm": _rounded(frame_nib),
        "planned_effective_ink_mm2": _rounded(frame_ink),
    }
    total += frame_ink

    north_nib = _decoration_nib(
        effective_nib_by_layer,
        "north",
        "map_furniture",
        default=0.25,
    )
    north_length = 0.0
    if include_north:
        north_x = right - 5.0
        north_top = top + 4.0
        north_mark = [
            (north_x, north_top + 8.0),
            (north_x, north_top),
            (north_x - 1.7, north_top + 2.4),
            (north_x, north_top),
            (north_x + 1.7, north_top + 2.4),
        ]
        cap_height = max(2.0, 8 * north_nib)
        north_label = stroke_text(
            "N",
            x_mm=north_x - 0.75,
            y_mm=north_top - max(3.0, cap_height + 1.0),
            height_mm=cap_height,
        )
        north_parts = [north_mark]
        north_parts.extend(
            stroke
            for stroke in north_label
            if len(stroke) >= 2 and _polyline_length(stroke) + _EPSILON >= 3 * north_nib
        )
        north_length = sum(
            _field_length(
                tuple((float(f"{x:.3f}"), float(f"{y:.3f}")) for x, y in points),
                rect,
            )
            for points in north_parts
        )
    north_ink = north_length * north_nib
    fixed["north"] = {
        "included": include_north,
        "field_serialized_length_mm": _rounded(north_length),
        "effective_nib_mm": _rounded(north_nib),
        "planned_effective_ink_mm2": _rounded(north_ink),
    }
    total += north_ink
    return total, fixed


def _binding_format_budget(
    layout: Layout,
) -> tuple[str, float, float, tuple[float, float, float, float]]:
    format_id = f"{layout.page.name.casefold()}-{layout.page.orientation.casefold()}"
    resolved = load_plate_format(format_id)
    try:
        page = resolved["page_mm"]
        zone = resolved["zones_mm"]["map_field"]
        budget = resolved["ink_budget"]
        page_width = float(page["width"])
        page_height = float(page["height"])
        field_rect = (
            float(zone["x"]),
            float(zone["y"]),
            float(zone["x"]) + float(zone["width"]),
            float(zone["y"]) + float(zone["height"]),
        )
        hard_max_coverage = _positive_finite(
            budget["max_coverage"],
            label=f"{format_id} hard ink coverage",
        )
        field_area = _positive_finite(
            budget["field_area_mm2"],
            label=f"{format_id} map-field area",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MapPlotterError(
            f"Plate format {format_id!r} has an invalid ink-budget contract."
        ) from exc
    if hard_max_coverage > 1:
        raise MapPlotterError(
            f"Plate format {format_id!r} has an invalid ink-coverage fraction."
        )

    if (
        abs(layout.page.width_mm - page_width) > 0.001
        or abs(layout.page.height_mm - page_height) > 0.001
    ):
        raise MapPlotterError(
            f"Layout page dimensions do not match binding format {format_id!r}."
        )
    left, top, right, bottom = layout.clip_rect
    field_left, field_top, field_right, field_bottom = field_rect
    if (
        left < field_left - 0.001
        or top < field_top - 0.001
        or right > field_right + 0.001
        or bottom > field_bottom + 0.001
    ):
        raise MapPlotterError(
            f"Layout map bounds exceed the binding {format_id!r} map field."
        )
    calculated_area = (field_right - field_left) * (field_bottom - field_top)
    if abs(calculated_area - field_area) > 0.001:
        raise MapPlotterError(
            f"Binding format {format_id!r} has inconsistent map-field area values."
        )
    return format_id, hard_max_coverage, field_area, field_rect


def _summaries_by_layer(
    groups: Sequence[_StrokeGroup], selected_group_ids: set[str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    def stats(items: Sequence[_StrokeGroup]) -> dict[str, Any]:
        return {
            "group_count": len(items),
            "stroke_count": sum(len(group.records) for group in items),
            "source_ref_count": len(
                {
                    source_ref
                    for group in items
                    for record in group.records
                    for source_ref in record.evidence.source_refs
                }
            ),
            "canonical_source_ref_count": len(
                {
                    source_ref
                    for group in items
                    for source_ref in group.canonical_source_refs
                }
            ),
            "serialized_length_mm": _rounded(
                sum(group.serialized_length_mm for group in items)
            ),
            "planned_effective_ink_mm2": _rounded(
                sum(group.planned_effective_ink_mm2 for group in items)
            ),
        }

    retained: dict[str, Any] = {}
    omitted: dict[str, Any] = {}
    combined: dict[str, Any] = {}
    for layer in sorted({group.layer for group in groups}):
        layer_groups = [group for group in groups if group.layer == layer]
        retained_groups = [
            group for group in layer_groups if group.group_id in selected_group_ids
        ]
        omitted_groups = [
            group for group in layer_groups if group.group_id not in selected_group_ids
        ]
        input_stats = stats(layer_groups)
        retained_stats = stats(retained_groups)
        omitted_stats = stats(omitted_groups)
        if retained_groups:
            retained[layer] = retained_stats
        if omitted_groups:
            omitted[layer] = omitted_stats

        def ratio(numerator: str, denominator: str | None = None) -> float:
            denominator_key = numerator if denominator is None else denominator
            total = float(input_stats[denominator_key])
            if total <= 0:
                return 1.0
            return _rounded(float(retained_stats[numerator]) / total)

        combined[layer] = {
            "tier": _tier_for_layer(layer),
            "source_ref_count": input_stats["source_ref_count"],
            "canonical_source_ref_count": input_stats["canonical_source_ref_count"],
            "candidate_group_count": input_stats["group_count"],
            "selected_group_count": retained_stats["group_count"],
            "omitted_group_count": omitted_stats["group_count"],
            "candidate_stroke_count": input_stats["stroke_count"],
            "selected_stroke_count": retained_stats["stroke_count"],
            "omitted_stroke_count": omitted_stats["stroke_count"],
            "selected_source_ref_count": retained_stats["source_ref_count"],
            "omitted_source_ref_count": omitted_stats["source_ref_count"],
            "candidate_serialized_length_mm": input_stats["serialized_length_mm"],
            "selected_serialized_length_mm": retained_stats["serialized_length_mm"],
            "omitted_serialized_length_mm": omitted_stats["serialized_length_mm"],
            "candidate_planned_effective_ink_mm2": input_stats[
                "planned_effective_ink_mm2"
            ],
            "selected_planned_effective_ink_mm2": retained_stats[
                "planned_effective_ink_mm2"
            ],
            "omitted_planned_effective_ink_mm2": omitted_stats[
                "planned_effective_ink_mm2"
            ],
            "group_retention_ratio": ratio("group_count"),
            "stroke_retention_ratio": ratio("stroke_count"),
            "source_ref_retention_ratio": ratio("source_ref_count"),
            "length_retention_ratio": ratio("serialized_length_mm"),
            "ink_retention_ratio": ratio("planned_effective_ink_mm2"),
        }
    return retained, omitted, combined


def select_ink_balanced_strokes(
    strokes: Sequence[PlotStroke],
    styles: Sequence[LayerStyle],
    layout: Layout,
    *,
    include_frame: bool,
    include_north: bool,
    effective_nib_by_layer: Mapping[str, float] | None = None,
    effective_nib_resolver: EffectiveNibResolver | None = None,
    road_style: str = "centreline",
    target_coverage: float = DEFAULT_TARGET_COVERAGE,
    context_reserve_coverage: float = DEFAULT_CONTEXT_RESERVE_COVERAGE,
) -> InkBudgetResult:
    """Select an A5-safe, geographically distributed cartographic stroke set.

    Selection is atomic by canonical OSM object within a logical layer. A stroke
    carrying several source references joins every related object into one
    indivisible group, which also keeps relation rings and topology chains intact.
    The returned strokes are the original objects in their original document order.

    ``effective_nib_resolver`` has precedence for map strokes. The optional mapping
    supplies per-layer values and the decoration keys ``frame`` and ``north`` (or
    ``map_furniture``). Values represent the physical mark width whose product with
    serialized in-field length is the planned ink area. Road geometry is explicitly
    contracted to one centreline per pass; non-road parallel strokes and every
    repeat pass are included in the planned area.
    """

    if not isinstance(include_frame, bool) or not isinstance(include_north, bool):
        raise MapPlotterError("Frame and north inclusion flags must be booleans.")
    if road_style != "centreline":
        raise MapPlotterError(
            "Ink-balanced selection requires centreline road compilation so its "
            "pre-physical ink accounting remains exact."
        )
    format_id, hard_max_coverage, field_area, binding_field_rect = (
        _binding_format_budget(layout)
    )
    if (
        isinstance(target_coverage, bool)
        or not isinstance(target_coverage, (int, float))
        or not isfinite(target_coverage)
        or target_coverage <= 0
        or target_coverage > hard_max_coverage
    ):
        raise MapPlotterError(
            f"Ink target coverage must be greater than zero and no more than "
            f"the binding {format_id} maximum of {hard_max_coverage:g}."
        )
    if (
        isinstance(context_reserve_coverage, bool)
        or not isinstance(context_reserve_coverage, (int, float))
        or not isfinite(context_reserve_coverage)
        or abs(context_reserve_coverage - DEFAULT_CONTEXT_RESERVE_COVERAGE) > _EPSILON
    ):
        raise MapPlotterError(
            "Ink-balanced-v2 requires the binding 0.005 context reserve coverage."
        )
    if (
        not isfinite(layout.map_width_mm)
        or not isfinite(layout.map_height_mm)
        or layout.map_width_mm <= 0
        or layout.map_height_mm <= 0
    ):
        raise MapPlotterError("The ink-budget map field must have positive dimensions.")

    style_by_layer: dict[str, LayerStyle] = {}
    for style in styles:
        if style.id in style_by_layer:
            raise MapPlotterError(
                f"Ink-budget styles contain duplicate layer id {style.id!r}."
            )
        style_by_layer[style.id] = style

    records: list[_StrokeRecord] = []
    for index, stroke in enumerate(strokes):
        try:
            style = style_by_layer[stroke.layer]
        except KeyError as exc:
            raise MapPlotterError(
                f"Ink-budget input layer {stroke.layer!r} has no LayerStyle."
            ) from exc
        if not style.enabled:
            raise MapPlotterError(
                f"Ink-budget input layer {stroke.layer!r} uses a disabled style."
            )
        points = _serialized_points(stroke)
        source_refs = _source_refs(stroke)
        canonical_refs = tuple(
            sorted({_canonical_source_ref(source_ref) for source_ref in source_refs})
        )
        if effective_nib_resolver is not None:
            effective_nib = _positive_finite(
                effective_nib_resolver(stroke, style),
                label=f"Resolved effective nib for layer {stroke.layer!r}",
            )
        elif (
            effective_nib_by_layer is not None
            and stroke.layer in effective_nib_by_layer
        ):
            effective_nib = _positive_finite(
                effective_nib_by_layer[stroke.layer],
                label=f"Effective nib for layer {stroke.layer!r}",
            )
        else:
            assert style.nib_mm is not None
            effective_nib = _positive_finite(
                style.nib_mm,
                label=f"Style nib for layer {stroke.layer!r}",
            )
        serialized_length = _polyline_length(points)
        field_length = _field_length(points, layout.clip_rect)
        recorded_field_length = _rounded(field_length)
        recorded_effective_nib = _rounded(effective_nib)
        emitted_path_multiplier = style.passes * (
            1 if stroke.layer in _CENTRELINE_ROAD_LAYERS else style.strokes
        )
        evidence = InkBudgetInputStrokeEvidence(
            index=index,
            layer=stroke.layer,
            part=str(stroke.part),
            source_refs=source_refs,
            canonical_source_refs=canonical_refs,
            serialized_length_mm=_rounded(serialized_length),
            field_serialized_length_mm=recorded_field_length,
            serialized_geometry_sha256=serialized_geometry_sha256(stroke),
            effective_nib_mm=recorded_effective_nib,
            emitted_path_multiplier=emitted_path_multiplier,
            planned_effective_ink_mm2=_rounded(
                recorded_field_length * recorded_effective_nib * emitted_path_multiplier
            ),
        )
        records.append(
            _StrokeRecord(
                index=index,
                stroke=stroke,
                evidence=evidence,
                points=points,
                max_road_rank=_road_rank(stroke),
                named=bool(
                    stroke.name or stroke.tags.get("name") or stroke.tags.get("ref")
                ),
                bridge_or_tunnel=(
                    _truthy_osm_tag(stroke.tags.get("bridge"))
                    or _truthy_osm_tag(stroke.tags.get("tunnel"))
                ),
            )
        )

    groups = _build_groups(records, layout)
    budget_ink = field_area * float(target_coverage)
    fixed_ink, fixed_diagnostics = _fixed_reservation(
        layout,
        include_frame=include_frame,
        include_north=include_north,
        effective_nib_by_layer=effective_nib_by_layer,
    )
    if fixed_ink > budget_ink + _EPSILON:
        raise MapPlotterError(
            "Fixed map furniture exceeds the ink-balanced plate budget: "
            f"{fixed_ink:.3f} mm² required, {budget_ink:.3f} mm² available "
            f"at {100 * target_coverage:.2f}% coverage."
        )

    groups_by_layer: dict[str, list[_StrokeGroup]] = defaultdict(list)
    groups_by_tier: dict[str, list[_StrokeGroup]] = defaultdict(list)
    group_by_id = {group.group_id: group for group in groups}
    for group in groups:
        groups_by_layer[group.layer].append(group)
        groups_by_tier[group.tier].append(group)

    selected_group_ids: set[str] = set()
    selected_map_ink = 0.0
    reserve_selected_ids: set[str] = set()
    reserve_selected_by_layer: dict[str, set[str]] = {
        layer: set() for layer in _RESERVE_LAYERS
    }
    selected_by_stage: dict[str, set[str]] = {
        "reserve_prefill": set(),
        "road_fill": set(),
        "fair_context_fill": set(),
        "late_fill": set(),
    }

    available_map_ink = max(0.0, budget_ink - fixed_ink)
    reserve_requested: dict[str, float] = {}
    reserve_claimable: dict[str, float] = {}
    reserve_candidate_ink: dict[str, float] = {}
    reserve_fittable: dict[str, list[_StrokeGroup]] = {}
    for layer in _RESERVE_LAYERS:
        layer_groups = groups_by_layer.get(layer, [])
        candidate_ink = sum(group.planned_effective_ink_mm2 for group in layer_groups)
        coverage = (
            DEFAULT_ROAD_RESERVE_COVERAGE
            if layer in {"roads_major", "roads_secondary", "roads_local"}
            else float(context_reserve_coverage)
        )
        requested = min(candidate_ink, field_area * coverage)
        fittable = [
            group
            for group in layer_groups
            if group.planned_effective_ink_mm2 <= available_map_ink + _EPSILON
        ]
        reserve_candidate_ink[layer] = candidate_ink
        reserve_requested[layer] = requested
        reserve_fittable[layer] = fittable
        reserve_claimable[layer] = min(
            requested,
            sum(group.planned_effective_ink_mm2 for group in fittable),
        )

    reserve_selected_ink = {layer: 0.0 for layer in _RESERVE_LAYERS}

    def select_reserve_group(layer: str, group: _StrokeGroup) -> None:
        nonlocal selected_map_ink
        selected_group_ids.add(group.group_id)
        reserve_selected_ids.add(group.group_id)
        reserve_selected_by_layer[layer].add(group.group_id)
        selected_by_stage["reserve_prefill"].add(group.group_id)
        selected_map_ink += group.planned_effective_ink_mm2
        reserve_selected_ink[layer] += group.planned_effective_ink_mm2

    # Seed every source-present layer before pursuing full reserve goals. Holding
    # the smallest future atomic group prevents a late layer from being starved
    # merely because its requested share cannot be divided continuously.
    minimum_fittable_ink = {
        layer: min(
            (group.planned_effective_ink_mm2 for group in reserve_fittable[layer]),
            default=0.0,
        )
        for layer in _RESERVE_LAYERS
    }
    for position, layer in enumerate(_RESERVE_LAYERS):
        if not reserve_fittable[layer]:
            continue
        future_minimum = sum(
            minimum_fittable_ink[future_layer]
            for future_layer in _RESERVE_LAYERS[position + 1 :]
            if reserve_fittable[future_layer]
            and not reserve_selected_by_layer[future_layer]
        )
        reserve_limit = max(
            0.0,
            budget_ink - fixed_ink - selected_map_ink - future_minimum,
        )
        for group, _tile_round in _ordered_tile_first(groups_by_layer.get(layer, [])):
            if group.planned_effective_ink_mm2 <= reserve_limit + _EPSILON:
                select_reserve_group(layer, group)
                break

    # Grow each seed tile-first toward its full request while preserving the
    # still-unmet, actually attainable capacity of all later reserve layers.
    for position, layer in enumerate(_RESERVE_LAYERS):
        requested = reserve_requested[layer]
        future_capacity = sum(
            max(
                0.0,
                reserve_claimable[future_layer] - reserve_selected_ink[future_layer],
            )
            for future_layer in _RESERVE_LAYERS[position + 1 :]
        )
        reserve_limit = max(
            0.0,
            budget_ink - fixed_ink - selected_map_ink - future_capacity,
        )
        for group, _tile_round in _ordered_tile_first(groups_by_layer.get(layer, [])):
            if reserve_selected_ink[layer] + _EPSILON >= requested:
                break
            if group.group_id in selected_group_ids:
                continue
            if group.planned_effective_ink_mm2 <= reserve_limit + _EPSILON:
                select_reserve_group(layer, group)
                reserve_limit -= group.planned_effective_ink_mm2

    reserve_diagnostics: dict[str, Any] = {}
    for layer in _RESERVE_LAYERS:
        layer_groups = groups_by_layer.get(layer, [])
        requested = reserve_requested[layer]
        selected_for_reserve = reserve_selected_ink[layer]
        coverage = (
            DEFAULT_ROAD_RESERVE_COVERAGE
            if layer in {"roads_major", "roads_secondary", "roads_local"}
            else float(context_reserve_coverage)
        )
        minimum_ink = min(
            (group.planned_effective_ink_mm2 for group in layer_groups),
            default=0.0,
        )
        reserve_diagnostics[layer] = {
            "present": bool(layer_groups),
            "candidate_group_count": len(layer_groups),
            "candidate_ink_mm2": _rounded(reserve_candidate_ink[layer]),
            "fittable_group_count": len(reserve_fittable[layer]),
            "minimum_group_ink_mm2": _rounded(minimum_ink),
            "requested_coverage": _rounded(coverage),
            "requested_ink_mm2": _rounded(requested),
            "selected_reserve_group_count": len(reserve_selected_by_layer[layer]),
            "selected_reserve_ink_mm2": _rounded(selected_for_reserve),
            "achieved": selected_for_reserve + _EPSILON >= requested,
            "nonzero_selected": bool(reserve_selected_by_layer[layer]),
        }

    omissions_by_group: dict[str, dict[str, Any]] = {}
    selection_decisions: dict[str, dict[str, Any]] = {}
    consideration_order = 0
    oversized_skip_count = 0
    reserve_road_groups = [
        group
        for group in groups
        if group.group_id in reserve_selected_ids and group.layer in _ROAD_LAYERS
    ]
    selected_topology_nodes = {
        node for group in reserve_road_groups for node in group.topology_nodes
    }
    initial_selected_topology_nodes = set(selected_topology_nodes)
    for group in reserve_road_groups:
        other_nodes = {
            node
            for other in reserve_road_groups
            if other.group_id != group.group_id
            for node in other.topology_nodes
        }
        shared_nodes = set(group.topology_nodes) & other_nodes
        selection_decisions[group.group_id] = {
            "stage": "reserve_prefill",
            "retained": True,
            "connected_to_selected_topology": bool(shared_nodes),
            "shared_topology_node_count": len(shared_nodes),
        }

    def consider_group(
        group: _StrokeGroup,
        *,
        tile_round: int,
        stage: str,
        topology_priority: bool = False,
    ) -> bool:
        nonlocal consideration_order, oversized_skip_count, selected_map_ink
        if group.group_id in selected_group_ids:
            return True
        consideration_order += 1
        shared_topology_nodes = (
            set(group.topology_nodes) & selected_topology_nodes
            if topology_priority
            else set()
        )
        remaining = max(0.0, budget_ink - fixed_ink - selected_map_ink)
        retained = group.planned_effective_ink_mm2 <= remaining + _EPSILON
        decision = {
            "stage": stage,
            "tile_round": tile_round,
            "consideration_order": consideration_order,
            "selected_map_ink_before_mm2": selected_map_ink,
            "remaining_ink_before_mm2": remaining,
            "connected_to_selected_topology": bool(shared_topology_nodes),
            "shared_topology_node_count": len(shared_topology_nodes),
            "retained": retained,
        }
        selection_decisions[group.group_id] = decision
        if retained:
            selected_group_ids.add(group.group_id)
            selected_by_stage[stage].add(group.group_id)
            selected_map_ink += group.planned_effective_ink_mm2
            if topology_priority:
                selected_topology_nodes.update(group.topology_nodes)
            return True
        oversized_skip_count += 1
        omissions_by_group[group.group_id] = decision
        return False

    for layer in _ROAD_LAYERS:
        ordered_roads = _ordered_tile_first(groups_by_layer.get(layer, []))
        remaining_ids = {
            group.group_id
            for group, _tile_round in ordered_roads
            if group.group_id not in selected_group_ids
        }
        while remaining_ids:
            minimum_semantic_rank = min(
                group.semantic_rank
                for group, _tile_round in ordered_roads
                if group.group_id in remaining_ids
            )
            semantic_candidates = [
                (group, tile_round)
                for group, tile_round in ordered_roads
                if group.group_id in remaining_ids
                and group.semantic_rank == minimum_semantic_rank
            ]
            connected_candidates = [
                (group, tile_round)
                for group, tile_round in semantic_candidates
                if bool(set(group.topology_nodes) & selected_topology_nodes)
            ]
            group, tile_round = (
                connected_candidates[0]
                if connected_candidates
                else semantic_candidates[0]
            )
            consider_group(
                group,
                tile_round=tile_round,
                stage="road_fill",
                topology_priority=True,
            )
            remaining_ids.remove(group.group_id)

    context_queues = {
        layer: [
            (group, tile_round)
            for group, tile_round in _ordered_tile_first(groups_by_layer.get(layer, []))
            if group.group_id not in selected_group_ids
        ]
        for layer in _FAIR_CONTEXT_LAYERS
    }
    selected_context_ink = {
        layer: sum(
            group.planned_effective_ink_mm2
            for group in groups_by_layer.get(layer, [])
            if group.group_id in selected_group_ids
        )
        for layer in _FAIR_CONTEXT_LAYERS
    }
    context_order = {
        layer: position for position, layer in enumerate(_FAIR_CONTEXT_LAYERS)
    }
    while any(context_queues.values()):
        available_layers = [
            layer for layer in _FAIR_CONTEXT_LAYERS if context_queues[layer]
        ]
        layer = min(
            available_layers,
            key=lambda candidate: (
                selected_context_ink[candidate],
                context_order[candidate],
            ),
        )
        group, tile_round = context_queues[layer].pop(0)
        if consider_group(
            group,
            tile_round=tile_round,
            stage="fair_context_fill",
        ):
            selected_context_ink[layer] += group.planned_effective_ink_mm2

    def consume_late(candidate_groups: Sequence[_StrokeGroup]) -> None:
        for group, tile_round in _ordered_tile_first(candidate_groups):
            if group.group_id not in selected_group_ids:
                consider_group(
                    group,
                    tile_round=tile_round,
                    stage="late_fill",
                )

    consume_late(groups_by_tier.get("road_areas", []))
    consume_late(groups_by_tier.get("supplemental", []))

    omitted_groups = [
        group for group in groups if group.group_id not in selected_group_ids
    ]
    if set(omissions_by_group) != {group.group_id for group in omitted_groups}:
        raise MapPlotterError(
            "Internal ink-budget error: the selection did not partition every "
            "atomic source group."
        )

    omitted_entries: list[InkBudgetGateOmission] = []
    for number, group in enumerate(
        sorted(
            omitted_groups,
            key=lambda item: omissions_by_group[item.group_id]["consideration_order"],
        ),
        start=1,
    ):
        decision = omissions_by_group[group.group_id]
        recorded_budget = _rounded(budget_ink)
        recorded_fixed = _rounded(fixed_ink)
        recorded_before = _rounded(float(decision["selected_map_ink_before_mm2"]))
        recorded_remaining = _rounded(
            max(0.0, recorded_budget - recorded_fixed - recorded_before)
        )
        omitted_entries.append(
            InkBudgetGateOmission(
                omission_id=f"ink-budget-{number}",
                group_id=group.group_id,
                layer=group.layer,
                tier=group.tier,
                stage=str(decision["stage"]),
                semantic_role=group.semantic_role,
                cullable=True,
                canonical_source_refs=group.canonical_source_refs,
                input_indexes=group.input_indexes,
                input_strokes=tuple(record.evidence for record in group.records),
                serialized_length_mm=group.serialized_length_mm,
                field_serialized_length_mm=group.field_serialized_length_mm,
                serialized_geometry_sha256=group.serialized_geometry_sha256,
                planned_effective_ink_mm2=group.planned_effective_ink_mm2,
                effective_nibs_mm=group.effective_nibs_mm,
                priority={
                    "tile_x": group.tile_x,
                    "tile_y": group.tile_y,
                    "tile_order": group.tile_order,
                    "tile_round": int(decision["tile_round"]),
                    "max_road_rank": group.max_road_rank,
                    "semantic_rank": group.semantic_rank,
                    "named": group.named,
                    "bridge_or_tunnel": group.bridge_or_tunnel,
                    "stable_group_sha256": group.serialized_geometry_sha256,
                    "consideration_order": int(decision["consideration_order"]),
                    "connected_to_selected_topology": bool(
                        decision["connected_to_selected_topology"]
                    ),
                    "shared_topology_node_count": int(
                        decision["shared_topology_node_count"]
                    ),
                    "link": group.link,
                    "active_rail": group.active_rail,
                    "rail_service": group.rail_service,
                    "principal_water": group.principal_water,
                },
                cutoff={
                    "budget_ink_mm2": recorded_budget,
                    "fixed_ink_mm2": recorded_fixed,
                    "selected_map_ink_before_mm2": recorded_before,
                    "remaining_ink_before_mm2": recorded_remaining,
                    "group_planned_effective_ink_mm2": (
                        group.planned_effective_ink_mm2
                    ),
                    "would_exceed_by_mm2": _rounded(
                        max(
                            0.0,
                            recorded_fixed
                            + recorded_before
                            + group.planned_effective_ink_mm2
                            - recorded_budget,
                        )
                    ),
                },
            )
        )

    retained_indexes = tuple(
        sorted(
            record.index
            for group in groups
            if group.group_id in selected_group_ids
            for record in group.records
        )
    )
    omitted_indexes = tuple(
        sorted(record.index for group in omitted_groups for record in group.records)
    )
    expected_indexes = tuple(range(len(records)))
    if tuple(sorted((*retained_indexes, *omitted_indexes))) != expected_indexes:
        raise MapPlotterError(
            "Internal ink-budget error: retained and omitted strokes are not an "
            "exact input partition."
        )
    partition_sha256 = ink_budget_partition_sha256(
        input_count=len(records),
        retained_input_indexes=retained_indexes,
        omitted_groups=[
            (entry.group_id, entry.input_indexes) for entry in omitted_entries
        ],
    )
    omitted_source_refs = tuple(
        sorted(
            {
                source_ref
                for entry in omitted_entries
                for input_stroke in entry.input_strokes
                for source_ref in input_stroke.source_refs
            }
        )
    )
    omitted_canonical_refs = tuple(
        sorted(
            {
                source_ref
                for entry in omitted_entries
                for source_ref in entry.canonical_source_refs
            }
        )
    )
    ledger = InkBudgetGateLedger(
        entries=tuple(omitted_entries),
        input_count=len(records),
        retained_input_indexes=retained_indexes,
        omitted_input_indexes=omitted_indexes,
        omitted_source_refs=omitted_source_refs,
        omitted_canonical_source_refs=omitted_canonical_refs,
        selection_partition_sha256=partition_sha256,
    )
    retained_by_layer, omitted_by_layer, by_layer = _summaries_by_layer(
        groups, selected_group_ids
    )

    for layer in _RESERVE_LAYERS:
        layer_groups = groups_by_layer.get(layer, [])
        final_groups = [
            group for group in layer_groups if group.group_id in selected_group_ids
        ]
        final_ink = sum(group.planned_effective_ink_mm2 for group in final_groups)
        candidate_ink = reserve_candidate_ink[layer]
        reserve_diagnostics[layer].update(
            {
                "final_selected_group_count": len(final_groups),
                "final_selected_ink_mm2": _rounded(final_ink),
                "final_ink_retention_ratio": (
                    1.0 if candidate_ink <= 0 else _rounded(final_ink / candidate_ink)
                ),
            }
        )

    selection_stages: dict[str, Any] = {}
    for stage in (
        "reserve_prefill",
        "road_fill",
        "fair_context_fill",
        "late_fill",
    ):
        selected_ids = selected_by_stage[stage]
        omitted_stage_groups = [
            group
            for group in omitted_groups
            if omissions_by_group[group.group_id]["stage"] == stage
        ]
        selection_stages[stage] = {
            "selected_group_count": len(selected_ids),
            "selected_ink_mm2": _rounded(
                sum(
                    group_by_id[group_id].planned_effective_ink_mm2
                    for group_id in selected_ids
                )
            ),
            "omitted_group_count": len(omitted_stage_groups),
            "omitted_ink_mm2": _rounded(
                sum(group.planned_effective_ink_mm2 for group in omitted_stage_groups)
            ),
        }

    semantic_by_role: dict[str, Any] = {}
    for role in _SEMANTIC_ROLE_ORDER:
        candidates = [group for group in groups if group.semantic_role == role]
        selected = [
            group for group in candidates if group.group_id in selected_group_ids
        ]
        omitted = [
            group for group in candidates if group.group_id not in selected_group_ids
        ]
        candidate_ink = sum(group.planned_effective_ink_mm2 for group in candidates)
        selected_ink = sum(group.planned_effective_ink_mm2 for group in selected)
        semantic_by_role[role] = {
            "candidate_group_count": len(candidates),
            "candidate_ink_mm2": _rounded(candidate_ink),
            "selected_group_count": len(selected),
            "selected_ink_mm2": _rounded(selected_ink),
            "omitted_group_count": len(omitted),
            "omitted_ink_mm2": _rounded(
                sum(group.planned_effective_ink_mm2 for group in omitted)
            ),
            "retention_ratio": (
                1.0 if candidate_ink <= 0 else _rounded(selected_ink / candidate_ink)
            ),
        }
    semantic_priorities = {
        "ordering": list(_SEMANTIC_ROLE_ORDER),
        "by_role": semantic_by_role,
    }

    road_connectivity_by_tier: dict[str, Any] = {}
    for layer in _ROAD_LAYERS:
        candidates = groups_by_layer.get(layer, [])
        selected = [
            group for group in candidates if group.group_id in selected_group_ids
        ]
        omitted = [
            group for group in candidates if group.group_id not in selected_group_ids
        ]

        def connected(group: _StrokeGroup) -> bool:
            return bool(
                selection_decisions.get(group.group_id, {}).get(
                    "connected_to_selected_topology", False
                )
            )

        candidate_tiles = {(group.tile_x, group.tile_y) for group in candidates}
        selected_tiles = {(group.tile_x, group.tile_y) for group in selected}
        selected_nodes = {node for group in selected for node in group.topology_nodes}
        road_connectivity_by_tier[layer] = {
            "candidate_group_count": len(candidates),
            "selected_group_count": len(selected),
            "reserve_selected_group_count": len(
                reserve_selected_by_layer.get(layer, set())
            ),
            "connected_selected_group_count": sum(
                connected(group) for group in selected
            ),
            "isolated_selected_group_count": sum(
                not connected(group) for group in selected
            ),
            "connected_omitted_group_count": sum(connected(group) for group in omitted),
            "isolated_omitted_group_count": sum(
                not connected(group) for group in omitted
            ),
            "candidate_tile_count": len(candidate_tiles),
            "selected_tile_count": len(selected_tiles),
            "topology_node_count": len(selected_nodes),
            "selection_ratio": (
                1.0 if not candidates else _rounded(len(selected) / len(candidates))
            ),
        }

    road_rows = list(road_connectivity_by_tier.values())
    all_road_candidates = [group for group in groups if group.layer in _ROAD_LAYERS]
    all_selected_roads = [
        group for group in all_road_candidates if group.group_id in selected_group_ids
    ]
    road_connectivity = {
        "initial_selected_topology_node_count": len(initial_selected_topology_nodes),
        "retained_topology_node_count": len(selected_topology_nodes),
        "candidate_group_count": sum(row["candidate_group_count"] for row in road_rows),
        "selected_group_count": sum(row["selected_group_count"] for row in road_rows),
        "connected_selected_group_count": sum(
            row["connected_selected_group_count"] for row in road_rows
        ),
        "isolated_selected_group_count": sum(
            row["isolated_selected_group_count"] for row in road_rows
        ),
        "connected_omitted_group_count": sum(
            row["connected_omitted_group_count"] for row in road_rows
        ),
        "isolated_omitted_group_count": sum(
            row["isolated_omitted_group_count"] for row in road_rows
        ),
        "candidate_tile_count": len(
            {(group.tile_x, group.tile_y) for group in all_road_candidates}
        ),
        "selected_tile_count": len(
            {(group.tile_x, group.tile_y) for group in all_selected_roads}
        ),
        "selection_ratio": (
            1.0
            if not all_road_candidates
            else _rounded(len(all_selected_roads) / len(all_road_candidates))
        ),
        "by_tier": road_connectivity_by_tier,
    }

    planned_total_ink = fixed_ink + selected_map_ink
    if planned_total_ink > budget_ink + _EPSILON:
        raise MapPlotterError(
            "Internal ink-budget error: selected strokes exceed the planned budget."
        )
    diagnostics: dict[str, Any] = {
        "schema_version": INK_BUDGET_GATE_SCHEMA_VERSION,
        "policy": INK_BALANCED_POLICY,
        "format_id": format_id,
        "road_geometry_contract": "centreline",
        "target_coverage": _rounded(float(target_coverage)),
        "hard_max_coverage": _rounded(hard_max_coverage),
        "field_area_mm2": _rounded(field_area),
        "binding_map_field": {
            "left_mm": _rounded(binding_field_rect[0]),
            "top_mm": _rounded(binding_field_rect[1]),
            "right_mm": _rounded(binding_field_rect[2]),
            "bottom_mm": _rounded(binding_field_rect[3]),
        },
        "budget_ink_mm2": _rounded(budget_ink),
        "fixed_reservation": fixed_diagnostics,
        "fixed_ink_mm2": _rounded(fixed_ink),
        "selected_map_ink_mm2": _rounded(selected_map_ink),
        "planned_total_ink_mm2": _rounded(planned_total_ink),
        "planned_coverage": _rounded(planned_total_ink / field_area),
        "budget_headroom_mm2": _rounded(budget_ink - planned_total_ink),
        "input_stroke_count": len(records),
        "retained_stroke_count": len(retained_indexes),
        "omitted_stroke_count": len(omitted_indexes),
        "input_group_count": len(groups),
        "retained_group_count": len(selected_group_ids),
        "omitted_group_count": len(omitted_groups),
        "uncullable_source_group_count": 0,
        "oversized_groups_skipped_while_continuing": oversized_skip_count,
        "reserves": reserve_diagnostics,
        "selection_stages": selection_stages,
        "semantic_priorities": semantic_priorities,
        "road_connectivity": road_connectivity,
        "retained_by_layer": retained_by_layer,
        "omitted_by_layer": omitted_by_layer,
        "by_layer": by_layer,
        "retained_input_indexes": list(retained_indexes),
        "omitted_input_indexes": list(omitted_indexes),
        "selection_partition_sha256": partition_sha256,
    }
    return InkBudgetResult(
        strokes=[strokes[index] for index in retained_indexes],
        diagnostics=diagnostics,
        ink_budget_gate=ledger,
    )
