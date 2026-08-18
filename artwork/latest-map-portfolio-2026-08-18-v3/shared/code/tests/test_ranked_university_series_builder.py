from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_ranked_university_series as series_builder  # noqa: E402
from build_ranked_university_series import (  # noqa: E402
    EXPECTED_ARCHIVE_SHA256,
    EXPECTED_BASE_RENDERER_TREE_SHA256,
    EXPECTED_CATALOG_SHA256,
    EXPECTED_DERIVED_RENDERER_FINGERPRINT,
    EXPECTED_DERIVED_RENDERER_TREE_SHA256,
    EXPECTED_PATCHED_BATCH_SHA256,
    EXPECTED_PATCHED_CARTOGRAPHY_SHA256,
    EXPECTED_PATCHED_CLI_SHA256,
    EXPECTED_PATCHED_COMPLETENESS_SHA256,
    EXPECTED_PATCHED_SVG_SHA256,
    EXPECTED_RENDERER_FINGERPRINT,
    EXPECTED_RENDER_RECIPE_SHA256,
    EXPECTED_SOURCE_COHORT_SHA256,
    EXPECTED_SOURCE_MANIFEST_SHA256,
    EXPECTED_STYLE_SHA256,
    UK_COLLECTION,
    US_COLLECTION,
    SeriesBuildError,
    _build_command,
    _copy_exact,
    _prepare_release,
    _sha256,
)


def _args(output: Path) -> argparse.Namespace:
    return argparse.Namespace(
        output_dir=output,
        dry_run=False,
        limit=None,
        keep_going=True,
        overwrite=False,
        delay_seconds=2.0,
        timeout=300.0,
        user_agent="Ranked university test agent",
        overpass_url=None,
        print_command=False,
    )


def test_prepare_release_copies_and_binds_frozen_dependencies(tmp_path: Path) -> None:
    output = tmp_path / "series"
    dependencies = _prepare_release(output)
    contract = json.loads((output / "SERIES-CONTRACT.json").read_text())

    assert contract["expected_subject_count"] == 50
    assert contract["series_id"] == "university-memorabilia-ranked-2026-v2.1.4"
    assert contract["catalog"]["sha256"] == EXPECTED_CATALOG_SHA256
    assert contract["render_recipe"]["sha256"] == EXPECTED_RENDER_RECIPE_SHA256
    assert _sha256(output / contract["render_recipe"]["path"]) == (
        EXPECTED_RENDER_RECIPE_SHA256
    )
    assert contract["collections"] == [UK_COLLECTION, US_COLLECTION]
    assert contract["renderer"]["archive_sha256"] == EXPECTED_ARCHIVE_SHA256
    assert (
        contract["renderer"]["base_tree_sha256"]
        == EXPECTED_BASE_RENDERER_TREE_SHA256
    )
    assert (
        contract["renderer"]["tree_sha256"]
        == EXPECTED_DERIVED_RENDERER_TREE_SHA256
    )
    assert (
        contract["renderer"]["base_fingerprint"]["sha256"]
        == EXPECTED_RENDERER_FINGERPRINT
    )
    assert (
        contract["renderer"]["fingerprint"]["sha256"]
        == EXPECTED_DERIVED_RENDERER_FINGERPRINT
    )
    overrides = {
        item["path"]: item
        for item in contract["renderer"]["derivation"]["overrides"]
    }
    assert set(overrides) == {
        "city_map_plotter/cartography.py",
        "city_map_plotter/batch.py",
        "city_map_plotter/cli.py",
        "city_map_plotter/completeness.py",
        "city_map_plotter/svg.py",
    }
    assert (
        overrides["city_map_plotter/cartography.py"]["source_sha256"]
        == EXPECTED_PATCHED_CARTOGRAPHY_SHA256
    )
    assert (
        overrides["city_map_plotter/batch.py"]["source_sha256"]
        == EXPECTED_PATCHED_BATCH_SHA256
    )
    assert (
        overrides["city_map_plotter/cli.py"]["source_sha256"]
        == EXPECTED_PATCHED_CLI_SHA256
    )
    assert (
        overrides["city_map_plotter/completeness.py"]["source_sha256"]
        == EXPECTED_PATCHED_COMPLETENESS_SHA256
    )
    assert (
        overrides["city_map_plotter/svg.py"]["source_sha256"]
        == EXPECTED_PATCHED_SVG_SHA256
    )
    assert (
        _sha256(dependencies["renderer"] / "city_map_plotter/svg.py")
        == EXPECTED_PATCHED_SVG_SHA256
    )
    assert contract["output_contract"]["inventory_pen_slots"] == 10
    assert (
        contract["output_contract"]["empty_pen_slot_policy"]
        == "manifest-and-zero-path-split-without-empty-master-group"
    )
    assert contract["style"]["sha256"] == EXPECTED_STYLE_SHA256
    assert _sha256(dependencies["style"]) == EXPECTED_STYLE_SHA256
    assert contract["source_contract"] == {
        "mode": "pinned-input-json-set",
        "path": "release-metadata/source-snapshots/source-manifest.json",
        "manifest_sha256": EXPECTED_SOURCE_MANIFEST_SHA256,
        "cohort_sha256": EXPECTED_SOURCE_COHORT_SHA256,
        "subject_count": 50,
        "network_fallback": False,
        "production_eligible": False,
    }
    assert _sha256(dependencies["source_manifest"]) == EXPECTED_SOURCE_MANIFEST_SHA256
    source_manifest = json.loads(dependencies["source_manifest"].read_text())
    assert source_manifest["cohort_sha256"] == EXPECTED_SOURCE_COHORT_SHA256
    assert source_manifest["subject_count"] == 50
    assert len(source_manifest["entries"]) == 50
    for entry in source_manifest["entries"]:
        source = dependencies["source_manifest"].parent / entry["path"]
        assert source.is_file()
        assert _sha256(source) == entry["sha256"]
    assert dependencies["catalog"].is_relative_to(output)
    assert dependencies["renderer"].is_relative_to(output)
    assert dependencies["source_manifest"].is_relative_to(output)
    assert "OpenStreetMap contributors" in (output / "ATTRIBUTION.md").read_text()
    ranking_readme = (output / "RANKED-UNIVERSITIES.md").read_text()
    assert "London School of Economics" in ranking_readme
    assert "University of Texas at Austin" in ranking_readme

    # A resume verifies rather than silently replacing release dependencies.
    assert _prepare_release(output) == dependencies


def test_frozen_svg_backport_ignores_unrelated_edits_but_fails_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "svg.py"
    reviewed = series_builder.PATCHED_SVG.read_text(encoding="utf-8")
    source.write_text(reviewed + "\n# unrelated post-v2.1 feature\n", encoding="utf-8")
    payload = series_builder._patched_svg_payload(
        series_builder.FROZEN_RENDERER / "city_map_plotter/svg.py", source
    )
    assert hashlib.sha256(payload).hexdigest() == EXPECTED_PATCHED_SVG_SHA256

    source.write_text(
        reviewed.replace(
            "empty fixed inventory slot; no paths",
            "changed fixed inventory slot; no paths",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(SeriesBuildError, match="function payload changed"):
        series_builder._patched_svg_payload(
            series_builder.FROZEN_RENDERER / "city_map_plotter/svg.py", source
        )


def test_copy_exact_refuses_a_different_existing_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"reviewed")
    destination.write_bytes(b"changed")
    with pytest.raises(SeriesBuildError, match="different bytes"):
        _copy_exact(source, destination)


def test_prepare_release_refuses_an_unpinned_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed_catalog = tmp_path / "ranked.json"
    changed_catalog.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(series_builder, "CATALOG", changed_catalog)

    with pytest.raises(SeriesBuildError, match="Frozen dependency changed"):
        _prepare_release(tmp_path / "series")


def test_command_owns_the_exact_v21_visual_and_review_parameters(
    tmp_path: Path,
) -> None:
    output = tmp_path / "series"
    output.mkdir()
    dependencies = {
        "catalog": output / "release-metadata/catalog.json",
        "renderer": output / "release-metadata/renderer-contract",
        "style": output / "release-metadata/renderer-contract/style.json",
        "source_manifest": output
        / "release-metadata/source-snapshots/source-manifest.json",
    }
    command = _build_command(_args(output), dependencies)

    assert command.count("--collection") == 2
    assert UK_COLLECTION in command
    assert US_COLLECTION in command
    assert command[command.index("--title-mode") + 1] == "city"
    assert command[command.index("--source-manifest") + 1] == str(
        dependencies["source_manifest"]
    )
    assert command[command.index("--radius-km") + 1] == "2"
    assert command[command.index("--preset") + 1] == "a5-balanced-poster"
    assert (
        command[command.index("--poster-layout") + 1]
        == "university-memorabilia"
    )
    assert command[command.index("--detail-profile") + 1] == "plotter-faithful"
    assert command[command.index("--simplify-mm") + 1] == "0.04"
    assert command[command.index("--water-fill") + 1] == "dots"
    assert command[command.index("--road-style") + 1] == "centreline"
    assert command[command.index("--extent-fit") + 1] == "contain"
    assert command[command.index("--pen-profile") + 1] == "actual-pens"
    assert "--landmark-buildings" in command
    assert "--split-by-pen" in command
    assert command.count("--physical-audit") == 1
    assert "--no-scale-bar" in command
    assert "--no-scale-detail" in command
    assert "--theme" not in command
    assert command[command.index("--cache-dir") + 1] == str(output / "source-cache")
