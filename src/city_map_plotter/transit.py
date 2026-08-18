"""Source-qualified passenger rail and urban-transit network contracts.

This module deliberately keeps a passenger service graph separate from the
existing ``railways`` basemap layer.  A physical rail way proves that track is
present; it does not prove that a named operator or line currently serves it.

The normalized contract is intentionally small and boring: geographic nodes,
atomic geographic edges, display lines, ordered service patterns, and explicit
source records.  Renderers may simplify those coordinates for a particular
sheet, but they may not invent connectivity or silently drop a route branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
import hashlib
import json
from importlib.resources import files
from math import isfinite
from pathlib import Path
import re
from typing import Any, Iterable, NoReturn

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .models import BoundingBox, MapPlotterError


TRANSIT_CATALOG_RESOURCE = "data/transit-catalog-v1.json"
TRANSIT_CONTRACT_SCHEMA_RESOURCE = "data/transit-network-schema-v1.json"
TRANSIT_SCHEMA_VERSION = 1
TRANSIT_CATALOG_SCHEMA_VERSION = 1

_STABLE_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_HEX_COLOUR = re.compile(r"#[0-9A-Fa-f]{6}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

COLOUR_ROLES = frozenset(
    {"line", "operator-network", "service-pattern", "service-level", "house"}
)
COLOUR_PROVENANCE = frozenset(
    {
        "published-standard",
        "official-map-sample",
        "licensed-source-data",
        "community-source-tag",
        "house-palette",
    }
)
NUMERIC_COLOUR_STATUS = frozenset(
    {"official-numeric", "sampled-not-standard", "community-value", "house-value"}
)
PEN_MATCH_STATUSES = frozenset(
    {"exact-measured", "nominal-unmeasured", "approximate", "unresolved"}
)
NODE_KINDS = frozenset({"junction", "station", "terminal", "interchange", "portal"})
STATION_TIERS = frozenset({"local", "major", "interchange", "terminal"})
NETWORK_KINDS = frozenset(
    {
        "national-operator",
        "national-operator-overview",
        "regional-rail",
        "metro",
        "subway",
        "tram",
        "light-rail",
    }
)
SOURCE_REUSE_STATUSES = frozenset(
    {"commercial-allowed", "permission-required", "review-required", "unknown"}
)


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(message)


def _object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{field} must be an object.")
    return value


def _list(value: Any, *, field: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{field} must be a list.")
    return value


def _text(value: Any, *, field: str) -> str:
    result = value.strip() if isinstance(value, str) else ""
    if not result:
        _fail(f"{field} must be non-empty text.")
    return result


def _id(value: Any, *, field: str) -> str:
    result = _text(value, field=field)
    if _STABLE_ID.fullmatch(result) is None:
        _fail(f"{field} must use lower-case letters, digits, and hyphens.")
    return result


def _number(value: Any, *, field: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{field} must be a finite number.")
    result = float(value)
    if not isfinite(result) or (positive and result <= 0.0):
        qualifier = "positive " if positive else ""
        _fail(f"{field} must be a {qualifier}finite number.")
    return result


def _date(value: Any, *, field: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    result = _text(value, field=field)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise MapPlotterError(f"{field} must be an ISO-8601 date.") from exc
    return result


def _coordinates(value: Any, *, field: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        _fail(f"{field} must be a [longitude, latitude] pair.")
    lon = _number(value[0], field=f"{field}[0]")
    lat = _number(value[1], field=f"{field}[1]")
    if not -180.0 <= lon <= 180.0 or not -85.0 <= lat <= 85.0:
        _fail(f"{field} lies outside supported WGS84 bounds.")
    return (lon, lat)


def _unique_ids(values: Iterable[str], *, field: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        _fail(f"{field} repeats IDs: {', '.join(sorted(duplicates))}.")


@dataclass(frozen=True, slots=True)
class TransitSource:
    id: str
    publisher: str
    url: str
    licence: str
    attribution: str
    retrieved_at: str
    sha256: str
    use: str
    commercial_reuse_status: str
    valid_from: str | None = None
    valid_to: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, *, index: int) -> "TransitSource":
        value = _object(raw, field=f"sources[{index}]")
        digest = _text(value.get("sha256"), field=f"sources[{index}].sha256")
        if _SHA256.fullmatch(digest) is None:
            _fail(f"sources[{index}].sha256 must be 64 lower-case hex digits.")
        reuse = _text(
            value.get("commercial_reuse_status"),
            field=f"sources[{index}].commercial_reuse_status",
        )
        if reuse not in SOURCE_REUSE_STATUSES:
            _fail(
                f"sources[{index}].commercial_reuse_status must be one of "
                f"{', '.join(sorted(SOURCE_REUSE_STATUSES))}."
            )
        valid_from = _date(
            value.get("valid_from"),
            field=f"sources[{index}].valid_from",
            optional=True,
        )
        valid_to = _date(
            value.get("valid_to"),
            field=f"sources[{index}].valid_to",
            optional=True,
        )
        if valid_from and valid_to and valid_from > valid_to:
            _fail(f"sources[{index}] has valid_from after valid_to.")
        return cls(
            id=_id(value.get("id"), field=f"sources[{index}].id"),
            publisher=_text(
                value.get("publisher"), field=f"sources[{index}].publisher"
            ),
            url=_text(value.get("url"), field=f"sources[{index}].url"),
            licence=_text(value.get("licence"), field=f"sources[{index}].licence"),
            attribution=_text(
                value.get("attribution"), field=f"sources[{index}].attribution"
            ),
            retrieved_at=_date(
                value.get("retrieved_at"),
                field=f"sources[{index}].retrieved_at",
            )
            or "",
            sha256=digest,
            use=_text(value.get("use"), field=f"sources[{index}].use"),
            commercial_reuse_status=reuse,
            valid_from=valid_from,
            valid_to=valid_to,
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "publisher": self.publisher,
            "url": self.url,
            "licence": self.licence,
            "attribution": self.attribution,
            "retrieved_at": self.retrieved_at,
            "sha256": self.sha256,
            "use": self.use,
            "commercial_reuse_status": self.commercial_reuse_status,
        }
        if self.valid_from:
            result["valid_from"] = self.valid_from
        if self.valid_to:
            result["valid_to"] = self.valid_to
        return result


@dataclass(frozen=True, slots=True)
class ColourSpec:
    name: str
    display_hex: str
    role: str
    provenance: str
    numeric_value_status: str
    source_ref: str

    @classmethod
    def from_dict(cls, raw: Any, *, field: str) -> "ColourSpec":
        value = _object(raw, field=field)
        display_hex = _text(value.get("display_hex"), field=f"{field}.display_hex")
        if _HEX_COLOUR.fullmatch(display_hex) is None:
            _fail(f"{field}.display_hex must be a six-digit #RRGGBB colour.")
        role = _text(value.get("role"), field=f"{field}.role")
        provenance = _text(value.get("provenance"), field=f"{field}.provenance")
        numeric = _text(
            value.get("numeric_value_status"),
            field=f"{field}.numeric_value_status",
        )
        if role not in COLOUR_ROLES:
            _fail(f"{field}.role is not a supported colour role.")
        if provenance not in COLOUR_PROVENANCE:
            _fail(f"{field}.provenance is not supported.")
        if numeric not in NUMERIC_COLOUR_STATUS:
            _fail(f"{field}.numeric_value_status is not supported.")
        if numeric == "official-numeric" and provenance != "published-standard":
            _fail(
                f"{field} may claim an official numeric colour only from a "
                "published standard."
            )
        return cls(
            name=_text(value.get("name"), field=f"{field}.name"),
            display_hex=display_hex.upper(),
            role=role,
            provenance=provenance,
            numeric_value_status=numeric,
            source_ref=_id(value.get("source_ref"), field=f"{field}.source_ref"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "display_hex": self.display_hex,
            "role": self.role,
            "provenance": self.provenance,
            "numeric_value_status": self.numeric_value_status,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True, slots=True)
class TransitPen:
    ink: str
    nominal_nib_mm: float
    match_status: str
    pen_id: str | None = None
    calibration_state: str = "nominal-unmeasured"
    preview_hex: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, *, field: str) -> "TransitPen":
        value = _object(raw, field=field)
        status = _text(value.get("match_status"), field=f"{field}.match_status")
        if status not in PEN_MATCH_STATUSES:
            _fail(f"{field}.match_status is not supported.")
        pen_id_raw = value.get("pen_id")
        pen_id = (
            None if pen_id_raw is None else _id(pen_id_raw, field=f"{field}.pen_id")
        )
        if status == "exact-measured" and pen_id is None:
            _fail(f"{field} needs pen_id for an exact measured match.")
        preview_raw = value.get("preview_hex")
        preview = None
        if preview_raw is not None:
            preview = _text(preview_raw, field=f"{field}.preview_hex").upper()
            if _HEX_COLOUR.fullmatch(preview) is None:
                _fail(f"{field}.preview_hex must be a six-digit #RRGGBB colour.")
        return cls(
            ink=_text(value.get("ink"), field=f"{field}.ink"),
            nominal_nib_mm=_number(
                value.get("nominal_nib_mm"),
                field=f"{field}.nominal_nib_mm",
                positive=True,
            ),
            match_status=status,
            pen_id=pen_id,
            calibration_state=_text(
                value.get("calibration_state", "nominal-unmeasured"),
                field=f"{field}.calibration_state",
            ),
            preview_hex=preview,
        )

    @property
    def plot_key(self) -> str:
        if self.pen_id:
            return self.pen_id
        slug = re.sub(r"[^a-z0-9]+", "-", self.ink.casefold()).strip("-")
        nib = f"{self.nominal_nib_mm:g}".replace(".", "-")
        return f"required-{slug}-{nib}"

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ink": self.ink,
            "nominal_nib_mm": self.nominal_nib_mm,
            "match_status": self.match_status,
            "calibration_state": self.calibration_state,
        }
        if self.pen_id:
            result["pen_id"] = self.pen_id
        if self.preview_hex:
            result["preview_hex"] = self.preview_hex
        return result


@dataclass(frozen=True, slots=True)
class TransitLine:
    id: str
    name: str
    short_name: str
    order: int
    colour: ColourSpec
    pen: TransitPen
    service_class: str
    source_ref: str

    @classmethod
    def from_dict(cls, raw: Any, *, index: int) -> "TransitLine":
        field = f"lines[{index}]"
        value = _object(raw, field=field)
        order_raw = value.get("order")
        if (
            isinstance(order_raw, bool)
            or not isinstance(order_raw, int)
            or order_raw < 0
        ):
            _fail(f"{field}.order must be a non-negative integer.")
        return cls(
            id=_id(value.get("id"), field=f"{field}.id"),
            name=_text(value.get("name"), field=f"{field}.name"),
            short_name=_text(value.get("short_name"), field=f"{field}.short_name"),
            order=order_raw,
            colour=ColourSpec.from_dict(value.get("colour"), field=f"{field}.colour"),
            pen=TransitPen.from_dict(value.get("pen"), field=f"{field}.pen"),
            service_class=_text(
                value.get("service_class", "regular"),
                field=f"{field}.service_class",
            ),
            source_ref=_id(value.get("source_ref"), field=f"{field}.source_ref"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "short_name": self.short_name,
            "order": self.order,
            "colour": self.colour.as_dict(),
            "pen": self.pen.as_dict(),
            "service_class": self.service_class,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True, slots=True)
class TransitNode:
    id: str
    kind: str
    lon: float
    lat: float
    source_ref: str
    name: str | None = None
    station_tier: str | None = None
    source_object: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, *, index: int) -> "TransitNode":
        field = f"nodes[{index}]"
        value = _object(raw, field=field)
        kind = _text(value.get("kind"), field=f"{field}.kind")
        if kind not in NODE_KINDS:
            _fail(f"{field}.kind is not supported.")
        lon, lat = _coordinates(value.get("position"), field=f"{field}.position")
        tier_raw = value.get("station_tier")
        tier = (
            None if tier_raw is None else _text(tier_raw, field=f"{field}.station_tier")
        )
        if tier is not None and tier not in STATION_TIERS:
            _fail(f"{field}.station_tier is not supported.")
        if kind in {"station", "terminal", "interchange"} and not value.get("name"):
            _fail(f"{field} needs a name because it is a station node.")
        return cls(
            id=_id(value.get("id"), field=f"{field}.id"),
            kind=kind,
            lon=lon,
            lat=lat,
            source_ref=_id(value.get("source_ref"), field=f"{field}.source_ref"),
            name=(
                _text(value.get("name"), field=f"{field}.name")
                if value.get("name") is not None
                else None
            ),
            station_tier=tier,
            source_object=(
                _text(value.get("source_object"), field=f"{field}.source_object")
                if value.get("source_object") is not None
                else None
            ),
        )

    @property
    def position(self) -> tuple[float, float]:
        return (self.lon, self.lat)

    @property
    def is_station(self) -> bool:
        return self.kind in {"station", "terminal", "interchange"}

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "position": [self.lon, self.lat],
            "source_ref": self.source_ref,
        }
        if self.name:
            result["name"] = self.name
        if self.station_tier:
            result["station_tier"] = self.station_tier
        if self.source_object:
            result["source_object"] = self.source_object
        return result


@dataclass(frozen=True, slots=True)
class TransitEdge:
    id: str
    from_node: str
    to_node: str
    geometry: tuple[tuple[float, float], ...]
    line_ids: tuple[str, ...]
    source_ref: str
    source_object: str
    status: str = "operational"
    grade: str = "unknown"

    @classmethod
    def from_dict(cls, raw: Any, *, index: int) -> "TransitEdge":
        field = f"edges[{index}]"
        value = _object(raw, field=field)
        points = tuple(
            _coordinates(point, field=f"{field}.geometry[{point_index}]")
            for point_index, point in enumerate(
                _list(value.get("geometry"), field=f"{field}.geometry")
            )
        )
        if len(points) < 2:
            _fail(f"{field}.geometry needs at least two points.")
        if all(point == points[0] for point in points[1:]):
            _fail(f"{field}.geometry is degenerate.")
        line_ids = tuple(
            _id(item, field=f"{field}.line_ids[{line_index}]")
            for line_index, item in enumerate(
                _list(value.get("line_ids"), field=f"{field}.line_ids")
            )
        )
        if not line_ids:
            _fail(f"{field}.line_ids cannot be empty.")
        _unique_ids(line_ids, field=f"{field}.line_ids")
        return cls(
            id=_id(value.get("id"), field=f"{field}.id"),
            from_node=_id(value.get("from_node"), field=f"{field}.from_node"),
            to_node=_id(value.get("to_node"), field=f"{field}.to_node"),
            geometry=points,
            line_ids=line_ids,
            source_ref=_id(value.get("source_ref"), field=f"{field}.source_ref"),
            source_object=_text(
                value.get("source_object"), field=f"{field}.source_object"
            ),
            status=_text(value.get("status", "operational"), field=f"{field}.status"),
            grade=_text(value.get("grade", "unknown"), field=f"{field}.grade"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from_node": self.from_node,
            "to_node": self.to_node,
            "geometry": [[lon, lat] for lon, lat in self.geometry],
            "line_ids": list(self.line_ids),
            "source_ref": self.source_ref,
            "source_object": self.source_object,
            "status": self.status,
            "grade": self.grade,
        }


@dataclass(frozen=True, slots=True)
class EdgeTraversal:
    edge_id: str
    direction: str

    @classmethod
    def from_dict(cls, raw: Any, *, field: str) -> "EdgeTraversal":
        value = _object(raw, field=field)
        direction = _text(value.get("direction"), field=f"{field}.direction")
        if direction not in {"forward", "reverse"}:
            _fail(f"{field}.direction must be forward or reverse.")
        return cls(
            edge_id=_id(value.get("edge_id"), field=f"{field}.edge_id"),
            direction=direction,
        )

    def as_dict(self) -> dict[str, str]:
        return {"edge_id": self.edge_id, "direction": self.direction}


@dataclass(frozen=True, slots=True)
class ServicePattern:
    id: str
    line_id: str
    name: str
    traversals: tuple[EdgeTraversal, ...]
    station_ids: tuple[str, ...]
    source_ref: str
    valid_from: str | None = None
    valid_to: str | None = None
    derivation_status: str = "source-order"
    continuity_breaks: tuple[int, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, *, index: int) -> "ServicePattern":
        field = f"service_patterns[{index}]"
        value = _object(raw, field=field)
        traversals = tuple(
            EdgeTraversal.from_dict(item, field=f"{field}.traversals[{item_index}]")
            for item_index, item in enumerate(
                _list(value.get("traversals"), field=f"{field}.traversals")
            )
        )
        if not traversals:
            _fail(f"{field}.traversals cannot be empty.")
        station_ids = tuple(
            _id(item, field=f"{field}.station_ids[{item_index}]")
            for item_index, item in enumerate(
                _list(value.get("station_ids", []), field=f"{field}.station_ids")
            )
        )
        breaks: list[int] = []
        for break_index, item in enumerate(
            _list(
                value.get("continuity_breaks", []), field=f"{field}.continuity_breaks"
            )
        ):
            if (
                isinstance(item, bool)
                or not isinstance(item, int)
                or not 0 <= item < len(traversals) - 1
            ):
                _fail(
                    f"{field}.continuity_breaks[{break_index}] must index a gap "
                    "between traversals."
                )
            breaks.append(item)
        # Circular and out-and-back services may legitimately visit the same
        # station more than once.  ``station_ids`` is an ordered visit list,
        # not a set, so preserving those repeats is part of topology fidelity.
        valid_from = _date(
            value.get("valid_from"), field=f"{field}.valid_from", optional=True
        )
        valid_to = _date(
            value.get("valid_to"), field=f"{field}.valid_to", optional=True
        )
        if valid_from and valid_to and valid_from > valid_to:
            _fail(f"{field} has valid_from after valid_to.")
        return cls(
            id=_id(value.get("id"), field=f"{field}.id"),
            line_id=_id(value.get("line_id"), field=f"{field}.line_id"),
            name=_text(value.get("name"), field=f"{field}.name"),
            traversals=traversals,
            station_ids=station_ids,
            source_ref=_id(value.get("source_ref"), field=f"{field}.source_ref"),
            valid_from=valid_from,
            valid_to=valid_to,
            derivation_status=_text(
                value.get("derivation_status", "source-order"),
                field=f"{field}.derivation_status",
            ),
            continuity_breaks=tuple(sorted(set(breaks))),
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "line_id": self.line_id,
            "name": self.name,
            "traversals": [item.as_dict() for item in self.traversals],
            "station_ids": list(self.station_ids),
            "source_ref": self.source_ref,
            "derivation_status": self.derivation_status,
            "continuity_breaks": list(self.continuity_breaks),
        }
        if self.valid_from:
            result["valid_from"] = self.valid_from
        if self.valid_to:
            result["valid_to"] = self.valid_to
        return result


@dataclass(frozen=True, slots=True)
class ContextFeature:
    id: str
    kind: str
    geometry: tuple[tuple[float, float], ...]
    source_ref: str
    source_object: str
    source_layer: str | None = None
    source_tags: tuple[tuple[str, str], ...] = ()
    node_refs: tuple[str, ...] = ()
    geometry_type: str = "line"
    ring_role: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, *, index: int) -> "ContextFeature":
        field = f"context[{index}]"
        value = _object(raw, field=field)
        points = tuple(
            _coordinates(item, field=f"{field}.geometry[{item_index}]")
            for item_index, item in enumerate(
                _list(value.get("geometry"), field=f"{field}.geometry")
            )
        )
        if len(points) < 2:
            _fail(f"{field}.geometry needs at least two points.")
        source_layer_raw = value.get("source_layer")
        source_layer = (
            None
            if source_layer_raw is None
            else _text(source_layer_raw, field=f"{field}.source_layer")
        )
        source_tags_raw = value.get("source_tags", {})
        if not isinstance(source_tags_raw, dict):
            _fail(f"{field}.source_tags must be an object.")
        source_tags: list[tuple[str, str]] = []
        for key, item in sorted(source_tags_raw.items()):
            tag_key = _text(key, field=f"{field}.source_tags key")
            tag_value = _text(item, field=f"{field}.source_tags.{tag_key}")
            source_tags.append((tag_key, tag_value))
        node_refs_raw = value.get("node_refs", [])
        if not isinstance(node_refs_raw, list):
            _fail(f"{field}.node_refs must be a list.")
        node_refs = tuple(
            _text(item, field=f"{field}.node_refs[{item_index}]")
            for item_index, item in enumerate(node_refs_raw)
        )
        geometry_type = _text(
            value.get("geometry_type", "line"), field=f"{field}.geometry_type"
        )
        if geometry_type not in {"line", "area-ring"}:
            _fail(f"{field}.geometry_type must be line or area-ring.")
        ring_role_raw = value.get("ring_role")
        ring_role = (
            None
            if ring_role_raw is None
            else _text(ring_role_raw, field=f"{field}.ring_role")
        )
        if ring_role is not None and ring_role not in {"outer", "inner"}:
            _fail(f"{field}.ring_role must be outer or inner.")
        if ring_role is not None and geometry_type != "area-ring":
            _fail(f"{field}.ring_role requires geometry_type area-ring.")
        return cls(
            id=_id(value.get("id"), field=f"{field}.id"),
            kind=_id(value.get("kind"), field=f"{field}.kind"),
            geometry=points,
            source_ref=_id(value.get("source_ref"), field=f"{field}.source_ref"),
            source_object=_text(
                value.get("source_object"), field=f"{field}.source_object"
            ),
            source_layer=source_layer,
            source_tags=tuple(source_tags),
            node_refs=node_refs,
            geometry_type=geometry_type,
            ring_role=ring_role,
        )

    @property
    def source_tag_map(self) -> dict[str, str]:
        return dict(self.source_tags)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "geometry": [[lon, lat] for lon, lat in self.geometry],
            "source_ref": self.source_ref,
            "source_object": self.source_object,
        }
        if self.source_layer is not None:
            result["source_layer"] = self.source_layer
        if self.source_tags:
            result["source_tags"] = dict(self.source_tags)
        if self.node_refs:
            result["node_refs"] = list(self.node_refs)
        if self.geometry_type != "line":
            result["geometry_type"] = self.geometry_type
        if self.ring_role is not None:
            result["ring_role"] = self.ring_role
        return result


@dataclass(frozen=True, slots=True)
class TransitNetwork:
    id: str
    name: str
    kind: str
    scope: str
    format_id: str
    snapshot: str
    validity_status: str
    geometry_mode: str
    sources: tuple[TransitSource, ...]
    lines: tuple[TransitLine, ...]
    nodes: tuple[TransitNode, ...]
    edges: tuple[TransitEdge, ...]
    service_patterns: tuple[ServicePattern, ...]
    context: tuple[ContextFeature, ...]
    omissions: tuple[dict[str, Any], ...]
    notes: tuple[str, ...]
    contract_sha256: str

    @property
    def line_by_id(self) -> dict[str, TransitLine]:
        return {line.id: line for line in self.lines}

    @property
    def node_by_id(self) -> dict[str, TransitNode]:
        return {node.id: node for node in self.nodes}

    @property
    def edge_by_id(self) -> dict[str, TransitEdge]:
        return {edge.id: edge for edge in self.edges}

    @property
    def source_by_id(self) -> dict[str, TransitSource]:
        return {source.id: source for source in self.sources}

    def bbox(self) -> BoundingBox:
        coordinates = [point for edge in self.edges for point in edge.geometry]
        if not coordinates:
            _fail(f"Transit network {self.id!r} has no edge coordinates.")
        longitudes = [point[0] for point in coordinates]
        latitudes = [point[1] for point in coordinates]
        return BoundingBox(
            min(longitudes), min(latitudes), max(longitudes), max(latitudes)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TRANSIT_SCHEMA_VERSION,
            "network": {
                "id": self.id,
                "name": self.name,
                "kind": self.kind,
                "scope": self.scope,
                "format_id": self.format_id,
                "snapshot": self.snapshot,
                "validity_status": self.validity_status,
                "geometry_mode": self.geometry_mode,
            },
            "sources": [source.as_dict() for source in self.sources],
            "lines": [line.as_dict() for line in self.lines],
            "nodes": [node.as_dict() for node in self.nodes],
            "edges": [edge.as_dict() for edge in self.edges],
            "service_patterns": [
                pattern.as_dict() for pattern in self.service_patterns
            ],
            "context": [feature.as_dict() for feature in self.context],
            "omissions": [dict(item) for item in self.omissions],
            "notes": list(self.notes),
        }


def _load_document(path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise MapPlotterError(f"Could not read transit contract {path}: {exc}") from exc
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MapPlotterError(
            f"Transit contract {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        _fail(f"Transit contract {path} must contain one JSON object.")
    return value, hashlib.sha256(payload).hexdigest()


@lru_cache(maxsize=1)
def _contract_schema_validator() -> Draft202012Validator:
    """Load the published contract schema used by normal CLI/API reads.

    Keeping the validator pointed at the checked-in public contract avoids a
    second, permissive Python-only interpretation of the format.  Semantic
    topology and provenance checks still run after this structural gate.
    """

    resource = files("city_map_plotter").joinpath(
        "data", "transit-network-schema-v1.json"
    )
    try:
        schema = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MapPlotterError(
            "Could not load the published transit contract schema at "
            f"{TRANSIT_CONTRACT_SCHEMA_RESOURCE}: {exc}"
        ) from exc
    if not isinstance(schema, dict):
        _fail("The published transit contract schema must contain one JSON object.")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise MapPlotterError(
            f"The published transit contract schema is invalid: {exc.message}"
        ) from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate_contract_schema(raw: dict[str, Any], *, path: Path) -> None:
    errors = sorted(
        _contract_schema_validator().iter_errors(raw),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    location = error.json_path or "$"
    raise MapPlotterError(
        f"Transit contract {path} violates the published Draft 2020-12 "
        f"schema at {location}: {error.message}"
    )


def load_transit_network(path: Path) -> TransitNetwork:
    raw, digest = _load_document(path)
    if raw.get("schema_version") != TRANSIT_SCHEMA_VERSION:
        _fail(
            f"Transit contract {path} must use schema_version {TRANSIT_SCHEMA_VERSION}."
        )
    network_raw = _object(raw.get("network"), field="network")
    kind = _text(network_raw.get("kind"), field="network.kind")
    if kind not in NETWORK_KINDS:
        _fail("network.kind is not supported.")
    sources = tuple(
        TransitSource.from_dict(value, index=index)
        for index, value in enumerate(_list(raw.get("sources"), field="sources"))
    )
    lines = tuple(
        TransitLine.from_dict(value, index=index)
        for index, value in enumerate(_list(raw.get("lines"), field="lines"))
    )
    nodes = tuple(
        TransitNode.from_dict(value, index=index)
        for index, value in enumerate(_list(raw.get("nodes"), field="nodes"))
    )
    edges = tuple(
        TransitEdge.from_dict(value, index=index)
        for index, value in enumerate(_list(raw.get("edges"), field="edges"))
    )
    patterns = tuple(
        ServicePattern.from_dict(value, index=index)
        for index, value in enumerate(
            _list(raw.get("service_patterns"), field="service_patterns")
        )
    )
    context = tuple(
        ContextFeature.from_dict(value, index=index)
        for index, value in enumerate(_list(raw.get("context", []), field="context"))
    )
    if not sources or not lines or not nodes or not edges or not patterns:
        _fail(
            "A transit contract needs sources, lines, nodes, edges, and service patterns."
        )
    omissions_raw = _list(raw.get("omissions", []), field="omissions")
    omissions = tuple(
        dict(_object(value, field=f"omissions[{index}]"))
        for index, value in enumerate(omissions_raw)
    )
    notes = tuple(
        _text(value, field=f"notes[{index}]")
        for index, value in enumerate(_list(raw.get("notes", []), field="notes"))
    )
    result = TransitNetwork(
        id=_id(network_raw.get("id"), field="network.id"),
        name=_text(network_raw.get("name"), field="network.name"),
        kind=kind,
        scope=_text(network_raw.get("scope"), field="network.scope"),
        format_id=_id(network_raw.get("format_id"), field="network.format_id"),
        snapshot=_date(network_raw.get("snapshot"), field="network.snapshot") or "",
        validity_status=_text(
            network_raw.get("validity_status"), field="network.validity_status"
        ),
        geometry_mode=_text(
            network_raw.get("geometry_mode"), field="network.geometry_mode"
        ),
        sources=sources,
        lines=lines,
        nodes=nodes,
        edges=edges,
        service_patterns=patterns,
        context=context,
        omissions=omissions,
        notes=notes,
        contract_sha256=digest,
    )
    validate_transit_network(result)
    _validate_contract_schema(raw, path=path)
    return result


def validate_transit_network(network: TransitNetwork) -> None:
    _unique_ids((source.id for source in network.sources), field="sources")
    _unique_ids((line.id for line in network.lines), field="lines")
    _unique_ids((node.id for node in network.nodes), field="nodes")
    _unique_ids((edge.id for edge in network.edges), field="edges")
    _unique_ids(
        (pattern.id for pattern in network.service_patterns), field="service_patterns"
    )
    _unique_ids((feature.id for feature in network.context), field="context")
    source_by_id = network.source_by_id
    line_by_id = network.line_by_id
    node_by_id = network.node_by_id
    edge_by_id = network.edge_by_id
    source_ids = set(source_by_id)
    line_ids = set(line_by_id)
    node_ids = set(node_by_id)
    edge_ids = set(edge_by_id)
    for line in network.lines:
        for ref_name, ref in (
            ("source_ref", line.source_ref),
            ("colour.source_ref", line.colour.source_ref),
        ):
            if ref not in source_ids:
                _fail(f"Line {line.id!r} {ref_name} names missing source {ref!r}.")
    for node in network.nodes:
        if node.source_ref not in source_ids:
            _fail(f"Node {node.id!r} names missing source {node.source_ref!r}.")
    tolerance = 1e-7
    for edge in network.edges:
        if edge.source_ref not in source_ids:
            _fail(f"Edge {edge.id!r} names missing source {edge.source_ref!r}.")
        if edge.from_node not in node_ids or edge.to_node not in node_ids:
            _fail(f"Edge {edge.id!r} references a missing endpoint node.")
        unknown_lines = sorted(set(edge.line_ids) - line_ids)
        if unknown_lines:
            _fail(f"Edge {edge.id!r} names missing lines: {', '.join(unknown_lines)}.")
        from_pos = node_by_id[edge.from_node].position
        to_pos = node_by_id[edge.to_node].position
        if (
            max(abs(edge.geometry[0][axis] - from_pos[axis]) for axis in (0, 1))
            > tolerance
        ):
            _fail(f"Edge {edge.id!r} geometry does not start at from_node.")
        if (
            max(abs(edge.geometry[-1][axis] - to_pos[axis]) for axis in (0, 1))
            > tolerance
        ):
            _fail(f"Edge {edge.id!r} geometry does not end at to_node.")
    station_ids = {node.id for node in network.nodes if node.is_station}
    for pattern in network.service_patterns:
        if pattern.line_id not in line_ids:
            _fail(f"Service pattern {pattern.id!r} names a missing line.")
        if pattern.source_ref not in source_ids:
            _fail(f"Service pattern {pattern.id!r} names a missing source.")
        unknown_edges = [
            traversal.edge_id
            for traversal in pattern.traversals
            if traversal.edge_id not in edge_ids
        ]
        if unknown_edges:
            _fail(
                f"Service pattern {pattern.id!r} names missing edges: "
                f"{', '.join(sorted(set(unknown_edges)))}."
            )
        wrong_line = [
            traversal.edge_id
            for traversal in pattern.traversals
            if pattern.line_id not in edge_by_id[traversal.edge_id].line_ids
        ]
        if wrong_line:
            _fail(
                f"Service pattern {pattern.id!r} traverses edges not assigned "
                f"to its line: {', '.join(sorted(set(wrong_line)))}."
            )
        unknown_stations = sorted(set(pattern.station_ids) - station_ids)
        if unknown_stations:
            _fail(
                f"Service pattern {pattern.id!r} names missing/non-station nodes: "
                f"{', '.join(unknown_stations)}."
            )
        allowed_breaks = set(pattern.continuity_breaks)
        for index, (first, second) in enumerate(
            zip(pattern.traversals, pattern.traversals[1:])
        ):
            first_edge = edge_by_id[first.edge_id]
            second_edge = edge_by_id[second.edge_id]
            first_end = (
                first_edge.to_node
                if first.direction == "forward"
                else first_edge.from_node
            )
            second_start = (
                second_edge.from_node
                if second.direction == "forward"
                else second_edge.to_node
            )
            if first_end != second_start and index not in allowed_breaks:
                _fail(
                    f"Service pattern {pattern.id!r} has an undeclared topology "
                    f"break after traversal {index}."
                )
            if first_end == second_start and index in allowed_breaks:
                _fail(
                    f"Service pattern {pattern.id!r} declares a false continuity "
                    f"break after traversal {index}."
                )
    for feature in network.context:
        if feature.source_ref not in source_ids:
            _fail(f"Context feature {feature.id!r} names a missing source.")


def load_transit_catalog() -> dict[str, dict[str, Any]]:
    resource = files("city_map_plotter").joinpath("data", "transit-catalog-v1.json")
    try:
        raw = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise MapPlotterError(
            f"Could not load {TRANSIT_CATALOG_RESOURCE}: {exc}"
        ) from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != TRANSIT_CATALOG_SCHEMA_VERSION
    ):
        _fail(f"{TRANSIT_CATALOG_RESOURCE} has an unsupported schema version.")
    records = _list(raw.get("networks"), field="catalog.networks")
    result: dict[str, dict[str, Any]] = {}
    for index, record_raw in enumerate(records):
        record = _object(record_raw, field=f"catalog.networks[{index}]")
        network_id = _id(record.get("id"), field=f"catalog.networks[{index}].id")
        if network_id in result:
            _fail(f"Transit catalog repeats network {network_id!r}.")
        result[network_id] = dict(record)
    return result


def catalog_network(network_id: str) -> dict[str, Any]:
    try:
        return load_transit_catalog()[network_id]
    except KeyError as exc:
        known = ", ".join(sorted(load_transit_catalog()))
        raise MapPlotterError(
            f"Unknown transit network {network_id!r}. Choose {known}."
        ) from exc


def apply_pen_map(network: TransitNetwork, path: Path | None) -> TransitNetwork:
    """Return ``network`` with caller-supplied, explicit physical pen matches.

    The catalog never assumes that a screen colour exists in the studio.  A
    pen map is therefore an explicit local calibration input keyed by line ID.
    """

    if path is None:
        return network
    raw, _ = _load_document(path)
    if raw.get("schema_version") != 1:
        _fail("Transit pen map must use schema_version 1.")
    mappings = _object(raw.get("lines"), field="pen-map.lines")
    unknown = sorted(set(mappings) - set(network.line_by_id))
    if unknown:
        _fail(f"Transit pen map names unknown lines: {', '.join(unknown)}.")
    lines: list[TransitLine] = []
    for line in network.lines:
        pen = (
            TransitPen.from_dict(mappings[line.id], field=f"pen-map.lines.{line.id}")
            if line.id in mappings
            else line.pen
        )
        lines.append(
            TransitLine(
                id=line.id,
                name=line.name,
                short_name=line.short_name,
                order=line.order,
                colour=line.colour,
                pen=pen,
                service_class=line.service_class,
                source_ref=line.source_ref,
            )
        )
    return TransitNetwork(
        id=network.id,
        name=network.name,
        kind=network.kind,
        scope=network.scope,
        format_id=network.format_id,
        snapshot=network.snapshot,
        validity_status=network.validity_status,
        geometry_mode=network.geometry_mode,
        sources=network.sources,
        lines=tuple(lines),
        nodes=network.nodes,
        edges=network.edges,
        service_patterns=network.service_patterns,
        context=network.context,
        omissions=network.omissions,
        notes=network.notes,
        contract_sha256=network.contract_sha256,
    )


def canonical_contract_bytes(network: TransitNetwork) -> bytes:
    """Serialize a deterministic normalized contract for release hashing."""

    return (
        json.dumps(
            network.as_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MapPlotterError(f"Could not hash {path}: {exc}") from exc
    return digest.hexdigest()
