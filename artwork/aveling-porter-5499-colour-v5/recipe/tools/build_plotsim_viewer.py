#!/usr/bin/env python3
"""Build a self-contained animated plot-simulator page for one or more plates.

    python3 tools/build_plotsim_viewer.py a.svg b.svg --out build/plotsim/index.html

Every plate is simulated twice -- in the order the SVG writes its layers, and
with pens merged and strokes travel-ordered -- and both are inlined so the page
can switch between them and show the saving. Multiple plates get a dropdown.

Payload notes. Naively this is enormous: a 6 MB plate has ~64k vertices and two
orderings. Three things keep it manageable:
  * geometry is stored ONCE and each ordering is only a permutation plus a
    reversal flag, because reordering never changes the strokes themselves;
  * pen-up travel is implicit -- it is always the straight line from the end of
    one stroke to the start of the next, so none of it is stored;
  * display geometry is simplified at --display-tolerance (default 0.08 mm,
    well under any nib) while TIMINGS come from the full-resolution geometry.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from html import escape
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from plotsim import (  # noqa: E402
    Machine,
    NON_SIMULATABLE_SVG_CODES,
    _length,
    load_plate,
    order_strokes,
    plan_polyline,
    preflight_svg,
    simulate,
)

TEMPLATE = Path(__file__).resolve().parent / "plotsim_viewer.tmpl"


def _json_for_html(value: Any) -> str:
    """Serialize JSON without permitting an embedded SVG title to end a script."""

    return (
        json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _simplify_indices(points: list[tuple[float, float]], tol: float) -> list[int]:
    """Indices retained by iterative Douglas-Peucker display simplification."""

    if tol <= 0 or len(points) <= 2:
        return list(range(len(points)))
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi <= lo + 1:
            continue
        ax, ay = points[lo]
        bx, by = points[hi]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, best_i = -1.0, lo
        for i in range(lo + 1, hi):
            px, py = points[i]
            if norm < 1e-12:
                dist = math.hypot(px - ax, py - ay)
            else:
                position = max(
                    0.0,
                    min(1.0, ((px - ax) * dx + (py - ay) * dy) / (norm * norm)),
                )
                dist = math.hypot(
                    px - (ax + position * dx),
                    py - (ay + position * dy),
                )
            if dist > best:
                best, best_i = dist, i
        if best > tol:
            keep.add(best_i)
            stack.append((lo, best_i))
            stack.append((best_i, hi))
    return sorted(keep)


def _encode_plate(
    svg: Path, machine: Machine, tol: float, *, strict_svg: bool = False
) -> dict:
    if not math.isfinite(tol) or tol < 0:
        raise ValueError("display tolerance must be finite and non-negative")
    preflight = preflight_svg(svg, machine)
    if strict_svg and preflight.errors:
        summary = "; ".join(
            f"{issue.code}: {issue.message}" for issue in preflight.errors[:5]
        )
        raise ValueError(f"strict SVG preflight failed for {svg}: {summary}")
    non_simulatable = [
        issue for issue in preflight.errors if issue.code in NON_SIMULATABLE_SVG_CODES
    ]
    if non_simulatable:
        raise ValueError(
            f"SVG has no safe millimetre simulation for {svg}: "
            + "; ".join(f"{issue.code}: {issue.message}" for issue in non_simulatable)
        )
    try:
        strokes, page = load_plate(svg, machine)
    except ValueError as exc:
        raise ValueError(
            f"SVG path geometry could not be compiled for {svg}: {exc}"
        ) from exc
    if not strokes:
        raise SystemExit(f"no plottable strokes in {svg}")

    # --- shared geometry, simplified for display -------------------------
    pens: list[dict] = []
    pen_index: dict[str, int] = {}
    offsets = [0]
    flat: list[float] = []
    curve_ms: list[float] = []
    raw_vertices: list[int] = []
    stroke_motion_s: list[float] = []
    stroke_pen: list[int] = []
    for stroke in strokes:
        if stroke.pen.id not in pen_index:
            pen_index[stroke.pen.id] = len(pens)
            pens.append(
                {
                    "pen": stroke.pen.label,
                    "id": stroke.pen.id,
                    "ink": stroke.pen.ink,
                    "colour": stroke.pen.colour,
                    # The width the pen actually marks -- what the viewer draws
                    # at true scale -- kept separate from the barrel size.
                    "nib_mm": stroke.pen.nib_mm,
                    "nominal_nib_mm": stroke.pen.nominal_mm,
                    "measured": stroke.pen.measured,
                }
            )
        retained = _simplify_indices(stroke.points, tol)
        full_timing = plan_polyline(
            stroke.points,
            machine.pen_down_speed_mm_s,
            machine.acceleration_mm_s2,
            machine.cornering_tolerance_mm,
            grbl_cartesian=machine.motion_model == "grbl-cartesian",
        )
        full_timing = [value * machine.timing_scale for value in full_timing]
        for point_index in retained:
            x, y = stroke.points[point_index]
            flat.append(round(x, 2))
            flat.append(round(y, 2))
            # Keep the vertex clock at the same microsecond resolution as the
            # enclosing move. Integer milliseconds can round a stroke up past
            # its own timeline entry and leave a one-frame visual overrun.
            curve_ms.append(round(full_timing[point_index] * 1000, 3))
        offsets.append(len(flat) // 2)
        raw_vertices.append(len(stroke.points))
        stroke_motion_s.append(full_timing[-1])
        stroke_pen.append(pen_index[stroke.pen.id])

    plate = {
        "name": svg.stem,
        "title": page["title"],
        "page": {
            "width": page["width"],
            "height": page["height"],
            "title": page["title"],
            "metadata": page.get("metadata", False),
        },
        "preflight": preflight.as_dict(),
        "layers": [
            {
                "label": layer["label"],
                "pen": pen_index[layer["pen"].id],
                "nib_mm": layer["pen"].nib_mm,
                "width_mm": layer["declared"]["width_mm"] or layer["pen"].nib_mm,
                "strokes": layer["declared"]["strokes"],
                "passes": layer["declared"]["passes"],
                "pitch_mm": layer["declared"]["pitch_mm"],
                "mode": layer["declared"]["mode"],
                "subpaths": layer["subpaths"],
                "length_mm": round(layer["length_mm"], 1),
            }
            for layer in page.get("layers", [])
            if layer["pen"].id in pen_index
        ],
        "pens": pens,
        "geom": {
            "off": offsets,
            "pts": flat,
            "ct": curve_ms,
            "raw_n": raw_vertices,
            "pen": stroke_pen,
        },
        "orders": {},
    }

    # --- one entry per ordering: a permutation plus timings --------------
    for mode in ("document", "optimised"):
        groups = order_strokes(strokes, mode)
        _, stats = simulate(groups, machine)

        seq: list[int] = []
        rev: list[int] = []
        travel_ms: list[float] = []
        draw_ms: list[float] = []
        changes: list[dict] = []

        cursor = (0.0, 0.0)
        position = 0
        for group_index, (pen, items) in enumerate(groups):
            if not items:
                continue
            changes.append(
                {
                    "at": position,
                    "p": pen_index[pen.id],
                    "d": 0.0 if group_index == 0 else machine.pen_change_s,
                }
            )
            for stroke in items:
                gap = (
                    plan_polyline(
                        [cursor, stroke.start],
                        machine.pen_up_speed_mm_s,
                        machine.acceleration_mm_s2,
                        machine.cornering_tolerance_mm,
                        grbl_cartesian=machine.motion_model == "grbl-cartesian",
                    )[-1]
                    * machine.timing_scale
                    + machine.command_latency_s
                    if math.dist(cursor, stroke.start) > 1e-9
                    else 0.0
                )
                span = stroke_motion_s[stroke.sid] + machine.command_latency_s
                seq.append(stroke.sid)
                rev.append(1 if stroke.rev else 0)
                # Microsecond precision prevents per-stroke millisecond
                # rounding from accumulating into visible drift on huge maps.
                travel_ms.append(round(gap * 1000, 3))
                draw_ms.append(round(span * 1000, 3))
                cursor = stroke.end
                position += 1

        plate["orders"][mode] = {
            "seq": seq,
            "rev": rev,
            "tm": travel_ms,
            "dm": draw_ms,
            "changes": changes,
            "stats": {
                "total_seconds": round(stats["total_seconds"], 1),
                "total_low_seconds": round(stats["total_low_seconds"], 1),
                "total_high_seconds": round(stats["total_high_seconds"], 1),
                "timing_uncertainty_fraction": stats["timing_uncertainty_fraction"],
                "calibration_state": stats["calibration_state"],
                "pen_down_mm": round(stats["pen_down_mm"], 1),
                "pen_up_mm": round(stats["pen_up_mm"], 1),
                "pen_lifts": stats["pen_lifts"],
                "pen_changes": stats["pen_changes"],
                "pen_loads": stats["pen_loads"],
                "pen_swaps": stats["pen_swaps"],
                "kinematic_seconds": round(stats["kinematic_seconds"], 1),
                "command_latency_seconds": round(stats["command_latency_seconds"], 1),
                "servo_seconds": round(stats["servo_seconds"], 1),
                "manual_change_seconds": round(stats["manual_change_seconds"], 1),
                "travel_ratio": round(stats["travel_ratio"], 3),
                "ink_mm2": round(stats["ink_mm2"], 1),
                "bounds": {
                    key: round(value, 3) for key, value in stats["bounds"].items()
                },
            },
        }
    return plate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("svg", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, default=Path("build/plotsim/index.html"))
    parser.add_argument("--display-tolerance", type=float, default=0.08)
    parser.add_argument("--machine-profile", type=Path)
    parser.add_argument("--pen-down-speed", type=float)
    parser.add_argument("--pen-up-speed", type=float)
    parser.add_argument("--acceleration", type=float)
    parser.add_argument("--pen-change-seconds", type=float)
    parser.add_argument("--pen-lift-seconds", type=float)
    parser.add_argument("--pen-lower-seconds", type=float)
    parser.add_argument("--cornering-tolerance", type=float)
    parser.add_argument("--curve-flatness", type=float)
    parser.add_argument("--timing-uncertainty", type=float)
    parser.add_argument("--strict-svg", action="store_true")
    args = parser.parse_args(argv)

    try:
        base = (
            Machine.from_json(args.machine_profile)
            if args.machine_profile
            else Machine()
        )
        machine_values = base.as_dict()
        overrides = {
            "pen_down_speed_mm_s": args.pen_down_speed,
            "pen_up_speed_mm_s": args.pen_up_speed,
            "acceleration_mm_s2": args.acceleration,
            "pen_change_s": args.pen_change_seconds,
            "pen_lift_s": args.pen_lift_seconds,
            "pen_lower_s": args.pen_lower_seconds,
            "cornering_tolerance_mm": args.cornering_tolerance,
            "curve_flatness_mm": args.curve_flatness,
            "timing_uncertainty_fraction": args.timing_uncertainty,
        }
        machine_values.update(
            {key: value for key, value in overrides.items() if value is not None}
        )
        machine = Machine(**machine_values)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    plates = []
    for path in args.svg:
        if not path.exists():
            print(f"  skipping missing {path}", file=sys.stderr)
            continue
        try:
            plate = _encode_plate(
                path,
                machine,
                args.display_tolerance,
                strict_svg=args.strict_svg,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        plates.append(plate)
        doc = plate["orders"]["document"]["stats"]
        opt = plate["orders"]["optimised"]["stats"]
        saved = doc["total_seconds"] - opt["total_seconds"]
        print(
            f"  {path.name:<46} {len(plate['geom']['pen']):>6} strokes  "
            f"{doc['total_seconds'] / 60:6.1f} -> {opt['total_seconds'] / 60:6.1f} min  "
            f"travel {doc['pen_up_mm'] / 1000:7.1f} -> {opt['pen_up_mm'] / 1000:5.1f} m  "
            f"(-{100 * saved / max(doc['total_seconds'], 1e-9):.0f}% time)"
        )

    if not plates:
        raise SystemExit("nothing to build")

    payload = {
        "machine": machine.as_dict(),
        "live": False,
        "plates": plates,
    }
    label = (
        f"{len(plates)} plates"
        if len(plates) > 1
        else (plates[0]["title"] or plates[0]["name"])
    )
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("__TITLE__", escape(str(label), quote=False))
    html = html.replace("__DATA__", _json_for_html(payload))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")
    print(
        f"\n  wrote {args.out}  ({args.out.stat().st_size / 1024 / 1024:.1f} MB, "
        f"{len(plates)} plate{'s' if len(plates) != 1 else ''})"
    )
    _ = _length
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
