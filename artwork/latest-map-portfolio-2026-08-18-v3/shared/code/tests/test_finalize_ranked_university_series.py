from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from finalize_ranked_university_series import (  # noqa: E402
    FinalizeError,
    UK_COLLECTION,
    _build_contact_sheet,
    finalize,
)


class RankedUniversityFinalizationTests(unittest.TestCase):
    def test_contact_sheet_is_opaque_8_bit_rgb_at_254_dpi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plate = root / "plate.png"
            plate.write_bytes(b"plate")
            output = root / "contact.png"
            captured: list[str] = []

            def fake_run(command: list[str], **_kwargs: object) -> object:
                captured.extend(command)
                output.write_bytes(b"contact")

                class Result:
                    returncode = 0
                    stderr = ""

                return Result()

            with (
                patch(
                    "finalize_ranked_university_series.shutil.which",
                    return_value="/usr/bin/magick",
                ),
                patch(
                    "finalize_ranked_university_series.subprocess.run",
                    side_effect=fake_run,
                ),
            ):
                _build_contact_sheet([plate], output)

            expected_options = [
                "-background",
                "white",
                "-alpha",
                "remove",
                "-alpha",
                "off",
                "-depth",
                "8",
                "-units",
                "PixelsPerInch",
                "-density",
                "254",
                "-define",
                "png:color-type=2",
            ]
            start = captured.index("-background")
            self.assertEqual(captured[start : start + len(expected_options)], expected_options)

    def _pilot(self, root: Path) -> Path:
        plate = root / UK_COLLECTION / "001-uk-university-lse.png"
        plate.parent.mkdir(parents=True)
        plate.write_bytes(b"pilot-png")
        cache = root / "source-cache/overpass" / ("a" * 64 + ".json.gz")
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"pilot-source-cache")
        manifest = plate.with_suffix(".plot.json")
        manifest.write_text(
            json.dumps({"source": {"cache_path": str(cache)}}),
            encoding="utf-8",
        )
        report = root / "ranked-universities.batch.json"
        report.write_text(
            json.dumps(
                {
                    "output_dir": str(root),
                    "items": [
                        {
                            "collection_id": UK_COLLECTION,
                            "position": 1,
                            "subject_id": "uk-university-lse",
                            "status": "completed",
                            "png": str(plate),
                            "manifest": str(manifest),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return report

    def test_pilot_writes_contact_finalization_and_exact_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            report = self._pilot(root)

            def fake_contact(_plates: list[Path], output: Path) -> None:
                output.write_bytes(b"contact-png")

            with patch(
                "finalize_ranked_university_series._build_contact_sheet",
                side_effect=fake_contact,
            ):
                result = finalize(report, allow_incomplete=True)

            self.assertEqual(result["status"], "pilot")
            self.assertEqual(result["completed_plate_count"], 1)
            checksums = (root / "CHECKSUMS.sha256").read_text(encoding="utf-8")
            self.assertIn("FINALIZATION.json", checksums)
            self.assertIn("ranked-universities.batch.json", checksums)
            self.assertIn("uk-ranked-universities-contact-sheet.png", checksums)
            self.assertIn("001-uk-university-lse.png", checksums)
            self.assertIn("001-uk-university-lse.plot.json", checksums)
            self.assertIn("source-cache/overpass/" + "a" * 64 + ".json.gz", checksums)
            self.assertNotIn("CHECKSUMS.sha256", checksums)

    def test_pinned_json_set_finalizes_from_release_metadata_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            report = self._pilot(root)
            report_value = json.loads(report.read_text(encoding="utf-8"))
            manifest_path = Path(report_value["items"][0]["manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            old_source = Path(manifest["source"]["cache_path"])
            source_root = root / "release-metadata/source-snapshots"
            new_source = source_root / "overpass/uk-university-lse.json.gz"
            new_source.parent.mkdir(parents=True)
            old_source.replace(new_source)
            manifest["source"]["cache_path"] = str(new_source)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (source_root / "source-manifest.json").write_text(
                "{}\n", encoding="utf-8"
            )
            (root / "SERIES-CONTRACT.json").write_text(
                json.dumps(
                    {
                        "source_contract": {
                            "mode": "pinned-input-json-set",
                            "path": (
                                "release-metadata/source-snapshots/"
                                "source-manifest.json"
                            ),
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "finalize_ranked_university_series._build_contact_sheet",
                side_effect=lambda _plates, output: output.write_bytes(b"contact"),
            ):
                finalize(report, allow_incomplete=True)

            checksums = (root / "CHECKSUMS.sha256").read_text(encoding="utf-8")
            self.assertIn(
                "release-metadata/source-snapshots/overpass/"
                "uk-university-lse.json.gz",
                checksums,
            )

    def test_strict_finalization_rejects_a_pilot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = self._pilot(Path(directory).resolve())
            with self.assertRaisesRegex(FinalizeError, "expected 30"):
                finalize(report)

    def test_forbidden_transients_are_rejected_in_any_directory(self) -> None:
        cases = (
            "ranked-universities.batch.json.lock",
            "nested/render.123.tmp",
            "release-metadata/__pycache__/module.py",
            "release-metadata/module.pyc",
            "release-metadata/module.pyo",
        )
        for relative in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                report = self._pilot(root)
                transient = root / relative
                transient.parent.mkdir(parents=True, exist_ok=True)
                transient.write_bytes(b"transient")
                with self.assertRaisesRegex(
                    FinalizeError, "forbidden transient paths"
                ):
                    finalize(report, allow_incomplete=True)

    def test_only_exact_root_qa_report_names_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            report = self._pilot(root)
            excluded = (
                root / "RANKED_UNIVERSITY_QA_REPORT.json",
                root / "RANKED_UNIVERSITY_QA_REPORT-pilot.json",
            )
            included = (
                root / "RANKED_UNIVERSITY_QA_REPORT-copy.json",
                root / "nested/RANKED_UNIVERSITY_QA_REPORT.json",
            )
            for path in (*excluded, *included):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")

            with patch(
                "finalize_ranked_university_series._build_contact_sheet",
                side_effect=lambda _plates, output: output.write_bytes(b"contact"),
            ):
                finalize(report, allow_incomplete=True)

            checksums = (root / "CHECKSUMS.sha256").read_text(encoding="utf-8")
            declared = {
                line.split("  ", maxsplit=1)[1]
                for line in checksums.splitlines()
            }
            for path in excluded:
                self.assertNotIn(path.relative_to(root).as_posix(), declared)
            for path in included:
                self.assertIn(path.relative_to(root).as_posix(), declared)

    def test_unreferenced_source_cache_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            report = self._pilot(root)
            stale = root / "source-cache/overpass" / ("b" * 64 + ".json.gz")
            stale.write_bytes(b"stale")

            with self.assertRaisesRegex(FinalizeError, "unreferenced source-cache"):
                finalize(report, allow_incomplete=True)

    def test_missing_manifest_cache_reference_and_file_are_rejected(self) -> None:
        for mode in ("missing-reference", "missing-file"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                report = self._pilot(root)
                report_value = json.loads(report.read_text(encoding="utf-8"))
                manifest = Path(report_value["items"][0]["manifest"])
                manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
                cache = Path(manifest_value["source"]["cache_path"])
                if mode == "missing-reference":
                    manifest_value["source"] = {}
                    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
                    expected = "source cache is missing"
                else:
                    cache.unlink()
                    expected = "referenced source-cache file is missing"

                with self.assertRaisesRegex(FinalizeError, expected):
                    finalize(report, allow_incomplete=True)

    def test_duplicate_manifest_cache_reference_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            report = self._pilot(root)
            report_value = json.loads(report.read_text(encoding="utf-8"))
            first = report_value["items"][0]
            second_plate = (
                root / UK_COLLECTION / "002-uk-university-st-andrews.png"
            )
            second_plate.write_bytes(b"second-pilot-png")
            second_manifest = second_plate.with_suffix(".plot.json")
            first_manifest = json.loads(
                Path(first["manifest"]).read_text(encoding="utf-8")
            )
            second_manifest.write_text(json.dumps(first_manifest), encoding="utf-8")
            report_value["items"].append(
                {
                    "collection_id": UK_COLLECTION,
                    "position": 2,
                    "subject_id": "uk-university-st-andrews",
                    "status": "completed",
                    "png": str(second_plate),
                    "manifest": str(second_manifest),
                }
            )
            report.write_text(json.dumps(report_value), encoding="utf-8")

            with self.assertRaisesRegex(FinalizeError, "duplicate source-cache"):
                finalize(report, allow_incomplete=True)

    def test_cache_reference_path_escape_and_traversal_are_rejected(self) -> None:
        for mode in ("traversal", "outside-cache", "outside-release"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                report = self._pilot(root)
                report_value = json.loads(report.read_text(encoding="utf-8"))
                manifest = Path(report_value["items"][0]["manifest"])
                manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
                if mode == "traversal":
                    cache_path = "source-cache/../outside.json.gz"
                    expected = "path traversal"
                elif mode == "outside-cache":
                    cache_path = str(root / "outside.json.gz")
                    expected = "outside the release source-cache"
                else:
                    cache_path = str(root.parent / "outside.json.gz")
                    expected = "escapes the release root"
                manifest_value["source"]["cache_path"] = cache_path
                manifest.write_text(json.dumps(manifest_value), encoding="utf-8")

                with self.assertRaisesRegex(FinalizeError, expected):
                    finalize(report, allow_incomplete=True)


if __name__ == "__main__":
    unittest.main()
