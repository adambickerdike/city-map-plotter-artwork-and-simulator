from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_f1_circuit_series as series_builder  # noqa: E402
import qa_f1_circuit_series as f1_qa  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "f1-synthetic-complete-v1.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _catalog(event_count: int = 2) -> dict[str, Any]:
    catalog = json.loads(FIXTURE.read_text(encoding="utf-8"))
    first = catalog["events"][0]
    first["event_identity"] = "Synthetic Grand Prix"
    first["calendar_status"] = "confirmed"
    first["rights"] = {"release_gate": "hold-rights-review"}
    if event_count == 1:
        catalog["events"] = [first]
        return catalog

    second = copy.deepcopy(first)
    second.update(
        {
            "id": "synthetic-second-grand-prix",
            "calendar_order": 2,
            "event_identity": "Second Synthetic Grand Prix",
            "event_name": "Second Synthetic Grand Prix",
            "neutral_display_title": "Second Synthetic Circuit",
            "calendar_status": "conditional",
        }
    )
    second["circuit"].update(
        {
            "id": "synthetic-second-ring",
            "name": "Second Synthetic Ring",
            "official_name": "Second Synthetic Ring",
        }
    )
    second["rights"] = {"release_gate": "hold-conditional-calendar"}
    catalog["events"] = [first, second]
    return catalog


def _write_catalog(path: Path, catalog: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _args(
    *,
    catalog: Path,
    output: Path,
    formats: list[str],
    all_events: bool = True,
    events: list[str] | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        all=all_events,
        all_renderable=False,
        event=[] if events is None else events,
        catalog=catalog,
        output_dir=output,
        format=formats,
        dpi=254.0,
        no_png=True,
        no_split_pens=True,
        qa_profile="review",
        generated_at="2026-08-09T12:00:00+00:00",
    )


class _Artwork:
    def __init__(
        self,
        *,
        event_id: str,
        circuit_name: str,
        format_id: str,
        geometry_sha256: str,
        artifact_id: str | None = None,
    ) -> None:
        self.title = circuit_name
        self.variant_id = f"{series_builder.RENDERING_PRESET}-{format_id}"
        self.artifact_id = artifact_id or f"{event_id}--{self.variant_id}"
        self.context = SimpleNamespace(format_id=format_id)
        self.rights_status = "review-required"
        self.evidence_status = "source-qualified"
        self.geometry_sha256 = geometry_sha256


def _fake_builder(
    event: dict[str, Any],
    format_id: str,
    *,
    catalog: dict[str, Any],
) -> _Artwork:
    del catalog
    return _Artwork(
        event_id=str(event["id"]),
        circuit_name=str(event["circuit"]["name"]),
        format_id=format_id,
        geometry_sha256=str(event["circuit"]["geometry"]["model"]["geometry_sha256"]),
    )


def _fake_write_plate(
    artwork: _Artwork,
    output_dir: Path,
    **_kwargs: Any,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    svg_path = output_dir / f"{artwork.artifact_id}.svg"
    manifest_path = output_dir / f"{artwork.artifact_id}.plot.json"
    svg_path.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>\n', encoding="utf-8")
    outputs: dict[str, Any] = {
        "svg": {"path": str(svg_path.resolve()), "sha256": _sha256(svg_path)},
        "manifest": {"path": str(manifest_path.resolve())},
        "pen_files": [],
    }
    manifest = {
        "schema_version": 2,
        "artifact_kind": series_builder.ARTIFACT_KIND,
        "artifact_id": artwork.artifact_id,
        "rendering": {"f1_circuit": {"geometry_sha256": artwork.geometry_sha256}},
        "pen_sequence": [],
        "plot_summary": {},
        "outputs": copy.deepcopy(outputs),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    outputs["manifest"]["sha256"] = _sha256(manifest_path)
    return outputs


def _install_fake_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    catalog: dict[str, Any],
) -> None:
    def passing_semantic(staging: Path, **_kwargs: Any) -> dict[str, Any]:
        report = {
            "passed": True,
            "technical_pass": True,
            "rights_hold": True,
            "physical_proof_hold": True,
            "commercial_release_authorized": False,
        }
        (staging / "qa-f1-circuit-series.json").write_text(
            json.dumps(report) + "\n", encoding="utf-8"
        )
        (staging / "qa-f1-circuit-series.md").write_text(
            "# QA\n\nTechnical result: PASS\n", encoding="utf-8"
        )
        return report

    def passing_format(staging: Path, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        report = {"passed": True, "technical_pass": True}
        (staging / "qa-format-validation.json").write_text(
            json.dumps(report) + "\n", encoding="utf-8"
        )
        return report

    monkeypatch.setattr(
        series_builder,
        "load_f1_catalog",
        lambda _path: copy.deepcopy(catalog),
    )
    monkeypatch.setattr(series_builder, "build_f1_plate", _fake_builder)
    monkeypatch.setattr(series_builder, "write_plate", _fake_write_plate)
    monkeypatch.setattr(
        series_builder,
        "_run_semantic_qa",
        passing_semantic,
    )
    monkeypatch.setattr(
        series_builder,
        "_run_format_validation",
        passing_format,
    )


def test_series_build_is_exact_portable_and_byte_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog()
    for event in catalog["events"]:
        event.pop("source_refs", None)
        event["source_bindings"] = {
            "calendar_source_ref": "synthetic-calendar-source"
        }
    catalog_path = tmp_path / "catalog.json"
    output = tmp_path / "f1-review"
    _write_catalog(catalog_path, catalog)
    catalog_hash = _sha256(catalog_path)
    _install_fake_pipeline(monkeypatch, catalog)

    assert (
        series_builder.build_series(
            _args(
                catalog=catalog_path,
                output=output,
                formats=["a5-portrait", "a4-landscape"],
            )
        )
        == 0
    )

    index = json.loads((output / "index.json").read_text(encoding="utf-8"))
    assert index["event_count"] == 2
    assert index["plate_count"] == index["expected_cartesian_product"] == 4
    assert (
        index["formats"]
        == index["format_ids"]
        == [
            "a5-portrait",
            "a4-landscape",
        ]
    )
    assert index["generated_at"] == "2026-08-09T12:00:00+00:00"
    assert index["qa_profile"] == "review"
    assert index["qa_gate"] == {
        "state": "passed",
        "profile": "review",
        "promotion_authorized": True,
        "semantic_technical_pass": True,
        "format_technical_pass": True,
        "rights_hold": True,
        "physical_proof_hold": True,
        "commercial_release_authorized": False,
        "contact_sheets_required": False,
        "contact_sheet_count": 0,
        "selection_mode": "all",
        "held_event_count": 0,
    }
    assert index["release_id"] == series_builder._release_id(catalog)
    assert index["artifact_kind"] == series_builder.ARTIFACT_KIND
    assert index["rendering_preset"] == series_builder.RENDERING_PRESET
    assert index["selection_mode"] == "all"
    assert index["catalog_event_count"] == 2
    assert index["renderable_event_count"] == 2
    assert index["held_event_count"] == 0
    held_ledger = json.loads(
        (output / "HELD-EVENTS.json").read_text(encoding="utf-8")
    )
    assert held_ledger["selected_event_ids"] == [
        event["id"] for event in catalog["events"]
    ]
    assert held_ledger["held_events"] == []
    assert index["held_event_ledger"]["sha256"] == _sha256(
        output / "HELD-EVENTS.json"
    )
    assert index["catalog_sha256"] == index["catalog_file_sha256"] == catalog_hash

    entries = index["entries"]
    assert {(entry["event_id"], entry["format_id"]) for entry in entries} == {
        (event["id"], format_id)
        for event in catalog["events"]
        for format_id in index["formats"]
    }
    artifact_ids = [entry["artifact_id"] for entry in entries]
    assert len(artifact_ids) == len(set(artifact_ids)) == 4
    assert [entry["id"] for entry in entries] == artifact_ids

    expected_status = {
        event["id"]: event["calendar_status"] for event in catalog["events"]
    }
    expected_gates = {
        event["id"]: event["rights"]["release_gate"] for event in catalog["events"]
    }
    for entry in entries:
        event_id = entry["event_id"]
        geometry_hash = catalog["events"][entry["event_position"] - 1]["circuit"][
            "geometry"
        ]["model"]["geometry_sha256"]
        assert entry["artifact_kind"] == series_builder.ARTIFACT_KIND
        assert entry["calendar_status"] == expected_status[event_id]
        assert entry["status"]["calendar_status"] == expected_status[event_id]
        assert entry["status"]["release_gate"] == expected_gates[event_id]
        assert entry["catalog_sha256"] == entry["catalog_file_sha256"] == catalog_hash
        assert (
            entry["source_geometry_sha256"] == entry["geometry_sha256"] == geometry_hash
        )
        for output_record in entry["outputs"].values():
            records = (
                output_record if isinstance(output_record, list) else [output_record]
            )
            for record in records:
                if not isinstance(record, dict) or "path" not in record:
                    continue
                path = Path(record["path"])
                assert path.is_relative_to(output)
                assert path.is_file()
                assert ".staging-" not in str(path)
        manifest_path = Path(entry["outputs"]["manifest"]["path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert ".staging-" not in json.dumps(manifest, sort_keys=True)
        assert Path(manifest["outputs"]["svg"]["path"]).is_relative_to(output)
        assert Path(manifest["outputs"]["manifest"]["path"]) == manifest_path
        assert entry["outputs"]["manifest"]["sha256"] == _sha256(manifest_path)

    sources = json.loads((output / "SOURCES.json").read_text(encoding="utf-8"))
    assert sources["catalog_file_sha256"] == catalog_hash
    assert [item["event_id"] for item in sources["event_source_bindings"]] == [
        event["id"] for event in catalog["events"]
    ]
    assert all(
        binding["source_refs"]
        == ["synthetic-calendar-source", "synthetic-circuit-source"]
        for binding in sources["event_source_bindings"]
    )
    assert "OpenStreetMap contributors" in (output / "LICENSES.txt").read_text(
        encoding="utf-8"
    )
    for required in (
        "ARTIFACTS.md",
        "PEN-CHANGE-GUIDE.md",
        "SOURCES.json",
        "HELD-EVENTS.json",
        "LICENSES.txt",
        "gallery.html",
        "index.json",
        "qa-f1-circuit-series.json",
        "qa-f1-circuit-series.md",
        "qa-format-validation.json",
        "CHECKSUMS.sha256",
    ):
        assert (output / required).is_file()

    checksum_path = output / "CHECKSUMS.sha256"
    checksum_records = {
        relative: digest
        for digest, relative in (
            line.split("  ", 1)
            for line in checksum_path.read_text(encoding="ascii").splitlines()
        )
    }
    expected_files = {
        str(path.relative_to(output))
        for path in output.rglob("*")
        if path.is_file() and path != checksum_path
    }
    assert set(checksum_records) == expected_files
    assert all(
        _sha256(output / relative) == digest
        for relative, digest in checksum_records.items()
    )


def test_stage_path_rewriter_only_rewrites_descendants(tmp_path: Path) -> None:
    staging = tmp_path / "release.staging"
    final = tmp_path / "release"
    value = {
        "root": str(staging.resolve()),
        "nested": [{"path": str((staging / "plates" / "one.svg").resolve())}],
        "lookalike": str(staging.resolve()) + "-other/file.svg",
        "ordinary": "not a path",
    }

    rewritten = series_builder._replace_stage_paths(value, staging, final)

    assert rewritten["root"] == str(final.resolve())
    assert rewritten["nested"][0]["path"] == str(
        (final / "plates" / "one.svg").resolve()
    )
    assert rewritten["lookalike"] == value["lookalike"]
    assert rewritten["ordinary"] == "not a path"


def test_nested_source_ref_collection_is_recursive_unique_and_stable() -> None:
    event = {
        "sources": {
            "calendar_source_refs": ["calendar", "facts", "calendar"],
            "geometry": {"source_ref": "geometry"},
        },
        "circuit": {
            "geometry": {
                "model": {
                    "turn_stations": [
                        {"source_ref": "geometry"},
                        {"anchor_source_ref": "turn-document"},
                    ]
                }
            }
        },
    }

    assert series_builder._nested_source_refs(event) == [
        "calendar",
        "facts",
        "geometry",
        "turn-document",
    ]


def test_real_packager_output_matches_the_f1_release_qa_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_freeze = json.loads(FIXTURE.read_text(encoding="utf-8"))["freeze"][
        "frozen_at"
    ]
    monkeypatch.setattr(f1_qa, "FREEZE_DATE", fixture_freeze)
    output = tmp_path / "real-render-release"
    args = _args(
        catalog=FIXTURE,
        output=output,
        formats=["a5-portrait"],
    )
    args.no_split_pens = False

    assert series_builder.build_series(args) == 0
    report = f1_qa.audit_f1_circuit_series(
        output,
        catalog_file=FIXTURE,
        expected_event_count=1,
    )

    assert report["passed"], report["failures"]


def test_selection_rejects_ambiguous_duplicate_requests() -> None:
    catalog = _catalog()
    with pytest.raises(series_builder.SeriesBuildError, match="more than once"):
        series_builder._select_events(
            catalog,
            build_all=False,
            requested=[catalog["events"][0]["id"], catalog["events"][0]["id"]],
        )
    with pytest.raises(series_builder.SeriesBuildError, match="cannot be combined"):
        series_builder._select_events(
            catalog,
            build_all=True,
            requested=[catalog["events"][0]["id"]],
        )
    with pytest.raises(series_builder.SeriesBuildError, match="more than once"):
        series_builder._select_formats(["a5-portrait", "a5-portrait"])
    with pytest.raises(series_builder.SeriesBuildError, match="cannot be combined"):
        series_builder._select_formats(["all", "a5-portrait"])


def test_all_is_exact_but_all_renderable_ledgers_held_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog()
    held = catalog["events"][1]
    held["circuit"]["geometry"] = {
        "status": "provisional",
        "model": None,
        "reason": "configuration remains unqualified",
    }
    held["configuration_reference_season"] = 2026
    held["configuration_identity"] = {
        "status": "held",
        "disclosure": "Synthetic configuration remains source-unqualified.",
    }
    held["review"] = {
        "hold_reasons": [
            "configuration remains unqualified",
            "no connector may be inferred",
        ]
    }
    catalog_path = tmp_path / "catalog.json"
    output = tmp_path / "renderable-review"
    _write_catalog(catalog_path, catalog)
    _install_fake_pipeline(monkeypatch, catalog)

    exact_args = _args(
        catalog=catalog_path,
        output=tmp_path / "exact-all",
        formats=["a5-portrait"],
    )
    with pytest.raises(
        series_builder.SeriesBuildError,
        match="exact complete-catalog gate",
    ):
        series_builder.build_series(exact_args)
    assert not exact_args.output_dir.exists()

    review_args = _args(
        catalog=catalog_path,
        output=output,
        formats=["a5-portrait"],
        all_events=False,
    )
    review_args.all_renderable = True
    assert series_builder.build_series(review_args) == 0

    index = json.loads((output / "index.json").read_text(encoding="utf-8"))
    assert index["selection_mode"] == "all-renderable"
    assert index["catalog_event_count"] == 2
    assert index["event_count"] == index["renderable_event_count"] == 1
    assert index["held_event_count"] == 1
    assert index["held_event_ids"] == [held["id"]]
    assert {entry["event_id"] for entry in index["entries"]} == {
        catalog["events"][0]["id"]
    }

    ledger = json.loads(
        (output / "HELD-EVENTS.json").read_text(encoding="utf-8")
    )
    assert ledger["catalog_file_sha256"] == _sha256(catalog_path)
    assert ledger["artifact_kind"] == series_builder.ARTIFACT_KIND
    assert ledger["rendering_preset"] == series_builder.RENDERING_PRESET
    assert ledger["selected_event_ids"] == [catalog["events"][0]["id"]]
    assert ledger["held_events"] == [
        {
            "event_id": held["id"],
            "event_name": held["event_identity"],
            "circuit_id": held["circuit"]["id"],
            "circuit_name": held["circuit"]["name"],
            "calendar_order": held["calendar_order"],
            "calendar_status": "conditional",
            "configuration_reference_season": 2026,
            "configuration_identity_status": "held",
            "configuration_disclosure": (
                "Synthetic configuration remains source-unqualified."
            ),
            "geometry_status": "provisional",
            "normalized_model_present": False,
            "source_geometry_sha256": None,
            "hold_reason_codes": [
                "geometry-status-not-renderable",
                "normalized-model-absent",
            ],
            "hold_reasons": [
                "configuration remains unqualified",
                "no connector may be inferred",
            ],
            "status": {
                "calendar_status": "conditional",
                "wmsc_status": None,
                "homologation_status": None,
                "geometry_status": "provisional",
                "release_gate": "hold-conditional-calendar",
            },
            "source_refs": [
                "synthetic-calendar-source",
                "synthetic-circuit-source",
            ],
        }
    ]
    sources = json.loads((output / "SOURCES.json").read_text(encoding="utf-8"))
    assert [binding["event_id"] for binding in sources["event_source_bindings"]] == [
        event["id"] for event in catalog["events"]
    ]
    assert sources["event_source_bindings"][1][
        "renderability_hold_reason_codes"
    ] == ["geometry-status-not-renderable", "normalized-model-absent"]
    assert sources["event_source_bindings"][1]["hold_reasons"] == held["review"][
        "hold_reasons"
    ]


def test_all_renderable_requires_exact_declared_hold_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog()
    held = catalog["events"][1]
    held["circuit"]["geometry"] = {
        "status": "provisional",
        "model": None,
    }
    held["review"] = {"hold_reasons": []}
    catalog_path = tmp_path / "catalog.json"
    output = tmp_path / "renderable-review"
    _write_catalog(catalog_path, catalog)
    _install_fake_pipeline(monkeypatch, catalog)

    args = _args(
        catalog=catalog_path,
        output=output,
        formats=["a5-portrait"],
        all_events=False,
    )
    args.all_renderable = True
    with pytest.raises(
        series_builder.SeriesBuildError,
        match=r"non-empty review\.hold_reasons.*synthetic-second-grand-prix",
    ):
        series_builder.build_series(args)
    assert not output.exists()


def test_release_identity_tracks_renderer_and_schema_compatible_catalog_class() -> None:
    suffix = series_builder.RENDERING_PRESET.removeprefix("circuit-atlas-")
    assert series_builder.ARTIFACT_KIND == series_builder.f1_renderer.ARTIFACT_KIND
    assert series_builder._release_id_for_catalog_id("f1-circuits-2026") == (
        f"f1-circuit-atlas-2026-{suffix}"
    )
    assert series_builder._release_id_for_catalog_id("f1-circuits-legacy") == (
        f"f1-circuit-atlas-legacy-{suffix}"
    )


def test_existing_output_is_refused_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(event_count=1)
    catalog_path = tmp_path / "catalog.json"
    output = tmp_path / "existing-release"
    _write_catalog(catalog_path, catalog)
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("user-owned\n", encoding="utf-8")
    _install_fake_pipeline(monkeypatch, catalog)

    with pytest.raises(series_builder.SeriesBuildError, match="already exists"):
        series_builder.build_series(
            _args(catalog=catalog_path, output=output, formats=["a5-portrait"])
        )

    assert marker.read_text(encoding="utf-8") == "user-owned\n"
    assert list(output.iterdir()) == [marker]


def test_artifact_collision_aborts_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(event_count=1)
    catalog_path = tmp_path / "catalog.json"
    output = tmp_path / "collision-release"
    _write_catalog(catalog_path, catalog)
    _install_fake_pipeline(monkeypatch, catalog)

    def colliding_builder(
        event: dict[str, Any],
        format_id: str,
        *,
        catalog: dict[str, Any],
    ) -> _Artwork:
        artwork = _fake_builder(event, format_id, catalog=catalog)
        artwork.artifact_id = "same-artifact-id"
        return artwork

    monkeypatch.setattr(series_builder, "build_f1_plate", colliding_builder)

    with pytest.raises(series_builder.SeriesBuildError, match="colliding artifact IDs"):
        series_builder.build_series(
            _args(
                catalog=catalog_path,
                output=output,
                formats=["a5-portrait", "a4-landscape"],
            )
        )

    assert not output.exists()
    assert list(tmp_path.glob(".collision-release.staging-*")) == []


@pytest.mark.parametrize("failed_validator", ("semantic", "format"))
def test_technical_validator_failure_blocks_atomic_promotion(
    failed_validator: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(event_count=1)
    catalog_path = tmp_path / "catalog.json"
    output = tmp_path / f"{failed_validator}-failure"
    _write_catalog(catalog_path, catalog)
    _install_fake_pipeline(monkeypatch, catalog)
    calls: list[str] = []

    def semantic(staging: Path, **_kwargs: Any) -> dict[str, Any]:
        calls.append("semantic")
        assert (staging / "index.json").is_file()
        return {
            "technical_pass": failed_validator != "semantic",
            "rights_hold": True,
            "physical_proof_hold": True,
            "commercial_release_authorized": False,
        }

    def generic(staging: Path, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls.append("format")
        assert (staging / "index.json").is_file()
        return {"technical_pass": failed_validator != "format"}

    monkeypatch.setattr(series_builder, "_run_semantic_qa", semantic)
    monkeypatch.setattr(series_builder, "_run_format_validation", generic)

    with pytest.raises(series_builder.SeriesBuildError, match="promotion blocked"):
        series_builder.build_series(
            _args(catalog=catalog_path, output=output, formats=["a5-portrait"])
        )

    assert calls == ["semantic", "format"]
    assert not output.exists()
    assert list(tmp_path.glob(f".{output.name}.staging-*")) == []


def test_promotion_failure_reports_global_and_first_artifact_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(event_count=1)
    catalog_path = tmp_path / "catalog.json"
    output = tmp_path / "diagnostic-failure"
    _write_catalog(catalog_path, catalog)
    _install_fake_pipeline(monkeypatch, catalog)

    semantic_report = {
        "technical_pass": False,
        "rights_hold": True,
        "physical_proof_hold": True,
        "commercial_release_authorized": False,
        "failures": ["release matrix binding drifted"],
        "results": [
            {
                "artifact_id": "first-semantic-artifact",
                "event_id": "synthetic-grand-prix",
                "format_id": "a5-portrait",
                "passed": False,
                "failures": [
                    "travel ratio exceeds 2.0",
                    "turn label overlaps the lap",
                ],
            },
            {
                "artifact_id": "second-semantic-artifact",
                "passed": False,
                "failures": ["must not be included in the concise exception"],
            },
        ],
    }
    format_report = {
        "technical_pass": False,
        "failures": ["format specification could not be satisfied"],
        "results": [
            {
                "artifact_id": "first-format-artifact",
                "event_id": "synthetic-grand-prix",
                "format_id": "a5-portrait",
                "passed": False,
                "failures": ["safe-zone path leaves the map field"],
            }
        ],
    }
    monkeypatch.setattr(
        series_builder,
        "_run_semantic_qa",
        lambda *_args, **_kwargs: semantic_report,
    )
    monkeypatch.setattr(
        series_builder,
        "_run_format_validation",
        lambda *_args, **_kwargs: format_report,
    )

    with pytest.raises(series_builder.SeriesBuildError) as raised:
        series_builder.build_series(
            _args(catalog=catalog_path, output=output, formats=["a5-portrait"])
        )

    message = str(raised.value)
    assert "semantic F1 QA global failure: release matrix binding drifted" in message
    assert "first-semantic-artifact" in message
    assert "travel ratio exceeds 2.0" in message
    assert "turn label overlaps the lap" in message
    assert "generic format QA global failure" in message
    assert "first-format-artifact" in message
    assert "safe-zone path leaves the map field" in message
    assert "must not be included in the concise exception" not in message
    assert not output.exists()
    assert list(tmp_path.glob(f".{output.name}.staging-*")) == []


def test_validators_finish_before_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(event_count=1)
    catalog_path = tmp_path / "catalog.json"
    output = tmp_path / "ordered-promotion"
    _write_catalog(catalog_path, catalog)
    _install_fake_pipeline(monkeypatch, catalog)
    order: list[str] = []
    real_replace = series_builder.os.replace

    def semantic(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        order.append("semantic")
        return {
            "technical_pass": True,
            "rights_hold": True,
            "physical_proof_hold": True,
            "commercial_release_authorized": False,
        }

    def generic(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        order.append("format")
        return {"technical_pass": True}

    def replace(source: str | Path, destination: str | Path) -> None:
        order.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(series_builder, "_run_semantic_qa", semantic)
    monkeypatch.setattr(series_builder, "_run_format_validation", generic)
    monkeypatch.setattr(series_builder.os, "replace", replace)

    assert (
        series_builder.build_series(
            _args(catalog=catalog_path, output=output, formats=["a5-portrait"])
        )
        == 0
    )
    assert order == ["semantic", "format", "replace"]


def test_review_allows_declared_holds_but_release_requires_clearance_and_sheet(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "stage"
    final = tmp_path / "release"
    staging.mkdir()
    held = {
        "technical_pass": True,
        "rights_hold": True,
        "physical_proof_hold": True,
        "commercial_release_authorized": False,
    }
    format_pass = {"technical_pass": True}

    assert (
        series_builder._promotion_failures(
            qa_profile="review",
            semantic_report=held,
            format_report=format_pass,
            sheets=[],
            formats=["a5-portrait"],
            expected_contact_sheet_plates=1,
            staging=staging,
            final=final,
        )
        == []
    )

    release_failures = series_builder._promotion_failures(
        qa_profile="release",
        semantic_report=held,
        format_report=format_pass,
        sheets=[],
        formats=["a5-portrait"],
        expected_contact_sheet_plates=1,
        staging=staging,
        final=final,
    )
    assert any("rights hold" in failure for failure in release_failures)
    assert any("physical-proof hold" in failure for failure in release_failures)
    assert any("contact sheet" in failure for failure in release_failures)

    sheet_path = staging / "contact-sheets" / "a5-portrait.png"
    sheet_path.parent.mkdir()
    sheet_path.write_bytes(b"contact sheet")
    cleared = {
        "technical_pass": True,
        "rights_hold": False,
        "physical_proof_hold": False,
        "commercial_release_authorized": True,
    }
    sheet = {
        "format_id": "a5-portrait",
        "path": str(final / "contact-sheets" / "a5-portrait.png"),
        "sha256": _sha256(sheet_path),
        "plate_count": 1,
    }
    assert (
        series_builder._promotion_failures(
            qa_profile="release",
            semantic_report=cleared,
            format_report=format_pass,
            sheets=[sheet],
            formats=["a5-portrait"],
            expected_contact_sheet_plates=1,
            staging=staging,
            final=final,
        )
        == []
    )


def test_generated_at_is_fixed_or_uses_source_date_epoch() -> None:
    explicit = argparse.Namespace(generated_at="2026-08-09T13:00:00+01:00")
    assert series_builder._resolve_generated_at(explicit, environ={}) == (
        "2026-08-09T12:00:00+00:00"
    )
    fallback = argparse.Namespace(generated_at=None)
    assert (
        series_builder._resolve_generated_at(
            fallback, environ={"SOURCE_DATE_EPOCH": "0"}
        )
        == "1970-01-01T00:00:00+00:00"
    )
    with pytest.raises(series_builder.SeriesBuildError, match="fixed"):
        series_builder._resolve_generated_at(fallback, environ={})
    with pytest.raises(series_builder.SeriesBuildError, match="canonical integer"):
        series_builder._resolve_generated_at(
            fallback, environ={"SOURCE_DATE_EPOCH": "01"}
        )


def test_release_profile_preflight_requires_complete_png_matrix() -> None:
    args = argparse.Namespace(all=False, all_renderable=False, no_png=False)
    with pytest.raises(series_builder.SeriesBuildError, match="requires --all"):
        series_builder._validate_profile_preflight(
            args,
            qa_profile="release",
            formats=series_builder.FORMAT_IDS,
        )
    args.all = True
    with pytest.raises(series_builder.SeriesBuildError, match="all six"):
        series_builder._validate_profile_preflight(
            args,
            qa_profile="release",
            formats=("a5-portrait",),
        )
    args.no_png = True
    with pytest.raises(series_builder.SeriesBuildError, match="PNG previews"):
        series_builder._validate_profile_preflight(
            args,
            qa_profile="release",
            formats=series_builder.FORMAT_IDS,
        )

    args.all = False
    args.all_renderable = True
    args.no_png = False
    with pytest.raises(series_builder.SeriesBuildError, match="review"):
        series_builder._validate_profile_preflight(
            args,
            qa_profile="release",
            formats=series_builder.FORMAT_IDS,
        )


@pytest.mark.parametrize("target", (Path("/"), Path.home(), ROOT))
def test_unsafe_broad_targets_are_refused(target: Path) -> None:
    with pytest.raises(series_builder.SeriesBuildError, match="unsafe output target"):
        series_builder._assert_new_target(target)
