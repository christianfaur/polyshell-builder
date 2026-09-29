# SPDX-FileCopyrightText: 2026 Christian Faur
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field

import bmesh
import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from . import collide, core, presets


MAX_WALL_LEAN_DEG = 60.0

REFERENCE_COLLECTION = "PSB_Reference"
PANEL_COLLECTION = "PSB_Panels"
PLATE_ROOT_COLLECTION = "PSB_Print_Plates"
SPLINE_COLLECTION = "PSB_Splines"
# Generated collections carry this key so they are found even if renamed.
COLLECTION_ROLE = "psb_role"
# What the add-on made while it was called Panel Forge; cleared on the next build.
LEGACY_COLLECTIONS = ("PF_Reference", "PF_Panels", "PF_Print_Plates", "PF_Splines")


class PolyshellError(RuntimeError):
    pass


@dataclass
class EdgeUse:
    face_index: int
    slot: int
    a: int
    b: int


@dataclass
class ModelData:
    source: bpy.types.Object
    vertices: list[Vector]
    faces: list[list[int]]
    normals: list[Vector]
    centroids: list[Vector]
    adjacency: dict[tuple[int, int], list[EdgeUse]]
    source_edge_indices: dict[tuple[int, int], int]
    scale_factor: float
    max_dimension: float
    warnings: list[str] = field(default_factory=list)


@dataclass
class FaceBuild:
    face_index: int
    panel_id: str
    vertex_ids: list[int]
    normal: Vector
    centroid_world: Vector
    x_axis: Vector
    y_axis: Vector
    inward_axis: Vector
    matrix_world: Matrix
    matrix_inverse: Matrix
    vertices: list[Vector]
    faces: list[tuple[int, ...]]
    outer_local: list[Vector]
    inner_local: list[Vector]
    inner_world_by_vertex: dict[int, Vector]
    inner_polygon: list[core.Point2]
    inner_edge_by_key: dict[tuple[int, int], tuple[Vector, Vector]] = field(default_factory=dict)
    rate_by_key: dict[tuple[int, int], float] = field(default_factory=dict)
    dihedral_by_key: dict[tuple[int, int], float] = field(default_factory=dict)
    rim_height: float = 0.0
    thickness: float = 0.0
    longest_corner_run: float = 0.0
    inner_area_ratio: float = 1.0
    inner_is_simple: bool = True
    reserved: list[list[core.Point2]] = field(default_factory=list)
    hard_reserved: list[list[core.Point2]] = field(default_factory=list)
    labels: list[dict] = field(default_factory=list)
    id_box: list[core.Point2] | None = None
    connections: list[dict] = field(default_factory=list)


def find_collection(role: str) -> bpy.types.Collection | None:
    """A generated collection by its role tag, wherever it is and whatever it is called."""
    named = bpy.data.collections.get(role)
    if named is not None and named.get(COLLECTION_ROLE) == role:
        return named
    return next((item for item in bpy.data.collections if item.get(COLLECTION_ROLE) == role), None)


def remove_legacy_collections() -> None:
    """Remove collections Panel Forge (this add-on's earlier name) generated.

    Only collections holding nothing but Panel Forge parts are touched.
    """
    for name in LEGACY_COLLECTIONS:
        collection = bpy.data.collections.get(name)
        if collection is None:
            continue
        objects = list(collection.all_objects)
        if all(obj.get("pf_kind") for obj in objects):
            _remove_tree(collection, "pf_kind")


def remove_collection_tree(name: str) -> None:
    collection = find_collection(name)
    if collection is None:
        return
    _remove_tree(collection, "psb_kind")


def _remove_tree(collection: bpy.types.Collection, kind_key: str) -> None:
    meshes: list[bpy.types.Mesh] = []

    def remove_one(item: bpy.types.Collection) -> None:
        for child in list(item.children):
            remove_one(child)
        for obj in list(item.objects):
            if obj.type == "MESH" and obj.get(kind_key):
                meshes.append(obj.data)
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(item)

    remove_one(collection)
    for mesh in meshes:
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def create_scene_collection(name: str) -> bpy.types.Collection:
    collection = bpy.data.collections.new(name)
    collection[COLLECTION_ROLE] = name
    bpy.context.scene.collection.children.link(collection)
    return collection


def schedule_outliner_collapse() -> None:
    """Collapse Outliner hierarchies after generated collections are rebuilt."""
    if bpy.app.background:
        return

    def collapse():
        for window in bpy.context.window_manager.windows:
            screen = window.screen
            for area in screen.areas:
                if area.type != "OUTLINER":
                    continue
                region = next((item for item in area.regions if item.type == "WINDOW"), None)
                if region is None:
                    continue
                try:
                    with bpy.context.temp_override(window=window, screen=screen, area=area, region=region):
                        bpy.ops.outliner.expanded_toggle()
                except RuntimeError:
                    pass
        return None

    if not bpy.app.timers.is_registered(collapse):
        bpy.app.timers.register(collapse, first_interval=0.15)


def _material(name: str, color: tuple[float, float, float, float]) -> bpy.types.Material:
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    material.diffuse_color = color
    return material


def _newell_normal(points: list[Vector]) -> Vector:
    normal = Vector((0.0, 0.0, 0.0))
    for index, p in enumerate(points):
        q = points[(index + 1) % len(points)]
        normal.x += (p.y - q.y) * (p.z + q.z)
        normal.y += (p.z - q.z) * (p.x + q.x)
        normal.z += (p.x - q.x) * (p.y + q.y)
    if normal.length < 1e-10:
        raise PolyshellError("At least one face has zero area.")
    return normal.normalized()


def _signed_volume(vertices, faces) -> float:
    """Signed volume in double precision, measured from the first vertex.

    mathutils works in single precision, which is too coarse for a key a few
    millimetres across sitting a hundred millimetres from the origin.
    """
    if not vertices:
        return 0.0
    ox, oy, oz = (float(c) for c in vertices[0])
    points = [(float(v[0]) - ox, float(v[1]) - oy, float(v[2]) - oz) for v in vertices]
    total = 0.0
    for face in faces:
        ax, ay, az = points[face[0]]
        for index in range(1, len(face) - 1):
            bx, by, bz = points[face[index]]
            cx, cy, cz = points[face[index + 1]]
            total += ax * (by * cz - bz * cy) + ay * (bz * cx - bx * cz) + az * (bx * cy - by * cx)
    return total / 6.0


def _face_basis(points: list[Vector], normal: Vector) -> tuple[Vector, Vector, Vector]:
    centroid = sum(points, Vector()) / len(points)
    best = max((points[(i + 1) % len(points)] - points[i] for i in range(len(points))), key=lambda v: v.length)
    x_axis = best.normalized()
    y_axis = normal.cross(x_axis).normalized()
    return centroid, x_axis, y_axis


def _project(points: list[Vector], centroid: Vector, x_axis: Vector, y_axis: Vector) -> list[core.Point2]:
    return [((p - centroid).dot(x_axis), (p - centroid).dot(y_axis)) for p in points]


def _is_strictly_convex(poly: list[core.Point2]) -> bool:
    signs = []
    for i in range(len(poly)):
        value = core.cross(poly[i - 1], poly[i], poly[(i + 1) % len(poly)])
        if abs(value) > 1e-7:
            signs.append(value > 0.0)
    return bool(signs) and all(value == signs[0] for value in signs)


_DEFAULT_OVERRIDE = {"count": -1, "flip": False, "disabled": False, "offset_1": 0.0, "offset_2": 0.0, "offset_3": 0.0}
OVERRIDE_ATTRIBUTES = {
    "count": "psb_tab_count",
    "flip": "psb_flip",
    "disabled": "psb_disabled",
    "offset_1": "psb_offset_1",
    "offset_2": "psb_offset_2",
    "offset_3": "psb_offset_3",
}
OVERRIDE_FLAG = "psb_override"


def legacy_attribute_name(name: str) -> str:
    """The name an override layer had while the add-on was called Panel Forge."""
    return "pf_" + name[len("psb_"):]


def read_edge_overrides(mesh: bpy.types.Mesh) -> dict[int, dict]:
    """Per-edge overrides, keyed by source edge index.

    An edge is only overridden if the user applied an override to it, which
    `psb_override` records. Scenes from 0.2.4 and earlier have no such flag, and
    their count layer was created holding 0 on every edge, which would disable
    keys everywhere; there a stored 0 is treated as automatic.
    """
    layers = {}
    for key, name in OVERRIDE_ATTRIBUTES.items():
        attribute = mesh.attributes.get(name) or mesh.attributes.get(legacy_attribute_name(name))
        if attribute is not None and attribute.domain == "EDGE":
            layers[key] = attribute
    flag = mesh.attributes.get(OVERRIDE_FLAG) or mesh.attributes.get(legacy_attribute_name(OVERRIDE_FLAG))
    if flag is not None and flag.domain != "EDGE":
        flag = None
    result: dict[int, dict] = {}
    for edge_index in range(len(mesh.edges)):
        item = dict(_DEFAULT_OVERRIDE)
        if flag is not None and not flag.data[edge_index].value:
            result[edge_index] = item
            continue
        for key, attribute in layers.items():
            if edge_index < len(attribute.data):
                value = attribute.data[edge_index].value
                item[key] = bool(value) if key in {"flip", "disabled"} else value
        if flag is None and int(item["count"]) == 0:
            item["count"] = -1
        result[edge_index] = item
    return result


def _resolve_source_object(context: bpy.types.Context, settings) -> bpy.types.Object:
    """Resolve generated/reference selections back to their original source mesh."""
    active = context.active_object
    if active is not None and active.type == "MESH" and not active.get("psb_kind"):
        return active

    source_name = ""
    if active is not None:
        source_name = str(active.get("psb_source_name", ""))
    if not source_name:
        source_name = settings.last_source_name
    source = bpy.data.objects.get(source_name) if source_name else None
    if source is not None and source.type == "MESH" and not source.get("psb_kind"):
        return source

    if active is not None and active.get("psb_kind"):
        raise PolyshellError(
            "The original source mesh cannot be found. Select the original sculpture object and try again."
        )
    raise PolyshellError("Select one low-poly source mesh object first.")


def _build_adjacency(faces: list[list[int]]) -> dict[tuple[int, int], list[EdgeUse]]:
    adjacency: dict[tuple[int, int], list[EdgeUse]] = {}
    for face_index, face in enumerate(faces):
        for slot, a in enumerate(face):
            b = face[(slot + 1) % len(face)]
            adjacency.setdefault(tuple(sorted((a, b))), []).append(EdgeUse(face_index, slot, a, b))
    return adjacency


def _triangulate_faces(
    vertices: list[Vector], faces: list[list[int]], targets: list[int]
) -> tuple[list[list[int]], list[Vector]]:
    """Split the listed faces into triangles, keeping winding and existing vertex indices."""
    working = bmesh.new()
    try:
        verts = [working.verts.new(point) for point in vertices]
        made = [working.faces.new([verts[i] for i in face]) for face in faces]
        working.verts.index_update()
        bmesh.ops.triangulate(
            working, faces=[made[index] for index in targets], quad_method="BEAUTY", ngon_method="BEAUTY"
        )
        _split_straight_corners(working)
        working.verts.index_update()
        working.faces.ensure_lookup_table()
        return [[vertex.index for vertex in face.verts] for face in working.faces], [
            vertex.co.copy() for vertex in working.verts
        ]
    finally:
        working.free()


def validate_source(context: bpy.types.Context, settings) -> ModelData:
    if settings.configure_scene_units:
        units = context.scene.unit_settings
        units.system = "METRIC"
        units.length_unit = "MILLIMETERS"
        units.scale_length = 0.001
    obj = _resolve_source_object(context, settings)
    if obj.mode != "OBJECT":
        raise PolyshellError("Leave Edit Mode before validating or generating panels.")
    mesh = obj.data
    if len(mesh.polygons) < 4:
        raise PolyshellError("The source needs at least four faces forming a closed volume.")
    if any(len(poly.vertices) < 3 for poly in mesh.polygons):
        raise PolyshellError("Every source face must have at least three vertices.")

    world_vertices = [obj.matrix_world @ vertex.co for vertex in mesh.vertices]
    used = {index for polygon in mesh.polygons for index in polygon.vertices}
    measured = [world_vertices[index] for index in sorted(used)]
    mins = Vector((min(v.x for v in measured), min(v.y for v in measured), min(v.z for v in measured)))
    maxs = Vector((max(v.x for v in measured), max(v.y for v in measured), max(v.z for v in measured)))
    span = maxs - mins
    maximum = max(span)
    if maximum < 1e-10:
        raise PolyshellError("The source object has no measurable size.")
    scale_factor = settings.target_dimension_mm / maximum
    center = (mins + maxs) * 0.5
    vertices = [(v - center) * scale_factor for v in world_vertices]
    faces = [list(poly.vertices) for poly in mesh.polygons]

    adjacency: dict[tuple[int, int], list[EdgeUse]] = {}
    for face_index, face in enumerate(faces):
        for slot, a in enumerate(face):
            b = face[(slot + 1) % len(face)]
            adjacency.setdefault(tuple(sorted((a, b))), []).append(EdgeUse(face_index, slot, a, b))
    bad_edges = [key for key, uses in adjacency.items() if len(uses) != 2]
    if bad_edges:
        raise PolyshellError(
            f"Mesh is not watertight/manifold: {len(bad_edges)} edges do not have exactly two faces. "
            "In Edit Mode, Select > Select All by Trait > Non Manifold shows them; fill the holes "
            "or delete the extra faces."
        )
    inconsistent = [key for key, uses in adjacency.items() if uses[0].a == uses[1].a and uses[0].b == uses[1].b]
    if inconsistent:
        raise PolyshellError(
            f"Mesh has inconsistent face winding across {len(inconsistent)} edges. In Edit Mode, "
            "select everything and use Mesh > Normals > Recalculate Outside."
        )

    volume = _signed_volume(vertices, faces)
    if abs(volume) < 1e-7:
        raise PolyshellError("The source encloses no reliable volume. Check normals and non-manifold geometry.")
    if volume < 0.0:
        faces = [list(reversed(face)) for face in faces]
        adjacency.clear()
        for face_index, face in enumerate(faces):
            for slot, a in enumerate(face):
                b = face[(slot + 1) % len(face)]
                adjacency.setdefault(tuple(sorted((a, b))), []).append(EdgeUse(face_index, slot, a, b))

    warnings: list[str] = []
    planar_tolerance = max(0.02, settings.panel_thickness_mm * 0.05)

    def face_problem(face: list[int]) -> str:
        points = [vertices[i] for i in face]
        try:
            normal = _newell_normal(points)
        except PolyshellError:
            return "has zero area"
        centroid, x_axis, y_axis = _face_basis(points, normal)
        deviation = max(abs(normal.dot(point - points[0])) for point in points)
        if deviation > planar_tolerance:
            return f"is not flat ({deviation:.2f} mm out of plane)"
        if not _is_strictly_convex(_project(points, centroid, x_axis, y_axis)):
            return "is concave"
        if _straight_corners(points):
            return "has a vertex in the middle of an edge"
        return ""

    problems = [(index, face_problem(face)) for index, face in enumerate(faces)]
    bent = [index for index, problem in problems if problem and problem != "has zero area" and len(faces[index]) > 3]
    # A triangle with a straight corner is a zero-area sliver and is reported below.
    if bent and settings.auto_triangulate:
        faces, vertices = _triangulate_faces(vertices, faces, bent)
        adjacency = _build_adjacency(faces)
        warnings.append(
            f"Triangulated {len(bent)} faces that were not flat or not convex; the source mesh is unchanged."
        )
        problems = [(index, face_problem(face)) for index, face in enumerate(faces)]
    for index, problem in problems:
        if problem:
            hint = "Remove it (Merge by Distance / Dissolve Degenerate)." if problem == "has zero area" else (
                "Triangulate it (Ctrl+T) or enable Auto-Triangulate."
            )
            raise PolyshellError(f"Face {index} of the source mesh {problem}. {hint}")

    normals: list[Vector] = []
    centroids: list[Vector] = []
    for face in faces:
        points = [vertices[i] for i in face]
        normal = _newell_normal(points)
        centroid, _x, _y = _face_basis(points, normal)
        normals.append(normal)
        centroids.append(centroid)

    source_edge_indices = {tuple(sorted(edge.vertices)): edge.index for edge in mesh.edges}
    loose = len(mesh.vertices) - len(used)
    if loose:
        warnings.append(f"Ignored {loose} loose vertices that belong to no face.")
    if len(mesh.polygons) > 500:
        warnings.append("This mesh has more than 500 faces; generation and packing may take several minutes.")
    if obj.modifiers:
        warnings.append("Source modifiers are intentionally ignored so edge overrides remain stable. Apply required modifiers first.")
    result = ModelData(obj, vertices, faces, normals, centroids, adjacency, source_edge_indices, scale_factor, settings.target_dimension_mm, warnings)

    # Overall size says nothing about how sharply this particular mesh folds, so
    # measure the depth its faces can actually carry and hold the skin to it.
    limit = recommended_thickness_limit(result, settings)
    settings.mesh_thickness_limit_mm = round(limit, 2)
    settings.mesh_thickness_limit_ratio = limit / settings.target_dimension_mm if limit > 0.0 else 0.0
    if settings.auto_tune:
        # Always re-derive from the preset: thickness must come back up when
        # this mesh can carry more than the last one could.
        preset_thickness = presets.recommended_thickness(settings.target_dimension_mm, settings.thickness_preset)
        presets.apply(settings)
        if settings.thickness_preset != "CUSTOM" and settings.panel_thickness_mm < preset_thickness - 1e-4:
            warnings.append(
                f"Skin thickness held to {settings.panel_thickness_mm:.2f} mm (preset {preset_thickness:.2f}); "
                "sharp folds in this mesh cannot carry more."
            )
    if limit > 0.0 and settings.panel_thickness_mm > limit + 1e-4:
        warnings.append(
            f"Skin thickness {settings.panel_thickness_mm:.2f} mm exceeds the {limit:.2f} mm this "
            "mesh can carry; some panels will be thinned individually."
        )
    return result


def recommended_thickness_limit(model: ModelData, settings) -> float:
    """Largest skin depth this mesh can carry before miters eat panel interiors.

    A mitered wall steps `cot(dihedral / 2)` inward per unit of depth, so sharp
    folds around a small face set a hard ceiling that has nothing to do with the
    sculpture's overall size. Reported as a low percentile so a few awkward faces
    thin themselves rather than dragging every panel down.
    """
    limits: list[float] = []
    for face_index, face in enumerate(model.faces):
        points = [model.vertices[i] for i in face]
        normal = model.normals[face_index]
        centroid, x_axis, _unused = _face_basis(points, normal)
        inward = -normal
        y_axis = inward.cross(x_axis).normalized()
        outline = [
            ((point - centroid).dot(x_axis), (point - centroid).dot(y_axis)) for point in points
        ]
        normals2 = _edge_inward_normals(outline)
        middle = core.polygon_centroid(outline)
        worst = math.inf
        for slot in range(len(face)):
            rate = _inset_rate(
                interior_dihedral_deg(model, face_index, face[slot], face[(slot + 1) % len(face)]),
                settings,
            )
            if rate <= 1e-6:
                continue
            reach = (middle[0] - outline[slot][0]) * normals2[slot][0] + (
                middle[1] - outline[slot][1]
            ) * normals2[slot][1]
            if reach > 0.0:
                worst = min(worst, reach * 0.55 / rate)
        if worst < math.inf:
            limits.append(worst)
    if not limits:
        return 0.0
    limits.sort()
    return limits[max(0, int(len(limits) * 0.10) - 1)]


def steep_miter_edges(model: ModelData, limit_deg: float = 45.0) -> list[tuple[int, int]]:
    """Edges whose mitered wall leans out past the printable overhang limit.

    The mating plane between two panels is uniquely fixed by their dihedral, so
    this cannot be tuned away; the source mesh has to soften the fold instead.
    """
    steep: list[tuple[int, int]] = []
    for key, uses in model.adjacency.items():
        if len(uses) != 2:
            continue
        angle = interior_dihedral_deg(model, uses[0].face_index, key[0], key[1])
        if angle <= 180.0:
            continue
        half = math.radians(angle * 0.5)
        lean = -math.cos(half) / max(1e-6, math.sin(half))
        if math.degrees(math.atan(max(0.0, lean))) > limit_deg:
            steep.append(key)
    return steep


def inward_edge_count(model: ModelData) -> int:
    """Edges that fold inward, where no printed key can cross without support."""
    total = 0
    for key, uses in model.adjacency.items():
        if len(uses) != 2:
            continue
        if interior_dihedral_deg(model, uses[0].face_index, key[0], key[1]) > 180.0:
            total += 1
    return total


def face_flat_polygon(model: ModelData, face_index: int) -> list[core.Point2]:
    points = [model.vertices[i] for i in model.faces[face_index]]
    centroid, x_axis, y_axis = _face_basis(points, model.normals[face_index])
    return _project(points, centroid, x_axis, y_axis)


def _face_room(settings) -> tuple[float, float]:
    """Largest face outline that still fits the bed once its keys are added.

    A male key reaches across the joint by up to its depth, so a panel prints
    wider than its face. Testing the bare face against the bed let a panel that
    only just fitted pass validation and then fail at nesting.
    """
    usable_x = settings.bed_x_mm - 2.0 * settings.bed_margin_mm
    usable_y = settings.bed_y_mm - 2.0 * settings.bed_margin_mm
    if usable_x <= 0.0 or usable_y <= 0.0:
        raise PolyshellError("The safety margin leaves no usable print bed.")
    allowance = 2.0 * (settings.tab_depth_mm + settings.clearance_mm + 1.0)
    return max(1.0, usable_x - allowance), max(1.0, usable_y - allowance)


def oversized_faces(model: ModelData, settings) -> list[int]:
    room_x, room_y = _face_room(settings)
    return [
        index
        for index in range(len(model.faces))
        if not core.can_fit_rotated(face_flat_polygon(model, index), room_x, room_y)
    ]


def _face_fits_dimensions(
    vertices: list[Vector],
    face: list[int],
    width: float,
    height: float,
) -> bool:
    points = [vertices[index] for index in face]
    normal = _newell_normal(points)
    centroid, x_axis, y_axis = _face_basis(points, normal)
    return core.can_fit_rotated(_project(points, centroid, x_axis, y_axis), width, height)


def _straight_corners(points: list[Vector]) -> int:
    """Corners where the outline carries straight on (a vertex sitting mid-edge)."""
    count = 0
    for index in range(len(points)):
        before, here, after = points[index - 1], points[index], points[(index + 1) % len(points)]
        incoming, outgoing = here - before, after - here
        scale = incoming.length * outgoing.length
        if scale > 0.0 and incoming.cross(outgoing).length <= 1e-6 * scale:
            count += 1
    return count


def _split_straight_corners(bm: bmesh.types.BMesh) -> int:
    """Triangulate every face that has a vertex sitting in the middle of an edge.

    Such a vertex joins two edge halves that border different panels at
    different miter angles, which a single flat wall cannot follow. Beauty
    triangulation connects that vertex across the face; a face that would still
    leave a sliver is fanned about its centre instead.
    """
    targets = [face for face in bm.faces if _straight_corners([v.co for v in face.verts])]
    if not targets:
        return 0
    result = bmesh.ops.triangulate(bm, faces=targets, quad_method="BEAUTY", ngon_method="BEAUTY")
    slivers = [
        face for face in result["faces"]
        if face.is_valid and (face.calc_area() < 1e-6 or _straight_corners([v.co for v in face.verts]))
    ]
    if slivers:
        bmesh.ops.poke(bm, faces=slivers)
    bm.normal_update()
    return len(targets)


def auto_split_oversized_model(model: ModelData, settings) -> tuple[ModelData, int]:
    """Return a watertight virtual mesh whose panels all fit the print bed.

    The source Blender mesh is never edited. Each pass grid-subdivides the
    edges surrounding only the currently oversized faces. BMesh updates the
    neighboring topology at shared edges, preserving a closed two-manifold
    surface without propagating refinement across the whole sculpture.
    """
    original_oversize = oversized_faces(model, settings)
    if not original_oversize:
        return model, 0

    room_x, room_y = _face_room(settings)
    # A little breathing room prevents numerically exact bed-edge fits.
    target_x = room_x * 0.97
    target_y = room_y * 0.97
    maximum_faces = 5000
    maximum_passes = 12

    working = bmesh.new()
    try:
        bm_vertices = [working.verts.new(vertex) for vertex in model.vertices]
        for face in model.faces:
            working.faces.new([bm_vertices[index] for index in face])
        working.verts.ensure_lookup_table()
        working.faces.ensure_lookup_table()
        working.normal_update()

        for _pass_index in range(maximum_passes):
            oversized_bm_faces = []
            for face in working.faces:
                points = [vertex.co.copy() for vertex in face.verts]
                if not _face_fits_dimensions(
                    points,
                    list(range(len(points))),
                    target_x,
                    target_y,
                ):
                    oversized_bm_faces.append(face)
            if not oversized_bm_faces:
                break
            if len(working.faces) >= maximum_faces:
                raise PolyshellError(
                    "Automatic splitting would create more than 5,000 panels. "
                    "Reduce the sculpture size or use a larger print bed."
                )
            # Grid subdivision cannot fill a many-sided face, so a large n-gon
            # (a cylinder cap, say) is first fanned into triangles about its centre.
            ngons = [face for face in oversized_bm_faces if len(face.verts) > 4]
            if ngons:
                bmesh.ops.poke(working, faces=ngons)
                working.normal_update()
                continue
            edges = list({edge for face in oversized_bm_faces for edge in face.edges})
            previous_face_count = len(working.faces)
            bmesh.ops.subdivide_edges(
                working,
                edges=edges,
                cuts=1,
                use_grid_fill=True,
                use_only_quads=False,
            )
            working.normal_update()
            if len(working.faces) <= previous_face_count:
                raise PolyshellError(
                    "Automatic splitting could not refine the oversized faces. "
                    "Triangulate the red faces manually and try again."
                )
        else:
            raise PolyshellError(
                "Automatic splitting did not converge. Reduce the sculpture size or use a larger print bed."
            )

        # Subdividing an oversize face also puts a vertex in the middle of each
        # neighbour's shared edge; those neighbours are re-cut into clean triangles.
        _split_straight_corners(working)
        working.verts.ensure_lookup_table()
        working.verts.index_update()
        working.faces.ensure_lookup_table()
        working.faces.index_update()
        vertices = [vertex.co.copy() for vertex in working.verts]
        faces = [[vertex.index for vertex in face.verts] for face in working.faces]
    finally:
        working.free()

    def point_on_segment(point: Vector, a: Vector, b: Vector, tolerance: float) -> bool:
        segment = b - a
        length_squared = segment.length_squared
        if length_squared < 1e-15:
            return (point - a).length <= tolerance
        factor = (point - a).dot(segment) / length_squared
        if factor < -1e-7 or factor > 1.0 + 1e-7:
            return False
        return (point - (a + segment * factor)).length <= tolerance

    # Preserve selected-edge overrides on the smaller edge segments that still
    # lie along an original source edge. New internal seams use automatic keys.
    source_edges: dict[tuple[int, int], int] = {}
    original_edges = [
        (model.vertices[a], model.vertices[b], source_index)
        for (a, b), source_index in model.source_edge_indices.items()
    ]
    tolerance = max(1e-5, model.max_dimension * 1e-8)
    derived_edges = {
        tuple(sorted((face[index], face[(index + 1) % len(face)])))
        for face in faces
        for index in range(len(face))
    }
    for edge_key in derived_edges:
        p, q = vertices[edge_key[0]], vertices[edge_key[1]]
        for a, b, source_index in original_edges:
            if point_on_segment(p, a, b, tolerance) and point_on_segment(q, a, b, tolerance):
                source_edges[edge_key] = source_index
                break

    adjacency: dict[tuple[int, int], list[EdgeUse]] = {}
    for face_index, face in enumerate(faces):
        for slot, a in enumerate(face):
            b = face[(slot + 1) % len(face)]
            adjacency.setdefault(tuple(sorted((a, b))), []).append(EdgeUse(face_index, slot, a, b))
    bad_edges = [edge for edge, uses in adjacency.items() if len(uses) != 2]
    if bad_edges:
        raise PolyshellError(
            f"Automatic splitting produced {len(bad_edges)} non-manifold edges; the operation was cancelled."
        )

    normals: list[Vector] = []
    centroids: list[Vector] = []
    for face in faces:
        points = [vertices[index] for index in face]
        normal = _newell_normal(points)
        normals.append(normal)
        centroids.append(sum(points, Vector()) / len(points))

    added = len(faces) - len(model.faces)
    # The operators report the split themselves; repeating it here showed the
    # same news twice in the sidebar.
    warnings = list(model.warnings)
    result = ModelData(
        model.source,
        vertices,
        faces,
        normals,
        centroids,
        adjacency,
        source_edges,
        model.scale_factor,
        model.max_dimension,
        warnings,
    )
    remaining = oversized_faces(result, settings)
    if remaining:
        raise PolyshellError(
            f"Automatic splitting left {len(remaining)} oversized panels. "
            "Reduce the sculpture size or use a larger print bed."
        )
    return result, added


def create_reference(model: ModelData, oversize: list[int]) -> bpy.types.Object:
    remove_collection_tree(REFERENCE_COLLECTION)
    collection = create_scene_collection(REFERENCE_COLLECTION)
    mesh = bpy.data.meshes.new("PSB_Scaled_Source_Mesh")
    mesh.from_pydata(model.vertices, [], model.faces)
    mesh.update()
    obj = bpy.data.objects.new("PSB_Scaled_Source", mesh)
    collection.objects.link(obj)
    good = _material("PSB_Validated", (0.10, 0.55, 0.20, 0.45))
    bad = _material("PSB_Oversize", (0.95, 0.05, 0.03, 0.9))
    obj.data.materials.append(good)
    obj.data.materials.append(bad)
    oversize_set = set(oversize)
    for polygon in obj.data.polygons:
        polygon.material_index = 1 if polygon.index in oversize_set else 0
    obj.color = (0.10, 0.55, 0.20, 0.45)
    obj.show_name = True
    obj["psb_kind"] = "scaled_reference"
    obj["psb_source_name"] = model.source.name
    return obj


def _neighbor_face(model: ModelData, face_index: int, a: int, b: int) -> int:
    uses = model.adjacency[tuple(sorted((a, b)))]
    return uses[0].face_index if uses[1].face_index == face_index else uses[1].face_index


def interior_dihedral_deg(model: ModelData, face_index: int, a: int, b: int) -> float:
    """Angle between two panels measured through the sculpture's material.

    Below 180 degrees the edge is convex, above it the edge folds inward. The
    far vertex of the neighboring face gives a local, well conditioned test that
    stays reliable when the two normals are almost identical.
    """
    other = _neighbor_face(model, face_index, a, b)
    normal, other_normal = model.normals[face_index], model.normals[other]
    far = next(
        (index for index in model.faces[other] if index not in (a, b)),
        model.faces[other][0],
    )
    convex = normal.dot(model.vertices[far] - model.vertices[a]) < 0.0
    between = math.degrees(math.acos(max(-1.0, min(1.0, normal.dot(other_normal)))))
    return 180.0 - between if convex else 180.0 + between


def _inset_rate(dihedral_deg: float, settings) -> float:
    """Inward travel of a mitered side wall per unit of panel depth.

    Positive values step the wall inward (ordinary convex edges); negative
    values lean it out past the printed outline (inward-folding edges).
    Equal to cot(theta / 2), clamped so a razor-thin dihedral cannot drive the
    offset to infinity.
    """
    limit = max(5.0, settings.min_dihedral_deg)
    clamped = max(limit, min(360.0 - limit, dihedral_deg))
    half = math.radians(clamped * 0.5)
    rate = math.cos(half) / max(1e-6, math.sin(half))
    # A wall leaning out past MAX_WALL_LEAN_DEG cannot print without support, so
    # the exact miter is unusable there regardless. Both panels pull back by the
    # same amount, which opens a small groove rather than an interference.
    return max(rate, -math.tan(math.radians(MAX_WALL_LEAN_DEG)))


def _edge_inward_normals(polygon: list[core.Point2]) -> list[core.Point2]:
    """Unit normal of every edge pointing toward the polygon interior."""
    sign = 1.0 if core.polygon_area(polygon) > 0.0 else -1.0
    normals: list[core.Point2] = []
    for index in range(len(polygon)):
        ax, ay = polygon[index]
        bx, by = polygon[(index + 1) % len(polygon)]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length < 1e-12:
            normals.append((0.0, 0.0))
            continue
        normals.append((-dy / length * sign, dx / length * sign))
    return normals


def _offset_polygon(
    polygon: list[core.Point2],
    normals: list[core.Point2],
    distances: list[float],
) -> list[core.Point2 | None]:
    """Intersect neighboring displaced edge lines to build an offset outline."""
    count = len(polygon)
    corners: list[core.Point2 | None] = []
    for index in range(count):
        previous = index - 1
        pa, pb = core.offset_line(
            polygon[previous], polygon[index], normals[previous], distances[previous]
        )
        na, nb = core.offset_line(
            polygon[index], polygon[(index + 1) % count], normals[index], distances[index]
        )
        corners.append(
            core.line_intersection(pa, (pb[0] - pa[0], pb[1] - pa[1]), na, (nb[0] - na[0], nb[1] - na[1]))
        )
    return corners


def _clip_half_plane(
    loop: list[core.Point2],
    owners: list[int],
    origin: core.Point2,
    normal: core.Point2,
    offset: float,
    tag: int,
) -> tuple[list[core.Point2], list[int]]:
    """Keep the side of a line, remembering which constraint made each segment."""

    def depth(point: core.Point2) -> float:
        return (point[0] - origin[0]) * normal[0] + (point[1] - origin[1]) * normal[1] - offset

    out_points: list[core.Point2] = []
    out_owners: list[int] = []
    count = len(loop)
    for index in range(count):
        current, following = loop[index], loop[(index + 1) % count]
        owner = owners[index]
        here, there = depth(current), depth(following)
        if here >= -1e-9:
            out_points.append(current)
            out_owners.append(owner)
            if there < -1e-9:
                blend = here / (here - there)
                out_points.append(
                    (
                        current[0] + (following[0] - current[0]) * blend,
                        current[1] + (following[1] - current[1]) * blend,
                    )
                )
                out_owners.append(tag)
        elif there >= -1e-9:
            blend = here / (here - there)
            out_points.append(
                (
                    current[0] + (following[0] - current[0]) * blend,
                    current[1] + (following[1] - current[1]) * blend,
                )
            )
            out_owners.append(owner)
    return out_points, out_owners


def _relieved_inner_outline(
    outer: list[core.Point2],
    normals: list[core.Point2],
    distances: list[float],
    relief: float,
    extra: list[tuple[core.Point2, float, int]] | None = None,
) -> tuple[list[core.Point2], list[int], float]:
    """Interior outline as an intersection of half-planes, corners cut back.

    Every mitered wall is a half-plane, and every corner relief is one more, so
    intersecting them can never produce the self-crossing outline that pairwise
    line intersection does once one edge's miter runs deeper than its neighbors.
    The relief plane sits `relief` from the shared mesh vertex, which is what
    keeps panels around a high-valence vertex clear of one another.
    """
    count = len(outer)
    span = max(abs(value) for value in distances) + relief + 10.0
    x0 = min(point[0] for point in outer) - span
    x1 = max(point[0] for point in outer) + span
    y0 = min(point[1] for point in outer) - span
    y1 = max(point[1] for point in outer) + span
    loop: list[core.Point2] = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    owners: list[int] = [-1, -1, -1, -1]

    for index in range(count):
        loop, owners = _clip_half_plane(
            loop, owners, outer[index], normals[index], distances[index], index
        )
        if len(loop) < 3:
            return [], [], 0.0
    for index in range(count):
        bisector = (
            normals[index - 1][0] + normals[index][0],
            normals[index - 1][1] + normals[index][1],
        )
        length = math.hypot(*bisector)
        if length < 1e-9:
            continue
        bisector = (bisector[0] / length, bisector[1] / length)
        loop, owners = _clip_half_plane(
            loop, owners, outer[index], bisector, relief, count + index
        )
        if len(loop) < 3:
            return [], [], 0.0
    # Faces that touch one of this panel's vertices without sharing an edge are
    # still able to collide with it. Keeping only the part of the shell that is
    # nearer to this face than to any of them makes overlap impossible, because
    # the neighbor is clipped by the very same plane from the other side.
    for normal, offset, tag in extra or ():
        loop, owners = _clip_half_plane(loop, owners, (0.0, 0.0), normal, offset, tag)
        if len(loop) < 3:
            return [], [], 0.0

    cleaned: list[core.Point2] = []
    cleaned_owners: list[int] = []
    # Points closer than a tenth of a micron are the same corner reached by two
    # clips; keeping both only adds sliver faces.
    for index, point in enumerate(loop):
        if cleaned and math.dist(cleaned[-1], point) < 1e-4:
            cleaned_owners[-1] = owners[index]
            continue
        cleaned.append(point)
        cleaned_owners.append(owners[index])
    if len(cleaned) > 2 and math.dist(cleaned[0], cleaned[-1]) < 1e-4:
        cleaned.pop()
        cleaned_owners.pop()
    # A clip that coincides with a wall -- a face touching only this corner but
    # coplanar with the edge neighbor -- splits one straight side in two. The
    # extra vertex would give the interior face a zero-area triangle.
    removed = True
    while removed and len(cleaned) > 3:
        removed = False
        total = len(cleaned)
        for index in range(total):
            previous, point, following = cleaned[index - 1], cleaned[index], cleaned[(index + 1) % total]
            base = math.dist(previous, following)
            if base < 1e-9:
                continue
            offset = abs(
                (following[0] - previous[0]) * (point[1] - previous[1])
                - (following[1] - previous[1]) * (point[0] - previous[0])
            ) / base
            if offset >= 1e-5:
                continue
            incoming, outgoing = cleaned_owners[index - 1], cleaned_owners[index]
            # The merged side is a wall if either half was one.
            if not 0 <= incoming < count and 0 <= outgoing < count:
                cleaned_owners[index - 1] = outgoing
            del cleaned[index]
            del cleaned_owners[index]
            removed = True
            break
    # Clipping starts from a counter-clockwise box, so the result has to be put
    # back into the source outline's winding or the wall band stitches to the
    # wrong rim vertex and the shell comes out non-manifold.
    if len(cleaned) >= 3 and core.polygon_area(cleaned) * core.polygon_area(outer) < 0.0:
        total = len(cleaned)
        cleaned.reverse()
        cleaned_owners = [cleaned_owners[(total - 2 - index) % total] for index in range(total)]
    longest = max(
        (min(math.dist(point, vertex) for point in cleaned) for vertex in outer),
        default=0.0,
    )
    return cleaned, cleaned_owners, longest


def _vertex_faces(model: ModelData) -> dict[int, list[int]]:
    cached = getattr(model, "_vertex_faces_cache", None)
    if cached is None:
        cached = {}
        for face_index, face in enumerate(model.faces):
            for vertex_id in face:
                cached.setdefault(vertex_id, []).append(face_index)
        model._vertex_faces_cache = cached
    return cached


def _vertex_bisector_planes(
    model: ModelData,
    face_index: int,
    vertex_ids: list[int],
    matrix_world: Matrix,
    centroid: Vector,
    x_axis: Vector,
    y_axis: Vector,
    inward: Vector,
    thickness: float,
    outer_only: bool = False,
) -> list[tuple[core.Point2, float, int]]:
    """Half-planes keeping only the shell nearer to this face than to a neighbor.

    For edge-adjacent faces this is exactly the miter plane already in use, so
    only the faces that meet at a vertex without sharing an edge are added here.
    With `outer_only` the weaker constraint is used instead: stay behind the
    neighbor's printed face, which keeps the panel inside the sculpture even
    where the nearest-face split would erase one of its own walls.
    """
    vertex_faces = _vertex_faces(model)
    normal = model.normals[face_index]
    plane = normal.dot(model.vertices[model.faces[face_index][0]])
    adjacent = {
        _neighbor_face(model, face_index, vertex_ids[i], vertex_ids[(i + 1) % len(vertex_ids)])
        for i in range(len(vertex_ids))
    }
    planes: list[tuple[core.Point2, float, int]] = []
    seen: set[int] = set()
    for slot, vertex_id in enumerate(vertex_ids):
        for other in vertex_faces.get(vertex_id, ()):
            if other == face_index or other in adjacent or other in seen:
                continue
            seen.add(other)
            other_normal = model.normals[other]
            other_plane = other_normal.dot(model.vertices[model.faces[other][0]])
            # If this face pokes out through the other face's plane the two are
            # locally concave, the nearest-face split no longer describes the
            # offset shell, and applying it would slice the panel in half.
            if any(
                other_plane - other_normal.dot(model.vertices[index]) < -1e-6
                for index in model.faces[face_index]
            ):
                continue
            difference = other_normal if outer_only else other_normal - normal
            constant = other_plane if outer_only else other_plane - plane
            a = difference.dot(x_axis)
            b = difference.dot(y_axis)
            length = math.hypot(a, b)
            # Faces within about half a degree of coplanar cannot overlap beyond
            # what the corner relief removes, and their split plane is pure
            # rounding noise -- a random cut that could slice the panel anywhere.
            if length < 0.01:
                continue
            offset = constant - difference.dot(centroid) - thickness * difference.dot(inward)
            planes.append(((-a / length, -b / length), -offset / length, 2 * len(vertex_ids) + slot))
    return planes


def _assembly_matrix(centroid: Vector, x_axis: Vector, y_axis: Vector, inward: Vector) -> Matrix:
    return Matrix(
        (
            (x_axis.x, y_axis.x, inward.x, centroid.x),
            (x_axis.y, y_axis.y, inward.y, centroid.y),
            (x_axis.z, y_axis.z, inward.z, centroid.z),
            (0.0, 0.0, 0.0, 1.0),
        )
    )


def _face_is_sound(face: tuple[int, ...], vertices: list[Vector]) -> bool:
    if len(face) < 3 or len(set(face)) != len(face):
        return False
    origin = vertices[face[0]]
    doubled = Vector()
    for index in range(1, len(face) - 1):
        doubled += (vertices[face[index]] - origin).cross(vertices[face[index + 1]] - origin)
    return doubled.length > 1e-9


def _stitch_band(
    rim: list[core.Point2],
    inner: list[core.Point2],
    owners: list[int],
    rim_base: int,
    inner_base: int,
) -> tuple[list[tuple[int, ...]], dict[int, tuple[int, int]]]:
    """Faces joining the rim loop to the interior loop, watertight by construction.

    An interior segment owned by edge e is that edge's mitered wall and sits
    directly over rim edge e. Everything between two such walls -- corner
    reliefs, clips against faces that share only a vertex, walls that vanished
    under deeper neighbors -- is filled by walking both loops together and
    always taking the shorter diagonal, so the band closes whichever clip made
    a segment. Returns the faces and, per rim edge, the interior span above it.
    """
    count, inner_count = len(rim), len(inner)
    faces: list[tuple[int, ...]] = []
    segments: dict[int, tuple[int, int]] = {}

    def stitch(r: int, r_end: int, k: int, k_end: int) -> None:
        while r < r_end or k < k_end:
            take_inner = r >= r_end or (
                k < k_end
                and math.dist(rim[r % count], inner[(k + 1) % inner_count])
                <= math.dist(rim[(r + 1) % count], inner[k % inner_count])
            )
            if take_inner:
                faces.append((rim_base + r % count, inner_base + (k + 1) % inner_count, inner_base + k % inner_count))
                k += 1
            else:
                faces.append((rim_base + r % count, rim_base + (r + 1) % count, inner_base + k % inner_count))
                segments.setdefault(r % count, (k % inner_count, k % inner_count))
                r += 1

    # Walls of a convex interior appear in the same cyclic order as their edges;
    # a wall out of that order is stitched like any other segment, not trusted.
    pins: list[tuple[int, int]] = []
    for k, owner in enumerate(owners):
        if 0 <= owner < count:
            rank = (owner - pins[0][1]) % count if pins else 0
            if not pins or rank > (pins[-1][1] - pins[0][1]) % count:
                pins.append((k, owner))
    if not pins:
        start = min(range(inner_count), key=lambda k: math.dist(rim[0], inner[k]))
        stitch(0, count, start, start + inner_count)
        return faces, segments
    for position, (k, edge) in enumerate(pins):
        following = (k + 1) % inner_count
        faces.append((rim_base + edge, rim_base + (edge + 1) % count, inner_base + following, inner_base + k))
        segments[edge] = (k, following)
        next_k, next_edge = pins[(position + 1) % len(pins)]
        r_start, k_start = edge + 1, k + 1
        stitch(
            r_start, r_start + (next_edge - r_start) % count,
            k_start, k_start + (next_k - k_start) % inner_count,
        )
    return faces, segments


def _wall_depth_limits(model: ModelData) -> list[float]:
    """Deepest each panel may be before it meets the panel on the far wall.

    Where the sculpture is thinner than two skins (an ear, a tail, a fin) a
    full-depth panel runs into the panel on the opposite side, and where it is
    thinner than one skin it comes out through that panel's printed face.
    Rays are cast inward from points across each face; the first surface they
    meet that does not share a vertex with the face (folds with neighbors are
    the miters' job) is the far wall, and each side gets half the distance.
    """
    tree = BVHTree.FromPolygons(model.vertices, model.faces, epsilon=0.0)
    vertex_faces = _vertex_faces(model)
    limits: list[float] = []
    for face_index, face in enumerate(model.faces):
        near = {other for vertex in face for other in vertex_faces.get(vertex, ())}
        inward = -model.normals[face_index]
        points = [model.vertices[index] for index in face]
        centroid = sum(points, Vector()) / len(points)
        samples = [centroid]
        for index, point in enumerate(points):
            following = points[(index + 1) % len(points)]
            samples.append(centroid.lerp(point, 0.7))
            samples.append(centroid.lerp((point + following) * 0.5, 0.7))
        best = math.inf
        for sample in samples:
            origin = sample + inward * 1e-3
            travelled = 1e-3
            for _hop in range(8):
                hit, _normal, index, distance = tree.ray_cast(origin, inward)
                if hit is None:
                    break
                travelled += distance
                if index not in near:
                    best = min(best, travelled)
                    break
                origin = hit + inward * 1e-3
                travelled += 1e-3
        limits.append(best * 0.5 - 0.05 if best < math.inf else math.inf)
    return limits


def _build_shell(model: ModelData, face_index: int, settings, depth_limit: float = math.inf) -> FaceBuild:
    """Build one panel as an outer rim, a mitered wall, and a relieved interior.

    Three stacked outlines are produced:
      z = 0        printed outer face, pulled in by the edge relief
      z = rim      top of the vertical rim; the miter starts here
      z = thickness  interior face, with every corner cut back to the miter limit

    The vertical rim keeps the first layers of the exterior edge square instead
    of tapering to an unprintable feather, and the corner cut-back keeps panels
    that share a high-valence vertex from colliding.
    """
    thickness = settings.panel_thickness_mm
    floor_depth = max(0.4, min(thickness, settings.nozzle_mm * 2.0))
    # Never deeper than half the distance to the opposite wall of the sculpture.
    thickness = max(floor_depth, min(thickness, depth_limit))
    vertex_ids = model.faces[face_index]
    points = [model.vertices[i] for i in vertex_ids]
    normal = model.normals[face_index]
    inward = -normal
    centroid, x_axis, _unused = _face_basis(points, normal)
    # A right-handed assembly basis avoids Blender decomposing every panel
    # transform into a confusing negative scale.
    y_axis = inward.cross(x_axis).normalized()
    matrix_world = _assembly_matrix(centroid, x_axis, y_axis, inward)
    inverse = matrix_world.inverted()

    true_local = [inverse @ point for point in points]
    true_outline = [(point.x, point.y) for point in true_local]
    normals2 = _edge_inward_normals(true_outline)
    count = len(vertex_ids)

    dihedrals = [
        interior_dihedral_deg(model, face_index, vertex_ids[i], vertex_ids[(i + 1) % count])
        for i in range(count)
    ]
    rates = [_inset_rate(angle, settings) for angle in dihedrals]

    # A miter runs `cot(dihedral / 2)` inward per unit of depth, so a small face
    # ringed by sharp folds can have its whole interior consumed. The miter
    # plane itself does not depend on thickness, so making just this panel
    # shallower keeps its wall in exactly the same plane as its neighbor's --
    # the glue face simply gets narrower instead of the panel disappearing.
    outer_area = abs(core.polygon_area(true_outline))
    minimum_area = outer_area * 0.12
    inner_outline: list[core.Point2] = []
    owners: list[int] = []
    longest_run = 0.0
    relief = max(0.05, settings.corner_relief_mm)
    resolved = thickness
    fallback: tuple[list[core.Point2], list[int], float, float, float] | None = None
    floor = max(0.4, min(thickness, settings.nozzle_mm * 2.0))
    for depth_scale in (1.0, 0.8, 0.62, 0.48, 0.36, 0.26, 0.18, 0.12, 0.08):
        trial_thickness = max(floor, thickness * depth_scale)
        inner_distances = [rate * trial_thickness for rate in rates]
        planes = _vertex_bisector_planes(
            model, face_index, vertex_ids, matrix_world, centroid, x_axis, y_axis, inward,
            trial_thickness,
        )
        # When the nearest-face split erases one of this panel's own walls, still
        # keep it behind its corner neighbors' printed faces; dropping every
        # constraint let panels come out through a neighbor's face by millimetres.
        outer_planes = _vertex_bisector_planes(
            model, face_index, vertex_ids, matrix_world, centroid, x_axis, y_axis, inward,
            trial_thickness, outer_only=True,
        )
        best: tuple[list[core.Point2], list[int], float] | None = None
        # Relaxing the relief is preferable to emitting a panel whose mitered
        # wall has been clipped away entirely, which would leave a hole.
        for constraints in (planes, outer_planes, []):
            for scale in (1.0, 0.7, 0.45, 0.25, 0.1):
                trial_relief = max(0.05, settings.corner_relief_mm * scale)
                candidate, candidate_owners, run = _relieved_inner_outline(
                    true_outline, normals2, inner_distances, trial_relief, constraints
                )
                if len(candidate) < 3:
                    continue
                if best is None:
                    best = (candidate, candidate_owners, run, trial_relief)
                if all(owner in candidate_owners for owner in range(count)):
                    best = (candidate, candidate_owners, run, trial_relief)
                    break
            if best is not None and all(owner in best[1] for owner in range(count)):
                break
        if best is None:
            if trial_thickness <= floor:
                break
            continue
        if fallback is None:
            fallback = (*best, trial_thickness)
        # A three-point sliver technically closes, but its wall band degenerates
        # into zero-area faces. Keep giving up depth until real interior remains.
        if abs(core.polygon_area(best[0])) >= minimum_area or trial_thickness <= floor:
            inner_outline, owners, longest_run, relief = best
            resolved = trial_thickness
            break
        if trial_thickness <= floor:
            break
    if len(inner_outline) < 3 and fallback is not None:
        inner_outline, owners, longest_run, relief, resolved = fallback
    thickness = resolved

    rim_height = max(0.0, min(settings.edge_relief_mm, thickness * 0.45))
    # Inward-folding edges already present solid material at the outer face, so
    # only convex edges need their feather edge blunted. The rim is inset by
    # exactly the miter at its own height, so it never stands proud of the glue
    # plane. A very sharp fold has a nearly flat miter, which would open a wide
    # visible groove along the seam, so such a panel gets a lower rim instead;
    # capping the inset alone pushed the rim up to 1.3 mm into the neighbor.
    rim_cap = max(0.6, settings.edge_relief_mm * 1.5)
    steepest = max((rate for rate in rates if rate > 0.0), default=0.0)
    if steepest * rim_height > rim_cap:
        rim_height = rim_cap / steepest
    rim_distances = [max(0.0, rate) * rim_height for rate in rates]
    rim_outline_raw = _offset_polygon(true_outline, normals2, rim_distances)
    rim_outline = [
        point if point is not None else true_outline[index]
        for index, point in enumerate(rim_outline_raw)
    ]

    if len(inner_outline) < 3:
        # Nothing survived even at the floor depth. Emit a plain shallow plate so
        # the shell stays closed and the panel is reported rather than crashing.
        inner_outline, owners, longest_run = _relieved_inner_outline(
            true_outline, normals2, [floor * 0.5] * count, max(0.05, floor), []
        )
        if len(inner_outline) < 3:
            inner_outline = list(rim_outline)
            owners = list(range(count))
            longest_run = 0.0

    outer_local = [Vector((x, y, 0.0)) for x, y in rim_outline]
    inner_local = [Vector((x, y, thickness)) for x, y in inner_outline]
    # Faces are wound for a counter-clockwise outline (printed face down, walls
    # and interior outward) and all reversed below if the outline runs the
    # other way, so the shell is consistent without any normal recalculation.
    faces: list[tuple[int, ...]] = [tuple(reversed(range(count)))]
    if rim_height >= 0.01:
        rim_local = [Vector((x, y, rim_height)) for x, y in rim_outline]
        vertices: list[Vector] = list(outer_local) + list(rim_local) + list(inner_local)
        rim_base = count
        inner_base = count * 2
        for index in range(count):
            following = (index + 1) % count
            faces.append((index, following, rim_base + following, rim_base + index))
    else:
        # With no rim the miter starts at the printed face itself; a second loop
        # at the same height would only add zero-area faces and open the shell.
        rim_height = 0.0
        vertices = list(outer_local) + list(inner_local)
        rim_base = 0
        inner_base = count

    band, edge_segments = _stitch_band(rim_outline, inner_outline, owners, rim_base, inner_base)
    faces.extend(band)
    inner_count = len(inner_outline)
    for index in range(1, inner_count - 1):
        faces.append((inner_base, inner_base + index, inner_base + index + 1))

    inner_edge_by_key: dict[tuple[int, int], tuple[Vector, Vector]] = {}
    rate_by_key: dict[tuple[int, int], float] = {}
    dihedral_by_key: dict[tuple[int, int], float] = {}
    for index in range(count):
        a = vertex_ids[index]
        b = vertex_ids[(index + 1) % count]
        key = tuple(sorted((a, b)))
        first, last = edge_segments.get(index, (0, 0))
        start = matrix_world @ inner_local[first]
        finish = matrix_world @ inner_local[last]
        inner_edge_by_key[key] = (start, finish) if a <= b else (finish, start)
        rate_by_key[key] = rates[index]
        dihedral_by_key[key] = dihedrals[index]

    if core.polygon_area(true_outline) < 0.0:
        faces = [tuple(reversed(face)) for face in faces]
    # Safety net: a face with repeated corners or no measurable area contributes
    # nothing but breaks manifold checks and slicers downstream.
    faces = [face for face in faces if _face_is_sound(face, vertices)]
    faces = _outward(faces, vertices)

    build = FaceBuild(
        face_index=face_index,
        panel_id=core.panel_id(face_index),
        vertex_ids=vertex_ids,
        normal=normal,
        centroid_world=centroid,
        x_axis=x_axis,
        y_axis=y_axis,
        inward_axis=inward,
        matrix_world=matrix_world,
        matrix_inverse=inverse,
        vertices=vertices,
        faces=faces,
        outer_local=outer_local,
        inner_local=inner_local,
        inner_world_by_vertex={},
        inner_polygon=list(inner_outline),
        inner_edge_by_key=inner_edge_by_key,
        rate_by_key=rate_by_key,
        dihedral_by_key=dihedral_by_key,
        rim_height=rim_height,
        thickness=thickness,
        longest_corner_run=longest_run,
    )
    outer_area = abs(core.polygon_area(true_outline))
    build.inner_area_ratio = abs(core.polygon_area(inner_outline)) / max(1e-9, outer_area)
    build.inner_is_simple = core.polygon_is_simple(inner_outline)
    return build


def _tapered_box(
    center: core.Point2,
    u_axis: core.Point2,
    v_axis: core.Point2,
    width: float,
    depth: float,
    z0: float,
    height: float,
    top_inset: float = 0.0,
) -> tuple[list[Vector], list[tuple[int, ...]]]:
    ux, uy = u_axis
    vx, vy = v_axis
    widths = (width * 0.5, max(0.05, width * 0.5 - top_inset))
    depths = (depth * 0.5, max(0.05, depth * 0.5 - top_inset))
    vertices: list[Vector] = []
    for level in range(2):
        hw, hd = widths[level], depths[level]
        z = z0 + height * level
        for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            vertices.append(Vector((center[0] + ux * hw * su + vx * hd * sv, center[1] + uy * hw * su + vy * hd * sv, z)))
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return vertices, faces


def _profile_prism(
    origin: core.Point2,
    u_axis: Vector,
    v_axis: Vector,
    profile: list[tuple[float, float]],
    thickness: float,
    z0: float,
) -> tuple[list[Vector], list[tuple[int, ...]]]:
    """A (distance, height) profile swept `thickness` across the edge."""
    profile = _without_repeats(profile, 1e-4)
    vertices: list[Vector] = []
    for side in (-0.5, 0.5):
        for distance, height in profile:
            vertices.append(
                Vector(
                    (
                        origin[0] + u_axis.x * thickness * side + v_axis.x * distance,
                        origin[1] + u_axis.y * thickness * side + v_axis.y * distance,
                        z0 + height,
                    )
                )
            )
    count = len(profile)
    faces: list[tuple[int, ...]] = [tuple(reversed(range(count))), tuple(count + i for i in range(count))]
    faces.extend((i, (i + 1) % count, count + (i + 1) % count, count + i) for i in range(count))
    return vertices, faces


def _wedge_profile(
    start: float,
    end: float,
    height: float,
    wedge_tan: float | None,
    margin: float,
    minimum: float = 0.5,
) -> list[tuple[float, float]] | None:
    """Wall profile that stays inside the gap between two panels.

    Near a joint sharper than 90 degrees the mating panel leans over this one,
    so a full-height wall standing at the inner corner would run straight into
    it. The allowed height grows with distance from the corner as
    `distance * tan(dihedral) - margin`; the wall follows that line and then
    runs flat at full height.
    """
    if wedge_tan is None:
        return [(start, 0.0), (end, 0.0), (end, height), (start, height)]
    first = max(start, (minimum + margin) / wedge_tan)
    if first >= end - 1e-6:
        return None
    full = (height + margin) / wedge_tan
    top_end = min(height, end * wedge_tan - margin)
    profile = [(first, 0.0), (end, 0.0), (end, top_end)]
    if first < full < end:
        profile.append((full, height))
    profile.append((first, min(height, first * wedge_tan - margin)))
    return profile


def _ramp(
    start_center: core.Point2,
    u_axis: Vector,
    v_axis: Vector,
    width: float,
    depth: float,
    z0: float,
    height: float,
) -> tuple[list[Vector], list[tuple[int, ...]]]:
    """Closed triangular-prism ramp; every upper layer is supported by the one below."""
    vertices: list[Vector] = []
    center = Vector(start_center)
    for side in (-1.0, 1.0):
        offset = u_axis * (width * 0.5 * side)
        p0 = center + offset
        p1 = center + offset + v_axis * depth
        vertices.extend((Vector((p0.x, p0.y, z0)), Vector((p1.x, p1.y, z0)), Vector((p1.x, p1.y, z0 + height))))
    faces = [(0, 2, 1), (3, 4, 5), (0, 1, 4, 3), (1, 2, 5, 4), (2, 0, 3, 5)]
    return vertices, faces


def _section_prism_world(section: list[Vector], edge_axis: Vector, width: float) -> tuple[list[Vector], list[tuple[int, ...]]]:
    section = _without_repeats(section, 1e-3)
    half = edge_axis.normalized() * (width * 0.5)
    vertices = [point + offset for offset in (-half, half) for point in section]
    count = len(section)
    faces: list[tuple[int, ...]] = [tuple(reversed(range(count))), tuple(count + i for i in range(count))]
    faces.extend((i, (i + 1) % count, count + (i + 1) % count, count + i) for i in range(count))
    return vertices, faces


def _outward(faces: list[tuple[int, ...]], vertices) -> list[tuple[int, ...]]:
    """Wind a closed piece so its normals face out, whatever handedness built it."""
    if _signed_volume(vertices, faces) < 0.0:
        return [tuple(reversed(face)) for face in faces]
    return faces


def _without_repeats(points: list, tolerance: float) -> list:
    """Drop consecutive (and wrap-around) points closer than `tolerance`.

    A repeated corner turns one side of a prism into a zero-area face, which
    slicers flag and which leaves the piece's orientation undefined.
    """
    kept: list = []
    for point in points:
        if kept and math.dist(tuple(kept[-1]), tuple(point)) < tolerance:
            continue
        kept.append(point)
    while len(kept) > 2 and math.dist(tuple(kept[0]), tuple(kept[-1])) < tolerance:
        kept.pop()
    return kept


def _commit(build: FaceBuild, vertices: list[Vector], faces: list[tuple[int, ...]]) -> None:
    faces = _outward(faces, vertices)
    start = len(build.vertices)
    build.vertices.extend(vertices)
    build.faces.extend(tuple(start + i for i in face) for face in faces)


def _append_tapered_box(build: FaceBuild, *args, **kwargs) -> None:
    _commit(build, *_tapered_box(*args, **kwargs))


def _to_world(build: FaceBuild, points: list[Vector]) -> list[tuple[float, float, float]]:
    matrix = build.matrix_world
    return [tuple(float(c) for c in (matrix @ point)) for point in points]


def _edge_frame(build: FaceBuild, key: tuple[int, int]) -> tuple[Vector, core.Point2, core.Point2]:
    """Inward direction and inner-edge endpoints of one edge, in panel local 2-D."""
    start, finish = build.inner_edge_by_key[key]
    pa = build.matrix_inverse @ start
    pb = build.matrix_inverse @ finish
    edge2 = Vector((pb.x - pa.x, pb.y - pa.y))
    if edge2.length < 1e-9:
        edge2 = Vector((1.0, 0.0))
    edge2.normalize()
    mid = Vector(((pa.x + pb.x) * 0.5, (pa.y + pb.y) * 0.5))
    inward2 = Vector(core.polygon_centroid(build.inner_polygon)) - mid
    inward2 -= edge2 * inward2.dot(edge2)
    if inward2.length < 1e-8:
        inward2 = Vector((-edge2.y, edge2.x))
    inward2.normalize()
    return inward2, (pa.x, pa.y), (pb.x, pb.y)


def _reading_basis(edge: Vector, inward: Vector) -> Vector:
    """Flip an edge direction so text laid on (edge, inward) is never mirrored.

    Panels are printed interior-face up and read from inside the sculpture, so a
    label is only the right way round when its horizontal axis crossed with its
    vertical axis points along local +Z. The edge key is sorted by vertex index,
    which is unrelated to panel winding, so half of all edges arrive reversed.
    """
    if edge.x * inward.y - edge.y * inward.x < 0.0:
        return -edge
    return edge


def _reserve(
    build: FaceBuild,
    center: core.Point2,
    u: Vector,
    v: Vector,
    width: float,
    depth: float,
    hard: bool = False,
) -> None:
    """Mark an area of the interior face as taken so labels avoid it.

    Hard areas are where a mating part has to land -- a cradle cavity, the path
    a key takes into it, a spline channel. Nothing may be put there, not even a
    last-resort label, because anything raised there holds the joint open.
    """
    box = core.oriented_box(center, (u.x, u.y), (v.x, v.y), width, depth)
    build.reserved.append(box)
    if hard:
        build.hard_reserved.append(box)


def _area_is_free(
    build: FaceBuild,
    center: core.Point2,
    u: Vector,
    v: Vector,
    width: float,
    depth: float,
    hard_only: bool = False,
) -> bool:
    box = core.oriented_box(center, (u.x, u.y), (v.x, v.y), width, depth)
    taken = build.hard_reserved if hard_only else build.reserved
    return not any(core.convex_shapes_overlap(box, item) for item in taken)


def _corners_inside(poly: list[core.Point2], center: core.Point2, u: Vector, v: Vector, width: float, depth: float) -> bool:
    for su in (-0.5, 0.5):
        for sv in (-0.5, 0.5):
            point = (center[0] + u.x * width * su + v.x * depth * sv, center[1] + u.y * width * su + v.y * depth * sv)
            if not core.point_in_polygon(point, poly):
                return False
    return True


def _auto_count(edge_length: float, settings) -> int:
    if edge_length < max(12.0, settings.tab_width_mm * 1.35):
        return 0
    if edge_length < settings.short_edge_mm:
        return 1
    if edge_length < settings.long_edge_mm:
        return 2
    return 3


def _wedge_tangent(dihedral_deg: float) -> float | None:
    """tan(dihedral) for joints sharp enough that the mating panel leans over.

    From a right angle upward the mating panel leans away from a vertical wall
    standing at the corner, so no limit applies.
    """
    if dihedral_deg >= 89.5:
        return None
    return math.tan(math.radians(max(5.0, dihedral_deg)))


def _add_joint_pair(
    model: ModelData,
    builds: list[FaceBuild],
    edge_key: tuple[int, int],
    male_index: int,
    female_index: int,
    fraction: float,
    width: float,
    depth: float,
    settings,
    clearance: collide.Clearance,
    height_scale: float = 1.0,
) -> tuple[bool, str]:
    male = builds[male_index]
    female = builds[female_index]
    a, b = edge_key
    outer_a, outer_b = model.vertices[a], model.vertices[b]
    edge_axis = (outer_b - outer_a).normalized()
    tab_height = max(1.0, settings.tab_height_mm * height_scale)
    male_a, male_b = male.inner_edge_by_key[edge_key]
    female_a, female_b = female.inner_edge_by_key[edge_key]
    male_range = sorted(((male_a - outer_a).dot(edge_axis), (male_b - outer_a).dot(edge_axis)))
    female_range = sorted(((female_a - outer_a).dot(edge_axis), (female_b - outer_a).dot(edge_axis)))
    overlap_low, overlap_high = max(male_range[0], female_range[0]), min(male_range[1], female_range[1])
    if overlap_high - overlap_low < width + 1.0:
        return False, f"edge {edge_key}: mitered inner-edge overlap is too short"
    scalar = overlap_low + (overlap_high - overlap_low) * fraction

    def point_at_scalar(p0: Vector, p1: Vector) -> Vector:
        s0, s1 = (p0 - outer_a).dot(edge_axis), (p1 - outer_a).dot(edge_axis)
        if abs(s1 - s0) < 1e-8:
            return (p0 + p1) * 0.5
        return p0.lerp(p1, (scalar - s0) / (s1 - s0))

    male_q = point_at_scalar(male_a, male_b)
    female_q = point_at_scalar(female_a, female_b)

    male_in2, _, _ = _edge_frame(male, edge_key)
    female_in2, _, _ = _edge_frame(female, edge_key)
    male_in_world = (male.x_axis * male_in2.x + male.y_axis * male_in2.y).normalized()
    female_in_world = (female.x_axis * female_in2.x + female.y_axis * female_in2.y).normalized()

    root_depth = max(1.4, depth * 0.55)
    a0 = male_q + male_in_world * root_depth
    surface_b = female_q + female_in_world * depth

    def endpoint(lift: float) -> tuple[Vector, Vector, float]:
        point = surface_b + female.inward_axis * (lift + settings.clearance_mm)
        delta = male.matrix_inverse @ point - (male.matrix_inverse @ male_q)
        horizontal = math.hypot(delta.x, delta.y)
        angle = math.degrees(math.atan2(delta.z, max(1e-9, horizontal)))
        return point, delta, angle

    b0, delta_local, printable_angle = endpoint(0.0)
    lift = 0.0
    if delta_local.z < -1e-5 or printable_angle + 1e-4 < settings.min_joint_angle_deg:
        low, high = 0.0, max(depth * 4.0, tab_height * 6.0)
        high_point, high_delta, high_angle = endpoint(high)
        if high_delta.z < -1e-5 or high_angle + 1e-4 < settings.min_joint_angle_deg:
            return False, f"edge {edge_key}: inward-folding edge, no support-safe crossing"
        for _iteration in range(20):
            middle = (low + high) * 0.5
            _point, _delta, angle = endpoint(middle)
            if _delta.z >= 0.0 and angle >= settings.min_joint_angle_deg:
                high = middle
            else:
                low = middle
        lift = high
        b0, delta_local, printable_angle = endpoint(lift)
    if lift > settings.max_cradle_lift_mm:
        return False, f"edge {edge_key}: cradle would stand {lift:.1f} mm tall"

    male_q_local = male.matrix_inverse @ male_q
    male_root_local = male.matrix_inverse @ a0
    male_u2 = Vector((edge_axis.dot(male.x_axis), edge_axis.dot(male.y_axis))).normalized()
    male_v2 = male_in2
    male_center = ((male_q_local.x + male_root_local.x) * 0.5, (male_q_local.y + male_root_local.y) * 0.5)
    if not _corners_inside(male.inner_polygon, male_center, male_u2, male_v2, width, root_depth * 0.9):
        return False, f"edge {edge_key}: male key root is too close to a corner"

    fq = female.matrix_inverse @ female_q
    female_u2 = Vector((edge_axis.dot(female.x_axis), edge_axis.dot(female.y_axis))).normalized()
    female_v2 = female_in2
    wall = settings.cradle_wall_mm
    cavity_width = width + 2.0 * settings.clearance_mm
    total_depth = depth + wall
    cradle_center = (fq.x + female_v2.x * total_depth * 0.5, fq.y + female_v2.y * total_depth * 0.5)
    check_inset = min(0.25, total_depth * 0.08)
    check_center = (
        cradle_center[0] + female_v2.x * check_inset,
        cradle_center[1] + female_v2.y * check_inset,
    )
    if not _corners_inside(
        female.inner_polygon,
        check_center,
        female_u2,
        female_v2,
        cavity_width + 2.0 * wall,
        max(0.1, total_depth - check_inset * 2.0),
    ):
        return False, f"edge {edge_key}: female pocket is too close to a corner"
    # Another joint's cradle, or a key already crossing onto this panel, owns
    # this patch of the face.
    if not _area_is_free(female, cradle_center, female_u2, female_v2, cavity_width + 2.0 * wall, total_depth):
        return False, f"edge {edge_key}: female pocket overlaps another joint"
    if not _area_is_free(male, male_center, male_u2, male_v2, width, root_depth * 0.9):
        return False, f"edge {edge_key}: male key root overlaps another joint"

    top_a = a0 + male.inward_axis * tab_height
    top_b = b0 + female.inward_axis * tab_height
    section = [a0, male_q]
    if (female_q - male_q).length > 1e-5:
        section.append(female_q)
    section.extend((b0, top_b, top_a))
    key_world, key_faces = _section_prism_world(section, edge_axis, width)

    z0 = female.thickness
    rail_height = lift + tab_height + settings.clearance_mm * 2.0
    rail_depth = depth + wall
    wedge = _wedge_tangent(female.dihedral_by_key[edge_key])
    wedge_margin = 0.25
    if wedge is not None and depth * wedge - wedge_margin < settings.clearance_mm + tab_height * 0.5:
        return False, f"edge {edge_key}: joint too sharp for a cradle at this key height"
    pieces: list[tuple[list[Vector], list[tuple[int, ...]]]] = []
    for sign in (-1.0, 1.0):
        side = (
            fq.x + female_u2.x * sign * (cavity_width + wall) * 0.5,
            fq.y + female_u2.y * sign * (cavity_width + wall) * 0.5,
        )
        if wedge is None:
            center = (side[0] + female_v2.x * rail_depth * 0.5, side[1] + female_v2.y * rail_depth * 0.5)
            pieces.append(
                _tapered_box(
                    center, (female_u2.x, female_u2.y), (female_v2.x, female_v2.y),
                    wall, rail_depth, z0, rail_height, min(0.18, wall * 0.18),
                )
            )
        else:
            profile = _wedge_profile(0.0, rail_depth, rail_height, wedge, wedge_margin)
            if profile is None:
                return False, f"edge {edge_key}: joint too sharp for cradle rails"
            pieces.append(_profile_prism(side, female_u2, female_v2, profile, wall, z0))
    if wedge is None:
        end_center = (fq.x + female_v2.x * (depth + wall * 0.5), fq.y + female_v2.y * (depth + wall * 0.5))
        pieces.append(
            _tapered_box(
                end_center, (female_u2.x, female_u2.y), (female_v2.x, female_v2.y),
                cavity_width + 2.0 * wall, wall, z0, rail_height, min(0.18, wall * 0.18),
            )
        )
    else:
        profile = _wedge_profile(depth, depth + wall, rail_height, wedge, wedge_margin)
        if profile is None:
            return False, f"edge {edge_key}: joint too sharp for a cradle end wall"
        pieces.append(_profile_prism((fq.x, fq.y), female_u2, female_v2, profile, cavity_width + 2.0 * wall, z0))
    if lift > 0.05:
        pieces.append(_ramp((fq.x, fq.y), female_u2, female_v2, width, depth, z0, lift))

    # Assembled clearance. The key is built into the male panel and rests on the
    # female one along the inner corner, so that contact is lifted clear before
    # testing; everything else must stand off every other panel and part.
    lift_off = tuple(float(c) for c in (female.inward_axis * 0.05))
    key_points = [tuple(float(c) for c in point) for point in key_world]
    reason = clearance.blocked(
        key_points, male_index, skip_bodies={male_index}, lift={female_index: lift_off}
    )
    if reason:
        return False, f"edge {edge_key}: key would hit {reason}"
    # The cradle stands on the female panel right at the inner corner, which is
    # exactly where the male panel's body begins, so the two always touch along
    # that line. Sliding the test copy a hair away from the corner separates the
    # intended contact; any real overlap is far larger and still found.
    slide = tuple(float(c) for c in (female_in_world * 0.05))
    piece_points = [_to_world(female, vertices) for vertices, _faces in pieces]
    for points in piece_points:
        reason = clearance.blocked(points, female_index, skip_bodies={female_index}, lift={male_index: slide})
        if reason:
            return False, f"edge {edge_key}: cradle would hit {reason}"

    _commit(male, [male.matrix_inverse @ point for point in key_world], key_faces)
    for vertices, faces in pieces:
        _commit(female, vertices, faces)
    clearance.add(male_index, "key", key_points)
    for points in piece_points:
        clearance.add(female_index, "rail", points)
    _reserve(male, male_center, male_u2, male_v2, width * 1.35, root_depth * 1.6)
    _reserve(female, cradle_center, female_u2, female_v2, cavity_width + 2.0 * wall, total_depth)
    # The cavity and the stretch the key crosses to reach it stay empty.
    key_path = (fq.x + female_v2.x * depth * 0.5, fq.y + female_v2.y * depth * 0.5)
    _reserve(female, key_path, female_u2, female_v2, cavity_width, depth, hard=True)
    return True, ""


@dataclass
class SplineJob:
    code: str
    edge_key: tuple[int, int]
    first_panel: str
    second_panel: str
    dihedral_deg: float
    reach: float
    height: float
    width: float
    angle_key: int = 0
    assembly: Matrix | None = None


def _spline_assembly(
    first: FaceBuild,
    along_first: Vector,
    inward_first: Vector,
    apex_world: Vector,
    dihedral_deg: float,
    width: float,
) -> Matrix:
    """Where a spline sits in the assembled sculpture.

    The printed profile has its apex at the origin, the bisector on +Y and the
    first arm at -half-angle from it; the arm lies on the first panel's interior
    face pointing away from the joint, and its thickness grows into the hollow.
    """
    angle = max(20.0, min(340.0, dihedral_deg))
    half = math.radians(angle * 0.5)
    arm = (first.x_axis * inward_first.x + first.y_axis * inward_first.y).normalized()
    up = first.inward_axis.normalized()
    x_axis = arm * math.sin(half) - up * math.cos(half)
    y_axis = arm * math.cos(half) + up * math.sin(half)
    z_axis = x_axis.cross(y_axis).normalized()
    origin = apex_world - z_axis * (width * 0.5)
    return Matrix(
        (
            (x_axis.x, y_axis.x, z_axis.x, origin.x),
            (x_axis.y, y_axis.y, z_axis.y, origin.y),
            (x_axis.z, y_axis.z, z_axis.z, origin.z),
            (0.0, 0.0, 0.0, 1.0),
        )
    )


def _add_spline_pair(
    model: ModelData,
    builds: list[FaceBuild],
    edge_key: tuple[int, int],
    first_index: int,
    second_index: int,
    fraction: float,
    settings,
    clearance: collide.Clearance,
    scale: float = 1.0,
) -> tuple[SplineJob | None, str]:
    """Add guide rails for a loose spline bridging one edge.

    Rails go on both panels of an inward fold; on an outward fold they would
    meet inside the V, so only the first panel carries them.

    Inward-folding edges have no shared interior volume, so no key printed on
    one panel can ever reach the other without support material. A separate
    spline dropped into matching rails is the only support-free way to hold the
    dihedral there, and it registers the joint in both directions at once.
    """
    a, b = edge_key
    outer_a, outer_b = model.vertices[a], model.vertices[b]
    edge_axis = (outer_b - outer_a).normalized()
    width = max(4.0, settings.spline_width_mm * scale)
    reach = max(7.0, settings.spline_reach_mm * (0.4 + 0.6 * scale))
    height = max(2.0, settings.spline_height_mm)
    wall = settings.cradle_wall_mm
    gap = settings.clearance_mm

    if abs(builds[first_index].thickness - builds[second_index].thickness) > 0.15:
        return None, "the two panels were thinned to different depths"
    ranges = []
    for index in (first_index, second_index):
        start, finish = builds[index].inner_edge_by_key[edge_key]
        ranges.append(sorted(((start - outer_a).dot(edge_axis), (finish - outer_a).dot(edge_axis))))
    low = max(ranges[0][0], ranges[1][0])
    high = min(ranges[0][1], ranges[1][1])
    span = width + 2.0 * (wall + gap)
    if high - low < span + 1.0:
        return None, "mitered inner edge too short for a spline"
    scalar = low + (high - low) * fraction
    dihedral = builds[first_index].dihedral_by_key[edge_key]
    wedge = _wedge_tangent(dihedral)
    wedge_margin = 0.25

    job = SplineJob(
        code="",
        edge_key=edge_key,
        first_panel=builds[first_index].panel_id,
        second_panel=builds[second_index].panel_id,
        dihedral_deg=dihedral,
        reach=reach,
        height=height,
        width=width,
    )
    staged: list[tuple[int, FaceBuild, core.Point2, Vector, Vector, list]] = []
    apex_world = None
    for index in (first_index, second_index):
        build = builds[index]
        start, finish = build.inner_edge_by_key[edge_key]
        s0 = (start - outer_a).dot(edge_axis)
        s1 = (finish - outer_a).dot(edge_axis)
        blend = 0.5 if abs(s1 - s0) < 1e-8 else (scalar - s0) / (s1 - s0)
        station_world = start.lerp(finish, blend)
        station = build.matrix_inverse @ station_world
        inward2, _, _ = _edge_frame(build, edge_key)
        along2 = Vector((edge_axis.dot(build.x_axis), edge_axis.dot(build.y_axis))).normalized()
        center = (station.x, station.y)
        # The station sits exactly on the interior outline, so the test window is
        # nudged inward; otherwise its two leading corners land on the boundary
        # and fail the containment test on rounding alone.
        # In a V narrower than 180 degrees rails on both panels would meet in
        # the middle; the spline is rigid, so rails on one side locate it fully.
        guided = dihedral >= 180.0 or index == first_index
        footprint = span if guided else width + 2.0 * gap
        check_inset = min(0.3, reach * 0.06)
        check_depth = max(0.1, reach - check_inset * 2.0)
        window = (
            center[0] + inward2.x * (reach * 0.5 + check_inset),
            center[1] + inward2.y * (reach * 0.5 + check_inset),
        )
        if not _corners_inside(build.inner_polygon, window, along2, inward2, footprint, check_depth):
            return None, "spline rails too close to a corner"
        if not _area_is_free(build, window, along2, inward2, footprint, check_depth):
            return None, "spline rails overlap another joint"
        rails = []
        for sign in ((-1.0, 1.0) if guided else ()):
            offset = (width + wall) * 0.5 + gap
            side = (center[0] + along2.x * sign * offset, center[1] + along2.y * sign * offset)
            if wedge is None:
                rail_center = (side[0] + inward2.x * reach * 0.5, side[1] + inward2.y * reach * 0.5)
                rails.append(
                    _tapered_box(
                        rail_center, (along2.x, along2.y), (inward2.x, inward2.y),
                        wall, reach, build.thickness, height + gap, min(0.18, wall * 0.18),
                    )
                )
            else:
                profile = _wedge_profile(0.0, reach, height + gap, wedge, wedge_margin)
                if profile is None:
                    return None, "joint too sharp for spline rails"
                rails.append(_profile_prism(side, along2, inward2, profile, wall, build.thickness))
        if apex_world is None:
            apex_world = station_world
            job.assembly = _spline_assembly(build, along2, inward2, station_world, dihedral, width)
        staged.append((index, build, center, along2, inward2, rails))

    # The spline itself, as it will sit in the finished sculpture, one convex
    # piece at a time.
    lifts = {
        index: tuple(float(c) for c in (builds[index].inward_axis * 0.05))
        for index in (first_index, second_index)
    }
    spline_pieces = [
        [tuple(float(c) for c in (job.assembly @ point)) for point in piece]
        for piece in _spline_pieces(job, settings)
    ]
    for piece in spline_pieces:
        reason = clearance.blocked(piece, -1, lift=lifts)
        if reason:
            return None, f"spline would hit {reason}"
    rail_points = []
    for index, build, _center, _along2, inward2, rails in staged:
        other = second_index if index == first_index else first_index
        away = (build.x_axis * inward2.x + build.y_axis * inward2.y) * 0.05
        slide = {other: tuple(float(c) for c in away)}
        for vertices, _rail_faces in rails:
            points = _to_world(build, vertices)
            reason = clearance.blocked(points, index, skip_bodies={index}, lift=slide)
            if reason:
                return None, f"spline rails would hit {reason}"
            rail_points.append((index, points))

    for index, build, center, along2, inward2, rails in staged:
        channel = (center[0] + inward2.x * reach * 0.5, center[1] + inward2.y * reach * 0.5)
        _reserve(build, channel, along2, inward2, span if rails else width + 2.0 * gap, reach)
        _reserve(build, channel, along2, inward2, width + 2.0 * gap, reach, hard=True)
        for vertices, faces in rails:
            _commit(build, vertices, faces)
    for index, points in rail_points:
        clearance.add(index, "rail", points)
    for piece in spline_pieces:
        clearance.add(-1, "spline", piece)
    return job, ""


@dataclass
class SplineShape:
    outline: list[core.Point2]
    pieces: list[list[core.Point2]]
    label_start: Vector
    label_direction: Vector
    label_normal: Vector
    label_length: float


def _spline_shape(job: SplineJob, settings) -> SplineShape:
    """Cross-section of a spline, with the apex at the origin and bisector on +Y.

    Up to a right angle the spline is a solid wedge that fills the V between the
    two panels, touching both along their full reach -- the firmest possible
    reference for the angle, and the only shape that fits a narrow V at all:
    two thick arms would cross into the opposite panel. Wider joints use a bent
    bar whose arms lie on the two panels. The code goes on the face that looks
    into the hollow of the sculpture, never on one that meets a rail or panel.
    """
    angle = max(20.0, min(340.0, job.dihedral_deg))
    half = math.radians(angle * 0.5)
    reach = max(2.0, job.reach - settings.clearance_mm)
    thickness = job.height
    first = Vector((math.sin(half), math.cos(half)))
    second = Vector((-math.sin(half), math.cos(half)))
    origin = Vector((0.0, 0.0))
    if angle <= 90.0:
        tip_a, tip_b = first * reach, second * reach
        outline = [origin, tip_a, tip_b]
        direction = (tip_b - tip_a).normalized()
        return SplineShape(
            [(p.x, p.y) for p in outline],
            [[(p.x, p.y) for p in outline]],
            tip_a,
            direction,
            Vector((0.0, 1.0)),
            (tip_b - tip_a).length,
        )
    first_normal = Vector((-first.y, first.x))
    second_normal = Vector((second.y, -second.x))
    apex = Vector((0.0, thickness / max(0.2, math.sin(half))))
    outer_a = first * reach + first_normal * thickness
    outer_b = second * reach + second_normal * thickness
    outline = [origin, first * reach, outer_a, apex, outer_b, second * reach]
    # Split along the bisector: each arm is convex on its own, while the hull of
    # the whole V would also cover the solid corner between the two panels.
    arm_a = [origin, first * reach, outer_a, apex]
    arm_b = [origin, apex, outer_b, second * reach]
    length = (outer_a - apex).length
    return SplineShape(
        [(p.x, p.y) for p in outline],
        [[(p.x, p.y) for p in arm_a], [(p.x, p.y) for p in arm_b]],
        apex,
        (outer_a - apex).normalized(),
        first_normal,
        length,
    )


def _spline_profile(job: SplineJob, settings) -> tuple[list[Vector], list[tuple[int, ...]]]:
    """Flat-printable prism: the cross-section on the bed, extruded upward.

    Extruding a planar footprint straight up leaves no overhang anywhere, so a
    spline prints support-free at any dihedral it has to hold.
    """
    points = list(_spline_shape(job, settings).outline)
    if core.polygon_area(points) < 0.0:
        points.reverse()
    triangles = core.triangulate_simple_polygon(points)
    count = len(points)
    vertices = [Vector((x, y, 0.0)) for x, y in points]
    vertices.extend(Vector((x, y, job.width)) for x, y in points)
    faces: list[tuple[int, ...]] = [tuple(reversed(triangle)) for triangle in triangles]
    faces.extend(tuple(count + index for index in triangle) for triangle in triangles)
    for index in range(count):
        following = (index + 1) % count
        faces.append((index, following, count + following, count + index))
    return vertices, faces


def _spline_pieces(job: SplineJob, settings) -> list[list[Vector]]:
    """Convex pieces of the spline solid, for clearance checks."""
    return [
        [Vector((x, y, z)) for z in (0.0, job.width) for x, y in piece]
        for piece in _spline_shape(job, settings).pieces
    ]


def _append_spline_label(
    job: SplineJob,
    vertices: list[Vector],
    faces: list[tuple[int, ...]],
    settings,
    font: bpy.types.VectorFont,
    cache: dict,
) -> bool:
    """Stamp the code on the face that looks into the hollow of the sculpture.

    The end faces of the prism point along the shared edge, straight at guide
    rails only `clearance` away, and the other faces sit on the panels, so
    raised type anywhere else would hold the spline off its seat.
    """
    shape = _spline_shape(job, settings)
    relief = settings.label_relief_mm
    normalized, font_faces, aspect = _font_template(job.code, font, 10.0, 1.0, cache)
    size = min(settings.edge_label_size_mm, job.width * 0.70, (shape.label_length - 1.2) / max(0.8, aspect))
    if size < 2.0:
        return False
    direction = shape.label_direction
    normal = shape.label_normal
    # Reading axes chosen so `along x across` points out of the face; the other
    # handedness would mirror every glyph.
    along = Vector((direction.x, direction.y, 0.0))
    across = Vector((0.0, 0.0, -1.0))
    out = Vector((normal.x, normal.y, 0.0))
    if along.cross(across).dot(out) < 0.0:
        along = -along
    middle = shape.label_start + direction * (shape.label_length * 0.5)
    base = Vector((middle.x, middle.y, job.width * 0.5))
    start = len(vertices)
    for point in normalized:
        vertices.append(base + along * (point.x * size) + across * (point.y * size) + out * (point.z * relief))
    faces.extend(tuple(start + index for index in face) for face in font_faces)
    return True


def _build_spline_object(
    job: SplineJob,
    collection: bpy.types.Collection,
    settings,
    font: bpy.types.VectorFont,
    cache: dict,
) -> bpy.types.Object:
    vertices, faces = _spline_profile(job, settings)
    try:
        _append_spline_label(job, vertices, faces, settings, font, cache)
    except (PolyshellError, RuntimeError, ValueError) as exc:
        print(f"Polyshell Builder could not stamp spline {job.code}: {exc}")
    mesh = bpy.data.meshes.new(f"PSB_Spline_{job.code}_Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(f"PSB_Spline_{job.code}", mesh)
    collection.objects.link(obj)
    obj["psb_kind"] = "spline"
    obj["psb_panel_id"] = job.code
    obj["psb_spline_code"] = job.code
    obj["psb_spline_angle"] = job.angle_key
    obj["psb_spline_dihedral"] = round(job.dihedral_deg, 2)
    obj["psb_spline_panels"] = f"{job.first_panel}/{job.second_panel}"
    if job.assembly is not None:
        # Where this spline sits in the finished sculpture, for fit checks and previews.
        obj["psb_assembly_matrix"] = [float(value) for row in job.assembly for value in row]
    return obj


_SEG = {
    "a": ((0.12, 1.00), (0.88, 1.00)), "b": ((0.88, 1.00), (0.88, 0.52)),
    "c": ((0.88, 0.48), (0.88, 0.00)), "d": ((0.12, 0.00), (0.88, 0.00)),
    "e": ((0.12, 0.48), (0.12, 0.00)), "f": ((0.12, 1.00), (0.12, 0.52)),
    "g": ((0.12, 0.50), (0.88, 0.50)), "h": ((0.50, 1.00), (0.50, 0.00)),
    "i": ((0.12, 1.00), (0.88, 0.00)), "j": ((0.88, 1.00), (0.12, 0.00)),
    "k": ((0.12, 0.50), (0.50, 1.00)), "l": ((0.12, 0.50), (0.50, 0.00)),
    "m": ((0.88, 0.50), (0.50, 1.00)), "n": ((0.88, 0.50), (0.50, 0.00)),
}
_GLYPHS = {
    "0": "ab cdef".replace(" ", ""), "1": "bc", "2": "abged".replace(" ", ""), "3": "abgcd",
    "4": "fgbc", "5": "afgcd", "6": "afgecd", "7": "abc", "8": "abcdefg", "9": "abfgcd",
    "A": "abcefg", "B": "fegcd", "C": "afed", "D": "begcd", "E": "afged", "F": "afge",
    "G": "afedcg", "H": "fegbc", "I": "adh", "J": "bcde", "K": "fekn", "L": "fed",
    "M": "febcik", "N": "febci", "O": "abcdef", "P": "abfge", "Q": "abcdefn", "R": "abfgei",
    "S": "afgcd", "T": "ah", "U": "febcd", "V": "fen", "W": "febcdln", "X": "ijkl",
    "Y": "jkh", "Z": "ajd", "-": "g", "/": "j",
}


def _label_dimensions(text: str, size: float) -> tuple[float, float]:
    return size * (len(text) * 0.72 + max(0, len(text) - 1) * 0.16), size


_ORIENTATION_UNDERLINE_CHARACTERS = frozenset("69BGIMOSWZ")


def _needs_orientation_underline(text: str) -> bool:
    return any(character in _ORIENTATION_UNDERLINE_CHARACTERS for character in text.upper())


def _underline_geometry(aspect: float, size: float) -> tuple[float, float, float]:
    width = max(size * 0.72, aspect * size * 0.88)
    depth = max(0.55, min(1.0, size * 0.13))
    offset = size * 0.5 + max(0.32, size * 0.09) + depth * 0.5
    return width, depth, offset


def _label_fits(
    poly: list[core.Point2],
    center: core.Point2,
    u: Vector,
    v: Vector,
    text: str,
    size: float,
    aspect: float | None = None,
) -> bool:
    width, height = _label_dimensions(text, size)
    if aspect is not None:
        width = aspect * size
    if not _corners_inside(poly, center, u, v, width, height):
        return False
    if not _needs_orientation_underline(text):
        return True
    line_width, line_depth, line_offset = _underline_geometry(width / size, size)
    line_center = Vector(center) - v * line_offset
    return _corners_inside(poly, (line_center.x, line_center.y), u, v, line_width, line_depth)


def _append_orientation_underline(
    build: FaceBuild,
    text: str,
    center: core.Point2,
    u: Vector,
    v: Vector,
    aspect: float,
    size: float,
    relief: float,
) -> None:
    if not _needs_orientation_underline(text):
        return
    width, depth, offset = _underline_geometry(aspect, size)
    line_center = Vector(center) - v * offset
    _append_tapered_box(
        build,
        (line_center.x, line_center.y),
        (u.x, u.y),
        (v.x, v.y),
        width,
        depth,
        build.inner_local[0].z,
        relief,
        min(0.06, depth * 0.1),
    )


def _append_stroke_label(
    build: FaceBuild,
    text: str,
    center: core.Point2,
    u: Vector,
    v: Vector,
    requested_size: float,
    relief: float,
) -> bool:
    size = requested_size
    while size >= 1.0 and not _label_fits(build.inner_polygon, center, u, v, text, size):
        size *= 0.82
    if size < 1.0:
        return False
    glyph_width, spacing = size * 0.72, size * 0.16
    total, _ = _label_dimensions(text, size)
    origin_x = -total * 0.5
    stroke_width = max(0.28, size * 0.105)
    for char_index, char in enumerate(text.upper()):
        glyphs = _GLYPHS.get(char, "")
        offset = origin_x + char_index * (glyph_width + spacing)
        for segment_name in glyphs:
            p, q = _SEG[segment_name]
            p2 = Vector((offset + p[0] * glyph_width, (p[1] - 0.5) * size))
            q2 = Vector((offset + q[0] * glyph_width, (q[1] - 0.5) * size))
            delta = q2 - p2
            length = delta.length
            if length < 1e-8:
                continue
            axis = delta.normalized()
            across = Vector((-axis.y, axis.x))
            local_center = (p2 + q2) * 0.5
            world2 = Vector((center[0], center[1])) + u * local_center.x + v * local_center.y
            line_u = u * axis.x + v * axis.y
            line_v = u * across.x + v * across.y
            _append_tapered_box(
                build,
                (world2.x, world2.y),
                (line_u.x, line_u.y),
                (line_v.x, line_v.y),
                length + stroke_width,
                stroke_width,
                build.inner_local[0].z,
                relief,
                min(stroke_width * 0.12, 0.05),
            )
    _append_orientation_underline(
        build,
        text,
        center,
        u,
        v,
        _label_dimensions(text, size)[0] / size,
        size,
        relief,
    )
    return True


_BOLD_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _resolve_label_font(settings) -> bpy.types.VectorFont:
    requested = bpy.path.abspath(settings.label_font_path.strip()) if settings.label_font_path.strip() else ""
    paths = ([requested] if requested else []) + list(_BOLD_FONT_CANDIDATES)
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        try:
            font = bpy.data.fonts.load(path, check_existing=True)
            settings.resolved_label_font = os.path.splitext(os.path.basename(path))[0]
            return font
        except RuntimeError:
            continue
    # Blender's own font ships with every build, so labels never depend on
    # which fonts happen to be installed on this machine.
    try:
        font = bpy.data.fonts.load("<builtin>", check_existing=True)
    except RuntimeError:
        font = bpy.data.fonts[0] if bpy.data.fonts else None
    if font is None:
        raise PolyshellError("No usable font is available for interior labels.")
    settings.resolved_label_font = "Blender built-in font"
    return font


def _unflatten_glyph_triangles(bm: bmesh.types.BMesh) -> None:
    """Re-triangulate the flat top and bottom of extruded type for good shape.

    Some Blender versions fill a glyph face with slivers whose three corners
    lie on one straight edge (Blender 4.5 does it where the stem of a "4" or a
    "T" meets its bar). Rotating edges inside each flat face, never across its
    outline, removes them without moving any point, so the letter keeps its
    shape exactly.
    """
    caps = {face for face in bm.faces if len({round(vertex.co.z, 6) for vertex in face.verts}) == 1}
    inner = [
        edge for edge in bm.edges
        if len(edge.link_faces) == 2 and all(face in caps for face in edge.link_faces)
    ]
    if caps and inner:
        bmesh.ops.beautify_fill(bm, faces=list(caps), edges=inner)


def _font_template(
    text: str,
    font: bpy.types.VectorFont,
    requested_size: float,
    relief: float,
    cache: dict,
) -> tuple[list[Vector], list[tuple[int, ...]], float]:
    """Return centered font vertices normalized to one millimeter cap height."""
    key = (font.name_full, text, round(requested_size, 4), round(relief, 4))
    cached = cache.get(key)
    if cached is not None:
        return cached

    curve = bpy.data.curves.new("PSB_Label_Font_Temp", "FONT")
    obj = None
    mesh = None
    try:
        curve.body = text
        curve.font = font
        curve.align_x = "CENTER"
        curve.align_y = "CENTER"
        curve.size = requested_size
        curve.extrude = relief * 0.5
        curve.offset = 0.0
        curve.resolution_u = 4
        curve.fill_mode = "BOTH"
        obj = bpy.data.objects.new("PSB_Label_Font_Temp", curve)
        bpy.context.scene.collection.objects.link(obj)
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph), depsgraph=depsgraph)

        weld = bmesh.new()
        weld.from_mesh(mesh)
        bmesh.ops.remove_doubles(weld, verts=list(weld.verts), dist=0.00001)
        # A glyph whose outline overlaps itself (the tail of a "Q" in Blender's
        # built-in font) can come out with a hairline T-junction that leaves the
        # letter open. Welding points a fraction of a percent of the letter
        # height apart closes it without visibly changing the shape.
        for fraction in (0.001, 0.002, 0.005):
            if all(len(edge.link_faces) == 2 for edge in weld.edges):
                break
            bmesh.ops.remove_doubles(weld, verts=list(weld.verts), dist=requested_size * fraction)
            bmesh.ops.dissolve_degenerate(weld, dist=1e-6, edges=list(weld.edges))
        closed = bool(weld.faces) and all(len(edge.link_faces) == 2 for edge in weld.edges)
        _unflatten_glyph_triangles(weld)
        bmesh.ops.recalc_face_normals(weld, faces=list(weld.faces))
        weld.to_mesh(mesh)
        weld.free()
        if not closed:
            raise PolyshellError(f"The selected font produced an open letter shape for label {text}.")
        mesh.update()
        if not mesh.vertices or not mesh.polygons:
            raise PolyshellError(f"The selected font produced no geometry for label {text}.")

        min_x = min(vertex.co.x for vertex in mesh.vertices)
        max_x = max(vertex.co.x for vertex in mesh.vertices)
        min_y = min(vertex.co.y for vertex in mesh.vertices)
        max_y = max(vertex.co.y for vertex in mesh.vertices)
        min_z = min(vertex.co.z for vertex in mesh.vertices)
        max_z = max(vertex.co.z for vertex in mesh.vertices)
        raw_height = max_y - min_y
        raw_depth = max_z - min_z
        if raw_height < 1e-7 or raw_depth < 1e-7:
            raise PolyshellError(f"The selected font produced invalid geometry for label {text}.")
        center_x, center_y = (min_x + max_x) * 0.5, (min_y + max_y) * 0.5
        normalized = [
            Vector(
                (
                    (vertex.co.x - center_x) / raw_height,
                    (vertex.co.y - center_y) / raw_height,
                    (vertex.co.z - min_z) / raw_depth,
                )
            )
            for vertex in mesh.vertices
        ]
        faces = [tuple(polygon.vertices) for polygon in mesh.polygons]
        result = (normalized, faces, (max_x - min_x) / raw_height)
        cache[key] = result
        return result
    finally:
        if obj is not None:
            bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None:
            bpy.data.meshes.remove(mesh)
        if curve.name in bpy.data.curves:
            bpy.data.curves.remove(curve)


def _append_font_label(
    build: FaceBuild,
    text: str,
    center: core.Point2,
    u: Vector,
    v: Vector,
    requested_size: float,
    relief: float,
    font: bpy.types.VectorFont,
    cache: dict,
) -> bool:
    normalized, font_faces, aspect = _font_template(text, font, 10.0, 1.0, cache)
    size = requested_size
    while size >= 2.0 and not _label_fits(build.inner_polygon, center, u, v, text, size, aspect):
        size *= 0.88
    if size < 2.0:
        return False
    start = len(build.vertices)
    base_z = build.inner_local[0].z
    center2 = Vector(center)
    for point in normalized:
        xy = center2 + u * (point.x * size) + v * (point.y * size)
        build.vertices.append(Vector((xy.x, xy.y, base_z + point.z * relief)))
    build.faces.extend(tuple(start + vertex_index for vertex_index in face) for face in font_faces)
    _append_orientation_underline(build, text, center, u, v, aspect, size, relief)
    return True


_LABEL_SCALES = (1.0, 0.9, 0.8, 0.72, 0.64, 0.56, 0.5, 0.44)


class _Labeller:
    """Places raised labels that fit the face and clear every assembled part."""

    def __init__(self, settings, clearance: collide.Clearance):
        self.settings = settings
        self.clearance = clearance
        self.font = _resolve_label_font(settings)
        self.cache: dict = {}
        self.dropped = 0
        self.crowded = 0
        self.missing_ids = 0
        self.dropped_items: list[str] = []

    def aspect(self, text: str) -> float:
        try:
            return _font_template(text, self.font, 10.0, 1.0, self.cache)[2]
        except (PolyshellError, RuntimeError, ValueError):
            return _label_dimensions(text, 1.0)[0]

    def footprint(self, text: str, center: core.Point2, v: Vector, size: float) -> tuple[core.Point2, float, float]:
        """Centre, width and height of everything the label stamps, underline included."""
        aspect = self.aspect(text)
        top = size * 0.5
        bottom = -size * 0.5
        if _needs_orientation_underline(text):
            _width, depth, offset = _underline_geometry(aspect, size)
            bottom = -(offset + depth * 0.5)
        shift = (top + bottom) * 0.5
        middle = (center[0] + v.x * shift, center[1] + v.y * shift)
        return middle, max(aspect * size, size * 0.72) * 1.05, (top - bottom) * 1.1

    def world_box(self, build: FaceBuild, middle: core.Point2, u: Vector, v: Vector, width: float, height: float):
        corners = core.oriented_box(middle, (u.x, u.y), (v.x, v.y), width, height)
        base = build.thickness
        top = base + self.settings.label_relief_mm
        local = [Vector((x, y, z)) for z in (base, top) for x, y in corners]
        return _to_world(build, local)

    def emit(self, build: FaceBuild, text: str, center: core.Point2, u: Vector, v: Vector, size: float) -> bool:
        try:
            return _append_font_label(
                build, text, center, u, v, size, self.settings.label_relief_mm, self.font, self.cache
            )
        except (PolyshellError, RuntimeError, ValueError) as exc:
            print(f"Polyshell Builder font fallback for {text}: {exc}")
            return _append_stroke_label(
                build, text, center, u, v, max(3.0, size), max(0.5, self.settings.label_relief_mm)
            )

    def place(
        self,
        build: FaceBuild,
        text: str,
        center: core.Point2,
        u: Vector,
        v: Vector,
        size: float,
        forced: bool = False,
    ) -> bool:
        """Stamp `text` if it fits the face, its own panel's free area, and the assembly.

        A forced label may crowd its own panel's rails and key roots as a last
        resort, but never a cavity, a key path, a spline channel, or anything
        belonging to another panel -- raised type there holds a joint open.
        """
        if not _label_fits(build.inner_polygon, center, u, v, text, size, self.aspect(text)):
            return False
        middle, width, height = self.footprint(text, center, v, size)
        if not _area_is_free(build, middle, u, v, width, height, hard_only=forced):
            return False
        box = self.world_box(build, middle, u, v, width, height)
        if self.clearance.blocked(
            box, build.face_index, skip_bodies={build.face_index}, ignore_host_features=forced
        ):
            return False
        if not self.emit(build, text, center, u, v, size):
            return False
        _reserve(build, middle, u, v, width, height)
        build.labels.append({"text": text, "center": [round(center[0], 3), round(center[1], 3)], "size": round(size, 2)})
        return True


def _add_labels(
    builds: list[FaceBuild],
    edge_labels: dict[int, list[tuple[tuple[int, int], str]]],
    settings,
    clearance: collide.Clearance,
    label_depths: dict[tuple[int, tuple[int, int]], float] | None = None,
    spline_codes: dict[tuple[int, tuple[int, int]], str] | None = None,
) -> _Labeller:
    labeller = _Labeller(settings, clearance)
    horizontal, vertical = Vector((1.0, 0.0)), Vector((0.0, 1.0))
    for build in builds:
        centre = Vector(core.polygon_centroid(build.inner_polygon))
        spots = [centre] + [
            centre + (Vector(point) - centre) * blend
            for blend in (0.35, 0.55)
            for point in build.inner_polygon
        ]
        room = math.sqrt(max(1e-6, abs(core.polygon_area(build.inner_polygon))))
        headline = max(2.4, min(settings.panel_label_size_mm, room * 0.42))
        if build.id_box is not None and build.id_box in build.reserved:
            build.reserved.remove(build.id_box)
        placed = False
        for forced in (False, True):
            for scale in _LABEL_SCALES:
                size = headline * scale
                for spot in spots:
                    if labeller.place(build, build.panel_id, (spot.x, spot.y), horizontal, vertical, size, forced):
                        placed = True
                        break
                if placed:
                    break
            if placed:
                labeller.crowded += int(forced)
                break
        if not placed:
            labeller.dropped += 1
            labeller.missing_ids += 1
            labeller.dropped_items.append(f"{build.panel_id}: its own panel ID")

        # Spline codes go on before neighbor IDs: on a crowded face, knowing
        # which spline an edge needs matters more than repeating the neighbor.
        ordered = sorted(
            edge_labels.get(build.face_index, []),
            key=lambda item: 0 if (spline_codes or {}).get((build.face_index, item[0])) else 1,
        )
        for edge_key, code in ordered:
            inward, pa, pb = _edge_frame(build, edge_key)
            edge = Vector((pb[0] - pa[0], pb[1] - pa[1]))
            if edge.length < 1e-9:
                continue
            length = edge.length
            # Without this the glyphs come out mirrored on roughly half of all
            # edges, because the edge key is ordered by mesh vertex index.
            edge = _reading_basis(edge.normalized(), inward)
            midpoint = Vector(((pa[0] + pb[0]) * 0.5, (pa[1] + pb[1]) * 0.5))
            occupied = (label_depths or {}).get(
                (build.face_index, edge_key), settings.tab_depth_mm + settings.cradle_wall_mm
            )
            spline_code = (spline_codes or {}).get((build.face_index, edge_key))
            texts = [(spline_code, settings.edge_label_size_mm * 1.05), (code, settings.edge_label_size_mm)] if spline_code else [(code, settings.edge_label_size_mm)]
            for text, base_size in texts:
                placed = False
                for forced in (False, True):
                    reaches = (1.0, 1.35, 1.75, 0.72, 2.2) if not forced else (1.0, 0.7, 0.5, 0.35, 1.4)
                    slides = (0.0, 0.16, -0.16, 0.3, -0.3) if not forced else (0.0,)
                    for scale in _LABEL_SCALES:
                        size = base_size * scale
                        for reach in reaches:
                            for slide in slides:
                                spot = midpoint + inward * (occupied * reach + size * 0.7) + edge * (length * slide)
                                if labeller.place(build, text, (spot.x, spot.y), edge, inward, size, forced):
                                    placed = True
                                    break
                            if placed:
                                break
                        if placed:
                            break
                    if placed:
                        labeller.crowded += int(forced)
                        break
                if not placed:
                    labeller.dropped += 1
                    kind = "spline code" if text == spline_code else "neighbour ID"
                    labeller.dropped_items.append(f"{build.panel_id}: {kind} {text} (edge to {code})")
    return labeller


def _shrunk(points: list[tuple[float, float, float]], distance: float) -> list[tuple[float, float, float]]:
    """Pull every point toward the centroid so parts that merely touch separate."""
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    cz = sum(p[2] for p in points) / len(points)
    out = []
    for x, y, z in points:
        dx, dy, dz = x - cx, y - cy, z - cz
        length = math.sqrt(dx * dx + dy * dy + dz * dz)
        if length <= distance * 2.0:
            out.append((x, y, z))
            continue
        scale = distance / length
        out.append((x - dx * scale, y - dy * scale, z - dz * scale))
    return out


def _mesh_is_closed(mesh: bpy.types.Mesh) -> bool:
    counts: dict[tuple[int, int], int] = {}
    for polygon in mesh.polygons:
        vertices = polygon.vertices
        for index in range(len(vertices)):
            key = tuple(sorted((vertices[index], vertices[(index + 1) % len(vertices)])))
            counts[key] = counts.get(key, 0) + 1
    return bool(counts) and all(value == 2 for value in counts.values())


def _mesh_volume(mesh: bpy.types.Mesh) -> float:
    return _signed_volume([vertex.co for vertex in mesh.vertices], [tuple(polygon.vertices) for polygon in mesh.polygons])


def _self_crossings(points: list, faces: list[tuple[int, ...]], tolerance: float = 0.01) -> int:
    """Pairs of faces without a shared vertex that pass through each other."""
    if len(faces) < 4:
        return 0
    tree = BVHTree.FromPolygons([Vector(point) for point in points], [list(face) for face in faces], epsilon=0.0)
    coordinates = [tuple(float(c) for c in point) for point in points]

    def plane(face):
        nx = ny = nz = 0.0
        for index in range(len(face)):
            x1, y1, z1 = coordinates[face[index]]
            x2, y2, z2 = coordinates[face[(index + 1) % len(face)]]
            nx += (y1 - y2) * (z1 + z2)
            ny += (z1 - z2) * (x1 + x2)
            nz += (x1 - x2) * (y1 + y2)
        length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        return (nx / length, ny / length, nz / length), coordinates[face[0]]

    def crosses(first, second) -> bool:
        (nx, ny, nz), (ox, oy, oz) = plane(first)
        heights = [
            (coordinates[i][0] - ox) * nx + (coordinates[i][1] - oy) * ny + (coordinates[i][2] - oz) * nz
            for i in second
        ]
        return min(heights) < -tolerance and max(heights) > tolerance

    count = 0
    for a, b in tree.overlap(tree):
        if a < b and not set(faces[a]) & set(faces[b]) and crosses(faces[a], faces[b]) and crosses(faces[b], faces[a]):
            count += 1
    return count


def _tidy_boolean_result(mesh: bpy.types.Mesh) -> None:
    """Dissolve the zero-area slivers an exact boolean can leave along a cut.

    Collapsing short edges occasionally tears the surface open or folds it
    through itself instead, so the tidied copy is only kept when it is still
    closed, has fewer slivers, and crosses itself no more than the boolean
    result did. A clean result is left alone: collapsing edges on a mesh with
    nothing to tidy can itself create zero-area faces.
    """
    slivers = sum(1 for polygon in mesh.polygons if polygon.area <= 1e-8)
    if not slivers:
        return
    original: int | None = None
    # A smaller collapse distance still clears exact zero-area slivers when the
    # coarser one would fold the surface.
    for distance in (1e-4, 1e-5, 1e-6):
        trial = bmesh.new()
        try:
            trial.from_mesh(mesh)
            bmesh.ops.dissolve_degenerate(trial, dist=distance, edges=list(trial.edges))
            if not trial.faces or any(len(edge.link_faces) != 2 for edge in trial.edges):
                continue
            if sum(1 for face in trial.faces if face.calc_area() <= 1e-8) >= slivers:
                continue
            trial.verts.index_update()
            tidied = _self_crossings(
                [vertex.co for vertex in trial.verts], [tuple(v.index for v in face.verts) for face in trial.faces]
            )
            if tidied:
                if original is None:
                    original = _self_crossings(
                        [vertex.co for vertex in mesh.vertices], [tuple(p.vertices) for p in mesh.polygons]
                    )
                if tidied > original:
                    continue
            trial.to_mesh(mesh)
            return
        finally:
            trial.free()


def _keep_main_piece(mesh: bpy.types.Mesh) -> bool:
    """Drop specks a cut split off the panel. False if a real piece came loose.

    A cut through the very tip of a sharp corner can isolate a fleck of rim a
    few hundredths of a cubic millimetre in size; it would print as a loose
    speck, so it is removed. Anything larger means the cut went wrong.
    """
    parent = list(range(len(mesh.vertices)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for polygon in mesh.polygons:
        root = find(polygon.vertices[0])
        for vertex in polygon.vertices[1:]:
            other = find(vertex)
            if other != root:
                parent[other] = root
    groups: dict[int, list[int]] = {}
    for polygon in mesh.polygons:
        groups.setdefault(find(polygon.vertices[0]), []).append(polygon.index)
    if len(groups) <= 1:
        return True
    points = [vertex.co for vertex in mesh.vertices]
    volumes = {
        root: _signed_volume(points, [tuple(mesh.polygons[index].vertices) for index in members])
        for root, members in groups.items()
    }
    main = max(volumes, key=lambda root: volumes[root])
    loose = sum(abs(volume) for root, volume in volumes.items() if root != main)
    if loose > max(0.5, volumes[main] * 0.002):
        return False
    edit = bmesh.new()
    try:
        edit.from_mesh(mesh)
        edit.faces.ensure_lookup_table()
        doomed = [edit.faces[index] for root, members in groups.items() if root != main for index in members]
        bmesh.ops.delete(edit, geom=doomed, context="FACES")
        loose_verts = [vertex for vertex in edit.verts if not vertex.link_faces]
        if loose_verts:
            bmesh.ops.delete(edit, geom=loose_verts, context="VERTS")
        edit.to_mesh(mesh)
    finally:
        edit.free()
    return True


def _exterior_area(mesh: bpy.types.Mesh) -> float:
    """Area of the printed outer face: everything lying flat at z = 0, facing down."""
    total = 0.0
    for polygon in mesh.polygons:
        if polygon.normal.z < -0.999 and all(abs(mesh.vertices[index].co.z) < 1e-3 for index in polygon.vertices):
            total += polygon.area
    return total


# Test hook: when set to a list, every attempted overlap cut is recorded there.
_CUT_LOG: list | None = None


def _resolve_corner_overlaps(
    model: ModelData,
    builds: list[FaceBuild],
    unresolved: list[str],
    surface_cuts: list[str] | None = None,
) -> list[str]:
    """Carve out wherever two panels would occupy the same space.

    The inward offsets of more than three faces around a vertex never meet at
    a single point, so the thick parts of panels around such a vertex can
    collide even though their outer skins meet exactly; panels on opposite
    sides of a thin part can still touch where the ray-based depth limit missed.
    Subtracting one body from the other with Blender's exact boolean removes
    precisely the shared volume, which is hidden interior material except at
    folds sharper than the miter limit (whose miter is capped) and at
    saddle-shaped corners. Cuts that reach the printed face are listed in
    `surface_cuts`. Returns the IDs of the panels trimmed.
    """
    shells = {index: _shrunk(_to_world(build, build.vertices), 0.02) for index, build in enumerate(builds)}
    boxes = {
        index: (
            min(p[0] for p in points), max(p[0] for p in points),
            min(p[1] for p in points), max(p[1] for p in points),
            min(p[2] for p in points), max(p[2] for p in points),
        )
        for index, points in shells.items()
    }
    # Sweep along x so every pair whose boxes overlap is tested once, not just
    # panels that share a vertex.
    order = sorted(boxes, key=lambda index: boxes[index][0])
    cuts: dict[int, list[int]] = {}
    for position, index in enumerate(order):
        box = boxes[index]
        for other in order[position + 1:]:
            other_box = boxes[other]
            if other_box[0] > box[1]:
                break
            if other_box[2] > box[3] or box[2] > other_box[3] or other_box[4] > box[5] or box[4] > other_box[5]:
                continue
            if collide.intersects(shells[index], shells[other], 0.0):
                low, high = min(index, other), max(index, other)
                cuts.setdefault(high, []).append(low)
    for others in cuts.values():
        others.sort()
    if not cuts:
        return []

    scene = bpy.context.scene
    holder = bpy.data.collections.new("PSB_Temp_Corner_Cuts")
    scene.collection.children.link(holder)
    temporary: dict[int, bpy.types.Object] = {}

    def solid(index: int) -> bpy.types.Object:
        existing = temporary.get(index)
        if existing is not None:
            return existing
        build = builds[index]
        # Bodies are wound outward by construction; Blender's normal
        # recalculation guesses from the outermost vertex and can turn a shell
        # with a sliver face inside out, which would invert the boolean.
        mesh = bpy.data.meshes.new(f"PSB_Temp_Body_{index}")
        mesh.from_pydata(build.vertices, [], build.faces)
        mesh.update()
        obj = bpy.data.objects.new(mesh.name, mesh)
        obj.matrix_world = build.matrix_world
        holder.objects.link(obj)
        temporary[index] = obj
        return obj

    trimmed: set[int] = set()

    def cut(index: int, other: int, tolerant: bool) -> str:
        outcome = _cut_once(index, other, tolerant)
        if _CUT_LOG is not None:
            _CUT_LOG.append((builds[index].panel_id, builds[other].panel_id, tolerant, outcome))
        return outcome

    def _cut_once(index: int, other: int, tolerant: bool) -> str:
        """Subtract `other` from `index`. Returns '' on success, else why it failed."""
        target = solid(index)
        before = _mesh_volume(target.data)
        face_before = _exterior_area(target.data)
        modifier = target.modifiers.new(f"cut_{other}", "BOOLEAN")
        modifier.operation = "DIFFERENCE"
        modifier.solver = "EXACT"
        modifier.use_hole_tolerant = tolerant
        modifier.object = solid(other)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        result = bpy.data.meshes.new_from_object(target.evaluated_get(depsgraph), depsgraph=depsgraph)
        target.modifiers.remove(modifier)
        # Only ever accept a clean, closed result that lost a sliver.
        if not _mesh_is_closed(result):
            bpy.data.meshes.remove(result)
            return "boolean failed"
        _tidy_boolean_result(result)
        if not _keep_main_piece(result):
            bpy.data.meshes.remove(result)
            return "boolean failed"
        after = _mesh_volume(result)
        if before - after < 1e-3:
            bpy.data.meshes.remove(result)
            return "nothing to remove"
        if not _mesh_is_closed(result) or not (0.5 * before <= after <= before + 1e-3):
            bpy.data.meshes.remove(result)
            return "boolean failed"
        old = target.data
        target.data = result
        bpy.data.meshes.remove(old)
        trimmed.add(index)
        if surface_cuts is not None and face_before - _exterior_area(result) > 0.5:
            surface_cuts.append(f"{builds[index].panel_id}/{builds[other].panel_id}")
        return ""

    try:
        for index in sorted(cuts):
            # One neighbour at a time: a single pass over every neighbour that
            # meets at the same vertex can fail where each cut alone succeeds.
            for other in cuts[index]:
                outcome = cut(index, other, False)
                if outcome != "boolean failed":
                    continue
                # Taking the overlap out of the other panel instead is just as
                # valid -- removing material can never create a new collision.
                if cut(other, index, False) != "boolean failed":
                    continue
                if cut(index, other, True) != "boolean failed":
                    continue
                if cut(other, index, True) != "boolean failed":
                    continue
                # Booleans also fail on overlaps too small to matter; only an
                # overlap that survives pulling both bodies in by 0.1 mm, below
                # what a nozzle can print, is worth a warning.
                deep = collide.intersects(
                    _shrunk(_to_world(builds[index], builds[index].vertices), 0.1),
                    _shrunk(_to_world(builds[other], builds[other].vertices), 0.1),
                    0.0,
                )
                if deep:
                    unresolved.append(f"{builds[index].panel_id}/{builds[other].panel_id}")
        for index in trimmed:
            build = builds[index]
            target = temporary[index]
            build.vertices = [vertex.co.copy() for vertex in target.data.vertices]
            build.faces = [tuple(polygon.vertices) for polygon in target.data.polygons]
    finally:
        for obj in temporary.values():
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        bpy.data.collections.remove(holder)
    return sorted(builds[index].panel_id for index in trimmed)


def _hold_id_space(build: FaceBuild, settings) -> None:
    """Keep a patch of the face free for the panel's own ID before joints go on.

    Joints are placed first, and on a small panel they can fill the whole face;
    an unlabelled panel among hundreds is far harder to deal with than one edge
    without a key, so the ID's space is claimed up front.
    """
    # Only the smallest size that still reads is held; the ID is stamped larger
    # wherever the finished face leaves room.
    size = min(4.0, settings.panel_label_size_mm)
    centre = core.polygon_centroid(build.inner_polygon)
    width, height = _label_dimensions(build.panel_id, size)
    u, v = Vector((1.0, 0.0)), Vector((0.0, 1.0))
    for scale in (1.0, 0.8):
        if _corners_inside(build.inner_polygon, centre, u, v, width * scale * 1.1, height * scale * 1.3):
            box = core.oriented_box(centre, (1.0, 0.0), (0.0, 1.0), width * scale * 1.1, height * scale * 1.3)
            build.reserved.append(box)
            build.id_box = box
            return


def _panel_warnings(builds: list[FaceBuild], settings) -> list[str]:
    shallow = [b.panel_id for b in builds if b.thickness < settings.panel_thickness_mm - 1e-4]
    thin = [b.panel_id for b in builds if b.inner_area_ratio < 0.12]
    tangled = [b.panel_id for b in builds if not b.inner_is_simple]
    messages: list[str] = []
    if shallow:
        thinnest = min(b.thickness for b in builds)
        messages.append(
            f"{len(shallow)} panels were thinned to as little as {thinnest:.2f} mm where sharp folds "
            f"or thin parts of the sculpture leave no room ({', '.join(shallow[:6])})."
        )
    if tangled:
        messages.append(
            f"{len(tangled)} panels have a self-overlapping interior face "
            f"({', '.join(tangled[:6])}). Reduce Skin Thickness or split those faces."
        )
    if thin:
        messages.append(
            f"{len(thin)} panels lose most of their interior to deep miters "
            f"({', '.join(thin[:6])}). Reduce Skin Thickness for this sculpture."
        )
    return messages


def generate_panels(model: ModelData, settings, progress=None) -> tuple[list[bpy.types.Object], list[bpy.types.Object], list[str], dict]:
    """Build every panel and spline. Returns (panels, splines, notes, stats)."""

    def step(fraction: float, message: str) -> None:
        if progress is not None:
            progress(fraction, message)

    # Snapshot source attributes before replacing generated collections. This
    # also prevents a stale Blender RNA reference from leaking into an error.
    overrides = read_edge_overrides(model.source.data)
    source_name = model.source.name
    remove_legacy_collections()
    remove_collection_tree(PLATE_ROOT_COLLECTION)
    remove_collection_tree(PANEL_COLLECTION)
    remove_collection_tree(SPLINE_COLLECTION)
    step(0.02, "Building panel shells")
    depth_limits = _wall_depth_limits(model)
    builds = [_build_shell(model, index, settings, depth_limits[index]) for index in range(len(model.faces))]
    step(0.12, "Clearing places where panels would overlap")
    corner_failures: list[str] = []
    surface_cuts: list[str] = []
    trimmed = _resolve_corner_overlaps(model, builds, corner_failures, surface_cuts)
    clearance = collide.Clearance(margin=0.05)
    for build in builds:
        clearance.set_body(build.face_index, _to_world(build, build.vertices))
        _hold_id_space(build, settings)
    notes: list[str] = _panel_warnings(builds, settings)
    edge_labels: dict[int, list[tuple[tuple[int, int], str]]] = {}
    spline_jobs: list[SplineJob] = []
    label_depths: dict[tuple[int, tuple[int, int]], float] = {}
    spline_codes: dict[tuple[int, tuple[int, int]], str] = {}
    spline_edge_keys: list[tuple[int, int, tuple[int, int], int]] = []
    keyed_edges = 0
    spline_edges = 0
    bare_edges: list[str] = []

    shared_edges = sorted(model.adjacency.items(), key=lambda item: model.source_edge_indices.get(item[0], 10**9))
    for connection_index, (edge_key, uses) in enumerate(shared_edges):
        if connection_index % 16 == 0:
            step(0.15 + 0.55 * connection_index / max(1, len(shared_edges)), "Placing keys and splines")
        first, second = uses[0].face_index, uses[1].face_index
        edge_labels.setdefault(first, []).append((edge_key, core.panel_id(second)))
        edge_labels.setdefault(second, []).append((edge_key, core.panel_id(first)))
        source_edge_index = model.source_edge_indices.get(edge_key, -1)
        override = overrides.get(source_edge_index, dict(_DEFAULT_OVERRIDE))
        length = (model.vertices[edge_key[1]] - model.vertices[edge_key[0]]).length
        count = _auto_count(length, settings) if int(override["count"]) < 0 else int(override["count"])
        male_index, female_index = (first, second) if first < second else (second, first)
        if override["flip"]:
            male_index, female_index = female_index, male_index
        record_a = {"neighbor": core.panel_id(second), "role": "male" if first == male_index else "female", "keys": 0, "splines": 0}
        record_b = {"neighbor": core.panel_id(first), "role": "male" if second == male_index else "female", "keys": 0, "splines": 0}
        builds[first].connections.append(record_a)
        builds[second].connections.append(record_b)
        if override["disabled"]:
            continue

        successful = 0
        reason = "edge is too short to carry a printed key"
        # count == 0 only means no *key* fits; the edge still falls through to
        # the spline path below rather than being left unregistered.
        for tab_index in range(count):
            base_fraction = (tab_index + 1.0) / (count + 1.0)
            requested = base_fraction + float(override[f"offset_{tab_index + 1}"])
            placed = False
            reason = "connector footprint does not fit"
            # Sliding the station and shrinking the connector recovers many
            # joints that a single fixed attempt used to abandon outright.
            for nudge in (0.0, 0.06, -0.06, 0.12, -0.12):
                fraction = max(0.12, min(0.88, requested + nudge))
                for connector_scale in (1.0, 0.85, 0.70, 0.55):
                    trial_width = max(3.2, settings.tab_width_mm * connector_scale)
                    trial_width = min(trial_width, length / (count + 0.65) * 0.62)
                    trial_depth = max(2.2, settings.tab_depth_mm * connector_scale)
                    placed, reason = _add_joint_pair(
                        model,
                        builds,
                        edge_key,
                        male_index,
                        female_index,
                        fraction,
                        max(3.2, trial_width),
                        trial_depth,
                        settings,
                        clearance,
                        height_scale=max(0.6, connector_scale),
                    )
                    if placed:
                        break
                if placed:
                    break
            if placed:
                successful += 1
        record_a["keys"] = record_b["keys"] = successful

        if successful:
            keyed_edges += 1
            continue
        # Inward-folding edges can never carry a crossing key, so they fall back
        # to rails plus a loose spline rather than being left unregistered.
        if not settings.use_splines:
            bare_edges.append(f"{core.panel_id(first)}/{core.panel_id(second)}: {reason}")
            continue
        station_count = 2 if length > settings.short_edge_mm else 1
        made = 0
        for station in range(station_count):
            fraction = (station + 1.0) / (station_count + 1.0)
            job = None
            for scale in (1.0, 0.8, 0.65, 0.5, 0.4):
                for nudge in (0.0, 0.08, -0.08, 0.16, -0.16, 0.24, -0.24):
                    job, spline_reason = _add_spline_pair(
                        model, builds, edge_key, first, second,
                        max(0.12, min(0.88, fraction + nudge)), settings, clearance, scale,
                    )
                    if job is not None:
                        break
                if job is not None:
                    break
            if job is None:
                reason = f"{reason}; spline: {spline_reason}"
                continue
            job.angle_key = int(round(job.dihedral_deg)) % 360
            spline_jobs.append(job)
            made += 1
        record_a["splines"] = record_b["splines"] = made
        if made:
            spline_edges += 1
            depth = max(job.reach for job in spline_jobs[-made:]) + settings.cradle_wall_mm
            label_depths[(first, edge_key)] = depth
            label_depths[(second, edge_key)] = depth
            spline_edge_keys.append((first, second, edge_key, spline_jobs[-1].angle_key))
        else:
            bare_edges.append(f"{core.panel_id(first)}/{core.panel_id(second)}: {reason}")

    # Splines that share a bend angle are the same part, so the model only needs
    # as many codes as it has distinct angles. A short code prints far larger on
    # a small tab than the raw angle did, and keeps panel edge labels compact.
    # Codes are letter-first, which the digit-first panel IDs never are.
    angle_codes = {
        angle: "S" + core.panel_id(index)
        for index, angle in enumerate(sorted({job.angle_key for job in spline_jobs}))
    }
    for job in spline_jobs:
        job.code = angle_codes[job.angle_key]
    for first, second, edge_key, angle in spline_edge_keys:
        spline_codes[(first, edge_key)] = angle_codes[angle]
        spline_codes[(second, edge_key)] = angle_codes[angle]

    step(0.72, "Stamping labels")
    labeller = _add_labels(builds, edge_labels, settings, clearance, label_depths, spline_codes)
    step(0.9, "Creating panel objects")
    collection = create_scene_collection(PANEL_COLLECTION)
    panel_material = _material("PSB_Panel", (0.72, 0.76, 0.82, 1.0))
    objects: list[bpy.types.Object] = []
    for build in builds:
        # Every piece is wound outward where it is built (see _outward), so no
        # normal recalculation is needed -- or wanted, since its outermost-
        # vertex guess can flip a correct shell that has a sliver face.
        mesh = bpy.data.meshes.new(f"PSB_{build.panel_id}_Mesh")
        mesh.from_pydata(build.vertices, [], build.faces)
        mesh.update()
        obj = bpy.data.objects.new(f"PSB_Panel_{build.panel_id}", mesh)
        collection.objects.link(obj)
        obj.matrix_world = build.matrix_world
        obj.data.materials.append(panel_material)
        obj["psb_kind"] = "panel"
        obj["psb_panel_id"] = build.panel_id
        obj["psb_face_index"] = build.face_index
        obj["psb_outer_count"] = len(build.outer_local)
        # The printed outline, stored explicitly: corner trimming rebuilds the
        # body mesh, so vertex order can no longer be relied on to find it.
        obj["psb_outer_loop"] = json.dumps(
            [[round(point.x, 5), round(point.y, 5)] for point in build.outer_local], separators=(",", ":")
        )
        obj["psb_vertex_ids"] = json.dumps(build.vertex_ids, separators=(",", ":"))
        obj["psb_connections"] = json.dumps(build.connections, separators=(",", ":"))
        obj["psb_labels"] = json.dumps(build.labels, separators=(",", ":"))
        obj["psb_thickness_mm"] = round(build.thickness, 3)
        # Slot index of every edge that needs a spline, so the unfolded map can
        # mark exactly those cut edges.
        slots = {}
        ids = build.vertex_ids
        for slot in range(len(ids)):
            key = tuple(sorted((ids[slot], ids[(slot + 1) % len(ids)])))
            code = spline_codes.get((build.face_index, key))
            if code:
                slots[str(slot)] = code
        obj["psb_spline_slots"] = json.dumps(slots, separators=(",", ":"))
        obj["psb_edge_labels"] = json.dumps(
            [label for _edge_key, label in edge_labels.get(build.face_index, [])],
            separators=(",", ":"),
        )
        obj["psb_source_name"] = source_name
        obj["psb_label_font"] = settings.resolved_label_font
        obj["psb_panel_label_height_mm"] = settings.panel_label_size_mm
        obj["psb_joint_label_height_mm"] = settings.edge_label_size_mm
        obj["psb_label_relief_mm"] = settings.label_relief_mm
        objects.append(obj)

    splines: list[bpy.types.Object] = []
    if spline_jobs:
        spline_collection = create_scene_collection(SPLINE_COLLECTION)
        spline_material = _material("PSB_Spline", (0.90, 0.72, 0.36, 1.0))
        for job in spline_jobs:
            obj = _build_spline_object(job, spline_collection, settings, labeller.font, labeller.cache)
            obj.data.materials.append(spline_material)
            obj["psb_source_name"] = source_name
            obj.location = (0.0, 0.0, 0.0)
            splines.append(obj)

    stats = {
        "panels": len(objects),
        "splines": len(splines),
        "edges": len(shared_edges),
        "keyed_edges": keyed_edges,
        "spline_edges": spline_edges,
        "bare_edges": bare_edges,
        "labels_crowded": labeller.crowded,
        "labels_dropped": labeller.dropped,
        "ids_missing": labeller.missing_ids,
        "trimmed_corners": trimmed,
        "corner_failures": corner_failures,
        "surface_cuts": surface_cuts,
        "dropped_labels": labeller.dropped_items,
        "thinned": [build.panel_id for build in builds if build.thickness < settings.panel_thickness_mm - 1e-4],
    }
    if labeller.crowded:
        notes.append(f"{labeller.crowded} labels sit close to their own panel's joint features.")
    if labeller.dropped:
        detail = ""
        if labeller.missing_ids:
            plural = "" if labeller.missing_ids == 1 else "s"
            detail = f" including {labeller.missing_ids} panel ID{plural}"
        notes.append(
            f"{labeller.dropped} labels do not fit their panel at all{detail}; "
            "read those from the unfolded map."
        )
    coverage = f"{keyed_edges} of {len(shared_edges)} edges carry printed keys"
    if splines:
        coverage += f"; {spline_edges} more use {len(splines)} loose splines"
    notes.append(coverage + ".")
    if bare_edges:
        notes.append(f"{len(bare_edges)} edges have no alignment feature; see the report.")
    if corner_failures:
        notes.append(
            f"{len(corner_failures)} panel overlaps could not be trimmed automatically; see the report."
        )
    if surface_cuts:
        notes.append(
            f"{len(surface_cuts)} trims reach the printed outer face at very sharp folds or crowded "
            "corners; see the report."
        )
    step(1.0, "Done")
    return objects, splines, notes, stats


def get_spline_objects() -> list[bpy.types.Object]:
    collection = find_collection(SPLINE_COLLECTION)
    if collection is None:
        return []
    return sorted(
        [obj for obj in collection.objects if obj.get("psb_kind") == "spline"],
        key=lambda obj: str(obj.get("psb_panel_id", "")),
    )


def get_panel_objects() -> list[bpy.types.Object]:
    collection = find_collection(PANEL_COLLECTION)
    if collection is None:
        return []
    return sorted(
        [obj for obj in collection.objects if obj.get("psb_kind") == "panel"],
        key=lambda obj: int(obj.get("psb_face_index", 0)),
    )


def panel_footprint(obj: bpy.types.Object) -> list[core.Point2]:
    return core.convex_hull((vertex.co.x, vertex.co.y) for vertex in obj.data.vertices)
