# SPDX-FileCopyrightText: 2026 Christian Faur
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import math
import os
import re
import struct

import bpy
from mathutils import Matrix, Vector

from . import core, geometry
from .pdf_writer import PDFDocument


def _make_bed_outline(collection: bpy.types.Collection, bed_x: float, bed_y: float, index: int) -> None:
    mesh = bpy.data.meshes.new(f"PSB_Bed_{index:03d}_Mesh")
    mesh.from_pydata(((0.0, 0.0, 0.0), (bed_x, 0.0, 0.0), (bed_x, bed_y, 0.0), (0.0, bed_y, 0.0)), ((0, 1), (1, 2), (2, 3), (3, 0)), ())
    obj = bpy.data.objects.new(f"PSB_Bed_Outline_{index:03d}", mesh)
    collection.objects.link(obj)
    obj.display_type = "WIRE"
    obj.color = (0.08, 0.35, 0.90, 1.0)
    obj["psb_kind"] = "bed_outline"


def create_print_plates(settings, progress=None) -> list[bpy.types.Collection]:
    panels = geometry.get_panel_objects()
    if not panels:
        raise geometry.PolyshellError("Generate panels before packing print plates.")
    loose = geometry.get_spline_objects()
    total = len(panels) + len(loose)
    done = [0]

    def advance(count: int) -> None:
        done[0] += count
        if progress is not None:
            progress(done[0] / max(1, total), f"Nesting part {done[0]} of {total}")

    def nest(objects):
        parts = [core.Part2D(obj["psb_panel_id"], geometry.panel_footprint(obj), obj) for obj in objects]
        try:
            return core.pack_parts(
                parts, settings.bed_x_mm, settings.bed_y_mm, settings.bed_margin_mm, settings.part_gap_mm,
                advance,
            )
        except ValueError as exc:
            raise geometry.PolyshellError(str(exc)) from exc

    packed = nest(panels)
    # Splines are small and numerous. Nesting them after the panels keeps them
    # on their own dense plates, which prints and sorts as one batch instead of
    # scattering tiny parts across every panel plate.
    if loose:
        packed = packed + nest(loose)

    geometry.remove_collection_tree(geometry.PLATE_ROOT_COLLECTION)
    root = geometry.create_scene_collection(geometry.PLATE_ROOT_COLLECTION)
    collections: list[bpy.types.Collection] = []
    for plate_index, plate in enumerate(packed, 1):
        collection = bpy.data.collections.new(f"PSB_Plate_{plate_index:03d}")
        root.children.link(collection)
        collection["psb_kind"] = "print_plate"
        collection["psb_plate_index"] = plate_index
        _make_bed_outline(collection, settings.bed_x_mm, settings.bed_y_mm, plate_index)
        for placement in plate.placements:
            source = placement.part.payload
            duplicate = source.copy()
            duplicate.data = source.data.copy()
            duplicate.name = f"Plate_{plate_index:03d}_{placement.part.key}"
            collection.objects.link(duplicate)
            duplicate.matrix_world = Matrix.Translation((placement.shift_x, placement.shift_y, 0.0)) @ Matrix.Rotation(
                placement.angle, 4, "Z"
            )
            duplicate["psb_kind"] = "plate_part"
            duplicate["psb_part_kind"] = source.get("psb_kind", "panel")
            duplicate["psb_plate_index"] = plate_index
            duplicate["psb_rotation_degrees"] = math.degrees(placement.angle)
        collections.append(collection)
    return collections


def get_plate_collections() -> list[bpy.types.Collection]:
    root = geometry.find_collection(geometry.PLATE_ROOT_COLLECTION)
    if root is None:
        return []
    return sorted([collection for collection in root.children if collection.get("psb_kind") == "print_plate"], key=lambda c: c.name)


def output_directory(settings) -> str:
    """Absolute output folder.

    A path starting with // is relative to the saved .blend file. An unsaved
    file has no folder, and Blender would silently resolve the path against
    whatever directory it was launched from (on macOS, the read-only disk
    root), so fall back to a folder in the user's home directory instead.
    """
    raw = settings.output_directory.strip() or "//Polyshell_Output/"
    if raw.startswith("//") and not bpy.data.filepath:
        tail = raw[2:].strip("/\\") or "Polyshell_Output"
        return os.path.join(os.path.expanduser("~"), tail)
    return os.path.normpath(bpy.path.abspath(raw))


def ensure_output_directory(settings) -> str:
    path = output_directory(settings)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise geometry.PolyshellError(f"Cannot create the output folder {path}: {exc.strerror}.") from exc
    if not os.access(path, os.W_OK):
        raise geometry.PolyshellError(f"The output folder {path} is not writable. Choose another Output Folder.")
    return path


def _write_binary_stl(path: str, objects: list[bpy.types.Object]) -> int:
    """Binary STL in millimetres, straight from mesh data.

    Writing directly avoids the STL export operator, which depends on the
    current selection and editor mode and has changed options between Blender
    releases.
    """
    triangles = []
    for obj in objects:
        mesh = obj.data
        mesh.calc_loop_triangles()
        matrix = obj.matrix_world
        points = [matrix @ vertex.co for vertex in mesh.vertices]
        for triangle in mesh.loop_triangles:
            a, b, c = (points[index] for index in triangle.vertices)
            normal = (b - a).cross(c - a)
            if normal.length > 0.0:
                normal.normalize()
            triangles.append((normal, a, b, c))
    temporary = path + ".partial"
    with open(temporary, "wb") as handle:
        # A header starting with "solid" makes some slicers guess ASCII.
        handle.write(b"Polyshell Builder binary STL, millimetres".ljust(80, b" "))
        handle.write(struct.pack("<I", len(triangles)))
        record = struct.Struct("<12fH")
        for normal, a, b, c in triangles:
            handle.write(record.pack(normal.x, normal.y, normal.z, a.x, a.y, a.z, b.x, b.y, b.z, c.x, c.y, c.z, 0))
    os.replace(temporary, path)
    return len(triangles)


def export_stl_plates(settings) -> tuple[list[str], int]:
    """Write one STL per plate. Returns the paths and how many stale plates were removed."""
    plates = get_plate_collections()
    if not plates:
        raise geometry.PolyshellError("Pack print plates before exporting STL files.")
    directory = ensure_output_directory(settings)
    prefix = settings.stl_prefix.strip() or "polyshell"
    paths: list[str] = []
    for plate_index, collection in enumerate(plates, 1):
        parts = [obj for obj in collection.objects if obj.get("psb_kind") == "plate_part"]
        if not parts:
            continue
        filepath = os.path.join(directory, f"{prefix}_plate_{plate_index:03d}.stl")
        _write_binary_stl(filepath, parts)
        paths.append(filepath)
    # Plates beyond this export's count are left over from an earlier, larger
    # build. Printing them would add parts from the wrong model, so remove them.
    pattern = re.compile(rf"^{re.escape(prefix)}_plate_(\d{{3,}})\.stl$")
    removed = 0
    for name in os.listdir(directory):
        match = pattern.match(name)
        if match and int(match.group(1)) > len(plates):
            os.remove(os.path.join(directory, name))
            removed += 1
    return paths, removed


def _outer_loop(panel: bpy.types.Object) -> list[core.Point2]:
    """Printed outline in panel-local XY, from the stored loop when available."""
    raw = panel.get("psb_outer_loop")
    if raw:
        try:
            return [(float(x), float(y)) for x, y in json.loads(raw)]
        except (TypeError, ValueError):
            pass
    count = int(panel.get("psb_outer_count", 0))
    return [(panel.data.vertices[i].co.x, panel.data.vertices[i].co.y) for i in range(count)]


# Two opposite camera positions, both above the sculpture. Each pair of screen
# axes is chosen so that right x up points at the camera, which keeps the
# drawing from being mirrored.
FRONT_VIEW = (Vector((1.0, -1.0, 0.72)), Vector((1.0, 1.0, 0.0)))
BACK_VIEW = (Vector((-1.0, 1.0, 0.72)), Vector((-1.0, -1.0, 0.0)))
UNDERSIDE_VIEW = (Vector((1.0, -1.0, -1.4)), Vector((1.0, 1.0, 0.0)))
SMALL_MODEL_PANELS = 60


def _project_exploded_panels(panels: list[bpy.types.Object], explode_distance: float, camera=FRONT_VIEW):
    """Panels pushed out along their normals and projected, farthest first."""
    view = camera[0].normalized()
    screen_x = camera[1].normalized()
    screen_y = view.cross(screen_x).normalized()
    projected = []
    for panel in panels:
        outward = -(panel.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
        points3 = [
            panel.matrix_world @ Vector((x, y, 0.0)) + outward * explode_distance for x, y in _outer_loop(panel)
        ]
        points2 = [(point.dot(screen_x), point.dot(screen_y)) for point in points3]
        center3 = sum(points3, Vector()) / max(1, len(points3))
        projected.append(
            {
                "id": panel["psb_panel_id"],
                "points": points2,
                "center": (center3.dot(screen_x), center3.dot(screen_y)),
                "depth": center3.dot(view),
                # Positive when the printed outer face looks at the camera.
                "facing": outward.dot(view),
            }
        )
    return sorted(projected, key=lambda item: item["depth"])


def _label_box(x: float, y: float, text: str, size: float) -> tuple[float, float, float, float]:
    width = len(text) * size * 0.62
    height = size * 0.95
    return (x - width * 0.5, y - height * 0.5, x + width * 0.5, y + height * 0.5)


def _box_samples(box: tuple[float, float, float, float]) -> list[core.Point2]:
    x0, y0, x1, y1 = box
    xm, ym = (x0 + x1) * 0.5, (y0 + y1) * 0.5
    return [(xm, ym), (x0, y0), (x1, y0), (x1, y1), (x0, y1), (xm, y0), (xm, y1), (x0, ym), (x1, ym)]


def _boxes_overlap(a, b, pad: float = 0.8) -> bool:
    return a[0] - pad < b[2] and b[0] - pad < a[2] and a[1] - pad < b[3] and b[1] - pad < a[3]


def _draw_exploded_page(document: PDFDocument, drawing: list[dict], title: str, subtitle: str, opaque: bool) -> set[str]:
    """One exploded view. Returns the IDs that received a label.

    Only labels that can be read unambiguously are printed: a panel facing away
    from the camera is seen from inside, and an ID printed where another panel
    covers it would name the wrong part. Opaque pages label only panels that
    face the camera and are not hidden; the translucent overview also names
    the far side, in gray. Nothing is ever overprinted.
    """
    all_points = [point for item in drawing for point in item["points"]]
    min_x = min(point[0] for point in all_points)
    max_x = max(point[0] for point in all_points)
    min_y = min(point[1] for point in all_points)
    max_y = max(point[1] for point in all_points)
    area_x, area_y, area_w, area_h = 36.0, 82.0, 540.0, 642.0
    scale = min(area_w / max(1e-8, max_x - min_x), area_h / max(1e-8, max_y - min_y))
    offset_x = area_x + (area_w - (max_x - min_x) * scale) * 0.5 - min_x * scale
    offset_y = area_y + (area_h - (max_y - min_y) * scale) * 0.5 - min_y * scale

    page = document.add_page()
    page.text(36, 758, title, 16, bold=True)
    page.text(36, 739, subtitle, 8.5)
    page.rectangle(area_x, area_y, area_w, area_h, stroke_gray=0.72)
    screen = []
    for index, item in enumerate(drawing):
        points = [(x * scale + offset_x, y * scale + offset_y) for x, y in item["points"]]
        screen.append(points)
        tint = 0.88 + (index % 5) * 0.018
        if opaque and item["facing"] <= 0.0:
            # Seen from inside: darker, so the eye reads it as the far wall.
            tint -= 0.14
        page.polygon(
            points,
            fill=(tint, min(0.97, tint + 0.02), min(0.99, tint + 0.04)),
            opacity=1.0 if opaque else 0.72,
        )

    extents = [core.bounds(points) for points in screen]

    def covered(sample: core.Point2, index: int) -> bool:
        """True when a panel drawn after `index` (nearer the camera) hides the point."""
        x, y = sample
        for other in range(index + 1, len(screen)):
            x0, y0, x1, y1 = extents[other]
            if x0 <= x <= x1 and y0 <= y <= y1 and core.point_in_polygon(sample, screen[other]):
                return True
        return False

    sides = sorted(math.sqrt(abs(core.polygon_area(points))) for points in screen)
    typical = sides[len(sides) // 2] if sides else 20.0
    base_size = max(5.0, min(10.0, typical * 0.34))
    placed: list[tuple[float, float, float, float]] = []
    labelled: set[str] = set()
    nearest_first = sorted(range(len(drawing)), key=lambda index: -drawing[index]["depth"])
    passes = [(True, 0.0)] if opaque else [(True, 0.0), (False, 0.35)]
    for front, gray in passes:
        for index in nearest_first:
            item = drawing[index]
            if (item["facing"] > 0.0) != front:
                continue
            points = screen[index]
            center = core.polygon_centroid(points)
            size = base_size if front else base_size * 0.8
            while size >= 4.0:
                box = _label_box(center[0], center[1], item["id"], size)
                samples = _box_samples(box)
                fits = all(core.point_in_polygon(sample, points) for sample in samples)
                if fits and not (opaque and any(covered(sample, index) for sample in samples)):
                    break
                size *= 0.85
            else:
                # Too small to hold its ID inside the outline: still label it
                # at the minimum size as long as the spot is clear and visible.
                size = 4.0
                box = _label_box(center[0], center[1], item["id"], size)
                samples = _box_samples(box)
                if not core.point_in_polygon(center, points) or (
                    opaque and any(covered(sample, index) for sample in samples)
                ):
                    continue
            if any(_boxes_overlap(box, other) for other in placed):
                continue
            placed.append(box)
            labelled.add(item["id"])
            page.text(
                box[0], box[1] + size * 0.12, item["id"], size, bold=front,
                color=None if front else (gray, gray, gray),
            )
    return labelled


def _unfold_data(panels: list[bpy.types.Object]):
    ordered = sorted(panels, key=lambda panel: int(panel.get("psb_face_index", 0)))
    saved_vertex_ids: dict[str, list] = {}
    use_saved_ids = True
    loops = {panel["psb_panel_id"]: _outer_loop(panel) for panel in ordered}
    for panel in ordered:
        try:
            vertex_ids = json.loads(panel.get("psb_vertex_ids", "[]"))
        except (TypeError, ValueError):
            vertex_ids = []
        if len(vertex_ids) != len(loops[panel["psb_panel_id"]]):
            use_saved_ids = False
        saved_vertex_ids[panel["psb_panel_id"]] = vertex_ids

    entries: dict[str, dict] = {}
    edge_records: dict[frozenset, list[dict]] = {}
    for panel in ordered:
        panel_id = panel["psb_panel_id"]
        local_points = loops[panel_id]
        if use_saved_ids:
            tokens = saved_vertex_ids[panel_id]
        else:
            tokens = []
            for x, y in local_points:
                point = panel.matrix_world @ Vector((x, y, 0.0))
                tokens.append((round(point.x, 3), round(point.y, 3), round(point.z, 3)))
        entries[panel_id] = {
            "id": panel_id,
            "points": local_points,
            "tokens": tokens,
            "area": abs(core.polygon_area(local_points)),
            "face_index": int(panel.get("psb_face_index", 0)),
        }
        for slot, a in enumerate(tokens):
            b = tokens[(slot + 1) % len(tokens)]
            edge_records.setdefault(frozenset((a, b)), []).append(
                {"panel": panel_id, "slot": slot, "a": a, "b": b}
            )
    return entries, edge_records


def _transform_child_for_unfold(
    child: dict,
    edge_key: frozenset,
    parent_points: list[core.Point2],
    parent: dict,
    gap: float,
) -> list[core.Point2]:
    a, b = tuple(edge_key)
    parent_map = dict(zip(parent["tokens"], parent_points))
    child_map = dict(zip(child["tokens"], child["points"]))
    target_a, target_b = Vector(parent_map[a]), Vector(parent_map[b])
    source_a, source_b = Vector(child_map[a]), Vector(child_map[b])
    target_axis = target_b - target_a
    source_axis = source_b - source_a
    angle = math.atan2(target_axis.y, target_axis.x) - math.atan2(source_axis.y, source_axis.x)
    cosine, sine = math.cos(angle), math.sin(angle)

    def rotate(point):
        return Vector(
            (
                point[0] * cosine - point[1] * sine,
                point[0] * sine + point[1] * cosine,
            )
        )

    translation = target_a - rotate(source_a)
    candidate = [rotate(point) + translation for point in child["points"]]
    edge_unit = target_axis.normalized()
    left = Vector((-edge_unit.y, edge_unit.x))
    parent_center = Vector(core.polygon_centroid(parent_points))
    midpoint = (target_a + target_b) * 0.5
    parent_side = (parent_center - midpoint).dot(left)
    outward = -left if parent_side >= 0.0 else left
    return [(point.x + outward.x * gap, point.y + outward.y * gap) for point in candidate]


def _build_unfolded_islands(panels: list[bpy.types.Object], gap: float = 0.5):
    entries, edge_records = _unfold_data(panels)
    adjacency: dict[str, list[tuple[frozenset, str]]] = {panel_id: [] for panel_id in entries}
    for edge_key, records in edge_records.items():
        if len(records) != 2:
            continue
        first, second = records
        adjacency[first["panel"]].append((edge_key, second["panel"]))
        adjacency[second["panel"]].append((edge_key, first["panel"]))

    unplaced = set(entries)
    islands: list[dict[str, list[core.Point2]]] = []
    joined_edges: set[frozenset] = set()
    maximum_group_panels = 80
    while unplaced:
        root_id = max(unplaced, key=lambda panel_id: entries[panel_id]["area"])
        placed = {root_id: list(entries[root_id]["points"])}
        unplaced.remove(root_id)
        while True:
            if len(placed) >= maximum_group_panels:
                break
            best = None
            for parent_id, parent_points in placed.items():
                for edge_key, child_id in adjacency[parent_id]:
                    if child_id not in unplaced:
                        continue
                    candidate = _transform_child_for_unfold(
                        entries[child_id],
                        edge_key,
                        parent_points,
                        entries[parent_id],
                        gap,
                    )
                    if any(
                        core.polygons_distance(candidate, polygon) < gap * 0.80
                        for polygon in placed.values()
                    ):
                        continue
                    all_points = [
                        point
                        for polygon in list(placed.values()) + [candidate]
                        for point in polygon
                    ]
                    x0, y0, x1, y1 = core.bounds(all_points)
                    score = ((x1 - x0) * (y1 - y0), -entries[child_id]["area"])
                    if best is None or score < best[0]:
                        best = (score, child_id, edge_key, candidate)
            if best is None:
                break
            _score, child_id, edge_key, candidate = best
            placed[child_id] = candidate
            unplaced.remove(child_id)
            joined_edges.add(edge_key)
        islands.append(placed)
    return entries, edge_records, islands, joined_edges


def _arrange_unfolded_islands(islands: list[dict[str, list[core.Point2]]], separation: float):
    measured = []
    total_area = 0.0
    maximum_width = 0.0
    for island in islands:
        points = [point for polygon in island.values() for point in polygon]
        x0, y0, x1, y1 = core.bounds(points)
        width, height = x1 - x0, y1 - y0
        measured.append((island, x0, y0, width, height))
        total_area += max(1.0, width * height)
        maximum_width = max(maximum_width, width)
    target_width = max(maximum_width, math.sqrt(total_area * 1.65))
    measured.sort(key=lambda item: item[3] * item[4], reverse=True)

    arranged: dict[str, list[core.Point2]] = {}
    x = y = row_height = 0.0
    for island, x0, y0, width, height in measured:
        if x > 0.0 and x + width > target_width:
            x = 0.0
            y += row_height + separation
            row_height = 0.0
        shift_x, shift_y = x - x0, y - y0
        for panel_id, polygon in island.items():
            arranged[panel_id] = [
                (point[0] + shift_x, point[1] + shift_y)
                for point in polygon
            ]
        x += width + separation
        row_height = max(row_height, height)
    return arranged


SPLINE_RED = (0.82, 0.10, 0.12)


def _spline_slot_lookup(panels: list[bpy.types.Object]) -> dict[tuple[str, int], str]:
    lookup: dict[tuple[str, int], str] = {}
    for panel in panels:
        try:
            slots = json.loads(panel.get("psb_spline_slots", "{}"))
        except (TypeError, ValueError):
            slots = {}
        for slot, code in slots.items():
            lookup[(panel["psb_panel_id"], int(slot))] = code
    return lookup


def _paginate_islands(
    islands: list[dict[str, list[core.Point2]]], per_page: int
) -> list[list[dict[str, list[core.Point2]]]]:
    """Group unfold islands into sheets of roughly `per_page` panels each.

    One sheet per model makes the drawing unreadable past a couple of hundred
    panels, which is exactly when the map matters most.
    """
    pages: list[list[dict[str, list[core.Point2]]]] = []
    current: list[dict[str, list[core.Point2]]] = []
    count = 0
    for island in sorted(islands, key=len, reverse=True):
        if current and count + len(island) > per_page:
            pages.append(current)
            current, count = [], 0
        current.append(island)
        count += len(island)
    if current:
        pages.append(current)
    return pages


def _add_unfolded_map_pages(
    document: PDFDocument,
    panels: list[bpy.types.Object],
    target_dimension: float,
    per_page: int = 85,
) -> None:
    area_x, area_y, area_w, area_h = 36.0, 52.0, 1152.0, 670.0
    gap = 0.5
    for _iteration in range(2):
        entries, edge_records, islands, joined_edges = _build_unfolded_islands(panels, gap)
        arranged = _arrange_unfolded_islands(islands, max(5.0, target_dimension * 0.015))
        all_points = [point for polygon in arranged.values() for point in polygon]
        min_x, min_y, max_x, max_y = core.bounds(all_points)
        scale = min(area_w / max(1e-8, max_x - min_x), area_h / max(1e-8, max_y - min_y))
        # 1 PDF point is 1/72 inch. Rebuild once so the displayed gap is
        # physically 0.5 mm on the printed sheet at any drawing scale.
        gap = (0.5 * 72.0 / 25.4) / max(scale, 1e-8)

    spline_slots = _spline_slot_lookup(panels)
    order = {panel["psb_panel_id"]: index for index, panel in enumerate(panels)}
    pages = _paginate_islands(islands, per_page)
    sheet_of = {panel_id: number for number, group in enumerate(pages, 1) for island in group for panel_id in island}
    for page_index, group in enumerate(pages, 1):
        arranged = _arrange_unfolded_islands(group, max(5.0, target_dimension * 0.015))
        all_points = [point for polygon in arranged.values() for point in polygon]
        min_x, min_y, max_x, max_y = core.bounds(all_points)
        scale = min(area_w / max(1e-8, max_x - min_x), area_h / max(1e-8, max_y - min_y))
        page = document.add_page(1224.0, 792.0)
        page.text(36, 758, "POLYSHELL BUILDER - UNFOLDED ASSEMBLY MAP", 16, bold=True)
        page.text(
            36,
            740,
            f"Sheet {page_index} of {len(pages)} | Tabloid landscape (11 x 17 in) | "
            "Cut edges show the mating panel ID.",
            8.5,
        )
        page.text(
            36,
            728,
            "Red edges need a loose spline; the red code is stamped on the spline itself. See the spline index.",
            8.5,
            color=SPLINE_RED,
        )
        offset_x = area_x + (area_w - (max_x - min_x) * scale) * 0.5 - min_x * scale
        offset_y = area_y + (area_h - (max_y - min_y) * scale) * 0.5 - min_y * scale

        occupied: list[tuple[float, float, float, float]] = []

        def stamp(text: str, x: float, y: float, size: float, color=None, bold=True) -> None:
            """Draw map text, nudging it clear of anything already placed."""
            width = len(text) * size * 0.62
            height = size * 0.95
            for shift_x, shift_y in (
                (0.0, 0.0), (0.0, height), (0.0, -height), (width * 0.7, 0.0),
                (-width * 0.7, 0.0), (width * 0.7, height), (-width * 0.7, -height),
                (0.0, height * 2.0), (0.0, -height * 2.0),
            ):
                left = x + shift_x - width * 0.5
                bottom = y + shift_y - height * 0.5
                box = (left, bottom, left + width, bottom + height)
                if any(
                    box[0] < item[2] and item[0] < box[2] and box[1] < item[3] and item[1] < box[3]
                    for item in occupied
                ):
                    continue
                occupied.append(box)
                page.text(left, bottom, text, size, bold=bold, color=color)
                return
            # Everything collided; draw it anyway rather than lose the ID.
            page.text(x - width * 0.5, y - height * 0.5, text, size, bold=bold, color=color)

        members = {panel_id for island in group for panel_id in island}
        transformed: dict[str, list[core.Point2]] = {}
        for index, panel_id in enumerate(sorted(members, key=lambda item: order.get(item, 10**9))):
            points = [(x * scale + offset_x, y * scale + offset_y) for x, y in arranged[panel_id]]
            transformed[panel_id] = points
            tint = 0.90 + (index % 4) * 0.018
            page.polygon(
                points,
                fill=(min(0.98, tint), min(0.98, tint + 0.015), min(0.99, tint + 0.035)),
                width=0.65,
            )

        label_size = max(5.5, min(11.0, 11.0 - max(0, len(members) - 45) * 0.05))
        for panel_id, points in transformed.items():
            center = core.polygon_centroid(points)
            stamp(panel_id, center[0], center[1], label_size)

        # Light bridge lines make the intentionally separated tree seams read as
        # attached. Every other edge is a cut and receives reciprocal panel IDs.
        for edge_key in joined_edges:
            records = edge_records.get(edge_key, [])
            if len(records) != 2 or any(item["panel"] not in transformed for item in records):
                continue
            midpoints = []
            for record in records:
                points = transformed[record["panel"]]
                slot = record["slot"]
                a, b = points[slot], points[(slot + 1) % len(points)]
                midpoints.append(((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5))
            page.line(*midpoints[0], *midpoints[1], width=0.35, gray=0.62)

        for edge_key, records in edge_records.items():
            if len(records) != 2:
                continue
            present = [item for item in records if item["panel"] in transformed]
            if not present:
                continue
            code = next(
                (
                    spline_slots.get((item["panel"], item["slot"]))
                    for item in records
                    if spline_slots.get((item["panel"], item["slot"]))
                ),
                None,
            )
            if len(present) == 1:
                # The mate is on another sheet: this is exactly the seam a
                # builder has to find across pages, so say where it continues.
                record = present[0]
                other = records[1] if record is records[0] else records[0]
                points = transformed[record["panel"]]
                slot = record["slot"]
                a, b = Vector(points[slot]), Vector(points[(slot + 1) % len(points)])
                if code:
                    page.line(a.x, a.y, b.x, b.y, width=1.6, color=SPLINE_RED)
                midpoint = (a + b) * 0.5
                outward = midpoint - Vector(core.polygon_centroid(points))
                if outward.length > 1e-8:
                    outward.normalize()
                location = midpoint + outward * (label_size * 0.85)
                stamp(other["panel"], location.x, location.y, max(5.0, label_size - 0.7))
                note = midpoint + outward * (label_size * 1.9)
                stamp(f"sheet {sheet_of.get(other['panel'], '?')}", note.x, note.y, max(4.5, label_size - 2.5),
                      color=(0.35, 0.35, 0.35), bold=False)
                if code:
                    spot = midpoint + outward * (label_size * 3.0)
                    stamp(code, spot.x, spot.y, max(6.0, label_size), color=SPLINE_RED)
                continue
            if code:
                # Trace the seam itself in red so the spline requirement reads
                # at a glance, whether or not the two panels stayed joined.
                for record in records:
                    points = transformed[record["panel"]]
                    slot = record["slot"]
                    a, b = points[slot], points[(slot + 1) % len(points)]
                    page.line(a[0], a[1], b[0], b[1], width=1.6, color=SPLINE_RED)
            if edge_key in joined_edges:
                if code:
                    points = transformed[records[0]["panel"]]
                    slot = records[0]["slot"]
                    a, b = Vector(points[slot]), Vector(points[(slot + 1) % len(points)])
                    middle = (a + b) * 0.5
                    along = b - a
                    across = Vector((-along.y, along.x))
                    if across.length > 1e-8:
                        across.normalize()
                    spot = middle + across * (label_size * 0.95)
                    stamp(code, spot.x, spot.y, max(6.0, label_size), color=SPLINE_RED)
                continue
            for record, other in ((records[0], records[1]), (records[1], records[0])):
                points = transformed[record["panel"]]
                slot = record["slot"]
                a, b = Vector(points[slot]), Vector(points[(slot + 1) % len(points)])
                midpoint = (a + b) * 0.5
                center = Vector(core.polygon_centroid(points))
                outward = midpoint - center
                if outward.length > 1e-8:
                    outward.normalize()
                location = midpoint + outward * (label_size * 0.85)
                stamp(other["panel"], location.x, location.y, max(5.0, label_size - 0.7))
                if code:
                    spot = midpoint + outward * (label_size * 2.1)
                    stamp(code, spot.x, spot.y, max(6.0, label_size), color=SPLINE_RED)
        page.text(
            36,
            30,
            f"{len(members)} panels on this sheet, {len(panels)} in total across "
            f"{len(pages)} sheet(s). Close gaps in sequence; match every cut-edge ID.",
            8,
        )


def _add_spline_pages(document: PDFDocument, splines: list[bpy.types.Object]) -> None:
    if not splines:
        return
    # Splines are identified by bend angle, so every spline sharing an angle is
    # the same part and they are interchangeable.
    groups: dict[str, list[str]] = {}
    angles: dict[str, int] = {}
    for obj in splines:
        code = str(obj.get("psb_panel_id", ""))
        groups.setdefault(code, []).append(str(obj.get("psb_spline_panels", "")))
        angles[code] = int(obj.get("psb_spline_angle", 0))
    rows = sorted(groups.items(), key=lambda item: angles.get(item[0], 0))
    rows_per_page = 34
    page_count = math.ceil(len(rows) / rows_per_page)
    for start_index in range(0, len(rows), rows_per_page):
        page = document.add_page()
        page.text(36, 758, "SPLINE INDEX", 16, bold=True)
        page.text(
            36,
            742,
            f"{len(splines)} splines in {len(rows)} sizes. Every spline is stamped with its code.",
            8.5,
            color=SPLINE_RED,
        )
        page.text(
            36,
            730,
            "Splines sharing a code are identical; any one of them fits any edge marked with that code.",
            8.0,
        )
        page.rectangle(36, 54, 540, 660, stroke_gray=0.78)
        page.text(50, 706, "CODE", 8, bold=True)
        page.text(96, 706, "BEND", 8, bold=True)
        page.text(140, 706, "QTY", 8, bold=True)
        page.text(176, 706, "JOINS", 8, bold=True)
        y = 688.0
        for code, pairs in rows[start_index : start_index + rows_per_page]:
            page.text(50, y, code, 12, bold=True, color=SPLINE_RED)
            page.text(96, y, f"{angles.get(code, 0)} deg", 9)
            page.text(140, y, f"x{len(pairs)}", 9)
            joined = ", ".join(sorted(set(pairs)))
            page.text(176, y, joined[:88] + ("..." if len(joined) > 88 else ""), 8.5)
            y -= 18.5
        page.text(
            36,
            35,
            f"Spline index page {start_index // rows_per_page + 1} of {page_count}",
            8,
        )


def export_assembly_pdf(settings) -> str:
    panels = geometry.get_panel_objects()
    if not panels:
        raise geometry.PolyshellError("Generate panels before exporting the assembly guide.")
    directory = ensure_output_directory(settings)
    filename = settings.pdf_filename if settings.pdf_filename.lower().endswith(".pdf") else settings.pdf_filename + ".pdf"
    filepath = os.path.join(directory, filename)
    explode = settings.target_dimension_mm * settings.exploded_distance_percent / 100.0

    document = PDFDocument()
    ids = "IDs: 1-9, A-Z, then 1A..."
    if len(panels) <= SMALL_MODEL_PANELS:
        labelled = _draw_exploded_page(
            document,
            _project_exploded_panels(panels, explode, FRONT_VIEW),
            "POLYSHELL BUILDER - EXPLODED ASSEMBLY VIEW",
            f"{len(panels)} panels | {ids} | Black IDs face you; gray IDs are on the far side.",
            opaque=False,
        )
        pages = [document.pages[-1]]
    else:
        # A large model drawn see-through is a thicket of overlapping IDs, so
        # it is shown solid from two opposite sides instead.
        labelled = set()
        pages = []
        for camera, name in ((FRONT_VIEW, "FRONT"), (BACK_VIEW, "BACK"), (UNDERSIDE_VIEW, "UNDERSIDE")):
            found = _draw_exploded_page(
                document,
                _project_exploded_panels(panels, explode, camera),
                f"POLYSHELL BUILDER - EXPLODED VIEW, {name}",
                f"{len(panels)} panels | {ids} | Panels facing you are labelled; darker panels are seen from inside.",
                opaque=True,
            )
            # The view from below is only worth a page when it names panels
            # (a base, an underside) that neither upper view could show.
            if camera is UNDERSIDE_VIEW and not found - labelled:
                document.pages.pop()
                continue
            labelled |= found
            pages.append(document.pages[-1])
    missing = len(panels) - len(labelled)
    page = pages[-1]
    if missing:
        page.text(
            36, 68,
            f"{missing} panels have no clear spot in {'this view' if len(pages) == 1 else 'these views'}; "
            "every panel is on the unfolded map.",
            8.5,
        )
    spline_count = len(geometry.get_spline_objects())
    footer = "Dry-fit several neighboring panels before gluing. Leave user-designated cap edges until last."
    if spline_count:
        footer = (
            f"{spline_count} loose splines register the inward-folding joints. Red edges on the "
            "unfolded map show where they go."
        )
    for page in pages:
        page.text(36, 54, "Glue alignment: an edge label is the ID of the panel that connects to that edge.", 8.5)
        page.text(36, 40, footer, 8.5)

    _add_unfolded_map_pages(document, panels, settings.target_dimension_mm)
    _add_spline_pages(document, geometry.get_spline_objects())

    document.save(filepath)
    return filepath
