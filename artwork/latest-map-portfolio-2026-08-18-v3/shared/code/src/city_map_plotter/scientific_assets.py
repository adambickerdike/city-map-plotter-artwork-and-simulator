"""Rights-neutral local asset loaders for academic artwork.

No network acquisition lives here.  Assets are read only from paths explicitly
referenced by the caller, verified by SHA-256, and converted to the repository's
canonical line/cubic interchange.  SVG constructs that could hide raster data,
screen text, cloned branding, or unsupported geometry fail closed.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, NoReturn, Sequence
from xml.etree import ElementTree as ET

from .models import MapPlotterError
from .niche_common import Rect
from .scientific_data import NumericValue, coerce_numeric_columns
from .vector_path import Affine2D, CubicSegment, LineSegment, VectorPath


SVG_NS = "http://www.w3.org/2000/svg"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
_PATH_TOKEN = re.compile(rf"[MLCZ]|{_NUMBER}")
_TRANSFORM = re.compile(r"([A-Za-z]+)\s*\(([^)]*)\)")
_POINT_TOKEN = re.compile(_NUMBER)


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(f"Scientific asset review required: {message}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_local_asset(spec_path: Path, asset_path: str) -> Path:
    """Resolve one normalized path beneath the specification's directory."""

    if not isinstance(asset_path, str) or not asset_path.strip():
        raise MapPlotterError("Scientific asset path must be non-empty text.")
    pure = PurePosixPath(asset_path)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != asset_path:
        raise MapPlotterError(
            "Scientific asset paths must be normalized relative POSIX paths."
        )
    base = spec_path.resolve().parent
    candidate = (base / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise MapPlotterError("Scientific asset path escapes the input directory.") from exc
    if not candidate.is_file():
        raise MapPlotterError(f"Scientific asset does not exist: {asset_path}.")
    return candidate


def verify_asset(path: Path, expected_sha256: str) -> str:
    if not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None:
        raise MapPlotterError("Scientific asset sha256 must be lowercase hexadecimal.")
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise MapPlotterError(
            f"Scientific asset hash mismatch for {path.name}: expected "
            f"{expected_sha256}, found {actual}."
        )
    return actual


def _reject_constant(value: str) -> NoReturn:
    raise MapPlotterError(f"Scientific JSON may not contain {value}.")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MapPlotterError(f"Scientific JSON repeats key {key!r}.")
        result[key] = value
    return result


def load_json_asset(path: Path, expected_sha256: str) -> Any:
    verify_asset(path, expected_sha256)
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MapPlotterError(f"Could not read scientific JSON {path.name}: {exc}") from exc


def load_csv_columns(path: Path, expected_sha256: str) -> dict[str, tuple[NumericValue, ...]]:
    """Load a strict headered numeric CSV, retaining blank fields as gaps."""

    verify_asset(path, expected_sha256)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or any(not name or not name.strip() for name in reader.fieldnames):
                raise MapPlotterError("Scientific CSV needs non-empty column headings.")
            names = [name.strip() for name in reader.fieldnames]
            if len(names) != len(set(names)):
                raise MapPlotterError("Scientific CSV repeats a column heading.")
            raw: dict[str, list[float | None]] = {name: [] for name in names}
            for row_index, row in enumerate(reader, start=2):
                if None in row:
                    raise MapPlotterError(f"Scientific CSV row {row_index} has extra fields.")
                for original, name in zip(reader.fieldnames, names, strict=True):
                    cell = row.get(original)
                    if cell is None:
                        raise MapPlotterError(
                            f"Scientific CSV row {row_index} is missing column {name!r}."
                        )
                    stripped = cell.strip()
                    if not stripped:
                        raw[name].append(None)
                        continue
                    try:
                        value = float(stripped)
                    except ValueError as exc:
                        raise MapPlotterError(
                            f"Scientific CSV {name}[row {row_index}] is not numeric."
                        ) from exc
                    if math.isnan(value):
                        raw[name].append(None)
                    elif not math.isfinite(value):
                        raise MapPlotterError(
                            f"Scientific CSV {name}[row {row_index}] may not be infinite."
                        )
                    else:
                        raw[name].append(value)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise MapPlotterError(f"Could not read scientific CSV {path.name}: {exc}") from exc
    return coerce_numeric_columns(raw, f"CSV {path.name}")


def _finite(raw: str | None, label: str, *, default: float | None = None) -> float:
    if raw is None:
        if default is None:
            _fail(f"{label} is required.")
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise MapPlotterError(f"Scientific asset review required: {label} is not numeric.") from exc
    if not math.isfinite(value):
        _fail(f"{label} must be finite.")
    return value


def _tokenize_path(data: str) -> list[str]:
    tokens: list[str] = []
    cursor = 0
    for match in _PATH_TOKEN.finditer(data):
        if data[cursor : match.start()].strip(" ,\t\r\n"):
            _fail("SVG path contains unsupported syntax or a relative command.")
        tokens.append(match.group(0))
        cursor = match.end()
    if data[cursor:].strip(" ,\t\r\n"):
        _fail("SVG path contains unsupported trailing syntax.")
    if not tokens:
        _fail("SVG path data is empty.")
    return tokens


def parse_absolute_path_data(data: str) -> tuple[VectorPath, ...]:
    """Parse the established absolute M/L/C/Z subset, including subpaths."""

    tokens = _tokenize_path(data)
    paths: list[VectorPath] = []
    start: tuple[float, float] | None = None
    segments: list[LineSegment | CubicSegment] = []
    closed = False
    command: str | None = None
    index = 0

    def finish() -> None:
        nonlocal start, segments, closed
        if start is None:
            return
        if not segments:
            _fail("SVG subpath has no drawable segment.")
        paths.append(VectorPath(start=start, segments=tuple(segments), closed=closed))
        start = None
        segments = []
        closed = False

    def numbers(count: int) -> tuple[float, ...]:
        nonlocal index
        if index + count > len(tokens) or any(
            token in {"M", "L", "C", "Z"} for token in tokens[index : index + count]
        ):
            _fail(f"SVG {command} command has too few coordinates.")
        result = tuple(_finite(token, "SVG path coordinate") for token in tokens[index : index + count])
        index += count
        return result

    while index < len(tokens):
        token = tokens[index]
        if token in {"M", "L", "C", "Z"}:
            command = token
            index += 1
            if command == "Z":
                if start is None or not segments:
                    _fail("SVG Z command does not close a drawable subpath.")
                closed = True
                finish()
                command = None
                continue
        if command is None:
            _fail("SVG path coordinate appears without a command.")
        if command == "M":
            x, y = numbers(2)
            if start is not None:
                finish()
            start = (x, y)
            command = "L"
        elif command == "L":
            if start is None:
                _fail("SVG path must begin with M.")
            x, y = numbers(2)
            segments.append(LineSegment((x, y)))
        elif command == "C":
            if start is None:
                _fail("SVG path must begin with M.")
            x1, y1, x2, y2, x, y = numbers(6)
            segments.append(CubicSegment((x1, y1), (x2, y2), (x, y)))
        else:
            _fail(f"Unsupported SVG path command {command!r}.")
    finish()
    return tuple(paths)


def _compose(outer: Affine2D, inner: Affine2D) -> Affine2D:
    """Return the matrix applying ``inner`` and then ``outer``."""

    return Affine2D(
        a=outer.a * inner.a + outer.c * inner.b,
        b=outer.b * inner.a + outer.d * inner.b,
        c=outer.a * inner.c + outer.c * inner.d,
        d=outer.b * inner.c + outer.d * inner.d,
        e=outer.a * inner.e + outer.c * inner.f + outer.e,
        f=outer.b * inner.e + outer.d * inner.f + outer.f,
    )


def _parse_transform(value: str | None) -> Affine2D:
    if value is None or not value.strip():
        return Affine2D()
    cursor = 0
    matrix = Affine2D()
    for match in _TRANSFORM.finditer(value):
        if value[cursor : match.start()].strip(" ,\t\r\n"):
            _fail("SVG transform contains unsupported syntax.")
        name = match.group(1)
        arguments = [
            _finite(token, f"SVG {name} argument")
            for token in re.split(r"[\s,]+", match.group(2).strip())
            if token
        ]
        if name == "matrix" and len(arguments) == 6:
            local = Affine2D(*arguments)
        elif name == "translate" and len(arguments) in {1, 2}:
            local = Affine2D(e=arguments[0], f=arguments[1] if len(arguments) == 2 else 0.0)
        elif name == "scale" and len(arguments) in {1, 2}:
            local = Affine2D(a=arguments[0], d=arguments[1] if len(arguments) == 2 else arguments[0])
        elif name == "rotate" and len(arguments) in {1, 3}:
            angle = math.radians(arguments[0])
            rotation = Affine2D(a=math.cos(angle), b=math.sin(angle), c=-math.sin(angle), d=math.cos(angle))
            if len(arguments) == 3:
                cx, cy = arguments[1:]
                local = _compose(
                    Affine2D(e=cx, f=cy),
                    _compose(rotation, Affine2D(e=-cx, f=-cy)),
                )
            else:
                local = rotation
        else:
            _fail(f"SVG transform {name!r} has unsupported arguments.")
        matrix = _compose(matrix, local)
        cursor = match.end()
    if value[cursor:].strip(" ,\t\r\n"):
        _fail("SVG transform contains unsupported trailing syntax.")
    return matrix


def _points(value: str | None, label: str) -> list[tuple[float, float]]:
    if value is None:
        _fail(f"{label} points are required.")
    tokens = _POINT_TOKEN.findall(value)
    remainder = _POINT_TOKEN.sub("", value).strip(" ,\t\r\n")
    if remainder or len(tokens) < 4 or len(tokens) % 2:
        _fail(f"{label} points are malformed.")
    numbers = [_finite(token, f"{label} coordinate") for token in tokens]
    return list(zip(numbers[::2], numbers[1::2], strict=True))


def _polyline_path(points: Sequence[tuple[float, float]], *, closed: bool) -> VectorPath:
    if len(points) < (3 if closed else 2):
        _fail("SVG polygon/polyline has too few points.")
    return VectorPath(
        start=points[0],
        segments=tuple(LineSegment(point) for point in points[1:]),
        closed=closed,
    )


def _ellipse_path(cx: float, cy: float, rx: float, ry: float) -> VectorPath:
    if rx <= 0 or ry <= 0:
        _fail("SVG circle/ellipse radii must be positive.")
    kappa = 0.5522847498307936
    return VectorPath(
        start=(cx + rx, cy),
        segments=(
            CubicSegment((cx + rx, cy + kappa * ry), (cx + kappa * rx, cy + ry), (cx, cy + ry)),
            CubicSegment((cx - kappa * rx, cy + ry), (cx - rx, cy + kappa * ry), (cx - rx, cy)),
            CubicSegment((cx - rx, cy - kappa * ry), (cx - kappa * rx, cy - ry), (cx, cy - ry)),
            CubicSegment((cx + kappa * rx, cy - ry), (cx + rx, cy - kappa * ry), (cx + rx, cy)),
        ),
        closed=True,
    )


def _semantic_role(element: ET.Element) -> str:
    candidates = " ".join(
        filter(
            None,
            (
                element.get("data-role", ""),
                element.get("id", ""),
                element.get("class", ""),
            ),
        )
    ).casefold()
    matches = (
        ("error", "error-bar"),
        ("uncert", "uncertainty"),
        ("fit", "fit"),
        ("trace", "trace"),
        ("curve", "trace"),
        ("axis", "axis"),
        ("tick", "tick"),
        ("marker", "marker"),
        ("legend", "legend"),
        ("annot", "annotation"),
        ("panel", "panel"),
    )
    return next((role for needle, role in matches if needle in candidates), "unclassified")


@dataclass(frozen=True)
class ImportedVectorPath:
    path: VectorPath
    element_id: str | None
    candidate_role: str
    source_order: int


@dataclass(frozen=True)
class SVGLinework:
    paths: tuple[ImportedVectorPath, ...]
    content_sha256: str
    source_view_box: tuple[float, float, float, float] | None
    classification: str = "conservative-id-class-data-role-v1"
    parser: str = "absolute-mlcz-and-basic-primitives-v1"


def extract_svg_linework(path: Path, expected_sha256: str) -> SVGLinework:
    """Extract explicit vector centre-lines from a verified local SVG."""

    digest = verify_asset(path, expected_sha256)
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise MapPlotterError(f"Could not parse scientific SVG {path.name}: {exc}") from exc
    if root.tag != f"{{{SVG_NS}}}svg":
        _fail("asset root is not an SVG document.")
    view_box: tuple[float, float, float, float] | None = None
    if root.get("viewBox"):
        values = [
            _finite(token, "SVG viewBox")
            for token in re.split(r"[\s,]+", root.get("viewBox", "").strip())
            if token
        ]
        if len(values) != 4 or values[2] <= 0 or values[3] <= 0:
            _fail("SVG viewBox must contain x, y, positive width, and positive height.")
        view_box = (values[0], values[1], values[2], values[3])

    imported: list[ImportedVectorPath] = []
    forbidden = {"image", "text", "use", "foreignObject", "style", "filter", "mask", "pattern"}
    containers = {"svg", "g", "a", "switch"}
    ignored = {"metadata", "title", "desc", "namedview"}

    def visit(element: ET.Element, parent_matrix: Affine2D) -> None:
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name == "defs":
            return
        if local_name in forbidden:
            _fail(f"SVG contains unsupported {local_name!r}; reconstruct it from cleared vectors/data.")
        if local_name in ignored:
            return
        matrix = _compose(parent_matrix, _parse_transform(element.get("transform")))
        raw_paths: tuple[VectorPath, ...] = ()
        if local_name in containers:
            for child in element:
                visit(child, matrix)
            return
        if local_name == "path":
            raw_paths = parse_absolute_path_data(element.get("d", ""))
        elif local_name == "line":
            raw_paths = (
                VectorPath(
                    start=(_finite(element.get("x1"), "SVG line.x1", default=0.0), _finite(element.get("y1"), "SVG line.y1", default=0.0)),
                    segments=(LineSegment((_finite(element.get("x2"), "SVG line.x2", default=0.0), _finite(element.get("y2"), "SVG line.y2", default=0.0))),),
                ),
            )
        elif local_name in {"polyline", "polygon"}:
            raw_paths = (_polyline_path(_points(element.get("points"), f"SVG {local_name}"), closed=local_name == "polygon"),)
        elif local_name == "rect":
            if _finite(element.get("rx"), "SVG rect.rx", default=0.0) or _finite(element.get("ry"), "SVG rect.ry", default=0.0):
                _fail("rounded SVG rectangles require conversion to canonical cubic paths.")
            x = _finite(element.get("x"), "SVG rect.x", default=0.0)
            y = _finite(element.get("y"), "SVG rect.y", default=0.0)
            width = _finite(element.get("width"), "SVG rect.width")
            height = _finite(element.get("height"), "SVG rect.height")
            if width <= 0 or height <= 0:
                _fail("SVG rectangle dimensions must be positive.")
            raw_paths = (_polyline_path(((x, y), (x + width, y), (x + width, y + height), (x, y + height)), closed=True),)
        elif local_name == "circle":
            radius = _finite(element.get("r"), "SVG circle.r")
            raw_paths = (_ellipse_path(_finite(element.get("cx"), "SVG circle.cx", default=0.0), _finite(element.get("cy"), "SVG circle.cy", default=0.0), radius, radius),)
        elif local_name == "ellipse":
            raw_paths = (_ellipse_path(_finite(element.get("cx"), "SVG ellipse.cx", default=0.0), _finite(element.get("cy"), "SVG ellipse.cy", default=0.0), _finite(element.get("rx"), "SVG ellipse.rx"), _finite(element.get("ry"), "SVG ellipse.ry")),)
        else:
            _fail(f"SVG element {local_name!r} is unsupported.")
        role = _semantic_role(element)
        for raw_path in raw_paths:
            imported.append(
                ImportedVectorPath(
                    raw_path.transformed(matrix),
                    element.get("id"),
                    role,
                    len(imported),
                )
            )

    visit(root, Affine2D())
    if not imported:
        _fail("SVG contains no supported vector linework.")
    return SVGLinework(tuple(imported), digest, view_box)


def fit_vector_paths(
    paths: Iterable[VectorPath],
    rect: Rect,
    *,
    preserve_aspect: bool = True,
) -> tuple[VectorPath, ...]:
    """Fit canonical vectors to a format-owned field rectangle."""

    values = tuple(paths)
    if not values:
        raise MapPlotterError("Cannot fit an empty scientific vector asset.")
    bounds = [path.bounds() for path in values]
    minimum_x = min(bound.min_x for bound in bounds)
    minimum_y = min(bound.min_y for bound in bounds)
    maximum_x = max(bound.max_x for bound in bounds)
    maximum_y = max(bound.max_y for bound in bounds)
    span_x = maximum_x - minimum_x
    span_y = maximum_y - minimum_y
    if span_x <= 0 or span_y <= 0:
        raise MapPlotterError("Scientific vector asset has degenerate bounds.")
    scale_x = rect.width / span_x
    scale_y = rect.height / span_y
    if preserve_aspect:
        scale_x = scale_y = min(scale_x, scale_y)
    used_width = span_x * scale_x
    used_height = span_y * scale_y
    target_x = rect.x + (rect.width - used_width) / 2
    target_y = rect.y + (rect.height - used_height) / 2
    transform = Affine2D(
        a=scale_x,
        d=scale_y,
        e=target_x - minimum_x * scale_x,
        f=target_y - minimum_y * scale_y,
    )
    return tuple(path.transformed(transform) for path in values)


__all__ = [
    "ImportedVectorPath",
    "SVGLinework",
    "extract_svg_linework",
    "fit_vector_paths",
    "load_csv_columns",
    "load_json_asset",
    "parse_absolute_path_data",
    "resolve_local_asset",
    "sha256_file",
    "verify_asset",
]
