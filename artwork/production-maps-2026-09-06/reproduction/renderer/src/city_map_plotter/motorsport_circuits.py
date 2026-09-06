"""Source-qualified endurance-circuit studies using the shared plate engine."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from .f1_circuits import FORMAT_IDS, build_f1_plate, validate_f1_catalog
from .models import MapPlotterError
from .niche_common import PlateArtwork


CATALOG_PATH = Path(__file__).with_name("data") / "motorsport-circuit-studies-v1.json"
CATALOG_CLASS = "motorsport-circuit-studies"
ARTIFACT_KIND = "motorsport-circuit-study"
RENDERING_PRESET = "circuit-study-v1"


def validate_motorsport_catalog(value: Any) -> dict[str, Any]:
    checked = validate_f1_catalog(value)
    if checked.get("catalog_class") != CATALOG_CLASS:
        raise MapPlotterError(f"Motorsport catalog class must be {CATALOG_CLASS!r}.")
    if checked.get("season_scope") != "current-course-studies":
        raise MapPlotterError(
            "Motorsport catalog season_scope must be 'current-course-studies'."
        )
    return checked


def load_motorsport_catalog(path: Path | None = None) -> dict[str, Any]:
    source = path or CATALOG_PATH
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise MapPlotterError(
            f"Could not load motorsport circuit catalog {source}: {exc}"
        ) from exc
    return validate_motorsport_catalog(value)


def list_motorsport_circuits(
    catalog: Mapping[str, Any] | Path | None = None,
) -> list[dict[str, Any]]:
    if catalog is None or isinstance(catalog, Path):
        checked = load_motorsport_catalog(catalog)
    else:
        checked = validate_motorsport_catalog(catalog)
    return [
        {
            "id": event["id"],
            "title": event["neutral_display_title"],
            "circuit_name": event["circuit"]["official_name"],
            "configuration_id": event["circuit"]["configuration_id"],
            "published_length_m": event["circuit"]["lap_length_m"],
            "measured_length_m": event["circuit"]["geometry"]["review"][
                "measured_length_m"
            ],
            "length_discrepancy_percent": event["circuit"]["geometry"]["review"][
                "length_discrepancy_percent"
            ],
            "formats": list(FORMAT_IDS),
        }
        for event in checked["events"]
    ]


def build_motorsport_plate(
    circuit: str | Mapping[str, Any],
    format_id: str = "a4-landscape",
    *,
    catalog: Mapping[str, Any] | None = None,
) -> PlateArtwork:
    checked = (
        validate_motorsport_catalog(catalog)
        if catalog is not None
        else load_motorsport_catalog()
    )
    if isinstance(circuit, str):
        matches = [event for event in checked["events"] if event["id"] == circuit]
        if len(matches) != 1:
            known = ", ".join(str(event["id"]) for event in checked["events"])
            raise MapPlotterError(
                f"Unknown motorsport circuit {circuit!r}; choose {known}."
            )
        event = matches[0]
    else:
        event = dict(circuit)

    artwork = build_f1_plate(event, format_id, catalog=checked)
    artwork.domain = "motorsport-circuits"
    artwork.artifact_kind = ARTIFACT_KIND
    artwork.variant_id = f"{RENDERING_PRESET}-{format_id}"
    artwork.rendering_preset = RENDERING_PRESET
    artwork.source_provider = "frozen multi-source motorsport circuit catalog"
    artwork.notes = (
        "FULL CURRENT COURSE CENTRELINE STUDY",
        "NO SURVEYED TRACK WIDTH OR RACING LINE CLAIM",
        "NO ORGANISER ARTWORK, LOGOS, OR EVENT TRADE DRESS",
        "REVIEW ONLY / PHYSICAL PEN CALIBRATION AND RIGHTS REVIEW REQUIRED",
    )
    artwork.svg_metadata = {
        **copy.deepcopy(artwork.svg_metadata),
        "motorsport_circuit": {
            "catalog_id": checked["catalog_id"],
            "event_id": event["id"],
            "configuration_id": event["circuit"]["configuration_id"],
            "formula_1_claimed": False,
        },
    }
    artwork.rendering_metadata["motorsport_circuit"] = {
        "catalog_id": checked["catalog_id"],
        "event_id": event["id"],
        "configuration_id": event["circuit"]["configuration_id"],
        "formula_1_claimed": False,
        "course_study_only": True,
    }
    return artwork


__all__ = [
    "ARTIFACT_KIND",
    "CATALOG_PATH",
    "FORMAT_IDS",
    "RENDERING_PRESET",
    "build_motorsport_plate",
    "list_motorsport_circuits",
    "load_motorsport_catalog",
    "validate_motorsport_catalog",
]
