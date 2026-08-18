"""Art-directed 3D scene recipes for the abstract-art v2 pilot.

The scenes borrow broad production grammar from early CGI music graphics—one
dominant object, oblique cameras, engineered worlds, sparse metallic accents
and deliberate voids—without copying an existing sleeve, logo or composition.
"""

from __future__ import annotations

import math
import random
from typing import Callable, Sequence

import numpy as np
from shapely.geometry import LineString, MultiLineString, MultiPoint

from .models import MapPlotterError
from .abstract_3d_scenes_expanded import EXPANDED_SCENE_FACTORIES
from .plot3d import (
    Camera3D,
    Curve3D,
    Mesh3D,
    ParametricMesh,
    Scene3D,
    Vec3,
    box_mesh,
    curves_from_family,
    mesh_with_transform,
    parametric_mesh,
    transform_points,
)


SceneFactory = Callable[[int], Scene3D]


def _torus(
    major: float,
    minor: float,
    *,
    translation: Vec3 = (0.0, 0.0, 0.0),
    rotation: Vec3 = (0.0, 0.0, 0.0),
    scale: Vec3 = (1.0, 1.0, 1.0),
    phase: float = 0.0,
    warp: float = 0.0,
    u_steps: int = 38,
    v_steps: int = 14,
) -> ParametricMesh:
    def surface(u: float, v: float) -> tuple[float, float, float]:
        radial = major + minor * math.cos(v)
        warped_minor = minor * math.sin(v) * (1.0 + warp * math.sin(3.0 * u + phase))
        radial += warp * minor * 0.55 * math.sin(2.0 * u - phase)
        return (
            radial * math.cos(u),
            radial * math.sin(u),
            warped_minor,
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
            curve_samples=110,
        ),
        translation=translation,
        rotation_deg=rotation,
        scale=scale,
    )


def _superform(
    *,
    exponents: tuple[float, float] = (0.7, 0.7),
    ripple: tuple[float, int] = (0.0, 3),
    translation: Vec3 = (0.0, 0.0, 0.0),
    rotation: Vec3 = (0.0, 0.0, 0.0),
    scale: Vec3 = (1.0, 1.0, 1.0),
    u_steps: int = 42,
    v_steps: int = 21,
) -> ParametricMesh:
    longitude_exp, latitude_exp = exponents
    amplitude, frequency = ripple

    def signed_power(value: float, exponent: float) -> float:
        return math.copysign(abs(value) ** exponent, value)

    def surface(u: float, v: float) -> tuple[float, float, float]:
        longitude = signed_power(math.cos(u), longitude_exp)
        side = signed_power(math.sin(u), longitude_exp)
        latitude = signed_power(math.cos(v), latitude_exp)
        height = signed_power(math.sin(v), latitude_exp)
        radius = 1.0 + amplitude * math.sin(frequency * u + 1.7 * v)
        return (radius * latitude * longitude, radius * latitude * side, height)

    return mesh_with_transform(
        parametric_mesh(
            surface,
            u_range=(0.0, 2.0 * math.pi),
            v_range=(-math.pi / 2 + 0.035, math.pi / 2 - 0.035),
            u_steps=u_steps,
            v_steps=v_steps,
            wrap_u=True,
            wrap_v=False,
            curve_samples=120,
        ),
        translation=translation,
        rotation_deg=rotation,
        scale=scale,
    )


def _surface_object(
    object_id: str,
    surface: ParametricMesh,
    *,
    u_pens: Sequence[str] = (),
    v_pens: Sequence[str] = (),
    u_every: int = 3,
    v_every: int = 3,
    silhouette: str | None = "black-0-6",
    crease: str | None = None,
    material: str,
) -> Mesh3D:
    attributes = {
        "data-material": material,
        "data-geometry-source": "project-authored-triangle-mesh",
    }
    curves: list[Curve3D] = []
    if u_pens:
        curves.extend(
            curves_from_family(
                object_id=object_id,
                family=surface.u_curves,
                pen_ids=u_pens,
                role="surface-longitude",
                prefix=f"{object_id}-longitude",
                every=u_every,
                attributes=attributes,
            )
        )
    if v_pens:
        curves.extend(
            curves_from_family(
                object_id=object_id,
                family=surface.v_curves,
                pen_ids=v_pens,
                role="surface-latitude",
                prefix=f"{object_id}-latitude",
                every=v_every,
                attributes=attributes,
            )
        )
    return Mesh3D(
        object_id,
        surface.vertices,
        surface.faces,
        curves,
        silhouette,
        crease,
        44.0,
        attributes,
    )


def _box_object(
    object_id: str,
    *,
    size: Vec3,
    translation: Vec3,
    rotation: Vec3,
    silhouette: str = "black-0-6",
    crease: str = "black-0-4",
    material: str = "industrial-slab",
) -> Mesh3D:
    vertices, faces, _ = box_mesh(
        size=size, translation=translation, rotation_deg=rotation
    )
    return Mesh3D(
        object_id,
        vertices,
        faces,
        [],
        silhouette,
        crease,
        35.0,
        {
            "data-material": material,
            "data-geometry-source": "project-authored-triangle-mesh",
        },
    )


def _ground_grid(
    *,
    extent: float,
    step: float,
    z: float,
    pen_id: str,
    rotation_deg: float = 0.0,
    omit_centre: bool = False,
) -> list[Curve3D]:
    rotation = math.radians(rotation_deg)
    cosine, sine = math.cos(rotation), math.sin(rotation)

    def rotate(points: np.ndarray) -> np.ndarray:
        x = points[:, 0] * cosine - points[:, 1] * sine
        y = points[:, 0] * sine + points[:, 1] * cosine
        return np.column_stack((x, y, points[:, 2]))

    values = np.arange(-extent, extent + step * 0.5, step)
    curves: list[Curve3D] = []
    for index, value in enumerate(values):
        if omit_centre and abs(float(value)) < step * 0.25:
            continue
        horizontal = np.array(((-extent, value, z), (extent, value, z)), dtype=float)
        vertical = np.array(((value, -extent, z), (value, extent, z)), dtype=float)
        for axis, points in (("x", horizontal), ("y", vertical)):
            curves.append(
                Curve3D(
                    f"ground-{axis}-{index:03d}",
                    rotate(points),
                    pen_id,
                    "perspective-ground-grid",
                    "ground-plane",
                    True,
                    -0.015,
                    {
                        "data-material": "unfilled-perspective-grid",
                        "data-ground-z": f"{z:g}",
                    },
                )
            )
    return curves


def _orbit_curve(
    object_id: str,
    curve_id: str,
    *,
    radii: Vec3,
    translation: Vec3,
    rotation: Vec3,
    pen_id: str,
    phase: float = 0.0,
    role: str = "orbital-path",
) -> Curve3D:
    values = np.linspace(0.0, 2.0 * math.pi, 181)
    points = np.column_stack(
        (
            radii[0] * np.cos(values + phase),
            radii[1] * np.sin(values + phase),
            radii[2] * np.sin(2.0 * values + phase),
        )
    )
    points = transform_points(points, translation=translation, rotation_deg=rotation)
    return Curve3D(
        curve_id,
        points,
        pen_id,
        role,
        object_id,
        True,
        -0.018,
        {"data-material": "orbital-signal"},
    )


def _lit_face_hatching(
    mesh: Mesh3D,
    *,
    light_from: Vec3 = (-0.7, -0.45, 1.0),
    shadow_pen: str,
    mid_pen: str,
    highlight_pen: str | None = None,
    maximum_lines: int = 4,
) -> list[Curve3D]:
    """Create object-space facet hatching whose density follows real normals."""

    light = np.asarray(light_from, dtype=float)
    light /= np.linalg.norm(light)
    triangles = mesh.vertices[mesh.faces]
    result: list[Curve3D] = []
    for face_index, triangle in enumerate(triangles):
        normal = np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])
        length = float(np.linalg.norm(normal))
        if length <= 1e-10:
            continue
        normal /= length
        illumination = max(0.0, float(np.dot(normal, light)))
        darkness = 1.0 - illumination
        line_count = max(0, min(maximum_lines, int(round(darkness * maximum_lines))))
        if line_count == 0 and highlight_pen is not None and illumination > 0.78:
            line_count = 1
        if line_count == 0:
            continue
        if illumination > 0.72 and highlight_pen is not None:
            pen_id = highlight_pen
        elif illumination > 0.34:
            pen_id = mid_pen
        else:
            pen_id = shadow_pen
        for hatch_index in range(line_count):
            fraction = (hatch_index + 1.0) / (line_count + 1.7)
            start = triangle[0] * (1.0 - fraction) + triangle[1] * fraction
            end = triangle[0] * (1.0 - fraction) + triangle[2] * fraction
            result.append(
                Curve3D(
                    f"{mesh.id}-light-hatch-{face_index:04d}-{hatch_index:02d}",
                    np.vstack((start, end)),
                    pen_id,
                    "light-derived-facet-hatch",
                    mesh.id,
                    True,
                    -0.028,
                    {
                        "data-material": mesh.attributes.get(
                            "data-material", "faceted-solid"
                        ),
                        "data-light-model": "directional-lambert-density",
                        "data-light-intensity": f"{illumination:.5f}",
                    },
                )
            )
    return result


def _cast_shadow(
    meshes: Sequence[Mesh3D],
    *,
    ground_z: float,
    light_ray: Vec3,
    pen_id: str,
    hatch_spacing: float = 0.24,
    hatch_angle_deg: float = -18.0,
) -> list[Curve3D]:
    """Project mesh vertices along a light ray and hatch the ground footprint."""

    ray = np.asarray(light_ray, dtype=float)
    if ray[2] >= -1e-6:
        raise MapPlotterError("Cast-shadow light ray must point toward the ground.")
    projected: list[tuple[float, float]] = []
    for mesh in meshes:
        for vertex in mesh.vertices:
            amount = (ground_z - float(vertex[2])) / float(ray[2])
            point = vertex + ray * amount
            projected.append((float(point[0]), float(point[1])))
    polygon = MultiPoint(projected).convex_hull
    if polygon.is_empty or polygon.area <= 1e-9:
        return []
    angle = math.radians(hatch_angle_deg)
    direction = np.array((math.cos(angle), math.sin(angle)), dtype=float)
    normal = np.array((-direction[1], direction[0]), dtype=float)
    coordinates = np.asarray(polygon.exterior.coords, dtype=float)
    offsets = coordinates @ normal
    diagonal = (
        math.hypot(
            polygon.bounds[2] - polygon.bounds[0], polygon.bounds[3] - polygon.bounds[1]
        )
        * 1.5
    )
    value = math.floor(float(offsets.min()) / hatch_spacing) * hatch_spacing
    result: list[Curve3D] = []
    index = 0
    while value <= float(offsets.max()) + hatch_spacing:
        centre = normal * value
        first = centre - direction * diagonal
        second = centre + direction * diagonal
        clipped = LineString((first, second)).intersection(polygon)
        parts = (
            [clipped]
            if isinstance(clipped, LineString)
            else list(clipped.geoms)
            if isinstance(clipped, MultiLineString)
            else []
        )
        for part in parts:
            points = np.array(
                [(float(x), float(y), ground_z) for x, y in part.coords], dtype=float
            )
            if len(points) >= 2:
                result.append(
                    Curve3D(
                        f"cast-shadow-hatch-{index:03d}",
                        points,
                        pen_id,
                        "cast-shadow-hatch",
                        "cast-shadow",
                        True,
                        -0.03,
                        {
                            "data-light-model": "directional-ground-projection",
                            "data-ground-z": f"{ground_z:g}",
                        },
                    )
                )
                index += 1
        value += hatch_spacing
    boundary = np.array(
        [(float(x), float(y), ground_z) for x, y in polygon.exterior.coords],
        dtype=float,
    )
    result.append(
        Curve3D(
            "cast-shadow-boundary",
            boundary,
            pen_id,
            "cast-shadow-boundary",
            "cast-shadow",
            True,
            -0.03,
            {"data-light-model": "directional-ground-projection"},
        )
    )
    return result


def _chrome_pressure(seed: int) -> Scene3D:
    rng = random.Random(seed)
    outer = _torus(
        2.15,
        0.62,
        rotation=(58.0, 10.0, -22.0),
        warp=0.08,
        phase=rng.random() * math.pi,
    )
    cross = _torus(
        1.42,
        0.28,
        translation=(0.45, 0.15, 0.2),
        rotation=(18.0, 72.0, 14.0),
        warp=0.04,
    )
    core = _superform(
        exponents=(0.62, 0.74),
        ripple=(0.06, 5),
        translation=(-0.25, 0.0, 0.05),
        rotation=(0.0, 14.0, -16.0),
        scale=(0.95, 0.82, 1.2),
    )
    meshes = [
        _surface_object(
            "pressure-ring",
            outer,
            u_pens=("blue-0-25",),
            v_pens=("silver-1",),
            u_every=4,
            v_every=4,
            silhouette="black-1",
            material="ice-chrome",
        ),
        _surface_object(
            "cross-ring",
            cross,
            u_pens=("purple-0-25",),
            v_pens=("blue-0-25",),
            u_every=5,
            v_every=4,
            material="violet-alloy",
        ),
        _surface_object(
            "pressure-core",
            core,
            u_pens=("blue-0-25",),
            v_pens=("purple-0-25",),
            u_every=3,
            v_every=3,
            material="compressed-signal",
        ),
    ]
    curves = _ground_grid(
        extent=4.2,
        step=0.52,
        z=-1.55,
        pen_id="grey-0-25",
        rotation_deg=-12.0,
    )
    curves.extend(
        [
            _orbit_curve(
                "pressure-system",
                "wide-orbit",
                radii=(3.4, 2.6, 0.35),
                translation=(0.0, 0.0, 0.15),
                rotation=(20.0, -12.0, 8.0),
                pen_id="gold-1",
            )
        ]
    )
    curves.extend(
        _cast_shadow(
            meshes,
            ground_z=-1.55,
            light_ray=(0.72, -0.58, -1.7),
            pen_id="grey-0-25",
            hatch_spacing=0.28,
            hatch_angle_deg=-24.0,
        )
    )
    return Scene3D(
        "chrome-pressure",
        Camera3D((7.8, -9.6, 6.6), (0.0, 0.0, 0.05), fov_y_deg=35.0),
        meshes,
        curves,
        zoom=1.14,
        framing_offset=(-0.04, 0.015),
        crop_intent="intentional-crop",
    )


def _torque_monolith(seed: int) -> Scene3D:
    rng = random.Random(seed)
    rotation = (0.0, -7.0, -13.0)
    meshes: list[Mesh3D] = [
        _box_object(
            "monolith-core",
            size=(2.15, 1.45, 5.7),
            translation=(0.55, 0.3, 1.25),
            rotation=rotation,
            silhouette="black-1",
            material="black-ceramic-monolith",
        )
    ]
    for index in range(11):
        z = -1.18 + index * 0.5
        width = 2.55 + 0.18 * math.sin(index * 1.7)
        depth = 1.82 + 0.08 * math.cos(index * 1.3)
        meshes.append(
            _box_object(
                f"monolith-rib-{index:02d}",
                size=(width, depth, 0.17),
                translation=(0.55, 0.3, z),
                rotation=rotation,
                silhouette="purple-0-4" if index in {2, 7} else "black-0-4",
                crease="blue-0-25",
                material="ribbed-torque-shell",
            )
        )
    portal = _torus(
        2.15,
        0.18,
        translation=(-0.8, 0.8, 1.55),
        rotation=(80.0, 18.0, 38.0),
        warp=0.03,
    )
    meshes.append(
        _surface_object(
            "monolith-portal",
            portal,
            u_pens=("gold-1",),
            u_every=5,
            v_pens=("red-0-25",),
            v_every=5,
            silhouette="black-1",
            material="hazard-metal",
        )
    )
    curves = _ground_grid(
        extent=5.2,
        step=0.62,
        z=-1.72,
        pen_id="grey-0-25",
        rotation_deg=-13.0,
    )
    for mesh in meshes[:-1]:
        curves.extend(
            _lit_face_hatching(
                mesh,
                shadow_pen="black-0-4",
                mid_pen="blue-0-25",
                maximum_lines=2,
            )
        )
    curves.extend(
        _cast_shadow(
            meshes,
            ground_z=-1.72,
            light_ray=(0.9, -0.45, -1.6),
            pen_id="purple-0-4",
            hatch_spacing=0.24,
            hatch_angle_deg=-11.0,
        )
    )
    origin = np.array((-3.8, -1.8, -1.7), dtype=float)
    for index in range(9):
        angle = -0.16 + index * 0.055 + rng.uniform(-0.006, 0.006)
        target = np.array((4.7, -1.8 + 8.0 * angle, -1.7), dtype=float)
        curves.append(
            Curve3D(
                f"hazard-ray-{index:02d}",
                np.vstack((origin, target)),
                "gold-1" if index % 2 == 0 else "red-0-25",
                "hazard-direction",
                "hazard-plane",
                True,
                -0.02,
                {"data-material": "directional-hazard-field"},
            )
        )
    return Scene3D(
        "torque-monolith",
        Camera3D((8.8, -11.4, 4.8), (0.3, 0.3, 1.1), fov_y_deg=32.0),
        meshes,
        curves,
        zoom=1.22,
        framing_offset=(0.055, 0.035),
        crop_intent="intentional-crop",
    )


def _velocity_tunnel(seed: int) -> Scene3D:
    rng = random.Random(seed)
    meshes: list[Mesh3D] = []
    for index in range(9):
        distance = index * 1.5
        radius = 2.65 - index * 0.075
        ring = _torus(
            radius,
            0.18 + 0.025 * (index % 3),
            translation=(
                0.18 * math.sin(index),
                distance,
                0.15 * math.cos(index * 0.8),
            ),
            rotation=(90.0, 0.0, rng.uniform(-7.0, 7.0)),
            u_steps=34,
            v_steps=10,
        )
        meshes.append(
            _surface_object(
                f"tunnel-ring-{index:02d}",
                ring,
                u_pens=("blue-0-25",) if index % 2 else ("silver-1",),
                v_pens=("purple-0-25",),
                u_every=6,
                v_every=5,
                silhouette="black-1" if index < 3 else "blue-0-4",
                material="velocity-ring",
            )
        )
    curves: list[Curve3D] = []
    for rail_index, angle in enumerate(
        np.linspace(0.0, 2.0 * math.pi, 12, endpoint=False)
    ):
        values = np.linspace(-0.2, 12.4, 180)
        radius = 2.62 - 0.012 * values
        points = np.column_stack(
            (
                radius * math.cos(float(angle)) + 0.12 * np.sin(values * 0.8),
                values,
                radius * math.sin(float(angle)) + 0.08 * np.cos(values),
            )
        )
        curves.append(
            Curve3D(
                f"tunnel-rail-{rail_index:02d}",
                points,
                "green-0-25" if rail_index % 3 == 0 else "grey-0-25",
                "depth-rail",
                "tunnel-rails",
                True,
                -0.02,
                {"data-material": "receding-signal-rail"},
            )
        )
    return Scene3D(
        "velocity-tunnel",
        Camera3D(
            (0.1, -5.6, 0.45), (0.0, 6.0, 0.0), up=(0.0, 0.0, 1.0), fov_y_deg=55.0
        ),
        meshes,
        curves,
        zoom=1.25,
        framing_offset=(0.0, 0.0),
        crop_intent="intentional-crop",
    )


def _liquid_alloy(seed: int) -> Scene3D:
    rng = random.Random(seed)
    body = _superform(
        exponents=(0.46, 0.58),
        ripple=(0.16, 5),
        translation=(0.0, 0.0, 0.35),
        rotation=(17.0, -20.0, 28.0),
        scale=(2.1, 1.55, 2.65),
        u_steps=48,
        v_steps=25,
    )
    shell = _surface_object(
        "liquid-alloy-body",
        body,
        u_pens=("blue-0-25", "purple-0-25"),
        v_pens=("silver-1",),
        u_every=2,
        v_every=3,
        silhouette="black-1",
        material="liquid-chrome",
    )
    belt_one = _torus(
        2.05,
        0.13,
        translation=(0.0, 0.0, 0.35),
        rotation=(72.0, 8.0, 26.0),
        scale=(1.0, 0.72, 1.0),
    )
    belt_two = _torus(
        1.63,
        0.1,
        translation=(0.0, 0.0, 0.4),
        rotation=(12.0, 66.0, -24.0),
        scale=(1.0, 0.88, 1.0),
    )
    meshes = [
        shell,
        _surface_object(
            "alloy-belt-a",
            belt_one,
            u_pens=("gold-1",),
            v_pens=("red-0-25",),
            u_every=6,
            v_every=5,
            silhouette="black-1",
            material="warm-highlight-band",
        ),
        _surface_object(
            "alloy-belt-b",
            belt_two,
            u_pens=("purple-0-25",),
            v_pens=("blue-0-25",),
            u_every=5,
            v_every=5,
            silhouette="black-1",
            material="cold-highlight-band",
        ),
    ]
    curves = _cast_shadow(
        meshes,
        ground_z=-2.22,
        light_ray=(0.68 + rng.uniform(-0.02, 0.02), -0.52, -1.65),
        pen_id="grey-0-25",
        hatch_spacing=0.2,
        hatch_angle_deg=-24.0,
    )
    return Scene3D(
        "liquid-alloy",
        Camera3D((7.4, -9.8, 6.9), (0.0, 0.0, 0.2), fov_y_deg=34.0),
        meshes,
        curves,
        zoom=1.17,
        framing_offset=(-0.035, 0.015),
        crop_intent="intentional-crop",
    )


def _hardstep_array(seed: int) -> Scene3D:
    rng = random.Random(seed)
    meshes: list[Mesh3D] = []
    for depth_index in range(3):
        for row in range(4):
            for column in range(5):
                if (row * 7 + column * 3 + depth_index * 5 + seed) % 6 in {0, 1}:
                    continue
                jitter = rng.uniform(-0.08, 0.08)
                meshes.append(
                    _box_object(
                        f"voxel-{depth_index}-{row}-{column}",
                        size=(0.72, 0.72, 0.72 + 0.18 * ((row + column) % 2)),
                        translation=(
                            (column - 2) * 0.92,
                            depth_index * 1.12 - 0.75,
                            (row - 1.5) * 0.9 + jitter,
                        ),
                        rotation=(0.0, 0.0, -8.0 + depth_index * 3.0),
                        silhouette="black-1",
                        crease=(
                            "green-0-25"
                            if (row + column + depth_index) % 4 == 0
                            else "blue-0-25"
                        ),
                        material="hardstep-voxel",
                    )
                )
    gate = _torus(
        2.35,
        0.22,
        translation=(0.1, 0.4, 0.0),
        rotation=(18.0, 78.0, 10.0),
        scale=(1.0, 0.86, 1.0),
        u_steps=36,
        v_steps=11,
    )
    meshes.append(
        _surface_object(
            "array-gate",
            gate,
            u_pens=("gold-1",),
            v_pens=("purple-0-25",),
            u_every=5,
            v_every=5,
            silhouette="black-1",
            material="array-gate-metal",
        )
    )
    curves = _ground_grid(
        extent=4.7,
        step=0.58,
        z=-2.05,
        pen_id="grey-0-25",
        rotation_deg=-8.0,
    )
    for mesh in meshes[:-1]:
        curves.extend(
            _lit_face_hatching(
                mesh,
                shadow_pen="black-0-4",
                mid_pen="green-0-25",
                highlight_pen="blue-0-25",
                maximum_lines=2,
            )
        )
    curves.extend(
        _cast_shadow(
            meshes,
            ground_z=-2.05,
            light_ray=(0.78, -0.42, -1.55),
            pen_id="grey-0-25",
            hatch_spacing=0.24,
            hatch_angle_deg=-8.0,
        )
    )
    return Scene3D(
        "hardstep-array",
        Camera3D((8.7, -11.2, 6.4), (0.0, 0.15, -0.05), fov_y_deg=39.0),
        meshes,
        curves,
        zoom=1.11,
        framing_offset=(0.015, 0.0),
        crop_intent="contained",
    )


_ICO_FACES = np.array(
    [
        (0, 11, 5),
        (0, 5, 1),
        (0, 1, 7),
        (0, 7, 10),
        (0, 10, 11),
        (1, 5, 9),
        (5, 11, 4),
        (11, 10, 2),
        (10, 7, 6),
        (7, 1, 8),
        (3, 9, 4),
        (3, 4, 2),
        (3, 2, 6),
        (3, 6, 8),
        (3, 8, 9),
        (4, 9, 5),
        (2, 4, 11),
        (6, 2, 10),
        (8, 6, 7),
        (9, 8, 1),
    ],
    dtype=np.int64,
)


def _spiked_icosahedron(
    *, translation: Vec3, rotation: Vec3, scale: Vec3
) -> tuple[np.ndarray, np.ndarray]:
    phi = (1.0 + math.sqrt(5.0)) / 2.0
    base = np.array(
        [
            (-1, phi, 0),
            (1, phi, 0),
            (-1, -phi, 0),
            (1, -phi, 0),
            (0, -1, phi),
            (0, 1, phi),
            (0, -1, -phi),
            (0, 1, -phi),
            (phi, 0, -1),
            (phi, 0, 1),
            (-phi, 0, -1),
            (-phi, 0, 1),
        ],
        dtype=float,
    )
    base /= np.linalg.norm(base[0])
    vertices = list(base)
    faces: list[tuple[int, int, int]] = []
    for face in _ICO_FACES:
        centre = base[face].mean(axis=0)
        apex = centre / np.linalg.norm(centre) * 1.68
        apex_index = len(vertices)
        vertices.append(apex)
        a, b, c = (int(value) for value in face)
        faces.extend(((a, b, apex_index), (b, c, apex_index), (c, a, apex_index)))
    transformed = transform_points(
        np.asarray(vertices),
        translation=translation,
        rotation_deg=rotation,
        scale=scale,
    )
    return transformed, np.asarray(faces, dtype=np.int64)


def _chrome_bloom(seed: int) -> Scene3D:
    rng = random.Random(seed)
    vertices, faces = _spiked_icosahedron(
        translation=(0.0, 0.0, 0.35),
        rotation=(18.0, -12.0, 24.0),
        scale=(2.05, 2.05, 2.05),
    )
    meshes: list[Mesh3D] = [
        Mesh3D(
            "bloom-core",
            vertices,
            faces,
            [],
            "black-1",
            "red-0-25",
            24.0,
            {
                "data-material": "faceted-bloom-core",
                "data-geometry-source": "project-authored-spiked-icosahedron",
            },
        )
    ]
    for index, (radius, rotation) in enumerate(
        (
            (2.75, (68.0, 12.0, 5.0)),
            (3.15, (8.0, 72.0, -18.0)),
            (3.5, (42.0, 38.0, 38.0)),
        )
    ):
        ring = _torus(
            radius,
            0.105 + index * 0.025,
            translation=(0.0, 0.0, 0.35),
            rotation=rotation,
            u_steps=38,
            v_steps=10,
        )
        meshes.append(
            _surface_object(
                f"bloom-orbit-{index}",
                ring,
                u_pens=(("gold-1",) if index == 1 else ("blue-0-25",)),
                v_pens=("purple-0-25",),
                u_every=7,
                v_every=5,
                silhouette="black-1",
                material="orbital-bloom-ring",
            )
        )
    curves: list[Curve3D] = []
    curves.extend(
        _lit_face_hatching(
            meshes[0],
            shadow_pen="black-0-6",
            mid_pen="red-0-25",
            highlight_pen="gold-1",
            maximum_lines=5,
        )
    )
    for index in range(18):
        direction = vertices[index % len(vertices)] - np.array((0.0, 0.0, 0.35))
        direction /= np.linalg.norm(direction)
        start = np.array((0.0, 0.0, 0.35)) + direction * 2.05
        end = np.array((0.0, 0.0, 0.35)) + direction * (4.0 + rng.uniform(-0.25, 0.35))
        curves.append(
            Curve3D(
                f"bloom-ray-{index:02d}",
                np.vstack((start, end)),
                "purple-0-25" if index % 3 else "gold-1",
                "radial-signal-ray",
                "bloom-rays",
                True,
                -0.02,
                {"data-material": "radial-energy"},
            )
        )
    return Scene3D(
        "chrome-bloom",
        Camera3D((8.5, -10.8, 7.1), (0.0, 0.0, 0.3), fov_y_deg=35.0),
        meshes,
        curves,
        zoom=1.16,
        framing_offset=(-0.025, 0.0),
        crop_intent="intentional-crop",
    )


PILOT_SCENES: dict[str, SceneFactory] = {
    "chrome-pressure": _chrome_pressure,
    "torque-monolith": _torque_monolith,
    "velocity-tunnel": _velocity_tunnel,
    "liquid-alloy": _liquid_alloy,
    "hardstep-array": _hardstep_array,
    "chrome-bloom": _chrome_bloom,
}

ALL_SCENES: dict[str, SceneFactory] = {
    **PILOT_SCENES,
    **EXPANDED_SCENE_FACTORIES,
}


def build_scene(scene_id: str, seed: int) -> Scene3D:
    try:
        factory = ALL_SCENES[scene_id]
    except KeyError as exc:
        raise MapPlotterError(f"Unknown abstract 3D scene {scene_id!r}.") from exc
    return factory(seed)
