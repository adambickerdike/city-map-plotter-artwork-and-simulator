"""Independent physical-retrace audit for emitted pen-plotter SVGs.

The renderer deliberately does not call this module.  It reads the final SVG
bytes, resolves selected linework by the declared physical pen identity, and
compares every emitted pen-down millimetre with the unary union of that ink.
The default scope remains the city renderer's road/path/rail network.  The
explicit ``all-physical`` scope instead audits every top-level physical pen
layer, including plate engines that nest logical artwork groups below one
``layer-pen-*`` group.  Point crossings and shared endpoints therefore
contribute no retrace length, while coincident whole, partial, reversed, or
self-overlapping segments do.

No geometry is rewritten or de-duplicated here.  A report with any retrace or
any audited layer declaring more than one pass fails the audit.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
import re
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from shapely import unary_union
from shapely.errors import GEOSException
from shapely.geometry import LineString


SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
COORDINATE_PRECISION_MM = Decimal("0.001")
CURVE_FLATNESS_MM = 0.0005
MAX_CURVE_SUBDIVISION_DEPTH = 24
RETRACE_EPSILON_MM = 1e-6
AUDIT_SCOPE_NETWORK = "network"
AUDIT_SCOPE_ALL_PHYSICAL = "all-physical"
AUDIT_SCOPES = frozenset({AUDIT_SCOPE_NETWORK, AUDIT_SCOPE_ALL_PHYSICAL})

_TOKEN = re.compile(
    r"(?P<command>[A-Za-z])|"
    r"(?P<number>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)|"
    r"(?P<separator>[\s,]+)|(?P<invalid>.)"
)

Point = tuple[float, float]


class RetraceAuditError(ValueError):
    """The SVG cannot be audited without making an unsafe assumption."""


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


@dataclass(frozen=True)
class _PathGeometry:
    lines: tuple[LineString, ...]
    length_mm: float
    segment_count: int


@dataclass(frozen=True)
class _PenDefinition:
    pen_id: str
    profile: str
    ink: str
    nib_mm: float
    nominal_nib_mm: float


@dataclass
class _PenAccumulator:
    definition: _PenDefinition
    layers: set[str]
    lines: list[LineString]
    raw_length_mm: float = 0.0
    path_count: int = 0
    segment_count: int = 0
    declared_passes_max: int = 1
    repeat_pass_count: int = 0
    declared_repeat_path_count: int = 0
    declared_repeat_length_mm: float = 0.0


@dataclass(frozen=True)
class PenRetraceResult:
    """Physical linework totals for one declared pen load."""

    pen_id: str
    pen_profile: str
    ink: str
    nib_mm: float
    nominal_nib_mm: float
    layers: tuple[str, ...]
    path_count: int
    segment_count: int
    declared_passes_max: int
    repeat_pass_count: int
    declared_repeat_path_count: int
    declared_repeat_length_mm: float
    raw_length_mm: float
    unique_length_mm: float
    retrace_length_mm: float

    @property
    def retrace_ratio(self) -> float:
        if self.raw_length_mm <= 0:
            return 0.0
        return self.retrace_length_mm / self.raw_length_mm

    @property
    def certified_zero_retrace(self) -> bool:
        return self.retrace_length_mm <= RETRACE_EPSILON_MM

    def as_dict(self) -> dict[str, Any]:
        return {
            "pen_id": self.pen_id,
            "pen_profile": self.pen_profile,
            "ink": self.ink,
            "nib_mm": _rounded(self.nib_mm),
            "nominal_nib_mm": _rounded(self.nominal_nib_mm),
            "layers": list(self.layers),
            "path_count": self.path_count,
            "segment_count": self.segment_count,
            "declared_passes_max": self.declared_passes_max,
            "repeat_pass_count": self.repeat_pass_count,
            "declared_repeat_path_count": self.declared_repeat_path_count,
            "declared_repeat_length_mm": _rounded(self.declared_repeat_length_mm),
            "raw_length_mm": _rounded(self.raw_length_mm),
            "unique_length_mm": _rounded(self.unique_length_mm),
            "retrace_length_mm": _rounded(self.retrace_length_mm),
            "retrace_ratio": round(self.retrace_ratio, 9),
            "certified_zero_retrace": self.certified_zero_retrace,
        }


@dataclass(frozen=True)
class AuditedPhysicalLayer:
    """One top-level physical SVG layer selected by ``all-physical`` scope."""

    svg_group_id: str
    layer_id: str
    logical_layer_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "svg_group_id": self.svg_group_id,
            "layer_id": self.layer_id,
            "logical_layer_ids": list(self.logical_layer_ids),
        }


@dataclass(frozen=True)
class RetraceAuditReport:
    """Complete, serialization-friendly retrace audit result."""

    source: str
    network_layers: tuple[str, ...]
    pens: tuple[PenRetraceResult, ...]
    failures: tuple[str, ...]
    scope: str = AUDIT_SCOPE_NETWORK
    audited_physical_layers: tuple[AuditedPhysicalLayer, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def raw_length_mm(self) -> float:
        return sum(item.raw_length_mm for item in self.pens)

    @property
    def unique_length_mm(self) -> float:
        return sum(item.unique_length_mm for item in self.pens)

    @property
    def retrace_length_mm(self) -> float:
        return sum(item.retrace_length_mm for item in self.pens)

    @property
    def declared_repeat_length_mm(self) -> float:
        return sum(item.declared_repeat_length_mm for item in self.pens)

    @property
    def repeat_pass_count(self) -> int:
        return sum(item.repeat_pass_count for item in self.pens)

    @property
    def certified_zero_retrace(self) -> bool:
        return all(item.certified_zero_retrace for item in self.pens)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "policy": (
                "network-physical-retrace-v1"
                if self.scope == AUDIT_SCOPE_NETWORK
                else "all-physical-retrace-v1"
            ),
            "source": self.source,
            "coordinate_precision_mm": float(COORDINATE_PRECISION_MM),
            "passed": self.passed,
            "failures": list(self.failures),
            "pen_count": len(self.pens),
            "repeat_pass_count": self.repeat_pass_count,
            "raw_length_mm": _rounded(self.raw_length_mm),
            "unique_length_mm": _rounded(self.unique_length_mm),
            "retrace_length_mm": _rounded(self.retrace_length_mm),
            "certified_zero_retrace": self.certified_zero_retrace,
            "declared_repeat_length_mm": _rounded(self.declared_repeat_length_mm),
            "pens": [item.as_dict() for item in self.pens],
        }
        if self.scope == AUDIT_SCOPE_NETWORK:
            # Preserve the original schema exactly for existing city reports.
            payload["network_layers"] = list(self.network_layers)
            payload["network_layer_scope"] = {
                "included_ids": [
                    "road_areas",
                    "roads",
                    "roads_*",
                    "paths",
                    "railways",
                ],
                "observed_ids": list(self.network_layers),
            }
            return payload

        payload["scope"] = AUDIT_SCOPE_ALL_PHYSICAL
        payload["audited_layers"] = [
            item.as_dict() for item in self.audited_physical_layers
        ]
        payload["physical_layer_scope"] = {
            "selection": (
                "every top-level SVG group declared as an Inkscape layer and "
                "identified by a layer-* id"
            ),
            "observed_svg_group_ids": [
                item.svg_group_id for item in self.audited_physical_layers
            ],
        }
        return payload


def _rounded(value: float) -> float:
    return round(value, 6)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]


def _network_layer(layer_id: str) -> bool:
    return (
        layer_id == "paths"
        or layer_id == "railways"
        or layer_id == "road_areas"
        or layer_id == "roads"
        or layer_id.startswith("roads_")
    )


def _required_decimal(
    element: ET.Element,
    attribute: str,
    *,
    subject: str,
) -> float:
    raw = element.get(attribute)
    if raw is None or not raw.strip():
        raise RetraceAuditError(f"{subject} lacks required {attribute!r} metadata.")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise RetraceAuditError(
            f"{subject} has non-numeric {attribute!r} metadata: {raw!r}."
        ) from exc
    if not value.is_finite():
        raise RetraceAuditError(
            f"{subject} has non-finite {attribute!r} metadata: {raw!r}."
        )
    return float(value)


def _required_count(
    element: ET.Element,
    attribute: str,
    *,
    subject: str,
) -> int:
    raw = element.get(attribute)
    if raw is None or not raw.strip():
        raise RetraceAuditError(f"{subject} lacks required {attribute!r} metadata.")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RetraceAuditError(
            f"{subject} has non-integer {attribute!r} metadata: {raw!r}."
        ) from exc
    if value < 1:
        raise RetraceAuditError(
            f"{subject} has invalid {attribute!r} metadata: {raw!r}."
        )
    return value


def _tokens(path_data: str, *, subject: str) -> list[_Token]:
    tokens: list[_Token] = []
    for match in _TOKEN.finditer(path_data):
        kind = match.lastgroup
        assert kind is not None
        value = match.group()
        if kind == "separator":
            continue
        if kind == "invalid":
            raise RetraceAuditError(
                f"{subject} contains invalid SVG path syntax {value!r}."
            )
        if kind == "command":
            if value not in {"M", "L", "C", "Z"}:
                raise RetraceAuditError(
                    f"{subject} uses unsupported path command {value!r}; "
                    "only absolute M/L/C/Z are auditable."
                )
            tokens.append(_Token(kind, value))
            continue
        try:
            decimal = Decimal(value)
        except InvalidOperation as exc:
            raise RetraceAuditError(
                f"{subject} contains invalid coordinate {value!r}."
            ) from exc
        if not decimal.is_finite():
            raise RetraceAuditError(
                f"{subject} contains non-finite coordinate {value!r}."
            )
        if decimal != decimal.quantize(COORDINATE_PRECISION_MM):
            raise RetraceAuditError(
                f"{subject} coordinate {value!r} exceeds the emitted "
                "0.001 mm precision contract."
            )
        tokens.append(_Token(kind, value))
    if not tokens:
        raise RetraceAuditError(f"{subject} has empty path geometry.")
    return tokens


def _midpoint(left: Point, right: Point) -> Point:
    return ((left[0] + right[0]) / 2, (left[1] + right[1]) / 2)


def _distance_to_chord(point: Point, start: Point, end: Point) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    denominator = math.hypot(dx, dy)
    if denominator <= 1e-15:
        return math.dist(point, start)
    return (
        abs(dy * point[0] - dx * point[1] + end[0] * start[1] - end[1] * start[0])
        / denominator
    )


def _flatten_cubic(
    start: Point,
    control_1: Point,
    control_2: Point,
    end: Point,
    *,
    depth: int = 0,
) -> list[Point]:
    flatness = max(
        _distance_to_chord(control_1, start, end),
        _distance_to_chord(control_2, start, end),
    )
    if flatness <= CURVE_FLATNESS_MM:
        return [end]
    if depth >= MAX_CURVE_SUBDIVISION_DEPTH:
        raise RetraceAuditError(
            "Cubic path could not be flattened within the 0.001 mm audit precision."
        )
    start_control = _midpoint(start, control_1)
    controls_midpoint = _midpoint(control_1, control_2)
    control_end = _midpoint(control_2, end)
    left_control = _midpoint(start_control, controls_midpoint)
    right_control = _midpoint(controls_midpoint, control_end)
    split = _midpoint(left_control, right_control)
    return _flatten_cubic(
        start,
        start_control,
        left_control,
        split,
        depth=depth + 1,
    ) + _flatten_cubic(
        split,
        right_control,
        control_end,
        end,
        depth=depth + 1,
    )


def _path_geometry(path_data: str, *, subject: str) -> _PathGeometry:
    tokens = _tokens(path_data, subject=subject)
    index = 0
    command = ""
    current: list[Point] = []
    lines: list[LineString] = []
    segment_count = 0

    def finish_subpath() -> None:
        nonlocal current, segment_count
        if not current:
            return
        distinct = [current[0]]
        distinct.extend(point for point in current[1:] if point != distinct[-1])
        if len(distinct) < 2:
            raise RetraceAuditError(f"{subject} contains no positive-length mark.")
        geometry = LineString(distinct)
        if geometry.length <= 0 or not geometry.is_valid:
            raise RetraceAuditError(f"{subject} contains invalid line geometry.")
        lines.append(geometry)
        segment_count += len(distinct) - 1
        current = []

    def coordinates(count: int) -> list[float]:
        nonlocal index
        if index + count > len(tokens):
            raise RetraceAuditError(
                f"{subject} ends in an incomplete {command!r} command."
            )
        chunk = tokens[index : index + count]
        if any(item.kind != "number" for item in chunk):
            raise RetraceAuditError(f"{subject} has an incomplete {command!r} command.")
        index += count
        return [float(item.value) for item in chunk]

    while index < len(tokens):
        token = tokens[index]
        if token.kind == "command":
            command = token.value
            index += 1
            if command == "Z":
                if not current:
                    raise RetraceAuditError(f"{subject} closes an empty subpath.")
                if current[-1] != current[0]:
                    current.append(current[0])
                command = ""
                continue
        if command == "M":
            values = coordinates(2)
            finish_subpath()
            current = [(values[0], values[1])]
            command = "L"
        elif command == "L":
            if not current:
                raise RetraceAuditError(f"{subject} draws before its first move.")
            values = coordinates(2)
            current.append((values[0], values[1]))
        elif command == "C":
            if not current:
                raise RetraceAuditError(f"{subject} draws before its first move.")
            values = coordinates(6)
            current.extend(
                _flatten_cubic(
                    current[-1],
                    (values[0], values[1]),
                    (values[2], values[3]),
                    (values[4], values[5]),
                )
            )
        else:
            raise RetraceAuditError(f"{subject} has coordinates without a command.")
    finish_subpath()
    if not lines:
        raise RetraceAuditError(f"{subject} contains no auditable line geometry.")
    return _PathGeometry(
        tuple(lines),
        sum(line.length for line in lines),
        segment_count,
    )


def _page_contract(root: ET.Element) -> None:
    if root.tag != f"{{{SVG_NS}}}svg":
        raise RetraceAuditError("Document root is not an SVG element.")
    if root.get("transform"):
        raise RetraceAuditError(
            "SVG root transforms violate the 1 unit = 1 mm contract."
        )
    if root.findall(f".//{{{SVG_NS}}}style"):
        raise RetraceAuditError(
            "SVG stylesheets are not auditable physical geometry; inline the emitted plan."
        )

    def millimetres(attribute: str) -> Decimal:
        raw = root.get(attribute, "")
        match = re.fullmatch(r"\s*([^\s]+?)\s*mm\s*", raw)
        if match is None:
            raise RetraceAuditError(
                f"SVG {attribute!r} must be an explicit millimetre measurement."
            )
        try:
            value = Decimal(match.group(1))
        except InvalidOperation as exc:
            raise RetraceAuditError(f"SVG {attribute!r} is not numeric.") from exc
        if not value.is_finite() or value <= 0:
            raise RetraceAuditError(f"SVG {attribute!r} is not a finite positive size.")
        return value

    width = millimetres("width")
    height = millimetres("height")
    view_box = root.get("viewBox", "").split()
    if len(view_box) != 4:
        raise RetraceAuditError("SVG requires a four-number viewBox.")
    try:
        values = [Decimal(item) for item in view_box]
    except InvalidOperation as exc:
        raise RetraceAuditError("SVG viewBox is not numeric.") from exc
    if values != [Decimal(0), Decimal(0), width, height]:
        raise RetraceAuditError(
            "SVG viewBox must be '0 0 width height' with 1 user unit = 1 mm."
        )


def _layer_definition(
    group: ET.Element,
    layer_id: str,
    *,
    layer_kind: str = "network layer",
) -> _PenDefinition:
    subject = f"{layer_kind} {layer_id!r}"
    pen_id = (group.get("data-plot-pen-id") or "").strip()
    profile = (group.get("data-plot-pen-profile") or "").strip()
    ink = (group.get("data-plot-ink") or "").strip()
    if not pen_id:
        raise RetraceAuditError(f"{subject} lacks a physical pen id.")
    if not profile:
        raise RetraceAuditError(f"{subject} lacks a physical pen profile.")
    if not ink:
        raise RetraceAuditError(f"{subject} lacks a physical ink declaration.")
    nib = _required_decimal(group, "data-plot-nib-mm", subject=subject)
    nominal = _required_decimal(
        group,
        "data-plot-nominal-nib-mm",
        subject=subject,
    )
    if nib <= 0 or nominal <= 0:
        raise RetraceAuditError(f"{subject} declares a non-positive nib size.")
    return _PenDefinition(pen_id, profile, ink, nib, nominal)


def _assert_supported_layer_tree(
    group: ET.Element,
    *,
    layer_id: str,
    layer_kind: str = "network layer",
) -> None:
    for element in group.iter():
        subject = (
            f"{layer_kind} {layer_id!r} "
            f"{element.get('id', _local_name(element.tag))!r}"
        )
        if element.get("transform"):
            raise RetraceAuditError(
                f"{subject} uses a transform; transformed physical geometry is not auditable."
            )
        name = _local_name(element.tag)
        if name in {"g", "title", "desc", "metadata", "path"}:
            continue
        raise RetraceAuditError(f"{subject} contains unsupported SVG element {name!r}.")


def _logical_layer_ids(group: ET.Element) -> tuple[str, ...]:
    """Return explicit logical identities nested below a physical pen group."""

    identities: set[str] = set()
    for element in group.iter():
        logical_id = (element.get("data-logical-layer") or "").strip()
        if logical_id:
            identities.add(logical_id)
    return tuple(sorted(identities))


def _all_physical_candidate_groups(root: ET.Element) -> list[ET.Element]:
    """Select every top-level emitted layer without trusting pen metadata.

    Selection deliberately happens from structural layer declarations and IDs,
    before physical metadata is read.  A malformed layer therefore cannot make
    itself disappear from the audit merely by omitting ``data-plot-pen-id``.
    """

    layer_tag = f"{{{SVG_NS}}}g"
    group_mode = f"{{{INKSCAPE_NS}}}groupmode"
    candidates: list[ET.Element] = []
    for group in root.findall(layer_tag):
        group_id = (group.get("id") or "").strip()
        declared_layer = group.get(group_mode) == "layer"
        layer_named = group_id.startswith("layer-")
        has_physical_marker = any(
            group.get(attribute) is not None
            for attribute in (
                "data-plot-pen-id",
                "data-plot-pen-profile",
                "data-plot-ink",
                "data-plot-nib-mm",
                "data-plot-passes",
            )
        )
        if not declared_layer and not layer_named and not has_physical_marker:
            continue
        if not group_id or not layer_named:
            raise RetraceAuditError(
                "Top-level physical layer must have a non-empty layer-* SVG id."
            )
        if not declared_layer:
            raise RetraceAuditError(
                f"Physical group {group_id!r} lacks the Inkscape layer declaration."
            )
        candidates.append(group)
    return candidates


def _matching_definition(left: _PenDefinition, right: _PenDefinition) -> bool:
    return (
        left.pen_id == right.pen_id
        and left.profile == right.profile
        and left.ink.casefold() == right.ink.casefold()
        and math.isclose(left.nib_mm, right.nib_mm, abs_tol=1e-9)
        and math.isclose(left.nominal_nib_mm, right.nominal_nib_mm, abs_tol=1e-9)
    )


def _unique_length(lines: Iterable[LineString]) -> float:
    geometries = list(lines)
    if not geometries:
        return 0.0
    try:
        merged = unary_union(geometries)
    except GEOSException as exc:
        raise RetraceAuditError(
            "Unary union could not resolve the emitted physical line geometry."
        ) from exc
    if merged.is_empty or not merged.is_valid:
        raise RetraceAuditError("Unary union produced invalid physical line geometry.")
    return float(merged.length)


def audit_svg_retraces(
    path: str | Path,
    *,
    scope: str = AUDIT_SCOPE_NETWORK,
) -> RetraceAuditReport:
    """Audit final SVG linework without altering it.

    Invalid SVG structure or physical metadata raises :class:`RetraceAuditError`.
    Valid-but-retraced output returns a report whose ``passed`` property is
    false, allowing callers to preserve all measurements in CI artifacts.  The
    backwards-compatible default audits only road/path/rail network layers;
    ``scope="all-physical"`` audits every top-level physical pen layer.
    """

    if scope not in AUDIT_SCOPES:
        raise RetraceAuditError(
            f"Unknown retrace audit scope {scope!r}; choose "
            f"{', '.join(sorted(AUDIT_SCOPES))}."
        )

    source = Path(path)
    try:
        root = ET.parse(source).getroot()
    except (ET.ParseError, OSError) as exc:
        raise RetraceAuditError(f"Cannot read SVG {source}: {exc}") from exc
    _page_contract(root)

    accumulators: dict[str, _PenAccumulator] = {}
    network_layers: list[str] = []
    audited_physical_layers: list[AuditedPhysicalLayer] = []
    failures: list[str] = []
    seen_layer_ids: set[str] = set()
    layer_tag = f"{{{SVG_NS}}}g"
    path_tag = f"{{{SVG_NS}}}path"
    group_mode = f"{{{INKSCAPE_NS}}}groupmode"
    layer_kind = (
        "network layer" if scope == AUDIT_SCOPE_NETWORK else "physical layer"
    )

    top_level_groups = set(root.findall(layer_tag))
    if scope == AUDIT_SCOPE_NETWORK:
        candidate_groups: list[ET.Element] = []
        for group in root.iter(layer_tag):
            group_id = (group.get("id") or "").strip()
            if not group_id.startswith("layer-"):
                continue
            layer_id = group_id.removeprefix("layer-")
            if not _network_layer(layer_id):
                continue
            if group not in top_level_groups:
                raise RetraceAuditError(
                    f"Network layer {layer_id!r} is nested; emitted physical layers "
                    "must be top-level SVG groups."
                )
            if group.get(group_mode) != "layer":
                raise RetraceAuditError(
                    f"Network group {group_id!r} lacks the Inkscape layer declaration."
                )
            candidate_groups.append(group)
    else:
        candidate_groups = _all_physical_candidate_groups(root)

    for group in candidate_groups:
        group_id = (group.get("id") or "").strip()
        layer_id = group_id.removeprefix("layer-")
        if layer_id in seen_layer_ids:
            raise RetraceAuditError(
                f"{layer_kind.capitalize()} id {layer_id!r} is duplicated."
            )
        seen_layer_ids.add(layer_id)
        if scope == AUDIT_SCOPE_NETWORK:
            network_layers.append(layer_id)
        else:
            audited_physical_layers.append(
                AuditedPhysicalLayer(
                    svg_group_id=group_id,
                    layer_id=layer_id,
                    logical_layer_ids=_logical_layer_ids(group),
                )
            )
        _assert_supported_layer_tree(
            group,
            layer_id=layer_id,
            layer_kind=layer_kind,
        )
        definition = _layer_definition(
            group,
            layer_id,
            layer_kind=layer_kind,
        )
        passes = _required_count(
            group,
            "data-plot-passes",
            subject=f"{layer_kind} {layer_id!r}",
        )
        if passes > 1:
            failures.append(
                f"{layer_kind.capitalize()} {layer_id!r} declares {passes} passes; "
                "repeat plotting is forbidden."
            )

        accumulator = accumulators.get(definition.pen_id)
        if accumulator is None:
            accumulator = _PenAccumulator(definition, set(), [])
            accumulators[definition.pen_id] = accumulator
        elif not _matching_definition(accumulator.definition, definition):
            raise RetraceAuditError(
                f"Physical pen id {definition.pen_id!r} has conflicting profile, "
                "ink, or nib declarations."
            )
        accumulator.layers.add(layer_id)
        accumulator.declared_passes_max = max(
            accumulator.declared_passes_max,
            passes,
        )
        accumulator.repeat_pass_count += passes - 1

        paths = group.findall(f".//{path_tag}")
        if not paths:
            raise RetraceAuditError(
                f"{layer_kind.capitalize()} {layer_id!r} has no SVG paths."
            )
        pass_counts: dict[int, int] = defaultdict(int)
        pass_lengths: dict[int, float] = defaultdict(float)
        for path_index, element in enumerate(paths, start=1):
            subject = f"{layer_kind} {layer_id!r} path {path_index}"
            path_metadata = {
                name: element.get(name)
                for name in (
                    "data-plot-pen-id",
                    "data-plot-pass-count",
                    "data-plot-pass-index",
                )
            }
            present_path_metadata = {
                name
                for name, value in path_metadata.items()
                if value is not None and value.strip()
            }
            if scope == AUDIT_SCOPE_NETWORK:
                path_pen = (element.get("data-plot-pen-id") or "").strip()
                if path_pen != definition.pen_id:
                    raise RetraceAuditError(
                        f"{subject} physical pen id {path_pen!r} does not match "
                        "its layer."
                    )
                path_passes = _required_count(
                    element,
                    "data-plot-pass-count",
                    subject=subject,
                )
                pass_index = _required_count(
                    element,
                    "data-plot-pass-index",
                    subject=subject,
                )
            elif present_path_metadata:
                if len(present_path_metadata) != len(path_metadata):
                    missing = sorted(set(path_metadata) - present_path_metadata)
                    raise RetraceAuditError(
                        f"{subject} has incomplete physical metadata; missing "
                        f"{', '.join(missing)}."
                    )
                path_pen = str(path_metadata["data-plot-pen-id"]).strip()
                if path_pen != definition.pen_id:
                    raise RetraceAuditError(
                        f"{subject} physical pen id {path_pen!r} does not match "
                        "its layer."
                    )
                path_passes = _required_count(
                    element,
                    "data-plot-pass-count",
                    subject=subject,
                )
                pass_index = _required_count(
                    element,
                    "data-plot-pass-index",
                    subject=subject,
                )
            else:
                if passes != 1:
                    raise RetraceAuditError(
                        f"{subject} inherits a {passes}-pass layer but does not "
                        "declare its pass index."
                    )
                path_pen = definition.pen_id
                path_passes = 1
                pass_index = 1
            if path_passes != passes or pass_index > passes:
                raise RetraceAuditError(
                    f"{subject} pass metadata ({pass_index}/{path_passes}) does not "
                    f"match its layer declaration ({passes})."
                )
            geometry = _path_geometry(element.get("d", ""), subject=subject)
            accumulator.lines.extend(geometry.lines)
            accumulator.raw_length_mm += geometry.length_mm
            accumulator.path_count += 1
            accumulator.segment_count += geometry.segment_count
            pass_counts[pass_index] += 1
            pass_lengths[pass_index] += geometry.length_mm
            if pass_index > 1:
                accumulator.declared_repeat_path_count += 1
                accumulator.declared_repeat_length_mm += geometry.length_mm

        expected_indices = set(range(1, passes + 1))
        if set(pass_counts) != expected_indices:
            raise RetraceAuditError(
                f"{layer_kind.capitalize()} {layer_id!r} does not emit every "
                "declared pass index."
            )
        if len(set(pass_counts.values())) != 1:
            raise RetraceAuditError(
                f"{layer_kind.capitalize()} {layer_id!r} emits a different path "
                "count per pass."
            )
        reference_length = pass_lengths[1]
        if any(
            not math.isclose(length, reference_length, abs_tol=RETRACE_EPSILON_MM)
            for length in pass_lengths.values()
        ):
            raise RetraceAuditError(
                f"{layer_kind.capitalize()} {layer_id!r} emits a different length "
                "per pass."
            )

    if not candidate_groups:
        if scope == AUDIT_SCOPE_NETWORK:
            raise RetraceAuditError(
                "SVG contains no auditable roads, paths, or railways layers."
            )
        raise RetraceAuditError(
            "SVG contains no auditable top-level physical pen layers."
        )

    pen_results: list[PenRetraceResult] = []
    for pen_id, accumulator in accumulators.items():
        unique = _unique_length(accumulator.lines)
        difference = accumulator.raw_length_mm - unique
        if difference < -RETRACE_EPSILON_MM:
            raise RetraceAuditError(
                f"Unary-union length exceeds raw length for pen {pen_id!r}."
            )
        retrace = max(0.0, difference)
        if retrace > RETRACE_EPSILON_MM:
            failures.append(
                f"Physical pen {pen_id!r} retraces {_rounded(retrace):.6f} mm "
                + (
                    "of network linework."
                    if scope == AUDIT_SCOPE_NETWORK
                    else "of all audited physical linework."
                )
            )
        pen_results.append(
            PenRetraceResult(
                pen_id=pen_id,
                pen_profile=accumulator.definition.profile,
                ink=accumulator.definition.ink,
                nib_mm=accumulator.definition.nib_mm,
                nominal_nib_mm=accumulator.definition.nominal_nib_mm,
                layers=tuple(sorted(accumulator.layers)),
                path_count=accumulator.path_count,
                segment_count=accumulator.segment_count,
                declared_passes_max=accumulator.declared_passes_max,
                repeat_pass_count=accumulator.repeat_pass_count,
                declared_repeat_path_count=accumulator.declared_repeat_path_count,
                declared_repeat_length_mm=accumulator.declared_repeat_length_mm,
                raw_length_mm=accumulator.raw_length_mm,
                unique_length_mm=unique,
                retrace_length_mm=retrace,
            )
        )

    return RetraceAuditReport(
        source=str(source),
        network_layers=tuple(network_layers),
        pens=tuple(pen_results),
        failures=tuple(failures),
        scope=scope,
        audited_physical_layers=tuple(audited_physical_layers),
    )
