"""Deterministic perspective and hidden-line rendering for physical pen plots.

This module is deliberately small enough to audit.  It does not trace a
raster image: triangle meshes are projected through a real look-at camera,
their faces populate a transient perspective-correct depth buffer, and native
3D curves are split into the portions that remain visible.  Only those vector
fragments leave the renderer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any, Callable, Iterable, Sequence

import numpy as np
from numpy.typing import NDArray

from .models import MapPlotterError
from .niche_common import Point, Rect, polyline_length_mm


FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]
Vec3 = tuple[float, float, float]


def _finite_array(value: Any, shape_tail: tuple[int, ...], label: str) -> FloatArray:
    result = np.asarray(value, dtype=np.float64)
    if result.ndim < len(shape_tail) or result.shape[-len(shape_tail) :] != shape_tail:
        raise MapPlotterError(f"{label} must end with shape {shape_tail}.")
    if not np.isfinite(result).all():
        raise MapPlotterError(f"{label} contains a non-finite coordinate.")
    return result


def _unit(vector: FloatArray, label: str) -> FloatArray:
    length = float(np.linalg.norm(vector))
    if not math.isfinite(length) or length <= 1e-12:
        raise MapPlotterError(f"{label} must be non-zero.")
    return vector / length


def _rotation_matrix(rotation_deg: Vec3) -> FloatArray:
    rx, ry, rz = (math.radians(float(value)) for value in rotation_deg)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    matrix_x = np.array(((1, 0, 0), (0, cx, -sx), (0, sx, cx)), dtype=float)
    matrix_y = np.array(((cy, 0, sy), (0, 1, 0), (-sy, 0, cy)), dtype=float)
    matrix_z = np.array(((cz, -sz, 0), (sz, cz, 0), (0, 0, 1)), dtype=float)
    return matrix_z @ matrix_y @ matrix_x


def transform_points(
    points: FloatArray | Sequence[Sequence[float]],
    *,
    translation: Vec3 = (0.0, 0.0, 0.0),
    rotation_deg: Vec3 = (0.0, 0.0, 0.0),
    scale: Vec3 = (1.0, 1.0, 1.0),
) -> FloatArray:
    """Apply a scale, Euler rotation and translation to 3D points."""

    values = _finite_array(points, (3,), "3D points")
    scaled = values * np.asarray(scale, dtype=float)
    return scaled @ _rotation_matrix(rotation_deg).T + np.asarray(
        translation, dtype=float
    )


@dataclass(frozen=True)
class Camera3D:
    eye: Vec3
    target: Vec3
    up: Vec3 = (0.0, 0.0, 1.0)
    fov_y_deg: float = 42.0
    near: float = 0.05
    far: float = 1000.0
    lens_shift: tuple[float, float] = (0.0, 0.0)

    def validated(self) -> Camera3D:
        eye = _finite_array(self.eye, (3,), "camera.eye")
        target = _finite_array(self.target, (3,), "camera.target")
        up = _finite_array(self.up, (3,), "camera.up")
        forward = _unit(target - eye, "camera view direction")
        _unit(np.cross(forward, up), "camera right vector")
        if not 5.0 <= float(self.fov_y_deg) <= 150.0:
            raise MapPlotterError("camera.fov_y_deg must be between 5 and 150.")
        if not 0.0 < float(self.near) < float(self.far):
            raise MapPlotterError("camera near/far planes are invalid.")
        if any(not math.isfinite(float(value)) for value in self.lens_shift):
            raise MapPlotterError("camera lens shift must be finite.")
        return self


@dataclass
class Curve3D:
    id: str
    points: FloatArray
    pen_id: str
    role: str
    object_id: str
    occluded: bool = True
    depth_bias: float = -0.01
    attributes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.points = _finite_array(self.points, (3,), f"curve {self.id!r}")
        if self.points.ndim != 2 or len(self.points) < 2:
            raise MapPlotterError(f"Curve {self.id!r} needs at least two 3D points.")


@dataclass
class Mesh3D:
    id: str
    vertices: FloatArray
    faces: IntArray
    curves: list[Curve3D] = field(default_factory=list)
    silhouette_pen_id: str | None = "black-0-6"
    crease_pen_id: str | None = None
    crease_angle_deg: float = 48.0
    attributes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.vertices = _finite_array(self.vertices, (3,), f"mesh {self.id!r} vertices")
        self.faces = np.asarray(self.faces, dtype=np.int64)
        if self.vertices.ndim != 2 or len(self.vertices) < 3:
            raise MapPlotterError(f"Mesh {self.id!r} needs at least three vertices.")
        if self.faces.ndim != 2 or self.faces.shape[1] != 3 or len(self.faces) < 1:
            raise MapPlotterError(f"Mesh {self.id!r} needs triangular faces.")
        if int(self.faces.min()) < 0 or int(self.faces.max()) >= len(self.vertices):
            raise MapPlotterError(f"Mesh {self.id!r} has an invalid face index.")
        triangles = self.vertices[self.faces]
        areas = np.linalg.norm(
            np.cross(
                triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
            ),
            axis=1,
        )
        if np.count_nonzero(areas > 1e-10) < max(1, int(0.98 * len(areas))):
            raise MapPlotterError(f"Mesh {self.id!r} has excessive degenerate faces.")


@dataclass
class Scene3D:
    id: str
    camera: Camera3D
    meshes: list[Mesh3D]
    curves: list[Curve3D] = field(default_factory=list)
    zoom: float = 1.0
    framing_offset: tuple[float, float] = (0.0, 0.0)
    depth_buffer_px: int = 1800
    crop_intent: str = "contained"

    def __post_init__(self) -> None:
        self.camera.validated()
        if not self.meshes:
            raise MapPlotterError(f"3D scene {self.id!r} has no triangle mesh.")
        if not 0.5 <= float(self.zoom) <= 2.5:
            raise MapPlotterError("Scene zoom must be between 0.5 and 2.5.")
        if not 320 <= int(self.depth_buffer_px) <= 2400:
            raise MapPlotterError("Scene depth-buffer width must be 320..2400 px.")
        if self.crop_intent not in {"contained", "intentional-crop"}:
            raise MapPlotterError("Scene crop_intent is invalid.")


@dataclass(frozen=True)
class RenderedCurve:
    id: str
    points: tuple[Point, ...]
    pen_id: str
    role: str
    object_id: str
    mean_depth: float
    attributes: dict[str, str]


@dataclass(frozen=True)
class RenderStats:
    object_count: int
    vertex_count: int
    triangle_count: int
    candidate_curve_count: int
    candidate_sample_count: int
    visible_sample_count: int
    occluded_sample_count: int
    clipped_sample_count: int
    visible_fragment_count: int
    depth_min: float
    depth_max: float
    depth_span: float
    projected_occupancy: float
    depth_buffer_width_px: int
    depth_buffer_height_px: int
    crop_intent: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "object_count": self.object_count,
            "vertex_count": self.vertex_count,
            "triangle_count": self.triangle_count,
            "candidate_curve_count": self.candidate_curve_count,
            "candidate_sample_count": self.candidate_sample_count,
            "visible_sample_count": self.visible_sample_count,
            "occluded_sample_count": self.occluded_sample_count,
            "clipped_sample_count": self.clipped_sample_count,
            "visible_fragment_count": self.visible_fragment_count,
            "depth_min": round(self.depth_min, 6),
            "depth_max": round(self.depth_max, 6),
            "depth_span": round(self.depth_span, 6),
            "projected_occupancy": round(self.projected_occupancy, 6),
            "depth_buffer_width_px": self.depth_buffer_width_px,
            "depth_buffer_height_px": self.depth_buffer_height_px,
            "crop_intent": self.crop_intent,
        }


@dataclass(frozen=True)
class RenderedScene:
    curves: tuple[RenderedCurve, ...]
    stats: RenderStats
    camera_matrix: tuple[tuple[float, ...], ...]
    scene_sha256: str


@dataclass(frozen=True)
class ParametricMesh:
    vertices: FloatArray
    faces: IntArray
    u_curves: tuple[FloatArray, ...]
    v_curves: tuple[FloatArray, ...]


def parametric_mesh(
    function: Callable[[float, float], Sequence[float]],
    *,
    u_range: tuple[float, float],
    v_range: tuple[float, float],
    u_steps: int,
    v_steps: int,
    wrap_u: bool = False,
    wrap_v: bool = False,
    curve_samples: int = 120,
) -> ParametricMesh:
    """Triangulate a parametric surface and retain its native isolines."""

    if u_steps < 3 or v_steps < 3 or curve_samples < 8:
        raise MapPlotterError("Parametric mesh resolution is too low.")
    us = np.linspace(u_range[0], u_range[1], u_steps, endpoint=not wrap_u)
    vs = np.linspace(v_range[0], v_range[1], v_steps, endpoint=not wrap_v)
    vertices = np.array([function(float(u), float(v)) for u in us for v in vs])
    vertices = _finite_array(vertices, (3,), "parametric surface")
    faces: list[tuple[int, int, int]] = []
    u_cells = u_steps if wrap_u else u_steps - 1
    v_cells = v_steps if wrap_v else v_steps - 1
    for ui in range(u_cells):
        next_u = (ui + 1) % u_steps
        for vi in range(v_cells):
            next_v = (vi + 1) % v_steps
            a = ui * v_steps + vi
            b = next_u * v_steps + vi
            c = next_u * v_steps + next_v
            d = ui * v_steps + next_v
            faces.extend(((a, b, c), (a, c, d)))
    u_sample = np.linspace(u_range[0], u_range[1], curve_samples, endpoint=not wrap_u)
    v_sample = np.linspace(v_range[0], v_range[1], curve_samples, endpoint=not wrap_v)
    u_curves: list[FloatArray] = []
    for u in us:
        curve = np.array([function(float(u), float(v)) for v in v_sample])
        if wrap_v:
            curve = np.vstack((curve, curve[0]))
        u_curves.append(curve)
    v_curves: list[FloatArray] = []
    for v in vs:
        curve = np.array([function(float(u), float(v)) for u in u_sample])
        if wrap_u:
            curve = np.vstack((curve, curve[0]))
        v_curves.append(curve)
    return ParametricMesh(
        vertices,
        np.asarray(faces, dtype=np.int64),
        tuple(u_curves),
        tuple(v_curves),
    )


def mesh_with_transform(
    mesh: ParametricMesh,
    *,
    translation: Vec3 = (0.0, 0.0, 0.0),
    rotation_deg: Vec3 = (0.0, 0.0, 0.0),
    scale: Vec3 = (1.0, 1.0, 1.0),
) -> ParametricMesh:
    transform = lambda values: transform_points(  # noqa: E731
        values,
        translation=translation,
        rotation_deg=rotation_deg,
        scale=scale,
    )
    return ParametricMesh(
        transform(mesh.vertices),
        mesh.faces.copy(),
        tuple(transform(curve) for curve in mesh.u_curves),
        tuple(transform(curve) for curve in mesh.v_curves),
    )


def box_mesh(
    *,
    size: Vec3 = (1.0, 1.0, 1.0),
    translation: Vec3 = (0.0, 0.0, 0.0),
    rotation_deg: Vec3 = (0.0, 0.0, 0.0),
) -> tuple[FloatArray, IntArray, tuple[FloatArray, ...]]:
    corners = np.array(
        [
            (-0.5, -0.5, -0.5),
            (0.5, -0.5, -0.5),
            (0.5, 0.5, -0.5),
            (-0.5, 0.5, -0.5),
            (-0.5, -0.5, 0.5),
            (0.5, -0.5, 0.5),
            (0.5, 0.5, 0.5),
            (-0.5, 0.5, 0.5),
        ],
        dtype=float,
    )
    vertices = transform_points(
        corners,
        translation=translation,
        rotation_deg=rotation_deg,
        scale=size,
    )
    faces = np.array(
        [
            (0, 2, 1),
            (0, 3, 2),
            (4, 5, 6),
            (4, 6, 7),
            (0, 1, 5),
            (0, 5, 4),
            (1, 2, 6),
            (1, 6, 5),
            (2, 3, 7),
            (2, 7, 6),
            (3, 0, 4),
            (3, 4, 7),
        ],
        dtype=np.int64,
    )
    edge_indices = (
        (0, 1),
        (1, 2),
        (2, 3),
        (3, 0),
        (4, 5),
        (5, 6),
        (6, 7),
        (7, 4),
        (0, 4),
        (1, 5),
        (2, 6),
        (3, 7),
    )
    return vertices, faces, tuple(vertices[list(edge)] for edge in edge_indices)


def extruded_polygon_mesh(
    polygon: Sequence[tuple[float, float]],
    *,
    depth: float,
    translation: Vec3 = (0.0, 0.0, 0.0),
    rotation_deg: Vec3 = (0.0, 0.0, 0.0),
    scale: Vec3 = (1.0, 1.0, 1.0),
) -> tuple[FloatArray, IntArray, tuple[FloatArray, ...]]:
    if len(polygon) < 3 or depth <= 0:
        raise MapPlotterError("An extruded polygon needs three points and depth.")
    lower = [(float(x), float(y), -depth / 2) for x, y in polygon]
    upper = [(float(x), float(y), depth / 2) for x, y in polygon]
    vertices = transform_points(
        np.asarray([*lower, *upper], dtype=float),
        translation=translation,
        rotation_deg=rotation_deg,
        scale=scale,
    )
    count = len(polygon)
    faces: list[tuple[int, int, int]] = []
    for index in range(1, count - 1):
        faces.append((0, index + 1, index))
        faces.append((count, count + index, count + index + 1))
    for index in range(count):
        nxt = (index + 1) % count
        faces.extend(((index, nxt, count + nxt), (index, count + nxt, count + index)))
    curves: list[FloatArray] = []
    curves.append(np.vstack((vertices[:count], vertices[0])))
    curves.append(np.vstack((vertices[count:], vertices[count])))
    curves.extend(vertices[[index, count + index]] for index in range(count))
    return vertices, np.asarray(faces, dtype=np.int64), tuple(curves)


def _camera_basis(
    camera: Camera3D,
) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray]:
    eye = np.asarray(camera.eye, dtype=float)
    forward = _unit(np.asarray(camera.target, dtype=float) - eye, "camera forward")
    right = _unit(np.cross(forward, np.asarray(camera.up, dtype=float)), "camera right")
    up = _unit(np.cross(right, forward), "camera up")
    return eye, right, up, forward


@dataclass(frozen=True)
class _Projector:
    camera: Camera3D
    eye: FloatArray
    right: FloatArray
    up: FloatArray
    forward: FloatArray
    tan_half_fov: float
    scale: float
    projected_centre: tuple[float, float]
    paper_centre: tuple[float, float]
    field: Rect
    pixel_width: int
    pixel_height: int

    def camera_space(self, points: FloatArray) -> FloatArray:
        relative = points - self.eye
        return np.column_stack(
            (
                relative @ self.right,
                relative @ self.up,
                relative @ self.forward,
            )
        )

    def raw_project(
        self, points: FloatArray
    ) -> tuple[FloatArray, FloatArray, NDArray[np.bool_]]:
        camera_points = self.camera_space(points)
        depth = camera_points[:, 2]
        valid = (depth >= self.camera.near) & (depth <= self.camera.far)
        denominator = np.maximum(depth * self.tan_half_fov, 1e-12)
        projected = np.column_stack(
            (
                camera_points[:, 0] / denominator + self.camera.lens_shift[0],
                camera_points[:, 1] / denominator + self.camera.lens_shift[1],
            )
        )
        return projected, depth, valid

    def project(
        self, points: FloatArray
    ) -> tuple[FloatArray, FloatArray, NDArray[np.bool_]]:
        raw, depth, valid = self.raw_project(points)
        x = self.paper_centre[0] + (raw[:, 0] - self.projected_centre[0]) * self.scale
        y = self.paper_centre[1] - (raw[:, 1] - self.projected_centre[1]) * self.scale
        return np.column_stack((x, y)), depth, valid

    def pixels(self, paper: FloatArray) -> FloatArray:
        x = (paper[:, 0] - self.field.left) / self.field.width * (self.pixel_width - 1)
        y = (paper[:, 1] - self.field.top) / self.field.height * (self.pixel_height - 1)
        return np.column_stack((x, y))


def _make_projector(scene: Scene3D, field: Rect) -> tuple[_Projector, float]:
    camera = scene.camera.validated()
    eye, right, up, forward = _camera_basis(camera)
    tan_half = math.tan(math.radians(camera.fov_y_deg) / 2.0)
    # Camera framing belongs to the modeled solids.  Decorative ground grids,
    # orbit paths, signal rays and cast-shadow hatches are intentionally allowed
    # to run beyond the viewport; including them in the fit would make a small
    # hero object float in an oversized engineering diagram.
    framing_points = np.vstack([mesh.vertices for mesh in scene.meshes])
    relative = framing_points - eye
    camera_points = np.column_stack(
        (relative @ right, relative @ up, relative @ forward)
    )
    depth = camera_points[:, 2]
    visible = (depth >= camera.near) & (depth <= camera.far)
    if np.count_nonzero(visible) < 3:
        raise MapPlotterError(f"Scene {scene.id!r} is outside its camera frustum.")
    projected = np.column_stack(
        (
            camera_points[:, 0] / np.maximum(depth * tan_half, 1e-12)
            + camera.lens_shift[0],
            camera_points[:, 1] / np.maximum(depth * tan_half, 1e-12)
            + camera.lens_shift[1],
        )
    )[visible]
    minimum = projected.min(axis=0)
    maximum = projected.max(axis=0)
    span = maximum - minimum
    if float(span.min()) <= 1e-9:
        raise MapPlotterError(f"Scene {scene.id!r} has a flat camera projection.")
    base_scale = min(field.width / span[0], field.height / span[1])
    scale = base_scale * float(scene.zoom)
    projected_centre = tuple(((minimum + maximum) / 2.0).tolist())
    paper_centre = (
        field.centre[0] + float(scene.framing_offset[0]) * field.width,
        field.centre[1] + float(scene.framing_offset[1]) * field.height,
    )
    pixel_width = int(scene.depth_buffer_px)
    pixel_height = max(320, round(pixel_width * field.height / field.width))
    projector = _Projector(
        camera,
        eye,
        right,
        up,
        forward,
        tan_half,
        scale,
        projected_centre,
        paper_centre,
        field,
        pixel_width,
        pixel_height,
    )
    paper, _, valid = projector.project(framing_points)
    in_field = (
        valid
        & (paper[:, 0] >= field.left)
        & (paper[:, 0] <= field.right)
        & (paper[:, 1] >= field.top)
        & (paper[:, 1] <= field.bottom)
    )
    if not np.any(in_field):
        raise MapPlotterError(f"Scene {scene.id!r} does not intersect the plate field.")
    clipped = paper[in_field]
    occupancy = (
        (float(clipped[:, 0].max()) - float(clipped[:, 0].min()))
        * (float(clipped[:, 1].max()) - float(clipped[:, 1].min()))
        / (field.width * field.height)
    )
    return projector, min(max(occupancy, 0.0), 1.0)


def _rasterize_meshes(scene: Scene3D, projector: _Projector) -> FloatArray:
    depth_buffer = np.full(
        (projector.pixel_height, projector.pixel_width), np.inf, dtype=np.float64
    )
    for mesh in scene.meshes:
        paper, depths, valid = projector.project(mesh.vertices)
        pixels = projector.pixels(paper)
        for face in mesh.faces:
            if not bool(valid[face].all()):
                continue
            triangle = pixels[face]
            triangle_depths = depths[face]
            min_x = max(0, int(math.floor(float(triangle[:, 0].min()))))
            max_x = min(
                projector.pixel_width - 1,
                int(math.ceil(float(triangle[:, 0].max()))),
            )
            min_y = max(0, int(math.floor(float(triangle[:, 1].min()))))
            max_y = min(
                projector.pixel_height - 1,
                int(math.ceil(float(triangle[:, 1].max()))),
            )
            if min_x > max_x or min_y > max_y:
                continue
            x0, y0 = triangle[0]
            x1, y1 = triangle[1]
            x2, y2 = triangle[2]
            denominator = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
            if abs(float(denominator)) <= 1e-10:
                continue
            yy, xx = np.mgrid[min_y : max_y + 1, min_x : max_x + 1]
            sample_x = xx + 0.5
            sample_y = yy + 0.5
            weight_0 = (
                (y1 - y2) * (sample_x - x2) + (x2 - x1) * (sample_y - y2)
            ) / denominator
            weight_1 = (
                (y2 - y0) * (sample_x - x2) + (x0 - x2) * (sample_y - y2)
            ) / denominator
            weight_2 = 1.0 - weight_0 - weight_1
            inside = (weight_0 >= -1e-8) & (weight_1 >= -1e-8) & (weight_2 >= -1e-8)
            if not np.any(inside):
                continue
            reciprocal = (
                weight_0 / triangle_depths[0]
                + weight_1 / triangle_depths[1]
                + weight_2 / triangle_depths[2]
            )
            depth = np.divide(
                1.0,
                reciprocal,
                out=np.full_like(reciprocal, np.inf),
                where=np.abs(reciprocal) > 1e-12,
            )
            window = depth_buffer[min_y : max_y + 1, min_x : max_x + 1]
            np.minimum(window, np.where(inside, depth, np.inf), out=window)
    return depth_buffer


def _edge_curves(mesh: Mesh3D, camera: Camera3D) -> list[Curve3D]:
    """Extract joined silhouette and high-dihedral crease curves."""

    triangles = mesh.vertices[mesh.faces]
    normals = np.cross(
        triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    )
    lengths = np.linalg.norm(normals, axis=1)
    good = lengths > 1e-12
    normals[good] /= lengths[good, None]
    centres = triangles.mean(axis=1)
    to_camera = np.asarray(camera.eye, dtype=float) - centres
    front = np.sum(normals * to_camera, axis=1) > 0.0
    adjacency: dict[tuple[int, int], list[int]] = {}
    for face_index, face in enumerate(mesh.faces):
        for first, second in (
            (face[0], face[1]),
            (face[1], face[2]),
            (face[2], face[0]),
        ):
            edge = (min(int(first), int(second)), max(int(first), int(second)))
            adjacency.setdefault(edge, []).append(face_index)
    silhouette_edges: list[tuple[int, int]] = []
    crease_edges: list[tuple[int, int]] = []
    cosine_threshold = math.cos(math.radians(float(mesh.crease_angle_deg)))
    for edge, faces in adjacency.items():
        if len(faces) == 1:
            silhouette_edges.append(edge)
            continue
        if len(faces) != 2:
            continue
        first, second = faces
        if bool(front[first]) != bool(front[second]):
            silhouette_edges.append(edge)
        elif (
            mesh.crease_pen_id is not None
            and (front[first] or front[second])
            and float(np.dot(normals[first], normals[second])) < cosine_threshold
        ):
            crease_edges.append(edge)

    def join_edges(
        edges: Sequence[tuple[int, int]], pen_id: str, role: str
    ) -> list[Curve3D]:
        remaining = set(edges)
        neighbours: dict[int, set[int]] = {}
        for first, second in remaining:
            neighbours.setdefault(first, set()).add(second)
            neighbours.setdefault(second, set()).add(first)
        results: list[Curve3D] = []
        index = 0
        while remaining:
            edge = next(iter(remaining))
            endpoints = [value for value in edge if len(neighbours.get(value, ())) != 2]
            start = endpoints[0] if endpoints else edge[0]
            path = [start]
            previous: int | None = None
            current = start
            while True:
                options = [
                    value
                    for value in neighbours.get(current, ())
                    if (min(current, value), max(current, value)) in remaining
                    and value != previous
                ]
                if not options:
                    break
                nxt = options[0]
                remaining.remove((min(current, nxt), max(current, nxt)))
                path.append(nxt)
                previous, current = current, nxt
                if current == start:
                    break
            if len(path) >= 2:
                results.append(
                    Curve3D(
                        f"{mesh.id}-{role}-{index:03d}",
                        mesh.vertices[path],
                        pen_id,
                        role,
                        mesh.id,
                        True,
                        -0.025,
                        {**mesh.attributes, "data-line-source": role},
                    )
                )
                index += 1
        return results

    result: list[Curve3D] = []
    if mesh.silhouette_pen_id is not None:
        result.extend(
            join_edges(silhouette_edges, mesh.silhouette_pen_id, "silhouette")
        )
    if mesh.crease_pen_id is not None:
        result.extend(join_edges(crease_edges, mesh.crease_pen_id, "crease"))
    return result


def _resample_curve(
    curve: Curve3D, projector: _Projector
) -> tuple[FloatArray, FloatArray, FloatArray, NDArray[np.bool_]]:
    points: list[FloatArray] = []
    for index, (first, second) in enumerate(zip(curve.points, curve.points[1:])):
        pair = np.vstack((first, second))
        paper, _, valid = projector.project(pair)
        if not bool(valid.any()):
            continue
        pixels = projector.pixels(paper)
        pixel_distance = float(np.linalg.norm(pixels[1] - pixels[0]))
        paper_distance = float(np.linalg.norm(paper[1] - paper[0]))
        steps = max(
            2,
            int(math.ceil(pixel_distance / 0.65)) + 1,
            int(math.ceil(paper_distance / 0.10)) + 1,
        )
        values = np.linspace(0.0, 1.0, steps)
        segment = (
            first[None, :] * (1.0 - values[:, None]) + second[None, :] * values[:, None]
        )
        if index and points and np.allclose(points[-1][-1], segment[0]):
            segment = segment[1:]
        if len(segment):
            points.append(segment)
    if not points:
        empty = np.empty((0, 2), dtype=float)
        return empty, np.empty(0), empty, np.empty(0, dtype=bool)
    world = np.vstack(points)
    paper, depth, valid = projector.project(world)
    pixels = projector.pixels(paper)
    return paper, depth, pixels, valid


def _rdp(points: FloatArray, tolerance: float) -> FloatArray:
    if len(points) <= 2:
        return points
    start, end = points[0], points[-1]
    vector = end - start
    length = float(np.linalg.norm(vector))
    if length <= 1e-12:
        distances = np.linalg.norm(points - start, axis=1)
    else:
        offsets = points - start
        distances = (
            np.abs(vector[0] * offsets[:, 1] - vector[1] * offsets[:, 0]) / length
        )
    index = int(np.argmax(distances))
    if float(distances[index]) <= tolerance:
        return np.vstack((start, end))
    return np.vstack(
        (_rdp(points[: index + 1], tolerance), _rdp(points[index:], tolerance)[1:])
    )


def _visible_fragments(
    curve: Curve3D,
    projector: _Projector,
    depth_buffer: FloatArray,
) -> tuple[list[tuple[FloatArray, float]], int, int, int, int]:
    paper, depth, pixels, valid = _resample_curve(curve, projector)
    if not len(paper):
        return [], 0, 0, 0, 0
    inside = (
        valid
        & (paper[:, 0] >= projector.field.left)
        & (paper[:, 0] <= projector.field.right)
        & (paper[:, 1] >= projector.field.top)
        & (paper[:, 1] <= projector.field.bottom)
    )
    visible = inside.copy()
    if curve.occluded and np.any(inside):
        px = np.clip(np.rint(pixels[:, 0]).astype(int), 0, projector.pixel_width - 1)
        py = np.clip(np.rint(pixels[:, 1]).astype(int), 0, projector.pixel_height - 1)
        surface_depth = depth_buffer[py, px]
        epsilon = np.maximum(0.018, depth * 0.0025)
        visible &= depth + float(curve.depth_bias) <= surface_depth + epsilon
    fragments: list[tuple[FloatArray, float]] = []
    start: int | None = None
    for index, state in enumerate(visible):
        if state and start is None:
            start = index
        if start is not None and (not state or index == len(visible) - 1):
            stop = index + 1 if state and index == len(visible) - 1 else index
            if stop - start >= 2:
                fragment = _rdp(paper[start:stop], 0.045)
                if len(fragment) >= 2:
                    fragments.append((fragment, float(depth[start:stop].mean())))
            start = None
    total = len(visible)
    visible_count = int(np.count_nonzero(visible))
    clipped_count = int(np.count_nonzero(~inside))
    occluded_count = total - visible_count - clipped_count
    return fragments, total, visible_count, max(0, occluded_count), clipped_count


def _scene_digest(scene: Scene3D) -> str:
    def curve_record(curve: Curve3D) -> dict[str, Any]:
        return {
            "id": curve.id,
            "points": np.round(curve.points, 9).tolist(),
            "pen_id": curve.pen_id,
            "role": curve.role,
            "object_id": curve.object_id,
            "occluded": curve.occluded,
            "depth_bias": curve.depth_bias,
            "attributes": curve.attributes,
        }

    payload = {
        "id": scene.id,
        "camera": {
            "eye": scene.camera.eye,
            "target": scene.camera.target,
            "up": scene.camera.up,
            "fov_y_deg": scene.camera.fov_y_deg,
            "near": scene.camera.near,
            "far": scene.camera.far,
            "lens_shift": scene.camera.lens_shift,
        },
        "zoom": scene.zoom,
        "framing_offset": scene.framing_offset,
        "depth_buffer_px": scene.depth_buffer_px,
        "crop_intent": scene.crop_intent,
        "meshes": [
            {
                "id": mesh.id,
                "vertices": np.round(mesh.vertices, 9).tolist(),
                "faces": mesh.faces.tolist(),
                "silhouette_pen_id": mesh.silhouette_pen_id,
                "crease_pen_id": mesh.crease_pen_id,
                "crease_angle_deg": mesh.crease_angle_deg,
                "attributes": mesh.attributes,
                "curves": [curve_record(curve) for curve in mesh.curves],
            }
            for mesh in scene.meshes
        ],
        "curves": [curve_record(curve) for curve in scene.curves],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def render_hidden_line_scene(scene: Scene3D, field: Rect) -> RenderedScene:
    """Render true visible 3D curves into the named physical plate field."""

    projector, occupancy = _make_projector(scene, field)
    depth_buffer = _rasterize_meshes(scene, projector)
    candidates = [*scene.curves]
    for mesh in scene.meshes:
        candidates.extend(mesh.curves)
        candidates.extend(_edge_curves(mesh, scene.camera))
    rendered: list[RenderedCurve] = []
    sample_total = visible_total = occluded_total = clipped_total = 0
    for curve in candidates:
        fragments, total, visible, occluded, clipped = _visible_fragments(
            curve, projector, depth_buffer
        )
        sample_total += total
        visible_total += visible
        occluded_total += occluded
        clipped_total += clipped
        for index, (fragment, mean_depth) in enumerate(fragments):
            points = tuple((float(x), float(y)) for x, y in fragment)
            if polyline_length_mm(points) <= 1e-9:
                continue
            rendered.append(
                RenderedCurve(
                    f"{curve.id}-visible-{index:03d}",
                    points,
                    curve.pen_id,
                    curve.role,
                    curve.object_id,
                    mean_depth,
                    {
                        **curve.attributes,
                        "data-3d-object": curve.object_id,
                        "data-3d-curve": curve.id,
                        "data-visibility": (
                            "depth-tested" if curve.occluded else "unoccluded-overlay"
                        ),
                    },
                )
            )
    finite_depth = depth_buffer[np.isfinite(depth_buffer)]
    if not len(finite_depth):
        raise MapPlotterError(f"Scene {scene.id!r} produced no visible depth surface.")
    stats = RenderStats(
        len(scene.meshes),
        sum(len(mesh.vertices) for mesh in scene.meshes),
        sum(len(mesh.faces) for mesh in scene.meshes),
        len(candidates),
        sample_total,
        visible_total,
        occluded_total,
        clipped_total,
        len(rendered),
        float(finite_depth.min()),
        float(finite_depth.max()),
        float(finite_depth.max() - finite_depth.min()),
        occupancy,
        projector.pixel_width,
        projector.pixel_height,
        scene.crop_intent,
    )
    if stats.depth_span <= 0.2:
        raise MapPlotterError(f"Scene {scene.id!r} has insufficient 3D depth span.")
    if stats.occluded_sample_count <= 0:
        raise MapPlotterError(f"Scene {scene.id!r} proves no hidden-line occlusion.")
    if stats.projected_occupancy < 0.18:
        raise MapPlotterError(f"Scene {scene.id!r} is too small in its field.")
    camera_matrix = tuple(
        tuple(float(value) for value in row)
        for row in np.vstack((projector.right, projector.up, projector.forward))
    )
    return RenderedScene(tuple(rendered), stats, camera_matrix, _scene_digest(scene))


def curves_from_family(
    *,
    object_id: str,
    family: Iterable[FloatArray],
    pen_ids: Sequence[str],
    role: str,
    prefix: str,
    every: int = 1,
    phase: int = 0,
    occluded: bool = True,
    depth_bias: float = -0.01,
    attributes: dict[str, str] | None = None,
) -> list[Curve3D]:
    """Turn selected native surface isolines into semantically coloured curves."""

    if not pen_ids or every < 1:
        raise MapPlotterError("A 3D curve family needs pens and a positive stride.")
    result: list[Curve3D] = []
    for index, points in enumerate(family):
        if (index - phase) % every:
            continue
        pen_id = pen_ids[(index // every) % len(pen_ids)]
        result.append(
            Curve3D(
                f"{prefix}-{index:03d}",
                points,
                pen_id,
                role,
                object_id,
                occluded,
                depth_bias,
                dict(attributes or {}),
            )
        )
    return result
