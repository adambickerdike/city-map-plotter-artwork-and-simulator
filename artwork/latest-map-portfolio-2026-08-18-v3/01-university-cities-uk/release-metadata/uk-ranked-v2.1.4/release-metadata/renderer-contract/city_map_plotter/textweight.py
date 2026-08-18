"""Type weight, built from the same parallel offsets the road engine uses.

``LayerStyle.strokes`` means one thing across this program: *n* pen paths at
``0.85 x nib`` pitch, so the mark is ``nib + (n-1) x 0.85 x nib`` wide. Roads
get that from :mod:`physical`; lettering gets it from here, off the same
``_offset_positions`` table, so a "2-stroke" title and a "2-stroke" A-road are
the same physical decision.

The one thing type may not do is offset a glyph as a single geometry. Offsetting
the union of ``A``'s outline and crossbar, or of ``K``'s three strokes, folds
the apex and collapses the junction. Every stroke is therefore offset on its
own, and the family is recombined afterwards -- see ``weight_text`` for why the
recombination merges shared endpoints instead of noding every crossing.
"""

from __future__ import annotations

from dataclasses import dataclass

from shapely.errors import GEOSException
from shapely.geometry import LineString, MultiLineString
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, unary_union

from .models import MapPlotterError
from .pens import DEFAULT_OFFSET_PITCH_RATIO, MAX_PARALLEL_STROKES

# The road compiler's offset ladder. Imported rather than reimplemented: if the
# spacing rule ever changes, type must move with it.
from .physical import _offset_positions as offset_positions


Point = tuple[float, float]
Stroke = list[Point]


#: Matches the ``mitre_limit`` passed to ``offset_curve`` below. A mitred corner
#: can throw the outer offset this many times the offset distance past the
#: original path, which is what a zone has to leave room for.
OFFSET_MITRE_LIMIT = 2.5


def weight_bleed_mm(
    *, nib_mm: float, stroke_count: int, pitch_ratio: float = DEFAULT_OFFSET_PITCH_RATIO
) -> float:
    """Worst-case distance weighted lettering reaches past its glyph path."""

    if stroke_count <= 1:
        return 0.0
    outermost = (stroke_count - 1) / 2 * pitch_ratio * nib_mm
    return outermost * OFFSET_MITRE_LIMIT


def weighted_mark_width_mm(
    *, nib_mm: float, stroke_count: int, pitch_ratio: float = DEFAULT_OFFSET_PITCH_RATIO
) -> float:
    """The stem width ``stroke_count`` offsets actually achieve."""

    return nib_mm + (stroke_count - 1) * pitch_ratio * nib_mm


def _line_parts(geometry: BaseGeometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return [part for part in geometry.geoms if not part.is_empty]
    parts: list[LineString] = []
    for part in getattr(geometry, "geoms", ()):
        parts.extend(_line_parts(part))
    return parts


def _points(line: LineString) -> Stroke:
    return [(float(x), float(y)) for x, y in line.coords]


@dataclass(frozen=True)
class WeightedText:
    """The emboldened pen paths, plus what the physical gate had to discard."""

    strokes: list[Stroke]
    stroke_count: int
    pitch_mm: float
    plotted_width_mm: float
    #: Companion offsets that came back under ``3 x nib`` and were dropped.
    dropped_companions: int
    #: Components whose companions all collapsed, left at single weight.
    filled_components: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "stroke_count": self.stroke_count,
            "offset_pitch_mm": round(self.pitch_mm, 6),
            "plotted_width_mm": round(self.plotted_width_mm, 6),
            "dropped_companions": self.dropped_companions,
            "filled_components": self.filled_components,
        }


def weight_text(
    strokes: list[Stroke],
    *,
    stroke_count: int,
    nib_mm: float,
    pitch_ratio: float = DEFAULT_OFFSET_PITCH_RATIO,
) -> WeightedText:
    """Return ``stroke_count`` parallel pen paths for each glyph stroke.

    ``stroke_count == 1`` returns the input untouched, so ordinary lettering
    never pays for a geometry round-trip.

    A companion offset is not copy: it is a second pass alongside a stroke that
    is already being drawn. When an inward companion of a small closed
    component -- the dot on an ``i``, a full stop -- shrinks under the three-nib
    floor, that is the ink filling the counter, and the companion is dropped and
    counted rather than emitted as a blot. If *every* companion of a component
    collapses, the component is emitted unweighted so no copy can vanish.
    """

    if isinstance(stroke_count, bool) or not isinstance(stroke_count, int):
        raise MapPlotterError("Text weight must be an integer stroke count.")
    if not 1 <= stroke_count <= MAX_PARALLEL_STROKES:
        raise MapPlotterError(
            f"Text weight must be between 1 and {MAX_PARALLEL_STROKES} strokes; "
            f"{stroke_count} was requested."
        )
    if nib_mm <= 0:
        raise MapPlotterError("Text weight needs a positive nib width.")
    if stroke_count == 1:
        return WeightedText(
            strokes=strokes,
            stroke_count=1,
            pitch_mm=0.0,
            plotted_width_mm=nib_mm,
            dropped_companions=0,
            filled_components=0,
        )

    pitch_mm = nib_mm * pitch_ratio
    positions = offset_positions(stroke_count, pitch_mm)
    minimum_length_mm = 3 * nib_mm
    offsets: list[LineString] = []
    dropped = 0
    filled = 0
    for stroke in strokes:
        if len(stroke) < 2:
            continue
        centre = LineString(stroke)
        component: list[LineString] = []
        for distance in positions:
            if abs(distance) <= 1e-9:
                component.append(centre)
                continue
            try:
                shifted = centre.offset_curve(
                    distance,
                    quad_segs=4,
                    join_style="mitre",
                    mitre_limit=OFFSET_MITRE_LIMIT,
                )
            except GEOSException as exc:  # pragma: no cover - GEOS edge case
                raise MapPlotterError(
                    "A glyph stroke could not be offset for bold lettering: "
                    f"{exc}. Reduce the weight or the cap height."
                ) from exc
            if not shifted.is_simple:
                shifted = unary_union(shifted)
            for part in _line_parts(shifted):
                if part.length + 1e-9 < minimum_length_mm:
                    dropped += 1
                    continue
                component.append(part)
        if not component:
            filled += 1
            component.append(centre)
        offsets.extend(component)

    if not offsets:
        return WeightedText([], stroke_count, pitch_mm, nib_mm, dropped, filled)
    # Join, but do not node. ``unary_union`` across the whole family would trim
    # the overlap where two independently offset strokes of a glyph meet, but it
    # also splits every place two offsets merely cross: measured on a 7 mm
    # title that shatters 28 pen paths into 112, 44 of them under the three-nib
    # floor. ``linemerge`` only concatenates lines that already share an
    # endpoint, so the worst case is a few tenths of a millimetre of re-inked
    # overlap inside a stem junction -- invisible, and far cheaper than a
    # fragmented plate.
    merged = linemerge(MultiLineString(offsets)) if len(offsets) > 1 else offsets[0]
    return WeightedText(
        strokes=[
            _points(line) for line in _line_parts(merged) if len(line.coords) >= 2
        ],
        stroke_count=stroke_count,
        pitch_mm=pitch_mm,
        plotted_width_mm=weighted_mark_width_mm(
            nib_mm=nib_mm, stroke_count=stroke_count, pitch_ratio=pitch_ratio
        ),
        dropped_companions=dropped,
        filled_components=filled,
    )


def weighted_glyph_strokes(
    strokes: list[Stroke],
    *,
    stroke_count: int,
    nib_mm: float,
    pitch_ratio: float = DEFAULT_OFFSET_PITCH_RATIO,
) -> list[Stroke]:
    """Convenience wrapper for callers that only need the geometry."""

    return weight_text(
        strokes,
        stroke_count=stroke_count,
        nib_mm=nib_mm,
        pitch_ratio=pitch_ratio,
    ).strokes
