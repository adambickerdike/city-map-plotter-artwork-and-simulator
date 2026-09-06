"""Customer copy for derivatives of the reviewed map editions.

The geographic paths are immutable inputs. Only explicitly identified furniture
is changed; source credits and the original copy travel in the manifest.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
import xml.etree.ElementTree as ET

SVG = "http://www.w3.org/2000/svg"
INKSCAPE = "http://www.inkscape.org/namespaces/inkscape"
POLICY_ID = "customer-map-copy-v1"
FORBIDDEN_COPY = re.compile(
    r"open\s*street\s*map|\bosm\b|\bodbl\b|open\s+map\s+data|"
    r"\bmapzen\b|\boverpass\b|\bnibs?\b|"
    r"\bpens?\s+(?:size|width|plan|setup|change|load|profile|inventory|instructions)\b|"
    r"\b\d+(?:\.\d+)?\s*mm\b|\b(?:draft|proof|preview)\b",
    re.IGNORECASE,
)
DRAWN = {"path", "polyline", "polygon", "line", "rect", "circle", "ellipse", "text", "tspan", "use"}
FURNITURE_ROLES = {
    "attribution", "detail", "circuit-information-label", "circuit-information-value",
}
MEASURED_ROUTE_COPY = re.compile(r"^MEASURED\s+[\d.]+\s+KM$", re.IGNORECASE)


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def visible_copies(root: ET.Element, manifest: dict | None = None) -> list[str]:
    """Read literal, path and inherited group copy, including older manifests."""
    copies: set[str] = set()

    def walk(element: ET.Element, inherited: str = "", hidden: bool = False) -> None:
        name = local_name(element)
        if name in {"metadata", "defs", "title", "desc"}:
            return
        style = element.get("style", "").replace(" ", "")
        hidden = hidden or element.get("display") == "none" or "display:none" in style
        if hidden:
            return
        copy = element.get("data-copy", inherited)
        if name in DRAWN and copy:
            copies.add(copy)
        if name in {"text", "tspan"}:
            copies.add("".join(element.itertext()).strip())
        for child in element:
            walk(child, copy, hidden)

    walk(root)
    if manifest:
        # Early niche lettering did not carry data-copy. Its role-bound copy is
        # declared here, so a missing path annotation cannot make the audit pass.
        copies.update(str(manifest.get(k) or "") for k in ("title", "subtitle"))
        copies.update(str(x) for x in manifest.get("details", []))
    return sorted(copies - {""})


def geometry_digest(
    root: ET.Element, *, exclude_furniture: bool = False,
    exclude_group_ids: frozenset[str] = frozenset(),
) -> str:
    """Hash a multiset of actual paths and inherited physical attributes."""
    records: Counter[str] = Counter()

    def walk(element: ET.Element, inherited: dict[str, str], furniture: bool) -> None:
        if element.get("id") in exclude_group_ids:
            return
        name = local_name(element)
        if name in {"metadata", "defs", "title", "desc"}:
            return
        attrs = dict(inherited)
        for key in ("stroke", "stroke-width", "transform", "data-plot-pen-id", "fill"):
            if key in element.attrib:
                attrs[key] = element.attrib[key]
        furniture = furniture or "data-detail-line" in element.attrib or element.get("data-role") in FURNITURE_ROLES or (
            element.get("data-logical-layer") == "plate_attribution"
        )
        if name in DRAWN and not (exclude_furniture and furniture):
            geometry = {k: v for k, v in element.attrib.items() if k in {
                "d", "points", "x", "y", "width", "height", "cx", "cy", "r", "rx", "ry",
                "x1", "y1", "x2", "y2", "transform", "stroke", "stroke-width", "fill",
            }}
            records[json.dumps([name, attrs, geometry], sort_keys=True)] += 1
        for child in element:
            walk(child, attrs, furniture)

    walk(root, {}, False)
    return hashlib.sha256(json.dumps(sorted(records.items())).encode()).hexdigest()


def _remove(root: ET.Element, predicate) -> int:
    removed = 0
    for parent in list(root.iter()):
        for child in list(parent):
            if predicate(child):
                removed += sum(local_name(x) in DRAWN for x in child.iter())
                parent.remove(child)
    return removed


def _detail_copy(manifest: dict) -> list[str] | None:
    domain = manifest.get("domain")
    if domain == "hikes":
        return list(manifest.get("details", []))[:1]
    if domain == "golf":
        count = manifest["rendering"]["course_hole_count"]
        context = manifest["catalog_record"].get("championship_context", "")
        return [f"{count} HOLES", *(x.strip() for x in context.split("/") if x.strip())][:3]
    return None


def _replace_details(root: ET.Element, manifest: dict, lines: list[str]) -> int:
    from .niche_common import context_for, text_strokes_fit
    from .svgkit import path_data

    target = next((x for x in root.iter() if x.get("id") == "logical-plate_copy"), None)
    if target is None:
        raise ValueError("Cannot identify the original detail layer")
    parents = {child: parent for parent in root.iter() for child in parent}
    pen_group = target
    while not pen_group.get("data-plot-pen-id"):
        pen_group = parents[pen_group]
    pen_id = pen_group.attrib["data-plot-pen-id"]
    context = context_for(manifest["page"]["format_id"])
    zone = context.zones["detail"]
    cap = float(context.plate["type_scale_mm"]["detail"])
    gap = 4 * float(pen_group.attrib["data-plot-nib-mm"]) if manifest["domain"] == "golf" else 0.9
    y = zone.y + max((zone.height - len(lines) * cap - max(len(lines) - 1, 0) * gap) / 2, 0)
    removed = _remove(root, lambda e: e.get("data-role") == "detail")
    for index, line in enumerate(lines):
        strokes = text_strokes_fit(
            line, x_mm=zone.centre[0], y_mm=y + index * (cap + gap),
            preferred_cap_mm=cap, maximum_width_mm=zone.width,
            pen_id=pen_id, anchor="middle", allow_horizontal_condense=manifest["domain"] == "hikes",
        )
        for points in strokes:
            ET.SubElement(target, f"{{{SVG}}}path", {
                "d": path_data(points), "data-role": "detail", "data-logical-layer": "plate_copy",
                "data-copy": line, "data-cap-height-mm": str(cap),
            })
    manifest["details"] = lines
    return removed


def _replace_circuit_copy(root: ET.Element) -> int:
    from .niche_common import text_strokes_fit
    from .svgkit import path_data

    replacements = {
        "COURSE DRAWING": "CIRCUIT PLAN",
        "SOURCE CENTRELINE": "CIRCUIT LAYOUT",
        "CURRENT MAP / GRANDSTANDS": "GRANDSTANDS",
        "CURRENT MAP / VENUE + GRANDSTANDS": "VENUE / GRANDSTANDS",
    }
    removed = 0
    for parent in list(root.iter()):
        for old, new in replacements.items():
            paths = [e for e in parent if e.get("data-copy") == old and local_name(e) == "path"]
            if not paths:
                continue
            parents = {c: p for p in root.iter() for c in p}
            group = parent
            while not group.get("data-plot-pen-id"):
                group = parents[group]
            coords = []
            for path in paths:
                numbers = [float(v) for v in re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", path.attrib["d"])]
                coords.extend(zip(numbers[::2], numbers[1::2]))
            x0, y0 = min(p[0] for p in coords), min(p[1] for p in coords)
            x1, y1 = max(p[0] for p in coords), max(p[1] for p in coords)
            attrs = {k: v for k, v in paths[0].attrib.items() if k != "d"}
            attrs["data-copy"] = new
            strokes = text_strokes_fit(
                new, x_mm=x0, y_mm=y0, preferred_cap_mm=y1-y0,
                maximum_width_mm=x1-x0, pen_id=group.attrib["data-plot-pen-id"],
                allow_horizontal_condense=True,
            )
            for path in paths:
                parent.remove(path)
                removed += 1
            for points in strokes:
                ET.SubElement(parent, f"{{{SVG}}}path", {**attrs, "d": path_data(points)})
    return removed


def clean_customer_copy(root: ET.Element, manifest: dict) -> dict:
    """Edit identified copy and fail if any other drawable geometry changes."""
    before = geometry_digest(root, exclude_furniture=True)
    original_copy = visible_copies(root, manifest)
    removed = _remove(root, lambda e: e.get("data-role") == "attribution" or e.get("data-logical-layer") == "plate_attribution")
    # A route measurement of sampled geometry is production evidence. Keep
    # the published race distance on the page and the measured value alongside
    # its route source in the manifest, rather than printing two distances.
    removed += _remove(root, lambda e: "data-detail-line" in e.attrib and bool(MEASURED_ROUTE_COPY.fullmatch(e.get("data-copy", ""))))
    manifest["details"] = [s for s in manifest.get("details", []) if not MEASURED_ROUTE_COPY.fullmatch(s)]
    details = _detail_copy(manifest)
    if details is not None:
        removed += _replace_details(root, manifest, details)
    removed += _replace_circuit_copy(root)
    after = geometry_digest(root, exclude_furniture=True)
    if before != after:
        raise ValueError("Customer copy transform changed non-furniture geometry")
    # Empty physical layers cannot be emitted to the plotter.
    for parent in list(root.iter()):
        for child in list(parent):
            if local_name(child) == "g" and not any(local_name(e) in DRAWN for e in child.iter()):
                parent.remove(child)
    copies = visible_copies(root, manifest)
    bad = [s for s in copies if FORBIDDEN_COPY.search(s)]
    if bad:
        raise ValueError(f"Forbidden customer copy: {bad}")
    manifest.setdefault("rendering", {}).update({
        "visible_attribution": False, "openstreetmap_attribution_mode": "external",
        "on_page_openstreetmap_reference": False,
        "external_openstreetmap_attribution_placement": "Accompanying ATTRIBUTION.md, product page and packaging",
    })
    report = {
        "policy_id": POLICY_ID, "non_furniture_geometry_sha256": after,
        "non_furniture_geometry_unchanged": True, "removed_furniture_paths": removed,
        "original_visible_copy": original_copy, "final_visible_copy": copies,
        "external_attribution": "ATTRIBUTION.md", "visible_copy_audit": "passed",
    }
    manifest["customer_copy"] = report
    return report
