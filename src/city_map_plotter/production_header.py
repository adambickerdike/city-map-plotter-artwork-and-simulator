"""Refresh released city headers using the canonical furniture compositor.

The verifier is stdlib-only so the artwork repository can audit the actual SVG
without installing the map acquisition/rendering stack.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

from .production_copy import INKSCAPE, geometry_digest, local_name

POLICY_ID = "city-header-left-stack-v1"
CITY_LAYOUTS = {"city-map", "university-memorabilia"}
HEADER_IDS = frozenset({"layer-poster_title", "layer-poster_coordinates", "layer-poster_compass"})
NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def is_city_map(manifest: dict) -> bool:
    return manifest.get("rendering", {}).get("poster_layout") in CITY_LAYOUTS


def _bounds(group: ET.Element) -> tuple[float, float, float, float]:
    """Bounds of baked, absolute straight-line header paths; fail on other SVG."""
    points = []
    for element in group.iter():
        if element.get("transform"):
            raise ValueError("Header geometry must be baked into page coordinates")
        if local_name(element) != "path":
            continue
        data = element.get("d", "")
        if re.sub(r"[MLZ\s,]", "", NUMBER.sub("", data)):
            raise ValueError("Unexpected city header path command")
        values = [float(x) for x in NUMBER.findall(data)]
        if len(values) % 2 or not all(math.isfinite(x) for x in values):
            raise ValueError("Invalid city header coordinates")
        points.extend(zip(values[::2], values[1::2]))
    if not points:
        raise ValueError("City header is missing plotted paths")
    return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)


def verify_city_header(root: ET.Element, manifest: dict) -> dict:
    """Check physical placement, not just a declared alignment in metadata."""
    selected = [g for g in root.iter() if g.get("id") in HEADER_IDS]
    groups = {g.get("id"): g for g in root if g.get("id") in HEADER_IDS}
    if len(selected) != len(HEADER_IDS) or set(groups) != HEADER_IDS:
        raise ValueError("City map must contain one title, coordinate and compass layer")
    title, coordinates, compass = [groups[f"layer-poster_{name}"] for name in ("title", "coordinates", "compass")]
    bounds = [_bounds(g) for g in (title, coordinates, compass)]
    t, c, n = bounds
    nibs = [float(g.attrib["data-plot-nib-mm"]) for g in (title, coordinates, compass)]
    if abs(t[0] - c[0]) > 0.003 or c[1] - nibs[1] / 2 <= t[3] + nibs[0] / 2:
        raise ValueError("City coordinates must sit beneath the title on its left edge")
    if n[0] - nibs[2] / 2 <= max(t[2] + nibs[0] / 2, c[2] + nibs[1] / 2):
        raise ValueError("City compass overlaps the title/coordinate column")
    header_centre = (t[1] - nibs[0] / 2 + c[3] + nibs[1] / 2) / 2
    if abs((n[1] + n[3]) / 2 - header_centre) > 0.003:
        raise ValueError("City compass must be centred beside the title and coordinates")
    zones = manifest["page"]["zones_mm"]
    for name, box, nib in zip(("title", "coordinates", "compass"), bounds, nibs):
        zone = zones[f"city_{name}"]
        # Title/coordinate zones historically describe centreline extents.
        # Compass includes the complete ink envelope in its dedicated column.
        inset = nib / 2 if name == "compass" else 0
        if (box[0] - inset < zone["x"] - 0.003 or box[1] - inset < zone["y"] - 0.003
                or box[2] + inset > zone["x"] + zone["width"] + 0.003
                or box[3] + inset > zone["y"] + zone["height"] + 0.003):
            raise ValueError(f"City {name} leaves its binding header zone")
    map_top = manifest["page"]["map_bounds_mm"]["y"]
    if max(c[3] + nibs[1] / 2, n[3] + nibs[2] / 2) >= map_top:
        raise ValueError("City header touches the map field")
    return {"status": "passed", "policy_id": POLICY_ID,
            "bounds_mm": {name: list(b) for name, b in zip(("title", "coordinates", "compass"), bounds)},
            "compass_vertical_error_mm": round(abs((n[1] + n[3]) / 2 - header_centre), 4)}


def refresh_city_header(root: ET.Element, manifest: dict) -> dict | None:
    if not is_city_map(manifest):
        return None
    from .furniture import append_memorabilia_head
    from .geometry import load_plate_format, make_poster_layout
    from .models import BoundingBox
    from .pens import BUILTIN_PEN_INVENTORIES

    before = geometry_digest(root, exclude_group_ids=HEADER_IDS)
    rendering, page = manifest["rendering"], manifest["page"]
    format_id = page.get("format_id", page["paper"].lower() + "-" + page["orientation"])
    plate = load_plate_format(format_id)
    layout = make_poster_layout(
        BoundingBox(**manifest["extent_wgs84"]), format_id=format_id,
        preset=rendering["preset"], poster_layout=rendering["poster_layout"],
    )
    inventory = BUILTIN_PEN_INVENTORIES[rendering["pen_profile"]]
    if any(p.get("calibration_state") != "nominal-unmeasured" for p in rendering["pen_inventory"]["pens"]):
        raise ValueError("Header rebuild requires the original calibrated inventory")
    header_contract = dict(plate["city_header"])
    variant = manifest.get("memorabilia", {}).get("variant", "standard")
    if variant != "standard":
        header_contract.update(plate["memorabilia_variants"][variant]["header"])
    generated = ET.Element(root.tag)
    layers: list[dict] = []
    append_memorabilia_head(
        generated, layout, title=manifest["title"], subtitle=None,
        layer_stats=layers, pen_inventory=inventory,
        allowed_nibs_mm=tuple(rendering["allowed_nominal_nibs_mm"]),
        zone_names={name: f"city_{name}" for name in ("title", "coordinates", "compass")},
        wrap_title=True, header_contract=header_contract,
    )
    old_groups = {g.get("id"): g for g in root if g.get("id") in HEADER_IDS}
    if set(old_groups) != HEADER_IDS:
        raise ValueError("Cannot identify all original city header layers")
    old_copy = {key: g.get("data-copy") for key, g in old_groups.items()}
    for group in generated:
        old = old_groups[group.attrib["id"]]
        if group.get("data-plot-pen-id") != old.get("data-plot-pen-id"):
            raise ValueError("City header unexpectedly changes the physical pen")
        for attr in ("data-pen-step", f"{{{INKSCAPE}}}label"):
            if attr in old.attrib:
                group.set(attr, old.attrib[attr])
        index = list(root).index(old)
        root.remove(old)
        root.insert(index, group)
        stats = next(x for x in layers if x["svg_group_id"] == group.get("id"))
        previous = next(x for x in manifest["layers"] if x["svg_group_id"] == group.get("id"))
        stats["svg_layer_label"] = group.get(f"{{{INKSCAPE}}}label")
        if "pen_step" in previous:
            stats["pen_step"] = previous["pen_step"]
        previous.update(stats)
    if before != geometry_digest(root, exclude_group_ids=HEADER_IDS):
        raise ValueError("City header rebuild changed other artwork geometry")
    previous_zones = {k: v for k, v in page["zones_mm"].items() if k.startswith("city_") and k != "city_map_field"}
    page["zones_mm"].update({k: v for k, v in plate["city_zones_mm"].items() if k != "city_map_field"})
    coordinates = next(g.get("data-copy") for g in generated if g.get("id") == "layer-poster_coordinates")
    copy_record = manifest.get("city_map", manifest.get("memorabilia"))
    copy_record["coordinates"] = coordinates
    if "visible_copy" in copy_record:
        copy_record["visible_copy"] = [manifest["title"], coordinates, "N"]
    report = {**verify_city_header(root, manifest), "non_header_geometry_unchanged": True,
              "non_header_geometry_sha256": before, "original_visible_copy": old_copy,
              "original_header_zones_mm": previous_zones}
    manifest["header_layout"] = report
    return report
