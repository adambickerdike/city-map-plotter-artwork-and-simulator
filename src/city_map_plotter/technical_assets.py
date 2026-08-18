"""Local, content-addressed asset import for technical-object plates.

The importer accepts deliberately small vector contracts.  It never follows an
external URL, runs SVG script, expands CSS, or treats a filled logo as object
geometry.  Plain SVG paths can be prepared for a record, while richer files may
carry explicit ``data-*`` semantics that survive into the plot manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, NoReturn
from xml.etree import ElementTree as ET

from .technical_geometry import RasterReconstruction
from .vector_path import CubicSegment, LineSegment, VectorPath, VectorPathError


SVG_NS = "http://www.w3.org/2000/svg"
_PATH_TOKEN = re.compile(
    r"(?P<command>[A-Za-z])|"
    r"(?P<number>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)|"
    r"(?P<separator>[\s,]+)|(?P<invalid>.)"
)
_STABLE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class TechnicalAssetError(ValueError):
    """Raised when a supplied local asset is unsafe or ambiguous."""


@dataclass(frozen=True, slots=True)
class ImportedPrimitive:
    identifier: str
    component_id: str
    semantic_class: str
    view: str
    evidence: str
    source_ref: str
    path: VectorPath
    feature_kind: str | None = None
    confidence: float | None = None
    line_style: str | None = None
    source_path_ids: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.identifier,
            "component_id": self.component_id,
            "semantic_class": self.semantic_class,
            "source_refs": [self.source_ref],
            "evidence": self.evidence,
            "claim_status": "source-visible-geometry",
            "path": self.path.to_dict(),
        }
        if self.feature_kind is not None:
            result["feature_kind"] = self.feature_kind
        if self.confidence is not None:
            result["confidence"] = self.confidence
        if self.line_style is not None:
            result["line_style"] = self.line_style
        if self.source_path_ids:
            result["source_path_ids"] = list(self.source_path_ids)
        return result


@dataclass(frozen=True, slots=True)
class ImportedVectorAsset:
    path: Path
    sha256: str
    view_box: tuple[float, float, float, float]
    primitives: tuple[ImportedPrimitive, ...]


def _fail(message: str) -> NoReturn:
    raise TechnicalAssetError(message)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be non-empty text.")
    return value.strip()


def _identifier(value: Any, label: str) -> str:
    result = _text(value, label)
    if _STABLE_ID.fullmatch(result) is None:
        _fail(f"{label} must use lowercase letters, digits, and hyphens.")
    return result


def _semantic_identifier(value: Any, label: str) -> str:
    """Accept record-style underscores while validating one stable token."""

    normalized = _text(value, label).replace("_", "-")
    return _identifier(normalized, label).replace("-", "_")


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be a finite number.")
    result = float(value)
    if not math.isfinite(result):
        _fail(f"{label} must be a finite number.")
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise TechnicalAssetError(f"Cannot read source asset {path}: {exc}") from exc
    return digest.hexdigest()


def verify_asset(path: Path, expected_sha256: str) -> str:
    expected = _text(expected_sha256, "asset_sha256")
    if _SHA256.fullmatch(expected) is None:
        _fail("asset_sha256 must be a lowercase SHA-256.")
    actual = sha256_file(path)
    if actual != expected:
        _fail(f"Source asset SHA-256 changed: expected {expected}, got {actual}.")
    return actual


def parse_absolute_path_data(path_data: str) -> VectorPath:
    """Parse one strict absolute M/L/C/Z subpath into the shared vector IR."""

    if not isinstance(path_data, str) or not path_data.strip():
        _fail("SVG path data must be non-empty text.")
    tokens: list[str | float] = []
    for match in _PATH_TOKEN.finditer(path_data):
        kind = match.lastgroup
        token = match.group()
        if kind == "separator":
            continue
        if kind == "invalid":
            _fail(f"SVG path data contains invalid token {token!r}.")
        if kind == "command":
            if token not in {"M", "L", "C", "Z"}:
                _fail(
                    f"SVG command {token!r} is unsupported; normalize to absolute M/L/C/Z."
                )
            tokens.append(token)
        else:
            number = float(token)
            if not math.isfinite(number):
                _fail("SVG path data contains a non-finite coordinate.")
            tokens.append(number)
    if not tokens or tokens[0] != "M":
        _fail("SVG path data must begin with one absolute M command.")
    index = 0
    start: tuple[float, float] | None = None
    segments: list[LineSegment | CubicSegment] = []
    closed = False
    while index < len(tokens):
        command = tokens[index]
        if not isinstance(command, str):
            _fail("Every coordinate group needs an explicit absolute command.")
        index += 1
        arity = {"M": 2, "L": 2, "C": 6, "Z": 0}[command]
        if index + arity > len(tokens) or any(
            isinstance(token, str) for token in tokens[index : index + arity]
        ):
            _fail(f"SVG {command} command has the wrong number of coordinates.")
        values = [float(value) for value in tokens[index : index + arity]]
        index += arity
        if index < len(tokens) and not isinstance(tokens[index], str):
            _fail(f"SVG {command} command uses implicit repeated coordinates.")
        if command == "M":
            if start is not None:
                _fail("Each imported path must contain exactly one M/subpath.")
            start = (values[0], values[1])
        elif command == "L":
            if start is None:
                _fail("SVG L cannot precede M.")
            segments.append(LineSegment((values[0], values[1])))
        elif command == "C":
            if start is None:
                _fail("SVG C cannot precede M.")
            segments.append(
                CubicSegment(
                    (values[0], values[1]),
                    (values[2], values[3]),
                    (values[4], values[5]),
                )
            )
        else:
            if start is None or not segments or index != len(tokens):
                _fail("SVG Z is allowed only once at the end of a drawable path.")
            closed = True
    if start is None:
        _fail("SVG path has no move point.")
    try:
        return VectorPath(start=start, segments=tuple(segments), closed=closed)
    except VectorPathError as exc:
        raise TechnicalAssetError(f"Invalid SVG path geometry: {exc}") from exc


def circle_path(
    centre: tuple[float, float], radius_x: float, radius_y: float | None = None
) -> VectorPath:
    """Return a smooth four-cubic circle/ellipse in the shared vector IR."""

    cx, cy = centre
    rx = _number(radius_x, "circle radius_x")
    ry = rx if radius_y is None else _number(radius_y, "circle radius_y")
    if rx <= 0 or ry <= 0:
        _fail("Circle and ellipse radii must be positive.")
    kappa = 0.5522847498307936
    return VectorPath(
        start=(cx + rx, cy),
        segments=(
            CubicSegment(
                (cx + rx, cy + kappa * ry),
                (cx + kappa * rx, cy + ry),
                (cx, cy + ry),
            ),
            CubicSegment(
                (cx - kappa * rx, cy + ry),
                (cx - rx, cy + kappa * ry),
                (cx - rx, cy),
            ),
            CubicSegment(
                (cx - rx, cy - kappa * ry),
                (cx - kappa * rx, cy - ry),
                (cx, cy - ry),
            ),
            CubicSegment(
                (cx + kappa * rx, cy - ry),
                (cx + rx, cy - kappa * ry),
                (cx + rx, cy),
            ),
        ),
        closed=True,
    )


def polyline_path(points: Iterable[tuple[float, float]], *, closed: bool) -> VectorPath:
    values = [
        (_number(point[0], "polyline x"), _number(point[1], "polyline y"))
        for point in points
    ]
    if closed and len(values) > 1 and values[-1] == values[0]:
        values.pop()
    if len(values) < 2:
        _fail("Polyline source geometry needs at least two distinct points.")
    try:
        return VectorPath(
            start=values[0],
            segments=tuple(LineSegment(point) for point in values[1:]),
            closed=closed,
        )
    except VectorPathError as exc:
        raise TechnicalAssetError(f"Invalid polyline source geometry: {exc}") from exc


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _inherited(
    element: ET.Element, parents: dict[ET.Element, ET.Element], key: str
) -> str | None:
    current: ET.Element | None = element
    while current is not None:
        value = current.get(key)
        if value is not None:
            return value
        current = parents.get(current)
    return None


def _shape_path(element: ET.Element) -> VectorPath:
    local = _local_name(element.tag)
    if local == "path":
        return parse_absolute_path_data(_text(element.get("d"), "path.d"))
    if local in {"polyline", "polygon"}:
        raw_points = _text(element.get("points"), f"{local}.points")
        tokens = [value for value in re.split(r"[\s,]+", raw_points) if value]
        if len(tokens) < 4 or len(tokens) % 2:
            _fail(f"{local}.points must contain coordinate pairs.")
        try:
            points = [
                (float(tokens[index]), float(tokens[index + 1]))
                for index in range(0, len(tokens), 2)
            ]
        except ValueError as exc:
            raise TechnicalAssetError(f"{local}.points is not numeric.") from exc
        return polyline_path(points, closed=local == "polygon")
    if local == "line":
        return polyline_path(
            [
                (
                    float(_text(element.get("x1"), "line.x1")),
                    float(_text(element.get("y1"), "line.y1")),
                ),
                (
                    float(_text(element.get("x2"), "line.x2")),
                    float(_text(element.get("y2"), "line.y2")),
                ),
            ],
            closed=False,
        )
    if local == "circle":
        return circle_path(
            (
                float(_text(element.get("cx"), "circle.cx")),
                float(_text(element.get("cy"), "circle.cy")),
            ),
            float(_text(element.get("r"), "circle.r")),
        )
    if local == "ellipse":
        return circle_path(
            (
                float(_text(element.get("cx"), "ellipse.cx")),
                float(_text(element.get("cy"), "ellipse.cy")),
            ),
            float(_text(element.get("rx"), "ellipse.rx")),
            float(_text(element.get("ry"), "ellipse.ry")),
        )
    _fail(f"Unsupported SVG shape <{local}>.")


def import_svg_asset(
    path: Path,
    *,
    source_ref: str,
    view: str,
    expected_sha256: str | None = None,
    default_semantic_class: str = "panel_seam_lines",
    default_evidence: str = "supplied-visible",
) -> ImportedVectorAsset:
    """Import safe unfilled SVG centrelines while retaining exact cubics."""

    source_id = _identifier(source_ref, "source_ref")
    selected_view = _identifier(view, "view")
    digest = sha256_file(path)
    if expected_sha256 is not None:
        verify_asset(path, expected_sha256)
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise TechnicalAssetError(f"Cannot parse SVG asset {path}: {exc}") from exc
    if _local_name(root.tag) != "svg":
        _fail("Technical vector asset root must be <svg>.")
    try:
        view_box_values = tuple(
            float(value) for value in root.get("viewBox", "").split()
        )
    except ValueError as exc:
        raise TechnicalAssetError("SVG viewBox must contain four numbers.") from exc
    if (
        len(view_box_values) != 4
        or view_box_values[2] <= 0
        or view_box_values[3] <= 0
        or any(not math.isfinite(value) for value in view_box_values)
    ):
        _fail("SVG viewBox must be a finite positive four-number rectangle.")
    view_box = (
        view_box_values[0],
        view_box_values[1],
        view_box_values[2],
        view_box_values[3],
    )
    allowed = {"svg", "g", "path", "polyline", "polygon", "line", "circle", "ellipse"}
    shape_names = allowed - {"svg", "g"}
    parents = {child: parent for parent in root.iter() for child in parent}
    primitives: list[ImportedPrimitive] = []
    seen: set[str] = set()
    for element in root.iter():
        local = _local_name(element.tag)
        if local not in allowed:
            _fail(
                f"SVG contains unsupported <{local}>; raster, text, use, CSS and script are forbidden."
            )
        if "transform" in element.attrib:
            _fail("SVG transforms are forbidden; flatten them into source coordinates.")
        if element.get("style"):
            _fail(
                "SVG CSS/style is forbidden; geometry and semantics must be explicit."
            )
        if any(key.rsplit("}", 1)[-1] in {"href", "src"} for key in element.attrib):
            _fail("SVG external references are forbidden.")
        if local not in shape_names:
            continue
        fill = (_inherited(element, parents, "fill") or "none").strip().casefold()
        if fill != "none":
            _fail("Technical SVG shapes must be unfilled centrelines/outlines.")
        identifier = _identifier(element.get("id"), f"{local}.id")
        if identifier in seen:
            _fail(f"SVG repeats shape id {identifier!r}.")
        seen.add(identifier)
        component = _identifier(
            _inherited(element, parents, "data-component-id") or "object",
            f"{identifier}.component",
        )
        semantic = _semantic_identifier(
            _inherited(element, parents, "data-semantic-class")
            or default_semantic_class,
            f"{identifier}.semantic-class",
        )
        evidence = _identifier(
            _inherited(element, parents, "data-evidence") or default_evidence,
            f"{identifier}.evidence",
        )
        feature_kind = _inherited(element, parents, "data-feature-kind")
        if feature_kind is not None:
            feature_kind = _identifier(feature_kind, f"{identifier}.feature-kind")
        vector = _shape_path(element)
        bounds = vector.bounds()
        left, top, width, height = view_box
        if (
            bounds.min_x < left - 1e-9
            or bounds.min_y < top - 1e-9
            or bounds.max_x > left + width + 1e-9
            or bounds.max_y > top + height + 1e-9
        ):
            _fail(f"SVG shape {identifier!r} leaves the declared viewBox.")
        primitives.append(
            ImportedPrimitive(
                identifier=identifier,
                component_id=component,
                semantic_class=semantic,
                view=selected_view,
                evidence=evidence,
                source_ref=source_id,
                path=vector,
                feature_kind=feature_kind,
            )
        )
    if not primitives:
        _fail("SVG asset contains no supported drawable shapes.")
    return ImportedVectorAsset(
        path=path,
        sha256=digest,
        view_box=view_box,
        primitives=tuple(primitives),
    )


def raster_reconstruction_primitives(
    reconstruction: RasterReconstruction,
    *,
    source_ref: str,
    view: str,
) -> list[dict[str, Any]]:
    """Convert a passed raster quality gate into visible-only record geometry."""

    if reconstruction.quality_status != "usable-visible-portrait":
        _fail(
            "INSUFFICIENT_REFERENCE: raster reconstruction did not pass the visible-view quality gate."
        )
    source_id = _identifier(source_ref, "source_ref")
    selected_view = _identifier(view, "view")
    result: list[dict[str, Any]] = []
    for index, contour in enumerate(reconstruction.contours, start=1):
        try:
            path = VectorPath(
                start=contour.points[0],
                segments=tuple(LineSegment(point) for point in contour.points[1:]),
                closed=contour.closed,
            )
        except VectorPathError as exc:
            raise TechnicalAssetError(
                f"Raster contour {index} cannot enter the vector IR: {exc}"
            ) from exc
        result.append(
            {
                "id": f"{selected_view}-photo-contour-{index:03d}",
                "component_id": "visible-object",
                "semantic_class": contour.semantic_class,
                "source_refs": [source_id],
                "evidence": "supplied-visible",
                "claim_status": "photo-visible-stylised-portrait",
                "feature_kind": contour.feature_kind,
                "confidence": round(contour.confidence, 6),
                "path": path.to_dict(),
            }
        )
    return result


def load_converted_vector_json(path: Path) -> ImportedVectorAsset:
    """Load a DXF/CAD-converted source using canonical shared path documents."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TechnicalAssetError(
            f"Cannot read converted vector {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise TechnicalAssetError(
            f"Invalid converted vector JSON {path}: {exc}"
        ) from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "kind",
        "view_box",
        "primitives",
    }:
        _fail("Converted vector JSON has the wrong top-level fields.")
    if document["schema_version"] != 1 or document["kind"] != "technical-vector-source":
        _fail("Converted vector JSON kind/schema is unsupported.")
    raw_box = document["view_box"]
    if not isinstance(raw_box, list) or len(raw_box) != 4:
        _fail("Converted vector view_box must contain four numbers.")
    view_box = tuple(
        _number(value, f"view_box[{index}]") for index, value in enumerate(raw_box)
    )
    if view_box[2] <= 0 or view_box[3] <= 0:
        _fail("Converted vector view_box dimensions must be positive.")
    raw_primitives = document["primitives"]
    if not isinstance(raw_primitives, list) or not raw_primitives:
        _fail("Converted vector primitives must be a non-empty array.")
    result: list[ImportedPrimitive] = []
    for index, raw in enumerate(raw_primitives):
        if not isinstance(raw, dict) or set(raw) != {
            "id",
            "component_id",
            "semantic_class",
            "view",
            "evidence",
            "source_ref",
            "path",
        }:
            _fail(f"Converted vector primitive {index} has unsupported fields.")
        try:
            vector = VectorPath.from_dict(raw["path"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TechnicalAssetError(
                f"Converted vector primitive {index} has invalid path geometry: {exc}"
            ) from exc
        result.append(
            ImportedPrimitive(
                identifier=_identifier(raw["id"], f"primitives[{index}].id"),
                component_id=_identifier(
                    raw["component_id"], f"primitives[{index}].component_id"
                ),
                semantic_class=_semantic_identifier(
                    raw["semantic_class"],
                    f"primitives[{index}].semantic_class",
                ),
                view=_identifier(raw["view"], f"primitives[{index}].view"),
                evidence=_identifier(raw["evidence"], f"primitives[{index}].evidence"),
                source_ref=_identifier(
                    raw["source_ref"], f"primitives[{index}].source_ref"
                ),
                path=vector,
            )
        )
    return ImportedVectorAsset(
        path=path,
        sha256=sha256_file(path),
        view_box=(view_box[0], view_box[1], view_box[2], view_box[3]),
        primitives=tuple(result),
    )


__all__ = [
    "ImportedPrimitive",
    "ImportedVectorAsset",
    "TechnicalAssetError",
    "circle_path",
    "import_svg_asset",
    "load_converted_vector_json",
    "parse_absolute_path_data",
    "polyline_path",
    "raster_reconstruction_primitives",
    "sha256_file",
    "verify_asset",
]
