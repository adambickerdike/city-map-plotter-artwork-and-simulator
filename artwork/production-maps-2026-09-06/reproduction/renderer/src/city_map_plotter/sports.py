"""Premium sports, route, circuit, river, sailing, and stadium plates.

The module is a domain compiler on the existing :mod:`niche_common` plate
engine.  It owns sports-specific validation and composition only; page zones,
type sizes, physical pens, SVG emission, previews, and manifests continue to be
provided by the application's established format and output systems.

Every hero line or venue component must carry a supplied source reference.
Wide marks are emitted as real parallel pen strokes, quantitative gaps remain
gaps, and official distances are kept distinct from measured track lengths.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date
import math
import re
from typing import Any, Iterable, Mapping, NoReturn, Sequence

from shapely.errors import GEOSException
from shapely.geometry import GeometryCollection, LineString, MultiLineString, Polygon, box
from shapely.geometry.base import BaseGeometry

from .models import MapPlotterError
from .niche_common import (
    ArtworkLayer,
    PENS_BY_ID,
    PlateArtwork,
    PlateContext,
    Rect,
    add_text,
    circle_stroke,
    context_for,
    cross_strokes,
    polyline_length_mm,
)
from .pens import ACTUAL_PEN_INVENTORY, PenWidthFit, fit_pen_width
from .sports_route import (
    PhysicalSimplification,
    QuantitativeSample,
    RouteProcessingResult,
    SportsSegment,
    extrema_preserving_downsample,
    process_route,
    simplify_physical_route,
    split_quantitative_runs,
)


Point = tuple[float, float]
Stroke = list[Point]

SPORTS_SCHEMA_VERSION = 1
SPORTS_SUBJECT_KIND = "sports-artwork"
SPORTS_ARTIFACT_KIND = "sports-and-venue-pen-artwork"
SPORTS_PENS = (
    "grey-0-25",
    "green-0-25",
    "green-0-4",
    "blue-0-25",
    "blue-0-4",
    "purple-0-25",
    "purple-0-4",
    "red-0-25",
    "red-0-4",
    "black-0-25",
    "black-0-4",
    "black-0-6",
    "black-1",
)

_STABLE_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_PROTECTED_ASSET_FIELDS = frozenset(
    {
        "logo",
        "logos",
        "crest",
        "crests",
        "league_mark",
        "sponsor_graphic",
        "kit_pattern",
        "protected_mark",
    }
)

ROUTE_GEOMETRY_STATUSES = frozenset(
    {
        "official-route",
        "official-supplied",
        "source-derived",
        "source-sampled",
        "supplied-track",
        "user-supplied",
        "verified-centreline",
        "verified-racing-line",
    }
)
VENUE_SOURCE_LEVELS = frozenset(
    {
        "accurate-plan",
        "footprint-and-pitch",
        "traced-reference",
        "verified-footprint-and-pitch",
    }
)
ROUTE_STYLES = frozenset(
    {
        "single-line",
        "parallel-dual",
        "ribbon-edges",
        "hatched-corridor",
        "variable-density",
        "highlighted-line",
    }
)
ARRANGEMENTS = frozenset({"grid", "timeline", "radial"})
SCALE_POLICIES = frozenset({"common", "independent-labelled"})


@dataclass(frozen=True, slots=True)
class SportsPreset:
    id: str
    label: str
    letter: str
    hero_kind: str
    route_style: str | None
    context_policy: str
    profile_policy: str
    description: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "id": self.id,
            "label": self.label,
            "letter": self.letter,
            "hero_kind": self.hero_kind,
            "route_style": self.route_style,
            "context_policy": self.context_policy,
            "profile_policy": self.profile_policy,
            "description": self.description,
        }


SPORTS_PRESETS: dict[str, SportsPreset] = {
    preset.id: preset
    for preset in (
        SportsPreset(
            "route-hero",
            "Route Hero",
            "A",
            "route",
            "highlighted-line",
            "sparse-local",
            "none",
            "Large factual route with restrained supplied context.",
        ),
        SportsPreset(
            "route-and-elevation",
            "Route and Elevation",
            "B",
            "route",
            "highlighted-line",
            "sparse-local",
            "elevation",
            "Route and source elevation share linked map/profile space.",
        ),
        SportsPreset(
            "personal-best",
            "Personal Best",
            "C",
            "route",
            "highlighted-line",
            "sparse-local",
            "performance-summary",
            "Route and supplied performance result, composed as artwork.",
        ),
        SportsPreset(
            "minimal-line",
            "Minimal Line",
            "D",
            "route",
            "single-line",
            "none",
            "none",
            "Fast-plot isolated route, river course, or circuit.",
        ),
        SportsPreset(
            "topographic-challenge",
            "Topographic Challenge",
            "E",
            "route",
            "highlighted-line",
            "source-contours",
            "none",
            "Route over supplied factual contours and climb evidence.",
        ),
        SportsPreset(
            "multi-discipline",
            "Multi-Discipline",
            "F",
            "route",
            "highlighted-line",
            "sparse-local",
            "discipline-summary",
            "Unified swim, bike, run, transition, or stage composition.",
        ),
        SportsPreset(
            "river-course",
            "River Course",
            "G",
            "route",
            "parallel-dual",
            "river",
            "optional",
            "Banks and bridges frame a verified watercourse hero.",
        ),
        SportsPreset(
            "sailing-chart",
            "Sailing Chart",
            "H",
            "route",
            "single-line",
            "coastal",
            "optional",
            "Supplied coastline, marks, headings, and race legs.",
        ),
        SportsPreset(
            "circuit-blueprint",
            "Circuit Blueprint",
            "I",
            "route",
            "ribbon-edges",
            "circuit-site",
            "optional-elevation",
            "Track outline, sectors, corners, pit lane, and factual notes.",
        ),
        SportsPreset(
            "race-telemetry",
            "Race Telemetry",
            "J",
            "route",
            "ribbon-edges",
            "minimal-site",
            "telemetry",
            "Route or circuit integrated with supplied measured traces.",
        ),
        SportsPreset(
            "stadium-architecture",
            "Stadium Architecture",
            "K",
            "venue",
            None,
            "venue",
            "none",
            "Supplied stadium plan or structure as layered linework.",
        ),
        SportsPreset(
            "matchday-memory",
            "Matchday Memory",
            "L",
            "venue",
            None,
            "venue",
            "occasion",
            "Venue plan with supplied match, seat, score, and dedication.",
        ),
        SportsPreset(
            "season-or-series",
            "Season or Series",
            "M",
            "series",
            None,
            "none",
            "collection",
            "Multiple factual routes, circuits, or venues in one system.",
        ),
        SportsPreset(
            "route-fingerprint",
            "Route Fingerprint",
            "N",
            "route",
            "single-line",
            "none",
            "fingerprint",
            "Original route with a data-faithful secondary signature.",
        ),
    )
}

PRESET_ALIASES = {
    **{preset.letter.casefold(): preset.id for preset in SPORTS_PRESETS.values()},
    **{preset.label.casefold(): preset.id for preset in SPORTS_PRESETS.values()},
    "season-series": "season-or-series",
    "season": "season-or-series",
}

DISCIPLINE_INKS = {
    "swim": "Blue",
    "open-water-swim": "Blue",
    "sailing": "Blue",
    "rowing": "Blue",
    "bike": "Purple",
    "cycling": "Purple",
    "motorsport": "Purple",
    "drive": "Purple",
    "run": "Red",
    "running": "Red",
    "walk": "Red",
    "hike": "Red",
    "transition": "Black",
}

PROFILE_INKS = {
    "elevation": "Green",
    "pace": "Purple",
    "speed": "Purple",
    "heart_rate": "Red",
    "power": "Purple",
    "cadence": "Blue",
    "stroke_rate": "Blue",
    "throttle": "Green",
    "brake": "Red",
    "gear": "Black",
    "position": "Black",
    "heading": "Blue",
    "turn_angle": "Purple",
}

DEFAULT_CHANNEL_UNITS = {
    "elevation": "m",
    "pace": "supplied pace unit",
    "speed": "supplied speed unit",
    "heart_rate": "bpm",
    "power": "W",
    "cadence": "rpm",
    "stroke_rate": "spm",
    "throttle": "%",
    "brake": "%",
    "gear": "gear",
    "position": "position",
    "heading": "deg",
    "turn_angle": "deg",
}


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(f"Invalid sports artwork data: {message}")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be non-empty text.")
    return value.strip()


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be a finite number.")
    result = float(value)
    if not math.isfinite(result):
        _fail(f"{label} must be a finite number.")
    return result


def _optional_number(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be a finite number or null.")
    result = float(value)
    return result if math.isfinite(result) else None


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object.")
    return value


def _array(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "a non-empty" if nonempty else "an"
        _fail(f"{label} must be {qualifier} array.")
    return value


def _identifier(value: Any, label: str) -> str:
    result = _text(value, label)
    if _STABLE_ID.fullmatch(result) is None:
        _fail(f"{label} must use lower-case letters, digits, and hyphens.")
    return result


def resolve_sports_preset(value: str) -> SportsPreset:
    key = _text(value, "preset").casefold()
    key = PRESET_ALIASES.get(key, key)
    try:
        return SPORTS_PRESETS[key]
    except KeyError as exc:
        _fail(
            f"unknown preset {value!r}; choose "
            + ", ".join(SPORTS_PRESETS)
            + "."
        )
        raise AssertionError from exc


def list_sports_presets() -> tuple[dict[str, str | None], ...]:
    return tuple(preset.as_dict() for preset in SPORTS_PRESETS.values())


def _reject_protected_assets(value: Any, path: str = "record") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in _PROTECTED_ASSET_FIELDS:
                _fail(
                    f"{path}.{key} is a protected-mark field. Use the existing "
                    "rights-cleared asset mechanism outside this generator."
                )
            _reject_protected_assets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_protected_assets(child, f"{path}[{index}]")


def _validate_sources(record: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    sources = _array(record.get("sources"), "sources", nonempty=True)
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, source_value in enumerate(sources):
        source = _object(source_value, f"sources[{index}]")
        source_id = _identifier(source.get("id"), f"sources[{index}].id")
        if source_id in seen:
            _fail(f"sources repeats id {source_id!r}.")
        seen.add(source_id)
        for key in ("publisher", "license", "attribution", "use"):
            _text(source.get(key), f"sources[{index}].{key}")
        validated.append(copy.deepcopy(source))
    return tuple(validated)


def _source_ids(sources: Sequence[Mapping[str, Any]]) -> set[str]:
    return {str(source["id"]) for source in sources}


def _validate_source_ref(value: Any, label: str, source_ids: set[str]) -> str:
    source_ref = _text(value, label)
    if source_ref not in source_ids:
        _fail(f"{label} references unknown source {source_ref!r}.")
    return source_ref


def _geometry_paths(value: Mapping[str, Any], label: str) -> list[list[list[float]]]:
    """Return source paths from the compact or GeoJSON-shaped geometry forms."""

    if "points" in value:
        points = _array(value["points"], f"{label}.points", nonempty=True)
        return [points]
    geometry = value.get("geometry")
    if isinstance(geometry, list):
        return [geometry]
    if not isinstance(geometry, dict):
        _fail(f"{label} needs points or geometry.")
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "LineString":
        return [_array(coordinates, f"{label}.geometry.coordinates", nonempty=True)]
    if kind == "MultiLineString":
        return [
            _array(path, f"{label}.geometry.coordinates[{index}]", nonempty=True)
            for index, path in enumerate(
                _array(coordinates, f"{label}.geometry.coordinates", nonempty=True)
            )
        ]
    if kind == "Polygon":
        return [
            _array(ring, f"{label}.geometry.coordinates[{index}]", nonempty=True)
            for index, ring in enumerate(
                _array(coordinates, f"{label}.geometry.coordinates", nonempty=True)
            )
        ]
    _fail(f"{label}.geometry type {kind!r} is unsupported.")


def _validate_context(record: Mapping[str, Any], source_ids: set[str]) -> None:
    context = record.get("context")
    if context is None:
        return
    context_value = _object(context, "context")
    features = _array(context_value.get("features", []), "context.features")
    for index, feature_value in enumerate(features):
        feature = _object(feature_value, f"context.features[{index}]")
        _identifier(feature.get("id"), f"context.features[{index}].id")
        kind = _text(feature.get("kind"), f"context.features[{index}].kind")
        _validate_source_ref(
            feature.get("source_ref"),
            f"context.features[{index}].source_ref",
            source_ids,
        )
        paths = _geometry_paths(feature, f"context.features[{index}]")
        if not paths or any(len(path) < 2 for path in paths):
            _fail(f"context.features[{index}] needs two-point source geometry.")
        if kind == "contour" and feature.get("elevation_m") is None:
            _fail(f"context.features[{index}] contour needs elevation_m.")


def _validate_profiles(record: Mapping[str, Any], source_ids: set[str]) -> None:
    profiles = _array(record.get("profiles", []), "profiles")
    for index, profile_value in enumerate(profiles):
        profile = _object(profile_value, f"profiles[{index}]")
        _identifier(profile.get("id"), f"profiles[{index}].id")
        _text(profile.get("channel"), f"profiles[{index}].channel")
        _text(profile.get("unit"), f"profiles[{index}].unit")
        _validate_source_ref(
            profile.get("source_ref"),
            f"profiles[{index}].source_ref",
            source_ids,
        )
        processing = profile.get("processing", "raw")
        if isinstance(processing, str):
            status = processing
            method = None
        elif isinstance(processing, dict):
            status = _text(
                processing.get("status"), f"profiles[{index}].processing.status"
            )
            method = processing.get("method")
        else:
            _fail(f"profiles[{index}].processing must be text or an object.")
        if status not in {"raw", "smoothed"}:
            _fail(f"profiles[{index}] processing must be raw or smoothed.")
        if status == "smoothed" and not isinstance(method, str):
            _fail(f"profiles[{index}] smoothed data must name its supplied method.")
        samples = _array(
            profile.get("samples"), f"profiles[{index}].samples", nonempty=True
        )
        previous = -math.inf
        valid = 0
        for sample_index, sample_value in enumerate(samples):
            if isinstance(sample_value, dict):
                distance = _number(
                    sample_value.get("distance_m"),
                    f"profiles[{index}].samples[{sample_index}].distance_m",
                )
                value = _optional_number(
                    sample_value.get("value"),
                    f"profiles[{index}].samples[{sample_index}].value",
                )
            elif isinstance(sample_value, list) and len(sample_value) == 2:
                distance = _number(
                    sample_value[0],
                    f"profiles[{index}].samples[{sample_index}][0]",
                )
                value = _optional_number(
                    sample_value[1],
                    f"profiles[{index}].samples[{sample_index}][1]",
                )
            else:
                _fail(
                    f"profiles[{index}].samples[{sample_index}] must be "
                    "[distance_m, value] or an object."
                )
            if distance < previous:
                _fail(f"profiles[{index}] distances must be monotonic.")
            previous = distance
            valid += int(value is not None)
        if valid < 2:
            _fail(f"profiles[{index}] needs at least two finite supplied values.")


def _validate_distance_metadata(
    record: Mapping[str, Any], source_ids: set[str]
) -> None:
    event = record.get("event") or {}
    route = record.get("route") or {}
    claims: list[tuple[str, Any, bool]] = []
    if isinstance(event, dict):
        if event.get("official_distance") is not None:
            claims.append(("event.official_distance", event["official_distance"], True))
        if event.get("distance") is not None:
            claims.append(("event.distance", event["distance"], False))
    if isinstance(route, dict) and route.get("official_distance") is not None:
        claims.append(("route.official_distance", route["official_distance"], True))
    if isinstance(event, dict) and event.get("discipline_distances") is not None:
        discipline_distances = event["discipline_distances"]
        if not isinstance(discipline_distances, dict) or not discipline_distances:
            _fail("event.discipline_distances must be a non-empty object.")
        for discipline, claim in discipline_distances.items():
            discipline_name = _text(
                discipline, "event.discipline_distances discipline"
            ).casefold()
            claims.append(
                (
                    f"event.discipline_distances.{discipline_name}",
                    claim,
                    True,
                )
            )
    for label, claim, authoritative in claims:
        if isinstance(claim, str):
            if authoritative:
                _fail(f"{label} must be an object with value, unit, and source_ref.")
            _text(claim, label)
            continue
        value = _object(claim, label)
        distance = _number(value.get("value"), f"{label}.value")
        if distance <= 0.0:
            _fail(f"{label}.value must be positive.")
        _text(value.get("unit"), f"{label}.unit")
        if authoritative or value.get("source_ref") is not None:
            _validate_source_ref(
                value.get("source_ref"), f"{label}.source_ref", source_ids
            )


def _validate_route_controls(
    route: Mapping[str, Any], source_ids: set[str]
) -> None:
    for name in ("start", "finish"):
        control = route.get(name)
        point: Any
        if control is None:
            continue
        if isinstance(control, (list, tuple)):
            point = control
        elif isinstance(control, dict):
            point = control.get("point", control.get("coordinates"))
            _validate_source_ref(
                control.get("source_ref"), f"route.{name}.source_ref", source_ids
            )
        else:
            _fail(f"route.{name} must be a point array or source-labelled object.")
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            _fail(f"route.{name} needs point [x, y].")
        _number(point[0], f"route.{name}.point[0]")
        _number(point[1], f"route.{name}.point[1]")


def _validate_venue(record: Mapping[str, Any], source_ids: set[str]) -> None:
    venue = _object(record.get("venue"), "venue")
    level = _text(venue.get("source_level"), "venue.source_level")
    if level not in VENUE_SOURCE_LEVELS:
        _fail(
            f"venue.source_level {level!r} is unsupported; choose "
            + ", ".join(sorted(VENUE_SOURCE_LEVELS))
            + "."
        )
    components = _array(venue.get("components"), "venue.components", nonempty=True)
    kinds: set[str] = set()
    for index, component_value in enumerate(components):
        component = _object(component_value, f"venue.components[{index}]")
        _identifier(component.get("id"), f"venue.components[{index}].id")
        kind = _text(component.get("kind"), f"venue.components[{index}].kind")
        kinds.add(kind)
        _validate_source_ref(
            component.get("source_ref"),
            f"venue.components[{index}].source_ref",
            source_ids,
        )
        paths = _geometry_paths(component, f"venue.components[{index}]")
        if any(len(path) < 2 for path in paths):
            _fail(f"venue.components[{index}] has degenerate geometry.")
    if not kinds.intersection({"envelope", "footprint", "facade"}):
        _fail("venue needs an envelope, footprint, or facade hero component.")
    if not kinds.intersection({"pitch", "playing-surface"}):
        _fail("stadium artwork needs supplied pitch or playing-surface geometry.")


def _route_has_channel(record: Mapping[str, Any], channel: str) -> bool:
    for profile in record.get("profiles", []) or []:
        if isinstance(profile, dict) and str(profile.get("channel")) == channel:
            return True
    route = record.get("route")
    if not isinstance(route, dict):
        return False
    for segment in route.get("segments", []) or []:
        if not isinstance(segment, dict):
            continue
        for point in segment.get("points", []) or []:
            if isinstance(point, dict):
                if channel == "elevation" and (
                    point.get("elevation_m") is not None
                    or (
                        isinstance(point.get("coordinates"), list)
                        and len(point["coordinates"]) >= 3
                        and point["coordinates"][2] is not None
                    )
                ):
                    return True
                channels = point.get("channels") or {}
                if isinstance(channels, dict) and channels.get(channel) is not None:
                    return True
                if point.get(channel) is not None:
                    return True
            elif channel == "elevation" and isinstance(point, list) and len(point) >= 3:
                return point[2] is not None
    return False


def _validate_series(record: Mapping[str, Any], source_ids: set[str]) -> None:
    series = _object(record.get("series"), "series")
    arrangement = str(series.get("arrangement", "grid"))
    if arrangement not in ARRANGEMENTS:
        _fail(f"series.arrangement must be one of {', '.join(sorted(ARRANGEMENTS))}.")
    scale_policy = str(series.get("scale_policy", "common"))
    if scale_policy not in SCALE_POLICIES:
        _fail(
            f"series.scale_policy must be one of {', '.join(sorted(SCALE_POLICIES))}."
        )
    items = _array(series.get("items"), "series.items", nonempty=True)
    if len(items) < 2:
        _fail("series artwork needs at least two supplied items.")
    item_ids: set[str] = set()
    for index, item_value in enumerate(items):
        item = _object(item_value, f"series.items[{index}]")
        item_id = _identifier(item.get("id"), f"series.items[{index}].id")
        if item_id in item_ids:
            _fail(f"series repeats item id {item_id!r}.")
        item_ids.add(item_id)
        _text(item.get("label"), f"series.items[{index}].label")
        if isinstance(item.get("route"), dict):
            process_route(item["route"], source_ids=source_ids)
        elif isinstance(item.get("venue"), dict):
            nested = {"venue": item["venue"]}
            _validate_venue(nested, source_ids)
        else:
            _fail(f"series.items[{index}] needs route or venue geometry.")


def validate_sports_record(
    record: Any, *, preset_id: str | None = None
) -> tuple[dict[str, Any], SportsPreset, tuple[dict[str, Any], ...]]:
    """Validate one flexible sports record while keeping source claims strict."""

    value = _object(record, "record")
    _reject_protected_assets(value)
    schema_version = value.get("schema_version", SPORTS_SCHEMA_VERSION)
    if schema_version != SPORTS_SCHEMA_VERSION:
        _fail(f"schema_version must be {SPORTS_SCHEMA_VERSION}.")
    _identifier(value.get("id"), "id")
    sources = _validate_sources(value)
    source_ids = _source_ids(sources)
    _validate_distance_metadata(value, source_ids)
    requested_preset = preset_id or value.get("preset", "route-hero")
    preset = resolve_sports_preset(str(requested_preset))
    _validate_context(value, source_ids)
    _validate_profiles(value, source_ids)
    if preset.hero_kind == "route":
        route = _object(value.get("route"), "route")
        geometry_status = _text(route.get("geometry_status"), "route.geometry_status")
        if geometry_status not in ROUTE_GEOMETRY_STATUSES:
            _fail(
                f"route.geometry_status {geometry_status!r} is not drawable. "
                "Supply official, verified, source-derived, or user-owned geometry."
            )
        processed = process_route(route, source_ids=source_ids)
        _validate_route_controls(route, source_ids)
        if preset.id == "route-and-elevation" and not _route_has_channel(
            value, "elevation"
        ):
            _fail("route-and-elevation requires supplied elevation values.")
        if preset.id == "topographic-challenge":
            contours = [
                feature
                for feature in (value.get("context") or {}).get("features", [])
                if isinstance(feature, dict) and feature.get("kind") == "contour"
            ]
            if not contours:
                _fail("topographic-challenge requires supplied factual contours.")
        if preset.id == "multi-discipline":
            disciplines = {segment.discipline for segment in processed.segments}
            if len(disciplines) < 2:
                _fail("multi-discipline requires at least two supplied disciplines.")
        if preset.id == "river-course":
            kinds = {
                str(feature.get("kind"))
                for feature in (value.get("context") or {}).get("features", [])
                if isinstance(feature, dict)
            }
            if not kinds.intersection({"river-bank", "bank", "shoreline"}):
                _fail("river-course requires supplied river-bank geometry.")
        if preset.id == "sailing-chart":
            markers = route.get("markers") or []
            if not any(
                isinstance(marker, dict)
                and marker.get("kind") in {"mark", "buoy"}
                for marker in markers
            ):
                _fail("sailing-chart requires supplied marks or buoys.")
        if preset.id == "race-telemetry":
            available = {
                str(profile.get("channel"))
                for profile in value.get("profiles", [])
                if isinstance(profile, dict)
            }
            available.update(
                channel
                for channel in PROFILE_INKS
                if _route_has_channel(value, channel)
            )
            available.discard("elevation")
            available.discard("turn_angle")
            if not available:
                _fail("race-telemetry requires at least one supplied telemetry trace.")
    elif preset.hero_kind == "venue":
        _validate_venue(value, source_ids)
    else:
        _validate_series(value, source_ids)
    event = value.get("event")
    if event is not None:
        event_value = _object(event, "event")
        if event_value.get("date") is not None:
            raw_date = _text(event_value["date"], "event.date")
            try:
                date.fromisoformat(raw_date)
            except ValueError as exc:
                raise MapPlotterError(
                    "Invalid sports artwork data: event.date must be ISO-8601."
                ) from exc
    return copy.deepcopy(value), preset, sources


@dataclass(frozen=True, slots=True)
class _CoordinateTransform:
    coordinate_space: str
    scale_m_per_unit: float
    mean_latitude_deg: float
    longitude_reference_deg: float
    model_bounds: tuple[float, float, float, float]
    drawing: Rect
    scale_mm_per_model_unit: float

    def _model_point(self, point: Sequence[float]) -> Point:
        x = float(point[0])
        y = float(point[1])
        if self.coordinate_space == "local":
            return (x * self.scale_m_per_unit, y * self.scale_m_per_unit)
        delta = x - self.longitude_reference_deg
        if delta > 180.0:
            x -= 360.0
        elif delta < -180.0:
            x += 360.0
        earth = 6_371_008.8
        return (
            earth
            * math.radians(x - self.longitude_reference_deg)
            * math.cos(math.radians(self.mean_latitude_deg)),
            earth * math.radians(y - self.mean_latitude_deg),
        )

    def point(self, point: Sequence[float]) -> Point:
        model_x, model_y = self._model_point(point)
        minimum_x, minimum_y, maximum_x, maximum_y = self.model_bounds
        used_width = (maximum_x - minimum_x) * self.scale_mm_per_model_unit
        used_height = (maximum_y - minimum_y) * self.scale_mm_per_model_unit
        offset_x = self.drawing.x + (self.drawing.width - used_width) / 2.0
        offset_y = self.drawing.y + (self.drawing.height - used_height) / 2.0
        return (
            offset_x + (model_x - minimum_x) * self.scale_mm_per_model_unit,
            offset_y + (maximum_y - model_y) * self.scale_mm_per_model_unit,
        )

    def points(self, points: Iterable[Sequence[float]]) -> Stroke:
        return [self.point(point) for point in points]

    def as_dict(self) -> dict[str, Any]:
        return {
            "coordinate_space": self.coordinate_space,
            "scale_m_per_unit": self.scale_m_per_unit,
            "mean_latitude_deg": round(self.mean_latitude_deg, 8),
            "model_bounds": [round(value, 6) for value in self.model_bounds],
            "drawing_mm": self.drawing.as_dict(),
            "scale_mm_per_model_unit": self.scale_mm_per_model_unit,
        }


def _coordinate_transform(
    points: Sequence[Sequence[float]],
    drawing: Rect,
    *,
    coordinate_space: str,
    scale_m_per_unit: float = 1.0,
) -> _CoordinateTransform:
    if len(points) < 2:
        _fail("hero geometry needs at least two points to frame.")
    mean_latitude = (
        sum(float(point[1]) for point in points) / len(points)
        if coordinate_space == "wgs84"
        else 0.0
    )
    longitude_reference = float(points[0][0]) if coordinate_space == "wgs84" else 0.0
    provisional = _CoordinateTransform(
        coordinate_space=coordinate_space,
        scale_m_per_unit=scale_m_per_unit,
        mean_latitude_deg=mean_latitude,
        longitude_reference_deg=longitude_reference,
        model_bounds=(0.0, 0.0, 1.0, 1.0),
        drawing=drawing,
        scale_mm_per_model_unit=1.0,
    )
    model = [provisional._model_point(point) for point in points]
    xs = [point[0] for point in model]
    ys = [point[1] for point in model]
    minimum_x, maximum_x = min(xs), max(xs)
    minimum_y, maximum_y = min(ys), max(ys)
    span_x = max(maximum_x - minimum_x, 1e-6)
    span_y = max(maximum_y - minimum_y, 1e-6)
    scale = min(drawing.width / span_x, drawing.height / span_y)
    return _CoordinateTransform(
        coordinate_space=coordinate_space,
        scale_m_per_unit=scale_m_per_unit,
        mean_latitude_deg=mean_latitude,
        longitude_reference_deg=longitude_reference,
        model_bounds=(minimum_x, minimum_y, maximum_x, maximum_y),
        drawing=drawing,
        scale_mm_per_model_unit=scale,
    )


def _pen_id(ink: str, nib_mm: float) -> str:
    matches = [
        pen.identity
        for pen in ACTUAL_PEN_INVENTORY.pens
        if pen.ink.casefold() == ink.casefold()
        and math.isclose(pen.mark_width_mm, nib_mm, abs_tol=1e-9)
    ]
    if len(matches) != 1:
        _fail(f"studio inventory has no unique {ink} {nib_mm:g} mm pen.")
    return matches[0]


def _semantic_pen_id(context: PlateContext, ink: str, role: str) -> str:
    requested = float(context.plate["map_linework_nib_mm"][role])
    available = [
        pen
        for pen in ACTUAL_PEN_INVENTORY.pens
        if pen.ink.casefold() == ink.casefold()
        and round(pen.nominal_nib_mm, 6)
        in {round(float(value), 6) for value in context.plate["nib_ladder_mm"]}
    ]
    if not available:
        _fail(f"studio inventory has no {ink} pen on this plate's nib ladder.")
    selected = min(
        available,
        key=lambda pen: (abs(pen.mark_width_mm - requested), pen.mark_width_mm),
    )
    assert selected.identity
    return selected.identity


def _layer(
    artwork: PlateArtwork,
    layer_id: str,
    label: str,
    *,
    ink: str,
    role: str = "hairline",
) -> ArtworkLayer:
    return artwork.layer(
        layer_id,
        label,
        _semantic_pen_id(artwork.context, ink, role),
    )


def _route_pen_plan(context: PlateContext, ink: str) -> PenWidthFit:
    return fit_pen_width(
        ACTUAL_PEN_INVENTORY,
        ink=ink,
        requested_width_mm=float(context.plate["race_course"]["target_width_mm"]),
        allowed_nibs_mm=tuple(float(value) for value in context.plate["nib_ladder_mm"]),
    )


def _field_panels(
    context: PlateContext, preset: SportsPreset
) -> tuple[Rect, Rect | None]:
    gap = float(context.plate["gap_mm"])
    inner = context.field.inset(gap)
    if preset.id in {"route-and-elevation", "route-fingerprint"}:
        band = min(context.zones["detail"].height, inner.height - gap)
    elif preset.id in {"race-telemetry", "circuit-blueprint"}:
        band = min(
            context.zones["detail"].height
            + context.zones["furniture"].height
            + gap,
            inner.height - gap,
        )
    elif preset.id in {"personal-best", "matchday-memory"}:
        band = min(context.zones["title"].height, inner.height - gap)
    else:
        return inner, None
    hero = Rect(inner.x, inner.y, inner.width, inner.height - band - gap)
    secondary = Rect(inner.x, hero.bottom + gap, inner.width, band)
    if min(hero.width, hero.height, secondary.width, secondary.height) <= 0.0:
        _fail(f"preset {preset.id!r} cannot fit the binding map_field.")
    return hero, secondary


@dataclass(frozen=True, slots=True)
class _Trace:
    id: str
    channel: str
    label: str
    unit: str
    source_ref: str
    processing_status: str
    processing_method: str | None
    runs: tuple[tuple[QuantitativeSample, ...], ...]
    source_point_count: int

    @property
    def extent(self) -> tuple[float, float, float, float]:
        values = [sample for run in self.runs for sample in run]
        return (
            min(sample.distance_m for sample in values),
            min(sample.value for sample in values),
            max(sample.distance_m for sample in values),
            max(sample.value for sample in values),
        )

    def as_dict(self) -> dict[str, Any]:
        minimum_x, minimum_y, maximum_x, maximum_y = self.extent
        return {
            "id": self.id,
            "channel": self.channel,
            "label": self.label,
            "unit": self.unit,
            "source_ref": self.source_ref,
            "processing_status": self.processing_status,
            "processing_method": self.processing_method,
            "source_point_count": self.source_point_count,
            "run_count": len(self.runs),
            "distance_extent_m": [minimum_x, maximum_x],
            "value_extent": [minimum_y, maximum_y],
        }


def _explicit_traces(record: Mapping[str, Any]) -> list[_Trace]:
    result: list[_Trace] = []
    for profile_value in record.get("profiles", []) or []:
        profile = dict(profile_value)
        samples: list[tuple[float, float | None]] = []
        for sample in profile["samples"]:
            if isinstance(sample, dict):
                samples.append((float(sample["distance_m"]), sample.get("value")))
            else:
                samples.append((float(sample[0]), sample[1]))
        processing = profile.get("processing", "raw")
        if isinstance(processing, str):
            status = processing
            method = None
        else:
            status = str(processing["status"])
            method = (
                str(processing["method"])
                if processing.get("method") is not None
                else None
            )
        runs = split_quantitative_runs(samples)
        result.append(
            _Trace(
                id=str(profile["id"]),
                channel=str(profile["channel"]),
                label=str(profile.get("label") or profile["channel"]).upper(),
                unit=str(profile["unit"]),
                source_ref=str(profile["source_ref"]),
                processing_status=status,
                processing_method=method,
                runs=runs,
                source_point_count=len(samples),
            )
        )
    return result


def _segment_distances(
    processed: RouteProcessingResult, segment: SportsSegment
) -> list[float]:
    values = [0.0]
    for first, second in zip(segment.points, segment.points[1:]):
        if processed.coordinate_space == "wgs84":
            from .route_chainage import geodesic_distance_m

            distance = geodesic_distance_m(first.coordinates, second.coordinates)
        else:
            distance = (
                math.hypot(second.x - first.x, second.y - first.y)
                * processed.scale_m_per_unit
            )
        values.append(values[-1] + distance)
    return values


def _derived_trace(
    processed: RouteProcessingResult,
    record: Mapping[str, Any],
    channel: str,
) -> _Trace | None:
    route = dict(record["route"])
    units = route.get("channel_units") or {}
    if not isinstance(units, dict):
        _fail("route.channel_units must be an object.")
    unit = str(units.get(channel) or DEFAULT_CHANNEL_UNITS[channel])
    samples: list[tuple[float, float | None]] = []
    source_ref = processed.segments[0].source_ref
    offset = 0.0
    for segment in processed.segments:
        distances = _segment_distances(processed, segment)
        for point, distance in zip(segment.points, distances):
            supplied_distance = point.distance_m
            x = offset + (
                supplied_distance
                if supplied_distance is not None
                else distance
            )
            samples.append((x, point.channel(channel)))
        offset += distances[-1]
        # A null row makes the source break explicit in the profile.
        samples.append((offset, None))
    if samples:
        samples.pop()
    runs = split_quantitative_runs(samples)
    if not runs or sum(len(run) for run in runs) < 2:
        return None
    return _Trace(
        id=f"route-{channel}",
        channel=channel,
        label=channel.replace("_", " ").upper(),
        unit=unit,
        source_ref=source_ref,
        processing_status="raw",
        processing_method=None,
        runs=runs,
        source_point_count=len(samples),
    )


def _turn_angle_trace(processed: RouteProcessingResult) -> _Trace | None:
    samples: list[tuple[float, float | None]] = []
    offset = 0.0
    for segment in processed.segments:
        distances = _segment_distances(processed, segment)
        points = segment.points
        for index, distance in enumerate(distances):
            if index == 0 or index == len(points) - 1:
                angle = 0.0
            else:
                before = points[index - 1]
                current = points[index]
                after = points[index + 1]
                first = (current.x - before.x, current.y - before.y)
                second = (after.x - current.x, after.y - current.y)
                cross = first[0] * second[1] - first[1] * second[0]
                dot = first[0] * second[0] + first[1] * second[1]
                angle = math.degrees(math.atan2(cross, dot))
            samples.append((offset + distance, angle))
        offset += distances[-1]
        samples.append((offset, None))
    if samples:
        samples.pop()
    runs = split_quantitative_runs(samples)
    if not runs:
        return None
    return _Trace(
        id="derived-turn-angle",
        channel="turn_angle",
        label="SIGNED TURN ANGLE",
        unit="deg",
        source_ref=processed.segments[0].source_ref,
        processing_status="raw-derived",
        processing_method="signed-planar-vertex-angle-v1",
        runs=runs,
        source_point_count=len(samples),
    )


def _available_traces(
    record: Mapping[str, Any], processed: RouteProcessingResult
) -> list[_Trace]:
    explicit = _explicit_traces(record)
    channels = {trace.channel for trace in explicit}
    candidates = ["elevation", *PROFILE_INKS]
    for channel in dict.fromkeys(candidates):
        if channel in channels or channel == "turn_angle":
            continue
        trace = _derived_trace(processed, record, channel)
        if trace is not None:
            explicit.append(trace)
            channels.add(channel)
    return explicit


def _line_parts(geometry: BaseGeometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return [part for part in geometry.geoms if not part.is_empty]
    if isinstance(geometry, GeometryCollection):
        result: list[LineString] = []
        for part in geometry.geoms:
            result.extend(_line_parts(part))
        return result
    boundary = getattr(geometry, "boundary", None)
    return _line_parts(boundary) if isinstance(boundary, BaseGeometry) else []


def _clip_stroke(points: Sequence[Point], rect: Rect) -> list[Stroke]:
    if len(points) < 2:
        return []
    try:
        clipped = LineString(points).intersection(
            box(rect.left, rect.top, rect.right, rect.bottom)
        )
    except GEOSException as exc:
        raise MapPlotterError(f"Sports linework could not be clipped: {exc}") from exc
    return [
        [(float(x), float(y)) for x, y in part.coords]
        for part in _line_parts(clipped)
        if len(part.coords) >= 2
    ]


def _offset_strokes(
    points: Sequence[Point],
    offsets: Sequence[float],
    *,
    minimum_length_mm: float,
    clip_rect: Rect,
) -> list[Stroke]:
    if len(points) < 2:
        return []
    line = LineString(points)
    result: list[Stroke] = []
    for offset in offsets:
        try:
            shifted: BaseGeometry = (
                line
                if abs(offset) <= 1e-9
                else line.offset_curve(offset, quad_segs=4, join_style="round")
            )
        except GEOSException:
            shifted = line
        for part in _line_parts(shifted):
            coordinates = [(float(value[0]), float(value[1])) for value in part.coords]
            for clipped in _clip_stroke(coordinates, clip_rect):
                if polyline_length_mm(clipped) + 1e-9 >= minimum_length_mm:
                    result.append(clipped)
    return result


def _feature_source_points(feature: Mapping[str, Any], label: str) -> list[list[float]]:
    return [point for path in _geometry_paths(feature, label) for point in path]


def _context_features(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    context = record.get("context") or {}
    if not isinstance(context, dict):
        return []
    return [
        dict(feature)
        for feature in context.get("features", [])
        if isinstance(feature, dict)
    ]


def _context_bounds_points(
    record: Mapping[str, Any], preset: SportsPreset
) -> list[list[float]]:
    included: set[str]
    if preset.id == "river-course":
        included = {"river-bank", "bank", "shoreline", "bridge"}
    elif preset.id == "sailing-chart":
        included = {"coastline", "shoreline", "island"}
    elif preset.id in {"circuit-blueprint", "race-telemetry"}:
        included = {"site-outline", "pit-lane"}
    else:
        return []
    return [
        point
        for index, feature in enumerate(_context_features(record))
        if str(feature.get("kind")) in included
        for point in _feature_source_points(feature, f"context.features[{index}]")
    ]


def _context_layer_spec(kind: str) -> tuple[str, str, str, str]:
    if kind in {
        "river-bank",
        "bank",
        "shoreline",
        "coastline",
        "island",
        "water-boundary",
    }:
        return ("context_water", "Water boundaries", "Blue", "hairline")
    if kind == "contour":
        return ("context_contours", "Source contours", "Grey", "hairline")
    if kind in {"bridge", "landmark"}:
        return ("context_landmarks", "Event landmarks", "Black", "hairline")
    if kind in {"pit-lane", "sector-boundary", "drs-zone"}:
        return ("context_circuit", "Circuit context", "Black", "hairline")
    if kind in {"buoy", "mark"}:
        return ("context_marks", "Sailing marks", "Red", "hairline")
    return ("context_site", "Sparse site and map context", "Grey", "hairline")


def _render_context(
    artwork: PlateArtwork,
    record: Mapping[str, Any],
    preset: SportsPreset,
    transform: _CoordinateTransform,
    drawing: Rect,
    route_lines: Sequence[Stroke],
) -> dict[str, Any]:
    if preset.context_policy == "none":
        return {
            "policy": "none",
            "supplied_feature_count": len(_context_features(record)),
            "rendered_feature_count": 0,
            "omitted_feature_count": len(_context_features(record)),
        }
    route_geometry: BaseGeometry = (
        LineString(route_lines[0])
        if len(route_lines) == 1 and len(route_lines[0]) >= 2
        else GeometryCollection(
            [LineString(line) for line in route_lines if len(line) >= 2]
        )
    )
    candidates: list[
        tuple[float, float, dict[str, Any], list[Stroke], tuple[str, str, str, str]]
    ] = []
    for index, feature in enumerate(_context_features(record)):
        kind = str(feature.get("kind"))
        if preset.context_policy == "source-contours" and kind != "contour":
            continue
        if preset.context_policy == "river" and kind not in {
            "river-bank",
            "bank",
            "shoreline",
            "bridge",
            "landmark",
            "road",
        }:
            continue
        if preset.context_policy == "coastal" and kind not in {
            "coastline",
            "shoreline",
            "island",
            "buoy",
            "mark",
            "landmark",
        }:
            continue
        if preset.context_policy in {"circuit-site", "minimal-site"} and kind not in {
            "site-outline",
            "pit-lane",
            "sector-boundary",
            "drs-zone",
            "access-road",
        }:
            continue
        paths = _geometry_paths(feature, f"context.features[{index}]")
        clipped = [
            stroke
            for path in paths
            for stroke in _clip_stroke(transform.points(path), drawing)
        ]
        if not clipped:
            continue
        feature_geometry = GeometryCollection(
            [LineString(path) for path in clipped if len(path) >= 2]
        )
        proximity = (
            feature_geometry.distance(route_geometry)
            if not route_geometry.is_empty
            else 0.0
        )
        priority = float(feature.get("priority", 0.0))
        candidates.append(
            (
                -priority,
                proximity,
                feature,
                clipped,
                _context_layer_spec(kind),
            )
        )
    candidates.sort(key=lambda value: (value[0], value[1], str(value[2]["id"])))
    max_features = int(artwork.context.plate["landmark_buildings"]["max_objects"])
    maximum_ink = min(
        float(artwork.context.plate["ink_budget"]["max_ink_mm2"]),
        drawing.width * drawing.height,
    )
    # Context receives at most the same physical ink as three hairline passes
    # around the hero's bounding box.  The route therefore stays dominant even
    # when a caller supplies a very dense city extract.
    context_ink_limit = min(
        maximum_ink,
        3.0 * 2.0 * (drawing.width + drawing.height) * 0.25,
    )
    rendered = 0
    omitted = 0
    ink_used = 0.0
    per_kind: dict[str, int] = {}
    for _priority, _proximity, feature, strokes, spec in candidates:
        layer_id, label, ink, role = spec
        layer = _layer(artwork, layer_id, label, ink=ink, role=role)
        feature_ink = sum(polyline_length_mm(stroke) for stroke in strokes) * (
            layer.pen.mark_width_mm
        )
        if rendered >= max_features or ink_used + feature_ink > context_ink_limit:
            omitted += 1
            continue
        minimum = 3.0 * layer.pen.mark_width_mm
        accepted = 0
        for stroke in strokes:
            if polyline_length_mm(stroke) + 1e-9 < minimum:
                continue
            attributes = {
                "data-context-kind": str(feature["kind"]),
                "data-feature-id": str(feature["id"]),
                "data-context-selection": "route-relevant-physical-budget-v1",
            }
            if feature.get("elevation_m") is not None:
                attributes["data-elevation-m"] = f"{float(feature['elevation_m']):g}"
            layer.add(
                stroke,
                source_ref=str(feature["source_ref"]),
                role=str(feature["kind"]),
                attributes=attributes,
            )
            accepted += 1
        if accepted:
            rendered += 1
            ink_used += feature_ink
            kind = str(feature["kind"])
            per_kind[kind] = per_kind.get(kind, 0) + 1
        else:
            omitted += 1
    return {
        "policy": preset.context_policy,
        "selection_policy_id": "route-relevant-physical-context-budget-v1",
        "supplied_feature_count": len(_context_features(record)),
        "eligible_feature_count": len(candidates),
        "rendered_feature_count": rendered,
        "omitted_feature_count": omitted + max(0, len(_context_features(record)) - len(candidates)),
        "rendered_by_kind": per_kind,
        "ink_budget_mm2": round(context_ink_limit, 3),
        "ink_used_mm2_upper_bound": round(ink_used, 3),
    }


def _marker_point(marker: Mapping[str, Any], label: str) -> tuple[float, float]:
    point = marker.get("point", marker.get("coordinates"))
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        _fail(f"{label} needs point [x, y].")
    return (_number(point[0], f"{label}.point[0]"), _number(point[1], f"{label}.point[1]"))


def _route_markers(
    route: Mapping[str, Any], source_ids: set[str]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, marker_value in enumerate(route.get("markers", []) or []):
        marker = _object(marker_value, f"route.markers[{index}]")
        marker_id = _identifier(
            marker.get("id", f"marker-{index + 1}"), f"route.markers[{index}].id"
        )
        kind = _text(marker.get("kind"), f"route.markers[{index}].kind")
        point = _marker_point(marker, f"route.markers[{index}]")
        source_ref = _validate_source_ref(
            marker.get("source_ref"),
            f"route.markers[{index}].source_ref",
            source_ids,
        )
        normalized = copy.deepcopy(marker)
        for key in ("distance_m", "heading_deg", "wind_direction_deg", "wind_speed"):
            if marker.get(key) is None:
                continue
            number = _number(marker[key], f"route.markers[{index}].{key}")
            if key in {"distance_m", "wind_speed"} and number < 0.0:
                _fail(f"route.markers[{index}].{key} must not be negative.")
            normalized[key] = number
        result.append(
            {
                **normalized,
                "id": marker_id,
                "kind": kind,
                "point": point,
                "source_ref": source_ref,
            }
        )
    return result


def _nearest_vertex_indices(
    segment: SportsSegment, markers: Sequence[Mapping[str, Any]]
) -> set[int]:
    protected: set[int] = set()
    for marker in markers:
        if marker.get("segment_id") not in {None, segment.id}:
            continue
        if marker.get("vertex_index") is not None:
            index = int(marker["vertex_index"])
            if 0 <= index < len(segment.points):
                protected.add(index)
            continue
        marker_point = marker["point"]
        protected.add(
            min(
                range(len(segment.points)),
                key=lambda index: math.hypot(
                    segment.points[index].x - float(marker_point[0]),
                    segment.points[index].y - float(marker_point[1]),
                ),
            )
        )
    return protected


def _out_and_back_split(points: Sequence[Point], tolerance_mm: float) -> int | None:
    if len(points) < 6:
        return None
    start = points[0]
    finish = points[-1]
    extent = max(
        max(point[0] for point in points) - min(point[0] for point in points),
        max(point[1] for point in points) - min(point[1] for point in points),
    )
    if math.hypot(finish[0] - start[0], finish[1] - start[1]) > max(
        tolerance_mm * 4.0, extent * 0.08
    ):
        return None
    split = max(
        range(1, len(points) - 1),
        key=lambda index: math.hypot(
            points[index][0] - start[0], points[index][1] - start[1]
        ),
    )
    if split < 2 or len(points) - split < 3:
        return None
    outbound = LineString(points[: split + 1])
    returning = LineString(list(reversed(points[split:])))
    if min(outbound.length, returning.length) <= 0.0:
        return None
    if outbound.hausdorff_distance(returning) <= max(tolerance_mm * 5.0, extent * 0.03):
        return split
    return None


def _coincident_segment_groups(
    lines: Sequence[Stroke], tolerance_mm: float
) -> list[list[int]]:
    groups: list[list[int]] = []
    assigned: set[int] = set()
    for first_index, first in enumerate(lines):
        if first_index in assigned or len(first) < 2:
            continue
        group = [first_index]
        first_line = LineString(first)
        for second_index in range(first_index + 1, len(lines)):
            if second_index in assigned or len(lines[second_index]) < 2:
                continue
            second_line = LineString(lines[second_index])
            if first_line.hausdorff_distance(second_line) <= tolerance_mm:
                group.append(second_index)
                assigned.add(second_index)
        if len(group) > 1:
            assigned.add(first_index)
            groups.append(group)
    return groups


def _render_route_style(
    layer: ArtworkLayer,
    points: Sequence[Point],
    *,
    style: str,
    plan: PenWidthFit,
    drawing: Rect,
    source_ref: str,
    role: str,
    attributes: Mapping[str, str],
    forced_offset_mm: float = 0.0,
) -> int:
    minimum = 3.0 * layer.pen.mark_width_mm
    if style == "single-line":
        offsets = [forced_offset_mm]
    elif style in {"parallel-dual", "ribbon-edges", "hatched-corridor"}:
        half = max(
            plan.requested_width_mm / 2.0 - layer.pen.mark_width_mm / 2.0,
            layer.pen.mark_width_mm * 0.5,
        )
        offsets = [forced_offset_mm - half, forced_offset_mm + half]
    else:
        offsets = [forced_offset_mm + offset for offset in plan.offset_positions()]
    strokes = _offset_strokes(
        points,
        offsets,
        minimum_length_mm=minimum,
        clip_rect=drawing,
    )
    for sequence, stroke in enumerate(strokes, start=1):
        layer.add(
            stroke,
            source_ref=source_ref,
            role=role,
            sequence=sequence,
            attributes=dict(attributes),
        )
    if style not in {"hatched-corridor", "variable-density"}:
        return len(strokes)
    line = LineString(points)
    if line.length <= minimum:
        return len(strokes)
    spacing = max(float(plan.requested_width_mm) * 5.0, layer.pen.mark_width_mm * 12.0)
    hatch_length = max(plan.requested_width_mm, minimum * 1.02)
    position = spacing
    hatch_count = 0
    while position < line.length - spacing / 2.0:
        anchor = line.interpolate(position)
        before = line.interpolate(max(0.0, position - layer.pen.mark_width_mm))
        after = line.interpolate(min(line.length, position + layer.pen.mark_width_mm))
        dx = after.x - before.x
        dy = after.y - before.y
        tangent_length = math.hypot(dx, dy)
        if tangent_length > 1e-9:
            normal = (-dy / tangent_length, dx / tangent_length)
            if style == "variable-density":
                progression = position / line.length
                length = minimum * (1.02 + progression)
            else:
                length = hatch_length
            hatch = [
                (
                    anchor.x - normal[0] * length / 2.0,
                    anchor.y - normal[1] * length / 2.0,
                ),
                (
                    anchor.x + normal[0] * length / 2.0,
                    anchor.y + normal[1] * length / 2.0,
                ),
            ]
            clipped = _clip_stroke(hatch, drawing)
            for stroke in clipped:
                if polyline_length_mm(stroke) + 1e-9 >= minimum:
                    layer.add(
                        stroke,
                        source_ref=source_ref,
                        role=(
                            "distance-progression-motif"
                            if style == "variable-density"
                            else "corridor-hatch"
                        ),
                        attributes={
                            **dict(attributes),
                            "data-distance-fraction": f"{progression:.6f}"
                            if style == "variable-density"
                            else f"{position / line.length:.6f}",
                        },
                    )
                    hatch_count += 1
        position += spacing
    return len(strokes) + hatch_count


def _chevron_strokes(
    line: LineString, fraction: float, arm_mm: float
) -> list[Stroke]:
    distance = line.length * fraction
    anchor = line.interpolate(distance)
    delta = min(max(arm_mm, 0.1), line.length / 8.0)
    before = line.interpolate(max(0.0, distance - delta))
    after = line.interpolate(min(line.length, distance + delta))
    dx = after.x - before.x
    dy = after.y - before.y
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return []
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    back = (anchor.x - ux * arm_mm, anchor.y - uy * arm_mm)
    return [
        [(back[0] + px * arm_mm * 0.55, back[1] + py * arm_mm * 0.55), (anchor.x, anchor.y)],
        [(back[0] - px * arm_mm * 0.55, back[1] - py * arm_mm * 0.55), (anchor.x, anchor.y)],
    ]


def _render_direction_marks(
    artwork: PlateArtwork,
    route: Mapping[str, Any],
    lines: Sequence[Stroke],
    source_ref: str,
) -> dict[str, Any]:
    mode = str(route.get("direction_marks", "arrows"))
    if mode not in {"none", "arrows", "progressive", "kilometre-ticks", "checkpoints"}:
        _fail(f"route.direction_marks {mode!r} is unsupported.")
    if mode in {"none", "checkpoints"}:
        return {"mode": mode, "rendered_count": 0}
    layer = _layer(
        artwork, "accents_direction", "Direction and progression", ink="Red", role="hairline"
    )
    minimum = 3.0 * layer.pen.mark_width_mm
    count = 0
    for line_points in lines:
        if len(line_points) < 2:
            continue
        line = LineString(line_points)
        if line.length < minimum * 6.0:
            continue
        if mode in {"arrows", "progressive"}:
            fractions = tuple(
                (index + 1) / (int(artwork.context.plate["detail_lines"]) + 1)
                for index in range(int(artwork.context.plate["detail_lines"]))
            )
            for fraction in fractions:
                arm = max(minimum * 1.02, float(artwork.context.plate["gap_mm"]) / 2.0)
                for stroke in _chevron_strokes(line, fraction, arm):
                    if polyline_length_mm(stroke) + 1e-9 >= minimum:
                        layer.add(
                            stroke,
                            source_ref=source_ref,
                            role="direction-chevron",
                            attributes={"data-route-fraction": f"{fraction:.6f}"},
                        )
                        count += 1
        else:
            # Physical kilometre tick count is capped by the plate's landmark
            # capacity. Distances are measured from the supplied geometry and
            # labelled as measured unless a caller supplied matching stations.
            maximum = max(
                2,
                int(artwork.context.plate["landmark_buildings"]["max_objects"]) // 2,
            )
            tick_count = min(maximum, max(1, int(line.length / 10.0)))
            for index in range(1, tick_count + 1):
                fraction = index / (tick_count + 1)
                anchor = line.interpolate(fraction, normalized=True)
                delta = min(layer.pen.mark_width_mm, line.length / 20.0)
                before = line.interpolate(max(0.0, line.length * fraction - delta))
                after = line.interpolate(min(line.length, line.length * fraction + delta))
                dx, dy = after.x - before.x, after.y - before.y
                tangent = math.hypot(dx, dy)
                if tangent <= 1e-9:
                    continue
                normal = (-dy / tangent, dx / tangent)
                tick = [
                    (anchor.x - normal[0] * minimum / 2.0, anchor.y - normal[1] * minimum / 2.0),
                    (anchor.x + normal[0] * minimum / 2.0, anchor.y + normal[1] * minimum / 2.0),
                ]
                layer.add(
                    tick,
                    source_ref=source_ref,
                    role="measured-distance-tick",
                    attributes={"data-route-fraction": f"{fraction:.6f}"},
                )
                count += 1
    return {"mode": mode, "rendered_count": count}


def _finish_bars(points: Sequence[Point], length_mm: float) -> list[Stroke]:
    if len(points) < 2:
        return []
    finish = points[-1]
    neighbour = points[-2]
    dx = finish[0] - neighbour[0]
    dy = finish[1] - neighbour[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return []
    normal = (-dy / length, dx / length)
    tangent = (dx / length, dy / length)
    result: list[Stroke] = []
    for shift in (-length_mm * 0.35, length_mm * 0.35):
        centre = (
            finish[0] + tangent[0] * shift,
            finish[1] + tangent[1] * shift,
        )
        result.append(
            [
                (
                    centre[0] - normal[0] * length_mm / 2.0,
                    centre[1] - normal[1] * length_mm / 2.0,
                ),
                (
                    centre[0] + normal[0] * length_mm / 2.0,
                    centre[1] + normal[1] * length_mm / 2.0,
                ),
            ]
        )
    return result


def _nearest_route_tangent(point: Point, route_lines: Sequence[Stroke]) -> Point:
    best_distance = math.inf
    best_tangent = (1.0, 0.0)
    for line in route_lines:
        for first, second in zip(line, line[1:]):
            dx = second[0] - first[0]
            dy = second[1] - first[1]
            length_squared = dx * dx + dy * dy
            if length_squared <= 1e-18:
                continue
            fraction = min(
                1.0,
                max(
                    0.0,
                    ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy)
                    / length_squared,
                ),
            )
            nearest = (first[0] + fraction * dx, first[1] + fraction * dy)
            distance = math.hypot(point[0] - nearest[0], point[1] - nearest[1])
            if distance < best_distance:
                length = math.sqrt(length_squared)
                best_distance = distance
                best_tangent = (dx / length, dy / length)
    return best_tangent


def _render_markers(
    artwork: PlateArtwork,
    markers: Sequence[Mapping[str, Any]],
    transform: _CoordinateTransform,
    route_lines: Sequence[Stroke],
    *,
    start_control: Mapping[str, Any],
    finish_control: Mapping[str, Any],
) -> dict[str, Any]:
    symbol = _layer(
        artwork, "markers", "Start, finish, checkpoints, and transitions", ink="Black", role="hairline"
    )
    copy_layer = _layer(
        artwork, "annotations", "Sports annotations", ink="Black", role="hairline"
    )
    nib = symbol.pen.mark_width_mm
    minimum = 3.0 * nib
    gap = float(artwork.context.plate["gap_mm"])
    radius = max(2.0 * nib, gap / 3.0)
    first_line = next((line for line in route_lines if len(line) >= 2), None)
    last_line = next((line for line in reversed(route_lines) if len(line) >= 2), None)
    if first_line is not None:
        start_point = transform.point(start_control["point"])
        symbol.add(
            circle_stroke(start_point, radius, segments=20),
            source_ref=str(start_control["source_ref"]),
            role="start",
            attributes={
                "data-marker-kind": "start",
                "data-control-status": str(start_control["status"]),
            },
        )
    if last_line is not None:
        finish_point = transform.point(finish_control["point"])
        tangent = _nearest_route_tangent(finish_point, route_lines)
        finish_reference = [
            (finish_point[0] - tangent[0], finish_point[1] - tangent[1]),
            finish_point,
        ]
        for stroke in _finish_bars(
            finish_reference, max(minimum * 1.05, gap)
        ):
            symbol.add(
                stroke,
                source_ref=str(finish_control["source_ref"]),
                role="finish",
                attributes={
                    "data-marker-kind": "finish",
                    "data-control-status": str(finish_control["status"]),
                },
            )
    maximum_markers = max(
        1, int(artwork.context.plate["landmark_buildings"]["max_objects"]) // 2
    )
    rendered = 0
    omitted = 0
    cap = float(artwork.context.plate["type_scale_mm"]["attribution"])
    for marker in markers:
        if rendered >= maximum_markers:
            omitted += 1
            continue
        point = transform.point(marker["point"])
        if not (
            transform.drawing.left <= point[0] <= transform.drawing.right
            and transform.drawing.top <= point[1] <= transform.drawing.bottom
        ):
            omitted += 1
            continue
        kind = str(marker["kind"])
        if kind in {"transition", "sector", "checkpoint", "corner", "bridge"}:
            strokes = cross_strokes(point, max(radius, minimum / 2.0))
        else:
            strokes = [circle_stroke(point, radius, segments=16)]
        marker_attributes = {
            "data-marker-id": str(marker["id"]),
            "data-marker-kind": kind,
        }
        for key in (
            "distance_m",
            "split_time",
            "elapsed_time",
            "heading_deg",
            "wind_direction_deg",
            "wind_speed",
            "wind_unit",
            "category",
            "lap",
            "sector",
        ):
            if marker.get(key) is not None:
                marker_attributes[f"data-{key.replace('_', '-')}"] = str(marker[key])
        for stroke in strokes:
            if polyline_length_mm(stroke) + 1e-9 >= minimum:
                symbol.add(
                    stroke,
                    source_ref=str(marker["source_ref"]),
                    role=kind,
                    attributes=marker_attributes,
                )
        label_parts: list[str] = []
        explicit_label = marker.get("label", marker.get("name"))
        if explicit_label:
            label_parts.append(str(explicit_label))
        if kind == "split" and marker.get("distance_m") is not None:
            label_parts.append(f"{float(marker['distance_m']) / 1000.0:g} KM")
        for key in ("split_time", "elapsed_time"):
            if marker.get(key) is not None and str(marker[key]) not in label_parts:
                label_parts.append(str(marker[key]))
        if kind == "climb" and marker.get("category") is not None:
            label_parts.append(f"CAT {marker['category']}")
        if marker.get("heading_deg") is not None:
            label_parts.append(f"{float(marker['heading_deg']):g} DEG")
        if marker.get("wind_direction_deg") is not None:
            wind = f"WIND {float(marker['wind_direction_deg']):g} DEG"
            if marker.get("wind_speed") is not None:
                wind += f" / {marker['wind_speed']} {marker.get('wind_unit', '')}".rstrip()
            label_parts.append(wind)
        label = " / ".join(label_parts)
        if label:
            right_side = point[0] <= transform.drawing.centre[0]
            x = point[0] + gap / 2.0 if right_side else point[0] - gap / 2.0
            maximum_width = max(transform.drawing.width / 2.0 - gap, cap * 4.0)
            x = min(
                max(x, transform.drawing.left + (0.0 if right_side else maximum_width)),
                transform.drawing.right - (maximum_width if right_side else 0.0),
            )
            y = min(
                max(point[1] - cap / 2.0, artwork.context.safe.top),
                artwork.context.safe.bottom - cap,
            )
            add_text(
                copy_layer,
                str(label).upper(),
                x_mm=x,
                y_mm=y,
                preferred_cap_mm=cap,
                maximum_width_mm=maximum_width,
                anchor="start" if right_side else "end",
                allow_horizontal_condense=True,
                source_ref=str(marker["source_ref"]),
                role="marker-label",
                attributes=marker_attributes,
            )
        rendered += 1
    return {
        "supplied_count": len(markers),
        "rendered_count": rendered,
        "omitted_count": omitted,
        "start_rendered": first_line is not None,
        "finish_rendered": last_line is not None,
        "start_control": dict(start_control),
        "finish_control": dict(finish_control),
    }


def _trace_selection(
    record: Mapping[str, Any],
    preset: SportsPreset,
    available: Sequence[_Trace],
) -> list[_Trace]:
    by_channel: dict[str, _Trace] = {}
    for trace in available:
        by_channel.setdefault(trace.channel, trace)
    composition = record.get("composition") or {}
    requested = composition.get("profile_channels") if isinstance(composition, dict) else None
    if requested is not None:
        if not isinstance(requested, list) or not all(
            isinstance(channel, str) for channel in requested
        ):
            _fail("composition.profile_channels must be an array of channel names.")
        missing = [channel for channel in requested if channel not in by_channel]
        if missing:
            _fail(
                "composition.profile_channels requests unavailable supplied data: "
                + ", ".join(missing)
                + "."
            )
        return [by_channel[channel] for channel in requested]
    if preset.id == "route-and-elevation":
        return [by_channel["elevation"]] if "elevation" in by_channel else []
    if preset.id == "circuit-blueprint":
        return [by_channel["elevation"]] if "elevation" in by_channel else []
    if preset.id == "race-telemetry":
        telemetry = [
            trace
            for trace in available
            if trace.channel not in {"elevation", "turn_angle"}
        ]
        return telemetry[:3]
    if preset.id == "route-fingerprint":
        channel = str(
            composition.get("fingerprint_channel", "turn_angle")
            if isinstance(composition, dict)
            else "turn_angle"
        )
        if channel == "turn_angle":
            derived_trace = _turn_angle_trace(
                process_route(record["route"], source_ids={str(source["id"]) for source in record["sources"]})
            )
            return [derived_trace] if derived_trace is not None else []
        if channel not in by_channel:
            _fail(f"route fingerprint channel {channel!r} is not supplied.")
        return [by_channel[channel]]
    return []


def _trace_path(
    samples: Sequence[QuantitativeSample],
    drawing: Rect,
    extent: tuple[float, float, float, float],
) -> Stroke:
    minimum_x, minimum_y, maximum_x, maximum_y = extent
    span_x = max(maximum_x - minimum_x, 1e-9)
    span_y = max(maximum_y - minimum_y, 1e-9)
    return [
        (
            drawing.x + drawing.width * (sample.distance_m - minimum_x) / span_x,
            drawing.bottom - drawing.height * (sample.value - minimum_y) / span_y,
        )
        for sample in samples
    ]


def _render_traces(
    artwork: PlateArtwork,
    traces: Sequence[_Trace],
    panel: Rect,
    *,
    fingerprint: bool = False,
    stations: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    if not traces:
        return []
    gap = float(artwork.context.plate["gap_mm"])
    separator = max(
        artwork.context.plate["map_linework_nib_mm"]["hairline"] * 3.0,
        gap / 3.0,
    )
    available_height = panel.height - separator * (len(traces) - 1)
    trace_height = available_height / len(traces)
    if trace_height <= 0.0:
        _fail("quantitative traces cannot fit the selected plate.")
    axis = _layer(artwork, "data_axes", "Sparse quantitative axes", ink="Grey", role="hairline")
    labels = _layer(artwork, "data_labels", "Quantitative labels", ink="Black", role="hairline")
    linked_stations = [
        station for station in stations if station.get("distance_m") is not None
    ]
    link_layer = (
        _layer(
            artwork,
            "data_links",
            "Supplied route-to-profile stations",
            ink="Grey",
            role="hairline",
        )
        if linked_stations
        else None
    )
    cap = float(artwork.context.plate["type_scale_mm"]["attribution"])
    results: list[dict[str, Any]] = []
    for index, trace in enumerate(traces):
        trace_panel = Rect(
            panel.x,
            panel.y + index * (trace_height + separator),
            panel.width,
            trace_height,
        )
        label_height = min(cap, max(cap, trace_panel.height / 3.0))
        drawing = Rect(
            trace_panel.x,
            trace_panel.y + label_height + axis.pen.mark_width_mm * 3.0,
            trace_panel.width,
            max(
                trace_panel.height - label_height - axis.pen.mark_width_mm * 3.0,
                axis.pen.mark_width_mm * 4.0,
            ),
        )
        if drawing.bottom > trace_panel.bottom + 1e-9:
            drawing = Rect(
                trace_panel.x,
                trace_panel.y + label_height,
                trace_panel.width,
                trace_panel.height - label_height,
            )
        line_layer = _layer(
            artwork,
            f"data_{trace.channel}",
            f"{trace.label} trace",
            ink=PROFILE_INKS.get(trace.channel, "Black"),
            role="hairline",
        )
        minimum = 3.0 * line_layer.pen.mark_width_mm
        maximum_points = max(4, int(drawing.width / (line_layer.pen.mark_width_mm * 2.0)))
        all_samples = [sample for run in trace.runs for sample in run]
        extent = trace.extent
        rendered_points = 0
        rendered_runs = 0
        for run in trace.runs:
            if len(run) < 2:
                continue
            budget = max(4, round(maximum_points * len(run) / max(len(all_samples), 1)))
            downsampled = extrema_preserving_downsample(
                run,
                max_points=min(maximum_points, budget),
            )
            path = _trace_path(downsampled, drawing, extent)
            if polyline_length_mm(path) + 1e-9 < minimum:
                continue
            line_layer.add(
                path,
                source_ref=trace.source_ref,
                role="data-faithful-fingerprint" if fingerprint else "quantitative-trace",
                attributes={
                    "data-profile-id": trace.id,
                    "data-channel": trace.channel,
                    "data-unit": trace.unit,
                    "data-processing-status": trace.processing_status,
                    "data-downsampling": "bucket-extrema-no-interpolation-v1",
                },
            )
            rendered_points += len(downsampled)
            rendered_runs += 1
        baseline = [(drawing.left, drawing.bottom), (drawing.right, drawing.bottom)]
        if polyline_length_mm(baseline) >= 3.0 * axis.pen.mark_width_mm:
            axis.add(
                baseline,
                source_ref=trace.source_ref,
                role="distance-axis",
                attributes={"data-profile-id": trace.id, "data-axis-unit": "m"},
            )
        minimum_x, minimum_y, maximum_x, maximum_y = extent
        linked_count = 0
        if link_layer is not None:
            link_minimum = 3.0 * link_layer.pen.mark_width_mm
            for station in linked_stations:
                distance_m = float(station["distance_m"])
                if not minimum_x <= distance_m <= maximum_x:
                    continue
                x = drawing.left + drawing.width * (
                    distance_m - minimum_x
                ) / max(maximum_x - minimum_x, 1e-9)
                tick_height = max(link_minimum * 1.05, drawing.height / 5.0)
                link_layer.add(
                    [
                        (x, drawing.bottom),
                        (x, max(drawing.top, drawing.bottom - tick_height)),
                    ],
                    source_ref=str(station["source_ref"]),
                    role="route-profile-link",
                    attributes={
                        "data-marker-id": str(station["id"]),
                        "data-profile-id": trace.id,
                        "data-distance-m": f"{distance_m:g}",
                    },
                )
                linked_count += 1
        status = (
            "RAW"
            if trace.processing_status == "raw"
            else trace.processing_status.replace("-", " ").upper()
        )
        caption = (
            f"{trace.label} / {minimum_y:g}-{maximum_y:g} {trace.unit} / {status}"
        )
        add_text(
            labels,
            caption,
            x_mm=trace_panel.left,
            y_mm=trace_panel.top,
            preferred_cap_mm=cap,
            maximum_width_mm=trace_panel.width,
            allow_horizontal_condense=True,
            source_ref=trace.source_ref,
            role="profile-caption",
            attributes={
                "data-profile-id": trace.id,
                "data-distance-min-m": f"{minimum_x:g}",
                "data-distance-max-m": f"{maximum_x:g}",
                "data-value-min": f"{minimum_y:g}",
                "data-value-max": f"{maximum_y:g}",
            },
        )
        results.append(
            {
                **trace.as_dict(),
                "rendered_run_count": rendered_runs,
                "rendered_point_count": rendered_points,
                "linked_station_count": linked_count,
                "downsampling_policy_id": "bucket-extrema-no-interpolation-v1",
                "global_extrema_preserved": True,
                "missing_values_interpolated": False,
            }
        )
    return results


def _performance_value(record: Mapping[str, Any], key: str) -> str | None:
    participant = record.get("participant") or {}
    if isinstance(participant, dict) and participant.get(key) is not None:
        return str(participant[key])
    event = record.get("event") or {}
    if isinstance(event, dict) and event.get(key) is not None:
        return str(event[key])
    return None


def _render_performance_band(
    artwork: PlateArtwork,
    record: Mapping[str, Any],
    panel: Rect,
    *,
    matchday: bool = False,
) -> dict[str, Any]:
    layer = _layer(
        artwork,
        "accents_performance",
        "Performance or occasion statement",
        ink="Black",
        role="primary",
    )
    if matchday:
        occasion = record.get("occasion") or {}
        if not isinstance(occasion, dict):
            occasion = {}
        score = str(occasion.get("score") or "MATCHDAY")
        statement = score
        source_ref = str(record["venue"]["components"][0]["source_ref"])
    else:
        finish_time = _performance_value(record, "finish_time")
        personal_best = _performance_value(record, "personal_best")
        if finish_time:
            statement = finish_time
        elif personal_best and personal_best.casefold() not in {"true", "yes"}:
            statement = personal_best
        else:
            statement = "PERSONAL BEST" if personal_best else "FINISH"
        source_ref = str(record["route"]["segments"][0]["source_ref"])
    preferred = float(artwork.context.plate["type_scale_mm"]["title"])
    add_text(
        layer,
        statement.upper(),
        x_mm=panel.centre[0],
        y_mm=panel.y + max((panel.height - preferred) / 2.0, 0.0),
        preferred_cap_mm=preferred,
        maximum_width_mm=panel.width,
        anchor="middle",
        allow_horizontal_condense=True,
        source_ref=source_ref,
        role="occasion-statement" if matchday else "performance-statement",
        attributes={"data-supplied-value": statement},
    )
    return {
        "statement": statement,
        "kind": "matchday" if matchday else "performance",
        "supplied": statement not in {"MATCHDAY", "FINISH"},
    }


def _distance_claim(
    record: Mapping[str, Any], measured_length_m: float | None = None
) -> dict[str, Any]:
    event = record.get("event") or {}
    route = record.get("route") or {}
    candidates: list[Any] = []
    if isinstance(event, dict):
        candidates.extend(
            value
            for value in (event.get("official_distance"), event.get("distance"))
            if value is not None
        )
    if isinstance(route, dict) and route.get("official_distance") is not None:
        candidates.append(route["official_distance"])
    for candidate in candidates:
        if isinstance(candidate, str):
            return {
                "display": candidate,
                "status": "authoritative-supplied-label",
                "measured_track_m": measured_length_m,
            }
        if not isinstance(candidate, dict):
            _fail("official distance must be text or an object.")
        value = _number(candidate.get("value"), "official_distance.value")
        unit = _text(candidate.get("unit"), "official_distance.unit")
        source_ref = candidate.get("source_ref")
        display = str(candidate.get("label") or f"{value:g} {unit}")
        return {
            "display": display,
            "value": value,
            "unit": unit,
            "source_ref": source_ref,
            "status": str(candidate.get("status") or "official-supplied"),
            "measured_track_m": measured_length_m,
            "display_basis": "authoritative-metadata-not-gps-measurement",
        }
    if measured_length_m is None:
        return {"display": None, "status": "not-supplied"}
    return {
        "display": f"{measured_length_m / 1000.0:.2f} KM MEASURED",
        "value": round(measured_length_m / 1000.0, 3),
        "unit": "km",
        "status": "measured-retained-source-geometry",
        "measured_track_m": measured_length_m,
        "display_basis": "measured-track-no-authoritative-distance-supplied",
    }


def _discipline_distance_claims(
    record: Mapping[str, Any], processed: RouteProcessingResult
) -> list[dict[str, Any]]:
    event = record.get("event") or {}
    supplied = (
        event.get("discipline_distances") if isinstance(event, dict) else None
    )
    ordered_disciplines = list(
        dict.fromkeys(segment.discipline for segment in processed.segments)
    )
    if isinstance(supplied, dict):
        supplied_by_discipline = {
            str(discipline).casefold(): value
            for discipline, value in supplied.items()
        }
        ordered_disciplines.extend(
            discipline
            for discipline in supplied_by_discipline
            if discipline not in ordered_disciplines
        )
        results: list[dict[str, Any]] = []
        for discipline in ordered_disciplines:
            candidate = supplied_by_discipline.get(discipline)
            if candidate is None:
                continue
            value = _object(
                candidate, f"event.discipline_distances.{discipline}"
            )
            number = _number(
                value.get("value"),
                f"event.discipline_distances.{discipline}.value",
            )
            unit = _text(
                value.get("unit"),
                f"event.discipline_distances.{discipline}.unit",
            )
            results.append(
                {
                    "discipline": discipline,
                    "display": str(value.get("label") or f"{number:g} {unit}"),
                    "value": number,
                    "unit": unit,
                    "source_ref": str(value["source_ref"]),
                    "status": str(value.get("status") or "official-supplied"),
                    "display_basis": "authoritative-discipline-metadata",
                }
            )
        return results
    measured: dict[str, float] = {}
    source_refs: dict[str, list[str]] = {}
    for segment, report in zip(processed.segments, processed.reports):
        measured[segment.discipline] = (
            measured.get(segment.discipline, 0.0) + report.measured_length_m
        )
        source_refs.setdefault(segment.discipline, []).append(segment.source_ref)
    return [
        {
            "discipline": discipline,
            "display": f"{measured[discipline] / 1000.0:.2f} KM MEASURED",
            "value": round(measured[discipline] / 1000.0, 3),
            "unit": "km",
            "source_refs": list(dict.fromkeys(source_refs[discipline])),
            "status": "measured-retained-source-geometry",
            "display_basis": "measured-track-no-authoritative-discipline-distance-supplied",
        }
        for discipline in ordered_disciplines
    ]


def _route_hero_points(
    processed: RouteProcessingResult, record: Mapping[str, Any], preset: SportsPreset
) -> list[tuple[float, float]]:
    points = [point.coordinates for segment in processed.segments for point in segment.points]
    route = record.get("route") or {}
    if isinstance(route, dict):
        for name in ("start", "finish"):
            control = route.get(name)
            if isinstance(control, dict):
                control = control.get("point", control.get("coordinates"))
            if isinstance(control, (list, tuple)) and len(control) >= 2:
                points.append((float(control[0]), float(control[1])))
    points.extend(
        (float(point[0]), float(point[1]))
        for point in _context_bounds_points(record, preset)
        if len(point) >= 2
    )
    return points


def _resolved_route_control(
    route: Mapping[str, Any],
    name: str,
    processed: RouteProcessingResult,
    source_ids: set[str],
) -> dict[str, Any]:
    is_start = name == "start"
    segment = processed.segments[0] if is_start else processed.segments[-1]
    endpoint = segment.points[0] if is_start else segment.points[-1]
    raw = route.get(name)
    if raw is None:
        return {
            "point": endpoint.coordinates,
            "source_ref": segment.source_ref,
            "status": "retained-route-endpoint",
        }
    if isinstance(raw, dict):
        point = _marker_point(raw, f"route.{name}")
        source_ref = _validate_source_ref(
            raw.get("source_ref"), f"route.{name}.source_ref", source_ids
        )
        status = str(raw.get("status") or "supplied-control")
    else:
        point = _marker_point({"point": raw}, f"route.{name}")
        source_ref = segment.source_ref
        status = "supplied-control-inherits-route-source"
    return {
        "point": point,
        "source_ref": source_ref,
        "status": status,
    }


def _split_for_profile(
    context: PlateContext,
) -> tuple[Rect, Rect]:
    gap = float(context.plate["gap_mm"])
    inner = context.field.inset(gap)
    band = min(context.zones["detail"].height, inner.height - gap)
    hero = Rect(inner.x, inner.y, inner.width, inner.height - band - gap)
    return hero, Rect(inner.x, hero.bottom + gap, inner.width, band)


def _compose_route(
    artwork: PlateArtwork,
    record: Mapping[str, Any],
    preset: SportsPreset,
    sources: Sequence[Mapping[str, Any]],
) -> None:
    source_ids = _source_ids(sources)
    route = dict(record["route"])
    processed = process_route(route, source_ids=source_ids)
    markers = _route_markers(route, source_ids)
    start_control = _resolved_route_control(
        route, "start", processed, source_ids
    )
    finish_control = _resolved_route_control(
        route, "finish", processed, source_ids
    )
    for index, (first, second) in enumerate(
        zip(processed.segments, processed.segments[1:]), start=1
    ):
        if first.discipline == second.discipline:
            continue
        markers.append(
            {
                "id": f"transition-{index}",
                "kind": "transition",
                "label": str(second.attributes.get("transition_label") or "TRANSITION"),
                "point": second.points[0].coordinates,
                "source_ref": second.source_ref,
                "segment_id": second.id,
                "vertex_index": 0,
            }
        )
    available_traces = _available_traces(record, processed)
    selected_traces = _trace_selection(record, preset, available_traces)
    hero_panel, secondary_panel = _field_panels(artwork.context, preset)
    if preset.id == "circuit-blueprint" and not selected_traces:
        hero_panel = artwork.context.field.inset(float(artwork.context.plate["gap_mm"]))
        secondary_panel = None
    if selected_traces and secondary_panel is None:
        hero_panel, secondary_panel = _split_for_profile(artwork.context)
    gap = float(artwork.context.plate["gap_mm"])
    hero_drawing = hero_panel.inset(gap / 2.0)
    transform = _coordinate_transform(
        _route_hero_points(processed, record, preset),
        hero_drawing,
        coordinate_space=processed.coordinate_space,
        scale_m_per_unit=processed.scale_m_per_unit,
    )
    tolerance = _number(route.get("simplify_mm", 0.04), "route.simplify_mm")
    if tolerance < 0.0 or tolerance > 1.0:
        _fail("route.simplify_mm must be within 0..1 physical millimetres.")
    simplifications: list[PhysicalSimplification] = []
    projected_lines: list[Stroke] = []
    protected_controls = [
        {
            "point": start_control["point"],
            "segment_id": processed.segments[0].id,
        },
        {
            "point": finish_control["point"],
            "segment_id": processed.segments[-1].id,
        },
    ]
    for segment in processed.segments:
        raw = transform.points(point.coordinates for point in segment.points)
        simplification = simplify_physical_route(
            raw,
            tolerance_mm=tolerance,
            explicit_protected_indices=_nearest_vertex_indices(
                segment, [*markers, *protected_controls]
            ),
            coincidence_tolerance_mm=max(
                0.4,
                float(artwork.context.plate["race_course"]["target_width_mm"]) / 2.0,
            ),
        )
        simplifications.append(simplification)
        projected_lines.append(list(simplification.points))
    context_report = _render_context(
        artwork,
        record,
        preset,
        transform,
        hero_drawing,
        projected_lines,
    )
    requested_style = str(route.get("render_style") or preset.route_style or "single-line")
    if requested_style not in ROUTE_STYLES:
        _fail(
            f"route.render_style {requested_style!r} is unsupported; choose "
            + ", ".join(sorted(ROUTE_STYLES))
            + "."
        )
    target_width = float(artwork.context.plate["race_course"]["target_width_mm"])
    coincident_groups = _coincident_segment_groups(
        projected_lines, max(target_width / 2.0, 0.4)
    )
    forced_offsets: dict[int, float] = {}
    for group in coincident_groups:
        count = len(group)
        pitch = min(target_width / max(count, 1), 0.4 * 0.85)
        centre = (count - 1) / 2.0
        for position, segment_index in enumerate(group):
            forced_offsets[segment_index] = (position - centre) * pitch
    route_path_count = 0
    route_layers: set[str] = set()
    out_and_back_segments: list[str] = []
    display_lines: list[Stroke] = []
    for segment_index, (segment, line, simplification) in enumerate(
        zip(processed.segments, projected_lines, simplifications)
    ):
        ink = DISCIPLINE_INKS.get(segment.discipline, "Red")
        plan = _route_pen_plan(artwork.context, ink)
        safe_discipline = re.sub(r"[^a-z0-9]+", "_", segment.discipline).strip("_") or "route"
        layer_id = f"route_{safe_discipline}"
        layer = artwork.layer(
            layer_id,
            f"{segment.discipline.replace('_', ' ').title()} hero route",
            str(plan.pen.identity),
        )
        route_layers.add(layer_id)
        base_attributes = {
            "data-segment-id": segment.id,
            "data-discipline": segment.discipline,
            "data-route-style": requested_style,
            "data-simplify-mm": f"{tolerance:g}",
            "data-source-point-count": str(len(segment.points)),
            "data-rendered-vertex-count": str(len(simplification.points)),
            "data-route-width-mm": f"{target_width:g}",
            "data-wide-mark-construction": "parallel-physical-strokes",
        }
        if segment.lap is not None:
            base_attributes["data-lap"] = str(segment.lap)
        split = _out_and_back_split(line, max(target_width, 0.4))
        forced = forced_offsets.get(segment_index, 0.0)
        if split is not None:
            out_and_back_segments.append(segment.id)
            halves = (line[: split + 1], line[split:])
            lane_offset = max(plan.pen.mark_width_mm * 0.5, target_width / 4.0)
            for half_index, half in enumerate(halves):
                route_path_count += _render_route_style(
                    layer,
                    half,
                    style="single-line",
                    plan=plan,
                    drawing=hero_panel,
                    source_ref=segment.source_ref,
                    role="outbound" if half_index == 0 else "return",
                    attributes={
                        **base_attributes,
                        "data-overlap-treatment": "opposed-parallel-lanes",
                    },
                    forced_offset_mm=forced + (-lane_offset if half_index == 0 else lane_offset),
                )
                display_lines.append(list(half))
        else:
            route_path_count += _render_route_style(
                layer,
                line,
                style=requested_style,
                plan=plan,
                drawing=hero_panel,
                source_ref=segment.source_ref,
                role=segment.role,
                attributes=base_attributes,
                forced_offset_mm=forced,
            )
            display_lines.append(line)
    if route_path_count <= 0:
        _fail("route hero fell below the physical minimum after projection.")
    marker_report = _render_markers(
        artwork,
        markers,
        transform,
        display_lines,
        start_control=start_control,
        finish_control=finish_control,
    )
    direction_report = _render_direction_marks(
        artwork,
        route,
        display_lines,
        processed.segments[0].source_ref,
    )
    trace_report: list[dict[str, Any]] = []
    if selected_traces:
        if secondary_panel is None:
            _fail("selected quantitative traces have no binding field panel.")
        trace_report = _render_traces(
            artwork,
            selected_traces,
            secondary_panel,
            fingerprint=preset.id == "route-fingerprint",
            stations=markers,
        )
    performance_report = None
    if preset.id == "personal-best":
        if secondary_panel is None:
            _fail("personal-best has no binding statement panel.")
        performance_report = _render_performance_band(
            artwork, record, secondary_panel
        )
    distance = _distance_claim(record, processed.measured_length_m)
    discipline_distances = (
        _discipline_distance_claims(record, processed)
        if preset.id == "multi-discipline"
        else []
    )
    artwork.rendering_metadata.update(
        {
            "sports_preset": preset.as_dict(),
            "hero_subject": "factual-route-geometry",
            "route_processing": processed.as_dict(),
            "route_simplification": [
                {
                    "segment_id": segment.id,
                    **simplification.as_dict(),
                    "input_point_count": len(segment.points),
                }
                for segment, simplification in zip(
                    processed.segments, simplifications
                )
            ],
            "route_representation": {
                "style": requested_style,
                "logical_layers": sorted(route_layers),
                "physical_path_count": route_path_count,
                "target_width_mm": target_width,
                "digitally_filled": False,
                "wide_mark_method": "edge-or-parallel-physical-strokes",
            },
            "overlap_handling": {
                "policy_id": "coincident-route-parallel-lanes-v1",
                "out_and_back_segments": out_and_back_segments,
                "coincident_segment_groups": [
                    [processed.segments[index].id for index in group]
                    for group in coincident_groups
                ],
            },
            "direction_marks": direction_report,
            "markers": marker_report,
            "context_selection": context_report,
            "quantitative_profiles": trace_report,
            "performance_statement": performance_report,
            "distance_claim": distance,
            "discipline_distance_claims": discipline_distances,
            "geometry_transform": transform.as_dict(),
            "quantitative_integrity": {
                "units_preserved": True,
                "missing_values_interpolated": False,
                "unsupported_peaks_invented": False,
                "downsampling_preserves_extrema": True,
            },
        }
    )


def _venue_component_spec(kind: str) -> tuple[str, str, str, str]:
    if kind in {"envelope", "footprint", "facade"}:
        return ("venue_envelope", "Stadium envelope and facade", "Black", "primary")
    if kind in {"pitch", "playing-surface", "pitch-marking"}:
        return ("venue_pitch", "Pitch and supplied markings", "Green", "text")
    if kind in {"seating-bowl", "stand", "terrace"}:
        return ("venue_seating", "Seating bowl and principal stands", "Blue", "hairline")
    if kind in {"roof", "roof-truss", "structure"}:
        return ("venue_structure", "Roof and structural lines", "Black", "hairline")
    if kind in {"concourse", "access", "street-context"}:
        return ("venue_access", "Concourses and restrained access context", "Grey", "hairline")
    return ("venue_detail", "Supplied venue detail", "Grey", "hairline")


def _polygon_hatches(
    polygon_points: Sequence[Point], *, spacing_mm: float, minimum_length_mm: float
) -> list[Stroke]:
    if len(polygon_points) < 4 or polygon_points[0] != polygon_points[-1]:
        return []
    try:
        polygon = Polygon(polygon_points)
    except GEOSException:
        return []
    if polygon.is_empty or not polygon.is_valid or polygon.area <= 0.0:
        return []
    minimum_x, minimum_y, maximum_x, maximum_y = polygon.bounds
    result: list[Stroke] = []
    y = minimum_y + spacing_mm
    while y < maximum_y - spacing_mm / 2.0:
        candidate = LineString([(minimum_x, y), (maximum_x, y)])
        try:
            clipped = candidate.intersection(polygon)
        except GEOSException:
            break
        for part in _line_parts(clipped):
            stroke = [(float(x), float(value_y)) for x, value_y in part.coords]
            if polyline_length_mm(stroke) + 1e-9 >= minimum_length_mm:
                result.append(stroke)
        y += spacing_mm
    return result


def _compose_venue(
    artwork: PlateArtwork,
    record: Mapping[str, Any],
    preset: SportsPreset,
) -> None:
    venue = dict(record["venue"])
    hero_panel, secondary_panel = _field_panels(artwork.context, preset)
    gap = float(artwork.context.plate["gap_mm"])
    drawing = hero_panel.inset(gap / 2.0)
    components = [dict(component) for component in venue["components"]]
    all_points = [
        point
        for index, component in enumerate(components)
        for point in _feature_source_points(component, f"venue.components[{index}]")
    ]
    coordinate_space = str(venue.get("coordinate_space", "local")).casefold()
    if coordinate_space not in {"local", "wgs84"}:
        _fail("venue.coordinate_space must be local or wgs84.")
    scale_m_per_unit = _number(
        venue.get("scale_m_per_unit", 1.0), "venue.scale_m_per_unit"
    )
    if scale_m_per_unit <= 0.0:
        _fail("venue.scale_m_per_unit must be positive.")
    transform = _coordinate_transform(
        all_points,
        drawing,
        coordinate_space=coordinate_space,
        scale_m_per_unit=scale_m_per_unit,
    )
    component_counts: dict[str, int] = {}
    hatch_count = 0
    label_count = 0
    annotations = _layer(
        artwork,
        "venue_annotations",
        "Stand and venue annotations",
        ink="Black",
        role="hairline",
    )
    annotation_cap = float(
        artwork.context.plate["type_scale_mm"]["attribution"]
    )
    for index, component in enumerate(components):
        kind = str(component["kind"])
        layer_id, label, ink, role = _venue_component_spec(kind)
        layer = _layer(artwork, layer_id, label, ink=ink, role=role)
        minimum = 3.0 * layer.pen.mark_width_mm
        projected_paths: list[Stroke] = []
        for path in _geometry_paths(component, f"venue.components[{index}]"):
            projected = transform.points(path)
            for clipped in _clip_stroke(projected, hero_panel):
                if polyline_length_mm(clipped) + 1e-9 < minimum:
                    continue
                layer.add(
                    clipped,
                    source_ref=str(component["source_ref"]),
                    role=kind,
                    attributes={
                        "data-component-id": str(component["id"]),
                        "data-venue-kind": kind,
                        "data-source-level": str(venue["source_level"]),
                        "data-digitally-filled": "false",
                    },
                )
                projected_paths.append(clipped)
                component_counts[kind] = component_counts.get(kind, 0) + 1
        texture = component.get("texture")
        if texture == "hatch" and kind in {"seating-bowl", "stand", "terrace"}:
            spacing = max(gap, layer.pen.mark_width_mm * 8.0)
            for projected in projected_paths:
                for hatch in _polygon_hatches(
                    projected,
                    spacing_mm=spacing,
                    minimum_length_mm=minimum,
                ):
                    layer.add(
                        hatch,
                        source_ref=str(component["source_ref"]),
                        role="plot-efficient-seating-hatch",
                        attributes={
                            "data-component-id": str(component["id"]),
                            "data-texture": "open-line-hatch",
                        },
                    )
                    hatch_count += 1
        component_label = component.get("label")
        if component_label and projected_paths:
            values = [point for path in projected_paths for point in path]
            label_x = sum(point[0] for point in values) / len(values)
            label_y = sum(point[1] for point in values) / len(values)
            maximum_width = max(
                annotation_cap * 4.0,
                min(drawing.width / 3.0, drawing.right - label_x),
            )
            if maximum_width >= annotation_cap * 4.0:
                add_text(
                    annotations,
                    str(component_label).upper(),
                    x_mm=label_x,
                    y_mm=min(max(label_y, drawing.top), drawing.bottom - annotation_cap),
                    preferred_cap_mm=annotation_cap,
                    maximum_width_mm=maximum_width,
                    anchor="middle",
                    allow_horizontal_condense=True,
                    source_ref=str(component["source_ref"]),
                    role="stand-label",
                    attributes={"data-component-id": str(component["id"])},
                )
                label_count += 1
    if not component_counts:
        _fail("venue hero fell below the physical minimum after projection.")
    occasion_report = None
    if preset.id == "matchday-memory":
        if secondary_panel is None:
            _fail("matchday-memory has no binding occasion panel.")
        occasion_report = _render_performance_band(
            artwork,
            record,
            secondary_panel,
            matchday=True,
        )
    artwork.rendering_metadata.update(
        {
            "sports_preset": preset.as_dict(),
            "hero_subject": "supplied-stadium-architecture",
            "venue_representation": {
                "source_level": venue["source_level"],
                "component_count": len(components),
                "rendered_path_counts_by_kind": component_counts,
                "stand_label_count": label_count,
                "open_hatch_path_count": hatch_count,
                "digitally_filled_pitch": False,
                "digitally_filled_structure": False,
                "generic_unverified_structure_invented": False,
                "logos_or_protected_marks_used": False,
            },
            "occasion_statement": occasion_report,
            "geometry_transform": transform.as_dict(),
        }
    )


@dataclass(frozen=True, slots=True)
class _SeriesPath:
    points: tuple[Point, ...]
    source_ref: str
    role: str


@dataclass(frozen=True, slots=True)
class _SeriesItemGeometry:
    id: str
    label: str
    kind: str
    paths: tuple[_SeriesPath, ...]
    bounds: tuple[float, float, float, float]
    coordinate_space: str

    @property
    def width(self) -> float:
        return max(self.bounds[2] - self.bounds[0], 1e-6)

    @property
    def height(self) -> float:
        return max(self.bounds[3] - self.bounds[1], 1e-6)


def _model_paths(
    paths: Sequence[tuple[Sequence[Sequence[float]], str, str]],
    *,
    coordinate_space: str,
    scale_m_per_unit: float,
) -> tuple[tuple[_SeriesPath, ...], tuple[float, float, float, float]]:
    points = [point for path, _source_ref, _role in paths for point in path]
    if len(points) < 2:
        _fail("series item has no two-point hero geometry.")
    mean_latitude = (
        sum(float(point[1]) for point in points) / len(points)
        if coordinate_space == "wgs84"
        else 0.0
    )
    reference = float(points[0][0]) if coordinate_space == "wgs84" else 0.0
    provisional = _CoordinateTransform(
        coordinate_space=coordinate_space,
        scale_m_per_unit=scale_m_per_unit,
        mean_latitude_deg=mean_latitude,
        longitude_reference_deg=reference,
        model_bounds=(0.0, 0.0, 1.0, 1.0),
        drawing=Rect(0.0, 0.0, 1.0, 1.0),
        scale_mm_per_model_unit=1.0,
    )
    model_paths = tuple(
        _SeriesPath(
            points=tuple(provisional._model_point(point) for point in path),
            source_ref=source_ref,
            role=role,
        )
        for path, source_ref, role in paths
        if len(path) >= 2
    )
    model_points = [point for path in model_paths for point in path.points]
    xs = [point[0] for point in model_points]
    ys = [point[1] for point in model_points]
    return model_paths, (min(xs), min(ys), max(xs), max(ys))


def _series_item_geometry(
    item: Mapping[str, Any], source_ids: set[str]
) -> _SeriesItemGeometry:
    raw_paths: list[tuple[Sequence[Sequence[float]], str, str]]
    if isinstance(item.get("route"), dict):
        processed = process_route(item["route"], source_ids=source_ids)
        raw_paths = [
            (
                [point.coordinates for point in segment.points],
                segment.source_ref,
                segment.discipline,
            )
            for segment in processed.segments
        ]
        model_paths, bounds = _model_paths(
            raw_paths,
            coordinate_space=processed.coordinate_space,
            scale_m_per_unit=processed.scale_m_per_unit,
        )
        kind = "route"
        coordinate_space = processed.coordinate_space
    else:
        venue = dict(item["venue"])
        coordinate_space = str(venue.get("coordinate_space", "local"))
        scale = float(venue.get("scale_m_per_unit", 1.0))
        raw_paths = [
            (path, str(component["source_ref"]), str(component["kind"]))
            for index, component in enumerate(venue["components"])
            for path in _geometry_paths(component, f"series venue component {index}")
        ]
        model_paths, bounds = _model_paths(
            raw_paths,
            coordinate_space=coordinate_space,
            scale_m_per_unit=scale,
        )
        kind = "venue"
    return _SeriesItemGeometry(
        id=str(item["id"]),
        label=str(item["label"]),
        kind=kind,
        paths=model_paths,
        bounds=bounds,
        coordinate_space=coordinate_space,
    )


def _grid_cells(rect: Rect, count: int, gap: float) -> list[Rect]:
    aspect = rect.width / max(rect.height, 1e-9)
    columns = max(1, math.ceil(math.sqrt(count * aspect)))
    rows = math.ceil(count / columns)
    width = (rect.width - gap * (columns - 1)) / columns
    height = (rect.height - gap * (rows - 1)) / rows
    if min(width, height) <= 0.0:
        _fail("series grid cannot fit the selected plate.")
    return [
        Rect(
            rect.x + (index % columns) * (width + gap),
            rect.y + (index // columns) * (height + gap),
            width,
            height,
        )
        for index in range(count)
    ]


def _timeline_cells(rect: Rect, count: int, gap: float) -> list[Rect]:
    horizontal = rect.width >= rect.height
    if horizontal:
        width = (rect.width - gap * (count - 1)) / count
        if width <= 0.0:
            _fail("series timeline cannot fit the selected plate.")
        return [
            Rect(rect.x + index * (width + gap), rect.y, width, rect.height)
            for index in range(count)
        ]
    height = (rect.height - gap * (count - 1)) / count
    if height <= 0.0:
        _fail("series timeline cannot fit the selected plate.")
    return [
        Rect(rect.x, rect.y + index * (height + gap), rect.width, height)
        for index in range(count)
    ]


def _radial_cells(rect: Rect, count: int, gap: float) -> list[Rect]:
    side = min(rect.width, rect.height) / (math.ceil(math.sqrt(count)) + 1)
    side = max(side - gap, gap * 2.0)
    radius = max(0.0, (min(rect.width, rect.height) - side) / 2.0)
    centre_x, centre_y = rect.centre
    return [
        Rect(
            centre_x
            + math.cos(2.0 * math.pi * index / count - math.pi / 2.0) * radius
            - side / 2.0,
            centre_y
            + math.sin(2.0 * math.pi * index / count - math.pi / 2.0) * radius
            - side / 2.0,
            side,
            side,
        )
        for index in range(count)
    ]


def _fit_series_path(
    path: Sequence[Point],
    bounds: tuple[float, float, float, float],
    drawing: Rect,
    *,
    common_span: tuple[float, float] | None,
) -> Stroke:
    minimum_x, minimum_y, maximum_x, maximum_y = bounds
    width = max(maximum_x - minimum_x, 1e-6)
    height = max(maximum_y - minimum_y, 1e-6)
    fit_width, fit_height = common_span or (width, height)
    scale = min(drawing.width / fit_width, drawing.height / fit_height)
    used_width = width * scale
    used_height = height * scale
    offset_x = drawing.x + (drawing.width - used_width) / 2.0
    offset_y = drawing.y + (drawing.height - used_height) / 2.0
    return [
        (
            offset_x + (point[0] - minimum_x) * scale,
            offset_y + (maximum_y - point[1]) * scale,
        )
        for point in path
    ]


def _compose_series(
    artwork: PlateArtwork,
    record: Mapping[str, Any],
    preset: SportsPreset,
    sources: Sequence[Mapping[str, Any]],
) -> None:
    series = dict(record["series"])
    source_ids = _source_ids(sources)
    items = [
        _series_item_geometry(dict(item), source_ids)
        for item in series["items"]
    ]
    arrangement = str(series.get("arrangement", "grid"))
    scale_policy = str(series.get("scale_policy", "common"))
    gap = float(artwork.context.plate["gap_mm"])
    inner = artwork.context.field.inset(gap)
    cap = float(artwork.context.plate["type_scale_mm"]["attribution"])
    label_gap = 3.0 * PENS_BY_ID["black-0-25"].mark_width_mm
    if arrangement == "grid":
        cells = _grid_cells(inner, len(items), gap)
    elif arrangement == "timeline":
        cells = _timeline_cells(inner, len(items), gap)
    elif arrangement == "radial":
        cells = _radial_cells(inner, len(items), gap)
    else:
        _fail(f"unsupported series arrangement {arrangement!r}.")
    maximum_width = max(item.width for item in items)
    maximum_height = max(item.height for item in items)
    common_span = (
        (maximum_width, maximum_height) if scale_policy == "common" else None
    )
    route_layer = artwork.layer(
        "series_routes",
        "Series routes and circuits",
        _pen_id("Red", 0.4),
    )
    venue_layer = artwork.layer(
        "series_venues",
        "Series stadium and venue plans",
        _semantic_pen_id(artwork.context, "Black", "text"),
    )
    copy_layer = _layer(
        artwork,
        "series_labels",
        "Series item labels and scale disclosure",
        ink="Black",
        role="hairline",
    )
    rendered_by_item: dict[str, int] = {}
    for index, (item, cell) in enumerate(zip(items, cells)):
        label_height = cap + label_gap
        if cell.height <= label_height + gap:
            _fail("series item cell is too small for physical label copy.")
        drawing = Rect(
            cell.x,
            cell.y,
            cell.width,
            cell.height - label_height,
        ).inset(min(gap / 2.0, cell.width / 6.0, cell.height / 6.0))
        item_count = 0
        for path in item.paths:
            physical = _fit_series_path(
                path.points,
                item.bounds,
                drawing,
                common_span=common_span,
            )
            target = route_layer if item.kind == "route" else venue_layer
            minimum = 3.0 * target.pen.mark_width_mm
            for clipped in _clip_stroke(physical, inner):
                if polyline_length_mm(clipped) + 1e-9 < minimum:
                    continue
                target.add(
                    clipped,
                    source_ref=path.source_ref,
                    role=path.role,
                    attributes={
                        "data-series-item-id": item.id,
                        "data-series-position": str(index + 1),
                        "data-scale-policy": scale_policy,
                    },
                )
                item_count += 1
        if item_count <= 0:
            _fail(f"series item {item.id!r} fell below the physical minimum.")
        rendered_by_item[item.id] = item_count
        label_x = cell.centre[0]
        label_y = cell.bottom - cap
        anchor = "middle"
        maximum_label_width = cell.width
        add_text(
            copy_layer,
            item.label.upper(),
            x_mm=label_x,
            y_mm=label_y,
            preferred_cap_mm=cap,
            maximum_width_mm=maximum_label_width,
            anchor=anchor,
            allow_horizontal_condense=True,
            source_ref=item.paths[0].source_ref,
            role="series-item-label",
            attributes={"data-series-item-id": item.id},
        )
    if scale_policy == "independent-labelled":
        add_text(
            copy_layer,
            "SCALE VARIES BY ITEM",
            x_mm=inner.right,
            y_mm=inner.bottom - cap,
            preferred_cap_mm=cap,
            maximum_width_mm=inner.width,
            anchor="end",
            allow_horizontal_condense=True,
            source_ref=items[0].paths[0].source_ref,
            role="scale-disclosure",
            attributes={"data-scale-policy": scale_policy},
        )
    artwork.rendering_metadata.update(
        {
            "sports_preset": preset.as_dict(),
            "hero_subject": "multi-item-sports-collection",
            "series_composition": {
                "arrangement": arrangement,
                "scale_policy": scale_policy,
                "scale_varies_visibly_disclosed": scale_policy
                == "independent-labelled",
                "item_count": len(items),
                "rendered_path_counts": rendered_by_item,
                "common_scale_span_model_units": (
                    [maximum_width, maximum_height]
                    if scale_policy == "common"
                    else None
                ),
            },
        }
    )


def _record_title(record: Mapping[str, Any], preset: SportsPreset) -> str:
    if record.get("title"):
        return _text(record["title"], "title").upper()
    event = record.get("event") or {}
    venue = record.get("venue") or {}
    series = record.get("series") or {}
    if preset.hero_kind == "venue" and isinstance(venue, dict) and venue.get("name"):
        return str(venue["name"]).upper()
    if preset.hero_kind == "series" and isinstance(series, dict) and series.get("name"):
        return str(series["name"]).upper()
    if isinstance(event, dict) and event.get("name"):
        return str(event["name"]).upper()
    _fail("record needs title, event.name, venue.name, or series.name.")


def _record_subtitle(record: Mapping[str, Any], preset: SportsPreset) -> str:
    if record.get("subtitle"):
        return _text(record["subtitle"], "subtitle").upper()
    event = record.get("event") or {}
    values: list[str] = []
    if isinstance(event, dict):
        for key in ("location", "date", "discipline"):
            if event.get(key):
                values.append(str(event[key]).upper())
    if not values and preset.hero_kind == "venue":
        venue = record.get("venue") or {}
        if isinstance(venue, dict) and venue.get("location"):
            values.append(str(venue["location"]).upper())
    values.append(preset.label.upper())
    return " / ".join(values[:3])


def _participant_detail(record: Mapping[str, Any]) -> str | None:
    participant = record.get("participant")
    if not isinstance(participant, dict):
        return None
    name = next(
        (
            participant.get(key)
            for key in ("name", "recipient_name", "driver")
            if participant.get(key)
        ),
        None,
    )
    if not name:
        return None
    return str(name).upper()


def _result_detail(record: Mapping[str, Any]) -> str | None:
    participant = record.get("participant")
    if not isinstance(participant, dict):
        return None
    values: list[str] = []
    if participant.get("finish_time"):
        values.append(str(participant["finish_time"]))
    if participant.get("lap_time"):
        values.append(str(participant["lap_time"]))
    if participant.get("personal_best"):
        values.append("PERSONAL BEST")
    if participant.get("position") is not None:
        values.append(f"POS {participant['position']}")
    return " / ".join(values[:3]) or None


def _discipline_detail(record: Mapping[str, Any], preset: SportsPreset) -> str | None:
    participant = record.get("participant") or {}
    event = record.get("event") or {}
    values: list[str] = []
    if isinstance(participant, dict):
        charity_name = participant.get("charity_name")
        charity_amount = participant.get("charity_amount")
        if charity_name:
            charity = f"FOR {charity_name}"
            if charity_amount:
                charity += f" / {charity_amount}"
            values.append(charity)
        if participant.get("crew") is not None:
            values.append(f"CREW {participant['crew']}")
        if participant.get("bib_number") is not None:
            values.append(f"BIB {participant['bib_number']}")
        if participant.get("race_number") is not None:
            values.append(f"NO {participant['race_number']}")
        for key, label in (
            ("pace", "PACE"),
            ("speed", "SPEED"),
            ("category", ""),
            ("seat", "SEAT"),
            ("boat_name", "BOAT"),
            ("yacht_name", "YACHT"),
            ("vehicle", "VEHICLE"),
        ):
            if participant.get(key) is not None:
                values.append(
                    f"{label} {participant[key]}".strip()
                )
    if isinstance(event, dict) and event.get("edition"):
        values.append(str(event["edition"]).upper())
    for container in (participant, event, record):
        if isinstance(container, dict):
            dedication = container.get("dedication", container.get("quotation"))
            if dedication:
                values.append(str(dedication).upper())
                break
    if preset.id == "topographic-challenge" and isinstance(event, dict):
        for key, label in (
            ("elevation_gain", "GAIN"),
            ("highest_point", "HIGH"),
        ):
            if event.get(key) is not None:
                values.append(f"{label} {event[key]}")
    return " / ".join(values[:3]) or None


def _venue_details(
    record: Mapping[str, Any], *, matchday: bool = False
) -> list[str]:
    venue = record.get("venue") or {}
    occasion = record.get("occasion") or {}
    venue_values: list[str] = []
    match_values: list[str] = []
    if isinstance(venue, dict):
        facts: list[str] = []
        if venue.get("capacity") is not None:
            facts.append(f"CAPACITY {venue['capacity']}")
        if venue.get("opening_date"):
            facts.append(f"OPENED {venue['opening_date']}")
        if facts:
            venue_values.append(" / ".join(facts))
        if venue.get("coordinates"):
            coordinates = venue["coordinates"]
            if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
                venue_values.append(
                    f"{float(coordinates[1]):.5f} / {float(coordinates[0]):.5f}"
                )
        supplied_facts = venue.get("facts")
        if isinstance(supplied_facts, dict):
            facts = [
                f"{str(label).upper()} {value}"
                for label, value in supplied_facts.items()
                if value is not None
            ]
            if facts:
                venue_values.append(" / ".join(facts[:3]))
        elif isinstance(supplied_facts, list):
            facts = [str(value).upper() for value in supplied_facts if value]
            if facts:
                venue_values.append(" / ".join(facts[:3]))
    if isinstance(occasion, dict):
        match: list[str] = []
        teams = occasion.get("teams")
        if isinstance(teams, list) and len(teams) >= 2:
            match.append(f"{teams[0]} V {teams[1]}")
        if occasion.get("score"):
            match.append(str(occasion["score"]))
        if occasion.get("date"):
            match.append(str(occasion["date"]))
        if occasion.get("competition"):
            match.append(str(occasion["competition"]))
        if match:
            match_values.append(" / ".join(match).upper())
        place: list[str] = []
        for key, label in (("stand", "STAND"), ("seat", "SEAT")):
            if occasion.get(key):
                place.append(f"{label} {occasion[key]}")
        if occasion.get("dedication"):
            place.append(str(occasion["dedication"]).upper())
        if place:
            match_values.append(" / ".join(place))
    return [*match_values, *venue_values] if matchday else [*venue_values, *match_values]


def _record_details(
    record: Mapping[str, Any], preset: SportsPreset
) -> tuple[str, ...]:
    explicit = record.get("details")
    if explicit is not None:
        values = _array(explicit, "details", nonempty=True)
        if len(values) > 3:
            _fail("details can contain at most three binding lines.")
        return tuple(_text(value, f"details[{index}]").upper() for index, value in enumerate(values))
    if preset.hero_kind == "venue":
        values = _venue_details(record, matchday=preset.id == "matchday-memory")
    elif preset.hero_kind == "series":
        series = dict(record["series"])
        values = [
            f"{len(series['items'])} COLLECTED EVENTS / {str(series.get('arrangement', 'grid')).upper()}",
            (
                "COMMON SCALE"
                if series.get("scale_policy", "common") == "common"
                else "SCALE VARIES / LABELLED"
            ),
        ]
    else:
        processed = process_route(
            record["route"], source_ids={str(source["id"]) for source in record["sources"]}
        )
        distance = _distance_claim(record, processed.measured_length_m)
        if preset.id == "multi-discipline":
            discipline_claims = _discipline_distance_claims(record, processed)
            discipline_line = " / ".join(
                f"{claim['discipline'].upper()} {claim['display']}"
                for claim in discipline_claims
            )
            values = [
                value
                for value in (
                    _participant_detail(record),
                    (
                        " / ".join(
                            part
                            for part in (
                                str(distance["display"])
                                if distance.get("display")
                                else None,
                                _result_detail(record),
                            )
                            if part
                        )
                        or None
                    ),
                    discipline_line or None,
                )
                if value
            ]
        elif preset.id == "personal-best":
            participant = record.get("participant") or {}
            summary = [str(distance["display"])] if distance.get("display") else []
            if isinstance(participant, dict):
                if participant.get("personal_best"):
                    summary.append("PB")
                if participant.get("position") is not None:
                    summary.append(f"POS {participant['position']}")
            values = [
                value
                for value in (
                    _participant_detail(record),
                    " / ".join(summary) or None,
                    _discipline_detail(record, preset),
                )
                if value
            ]
        else:
            values = [
                value
                for value in (
                    _participant_detail(record),
                    (
                        " / ".join(
                            part
                            for part in (
                                str(distance["display"])
                                if distance.get("display")
                                else None,
                                _result_detail(record),
                            )
                            if part
                        )
                        or None
                    ),
                    _discipline_detail(record, preset),
                )
                if value
            ]
    fallback = [preset.label.upper(), "SUPPLIED SOURCE GEOMETRY", "PEN-PLOTTED EDITION"]
    for line in fallback:
        if len(values) >= 3:
            break
        if line not in values:
            values.append(line)
    return tuple(str(value).upper() for value in values[:3])


def _credit_line(
    record: Mapping[str, Any], sources: Sequence[Mapping[str, Any]]
) -> str:
    if record.get("credit_line"):
        raw = _text(record["credit_line"], "credit_line")
        lines = [line.strip() for line in raw.split("|") if line.strip()]
        if not 1 <= len(lines) <= 2:
            _fail("credit_line must contain one or two '|' separated lines.")
        return " | ".join(lines)
    attributions = list(
        dict.fromkeys(str(source["attribution"]).strip() for source in sources)
    )
    return " | ".join(attributions[:2])


def _rights_status(record: Mapping[str, Any], sources: Sequence[Mapping[str, Any]]) -> str:
    if record.get("rights_status"):
        return _text(record["rights_status"], "rights_status")
    licences = {str(source["license"]).casefold() for source in sources}
    return (
        "project-authored"
        if licences and licences <= {"project-authored", "cc0-1.0"}
        else "review-required"
    )


def _format_id(record: Mapping[str, Any], preset: SportsPreset, override: str | None) -> str:
    if override:
        return override
    composition = record.get("composition") or {}
    if isinstance(composition, dict) and composition.get("format_id"):
        return str(composition["format_id"])
    if record.get("format_id"):
        return str(record["format_id"])
    return "a3-portrait" if preset.hero_kind in {"venue", "series"} else "a4-portrait"


def build_sports_plate(
    record: Any,
    format_id: str | None = None,
    *,
    preset_id: str | None = None,
) -> PlateArtwork:
    """Build one layered sports artwork on the binding plate/output system."""

    validated, preset, sources = validate_sports_record(record, preset_id=preset_id)
    selected_format = _format_id(validated, preset, format_id)
    context = context_for(selected_format)
    if preset.hero_kind in {"venue", "series"} and context.plate["sheet"] != "A3":
        _fail(
            f"{preset.id} is a schematic/collection subject and must use an A3 "
            "binding format under the plate subject policy."
        )
    if context.plate["sheet"] not in {"A5", "A4", "A3"}:
        _fail("sports plates require a binding A-series format.")
    title = _record_title(validated, preset)
    subtitle = _record_subtitle(validated, preset)
    details = _record_details(validated, preset)
    rights = _rights_status(validated, sources)
    evidence_status = str(
        validated.get("evidence_status")
        or (
            validated.get("route", {}).get("geometry_status")
            if preset.hero_kind == "route"
            else validated.get("venue", {}).get("source_level")
            if preset.hero_kind == "venue"
            else "source-labelled-series"
        )
    )
    snapshot = str(
        validated.get("data_snapshot")
        or next(
            (
                source.get("snapshot_date")
                for source in sources
                if source.get("snapshot_date")
            ),
            "supplied-input",
        )
    )
    artwork = PlateArtwork(
        subject_id=str(validated["id"]),
        domain="sports",
        subject_kind=SPORTS_SUBJECT_KIND,
        title=title,
        subtitle=subtitle,
        details=details,
        credit_line=_credit_line(validated, sources),
        scale_status=str(
            validated.get("scale_status")
            or "source-geometry-fit-to-binding-field"
        ),
        evidence_status=evidence_status,
        rights_status=rights,
        sources=tuple(copy.deepcopy(sources)),
        context=context,
        layers=[],
        variant_id=preset.id,
        pen_order=SPORTS_PENS,
        artifact_kind=SPORTS_ARTIFACT_KIND,
        rendering_preset=f"sports-{preset.id}-v1",
        format_subject_policy=(
            "schematic" if preset.hero_kind in {"venue", "series"} else "route_plate"
        ),
        source_provider="supplied source register",
        source_license="per-source; see sources",
        data_snapshot=snapshot,
        notes=(
            "Hero geometry is supplied and source-labelled; the sports compiler does not map-match it.",
            "Wide marks use open edges, hatching, or parallel physical pen strokes; no solid route, pitch, track, or water fill is emitted.",
            "No club crest, league mark, sponsor graphic, kit pattern, or protected logo is generated.",
        ),
        catalog_record=copy.deepcopy(validated),
        rendering_metadata={
            "sports_schema_version": SPORTS_SCHEMA_VERSION,
            "personalisation_fields_present": sorted(
                str(key) for key in (validated.get("participant") or {})
            ),
        },
    )
    if preset.hero_kind == "route":
        _compose_route(artwork, validated, preset, sources)
    elif preset.hero_kind == "venue":
        _compose_venue(artwork, validated, preset)
    else:
        _compose_series(artwork, validated, preset, sources)
    return artwork
