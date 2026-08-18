"""Paper selection, per-sheet nib resolution, and simulator truthfulness.

These tests exist because three separate defects were possible before them:
an A4 plate silently inheriting A5's nib ladder, a requested width that no real
pen can draw reaching the plotter, and a simulator that reported the layer
caption rather than the physical plan.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from city_map_plotter.cartography import (
    DEFAULT_LANDMARK_POLICY,
    landmark_building_policy,
)
from city_map_plotter.geometry import (
    POSTER_PRESETS,
    load_plate_format,
    make_poster_layout,
    orientation_for_extent,
    plate_nib_ladder_mm,
    poster_plate_for_extent,
    poster_plate_format_id,
    poster_preset_composition,
    poster_sheet_name,
)
from city_map_plotter.models import BoundingBox, MapPlotterError, PlotStroke
from city_map_plotter.pens import (
    ACTUAL_NIB_LADDER_MM,
    ACTUAL_PEN_INVENTORY,
    fit_pen_width,
)
from city_map_plotter.physical import compile_physical_strokes
from city_map_plotter.styles import (
    DEFAULT_STYLES,
    MAP_LINEWORK_NIB_ROLES,
    load_styles,
    map_linework_nib_mm,
    map_linework_nib_role,
    race_course_ink,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from plotsim import (  # noqa: E402
    Machine,
    _offset_geometry_warnings,
    load_plate,
    order_strokes,
    simulate,
)

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
ALL_FORMATS = (
    "a5-portrait",
    "a5-landscape",
    "a4-portrait",
    "a4-landscape",
    "a3-portrait",
    "a3-landscape",
)
YORK = BoundingBox(-1.112, 53.9404, -1.0509, 53.9778)


def _plate_svg(layers: list[dict[str, object]], *, width=210.0, height=297.0) -> str:
    """Build a minimal plate whose captions and metadata can disagree."""

    parts = [
        f'<svg xmlns="{SVG_NS}" xmlns:inkscape="{INKSCAPE_NS}" '
        f'width="{width}mm" height="{height}mm" viewBox="0 0 {width} {height}">'
    ]
    for layer in layers:
        attributes = " ".join(
            f'{key}="{value}"'
            for key, value in layer.items()
            if key not in {"label", "paths"}
        )
        parts.append(
            f'<g inkscape:groupmode="layer" inkscape:label="{layer["label"]}" '
            f'stroke="#111111" {attributes}>'
        )
        for path in layer["paths"]:  # type: ignore[index]
            parts.append(f'<path d="{path}"/>')
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


class PosterPlateSelectionTests(unittest.TestCase):
    def test_every_poster_preset_binds_a_specified_plate(self) -> None:
        self.assertEqual(
            POSTER_PRESETS,
            {
                "a5-clean-poster",
                "a5-balanced-poster",
                "a4-clean-poster",
                "a4-balanced-poster",
                "a3-clean-poster",
                "a3-balanced-poster",
            },
        )
        for preset in POSTER_PRESETS:
            with self.subTest(preset=preset):
                format_id = poster_plate_format_id(preset)
                self.assertIsNotNone(format_id)
                assert format_id is not None
                self.assertIn(format_id, ALL_FORMATS)
                # The sheet the preset names is the sheet the plate resolves to.
                self.assertEqual(
                    load_plate_format(format_id)["sheet"].upper(),
                    poster_sheet_name(preset),
                )

    def test_orientation_selects_the_specified_landscape_plate(self) -> None:
        self.assertEqual(
            poster_plate_format_id("a4-balanced-poster", orientation="landscape"),
            "a4-landscape",
        )
        self.assertEqual(
            poster_plate_format_id("a5-clean-poster", orientation="landscape"),
            "a5-landscape",
        )
        self.assertEqual(
            poster_plate_format_id("a4-clean-poster", orientation="portrait"),
            "a4-portrait",
        )
        with self.assertRaises(MapPlotterError):
            poster_plate_format_id("a4-clean-poster", orientation="sideways")

    def test_compositions_are_shared_across_both_sheets(self) -> None:
        self.assertEqual(poster_preset_composition("a5-balanced-poster"), "balanced")
        self.assertEqual(poster_preset_composition("a4-balanced-poster"), "balanced")
        self.assertEqual(poster_preset_composition("a4-clean-poster"), "clean")
        self.assertIsNone(poster_preset_composition("standard"))

    def test_poster_layout_takes_page_and_zones_from_the_named_plate(self) -> None:
        for format_id in ALL_FORMATS:
            with self.subTest(format_id=format_id):
                plate = load_plate_format(format_id)
                layout = make_poster_layout(
                    YORK, preset="a4-balanced-poster", format_id=format_id
                )
                self.assertEqual(layout.format_id, format_id)
                self.assertAlmostEqual(
                    layout.page.width_mm, plate["page_mm"]["width"], places=6
                )
                self.assertAlmostEqual(
                    layout.page.height_mm, plate["page_mm"]["height"], places=6
                )
                self.assertAlmostEqual(
                    layout.margin_mm, plate["safe_margin_mm"], places=6
                )
                field = plate["zones_mm"]["map_field"]
                self.assertAlmostEqual(
                    layout.zones["map_field"].width_mm, field["width"], places=6
                )
                # The rendered map never spills out of its specified field.
                self.assertLessEqual(layout.map_width_mm, field["width"] + 1e-6)
                self.assertLessEqual(layout.map_height_mm, field["height"] + 1e-6)

    def test_a_preset_without_a_plate_is_refused(self) -> None:
        with self.assertRaises(MapPlotterError):
            make_poster_layout(YORK, preset="standard")

    def test_nib_ladder_comes_from_the_plate_not_a_constant(self) -> None:
        for format_id in ALL_FORMATS:
            with self.subTest(format_id=format_id):
                self.assertEqual(
                    plate_nib_ladder_mm(format_id),
                    tuple(load_plate_format(format_id)["nib_ladder_mm"]),
                )
        self.assertIsNone(plate_nib_ladder_mm(None))


class MapLineworkNibTests(unittest.TestCase):
    def test_linework_widths_are_read_from_the_active_plate(self) -> None:
        for format_id in ALL_FORMATS:
            table = load_plate_format(format_id)["map_linework_nib_mm"]
            for layer_id in MAP_LINEWORK_NIB_ROLES:
                if layer_id == "race_course":
                    continue  # specified separately; covered by RaceCourseTests
                with self.subTest(format_id=format_id, layer=layer_id):
                    role = map_linework_nib_role(format_id, layer_id)
                    self.assertEqual(
                        map_linework_nib_mm(format_id, layer_id), table[role]
                    )

    def test_fine_end_is_held_while_the_hierarchy_scales(self) -> None:
        """The measured policy: 89% of ink length is in the two finest roles."""

        a5 = load_plate_format("a5-portrait")["map_linework_nib_mm"]
        a4 = load_plate_format("a4-portrait")["map_linework_nib_mm"]
        self.assertEqual(a5["hairline"], a4["hairline"])
        self.assertEqual(a5["text"], a4["text"])
        self.assertLess(a5["primary"], a4["primary"])
        self.assertLess(a5["heavy"], a4["heavy"])

    def test_a4_poster_styles_differ_from_a5_only_where_the_plate_says(self) -> None:
        selected = set(MAP_LINEWORK_NIB_ROLES)
        a5 = {
            style.id: style
            for style in load_styles(
                None, selected, preset="a5-balanced-poster", format_id="a5-portrait"
            )
        }
        a4 = {
            style.id: style
            for style in load_styles(
                None, selected, preset="a4-balanced-poster", format_id="a4-portrait"
            )
        }
        self.assertEqual(set(a5), set(a4))
        changed = {
            layer_id for layer_id in a5 if a5[layer_id].nib_mm != a4[layer_id].nib_mm
        }
        # Exactly the primary/heavy layers move, plus the course, which has its
        # own per-sheet width. Every fine layer is held.
        self.assertEqual(
            changed,
            {
                "water_areas",
                "rivers",
                "roads_major",
                "roads_secondary",
                "race_course",
            },
        )
        for layer_id in set(a5) - changed:
            self.assertEqual(a5[layer_id].nib_mm, a4[layer_id].nib_mm)
        # The ink of a layer never changes with the paper, only its width.
        for layer_id in a5:
            self.assertEqual(a5[layer_id].ink, a4[layer_id].ink)

    def test_unknown_layer_has_no_silent_default_width(self) -> None:
        with self.assertRaises(MapPlotterError):
            map_linework_nib_mm("a4-portrait", "not_a_layer")


class RaceCourseWidthTests(unittest.TestCase):
    """The course is the subject of its plate, so it outweighs every road."""

    def test_course_is_bolder_than_every_road_on_every_sheet(self) -> None:
        for format_id in ALL_FORMATS:
            with self.subTest(format_id=format_id):
                course = map_linework_nib_mm(format_id, "race_course")
                roads = load_plate_format(format_id)["map_linework_nib_mm"]
                self.assertGreater(course, max(roads.values()))

    def test_course_is_realised_from_owned_colour_pens(self) -> None:
        """Colour exists only at 0.25/0.40, so bold means parallel offsets."""

        owned = {
            (pen.ink.casefold(), round(pen.nominal_nib_mm, 6))
            for pen in ACTUAL_PEN_INVENTORY.pens
        }
        for format_id in ALL_FORMATS:
            with self.subTest(format_id=format_id):
                ink = race_course_ink(format_id)
                fit = fit_pen_width(
                    ACTUAL_PEN_INVENTORY,
                    ink=ink,
                    requested_width_mm=map_linework_nib_mm(format_id, "race_course"),
                    allowed_nibs_mm=plate_nib_ladder_mm(format_id),
                )
                self.assertIn(
                    (fit.pen.ink.casefold(), round(fit.pen.nominal_nib_mm, 6)), owned
                )
                self.assertEqual(fit.mode, "parallel-offsets")
                self.assertGreater(fit.stroke_count, 1)
                self.assertAlmostEqual(
                    fit.plotted_width_mm,
                    fit.pen.mark_width_mm
                    + (fit.stroke_count - 1) * fit.offset_pitch_mm,
                    places=9,
                )

    def test_course_width_grows_with_the_sheet(self) -> None:
        a5 = map_linework_nib_mm("a5-portrait", "race_course")
        a4 = map_linework_nib_mm("a4-portrait", "race_course")
        a3 = map_linework_nib_mm("a3-portrait", "race_course")
        self.assertLess(a5, a4)
        self.assertLess(a4, a3)

    def test_a_plate_without_a_course_block_is_refused(self) -> None:
        with self.assertRaises(MapPlotterError):
            race_course_ink("not-a-format")


class OrientationFromExtentTests(unittest.TestCase):
    """The subject's own shape picks the archetype; nothing defaults."""

    def _bbox(self, width_deg: float, height_deg: float) -> BoundingBox:
        return BoundingBox(-0.1, 51.5, -0.1 + width_deg, 51.5 + height_deg)

    def test_a_tall_course_takes_the_portrait_stack(self) -> None:
        for sheet in ("A4", "A3"):
            self.assertEqual(
                orientation_for_extent(self._bbox(0.05, 0.20), sheet=sheet),
                "portrait",
            )

    def test_a_wide_course_takes_the_landscape_rail(self) -> None:
        for sheet in ("A4", "A3"):
            self.assertEqual(
                orientation_for_extent(self._bbox(0.40, 0.05), sheet=sheet),
                "landscape",
            )

    def test_the_chosen_field_is_the_closest_aspect_available(self) -> None:
        for sheet in ("A4", "A3"):
            for width, height in ((0.05, 0.20), (0.40, 0.05), (0.15, 0.10)):
                bbox = self._bbox(width, height)
                aspect = bbox.approximate_width_m / bbox.approximate_height_m
                chosen = orientation_for_extent(bbox, sheet=sheet)
                waste = {}
                for orientation in ("portrait", "landscape"):
                    field = float(
                        load_plate_format(f"{sheet.lower()}-{orientation}")[
                            "map_field_aspect"
                        ]
                    )
                    waste[orientation] = max(field / aspect, aspect / field)
                with self.subTest(sheet=sheet, w=width, h=height):
                    self.assertEqual(chosen, min(waste, key=lambda k: waste[k]))

    def test_extent_selection_resolves_a_specified_plate(self) -> None:
        wide = self._bbox(0.40, 0.05)
        self.assertEqual(
            poster_plate_for_extent(wide, preset="a3-balanced-poster"),
            "a3-landscape",
        )
        tall = self._bbox(0.05, 0.20)
        self.assertEqual(
            poster_plate_for_extent(tall, preset="a4-balanced-poster"),
            "a4-portrait",
        )

    def test_an_extreme_extent_still_resolves_a_real_plate(self) -> None:
        """BoundingBox already refuses zero spans, so nothing here can divide
        by zero; an extremely thin extent must still pick a specified plate."""

        thin = BoundingBox(-0.1, 51.5, 0.1, 51.5001)
        self.assertEqual(orientation_for_extent(thin, sheet="A4"), "landscape")


class NoInventedNibTests(unittest.TestCase):
    """Every width a plate can request must resolve to pens that exist."""

    def _inks(self) -> dict[str, str]:
        return {
            style.id: (style.ink or style.pen.split()[0]) for style in DEFAULT_STYLES
        }

    def test_every_role_width_on_every_sheet_fits_real_pens(self) -> None:
        owned = {
            (pen.ink.casefold(), round(pen.nominal_nib_mm, 6))
            for pen in ACTUAL_PEN_INVENTORY.pens
        }
        inks = self._inks()
        for format_id in ALL_FORMATS:
            ladder = plate_nib_ladder_mm(format_id)
            for layer_id, ink in inks.items():
                if layer_id not in MAP_LINEWORK_NIB_ROLES:
                    continue
                with self.subTest(format_id=format_id, layer=layer_id):
                    target = map_linework_nib_mm(format_id, layer_id)
                    fit = fit_pen_width(
                        ACTUAL_PEN_INVENTORY,
                        ink=ink,
                        requested_width_mm=target,
                        allowed_nibs_mm=ladder,
                    )
                    # A real pen, owned in this ink, on this sheet's ladder.
                    self.assertIn(
                        (fit.pen.ink.casefold(), round(fit.pen.nominal_nib_mm, 6)),
                        owned,
                    )
                    self.assertIn(
                        round(fit.pen.nominal_nib_mm, 6),
                        {round(width, 6) for width in ACTUAL_NIB_LADDER_MM},
                    )
                    # And it actually reaches the requested width.
                    self.assertAlmostEqual(
                        fit.plotted_width_mm,
                        fit.pen.mark_width_mm
                        + (fit.stroke_count - 1) * fit.offset_pitch_mm,
                        places=9,
                    )

    def test_blue_primary_on_a4_becomes_two_parallel_offsets(self) -> None:
        """The documented case: no blue 0.60 exists, so it is built."""

        target = map_linework_nib_mm("a4-portrait", "rivers")
        self.assertEqual(target, 0.60)
        fit = fit_pen_width(
            ACTUAL_PEN_INVENTORY,
            ink="Blue",
            requested_width_mm=target,
            allowed_nibs_mm=plate_nib_ladder_mm("a4-portrait"),
        )
        self.assertEqual(fit.mode, "parallel-offsets")
        self.assertEqual(fit.pen.nominal_nib_mm, 0.40)
        self.assertEqual(fit.stroke_count, 2)
        self.assertAlmostEqual(fit.offset_pitch_mm, 0.20, places=9)
        self.assertAlmostEqual(fit.plotted_width_mm, 0.60, places=9)

    def test_a_width_no_ink_can_reach_is_refused_not_invented(self) -> None:
        with self.assertRaises(MapPlotterError):
            fit_pen_width(
                ACTUAL_PEN_INVENTORY,
                ink="Blue",
                requested_width_mm=8.0,
                allowed_nibs_mm=ACTUAL_NIB_LADDER_MM,
            )


class LandmarkBuildingPolicyTests(unittest.TestCase):
    def _policy(self, format_id: str):
        return landmark_building_policy(
            make_poster_layout(YORK, preset="a4-balanced-poster", format_id=format_id)
        )

    def test_policy_is_read_from_the_plate(self) -> None:
        for format_id in ALL_FORMATS:
            with self.subTest(format_id=format_id):
                block = load_plate_format(format_id)["landmark_buildings"]
                policy = self._policy(format_id)
                self.assertEqual(policy.nib_role, block["nib_role"])
                self.assertEqual(policy.nib_mm, block["nib_mm"])
                self.assertEqual(
                    policy.ink_budget_field_fraction,
                    block["ink_budget_field_fraction"],
                )
                self.assertEqual(policy.minimum_area_scale, block["minimum_area_scale"])

    def test_a_bigger_sheet_carries_more_and_smaller_landmarks(self) -> None:
        a5 = self._policy("a5-portrait")
        a4 = self._policy("a4-portrait")
        self.assertLess(a5.max_source_count, a4.max_source_count)
        self.assertLess(a5.ink_budget_field_fraction, a4.ink_budget_field_fraction)
        # A5 demands a relatively larger footprint before admitting a landmark.
        self.assertGreater(a5.minimum_area_scale, a4.minimum_area_scale)
        # Every role keeps at least one slot even on the smallest sheet.
        for _name, _roles, limit in a5.role_buckets:
            self.assertGreaterEqual(limit, 1)

    def test_a_layout_without_a_plate_uses_the_documented_default(self) -> None:
        layout = make_poster_layout(
            YORK, preset="a5-clean-poster", format_id="a5-portrait"
        )
        self.assertEqual(
            landmark_building_policy(
                type(layout)(**{**layout.__dict__, "format_id": None})
            ),
            DEFAULT_LANDMARK_POLICY,
        )


class SimulatorReadsThePhysicalPlanTests(unittest.TestCase):
    machine = Machine()

    def _load(self, svg: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plate.svg"
            path.write_text(svg, encoding="utf-8")
            return load_plate(path, self.machine)

    def test_metadata_beats_a_stale_layer_caption(self) -> None:
        """A renamed layer must not change what the machine is reported to draw."""

        svg = _plate_svg(
            [
                {
                    "label": "01 — Primary roads — Black 0.25",
                    "data-plot-ink": "Black",
                    "data-plot-nib-mm": "1",
                    "data-plot-nominal-nib-mm": "1",
                    "data-plot-strokes": "1",
                    "data-plot-passes": "1",
                    "data-plot-width-mm": "1",
                    "data-plot-pen-id": "black-1",
                    "paths": ["M 10,10 L 110,10"],
                }
            ]
        )
        strokes, page = self._load(svg)
        self.assertTrue(page["metadata"])
        # The caption says 0.25; the plate says the pen is a 1.00.
        self.assertEqual(strokes[0].pen.nib_mm, 1.0)
        self.assertEqual(strokes[0].pen.id, "black-1")
        _, stats = simulate(order_strokes(strokes, "optimised"), self.machine)
        self.assertAlmostEqual(stats["ink_mm2"], 100.0, places=6)

    def test_measured_effective_width_drives_the_drawn_width(self) -> None:
        svg = _plate_svg(
            [
                {
                    "label": "01 — Local roads — Black 0.25",
                    "data-plot-ink": "Black",
                    "data-plot-nib-mm": "0.21",
                    "data-plot-nominal-nib-mm": "0.25",
                    "data-plot-calibration-state": "measured",
                    "data-plot-strokes": "1",
                    "data-plot-passes": "1",
                    "data-plot-width-mm": "0.21",
                    "data-plot-pen-id": "black-0-25",
                    "paths": ["M 10,10 L 110,10"],
                }
            ]
        )
        strokes, _ = self._load(svg)
        pen = strokes[0].pen
        self.assertTrue(pen.measured)
        self.assertEqual(pen.nib_mm, 0.21)
        self.assertEqual(pen.nominal_mm, 0.25)
        # The label says so, rather than quietly drawing the barrel size.
        self.assertIn("eff", pen.label)
        _, stats = simulate(order_strokes(strokes, "optimised"), self.machine)
        self.assertAlmostEqual(stats["ink_mm2"], 21.0, places=6)

    def test_offset_strokes_simulate_as_the_passes_they_are(self) -> None:
        """Two 0.40 offsets are two draws and two lots of ink, not one."""

        single = _plate_svg(
            [
                {
                    "label": "01 — Rivers — Blue 0.4",
                    "data-plot-ink": "Blue",
                    "data-plot-nib-mm": "0.4",
                    "data-plot-nominal-nib-mm": "0.4",
                    "data-plot-strokes": "1",
                    "data-plot-passes": "1",
                    "data-plot-width-mm": "0.4",
                    "data-plot-pen-id": "blue-0-4",
                    "paths": ["M 10,10 L 110,10"],
                }
            ]
        )
        offset = _plate_svg(
            [
                {
                    "label": "01 — Rivers — Blue 0.4",
                    "data-plot-ink": "Blue",
                    "data-plot-nib-mm": "0.4",
                    "data-plot-nominal-nib-mm": "0.4",
                    "data-plot-strokes": "2",
                    "data-plot-passes": "1",
                    "data-plot-offset-pitch-mm": "0.2",
                    "data-plot-width-mm": "0.6",
                    "data-plot-pen-id": "blue-0-4",
                    "paths": ["M 10,9.9 L 110,9.9", "M 10,10.1 L 110,10.1"],
                }
            ]
        )
        one_strokes, one_page = self._load(single)
        two_strokes, two_page = self._load(offset)
        _, one_stats = simulate(order_strokes(one_strokes, "optimised"), self.machine)
        _, two_stats = simulate(order_strokes(two_strokes, "optimised"), self.machine)
        self.assertEqual(one_stats["pen_lifts"], 1)
        self.assertEqual(two_stats["pen_lifts"], 2)
        self.assertAlmostEqual(two_stats["pen_down_mm"], 2 * one_stats["pen_down_mm"])
        self.assertAlmostEqual(two_stats["ink_mm2"], 2 * one_stats["ink_mm2"])
        self.assertGreater(two_stats["total_seconds"], one_stats["total_seconds"])
        # The declared 0.60 mark is reported, not just the 0.40 per-pass nib.
        self.assertEqual(two_page["layers"][0]["declared"]["width_mm"], 0.6)
        self.assertEqual(one_page["layers"][0]["declared"]["width_mm"], 0.4)

    def test_declared_offsets_missing_from_the_geometry_are_reported(self) -> None:
        svg = _plate_svg(
            [
                {
                    "label": "01 — Rivers — Blue 0.4",
                    "data-plot-ink": "Blue",
                    "data-plot-nib-mm": "0.4",
                    "data-plot-nominal-nib-mm": "0.4",
                    "data-plot-strokes": "2",
                    "data-plot-passes": "1",
                    "data-plot-offset-pitch-mm": "0.2",
                    "data-plot-width-mm": "0.6",
                    "data-plot-pen-id": "blue-0-4",
                    "paths": ["M 10,10 L 110,10"],
                }
            ]
        )
        _, page = self._load(svg)
        problems = _offset_geometry_warnings(page)
        self.assertEqual(len(problems), 1)
        self.assertIn("narrower than the plate claims", problems[0])

    def test_same_barrel_different_calibration_is_two_pen_loads(self) -> None:
        svg = _plate_svg(
            [
                {
                    "label": "01 — Roads — Black 0.25",
                    "data-plot-ink": "Black",
                    "data-plot-nib-mm": "0.25",
                    "data-plot-nominal-nib-mm": "0.25",
                    "data-plot-strokes": "1",
                    "data-plot-passes": "1",
                    "data-plot-width-mm": "0.25",
                    "data-plot-pen-id": "black-0-25-fresh",
                    "paths": ["M 10,10 L 110,10"],
                },
                {
                    "label": "02 — Paths — Black 0.25",
                    "data-plot-ink": "Black",
                    "data-plot-nib-mm": "0.22",
                    "data-plot-nominal-nib-mm": "0.25",
                    "data-plot-calibration-state": "measured",
                    "data-plot-strokes": "1",
                    "data-plot-passes": "1",
                    "data-plot-width-mm": "0.22",
                    "data-plot-pen-id": "black-0-25-worn",
                    "paths": ["M 10,20 L 110,20"],
                },
            ]
        )
        strokes, _ = self._load(svg)
        _, stats = simulate(order_strokes(strokes, "merged"), self.machine)
        self.assertEqual(stats["pen_changes"], 2)

    def test_a_plate_without_metadata_still_loads_and_says_so(self) -> None:
        svg = _plate_svg(
            [
                {
                    "label": "01 — Water outlines — Blue 0.3",
                    "paths": ["M 10,10 L 110,10"],
                }
            ]
        )
        strokes, page = self._load(svg)
        self.assertFalse(page["metadata"])
        self.assertEqual(strokes[0].pen.nib_mm, 0.3)


class CompiledPlateNibTests(unittest.TestCase):
    """What the physical compiler emits must be pens the studio owns.

    This runs the real compiler rather than inspecting the committed example
    plates: those predate the specification and are ratcheted separately by
    ``tests/test_format_conformance.py``.
    """

    def _compiled(self, format_id: str):
        layout = make_poster_layout(
            YORK, preset="a4-balanced-poster", format_id=format_id
        )
        selected = set(MAP_LINEWORK_NIB_ROLES)
        styles = load_styles(
            None, selected, preset="a4-balanced-poster", format_id=format_id
        )
        left, top, right, bottom = layout.clip_rect
        strokes = [
            PlotStroke(
                layer=style.id,
                part=f"{style.id}:0",
                points=[
                    (left + 5, top + 5 + index),
                    (right - 5, top + 5 + index),
                ],
                tags={},
            )
            for index, style in enumerate(styles)
        ]
        return compile_physical_strokes(
            strokes,
            styles,
            clip_rect=layout.clip_rect,
            road_style="centreline",
            pen_inventory=ACTUAL_PEN_INVENTORY,
            allowed_nibs_mm=plate_nib_ladder_mm(format_id),
        )

    def test_every_sheet_compiles_to_owned_pens_only(self) -> None:
        owned = {pen.identity for pen in ACTUAL_PEN_INVENTORY.pens}
        ladder = {round(width, 6) for width in ACTUAL_NIB_LADDER_MM}
        for format_id in ALL_FORMATS:
            result = self._compiled(format_id)
            self.assertTrue(result.strokes, f"{format_id} compiled nothing")
            for stroke in result.strokes:
                with self.subTest(format_id=format_id, layer=stroke.layer):
                    self.assertIn(stroke.tags["plot:pen-id"], owned)
                    self.assertIn(
                        round(float(stroke.tags["plot:nominal-nib-mm"]), 6), ladder
                    )
                    # The realised mark equals the offsets that build it.
                    # Compared at 0.1 um: these values are re-read from the
                    # serialised tags, so the last digit is rounding, and
                    # 0.1 um is still 2500x finer than the narrowest nib.
                    count = int(stroke.tags["plot:stroke-count"])
                    pitch = float(stroke.tags["plot:offset-pitch-mm"])
                    nib = float(stroke.tags["plot:nib-mm"])
                    self.assertAlmostEqual(
                        float(stroke.tags["plot:plotted-width-mm"]),
                        nib + (count - 1) * pitch,
                        delta=1e-4,
                    )

    def test_a4_rivers_compile_to_two_blue_offsets_a5_to_one(self) -> None:
        def river_plan(format_id: str) -> tuple[str, int, float]:
            for stroke in self._compiled(format_id).strokes:
                if stroke.layer == "rivers":
                    return (
                        stroke.tags["plot:pen-id"],
                        int(stroke.tags["plot:stroke-count"]),
                        float(stroke.tags["plot:plotted-width-mm"]),
                    )
            raise AssertionError(f"no river stroke compiled for {format_id}")

        self.assertEqual(river_plan("a5-portrait"), ("blue-0-4", 1, 0.4))
        self.assertEqual(river_plan("a4-portrait"), ("blue-0-4", 2, 0.6))

    def test_a4_major_roads_take_the_broader_black_pen(self) -> None:
        def major_pen(format_id: str) -> str:
            for stroke in self._compiled(format_id).strokes:
                if stroke.layer == "roads_major":
                    return stroke.tags["plot:pen-id"]
            raise AssertionError(f"no major road compiled for {format_id}")

        self.assertEqual(major_pen("a5-portrait"), "black-0-6")
        self.assertEqual(major_pen("a4-portrait"), "black-1")


if __name__ == "__main__":
    unittest.main()


class LandmarkAreaBudgetIdentityTests(unittest.TestCase):
    """A landmark that is re-tagged must stay findable by projected area.

    `projected_areas` is keyed by object identity, and the landmark path
    rebuilds each selected feature with `dataclasses.replace` to attach its
    role tags. That produces a NEW object, so any later lookup by `id()` --
    the buildings area budget among them -- raised KeyError and killed the
    export outright. Berlin, London and Paris all crashed on it.
    """

    def test_a_retagged_landmark_survives_the_buildings_area_budget(self) -> None:
        from city_map_plotter.cartography import prepare_clean_poster_strokes
        from city_map_plotter.models import MapFeature

        layout = make_poster_layout(
            YORK, preset="a4-balanced-poster", format_id="a4-portrait"
        )
        south, west = YORK.south, YORK.west
        north, east = YORK.north, YORK.east
        mid_lat = (south + north) / 2
        mid_lon = (west + east) / 2
        span = (north - south) / 60

        def square(lat: float, lon: float) -> list[tuple[float, float]]:
            return [
                (lat, lon),
                (lat + span, lon),
                (lat + span, lon + span),
                (lat, lon + span),
                (lat, lon),
            ]

        features = [
            MapFeature(
                layer="buildings",
                points=square(mid_lat + index * span * 2, mid_lon),
                osm_type="way",
                osm_id=str(1000 + index),
                geometry_type="area",
                tags={
                    "building": "cathedral",
                    "name": f"Minster {index}",
                },
            )
            for index in range(4)
        ]

        # The 'plot' profile is the one that applies the buildings area budget;
        # the full-cartography profiles skip it, which is why this hid.
        result = prepare_clean_poster_strokes(
            features,
            layout,
            simplify_mm=0.04,
            detail_profile="plot",
            landmark_buildings=True,
        )
        # Reaching here at all is the regression: this raised KeyError before.
        selection = result.diagnostics["landmark_buildings"]["selection"]
        self.assertTrue(result.diagnostics["landmark_buildings"]["enabled"])
        self.assertEqual(selection["candidate_object_count"], 4)
        # And the re-tagged landmarks survived the budget with their areas
        # intact, rather than being dropped or crashing the sort.
        self.assertGreater(selection["selected_object_count"], 0)
        self.assertGreater(selection["selected_outline_length_mm"], 0)
