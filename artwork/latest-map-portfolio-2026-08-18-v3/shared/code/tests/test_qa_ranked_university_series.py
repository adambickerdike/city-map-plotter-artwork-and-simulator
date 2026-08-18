from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import struct
import sys
from xml.etree import ElementTree as ET
import zlib

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import qa_ranked_university_series as qa  # noqa: E402
import build_ranked_university_series as series_builder  # noqa: E402


CATALOG = ROOT / "src/city_map_plotter/data/ranked-universities-2026-v1.json"
ARCHIVE = series_builder.FROZEN_ARCHIVE
BASE_RENDERER = series_builder.FROZEN_RENDERER


def _catalog() -> dict[str, object]:
    value = json.loads(CATALOG.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _test_pinned_bundle(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[qa.PinnedSourceBundle, qa.RankedRow, Path]:
    catalog_failures: list[str] = []
    row = qa.validate_catalog_document(_catalog(), catalog_failures)[0]
    assert catalog_failures == []
    source_root = root / "release-metadata/source-snapshots"
    overpass = source_root / "overpass"
    overpass.mkdir(parents=True)
    response = {
        "version": 0.6,
        "generator": "pinned QA test",
        "osm3s": {
            "timestamp_osm_base": "2026-08-03T12:00:00Z",
            "copyright": (
                "The data included in this document is from OpenStreetMap. "
                "The data is made available under ODbL."
            ),
        },
        "elements": [],
    }
    snapshot = overpass / f"{row.subject_id}.json.gz"
    with snapshot.open("wb") as raw_stream:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw_stream, mtime=0
        ) as stream:
            stream.write(json.dumps(response, separators=(",", ":")).encode())
    entry = {
        "subject_id": row.subject_id,
        "path": f"overpass/{row.subject_id}.json.gz",
        "size_bytes": snapshot.stat().st_size,
        "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "canonical_json_sha256": qa._stable_digest(response),
        "query_sha256": hashlib.sha256(b"reviewed query").hexdigest(),
        "osm_base_timestamp": "2026-08-03T12:00:00Z",
        "extent_wgs84": qa._expected_extent(row),
    }
    payload = {
        "schema_version": 1,
        "id": qa.SOURCE_CONTRACT_ID,
        "status": "review-only-pinned-source",
        "as_of": "2026-08-03",
        "subject_count": 1,
        "license": qa.SOURCE_LICENSE,
        "entries": [entry],
    }
    cohort_sha = qa._stable_digest(payload)
    manifest = source_root / "source-manifest.json"
    manifest.write_text(
        json.dumps({**payload, "cohort_sha256": cohort_sha}), encoding="utf-8"
    )
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    monkeypatch.setattr(qa, "EXPECTED_SOURCE_MANIFEST_SHA256", manifest_sha)
    monkeypatch.setattr(qa, "EXPECTED_SOURCE_COHORT_SHA256", cohort_sha)
    (source_root / "NOTICE.md").write_text(
        "OpenStreetMap contributors; ODbL; openstreetmap.org/copyright\n",
        encoding="utf-8",
    )
    (source_root / "CHECKSUMS.sha256").write_text(
        f"{entry['sha256']}  {entry['path']}\n{manifest_sha}  source-manifest.json\n",
        encoding="utf-8",
    )
    failures: list[str] = []
    bundle = qa._validate_pinned_source_manifest(manifest, [row], failures)
    assert failures == []
    assert bundle is not None
    return bundle, row, snapshot


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _rgba_png(width: int = 2, height: int = 2, *, alpha: int = 255) -> bytes:
    scanlines = b"".join(
        b"\0" + bytes((255, 255, 255, alpha)) * width for _row in range(height)
    )
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
            _png_chunk(b"pHYs", struct.pack(">IIB", 10000, 10000, 1)),
            _png_chunk(b"IDAT", zlib.compress(scanlines)),
            _png_chunk(b"IEND", b""),
        )
    )


def _rgb_png(
    *,
    pixel: tuple[int, int, int] = (0, 0, 0),
    idat: bytes | None = None,
    transparency: bytes | None = None,
    phys_after_idat: bool = False,
) -> bytes:
    scanlines = b"\0" + bytes(pixel)
    chunks = [_png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))]
    phys = _png_chunk(b"pHYs", struct.pack(">IIB", 10000, 10000, 1))
    if not phys_after_idat:
        chunks.append(phys)
    if transparency is not None:
        chunks.append(_png_chunk(b"tRNS", transparency))
    chunks.append(
        _png_chunk(b"IDAT", zlib.compress(scanlines) if idat is None else idat)
    )
    if phys_after_idat:
        chunks.append(phys)
    chunks.append(_png_chunk(b"IEND", b""))
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def _indexed_png(*, alpha: int = 255) -> bytes:
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0)),
            _png_chunk(b"pHYs", struct.pack(">IIB", 10000, 10000, 1)),
            _png_chunk(b"PLTE", b"\xff\xff\xff\x00\x00\x00"),
            _png_chunk(b"tRNS", bytes((255, alpha))),
            _png_chunk(b"IDAT", zlib.compress(b"\0\x01")),
            _png_chunk(b"IEND", b""),
        )
    )


def _svg_root(groups: list[tuple[int, str, str]]) -> ET.Element:
    root = ET.Element(f"{{{qa.SVG_NS}}}svg")
    for step, identifier, path_data in groups:
        group = ET.SubElement(
            root,
            f"{{{qa.SVG_NS}}}g",
            {
                "id": identifier,
                "data-pen-step": str(step),
                "data-plot-pen-id": "grey-0-25",
            },
        )
        ET.SubElement(group, f"{{{qa.SVG_NS}}}path", {"d": path_data})
    return root


def test_catalog_contract_is_exactly_30_uk_plus_20_us() -> None:
    failures: list[str] = []
    rows = qa.validate_catalog_document(_catalog(), failures)

    assert failures == []
    assert len(rows) == 50
    assert [row.collection_id for row in rows[:30]] == [qa.UK_COLLECTION] * 30
    assert [row.collection_id for row in rows[30:]] == [qa.US_COLLECTION] * 20
    assert (
        rows[0].institution_name == "London School of Economics and Political Science"
    )
    assert rows[0].title == "LONDON"
    assert rows[30].rank == "1"


def test_catalog_digest_pin_matches_builder_qa_and_committed_asset() -> None:
    actual = hashlib.sha256(CATALOG.read_bytes()).hexdigest()

    assert qa.EXPECTED_CATALOG_SHA256 == actual
    assert series_builder.EXPECTED_CATALOG_SHA256 == actual


def test_catalog_rejects_publisher_rank_notation_and_reused_campus_seed() -> None:
    document = deepcopy(_catalog())
    collections = document["collections"]
    subjects = document["subjects"]
    assert isinstance(collections, list) and isinstance(subjects, list)
    us = collections[1]
    assert isinstance(us, dict)
    entries = us["entries"]
    assert isinstance(entries, list) and isinstance(entries[1], dict)
    entries[1]["rank"] = "2="  # QS uses '=2', unlike the Times suffix notation.
    london_subjects = [
        item
        for item in subjects
        if isinstance(item, dict)
        and isinstance(item.get("location"), dict)
        and item["location"].get("city") == "London"
    ]
    assert len(london_subjects) > 1
    first_map = london_subjects[0]["map"]
    second_map = london_subjects[1]["map"]
    assert isinstance(first_map, dict) and isinstance(second_map, dict)
    second_map["center"] = list(first_map["center"])

    failures: list[str] = []
    qa.validate_catalog_document(document, failures)

    assert any("rank/tie encoding drift" in item for item in failures)
    assert any("reuse the same campus centre" in item for item in failures)


def test_report_selection_defaults_exact_and_pilot_is_order_preserving() -> None:
    catalog_failures: list[str] = []
    rows = qa.validate_catalog_document(_catalog(), catalog_failures)
    assert catalog_failures == []

    def record(row: qa.RankedRow) -> dict[str, object]:
        return {
            "collection_id": row.collection_id,
            "position": row.position,
            "subject_id": row.subject_id,
            "subject_name": row.institution_name,
            "subject_kind": "university",
            "map_purpose": "campus",
            "rank": row.rank,
            "rank_number": row.rank_number,
            "tied": row.tied,
            "edition": row.edition,
            "ranking_name": row.institution_name,
            "score": row.score,
            "visible_title": row.title,
            "status": "completed",
        }

    pilot = {"items": [record(rows[0]), record(rows[30])]}
    failures: list[str] = []
    pairs = qa._report_rows(pilot, rows, True, failures)
    assert failures == []
    assert [pair[0].subject_id for pair in pairs] == [
        rows[0].subject_id,
        rows[30].subject_id,
    ]

    failures = []
    qa._report_rows(pilot, rows, False, failures)
    assert any("exact 50" in item for item in failures)

    reordered = {"items": [record(rows[30]), record(rows[0])]}
    failures = []
    qa._report_rows(reordered, rows, True, failures)
    assert any("reordered" in item for item in failures)


def test_complete_png_parser_checks_crc_phys_and_opacity(tmp_path: Path) -> None:
    png = tmp_path / "plate.png"
    png.write_bytes(_rgba_png())

    result = qa.inspect_png(png)

    assert (result["width_px"], result["height_px"]) == (2, 2)
    assert result["x_pixels_per_metre"] == 10000
    assert result["y_pixels_per_metre"] == 10000
    assert result["physical_unit"] == 1
    assert result["opaque"] is True

    transparent = tmp_path / "transparent.png"
    transparent.write_bytes(_rgba_png(alpha=128))
    assert qa.inspect_png(transparent)["opaque"] is False

    corrupt = bytearray(_rgba_png())
    corrupt[-1] ^= 1
    png.write_bytes(corrupt)
    with pytest.raises(ValueError, match="CRC mismatch"):
        qa.inspect_png(png)


def test_png_parser_decodes_rgb_indexed_and_rejects_bad_stream_order(
    tmp_path: Path,
) -> None:
    rgb = tmp_path / "rgb.png"
    rgb.write_bytes(_rgb_png())
    assert qa.inspect_png(rgb)["has_nonwhite_pixel"] is True

    rgb.write_bytes(_rgb_png(transparency=struct.pack(">HHH", 0, 0, 0)))
    assert qa.inspect_png(rgb)["opaque"] is False

    indexed = tmp_path / "indexed.png"
    indexed.write_bytes(_indexed_png(alpha=0))
    indexed_result = qa.inspect_png(indexed)
    assert indexed_result["opaque"] is False
    assert indexed_result["has_nonwhite_pixel"] is True

    rgb.write_bytes(_rgb_png(idat=b"not a zlib stream"))
    with pytest.raises(ValueError, match="decompressed"):
        qa.inspect_png(rgb)

    rgb.write_bytes(_rgb_png(phys_after_idat=True))
    with pytest.raises(ValueError, match="pHYs must precede"):
        qa.inspect_png(rgb)


def test_split_parity_groups_every_master_layer_for_one_pen() -> None:
    master = _svg_root(
        [
            (5, "layer-roads_local", "M 0,0 L 1,1"),
            (5, "layer-paths", "M 2,2 L 3,3"),
            (6, "layer-roads_major", "M 4,4 L 5,5"),
        ]
    )
    split = _svg_root(
        [
            (5, "layer-roads_local", "M 0,0 L 1,1"),
            (5, "layer-paths", "M 2,2 L 3,3"),
        ]
    )
    assert qa.split_parity_failures(master, split, 5) == []

    split.find(f".//{{{qa.SVG_NS}}}path").set("d", "M 0,0 L 9,9")  # type: ignore[union-attr]
    assert any(
        "geometry/metadata drift" in item
        for item in qa.split_parity_failures(master, split, 5)
    )


def test_split_parity_accepts_explicit_empty_slot_without_invented_geometry() -> None:
    master = _svg_root([(2, "layer-waterways", "M 0,0 L 1,1")])
    split = _svg_root([])
    split.set(f"{{{qa.MAP_NS}}}pen-slot-status", "empty")

    assert qa.split_parity_failures(master, split, 1) == []

    split_path = ET.SubElement(split, f"{{{qa.SVG_NS}}}path")
    split_path.set("d", "M 0,0 L 1,1")
    assert any(
        "contains drawable paths" in item
        for item in qa.split_parity_failures(master, split, 1)
    )

    master_with_step = _svg_root([(1, "layer-rivers", "M 0,0 L 1,1")])
    explicit_empty = _svg_root([])
    explicit_empty.set(f"{{{qa.MAP_NS}}}pen-slot-status", "empty")
    assert any(
        "physical groups in master" in item
        for item in qa.split_parity_failures(master_with_step, explicit_empty, 1)
    )


def test_svg_rejects_unassigned_paths_and_active_content() -> None:
    document = _svg_root([(5, "layer-paths", "M 0,0 L 1,1")])
    assert qa._all_paths_are_physically_assigned(document) is True
    ET.SubElement(document, f"{{{qa.SVG_NS}}}path", {"d": "M 2,2 L 3,3"})
    assert qa._all_paths_are_physically_assigned(document) is False

    document.set("onload", "alert(1)")
    assert qa._svg_active_content_failures(document, "test")


def test_split_parity_accepts_all_ten_inventory_slots() -> None:
    groups = [
        (pen.step, f"layer-slot-{pen.step:02d}", f"M {pen.step},0 L {pen.step},1")
        for pen in qa.PENS
    ]
    master = _svg_root(groups)
    for pen in qa.PENS:
        split = _svg_root([groups[pen.step - 1]])
        assert qa.split_parity_failures(master, split, pen.step) == []


def test_checksum_parser_rejects_duplicate_traversal_and_mismatch(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "plate.svg"
    payload.write_bytes(b"plate")
    (tmp_path / "plate-alias.svg").symlink_to(payload)
    digest = hashlib.sha256(b"plate").hexdigest()
    checksums = tmp_path / "CHECKSUMS.sha256"
    checksums.write_text(
        f"{digest}  plate.svg\n"
        f"{digest}  plate.svg\n"
        f"{digest}  ./plate.svg\n"
        f"{digest}  plate-alias.svg\n"
        f"{digest}  ../foreign.svg\n"
        f"{'0' * 64}  missing.svg\n",
        encoding="utf-8",
    )
    failures: list[str] = []

    records = qa.read_checksums(checksums, tmp_path, failures)

    assert records["plate.svg"] == digest
    assert any("unsafe/duplicate" in item for item in failures)
    assert any("missing" in item for item in failures)


def test_renderer_derivation_reproduces_exact_approved_patch_set(
    tmp_path: Path,
) -> None:
    assert qa.EXPECTED_DERIVED_RENDERER_TREE_SHA256 == (
        series_builder.EXPECTED_DERIVED_RENDERER_TREE_SHA256
    )
    assert qa.EXPECTED_DERIVED_RENDERER_FINGERPRINT_SHA256 == (
        series_builder.EXPECTED_DERIVED_RENDERER_FINGERPRINT
    )
    assert qa.EXPECTED_RENDER_RECIPE_SHA256 == (
        series_builder.EXPECTED_RENDER_RECIPE_SHA256
    )
    assert qa.EXPECTED_SOURCE_MANIFEST_SHA256 == (
        series_builder.EXPECTED_SOURCE_MANIFEST_SHA256
    )
    assert qa.EXPECTED_SOURCE_COHORT_SHA256 == (
        series_builder.EXPECTED_SOURCE_COHORT_SHA256
    )
    derived = tmp_path / "renderer"
    series_builder._copy_renderer_exact(BASE_RENDERER, derived)
    files = qa._file_map(derived)
    fingerprint = qa._renderer_fingerprint_from_files(files)
    base_files = qa._archive_file_map(ARCHIVE)
    renderer = {
        "path": "renderer",
        "tree_sha256": qa._tree_digest_payloads(files),
        "fingerprint": fingerprint,
        "archive": "renderer-contract.tar",
        "archive_sha256": qa.EXPECTED_RENDERER_ARCHIVE_SHA256,
        "base_tree_sha256": qa._tree_digest_payloads(base_files),
        "base_fingerprint": qa.EXPECTED_BASE_FINGERPRINT,
        "derivation": {
            "id": qa.DERIVATION_ID,
            "visual_policy": qa.DERIVATION_VISUAL_POLICY,
            "overrides": [
                {
                    "path": relative,
                    "source_sha256": expected_sha,
                    "scope": "reviewed ranked-series correctness patch",
                }
                for relative, expected_sha in qa.DERIVATION_OVERRIDES.items()
            ],
        },
    }
    failures: list[str] = []
    qa._renderer_derivation(
        renderer,
        archive=ARCHIVE,
        root=tmp_path,
        report={"renderer_fingerprint": fingerprint},
        failures=failures,
    )
    assert failures == []

    (derived / "city_map_plotter/catalog.py").write_bytes(b"unapproved")
    failures = []
    qa._renderer_derivation(
        renderer,
        archive=ARCHIVE,
        root=tmp_path,
        report={"renderer_fingerprint": fingerprint},
        failures=failures,
    )
    assert any("unapproved files" in item for item in failures)


def test_export_contract_requires_the_v21_visual_flags(tmp_path: Path) -> None:
    style = tmp_path / "style.json"
    style.write_text("{}", encoding="utf-8")
    report = {
        "export_args": [
            "--radius-km",
            "2",
            "--preset",
            "a5-balanced-poster",
            "--poster-layout",
            "university-memorabilia",
            "--layers",
            "roads,water,railways,parks,buildings",
            "--style",
            str(style),
            "--water-fill",
            "dots",
            "--landmark-buildings",
            "--detail-profile",
            "plotter-faithful",
            "--simplify-mm",
            "0.04",
            "--road-style",
            "centreline",
            "--extent-fit",
            "contain",
            "--pen-profile",
            "actual-pens",
            "--no-scale-bar",
            "--no-scale-detail",
            "--optimise",
            "--physical-audit",
            "--split-by-pen",
            "--frame",
            "--attribution-mode",
            "external",
            "--external-attribution-placement",
            qa.EXPECTED_EXTERNAL_ATTRIBUTION,
        ]
    }
    assert qa._export_contract_failures(report, root=tmp_path, style_path=style) == []

    report["export_args"].remove("--split-by-pen")
    failures = qa._export_contract_failures(report, root=tmp_path, style_path=style)
    assert any("split-by-pen" in item for item in failures)


def test_cached_source_canonical_digest_and_path_leak_detection(tmp_path: Path) -> None:
    source = tmp_path / "source.json.gz"
    with gzip.open(source, "wt", encoding="utf-8") as stream:
        json.dump({"elements": [{"id": 2}, {"id": 1}]}, stream, indent=2)
    expected = hashlib.sha256(b'{"elements":[{"id":2},{"id":1}]}').hexdigest()

    assert qa._canonical_json_sha256(source) == expected
    assert qa._json_path_leaks({"cache": "source.json.gz"}, tmp_path, "test") == []
    leaks = qa._json_path_leaks({"cache": "/tmp/foreign-source.json"}, tmp_path, "test")
    assert any("temporary path leaked" in item for item in leaks)

    file_uri_leaks = qa._json_path_leaks(
        {"cache": "file:///etc/passwd"}, tmp_path, "test"
    )
    assert any("foreign absolute path leaked" in item for item in file_uri_leaks)


def test_exact_extent_and_overpass_query_are_bound_to_campus() -> None:
    failures: list[str] = []
    row = qa.validate_catalog_document(_catalog(), failures)[0]
    assert failures == []
    assert qa._expected_extent(row) == {
        "west": -0.14556862222963254,
        "south": 51.49519199894127,
        "east": -0.08776470977036746,
        "north": 51.53258577705872,
    }
    expected_bbox = "51.4940702,-0.1473027,51.5337076,-0.0860306"
    assert qa._expected_acquisition_bbox_text(row) == expected_bbox
    assert qa._query_uses_only_expected_bbox(
        f'[out:json];way["highway"]({expected_bbox});out geom;', row
    )
    assert not qa._query_uses_only_expected_bbox(
        '[out:json];way["highway"](0.0000000,0.0000000,1.0000000,1.0000000);out geom;',
        row,
    )


def test_qa_report_path_is_reserved_root_only_and_collision_safe(
    tmp_path: Path,
) -> None:
    report = tmp_path / "ranked-universities.batch.json"
    catalog = tmp_path / "catalog.json"
    reserved = tmp_path / "RANKED_UNIVERSITY_QA_REPORT-pilot.json"
    assert (
        qa._validate_qa_report_path(reserved, tmp_path, (report, catalog)) == reserved
    )

    with pytest.raises(qa.AuditInputError, match="reserved"):
        qa._validate_qa_report_path(report, tmp_path, (report, catalog))
    with pytest.raises(qa.AuditInputError, match="release root"):
        qa._validate_qa_report_path(
            tmp_path / "nested/RANKED_UNIVERSITY_QA_REPORT.json",
            tmp_path,
            (report, catalog),
        )
    with pytest.raises(qa.AuditInputError, match="exact reserved"):
        qa._validate_qa_report_path(
            tmp_path / "RANKED_UNIVERSITY_QA_REPORT-copy.json",
            tmp_path,
            (report, catalog),
        )


def test_release_hygiene_rejects_transients_and_qa_report_aliases(
    tmp_path: Path,
) -> None:
    allowed = (
        tmp_path / "RANKED_UNIVERSITY_QA_REPORT.json",
        tmp_path / "RANKED_UNIVERSITY_QA_REPORT-pilot.json",
    )
    for path in allowed:
        path.write_text("{}\n", encoding="utf-8")
    assert qa._release_hygiene_failures(tmp_path) == []
    paths = (
        tmp_path / "ranked-universities.batch.json.lock",
        tmp_path / "nested/render.123.tmp",
        tmp_path / "release-metadata/module.pyc",
        tmp_path / "release-metadata/module.pyo",
        tmp_path / "RANKED_UNIVERSITY_QA_REPORT-copy.json",
        tmp_path / "nested/RANKED_UNIVERSITY_QA_REPORT.json",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"transient-or-alias")
    (tmp_path / "release-metadata/__pycache__").mkdir(parents=True)

    failures = qa._release_hygiene_failures(tmp_path)

    assert sum("forbidden transient path" in item for item in failures) == 5
    assert sum("ambiguous QA-report alias" in item for item in failures) == 2


def test_pinned_source_manifest_validates_bytes_and_detects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, row, snapshot = _test_pinned_bundle(tmp_path, monkeypatch)

    assert list(bundle.entries) == [row.subject_id]
    assert bundle.entries[row.subject_id].path == snapshot

    snapshot.write_bytes(snapshot.read_bytes() + b"tampered")
    failures: list[str] = []
    assert (
        qa._validate_pinned_source_manifest(bundle.manifest_path, [row], failures)
        is None
    )
    assert any("compressed source bytes drift" in item for item in failures)


def test_pinned_cohort_item_dependencies_and_paths_are_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, row, snapshot = _test_pinned_bundle(tmp_path, monkeypatch)
    entry = bundle.entries[row.subject_id]
    cohort = qa._expected_pinned_source_cohort(bundle, [row.subject_id])
    report = {
        "source_cohort": cohort,
        "source_cohort_sha256": cohort["sha256"],
        "source_manifest": str(bundle.manifest_path),
    }
    failures: list[str] = []
    assert (
        qa._validate_pinned_source_cohort(report, bundle, [row.subject_id], failures)
        == cohort
    )
    assert failures == []

    style = tmp_path / "style.json"
    style.write_text("{}", encoding="utf-8")
    style_sha = hashlib.sha256(style.read_bytes()).hexdigest()
    monkeypatch.setattr(qa, "EXPECTED_STYLE_SHA256", style_sha)
    dependencies = [
        {
            "option": "--style",
            "path": str(style),
            "size_bytes": style.stat().st_size,
            "sha256": style_sha,
        },
        {
            "option": "--input-json",
            "path": str(snapshot),
            "size_bytes": entry.size_bytes,
            "sha256": entry.sha256,
        },
    ]
    argv = ["export", "--input-json", str(snapshot)]
    assert (
        qa._pinned_item_dependency_failures(
            dependencies,
            argv,
            root=tmp_path,
            entry=entry,
            subject_id=row.subject_id,
        )
        == []
    )
    assert (
        qa._resolve_pinned_source_path(
            str(snapshot), tmp_path, snapshot, "test snapshot"
        )
        == snapshot
    )

    tampered = deepcopy(cohort)
    tampered["json_set"]["entries"][0]["path"] = str(tmp_path / "foreign.json.gz")
    report["source_cohort"] = tampered
    failures = []
    qa._validate_pinned_source_cohort(report, bundle, [row.subject_id], failures)
    assert any("exact pinned JSON" in item for item in failures)

    with pytest.raises(qa.AuditInputError, match="path traversal"):
        qa._resolve_pinned_source_path(
            "release-metadata/../outside.json.gz",
            tmp_path,
            snapshot,
            "test snapshot",
        )
    assert qa._pinned_item_dependency_failures(
        dependencies[:1],
        argv,
        root=tmp_path,
        entry=entry,
        subject_id=row.subject_id,
    )


def test_render_recipe_binding_is_exact(tmp_path: Path) -> None:
    recipe = tmp_path / "release-metadata/render-recipe-v2.1.4.json"
    recipe.parent.mkdir(parents=True)
    recipe.write_bytes(series_builder.FROZEN_RENDER_RECIPE.read_bytes())
    contract = {
        "render_recipe": {
            "path": "release-metadata/render-recipe-v2.1.4.json",
            "sha256": qa.EXPECTED_RENDER_RECIPE_SHA256,
        }
    }
    failures: list[str] = []
    assert qa._validate_recipe_binding(contract, tmp_path, failures) == recipe
    assert failures == []

    recipe.write_bytes(recipe.read_bytes() + b"\n")
    failures = []
    qa._validate_recipe_binding(contract, tmp_path, failures)
    assert any("recipe bytes drift" in item for item in failures)
