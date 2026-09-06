"""Expanded art-directed 3D vocabulary for abstract pen-plot plates.

These scenes extend the original six recipes with distinct, authored spatial
ideas drawn from broad early-CGI and 1990s electronic-music design lineages.
Every recipe is real triangle geometry viewed through :mod:`plot3d`'s
perspective camera and depth buffer.  The references are a production grammar,
not templates: dominant hero form, severe camera, semantic material families,
controlled support geometry and intentional voids.

The module is deliberately integration-neutral.  ``EXPANDED_SCENE_FACTORIES``
can be merged into the abstract-art catalogue by its owner without creating a
reverse dependency from the established six-scene module.
"""

from __future__ import annotations

import math
from typing import Callable, Sequence

import numpy as np

from .plot3d import (
    Camera3D,
    Curve3D,
    Mesh3D,
    ParametricMesh,
    Scene3D,
    Vec3,
    box_mesh,
    curves_from_family,
    extruded_polygon_mesh,
    mesh_with_transform,
    parametric_mesh,
    transform_points,
)


SceneFactory = Callable[[int], Scene3D]


BLACK_HEAVY = "black-1"
BLACK = "black-0-4"
BLUE = "blue-0-25"
BLUE_HEAVY = "blue-0-4"
PURPLE = "purple-0-25"
PURPLE_HEAVY = "purple-0-4"
GREY = "grey-0-25"
GREEN = "green-0-25"
RED = "red-0-25"
GOLD = "gold-1"
SILVER = "silver-1"


def _seed_phase(seed: int, salt: int = 0) -> float:
    """Return a stable irrational phase without changing scene authorship."""

    return 2.0 * math.pi * (((seed + salt) * 0.6180339887498948) % 1.0)


def _attributes(
    material: str,
    lineage: str,
    composition_role: str,
) -> dict[str, str]:
    return {
        "data-material": material,
        "data-lineage": lineage,
        "data-composition-role": composition_role,
        "data-geometry-source": "project-authored-triangle-mesh",
    }


def _surface_object(
    object_id: str,
    surface: ParametricMesh,
    *,
    material: str,
    lineage: str,
    composition_role: str,
    u_pens: Sequence[str] = (),
    v_pens: Sequence[str] = (),
    u_every: int = 3,
    v_every: int = 3,
    u_phase: int = 0,
    v_phase: int = 0,
    silhouette: str | None = BLACK_HEAVY,
    crease: str | None = None,
    crease_angle_deg: float = 44.0,
) -> Mesh3D:
    attributes = _attributes(material, lineage, composition_role)
    curves: list[Curve3D] = []
    if u_pens:
        curves.extend(
            curves_from_family(
                object_id=object_id,
                family=surface.u_curves,
                pen_ids=u_pens,
                role="surface-transverse",
                prefix=f"{object_id}-transverse",
                every=u_every,
                phase=u_phase,
                depth_bias=-0.022,
                attributes=attributes,
            )
        )
    if v_pens:
        curves.extend(
            curves_from_family(
                object_id=object_id,
                family=surface.v_curves,
                pen_ids=v_pens,
                role="surface-longitudinal",
                prefix=f"{object_id}-longitudinal",
                every=v_every,
                phase=v_phase,
                depth_bias=-0.022,
                attributes=attributes,
            )
        )
    return Mesh3D(
        id=object_id,
        vertices=surface.vertices,
        faces=surface.faces,
        curves=curves,
        silhouette_pen_id=silhouette,
        crease_pen_id=crease,
        crease_angle_deg=crease_angle_deg,
        attributes=attributes,
    )


def _box_object(
    object_id: str,
    *,
    size: Vec3,
    translation: Vec3,
    rotation: Vec3 = (0.0, 0.0, 0.0),
    material: str,
    lineage: str,
    composition_role: str = "support",
    silhouette: str = BLACK_HEAVY,
    crease: str = BLACK,
) -> Mesh3D:
    vertices, faces, _ = box_mesh(
        size=size,
        translation=translation,
        rotation_deg=rotation,
    )
    return Mesh3D(
        id=object_id,
        vertices=vertices,
        faces=faces,
        silhouette_pen_id=silhouette,
        crease_pen_id=crease,
        crease_angle_deg=34.0,
        attributes=_attributes(material, lineage, composition_role),
    )


def _extruded_object(
    object_id: str,
    polygon: Sequence[tuple[float, float]],
    *,
    depth: float,
    translation: Vec3,
    rotation: Vec3 = (0.0, 0.0, 0.0),
    scale: Vec3 = (1.0, 1.0, 1.0),
    material: str,
    lineage: str,
    composition_role: str,
    silhouette: str = BLACK_HEAVY,
    crease: str = BLACK,
) -> Mesh3D:
    vertices, faces, _ = extruded_polygon_mesh(
        polygon,
        depth=depth,
        translation=translation,
        rotation_deg=rotation,
        scale=scale,
    )
    return Mesh3D(
        id=object_id,
        vertices=vertices,
        faces=faces,
        silhouette_pen_id=silhouette,
        crease_pen_id=crease,
        crease_angle_deg=32.0,
        attributes=_attributes(material, lineage, composition_role),
    )


def _superform(
    *,
    exponents: tuple[float, float] = (0.85, 0.85),
    ripple: tuple[float, int, float] = (0.0, 3, 0.0),
    translation: Vec3 = (0.0, 0.0, 0.0),
    rotation: Vec3 = (0.0, 0.0, 0.0),
    scale: Vec3 = (1.0, 1.0, 1.0),
    u_range: tuple[float, float] = (0.0, 2.0 * math.pi),
    wrap_u: bool = True,
    u_steps: int = 40,
    v_steps: int = 21,
) -> ParametricMesh:
    longitude_exp, latitude_exp = exponents
    amplitude, frequency, phase = ripple

    def signed_power(value: float, exponent: float) -> float:
        return math.copysign(abs(value) ** exponent, value)

    def surface(u: float, v: float) -> tuple[float, float, float]:
        longitude = signed_power(math.cos(u), longitude_exp)
        side = signed_power(math.sin(u), longitude_exp)
        latitude = signed_power(math.cos(v), latitude_exp)
        height = signed_power(math.sin(v), latitude_exp)
        radius = 1.0 + amplitude * math.sin(frequency * u + 1.7 * v + phase)
        return (
            radius * latitude * longitude,
            radius * latitude * side,
            height,
        )

    return mesh_with_transform(
        parametric_mesh(
            surface,
            u_range=u_range,
            v_range=(-math.pi / 2.0 + 0.045, math.pi / 2.0 - 0.045),
            u_steps=u_steps,
            v_steps=v_steps,
            wrap_u=wrap_u,
            wrap_v=False,
            curve_samples=112,
        ),
        translation=translation,
        rotation_deg=rotation,
        scale=scale,
    )


def _torus(
    major: float,
    minor: float,
    *,
    translation: Vec3 = (0.0, 0.0, 0.0),
    rotation: Vec3 = (0.0, 0.0, 0.0),
    scale: Vec3 = (1.0, 1.0, 1.0),
    phase: float = 0.0,
    corrugation: float = 0.0,
    u_steps: int = 42,
    v_steps: int = 14,
) -> ParametricMesh:
    def surface(u: float, v: float) -> tuple[float, float, float]:
        modulation = corrugation * math.cos(12.0 * u + phase)
        tube = minor * (1.0 + modulation)
        radial = major + tube * math.cos(v)
        return (
            radial * math.cos(u),
            radial * math.sin(u),
            tube * math.sin(v),
        )

    return mesh_with_transform(
        parametric_mesh(
            surface,
            u_range=(0.0, 2.0 * math.pi),
            v_range=(0.0, 2.0 * math.pi),
            u_steps=u_steps,
            v_steps=v_steps,
            wrap_u=True,
            wrap_v=True,
            curve_samples=112,
        ),
        translation=translation,
        rotation_deg=rotation,
        scale=scale,
    )


def _lathed_form(
    radius_at: Callable[[float], float],
    height_at: Callable[[float], float],
    *,
    translation: Vec3 = (0.0, 0.0, 0.0),
    rotation: Vec3 = (0.0, 0.0, 0.0),
    scale: Vec3 = (1.0, 1.0, 1.0),
    phase: float = 0.0,
    u_steps: int = 42,
    v_steps: int = 22,
) -> ParametricMesh:
    def surface(u: float, v: float) -> tuple[float, float, float]:
        radius = max(0.035, float(radius_at(v)))
        angle = u + phase
        return radius * math.cos(angle), radius * math.sin(angle), height_at(v)

    return mesh_with_transform(
        parametric_mesh(
            surface,
            u_range=(0.0, 2.0 * math.pi),
            v_range=(0.0, 1.0),
            u_steps=u_steps,
            v_steps=v_steps,
            wrap_u=True,
            wrap_v=False,
            curve_samples=112,
        ),
        translation=translation,
        rotation_deg=rotation,
        scale=scale,
    )


def _twisted_ribbon(
    *,
    length: float,
    half_width: float,
    twists: float,
    wave: float,
    translation: Vec3 = (0.0, 0.0, 0.0),
    rotation: Vec3 = (0.0, 0.0, 0.0),
    scale: Vec3 = (1.0, 1.0, 1.0),
    phase: float = 0.0,
    u_steps: int = 44,
    v_steps: int = 7,
) -> ParametricMesh:
    def surface(u: float, v: float) -> tuple[float, float, float]:
        axis = length * (u - 0.5)
        angle = phase + twists * 2.0 * math.pi * u
        centre_z = wave * math.sin(2.0 * math.pi * u + phase)
        return (
            axis,
            half_width * v * math.cos(angle),
            centre_z + half_width * v * math.sin(angle),
        )

    return mesh_with_transform(
        parametric_mesh(
            surface,
            u_range=(0.0, 1.0),
            v_range=(-1.0, 1.0),
            u_steps=u_steps,
            v_steps=v_steps,
            wrap_u=False,
            wrap_v=False,
            curve_samples=112,
        ),
        translation=translation,
        rotation_deg=rotation,
        scale=scale,
    )


def _saddle(
    *,
    size: tuple[float, float],
    curvature: float,
    translation: Vec3 = (0.0, 0.0, 0.0),
    rotation: Vec3 = (0.0, 0.0, 0.0),
    phase: float = 0.0,
) -> ParametricMesh:
    def surface(u: float, v: float) -> tuple[float, float, float]:
        x = size[0] * u
        y = size[1] * v
        z = curvature * (u * u - v * v) + 0.12 * math.sin(
            2.5 * math.pi * (u + v) + phase
        )
        return x, y, z

    return mesh_with_transform(
        parametric_mesh(
            surface,
            u_range=(-1.0, 1.0),
            v_range=(-1.0, 1.0),
            u_steps=25,
            v_steps=21,
            wrap_u=False,
            wrap_v=False,
            curve_samples=105,
        ),
        translation=translation,
        rotation_deg=rotation,
    )


def _petal(
    *,
    angle_deg: float,
    length: float,
    width: float,
    rise: float,
    translation: Vec3,
    phase: float,
) -> ParametricMesh:
    def surface(s: float, t: float) -> tuple[float, float, float]:
        bell = math.sin(math.pi * s)
        x = length * s
        y = width * bell * t
        z = rise * bell * (1.0 - 0.38 * t * t) + 0.10 * math.sin(
            3.0 * math.pi * s + phase
        )
        return x, y, z

    return mesh_with_transform(
        parametric_mesh(
            surface,
            u_range=(0.035, 0.975),
            v_range=(-1.0, 1.0),
            u_steps=24,
            v_steps=7,
            wrap_u=False,
            wrap_v=False,
            curve_samples=96,
        ),
        translation=translation,
        rotation_deg=(0.0, -8.0, angle_deg),
    )


def _curve(
    curve_id: str,
    points: Sequence[Sequence[float]] | np.ndarray,
    *,
    pen_id: str,
    role: str,
    object_id: str,
    material: str,
    lineage: str,
    composition_role: str = "support",
    occluded: bool = True,
    depth_bias: float = -0.02,
) -> Curve3D:
    return Curve3D(
        id=curve_id,
        points=np.asarray(points, dtype=float),
        pen_id=pen_id,
        role=role,
        object_id=object_id,
        occluded=occluded,
        depth_bias=depth_bias,
        attributes=_attributes(material, lineage, composition_role),
    )


def _facet_hatching(
    mesh: Mesh3D,
    *,
    pen_ids: Sequence[str],
    lineage: str,
    maximum_lines: int = 4,
    light_from: Vec3 = (-0.72, -0.46, 1.0),
) -> list[Curve3D]:
    """Derive broad face hatching from real mesh normals and a key light."""

    light = np.asarray(light_from, dtype=float)
    light /= np.linalg.norm(light)
    triangles = mesh.vertices[mesh.faces]
    result: list[Curve3D] = []
    for face_index, triangle in enumerate(triangles):
        edge_lengths = np.linalg.norm(np.roll(triangle, -1, axis=0) - triangle, axis=1)
        if float(edge_lengths.max()) < 0.48:
            continue
        normal = np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])
        normal_length = float(np.linalg.norm(normal))
        if normal_length <= 1e-10:
            continue
        normal /= normal_length
        illumination = max(0.0, float(np.dot(normal, light)))
        line_count = 1 + int(round((1.0 - illumination) * (maximum_lines - 1)))
        pen_id = pen_ids[min(len(pen_ids) - 1, int(illumination * len(pen_ids)))]
        for hatch_index in range(line_count):
            fraction = (hatch_index + 1.0) / (line_count + 1.7)
            start = triangle[0] * (1.0 - fraction) + triangle[1] * fraction
            end = triangle[0] * (1.0 - fraction) + triangle[2] * fraction
            if float(np.linalg.norm(end - start)) < 0.48:
                continue
            result.append(
                _curve(
                    f"{mesh.id}-lit-facet-{face_index:04d}-{hatch_index:02d}",
                    (start, end),
                    pen_id=pen_id,
                    role="light-derived-facet-hatch",
                    object_id=mesh.id,
                    material=mesh.attributes.get("data-material", "faceted-machine"),
                    lineage=lineage,
                    composition_role=mesh.attributes.get(
                        "data-composition-role", "hero-integral"
                    ),
                    depth_bias=-0.03,
                )
            )
    return result


def _orbit_curve(
    curve_id: str,
    *,
    radii: Vec3,
    translation: Vec3,
    rotation: Vec3,
    pen_id: str,
    material: str,
    lineage: str,
    phase: float = 0.0,
    role: str = "orbital-trajectory",
    composition_role: str = "support",
) -> Curve3D:
    values = np.linspace(0.0, 2.0 * math.pi, 181)
    points = np.column_stack(
        (
            radii[0] * np.cos(values + phase),
            radii[1] * np.sin(values + phase),
            radii[2] * np.sin(2.0 * values + phase),
        )
    )
    points = transform_points(
        points,
        translation=translation,
        rotation_deg=rotation,
    )
    return _curve(
        curve_id,
        points,
        pen_id=pen_id,
        role=role,
        object_id=curve_id.rsplit("-", 1)[0],
        material=material,
        lineage=lineage,
        composition_role=composition_role,
    )


def _backdrop_scans(
    prefix: str,
    *,
    x_extent: float,
    y: float,
    z_values: Sequence[float],
    skew: float,
    pen_ids: Sequence[str],
    lineage: str,
    material: str = "depth-register-signal",
) -> list[Curve3D]:
    curves: list[Curve3D] = []
    for index, z in enumerate(z_values):
        points = np.array(
            (
                (-x_extent, y - skew, z - 0.18 * skew),
                (x_extent, y + skew, z + 0.18 * skew),
            ),
            dtype=float,
        )
        curves.append(
            _curve(
                f"{prefix}-scan-{index:02d}",
                points,
                pen_id=pen_ids[index % len(pen_ids)],
                role="depth-register-scan",
                object_id=f"{prefix}-signal-field",
                material=material,
                lineage=lineage,
            )
        )
    return curves


def _scene(
    scene_id: str,
    *,
    camera: Camera3D,
    meshes: Sequence[Mesh3D],
    curves: Sequence[Curve3D],
    zoom: float,
    framing_offset: tuple[float, float] = (0.0, 0.0),
    depth_buffer_px: int = 720,
) -> Scene3D:
    return Scene3D(
        id=scene_id,
        camera=camera,
        meshes=list(meshes),
        curves=list(curves),
        zoom=zoom,
        framing_offset=framing_offset,
        depth_buffer_px=depth_buffer_px,
        crop_intent="intentional-crop",
    )


# ---------------------------------------------------------------------------
# Engineered CGI worlds


def scene_relay_canyon(seed: int) -> Scene3D:
    """A single suspended relay deck seen from below, spanning a signal void."""

    lineage = "engineered-cgi-world"
    phase = _seed_phase(seed, 11)
    hero = _extruded_object(
        "relay-canyon-cantilever",
        (
            (-4.2, -0.45),
            (-3.75, 0.52),
            (-1.45, 1.18),
            (1.45, 1.18),
            (3.75, 0.52),
            (4.2, -0.45),
            (2.45, -0.78),
            (-2.45, -0.78),
        ),
        depth=1.55,
        translation=(0.0, 0.0, 1.35),
        rotation=(90.0, 0.0, 0.0),
        material="blue-anodised-relay-chassis",
        lineage=lineage,
        composition_role="dominant-hero",
        crease=BLUE_HEAVY,
    )
    deck = _box_object(
        "relay-canyon-deck",
        size=(8.8, 1.72, 0.22),
        translation=(0.0, 0.0, 2.48),
        material="black-ceramic-deck",
        lineage=lineage,
        composition_role="hero-integral",
        crease=BLACK,
    )
    piers = [
        _box_object(
            f"relay-canyon-pier-{index}",
            size=(0.42, 0.72, 2.42),
            translation=(x, 0.0, 0.35),
            rotation=(0.0, (-1.0 if x < 0.0 else 1.0) * 7.0, 0.0),
            material="graphite-load-pier",
            lineage=lineage,
            crease=GREY,
        )
        for index, x in enumerate((-3.18, 3.18))
    ]
    relay_core = _surface_object(
        "relay-canyon-suspended-core",
        _superform(
            exponents=(0.56, 0.68),
            ripple=(0.055, 5, phase),
            translation=(0.0, 0.0, 0.38),
            rotation=(8.0, 18.0, 0.0),
            scale=(0.92, 0.92, 1.18),
            u_steps=36,
            v_steps=19,
        ),
        material="violet-suspended-relay-core",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(PURPLE, BLUE),
        v_pens=(SILVER,),
        u_every=2,
        v_every=2,
        silhouette=BLACK_HEAVY,
    )
    relay_collar = _surface_object(
        "relay-canyon-core-collar",
        _torus(
            1.30,
            0.13,
            translation=(0.0, 0.0, 0.38),
            rotation=(72.0, 8.0, 0.0),
            corrugation=0.025,
            phase=phase,
            u_steps=36,
            v_steps=10,
        ),
        material="silver-relay-collar",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(SILVER,),
        v_pens=(PURPLE,),
        u_every=4,
        v_every=4,
        silhouette=BLACK,
    )
    curves = _backdrop_scans(
        "relay-canyon",
        x_extent=6.0,
        y=2.55,
        z_values=(-0.35, 0.12, 0.62, 1.12, 1.62, 2.12, 2.62, 3.12),
        skew=0.45 * math.sin(phase),
        pen_ids=(BLUE, GREY, BLUE),
        lineage=lineage,
        material="canyon-depth-telemetry",
    )
    for index, y in enumerate((-0.62, 0.62)):
        curves.append(
            _curve(
                f"relay-canyon-deck-rail-{index}",
                ((-4.55, y, 2.64), (4.55, y, 2.64)),
                pen_id=SILVER,
                role="load-path-rail",
                object_id="relay-canyon-deck",
                material="polished-relay-rail",
                lineage=lineage,
                composition_role="hero-integral",
            )
        )
    for index, x in enumerate(np.linspace(-3.65, 3.65, 9)):
        sag = 0.38 + 1.28 * (abs(float(x)) / 3.65) ** 1.65
        curves.append(
            _curve(
                f"relay-canyon-core-stay-{index:02d}",
                ((float(x), -0.68, 2.54), (float(x) * 0.28, -0.18, sag)),
                pen_id=(PURPLE if index % 2 else BLUE),
                role="suspended-core-stay",
                object_id="relay-canyon-suspended-core",
                material="tensioned-signal-stay",
                lineage=lineage,
                composition_role="hero-integral",
            )
        )
    return _scene(
        "relay-canyon",
        camera=Camera3D(
            eye=(8.4, -11.8, 3.65),
            target=(0.0, 0.15, 1.35),
            fov_y_deg=38.0,
            lens_shift=(0.025, -0.025),
        ),
        meshes=[hero, deck, *piers, relay_core, relay_collar],
        curves=curves,
        zoom=1.13,
        framing_offset=(0.0, 0.035),
    )


def scene_circuit_ziggurat(seed: int) -> Scene3D:
    """A stepped computation monument with one routed trace climbing its mass."""

    lineage = "engineered-cgi-world"
    phase = _seed_phase(seed, 23)
    dimensions = (
        (7.4, 5.4, 0.62),
        (5.7, 4.1, 0.62),
        (4.15, 2.95, 0.62),
        (2.65, 1.82, 0.62),
    )
    tiers = [
        _box_object(
            f"circuit-ziggurat-tier-{index}",
            size=size,
            translation=(0.0, 0.0, 0.31 + index * 0.60),
            rotation=(0.0, 0.0, -5.0),
            material=(
                "black-glass-computation-tier"
                if index % 2 == 0
                else "violet-anodised-computation-tier"
            ),
            lineage=lineage,
            composition_role=("dominant-hero" if index == 0 else "hero-integral"),
            crease=(PURPLE_HEAVY if index % 2 else BLACK),
        )
        for index, size in enumerate(dimensions)
    ]
    crown = _box_object(
        "circuit-ziggurat-crown",
        size=(0.54, 0.54, 1.48),
        translation=(0.0, 0.0, 3.00),
        rotation=(0.0, 0.0, 40.0),
        material="silver-signal-crown",
        lineage=lineage,
        composition_role="hero-integral",
        crease=SILVER,
    )
    route = np.array(
        (
            (-4.5, -2.86, 0.14),
            (-2.9, -2.30, 0.66),
            (-2.15, -1.76, 1.22),
            (-1.33, -1.20, 1.82),
            (-0.72, -0.74, 2.39),
            (0.0, -0.28, 3.74),
        ),
        dtype=float,
    )
    route[:, 0] += 0.12 * math.sin(phase)
    curves = [
        _curve(
            "circuit-ziggurat-routed-trace",
            route,
            pen_id=GOLD,
            role="ascending-circuit-trace",
            object_id="circuit-ziggurat",
            material="gold-signal-trace",
            lineage=lineage,
            composition_role="hero-integral",
            depth_bias=-0.035,
        )
    ]
    curves.extend(
        _backdrop_scans(
            "circuit-ziggurat",
            x_extent=5.7,
            y=3.45,
            z_values=(0.18, 0.68, 1.18, 1.68, 2.18, 2.68, 3.18),
            skew=0.8,
            pen_ids=(PURPLE, GREY),
            lineage=lineage,
            material="violet-process-horizon",
        )
    )
    for tier in (*tiers, crown):
        curves.extend(
            _facet_hatching(
                tier,
                pen_ids=(BLACK, PURPLE),
                lineage=lineage,
                maximum_lines=4,
            )
        )
    return _scene(
        "circuit-ziggurat",
        camera=Camera3D(
            eye=(7.8, -10.6, 5.6),
            target=(0.0, 0.0, 1.45),
            fov_y_deg=36.0,
        ),
        meshes=[*tiers, crown],
        curves=curves,
        zoom=1.17,
        framing_offset=(-0.015, 0.035),
    )


def scene_vector_drydock(seed: int) -> Scene3D:
    """A finned data hull hanging inside a sparse maintenance gantry."""

    lineage = "engineered-cgi-world"
    phase = _seed_phase(seed, 37)
    hull = _extruded_object(
        "vector-drydock-hull",
        (
            (-4.35, -0.10),
            (-2.55, 0.92),
            (1.85, 0.83),
            (4.25, 0.18),
            (2.05, -0.74),
            (-2.65, -0.68),
        ),
        depth=1.55,
        translation=(0.0, 0.0, 1.72),
        rotation=(90.0, 0.0, 0.0),
        material="brushed-silver-data-hull",
        lineage=lineage,
        composition_role="dominant-hero",
        crease=SILVER,
    )
    rails = [
        _box_object(
            f"vector-drydock-rail-{index}",
            size=(9.8, 0.20, 0.20),
            translation=(0.0, y, 3.25),
            material="blue-gantry-rail",
            lineage=lineage,
            silhouette=BLACK_HEAVY if index == 0 else PURPLE_HEAVY,
            crease=BLUE,
        )
        for index, y in enumerate((-1.72, 1.72))
    ]
    pylons = [
        _box_object(
            f"vector-drydock-pylon-{index}",
            size=(0.20, 0.20, 4.45),
            translation=(x, y, 1.18),
            material="black-gantry-pylon",
            lineage=lineage,
            crease=GREY,
        )
        for index, (x, y) in enumerate(
            ((-4.55, -1.72), (-4.55, 1.72), (4.55, -1.72), (4.55, 1.72))
        )
    ]
    fins = [
        _box_object(
            f"vector-drydock-fin-{index}",
            size=(0.10, 2.20, 1.25),
            translation=(x, 0.0, 1.90 + 0.12 * math.sin(phase + index)),
            rotation=(0.0, 0.0, -4.0),
            material="violet-radiator-fin",
            lineage=lineage,
            composition_role="hero-integral",
            silhouette=PURPLE_HEAVY,
            crease=PURPLE,
        )
        for index, x in enumerate((-2.55, -1.25, 0.10, 1.45, 2.75))
    ]
    curves = _backdrop_scans(
        "vector-drydock",
        x_extent=5.5,
        y=2.55,
        z_values=(0.2, 0.7, 1.2, 1.7, 2.2, 2.7, 3.2, 3.7),
        skew=-0.35,
        pen_ids=(BLUE, PURPLE, GREY),
        lineage=lineage,
        material="drydock-scan-field",
    )
    return _scene(
        "vector-drydock",
        camera=Camera3D(
            eye=(8.9, -12.4, 3.15),
            target=(0.0, 0.0, 1.75),
            fov_y_deg=37.0,
            lens_shift=(0.02, -0.035),
        ),
        meshes=[hull, *fins, *rails, *pylons],
        curves=curves,
        zoom=1.14,
        framing_offset=(-0.015, 0.02),
    )


def scene_phase_foundry(seed: int) -> Scene3D:
    """A continuous alloy ribbon being tensioned over two mechanical rollers."""

    lineage = "engineered-cgi-world"
    phase = _seed_phase(seed, 41)
    ribbon = _surface_object(
        "phase-foundry-ribbon",
        _twisted_ribbon(
            length=9.4,
            half_width=1.18,
            twists=0.72,
            wave=0.52,
            translation=(0.0, 0.0, 2.25),
            rotation=(0.0, -5.0, -7.0),
            phase=phase * 0.14,
        ),
        material="silver-phase-alloy",
        lineage=lineage,
        composition_role="dominant-hero",
        u_pens=(SILVER, BLUE),
        v_pens=(GREY,),
        u_every=2,
        v_every=2,
        silhouette=BLACK_HEAVY,
    )
    rollers = [
        _surface_object(
            f"phase-foundry-roller-{index}",
            _torus(
                0.72,
                0.13,
                translation=(x, 0.0, 0.72),
                rotation=(90.0, 0.0, 0.0),
                corrugation=0.08,
                phase=phase,
                u_steps=30,
                v_steps=10,
            ),
            material="violet-tension-roller",
            lineage=lineage,
            composition_role="support",
            u_pens=(PURPLE,),
            v_pens=(GREY,),
            u_every=5,
            v_every=4,
            silhouette=PURPLE_HEAVY,
        )
        for index, x in enumerate((-2.95, 2.95))
    ]
    bed = _box_object(
        "phase-foundry-bed",
        size=(8.0, 2.65, 0.30),
        translation=(0.0, 0.0, 0.12),
        material="black-foundry-bed",
        lineage=lineage,
        crease=BLACK,
    )
    curves = _backdrop_scans(
        "phase-foundry",
        x_extent=5.8,
        y=2.20,
        z_values=(0.28, 0.78, 1.28, 1.78, 2.28, 2.78, 3.28, 3.78),
        skew=0.25,
        pen_ids=(GREY, BLUE),
        lineage=lineage,
        material="foundry-process-field",
    )
    return _scene(
        "phase-foundry",
        camera=Camera3D(
            eye=(8.7, -12.0, 3.85),
            target=(0.0, 0.0, 1.65),
            fov_y_deg=39.0,
        ),
        meshes=[ribbon, *rollers, bed],
        curves=curves,
        zoom=1.17,
        framing_offset=(0.0, 0.025),
    )


# ---------------------------------------------------------------------------
# Hero artifacts


def scene_orbital_medallion(seed: int) -> Scene3D:
    """A near-frontal chrome badge, thick enough to read as a physical relic."""

    lineage = "hero-artifact"
    phase = _seed_phase(seed, 53)
    face = _surface_object(
        "orbital-medallion-face",
        _superform(
            exponents=(0.82, 0.82),
            ripple=(0.018, 8, phase),
            translation=(0.0, 0.0, 1.75),
            rotation=(0.0, 0.0, -9.0),
            scale=(3.05, 0.57, 3.05),
            u_steps=44,
            v_steps=23,
        ),
        material="polished-silver-medallion-face",
        lineage=lineage,
        composition_role="dominant-hero",
        u_pens=(SILVER, GREY),
        v_pens=(GOLD,),
        u_every=2,
        v_every=2,
        silhouette=BLACK_HEAVY,
    )
    rim = _surface_object(
        "orbital-medallion-rim",
        _torus(
            3.10,
            0.18,
            translation=(0.0, -0.08, 1.75),
            rotation=(90.0, 0.0, -9.0),
            scale=(1.0, 1.0, 1.0),
            corrugation=0.045,
            phase=phase,
            u_steps=48,
            v_steps=12,
        ),
        material="gold-machined-medallion-rim",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(GOLD,),
        v_pens=(SILVER,),
        u_every=6,
        v_every=4,
        silhouette=BLACK_HEAVY,
    )
    boss = _surface_object(
        "orbital-medallion-boss",
        _superform(
            exponents=(0.55, 0.72),
            translation=(0.0, -0.72, 1.75),
            rotation=(0.0, 0.0, -9.0),
            scale=(0.82, 0.42, 0.82),
            u_steps=30,
            v_steps=17,
        ),
        material="black-ceramic-orbital-boss",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(BLACK,),
        v_pens=(PURPLE,),
        u_every=3,
        v_every=3,
        silhouette=BLACK_HEAVY,
    )
    curves = [
        _orbit_curve(
            f"orbital-medallion-calibration-{index}",
            radii=(radius, 0.85 + index * 0.12, radius),
            translation=(0.0, -0.05, 1.75),
            rotation=(90.0, 0.0, -9.0 + index * 23.0),
            pen_id=(BLUE if index == 0 else PURPLE),
            material="calibration-orbit",
            lineage=lineage,
            phase=phase * (0.12 + index * 0.05),
            composition_role="hero-integral",
        )
        for index, radius in enumerate((2.02, 2.45))
    ]
    return _scene(
        "orbital-medallion",
        camera=Camera3D(
            eye=(4.25, -12.8, 3.25),
            target=(0.0, 0.0, 1.72),
            fov_y_deg=31.5,
            lens_shift=(0.0, 0.01),
        ),
        meshes=[face, rim, boss],
        curves=curves,
        zoom=1.28,
        framing_offset=(0.0, 0.0),
        depth_buffer_px=780,
    )


def scene_split_shell_relic(seed: int) -> Scene3D:
    """Two displaced shell halves expose a narrow, luminous mechanical void."""

    lineage = "hero-artifact"
    phase = _seed_phase(seed, 67)
    halves = [
        _surface_object(
            f"split-shell-relic-half-{name}",
            _superform(
                exponents=(0.62, 0.70),
                ripple=(0.028, 5, phase + index),
                translation=(offset, 0.0, 1.7),
                rotation=(0.0, (-1.0 if index == 0 else 1.0) * 8.0, -7.0),
                scale=(2.65, 1.08, 3.35),
                u_range=u_range,
                wrap_u=False,
                u_steps=25,
                v_steps=23,
            ),
            material=f"{name}-violet-alloy-shell",
            lineage=lineage,
            composition_role="dominant-hero",
            u_pens=(PURPLE, SILVER),
            v_pens=(GREY,),
            u_every=3,
            v_every=3,
            silhouette=BLACK_HEAVY,
            crease=PURPLE_HEAVY,
            crease_angle_deg=50.0,
        )
        for index, (name, offset, u_range) in enumerate(
            (
                ("right", 0.36, (-math.pi / 2.0, math.pi / 2.0)),
                ("left", -0.36, (math.pi / 2.0, 3.0 * math.pi / 2.0)),
            )
        )
    ]
    spindle = _box_object(
        "split-shell-relic-spindle",
        size=(0.20, 0.20, 5.95),
        translation=(0.0, -0.52, 1.68),
        rotation=(0.0, -4.0, -7.0),
        material="gold-exposed-spindle",
        lineage=lineage,
        composition_role="hero-integral",
        silhouette=GOLD,
        crease=GOLD,
    )
    values = np.linspace(-2.45, 2.45, 9)
    curves = [
        _curve(
            f"split-shell-relic-void-rung-{index}",
            ((-0.28, -0.76, 1.70 + z), (0.28, -0.76, 1.70 + z)),
            pen_id=(GOLD if index % 2 == 0 else BLUE),
            role="exposed-void-rung",
            object_id="split-shell-relic-spindle",
            material="energised-shell-void",
            lineage=lineage,
            composition_role="hero-integral",
        )
        for index, z in enumerate(values)
    ]
    return _scene(
        "split-shell-relic",
        camera=Camera3D(
            eye=(6.4, -12.5, 3.65),
            target=(0.0, 0.0, 1.65),
            fov_y_deg=34.0,
        ),
        meshes=[*halves, spindle],
        curves=curves,
        zoom=1.24,
        framing_offset=(0.0, 0.0),
        depth_buffer_px=780,
    )


def scene_turbine_seed(seed: int) -> Scene3D:
    """An exploded radial turbine whose missing sector breaks the emblem."""

    lineage = "hero-artifact"
    phase = _seed_phase(seed, 79)
    vane_polygon = (
        (0.74, -0.18),
        (2.28, -0.42),
        (3.18, 0.04),
        (2.46, 0.50),
        (0.92, 0.34),
    )
    vanes: list[Mesh3D] = []
    for index in range(12):
        if index in {2, 3}:
            continue
        angle = 2.0 * math.pi * index / 12.0 + phase * 0.025
        vanes.append(
            _extruded_object(
                f"turbine-seed-vane-{index:02d}",
                vane_polygon,
                depth=0.30 + 0.055 * (index % 3),
                translation=(0.0, 0.0, 1.62 + 0.11 * math.sin(angle * 2.0)),
                rotation=(
                    13.0 * math.sin(angle),
                    -10.0 * math.cos(angle),
                    math.degrees(angle) + 17.0,
                ),
                scale=(1.0 + 0.055 * math.sin(angle + phase), 1.0, 1.0),
                material=(
                    "silver-compressor-vane"
                    if index % 3
                    else "violet-compressor-vane"
                ),
                lineage=lineage,
                composition_role="dominant-hero",
                silhouette=BLACK_HEAVY,
                crease=(BLUE if index % 3 else PURPLE),
            )
        )
    hub = _surface_object(
        "turbine-seed-bearing",
        _superform(
            exponents=(0.62, 0.72),
            ripple=(0.045, 7, phase),
            translation=(0.0, 0.0, 1.62),
            rotation=(12.0, -8.0, 17.0),
            scale=(1.10, 1.10, 1.34),
            u_steps=38,
            v_steps=21,
        ),
        material="blue-black-turbine-bearing",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(BLUE, BLACK),
        v_pens=(SILVER, PURPLE),
        u_every=3,
        v_every=3,
        silhouette=BLACK_HEAVY,
    )
    cages = [
        _surface_object(
            f"turbine-seed-bearing-cage-{index}",
            _torus(
                radius,
                0.12 + 0.025 * index,
                translation=(0.0, 0.0, 1.62),
                rotation=rotation,
                phase=phase,
                corrugation=0.035,
                u_steps=40,
                v_steps=10,
            ),
            material="silver-bearing-cage",
            lineage=lineage,
            composition_role="hero-integral",
            u_pens=(SILVER,),
            v_pens=(BLUE,),
            u_every=6,
            v_every=5,
            silhouette=BLACK,
        )
        for index, (radius, rotation) in enumerate(
            ((3.38, (0.0, 0.0, 17.0)), (2.05, (68.0, 14.0, 17.0)))
        )
    ]
    curves = [
        _orbit_curve(
            "turbine-seed-gyro",
            radii=(4.05, 2.65, 0.82),
            translation=(0.0, 0.0, 1.62),
            rotation=(28.0, 54.0, 17.0),
            pen_id=GOLD,
            material="gold-gyro-register",
            lineage=lineage,
            phase=phase * 0.18,
            composition_role="hero-integral",
        )
    ]
    for vane in vanes:
        curves.extend(
            _facet_hatching(
                vane,
                pen_ids=(BLACK, PURPLE),
                lineage=lineage,
                maximum_lines=4,
            )
        )
    return _scene(
        "turbine-seed",
        camera=Camera3D(
            eye=(7.8, -11.4, 8.1),
            target=(0.0, 0.0, 1.60),
            fov_y_deg=35.0,
        ),
        meshes=[*vanes, hub, *cages],
        curves=curves,
        zoom=1.25,
        framing_offset=(-0.01, 0.005),
        depth_buffer_px=820,
    )


def scene_prism_anchor(seed: int) -> Scene3D:
    """A monumental faceted pendant held just above a calibration plinth."""

    lineage = "hero-artifact"
    phase = _seed_phase(seed, 83)
    anchor = _extruded_object(
        "prism-anchor-body",
        ((-2.72, -0.05), (-0.15, 3.50), (2.72, -0.05), (0.0, -3.12)),
        depth=1.30,
        translation=(0.0, 0.0, 1.85),
        rotation=(90.0, 0.0, -7.0),
        material="black-glass-prism-anchor",
        lineage=lineage,
        composition_role="dominant-hero",
        crease=PURPLE_HEAVY,
    )
    inset = _extruded_object(
        "prism-anchor-inset",
        ((-1.20, 0.0), (0.0, 1.68), (1.20, 0.0), (0.0, -1.42)),
        depth=0.34,
        translation=(0.0, -0.78, 1.92),
        rotation=(90.0, 0.0, -7.0),
        material="violet-anodised-prism-inset",
        lineage=lineage,
        composition_role="hero-integral",
        silhouette=PURPLE_HEAVY,
        crease=SILVER,
    )
    eyelet = _surface_object(
        "prism-anchor-eyelet",
        _torus(
            0.58,
            0.11,
            translation=(-0.42, 0.0, 5.42),
            rotation=(90.0, 0.0, -7.0),
            corrugation=0.035,
            phase=phase,
            u_steps=34,
            v_steps=10,
        ),
        material="gold-anchor-eyelet",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(GOLD,),
        v_pens=(SILVER,),
        u_every=5,
        v_every=5,
        silhouette=BLACK,
    )
    plinth = _box_object(
        "prism-anchor-plinth",
        size=(4.65, 2.60, 0.28),
        translation=(0.25, 0.0, -1.32),
        rotation=(0.0, 0.0, -7.0),
        material="silver-calibration-plinth",
        lineage=lineage,
        crease=GREY,
    )
    curves = _backdrop_scans(
        "prism-anchor",
        x_extent=4.5,
        y=2.15,
        z_values=(-1.4, -0.55, 0.30, 1.15, 2.0, 2.85, 3.70, 4.55, 5.40),
        skew=0.38 * math.cos(phase),
        pen_ids=(PURPLE, GREY, PURPLE),
        lineage=lineage,
        material="artifact-registration-field",
    )
    curves.append(
        _curve(
            "prism-anchor-energy-slit",
            ((-0.36, -0.98, -0.25), (0.15, -0.98, 4.45)),
            pen_id=GOLD,
            role="artifact-energy-slit",
            object_id="prism-anchor-inset",
            material="gold-energised-slit",
            lineage=lineage,
            composition_role="hero-integral",
            depth_bias=-0.04,
        )
    )
    for solid in (anchor, inset, plinth):
        curves.extend(
            _facet_hatching(
                solid,
                pen_ids=(BLACK, PURPLE),
                lineage=lineage,
                maximum_lines=5,
            )
        )
    return _scene(
        "prism-anchor",
        camera=Camera3D(
            eye=(7.0, -13.4, 4.05),
            target=(0.0, 0.0, 1.80),
            fov_y_deg=34.0,
        ),
        meshes=[anchor, inset, eyelet, plinth],
        curves=curves,
        zoom=1.22,
        framing_offset=(0.0, 0.0),
        depth_buffer_px=780,
    )


# ---------------------------------------------------------------------------
# Techstep machine collage


def scene_signal_ram(seed: int) -> Scene3D:
    """A low, forward-driving machine wedge crossed by a disciplined hazard fan."""

    lineage = "techstep-machine-collage"
    phase = _seed_phase(seed, 97)
    chassis = _extruded_object(
        "signal-ram-chassis",
        (
            (-4.45, -0.82),
            (-3.35, 1.12),
            (1.78, 1.42),
            (4.30, 0.34),
            (2.40, -0.86),
        ),
        depth=2.10,
        translation=(0.0, 0.0, 1.34),
        rotation=(90.0, 0.0, -3.0),
        material="black-armoured-signal-chassis",
        lineage=lineage,
        composition_role="dominant-hero",
        crease=BLACK,
    )
    nose = _extruded_object(
        "signal-ram-nose",
        ((-0.78, -0.68), (-0.90, 0.74), (0.90, 0.74), (0.78, -0.68)),
        depth=2.58,
        translation=(3.42, -0.05, 1.44),
        rotation=(90.0, 0.0, -3.0),
        material="red-anodised-impact-nose",
        lineage=lineage,
        composition_role="hero-integral",
        silhouette=BLACK_HEAVY,
        crease=RED,
    )
    ribs = [
        _box_object(
            f"signal-ram-rib-{index}",
            size=(0.10, 2.48, 1.82 - 0.10 * abs(index - 2)),
            translation=(x, 0.0, 1.62 + 0.05 * math.sin(phase + index)),
            rotation=(0.0, -8.0 + index * 2.4, -3.0),
            material="violet-machine-rib",
            lineage=lineage,
            composition_role="hero-integral",
            silhouette=PURPLE_HEAVY,
            crease=PURPLE,
        )
        for index, x in enumerate((-2.75, -1.52, -0.25, 1.02, 2.23))
    ]
    drive_ring = _surface_object(
        "signal-ram-drive-ring",
        _torus(
            1.22,
            0.18,
            translation=(3.68, -0.02, 1.46),
            rotation=(0.0, 88.0, -3.0),
            corrugation=0.035,
            phase=phase,
            u_steps=38,
            v_steps=10,
        ),
        material="silver-blue-drive-ring",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(SILVER,),
        v_pens=(BLUE,),
        u_every=6,
        v_every=5,
        silhouette=BLACK_HEAVY,
    )
    impact_core = _surface_object(
        "signal-ram-impact-core",
        _superform(
            exponents=(0.58, 0.72),
            ripple=(0.035, 5, phase),
            translation=(4.08, -0.04, 1.46),
            rotation=(0.0, 84.0, -3.0),
            scale=(0.62, 0.62, 0.94),
            u_steps=32,
            v_steps=17,
        ),
        material="blue-impact-core",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(BLUE,),
        v_pens=(RED,),
        u_every=5,
        v_every=4,
        silhouette=BLACK_HEAVY,
    )
    curves: list[Curve3D] = []
    fan_origin = np.array((-5.4, 2.70, -0.55), dtype=float)
    for index, z in enumerate(np.linspace(0.35, 4.65, 11)):
        end = np.array((5.6, 2.70, float(z)), dtype=float)
        end[0] += 0.12 * math.sin(phase + index)
        curves.append(
            _curve(
                f"signal-ram-hazard-ray-{index:02d}",
                (fan_origin, end),
                pen_id=(RED if index % 3 == 0 else PURPLE),
                role="techstep-hazard-fan",
                object_id="signal-ram-hazard-field",
                material="red-violet-warning-signal",
                lineage=lineage,
            )
        )
    curves.append(
        _curve(
            "signal-ram-spine",
            ((-4.15, -1.18, 2.58), (4.20, -1.18, 1.80)),
            pen_id=SILVER,
            role="machine-load-spine",
            object_id="signal-ram-chassis",
            material="silver-load-spine",
            lineage=lineage,
            composition_role="hero-integral",
            depth_bias=-0.04,
        )
    )
    return _scene(
        "signal-ram",
        camera=Camera3D(
            eye=(9.1, -12.7, 2.55),
            target=(0.15, 0.0, 1.32),
            fov_y_deg=38.0,
            lens_shift=(0.035, -0.045),
        ),
        meshes=[chassis, nose, *ribs, drive_ring, impact_core],
        curves=curves,
        zoom=1.16,
        framing_offset=(-0.01, 0.03),
    )


def scene_rotor_cathedral(seed: int) -> Scene3D:
    """A single room-scale rotor turns the composition into machine architecture."""

    lineage = "techstep-machine-collage"
    phase = _seed_phase(seed, 101)
    rotor = _surface_object(
        "rotor-cathedral-wheel",
        _torus(
            3.30,
            0.38,
            translation=(0.0, 0.0, 2.55),
            rotation=(90.0, 0.0, -8.0),
            phase=phase,
            corrugation=0.12,
            u_steps=56,
            v_steps=14,
        ),
        material="black-corrugated-cathedral-rotor",
        lineage=lineage,
        composition_role="dominant-hero",
        u_pens=(BLACK, PURPLE),
        v_pens=(GREY,),
        u_every=4,
        v_every=5,
        silhouette=BLACK_HEAVY,
    )
    hub = _surface_object(
        "rotor-cathedral-hub",
        _superform(
            exponents=(0.48, 0.62),
            translation=(0.0, -0.25, 2.55),
            rotation=(0.0, 0.0, -8.0),
            scale=(0.88, 0.78, 0.88),
            u_steps=32,
            v_steps=17,
        ),
        material="silver-rotor-hub",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(SILVER,),
        v_pens=(BLUE,),
        u_every=5,
        v_every=4,
        silhouette=BLACK_HEAVY,
    )
    plinth = _box_object(
        "rotor-cathedral-plinth",
        size=(6.0, 3.2, 0.34),
        translation=(0.0, 0.15, -1.02),
        rotation=(0.0, 0.0, -8.0),
        material="violet-machine-altar",
        lineage=lineage,
        crease=PURPLE,
    )
    curves: list[Curve3D] = []
    for index in range(16):
        angle = 2.0 * math.pi * index / 16.0 + phase * 0.035
        outer = np.array(
            (3.02 * math.cos(angle), -0.20, 2.55 + 3.02 * math.sin(angle)),
            dtype=float,
        )
        inner = np.array(
            (
                0.70 * math.cos(angle + 0.18),
                -0.40,
                2.55 + 0.70 * math.sin(angle + 0.18),
            ),
            dtype=float,
        )
        points = transform_points(
            np.vstack((inner, outer)),
            rotation_deg=(0.0, 0.0, -8.0),
        )
        curves.append(
            _curve(
                f"rotor-cathedral-spoke-{index:02d}",
                points,
                pen_id=(RED if index % 4 == 0 else GREY),
                role="tensioned-rotor-spoke",
                object_id="rotor-cathedral-wheel",
                material="tensioned-metal-spoke",
                lineage=lineage,
                composition_role="hero-integral",
                depth_bias=-0.032,
            )
        )
    return _scene(
        "rotor-cathedral",
        camera=Camera3D(
            eye=(5.1, -13.1, 2.90),
            target=(0.0, 0.0, 2.30),
            fov_y_deg=34.0,
        ),
        meshes=[rotor, hub, plinth],
        curves=curves,
        zoom=1.27,
        framing_offset=(0.0, 0.005),
        depth_buffer_px=800,
    )


def scene_data_crucible(seed: int) -> Scene3D:
    """A broad receiving funnel turns descending data streams into one object."""

    lineage = "techstep-machine-collage"
    phase = _seed_phase(seed, 107)

    def crucible_radius(v: float) -> float:
        return 0.42 + 1.85 * (v**1.72) + 0.09 * math.sin(6.0 * math.pi * v + phase)

    crucible = _surface_object(
        "data-crucible-bowl",
        _lathed_form(
            crucible_radius,
            lambda v: 3.25 * (v - 0.5),
            translation=(0.0, 0.0, 2.05),
            rotation=(0.0, -4.0, 7.0),
            scale=(1.18, 1.18, 1.0),
            phase=phase * 0.04,
            u_steps=48,
            v_steps=26,
        ),
        material="violet-black-data-crucible",
        lineage=lineage,
        composition_role="dominant-hero",
        u_pens=(PURPLE, BLACK),
        v_pens=(SILVER, GREY),
        u_every=4,
        v_every=2,
        silhouette=BLACK_HEAVY,
    )
    collar = _surface_object(
        "data-crucible-mouth-collar",
        _torus(
            2.70,
            0.13,
            translation=(-0.13, 0.0, 3.67),
            rotation=(0.0, -4.0, 7.0),
            corrugation=0.04,
            phase=phase,
            u_steps=44,
            v_steps=10,
        ),
        material="silver-crucible-mouth",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(SILVER,),
        v_pens=(BLUE,),
        u_every=6,
        v_every=5,
        silhouette=BLACK,
    )
    arms = [
        _box_object(
            f"data-crucible-arm-{index}",
            size=(3.75, 0.26, 0.26),
            translation=(x, 0.12, 2.22),
            rotation=(0.0, tilt, 7.0),
            material="black-articulated-feed-arm",
            lineage=lineage,
            crease=GREY,
        )
        for index, (x, tilt) in enumerate(((-3.75, -18.0), (3.75, 18.0)))
    ]
    curves: list[Curve3D] = []
    heights = np.linspace(3.85, 7.0, 52)
    for index in range(7):
        offset = (index - 3) * 0.26
        points = np.column_stack(
            (
                offset + 0.15 * np.sin(1.8 * heights + phase + index * 0.3),
                -0.62 + 0.16 * np.cos(1.45 * heights + phase + index),
                heights,
            )
        )
        curves.append(
            _curve(
                f"data-crucible-feed-{index:02d}",
                points,
                pen_id=(BLUE if index % 2 == 0 else PURPLE),
                role="descending-data-feed",
                object_id="data-crucible-feed-field",
                material="energised-data-stream",
                lineage=lineage,
                composition_role="hero-integral",
            )
        )
    curves.append(
        _curve(
            "data-crucible-drain",
            ((0.08, 0.0, 0.42), (0.20, 0.05, -1.95)),
            pen_id=GOLD,
            role="resolved-data-drain",
            object_id="data-crucible-bowl",
            material="gold-resolved-output",
            lineage=lineage,
            composition_role="hero-integral",
        )
    )
    return _scene(
        "data-crucible",
        camera=Camera3D(
            eye=(7.7, -11.4, 7.3),
            target=(0.0, 0.0, 2.15),
            fov_y_deg=37.0,
        ),
        meshes=[crucible, collar, *arms],
        curves=curves,
        zoom=1.16,
        framing_offset=(0.0, 0.015),
        depth_buffer_px=780,
    )


# ---------------------------------------------------------------------------
# Dark optical signal


def scene_nocturne_oscilloscope(seed: int) -> Scene3D:
    """A thick optical gate interrupts a family of spatial wave traces."""

    lineage = "dark-optical-signal"
    phase = _seed_phase(seed, 113)
    gate = _surface_object(
        "nocturne-oscilloscope-gate",
        _superform(
            exponents=(0.74, 0.74),
            ripple=(0.015, 6, phase),
            translation=(0.0, 0.0, 1.80),
            rotation=(0.0, 11.0, -10.0),
            scale=(2.20, 0.42, 2.55),
            u_steps=42,
            v_steps=23,
        ),
        material="black-glass-oscilloscope-gate",
        lineage=lineage,
        composition_role="dominant-hero",
        u_pens=(BLACK, PURPLE),
        v_pens=(GREY,),
        u_every=5,
        v_every=4,
        silhouette=BLACK_HEAVY,
    )
    aperture = _surface_object(
        "nocturne-oscilloscope-aperture",
        _torus(
            1.36,
            0.10,
            translation=(-0.42, -0.43, 1.84),
            rotation=(90.0, 11.0, -10.0),
            corrugation=0.055,
            phase=phase,
            u_steps=42,
            v_steps=10,
        ),
        material="violet-optical-aperture",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(PURPLE,),
        v_pens=(BLUE,),
        u_every=5,
        v_every=5,
        silhouette=PURPLE_HEAVY,
    )
    curves: list[Curve3D] = []
    x_values = np.linspace(-6.2, 6.2, 165)
    for index in range(11):
        band = (index - 5) * 0.21
        amplitude = 0.62 + 0.04 * abs(index - 5)
        y = 0.72 + band + 0.22 * np.sin(0.73 * x_values + phase + index * 0.18)
        z = (
            1.80
            + band * 1.75
            + amplitude * np.sin(1.10 * x_values + phase + index * 0.31)
        )
        points = np.column_stack((x_values, y, z))
        curves.append(
            _curve(
                f"nocturne-oscilloscope-wave-{index:02d}",
                points,
                pen_id=(BLUE if index in {4, 5, 6} else PURPLE),
                role="spatial-oscilloscope-trace",
                object_id="nocturne-oscilloscope-wave-field",
                material="blue-violet-phosphor-signal",
                lineage=lineage,
                composition_role="dominant-signal",
                depth_bias=-0.026,
            )
        )
    curves.extend(
        _backdrop_scans(
            "nocturne-oscilloscope",
            x_extent=6.15,
            y=2.35,
            z_values=(-0.65, 0.15, 0.95, 1.75, 2.55, 3.35, 4.15),
            skew=-0.52,
            pen_ids=(GREY,),
            lineage=lineage,
            material="dark-optical-register",
        )
    )
    return _scene(
        "nocturne-oscilloscope",
        camera=Camera3D(
            eye=(7.4, -13.6, 4.75),
            target=(0.0, 0.0, 1.78),
            fov_y_deg=36.5,
            lens_shift=(0.02, -0.015),
        ),
        meshes=[gate, aperture],
        curves=curves,
        zoom=1.18,
        framing_offset=(-0.015, 0.015),
        depth_buffer_px=800,
    )


def scene_afterimage_reactor(seed: int) -> Scene3D:
    """One diagonal reactor body drags a calibrated train of contour echoes."""

    lineage = "dark-optical-signal"
    phase = _seed_phase(seed, 127)
    reactor = _surface_object(
        "afterimage-reactor-body",
        _superform(
            exponents=(0.46, 0.62),
            ripple=(0.035, 7, phase),
            translation=(1.25, 0.0, 2.10),
            rotation=(0.0, 69.0, 18.0),
            scale=(1.00, 1.00, 2.95),
            u_steps=44,
            v_steps=23,
        ),
        material="black-violet-afterimage-reactor",
        lineage=lineage,
        composition_role="dominant-hero",
        u_pens=(PURPLE, BLACK),
        v_pens=(SILVER, GREY),
        u_every=2,
        v_every=2,
        silhouette=BLACK_HEAVY,
    )
    core = _surface_object(
        "afterimage-reactor-core",
        _superform(
            exponents=(0.70, 0.70),
            translation=(1.15, -0.72, 2.08),
            rotation=(0.0, 69.0, 18.0),
            scale=(0.48, 0.48, 1.15),
            u_steps=30,
            v_steps=17,
        ),
        material="blue-reactor-core",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(BLUE,),
        v_pens=(SILVER,),
        u_every=3,
        v_every=3,
        silhouette=BLUE_HEAVY,
    )
    curves: list[Curve3D] = []
    for index in range(8):
        offset = -0.52 * index
        radius = 1.03 + index * 0.10
        curves.append(
            _orbit_curve(
                f"afterimage-reactor-echo-{index}",
                radii=(0.46 + index * 0.035, radius, radius * 0.62),
                translation=(1.15 + offset, 0.16 * index, 2.08 - 0.14 * index),
                rotation=(0.0, 69.0, 18.0),
                pen_id=(PURPLE if index < 4 else GREY),
                material="decaying-reactor-afterimage",
                lineage=lineage,
                phase=phase * 0.03 + index * 0.17,
                role="reactor-afterimage-contour",
                composition_role="signal-trail",
            )
        )
    tail_values = np.linspace(-5.5, 4.6, 110)
    tail = np.column_stack(
        (
            tail_values,
            1.9 + 0.25 * np.sin(0.9 * tail_values + phase),
            0.45 + 0.25 * tail_values + 0.18 * np.cos(1.4 * tail_values),
        )
    )
    curves.append(
        _curve(
            "afterimage-reactor-vector",
            tail,
            pen_id=PURPLE,
            role="reactor-direction-vector",
            object_id="afterimage-reactor-body",
            material="red-direction-signal",
            lineage=lineage,
            composition_role="hero-integral",
        )
    )
    return _scene(
        "afterimage-reactor",
        camera=Camera3D(
            eye=(8.6, -12.3, 6.1),
            target=(-0.45, 0.3, 1.65),
            fov_y_deg=37.0,
        ),
        meshes=[reactor, core],
        curves=curves,
        zoom=1.22,
        framing_offset=(0.015, 0.015),
        depth_buffer_px=800,
    )


def scene_diffraction_vault(seed: int) -> Scene3D:
    """A warped optical vault catches a narrow fan of projected trajectories."""

    lineage = "dark-optical-signal"
    phase = _seed_phase(seed, 131)
    vault = _surface_object(
        "diffraction-vault-surface",
        _saddle(
            size=(3.85, 2.75),
            curvature=1.22,
            translation=(0.0, 0.65, 1.72),
            rotation=(56.0, -7.0, 22.0),
            phase=phase,
        ),
        material="black-optical-diffraction-vault",
        lineage=lineage,
        composition_role="dominant-hero",
        u_pens=(PURPLE, GREY),
        v_pens=(BLUE, BLACK),
        u_every=2,
        v_every=2,
        silhouette=BLACK_HEAVY,
        crease=PURPLE_HEAVY,
        crease_angle_deg=24.0,
    )
    focus = _surface_object(
        "diffraction-vault-focus",
        _torus(
            0.72,
            0.09,
            translation=(-0.55, -0.62, 1.48),
            rotation=(88.0, 15.0, 22.0),
            phase=phase,
            corrugation=0.06,
            u_steps=34,
            v_steps=10,
        ),
        material="silver-optical-focus",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(SILVER,),
        v_pens=(BLUE,),
        u_every=5,
        v_every=5,
        silhouette=BLACK,
    )
    curves: list[Curve3D] = []
    origin = np.array((-5.4, -1.75, -0.85), dtype=float)
    for index, angle in enumerate(np.linspace(-0.72, 0.88, 13)):
        end = np.array(
            (
                5.25,
                2.55 + 0.22 * math.sin(phase + index),
                1.45 + 3.15 * angle,
            ),
            dtype=float,
        )
        curves.append(
            _curve(
                f"diffraction-vault-ray-{index:02d}",
                (origin, end),
                pen_id=(BLUE if index in {5, 6, 7} else PURPLE),
                role="diffracted-optical-ray",
                object_id="diffraction-vault-ray-field",
                material="blue-violet-diffraction-ray",
                lineage=lineage,
            )
        )
    return _scene(
        "diffraction-vault",
        camera=Camera3D(
            eye=(8.2, -12.6, 5.9),
            target=(0.0, 0.45, 1.65),
            fov_y_deg=39.0,
        ),
        meshes=[vault, focus],
        curves=curves,
        zoom=1.20,
        framing_offset=(0.0, 0.015),
        depth_buffer_px=800,
    )


# ---------------------------------------------------------------------------
# Orbital and atmospheric worlds


def scene_liminal_horizon(seed: int) -> Scene3D:
    """A planet is deliberately too large for the plate; a moon fixes its scale."""

    lineage = "orbital-atmospheric"
    phase = _seed_phase(seed, 137)
    planet = _surface_object(
        "liminal-horizon-planet",
        _superform(
            exponents=(0.94, 0.94),
            ripple=(0.012, 9, phase),
            translation=(-3.15, 1.75, 1.10),
            rotation=(0.0, 0.0, -12.0),
            scale=(5.85, 5.85, 5.85),
            u_steps=50,
            v_steps=27,
        ),
        material="black-blue-liminal-planet",
        lineage=lineage,
        composition_role="dominant-hero",
        u_pens=(BLUE, BLACK, BLUE),
        v_pens=(GREY, PURPLE),
        u_every=3,
        v_every=2,
        silhouette=BLACK_HEAVY,
    )
    moon = _surface_object(
        "liminal-horizon-moon",
        _superform(
            exponents=(0.76, 0.76),
            ripple=(0.055, 5, phase + 1.2),
            translation=(4.20, -0.20, 4.05),
            scale=(0.62, 0.62, 0.62),
            u_steps=28,
            v_steps=15,
        ),
        material="silver-calibration-moon",
        lineage=lineage,
        composition_role="scale-counterpoint",
        u_pens=(SILVER,),
        v_pens=(GREY,),
        u_every=3,
        v_every=3,
        silhouette=BLACK,
    )
    angles = np.linspace(-0.56 * math.pi, 0.64 * math.pi, 176) + phase * 0.025
    orbit_points = np.column_stack(
        (
            -3.15 + 7.10 * np.cos(angles),
            1.75 + 2.65 * np.sin(angles),
            1.10 + 5.10 * np.sin(angles),
        )
    )
    curves = [
        _curve(
            "liminal-horizon-moon-trajectory",
            orbit_points,
            pen_id=GOLD,
            role="partial-lunar-trajectory",
            object_id="liminal-horizon-system",
            material="gold-orbital-trajectory",
            lineage=lineage,
            composition_role="scale-counterpoint",
        )
    ]
    return _scene(
        "liminal-horizon",
        camera=Camera3D(
            eye=(7.0, -17.5, 7.2),
            target=(-1.15, 0.7, 1.45),
            fov_y_deg=35.0,
        ),
        meshes=[planet, moon],
        curves=curves,
        zoom=1.40,
        framing_offset=(-0.105, 0.065),
        depth_buffer_px=820,
    )


def scene_eclipse_array(seed: int) -> Scene3D:
    """A dark planetary core is held by three differently inclined machine orbits."""

    lineage = "orbital-atmospheric"
    phase = _seed_phase(seed, 149)
    planet = _surface_object(
        "eclipse-array-core",
        _superform(
            exponents=(0.72, 0.72),
            ripple=(0.028, 7, phase),
            translation=(0.0, 0.0, 1.85),
            rotation=(0.0, 0.0, -6.0),
            scale=(2.25, 2.25, 2.25),
            u_steps=44,
            v_steps=23,
        ),
        material="black-eclipse-core",
        lineage=lineage,
        composition_role="dominant-hero",
        u_pens=(BLACK, PURPLE),
        v_pens=(GREY,),
        u_every=5,
        v_every=4,
        silhouette=BLACK_HEAVY,
    )
    ring_specs = (
        (3.25, 0.115, (72.0, 8.0, -18.0), GOLD, "gold-ecliptic-ring"),
        (3.85, 0.085, (32.0, 61.0, 17.0), BLUE, "blue-polar-ring"),
        (4.45, 0.070, (105.0, 35.0, 42.0), PURPLE, "violet-outer-ring"),
    )
    rings = [
        _surface_object(
            f"eclipse-array-ring-{index}",
            _torus(
                major,
                minor,
                translation=(0.0, 0.0, 1.85),
                rotation=rotation,
                phase=phase + index,
                corrugation=0.025,
                u_steps=48,
                v_steps=9,
            ),
            material=material,
            lineage=lineage,
            composition_role="hero-integral",
            u_pens=(pen,),
            v_pens=(GREY,),
            u_every=7,
            v_every=5,
            silhouette=pen,
        )
        for index, (major, minor, rotation, pen, material) in enumerate(ring_specs)
    ]
    curves = [
        _curve(
            "eclipse-array-axis",
            ((-0.35, 0.05, -3.25), (0.35, -0.05, 6.95)),
            pen_id=SILVER,
            role="planetary-rotation-axis",
            object_id="eclipse-array-core",
            material="silver-rotation-axis",
            lineage=lineage,
            composition_role="hero-integral",
        )
    ]
    return _scene(
        "eclipse-array",
        camera=Camera3D(
            eye=(8.1, -14.7, 6.25),
            target=(0.0, 0.0, 1.85),
            fov_y_deg=35.0,
        ),
        meshes=[planet, *rings],
        curves=curves,
        zoom=1.26,
        framing_offset=(0.0, 0.005),
        depth_buffer_px=820,
    )


def scene_atmospheric_reentry(seed: int) -> Scene3D:
    """A compact capsule drives a fully modeled conical wake across the field."""

    lineage = "orbital-atmospheric"
    phase = _seed_phase(seed, 157)

    def wake_surface(u: float, v: float) -> tuple[float, float, float]:
        radius = 0.18 + 1.62 * v
        angle = u + 0.24 * math.sin(3.0 * math.pi * v + phase)
        return (
            -7.4 * v,
            radius * math.cos(angle),
            radius * math.sin(angle),
        )

    wake_parametric = mesh_with_transform(
        parametric_mesh(
            wake_surface,
            u_range=(0.0, 2.0 * math.pi),
            v_range=(0.0, 1.0),
            u_steps=46,
            v_steps=25,
            wrap_u=True,
            wrap_v=False,
            curve_samples=116,
        ),
        translation=(2.35, 0.0, 2.20),
        rotation_deg=(9.0, -4.0, 18.0),
    )
    wake = _surface_object(
        "atmospheric-reentry-wake",
        wake_parametric,
        material="blue-violet-volumetric-wake",
        lineage=lineage,
        composition_role="dominant-hero",
        u_pens=(PURPLE, BLUE),
        v_pens=(GREY,),
        u_every=4,
        v_every=2,
        silhouette=PURPLE_HEAVY,
    )
    capsule = _surface_object(
        "atmospheric-reentry-capsule",
        _superform(
            exponents=(0.52, 0.68),
            ripple=(0.025, 6, phase),
            translation=(3.05, -0.05, 2.42),
            rotation=(0.0, 88.0, 18.0),
            scale=(0.84, 0.84, 1.55),
            u_steps=38,
            v_steps=21,
        ),
        material="black-silver-reentry-capsule",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(SILVER,),
        v_pens=(GOLD,),
        u_every=4,
        v_every=4,
        silhouette=BLACK_HEAVY,
    )
    curves: list[Curve3D] = []
    for index, angle in enumerate(np.linspace(-0.78, 0.78, 7)):
        points = np.array(
            (
                (3.25, -0.05, 2.42),
                (-5.35, 1.72 * math.cos(angle), 2.42 + 1.72 * math.sin(angle)),
            ),
            dtype=float,
        )
        points = transform_points(points, rotation_deg=(0.0, -4.0, 18.0))
        curves.append(
            _curve(
                f"atmospheric-reentry-shear-{index:02d}",
                points,
                pen_id=(GOLD if index == 3 else BLUE),
                role="atmospheric-shear-line",
                object_id="atmospheric-reentry-wake",
                material="heated-atmospheric-shear",
                lineage=lineage,
                composition_role="hero-integral",
            )
        )
    return _scene(
        "atmospheric-reentry",
        camera=Camera3D(
            eye=(8.7, -13.1, 6.55),
            target=(-0.55, 0.0, 1.65),
            fov_y_deg=38.0,
        ),
        meshes=[wake, capsule],
        curves=curves,
        zoom=1.22,
        framing_offset=(-0.01, 0.02),
        depth_buffer_px=800,
    )


# ---------------------------------------------------------------------------
# Bio-CGI artifacts


def scene_spore_observatory(seed: int) -> Scene3D:
    """A single engineered spore pod rises above two disciplined sensor buds."""

    lineage = "bio-cgi"
    phase = _seed_phase(seed, 163)
    pod = _surface_object(
        "spore-observatory-pod",
        _superform(
            exponents=(0.54, 0.66),
            ripple=(0.075, 5, phase),
            translation=(0.0, 0.0, 2.25),
            rotation=(0.0, -7.0, -8.0),
            scale=(2.25, 1.72, 2.85),
            u_steps=46,
            v_steps=25,
        ),
        material="violet-bioluminescent-spore-shell",
        lineage=lineage,
        composition_role="dominant-hero",
        u_pens=(PURPLE, BLUE),
        v_pens=(GREEN, GREY),
        u_every=4,
        v_every=3,
        silhouette=BLACK_HEAVY,
    )
    stem = _surface_object(
        "spore-observatory-stem",
        _lathed_form(
            lambda v: 0.22 + 0.18 * math.sin(math.pi * v) ** 1.5,
            lambda v: 3.7 * (v - 0.5),
            translation=(0.28, 0.15, -0.92),
            rotation=(0.0, -8.0, -8.0),
            scale=(1.0, 1.0, 1.0),
            phase=phase,
            u_steps=30,
            v_steps=20,
        ),
        material="black-organic-observatory-stem",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(BLACK,),
        v_pens=(GREEN,),
        u_every=6,
        v_every=3,
        silhouette=BLACK_HEAVY,
    )
    buds = [
        _surface_object(
            f"spore-observatory-bud-{index}",
            _superform(
                exponents=(0.60, 0.72),
                ripple=(0.045, 4, phase + index),
                translation=(x, y, z),
                rotation=(0.0, tilt, -8.0),
                scale=(0.72, 0.58, 0.92),
                u_steps=28,
                v_steps=15,
            ),
            material="green-sensor-bud",
            lineage=lineage,
            composition_role="scale-counterpoint",
            u_pens=(GREEN,),
            v_pens=(GREY,),
            u_every=5,
            v_every=4,
            silhouette=BLACK,
        )
        for index, (x, y, z, tilt) in enumerate(
            ((-3.25, 0.65, -0.35, -18.0), (3.05, 0.85, -0.12, 21.0))
        )
    ]
    curves: list[Curve3D] = []
    for index, target in enumerate(((-3.25, 0.65, -0.35), (3.05, 0.85, -0.12))):
        t = np.linspace(0.0, 1.0, 80)
        start = np.array((0.18, 0.10, 0.05), dtype=float)
        end = np.asarray(target, dtype=float)
        control_a = start + np.array(((-1.0 if index == 0 else 1.0) * 0.8, -0.9, 1.4))
        control_b = end + np.array(((1.0 if index == 0 else -1.0) * 0.7, -0.4, 0.9))
        points = (
            ((1.0 - t) ** 3)[:, None] * start
            + (3.0 * (1.0 - t) ** 2 * t)[:, None] * control_a
            + (3.0 * (1.0 - t) * t * t)[:, None] * control_b
            + (t**3)[:, None] * end
        )
        curves.append(
            _curve(
                f"spore-observatory-tendril-{index}",
                points,
                pen_id=(GREEN if index == 0 else BLUE),
                role="bio-signal-tendril",
                object_id="spore-observatory-pod",
                material="bioluminescent-neural-tendril",
                lineage=lineage,
                composition_role="hero-integral",
            )
        )
    curves.extend(
        _backdrop_scans(
            "spore-observatory",
            x_extent=5.2,
            y=2.50,
            z_values=(-1.55, -0.65, 0.25, 1.15, 2.05, 2.95, 3.85, 4.75),
            skew=0.18,
            pen_ids=(GREY, GREEN),
            lineage=lineage,
            material="bio-observation-register",
        )
    )
    return _scene(
        "spore-observatory",
        camera=Camera3D(
            eye=(8.0, -13.2, 5.45),
            target=(0.0, 0.0, 1.25),
            fov_y_deg=37.0,
        ),
        meshes=[pod, stem, *buds],
        curves=curves,
        zoom=1.18,
        framing_offset=(0.0, 0.02),
        depth_buffer_px=800,
    )


def scene_ferroflora(seed: int) -> Scene3D:
    """Seven modeled alloy petals orbit one dense seed under a high camera."""

    lineage = "bio-cgi"
    phase = _seed_phase(seed, 173)
    petals: list[Mesh3D] = []
    petal_pens = (
        (PURPLE, BLUE),
        (BLUE, SILVER),
        (PURPLE, GREEN),
        (SILVER, BLUE),
        (PURPLE, GOLD),
        (BLUE, GREEN),
        (PURPLE, SILVER),
    )
    for index in range(7):
        angle = -9.0 + 360.0 * index / 7.0
        primary, secondary = petal_pens[index]
        petals.append(
            _surface_object(
                f"ferroflora-petal-{index}",
                _petal(
                    angle_deg=angle,
                    length=3.55 + 0.12 * math.sin(phase + index),
                    width=0.82,
                    rise=1.48 + 0.08 * math.cos(phase + index),
                    translation=(0.0, 0.0, 1.22),
                    phase=phase * 0.08 + index * 0.16,
                ),
                material=f"alloy-botanical-petal-{index}",
                lineage=lineage,
                composition_role="dominant-hero",
                u_pens=(primary,),
                v_pens=(secondary,),
                u_every=2,
                v_every=2,
                silhouette=BLACK_HEAVY,
            )
        )
    seed_core = _surface_object(
        "ferroflora-seed-core",
        _superform(
            exponents=(0.42, 0.54),
            ripple=(0.055, 9, phase),
            translation=(0.0, 0.0, 1.72),
            rotation=(0.0, 0.0, -9.0),
            scale=(1.10, 1.10, 1.25),
            u_steps=38,
            v_steps=21,
        ),
        material="black-gold-ferroflora-seed",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(BLACK, GOLD),
        v_pens=(PURPLE,),
        u_every=4,
        v_every=3,
        silhouette=BLACK_HEAVY,
    )
    stalk = _surface_object(
        "ferroflora-stalk",
        _lathed_form(
            lambda v: 0.16 + 0.10 * math.sin(math.pi * v),
            lambda v: 3.4 * (v - 0.5),
            translation=(0.0, 0.0, -1.05),
            rotation=(0.0, -6.0, -9.0),
            u_steps=26,
            v_steps=17,
        ),
        material="black-ferroflora-stalk",
        lineage=lineage,
        composition_role="hero-integral",
        u_pens=(BLACK,),
        v_pens=(GREEN,),
        u_every=7,
        v_every=4,
        silhouette=BLACK_HEAVY,
    )
    curves = [
        _orbit_curve(
            "ferroflora-growth-register",
            radii=(4.35, 4.35, 0.28),
            translation=(0.0, 0.0, 0.68),
            rotation=(0.0, 0.0, -9.0),
            pen_id=GREEN,
            material="green-growth-register",
            lineage=lineage,
            phase=phase * 0.04,
            role="botanical-growth-register",
            composition_role="support",
        )
    ]
    return _scene(
        "ferroflora",
        camera=Camera3D(
            eye=(8.6, -12.2, 10.8),
            target=(0.0, 0.0, 1.10),
            fov_y_deg=37.0,
        ),
        meshes=[*petals, seed_core, stalk],
        curves=curves,
        zoom=1.23,
        framing_offset=(0.0, 0.01),
        depth_buffer_px=820,
    )


EXPANDED_SCENE_FACTORIES: dict[str, SceneFactory] = {
    "relay-canyon": scene_relay_canyon,
    "circuit-ziggurat": scene_circuit_ziggurat,
    "vector-drydock": scene_vector_drydock,
    "phase-foundry": scene_phase_foundry,
    "orbital-medallion": scene_orbital_medallion,
    "split-shell-relic": scene_split_shell_relic,
    "turbine-seed": scene_turbine_seed,
    "prism-anchor": scene_prism_anchor,
    "signal-ram": scene_signal_ram,
    "rotor-cathedral": scene_rotor_cathedral,
    "data-crucible": scene_data_crucible,
    "nocturne-oscilloscope": scene_nocturne_oscilloscope,
    "afterimage-reactor": scene_afterimage_reactor,
    "diffraction-vault": scene_diffraction_vault,
    "liminal-horizon": scene_liminal_horizon,
    "eclipse-array": scene_eclipse_array,
    "atmospheric-reentry": scene_atmospheric_reentry,
    "spore-observatory": scene_spore_observatory,
    "ferroflora": scene_ferroflora,
}


__all__ = ["EXPANDED_SCENE_FACTORIES", "SceneFactory"]
