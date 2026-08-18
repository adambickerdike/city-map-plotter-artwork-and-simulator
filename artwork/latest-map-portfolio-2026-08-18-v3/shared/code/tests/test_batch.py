from __future__ import annotations

import contextlib
import fcntl
import gzip
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

from city_map_plotter.batch import (
    _batch_report_lock_path,
    artifacts_are_valid,
    bind_artifact_contract,
    build_batch_plan,
    default_report_path,
    execute_batch_plan,
    normalise_export_args,
    renderer_format_fingerprint,
    source_cohort_is_valid,
)
from city_map_plotter.catalog import load_catalog
from city_map_plotter.cli import main
from city_map_plotter.models import MapPlotterError


def _stable_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _write_pinned_json_source_manifest(
    root: Path, subject_ids: list[str]
) -> Path:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "sample-overpass.json").read_text(
            encoding="utf-8"
        )
    )
    canonical_sha256 = _stable_json_sha256(fixture)
    snapshot_root = root / "snapshots"
    snapshot_root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    for subject_id in subject_ids:
        snapshot = snapshot_root / f"{subject_id}.json.gz"
        payload = json.dumps(fixture, ensure_ascii=False).encode("utf-8")
        snapshot.write_bytes(gzip.compress(payload, mtime=0))
        entries.append(
            {
                "subject_id": subject_id,
                "path": snapshot.relative_to(root).as_posix(),
                "size_bytes": snapshot.stat().st_size,
                "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                "canonical_json_sha256": canonical_sha256,
                "query_sha256": hashlib.sha256(
                    f"query:{subject_id}".encode("utf-8")
                ).hexdigest(),
                "osm_base_timestamp": "2026-01-01T00:00:00Z",
                "extent_wgs84": {
                    "west": -2.0,
                    "south": 50.0,
                    "east": 1.0,
                    "north": 55.0,
                },
            }
        )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "id": "test-pinned-overpass-cohort",
        "license": {
            "provider": "OpenStreetMap contributors",
            "license": "ODbL 1.0",
        },
        "entries": entries,
    }
    manifest["cohort_sha256"] = _stable_json_sha256(manifest)
    path = root / "source-manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def _write_valid_artifacts(item: Mapping[str, object]) -> None:
    contract = item["artifact_contract"]
    assert isinstance(contract, dict)
    identity = contract["identity"]
    rendering = contract["rendering"]
    details = contract["details"]
    page = contract["page"]
    source_cohort = contract["source_cohort"]
    assert isinstance(identity, dict)
    assert isinstance(rendering, dict)
    assert isinstance(details, dict)
    assert isinstance(page, dict)
    assert isinstance(source_cohort, dict)
    output = Path(str(item["output"]))
    manifest = Path(str(item["manifest"]))
    metadata = {
        "preset": rendering["preset"],
        "detail_profile": rendering["detail_profile"],
        "extent_wgs84": contract["extent_wgs84"],
        "attribution": (
            "Map data © OpenStreetMap contributors — "
            "https://www.openstreetmap.org/copyright"
        ),
    }
    scale_path = (
        '<path data-scale-distance-m="100" d="M 0 0 L 1 0"/>'
        if rendering["scale_bar"]
        else ""
    )
    attribution_group = (
        '<g id="layer-attribution"><path d="M 0 0 L 1 0"/></g>'
        if rendering["visible_attribution"]
        else ""
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{page["width_mm"]}mm" height="{page["height_mm"]}mm" '
            f'viewBox="0 0 {page["width_mm"]} {page["height_mm"]}">'
            f"<title>{identity['title']}</title>"
            f"<metadata>{json.dumps(metadata)}</metadata>"
            f'<g id="layer-map_furniture">{scale_path}<path d="M 0 0 L 0 1"/></g>'
            f"{attribution_group}</svg>\n"
        ),
        encoding="utf-8",
    )
    source: dict[str, object] = {
        "provider": "OpenStreetMap contributors",
        "license": "ODbL 1.0",
        "attribution": (
            "Map data © OpenStreetMap contributors — "
            "https://www.openstreetmap.org/copyright"
        ),
    }
    if source_cohort["mode"] == "pinned-input-pbf":
        pbf = source_cohort["pbf"]
        assert isinstance(pbf, dict)
        source.update(
            {
                "endpoint": f"file:{pbf['path']}",
                "cache_path": pbf["path"],
                "from_cache": False,
                "provenance": {
                    "format": "osm.pbf",
                    "source_path": pbf["path"],
                    "size_bytes": pbf["size_bytes"],
                    "content_sha256": pbf["content_sha256"],
                    "source_file_sha256": pbf["content_sha256"],
                    "acquisition_mode": "pinned-pbf",
                    "source_pinned": True,
                },
            }
        )
    elif source_cohort["mode"] == "pinned-input-json-set":
        json_set = source_cohort["json_set"]
        assert isinstance(json_set, dict)
        entries = json_set["entries"]
        assert isinstance(entries, list)
        entry = next(
            value
            for value in entries
            if isinstance(value, dict)
            and value.get("subject_id") == identity["subject_id"]
        )
        path = entry["path"]
        source.update(
            {
                "endpoint": f"file:{path}",
                "timestamp": entry["osm_base_timestamp"],
                "cache_path": path,
                "from_cache": True,
                "provenance": {
                    "canonical_source_data_sha256": entry[
                        "canonical_json_sha256"
                    ],
                    "acquisition_mode": "pinned-json",
                    "source_pinned": True,
                    "source_file_sha256": entry["sha256"],
                },
            }
        )
    else:
        overpass = source_cohort["overpass"]
        assert isinstance(overpass, dict)
        source["endpoint"] = overpass["endpoint"]

    manifest_value: dict[str, object] = {
        "schema_version": 2,
        "generator": "city-map-plotter 0.2.0",
        "title": identity["title"],
        "details": details["lines"],
        "extent_wgs84": contract["extent_wgs84"],
        "families": contract["families"],
        "page": page,
        "rendering": {
            key: value for key, value in rendering.items() if key != "scale_detail"
        },
        "source": source,
    }
    if "png" in item:
        png = Path(str(item["png"]))
        raster = contract["raster"]
        assert isinstance(raster, dict)
        png.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            + int(raster["width_px"]).to_bytes(4, "big")
            + int(raster["height_px"]).to_bytes(4, "big")
        )
        manifest_value["raster_exports"] = [
            {
                **raster,
                "renderer": "test renderer",
                "source_svg_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "png_sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
            }
        ]
    manifest.write_text(json.dumps(manifest_value) + "\n", encoding="utf-8")
    bind_artifact_contract(dict(item))


class BatchPlanTests(unittest.TestCase):
    def test_live_plan_and_every_artifact_declare_one_unpinned_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_batch_plan(
                load_catalog(),
                collection_ids=["uk-russell-group"],
                output_dir=Path(directory),
                catalog_file=None,
                export_args=("--overpass-url", "https://osm.example.test/api"),
                limit=2,
            )

        cohort = plan["source_cohort"]
        self.assertTrue(source_cohort_is_valid(cohort))
        self.assertEqual(cohort["mode"], "live-overpass-unpinned")
        self.assertFalse(cohort["pinned"])
        self.assertFalse(cohort["production_eligible"])
        self.assertEqual(plan["source_cohort_sha256"], cohort["sha256"])
        self.assertEqual(
            cohort["overpass"]["endpoint"], "https://osm.example.test/api"
        )
        self.assertTrue(
            all(item["artifact_contract"]["source_cohort"] == cohort for item in plan["items"])
        )

    def test_input_pbf_content_defines_the_plan_and_artifact_source_cohort(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pbf = root / "region.osm.pbf"
            pbf.write_bytes(b"pinned source revision one")
            arguments: dict[str, Any] = {
                "catalog": load_catalog(),
                "collection_ids": ["uk-russell-group"],
                "output_dir": root / "maps",
                "catalog_file": None,
                "export_args": ("--input-pbf", str(pbf)),
                "limit": 1,
            }
            first = build_batch_plan(**arguments)
            first_sha = hashlib.sha256(pbf.read_bytes()).hexdigest()
            pbf.write_bytes(b"pinned source revision two")
            second = build_batch_plan(**arguments)

        cohort = first["source_cohort"]
        self.assertTrue(source_cohort_is_valid(cohort))
        self.assertEqual(cohort["mode"], "pinned-input-pbf")
        self.assertTrue(cohort["pinned"])
        self.assertTrue(cohort["production_eligible"])
        self.assertEqual(cohort["cohort_id"], f"osm-pbf-sha256:{first_sha}")
        self.assertEqual(cohort["pbf"]["content_sha256"], first_sha)
        self.assertEqual(
            first["items"][0]["artifact_contract"]["source_cohort"], cohort
        )
        self.assertEqual(
            first["items"][0]["artifact_contract"]["source_cohort_sha256"],
            cohort["sha256"],
        )
        self.assertNotEqual(first["plan_id"], second["plan_id"])
        self.assertNotEqual(cohort["sha256"], second["source_cohort"]["sha256"])

    def test_pinned_json_set_injects_one_verified_source_per_subject(self) -> None:
        subject_ids = [
            "uk-university-birmingham",
            "uk-university-bristol",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_manifest = _write_pinned_json_source_manifest(root, subject_ids)
            plan = build_batch_plan(
                load_catalog(),
                collection_ids=["uk-russell-group"],
                output_dir=root / "maps",
                catalog_file=None,
                export_args=(),
                source_manifest=source_manifest,
                limit=2,
            )

        cohort = plan["source_cohort"]
        self.assertTrue(source_cohort_is_valid(cohort))
        self.assertEqual(cohort["mode"], "pinned-input-json-set")
        self.assertTrue(cohort["pinned"])
        self.assertFalse(cohort["production_eligible"])
        self.assertEqual(plan["source_manifest"], str(source_manifest.resolve()))
        self.assertEqual(
            [entry["subject_id"] for entry in cohort["json_set"]["entries"]],
            subject_ids,
        )
        for item, subject_id in zip(plan["items"], subject_ids, strict=True):
            argv = item["export_argv"]
            self.assertNotIn("--input-pbf", argv)
            self.assertIn("--input-json", argv)
            input_index = argv.index("--input-json")
            expected_entry = next(
                entry
                for entry in cohort["json_set"]["entries"]
                if entry["subject_id"] == subject_id
            )
            self.assertEqual(argv[input_index + 1], expected_entry["path"])
            self.assertEqual(
                item["artifact_contract"]["source_cohort"], cohort
            )
            self.assertIn(
                {
                    "option": "--input-json",
                    "path": expected_entry["path"],
                    "size_bytes": expected_entry["size_bytes"],
                    "sha256": expected_entry["sha256"],
                },
                item["artifact_contract"]["dependency_fingerprints"],
            )

    def test_pinned_json_set_rejects_missing_subject_before_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_manifest = _write_pinned_json_source_manifest(
                root, ["uk-university-birmingham"]
            )
            with self.assertRaisesRegex(
                MapPlotterError, "no verified entry.*uk-university-bristol"
            ):
                build_batch_plan(
                    load_catalog(),
                    collection_ids=["uk-russell-group"],
                    output_dir=root / "maps",
                    catalog_file=None,
                    export_args=(),
                    source_manifest=source_manifest,
                    limit=2,
                )

    def test_pinned_json_set_rejects_snapshot_digest_mismatch_before_plan(self) -> None:
        subject_id = "uk-university-birmingham"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_manifest = _write_pinned_json_source_manifest(root, [subject_id])
            snapshot = root / "snapshots" / f"{subject_id}.json.gz"
            snapshot.write_bytes(snapshot.read_bytes() + b"changed")
            with self.assertRaisesRegex(MapPlotterError, "declared size_bytes and sha256"):
                build_batch_plan(
                    load_catalog(),
                    collection_ids=["uk-russell-group"],
                    output_dir=root / "maps",
                    catalog_file=None,
                    export_args=(),
                    source_manifest=source_manifest,
                    limit=1,
                )

    def test_pinned_json_set_remains_review_only_for_production(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_manifest = _write_pinned_json_source_manifest(
                root, ["uk-university-birmingham"]
            )
            with self.assertRaisesRegex(MapPlotterError, "pinned --input-pbf"):
                build_batch_plan(
                    load_catalog(),
                    collection_ids=["uk-russell-group"],
                    output_dir=root / "maps",
                    catalog_file=None,
                    export_args=("--production",),
                    source_manifest=source_manifest,
                    limit=1,
                )

    def test_production_plan_rejects_live_unpinned_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            MapPlotterError, "pinned --input-pbf"
        ):
            build_batch_plan(
                load_catalog(),
                collection_ids=["uk-russell-group"],
                output_dir=Path(directory),
                catalog_file=None,
                export_args=("--production",),
                limit=1,
            )

    def test_pinned_production_intent_is_bound_into_artifact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pbf = root / "source.osm.pbf"
            pbf.write_bytes(b"pinned production source")
            plan = build_batch_plan(
                load_catalog(),
                collection_ids=["uk-russell-group"],
                output_dir=root / "maps",
                catalog_file=None,
                export_args=("--input-pbf", str(pbf), "--production"),
                limit=1,
            )

        self.assertTrue(
            plan["items"][0]["artifact_contract"]["rendering"][
                "production_requested"
            ]
        )

    def test_full_catalog_plan_is_complete_unique_and_deterministic(self) -> None:
        catalog = load_catalog()
        collection_ids = [collection.id for collection in catalog.collections]
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "maps"
            arguments = (
                "--preset",
                "a5-balanced-poster",
                "--detail-profile",
                "faithful",
                "--road-style",
                "single-nib",
                "--nib-mm",
                "0.2",
            )
            first = build_batch_plan(
                catalog,
                collection_ids=collection_ids,
                output_dir=output_dir,
                catalog_file=None,
                export_args=arguments,
            )
            second = build_batch_plan(
                catalog,
                collection_ids=collection_ids,
                output_dir=output_dir,
                catalog_file=None,
                export_args=arguments,
            )

        self.assertEqual(first, second)
        self.assertEqual(first["item_count"], 108)
        self.assertEqual(first["marathon_city_basemap_count"], 30)
        self.assertEqual(len({item["subject_id"] for item in first["items"]}), 108)
        self.assertEqual(len({item["output"] for item in first["items"]}), 108)
        self.assertEqual(
            first["items"][0]["output"],
            str(
                output_dir.resolve()
                / "uk-russell-group"
                / "001-uk-university-birmingham.svg"
            ),
        )
        for option in arguments:
            self.assertIn(option, first["items"][0]["export_argv"])

    def test_marathon_plan_cannot_claim_unverified_course_geometry(self) -> None:
        catalog = load_catalog()
        with tempfile.TemporaryDirectory() as directory:
            plan = build_batch_plan(
                catalog,
                collection_ids=["global-marathons-core-2026"],
                output_dir=Path(directory),
                catalog_file=None,
                export_args=("--preset", "a5-balanced-poster"),
                limit=1,
            )

        item = plan["items"][0]
        self.assertEqual(item["subject_id"], "marathon-london")
        self.assertFalse(item["course_geometry_included"])
        self.assertEqual(item["geometry_status"], "pending_official_route")
        self.assertEqual(item["product_disclosure"], "CITY BASEMAP PREVIEW")
        self.assertEqual(
            item["artifact_contract"]["details"]["lines"][:2],
            ["CITY BASEMAP PREVIEW", "COURSE NOT INCLUDED"],
        )
        self.assertIn("--radius-km", item["export_argv"])
        radius_index = item["export_argv"].index("--radius-km")
        self.assertEqual(item["export_argv"][radius_index + 1], "8")
        title_index = item["export_argv"].index("--title")
        self.assertEqual(item["export_argv"][title_index + 1], "London City Basemap")
        self.assertNotIn("TCS London Marathon", item["export_argv"])

    def test_reserved_per_subject_options_are_rejected(self) -> None:
        for arguments in (
            ("--title", "Wrong"),
            ("--input-json=one-city.json",),
            ("-ooutput.svg",),
            ("--help",),
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(MapPlotterError):
                    normalise_export_args(arguments)

    def test_selected_collections_can_use_city_titles_and_require_pngs(self) -> None:
        catalog = load_catalog()
        with tempfile.TemporaryDirectory() as directory:
            plan = build_batch_plan(
                catalog,
                collection_ids=[
                    "uk-russell-group",
                    "us-student-cities-qs-2027",
                ],
                output_dir=Path(directory),
                catalog_file=None,
                export_args=("--radius-km", "2"),
                title_mode="city",
                png_dpi=254,
            )

        self.assertEqual(plan["item_count"], 38)
        self.assertEqual(plan["title_mode"], "city")
        self.assertEqual(plan["png_dpi"], 254)
        first = plan["items"][0]
        self.assertEqual(first["png"], str(Path(first["output"]).with_suffix(".png")))
        title_index = first["export_argv"].index("--title")
        self.assertEqual(first["export_argv"][title_index + 1], "Birmingham")

    def test_item_contract_resolves_the_complete_a5_render_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_batch_plan(
                load_catalog(),
                collection_ids=["uk-russell-group"],
                output_dir=Path(directory),
                catalog_file=None,
                export_args=(
                    "--preset",
                    "a5-balanced-poster",
                    "--radius-km",
                    "2",
                    "--layers",
                    "roads,water,railways,parks",
                    "--detail-profile",
                    "plotter-faithful",
                    "--road-style",
                    "centreline",
                    "--simplify-mm",
                    "0.04",
                    "--extent-fit",
                    "contain",
                    "--attribution-mode",
                    "external",
                    "--external-attribution-placement",
                    "Companion attribution file",
                    "--no-scale-bar",
                    "--no-scale-detail",
                    "--no-optimise",
                ),
                limit=1,
                title_mode="city",
                png_dpi=254,
            )

        item = plan["items"][0]
        contract = item["artifact_contract"]
        self.assertEqual(
            contract["identity"],
            {
                "collection_id": "uk-russell-group",
                "position": 1,
                "subject_id": "uk-university-birmingham",
                "subject_name": "University of Birmingham",
                "subject_kind": "university",
                "map_purpose": "campus",
                "title": "Birmingham",
            },
        )
        self.assertEqual(contract["families"], ["roads", "water", "railways", "parks"])
        self.assertEqual(
            contract["rendering"],
            {
                "preset": "a5-balanced-poster",
                "detail_profile": "plotter-faithful",
                "road_style": "centreline",
                "simplify_tolerance_mm": 0.04,
                "extent_fit": "contain",
                "travel_optimisation_enabled": False,
                "visible_attribution": False,
                "attribution_mode": "external",
                "external_attribution_placement": "Companion attribution file",
                "scale_bar": False,
                "scale_detail": False,
                "north_mark": True,
                "production_requested": False,
            },
        )
        self.assertEqual(contract["details"]["purpose"], "UNIVERSITY CAMPUS")
        self.assertEqual(contract["details"]["coordinate"], "52.4506 N / 1.9306 W")
        self.assertEqual(
            contract["details"]["lines"],
            [
                "UNIVERSITY CAMPUS",
                "52.4506 N / 1.9306 W",
            ],
        )
        self.assertEqual(contract["page"]["width_mm"], 148.0)
        self.assertEqual(contract["page"]["height_mm"], 210.0)
        self.assertEqual(contract["raster"]["dpi"], 254)
        self.assertEqual(contract["raster"]["width_px"], 1480)
        self.assertEqual(contract["raster"]["height_px"], 2100)
        self.assertEqual(
            contract["renderer_fingerprint_sha256"],
            plan["renderer_fingerprint"]["sha256"],
        )

    def test_implicit_a5_title_matches_renderer_comma_shortening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_batch_plan(
                load_catalog(),
                collection_ids=["us-student-cities-qs-2027"],
                output_dir=Path(directory),
                catalog_file=None,
                export_args=("--preset", "a5-balanced-poster"),
            )

        washington = next(
            item
            for item in plan["items"]
            if item["subject_id"] == "us-city-washington-dc"
        )
        self.assertEqual(
            washington["artifact_contract"]["identity"]["title"], "Washington"
        )

    def test_renderer_fingerprint_is_deterministic_and_changes_plan_id(self) -> None:
        first_fingerprint = renderer_format_fingerprint()
        self.assertEqual(first_fingerprint, renderer_format_fingerprint())
        self.assertEqual(len(first_fingerprint["source_tree_sha256"]), 64)
        self.assertEqual(len(first_fingerprint["format_sha256"]), 64)

        with tempfile.TemporaryDirectory() as directory:
            arguments: dict[str, Any] = {
                "catalog": load_catalog(),
                "collection_ids": ["uk-russell-group"],
                "output_dir": Path(directory),
                "catalog_file": None,
                "export_args": (),
                "limit": 1,
            }
            first = build_batch_plan(**arguments)
            changed_fingerprint = {
                **first_fingerprint,
                "source_tree_sha256": "0" * 64,
                "sha256": "1" * 64,
            }
            with patch(
                "city_map_plotter.batch.renderer_format_fingerprint",
                return_value=changed_fingerprint,
            ):
                changed = build_batch_plan(**arguments)

        self.assertNotEqual(first["plan_id"], changed["plan_id"])
        self.assertNotEqual(
            first["items"][0]["artifact_contract_sha256"],
            changed["items"][0]["artifact_contract_sha256"],
        )


class BatchExecutionTests(unittest.TestCase):
    def _plan(self, directory: str, *, limit: int = 2) -> dict[str, Any]:
        return build_batch_plan(
            load_catalog(),
            collection_ids=["uk-russell-group"],
            output_dir=Path(directory) / "maps",
            catalog_file=None,
            export_args=("--detail-profile", "faithful"),
            limit=limit,
        )

    def test_completed_batch_resumes_without_rerendering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = self._plan(directory)
            report_path = default_report_path(plan)
            calls: list[str] = []

            def render(item: dict[str, object]) -> None:
                calls.append(str(item["subject_id"]))
                _write_valid_artifacts(item)

            report, result = execute_batch_plan(
                plan,
                report_path=report_path,
                render_item=render,
                delay_seconds=0,
            )
            self.assertEqual(result["attempted"], 2)
            self.assertEqual(result["summary"]["completed"], 2)
            self.assertTrue(report_path.is_file())
            self.assertTrue(all("svg_sha256" in item for item in report["items"]))

            calls.clear()
            _resumed_report, resumed = execute_batch_plan(
                plan,
                report_path=report_path,
                render_item=render,
                delay_seconds=0,
            )
            self.assertEqual(calls, [])
            self.assertEqual(resumed["attempted"], 0)
            self.assertEqual(resumed["skipped"], 2)

    def test_changed_pbf_or_pen_inventory_aborts_before_render(self) -> None:
        for option, filename in (
            ("--input-pbf", "source.osm.pbf"),
            ("--pen-inventory", "pens.json"),
        ):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                dependency = root / filename
                dependency.write_bytes(b"planned dependency")
                plan = build_batch_plan(
                    load_catalog(),
                    collection_ids=["uk-russell-group"],
                    output_dir=root / "maps",
                    catalog_file=None,
                    export_args=(option, str(dependency)),
                    limit=1,
                )
                dependency.write_bytes(b"changed dependency")
                calls: list[str] = []

                with self.assertRaisesRegex(MapPlotterError, "changed after"):
                    execute_batch_plan(
                        plan,
                        report_path=default_report_path(plan),
                        render_item=lambda item: calls.append(str(item["subject_id"])),
                        keep_going=True,
                        delay_seconds=0,
                    )

                self.assertEqual(calls, [])

    def test_changed_pinned_json_aborts_before_render_without_network_fallback(
        self,
    ) -> None:
        subject_id = "uk-university-birmingham"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_manifest = _write_pinned_json_source_manifest(root, [subject_id])
            plan = build_batch_plan(
                load_catalog(),
                collection_ids=["uk-russell-group"],
                output_dir=root / "maps",
                catalog_file=None,
                export_args=(),
                source_manifest=source_manifest,
                limit=1,
            )
            snapshot = root / "snapshots" / f"{subject_id}.json.gz"
            snapshot.write_bytes(snapshot.read_bytes() + b"changed")
            calls: list[str] = []

            with self.assertRaisesRegex(MapPlotterError, "changed after"):
                execute_batch_plan(
                    plan,
                    report_path=default_report_path(plan),
                    render_item=lambda item: calls.append(str(item["subject_id"])),
                    keep_going=True,
                    delay_seconds=0,
                )

            self.assertEqual(calls, [])

    def test_exact_pinned_json_source_is_bound_to_completed_artifacts(self) -> None:
        subject_id = "uk-university-birmingham"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_manifest = _write_pinned_json_source_manifest(root, [subject_id])
            plan = build_batch_plan(
                load_catalog(),
                collection_ids=["uk-russell-group"],
                output_dir=root / "maps",
                catalog_file=None,
                export_args=(),
                source_manifest=source_manifest,
                limit=1,
            )
            report, result = execute_batch_plan(
                plan,
                report_path=default_report_path(plan),
                render_item=lambda item: _write_valid_artifacts(item),
                delay_seconds=0,
            )

            self.assertEqual(result["summary"]["completed"], 1)
            self.assertTrue(artifacts_are_valid(report["items"][0]))

    def test_dependencies_are_rehashed_before_each_pending_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pbf = root / "source.osm.pbf"
            pbf.write_bytes(b"one immutable cohort")
            plan = build_batch_plan(
                load_catalog(),
                collection_ids=["uk-russell-group"],
                output_dir=root / "maps",
                catalog_file=None,
                export_args=("--input-pbf", str(pbf)),
                limit=2,
            )
            calls: list[str] = []

            def render(item: dict[str, object]) -> None:
                calls.append(str(item["subject_id"]))
                _write_valid_artifacts(item)
                pbf.write_bytes(b"different cohort after first item")

            with self.assertRaisesRegex(MapPlotterError, "changed after"):
                execute_batch_plan(
                    plan,
                    report_path=default_report_path(plan),
                    render_item=render,
                    keep_going=True,
                    delay_seconds=0,
                )

            self.assertEqual(calls, [str(plan["items"][0]["subject_id"])])

    def test_pbf_manifest_must_match_the_planned_content_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pbf = root / "source.osm.pbf"
            pbf.write_bytes(b"pinned source")
            plan = build_batch_plan(
                load_catalog(),
                collection_ids=["uk-russell-group"],
                output_dir=root / "maps",
                catalog_file=None,
                export_args=("--input-pbf", str(pbf)),
                limit=1,
            )
            target = plan["items"][0]
            _write_valid_artifacts(target)
            manifest_path = Path(str(target["manifest"]))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source"]["provenance"]["content_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            self.assertFalse(artifacts_are_valid(target))
            with self.assertRaisesRegex(MapPlotterError, "planned source cohort"):
                bind_artifact_contract(target)

    def test_manifest_source_cohort_binding_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = self._plan(directory, limit=1)
            target = plan["items"][0]
            _write_valid_artifacts(target)
            manifest_path = Path(str(target["manifest"]))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["batch_source_cohort"]["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            self.assertFalse(artifacts_are_valid(target))

    def test_artifacts_are_bound_to_the_planned_catalog_subject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = self._plan(directory, limit=3)
            target = plan["items"][0]
            donor = plan["items"][2]
            _write_valid_artifacts(donor)
            Path(str(target["output"])).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(donor["output"]), str(target["output"]))
            shutil.copyfile(str(donor["manifest"]), str(target["manifest"]))

            self.assertFalse(artifacts_are_valid(target))

    def test_manifest_render_contract_rejects_adversarial_field_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_batch_plan(
                load_catalog(),
                collection_ids=["uk-russell-group"],
                output_dir=Path(directory) / "maps",
                catalog_file=None,
                export_args=(
                    "--preset",
                    "a5-balanced-poster",
                    "--layers",
                    "roads,water,railways,parks",
                    "--detail-profile",
                    "plotter-faithful",
                    "--road-style",
                    "centreline",
                    "--simplify-mm",
                    "0.04",
                    "--attribution-mode",
                    "external",
                    "--external-attribution-placement",
                    "Companion attribution file",
                    "--no-scale-bar",
                    "--no-scale-detail",
                    "--no-optimise",
                ),
                limit=1,
                title_mode="city",
            )
            target = plan["items"][0]
            changes = (
                ("family", ("families",), ["water"]),
                ("road style", ("rendering", "road_style"), "multi"),
                (
                    "simplification",
                    ("rendering", "simplify_tolerance_mm"),
                    0.08,
                ),
                ("extent fit", ("rendering", "extent_fit"), "cover"),
                (
                    "attribution mode",
                    ("rendering", "attribution_mode"),
                    "embedded",
                ),
                (
                    "visible attribution",
                    ("rendering", "visible_attribution"),
                    True,
                ),
                ("scale flag", ("rendering", "scale_bar"), True),
                (
                    "optimisation",
                    ("rendering", "travel_optimisation_enabled"),
                    True,
                ),
                ("coordinate detail", ("details", 1), "0.0000 N / 0.0000 E"),
            )
            for label, path, wrong_value in changes:
                with self.subTest(label=label):
                    _write_valid_artifacts(target)
                    manifest_path = Path(str(target["manifest"]))
                    value = json.loads(manifest_path.read_text(encoding="utf-8"))
                    parent: Any = value
                    for key in path[:-1]:
                        parent = parent[key]
                    parent[path[-1]] = wrong_value
                    manifest_path.write_text(json.dumps(value), encoding="utf-8")
                    self.assertFalse(artifacts_are_valid(target))

    def test_png_contract_rejects_self_consistent_wrong_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_batch_plan(
                load_catalog(),
                collection_ids=["uk-russell-group"],
                output_dir=Path(directory) / "maps",
                catalog_file=None,
                export_args=("--preset", "a5-balanced-poster"),
                limit=1,
                png_dpi=254,
            )
            target = plan["items"][0]
            _write_valid_artifacts(target)
            self.assertTrue(artifacts_are_valid(target))
            png_path = Path(str(target["png"]))
            png_value = bytearray(png_path.read_bytes())
            png_value[16:20] = (1479).to_bytes(4, "big")
            png_path.write_bytes(png_value)
            manifest_path = Path(str(target["manifest"]))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            raster = manifest["raster_exports"][0]
            raster["width_px"] = 1479
            raster["png_sha256"] = hashlib.sha256(png_value).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            self.assertFalse(artifacts_are_valid(target))

    def test_concurrent_runner_is_rejected_by_report_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            release = root / "release"
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
                plan = self._plan(str(release), limit=1)
                report_path = default_report_path(plan)
                release_root = Path(str(plan["output_dir"]))
                lock_path = _batch_report_lock_path(
                    report_path, release_root=release_root
                )
                self.assertFalse(lock_path.is_relative_to(report_path.parent))
                self.assertEqual(
                    lock_path,
                    _batch_report_lock_path(
                        report_path, release_root=release_root
                    ),
                )
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("a+", encoding="utf-8") as lock_stream:
                    fcntl.flock(
                        lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                    with self.assertRaisesRegex(
                        MapPlotterError, "Another batch runner already holds"
                    ):
                        execute_batch_plan(
                            plan,
                            report_path=report_path,
                            render_item=lambda item: _write_valid_artifacts(item),
                            delay_seconds=0,
                        )
                    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
                self.assertFalse(
                    report_path.with_suffix(report_path.suffix + ".lock").exists()
                )

    def test_resume_refuses_to_overwrite_a_changed_completed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = self._plan(directory, limit=1)
            report_path = default_report_path(plan)
            execute_batch_plan(
                plan,
                report_path=report_path,
                render_item=lambda item: _write_valid_artifacts(item),
                delay_seconds=0,
            )
            output = Path(str(plan["items"][0]["output"]))
            output.write_text("<svg>user edit</svg>\n", encoding="utf-8")

            with self.assertRaisesRegex(MapPlotterError, "Refusing to overwrite"):
                execute_batch_plan(
                    plan,
                    report_path=report_path,
                    render_item=lambda item: _write_valid_artifacts(item),
                    delay_seconds=0,
                )

    def test_failed_item_can_resume_while_completed_item_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = self._plan(directory)
            report_path = default_report_path(plan)
            failed_id = str(plan["items"][0]["subject_id"])

            def first_pass(item: dict[str, object]) -> None:
                if item["subject_id"] == failed_id:
                    raise MapPlotterError("synthetic acquisition failure")
                _write_valid_artifacts(item)

            first_report, first_result = execute_batch_plan(
                plan,
                report_path=report_path,
                render_item=first_pass,
                keep_going=True,
                delay_seconds=0,
            )
            self.assertEqual(
                first_result["summary"],
                {
                    "pending": 0,
                    "running": 0,
                    "completed": 1,
                    "failed": 1,
                },
            )
            self.assertEqual(first_report["items"][0]["attempts"], 1)

            resumed_report, resumed = execute_batch_plan(
                plan,
                report_path=report_path,
                render_item=lambda item: _write_valid_artifacts(item),
                delay_seconds=0,
            )
            self.assertEqual(resumed["attempted"], 1)
            self.assertEqual(resumed["skipped"], 1)
            self.assertEqual(resumed["summary"]["completed"], 2)
            self.assertEqual(resumed_report["items"][0]["attempts"], 2)

    def test_explicit_failed_item_with_valid_files_is_rerendered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = self._plan(directory, limit=1)
            report_path = default_report_path(plan)

            def failed_after_publish(item: dict[str, object]) -> None:
                _write_valid_artifacts(item)
                raise MapPlotterError("post-publish validation failed")

            first_report, _first_result = execute_batch_plan(
                plan,
                report_path=report_path,
                render_item=failed_after_publish,
                keep_going=True,
                delay_seconds=0,
            )
            self.assertEqual(first_report["items"][0]["status"], "failed")
            calls = 0

            def successful_retry(item: dict[str, object]) -> None:
                nonlocal calls
                calls += 1
                _write_valid_artifacts(item)

            resumed_report, resumed = execute_batch_plan(
                plan,
                report_path=report_path,
                render_item=successful_retry,
                delay_seconds=0,
            )
            self.assertEqual(calls, 1)
            self.assertEqual(resumed["recovered"], 0)
            self.assertEqual(resumed_report["items"][0]["attempts"], 2)

    def test_report_cannot_overwrite_a_protected_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = self._plan(directory, limit=1)
            protected = Path(directory) / "source.osm.pbf"
            protected.write_bytes(b"fixture")
            with self.assertRaisesRegex(MapPlotterError, "protected input"):
                execute_batch_plan(
                    plan,
                    report_path=protected,
                    render_item=lambda item: _write_valid_artifacts(item),
                    protected_paths=(protected,),
                    delay_seconds=0,
                )

    def test_png_is_hash_verified_as_a_required_batch_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_batch_plan(
                load_catalog(),
                collection_ids=["uk-russell-group"],
                output_dir=Path(directory) / "maps",
                catalog_file=None,
                export_args=(),
                limit=1,
                png_dpi=254,
            )
            report_path = default_report_path(plan)
            report, _result = execute_batch_plan(
                plan,
                report_path=report_path,
                render_item=lambda item: _write_valid_artifacts(item),
                delay_seconds=0,
            )
            self.assertIn("png_sha256", report["items"][0])
            Path(str(plan["items"][0]["png"])).unlink()

            with self.assertRaisesRegex(MapPlotterError, "Refusing to overwrite"):
                execute_batch_plan(
                    plan,
                    report_path=report_path,
                    render_item=lambda item: _write_valid_artifacts(item),
                    delay_seconds=0,
                )


class BatchCliTests(unittest.TestCase):
    def test_pinned_json_set_cli_dry_run_is_offline_and_subject_specific(self) -> None:
        subject_id = "uk-university-birmingham"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "maps"
            source_manifest = _write_pinned_json_source_manifest(root, [subject_id])
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "catalog",
                        "export",
                        "--collection",
                        "uk-russell-group",
                        "--output-dir",
                        str(output_dir),
                        "--source-manifest",
                        str(source_manifest),
                        "--limit",
                        "1",
                        "--dry-run",
                    ]
                )
            preview = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertFalse(preview["delay_between_items"])
            self.assertEqual(
                preview["source_cohort"]["mode"], "pinned-input-json-set"
            )
            self.assertIn("--input-json", preview["items"][0]["export_argv"])
            self.assertFalse(output_dir.exists())

    def test_all_collections_dry_run_validates_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "maps"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "catalog",
                        "export",
                        "--all-collections",
                        "--output-dir",
                        str(output_dir),
                        "--dry-run",
                        "--export-args",
                        "--preset",
                        "a5-balanced-poster",
                        "--detail-profile",
                        "faithful",
                        "--road-style",
                        "single-nib",
                        "--nib-mm",
                        "0.2",
                    ]
                )
            preview = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertTrue(preview["dry_run"])
            self.assertEqual(preview["item_count"], 108)
            self.assertEqual(preview["marathon_city_basemap_count"], 30)
            self.assertEqual(len(preview["collections"]), 5)
            self.assertFalse(output_dir.exists())
            self.assertFalse(Path(preview["report"]).exists())

    def test_cli_batch_uses_normal_export_path_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "maps"
            calls: list[str] = []
            fixture_plan = build_batch_plan(
                load_catalog(),
                collection_ids=["uk-russell-group"],
                output_dir=output_dir,
                catalog_file=None,
                export_args=(),
                limit=2,
            )
            fixtures = {str(item["subject_id"]): item for item in fixture_plan["items"]}

            def fake_export(args: Any) -> int:
                calls.append(args.subject)
                _write_valid_artifacts(fixtures[str(args.subject)])
                print(
                    json.dumps(
                        {"svg": str(args.output), "catalog_subject": args.subject}
                    )
                )
                print("\nPen sequence:")
                return 0

            command = [
                "catalog",
                "export",
                "--collection",
                "uk-russell-group",
                "--output-dir",
                str(output_dir),
                "--limit",
                "2",
                "--delay-seconds",
                "0",
            ]
            with (
                patch("city_map_plotter.cli._run_export", side_effect=fake_export),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(command), 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(
                calls,
                [
                    "uk-university-birmingham",
                    "uk-university-bristol",
                ],
            )
            self.assertEqual(result["summary"]["completed"], 2)

            calls.clear()
            with (
                patch("city_map_plotter.cli._run_export", side_effect=fake_export),
                contextlib.redirect_stdout(io.StringIO()) as resumed_stdout,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(command), 0)
            resumed = json.loads(resumed_stdout.getvalue())
            self.assertEqual(calls, [])
            self.assertEqual(resumed["attempted"], 0)
            self.assertEqual(resumed["skipped"], 2)


if __name__ == "__main__":
    unittest.main()
