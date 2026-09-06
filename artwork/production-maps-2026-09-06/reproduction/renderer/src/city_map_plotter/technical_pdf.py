"""Deterministic vector extraction from technical PDF pages.

The production renderer deliberately accepts only a small, already-normalized
SVG contract.  Manufacturer service sheets are usually PDF, however, and a
useful page can contain native line/cubic geometry alongside text, logos,
shading and callout furniture.  This module provides the narrow bridge between
those two worlds:

* Inkscape imports exactly one hash-pinned PDF page and deletes every text
  object.  Text is never outlined and therefore cannot leak a wordmark into the
  mechanical geometry.
* The normalized SVG is parsed without rasterisation.  Only M/L/H/V/C/Z path
  commands and explicit affine matrices are accepted.
* Every compound path is split into canonical one-subpath ``VectorPath``
  records.  Source strokes remain centrelines; optional fill handling emits the
  original fill boundary, never a stroke-to-path double outline.
* A rectangular extraction recipe accepts only paths wholly contained by the
  crop.  Intersecting callout leaders are reported and rejected rather than
  clipped into invented endpoints.

The PDF itself is not a licence.  Rights and source authority remain record
fields enforced by :mod:`city_map_plotter.technical`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Iterable, NoReturn
from xml.etree import ElementTree as ET

from .technical_assets import ImportedPrimitive, TechnicalAssetError, sha256_file
from .vector_path import (
    Affine2D,
    CubicSegment,
    LineSegment,
    PathBounds,
    VectorPath,
    VectorPathError,
)


_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9-]*")
_MATRIX = re.compile(
    r"matrix\(\s*([-+0-9.eE]+)[,\s]+([-+0-9.eE]+)[,\s]+"
    r"([-+0-9.eE]+)[,\s]+([-+0-9.eE]+)[,\s]+"
    r"([-+0-9.eE]+)[,\s]+([-+0-9.eE]+)\s*\)"
)
_PATH_TOKEN = re.compile(
    r"(?P<command>[MmLlHhVvCcZz])|"
    r"(?P<number>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)|"
    r"(?P<separator>[\s,]+)|(?P<invalid>.)"
)
_PDF_SEMANTIC_CLASSES = frozenset(
    {
        "principal_silhouette",
        "major_structural_edges",
        "glazing_openings",
        "panel_seam_lines",
        "mechanical_detail",
        "internal_cutaway_structure",
        "texture_material_hatching",
        "shadow_hatching",
        "construction_geometry",
        "dimensions_leaders",
        "labels_specifications",
        "accent_feature",
        "background_context",
    }
)


def _fail(message: str) -> NoReturn:
    raise TechnicalAssetError(message)


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        _fail(f"{label} must use lowercase letters, digits, and hyphens.")
    return value


def _finite(value: float | int | str, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TechnicalAssetError(f"{label} must be a finite number.") from exc
    if not math.isfinite(number):
        _fail(f"{label} must be a finite number.")
    return 0.0 if number == 0.0 else number


@dataclass(frozen=True, slots=True)
class ExtractionCrop:
    """A normalized-SVG crop in root viewBox coordinates."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "width", "height"):
            object.__setattr__(self, name, _finite(getattr(self, name), f"crop.{name}"))
        if self.width <= 0 or self.height <= 0:
            _fail("PDF extraction crop width and height must be positive.")

    @property
    def max_x(self) -> float:
        return self.x + self.width

    @property
    def max_y(self) -> float:
        return self.y + self.height

    def record(self) -> list[float]:
        return [self.x, self.y, self.width, self.height]


@dataclass(frozen=True, slots=True)
class PdfExtractionStats:
    source_elements: int
    source_subpaths: int
    rejected_empty_geometry_elements: int
    accepted_stroke_subpaths: int
    accepted_fill_boundaries: int
    emitted_primitives: int
    stitched_source_subpaths: int
    rejected_outside_crop: int
    rejected_crossing_crop: int
    rejected_white_paint: int
    rejected_unpainted: int
    rejected_fill_policy: int
    rejected_open_fill: int
    rejected_exclusion_zone: int
    rejected_source_element: int
    rejected_leader_candidate: int

    @property
    def accepted_subpaths(self) -> int:
        return self.accepted_stroke_subpaths + self.accepted_fill_boundaries

    def record(self) -> dict[str, int]:
        return {
            "source_elements": self.source_elements,
            "source_subpaths": self.source_subpaths,
            "rejected_empty_geometry_elements": self.rejected_empty_geometry_elements,
            "accepted_subpaths": self.accepted_subpaths,
            "accepted_stroke_subpaths": self.accepted_stroke_subpaths,
            "accepted_fill_boundaries": self.accepted_fill_boundaries,
            "emitted_primitives": self.emitted_primitives,
            "stitched_source_subpaths": self.stitched_source_subpaths,
            "rejected_outside_crop": self.rejected_outside_crop,
            "rejected_crossing_crop": self.rejected_crossing_crop,
            "rejected_white_paint": self.rejected_white_paint,
            "rejected_unpainted": self.rejected_unpainted,
            "rejected_fill_policy": self.rejected_fill_policy,
            "rejected_open_fill": self.rejected_open_fill,
            "rejected_exclusion_zone": self.rejected_exclusion_zone,
            "rejected_source_element": self.rejected_source_element,
            "rejected_leader_candidate": self.rejected_leader_candidate,
        }


@dataclass(frozen=True, slots=True)
class ImportedPdfVectorAsset:
    pdf_path: Path
    pdf_sha256: str
    page: int
    converter: str
    converter_version: str
    normalized_svg_sha256: str
    crop: ExtractionCrop
    primitives: tuple[ImportedPrimitive, ...]
    stats: PdfExtractionStats

    @property
    def view_box(self) -> tuple[float, float, float, float]:
        return (0.0, 0.0, self.crop.width, self.crop.height)


def _path_tokens(path_data: str) -> list[str | float]:
    if not isinstance(path_data, str) or not path_data.strip():
        _fail("Normalized PDF SVG path data must be non-empty text.")
    result: list[str | float] = []
    for match in _PATH_TOKEN.finditer(path_data):
        kind = match.lastgroup
        token = match.group()
        if kind == "separator":
            continue
        if kind == "invalid":
            if token.isalpha():
                _fail(
                    f"Normalized PDF SVG uses unsupported path command {token!r}; "
                    "only M/L/H/V/C/Z are accepted."
                )
            _fail(f"Normalized PDF SVG path contains invalid token {token!r}.")
        if kind == "command":
            result.append(token)
        else:
            result.append(_finite(token, "SVG path coordinate"))
    if not result or not isinstance(result[0], str) or result[0] not in {"M", "m"}:
        _fail("Normalized PDF SVG path must begin with M or m.")
    return result


def parse_normalized_pdf_path(
    path_data: str, *, allow_empty: bool = False
) -> tuple[VectorPath, ...]:
    """Parse Inkscape-normalized M/L/H/V/C/Z data into exact subpaths."""

    tokens = _path_tokens(path_data)
    index = 0
    command: str | None = None
    current = (0.0, 0.0)
    start: tuple[float, float] | None = None
    segments: list[LineSegment | CubicSegment] = []
    result: list[VectorPath] = []

    def finish(*, closed: bool) -> None:
        nonlocal start, segments, current
        if start is None:
            _fail("Normalized PDF SVG closes a path before moving to it.")
        if not segments:
            # PDF producers occasionally retain a path-paint operation whose
            # geometry is only ``M ... Z``.  It has no drawable centreline or
            # boundary and therefore cannot become a plotter primitive.
            current = start
            start = None
            return
        if (
            closed
            and isinstance(segments[-1], LineSegment)
            and segments[-1].to == start
        ):
            segments.pop()
            if not segments:
                _fail("Normalized PDF SVG contains an empty closed subpath.")
        try:
            result.append(
                VectorPath(start=start, segments=tuple(segments), closed=closed)
            )
        except VectorPathError as exc:
            raise TechnicalAssetError(
                f"Normalized PDF SVG contains invalid source geometry: {exc}"
            ) from exc
        current = start if closed else segments[-1].to
        start = None
        segments = []

    def available_numbers() -> int:
        cursor = index
        while cursor < len(tokens) and not isinstance(tokens[cursor], str):
            cursor += 1
        return cursor - index

    while index < len(tokens):
        if isinstance(tokens[index], str):
            command = str(tokens[index])
            index += 1
            if command in {"Z", "z"}:
                finish(closed=True)
                command = None
                continue
        if command is None:
            _fail("Normalized PDF SVG coordinates have no active command.")
        upper = command.upper()
        arity = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6}[upper]
        count = available_numbers()
        if count < arity:
            _fail(f"Normalized PDF SVG {command} command has too few coordinates.")
        if count % arity:
            _fail(f"Normalized PDF SVG {command} command has incomplete coordinates.")
        relative = command.islower()
        groups = count // arity
        for group_index in range(groups):
            values = [float(value) for value in tokens[index : index + arity]]
            index += arity
            if upper == "M":
                target = (
                    current[0] + values[0] if relative else values[0],
                    current[1] + values[1] if relative else values[1],
                )
                if start is not None:
                    if group_index == 0:
                        finish(closed=False)
                        current = target
                        start = target
                    else:
                        segments.append(LineSegment(target))
                        current = target
                elif group_index == 0:
                    current = target
                    start = target
                else:
                    if target != current:
                        segments.append(LineSegment(target))
                    current = target
                continue
            if start is None:
                _fail(f"Normalized PDF SVG {command} command precedes a move.")
            if upper == "L":
                target = (
                    current[0] + values[0] if relative else values[0],
                    current[1] + values[1] if relative else values[1],
                )
                if target != current:
                    segments.append(LineSegment(target))
                current = target
            elif upper == "H":
                target = (current[0] + values[0] if relative else values[0], current[1])
                if target != current:
                    segments.append(LineSegment(target))
                current = target
            elif upper == "V":
                target = (current[0], current[1] + values[0] if relative else values[0])
                if target != current:
                    segments.append(LineSegment(target))
                current = target
            else:
                base_x, base_y = current if relative else (0.0, 0.0)
                segment = CubicSegment(
                    (base_x + values[0], base_y + values[1]),
                    (base_x + values[2], base_y + values[3]),
                    (base_x + values[4], base_y + values[5]),
                )
                if not (
                    segment.control_1 == current
                    and segment.control_2 == current
                    and segment.to == current
                ):
                    segments.append(segment)
                current = segment.to
        if upper == "M":
            command = "l" if relative else "L"
    if start is not None:
        finish(closed=False)
    if not result and not allow_empty:
        _fail("Normalized PDF SVG contains no drawable subpaths.")
    return tuple(result)


def _matrix(value: str) -> Affine2D:
    match = _MATRIX.fullmatch(value.strip())
    if match is None:
        _fail(
            f"Normalized PDF SVG transform {value!r} is unsupported; "
            "flatten it to one matrix first."
        )
    return Affine2D(
        *(_finite(item, "SVG matrix coefficient") for item in match.groups())
    )


def _compose(parent: Affine2D, child: Affine2D) -> Affine2D:
    """Return the SVG matrix applying ``child`` and then ``parent``."""

    return Affine2D(
        a=parent.a * child.a + parent.c * child.b,
        b=parent.b * child.a + parent.d * child.b,
        c=parent.a * child.c + parent.c * child.d,
        d=parent.b * child.c + parent.d * child.d,
        e=parent.a * child.e + parent.c * child.f + parent.e,
        f=parent.b * child.e + parent.d * child.f + parent.f,
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _style(value: str | None) -> dict[str, str]:
    if value is None:
        return {}
    result: dict[str, str] = {}
    for declaration in value.split(";"):
        declaration = declaration.strip()
        if not declaration:
            continue
        if ":" not in declaration:
            _fail(f"Normalized PDF SVG has malformed style {declaration!r}.")
        key, item = declaration.split(":", 1)
        key, item = key.strip(), item.strip()
        if not key or not item or key in result:
            _fail("Normalized PDF SVG style properties must be unique and non-empty.")
        result[key] = item
    return result


def _paint(element: ET.Element, parents: dict[ET.Element, ET.Element], key: str) -> str:
    chain: list[ET.Element] = []
    current: ET.Element | None = element
    while current is not None:
        chain.append(current)
        current = parents.get(current)
    result: str | None = None
    for item in reversed(chain):
        item_style = _style(item.get("style"))
        if key in item_style:
            result = item_style[key]
        if item.get(key) is not None:
            result = str(item.get(key))
    return (result or ("black" if key == "fill" else "none")).strip().casefold()


def _opacity(
    element: ET.Element, parents: dict[ET.Element, ET.Element], key: str
) -> float:
    values = [_paint(element, parents, "opacity"), _paint(element, parents, key)]
    opacity = 1.0
    for value in values:
        if value in {"none", "black"}:
            continue
        opacity *= _finite(value, key)
    if not 0.0 <= opacity <= 1.0:
        _fail(f"Normalized PDF SVG {key} must be between zero and one.")
    return opacity


def _white(value: str) -> bool:
    compact = value.replace(" ", "").casefold()
    return compact in {
        "white",
        "#fff",
        "#ffffff",
        "rgb(255,255,255)",
        "rgb(100%,100%,100%)",
    }


def _neutral_dark(value: str) -> bool:
    compact = value.replace(" ", "").casefold()
    return compact in {
        "black",
        "#000",
        "#000000",
        "#231f20",
        "rgb(0,0,0)",
        "rgb(0%,0%,0%)",
    }


def _construction_red(value: str) -> bool:
    compact = value.replace(" ", "").casefold()
    return compact in {"#e31836", "rgb(227,24,54)"}


def _element_matrix(
    element: ET.Element, parents: dict[ET.Element, ET.Element]
) -> Affine2D:
    chain: list[ET.Element] = []
    current: ET.Element | None = element
    while current is not None:
        chain.append(current)
        current = parents.get(current)
    result = Affine2D()
    for item in reversed(chain):
        if item.get("transform"):
            result = _compose(result, _matrix(str(item.get("transform"))))
    return result


def _inside(bounds: PathBounds, crop: ExtractionCrop, tolerance: float = 1e-7) -> bool:
    return (
        bounds.min_x >= crop.x - tolerance
        and bounds.min_y >= crop.y - tolerance
        and bounds.max_x <= crop.max_x + tolerance
        and bounds.max_y <= crop.max_y + tolerance
    )


def _outside(bounds: PathBounds, crop: ExtractionCrop, tolerance: float = 1e-7) -> bool:
    return (
        bounds.max_x < crop.x - tolerance
        or bounds.max_y < crop.y - tolerance
        or bounds.min_x > crop.max_x + tolerance
        or bounds.min_y > crop.max_y + tolerance
    )


def _contained_by(
    bounds: PathBounds, region: ExtractionCrop, tolerance: float = 1e-7
) -> bool:
    return (
        bounds.min_x >= region.x - tolerance
        and bounds.min_y >= region.y - tolerance
        and bounds.max_x <= region.max_x + tolerance
        and bounds.max_y <= region.max_y + tolerance
    )


def _leader_candidate(
    path: VectorPath,
    minimum_length: float | None,
    *,
    actual_stroke_width: float | None,
    required_stroke_width: float | None,
) -> bool:
    if minimum_length is None or path.closed or len(path.segments) > 3:
        return False
    if required_stroke_width is not None and (
        actual_stroke_width is None
        or not math.isclose(
            actual_stroke_width,
            required_stroke_width,
            rel_tol=0.0,
            abs_tol=1e-7,
        )
    ):
        return False
    if any(isinstance(segment, CubicSegment) for segment in path.segments):
        return False
    return path.length(0.01) + 1e-9 >= minimum_length


def _reverse_vector_path(path: VectorPath) -> VectorPath:
    """Reverse one open path without flattening source cubics."""

    if path.closed:
        _fail("Closed source paths cannot enter endpoint stitching.")
    starts: list[tuple[float, float]] = [path.start]
    starts.extend(segment.to for segment in path.segments[:-1])
    reversed_segments: list[LineSegment | CubicSegment] = []
    for segment, previous in reversed(list(zip(path.segments, starts, strict=True))):
        if isinstance(segment, LineSegment):
            reversed_segments.append(LineSegment(previous))
        else:
            reversed_segments.append(
                CubicSegment(segment.control_2, segment.control_1, previous)
            )
    return VectorPath(
        start=path.end,
        segments=tuple(reversed_segments),
        closed=False,
    )


def _stitch_exact_source_subpaths(
    primitives: list[ImportedPrimitive],
) -> tuple[list[ImportedPrimitive], int]:
    """Join exact non-branching endpoints from the same PDF paint element.

    No connector is inserted: paths are joined only when their transformed
    source endpoints compare exactly equal and that endpoint has graph degree
    two inside one source element/style group.
    """

    # ``:03d`` is a minimum width, not a maximum: service-guide compound
    # paths can contain thousands of subpaths (Seiko page 3 reaches s3546).
    element_pattern = re.compile(r"^(?P<prefix>.+-pdf-e\d{5})-s\d{3,}$")
    groups: dict[tuple[object, ...], list[tuple[int, ImportedPrimitive]]] = {}
    passthrough: list[tuple[int, ImportedPrimitive]] = []
    for position, primitive in enumerate(primitives):
        match = element_pattern.fullmatch(primitive.identifier)
        if (
            match is None
            or primitive.path.closed
            or primitive.path.start == primitive.path.end
        ):
            passthrough.append((position, primitive))
            continue
        group_key: tuple[object, ...] = (
            match.group("prefix"),
            primitive.component_id,
            primitive.semantic_class,
            primitive.view,
            primitive.evidence,
            primitive.source_ref,
            primitive.feature_kind,
            primitive.confidence,
            primitive.line_style,
        )
        groups.setdefault(group_key, []).append((position, primitive))

    stitched_count = 0
    output = list(passthrough)
    for group_key, members in groups.items():
        chains: dict[
            int, tuple[VectorPath, tuple[str, ...], int, ImportedPrimitive]
        ] = {
            chain_id: (
                primitive.path,
                primitive.source_path_ids or (primitive.identifier,),
                position,
                primitive,
            )
            for chain_id, (position, primitive) in enumerate(members)
        }
        next_chain_id = len(chains)
        while True:
            endpoints: dict[tuple[float, float], list[tuple[int, str]]] = {}
            for chain_id, (path, _ids, _position, _template) in chains.items():
                if path.start == path.end:
                    continue
                endpoints.setdefault(path.start, []).append((chain_id, "start"))
                endpoints.setdefault(path.end, []).append((chain_id, "end"))
            candidates = [
                (
                    point,
                    sorted(
                        occurrences,
                        key=lambda occurrence: (
                            chains[occurrence[0]][2],
                            occurrence,
                        ),
                    ),
                )
                for point, occurrences in endpoints.items()
                if len(occurrences) == 2 and occurrences[0][0] != occurrences[1][0]
            ]
            if not candidates:
                break
            point, occurrences = min(candidates, key=lambda item: (item[0], item[1]))
            first_id, first_end = occurrences[0]
            second_id, second_end = occurrences[1]
            first_path, first_sources, first_position, first_template = chains.pop(
                first_id
            )
            second_path, second_sources, second_position, second_template = chains.pop(
                second_id
            )
            if first_end == "start":
                first_path = _reverse_vector_path(first_path)
            if second_end == "end":
                second_path = _reverse_vector_path(second_path)
            if first_path.end != point or second_path.start != point:
                _fail("Exact endpoint stitching lost its reviewed source junction.")
            merged = VectorPath(
                start=first_path.start,
                segments=(*first_path.segments, *second_path.segments),
                closed=False,
            )
            if first_position <= second_position:
                template = first_template
                source_ids = (*first_sources, *second_sources)
                position = first_position
            else:
                template = second_template
                source_ids = (*second_sources, *first_sources)
                position = second_position
            chains[next_chain_id] = (merged, source_ids, position, template)
            next_chain_id += 1
            stitched_count += 1

        prefix = str(group_key[0])
        for chain_index, (path, source_ids, position, template) in enumerate(
            sorted(chains.values(), key=lambda item: (item[2], item[1])), start=1
        ):
            if len(source_ids) == 1:
                output.append((position, template))
                continue
            output.append(
                (
                    position,
                    replace(
                        template,
                        identifier=f"{prefix}-chain-{chain_index:04d}",
                        path=path,
                        source_path_ids=source_ids,
                    ),
                )
            )
    return [
        primitive for _position, primitive in sorted(output, key=lambda item: item[0])
    ], stitched_count


def extract_normalized_pdf_svg(
    svg_path: Path,
    *,
    source_ref: str,
    view: str,
    crop: ExtractionCrop,
    include_fill_boundaries: bool = False,
    select_principal: bool = True,
    default_evidence: str = "repository-verified",
    exclusion_zones: tuple[ExtractionCrop, ...] = (),
    excluded_element_indices: frozenset[int] = frozenset(),
    leader_minimum_length: float | None = None,
    leader_stroke_width: float | None = None,
    principal_source_path: tuple[int, int] | None = None,
    semantic_by_stroke_width: tuple[tuple[float, str], ...] = (),
    stitch_exact_subpaths: bool = False,
) -> tuple[tuple[ImportedPrimitive, ...], PdfExtractionStats]:
    """Extract plot-safe source paths from one normalized, text-free PDF SVG."""

    source_id = _identifier(source_ref, "source_ref")
    selected_view = _identifier(view, "view")
    evidence = _identifier(default_evidence, "evidence")
    if leader_minimum_length is not None:
        leader_minimum_length = _finite(leader_minimum_length, "leader_minimum_length")
        if leader_minimum_length <= 0:
            _fail("leader_minimum_length must be positive when supplied.")
    if leader_stroke_width is not None:
        leader_stroke_width = _finite(leader_stroke_width, "leader_stroke_width")
        if leader_stroke_width <= 0:
            _fail("leader_stroke_width must be positive when supplied.")
        if leader_minimum_length is None:
            _fail("leader_stroke_width requires leader_minimum_length.")
    if any(
        isinstance(index, bool) or not isinstance(index, int) or index < 1
        for index in excluded_element_indices
    ):
        _fail("excluded_element_indices must contain positive integers.")
    if principal_source_path is not None and (
        len(principal_source_path) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in principal_source_path
        )
    ):
        _fail(
            "principal_source_path must contain positive element and subpath indices."
        )
    semantic_widths: list[tuple[float, str]] = []
    for raw_width, raw_semantic in semantic_by_stroke_width:
        width = _finite(raw_width, "semantic source stroke width")
        if width <= 0:
            _fail("semantic source stroke widths must be positive.")
        if raw_semantic not in _PDF_SEMANTIC_CLASSES:
            _fail(f"Unsupported PDF stroke semantic {raw_semantic!r}.")
        if any(
            math.isclose(width, existing[0], abs_tol=1e-9)
            for existing in semantic_widths
        ):
            _fail("PDF stroke semantic widths must be unique.")
        semantic_widths.append((width, raw_semantic))
    try:
        root = ET.parse(svg_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise TechnicalAssetError(
            f"Cannot parse normalized PDF SVG {svg_path}: {exc}"
        ) from exc
    if _local_name(root.tag) != "svg":
        _fail("Normalized PDF vector root must be <svg>.")
    parents = {child: parent for parent in root.iter() for child in parent}
    forbidden = {"image", "text", "use", "script", "foreignObject"}
    for element in root.iter():
        local = _local_name(element.tag)
        if local not in forbidden:
            continue
        ancestor: ET.Element | None = element
        in_defs = False
        while ancestor is not None:
            if _local_name(ancestor.tag) == "defs":
                in_defs = True
                break
            ancestor = parents.get(ancestor)
        if not in_defs or local in {"image", "script", "foreignObject"}:
            _fail(
                f"Normalized PDF SVG contains unsupported <{local}>; "
                "text, raster content and references must be removed before extraction."
            )

    counters = {
        "source_elements": 0,
        "source_subpaths": 0,
        "rejected_empty_geometry_elements": 0,
        "accepted_stroke_subpaths": 0,
        "accepted_fill_boundaries": 0,
        "emitted_primitives": 0,
        "stitched_source_subpaths": 0,
        "rejected_outside_crop": 0,
        "rejected_crossing_crop": 0,
        "rejected_white_paint": 0,
        "rejected_unpainted": 0,
        "rejected_fill_policy": 0,
        "rejected_open_fill": 0,
        "rejected_exclusion_zone": 0,
        "rejected_source_element": 0,
        "rejected_leader_candidate": 0,
    }
    primitives: list[ImportedPrimitive] = []
    translate = Affine2D(e=-crop.x, f=-crop.y)
    for element_index, element in enumerate(root.iter(), start=1):
        if _local_name(element.tag) != "path":
            continue
        path_ancestor: ET.Element | None = element
        path_in_defs = False
        while path_ancestor is not None:
            if _local_name(path_ancestor.tag) == "defs":
                path_in_defs = True
                break
            path_ancestor = parents.get(path_ancestor)
        if path_in_defs:
            continue
        counters["source_elements"] += 1
        paths = parse_normalized_pdf_path(str(element.get("d", "")), allow_empty=True)
        if not paths:
            counters["rejected_empty_geometry_elements"] += 1
            continue
        counters["source_subpaths"] += len(paths)
        if element_index in excluded_element_indices:
            counters["rejected_source_element"] += len(paths)
            continue
        transform = _element_matrix(element, parents)
        stroke = _paint(element, parents, "stroke")
        fill = _paint(element, parents, "fill")
        raw_stroke_width = _paint(element, parents, "stroke-width")
        dash_array = _paint(element, parents, "stroke-dasharray")
        stroke_width = (
            None
            if raw_stroke_width in {"none", "black"}
            else _finite(raw_stroke_width, "stroke-width")
        )
        stroke_visible = (
            stroke != "none" and _opacity(element, parents, "stroke-opacity") > 0.0
        )
        fill_visible = (
            fill != "none" and _opacity(element, parents, "fill-opacity") > 0.0
        )
        for subpath_index, raw_path in enumerate(paths, start=1):
            transformed = raw_path.transformed(transform)
            bounds = transformed.bounds()
            if _outside(bounds, crop):
                counters["rejected_outside_crop"] += 1
                continue
            if not _inside(bounds, crop):
                counters["rejected_crossing_crop"] += 1
                continue
            if any(_contained_by(bounds, region) for region in exclusion_zones):
                counters["rejected_exclusion_zone"] += 1
                continue
            if _neutral_dark(stroke) and _leader_candidate(
                transformed,
                leader_minimum_length,
                actual_stroke_width=stroke_width,
                required_stroke_width=leader_stroke_width,
            ):
                counters["rejected_leader_candidate"] += 1
                continue
            paint_kind: str | None = None
            semantic = "mechanical_detail"
            if stroke_visible and not _white(stroke):
                paint_kind = (
                    "pdf-construction-stroke"
                    if _construction_red(stroke)
                    else "pdf-stroke"
                )
                if _construction_red(stroke):
                    semantic = "construction_geometry"
                else:
                    for semantic_width, mapped_semantic in semantic_widths:
                        if stroke_width is not None and math.isclose(
                            stroke_width,
                            semantic_width,
                            rel_tol=0.0,
                            abs_tol=1e-7,
                        ):
                            semantic = mapped_semantic
                            break
                counters["accepted_stroke_subpaths"] += 1
            elif stroke_visible and _white(stroke):
                counters["rejected_white_paint"] += 1
                continue
            elif fill_visible and _white(fill):
                counters["rejected_white_paint"] += 1
                continue
            elif fill_visible and not include_fill_boundaries:
                counters["rejected_fill_policy"] += 1
                continue
            elif fill_visible and fill.startswith("url("):
                counters["rejected_fill_policy"] += 1
                continue
            elif fill_visible and not raw_path.closed:
                counters["rejected_open_fill"] += 1
                continue
            elif fill_visible:
                paint_kind = "pdf-fill-boundary"
                semantic = "major_structural_edges"
                counters["accepted_fill_boundaries"] += 1
            else:
                counters["rejected_unpainted"] += 1
                continue
            identifier = (
                f"{selected_view}-pdf-e{element_index:05d}-s{subpath_index:03d}"
            )
            primitives.append(
                ImportedPrimitive(
                    identifier=identifier,
                    component_id="movement",
                    semantic_class=semantic,
                    view=selected_view,
                    evidence=evidence,
                    source_ref=source_id,
                    path=transformed.transformed(translate),
                    feature_kind=paint_kind,
                    line_style=(
                        "dashed" if dash_array not in {"none", "black"} else None
                    ),
                )
            )
    if not primitives:
        _fail("PDF extraction crop contains no accepted source vector paths.")
    if stitch_exact_subpaths:
        primitives, stitched_count = _stitch_exact_source_subpaths(primitives)
        counters["stitched_source_subpaths"] = stitched_count
    counters["emitted_primitives"] = len(primitives)
    if principal_source_path is not None:
        principal_identifier = (
            f"{selected_view}-pdf-e{principal_source_path[0]:05d}"
            f"-s{principal_source_path[1]:03d}"
        )
        matches = [
            index
            for index, primitive in enumerate(primitives)
            if primitive.identifier == principal_identifier
            or principal_identifier in primitive.source_path_ids
        ]
        if len(matches) != 1:
            _fail(
                f"Explicit principal source path {principal_identifier!r} was not accepted exactly once."
            )
        primitives[matches[0]] = replace(
            primitives[matches[0]], semantic_class="principal_silhouette"
        )
    elif select_principal:
        candidates = [
            (primitive.path.bounds().width * primitive.path.bounds().height, index)
            for index, primitive in enumerate(primitives)
            if primitive.path.closed
        ]
        if not candidates:
            _fail(
                "PDF extraction has no closed source path for the principal silhouette."
            )
        _, principal_index = max(candidates)
        primitives[principal_index] = replace(
            primitives[principal_index], semantic_class="principal_silhouette"
        )
    return tuple(primitives), PdfExtractionStats(**counters)


def _inkscape_version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise TechnicalAssetError(f"Cannot run Inkscape PDF converter: {exc}") from exc
    version = result.stdout.strip().splitlines()
    if not version:
        _fail("Inkscape PDF converter did not report a version.")
    return version[0]


def normalize_pdf_page(
    pdf_path: Path,
    *,
    page: int,
    output: Path,
    inkscape_executable: str = "inkscape",
) -> tuple[str, str]:
    """Import one PDF page as deterministic plain SVG with all text deleted."""

    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        _fail("PDF page must be a positive one-based integer.")
    executable = shutil.which(inkscape_executable)
    if executable is None:
        _fail(f"Inkscape executable {inkscape_executable!r} is unavailable.")
    version = _inkscape_version(executable)
    output.parent.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="mapplot-inkscape-profile-"))
    environment = dict(os.environ)
    environment["INKSCAPE_PROFILE_DIR"] = str(profile)
    command = [
        executable,
        f"--pages={page}",
        "--pdf-font-strategy=delete-all",
        "--export-plain-svg",
        f"--export-filename={output}",
        str(pdf_path),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TechnicalAssetError(
            f"Cannot normalize PDF page with Inkscape: {exc}"
        ) from exc
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if result.returncode != 0 or not output.is_file():
        detail = (result.stderr or result.stdout).strip()
        _fail(f"Inkscape PDF normalization failed: {detail or 'no SVG was written'}")
    return version, sha256_file(output)


def import_pdf_page_asset(
    pdf_path: Path,
    *,
    page: int,
    crop: ExtractionCrop,
    source_ref: str,
    view: str,
    expected_sha256: str | None = None,
    include_fill_boundaries: bool = False,
    default_evidence: str = "repository-verified",
    inkscape_executable: str = "inkscape",
    exclusion_zones: tuple[ExtractionCrop, ...] = (),
    excluded_element_indices: frozenset[int] = frozenset(),
    leader_minimum_length: float | None = None,
    leader_stroke_width: float | None = None,
    principal_source_path: tuple[int, int] | None = None,
    semantic_by_stroke_width: tuple[tuple[float, str], ...] = (),
    stitch_exact_subpaths: bool = False,
) -> ImportedPdfVectorAsset:
    """Normalize and extract one hash-pinned, vector-only technical PDF page."""

    digest = sha256_file(pdf_path)
    if expected_sha256 is not None and digest != expected_sha256:
        _fail(f"Source PDF SHA-256 changed: expected {expected_sha256}, got {digest}.")
    with tempfile.TemporaryDirectory(prefix="mapplot-pdf-page-") as directory:
        normalized = Path(directory) / "page.svg"
        version, normalized_digest = normalize_pdf_page(
            pdf_path,
            page=page,
            output=normalized,
            inkscape_executable=inkscape_executable,
        )
        primitives, stats = extract_normalized_pdf_svg(
            normalized,
            source_ref=source_ref,
            view=view,
            crop=crop,
            include_fill_boundaries=include_fill_boundaries,
            default_evidence=default_evidence,
            exclusion_zones=exclusion_zones,
            excluded_element_indices=excluded_element_indices,
            leader_minimum_length=leader_minimum_length,
            leader_stroke_width=leader_stroke_width,
            principal_source_path=principal_source_path,
            semantic_by_stroke_width=semantic_by_stroke_width,
            stitch_exact_subpaths=stitch_exact_subpaths,
        )
    return ImportedPdfVectorAsset(
        pdf_path=pdf_path,
        pdf_sha256=digest,
        page=page,
        converter="inkscape-delete-all-text",
        converter_version=version,
        normalized_svg_sha256=normalized_digest,
        crop=crop,
        primitives=primitives,
        stats=stats,
    )


def extraction_geometry_sha256(primitives: Iterable[ImportedPrimitive]) -> str:
    """Digest only normalized geometry and stable source correspondence."""

    digest = hashlib.sha256()
    for primitive in primitives:
        digest.update(primitive.identifier.encode("utf-8"))
        digest.update(b"\0")
        for source_path_id in primitive.source_path_ids:
            digest.update(source_path_id.encode("utf-8"))
            digest.update(b"\0")
        digest.update(primitive.path.canonical_sha256().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = [
    "ExtractionCrop",
    "ImportedPdfVectorAsset",
    "PdfExtractionStats",
    "extract_normalized_pdf_svg",
    "extraction_geometry_sha256",
    "import_pdf_page_asset",
    "normalize_pdf_page",
    "parse_normalized_pdf_path",
]
