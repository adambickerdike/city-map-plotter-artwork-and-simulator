#!/usr/bin/env python3
"""Freeze authoritative facts and exact OSM objects for former-F1 courses.

The acquisition layer deliberately uses the read-only OpenStreetMap API rather
than a rendered map.  Each raw API response is retained and hash-bound beside a
deterministic, geometry-hydrated union used by the offline compiler.  No gap is
closed and no official circuit illustration is downloaded as geometry.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
CONTRACT_ROOT = ROOT / "contracts" / "f1-circuits-legacy-v1"
REGISTRY_PATH = CONTRACT_ROOT / "event-registry.json"
MANIFEST_PATH = CONTRACT_ROOT / "source-manifest.json"
OFFICIAL_ROOT = CONTRACT_ROOT / "source-extracts" / "official"
OSM_ROOT = CONTRACT_ROOT / "source-extracts" / "osm"
USER_AGENT = (
    "city-map-plotter-f1-legacy-source-builder/1.0 "
    "(+https://www.openstreetmap.org/copyright)"
)
OSM_API = "https://api.openstreetmap.org/api/0.6"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.casefold()).strip("-")


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _fetch(url: str, *, timeout: float, attempts: int = 4) -> tuple[bytes, str]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json,text/html,application/pdf;q=0.9,*/*;q=0.5",
                "Accept-Encoding": "identity",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(), response.geturl()
        except Exception as exc:  # recorded in the immutable manifest
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(12.0, 2.0**attempt))
    assert last_error is not None
    raise last_error


def _write_gzip(path: Path, payload: bytes) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    compressed = gzip.compress(payload, compresslevel=9, mtime=0)
    path.write_bytes(compressed)
    return compressed


def _component(path: Path, url: str, payload: bytes) -> dict[str, Any]:
    compressed = _write_gzip(path, payload)
    return {
        "path": _relative(path),
        "url": url,
        "payload_bytes": len(payload),
        "payload_sha256": _sha256(payload),
        "compressed_bytes": len(compressed),
        "compressed_sha256": _sha256(compressed),
    }


def _source_entry(
    *,
    source_id: str,
    source_kind: str,
    publisher: str,
    title: str,
    url: str,
    path: Path,
    media_type: str,
    retrieved_at: str,
    payload: bytes,
    compressed: bytes,
    licence: str,
    commercial_use_status: str,
    allowed_uses: list[str],
    attribution: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": source_id,
        "source_kind": source_kind,
        "publisher": publisher,
        "title": title,
        "url": url,
        "path": _relative(path),
        "media_type": media_type,
        "retrieved_at": retrieved_at,
        "payload_bytes": len(payload),
        "payload_sha256": _sha256(payload),
        "compressed_bytes": len(compressed),
        "compressed_sha256": _sha256(compressed),
        "licence": licence,
        "commercial_use_status": commercial_use_status,
        "allowed_uses": allowed_uses,
        "attribution": attribution,
    }
    if extra:
        result.update(extra)
    return result


def _existing_sources() -> dict[str, dict[str, Any]]:
    if not MANIFEST_PATH.is_file():
        return {}
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {
        str(source["id"]): source
        for source in manifest.get("sources", [])
        if isinstance(source, dict) and source.get("id")
    }


def _reusable(source: dict[str, Any] | None) -> bool:
    if not source or not source.get("path"):
        return False
    path = ROOT / str(source["path"])
    if not path.is_file():
        return False
    compressed = path.read_bytes()
    try:
        payload = gzip.decompress(compressed)
    except gzip.BadGzipFile:
        return False
    if _sha256(compressed) != source.get("compressed_sha256"):
        return False
    if _sha256(payload) != source.get("payload_sha256"):
        return False
    for component in source.get("component_snapshots", []):
        component_path = ROOT / str(component.get("path") or "")
        if not component_path.is_file():
            return False
        component_compressed = component_path.read_bytes()
        if _sha256(component_compressed) != component.get("compressed_sha256"):
            return False
        try:
            component_payload = gzip.decompress(component_compressed)
        except gzip.BadGzipFile:
            return False
        if _sha256(component_payload) != component.get("payload_sha256"):
            return False
    return True


def _official_entry(source: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    payload, final_url = _fetch(str(source["url"]), timeout=timeout)
    media_type = str(source.get("media_type") or "text/html")
    extension = str(
        source.get("file_extension")
        or ("pdf" if media_type == "application/pdf" else "html")
    ).lstrip(".")
    path = OFFICIAL_ROOT / f"{_safe_name(str(source['id']))}.{extension}.gz"
    compressed = _write_gzip(path, payload)
    return _source_entry(
        source_id=str(source["id"]),
        source_kind=str(source["source_kind"]),
        publisher=str(source["publisher"]),
        title=str(source["title"]),
        url=final_url,
        path=path,
        media_type=media_type,
        retrieved_at=datetime.now(UTC).isoformat(),
        payload=payload,
        compressed=compressed,
        licence=str(source.get("licence") or "all-rights-reserved-reference-only"),
        commercial_use_status=str(
            source.get("commercial_use_status") or "reference-only"
        ),
        allowed_uses=list(
            source.get("allowed_uses")
            or ["factual-transcription", "source-validation"]
        ),
        attribution=str(source.get("attribution") or source["publisher"]),
        extra={
            key: source[key]
            for key in ("geometry_derivation_status", "evidence_scope")
            if source.get(key) is not None
        },
    )


def _merge_elements(target: dict[tuple[str, int], dict[str, Any]], value: Any) -> None:
    if not isinstance(value, dict):
        return
    for element in value.get("elements", []):
        if not isinstance(element, dict):
            continue
        element_type = element.get("type")
        element_id = element.get("id")
        if isinstance(element_type, str) and isinstance(element_id, int):
            target[(element_type, element_id)] = element


def _hydrate_way_geometry(
    elements: dict[tuple[str, int], dict[str, Any]]
) -> None:
    nodes = {
        element_id: element
        for (element_type, element_id), element in elements.items()
        if element_type == "node"
    }
    for (element_type, _), element in elements.items():
        if element_type != "way":
            continue
        geometry: list[dict[str, float]] = []
        for node_id in element.get("nodes", []):
            node = nodes.get(node_id)
            if node is None:
                continue
            latitude = node.get("lat")
            longitude = node.get("lon")
            if isinstance(latitude, (int, float)) and isinstance(
                longitude, (int, float)
            ):
                geometry.append({"lat": float(latitude), "lon": float(longitude)})
        if geometry:
            element["geometry"] = geometry


def _track_requests(event: dict[str, Any]) -> list[str]:
    selection = event["osm_selection"]
    if selection["mode"] == "relation":
        return [f"{OSM_API}/relation/{int(selection['relation_id'])}/full.json"]
    if selection["mode"] == "explicit-ordered-way-list":
        return [
            f"{OSM_API}/way/{int(way_id)}/full.json"
            for way_id in selection["way_ids"]
        ]
    raise ValueError(f"Unsupported OSM selection mode: {selection['mode']!r}")


def _selected_way_ids(
    event: dict[str, Any], elements: dict[tuple[str, int], dict[str, Any]]
) -> list[int]:
    selection = event["osm_selection"]
    if selection["mode"] == "explicit-ordered-way-list":
        return [int(value) for value in selection["way_ids"]]
    relation = elements.get(("relation", int(selection["relation_id"])))
    if relation is None:
        raise ValueError("selected OSM relation is absent from its full response")
    return [
        int(member["ref"])
        for member in relation.get("members", [])
        if isinstance(member, dict)
        and member.get("type") == "way"
        and isinstance(member.get("ref"), int)
    ]


def _context_bbox(
    event: dict[str, Any], elements: dict[tuple[str, int], dict[str, Any]], padding_m: float
) -> tuple[str, list[int]]:
    selected_ids = _selected_way_ids(event, elements)
    coordinates: list[tuple[float, float]] = []
    for way_id in selected_ids:
        way = elements.get(("way", way_id))
        if way is None:
            continue
        for value in way.get("geometry", []):
            if isinstance(value.get("lat"), (int, float)) and isinstance(
                value.get("lon"), (int, float)
            ):
                coordinates.append((float(value["lat"]), float(value["lon"])))
    if not coordinates:
        raise ValueError("selected OSM circuit source contains no coordinates")
    south = min(value[0] for value in coordinates)
    north = max(value[0] for value in coordinates)
    west = min(value[1] for value in coordinates)
    east = max(value[1] for value in coordinates)
    centre_latitude = (south + north) / 2.0
    latitude_padding = padding_m / 111_320.0
    longitude_padding = padding_m / max(
        1.0, 111_320.0 * math.cos(math.radians(centre_latitude))
    )
    bbox = (
        f"{west - longitude_padding:.7f},"
        f"{south - latitude_padding:.7f},"
        f"{east + longitude_padding:.7f},"
        f"{north + latitude_padding:.7f}"
    )
    return bbox, selected_ids


def _context_tiles(event: dict[str, Any], bbox: str) -> list[str]:
    """Split large source envelopes without changing the retained envelope."""

    grid = event.get("context_grid")
    if grid is None:
        return [bbox]
    if not isinstance(grid, dict):
        raise ValueError("context_grid must be an object")
    columns = grid.get("columns")
    rows = grid.get("rows")
    if (
        isinstance(columns, bool)
        or not isinstance(columns, int)
        or isinstance(rows, bool)
        or not isinstance(rows, int)
        or not 1 <= columns <= 8
        or not 1 <= rows <= 8
    ):
        raise ValueError("context_grid rows and columns must be integers from 1 to 8")
    west, south, east, north = (float(value) for value in bbox.split(","))
    longitude_step = (east - west) / columns
    latitude_step = (north - south) / rows
    return [
        (
            f"{west + column * longitude_step:.7f},"
            f"{south + row * latitude_step:.7f},"
            f"{west + (column + 1) * longitude_step:.7f},"
            f"{south + (row + 1) * latitude_step:.7f}"
        )
        for row in range(rows)
        for column in range(columns)
    ]


def _osm_entry(
    event: dict[str, Any], *, timeout: float, request_interval: float
) -> dict[str, Any]:
    event_id = str(event["id"])
    elements: dict[tuple[str, int], dict[str, Any]] = {}
    components: list[dict[str, Any]] = []
    component_root = OSM_ROOT / "components" / event_id
    for index, url in enumerate(_track_requests(event), start=1):
        payload, final_url = _fetch(url, timeout=timeout)
        parsed = json.loads(payload)
        _merge_elements(elements, parsed)
        components.append(
            {
                "role": "selected-track-object",
                **_component(
                    component_root / f"track-{index:03d}.raw.json.gz",
                    final_url,
                    payload,
                ),
            }
        )
        time.sleep(max(0.0, request_interval))
    _hydrate_way_geometry(elements)
    bbox, selected_way_ids = _context_bbox(event, elements, padding_m=650.0)
    context_elements: dict[tuple[str, int], dict[str, Any]] = {}
    context_bboxes = _context_tiles(event, bbox)
    context_urls: list[str] = []
    for index, context_bbox in enumerate(context_bboxes, start=1):
        context_url = (
            f"{OSM_API}/map.json?"
            f"{urllib.parse.urlencode({'bbox': context_bbox})}"
        )
        context_payload, context_final_url = _fetch(context_url, timeout=timeout)
        context_urls.append(context_final_url)
        _merge_elements(context_elements, json.loads(context_payload))
        context_name = (
            "context.raw.json.gz"
            if len(context_bboxes) == 1
            else f"context-{index:03d}.raw.json.gz"
        )
        components.append(
            {
                "role": (
                    "derived-bbox-context"
                    if len(context_bboxes) == 1
                    else "derived-bbox-context-tile"
                ),
                "context_bbox": context_bbox,
                **_component(
                    component_root / context_name,
                    context_final_url,
                    context_payload,
                ),
            }
        )
        if index < len(context_bboxes):
            time.sleep(max(0.0, request_interval))
    _hydrate_way_geometry(context_elements)
    # Context is visited first; exact selected objects win in the final union.
    combined = dict(context_elements)
    combined.update(elements)
    _hydrate_way_geometry(combined)
    type_order = {"node": 0, "way": 1, "relation": 2}
    selection = event["osm_selection"]
    selected_relation = (
        combined.get(("relation", int(selection["relation_id"])))
        if selection["mode"] == "relation"
        else None
    )
    selected_objects = []
    if selected_relation is not None:
        selected_objects.append(
            {
                "type": "relation",
                "id": int(selected_relation["id"]),
                "version": selected_relation.get("version"),
                "timestamp": selected_relation.get("timestamp"),
            }
        )
    for way_id in selected_way_ids:
        way = combined.get(("way", way_id))
        if way is None:
            continue
        selected_objects.append(
            {
                "type": "way",
                "id": way_id,
                "version": way.get("version"),
                "timestamp": way.get("timestamp"),
                "name": (way.get("tags") or {}).get("name"),
            }
        )
    snapshot = {
        "version": 0.6,
        "generator": "city-map-plotter-f1-legacy-source-builder/1.0",
        "copyright": (
            "The data included in this document is from www.openstreetmap.org "
            "and is made available under ODbL."
        ),
        "elements": [
            combined[key]
            for key in sorted(
                combined,
                key=lambda key: (type_order.get(key[0], 9), key[1]),
            )
        ],
        "_city_map_plotter": {
            "merge_recipe": (
                "exact-selected-api-objects-plus-derived-bbox-context-v1"
                if len(context_bboxes) == 1
                else "exact-selected-api-objects-plus-tiled-bbox-context-v2"
            ),
            "selection": selection,
            "selected_objects": selected_objects,
            "context_bbox": bbox,
            "context_bboxes": context_bboxes,
            "component_payload_sha256": [
                value["payload_sha256"] for value in components
            ],
        },
    }
    payload = _canonical_json(snapshot)
    path = OSM_ROOT / f"{event_id}-api-v1.json.gz"
    compressed = _write_gzip(path, payload)
    return _source_entry(
        source_id=f"osm-circuit-context-{event_id}",
        source_kind="osm-api-snapshot",
        publisher="OpenStreetMap contributors",
        title=f"OSM circuit and context snapshot: {event['circuit_name']}",
        url=context_urls[0],
        path=path,
        media_type="application/json",
        retrieved_at=datetime.now(UTC).isoformat(),
        payload=payload,
        compressed=compressed,
        licence="ODbL-1.0",
        commercial_use_status="conditional",
        allowed_uses=["geometry-derived-produced-work", "factual-transcription"],
        attribution="© OpenStreetMap contributors",
        extra={
            "event_id": event_id,
            "selection": selection,
            "selected_objects": selected_objects,
            "context_bbox": bbox,
            "context_bboxes": context_bboxes,
            "context_request_urls": context_urls,
            "element_count": len(snapshot["elements"]),
            "component_snapshots": components,
        },
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--event", action="append", default=[])
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--official-only", action="store_true")
    parser.add_argument("--osm-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--request-interval", type=float, default=0.25)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.official_only and args.osm_only:
        raise SystemExit("--official-only and --osm-only are mutually exclusive")
    if not args.all and not args.event:
        raise SystemExit("choose --all or at least one --event")
    registry_bytes = REGISTRY_PATH.read_bytes()
    registry = json.loads(registry_bytes)
    candidates = [
        event
        for event in registry["events"]
        if event.get("release_status") == "geometry-candidate"
    ]
    by_id = {str(event["id"]): event for event in candidates}
    requested = list(args.event)
    if requested:
        missing = sorted(set(requested) - set(by_id))
        if missing:
            raise SystemExit("unknown renderable event id(s): " + ", ".join(missing))
        candidates = [by_id[event_id] for event_id in requested]
    existing = _existing_sources()
    sources = dict(existing)
    errors: list[dict[str, str]] = []

    if not args.osm_only:
        for source in registry["official_sources"]:
            source_id = str(source["id"])
            if not args.refresh and _reusable(existing.get(source_id)):
                print(f"reuse official {source_id}")
                continue
            try:
                entry = _official_entry(source, timeout=args.timeout)
                sources[source_id] = entry
                print(f"official {source_id}: {entry['payload_sha256']}")
            except Exception as exc:
                errors.append(
                    {
                        "source_id": source_id,
                        "stage": "official",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    if not args.official_only:
        for event in candidates:
            source_id = f"osm-circuit-context-{event['id']}"
            if not args.refresh and _reusable(existing.get(source_id)):
                print(f"reuse osm {event['id']}")
                continue
            try:
                entry = _osm_entry(
                    event,
                    timeout=args.timeout,
                    request_interval=args.request_interval,
                )
                sources[source_id] = entry
                print(
                    f"osm {event['id']}: {entry['element_count']} elements / "
                    f"{entry['payload_sha256']}"
                )
            except Exception as exc:
                errors.append(
                    {
                        "source_id": source_id,
                        "stage": "osm",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    manifest = {
        "schema_version": 1,
        "contract_id": "f1-circuits-legacy-v1-sources",
        "catalog_class": "legacy-f1-configurations",
        "season_scope": "multi-era",
        "season": 2026,
        "generated_at": datetime.now(UTC).isoformat(),
        "network_fallback_for_rendering": False,
        "freeze": {
            "frozen_at": registry["frozen_at"],
            "event_registry_path": _relative(REGISTRY_PATH),
            "event_registry_sha256": _sha256(registry_bytes),
            "candidate_count": len(by_id),
            "hold_count": sum(
                event.get("release_status") == "hold"
                for event in registry["events"]
            ),
        },
        "sources": sorted(sources.values(), key=lambda value: value["id"]),
        "acquisition_errors": errors,
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_bytes(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )
    print(
        f"wrote {MANIFEST_PATH}: {len(manifest['sources'])} sources, "
        f"{len(errors)} errors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
