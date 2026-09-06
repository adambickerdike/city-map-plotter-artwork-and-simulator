"""Structured mathematical lettering for physical pen plots.

The repository's bundled single-stroke font remains the typography authority.
This module composes those glyphs into scripts, fractions, radicals, integrals,
aligned rows, and matrices.  The input is a small explicit JSON tree rather than
screen-font text or a screenshot, so line breaking can never alter an equation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from .models import MapPlotterError
from .stroke_font import stroke_text, text_width_mm
from .svgkit import reliable_vector_strokes


Point = tuple[float, float]
Stroke = tuple[Point, ...]
MATH_LAYOUT_ID = "structured-stroke-math-v1"


@dataclass(frozen=True)
class MathStroke:
    points: Stroke
    pen_role: str = "primary"
    semantic_role: str = "glyph"

    def transformed(self, scale: float, dx: float, dy: float) -> "MathStroke":
        return MathStroke(
            tuple((dx + x * scale, dy + y * scale) for x, y in self.points),
            self.pen_role,
            self.semantic_role,
        )


@dataclass(frozen=True)
class MathBox:
    strokes: tuple[MathStroke, ...]
    width: float
    height: float
    baseline: float

    def translated(self, dx: float, dy: float) -> "MathBox":
        return MathBox(
            tuple(stroke.transformed(1.0, dx, dy) for stroke in self.strokes),
            self.width,
            self.height,
            self.baseline,
        )

    def scaled(self, factor: float) -> "MathBox":
        return MathBox(
            tuple(stroke.transformed(factor, 0.0, 0.0) for stroke in self.strokes),
            self.width * factor,
            self.height * factor,
            self.baseline * factor,
        )


@dataclass(frozen=True)
class MathLayout:
    strokes: tuple[MathStroke, ...]
    width_mm: float
    height_mm: float
    baseline_mm: float
    requested_cap_height_mm: float
    effective_cap_height_mm: float
    expression_sha256: str
    renderer: str = MATH_LAYOUT_ID

    def placed(self, x_mm: float, y_mm: float) -> tuple[MathStroke, ...]:
        return tuple(stroke.transformed(1.0, x_mm, y_mm) for stroke in self.strokes)


# Six-unit coordinate cells, matching the bundled plotter font's nominal cap.
# Each symbol is an original centre-line construction, not an outline font.
_SYMBOLS: dict[str, tuple[tuple[Point, ...], ...]] = {
    "alpha": (
        ((4.0, 2.0), (3.0, 1.2), (1.2, 1.2), (0.2, 2.4), (0.2, 4.6), (1.2, 5.6), (3.0, 5.6), (4.0, 4.6), (4.0, 2.0)),
        ((3.2, 1.5), (4.7, 5.8)),
    ),
    "beta": (
        ((0.8, 6.0), (0.8, 0.8), (2.4, 0.0), (3.8, 0.8), (3.8, 2.3), (2.7, 3.1), (0.8, 3.1)),
        ((2.7, 3.1), (4.1, 4.0), (3.8, 5.2), (2.5, 6.0), (0.8, 5.2)),
    ),
    "gamma": (((0.2, 1.0), (1.4, 0.2), (3.8, 0.2), (2.4, 2.8), (2.4, 6.0)),),
    "delta": (((2.1, 0.0), (0.2, 5.8), (4.0, 5.8), (2.1, 0.0)),),
    "Delta": (((2.1, 0.0), (0.0, 6.0), (4.2, 6.0), (2.1, 0.0)),),
    "lambda": (((0.2, 0.0), (1.2, 0.0), (3.9, 6.0)), ((1.8, 2.1), (0.1, 6.0))),
    "mu": (((0.4, 1.0), (0.4, 5.0), (1.2, 5.8), (2.4, 5.8), (3.4, 4.8), (3.4, 1.0)), ((0.4, 4.8), (0.4, 6.8))),
    "sigma": (((4.2, 2.0), (3.2, 1.2), (1.2, 1.2), (0.2, 2.2), (0.2, 4.6), (1.2, 5.6), (3.2, 5.6), (4.2, 4.6), (4.2, 2.0)),),
    "Sigma": (((4.3, 0.0), (0.3, 0.0), (2.7, 3.0), (0.3, 6.0), (4.3, 6.0)),),
    "pi": (((0.2, 1.0), (4.2, 1.0)), ((1.0, 1.0), (1.0, 6.0)), ((3.5, 1.0), (3.5, 6.0))),
    "Pi": (((0.2, 0.0), (4.2, 0.0)), ((0.8, 0.0), (0.8, 6.0)), ((3.6, 0.0), (3.6, 6.0))),
    "phi": (
        ((2.1, 0.0), (2.1, 6.0)),
        ((2.1, 1.3), (0.8, 1.8), (0.2, 3.0), (0.8, 4.2), (2.1, 4.7), (3.4, 4.2), (4.0, 3.0), (3.4, 1.8), (2.1, 1.3)),
    ),
    "psi": (((0.2, 0.8), (0.2, 3.4), (1.1, 4.3), (3.1, 4.3), (4.0, 3.4), (4.0, 0.8)), ((2.1, 0.0), (2.1, 6.0))),
    "omega": (((0.2, 1.8), (0.5, 4.7), (1.3, 5.7), (2.1, 4.2), (2.9, 5.7), (3.7, 4.7), (4.0, 1.8)),),
    "Omega": (((0.2, 5.8), (1.2, 5.8), (0.4, 4.5), (0.2, 3.0), (0.8, 1.0), (2.1, 0.0), (3.4, 1.0), (4.0, 3.0), (3.8, 4.5), (3.0, 5.8), (4.0, 5.8)),),
    "integral": (((3.8, 0.3), (3.0, 0.0), (2.2, 0.6), (1.8, 2.0), (1.2, 4.8), (0.5, 5.8), (0.0, 5.6)),),
    "sum": (((4.3, 0.0), (0.2, 0.0), (2.8, 3.0), (0.2, 6.0), (4.3, 6.0)),),
    "product": (((0.2, 0.0), (4.2, 0.0)), ((0.8, 0.0), (0.8, 6.0)), ((3.6, 0.0), (3.6, 6.0))),
    "partial": (((3.5, 0.0), (1.8, 0.4), (0.8, 1.5), (0.3, 3.5), (0.8, 5.2), (2.0, 6.0), (3.4, 5.2), (4.0, 3.6), (3.4, 2.5), (2.2, 2.2), (0.5, 3.0)),),
    "nabla": (((0.1, 0.0), (4.1, 0.0), (2.1, 6.0), (0.1, 0.0)),),
    "infinity": (((2.1, 3.0), (1.1, 1.7), (0.2, 2.0), (0.0, 3.0), (0.2, 4.0), (1.1, 4.3), (2.1, 3.0), (3.1, 1.7), (4.0, 2.0), (4.2, 3.0), (4.0, 4.0), (3.1, 4.3), (2.1, 3.0)),),
    "plus-minus": (((2.1, 0.8), (2.1, 4.0)), ((0.4, 2.4), (3.8, 2.4)), ((0.4, 5.5), (3.8, 5.5))),
    "approximately": (((0.1, 2.0), (1.1, 1.2), (3.1, 2.0), (4.1, 1.2)), ((0.1, 4.6), (1.1, 3.8), (3.1, 4.6), (4.1, 3.8))),
    "not-equal": (((0.4, 1.8), (3.8, 1.8)), ((0.4, 4.2), (3.8, 4.2)), ((3.5, 0.5), (0.7, 5.5))),
    "less-equal": (((3.7, 0.8), (0.5, 3.0), (3.7, 5.2)), ((0.5, 5.8), (3.7, 5.8))),
    "greater-equal": (((0.5, 0.8), (3.7, 3.0), (0.5, 5.2)), ((0.5, 5.8), (3.7, 5.8))),
    "arrow": (((0.1, 3.0), (4.1, 3.0)), ((2.8, 1.6), (4.1, 3.0), (2.8, 4.4))),
}

_UNICODE_SYMBOLS = {
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
    "δ": "delta",
    "Δ": "Delta",
    "λ": "lambda",
    "μ": "mu",
    "σ": "sigma",
    "Σ": "Sigma",
    "π": "pi",
    "Π": "Pi",
    "φ": "phi",
    "ψ": "psi",
    "ω": "omega",
    "Ω": "Omega",
    "∫": "integral",
    "∑": "sum",
    "∏": "product",
    "∂": "partial",
    "∇": "nabla",
    "∞": "infinity",
    "±": "plus-minus",
    "≈": "approximately",
    "≠": "not-equal",
    "≤": "less-equal",
    "≥": "greater-equal",
    "→": "arrow",
}


def _role(node: Mapping[str, Any], inherited: str) -> str:
    role = node.get("pen_role", inherited)
    if role not in {"primary", "accent"}:
        raise MapPlotterError("Mathematical pen_role must be primary or accent.")
    return str(role)


def _text_box(value: str, cap: float, role: str) -> MathBox:
    if not value or value.isspace():
        raise MapPlotterError("Mathematical text nodes must contain drawable text.")
    strokes = tuple(
        MathStroke(tuple(stroke), role, "math-text")
        for stroke in stroke_text(value, x_mm=0.0, y_mm=0.0, height_mm=cap)
    )
    return MathBox(strokes, text_width_mm(value, cap_height_mm=cap), cap, cap * 0.82)


def _symbol_box(name: str, cap: float, role: str) -> MathBox:
    name = _UNICODE_SYMBOLS.get(name, name)
    try:
        source = _SYMBOLS[name]
    except KeyError as exc:
        raise MapPlotterError(f"Unsupported mathematical symbol {name!r}.") from exc
    xs = [point[0] for stroke in source for point in stroke]
    ys = [point[1] for stroke in source for point in stroke]
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    scale = cap / max(span_y, 1e-9)
    strokes = tuple(
        MathStroke(
            tuple(((x - min(xs)) * scale, (y - min(ys)) * scale) for x, y in stroke),
            role,
            f"math-symbol:{name}",
        )
        for stroke in source
    )
    return MathBox(strokes, span_x * scale, cap, cap * 0.82)


def _row_box(items: Sequence[Any], cap: float, role: str) -> MathBox:
    if not items:
        raise MapPlotterError("A mathematical row must contain at least one item.")
    boxes = [_layout_node(item, cap, role) for item in items]
    baseline = max(box.baseline for box in boxes)
    gap = cap * 0.10
    cursor = 0.0
    strokes: list[MathStroke] = []
    height = 0.0
    for index, box in enumerate(boxes):
        y = baseline - box.baseline
        strokes.extend(box.translated(cursor, y).strokes)
        height = max(height, y + box.height)
        cursor += box.width + (gap if index + 1 < len(boxes) else 0.0)
    return MathBox(tuple(strokes), cursor, height, baseline)


def _script_box(node: Mapping[str, Any], cap: float, role: str) -> MathBox:
    if "base" not in node or not any(key in node for key in ("sub", "sup")):
        raise MapPlotterError("A script node needs base and at least one of sub/sup.")
    base = _layout_node(node["base"], cap, role)
    script_cap = cap * 0.60
    superscript = _layout_node(node["sup"], script_cap, role) if "sup" in node else None
    subscript = _layout_node(node["sub"], script_cap, role) if "sub" in node else None
    base_y = superscript.height * 0.72 if superscript else 0.0
    script_x = base.width + cap * 0.08
    strokes = list(base.translated(0.0, base_y).strokes)
    width = base.width
    height = base_y + base.height
    if superscript:
        strokes.extend(superscript.translated(script_x, 0.0).strokes)
        width = max(width, script_x + superscript.width)
        height = max(height, superscript.height)
    if subscript:
        sub_y = base_y + base.baseline * 0.78
        strokes.extend(subscript.translated(script_x, sub_y).strokes)
        width = max(width, script_x + subscript.width)
        height = max(height, sub_y + subscript.height)
    return MathBox(tuple(strokes), width, height, base_y + base.baseline)


def _fraction_box(node: Mapping[str, Any], cap: float, role: str) -> MathBox:
    if "numerator" not in node or "denominator" not in node:
        raise MapPlotterError("A fraction needs numerator and denominator.")
    child_cap = cap * 0.78
    numerator = _layout_node(node["numerator"], child_cap, role)
    denominator = _layout_node(node["denominator"], child_cap, role)
    padding = cap * 0.18
    gap = cap * 0.14
    width = max(numerator.width, denominator.width) + 2 * padding
    bar_y = numerator.height + gap
    denominator_y = bar_y + gap
    strokes = [
        *numerator.translated((width - numerator.width) / 2, 0.0).strokes,
        MathStroke(((0.0, bar_y), (width, bar_y)), role, "fraction-bar"),
        *denominator.translated((width - denominator.width) / 2, denominator_y).strokes,
    ]
    height = denominator_y + denominator.height
    return MathBox(tuple(strokes), width, height, bar_y + gap + denominator.baseline * 0.25)


def _group_box(node: Mapping[str, Any], cap: float, role: str) -> MathBox:
    if "body" not in node:
        raise MapPlotterError("A mathematical group needs a body.")
    left = str(node.get("left", "("))
    right = str(node.get("right", ")"))
    if left not in {"(", "[", "{"} or right not in {")", "]", "}"}:
        raise MapPlotterError("Mathematical group delimiters must be (), [], or {}.")
    body = _layout_node(node["body"], cap, role)
    delimiter_cap = max(cap, body.height)
    left_box = _text_box(left, delimiter_cap, role)
    right_box = _text_box(right, delimiter_cap, role)
    gap = cap * 0.08
    baseline = max(body.baseline, left_box.baseline, right_box.baseline)
    x_body = left_box.width + gap
    x_right = x_body + body.width + gap
    strokes = (
        *left_box.translated(0.0, baseline - left_box.baseline).strokes,
        *body.translated(x_body, baseline - body.baseline).strokes,
        *right_box.translated(x_right, baseline - right_box.baseline).strokes,
    )
    height = max(
        baseline - left_box.baseline + left_box.height,
        baseline - body.baseline + body.height,
        baseline - right_box.baseline + right_box.height,
    )
    return MathBox(strokes, x_right + right_box.width, height, baseline)


def _matrix_box(node: Mapping[str, Any], cap: float, role: str) -> MathBox:
    rows = node.get("rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
        raise MapPlotterError("A mathematical matrix needs non-empty rows.")
    if any(not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or not row for row in rows):
        raise MapPlotterError("Each mathematical matrix row must be non-empty.")
    column_count = len(rows[0])
    if any(len(row) != column_count for row in rows):
        raise MapPlotterError("Mathematical matrix rows must have equal lengths.")
    entry_cap = cap * 0.68
    boxes = [[_layout_node(item, entry_cap, role) for item in row] for row in rows]
    column_widths = [max(row[column].width for row in boxes) for column in range(column_count)]
    row_heights = [max(box.height for box in row) for row in boxes]
    column_gap = cap * 0.35
    row_gap = cap * 0.24
    inner_width = sum(column_widths) + column_gap * max(column_count - 1, 0)
    inner_height = sum(row_heights) + row_gap * max(len(rows) - 1, 0)
    bracket_width = cap * 0.28
    padding = cap * 0.16
    strokes: list[MathStroke] = []
    y = 0.0
    for row_index, row in enumerate(boxes):
        x = bracket_width + padding
        for column_index, box in enumerate(row):
            strokes.extend(
                box.translated(
                    x + (column_widths[column_index] - box.width) / 2,
                    y + (row_heights[row_index] - box.height) / 2,
                ).strokes
            )
            x += column_widths[column_index] + column_gap
        y += row_heights[row_index] + row_gap
    right_x = bracket_width + 2 * padding + inner_width
    strokes.extend(
        (
            MathStroke(
                ((bracket_width, 0.0), (0.0, 0.0), (0.0, inner_height), (bracket_width, inner_height)),
                role,
                "matrix-bracket",
            ),
            MathStroke(
                ((right_x, 0.0), (right_x + bracket_width, 0.0), (right_x + bracket_width, inner_height), (right_x, inner_height)),
                role,
                "matrix-bracket",
            ),
        )
    )
    return MathBox(
        tuple(strokes),
        right_x + bracket_width,
        inner_height,
        inner_height / 2 + entry_cap * 0.30,
    )


def _operator_box(node: Mapping[str, Any], cap: float, role: str) -> MathBox:
    operator_type = str(node["type"])
    symbol_name = {"integral": "integral", "sum": "sum", "product": "product"}[operator_type]
    symbol = _symbol_box(symbol_name, cap * 1.35, role)
    lower = _layout_node(node["lower"], cap * 0.50, role) if "lower" in node else None
    upper = _layout_node(node["upper"], cap * 0.50, role) if "upper" in node else None
    body_value = node.get("body", node.get("integrand"))
    if body_value is None:
        raise MapPlotterError(f"A mathematical {operator_type} needs a body.")
    body = _layout_node(body_value, cap, role)
    op_width = max(symbol.width, lower.width if lower else 0.0, upper.width if upper else 0.0)
    symbol_y = upper.height * 0.75 if upper else 0.0
    strokes: list[MathStroke] = list(
        symbol.translated((op_width - symbol.width) / 2, symbol_y).strokes
    )
    if upper:
        strokes.extend(upper.translated((op_width - upper.width) / 2, 0.0).strokes)
    if lower:
        lower_y = symbol_y + symbol.height * 0.78
        strokes.extend(lower.translated((op_width - lower.width) / 2, lower_y).strokes)
    op_height = max(
        symbol_y + symbol.height,
        (symbol_y + symbol.height * 0.78 + lower.height) if lower else 0.0,
    )
    op_baseline = symbol_y + symbol.baseline * 0.72
    gap = cap * 0.16
    body_y = op_baseline - body.baseline
    if body_y < 0:
        shift = -body_y
        strokes = [stroke.transformed(1.0, 0.0, shift) for stroke in strokes]
        op_baseline += shift
        op_height += shift
        body_y = 0.0
    strokes.extend(body.translated(op_width + gap, body_y).strokes)
    return MathBox(
        tuple(strokes),
        op_width + gap + body.width,
        max(op_height, body_y + body.height),
        op_baseline,
    )


def _sqrt_box(node: Mapping[str, Any], cap: float, role: str) -> MathBox:
    if "radicand" not in node:
        raise MapPlotterError("A square-root node needs a radicand.")
    body = _layout_node(node["radicand"], cap, role)
    hook_width = cap * 0.55
    top_y = 0.0
    baseline = body.baseline + cap * 0.14
    body_y = cap * 0.14
    strokes = [
        MathStroke(
            (
                (0.0, body_y + body.height * 0.58),
                (hook_width * 0.30, body_y + body.height * 0.75),
                (hook_width * 0.55, body_y + body.height),
                (hook_width, top_y),
                (hook_width + body.width + cap * 0.10, top_y),
            ),
            role,
            "radical",
        ),
        *body.translated(hook_width + cap * 0.06, body_y).strokes,
    ]
    return MathBox(
        tuple(strokes),
        hook_width + body.width + cap * 0.10,
        body_y + body.height,
        baseline,
    )


def _accent_box(node: Mapping[str, Any], cap: float, role: str) -> MathBox:
    if "base" not in node:
        raise MapPlotterError("A mathematical accent needs a base.")
    kind = node.get("kind")
    if kind not in {"hat", "bar", "vector"}:
        raise MapPlotterError("Mathematical accent kind must be hat, bar, or vector.")
    body = _layout_node(node["base"], cap, role)
    gap = cap * 0.10
    body_y = cap * 0.28
    accent_points: tuple[Point, ...]
    if kind == "hat":
        accent_points = ((0.0, gap), (body.width / 2, 0.0), (body.width, gap))
    elif kind == "bar":
        accent_points = ((0.0, gap * 0.5), (body.width, gap * 0.5))
    else:
        accent_points = (
            (0.0, gap * 0.5),
            (body.width, gap * 0.5),
            (body.width - cap * 0.20, 0.0),
            (body.width, gap * 0.5),
            (body.width - cap * 0.20, gap),
        )
    accent_role = str(node.get("accent_pen_role", role))
    if accent_role not in {"primary", "accent"}:
        raise MapPlotterError("Mathematical accent_pen_role must be primary or accent.")
    strokes = (
        MathStroke(accent_points, accent_role, f"math-accent:{kind}"),
        *body.translated(0.0, body_y).strokes,
    )
    return MathBox(strokes, body.width, body_y + body.height, body_y + body.baseline)


def _layout_node(value: Any, cap: float, inherited_role: str) -> MathBox:
    if isinstance(value, str):
        return _text_box(value, cap, inherited_role)
    if not isinstance(value, Mapping):
        raise MapPlotterError("Mathematical expressions must be text or structured objects.")
    node_type = value.get("type")
    if not isinstance(node_type, str):
        raise MapPlotterError("Every mathematical object needs a type.")
    role = _role(value, inherited_role)
    if node_type == "text":
        text = value.get("value")
        if not isinstance(text, str):
            raise MapPlotterError("Mathematical text.value must be text.")
        return _text_box(text, cap, role)
    if node_type == "symbol":
        name = value.get("name")
        if not isinstance(name, str):
            raise MapPlotterError("Mathematical symbol.name must be text.")
        return _symbol_box(name, cap, role)
    if node_type == "row":
        items = value.get("items")
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise MapPlotterError("Mathematical row.items must be an array.")
        return _row_box(items, cap, role)
    if node_type == "script":
        return _script_box(value, cap, role)
    if node_type == "fraction":
        return _fraction_box(value, cap, role)
    if node_type == "group":
        return _group_box(value, cap, role)
    if node_type == "matrix":
        return _matrix_box(value, cap, role)
    if node_type in {"integral", "sum", "product"}:
        return _operator_box(value, cap, role)
    if node_type == "sqrt":
        return _sqrt_box(value, cap, role)
    if node_type == "accent":
        return _accent_box(value, cap, role)
    raise MapPlotterError(f"Unsupported mathematical node type {node_type!r}.")


def expression_sha256(expression: Any) -> str:
    try:
        encoded = json.dumps(
            expression,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MapPlotterError(f"Mathematical expression is not canonical JSON: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def layout_math(
    expression: Any,
    *,
    cap_height_mm: float,
    nib_mm: float,
    maximum_width_mm: float,
    maximum_height_mm: float,
) -> MathLayout:
    """Lay out one indivisible expression and fit it uniformly to a rectangle."""

    values = (cap_height_mm, nib_mm, maximum_width_mm, maximum_height_mm)
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise MapPlotterError("Mathematical physical dimensions must be positive and finite.")
    box = _layout_node(expression, cap_height_mm, "primary")
    factor = min(1.0, maximum_width_mm / box.width, maximum_height_mm / box.height)
    fitted = box.scaled(factor)
    effective_cap = cap_height_mm * factor
    if effective_cap + 1e-9 < 8.0 * nib_mm:
        raise MapPlotterError(
            "Mathematical expression cannot fit without falling below the binding "
            f"eight-nib cap-height floor ({8.0 * nib_mm:g} mm)."
        )
    reliable_vector_strokes(
        [list(stroke.points) for stroke in fitted.strokes],
        nib_mm=nib_mm,
    )
    return MathLayout(
        strokes=fitted.strokes,
        width_mm=fitted.width,
        height_mm=fitted.height,
        baseline_mm=fitted.baseline,
        requested_cap_height_mm=cap_height_mm,
        effective_cap_height_mm=effective_cap,
        expression_sha256=expression_sha256(expression),
    )


def symbol_names() -> tuple[str, ...]:
    return tuple(sorted(_SYMBOLS))


__all__ = [
    "MATH_LAYOUT_ID",
    "MathLayout",
    "MathStroke",
    "expression_sha256",
    "layout_math",
    "symbol_names",
]
