#!/usr/bin/env python3
"""Build a source-backed F1 circuit-atlas review collection.

The builder deliberately treats the catalog as a review ledger, rather than a
claim that every calendar or legacy candidate is commercially releasable.
``--all`` is the complete-catalog gate and therefore refuses any event without
an eligible normalized model.  ``--all-renderable`` is an explicit review mode
which emits the eligible subset plus a machine-readable ledger for every held
event.  Rights and physical-proof holds are preserved independently.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from city_map_plotter import f1_circuits as f1_renderer  # noqa: E402
from city_map_plotter.f1_circuits import build_f1_plate, load_f1_catalog  # noqa: E402
from city_map_plotter.models import MapPlotterError  # noqa: E402
from city_map_plotter.niche_common import PlateArtwork, write_plate  # noqa: E402
import qa_f1_circuit_series as semantic_qa  # noqa: E402
import validate_format as format_qa  # noqa: E402


FORMAT_IDS = (
    "a5-portrait",
    "a5-landscape",
    "a4-portrait",
    "a4-landscape",
    "a3-portrait",
    "a3-landscape",
)
DEFAULT_CATALOG = ROOT / "src/city_map_plotter/data/f1-circuits-2026.json"
RENDERING_PRESET = f1_renderer.RENDERING_PRESET
ARTIFACT_KIND = f1_renderer.ARTIFACT_KIND
QA_PROFILES = ("review", "release")


class SeriesBuildError(MapPlotterError):
    """Raised when the collection cannot be emitted without ambiguity."""


def _release_id_for_catalog_id(catalog_id: str) -> str:
    """Derive a stable series identity from catalog and renderer identities."""

    normalized_catalog_id = str(catalog_id).strip()
    if normalized_catalog_id.startswith("f1-circuits-"):
        collection_id = "f1-circuit-atlas-" + normalized_catalog_id.removeprefix(
            "f1-circuits-"
        )
    else:
        collection_id = normalized_catalog_id + "-circuit-atlas"
    preset_prefix = "circuit-atlas-"
    renderer_version = (
        RENDERING_PRESET.removeprefix(preset_prefix)
        if RENDERING_PRESET.startswith(preset_prefix)
        else RENDERING_PRESET
    )
    release_id = f"{collection_id}-{renderer_version}"
    if not release_id or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
        for character in release_id
    ):
        raise SeriesBuildError(
            "Catalog and renderer identities do not form a stable release ID: "
            f"{release_id!r}."
        )
    return release_id


def _release_id(catalog: Mapping[str, Any]) -> str:
    catalog_id = catalog.get("catalog_id")
    if not isinstance(catalog_id, str) or not catalog_id.strip():
        raise SeriesBuildError("F1 catalog has no stable catalog_id.")
    return _release_id_for_catalog_id(catalog_id)


# Backwards-compatible module constants for the packaged current-calendar
# catalog.  Actual builds derive the release ID again from the loaded catalog,
# which also permits a schema-compatible legacy catalog to retain its identity.
RELEASE_ID = _release_id_for_catalog_id("f1-circuits-2026")
DEFAULT_OUTPUT = ROOT / "output" / RELEASE_ID


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _absolute(path: Path) -> Path:
    return path.expanduser().resolve()


def _resolve_generated_at(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve one reproducible, timezone-aware generation timestamp.

    Wall-clock time is deliberately not a fallback: a release rebuild must
    receive an explicit timestamp or the standard reproducible-build epoch.
    Both forms normalize to UTC so equivalent inputs produce identical bytes.
    """

    value = getattr(args, "generated_at", None)
    if value:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise SeriesBuildError(
                "--generated-at must be a timezone-aware ISO-8601 timestamp."
            ) from exc
        if parsed.utcoffset() is None:
            raise SeriesBuildError(
                "--generated-at must include an explicit timezone offset."
            )
        return parsed.astimezone(UTC).isoformat()

    environment = os.environ if environ is None else environ
    epoch_text = environment.get("SOURCE_DATE_EPOCH")
    if epoch_text is None:
        raise SeriesBuildError(
            "A fixed --generated-at value or SOURCE_DATE_EPOCH is required."
        )
    try:
        epoch = int(epoch_text, 10)
    except ValueError as exc:
        raise SeriesBuildError(
            "SOURCE_DATE_EPOCH must be an integer number of UTC seconds."
        ) from exc
    if str(epoch) != epoch_text or epoch < 0:
        raise SeriesBuildError(
            "SOURCE_DATE_EPOCH must be a non-negative canonical integer."
        )
    try:
        return datetime.fromtimestamp(epoch, UTC).isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise SeriesBuildError(
            "SOURCE_DATE_EPOCH is outside the supported range."
        ) from exc


def _assert_new_target(path: Path) -> None:
    forbidden = {Path("/"), Path.home().resolve(), ROOT.resolve()}
    if path.resolve() in forbidden:
        raise SeriesBuildError(f"Refusing unsafe output target: {path}")
    if path.exists():
        raise SeriesBuildError(
            f"Output already exists: {path}. Choose a new review release path."
        )


def _events(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = catalog.get("events")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise SeriesBuildError("F1 catalog must contain an events array.")
    result = [copy.deepcopy(item) for item in value]
    ids = [str(item.get("id") or "") for item in result]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise SeriesBuildError("F1 event IDs must be non-empty and unique.")
    return result


def _renderability_hold_reasons(event: Mapping[str, Any]) -> list[str]:
    """Return stable reason codes when the renderer must not consume an event."""

    circuit = event.get("circuit")
    if not isinstance(circuit, Mapping):
        return ["circuit-record-absent"]
    geometry = circuit.get("geometry")
    if not isinstance(geometry, Mapping):
        return ["geometry-record-absent"]

    reasons: list[str] = []
    geometry_status = geometry.get("status")
    eligible_statuses = getattr(
        f1_renderer,
        "RENDERABLE_GEOMETRY_STATUSES",
        frozenset(),
    )
    if not isinstance(geometry_status, str) or geometry_status not in eligible_statuses:
        reasons.append("geometry-status-not-renderable")

    model = geometry.get("model")
    if model is None:
        reasons.append("normalized-model-absent")
    elif not isinstance(model, Mapping):
        reasons.append("normalized-model-invalid")
    elif not model:
        reasons.append("normalized-model-empty")
    return reasons


def _renderability_partition(
    catalog: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    renderable: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    for event in _events(catalog):
        if _renderability_hold_reasons(event):
            held.append(event)
        else:
            renderable.append(event)
    return renderable, held


def _select_events(
    catalog: Mapping[str, Any],
    *,
    build_all: bool,
    requested: Sequence[str],
    build_all_renderable: bool = False,
) -> list[dict[str, Any]]:
    events = _events(catalog)
    if sum((bool(build_all), bool(build_all_renderable), bool(requested))) > 1:
        raise SeriesBuildError(
            "--all, --all-renderable, and explicit --event IDs cannot be combined."
        )
    if build_all:
        held = [event for event in events if _renderability_hold_reasons(event)]
        if held:
            held_ids = ", ".join(str(event["id"]) for event in held)
            raise SeriesBuildError(
                "--all is the exact complete-catalog gate and requires an eligible "
                "normalized model for every event. Held event(s): "
                f"{held_ids}. Use --all-renderable with --qa-profile review to "
                "build the eligible subset and preserve the hold ledger."
            )
        return events
    if build_all_renderable:
        renderable = [
            event for event in events if not _renderability_hold_reasons(event)
        ]
        if not renderable:
            raise SeriesBuildError(
                "--all-renderable found no event with an eligible normalized model."
            )
        return renderable
    if len(requested) != len(set(requested)):
        repeated = sorted(
            {event_id for event_id in requested if requested.count(event_id) > 1}
        )
        raise SeriesBuildError(
            "F1 event IDs were requested more than once: " + ", ".join(repeated) + "."
        )
    wanted = set(requested)
    selected = [event for event in events if str(event["id"]) in wanted]
    missing = sorted(wanted - {str(event["id"]) for event in selected})
    if missing:
        raise SeriesBuildError("Unknown F1 event(s): " + ", ".join(missing) + ".")
    if not selected:
        raise SeriesBuildError(
            "Choose --all, --all-renderable, or at least one --event ID."
        )
    held_selected = [
        event for event in selected if _renderability_hold_reasons(event)
    ]
    if held_selected:
        details = "; ".join(
            f"{event['id']} ({', '.join(_renderability_hold_reasons(event))})"
            for event in held_selected
        )
        raise SeriesBuildError(
            "Explicit selection includes event(s) without eligible normalized "
            f"models: {details}."
        )
    return selected


def _select_formats(values: Sequence[str]) -> tuple[str, ...]:
    if not values:
        return FORMAT_IDS
    if "all" in values:
        if len(values) != 1:
            raise SeriesBuildError(
                "Format 'all' cannot be combined with another --format value."
            )
        return FORMAT_IDS
    selected: list[str] = []
    for value in values:
        if value not in FORMAT_IDS:
            raise SeriesBuildError(
                f"Unknown format {value!r}; choose " + ", ".join(FORMAT_IDS) + "."
            )
        if value in selected:
            raise SeriesBuildError(f"Format {value!r} was requested more than once.")
        selected.append(value)
    return tuple(selected)


def _replace_stage_paths(value: Any, staging: Path, final: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_stage_paths(item, staging, final)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_stage_paths(item, staging, final) for item in value]
    if isinstance(value, str):
        staging_text = str(staging.resolve())
        if value == staging_text or value.startswith(staging_text + os.sep):
            relative = Path(value).relative_to(staging.resolve())
            return str(final.resolve() / relative)
    return value


def _finalize_outputs(
    outputs: dict[str, Any], staging: Path, final: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = Path(outputs["manifest"]["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = _replace_stage_paths(manifest, staging, final)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    finalized = _replace_stage_paths(outputs, staging, final)
    finalized["manifest"]["sha256"] = _sha256(manifest_path)
    return finalized, manifest


def _event_status(event: Mapping[str, Any]) -> dict[str, Any]:
    status = event.get("status")
    if isinstance(status, dict):
        return copy.deepcopy(status)
    release = event.get("release_status")
    if isinstance(release, dict):
        return copy.deepcopy(release)
    circuit = event.get("circuit") if isinstance(event.get("circuit"), dict) else {}
    geometry = (
        circuit.get("geometry") if isinstance(circuit.get("geometry"), dict) else {}
    )
    rights = event.get("rights") if isinstance(event.get("rights"), dict) else {}
    approval = event.get("approval") if isinstance(event.get("approval"), dict) else {}
    calendar_status = event.get("calendar_status")
    if calendar_status is not None:
        return {
            "calendar_status": str(calendar_status),
            "wmsc_status": event.get("wmsc_status", approval.get("wmsc_status")),
            "homologation_status": event.get(
                "homologation_status", approval.get("homologation_status")
            ),
            "geometry_status": geometry.get("status"),
            "release_gate": rights.get(
                "release_gate", rights.get("production_release_status", "hold")
            ),
        }
    return {
        "calendar": str(status or "unclassified"),
        "review_state": "hold-unclassified",
    }


def _calendar_status(event: Mapping[str, Any]) -> str:
    """Return the catalog's scalar calendar state without replacing its ledger."""

    for key in ("calendar_status", "release_status", "status"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested_key in ("calendar_status", "calendar", "status"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return "unclassified"


def _nested_source_refs(value: Any) -> list[str]:
    """Collect every singular/plural source reference below ``value``.

    F1 catalog events bind evidence in several nested records (calendar facts,
    geometry, turn stations, start/finish, context, and operational overlays).
    The source register must reflect those actual bindings rather than relying
    on an optional, denormalized top-level ``event.source_refs`` list.
    """

    refs: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for raw_key, nested in item.items():
                key = str(raw_key)
                if key == "source_ref" or key.endswith("_source_ref"):
                    if isinstance(nested, str) and nested:
                        refs.add(nested)
                elif key == "source_refs" or key.endswith("_source_refs"):
                    if isinstance(nested, list):
                        refs.update(
                            source_ref
                            for source_ref in nested
                            if isinstance(source_ref, str) and source_ref
                        )
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return sorted(refs)


def _event_display_name(event: Mapping[str, Any]) -> str:
    circuit = event.get("circuit") if isinstance(event.get("circuit"), Mapping) else {}
    return str(
        event.get("event_identity")
        or event.get("event_name")
        or event.get("title")
        or event.get("name")
        or circuit.get("name")
        or event.get("id")
        or "Unnamed circuit event"
    )


def _declared_hold_reasons(event: Mapping[str, Any]) -> list[str]:
    """Return exact, ordered catalog hold reasons only when fully valid."""

    review = event.get("review") if isinstance(event.get("review"), Mapping) else {}
    raw_reasons = review.get("hold_reasons")
    if not isinstance(raw_reasons, list) or not raw_reasons:
        return []
    if any(not isinstance(reason, str) or not reason.strip() for reason in raw_reasons):
        return []
    return copy.deepcopy(raw_reasons)


def _held_event_record(event: Mapping[str, Any]) -> dict[str, Any]:
    circuit = event.get("circuit") if isinstance(event.get("circuit"), Mapping) else {}
    identity = (
        event.get("configuration_identity")
        if isinstance(event.get("configuration_identity"), Mapping)
        else {}
    )
    geometry = (
        circuit.get("geometry")
        if isinstance(circuit.get("geometry"), Mapping)
        else {}
    )
    model = geometry.get("model")
    declared_geometry_hash = (
        model.get("geometry_sha256") if isinstance(model, Mapping) else None
    )
    return {
        "event_id": str(event.get("id") or ""),
        "event_name": _event_display_name(event),
        "circuit_id": circuit.get("id"),
        "circuit_name": circuit.get("name") or circuit.get("official_name"),
        "calendar_order": event.get("calendar_order"),
        "calendar_status": _calendar_status(event),
        "configuration_reference_season": event.get(
            "configuration_reference_season"
        ),
        "configuration_identity_status": identity.get("status"),
        "configuration_disclosure": identity.get("disclosure"),
        "geometry_status": geometry.get("status", "absent"),
        "normalized_model_present": isinstance(model, Mapping) and bool(model),
        "source_geometry_sha256": declared_geometry_hash,
        "hold_reason_codes": _renderability_hold_reasons(event),
        "hold_reasons": _declared_hold_reasons(event),
        "status": _event_status(event),
        "source_refs": _nested_source_refs(event),
    }


def _selection_document(
    *,
    catalog: Mapping[str, Any],
    catalog_file_sha256: str,
    release_id: str,
    selection_mode: str,
    selected: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    renderable, held = _renderability_partition(catalog)
    if selection_mode == "all-renderable":
        missing_reasons = [
            str(event.get("id") or "<missing-event-id>")
            for event in held
            if not _declared_hold_reasons(event)
        ]
        if missing_reasons:
            raise SeriesBuildError(
                "--all-renderable held-event transparency gate requires every "
                "held event to declare a non-empty review.hold_reasons list of "
                "non-blank strings. Missing or invalid: "
                + ", ".join(missing_reasons)
                + "."
            )
    selected_ids = [str(event["id"]) for event in selected]
    renderable_ids = [str(event["id"]) for event in renderable]
    return {
        "schema_version": 1,
        "release_id": release_id,
        "catalog_id": catalog.get("catalog_id"),
        "catalog_file_sha256": catalog_file_sha256,
        "season": catalog.get("season"),
        "rendering_preset": RENDERING_PRESET,
        "artifact_kind": ARTIFACT_KIND,
        "selection_mode": selection_mode,
        "catalog_event_count": len(renderable) + len(held),
        "selected_event_count": len(selected_ids),
        "selected_event_ids": selected_ids,
        "renderable_event_count": len(renderable_ids),
        "renderable_event_ids": renderable_ids,
        "unselected_renderable_event_ids": [
            event_id for event_id in renderable_ids if event_id not in set(selected_ids)
        ],
        "held_event_count": len(held),
        "held_event_ids": [str(event["id"]) for event in held],
        "held_events": [_held_event_record(event) for event in held],
        "policy": {
            "all": (
                "Exact complete-catalog gate; refuses any event without an "
                "eligible normalized geometry model."
            ),
            "all_renderable": (
                "Review-only subset; selects only renderer-eligible normalized "
                "models and preserves every omitted event in this ledger."
            ),
            "rights_and_physical_proof": (
                "Independent release holds; renderability does not clear either."
            ),
        },
    }


def _write_held_event_ledger(
    staging: Path,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    path = staging / "HELD-EVENTS.json"
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "path": path.name,
        "sha256": _sha256(path),
        "held_event_count": document.get("held_event_count", 0),
    }


def _without_geometry_hashes(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_geometry_hashes(item)
            for key, item in value.items()
            if key not in {"geometry_sha256", "source_geometry_sha256"}
        }
    if isinstance(value, list):
        return [_without_geometry_hashes(item) for item in value]
    return value


def _geometry_hash(event: Mapping[str, Any]) -> str:
    try:
        model = event["circuit"]["geometry"]["model"]
    except (KeyError, TypeError) as exc:
        raise SeriesBuildError(
            f"{event.get('id', '<unknown>')}: missing circuit geometry model."
        ) from exc
    payload = json.dumps(
        _without_geometry_hashes(model),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    declared = model.get("geometry_sha256") if isinstance(model, dict) else None
    if declared != digest:
        raise SeriesBuildError(
            f"{event.get('id', '<unknown>')}: geometry hash is {declared!r}; "
            f"canonical bytes require {digest}."
        )
    return digest


def _entry(
    event: Mapping[str, Any],
    artwork: PlateArtwork,
    outputs: dict[str, Any],
    manifest: Mapping[str, Any],
    *,
    catalog_file_sha256: str,
    event_position: int,
    plate_position: int,
) -> dict[str, Any]:
    circuit = event.get("circuit") if isinstance(event.get("circuit"), dict) else {}
    geometry_sha256 = _geometry_hash(event)
    f1_rendering = (manifest.get("rendering") or {}).get("f1_circuit")
    if not isinstance(f1_rendering, dict) or (
        f1_rendering.get("geometry_sha256") != geometry_sha256
    ):
        raise SeriesBuildError(
            f"{event['id']}/{artwork.context.format_id}: rendered manifest does not "
            "bind the catalog geometry hash."
        )
    return {
        "id": artwork.artifact_id,
        "artifact_kind": ARTIFACT_KIND,
        "rendering_preset": RENDERING_PRESET,
        "plate_position": plate_position,
        "event_position": event_position,
        "event_id": str(event["id"]),
        "event_name": _event_display_name(event),
        "circuit_name": str(circuit.get("name") or artwork.title),
        "format_id": artwork.context.format_id,
        "variant_id": artwork.variant_id,
        "artifact_id": artwork.artifact_id,
        "calendar_status": _calendar_status(event),
        "catalog_sha256": catalog_file_sha256,
        "catalog_file_sha256": catalog_file_sha256,
        "source_geometry_sha256": geometry_sha256,
        "geometry_sha256": geometry_sha256,
        "geometry_status": (circuit.get("geometry") or {}).get("status"),
        "status": _event_status(event),
        "rights_status": artwork.rights_status,
        "evidence_status": artwork.evidence_status,
        "pen_sequence": copy.deepcopy(manifest.get("pen_sequence", [])),
        "plot_summary": copy.deepcopy(manifest.get("plot_summary", {})),
        "outputs": outputs,
    }


def _relative(path: str | Path, final_root: Path) -> str:
    return str(Path(path).resolve().relative_to(final_root.resolve()))


def _staged_output_path(value: Any, staging: Path, final: Path) -> Path:
    """Resolve an output record to its physical file inside ``staging``."""

    if isinstance(value, Mapping):
        value = value.get("path")
    if not isinstance(value, (str, Path)) or not str(value):
        raise SeriesBuildError("Output record has no path.")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = staging / candidate
    candidate = candidate.resolve()
    stage_root = staging.resolve()
    final_root = final.resolve()
    try:
        candidate.relative_to(stage_root)
        return candidate
    except ValueError:
        pass
    try:
        relative = candidate.relative_to(final_root)
    except ValueError as exc:
        raise SeriesBuildError(
            f"Output path leaves both staging and release roots: {candidate}"
        ) from exc
    return stage_root / relative


def _write_contact_sheet(
    staging: Path,
    final: Path,
    entries: Sequence[Mapping[str, Any]],
    format_id: str,
) -> dict[str, Any] | None:
    montage = shutil.which("montage")
    pngs = [
        _staged_output_path(entry["outputs"]["png"], staging, final)
        for entry in entries
        if entry["format_id"] == format_id and "png" in entry["outputs"]
    ]
    if montage is None or not pngs:
        return None
    destination = staging / "contact-sheets" / f"{format_id}.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    columns = 4 if "landscape" in format_id else 5
    result = subprocess.run(
        [
            montage,
            *map(str, pngs),
            "-thumbnail",
            "520x700",
            "-gravity",
            "center",
            "-tile",
            f"{min(columns, len(pngs))}x",
            "-geometry",
            "520x700+20+20",
            "-background",
            "white",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return {
        "format_id": format_id,
        "path": str(final.resolve() / destination.relative_to(staging)),
        "sha256": _sha256(destination),
        "plate_count": len(pngs),
    }


def _run_semantic_qa(
    staging: Path,
    *,
    catalog_path: Path,
    expected_event_count: int,
    qa_profile: str,
) -> dict[str, Any]:
    """Run and persist the independent F1-domain audit on staged bytes."""

    report = semantic_qa.audit_f1_circuit_series(
        staging,
        catalog_file=catalog_path,
        expected_event_count=expected_event_count,
        complete_release=qa_profile == "release",
    )
    report = copy.deepcopy(report)
    report["qa_profile"] = qa_profile
    semantic_qa.write_qa_artifacts(staging, report)
    return report


def _run_format_validation(
    staging: Path,
    final: Path,
    entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Run the binding generic plate validator over every staged master."""

    spec = json.loads(format_qa.SPEC_PATH.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    for entry in entries:
        svg_path = _staged_output_path(entry["outputs"]["svg"], staging, final)
        report = format_qa.validate(svg_path, spec, str(entry["format_id"]))
        results.append(
            {
                "artifact_id": str(entry["artifact_id"]),
                "event_id": str(entry["event_id"]),
                "format_id": str(entry["format_id"]),
                "path": svg_path.relative_to(staging).as_posix(),
                "passed": report.passed,
                "checks": report.checks,
                "failures": list(report.failures),
                "warnings": list(report.warnings),
                "advisories": list(report.advisories),
            }
        )
    passed = bool(results) and all(result["passed"] for result in results)
    summary = {
        "schema_version": 1,
        "validator": "tools/validate_format.py",
        "passed": passed,
        "technical_pass": passed,
        "artifact_count": len(results),
        "results": results,
    }
    (staging / "qa-format-validation.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return summary


def _contact_sheet_failures(
    sheets: Sequence[Mapping[str, Any]],
    *,
    formats: Sequence[str],
    expected_plate_count: int,
    staging: Path,
    final: Path,
) -> list[str]:
    failures: list[str] = []
    by_format = {
        str(sheet.get("format_id") or ""): sheet
        for sheet in sheets
        if isinstance(sheet, Mapping)
    }
    if set(by_format) != set(formats) or len(sheets) != len(formats):
        failures.append("release profile requires exactly one contact sheet per format")
    for format_id in formats:
        sheet = by_format.get(format_id)
        if sheet is None:
            continue
        if sheet.get("plate_count") != expected_plate_count:
            failures.append(
                f"{format_id} contact sheet has {sheet.get('plate_count')} plates; "
                f"expected {expected_plate_count}"
            )
        try:
            path = _staged_output_path(sheet, staging, final)
        except SeriesBuildError as exc:
            failures.append(f"{format_id} contact sheet path is invalid: {exc}")
            continue
        if not path.is_file():
            failures.append(f"{format_id} contact sheet is absent")
        elif sheet.get("sha256") != _sha256(path):
            failures.append(f"{format_id} contact sheet digest does not bind its bytes")
    return failures


def _failure_messages(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _qa_report_diagnostics(
    report: Mapping[str, Any], *, validator: str
) -> list[str]:
    """Expose global and first-artifact evidence before staging is discarded."""

    diagnostics = [
        f"{validator} global failure: {message}"
        for message in _failure_messages(report.get("failures"))
    ]
    raw_results = report.get("results")
    if not isinstance(raw_results, list):
        return diagnostics
    for raw_result in raw_results:
        if not isinstance(raw_result, Mapping):
            continue
        messages = _failure_messages(raw_result.get("failures"))
        if raw_result.get("passed") is not False and not messages:
            continue
        artifact_id = str(
            raw_result.get("artifact_id")
            or raw_result.get("id")
            or "<unknown-artifact>"
        )
        event_id = str(raw_result.get("event_id") or "<unknown-event>")
        format_id = str(raw_result.get("format_id") or "<unknown-format>")
        identity = f"{artifact_id} ({event_id}/{format_id})"
        if not messages:
            diagnostics.append(
                f"{validator} first failed artifact {identity}: no failure detail"
            )
        else:
            diagnostics.extend(
                f"{validator} first failed artifact {identity}: {message}"
                for message in messages
            )
        break
    return diagnostics


def _promotion_failures(
    *,
    qa_profile: str,
    semantic_report: Mapping[str, Any],
    format_report: Mapping[str, Any],
    sheets: Sequence[Mapping[str, Any]],
    formats: Sequence[str],
    expected_contact_sheet_plates: int,
    staging: Path,
    final: Path,
) -> list[str]:
    """Return every condition that forbids the atomic directory promotion."""

    failures: list[str] = []
    if semantic_report.get("technical_pass") is not True:
        failures.append("semantic F1 QA did not report a technical pass")
        failures.extend(
            _qa_report_diagnostics(
                semantic_report,
                validator="semantic F1 QA",
            )
        )
    if format_report.get("technical_pass") is not True:
        failures.append("generic format validation did not report a technical pass")
        failures.extend(
            _qa_report_diagnostics(
                format_report,
                validator="generic format QA",
            )
        )
    if qa_profile == "release":
        if semantic_report.get("rights_hold") is not False:
            failures.append("release profile forbids an unresolved rights hold")
        if semantic_report.get("physical_proof_hold") is not False:
            failures.append("release profile forbids an unresolved physical-proof hold")
        if semantic_report.get("commercial_release_authorized") is not True:
            failures.append("release profile requires commercial-release authorization")
        failures.extend(
            _contact_sheet_failures(
                sheets,
                formats=formats,
                expected_plate_count=expected_contact_sheet_plates,
                staging=staging,
                final=final,
            )
        )
    return failures


def _series_title(catalog: Mapping[str, Any]) -> str:
    explicit = catalog.get("display_title") or catalog.get("title")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    season = catalog.get("season")
    if season is not None:
        return f"F1 circuit atlas — {season}"
    return f"F1 circuit atlas — {catalog.get('catalog_id', 'review')}"


def _write_gallery(
    staging: Path,
    final: Path,
    entries: Sequence[Mapping[str, Any]],
    *,
    catalog: Mapping[str, Any],
) -> Path:
    cards: list[str] = []
    for entry in entries:
        outputs = entry["outputs"]
        if "png" not in outputs:
            continue
        png = html.escape(_relative(outputs["png"]["path"], final))
        svg = html.escape(_relative(outputs["svg"]["path"], final))
        label = html.escape(
            f"{entry['circuit_name']} — {entry['format_id']} — {entry['status']}"
        )
        cards.append(
            f'<figure><a href="{svg}"><img src="{png}" alt="{label}"></a>'
            f"<figcaption>{html.escape(entry['circuit_name'])}<br>"
            f"{html.escape(entry['format_id'])}<br>"
            f"{html.escape(entry['event_id'])}</figcaption></figure>"
        )
    title = html.escape(_series_title(catalog))
    document = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>""" + title + """ review</title><style>
body{font:15px system-ui;margin:2rem;background:#eee;color:#171717}h1{font-weight:500}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1.5rem}
figure{margin:0;background:white;padding:1rem;box-shadow:0 2px 12px #0002}
img{display:block;width:100%;height:auto}figcaption{line-height:1.45;padding-top:.7rem}a{color:inherit}
</style></head><body><h1>""" + title + """ — review collection</h1>
<p>These are source-backed pen-plotter studies, not official Formula 1 circuit maps. Pending and rights-held plates remain review-only.</p><main>"""
    path = staging / "gallery.html"
    path.write_text(
        document + "\n".join(cards) + "</main></body></html>\n", encoding="utf-8"
    )
    return path


def _write_pen_guide(staging: Path, entries: Sequence[Mapping[str, Any]]) -> Path:
    lines = [
        "# Pen-change guide",
        "",
        "Plot the numbered pen-only SVGs in manifest order. Red is reserved for the source-centred, diagrammatic lap corridor; it is neither a claimed racing line nor surveyed track width. Calibrate each real pen, paper stock, speed and pressure before production.",
        "",
    ]
    for entry in entries:
        lines.extend(
            [
                f"## {entry['circuit_name']} — {entry['format_id']}",
                "",
                f"Artifact: `{entry['artifact_id']}`",
                "",
            ]
        )
        for step in entry["pen_sequence"]:
            lines.append(
                f"{step['step']}. **{step['pen']}** (`{step['pen_id']}`) — "
                f"{step['path_count']} paths, {step['pen_down_distance_mm']:.1f} mm pen-down."
            )
        lines.append("")
    path = staging / "PEN-CHANGE-GUIDE.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_sources(
    staging: Path,
    catalog: Mapping[str, Any],
    *,
    catalog_file_sha256: str,
    release_id: str,
) -> tuple[Path, Path]:
    source_register = {
        "schema_version": 1,
        "release_id": release_id,
        "rendering_preset": RENDERING_PRESET,
        "artifact_kind": ARTIFACT_KIND,
        "catalog_file_sha256": catalog_file_sha256,
        "season": catalog.get("season"),
        "freeze": copy.deepcopy(catalog.get("freeze", {})),
        "sources": copy.deepcopy(catalog.get("sources", [])),
        "event_source_bindings": [
            {
                "event_id": event.get("id"),
                "status": _event_status(event),
                "geometry_status": (
                    (((event.get("circuit") or {}).get("geometry") or {}).get("status"))
                ),
                "renderability_hold_reason_codes": _renderability_hold_reasons(event),
                "hold_reasons": (
                    _declared_hold_reasons(event)
                    if _renderability_hold_reasons(event)
                    else []
                ),
                "source_refs": _nested_source_refs(event),
                "lap_source_objects": copy.deepcopy(
                    (
                        ((event.get("circuit") or {}).get("geometry") or {}).get(
                            "model"
                        )
                        or {}
                    ).get("lap_source_objects", [])
                ),
            }
            for event in _events(catalog)
        ],
        "excluded_calendar_events": copy.deepcopy(
            catalog.get("excluded_calendar_events", [])
        ),
    }
    source_path = staging / "SOURCES.json"
    source_path.write_text(
        json.dumps(source_register, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    licence_lines = [
        "F1 CIRCUIT ATLAS — SOURCE AND LICENCE NOTICE",
        "",
        "Map geometry and contextual features derived from OpenStreetMap must retain:",
        "© OpenStreetMap contributors — https://www.openstreetmap.org/copyright",
        "",
        "FIA and Formula 1 pages are used only as factual calendar/circuit references. No official map imagery, logos, broadcast frames or trade dress are traced or reproduced.",
        "",
        "Circuit-outline, event-name, venue, sponsorship and merchandising rights are separate from map-data licensing. Every plate remains review-only until those rights and the exact plotter setup are cleared.",
        "",
        "See SOURCES.json and each .plot.json manifest for the exact frozen evidence and object bindings.",
    ]
    licence_path = staging / "LICENSES.txt"
    licence_path.write_text("\n".join(licence_lines) + "\n", encoding="utf-8")
    return source_path, licence_path


def _write_artifacts(
    staging: Path,
    final: Path,
    entries: Sequence[Mapping[str, Any]],
    sheets: Sequence[Mapping[str, Any]],
    *,
    catalog: Mapping[str, Any],
) -> Path:
    lines = [
        f"# {_series_title(catalog)} review artifacts",
        "",
        "Source-backed pen-plotter circuit studies in the six binding paper formats. These are not official Formula 1 maps and are not automatically cleared merchandise.",
        "",
        "## Contact sheets",
        "",
    ]
    for sheet in sheets:
        lines.append(
            f"- [{sheet['format_id']}]({_relative(sheet['path'], final)}) — {sheet['plate_count']} plates"
        )
    lines.extend(
        [
            "",
            "## Plates",
            "",
            "| Event | Circuit | Format | Status | Files |",
            "|---|---|---|---|---|",
        ]
    )
    for entry in entries:
        outputs = entry["outputs"]
        links = [
            f"[SVG]({_relative(outputs['svg']['path'], final)})",
            f"[manifest]({_relative(outputs['manifest']['path'], final)})",
        ]
        if "png" in outputs:
            links.insert(1, f"[PNG]({_relative(outputs['png']['path'], final)})")
        status = json.dumps(entry["status"], ensure_ascii=False, sort_keys=True)
        lines.append(
            f"| {entry['event_name']} | {entry['circuit_name']} | {entry['format_id']} | `{status}` | {' / '.join(links)} |"
        )
    lines.extend(
        [
            "",
            "## Operator files",
            "",
            "- [Pen-change guide](PEN-CHANGE-GUIDE.md)",
            "- [Source register](SOURCES.json)",
            "- [Held-event ledger](HELD-EVENTS.json)",
            "- [Licence notice](LICENSES.txt)",
            "- [Machine-readable index](index.json)",
            "- [Semantic F1 QA](qa-f1-circuit-series.md)",
            "- [Semantic F1 QA data](qa-f1-circuit-series.json)",
            "- [Generic format QA](qa-format-validation.json)",
            "- [Checksums](CHECKSUMS.sha256)",
            "",
        ]
    )
    path = staging / "ARTIFACTS.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_checksums(directory: Path) -> Path:
    destination = directory / "CHECKSUMS.sha256"
    files = sorted(
        path for path in directory.rglob("*") if path.is_file() and path != destination
    )
    destination.write_text(
        "".join(f"{_sha256(path)}  {path.relative_to(directory)}\n" for path in files),
        encoding="ascii",
    )
    return destination


def _ensure_unique_artifacts(entries: Iterable[Mapping[str, Any]]) -> None:
    values = [str(entry["artifact_id"]) for entry in entries]
    if len(values) != len(set(values)):
        repeated = sorted({value for value in values if values.count(value) > 1})
        raise SeriesBuildError(
            "Renderer produced colliding artifact IDs: " + ", ".join(repeated)
        )


def _index_document(
    *,
    catalog: Mapping[str, Any],
    catalog_file_sha256: str,
    release_id: str,
    selection_mode: str,
    selection_document: Mapping[str, Any],
    held_event_ledger: Mapping[str, Any],
    generated_at: str,
    qa_profile: str,
    selected: Sequence[Mapping[str, Any]],
    formats: Sequence[str],
    entries: Sequence[Mapping[str, Any]],
    sheets: Sequence[Mapping[str, Any]],
    gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expected_count = len(selected) * len(formats)
    return {
        "schema_version": 1,
        "release_id": release_id,
        "artifact_kind": ARTIFACT_KIND,
        "rendering_preset": RENDERING_PRESET,
        "catalog_id": catalog.get("catalog_id"),
        "catalog_sha256": catalog_file_sha256,
        "catalog_file_sha256": catalog_file_sha256,
        "season": catalog.get("season"),
        "generated_at": generated_at,
        "qa_profile": qa_profile,
        "review_only": qa_profile == "review",
        "selection_mode": selection_mode,
        "catalog_event_count": selection_document.get("catalog_event_count"),
        "renderable_event_count": selection_document.get("renderable_event_count"),
        "held_event_count": selection_document.get("held_event_count"),
        "held_event_ids": copy.deepcopy(selection_document.get("held_event_ids", [])),
        "held_event_ledger": dict(held_event_ledger),
        "event_count": len(selected),
        "formats": list(formats),
        "format_ids": list(formats),
        "plate_count": len(entries),
        "expected_cartesian_product": expected_count,
        "contact_sheets": list(sheets),
        "qa_artifacts": {
            "semantic_json": "qa-f1-circuit-series.json",
            "semantic_markdown": "qa-f1-circuit-series.md",
            "format_json": "qa-format-validation.json",
        },
        "qa_gate": dict(gate or {"state": "pending", "promotion_authorized": False}),
        "entries": list(entries),
    }


def _write_index(staging: Path, index: Mapping[str, Any]) -> Path:
    path = staging / "index.json"
    path.write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _validate_profile_preflight(
    args: argparse.Namespace,
    *,
    qa_profile: str,
    formats: Sequence[str],
) -> None:
    if qa_profile not in QA_PROFILES:
        raise SeriesBuildError(
            f"Unknown QA profile {qa_profile!r}; choose review or release."
        )
    if bool(getattr(args, "all_renderable", False)) and qa_profile != "review":
        raise SeriesBuildError("--all-renderable is restricted to --qa-profile review.")
    if qa_profile != "release":
        return
    if not bool(getattr(args, "all", False)):
        raise SeriesBuildError(
            "Release QA requires --all as the exact complete-catalog gate, not a "
            "renderable subset or explicit event subset."
        )
    if tuple(formats) != FORMAT_IDS:
        raise SeriesBuildError("Release QA requires all six binding formats in order.")
    if bool(args.no_png):
        raise SeriesBuildError(
            "Release QA requires PNG previews so contact sheets can be verified."
        )


def build_series(args: argparse.Namespace) -> int:
    if not math.isfinite(args.dpi) or args.dpi <= 0:
        raise SeriesBuildError("--dpi must be a positive finite number.")
    formats = _select_formats(args.format)
    qa_profile = str(getattr(args, "qa_profile", "review"))
    _validate_profile_preflight(args, qa_profile=qa_profile, formats=formats)
    catalog_path = _absolute(args.catalog)
    catalog_file_sha256 = _sha256(catalog_path)
    catalog = load_f1_catalog(catalog_path)
    release_id = _release_id(catalog)
    build_all_renderable = bool(getattr(args, "all_renderable", False))
    selection_mode = (
        "all"
        if bool(args.all)
        else "all-renderable"
        if build_all_renderable
        else "explicit-events"
    )
    selected = _select_events(
        catalog,
        build_all=bool(args.all),
        build_all_renderable=build_all_renderable,
        requested=args.event or [],
    )
    selection_document = _selection_document(
        catalog=catalog,
        catalog_file_sha256=catalog_file_sha256,
        release_id=release_id,
        selection_mode=selection_mode,
        selected=selected,
    )
    final = _absolute(args.output_dir)
    _assert_new_target(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    generated_at = _resolve_generated_at(args)
    expected_count = len(selected) * len(formats)

    with tempfile.TemporaryDirectory(
        prefix=f".{final.name}.staging-", dir=final.parent
    ) as temporary:
        staging = Path(temporary)
        entries: list[dict[str, Any]] = []
        plate_position = 0
        for event_position, event in enumerate(selected, start=1):
            for format_id in formats:
                plate_position += 1
                artwork = build_f1_plate(event, format_id, catalog=catalog)
                if artwork.context.format_id != format_id:
                    raise SeriesBuildError(
                        f"{event['id']}: renderer returned {artwork.context.format_id!r} for {format_id!r}."
                    )
                outputs = write_plate(
                    artwork,
                    staging / "plates" / format_id,
                    png=not args.no_png,
                    png_dpi=args.dpi,
                    split_pens=not args.no_split_pens,
                    generated_at=generated_at,
                )
                manifest = json.loads(
                    Path(outputs["manifest"]["path"]).read_text(encoding="utf-8")
                )
                entries.append(
                    _entry(
                        event,
                        artwork,
                        outputs,
                        manifest,
                        catalog_file_sha256=catalog_file_sha256,
                        event_position=event_position,
                        plate_position=plate_position,
                    )
                )
                print(
                    f"[{plate_position:03d}/{expected_count:03d}] staged {event['id']} / {format_id}",
                    flush=True,
                )

        if len(entries) != expected_count:
            raise SeriesBuildError(
                f"Built {len(entries)} plates; expected exact product {expected_count}."
            )
        _ensure_unique_artifacts(entries)
        sheets = [
            sheet
            for format_id in formats
            if (sheet := _write_contact_sheet(staging, final, entries, format_id))
            is not None
        ]
        held_event_ledger = _write_held_event_ledger(staging, selection_document)
        provisional_index = _index_document(
            catalog=catalog,
            catalog_file_sha256=catalog_file_sha256,
            release_id=release_id,
            selection_mode=selection_mode,
            selection_document=selection_document,
            held_event_ledger=held_event_ledger,
            generated_at=generated_at,
            qa_profile=qa_profile,
            selected=selected,
            formats=formats,
            entries=entries,
            sheets=sheets,
        )
        _write_index(staging, provisional_index)

        # Both independent validators inspect the staged masters and manifests.
        # Run both even if the first reports failures so one rejected build
        # yields a complete diagnostic rather than a misleading first error.
        semantic_report = _run_semantic_qa(
            staging,
            catalog_path=catalog_path,
            expected_event_count=len(_events(catalog)),
            qa_profile=qa_profile,
        )
        format_report = _run_format_validation(staging, final, entries)
        promotion_failures = _promotion_failures(
            qa_profile=qa_profile,
            semantic_report=semantic_report,
            format_report=format_report,
            sheets=sheets,
            formats=formats,
            expected_contact_sheet_plates=len(selected),
            staging=staging,
            final=final,
        )
        if promotion_failures:
            raise SeriesBuildError(
                "Atomic promotion blocked by QA: " + "; ".join(promotion_failures)
            )

        # The validators consumed the staged paths. Rewrite the small manifest
        # path ledger only after they pass; the SVG and split-job bytes they
        # audited are never mutated between validation and promotion.
        for entry in entries:
            finalized, _manifest = _finalize_outputs(entry["outputs"], staging, final)
            entry["outputs"] = finalized

        gate = {
            "state": "passed",
            "profile": qa_profile,
            "promotion_authorized": True,
            "semantic_technical_pass": True,
            "format_technical_pass": True,
            "rights_hold": bool(semantic_report.get("rights_hold")),
            "physical_proof_hold": bool(semantic_report.get("physical_proof_hold")),
            "commercial_release_authorized": bool(
                semantic_report.get("commercial_release_authorized")
            ),
            "contact_sheets_required": qa_profile == "release",
            "contact_sheet_count": len(sheets),
            "selection_mode": selection_mode,
            "held_event_count": selection_document["held_event_count"],
        }
        index = _index_document(
            catalog=catalog,
            catalog_file_sha256=catalog_file_sha256,
            release_id=release_id,
            selection_mode=selection_mode,
            selection_document=selection_document,
            held_event_ledger=held_event_ledger,
            generated_at=generated_at,
            qa_profile=qa_profile,
            selected=selected,
            formats=formats,
            entries=entries,
            sheets=sheets,
            gate=gate,
        )
        _write_index(staging, index)
        _write_gallery(staging, final, entries, catalog=catalog)
        _write_pen_guide(staging, entries)
        _write_sources(
            staging,
            catalog,
            catalog_file_sha256=catalog_file_sha256,
            release_id=release_id,
        )
        _write_artifacts(staging, final, entries, sheets, catalog=catalog)
        _write_checksums(staging)
        _assert_new_target(final)
        os.replace(staging, final)

    print(
        f"Built {len(entries)} review plates for {len(selected)} events in {final}",
        flush=True,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build-f1-circuit-series",
        description=(
            "Build source-backed F1 circuit pen plates in one or all binding formats."
        ),
        allow_abbrev=False,
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--all",
        action="store_true",
        help=(
            "Exact complete-catalog gate; fail if any event lacks an eligible "
            "normalized model."
        ),
    )
    selection.add_argument(
        "--all-renderable",
        action="store_true",
        help=(
            "Review-only: build every eligible normalized model and preserve "
            "all omitted events in HELD-EVENTS.json."
        ),
    )
    selection.add_argument(
        "--event", action="append", help="Build one event ID; repeatable."
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--format",
        action="append",
        default=[],
        help="Binding format ID; repeatable. Omit or pass 'all' for all six.",
    )
    parser.add_argument("--dpi", type=float, default=254.0)
    parser.add_argument("--no-png", action="store_true")
    parser.add_argument("--no-split-pens", action="store_true")
    parser.add_argument(
        "--qa-profile",
        choices=QA_PROFILES,
        default="review",
        help=(
            "Review permits explicit rights/physical holds after a technical "
            "pass; release also requires cleared holds and contact sheets."
        ),
    )
    parser.add_argument(
        "--generated-at",
        help=(
            "Fixed timezone-aware ISO-8601 timestamp. If omitted, "
            "SOURCE_DATE_EPOCH is required."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return build_series(args)
    except (MapPlotterError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"build-f1-circuit-series: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
