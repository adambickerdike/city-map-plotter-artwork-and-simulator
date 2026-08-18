"""Bind generated output to docs/format/format-v1.json.

Three layers of protection:

1. The committed spec must be exactly what the generator produces, so nobody can
   hand-edit format-v1.json and have it survive.
2. The spec must be internally coherent -- zones inside the plotter-safe area, no
   overlaps, every type role clearing its legibility floor.
3. A ratchet on the committed examples: their conformance failure counts may go
   DOWN but never up. Lowering a baseline here is expected maintenance; raising
   one means a regression.
"""

from __future__ import annotations

import contextlib
from copy import deepcopy
import io
import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

from build_format_spec import (  # noqa: E402
    AUTHORITATIVE_SPEC_PATH,
    CREW_MAP_FIELD_ASPECT_DECIMALS,
    PACKAGED_SPEC_PATH,
    build_spec,
    serialize_spec,
)
from validate_format import (  # noqa: E402
    _polyline_length_in_box,
    _stable_json_sha256,
    validate,
)

from city_map_plotter.cli import main as map_plotter_main  # noqa: E402
from city_map_plotter.svgkit import path_geometry_sha256  # noqa: E402

SPEC_PATH = ROOT / "docs" / "format" / "format-v1.json"

EXPECTED_FORMATS = {
    "a5-portrait",
    "a5-landscape",
    "a4-portrait",
    "a4-landscape",
    "a3-portrait",
    "a3-landscape",
}

# Conformance debt in the pre-specification examples. These may only shrink.
#
# Re-baselined when NIB_LADDER moved to the real studio inventory
# (0.25/0.40/0.60/0.70/1.00). The examples did not get worse -- the specification
# got stricter, and their legacy 0.2/0.3/0.35/0.5 nibs are no longer purchasable.
# 18 of the failures below are exactly that off-ladder nib check.
BASELINE_FAILURES = {
    "sample-a3.svg": 30,
    "york-a5-clean-poster.svg": 40,
    "york-live-a5.svg": 23,
}


class SpecIntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))

    def test_committed_spec_matches_generator(self) -> None:
        self.assertEqual(
            self.spec,
            build_spec(),
            "docs/format/format-v1.json is out of date or was hand-edited. "
            "Edit the rules in tools/build_format_spec.py and rerun it.",
        )

    def test_authoritative_and_packaged_specs_are_identical_and_current(self) -> None:
        expected = serialize_spec(build_spec()).encode("utf-8")

        self.assertEqual(AUTHORITATIVE_SPEC_PATH, SPEC_PATH)
        self.assertEqual(AUTHORITATIVE_SPEC_PATH.read_bytes(), expected)
        self.assertEqual(PACKAGED_SPEC_PATH.read_bytes(), expected)
        self.assertEqual(
            AUTHORITATIVE_SPEC_PATH.read_bytes(), PACKAGED_SPEC_PATH.read_bytes()
        )

    def test_all_six_formats_present(self) -> None:
        self.assertEqual(set(self.spec["formats"]), EXPECTED_FORMATS)

    def test_zones_sit_inside_the_plotter_safe_area(self) -> None:
        for key, fmt in self.spec["formats"].items():
            page, safe = fmt["page_mm"], fmt["safe_margin_mm"]
            for name, rect in fmt["zones_mm"].items():
                with self.subTest(format=key, zone=name):
                    self.assertGreater(rect["width"], 0)
                    self.assertGreater(rect["height"], 0)
                    self.assertGreaterEqual(rect["x"], safe - 1e-6)
                    self.assertGreaterEqual(rect["y"], safe - 1e-6)
                    self.assertLessEqual(
                        rect["x"] + rect["width"], page["width"] - safe + 1e-6
                    )
                    self.assertLessEqual(
                        rect["y"] + rect["height"], page["height"] - safe + 1e-6
                    )

    def test_zones_do_not_overlap(self) -> None:
        for key, fmt in self.spec["formats"].items():
            zones = list(fmt["zones_mm"].items())
            for (name_a, a), (name_b, b) in itertools.combinations(zones, 2):
                overlap_x = min(a["x"] + a["width"], b["x"] + b["width"]) - max(
                    a["x"], b["x"]
                )
                overlap_y = min(a["y"] + a["height"], b["y"] + b["height"]) - max(
                    a["y"], b["y"]
                )
                with self.subTest(format=key, zones=f"{name_a}/{name_b}"):
                    self.assertFalse(
                        overlap_x > 1e-3 and overlap_y > 1e-3,
                        f"{name_a} and {name_b} overlap in {key}",
                    )

    def test_every_type_role_clears_its_legibility_floor(self) -> None:
        for key, fmt in self.spec["formats"].items():
            floors = fmt["rules"]["min_cap_height_mm"]
            for role, cap in fmt["type_scale_mm"].items():
                with self.subTest(format=key, role=role):
                    self.assertGreaterEqual(
                        cap,
                        floors[role],
                        f"{key}.{role} cap {cap} mm is below 8 x nib ({floors[role]} mm)",
                    )

    def test_title_zones_contain_two_line_physical_ink_envelopes(self) -> None:
        for key, fmt in self.spec["formats"].items():
            rule = fmt["rules"]["title_line_layout"]
            title_nib = fmt["nib_roles_mm"][fmt["type_nib_role"]["title"]]
            minimum_cap = fmt["rules"]["min_cap_height_mm"]["title"]
            required_zone_height = (
                2.0 * minimum_cap
                + rule["min_path_bounds_gap_mm"]
                + title_nib
            )
            with self.subTest(format=key):
                self.assertEqual(rule["maximum_lines"], 2)
                self.assertAlmostEqual(rule["nib_mm"], title_nib)
                self.assertGreaterEqual(
                    rule["horizontal_ink_inset_mm"],
                    title_nib / 2.0,
                )
                self.assertGreaterEqual(rule["min_ink_clearance_mm"], title_nib)
                self.assertGreaterEqual(
                    rule["min_path_bounds_gap_mm"],
                    title_nib + rule["min_ink_clearance_mm"],
                )
                self.assertGreaterEqual(
                    fmt["zones_mm"]["title"]["height"] + 1e-9,
                    required_zone_height,
                )

    def test_nib_ladders_are_ascending_and_distinct(self) -> None:
        for key, fmt in self.spec["formats"].items():
            ladder = fmt["nib_ladder_mm"]
            with self.subTest(format=key):
                self.assertEqual(ladder, sorted(set(ladder)))
                self.assertTrue(set(fmt["nib_roles_mm"].values()) <= set(ladder))
                self.assertTrue(set(fmt["map_linework_nib_mm"].values()) <= set(ladder))

    def test_crew_map_field_aspect_matches_the_emitted_rectangle(self) -> None:
        for key, fmt in self.spec["formats"].items():
            field = fmt["crew_zones_mm"]["crew_map_field"]
            expected = round(
                field["width"] / field["height"],
                CREW_MAP_FIELD_ASPECT_DECIMALS,
            )
            with self.subTest(format=key):
                self.assertEqual(fmt["crew_map_field_aspect"], expected)

    def test_bridge_composition_and_pen_roles_are_generated_for_every_format(
        self,
    ) -> None:
        required_zones = {
            "bridge_field_label",
            "bridge_drawing",
            "bridge_dimension_label",
        }
        required_pen_roles = {
            "construction",
            "context",
            "dimension",
            "copy",
            "fine",
            "secondary",
            "primary",
            "frame",
        }
        for key, fmt in self.spec["formats"].items():
            with self.subTest(format=key):
                bridge_zones = fmt["bridge_zones_mm"]
                self.assertEqual(set(bridge_zones), required_zones)
                field = fmt["zones_mm"]["map_field"]
                for zone in bridge_zones.values():
                    self.assertGreaterEqual(zone["x"], field["x"])
                    self.assertGreaterEqual(zone["y"], field["y"])
                    self.assertLessEqual(
                        zone["x"] + zone["width"],
                        field["x"] + field["width"],
                    )
                    self.assertLessEqual(
                        zone["y"] + zone["height"],
                        field["y"] + field["height"],
                    )
                drawing = bridge_zones["bridge_drawing"]
                self.assertGreaterEqual(
                    drawing["y"],
                    bridge_zones["bridge_field_label"]["y"]
                    + bridge_zones["bridge_field_label"]["height"],
                )
                self.assertLessEqual(
                    drawing["y"] + drawing["height"],
                    bridge_zones["bridge_dimension_label"]["y"] + 1e-9,
                )

                bridge_pens = fmt["bridge_pen_roles"]
                self.assertEqual(set(bridge_pens), required_pen_roles)
                self.assertTrue(
                    all(set(role) == {"ink", "nib_mm"} for role in bridge_pens.values())
                )
                self.assertTrue(
                    {role["nib_mm"] for role in bridge_pens.values()}
                    <= set(fmt["nib_ladder_mm"])
                )
                self.assertEqual(
                    {role: bridge_pens[role]["ink"] for role in required_pen_roles},
                    {
                        "construction": "Grey",
                        "context": "Blue",
                        "dimension": "Red",
                        "copy": "Black",
                        "fine": "Black",
                        "secondary": "Black",
                        "primary": "Black",
                        "frame": "Black",
                    },
                )
                self.assertEqual(bridge_pens["construction"]["nib_mm"], 0.25)
                self.assertEqual(bridge_pens["context"]["nib_mm"], 0.25)
                self.assertEqual(bridge_pens["dimension"]["nib_mm"], 0.25)
                self.assertEqual(bridge_pens["fine"]["nib_mm"], 0.25)
                self.assertEqual(bridge_pens["secondary"]["nib_mm"], 0.40)
                self.assertEqual(
                    bridge_pens["frame"]["nib_mm"],
                    fmt["nib_roles_mm"]["primary"],
                )
                self.assertEqual(bridge_pens["frame"], bridge_pens["primary"])
                self.assertEqual(
                    bridge_pens["copy"]["nib_mm"],
                    fmt["nib_roles_mm"]["text"],
                )

    def test_circuit_information_zones_fit_four_physical_cards(self) -> None:
        required = {
            "circuit_course",
            "circuit_history",
            "circuit_record",
            "circuit_drawing",
        }
        for key, fmt in self.spec["formats"].items():
            with self.subTest(format=key):
                cards = fmt["circuit_zones_mm"]
                self.assertEqual(set(cards), required)
                text_nib = fmt["nib_roles_mm"][fmt["type_nib_role"]["detail"]]
                label_cap = max(
                    fmt["type_scale_mm"]["attribution"],
                    8.0 * text_nib,
                )
                two_line_height = (
                    label_cap
                    + 2.0 * fmt["type_scale_mm"]["detail"]
                    + 4.0 * text_nib
                )
                for card in cards.values():
                    self.assertGreaterEqual(card["height"] + 1e-9, two_line_height)
                    self.assertGreaterEqual(
                        card["width"] - 4.0 * text_nib,
                        3.0 * text_nib,
                    )
                for first, second in itertools.combinations(cards.values(), 2):
                    overlap_x = min(
                        first["x"] + first["width"],
                        second["x"] + second["width"],
                    ) - max(first["x"], second["x"])
                    overlap_y = min(
                        first["y"] + first["height"],
                        second["y"] + second["height"],
                    ) - max(first["y"], second["y"])
                    self.assertFalse(overlap_x > 1e-3 and overlap_y > 1e-3)


class ExampleConformanceRatchetTest(unittest.TestCase):
    def _write_themed_plate(self, directory: str) -> Path:
        output = Path(directory) / "themed-london.svg"
        fixture = Path(__file__).parent / "fixtures" / "sample-overpass.json"
        with contextlib.redirect_stdout(io.StringIO()):
            result = map_plotter_main(
                [
                    "export",
                    "--theme",
                    "city-memorabilia-a5-series-v1",
                    "--subject",
                    "marathon-london",
                    "--input-json",
                    str(fixture),
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(result, 0)
        return output

    def _write_calibrated_plate(
        self,
        directory: str,
        *,
        effective_nib_mm: float,
        path_data: str = "M 12 40 L 20 40",
        layer_label: str | None = None,
        layer_id: str = "roads_local",
    ) -> Path:
        fmt = json.loads(SPEC_PATH.read_text(encoding="utf-8"))["formats"][
            "a5-portrait"
        ]
        svg_namespace = "http://www.w3.org/2000/svg"
        inkscape_namespace = "http://www.inkscape.org/namespaces/inkscape"
        ET.register_namespace("", svg_namespace)
        ET.register_namespace("inkscape", inkscape_namespace)
        root = ET.Element(
            f"{{{svg_namespace}}}svg",
            {
                "width": "148mm",
                "height": "210mm",
                "viewBox": "0 0 148 210",
            },
        )
        group = ET.SubElement(
            root,
            f"{{{svg_namespace}}}g",
            {
                f"{{{inkscape_namespace}}}groupmode": "layer",
                f"{{{inkscape_namespace}}}label": layer_label
                or f"01 — Calibrated fine roads — Black {effective_nib_mm:g}",
                "data-plot-nib-mm": f"{effective_nib_mm:g}",
                "data-plot-nominal-nib-mm": "0.25",
                "data-plot-width-mm": f"{max(effective_nib_mm, 0.25):g}",
            },
        )
        ET.SubElement(
            group,
            f"{{{svg_namespace}}}path",
            {"d": path_data},
        )
        svg_path = Path(directory) / "calibrated.svg"
        ET.ElementTree(root).write(svg_path, encoding="utf-8", xml_declaration=True)
        manifest = {
            "page": {"zones_mm": fmt["zones_mm"]},
            "layers": [
                {
                    "id": layer_id,
                    "nib_mm": effective_nib_mm,
                    "nominal_nib_mm": 0.25,
                    "plotted_width_mm": max(effective_nib_mm, 0.25),
                }
            ],
            "pen_sequence": [{"pen_up_distance_mm": 0.0}],
        }
        svg_path.with_suffix(".plot.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return svg_path

    def _add_pen_identity(
        self,
        svg_path: Path,
        *,
        profile_id: str,
        pen_id: str | None,
        ink: str,
        nominal_nib_mm: float,
        embedded_inventory: dict | None = None,
    ) -> None:
        svg_namespace = "http://www.w3.org/2000/svg"
        tree = ET.parse(svg_path)
        root = tree.getroot()
        group = root.find(f"{{{svg_namespace}}}g")
        assert group is not None
        group.set("data-plot-pen-profile", profile_id)
        group.set("data-plot-ink", ink)
        group.set("data-plot-nominal-nib-mm", f"{nominal_nib_mm:g}")
        if pen_id is not None:
            group.set("data-plot-pen-id", pen_id)
        tree.write(svg_path, encoding="utf-8", xml_declaration=True)

        manifest_path = svg_path.with_suffix(".plot.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["rendering"] = {
            "pen_profile": profile_id,
            "pen_inventory": embedded_inventory,
        }
        if embedded_inventory is None:
            manifest["rendering"].pop("pen_inventory")
        evidence = {
            "pen_profile": profile_id,
            "ink": ink,
            "nominal_nib_mm": nominal_nib_mm,
            "nib_mm": nominal_nib_mm,
        }
        if pen_id is not None:
            evidence["pen_id"] = pen_id
        manifest["layers"][0].update(evidence)
        manifest["pen_sequence"][0].update(evidence)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def _spec_with_ink_limit(self, max_ink_mm2: float) -> dict:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        budget = spec["formats"]["a5-portrait"]["ink_budget"]
        budget["max_coverage"] = max_ink_mm2 / budget["field_area_mm2"]
        budget["max_ink_mm2"] = max_ink_mm2
        return spec

    def test_calibrated_effective_width_is_not_treated_as_inventory_nib(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(directory, effective_nib_mm=0.27)
            report = validate(path, spec, "a5-portrait")

        self.assertEqual(report.failures, [])

    def test_bridge_manifest_must_publish_every_binding_bridge_zone(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        fmt = spec["formats"]["a5-portrait"]
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(directory, effective_nib_mm=0.25)
            manifest_path = path.with_suffix(".plot.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["domain"] = "bridges"
            manifest["page"]["zones_mm"].update(fmt["bridge_zones_mm"])
            del manifest["page"]["zones_mm"]["bridge_drawing"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            report = validate(path, spec, "a5-portrait")

        self.assertIn("manifest is missing zone 'bridge_drawing'", report.failures)

    def test_actual_inventory_accepts_exact_pen_id_ink_and_nib(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.25,
                layer_label="01 — Fine roads — Black 0.25",
            )
            self._add_pen_identity(
                path,
                profile_id="actual-pens",
                pen_id="black-0-25",
                ink="Black",
                nominal_nib_mm=0.25,
            )
            report = validate(path, spec, "a5-portrait")

        self.assertEqual(report.failures, [])

    def test_themed_artifact_contract_passes_as_one_coherent_plate(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_themed_plate(directory)
            report = validate(path, spec, "a5-portrait")

        self.assertEqual(report.failures, [])

    def test_themed_artifact_rejects_drifted_root_edition_signature(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_themed_plate(directory)
            tree = ET.parse(path)
            tree.getroot().set("data-edition-signature-sha256", "0" * 64)
            tree.write(path, encoding="utf-8", xml_declaration=True)

            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any(
                "SVG root edition_signature_sha256" in failure
                for failure in report.failures
            ),
            report.failures,
        )

    def test_themed_artifact_rejects_rehashed_installed_policy_drift(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        svg_namespace = "http://www.w3.org/2000/svg"
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_themed_plate(directory)
            manifest_path = path.with_suffix(".plot.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            contract = manifest["design_contract"]
            contract["source_policy_id"] = "invented-source-policy-v999"
            invariant = {
                "theme_id": contract["theme_id"],
                "theme_sha256": contract["theme_sha256"],
                "format_id": contract["format"]["id"],
                "format_contract_id": contract["format"]["contract_id"],
                "format_selected_plate_sha256": contract["format"][
                    "selected_plate_sha256"
                ],
                "font_sha256": contract["font"]["sha256"],
                "inventory_id": contract["inventory"]["id"],
                "inventory_sha256": contract["inventory"]["sha256"],
                "stock_tone": contract["inventory"]["stock_tone"],
                "resolved_map_layers": contract["resolved_map_layers"],
                "resolved_physical_layers": contract["resolved_physical_layers"],
                "typography_policy_id": contract["typography"]["policy_id"],
                "copy_policy_id": contract["copy_policy_id"],
                "placement_policy_id": contract["placement_policy_id"],
                "source_policy_id": contract["source_policy_id"],
                "validation_policy_id": contract["validation_policy_id"],
                "visual_renderer_contract_sha256": contract[
                    "visual_renderer_contract"
                ]["sha256"],
            }
            changed_edition = _stable_json_sha256(invariant)
            contract["edition_signature_sha256"] = changed_edition
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            tree = ET.parse(path)
            root = tree.getroot()
            root.set("data-edition-signature-sha256", changed_edition)
            metadata = root.find(f"{{{svg_namespace}}}metadata")
            assert metadata is not None
            metadata_value = json.loads(metadata.text or "{}")
            metadata_value["edition_signature_sha256"] = changed_edition
            metadata.text = json.dumps(metadata_value, separators=(",", ":"))
            tree.write(path, encoding="utf-8", xml_declaration=True)

            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any(
                "policy 'source_policy_id' does not match" in failure
                for failure in report.failures
            ),
            report.failures,
        )

    def test_themed_artifact_rejects_owned_but_wrong_role_pen(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_themed_plate(directory)
            manifest_path = path.with_suffix(".plot.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            water = next(
                layer
                for layer in manifest["layers"]
                if (layer.get("logical_layer_id") or layer.get("id")) == "water_areas"
            )
            # Both pens are genuinely owned 0.4 mm pens. Generic inventory
            # validation accepts either; the theme must preserve the Blue role.
            water["ink"] = "Black"
            water["pen_id"] = "black-0-4"
            water["pen"] = "Black 0.4"
            water["preview_color"] = "#17212b"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any(
                "themed map layer 'water_areas' field 'ink'" in failure
                for failure in report.failures
            ),
            report.failures,
        )

    def test_themed_artifact_rejects_one_pass_physical_drift(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        svg_namespace = "http://www.w3.org/2000/svg"
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_themed_plate(directory)
            tree = ET.parse(path)
            title_group = next(
                group
                for group in tree.getroot().findall(f"{{{svg_namespace}}}g")
                if group.get("id") == "layer-poster_title"
            )
            title_group.set("data-plot-passes", "2")
            tree.write(path, encoding="utf-8", xml_declaration=True)

            manifest_path = path.with_suffix(".plot.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            title = next(
                layer
                for layer in manifest["layers"]
                if layer.get("id") == "poster_title"
            )
            title["passes"] = 2
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any(
                "themed physical layer 'poster_title' field 'passes'" in failure
                for failure in report.failures
            ),
            report.failures,
        )

    def test_themed_artifact_rejects_typography_group_drift(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        svg_namespace = "http://www.w3.org/2000/svg"
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_themed_plate(directory)
            tree = ET.parse(path)
            title_group = next(
                group
                for group in tree.getroot().findall(f"{{{svg_namespace}}}g")
                if group.get("id") == "layer-poster_title"
            )
            title_group.set("data-theme-placement", "left-edge")
            tree.write(path, encoding="utf-8", xml_declaration=True)

            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any(
                "attribute 'data-theme-placement'" in failure
                for failure in report.failures
            ),
            report.failures,
        )

    def test_themed_artifact_rejects_rehashed_source_copy_geometry_drift(
        self,
    ) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        svg_namespace = "http://www.w3.org/2000/svg"
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_themed_plate(directory)
            tree = ET.parse(path)
            title_group = next(
                group
                for group in tree.getroot().findall(f"{{{svg_namespace}}}g")
                if group.get("id") == "layer-poster_title"
            )
            title_paths = list(title_group.iter(f"{{{svg_namespace}}}path"))
            title_paths[0].set("d", f"{title_paths[0].get('d')} L 13,13")
            tampered_digest = path_geometry_sha256(title_paths)
            title_group.set("data-copy-geometry-sha256", tampered_digest)
            tree.write(path, encoding="utf-8", xml_declaration=True)

            manifest_path = path.with_suffix(".plot.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["typography_evidence"]["roles"]["title"][
                "geometry_sha256"
            ] = tampered_digest
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any("cannot be regenerated" in failure for failure in report.failures),
            report.failures,
        )

    def test_themed_artifact_rejects_duplicate_declared_layer_group(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        svg_namespace = "http://www.w3.org/2000/svg"
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_themed_plate(directory)
            tree = ET.parse(path)
            root = tree.getroot()
            title_group = next(
                group
                for group in root.findall(f"{{{svg_namespace}}}g")
                if group.get("id") == "layer-poster_title"
            )
            insertion_index = list(root).index(title_group) + 1
            root.insert(insertion_index, deepcopy(title_group))
            tree.write(path, encoding="utf-8", xml_declaration=True)

            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any("repeats top-level layer group IDs" in failure for failure in report.failures),
            report.failures,
        )

    def test_themed_artifact_rejects_css_and_unlayered_geometry_bypasses(
        self,
    ) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        svg_namespace = "http://www.w3.org/2000/svg"
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_themed_plate(directory)
            tree = ET.parse(path)
            root = tree.getroot()
            title_group = next(
                group
                for group in root.findall(f"{{{svg_namespace}}}g")
                if group.get("id") == "layer-poster_title"
            )
            title_group.set("style", "stroke:#d4af37;display:none")
            title_path = next(title_group.iter(f"{{{svg_namespace}}}path"))
            title_path.set("stroke-dasharray", "0 9999")
            title_path.set("marker-start", "url(#uncontracted-marker)")
            ET.SubElement(
                title_group,
                f"{{{svg_namespace}}}circle",
                {"cx": "40", "cy": "40", "r": "10"},
            )
            ET.SubElement(
                root, f"{{{svg_namespace}}}style"
            ).text = "path { stroke: #d4af37 !important; }"
            ET.SubElement(
                root,
                f"{{{svg_namespace}}}line",
                {"x1": "0", "y1": "0", "x2": "20", "y2": "20"},
            )
            rogue = ET.SubElement(
                root,
                f"{{{svg_namespace}}}g",
                {"id": "uncontracted-geometry"},
            )
            ET.SubElement(
                rogue,
                f"{{{svg_namespace}}}path",
                {"d": "M 20,20 L 21,21"},
            )
            tree.write(path, encoding="utf-8", xml_declaration=True)

            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any("stylesheet" in failure for failure in report.failures),
            report.failures,
        )
        self.assertTrue(
            any("CSS/presentation" in failure for failure in report.failures),
            report.failures,
        )
        self.assertTrue(
            any("outside a top-level" in failure for failure in report.failures),
            report.failures,
        )
        self.assertTrue(
            any("path-only" in failure for failure in report.failures),
            report.failures,
        )
        self.assertTrue(
            any("unsupported attributes" in failure for failure in report.failures),
            report.failures,
        )

    def test_actual_inventory_rejects_numeric_but_unowned_ink_nib_pair(
        self,
    ) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.6,
                layer_label="01 — Imaginary broad grey — Grey 0.6",
            )
            self._add_pen_identity(
                path,
                profile_id="actual-pens",
                pen_id=None,
                ink="Grey",
                nominal_nib_mm=0.6,
            )
            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any(
                "requests unavailable Grey 0.6" in failure
                for failure in report.failures
            )
        )
        self.assertFalse(
            any("not in the A5 ladder" in failure for failure in report.failures)
        )

    def test_actual_inventory_rejects_unknown_and_mismatched_pen_ids(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            unknown_path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.4,
                layer_label="01 — Water — Blue 0.4",
            )
            self._add_pen_identity(
                unknown_path,
                profile_id="actual-pens",
                pen_id="blue-imaginary-0-4",
                ink="Blue",
                nominal_nib_mm=0.4,
            )
            unknown_report = validate(unknown_path, spec, "a5-portrait")

            mismatched_path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.4,
                layer_label="01 — Water — Blue 0.4",
            )
            self._add_pen_identity(
                mismatched_path,
                profile_id="actual-pens",
                pen_id="black-0-4",
                ink="Blue",
                nominal_nib_mm=0.4,
            )
            mismatched_report = validate(mismatched_path, spec, "a5-portrait")

        self.assertTrue(
            any(
                "pen id 'blue-imaginary-0-4'" in failure
                and "not in inventory 'actual-pens'" in failure
                for failure in unknown_report.failures
            )
        )
        self.assertTrue(
            any(
                "pen id 'black-0-4' is 'Black'" in failure
                for failure in mismatched_report.failures
            )
        )

    def test_path_pen_id_must_match_its_inventory_checked_layer(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        svg_namespace = "http://www.w3.org/2000/svg"
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.25,
                layer_label="01 — Fine roads — Black 0.25",
            )
            self._add_pen_identity(
                path,
                profile_id="actual-pens",
                pen_id="black-0-25",
                ink="Black",
                nominal_nib_mm=0.25,
            )
            tree = ET.parse(path)
            plotted_path = tree.getroot().find(f".//{{{svg_namespace}}}path")
            assert plotted_path is not None
            plotted_path.set("data-plot-pen-id", "blue-0-25")
            tree.write(path, encoding="utf-8", xml_declaration=True)
            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any(
                "uses pen id 'blue-0-25', not its layer pen id 'black-0-25'" in failure
                for failure in report.failures
            )
        )

    def test_custom_embedded_inventory_is_authoritative_for_custom_profile(
        self,
    ) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        inventory = {
            "id": "custom-studio",
            "pens": [
                {
                    "id": "grey-wide",
                    "ink": "Grey",
                    "nominal_nib_mm": 0.6,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.6,
                layer_label="01 — Custom broad grey — Grey 0.6",
            )
            self._add_pen_identity(
                path,
                profile_id="custom-studio",
                pen_id="grey-wide",
                ink="Grey",
                nominal_nib_mm=0.6,
                embedded_inventory=inventory,
            )
            report = validate(path, spec, "a5-portrait")

        self.assertEqual(report.failures, [])

    def test_legacy_plate_without_inventory_identity_keeps_numeric_review_rule(
        self,
    ) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.6,
                layer_label="01 — Legacy broad grey — Grey 0.6",
            )
            report = validate(path, spec, "a5-portrait")

        self.assertEqual(report.failures, [])

    def test_nominal_nib_does_not_hide_invalid_effective_width(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(directory, effective_nib_mm=0.0)
            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any("invalid effective nib width" in failure for failure in report.failures)
        )

    def test_cubic_arc_length_clears_gate_when_its_chord_does_not(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.25,
                path_data="M 20 40 C 20 40.6 20.5 40.6 20.5 40",
            )
            report = validate(path, spec, "a5-portrait")

        self.assertEqual(report.failures, [])

    def test_short_cubic_arc_still_fails_three_nib_gate(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.25,
                path_data="M 20 40 C 20 40.1 20.2 40.1 20.2 40",
            )
            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any("1 sub-nib strokes" in failure for failure in report.failures)
        )

    def test_stroke_exactly_on_generated_minimum_passes(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.25,
                path_data="M 20 40 L 20.75 40",
            )
            report = validate(path, spec, "a5-portrait")

        self.assertEqual(report.failures, [])

    def test_minimum_stroke_uses_selected_formats_generated_rule(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        rules = spec["formats"]["a5-portrait"]["rules"]["min_stroke_mm_by_nib"]
        for nib in rules:
            rules[nib] = 4 * float(nib)
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.25,
                path_data="M 20 40 L 20.8 40",
            )
            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any(
                "1 sub-nib strokes" in failure and "(4 x nib)" in failure
                for failure in report.failures
            )
        )

    def test_ink_budget_passes_at_threshold_and_fails_above_it(self) -> None:
        spec = self._spec_with_ink_limit(2.0)
        with tempfile.TemporaryDirectory() as directory:
            threshold_path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.25,
                path_data="M 20 40 L 28 40",
            )
            threshold_report = validate(threshold_path, spec, "a5-portrait")
            above_path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.25,
                path_data="M 20 40 L 28.01 40",
            )
            above_report = validate(above_path, spec, "a5-portrait")

        # Ink coverage is advisory, not blocking: the studio has chosen density
        # over the legibility budget. It must still be REPORTED accurately.
        self.assertFalse(
            any(
                "map field ink coverage" in message
                for message in threshold_report.advisories
            )
        )
        self.assertTrue(
            any(
                "map field ink coverage" in message
                for message in above_report.advisories
            )
        )
        self.assertFalse(
            any(
                "map field ink coverage" in failure
                for failure in above_report.failures
            ),
            "ink coverage must never block a build",
        )

    def test_crossing_path_is_clipped_to_exact_map_field(self) -> None:
        field = json.loads(SPEC_PATH.read_text(encoding="utf-8"))["formats"][
            "a5-portrait"
        ]["zones_mm"]["map_field"]
        field_box = (
            field["x"],
            field["y"],
            field["x"] + field["width"],
            field["y"] + field["height"],
        )

        self.assertAlmostEqual(
            _polyline_length_in_box(((6.0, 100.0), (142.0, 100.0)), field_box),
            124.0,
            places=9,
        )

    def test_frame_and_north_count_but_outside_furniture_does_not(self) -> None:
        base_spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        field = base_spec["formats"]["a5-portrait"]["zones_mm"]["map_field"]
        top = field["y"]
        path_data = f"M 6 {top:g} L 142 {top:g} M 130 150 L 130 160 M 20 170 L 120 170"
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.25,
                path_data=path_data,
                layer_label="94 — Map frame / north mark — Black 0.25",
                layer_id="frame",
            )
            under_report = validate(
                path, self._spec_with_ink_limit(33.0), "a5-portrait"
            )
            over_report = validate(path, self._spec_with_ink_limit(34.0), "a5-portrait")

        self.assertTrue(
            any(
                "map field ink coverage" in message
                for message in under_report.advisories
            )
        )
        self.assertFalse(
            any(
                "map field ink coverage" in message
                for message in over_report.advisories
            )
        )

    def test_relative_cubic_remains_outside_absolute_path_contract(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_calibrated_plate(
                directory,
                effective_nib_mm=0.25,
                path_data="M 20 40 c 0 0.6 0.5 0.6 0.5 0",
            )
            report = validate(path, spec, "a5-portrait")

        self.assertTrue(
            any(
                "only absolute M/L/C/Z allowed" in failure
                for failure in report.failures
            )
        )

    def test_examples_do_not_regress(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        for name, allowed in BASELINE_FAILURES.items():
            path = ROOT / "examples" / name
            if not path.exists():
                continue
            with self.subTest(example=name):
                report = validate(path, spec, None)
                self.assertLessEqual(
                    len(report.failures),
                    allowed,
                    f"{name} now has {len(report.failures)} conformance failures "
                    f"(baseline {allowed}):\n  " + "\n  ".join(report.failures[:10]),
                )

    def test_regenerated_output_is_fully_conformant(self) -> None:
        """Anything written to output/ must pass outright -- no baseline, no debt."""
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        output_directory = ROOT / "output"
        produced = (
            sorted(output_directory.rglob("*.svg")) if output_directory.is_dir() else []
        )
        if not produced:
            self.skipTest("output/ contains no regenerated SVG artifacts")
        for path in produced:
            with self.subTest(output=path.relative_to(output_directory)):
                report = validate(path, spec, None)
                self.assertEqual(
                    report.failures,
                    [],
                    f"{path.name} is off-format:\n  "
                    + "\n  ".join(report.failures[:10]),
                )


if __name__ == "__main__":
    unittest.main()
