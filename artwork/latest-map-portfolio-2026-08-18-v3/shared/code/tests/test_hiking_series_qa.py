from __future__ import annotations

import copy
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tools.audit_hiking_composition import audit_artifact
from tools.qa_niche_series import (
    CONTEXT_ATTRIBUTES,
    EXPECTED_ARTIFACT_COUNT,
    EXPECTED_SUBJECT_COUNT,
    EXPECTED_VARIANTS,
    FALL_LINE_NO_CLUSTER_OMISSION_REASON,
    HYDRO_LABEL_ROLES,
    INKSCAPE_NS,
    MOUNTAIN_LABEL_ROLES,
    SVG_NS,
    _check_a5_contract,
    _check_hiking_semantics,
    _check_paired_hiking_variant_semantics,
    _check_suite_contract,
    _source_has_frozen_snapshot,
    _valid_coverage_measurement,
)


PEN_PLAN = (
    "grey-0-25",
    "grey-0-4",
    "blue-0-25",
    "green-0-25",
    "black-0-25",
    "black-0-6",
    "red-0-4",
)


def test_frozen_raster_window_is_valid_source_evidence() -> None:
    source = {
        "source_raster_url": "https://example.test/source.tif",
        "retrieved_at": "2026-08-03T00:00:00Z",
        "derived_window_sha256": "a" * 64,
        "derived_window_valid_fraction": 1.0,
    }
    assert _source_has_frozen_snapshot(source)
    source["source_raster_url"] = "http://example.test/source.tif"
    assert not _source_has_frozen_snapshot(source)


def _dual_frozen_route_source() -> dict[str, object]:
    return {
        "id": "osm-route",
        "publisher": "OpenStreetMap contributors via Waymarked Trails",
        "url": "https://www.openstreetmap.org/relation/4080347",
        "retrieved_at": "2026-08-03T00:00:00Z",
        "relation_id": 4080347,
        "relation_version": 20,
        "relation_timestamp": "2025-04-30T14:45:30Z",
        "acquisition_url": (
            "https://hiking.waymarkedtrails.org/api/v1/details/relation/4080347?lang=en"
        ),
        "waymarked_snapshot_sha256": "a" * 64,
        "osm_relation_snapshot_sha256": "b" * 64,
    }


def test_official_raw_snapshot_digest_is_recognized_as_frozen_evidence() -> None:
    assert _source_has_frozen_snapshot(
        {
            "id": "official-identity",
            "retrieved_at": "2026-08-04T00:00:00Z",
            "raw_snapshot_sha256": "a" * 64,
        }
    )


def test_dual_frozen_route_evidence_requires_both_snapshots_and_metadata() -> None:
    source = _dual_frozen_route_source()
    assert _source_has_frozen_snapshot(source)

    for field in ("waymarked_snapshot_sha256", "osm_relation_snapshot_sha256"):
        missing = dict(source)
        missing.pop(field)
        # A generic digest must not mask a partial dual-evidence contract.
        missing["snapshot_sha256"] = "c" * 64
        assert not _source_has_frozen_snapshot(missing)

        malformed = dict(source)
        malformed[field] = "C" * 64
        assert not _source_has_frozen_snapshot(malformed)

    invalid_fields = {
        "relation_id": 0,
        "relation_version": True,
        "relation_timestamp": "2025-99-30T14:45:30Z",
        "acquisition_url": (
            "https://hiking.waymarkedtrails.org/api/v1/details/relation/999?lang=en"
        ),
        "url": "https://www.openstreetmap.org/relation/999",
        "retrieved_at": "",
    }
    for field, invalid_value in invalid_fields.items():
        malformed = dict(source)
        malformed[field] = invalid_value
        assert not _source_has_frozen_snapshot(malformed)

    no_dual_hashes = dict(source)
    no_dual_hashes.pop("waymarked_snapshot_sha256")
    no_dual_hashes.pop("osm_relation_snapshot_sha256")
    no_dual_hashes["snapshot_sha256"] = "c" * 64
    assert not _source_has_frozen_snapshot(no_dual_hashes)


def test_all_expansion_route_sources_use_valid_dual_frozen_evidence() -> None:
    from city_map_plotter.hike_plates import load_hike_release_catalog

    dual_sources = [
        source
        for record in load_hike_release_catalog()
        for source in record["sources"]
        if source.get("id") == "osm-route" and "waymarked_snapshot_sha256" in source
    ]
    assert len(dual_sources) == 31
    assert all(_source_has_frozen_snapshot(source) for source in dual_sources)


def _path(
    logical: ET.Element,
    *,
    role: str,
    source_ref: str | None = None,
    context: bool = False,
    osm: str | None = None,
    label_id: str | None = None,
    label_box: str | None = None,
    relief: bool = False,
) -> ET.Element:
    attributes = {
        "d": "M 10 10 L 12 12",
        "data-logical-layer": str(logical.get("data-logical-layer")),
        "data-role": role,
    }
    if source_ref:
        attributes["data-source-ref"] = source_ref
    if context:
        attributes.update(CONTEXT_ATTRIBUTES)
    if osm:
        attributes.update(
            {
                "data-feature-id": osm.replace("/", "-"),
                "data-feature-kind": role,
                "data-osm-element": osm,
                "data-source-url": f"https://www.openstreetmap.org/{osm}",
            }
        )
    if label_id:
        attributes["data-label-id"] = label_id
    if label_box:
        attributes["data-label-box"] = label_box
    if relief:
        attributes["data-relief-status"] = "stylized-source-anchored"
    return ET.SubElement(logical, f"{{{SVG_NS}}}path", attributes)


def _semantic_fixture() -> tuple[dict[str, object], ET.Element]:
    subject_id = "RTE-GB-WHW-01"
    sources = [
        {
            "id": "osm-route",
            "publisher": "OpenStreetMap contributors",
            "url": "https://www.openstreetmap.org/relation/16287",
            "license": "ODbL-1.0",
            "attribution": "OpenStreetMap contributors",
            "use": "route geometry",
            "retrieved_at": "2026-08-03",
            "snapshot_sha256": "1" * 64,
        },
        {
            "id": "osm-context",
            "publisher": "OpenStreetMap contributors",
            "url": "https://www.openstreetmap.org/copyright",
            "license": "ODbL-1.0",
            "attribution": "OpenStreetMap contributors",
            "use": "curated context features",
            "retrieved_at": "2026-08-03",
            "snapshot_sha256": "2" * 64,
        },
    ]
    manifest: dict[str, object] = {
        "subject_id": subject_id,
        "domain": "hikes",
        "sources": sources,
        "source": {
            "attribution": (
                "OPENSTREETMAP / OPENSTREETMAP.ORG/COPYRIGHT / "
                "SOURCES IN MANIFEST"
            )
        },
        "rendering": {"visible_attribution": True},
        "catalog_record": {
            "context": {
                "features": [
                    {
                        "kind": "woodland",
                        "id": "way-102",
                        "source_ref": "osm-context",
                        "paths": [[[0, 0], [1, 0], [0, 0]]],
                    },
                    {
                        "kind": "river",
                        "id": "way-101",
                        "source_ref": "osm-context",
                        "paths": [[[0, 0], [1, 1]]],
                    },
                ]
            }
        },
        "pen_sequence": [
            {"step": index, "pen_id": pen_id, "layers": []}
            for index, pen_id in enumerate(PEN_PLAN, start=1)
        ],
    }
    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {"width": "148mm", "height": "210mm", "viewBox": "0 0 148 210"},
    )
    metadata = ET.SubElement(root, f"{{{SVG_NS}}}metadata")
    metadata.text = json.dumps(
        {"subject_id": subject_id, "domain": "hikes", "sources": sources}
    )
    groups: dict[str, ET.Element] = {}
    for step, pen_id in enumerate(PEN_PLAN, start=1):
        groups[pen_id] = ET.SubElement(
            root,
            f"{{{SVG_NS}}}g",
            {
                "id": f"layer-pen-{pen_id}",
                f"{{{INKSCAPE_NS}}}groupmode": "layer",
                "data-pen-step": str(step),
            },
        )

    def logical(pen_id: str, layer_id: str) -> ET.Element:
        manifest["pen_sequence"][PEN_PLAN.index(pen_id)]["layers"].append(layer_id)  # type: ignore[index]
        return ET.SubElement(
            groups[pen_id],
            f"{{{SVG_NS}}}g",
            {"data-logical-layer": layer_id},
        )

    relief = logical("grey-0-25", "context_relief")
    relief_path = _path(
        relief,
        role="stylized-ridge-symbol",
        source_ref="osm-context",
        context=True,
        relief=True,
    )
    relief_path.set("d", "M 70,70 L 72,72")
    water = logical("blue-0-25", "context_water")
    water_path = _path(
        water,
        role="source-sampled-river-centreline",
        source_ref="osm-context",
        context=True,
        osm="way/101",
    )
    water_path.set("d", "M 75,75 L 77,77")
    woodland = logical("green-0-25", "context_woodland")
    woodland_path = _path(
        woodland,
        role="source-sampled-landcover-boundary",
        source_ref="osm-context",
        context=True,
        osm="way/102",
    )
    woodland_path.set("d", "M 80,80 L 82,82")
    markers = logical("black-0-25", "context_markers")
    _path(
        markers,
        role="settlement-marker",
        source_ref="osm-context",
        context=True,
        osm="node/103",
    )
    labels = logical("black-0-25", "context_labels")
    for role, label_id, label_box, osm in (
        ("settlement-label", "milngavie", "10,10,12,3", "node/103"),
        ("range-label", "grampians", "30,20,14,3", "relation/104"),
        ("water-label", "loch-lomond", "50,30,14,3", "way/101"),
    ):
        _path(
            labels,
            role=role,
            source_ref="osm-context",
            context=True,
            osm=osm,
            label_id=label_id,
            label_box=label_box,
        )
    copy = logical("black-0-25", "plate_copy")
    _path(copy, role="detail")
    attribution = logical("black-0-25", "plate_attribution")
    _path(attribution, role="attribution")
    heavy = logical("black-0-6", "plate_heavy")
    _path(heavy, role="title")
    route = logical("red-0-4", "hero_route")
    route_path = _path(route, role="source-sampled-route", source_ref="osm-route")
    route_path.set("d", "M 100,100 L 110,110")
    return manifest, root


def _marker_equivalent_semantic_fixture() -> tuple[dict[str, object], ET.Element]:
    manifest, root = _semantic_fixture()
    catalog_record = manifest["catalog_record"]
    assert isinstance(catalog_record, dict)
    context = catalog_record["context"]
    assert isinstance(context, dict)
    context["features"].extend(  # type: ignore[union-attr]
        [
            {
                "id": "route-start-context",
                "kind": "settlement",
                "label": "START PLACE",
                "point": [-4.0, 56.0],
                "source_ref": "osm-route",
                "priority": -2,
            },
            {
                "id": "route-finish-context",
                "kind": "settlement",
                "label": "FINISH PLACE",
                "point": [-3.0, 57.0],
                "source_ref": "osm-route",
                "priority": -2,
            },
        ]
    )
    catalog_record["route"] = {
        "source_ref": "osm-route",
        "profile_status": "source-elevation-sampled",
        "controls": [
            {
                "kind": "start",
                "name": "START PLACE",
                "point": [-4.0, 56.0],
                "source_ref": "osm-route",
            },
            {
                "kind": "finish",
                "name": "FINISH PLACE",
                "point": [-3.0, 57.0],
                "source_ref": "osm-route",
            },
        ],
    }
    rendering = manifest["rendering"]
    assert isinstance(rendering, dict)
    rendering["chainage_station_reservations"] = {
        "count": 5,
        "radius_mm": 1.65,
        "context_markers_suppressed": 2,
        "policy": "exact-route-station-copy-clearance-v1",
    }

    marker_layer = root.find(
        f".//{{{SVG_NS}}}g[@data-logical-layer='context_markers']"
    )
    label_layer = root.find(
        f".//{{{SVG_NS}}}g[@data-logical-layer='context_labels']"
    )
    assert marker_layer is not None and label_layer is not None
    for path in list(marker_layer):
        if path.get("data-role") == "settlement-marker":
            marker_layer.remove(path)
    for path in list(label_layer):
        if path.get("data-role") == "settlement-label":
            label_layer.remove(path)
    for label_id, label_box in (
        ("route-start-context", "70,10,16,3"),
        ("route-finish-context", "95,10,16,3"),
    ):
        _path(
            label_layer,
            role="settlement-label",
            source_ref="osm-route",
            context=True,
            label_id=label_id,
            label_box=label_box,
        )

    black_group = root.find(f"{{{SVG_NS}}}g[@id='layer-pen-black-0-25']")
    assert black_group is not None
    annotations = ET.SubElement(
        black_group,
        f"{{{SVG_NS}}}g",
        {"data-logical-layer": "route_annotations"},
    )
    pen_sequence = manifest["pen_sequence"]
    assert isinstance(pen_sequence, list)
    black_step = pen_sequence[PEN_PLAN.index("black-0-25")]
    assert isinstance(black_step, dict)
    black_step["layers"].append("route_annotations")  # type: ignore[union-attr]
    for index, (station_id, fraction) in enumerate(
        zip("ABCDE", (0.0, 0.25, 0.5, 0.75, 1.0), strict=True)
    ):
        station_attributes = {
            "data-chainage-id": station_id,
            "data-chainage-m": f"{fraction * 100000:.3f}",
            "data-distance-km": f"{fraction * 100:.6f}",
            "data-measured-chainage-m": f"{fraction * 100000:.3f}",
            "data-displayed-distance-m": f"{fraction * 100000:.3f}",
            "data-displayed-distance-km": f"{fraction * 100:.6f}",
            "data-route-fraction": f"{fraction:.9f}",
            "data-longitude": f"{-4.0 + fraction:.9f}",
            "data-latitude": f"{56.0 + fraction:.9f}",
            "data-elevation-m": f"{100.0 + index * 100:.3f}",
            "data-elevation-status": "interpolated",
            "data-source-vertex-before": str(index),
            "data-source-vertex-after": str(index + 1),
            "data-source-segment-fraction": f"{fraction:.9f}",
            "data-chainage-basis": "source-geometry-cumulative-geodesic-v1",
            "data-distance-label-basis": "measured-source-chainage-v1",
            "data-route-source-ref": "osm-route",
            "data-profile-status": "source-elevation-sampled",
            "data-official-total-distance-km": "100",
            "data-elevation-source-ref": "osm-route",
            "data-elevation-method": "route-source-embedded-elevation-v1",
            "data-elevation-datum": "test datum",
        }
        map_label = _path(
            annotations,
            role="map-chainage-label",
            source_ref="osm-route",
        )
        map_label.attrib.update(station_attributes)
        if station_id in {"A", "E"}:
            endpoint = _path(
                annotations,
                role="start" if station_id == "A" else "finish",
                source_ref="osm-route",
            )
            endpoint.attrib.update(station_attributes)
    return manifest, root


def test_hiking_semantic_contract_accepts_complete_geographic_plate() -> None:
    manifest, root = _semantic_fixture()
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert failures == []


def test_hiking_semantics_accept_exact_a_e_route_stations_as_suppressed_markers() -> (
    None
):
    manifest, root = _marker_equivalent_semantic_fixture()
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert failures == []


def test_marker_equivalence_ignores_an_unrelated_peak_label() -> None:
    manifest, root = _marker_equivalent_semantic_fixture()
    label_layer = root.find(
        f".//{{{SVG_NS}}}g[@data-logical-layer='context_labels']"
    )
    assert label_layer is not None
    _path(
        label_layer,
        role="peak-label",
        source_ref="osm-context",
        context=True,
        label_id="osm-node-4999575296",
        label_box="40,40,20,3",
    )
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert failures == []


def test_hiking_semantics_reject_marker_equivalence_suppression_count_drift() -> None:
    manifest, root = _marker_equivalent_semantic_fixture()
    rendering = manifest["rendering"]
    assert isinstance(rendering, dict)
    reservations = rendering["chainage_station_reservations"]
    assert isinstance(reservations, dict)
    reservations["context_markers_suppressed"] = 1
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("no geographic markers" in failure for failure in failures)
    assert any("count/identity" in failure for failure in failures)


def test_hiking_semantics_reject_marker_equivalence_label_identity_drift() -> None:
    manifest, root = _marker_equivalent_semantic_fixture()
    for path in root.findall(
        f".//{{{SVG_NS}}}path[@data-label-id='route-start-context']"
    ):
        path.set("data-label-id", "wrong-route-control")
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("no geographic markers" in failure for failure in failures)
    assert any("count/identity" in failure for failure in failures)


def test_hiking_semantics_reject_marker_equivalence_coordinate_drift() -> None:
    manifest, root = _marker_equivalent_semantic_fixture()
    start = root.find(f".//{{{SVG_NS}}}path[@data-role='start']")
    assert start is not None
    start.set("data-longitude", "-3.9")
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("no geographic markers" in failure for failure in failures)
    assert any("chainage mark metadata drift" in failure for failure in failures)


def test_hiking_semantic_contract_accepts_dual_frozen_route_evidence() -> None:
    manifest, root = _semantic_fixture()
    route_source = _dual_frozen_route_source()
    sources = manifest["sources"]
    assert isinstance(sources, list)
    sources[0] = route_source
    metadata = root.find(f"{{{SVG_NS}}}metadata")
    assert metadata is not None and metadata.text
    metadata_payload = json.loads(metadata.text)
    metadata_payload["sources"][0] = route_source
    metadata.text = json.dumps(metadata_payload)

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert failures == []


def test_hiking_semantic_contract_rejects_missing_context_and_route_order() -> None:
    manifest, root = _semantic_fixture()
    context_water = root.find(f".//{{{SVG_NS}}}g[@data-logical-layer='context_water']")
    assert context_water is not None
    context_water.clear()
    sequence = manifest["pen_sequence"]  # type: ignore[index]
    sequence[-1]["layers"] = []
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("no water/coast" in failure for failure in failures)
    assert any("not the last pen load" in failure for failure in failures)


def test_hiking_semantic_contract_requires_visible_non_odbl_route_credit() -> None:
    manifest, root = _semantic_fixture()
    route_source = manifest["sources"][0]  # type: ignore[index]
    route_source.update(  # type: ignore[union-attr]
        {
            "id": "open-route",
            "publisher": "Route publisher",
            "license": "CC-BY-4.0",
            "attribution": "Required route source / CC BY 4.0",
        }
    )
    manifest["catalog_record"]["route"] = {  # type: ignore[index]
        "source_ref": "open-route"
    }
    route_path = root.find(f".//{{{SVG_NS}}}path[@data-role='source-sampled-route']")
    assert route_path is not None
    route_path.set("data-source-ref", "open-route")
    metadata = root.find(f"{{{SVG_NS}}}metadata")
    assert metadata is not None
    metadata.text = json.dumps(
        {
            "subject_id": manifest["subject_id"],
            "domain": "hikes",
            "sources": manifest["sources"],
        }
    )

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-TEST-OPEN-ROUTE",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("non-ODbL route source" in failure for failure in failures)

    manifest["source"]["attribution"] = (  # type: ignore[index]
        "Required route source / CC BY 4.0 | © OpenStreetMap contributors / "
        "OPENSTREETMAP.ORG/COPYRIGHT"
    )
    failures = []
    _check_hiking_semantics(
        subject_id="RTE-TEST-OPEN-ROUTE",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert not any("non-ODbL route source" in failure for failure in failures)
    assert not any("OPENSTREETMAP.ORG/COPYRIGHT" in failure for failure in failures)


@pytest.mark.parametrize(
    "tampered_credit",
    (
        "OPENSTREETMAP CONTRIBUTORS / SOURCES IN MANIFEST",
        (
            "OPENSTREETMAP CONTRIBUTORS / OPENSTREETMAP.ORG/COPYR1GHT / "
            "SOURCES IN MANIFEST"
        ),
    ),
)
def test_hiking_semantic_contract_requires_the_canonical_printed_osm_url(
    tampered_credit: str,
) -> None:
    manifest, root = _semantic_fixture()
    manifest["source"]["attribution"] = tampered_credit  # type: ignore[index]

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-TEST-OSM-PRINTED-URL",
        manifest=manifest,
        root=root,
        failures=failures,
    )

    assert any("OPENSTREETMAP.ORG/COPYRIGHT" in failure for failure in failures)


def test_hiking_semantic_contract_fails_closed_on_provider_review() -> None:
    manifest, root = _semantic_fixture()
    source = manifest["sources"][1]  # type: ignore[index]
    source["provider_attribution_review_required"] = True  # type: ignore[index]
    manifest["rights"] = {"status": "open-data-attribution-required"}
    metadata = root.find(f"{{{SVG_NS}}}metadata")
    assert metadata is not None
    metadata.text = json.dumps(
        {
            "subject_id": manifest["subject_id"],
            "domain": "hikes",
            "sources": manifest["sources"],
        }
    )
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-TEST-PROVIDER-REVIEW",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("not fail-closed" in failure for failure in failures)

    manifest["rights"] = {"status": "review-required"}
    failures = []
    _check_hiking_semantics(
        subject_id="RTE-TEST-PROVIDER-REVIEW",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert not any("not fail-closed" in failure for failure in failures)


def test_hiking_semantic_contract_allows_truthful_landcover_omission() -> None:
    manifest, root = _semantic_fixture()
    manifest["catalog_record"] = {"context": {"features": []}}
    green_group = root.find(f".//{{{SVG_NS}}}g[@data-logical-layer='context_woodland']")
    assert green_group is not None
    for physical in root.findall(f"{{{SVG_NS}}}g"):
        if green_group in list(physical):
            physical.remove(green_group)
            break
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert failures == []


def test_hiking_semantics_require_mountain_labels_only_with_source_candidates() -> None:
    manifest, root = _semantic_fixture()
    label_layer = root.find(f".//{{{SVG_NS}}}g[@data-logical-layer='context_labels']")
    assert label_layer is not None
    for path in list(label_layer):
        if path.get("data-role") in MOUNTAIN_LABEL_ROLES:
            label_layer.remove(path)

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-TEST-NO-MOUNTAIN-SOURCE",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert not any("no mountain/range/pass labels" in item for item in failures)

    context = manifest["catalog_record"]["context"]  # type: ignore[index]
    context["features"].append(  # type: ignore[union-attr]
        {
            "id": "source-peak",
            "kind": "peak",
            "label": "SOURCE PEAK",
            "point": [0.5, 0.5],
        }
    )
    failures = []
    _check_hiking_semantics(
        subject_id="RTE-TEST-WITH-MOUNTAIN-SOURCE",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("no mountain/range/pass labels" in item for item in failures)


def test_hiking_semantics_require_hydro_label_only_with_named_source_candidate() -> (
    None
):
    manifest, root = _semantic_fixture()
    label_layer = root.find(f".//{{{SVG_NS}}}g[@data-logical-layer='context_labels']")
    assert label_layer is not None
    for path in list(label_layer):
        if path.get("data-role") in HYDRO_LABEL_ROLES:
            label_layer.remove(path)
    context = manifest["catalog_record"]["context"]  # type: ignore[index]
    context["family_evidence"] = [  # type: ignore[index]
        {
            "family": "hydrography",
            "status": "source-features-selected",
            "source_candidate_count": 1,
            "selected_feature_count": 1,
            "query_groups": ["labels", "linear", "areas"],
        }
    ]
    context["features"].append(  # type: ignore[union-attr]
        {
            "id": "generic-river",
            "kind": "river",
            "label": "RIVER",
            "display_label": False,
            "point": [0.5, 0.5],
        }
    )

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-TEST-GENERIC-HYDRO",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert not any("no water/sea labels" in item for item in failures)

    context["features"].append(  # type: ignore[union-attr]
        {
            "id": "named-river",
            "kind": "river",
            "label": "SOURCE RIVER",
            "display_label": True,
            "point": [0.5, 0.5],
        }
    )
    failures = []
    _check_hiking_semantics(
        subject_id="RTE-TEST-NAMED-HYDRO",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("no water/sea labels" in item for item in failures)


def test_hiking_semantic_contract_rejects_overlapping_label_boxes() -> None:
    manifest, root = _semantic_fixture()
    labels = root.findall(f".//{{{SVG_NS}}}path[@data-logical-layer='context_labels']")
    labels[1].set("data-label-box", "11,11,14,3")
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("label boxes overlap" in failure for failure in failures)


def _add_context_label_leader(
    root: ET.Element,
    *,
    target_id: str,
    path_data: str,
    routing_policy: str | None = "foreign-copy-route-and-leader-clearance-v1",
    minimum_clearance: str | None = "0.3",
) -> ET.Element:
    marker_layer = root.find(f".//{{{SVG_NS}}}g[@data-logical-layer='context_markers']")
    assert marker_layer is not None
    leader = _path(
        marker_layer,
        role="context-label-leader",
        source_ref="osm-context",
        context=True,
    )
    leader.set("d", path_data)
    leader.set("data-feature-id", target_id)
    if routing_policy is not None:
        leader.set("data-leader-routing-policy", routing_policy)
    if minimum_clearance is not None:
        leader.set("data-minimum-copy-clearance-mm", minimum_clearance)
    return leader


def _add_contour_label(
    root: ET.Element,
    *,
    label_id: str,
    contour_id: str,
    label_box: str,
) -> ET.Element:
    grey_group = root.find(f"{{{SVG_NS}}}g[@id='layer-pen-grey-0-25']")
    assert grey_group is not None
    label_layer = root.find(
        f".//{{{SVG_NS}}}g[@data-logical-layer='context_relief_labels']"
    )
    if label_layer is None:
        label_layer = ET.SubElement(
            grey_group,
            f"{{{SVG_NS}}}g",
            {"data-logical-layer": "context_relief_labels"},
        )
    label = _path(
        label_layer,
        role="source-derived-contour-altitude-label",
        source_ref="osm-context",
        context=True,
        label_id=label_id,
        label_box=label_box,
    )
    label.attrib.update(
        {
            "data-contour-id": contour_id,
            "data-elevation-m": contour_id.rsplit("-", 1)[-1],
        }
    )
    return label


def _add_contour_label_leader(
    root: ET.Element,
    *,
    target_id: str,
    contour_id: str,
    path_data: str,
) -> ET.Element:
    label_layer = root.find(
        f".//{{{SVG_NS}}}g[@data-logical-layer='context_relief_labels']"
    )
    assert label_layer is not None
    leader = _path(
        label_layer,
        role="source-derived-contour-altitude-leader",
        source_ref="osm-context",
        context=True,
    )
    leader.attrib.update(
        {
            "d": path_data,
            "data-feature-id": target_id,
            "data-contour-id": contour_id,
            "data-leader-routing-policy": (
                "foreign-copy-route-and-leader-clearance-v1"
            ),
            "data-minimum-copy-clearance-mm": "0.3",
        }
    )
    return leader


def test_hiking_semantic_contract_excludes_leaders_own_label_box() -> None:
    manifest, root = _semantic_fixture()
    _add_context_label_leader(
        root,
        target_id="milngavie",
        path_data="M 5,11.5 L 10,11.5",
    )
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert failures == []


def test_hiking_semantic_contract_enforces_exact_leader_clearance_boundary() -> None:
    manifest, root = _semantic_fixture()
    leader = _add_context_label_leader(
        root,
        target_id="milngavie",
        path_data="M 25,19.7 L 45,19.7",
    )
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert failures == []

    leader.set("d", "M 25,19.701 L 45,19.701")
    failures = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any(
        "foreign label 'grampians'; requires >=0.30 mm" in failure
        for failure in failures
    )


def test_hiking_semantic_contract_rejects_leader_through_foreign_context_label() -> (
    None
):
    manifest, root = _semantic_fixture()
    _add_context_label_leader(
        root,
        target_id="milngavie",
        path_data="M 25,21.5 L 35,21.5",
    )
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any(
        "foreign label 'grampians'" in failure and "0.000 mm" in failure
        for failure in failures
    )


def test_hiking_semantic_contract_checks_foreign_contour_label_boxes() -> None:
    manifest, root = _semantic_fixture()
    own_id = "contour-altitude-contour-400"
    foreign_id = "contour-altitude-contour-500"
    _add_contour_label(
        root,
        label_id=own_id,
        contour_id="contour-400",
        label_box="60,40,10,3",
    )
    _add_contour_label(
        root,
        label_id=foreign_id,
        contour_id="contour-500",
        label_box="80,40,10,3",
    )
    _add_contour_label_leader(
        root,
        target_id=own_id,
        contour_id="contour-400",
        path_data="M 65,41.5 L 85,41.5",
    )
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert not any(f"foreign label '{own_id}'" in failure for failure in failures)
    assert any(f"foreign label '{foreign_id}'" in failure for failure in failures)


def test_hiking_semantic_contract_allows_route_contact_inside_leader_endpoint_zone() -> (
    None
):
    manifest, root = _semantic_fixture()
    leader = _add_context_label_leader(
        root,
        target_id="milngavie",
        path_data="M 5,11.5 L 10,11.5",
    )
    route = root.find(f".//{{{SVG_NS}}}path[@data-role='source-sampled-route']")
    assert route is not None
    route.set("d", "M 5.3,5 L 5.3,18")

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert not any("crosses the hero route" in failure for failure in failures)

    route.set("d", "M 5.301,5 L 5.301,18")
    failures = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("crosses the hero route" in failure for failure in failures)
    assert leader.get("data-minimum-copy-clearance-mm") == "0.3"


def test_hiking_semantic_contract_rejects_contour_leader_crossing_hero_route() -> None:
    manifest, root = _semantic_fixture()
    target_id = "contour-altitude-contour-400"
    _add_contour_label(
        root,
        label_id=target_id,
        contour_id="contour-400",
        label_box="60,40,10,3",
    )
    _add_contour_label_leader(
        root,
        target_id=target_id,
        contour_id="contour-400",
        path_data="M 55,41.5 L 60,41.5",
    )
    route = root.find(f".//{{{SVG_NS}}}path[@data-role='source-sampled-route']")
    assert route is not None
    route.set("d", "M 58,35 L 58,48")

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any(
        "source-derived-contour-altitude-leader" in failure
        and "crosses the hero route" in failure
        for failure in failures
    )


def test_hiking_semantic_contract_checks_closed_route_segment_from_z() -> None:
    manifest, root = _semantic_fixture()
    _add_context_label_leader(
        root,
        target_id="milngavie",
        path_data="M 50,50 L 62,50",
    )
    route = root.find(f".//{{{SVG_NS}}}path[@data-role='source-sampled-route']")
    assert route is not None
    # The leader meets only the implicit closing edge from (65, 60) to
    # (55, 40), proving that Z is collision geometry rather than decoration.
    route.set("d", "M 55,40 L 65,40 L 65,60 Z")

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-TEST-CLOSED-LOOP",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert not any("hero route is not an absolute linear" in item for item in failures)
    assert any("crosses the hero route" in item for item in failures)


def test_hiking_semantic_contract_rejects_interior_leader_crossing() -> None:
    manifest, root = _semantic_fixture()
    _add_context_label_leader(
        root,
        target_id="milngavie",
        path_data="M 5,11.5 L 10,11.5",
    )
    _add_context_label_leader(
        root,
        target_id="grampians",
        path_data="M 7,8 L 7,21.5 L 30,21.5",
    )

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any(
        "context-label-leader for 'milngavie' crosses context-label-leader "
        "for 'grampians'" in failure
        for failure in failures
    )


def test_hiking_semantic_contract_allows_leader_contact_inside_endpoint_zone() -> None:
    manifest, root = _semantic_fixture()
    _add_context_label_leader(
        root,
        target_id="milngavie",
        path_data="M 5,11.5 L 10,11.5",
    )
    _add_context_label_leader(
        root,
        target_id="grampians",
        path_data="M 5.3,8 L 5.3,21.5 L 30,21.5",
    )

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert not any("crosses context-label-leader" in failure for failure in failures)


def test_hiking_semantic_contract_rejects_hero_route_through_context_label_box() -> (
    None
):
    manifest, root = _semantic_fixture()
    route = root.find(f".//{{{SVG_NS}}}path[@data-role='source-sampled-route']")
    assert route is not None
    route.set("d", "M 5,11.5 L 25,11.5")

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any(
        "hero_route/source-sampled-route crosses boxed settlement-label "
        "'milngavie'" in failure
        for failure in failures
    )


def test_hiking_semantic_contract_rejects_relief_through_contour_label_box() -> None:
    manifest, root = _semantic_fixture()
    target_id = "contour-altitude-contour-400"
    _add_contour_label(
        root,
        label_id=target_id,
        contour_id="contour-400",
        label_box="60,40,10,3",
    )
    relief_layer = root.find(f".//{{{SVG_NS}}}g[@data-logical-layer='context_relief']")
    assert relief_layer is not None
    hachure = _path(
        relief_layer,
        role="source-derived-dem-fall-line-hachure",
        source_ref="osm-context",
        context=True,
        relief=True,
    )
    hachure.set("d", "M 55,41.5 L 75,41.5")

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any(
        "context_relief/source-derived-dem-fall-line-hachure crosses boxed "
        "source-derived-contour-altitude-label" in failure
        for failure in failures
    )


def test_hiking_semantic_contract_checks_designation_boundaries_not_furniture() -> None:
    manifest, root = _semantic_fixture()
    designation_layer = root.find(
        f".//{{{SVG_NS}}}g[@data-logical-layer='context_designations']"
    )
    if designation_layer is None:
        grey_group = root.find(f"{{{SVG_NS}}}g[@id='layer-pen-grey-0-25']")
        assert grey_group is not None
        designation_layer = ET.SubElement(
            grey_group,
            f"{{{SVG_NS}}}g",
            {"data-logical-layer": "context_designations"},
        )
    boundary = _path(
        designation_layer,
        role="source-sampled-designation-boundary",
        source_ref="osm-context",
        context=True,
    )
    boundary.set("d", "M 5,11.5 L 25,11.5")
    furniture = _path(
        designation_layer,
        role="context-detail-inset-frame",
        source_ref="osm-context",
        context=True,
    )
    furniture.set("d", "M 4,10 L 26,10 L 26,13 L 4,13 Z")

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any(
        "context_designations/source-sampled-designation-boundary crosses boxed "
        "settlement-label 'milngavie'" in failure
        for failure in failures
    )
    assert not any(
        "context-detail-inset-frame crosses boxed" in failure for failure in failures
    )


def test_hiking_semantic_contract_separates_detail_geography_from_overview_copy() -> (
    None
):
    manifest, root = _semantic_fixture()
    relief_layer = root.find(f".//{{{SVG_NS}}}g[@data-logical-layer='context_relief']")
    assert relief_layer is not None
    detail = _path(
        relief_layer,
        role="source-derived-dtm-contour",
        source_ref="osm-context",
        context=True,
        relief=True,
    )
    detail.attrib.update(
        {
            "d": "M 5,11.5 L 25,11.5",
            "data-context-view": "framed-north-up-route-detail",
            "data-detail-extent": "-1,50,0,51",
        }
    )

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert not any("crosses boxed" in failure for failure in failures)


def test_hiking_semantic_contract_rejects_invalid_leader_routing_metadata() -> None:
    manifest, root = _semantic_fixture()
    _add_context_label_leader(
        root,
        target_id="not-a-label",
        path_data="M 70,70 L 75,75",
        routing_policy="obsolete-policy",
        minimum_clearance="0.2",
    )
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any(
        "does not resolve to its own boxed label" in failure for failure in failures
    )
    assert any("invalid routing policy metadata" in failure for failure in failures)
    assert any("invalid clearance metadata" in failure for failure in failures)


def test_hiking_semantic_contract_rejects_non_linear_leader_path() -> None:
    manifest, root = _semantic_fixture()
    _add_context_label_leader(
        root,
        target_id="milngavie",
        path_data="M 5,11.5 C 7,10 8,12 10,11.5",
    )
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("is not an absolute linear M/L path" in failure for failure in failures)


def test_hiking_semantic_contract_rejects_contour_leader_identity_drift() -> None:
    manifest, root = _semantic_fixture()
    target_id = "contour-altitude-contour-400"
    _add_contour_label(
        root,
        label_id=target_id,
        contour_id="contour-400",
        label_box="60,40,10,3",
    )
    _add_contour_label_leader(
        root,
        target_id=target_id,
        contour_id="contour-500",
        path_data="M 55,41.5 L 60,41.5",
    )
    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-GB-WHW-01",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("invalid contour routing metadata" in failure for failure in failures)


def test_a5_contract_checks_orientation_dimensions_and_one_unit_per_mm() -> None:
    manifest, root = _semantic_fixture()
    manifest["page"] = {
        "paper": "A5",
        "orientation": "portrait",
        "format_id": "a5-portrait",
        "width_mm": 148.0,
        "height_mm": 210.0,
    }
    entry = {"format_id": "a5-portrait"}
    failures: list[str] = []
    _check_a5_contract(
        subject_id="RTE-GB-WHW-01",
        entry=entry,
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert failures == []
    root.set("viewBox", "0 0 210 148")
    _check_a5_contract(
        subject_id="RTE-GB-WHW-01",
        entry=entry,
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("one-user-unit-per-mm" in failure for failure in failures)


def test_coverage_above_reference_budget_remains_a_valid_measurement() -> None:
    assert _valid_coverage_measurement(0.73)
    assert not _valid_coverage_measurement(float("nan"))
    assert not _valid_coverage_measurement(-0.01)


def _paired_variant_fixture(variant_id: str) -> tuple[dict[str, object], ET.Element]:
    manifest, root = _semantic_fixture()
    manifest["data_snapshot"] = "2026-08-04T00:00:00Z"
    manifest["source"]["timestamp"] = manifest["data_snapshot"]  # type: ignore[index]
    manifest["catalog_record"]["data_snapshot"] = manifest["data_snapshot"]  # type: ignore[index]
    manifest["rendering"] = {
        **manifest["rendering"],  # type: ignore[arg-type]
        "hiking_variant": variant_id,
        "orientation_policy": "north-up",
        "north_is_page_up": True,
        "north_mark": True,
        "route_representation": {
            "sectional_detail_policy": "full-field-continuous-context-v2"
        },
        "profile_extrema_disclosure": {
            "status": "sampled-approximate",
            "policy_id": "sampled-elevation-approximate-extrema-v1",
            "source_ref": "native-terrain-source",
            "minimum_m": 100.0,
            "maximum_m": 900.0,
            "caption": "SAMPLED ELEVATION / APPROX 100-900 M / MEASURED KM",
            "distance_label_basis": "measured-source-chainage-v1",
        },
        "terrain_contour_hierarchy": {
            "policy_id": "factual-fifth-index-grey-pen-hierarchy-v1",
            "minor_pen_id": "grey-0-25",
            "index_pen_id": "grey-0-4",
            "minor_pen_width_mm": 0.25,
            "index_pen_width_mm": 0.4,
            "index_every_n_minor_levels": 5,
            "minor_interval_m": 200.0,
            "index_interval_m": 1000.0,
            "index_levels_m": [1000.0],
            "intermediate_levels_m": [400.0, 800.0],
            "interval_basis": "renderable-source-level-modal-interval",
            "fallback_index_used": False,
            "zero_elevation_index_suppressed": True,
            "bathymetry_status": "not-rendered-no-qualified-source",
        },
    }
    terrain_source = {
        "id": "native-terrain-source",
        "publisher": "Terrain publisher",
        "url": "https://example.test/terrain",
        "license": "CC-BY-4.0",
        "attribution": "Terrain publisher",
        "use": "source-native factual terrain and route elevation",
        "retrieved_at": "2026-08-03",
        "snapshot_sha256": "3" * 64,
    }
    manifest["sources"].append(terrain_source)  # type: ignore[union-attr]
    catalog_record = manifest["catalog_record"]  # type: ignore[index]
    catalog_record["route"] = {  # type: ignore[index]
        "source_ref": "osm-route",
        "profile_status": "source-elevation-sampled",
        "elevation_source_ref": "native-terrain-source",
        "elevation_method": "route-source-embedded-elevation-v1",
        "elevation_datum": "official orthometric datum",
    }
    catalog_record["terrain_derivation"] = {  # type: ignore[index]
        "source_precedence": {
            "policy_id": "hiking-factual-source-precedence-v1",
            "native_terrain_restored": True,
            "global_route_profile_retained": False,
            "terrain_source_ref": "native-terrain-source",
            "route_elevation_source_ref": "native-terrain-source",
        }
    }
    context = manifest["catalog_record"]["context"]  # type: ignore[index]
    context.update(
        {
            "rotation_deg": 0.0,
            "orientation_status": "north-up",
            "family_evidence": [
                {
                    "family": "roads",
                    "status": "source-query-zero-results",
                    "source_candidate_count": 0,
                    "selected_feature_count": 0,
                    "query_groups": ["linear"],
                },
                {
                    "family": "hydrography",
                    "status": "source-features-selected",
                    "source_candidate_count": 1,
                    "selected_feature_count": 1,
                    "query_groups": ["labels", "linear", "areas"],
                },
                {
                    "family": "landcover",
                    "status": "source-features-selected",
                    "source_candidate_count": 1,
                    "selected_feature_count": 1,
                    "query_groups": ["areas"],
                },
            ],
            "terrain": {
                "status": "source-derived-dtm-relief",
                "source_ref": "native-terrain-source",
                "derivation_id": "native-contours-v1",
                "contours": [
                    {
                        "id": f"contour-{elevation}",
                        "elevation_m": elevation,
                        "paths": [[[0, 0], [1, 1]]],
                    }
                    for elevation in (200, 400, 600, 800, 1000)
                ],
                "relief_strokes": [
                    {"id": f"frozen-fall-{index}"} for index in range(3)
                ],
            },
        }
    )
    marker_layer = root.find(f".//{{{SVG_NS}}}g[@data-logical-layer='context_markers']")
    assert marker_layer is not None
    for role in ("north-arrow", "north-arrow-head", "north-label"):
        _path(
            marker_layer,
            role=role,
            source_ref="osm-context",
            context=True,
        )
    context["features"].append(  # type: ignore[union-attr]
        {
            "id": "peak-one",
            "kind": "peak",
            "label": "PEAK ONE",
            "elevation_m": 812,
            "elevation_method": "osm-ele-tag",
            "elevation_source_ref": "osm-context",
            "elevation_source_object": "node/210",
            "osm_type": "node",
            "osm_id": 210,
        }
    )
    label_layer = root.find(f".//{{{SVG_NS}}}g[@data-logical-layer='context_labels']")
    assert label_layer is not None
    peak_label = _path(
        label_layer,
        role="peak-label",
        source_ref="osm-context",
        context=True,
        label_id="peak-one",
        label_box="72,30,14,3",
    )
    peak_label.attrib.update(
        {
            "data-elevation-m": "812",
            "data-elevation-method": "osm-ele-tag",
            "data-elevation-source-ref": "osm-context",
        }
    )

    relief_layer = root.find(
        f".//{{{SVG_NS}}}g[@data-logical-layer='context_relief']"
    )
    assert relief_layer is not None
    grey_index_group = root.find(f"{{{SVG_NS}}}g[@id='layer-pen-grey-0-4']")
    assert grey_index_group is not None
    relief_index_layer = ET.SubElement(
        grey_index_group,
        f"{{{SVG_NS}}}g",
        {"data-logical-layer": "context_relief_index"},
    )
    next(
        step
        for step in manifest["pen_sequence"]  # type: ignore[index]
        if step["pen_id"] == "grey-0-4"
    )["layers"].append("context_relief_index")
    for elevation in (200, 400, 600, 800, 1000):
        target_layer = relief_index_layer if elevation == 1000 else relief_layer
        contour = _path(
            target_layer,
            role="source-derived-dtm-contour",
            source_ref="native-terrain-source",
            context=True,
            relief=True,
        )
        contour.attrib.update(
            {
                "data-relief-status": "source-derived-dtm",
                "data-contour-id": f"contour-{elevation}",
                "data-elevation-m": str(elevation),
                "data-contour-class": "index" if elevation == 1000 else "minor",
                "data-contour-hierarchy-policy": (
                    "factual-fifth-index-grey-pen-hierarchy-v1"
                ),
                "data-contour-minor-interval-m": "200",
                "data-contour-index-interval-m": "1000",
                "data-contour-pen-width-mm": (
                    "0.4" if elevation == 1000 else "0.25"
                ),
                "data-contour-index-fallback": "false",
                "data-bathymetry-status": "not-rendered-no-qualified-source",
            }
        )

    if variant_id == "terrain-relief":
        manifest["rendering"]["terrain_fall_lines"] = {  # type: ignore[index]
            "source_stroke_count": 3,
            "clearance_eligible_path_count": 0,
            "cluster_rejected_path_count": 0,
            "retained_path_count": 0,
            "omission_reason": FALL_LINE_NO_CLUSTER_OMISSION_REASON,
        }
        grey_group = root.find(f"{{{SVG_NS}}}g[@id='layer-pen-grey-0-25']")
        assert grey_group is not None
        altitude_layer = ET.SubElement(
            grey_group,
            f"{{{SVG_NS}}}g",
            {"data-logical-layer": "context_relief_labels"},
        )
        altitude = _path(
            altitude_layer,
            role="source-derived-contour-altitude-label",
            source_ref="native-terrain-source",
            context=True,
            relief=True,
        )
        altitude.attrib.update(
            {
                "data-relief-status": "source-derived-dtm",
                "data-contour-id": "contour-1000",
                "data-elevation-m": "1000",
                "data-contour-class": "index",
                "data-contour-hierarchy-policy": (
                    "factual-fifth-index-grey-pen-hierarchy-v1"
                ),
                "data-contour-minor-interval-m": "200",
                "data-contour-index-interval-m": "1000",
                "data-bathymetry-status": "not-rendered-no-qualified-source",
            }
        )

    grey_group = root.find(f"{{{SVG_NS}}}g[@id='layer-pen-grey-0-25']")
    black_group = root.find(f"{{{SVG_NS}}}g[@id='layer-pen-black-0-25']")
    assert grey_group is not None and black_group is not None
    guides = ET.SubElement(
        grey_group,
        f"{{{SVG_NS}}}g",
        {"data-logical-layer": "profile_guides"},
    )
    annotations = ET.SubElement(
        black_group,
        f"{{{SVG_NS}}}g",
        {"data-logical-layer": "route_annotations"},
    )
    profile_attributes = {
        "data-profile-status": "source-elevation-sampled",
        "data-distance-axis": "source-geometry-cumulative-geodesic-v1",
        "data-measured-distance-m": "100000.000",
        "data-elevation-min-m": "100.000",
        "data-elevation-max-m": "900.000",
        "data-elevation-method": "route-source-embedded-elevation-v1",
        "data-elevation-datum": "official orthometric datum",
        "data-elevation-extrema-status": "sampled-approximate",
        "data-elevation-extrema-policy": (
            "sampled-elevation-approximate-extrema-v1"
        ),
        "data-elevation-extrema-source-ref": "native-terrain-source",
        "data-elevation-extrema-caption": (
            "SAMPLED ELEVATION / APPROX 100-900 M / MEASURED KM"
        ),
        "data-distance-label-basis": "measured-source-chainage-v1",
    }
    baseline = _path(
        guides,
        role="profile-baseline",
        source_ref="native-terrain-source",
    )
    baseline.attrib.update(profile_attributes)
    profile = _path(
        annotations,
        role="source-elevation-profile",
        source_ref="native-terrain-source",
    )
    profile.attrib.update(profile_attributes)
    profile_status = _path(
        annotations,
        role="profile-status",
        source_ref="native-terrain-source",
    )
    profile_status.attrib.update(profile_attributes)
    for index, (station_id, fraction) in enumerate(
        zip("ABCDE", (0.0, 0.25, 0.5, 0.75, 1.0), strict=True)
    ):
        measured_m = fraction * 100_000.0
        station_attributes = {
            "data-chainage-id": station_id,
            "data-chainage-m": f"{measured_m:.3f}",
            "data-distance-km": f"{measured_m / 1_000.0:.6f}",
            "data-measured-chainage-m": f"{measured_m:.3f}",
            "data-displayed-distance-m": f"{measured_m:.3f}",
            "data-displayed-distance-km": f"{measured_m / 1_000.0:.6f}",
            "data-route-fraction": f"{fraction:.9f}",
            "data-longitude": f"{-4.0 + fraction:.9f}",
            "data-latitude": f"{56.0 + fraction:.9f}",
            "data-elevation-m": f"{100.0 + index * 200.0:.3f}",
            "data-elevation-status": "interpolated",
            "data-source-vertex-before": str(index),
            "data-source-vertex-after": str(index + 1),
            "data-source-segment-fraction": f"{fraction:.9f}",
            "data-chainage-basis": "source-geometry-cumulative-geodesic-v1",
            "data-distance-label-basis": "measured-source-chainage-v1",
            "data-route-source-ref": "osm-route",
            "data-profile-status": "source-elevation-sampled",
            "data-elevation-source-ref": "native-terrain-source",
            "data-elevation-method": "route-source-embedded-elevation-v1",
            "data-elevation-datum": "official orthometric datum",
        }
        for role, layer, source_ref in (
            ("map-chainage-label", annotations, "osm-route"),
            ("profile-chainage-label", annotations, "native-terrain-source"),
            ("profile-chainage-tick", guides, "native-terrain-source"),
            ("profile-chainage-station", annotations, "native-terrain-source"),
        ):
            path = _path(layer, role=role, source_ref=source_ref)
            path.attrib.update(station_attributes)
    return manifest, root


def test_paired_variant_contract_accepts_both_north_up_artworks() -> None:
    for variant_id in EXPECTED_VARIANTS:
        manifest, root = _paired_variant_fixture(variant_id)
        failures: list[str] = []
        _check_paired_hiking_variant_semantics(
            subject_id="RTE-GB-WHW-01",
            variant_id=variant_id,
            manifest=manifest,
            root=root,
            failures=failures,
        )
        assert failures == []


def test_relief_variant_uses_independently_selected_relief_terrain() -> None:
    manifest, root = _paired_variant_fixture("terrain-relief")
    relief_source = {
        "id": "aws-mapzen-terrarium-z9",
        "publisher": "Mapzen terrain tiles on AWS",
        "url": "https://registry.opendata.aws/terrain-tiles/",
        "license": "provider-review-required",
        "attribution": "Mapzen terrain tiles on AWS",
        "use": "terrain-relief edition continuous elevation contours",
        "retrieved_at": "2026-08-03",
        "snapshot_sha256": "4" * 64,
        "attribution_status": "provider-review-required",
    }
    manifest["sources"].append(relief_source)  # type: ignore[union-attr]
    context = manifest["catalog_record"]["context"]  # type: ignore[index]
    context["relief_terrain"] = {
        "status": "source-derived-dtm-relief",
        "source_ref": relief_source["id"],
        "derivation_id": "global-relief-v1",
        "contours": [
            {
                "id": f"relief-{elevation}",
                "elevation_m": elevation,
                "paths": [[[0, 0], [1, 1]]],
            }
            for elevation in (100, 300, 500, 700, 1000)
        ],
        "relief_strokes": [{"id": "fall-1"}, {"id": "fall-2"}],
    }
    precedence = manifest["catalog_record"]["terrain_derivation"][  # type: ignore[index]
        "source_precedence"
    ]
    precedence.update(  # type: ignore[union-attr]
        {
            "relief_terrain_source_ref": relief_source["id"],
            "global_relief_terrain_retained": True,
        }
    )
    manifest["rendering"]["terrain_fall_lines"][  # type: ignore[index]
        "source_stroke_count"
    ] = 2
    contour_paths = root.findall(
        f".//{{{SVG_NS}}}path[@data-role='source-derived-dtm-contour']"
    )
    for path, elevation in zip(
        contour_paths, (100, 300, 500, 700, 1000), strict=True
    ):
        path.set("data-source-ref", str(relief_source["id"]))
        path.set("data-contour-id", f"relief-{elevation}")
        path.set("data-elevation-m", str(elevation))
    altitude_paths = root.findall(
        f".//{{{SVG_NS}}}path["
        "@data-role='source-derived-contour-altitude-label']"
    )
    assert altitude_paths
    for path in altitude_paths:
        path.set("data-source-ref", str(relief_source["id"]))
        path.set("data-contour-id", "relief-1000")
        path.set("data-elevation-m", "1000")

    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-DUAL-TERRAIN",
        variant_id="terrain-relief",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert failures == []

    contour_paths[0].set("data-source-ref", "native-terrain-source")
    failures = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-DUAL-TERRAIN",
        variant_id="terrain-relief",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert sum("rendered contour lacks factual" in item for item in failures) == 1


def _sparse_native_detailed_fixture() -> tuple[dict[str, object], ET.Element]:
    manifest, root = _paired_variant_fixture("detailed-map")
    relief_source = {
        "id": "aws-mapzen-terrarium-z9",
        "publisher": "Mapzen terrain tiles on AWS",
        "url": "https://registry.opendata.aws/terrain-tiles/",
        "license": "provider-review-required",
        "attribution": "Mapzen terrain tiles on AWS",
        "use": "full-field factual relief fallback for sparse native terrain",
        "retrieved_at": "2026-08-03",
        "snapshot_sha256": "8" * 64,
        "attribution_status": "provider-review-required",
    }
    manifest["sources"].append(relief_source)  # type: ignore[union-attr]
    context = manifest["catalog_record"]["context"]  # type: ignore[index]
    context["relief_terrain"] = {
        "status": "source-derived-dtm-relief",
        "source_ref": relief_source["id"],
        "derivation_id": "global-relief-v1",
        "contours": [
            {
                "id": f"relief-{elevation}",
                "elevation_m": elevation,
                "paths": [[[0, 0], [1, 1]]],
            }
            for elevation in (100, 300, 500, 700, 1000)
        ],
        "relief_strokes": [],
    }
    selection = {
        "policy_id": "hiking-relief-terrain-density-selection-v1",
        "selected": "global-relief-terrain",
        "native": {
            "contour_level_count": 4,
            "contour_path_count": 4,
            "normalized_full_field_length": 3.0,
        },
        "global": {
            "contour_level_count": 5,
            "contour_path_count": 5,
            "normalized_full_field_length": 18.0,
        },
    }
    precedence = manifest["catalog_record"]["terrain_derivation"][  # type: ignore[index]
        "source_precedence"
    ]
    precedence.update(  # type: ignore[union-attr]
        {
            "relief_terrain_source_ref": relief_source["id"],
            "global_relief_terrain_retained": True,
            "relief_terrain_selection": selection,
        }
    )
    manifest["rendering"]["detailed_terrain_source_policy"] = {  # type: ignore[index]
        "policy_id": "full-field-relief-fallback-for-sparse-native-context-v1",
        "native_source_ref": "native-terrain-source",
        "selected_source_ref": relief_source["id"],
        "selection_evidence": copy.deepcopy(selection),
    }
    contour_paths = root.findall(
        f".//{{{SVG_NS}}}path[@data-role='source-derived-dtm-contour']"
    )
    for path, elevation in zip(
        contour_paths, (100, 300, 500, 700, 1000), strict=True
    ):
        path.set("data-source-ref", str(relief_source["id"]))
        path.set("data-contour-id", f"relief-{elevation}")
        path.set("data-elevation-m", str(elevation))
    return manifest, root


def test_detailed_variant_accepts_evidenced_sparse_native_relief_fallback() -> None:
    manifest, root = _sparse_native_detailed_fixture()
    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-DETAILED-TERRAIN-FALLBACK",
        variant_id="detailed-map",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert failures == []


def test_detailed_terrain_fallback_fails_closed_on_metadata_or_source_drift() -> None:
    manifest, root = _sparse_native_detailed_fixture()
    policy = manifest["rendering"]["detailed_terrain_source_policy"]  # type: ignore[index]
    policy["selected_source_ref"] = "unregistered-relief"  # type: ignore[index]
    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-DETAILED-TERRAIN-FALLBACK",
        variant_id="detailed-map",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert sum("detailed terrain source policy" in item for item in failures) == 1

    manifest, root = _sparse_native_detailed_fixture()
    contour = next(
        path
        for path in root.iter(f"{{{SVG_NS}}}path")
        if path.get("data-role") == "source-derived-dtm-contour"
    )
    contour.set("data-source-ref", "native-terrain-source")
    failures = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-DETAILED-TERRAIN-FALLBACK",
        variant_id="detailed-map",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert sum("rendered contour lacks factual" in item for item in failures) == 1


def test_structurally_referenced_paired_terrain_is_not_superseded() -> None:
    manifest, root = _semantic_fixture()
    retained_source = {
        "id": "relief-terrain-source",
        "publisher": "Terrain publisher",
        "url": "https://example.test/terrain",
        "license": "CC-BY-4.0",
        "attribution": "Terrain publisher",
        "use": "terrain-relief edition factual DEM contours",
        "retrieved_at": "2026-08-03",
        "snapshot_sha256": "7" * 64,
    }
    manifest["sources"].append(retained_source)  # type: ignore[union-attr]
    context = manifest["catalog_record"]["context"]  # type: ignore[index]
    context["relief_terrain"] = {"source_ref": retained_source["id"]}
    metadata = root.find(f"{{{SVG_NS}}}metadata")
    assert metadata is not None
    embedded = json.loads(metadata.text or "{}")
    embedded["sources"] = manifest["sources"]
    metadata.text = json.dumps(embedded)

    failures: list[str] = []
    _check_hiking_semantics(
        subject_id="RTE-TEST-PAIRED-SOURCES",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert not any("superseded unreferenced terrain" in item for item in failures)

    context.pop("relief_terrain")
    failures = []
    _check_hiking_semantics(
        subject_id="RTE-TEST-PAIRED-SOURCES",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("superseded unreferenced terrain" in item for item in failures)


def test_sublegible_selected_road_may_truthfully_emit_no_ink() -> None:
    manifest, root = _paired_variant_fixture("detailed-map")
    context = manifest["catalog_record"]["context"]  # type: ignore[index]
    context["features"].append(  # type: ignore[union-attr]
        {
            "id": "road-sub-mm",
            "kind": "road",
            "source_ref": "osm-context",
            "paths": [[[0, 0], [0.000001, 0.000001]]],
        }
    )
    road_evidence = next(
        item
        for item in context["family_evidence"]  # type: ignore[index]
        if item["family"] == "roads"
    )
    road_evidence.update(
        {
            "status": "source-features-selected-sub-legible-at-page-scale",
            "source_candidate_count": 1,
            "selected_feature_count": 1,
            "page_legibility_assessed_feature_count": 1,
            "page_legible_feature_count": 0,
            "sub_legible_feature_count": 1,
        }
    )
    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-SUB-MM-ROAD",
        variant_id="detailed-map",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert failures == []


def test_peak_glyphs_accept_exact_serialization_rounding_and_fail_once() -> None:
    manifest, root = _paired_variant_fixture("detailed-map")
    peak = manifest["catalog_record"]["context"]["features"][-1]  # type: ignore[index]
    peak["elevation_m"] = 1662.074  # type: ignore[index]
    label_layer = root.find(
        f".//{{{SVG_NS}}}g[@data-logical-layer='context_labels']"
    )
    assert label_layer is not None
    peak_path = next(
        path
        for path in label_layer
        if path.get("data-role") == "peak-label"
    )
    peak_path.set("data-elevation-m", "1662.07")
    for _ in range(5):
        label_layer.append(copy.deepcopy(peak_path))

    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-PEAK-ROUNDING",
        variant_id="detailed-map",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert not any("peak label" in item for item in failures)

    for path in label_layer:
        if path.get("data-role") == "peak-label":
            path.set("data-elevation-m", "1662.5")
    failures = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-PEAK-ROUNDING",
        variant_id="detailed-map",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert sum("peak label" in item for item in failures) == 1


def test_paired_variant_contract_rejects_release_snapshot_drift() -> None:
    manifest, root = _paired_variant_fixture("detailed-map")
    manifest["source"]["timestamp"] = "2026-08-02"  # type: ignore[index]
    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-GB-WHW-01",
        variant_id="detailed-map",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("data_snapshot" in failure for failure in failures)


def test_paired_variant_contract_rejects_orientation_and_altitude_drift() -> None:
    manifest, root = _paired_variant_fixture("terrain-relief")
    manifest["rendering"]["north_is_page_up"] = False  # type: ignore[index]
    manifest["rendering"]["north_mark"] = False  # type: ignore[index]
    context = manifest["catalog_record"]["context"]  # type: ignore[index]
    context["orientation_status"] = "rotated-to-fit-artwork"
    peak_labels = root.findall(f".//{{{SVG_NS}}}path[@data-role='peak-label']")
    assert peak_labels
    for path in peak_labels:
        path.attrib.pop("data-elevation-m", None)
    altitude_layer = root.find(
        f".//{{{SVG_NS}}}g[@data-logical-layer='context_relief_labels']"
    )
    assert altitude_layer is not None
    altitude_layer.clear()
    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-GB-WHW-01",
        variant_id="terrain-relief",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("north-up" in failure for failure in failures)
    assert any("peak label" in failure for failure in failures)
    assert any("contour labels" in failure for failure in failures)


def test_paired_variant_contract_allows_peak_without_explicit_height() -> None:
    manifest, root = _paired_variant_fixture("detailed-map")
    peak = manifest["catalog_record"]["context"]["features"][-1]  # type: ignore[index]
    for field in (
        "elevation_m",
        "elevation_method",
        "elevation_source_ref",
        "elevation_source_object",
    ):
        peak.pop(field)  # type: ignore[union-attr]
    for path in root.findall(f".//{{{SVG_NS}}}path[@data-role='peak-label']"):
        for attribute in (
            "data-elevation-m",
            "data-elevation-method",
            "data-elevation-source-ref",
        ):
            path.attrib.pop(attribute, None)
    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-UNHEIGHTED-PEAK",
        variant_id="detailed-map",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert failures == []


def test_paired_variant_contract_rejects_inferred_peak_height() -> None:
    manifest, root = _paired_variant_fixture("detailed-map")
    peak = manifest["catalog_record"]["context"]["features"][-1]  # type: ignore[index]
    peak["elevation_method"] = "mapzen-raster-sample"  # type: ignore[index]
    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-INFERRED-PEAK",
        variant_id="detailed-map",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("explicit authoritative source" in failure for failure in failures)


def test_paired_variant_contract_rejects_insets_frames_and_fall_lines() -> None:
    forbidden = (
        ("context-detail-inset-frame", "data-context-view"),
        ("profile-frame", None),
        ("source-derived-dem-fall-line-hachure", None),
    )
    for role, context_attribute in forbidden:
        manifest, root = _paired_variant_fixture("terrain-relief")
        relief_layer = root.find(
            f".//{{{SVG_NS}}}g[@data-logical-layer='context_relief']"
        )
        assert relief_layer is not None
        forbidden_path = _path(
            relief_layer,
            role=role,
            source_ref="native-terrain-source",
            context=True,
            relief=True,
        )
        if context_attribute:
            forbidden_path.set(context_attribute, "framed-north-up-route-detail")
        failures: list[str] = []
        _check_paired_hiking_variant_semantics(
            subject_id="RTE-TEST-FULL-FIELD",
            variant_id="terrain-relief",
            manifest=manifest,
            root=root,
            failures=failures,
        )
        assert any("full-field v4 contract" in failure for failure in failures)


def test_paired_variant_contract_rejects_chainage_mapping_drift() -> None:
    manifest, root = _paired_variant_fixture("terrain-relief")
    profile_b = next(
        path
        for path in root.findall(
            f".//{{{SVG_NS}}}path[@data-role='profile-chainage-label']"
        )
        if path.get("data-chainage-id") == "B"
    )
    profile_b.set("data-distance-km", "99.000000")
    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-CHAINAGE-DRIFT",
        variant_id="terrain-relief",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("same profile distance/elevation" in failure for failure in failures)


def test_paired_variant_contract_rejects_native_source_precedence_drift() -> None:
    manifest, root = _paired_variant_fixture("detailed-map")
    precedence = manifest["catalog_record"]["terrain_derivation"][  # type: ignore[index]
        "source_precedence"
    ]
    precedence["native_terrain_restored"] = False  # type: ignore[index]
    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-PRECEDENCE-DRIFT",
        variant_id="detailed-map",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("source precedence drift" in failure for failure in failures)


def test_paired_variant_contract_rejects_thin_context_contour_stack() -> None:
    manifest, root = _paired_variant_fixture("detailed-map")
    relief_layer = root.find(f".//{{{SVG_NS}}}g[@data-logical-layer='context_relief']")
    assert relief_layer is not None
    contour_paths = [
        path
        for path in relief_layer
        if path.get("data-role") == "source-derived-dtm-contour"
    ]
    for path in contour_paths[-2:]:
        relief_layer.remove(path)
    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-THIN-CONTOURS",
        variant_id="detailed-map",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("full-field contour stack" in failure for failure in failures)


def test_paired_variant_contract_rejects_minor_index_partition_overlap() -> None:
    manifest, root = _paired_variant_fixture("terrain-relief")
    relief_layer = root.find(f".//{{{SVG_NS}}}g[@data-logical-layer='context_relief']")
    index_layer = root.find(
        f".//{{{SVG_NS}}}g[@data-logical-layer='context_relief_index']"
    )
    assert relief_layer is not None and index_layer is not None
    duplicate = copy.deepcopy(
        next(
            path
            for path in index_layer
            if path.get("data-role") == "source-derived-dtm-contour"
        )
    )
    duplicate.set("data-contour-class", "minor")
    duplicate.set("data-contour-pen-width-mm", "0.25")
    relief_layer.append(duplicate)

    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-CONTOUR-PARTITION",
        variant_id="terrain-relief",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("minor/index contour partitions overlap" in item for item in failures)


def test_paired_variant_contract_rejects_negative_contour_without_bathymetry() -> (
    None
):
    manifest, root = _paired_variant_fixture("terrain-relief")
    relief_layer = root.find(f".//{{{SVG_NS}}}g[@data-logical-layer='context_relief']")
    assert relief_layer is not None
    contour = next(
        path
        for path in relief_layer
        if path.get("data-role") == "source-derived-dtm-contour"
    )
    contour.set("data-elevation-m", "-200")
    contour.set("data-contour-id", "contour-minus-200")

    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-NO-BATHYMETRY",
        variant_id="terrain-relief",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any(
        "negative or invalid contour elevation rendered while bathymetry is disabled"
        in item
        for item in failures
    )


def test_paired_variant_contract_rejects_exact_profile_extrema_without_evidence() -> (
    None
):
    manifest, root = _paired_variant_fixture("detailed-map")
    status_path = next(
        path
        for path in root.iter(f"{{{SVG_NS}}}path")
        if path.get("data-role") == "profile-status"
    )
    status_path.set("data-elevation-extrema-status", "source-verified-exact")
    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-EXACT-PROFILE-CLAIM",
        variant_id="detailed-map",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("required evidence disclosure" in failure for failure in failures)


def test_paired_variant_contract_rejects_nonzero_fall_line_summary() -> None:
    manifest, root = _paired_variant_fixture("terrain-relief")
    manifest["rendering"]["terrain_fall_lines"][  # type: ignore[index]
        "retained_path_count"
    ] = 1
    failures: list[str] = []
    _check_paired_hiking_variant_semantics(
        subject_id="RTE-TEST-FALL-LINE-SUMMARY",
        variant_id="terrain-relief",
        manifest=manifest,
        root=root,
        failures=failures,
    )
    assert any("zero-hachure terrain policy" in failure for failure in failures)


def test_composition_audit_rejects_return_of_an_inset_panel(tmp_path: Path) -> None:
    manifest, root = _paired_variant_fixture("detailed-map")
    manifest.update(
        {
            "artifact_id": "RTE-TEST-V4--detailed-map",
            "subject_id": "RTE-TEST-V4",
            "variant_id": "detailed-map",
            "page": {
                "zones_mm": {
                    "map_field": {
                        "x": 12.0,
                        "y": 35.122,
                        "width": 124.0,
                        "height": 128.924,
                    }
                }
            },
        }
    )
    profile_roles = {
        "profile-baseline",
        "source-elevation-profile",
        "profile-chainage-tick",
        "profile-chainage-station",
    }
    for path in root.iter(f"{{{SVG_NS}}}path"):
        if path.get("data-role") in profile_roles:
            path.set("d", "M 16 150 L 20 150")
    svg_path = tmp_path / "RTE-TEST-V4--detailed-map.svg"
    plot_path = svg_path.with_suffix(".plot.json")
    ET.ElementTree(root).write(svg_path, encoding="utf-8", xml_declaration=True)
    plot_path.write_text(json.dumps(manifest), encoding="utf-8")

    baseline = audit_artifact(svg_path)
    assert baseline.contract.status == "pass"

    relief_layer = root.find(f".//{{{SVG_NS}}}g[@data-logical-layer='context_relief']")
    assert relief_layer is not None
    inset = _path(
        relief_layer,
        role="context-detail-inset-frame",
        source_ref="native-terrain-source",
        context=True,
        relief=True,
    )
    inset.set("data-context-view", "framed-north-up-route-detail")
    ET.ElementTree(root).write(svg_path, encoding="utf-8", xml_declaration=True)

    rejected = audit_artifact(svg_path)
    assert rejected.contract.status == "fail"
    assert rejected.contract.context_view_path_count == 1
    assert rejected.contract.forbidden_role_counts == {
        "context-detail-inset-frame": 1
    }


def test_suite_contract_requires_exact_paired_release_inventory(
    tmp_path: Path,
) -> None:
    hiking_dir = tmp_path / "hikes"
    hiking_dir.mkdir()
    expected_subject_ids = frozenset(
        f"RTE-TEST-{index:03d}" for index in range(1, EXPECTED_SUBJECT_COUNT + 1)
    )
    entries: list[dict[str, object]] = []
    for subject_id in sorted(expected_subject_ids):
        for variant_id in EXPECTED_VARIANTS:
            artifact_id = f"{subject_id}--{variant_id}"
            svg = hiking_dir / f"{artifact_id}.svg"
            manifest = hiking_dir / f"{artifact_id}.plot.json"
            png = hiking_dir / f"{artifact_id}.png"
            for path in (svg, manifest, png):
                path.write_bytes(b"fixture")
            entries.append(
                {
                    "id": artifact_id,
                    "artifact_id": artifact_id,
                    "subject_id": subject_id,
                    "variant_id": variant_id,
                    "domain": "hikes",
                    "outputs": {
                        "svg": {"path": str(svg)},
                        "manifest": {"path": str(manifest)},
                        "png": {"path": str(png)},
                        "pen_files": [],
                    },
                }
            )
    contacts: dict[str, str] = {}
    for variant_id in EXPECTED_VARIANTS:
        contact = hiking_dir / f"hikes-{variant_id}-contact-sheet.png"
        contact.write_bytes(b"fixture")
        contacts[variant_id] = str(contact)
    index = {
        "schema_version": 2,
        "count": EXPECTED_ARTIFACT_COUNT,
        "subject_count": EXPECTED_SUBJECT_COUNT,
        "artifact_count": EXPECTED_ARTIFACT_COUNT,
        "variants": list(EXPECTED_VARIANTS),
        "counts_by_domain": {"hikes": EXPECTED_ARTIFACT_COUNT},
        "counts_by_variant": {
            variant_id: EXPECTED_SUBJECT_COUNT for variant_id in EXPECTED_VARIANTS
        },
        "contact_sheets": {"hikes": contacts},
    }
    source_subjects = []
    for subject_id in sorted(expected_subject_ids):
        source_subjects.append(
            {
                "subject_id": subject_id,
                "artifacts": [
                    {
                        "artifact_id": f"{subject_id}--{variant_id}",
                        "variant_id": variant_id,
                    }
                    for variant_id in EXPECTED_VARIANTS
                ],
            }
        )
    (tmp_path / "SOURCES.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_status": "review-only",
                "commercial_clearance_status": "incomplete",
                "subject_count": EXPECTED_SUBJECT_COUNT,
                "artifact_count": EXPECTED_ARTIFACT_COUNT,
                "provider_attribution_review_required": True,
                "subjects": source_subjects,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "LICENSES.txt").write_text(
        "NOT COMMERCIALLY CLEARED / MAPZEN PROVIDER REVIEW",
        encoding="utf-8",
    )
    failures: list[str] = []
    _check_suite_contract(
        index=index,
        entries=entries,
        series_dir=tmp_path,
        failures=failures,
        expected_subject_ids=expected_subject_ids,
    )
    assert failures == []
    (hiking_dir / "orphan.svg").write_bytes(b"fixture")
    _check_suite_contract(
        index=index,
        entries=entries,
        series_dir=tmp_path,
        failures=failures,
        expected_subject_ids=expected_subject_ids,
    )
    assert any("SVG inventory drift" in failure for failure in failures)
