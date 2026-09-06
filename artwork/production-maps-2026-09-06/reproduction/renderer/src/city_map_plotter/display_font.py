"""Classic proportional display lettering for plotter memorabilia plates."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib.resources import files
from math import isfinite
from typing import Any

from .models import MapPlotterError
from .stroke_font import (
    ASCII_TRANSLITERATION_POLICY_ID,
    ASCII_TRANSLITERATION_SHA256,
    transliterate_text,
)


DISPLAY_FONT_RESOURCE = "hershey-serif-medium.json"
DISPLAY_FONT_ID = "hershey-serif-medium-ascii-v1"


@lru_cache(maxsize=1)
def _font() -> tuple[dict[str, Any], str]:
    resource = files("city_map_plotter").joinpath("data", DISPLAY_FONT_RESOURCE)
    try:
        payload = resource.read_bytes()
        document = json.loads(payload)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise MapPlotterError(
            f"Could not load display font data/{DISPLAY_FONT_RESOURCE}: {exc}"
        ) from exc
    if not isinstance(document, dict) or document.get("id") != DISPLAY_FONT_ID:
        raise MapPlotterError("The packaged display-font contract is malformed.")
    return document, hashlib.sha256(payload).hexdigest()


def display_font_contract() -> dict[str, Any]:
    document, digest = _font()
    source = document.get("source")
    return {
        "schema_version": document.get("schema_version"),
        "font_id": DISPLAY_FONT_ID,
        "sha256": digest,
        "transliteration": {
            "policy_id": ASCII_TRANSLITERATION_POLICY_ID,
            "sha256": ASCII_TRANSLITERATION_SHA256,
        },
        "source": dict(source) if isinstance(source, dict) else None,
    }


def _drawable(text: str) -> str:
    value = transliterate_text(text)
    glyphs = _font()[0]["glyphs"]
    unsupported = tuple(
        dict.fromkeys(character for character in value if character not in glyphs)
    )
    if unsupported:
        labels = ", ".join(
            f"{character!r} (U+{ord(character):04X})" for character in unsupported
        )
        raise MapPlotterError(
            f"Hershey display font cannot draw unsupported character(s): {labels}."
        )
    return value


def display_text_width_units(text: str) -> float:
    document, _ = _font()
    glyphs = document["glyphs"]
    return sum(float(glyphs[character]["advance"]) for character in _drawable(text))


def display_text_width_mm(text: str, *, height_mm: float) -> float:
    document, _ = _font()
    metrics = document["metrics"]
    capital_height = float(metrics["capital_top"]) - float(metrics["baseline"])
    return display_text_width_units(text) * float(height_mm) / capital_height


def display_text(
    text: str,
    *,
    x_mm: float,
    y_mm: float,
    height_mm: float,
    anchor: str = "start",
) -> list[list[tuple[float, float]]]:
    """Return proportional Hershey Serif paths in page millimetres.

    ``x_mm`` is the selected horizontal anchor and ``y_mm`` is the top of the
    capital box. The imported SVG-font y axis is inverted into SVG page space.
    """

    if anchor not in {"start", "middle", "end"}:
        raise MapPlotterError("Display-text anchor must be start, middle, or end.")
    if (
        isinstance(height_mm, bool)
        or not isinstance(height_mm, (int, float))
        or not isfinite(height_mm)
        or height_mm <= 0
    ):
        raise MapPlotterError("Display-text height must be a positive finite number.")
    document, _ = _font()
    metrics = document["metrics"]
    glyphs = document["glyphs"]
    drawable = _drawable(text)
    capital_top = float(metrics["capital_top"])
    baseline = float(metrics["baseline"])
    scale = float(height_mm) / (capital_top - baseline)
    width_mm = display_text_width_units(drawable) * scale
    cursor = x_mm - {"start": 0.0, "middle": width_mm / 2, "end": width_mm}[anchor]
    result: list[list[tuple[float, float]]] = []
    for character in drawable:
        glyph = glyphs[character]
        for stroke in glyph["strokes"]:
            points = [
                (
                    cursor + float(point[0]) * scale,
                    y_mm + (capital_top - float(point[1])) * scale,
                )
                for point in stroke
            ]
            if len(points) >= 2:
                result.append(points)
        cursor += float(glyph["advance"]) * scale
    return result
