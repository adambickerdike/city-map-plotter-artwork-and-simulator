"""The course is the one line on a race plate a buyer reads as fact."""

from __future__ import annotations

import json
import math
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from city_map_plotter.furniture import (
    append_race_course,
    append_rowing_course_copy,
    with_split_zones,
)
from city_map_plotter.geometry import (
    expand_bbox_to_aspect,
    load_plate_format,
    make_poster_layout,
)
from city_map_plotter.models import MapPlotterError
from city_map_plotter.pens import ACTUAL_PEN_INVENTORY
from city_map_plotter.rowing import (
    ROWING_COURSE_CATALOG_ID,
    course_extent,
    course_pen_plan,
    load_rowing_course,
    load_rowing_courses,
    project_course,
)
from city_map_plotter.styles import race_course_ink, race_course_target_mm
from city_map_plotter.svgkit import PATH_NUMBER

RESOURCE = (
    Path(__file__).parents[1]
    / "src"
    / "city_map_plotter"
    / "data"
    / "rowing-courses-v1.json"
)
COURSES = ("horr-london", "pairs-head-london", "henley-royal", "head-of-the-charles")
LADDER = (0.25, 0.4, 0.6, 0.7, 1.0)


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    radius = 6_371_008.8
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(h)))


def _layout(course_id: str, preset: str = "a3-balanced-poster"):
    course = load_rowing_course(course_id)
    bbox = course_extent(course, margin=0.15)
    plate = load_plate_format("a3-portrait")
    bbox = expand_bbox_to_aspect(bbox, float(plate["map_field_aspect"]))
    return course, with_split_zones(make_poster_layout(bbox, preset=preset))


class CourseCatalogTests(unittest.TestCase):
    def test_all_four_head_courses_ship(self) -> None:
        self.assertEqual(set(load_rowing_courses()), set(COURSES))

    def test_unknown_course_lists_the_ones_that_exist(self) -> None:
        with self.assertRaisesRegex(MapPlotterError, "Choose from:.*henley-royal"):
            load_rowing_course("boat-race")

    def test_every_course_records_its_own_provenance(self) -> None:
        for course_id in COURSES:
            with self.subTest(course=course_id):
                course = load_rowing_course(course_id)
                evidence = course.as_dict()
                self.assertEqual(evidence["geometry_source"], "openstreetmap")
                self.assertEqual(evidence["geometry_licence"], "ODbL 1.0")
                self.assertEqual(evidence["catalog_id"], ROWING_COURSE_CATALOG_ID)
                self.assertTrue(course.source_urls)
                self.assertIn("centre-line", evidence["geometry_derivation"])
                # The claim is bounded on the plate as well as in the manifest.
                self.assertIn("not a survey of the raced line", evidence["claim_scope"])

    def test_measured_length_is_close_to_the_published_distance(self) -> None:
        # The generator refuses anything worse than 12%; assert the shipped
        # courses are actually well inside that, so a silent drift shows up
        # here rather than on paper.
        for course_id in COURSES:
            with self.subTest(course=course_id):
                course = load_rowing_course(course_id)
                self.assertLess(abs(course.relative_error), 0.10, course_id)

    def test_waypoints_measure_back_to_the_recorded_length(self) -> None:
        for course_id in COURSES:
            with self.subTest(course=course_id):
                course = load_rowing_course(course_id)
                walked = sum(
                    _haversine_m(a, b)
                    for a, b in zip(course.waypoints, course.waypoints[1:])
                )
                self.assertAlmostEqual(
                    walked / course.measured_length_m, 1.0, delta=0.01
                )

    def test_the_line_actually_starts_and_ends_at_its_named_places(self) -> None:
        for course_id in COURSES:
            with self.subTest(course=course_id):
                course = load_rowing_course(course_id)
                head = course.waypoints[0]
                tail = course.waypoints[-1]
                start = (course.start.lat, course.start.lon)
                finish = (course.finish.lat, course.finish.lon)
                # Endpoints are bank features, so they sit off the centre-line;
                # 250 m is the width of a river plus a boathouse, not a
                # different reach.
                self.assertLess(_haversine_m(head, start), 250.0)
                self.assertLess(_haversine_m(tail, finish), 250.0)

    def test_the_shipped_file_is_generated_not_hand_written(self) -> None:
        document = json.loads(RESOURCE.read_text(encoding="utf-8"))
        self.assertEqual(document["id"], ROWING_COURSE_CATALOG_ID)
        self.assertIn("OpenStreetMap", document["attribution"])
        for record in document["courses"]:
            self.assertEqual(record["geometry"]["resample_step_m"], 25.0)


class CourseFramingTests(unittest.TestCase):
    def test_the_extent_contains_the_whole_course(self) -> None:
        for course_id in COURSES:
            with self.subTest(course=course_id):
                course = load_rowing_course(course_id)
                extent = course_extent(course)
                for lat, lon in course.waypoints:
                    self.assertGreaterEqual(lat, extent.south)
                    self.assertLessEqual(lat, extent.north)
                    self.assertGreaterEqual(lon, extent.west)
                    self.assertLessEqual(lon, extent.east)

    def test_a_bigger_margin_leaves_more_paper_around_the_race(self) -> None:
        course = load_rowing_course("henley-royal")
        tight = course_extent(course, margin=0.02)
        loose = course_extent(course, margin=0.25)
        self.assertLess(loose.south, tight.south)
        self.assertGreater(loose.north, tight.north)

    def test_the_projected_course_lands_inside_the_map_field(self) -> None:
        for course_id in COURSES:
            with self.subTest(course=course_id):
                course, layout = _layout(course_id)
                parts = project_course(course, layout)
                self.assertTrue(parts)
                left, top, right, bottom = layout.clip_rect
                for part in parts:
                    for x, y in part:
                        self.assertGreaterEqual(x, left - 1e-6)
                        self.assertLessEqual(x, right + 1e-6)
                        self.assertGreaterEqual(y, top - 1e-6)
                        self.assertLessEqual(y, bottom + 1e-6)


class CoursePenTests(unittest.TestCase):
    def test_the_course_is_wider_than_every_road_on_the_sheet(self) -> None:
        for format_id in ("a5-portrait", "a4-portrait", "a3-portrait"):
            with self.subTest(format=format_id):
                plate = load_plate_format(format_id)
                heaviest_road = max(plate["map_linework_nib_mm"].values())
                self.assertGreater(race_course_target_mm(format_id), heaviest_road)

    def test_the_width_is_built_from_a_real_pen_not_an_invented_nib(self) -> None:
        for format_id in ("a5-portrait", "a4-portrait", "a3-portrait"):
            with self.subTest(format=format_id):
                plan = course_pen_plan(
                    format_id=format_id,
                    pen_inventory=ACTUAL_PEN_INVENTORY,
                    allowed_nibs_mm=LADDER,
                )
                self.assertEqual(plan.pen.ink, race_course_ink(format_id))
                self.assertIn(plan.pen.nominal_nib_mm, LADDER)
                self.assertGreaterEqual(plan.stroke_count, 1)
                self.assertAlmostEqual(
                    plan.plotted_width_mm,
                    race_course_target_mm(format_id),
                    delta=0.06,
                )


class CoursePlateTests(unittest.TestCase):
    def _emit(self, course_id: str):
        course, layout = _layout(course_id)
        root = ET.Element("svg")
        stats: list[dict[str, object]] = []
        evidence = append_race_course(
            root,
            layout,
            stats,
            course=course,
            pen_inventory=ACTUAL_PEN_INVENTORY,
            allowed_nibs_mm=LADDER,
        )
        return course, layout, root, stats, evidence

    def test_the_course_is_emitted_as_its_own_contracted_layer(self) -> None:
        _, _, root, stats, evidence = self._emit("henley-royal")
        group = next(iter(root))
        self.assertEqual(group.get("id"), "layer-race_course")
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["id"], "race_course")
        self.assertTrue(stats[0]["emitted"])
        self.assertEqual(evidence["course_id"], "henley-royal")
        self.assertGreater(evidence["drawn_paths"], 0)

    def test_a_bar_is_drawn_across_the_water_at_each_end(self) -> None:
        _, _, root, _, evidence = self._emit("henley-royal")
        bars = [
            path
            for path in root.iter()
            if path.get("data-course-part") == "end-bar"
        ]
        self.assertEqual(len(bars), 2)
        self.assertEqual(evidence["end_bars_drawn"], 2)

    def test_every_emitted_course_stroke_clears_the_three_nib_floor(self) -> None:
        for course_id in COURSES:
            with self.subTest(course=course_id):
                _, _, root, stats, _ = self._emit(course_id)
                floor = 3 * float(stats[0]["nib_mm"])
                for path in root.iter():
                    if path.get("d") is None:
                        continue
                    numbers = [float(v) for v in PATH_NUMBER.findall(path.get("d", ""))]
                    points = list(zip(numbers[0::2], numbers[1::2]))
                    length = sum(
                        math.dist(a, b) for a, b in zip(points, points[1:])
                    )
                    self.assertGreaterEqual(length + 1e-9, floor)

    def test_the_manifest_carries_both_distances(self) -> None:
        course, _, _, _, evidence = self._emit("head-of-the-charles")
        self.assertEqual(evidence["official_distance_m"], 4800.0)
        self.assertEqual(
            evidence["measured_centreline_m"], round(course.measured_length_m, 1)
        )
        self.assertEqual(evidence["official_distance_label"], "3 MILES / 4.8 KM")

    def test_a_course_off_the_sheet_is_refused_rather_than_half_drawn(self) -> None:
        course = load_rowing_course("head-of-the-charles")
        _, elsewhere = _layout("henley-royal")
        with self.assertRaisesRegex(MapPlotterError, "does not intersect the map field"):
            append_race_course(
                ET.Element("svg"),
                elsewhere,
                [],
                course=course,
                pen_inventory=ACTUAL_PEN_INVENTORY,
                allowed_nibs_mm=LADDER,
            )


class CourseCopyTests(unittest.TestCase):
    def test_the_footer_prints_the_facts_in_the_memorabilia_cells(self) -> None:
        course, layout = _layout("horr-london")
        root = ET.Element("svg")
        stats: list[dict[str, object]] = []
        append_rowing_course_copy(
            root,
            layout,
            course=course,
            layer_stats=stats,
            pen_inventory=ACTUAL_PEN_INVENTORY,
            allowed_nibs_mm=LADDER,
        )
        groups = {group.get("id") for group in root.iter() if group.get("id")}
        self.assertIn("layer-poster_title", groups)
        self.assertIn("layer-poster_subtitle", groups)
        self.assertIn("layer-poster_personalisation", groups)
        footer = next(
            group
            for group in root.iter()
            if group.get("id") == "layer-poster_personalisation"
        )
        printed = json.loads(footer.get("data-fields-json") or "{}")
        self.assertEqual(printed["person_name"], course.poster["course_line"])
        self.assertEqual(printed["degree"], "4 MILES 374 YDS")
        self.assertEqual(printed["honours"], "1926")
        self.assertEqual(printed["years"], "EIGHTS")

    def test_every_course_has_copy_that_fits_its_footer(self) -> None:
        for course_id in COURSES:
            with self.subTest(course=course_id):
                course, layout = _layout(course_id)
                append_rowing_course_copy(
                    ET.Element("svg"),
                    layout,
                    course=course,
                    layer_stats=[],
                    pen_inventory=ACTUAL_PEN_INVENTORY,
                    allowed_nibs_mm=LADDER,
                )

    def test_the_rowing_layout_refuses_to_render_without_a_course(self) -> None:
        from city_map_plotter.furniture import append_poster_decoration

        _, layout = _layout("henley-royal")
        with self.assertRaisesRegex(MapPlotterError, "needs a course"):
            append_poster_decoration(
                ET.Element("svg"),
                layout,
                title="X",
                subtitle=None,
                detail_lines=(),
                poster_layout="rowing-course",
                person_name=None,
                degree=None,
                honours=None,
                years=None,
                layer_stats=[],
                rowing_course=None,
                pen_inventory=ACTUAL_PEN_INVENTORY,
                allowed_nibs_mm=LADDER,
            )


if __name__ == "__main__":
    unittest.main()
