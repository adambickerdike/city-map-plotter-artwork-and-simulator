"""Shared simulator/job/controller invariants for physical plotting."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from plotjob import (  # noqa: E402
    PROFILE_SCHEMA,
    ControllerProfile,
    GrblStreamer,
    PlotJobError,
    _digest,
    _fit_timing_profile,
    _job_digest_payload,
    compile_gcode_files,
    compile_plot_job,
    validate_job_for_execution,
    verify_plot_job,
    write_gcode_files,
    write_plot_job,
)
from build_plotsim_viewer import (  # noqa: E402
    _encode_plate,
    _json_for_html,
    _simplify_indices,
)
from plotsim import (  # noqa: E402
    Machine,
    Pen,
    Stroke,
    _nearest_neighbour_grid,
    flatten_path,
    load_plate,
    plan_polyline,
    preflight_svg,
)

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"


def _svg(*, measured: bool = True, extra: str = "") -> str:
    calibration = "measured" if measured else "nominal-unmeasured"
    return f"""<svg xmlns="{SVG_NS}" xmlns:inkscape="{INKSCAPE_NS}"
      width="148mm" height="210mm" viewBox="0 0 148 210">
      <title>Controller fixture</title>
      <g inkscape:groupmode="layer" inkscape:label="01 — Black 0.25"
         stroke="#111111" fill="none" data-plot-ink="Black"
         data-plot-nib-mm="0.25" data-plot-nominal-nib-mm="0.25"
         data-plot-strokes="1" data-plot-passes="1" data-plot-width-mm="0.25"
         data-plot-pen-id="black-0-25" data-plot-calibration-state="{calibration}">
        <path id="one" d="M 10 10 L 60 10 L 60 30"/>
      </g>
      <g inkscape:groupmode="layer" inkscape:label="02 — Red 0.4"
         stroke="#c83232" fill="none" data-plot-ink="Red"
         data-plot-nib-mm="0.4" data-plot-nominal-nib-mm="0.4"
         data-plot-strokes="1" data-plot-passes="1" data-plot-width-mm="0.4"
         data-plot-pen-id="red-0-4" data-plot-calibration-state="{calibration}">
        <path id="two" d="M 100 100 C 110 100 110 120 120 120"/>
      </g>
      {extra}
    </svg>"""


def _machine() -> Machine:
    return Machine(
        name="Measured fixture",
        motion_model="grbl-cartesian",
        pen_down_speed_mm_s=25.0,
        pen_up_speed_mm_s=60.0,
        acceleration_mm_s2=400.0,
        pen_lift_s=0.1,
        pen_lower_s=0.12,
        pen_change_s=3.0,
        timing_uncertainty_fraction=0.04,
        calibration_state="timing-measured",
        work_width_mm=220.0,
        work_height_mm=160.0,
        allow_page_rotation=True,
    )


def _controller() -> ControllerProfile:
    return ControllerProfile.from_mapping(
        {
            "schema": PROFILE_SCHEMA,
            "id": "fixture-grbl",
            "name": "Fixture GRBL",
            "calibration_state": "hardware-verified",
            "work_area_mm": {"width": 220, "height": 160},
            "motion": _machine().as_dict(),
            "controller": {
                "driver": "grbl",
                "execution_enabled": True,
                "serial_baud": 115200,
                "origin_mm": {"x": 0, "y": 0},
                "invert_x": False,
                "invert_y": False,
                "page_rotation": "auto",
                "coordinate_system": "G54",
                "expected_work_offset_mm": {"x": 0, "y": 0, "z": 0},
                "header_commands": ["G17"],
                "pen_up_commands": ["M3 S0"],
                "pen_down_commands": ["M3 S1000"],
                "footer_commands": ["M5"],
                "home_command": "$H",
                "unlock_command": "$X",
                "return_home": False,
                "expected_settings": {
                    "$3": 0,
                    "$11": 0.05,
                    "$30": 1000,
                    "$31": 0,
                    "$32": 0,
                    "$100": 80,
                    "$101": 80,
                    "$110": 3600,
                    "$111": 3600,
                    "$120": 400,
                    "$121": 400,
                    "$130": 220,
                    "$131": 160,
                },
            },
        }
    )


def test_preflight_accepts_native_plot_paths_and_rejects_unbaked_shapes(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid.svg"
    valid.write_text(_svg(), encoding="utf-8")
    report = preflight_svg(valid, _machine())
    assert report.hardware_safe
    assert not report.errors
    assert report.layer_count == 2
    assert report.path_count == 2

    invalid = tmp_path / "invalid.svg"
    invalid.write_text(
        _svg(extra='<rect id="box" x="1" y="1" width="5" height="5"/>'),
        encoding="utf-8",
    )
    bad_report = preflight_svg(invalid, _machine())
    assert {issue.code for issue in bad_report.errors} == {"unsupported-drawable"}


def test_preflight_rejects_paths_outside_layers_but_accepts_exponents(
    tmp_path: Path,
) -> None:
    exponent = tmp_path / "exponent.svg"
    exponent.write_text(
        _svg().replace("M 10 10 L 60 10", "M 1e1 1e1 L 6e1 1e1"),
        encoding="utf-8",
    )
    assert preflight_svg(exponent, _machine()).hardware_safe

    outside = tmp_path / "outside.svg"
    outside.write_text(
        _svg(extra='<path id="outside" stroke="#000" d="M 1 1 L 2 2"/>'),
        encoding="utf-8",
    )
    report = preflight_svg(outside, _machine())
    assert "path-outside-physical-layer" in {issue.code for issue in report.errors}
    assert report.path_count == 2


def test_preflight_rejects_ambiguous_page_units_and_malformed_path_data(
    tmp_path: Path,
) -> None:
    unitless = tmp_path / "unitless.svg"
    unitless.write_text(
        _svg().replace('width="148mm"', 'width="148"'), encoding="utf-8"
    )
    assert "invalid-page-size" in {
        issue.code for issue in preflight_svg(unitless, _machine()).errors
    }
    with pytest.raises(ValueError, match="no safe millimetre simulation"):
        _encode_plate(unitless, _machine(), 0.08, strict_svg=False)

    malformed = tmp_path / "malformed.svg"
    malformed.write_text(
        _svg().replace("M 10 10 L 60 10 L 60 30", "M 10 10 L 60"),
        encoding="utf-8",
    )
    assert "invalid-path-data" in {
        issue.code for issue in preflight_svg(malformed, _machine()).errors
    }


def test_preflight_requires_valid_consistent_physical_pen_metadata(
    tmp_path: Path,
) -> None:
    incomplete = tmp_path / "incomplete.svg"
    incomplete.write_text(
        _svg().replace(' data-plot-ink="Black"', "", 1), encoding="utf-8"
    )
    assert "incomplete-physical-metadata" in {
        issue.code for issue in preflight_svg(incomplete, _machine()).errors
    }

    invalid = tmp_path / "invalid-nib.svg"
    invalid.write_text(
        _svg().replace('data-plot-nib-mm="0.25"', 'data-plot-nib-mm="nan"', 1),
        encoding="utf-8",
    )
    assert "invalid-pen-metadata" in {
        issue.code for issue in preflight_svg(invalid, _machine()).errors
    }

    conflicting = tmp_path / "conflicting.svg"
    conflicting.write_text(
        _svg().replace('data-plot-pen-id="red-0-4"', 'data-plot-pen-id="black-0-25"'),
        encoding="utf-8",
    )
    assert "inconsistent-pen-id" in {
        issue.code for issue in preflight_svg(conflicting, _machine()).errors
    }


def test_unsafe_xml_is_never_parsed_even_in_diagnostic_mode(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.svg"
    unsafe.write_text(
        '<!DOCTYPE svg [<!ENTITY x "expanded">]><svg>&x;</svg>',
        encoding="utf-8",
    )
    report = preflight_svg(unsafe, _machine())
    assert {issue.code for issue in report.errors} == {"unsafe-xml-declaration"}
    with pytest.raises(PlotJobError, match="no safe millimetre simulation"):
        compile_plot_job(unsafe, _machine(), strict_svg=False)
    with pytest.raises(ValueError, match="no safe millimetre simulation"):
        _encode_plate(unsafe, _machine(), 0.08, strict_svg=False)


def test_preflight_blocks_unplottable_fill_and_ignored_svg_elements(
    tmp_path: Path,
) -> None:
    filled = tmp_path / "filled.svg"
    filled.write_text(_svg().replace('fill="none"', 'fill="#f00"', 1), encoding="utf-8")
    assert "fill-is-not-plotted" in {
        issue.code for issue in preflight_svg(filled, _machine()).errors
    }

    styled = tmp_path / "styled.svg"
    styled.write_text(
        _svg(extra="<style>.foo { stroke: red; }</style>"), encoding="utf-8"
    )
    assert "unknown-svg-element" in {
        issue.code for issue in preflight_svg(styled, _machine()).errors
    }


@pytest.mark.parametrize(
    ("style", "code"),
    [
        ("transform:translate(1px, 1px)", "unbaked-transform"),
        ("clip-path:url(#cut)", "unbaked-visual-effect"),
        ("stroke-dasharray:1 1", "unbaked-stroke-decoration"),
        ("marker-end:url(#arrow)", "unbaked-stroke-decoration"),
        ("stroke-opacity:0.5", "partial-opacity"),
    ],
)
def test_preflight_rejects_unmodelled_inline_visual_styles(
    tmp_path: Path, style: str, code: str
) -> None:
    svg = tmp_path / "styled.svg"
    svg.write_text(
        _svg().replace('<path id="one"', f'<path style="{style}" id="one"'),
        encoding="utf-8",
    )
    assert code in {issue.code for issue in preflight_svg(svg, _machine()).errors}


def test_inherited_hidden_group_is_not_sent_to_the_motion_plan(tmp_path: Path) -> None:
    hidden = tmp_path / "hidden.svg"
    hidden_path = '<g style="display:none"><path id="hidden" d="M 1 1 L 2 2"/></g>'
    hidden.write_text(_svg().replace("</g>", hidden_path + "</g>", 1), encoding="utf-8")
    strokes, _page = load_plate(hidden, _machine())
    assert len(strokes) == 2

    definitions = tmp_path / "definitions.svg"
    defined_path = '<defs><path id="defined-only" d="M 1 1 L 2 2"/></defs>'
    definitions.write_text(
        _svg().replace("</g>", defined_path + "</g>", 1), encoding="utf-8"
    )
    report = preflight_svg(definitions, _machine())
    assert report.path_count == 2
    strokes, _page = load_plate(definitions, _machine())
    assert len(strokes) == 2


def test_embedded_viewer_json_cannot_close_its_script_element() -> None:
    value = {"title": "</script><script>globalThis.injected=true</script> & map"}
    encoded = _json_for_html(value)
    assert "</script>" not in encoded.casefold()
    assert json.loads(encoded) == value


def test_viewer_embeds_full_plan_vertex_timing_for_realistic_animation(
    tmp_path: Path,
) -> None:
    svg = tmp_path / "fixture.svg"
    svg.write_text(_svg(), encoding="utf-8")
    plate = _encode_plate(svg, _machine(), 0.08, strict_svg=True)
    geometry = plate["geom"]
    assert len(geometry["ct"]) == len(geometry["pts"]) // 2
    assert geometry["raw_n"][0] == 3
    for stroke_id in range(len(geometry["pen"])):
        start, end = geometry["off"][stroke_id : stroke_id + 2]
        times = geometry["ct"][start:end]
        assert times[0] == 0
        assert times == sorted(times)
    order = plate["orders"]["optimised"]
    for position, stroke_id in enumerate(order["seq"]):
        motion_ms = geometry["ct"][geometry["off"][stroke_id + 1] - 1]
        assert order["dm"][position] >= motion_ms
    timeline_seconds = (
        (sum(order["tm"]) + sum(order["dm"])) / 1000
        + len(order["seq"]) * (_machine().pen_lower_s + _machine().pen_lift_s)
        + len(order["changes"]) * _machine().pen_lift_s
        + sum(change["d"] for change in order["changes"])
    )
    assert timeline_seconds == pytest.approx(order["stats"]["total_seconds"], abs=0.051)


def test_display_simplification_preserves_collinear_backtracking() -> None:
    points = [(0.0, 0.0), (2.0, 0.0), (1.0, 0.0)]
    assert _simplify_indices(points, 0.08) == [0, 1, 2]


def test_cubic_flattening_honours_the_physical_tolerance() -> None:
    tolerance = 0.02
    points = flatten_path("M 0 0 C 0 200 200 200 200 0", tolerance)[0]
    assert len(points) > 65  # The former fixed 64-segment ceiling was insufficient.

    def segment_distance(
        point: tuple[float, float],
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> float:
        dx, dy = end[0] - start[0], end[1] - start[1]
        length_squared = dx * dx + dy * dy
        position = max(
            0.0,
            min(
                1.0,
                ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy)
                / length_squared,
            ),
        )
        return math.dist(
            point,
            (start[0] + position * dx, start[1] + position * dy),
        )

    maximum_error = 0.0
    for index in range(1001):
        t = index / 1000
        u = 1 - t
        sample = (
            600 * u * t * t + 200 * t * t * t,
            600 * u * u * t + 600 * u * t * t,
        )
        maximum_error = max(
            maximum_error,
            min(
                segment_distance(sample, points[i], points[i + 1])
                for i in range(len(points) - 1)
            ),
        )
    assert maximum_error <= tolerance


def test_motion_planner_treats_shallow_turns_and_grbl_axes_physically() -> None:
    shallow = plan_polyline([(0.0, 0.0), (20.0, 0.0), (40.0, 2.0)], 40.0, 400.0, 0.05)[
        -1
    ]
    sharp = plan_polyline([(0.0, 0.0), (20.0, 0.0), (20.0, 20.0)], 40.0, 400.0, 0.05)[
        -1
    ]
    reversal = plan_polyline([(0.0, 0.0), (20.0, 0.0), (0.0, 0.0)], 40.0, 400.0, 0.05)[
        -1
    ]
    assert shallow < sharp < reversal

    diagonal = [(0.0, 0.0), (20.0, 20.0)]
    isotropic = plan_polyline(diagonal, 100.0, 400.0, 0.05)[-1]
    grbl = plan_polyline(
        diagonal,
        100.0,
        400.0,
        0.05,
        grbl_cartesian=True,
    )[-1]
    assert grbl < isotropic


def test_job_is_deterministic_hash_bound_and_reports_realistic_range(
    tmp_path: Path,
) -> None:
    svg = tmp_path / "fixture.svg"
    svg.write_text(_svg(), encoding="utf-8")
    first = compile_plot_job(svg, _machine())
    second = compile_plot_job(svg, _machine())
    assert first == second
    assert first["safety"]["execution_allowed"]
    assert first["stats"]["total_low_seconds"] < first["stats"]["total_seconds"]
    assert first["stats"]["total_high_seconds"] > first["stats"]["total_seconds"]
    assert first["stats"]["pen_loads"] == 2
    assert first["stats"]["pen_swaps"] == 1
    assert first["stats"]["total_seconds"] == pytest.approx(
        first["stats"]["kinematic_seconds"]
        + first["stats"]["command_latency_seconds"]
        + first["stats"]["servo_seconds"]
        + first["stats"]["manual_change_seconds"],
        abs=1e-5,
    )
    assert first["geometry"]["stroke_count"] == 2
    verify_plot_job(first)

    tampered = json.loads(json.dumps(first))
    tampered["pen_groups"][0]["strokes"][0]["points_mm"][1][0] += 1
    with pytest.raises(PlotJobError, match="digest mismatch"):
        verify_plot_job(tampered)

    internally_inconsistent = json.loads(json.dumps(first))
    internally_inconsistent["stats"]["total_seconds"] += 1
    internally_inconsistent["job_sha256"] = _digest(
        _job_digest_payload(internally_inconsistent)
    )
    with pytest.raises(PlotJobError, match="statistics disagree"):
        verify_plot_job(internally_inconsistent)


def test_review_only_pen_calibration_blocks_execution(tmp_path: Path) -> None:
    svg = tmp_path / "review.svg"
    svg.write_text(_svg(measured=False), encoding="utf-8")
    job = compile_plot_job(svg, _machine())
    assert not job["safety"]["execution_allowed"]
    codes = {finding["code"] for finding in job["safety"]["findings"]}
    assert "unmeasured-pens" in codes
    with pytest.raises(PlotJobError, match="review-only"):
        validate_job_for_execution(job)
    validate_job_for_execution(job, allow_review_output=True)


def test_gcode_export_enforces_review_and_structural_safety_gates(
    tmp_path: Path,
) -> None:
    controller = _controller()
    review_svg = tmp_path / "review.svg"
    review_svg.write_text(_svg(measured=False), encoding="utf-8")
    review_job = compile_plot_job(
        review_svg,
        _machine(),
        profile_binding=controller.binding(),
    )
    with pytest.raises(PlotJobError, match="review-only"):
        compile_gcode_files(review_job, controller)
    assert compile_gcode_files(review_job, controller, allow_review_output=True)
    assert compile_gcode_files(review_job, controller, bounds_only=True)

    unsafe_svg = tmp_path / "outside.svg"
    unsafe_svg.write_text(
        _svg().replace("M 10 10 L 60 10 L 60 30", "M 0 10 L 60 10 L 60 30"),
        encoding="utf-8",
    )
    unsafe_job = compile_plot_job(
        unsafe_svg,
        _machine(),
        profile_binding=controller.binding(),
    )
    assert "geometry-outside-page" in {
        finding["code"] for finding in unsafe_job["safety"]["findings"]
    }
    with pytest.raises(PlotJobError, match="non-bypassable"):
        compile_gcode_files(
            unsafe_job,
            controller,
            allow_review_output=True,
        )

    # The page-envelope check is independently derived from the points, even
    # if someone edits the safety record and calculates a new integrity hash.
    forged = json.loads(json.dumps(unsafe_job))
    forged["safety"]["findings"] = [
        finding
        for finding in forged["safety"]["findings"]
        if finding["code"] != "geometry-outside-page"
    ]
    forged["safety"]["execution_allowed"] = True
    forged["job_sha256"] = _digest(_job_digest_payload(forged))
    with pytest.raises(PlotJobError, match="non-bypassable"):
        compile_gcode_files(forged, controller, allow_review_output=True)


def test_review_override_never_bypasses_structural_svg_errors(tmp_path: Path) -> None:
    filled = tmp_path / "filled.svg"
    filled.write_text(_svg().replace('fill="none"', 'fill="#f00"', 1), encoding="utf-8")
    job = compile_plot_job(filled, _machine(), strict_svg=False)
    with pytest.raises(PlotJobError, match="non-bypassable"):
        validate_job_for_execution(job, allow_review_output=True)

    incomplete = tmp_path / "incomplete-offset.svg"
    incomplete.write_text(
        _svg().replace('data-plot-strokes="1"', 'data-plot-strokes="2"', 1),
        encoding="utf-8",
    )
    incomplete_job = compile_plot_job(incomplete, _machine())
    assert "declared-geometry-mismatch" in {
        finding["code"] for finding in incomplete_job["safety"]["findings"]
    }
    with pytest.raises(PlotJobError, match="non-bypassable"):
        validate_job_for_execution(incomplete_job, allow_review_output=True)


def test_gcode_uses_same_ordered_points_and_keeps_bounds_preview_pen_up(
    tmp_path: Path,
) -> None:
    svg = tmp_path / "fixture.svg"
    svg.write_text(_svg(), encoding="utf-8")
    controller = _controller()
    job = compile_plot_job(svg, _machine(), profile_binding=controller.binding())
    programs = compile_gcode_files(job, controller)
    assert list(programs) == ["01-black-0-25.gcode", "02-red-0-4.gcode"]
    first = programs["01-black-0-25.gcode"]
    assert f"; job_sha256={job['job_sha256']}" in first
    assert "; page_rotation=90" in first
    assert "G21\nG90\nG94\nG54\nG17" in first
    assert "M3 S1000" in first
    assert "F1500" in first  # 25 mm/s pen-down, converted to mm/min.
    assert "F3600" in first  # 60 mm/s pen-up, converted to mm/min.

    bounds = compile_gcode_files(job, controller, bounds_only=True)
    preview = bounds["00-bounds-preview.gcode"]
    assert "PEN MUST REMAIN UP" in preview
    assert "M3 S0" in preview
    assert "M3 S1000" not in preview
    assert "G17" not in preview
    assert "M5" not in preview


def test_hardware_export_requires_the_exact_profile_binding(tmp_path: Path) -> None:
    svg = tmp_path / "fixture.svg"
    svg.write_text(_svg(), encoding="utf-8")
    controller = _controller()
    unbound = compile_plot_job(svg, _machine())
    with pytest.raises(PlotJobError, match="simulation-only"):
        compile_gcode_files(unbound, controller)

    wrong_motion = compile_plot_job(
        svg,
        Machine.from_mapping({**_machine().as_dict(), "pen_down_speed_mm_s": 26.0}),
        profile_binding=controller.binding(),
    )
    with pytest.raises(PlotJobError, match="motion model disagrees"):
        compile_gcode_files(wrong_motion, controller)

    bound = compile_plot_job(svg, _machine(), profile_binding=controller.binding())
    changed_raw = json.loads(
        (ROOT / "plotter-profiles/grbl-servo-template-v1.json").read_text()
    )
    changed_raw["id"] = controller.id
    changed_raw["controller"].update(
        {
            "execution_enabled": True,
            "pen_up_commands": ["M3 S0"],
            "pen_down_commands": ["M3 S1000"],
            "footer_commands": ["M5"],
        }
    )
    changed = ControllerProfile.from_mapping(changed_raw)
    with pytest.raises(PlotJobError, match="does not match"):
        compile_gcode_files(bound, changed)

    output = tmp_path / "gcode"
    output.mkdir()
    (output / "stale.gcode").write_text("stale", encoding="ascii")
    with pytest.raises(PlotJobError, match="not empty"):
        write_gcode_files(output, {"01-current.gcode": "G21\n"})


def test_timing_calibration_scales_motor_time_but_not_fixed_delays(
    tmp_path: Path,
) -> None:
    raw = json.loads(
        (ROOT / "plotter-profiles/grbl-servo-template-v1.json").read_text()
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(raw), encoding="utf-8")
    controller = ControllerProfile.from_mapping(raw)
    svg = tmp_path / "fixture.svg"
    svg.write_text(_svg(), encoding="utf-8")
    job = compile_plot_job(
        svg,
        Machine.from_mapping(raw),
        profile_binding=controller.binding(),
    )
    job_path = tmp_path / "fixture.plotjob.json"
    write_plot_job(job_path, job)
    kinematic = float(job["stats"]["kinematic_seconds"])
    fixed = float(job["stats"]["total_seconds"]) - kinematic
    actual = fixed + 1.2 * kinematic

    output = tmp_path / "calibrated.json"
    _fit_timing_profile(profile_path, output, [f"{job_path}:{actual}"])
    calibrated = json.loads(output.read_text())
    assert calibrated["motion"]["timing_scale"] == pytest.approx(1.2)
    assert calibrated["motion"]["timing_uncertainty_fraction"] == 0.02
    observation = calibrated["timing_calibration"]["observations"][0]
    assert observation["fixed_seconds"] == pytest.approx(fixed, abs=1e-6)
    assert observation["motor_scale_ratio"] == pytest.approx(1.2)


def test_controller_profile_rejects_hidden_motion_and_loose_boolean_types() -> None:
    template = json.loads(
        (ROOT / "plotter-profiles/grbl-servo-template-v1.json").read_text()
    )
    template["controller"]["header_commands"] = ["G01X10Y10"]
    with pytest.raises(PlotJobError, match="hidden motion"):
        ControllerProfile.from_mapping(template)

    template["controller"]["header_commands"] = ["G91"]
    with pytest.raises(PlotJobError, match="hidden motion"):
        ControllerProfile.from_mapping(template)

    template["controller"]["header_commands"] = ["$H"]
    with pytest.raises(PlotJobError, match="hidden motion"):
        ControllerProfile.from_mapping(template)

    template["controller"]["header_commands"] = []
    template["controller"]["pen_up_commands"] = ["G1 Z5 F200"]
    with pytest.raises(PlotJobError, match="unmodelled motion"):
        ControllerProfile.from_mapping(template)

    template["controller"]["pen_up_commands"] = []
    template["controller"]["execution_enabled"] = "false"
    with pytest.raises(PlotJobError, match="must be true or false"):
        ControllerProfile.from_mapping(template)

    verified = json.loads(
        (ROOT / "plotter-profiles/grbl-servo-template-v1.json").read_text()
    )
    verified["calibration_state"] = "hardware-verified"
    verified["controller"]["execution_enabled"] = True
    verified["controller"]["pen_up_commands"] = ["M3 S0"]
    verified["controller"]["pen_down_commands"] = ["M3 S1000"]
    with pytest.raises(PlotJobError, match="lacks expected setting"):
        ControllerProfile.from_mapping(verified)


def test_streamer_waits_for_idle_and_holds_on_alarm() -> None:
    class FakeConnection:
        def __init__(self, responses: list[bytes]):
            self.responses = responses
            self.writes: list[bytes] = []

        def write(self, payload: bytes) -> None:
            self.writes.append(payload)

        def flush(self) -> None:
            pass

        def readline(self) -> bytes:
            return self.responses.pop(0) if self.responses else b""

    connection = FakeConnection(
        [b"ok\n", b"<Run|MPos:0,0,0>\n", b"<Idle|MPos:1,1,0>\n"]
    )
    streamer = object.__new__(GrblStreamer)
    streamer.connection = connection
    streamer.timeout_s = 0.05
    streamer.stream("G21\n", idle_timeout_s=1.0)
    assert connection.writes == [b"G21\n", b"?", b"?"]

    check_connection = FakeConnection([b"ok\n", b"ok\n", b"ok\n", b"ok\n"])
    check_streamer = object.__new__(GrblStreamer)
    check_streamer.connection = check_connection
    check_streamer.timeout_s = 0.05
    check_streamer.check_program("G21\nG90\n")
    assert check_connection.writes == [b"$C\n", b"G21\n", b"G90\n", b"$C\n"]

    buffered_connection = FakeConnection(
        [b"ok\n", b"ok\n", b"ok\n", b"<Idle|MPos:1,1,0>\n"]
    )
    buffered_streamer = object.__new__(GrblStreamer)
    buffered_streamer.connection = buffered_connection
    buffered_streamer.timeout_s = 0.05
    progress: list[tuple[int, int]] = []
    buffered_streamer.stream(
        "G21\nG90\nG94\n",
        lambda done, total: progress.append((done, total)),
        idle_timeout_s=1.0,
    )
    assert buffered_connection.writes[:3] == [b"G21\n", b"G90\n", b"G94\n"]
    assert progress == [(1, 3), (2, 3), (3, 3)]

    alarm_connection = FakeConnection([b"ok\n", b"ALARM:1\n"])
    alarm_streamer = object.__new__(GrblStreamer)
    alarm_streamer.connection = alarm_connection
    alarm_streamer.timeout_s = 0.05
    with pytest.raises(PlotJobError, match="GRBL alarm"):
        alarm_streamer.stream("G21\n", idle_timeout_s=1.0)
    assert alarm_connection.writes[-1] == b"!"

    settings_connection = FakeConnection([b"$11=0.050\n", b"$120=400.000\n", b"ok\n"])
    settings_streamer = object.__new__(GrblStreamer)
    settings_streamer.connection = settings_connection
    settings_streamer.timeout_s = 0.05
    settings_streamer.verify_settings((("$11", 0.05), ("$120", 400.0)))
    assert settings_connection.writes == [b"$$\n"]

    mismatch_connection = FakeConnection([b"$11=0.020\n", b"ok\n"])
    mismatch_streamer = object.__new__(GrblStreamer)
    mismatch_streamer.connection = mismatch_connection
    mismatch_streamer.timeout_s = 0.05
    with pytest.raises(PlotJobError, match="firmware settings do not match"):
        mismatch_streamer.verify_settings((("$11", 0.05),))

    offset_connection = FakeConnection(
        [b"[G54:1.000,2.000,0.000]\n", b"[G92:0.000,0.000,0.000]\n", b"ok\n"]
    )
    offset_streamer = object.__new__(GrblStreamer)
    offset_streamer.connection = offset_connection
    offset_streamer.timeout_s = 0.05
    offset_streamer.verify_coordinate_offsets("G54", (1.0, 2.0, 0.0))
    assert offset_connection.writes == [b"$#\n"]

    stale_offset_connection = FakeConnection(
        [b"[G54:0.000,0.000,0.000]\n", b"[G92:3.000,0.000,0.000]\n", b"ok\n"]
    )
    stale_offset_streamer = object.__new__(GrblStreamer)
    stale_offset_streamer.connection = stale_offset_connection
    stale_offset_streamer.timeout_s = 0.05
    with pytest.raises(PlotJobError, match="active G92"):
        stale_offset_streamer.verify_coordinate_offsets("G54", (0.0, 0.0, 0.0))


def test_large_grid_order_is_exactly_the_brute_force_greedy_result() -> None:
    pen = Pen("p", "Black", 0.25, 0.25, "#000", "Black 0.25")
    strokes = [
        Stroke(
            "layer",
            pen,
            [
                ((index * 37) % 293 + 0.1, (index * 53) % 197 + 0.2),
                ((index * 71) % 293 + 0.3, (index * 89) % 197 + 0.4),
            ],
            index,
        )
        for index in range(540)
    ]
    remaining = list(strokes)
    cursor = (0.0, 0.0)
    expected: list[tuple[int, bool]] = []
    while remaining:
        best_index, best_cost, flip = 0, float("inf"), False
        for index, stroke in enumerate(remaining):
            forward = math.dist(cursor, stroke.start)
            backward = math.dist(cursor, stroke.end)
            if forward < best_cost:
                best_index, best_cost, flip = index, forward, False
            if backward < best_cost:
                best_index, best_cost, flip = index, backward, True
        selected = remaining.pop(best_index)
        if flip:
            selected = selected.reversed_copy()
        expected.append((selected.sid, selected.rev))
        cursor = selected.end
    actual = [
        (stroke.sid, stroke.rev) for stroke in _nearest_neighbour_grid(strokes, (0, 0))
    ]
    assert actual == expected


def test_committed_device_profiles_conform_to_their_schema() -> None:
    schema = json.loads(
        (ROOT / "docs/plotter/device-profile-v1.schema.json").read_text()
    )
    validator = Draft202012Validator(schema)
    for name in (
        "axidraw-class-simulation-v1.json",
        "grbl-servo-template-v1.json",
    ):
        profile = json.loads((ROOT / "plotter-profiles" / name).read_text())
        validator.validate(profile)


def test_compiled_plot_job_conforms_to_its_schema(tmp_path: Path) -> None:
    schema = json.loads((ROOT / "docs/plotter/plot-job-v1.schema.json").read_text())
    svg = tmp_path / "fixture.svg"
    svg.write_text(_svg(), encoding="utf-8")
    Draft202012Validator(schema).validate(compile_plot_job(svg, _machine()))
