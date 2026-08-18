from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import ceil, cos, floor, isfinite, radians, sin
from typing import Mapping, TypeAlias
from unicodedata import combining, normalize

from .models import MapPlotterError


Point: TypeAlias = tuple[float, float]
Stroke: TypeAlias = list[Point]


STROKE_FONT_CONTRACT_SCHEMA_VERSION = 3
STROKE_FONT_ID = "plotter-grid-v3"
TEXT_NORMALISATION_POLICY_ID = "nfkd-latin-transliteration-v1"

# These metrics are part of the font data, not renderer preferences. Keep the
# drawing and width functions below on the same constants so their provenance
# digest describes the geometry that the plotter actually emits.
FONT_CELL_WIDTH_UNITS = 4.0
FONT_CELL_HEIGHT_UNITS = 6.0
SPACE_ADVANCE_UNITS = 3.0

# v3 is proportional. Every glyph carries its own advance, measured from its own
# ink, and adjacent pairs are pulled together until their closest approach
# reaches the same gap two flat-sided glyphs would have. Both tables are
# *computed* from the glyph geometry below -- there is no hand-kerned data to
# maintain, and no way for the tables to disagree with the drawn strokes.
SIDEBEARING_UNITS = 0.5
TARGET_PAIR_GAP_UNITS = 2 * SIDEBEARING_UNITS
MAX_KERN_UNITS = 1.0
MIN_KERN_UNITS = 0.05
KERN_BAND_UNITS = 0.25
KERN_SAMPLE_UNITS = 0.125

_STROKE_FONT_METRICS: dict[str, float] = {
    "cell_width_units": FONT_CELL_WIDTH_UNITS,
    "cell_height_units": FONT_CELL_HEIGHT_UNITS,
    "space_advance_units": SPACE_ADVANCE_UNITS,
    "sidebearing_units": SIDEBEARING_UNITS,
    "target_pair_gap_units": TARGET_PAIR_GAP_UNITS,
    "max_kern_units": MAX_KERN_UNITS,
    "min_kern_units": MIN_KERN_UNITS,
    "kern_band_units": KERN_BAND_UNITS,
    "kern_sample_units": KERN_SAMPLE_UNITS,
}


# A compact, dependency-free single-line alphabet. Coordinates use a 4 by 6
# grid and are transformed into millimetres by stroke_text(). Plotters follow
# each stroke once instead of tracing the inner and outer edges of a screen font.
GLYPHS: dict[str, list[Stroke]] = {
    "A": [[(0, 6), (0, 2), (2, 0), (4, 2), (4, 6)], [(0, 3), (4, 3)]],
    "B": [
        [(0, 6), (0, 0), (3, 0), (4, 1), (4, 2), (3, 3), (0, 3)],
        [(3, 3), (4, 4), (4, 5), (3, 6), (0, 6)],
    ],
    "C": [[(4, 1), (3, 0), (1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5)]],
    "D": [[(0, 6), (0, 0), (2.5, 0), (4, 1.5), (4, 4.5), (2.5, 6), (0, 6)]],
    "E": [[(4, 0), (0, 0), (0, 6), (4, 6)], [(0, 3), (3, 3)]],
    "F": [[(0, 6), (0, 0), (4, 0)], [(0, 3), (3, 3)]],
    "G": [
        [(4, 1), (3, 0), (1, 0), (0, 1), (0, 5), (1, 6), (4, 6), (4, 3.5), (2.5, 3.5)]
    ],
    "H": [[(0, 0), (0, 6)], [(4, 0), (4, 6)], [(0, 3), (4, 3)]],
    "I": [[(0.5, 0), (3.5, 0)], [(2, 0), (2, 6)], [(0.5, 6), (3.5, 6)]],
    "J": [[(0, 4.5), (0.5, 6), (2.5, 6), (4, 4.5), (4, 0)]],
    "K": [[(0, 0), (0, 6)], [(4, 0), (0, 3), (4, 6)]],
    "L": [[(0, 0), (0, 6), (4, 6)]],
    "M": [[(0, 6), (0, 0), (2, 3), (4, 0), (4, 6)]],
    "N": [[(0, 6), (0, 0), (4, 6), (4, 0)]],
    "O": [[(1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0)]],
    "P": [[(0, 6), (0, 0), (3, 0), (4, 1), (4, 2), (3, 3), (0, 3)]],
    "Q": [
        [(1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0)],
        [(2.5, 4.5), (4.5, 6.5)],
    ],
    "R": [[(0, 6), (0, 0), (3, 0), (4, 1), (4, 2), (3, 3), (0, 3)], [(2.5, 3), (4, 6)]],
    "S": [
        [
            (4, 1),
            (3, 0),
            (1, 0),
            (0, 1),
            (0, 2),
            (1, 3),
            (3, 3),
            (4, 4),
            (4, 5),
            (3, 6),
            (1, 6),
            (0, 5),
        ]
    ],
    "T": [[(0, 0), (4, 0)], [(2, 0), (2, 6)]],
    "U": [[(0, 0), (0, 5), (1, 6), (3, 6), (4, 5), (4, 0)]],
    "V": [[(0, 0), (2, 6), (4, 0)]],
    "W": [[(0, 0), (0.75, 6), (2, 3), (3.25, 6), (4, 0)]],
    "X": [[(0, 0), (4, 6)], [(4, 0), (0, 6)]],
    "Y": [[(0, 0), (2, 3), (4, 0)], [(2, 3), (2, 6)]],
    "Z": [[(0, 0), (4, 0), (0, 6), (4, 6)]],
    "!": [
        [(2, 0), (2, 4.25)],
        [(2, 5.1), (2.45, 5.55), (2, 6), (1.55, 5.55), (2, 5.1)],
    ],
    '"': [[(1, 0), (1, 2.5)], [(3, 0), (3, 2.5)]],
    "#": [
        [(1.25, 0), (0.75, 6)],
        [(3.25, 0), (2.75, 6)],
        [(0, 2), (4, 2)],
        [(0, 4), (4, 4)],
    ],
    "$": [
        [
            (4, 1),
            (3, 0.5),
            (1, 0.5),
            (0, 1.5),
            (1, 3),
            (3, 3),
            (4, 4.5),
            (3, 5.5),
            (1, 5.5),
            (0, 5),
        ],
        [(2, 0), (2, 6)],
    ],
    "%": [
        [(0, 6), (4, 0)],
        [(0.5, 0), (1.5, 0), (1.5, 1.5), (0.5, 1.5), (0.5, 0)],
        [(2.5, 4.5), (3.5, 4.5), (3.5, 6), (2.5, 6), (2.5, 4.5)],
    ],
    "&": [
        [(4, 6), (1, 2), (1, 1), (2, 0), (3, 1), (3, 2), (0, 5), (1, 6), (2, 6), (4, 4)]
    ],
    "'": [[(2, 0), (2, 2.5)]],
    "(": [[(3, 0), (1.5, 1), (1, 3), (1.5, 5), (3, 6)]],
    ")": [[(1, 0), (2.5, 1), (3, 3), (2.5, 5), (1, 6)]],
    "*": [[(2, 1), (2, 5)], [(0.5, 2), (3.5, 4)], [(3.5, 2), (0.5, 4)]],
    "+": [[(2, 1), (2, 5)], [(0, 3), (4, 3)]],
    ",": [[(2, 5.6), (2.45, 5.15), (2, 4.7), (1.55, 5.15), (2, 5.6), (1.2, 6)]],
    ":": [
        [(2, 1.55), (2.45, 2), (2, 2.45), (1.55, 2), (2, 1.55)],
        [(2, 4.55), (2.45, 5), (2, 5.45), (1.55, 5), (2, 4.55)],
    ],
    ";": [
        [(2, 1.55), (2.45, 2), (2, 2.45), (1.55, 2), (2, 1.55)],
        [(2, 5.6), (2.45, 5.15), (2, 4.7), (1.55, 5.15), (2, 5.6), (1.2, 6)],
    ],
    "<": [[(3.5, 1), (0.5, 3), (3.5, 5)]],
    "=": [[(0.5, 2), (3.5, 2)], [(0.5, 4), (3.5, 4)]],
    ">": [[(0.5, 1), (3.5, 3), (0.5, 5)]],
    "?": [
        [(0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (2, 3), (2, 4)],
        [(2, 5.1), (2.45, 5.55), (2, 6), (1.55, 5.55), (2, 5.1)],
    ],
    "@": [
        [
            (3, 5),
            (1, 5),
            (0, 4),
            (0, 1),
            (1, 0),
            (3, 0),
            (4, 1),
            (4, 4),
            (3, 4),
            (3, 2),
            (2.5, 1.5),
            (1.5, 1.5),
            (1, 2),
            (1, 3.5),
            (1.5, 4),
            (3, 4),
        ]
    ],
    "[": [[(3, 0), (1, 0), (1, 6), (3, 6)]],
    "\\": [[(0, 0), (4, 6)]],
    "]": [[(1, 0), (3, 0), (3, 6), (1, 6)]],
    "^": [[(0.5, 2), (2, 0), (3.5, 2)]],
    "_": [[(0, 6), (4, 6)]],
    "`": [[(1, 0), (3, 2)]],
    "/": [[(0, 6), (4, 0)]],
    ".": [[(2, 5.1), (2.45, 5.55), (2, 6), (1.55, 5.55), (2, 5.1)]],
    "-": [[(0.5, 3), (3.5, 3)]],
    "0": [
        [(1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0)],
        [(0.8, 5.2), (3.2, 0.8)],
    ],
    "1": [[(1, 1), (2, 0), (2, 6)], [(0.5, 6), (3.5, 6)]],
    "2": [[(0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (0, 6), (4, 6)]],
    "3": [
        [(0, 0), (3, 0), (4, 1), (4, 2), (3, 3), (1, 3)],
        [(3, 3), (4, 4), (4, 5), (3, 6), (0, 6)],
    ],
    "4": [[(3, 6), (3, 0), (0, 4), (4, 4)]],
    "5": [[(4, 0), (0, 0), (0, 3), (3, 3), (4, 4), (4, 5), (3, 6), (0, 6)]],
    "6": [
        [
            (4, 1),
            (3, 0),
            (1, 0),
            (0, 1),
            (0, 5),
            (1, 6),
            (3, 6),
            (4, 5),
            (4, 4),
            (3, 3),
            (0, 3),
        ]
    ],
    "7": [[(0, 0), (4, 0), (1, 6)]],
    "8": [
        [(1, 0), (3, 0), (4, 1), (4, 2), (3, 3), (1, 3), (0, 2), (0, 1), (1, 0)],
        [(1, 3), (0, 4), (0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3)],
    ],
    "9": [
        [
            (4, 3),
            (1, 3),
            (0, 2),
            (0, 1),
            (1, 0),
            (3, 0),
            (4, 1),
            (4, 5),
            (3, 6),
            (1, 6),
            (0, 5),
        ]
    ],
    "a": [[(4, 6), (4, 2), (1, 2), (0, 3), (0, 5), (1, 6), (4, 6)], [(0, 4), (4, 4)]],
    "b": [
        [(0, 0), (0, 6)],
        [(0, 3), (1, 2), (3, 2), (4, 3), (4, 5), (3, 6), (1, 6), (0, 5)],
    ],
    "c": [[(4, 3), (3, 2), (1, 2), (0, 3), (0, 5), (1, 6), (3, 6), (4, 5)]],
    "d": [
        [(4, 0), (4, 6)],
        [(4, 3), (3, 2), (1, 2), (0, 3), (0, 5), (1, 6), (3, 6), (4, 5)],
    ],
    "e": [
        [(0, 4), (4, 4), (4, 3), (3, 2), (1, 2), (0, 3), (0, 5), (1, 6), (3, 6), (4, 5)]
    ],
    "f": [[(3.5, 0), (2.5, 0), (2, 1), (2, 6)], [(0.5, 2.5), (3.5, 2.5)]],
    "g": [
        [(4, 2), (1, 2), (0, 3), (0, 4.5), (1, 5), (4, 5)],
        [(4, 2), (4, 5.5), (3, 6), (1, 6)],
    ],
    "h": [[(0, 0), (0, 6)], [(0, 3), (1, 2), (3, 2), (4, 3), (4, 6)]],
    "i": [
        [(2, 2), (2, 6)],
        [(2, 0.2), (2.45, 0.65), (2, 1.1), (1.55, 0.65), (2, 0.2)],
    ],
    "j": [
        [(2.5, 2), (2.5, 5), (2, 6), (0.75, 6)],
        [(2.5, 0.2), (2.95, 0.65), (2.5, 1.1), (2.05, 0.65), (2.5, 0.2)],
    ],
    "k": [[(0, 0), (0, 6)], [(4, 2), (0, 4), (4, 6)]],
    "l": [[(1.5, 0), (2.5, 0), (2.5, 5), (3.5, 6)]],
    "m": [[(0, 6), (0, 2), (2, 4), (4, 2), (4, 6)]],
    "n": [[(0, 6), (0, 2), (1, 2), (4, 5), (4, 6)]],
    "o": [[(1, 2), (3, 2), (4, 3), (4, 5), (3, 6), (1, 6), (0, 5), (0, 3), (1, 2)]],
    "p": [[(0, 6), (0, 2), (3, 2), (4, 3), (4, 4), (3, 5), (0, 5)]],
    "q": [
        [(4, 6), (4, 2), (1, 2), (0, 3), (0, 5), (1, 6), (4, 6)],
        [(1.5, 4), (4, 6)],
    ],
    "r": [[(0, 6), (0, 2), (1, 2), (2, 3), (4, 2)]],
    "s": [
        [(4, 3), (3, 2), (1, 2), (0, 3), (1, 4), (3, 4), (4, 5), (3, 6), (1, 6), (0, 5)]
    ],
    "t": [[(2, 0), (2, 5), (3, 6), (4, 5)], [(0.5, 2), (3.5, 2)]],
    "u": [[(0, 2), (0, 5), (1, 6), (3, 6), (4, 5), (4, 2)]],
    "v": [[(0, 2), (2, 6), (4, 2)]],
    "w": [[(0, 2), (0.8, 6), (2, 4), (3.2, 6), (4, 2)]],
    "x": [[(0, 2), (4, 6)], [(4, 2), (0, 6)]],
    "y": [[(0, 2), (2, 5), (4, 2)], [(2, 4.5), (0, 6)]],
    "z": [[(0, 2), (4, 2), (0, 6), (4, 6)]],
    "{": [[(3, 0), (2, 0.5), (2, 2.25), (1, 3), (2, 3.75), (2, 5.5), (3, 6)]],
    "|": [[(2, 0), (2, 6)]],
    "}": [[(1, 0), (2, 0.5), (2, 2.25), (3, 3), (2, 3.75), (2, 5.5), (1, 6)]],
    "~": [[(0, 3.5), (1, 2.5), (3, 3.5), (4, 2.5)]],
    "°": [
        [
            (1.5, 0),
            (2.5, 0),
            (3, 0.5),
            (3, 1.5),
            (2.5, 2),
            (1.5, 2),
            (1, 1.5),
            (1, 0.5),
            (1.5, 0),
        ]
    ],
    "©": [
        [(1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0)],
        [
            (3, 2),
            (2.5, 1.5),
            (1.5, 1.5),
            (1, 2),
            (1, 4),
            (1.5, 4.5),
            (2.5, 4.5),
            (3, 4),
        ],
    ],
}


def _glyph_ink_bounds(strokes: list[Stroke]) -> tuple[float, float]:
    xs = [x for stroke in strokes for x, _ in stroke]
    return min(xs), max(xs)


@dataclass(frozen=True)
class GlyphMetric:
    """One glyph's proportional metrics, derived from its own geometry."""

    #: Horizontal shift applied to the drawn strokes so the ink starts one
    #: sidebearing after the pen cursor.
    offset_units: float
    #: Distance the cursor travels after drawing this glyph.
    advance_units: float
    #: Width of the ink itself, sidebearings excluded.
    ink_width_units: float


def _build_glyph_metrics(
    glyphs: Mapping[str, list[Stroke]],
) -> dict[str, GlyphMetric]:
    metrics: dict[str, GlyphMetric] = {}
    for character, strokes in glyphs.items():
        ink_left, ink_right = _glyph_ink_bounds(strokes)
        ink_width = ink_right - ink_left
        metrics[character] = GlyphMetric(
            offset_units=SIDEBEARING_UNITS - ink_left,
            advance_units=ink_width + 2 * SIDEBEARING_UNITS,
            ink_width_units=ink_width,
        )
    return metrics


def _band_profiles(
    strokes: list[Stroke], offset_units: float
) -> dict[int, tuple[float, float]]:
    """Sample a glyph's left and right silhouette, one entry per y band."""

    profile: dict[int, tuple[float, float]] = {}

    def record(x: float, y: float) -> None:
        band = int(floor(y / KERN_BAND_UNITS))
        shifted = x + offset_units
        current = profile.get(band)
        if current is None:
            profile[band] = (shifted, shifted)
        else:
            profile[band] = (min(current[0], shifted), max(current[1], shifted))

    for stroke in strokes:
        for (x0, y0), (x1, y1) in zip(stroke, stroke[1:]):
            span = max(abs(x1 - x0), abs(y1 - y0))
            steps = max(1, int(ceil(span / KERN_SAMPLE_UNITS)))
            for step in range(steps + 1):
                ratio = step / steps
                record(x0 + (x1 - x0) * ratio, y0 + (y1 - y0) * ratio)
    return profile


def _build_kerning(
    glyphs: Mapping[str, list[Stroke]],
    metrics: Mapping[str, GlyphMetric],
) -> dict[tuple[str, str], float]:
    """Pull every pair together until its closest approach is the target gap.

    The gap is measured band by band down the glyph, so ``AV`` closes on the
    diagonals that actually approach each other while ``AB`` stays open. Pairs
    that never share a band are left alone -- no ink can collide there.
    """

    profiles = {
        character: _band_profiles(strokes, metrics[character].offset_units)
        for character, strokes in glyphs.items()
    }
    kerning: dict[tuple[str, str], float] = {}
    for left, left_profile in profiles.items():
        advance = metrics[left].advance_units
        for right, right_profile in profiles.items():
            shared = left_profile.keys() & right_profile.keys()
            if not shared:
                continue
            gap = min(
                advance + right_profile[band][0] - left_profile[band][1]
                for band in shared
            )
            pull = min(gap - TARGET_PAIR_GAP_UNITS, MAX_KERN_UNITS)
            if pull >= MIN_KERN_UNITS:
                kerning[(left, right)] = -round(pull, 6)
    return kerning


GLYPH_METRICS: dict[str, GlyphMetric] = _build_glyph_metrics(GLYPHS)
KERNING: dict[tuple[str, str], float] = _build_kerning(GLYPHS, GLYPH_METRICS)


# Normalisation is part of the drawable font contract: changing a replacement
# changes the strokes produced for otherwise identical source copy.  Keep this
# as a string-keyed mapping so its canonical digest is independent of the
# integer-keyed implementation detail returned by ``str.maketrans``.
_TEXT_REPLACEMENT_MAP: dict[str, str] = {
    "\u00a0": " ",
    "\u00ad": "-",
    "\u00ab": '"',
    "\u00bb": '"',
    "\u00c6": "AE",
    "\u00d0": "D",
    "\u00d8": "O",
    "\u00de": "TH",
    "\u00df": "ss",
    "\u00e6": "ae",
    "\u00f0": "d",
    "\u00f8": "o",
    "\u00fe": "th",
    "\u0110": "D",
    "\u0111": "d",
    "\u0126": "H",
    "\u0127": "h",
    "\u0131": "i",
    "\u0138": "k",
    "\u0141": "L",
    "\u0142": "l",
    "\u014a": "N",
    "\u014b": "n",
    "\u0152": "OE",
    "\u0153": "oe",
    "\u0166": "T",
    "\u0167": "t",
    "\u02bc": "'",
    "\u1680": " ",
    "\u2000": " ",
    "\u2001": " ",
    "\u2002": " ",
    "\u2003": " ",
    "\u2004": " ",
    "\u2005": " ",
    "\u2006": " ",
    "\u2007": " ",
    "\u2008": " ",
    "\u2009": " ",
    "\u200a": " ",
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u2026": "...",
    "\u202f": " ",
    "\u2032": "'",
    "\u2033": '"',
    "\u205f": " ",
    "\u2212": "-",
    "\u3000": " ",
    "\u1e9e": "SS",
}


def _canonical_number(value: float) -> str:
    """Return one stable numeric representation for font provenance."""

    number = float(value)
    if not isfinite(number):
        raise ValueError("Stroke-font provenance cannot contain non-finite numbers.")
    if number == 0:
        number = 0.0
    return format(number, ".15g")


def _stroke_font_data_sha256(
    glyphs: Mapping[str, list[Stroke]],
    metrics: Mapping[str, float],
    replacements: Mapping[str, str] | None = None,
) -> str:
    """Hash glyphs, metrics, and the copy-to-glyph normalisation policy."""

    canonical_glyphs = []
    if any(len(character) != 1 for character in glyphs):
        raise ValueError("Stroke-font glyph keys must be single characters.")
    for character in sorted(glyphs, key=ord):
        canonical_glyphs.append(
            {
                "codepoint": ord(character),
                "strokes": [
                    [[_canonical_number(x), _canonical_number(y)] for x, y in stroke]
                    for stroke in glyphs[character]
                ],
            }
        )
    replacements = _TEXT_REPLACEMENT_MAP if replacements is None else replacements
    if any(len(character) != 1 for character in replacements):
        raise ValueError("Stroke-font replacement keys must be single characters.")
    if any(not isinstance(value, str) for value in replacements.values()):
        raise ValueError("Stroke-font replacement values must be strings.")
    glyph_metrics = _build_glyph_metrics(glyphs)
    kerning = _build_kerning(glyphs, glyph_metrics)
    payload = {
        "schema_version": STROKE_FONT_CONTRACT_SCHEMA_VERSION,
        "normalisation": {
            "policy_id": TEXT_NORMALISATION_POLICY_ID,
            "unicode_form": "NFKD",
            "combining_mark_policy": "drop",
            "replacements": [
                {"codepoint": ord(character), "replacement": replacements[character]}
                for character in sorted(replacements, key=ord)
            ],
        },
        "metrics": {
            key: _canonical_number(value) for key, value in sorted(metrics.items())
        },
        # Advances and kerning are derived from the glyphs above, but they are
        # what actually decides where the pen goes, so the provenance digest
        # names them explicitly rather than trusting the derivation.
        "advances": [
            {
                "codepoint": ord(character),
                "offset": _canonical_number(glyph_metrics[character].offset_units),
                "advance": _canonical_number(glyph_metrics[character].advance_units),
            }
            for character in sorted(glyphs, key=ord)
        ],
        "kerning": [
            {
                "left": ord(left),
                "right": ord(right),
                "kern": _canonical_number(kerning[(left, right)]),
            }
            for left, right in sorted(kerning, key=lambda pair: (ord(pair[0]), ord(pair[1])))
        ],
        "glyphs": canonical_glyphs,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


STROKE_FONT_SHA256 = _stroke_font_data_sha256(GLYPHS, _STROKE_FONT_METRICS)


def stroke_font_contract() -> dict[str, int | str]:
    """Return stable identifiers for the built-in stroke-font rendering contract."""

    return {
        "schema_version": STROKE_FONT_CONTRACT_SCHEMA_VERSION,
        "font_id": STROKE_FONT_ID,
        "normalisation_policy_id": TEXT_NORMALISATION_POLICY_ID,
        "sha256": STROKE_FONT_SHA256,
    }


_TEXT_REPLACEMENTS = str.maketrans(_TEXT_REPLACEMENT_MAP)


def normalise_text(text: str) -> str:
    """Convert common Unicode typography and Latin letters to drawable glyphs."""

    decomposed = normalize("NFKD", text.translate(_TEXT_REPLACEMENTS))
    return "".join(character for character in decomposed if not combining(character))


def _drawable_text(text: str) -> str:
    drawable = normalise_text(text)
    unsupported = tuple(
        dict.fromkeys(
            character
            for character in drawable
            if character != " " and character not in GLYPHS
        )
    )
    if unsupported:
        description = ", ".join(
            f"{character!r} (U+{ord(character):04X})" for character in unsupported
        )
        raise MapPlotterError(
            f"Plotter font cannot draw unsupported character(s): {description}."
        )
    return drawable


def _advance_units(character: str) -> float:
    if character == " ":
        return SPACE_ADVANCE_UNITS
    return GLYPH_METRICS[character].advance_units


def kern_units(left: str, right: str) -> float:
    """Return the (negative) pull applied between two adjacent glyphs."""

    return KERNING.get((left, right), 0.0)


def _advance_run_units(drawable: str) -> float:
    """Total cursor travel, kerning included, for one already-drawable string."""

    total = 0.0
    previous = ""
    for character in drawable:
        if previous:
            total += kern_units(previous, character)
        total += _advance_units(character)
        previous = character
    return total


def _text_width_units(drawable: str) -> float:
    """Ink width: the cursor run less the leading and trailing sidebearings."""

    if not drawable:
        return 0.0
    return max(_advance_run_units(drawable) - 2 * SIDEBEARING_UNITS, 0.0)


def text_width_units(text: str) -> float:
    return _text_width_units(_drawable_text(text))


def text_width_mm(text: str, *, cap_height_mm: float) -> float:
    """Width of ``text`` set at ``cap_height_mm``, in page millimetres."""

    return text_width_units(text) * cap_height_mm / FONT_CELL_HEIGHT_UNITS


def cap_height_for_width_mm(text: str, *, width_mm: float) -> float:
    """The largest cap height at which ``text`` still fits ``width_mm``."""

    units = text_width_units(text)
    if units <= 0:
        raise MapPlotterError("Cannot fit a cap height to text with no drawable ink.")
    return width_mm * FONT_CELL_HEIGHT_UNITS / units


def stroke_text(
    text: str,
    *,
    x_mm: float,
    y_mm: float,
    height_mm: float,
    angle_deg: float = 0.0,
    anchor: str = "start",
) -> list[Stroke]:
    """Return single-line glyph strokes, optionally anchored and rotated.

    ``x_mm``/``y_mm`` identify the selected horizontal anchor at the top of the
    nominal six-unit font cell. Positive angles follow SVG page coordinates,
    appearing clockwise because page y coordinates increase downwards.
    """

    if anchor not in {"start", "middle", "end"}:
        raise MapPlotterError("Text anchor must be start, middle, or end.")
    if isinstance(angle_deg, bool) or not isinstance(angle_deg, (int, float)):
        raise MapPlotterError("Text angle must be a finite number of degrees.")
    if not isfinite(angle_deg):
        raise MapPlotterError("Text angle must be a finite number of degrees.")

    drawable = _drawable_text(text)
    scale = height_mm / FONT_CELL_HEIGHT_UNITS
    width_mm = _text_width_units(drawable) * scale
    anchor_offset = {"start": 0.0, "middle": width_mm / 2, "end": width_mm}[anchor]
    # The cursor sits one sidebearing before the first glyph's ink, so a
    # ``start`` anchor still puts ink exactly on ``x_mm``.
    cursor = x_mm - anchor_offset - SIDEBEARING_UNITS * scale
    result: list[Stroke] = []
    previous = ""
    for character in drawable:
        if previous:
            cursor += kern_units(previous, character) * scale
        previous = character
        if character == " ":
            cursor += SPACE_ADVANCE_UNITS * scale
            continue
        metric = GLYPH_METRICS[character]
        origin = cursor + metric.offset_units * scale
        for stroke in GLYPHS[character]:
            result.append([(origin + x * scale, y_mm + y * scale) for x, y in stroke])
        cursor += metric.advance_units * scale

    if angle_deg % 360 == 0:
        return result
    angle = radians(angle_deg)
    cosine = cos(angle)
    sine = sin(angle)
    return [
        [
            (
                x_mm + (x - x_mm) * cosine - (y - y_mm) * sine,
                y_mm + (x - x_mm) * sine + (y - y_mm) * cosine,
            )
            for x, y in stroke
        ]
        for stroke in result
    ]
