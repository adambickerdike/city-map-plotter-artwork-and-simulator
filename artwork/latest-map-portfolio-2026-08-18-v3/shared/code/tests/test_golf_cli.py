from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from city_map_plotter.golf_cli import (
    DEFAULT_OUTPUT_DIR,
    RELEASE_ID,
    RENDERING_PRESET,
    _assert_manifest,
    build_parser,
    main,
)
from city_map_plotter.golf import (
    GREEN_FILL_INSET_MM,
    GREEN_ROUTE_TEXTURE_CLEARANCE_MM,
)
from city_map_plotter.models import MapPlotterError


class GolfCliTests(unittest.TestCase):
    def test_one_course_review_build_is_atomic_and_self_describing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "series"
            code = main(
                [
                    "build",
                    "--course",
                    "old-course-st-andrews",
                    "--output-dir",
                    str(output),
                    "--no-png",
                    "--no-split-pens",
                    "--generated-at",
                    "2026-08-04T00:00:00+00:00",
                ]
            )
            self.assertEqual(code, 0)
            series = json.loads(
                (output / "golf-course-series.json").read_text(encoding="utf-8")
            )
            self.assertEqual(series["artifact_count"], 1)
            self.assertEqual(series["catalog_count"], 25)
            self.assertEqual(series["series_id"], "golf-courses-v2")
            self.assertEqual(series["release_id"], RELEASE_ID)
            self.assertEqual(series["rendering_preset"], RENDERING_PRESET)
            self.assertEqual(series["title"], "TWENTY-FIVE ICONS OF GOLF")
            self.assertFalse(series["production_ready"])
            self.assertEqual(
                series["artifacts"][0]["subject_id"], "old-course-st-andrews"
            )
            self.assertTrue((output / "old-course-st-andrews.svg").is_file())
            self.assertTrue((output / "old-course-st-andrews.plot.json").is_file())
            manifest = json.loads(
                (output / "old-course-st-andrews.plot.json").read_text(encoding="utf-8")
            )
            rendering = manifest["rendering"]
            self.assertEqual(
                rendering["course_boundary_rendering"],
                "raw-root-boundary-omitted-selection-mask-only",
            )
            self.assertTrue(rendering["playing_envelope_emitted"])
            self.assertEqual(
                rendering["playing_envelope_rendering"],
                "grey-0.40-derived-from-source-hole-routes-and-nearby-playing-"
                "surfaces-illustrative-not-property-or-official-boundary",
            )
            self.assertEqual(
                rendering["fairway_rendering"],
                "green-0.25-source-outline-only",
            )
            self.assertEqual(
                rendering["green_and_tee_rendering"],
                "green-0.40-source-outlines-with-green-only-green-0.25-"
                "fine-line-fill-tees-outline-only",
            )
            green_coverage = rendering["green_fill_coverage"]
            self.assertEqual(green_coverage["fill_inset_mm"], GREEN_FILL_INSET_MM)
            self.assertEqual(green_coverage["fill_pen_nib_mm"], 0.25)
            self.assertEqual(
                green_coverage["gold_route_clearance_mm"],
                GREEN_ROUTE_TEXTURE_CLEARANCE_MM,
            )
            self.assertEqual(
                green_coverage["filled_source_count"],
                green_coverage["visible_source_count"],
            )
            self.assertEqual(green_coverage["physically_unfillable_source_refs"], [])
            self.assertEqual(green_coverage["uncovered_fillable_source_refs"], [])
            self.assertEqual(
                rendering["water_rendering"],
                "blue-0.40-area-outlines-with-blue-0.25-closed-dot-symbols-for-"
                "every-visible-area-linear-and-physically-narrow-water-source",
            )
            water_coverage = rendering["water_source_dot_coverage"]
            self.assertEqual(
                water_coverage["represented_source_count"],
                water_coverage["visible_source_count"],
            )
            self.assertEqual(water_coverage["uncovered_source_refs"], [])
            logical_pen_ids = {
                logical_id: layer["pen_id"]
                for layer in manifest["layers"]
                for logical_id in layer["logical_layers"]
            }
            self.assertEqual(logical_pen_ids["playing_envelope"], "grey-0-4")

            incomplete_water = copy.deepcopy(manifest)
            incomplete_water["rendering"]["water_source_dot_coverage"][
                "represented_source_count"
            ] -= 1
            with self.assertRaisesRegex(MapPlotterError, "visible water source"):
                _assert_manifest(incomplete_water, "old-course-st-andrews")

            incomplete_greens = copy.deepcopy(manifest)
            incomplete_greens["rendering"]["green_fill_coverage"][
                "uncovered_fillable_source_refs"
            ] = ["way/adversarial"]
            with self.assertRaisesRegex(MapPlotterError, "visible green"):
                _assert_manifest(incomplete_greens, "old-course-st-andrews")

            wrong_envelope_pen = copy.deepcopy(manifest)
            for layer in wrong_envelope_pen["layers"]:
                if "playing_envelope" in layer["logical_layers"]:
                    layer["pen_id"] = "grey-0-25"
            with self.assertRaisesRegex(MapPlotterError, "playing envelope"):
                _assert_manifest(wrong_envelope_pen, "old-course-st-andrews")

    def test_existing_target_and_unknown_course_fail_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "existing"
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            self.assertEqual(
                main(
                    [
                        "build",
                        "--course",
                        "old-course-st-andrews",
                        "--output-dir",
                        str(target),
                        "--no-png",
                    ]
                ),
                2,
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

            missing = Path(temporary) / "missing"
            self.assertEqual(
                main(
                    [
                        "build",
                        "--course",
                        "not-a-course",
                        "--output-dir",
                        str(missing),
                        "--no-png",
                    ]
                ),
                2,
            )
            self.assertFalse(missing.exists())

    def test_default_output_targets_the_v4_release(self) -> None:
        args = build_parser().parse_args(["build", "--all"])
        self.assertEqual(Path(args.output_dir), DEFAULT_OUTPUT_DIR)
        self.assertEqual(DEFAULT_OUTPUT_DIR, Path("output/golf-course-series-v4"))


if __name__ == "__main__":
    unittest.main()
