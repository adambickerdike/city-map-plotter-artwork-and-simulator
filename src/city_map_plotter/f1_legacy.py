"""Multi-era former-F1 catalog bridge for the shared circuit renderer."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping, NoReturn

from .f1_circuits import (
    FORMAT_IDS,
    PlateArtwork,
    _is_frozen_current_osm_grandstand,
    build_f1_plate,
    validate_f1_event,
)
from .models import MapPlotterError


LEGACY_CATALOG_PATH = (
    Path(__file__).with_name("data") / "f1-circuits-legacy-v1.json"
)
LEGACY_CATALOG_CLASS = "legacy-f1-configurations"
LEGACY_SEASON_SCOPE = "multi-era"
LEGACY_LENGTH_GATE_PERCENT = 1.0


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(message)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object.")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{label} must be an array.")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be non-empty text.")
    return value.strip()


def _reference_season(event: Mapping[str, Any], label: str) -> int:
    value = event.get("configuration_reference_season")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1950:
        _fail(f"{label}.configuration_reference_season must be an F1-era year.")
    return value


def _source_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "source_ref" or key.endswith("_source_ref"):
                if isinstance(child, str) and child.strip():
                    refs.add(child.strip())
            elif key == "source_refs" or key.endswith("_source_refs"):
                if isinstance(child, list):
                    refs.update(
                        item.strip()
                        for item in child
                        if isinstance(item, str) and item.strip()
                    )
            elif key != "tags":
                refs.update(_source_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(_source_refs(child))
    return refs


def validate_f1_legacy_catalog(value: Any) -> dict[str, Any]:
    """Validate the legacy wrapper and each event at its own reference year."""

    catalog = _object(value, "legacy catalog")
    if catalog.get("schema_version") != 1:
        _fail("legacy catalog.schema_version must be 1.")
    if catalog.get("catalog_class") != LEGACY_CATALOG_CLASS:
        _fail(
            f"legacy catalog.catalog_class must be {LEGACY_CATALOG_CLASS!r}."
        )
    if catalog.get("season_scope") != LEGACY_SEASON_SCOPE:
        _fail(f"legacy catalog.season_scope must be {LEGACY_SEASON_SCOPE!r}.")
    # Kept solely as release/freeze year for compatibility with catalog tooling.
    if catalog.get("season") != 2026:
        _fail("legacy catalog.season must remain the 2026 release/freeze year.")
    _object(catalog.get("freeze"), "legacy catalog.freeze")
    sources = _array(catalog.get("sources"), "legacy catalog.sources")
    source_registry: dict[str, Mapping[str, Any]] = {}
    for index, source_value in enumerate(sources):
        source = _object(source_value, f"legacy catalog.sources[{index}]")
        source_id = _text(source.get("id"), f"legacy catalog.sources[{index}].id")
        if source_id in source_registry:
            _fail(f"legacy catalog repeats source id {source_id!r}.")
        _text(source.get("publisher"), f"source {source_id}.publisher")
        _text(source.get("title"), f"source {source_id}.title")
        _text(source.get("url"), f"source {source_id}.url")
        _text(source.get("source_kind"), f"source {source_id}.source_kind")
        _text(source.get("licence"), f"source {source_id}.licence")
        source_registry[source_id] = source

    checked_events: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    for index, event_value in enumerate(
        _array(catalog.get("events"), "legacy catalog.events")
    ):
        event = _object(event_value, f"legacy catalog.events[{index}]")
        event_id = _text(event.get("id"), f"legacy catalog.events[{index}].id")
        if event_id in event_ids:
            _fail(f"legacy catalog repeats event id {event_id!r}.")
        event_ids.add(event_id)
        reference_season = _reference_season(event, f"event {event_id}")
        circuit = _object(event.get("circuit"), f"event {event_id}.circuit")
        if circuit.get("configuration_season") != reference_season:
            _fail(
                f"event {event_id!r} circuit.configuration_season must equal "
                "configuration_reference_season."
            )
        disclosure = _text(
            event.get("render_disclosure"), f"event {event_id}.render_disclosure"
        )
        if str(reference_season) not in disclosure:
            _fail(f"event {event_id!r} disclosure must name its reference year.")
        if not disclosure.startswith(
            ("CURRENT-SOURCE COURSE /", "HISTORIC SOURCE COURSE /")
        ) and circuit.get("geometry", {}).get("model") is not None:
            _fail(
                f"renderable event {event_id!r} must visibly disclose source "
                "temporality."
            )
        identity = _object(
            event.get("configuration_identity"),
            f"event {event_id}.configuration_identity",
        )
        if identity.get("f1_reference_season") != reference_season:
            _fail(
                f"event {event_id!r} identity reference season is inconsistent."
            )
        f1_seasons = _array(
            identity.get("f1_seasons", []),
            f"event {event_id}.configuration_identity.f1_seasons",
        )
        geometry_value = _object(
            circuit.get("geometry"), f"event {event_id}.circuit.geometry"
        )
        renderable_model = isinstance(geometry_value.get("model"), dict)
        if reference_season not in f1_seasons:
            _fail(
                f"event {event_id!r} identity seasons must include its "
                "reference season."
            )
        if renderable_model and f1_seasons != [reference_season]:
            _fail(
                f"renderable event {event_id!r} may claim only its explicitly "
                "sourced reference season."
            )
        identity_status = _text(
            identity.get("status"),
            f"event {event_id}.configuration_identity.status",
        )
        if identity_status == "current-source-f1-reference" and identity.get(
            "current_surviving_equivalent"
        ) is not False:
            _fail(
                f"event {event_id!r} current-source reference must not claim a "
                "surviving historic equivalent."
            )
        missing_refs = sorted(_source_refs(event) - set(source_registry))
        if missing_refs:
            _fail(
                f"event {event_id!r} has unresolved source refs: "
                + ", ".join(missing_refs)
                + "."
            )
        checked = validate_f1_event(
            event,
            source_registry=source_registry,
            season=reference_season,
        )
        geometry = checked["circuit"]["geometry"]
        model = geometry.get("model")
        if isinstance(model, dict):
            review = _object(geometry.get("review"), f"event {event_id}.review")
            discrepancy = review.get("length_discrepancy_percent")
            if not isinstance(discrepancy, (int, float)):
                _fail(f"renderable event {event_id!r} needs a length discrepancy.")
            if float(discrepancy) > LEGACY_LENGTH_GATE_PERCENT:
                _fail(
                    f"renderable event {event_id!r} exceeds the 1.0% length gate."
                )
            overlays = _object(
                model.get("operational_overlays"),
                f"event {event_id}.operational_overlays",
            )
            if not str(overlays.get("status", "")).startswith("withheld"):
                _fail(
                    f"event {event_id!r} operational overlays must remain withheld."
                )
            for feature in model.get("context", []):
                if feature.get("kind") == "grandstand":
                    if not _is_frozen_current_osm_grandstand(feature):
                        _fail(
                            f"event {event_id!r} grandstand must retain the "
                            "frozen current-OSM footprint-only contract."
                        )
                    continue
                if feature.get("valid_for_season") != reference_season:
                    _fail(
                        f"event {event_id!r} context must use its reference-year "
                        "renderer filter."
                    )
                if feature.get("source_temporality") != "snapshot-current-not-backdated":
                    _fail(
                        f"event {event_id!r} context must disclose current-source "
                        "temporality."
                    )
        checked_events.append(checked)

    result = copy.deepcopy(catalog)
    result["events"] = checked_events
    return result


def load_f1_legacy_catalog(path: Path | None = None) -> dict[str, Any]:
    """Load and validate the packaged former-F1 configuration catalog."""

    source = path or LEGACY_CATALOG_PATH
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise MapPlotterError(f"Could not load legacy F1 catalog {source}: {exc}") from exc
    return validate_f1_legacy_catalog(value)


def list_f1_legacy_events(
    catalog: Mapping[str, Any] | Path | None = None,
) -> list[dict[str, Any]]:
    """Return compact summaries without implying that held layouts render."""

    if catalog is None or isinstance(catalog, Path):
        checked = load_f1_legacy_catalog(catalog)
    else:
        checked = validate_f1_legacy_catalog(catalog)
    rows = []
    for event in checked["events"]:
        geometry = event["circuit"]["geometry"]
        rows.append(
            {
                "id": event["id"],
                "title": event["neutral_display_title"],
                "configuration_reference_season": event[
                    "configuration_reference_season"
                ],
                "identity_status": event["configuration_identity"]["status"],
                "geometry_status": geometry["status"],
                "renderable": isinstance(geometry.get("model"), dict),
                "render_disclosure": event["render_disclosure"],
                "formats": list(FORMAT_IDS),
            }
        )
    return rows


def build_f1_legacy_plate(
    event_id: str,
    format_id: str = "a4-landscape",
    *,
    catalog: Mapping[str, Any] | Path | None = None,
    context_mode: str | None = None,
) -> PlateArtwork:
    """Render one legacy record through the shared F1 plate implementation."""

    if catalog is None or isinstance(catalog, Path):
        checked = load_f1_legacy_catalog(catalog)
    else:
        checked = validate_f1_legacy_catalog(catalog)
    event = next(
        (value for value in checked["events"] if value["id"] == event_id), None
    )
    if event is None:
        raise MapPlotterError(f"Unknown legacy F1 event id {event_id!r}.")
    if not isinstance(event["circuit"]["geometry"].get("model"), dict):
        raise MapPlotterError(
            f"Legacy F1 event {event_id!r} is held and has no renderable geometry."
        )
    return build_f1_plate(
        event,
        format_id,
        catalog=checked,
        context_mode=context_mode,
    )


__all__ = [
    "LEGACY_CATALOG_CLASS",
    "LEGACY_CATALOG_PATH",
    "LEGACY_LENGTH_GATE_PERCENT",
    "LEGACY_SEASON_SCOPE",
    "build_f1_legacy_plate",
    "list_f1_legacy_events",
    "load_f1_legacy_catalog",
    "validate_f1_legacy_catalog",
]
