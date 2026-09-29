# SPDX-FileCopyrightText: 2026 Christian Faur
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure-Python geometry helpers used by Polyshell Builder and its tests."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

Point2 = tuple[float, float]


def panel_id(index: int) -> str:
    """Compact IDs: 1..9, A..Z, then 1A..1Z, 2A..2Z, ..."""
    if index < 0:
        raise ValueError("Panel index cannot be negative.")
    if index < 9:
        return str(index + 1)
    if index < 35:
        return chr(65 + index - 9)
    paired = index - 35
    return f"{paired // 26 + 1}{chr(65 + paired % 26)}"


def cross(a: Point2, b: Point2, c: Point2) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def polygon_area(poly: Sequence[Point2]) -> float:
    return 0.5 * sum(
        poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1]
        for i in range(len(poly))
    )


def polygon_centroid(poly: Sequence[Point2]) -> Point2:
    area6 = 6.0 * polygon_area(poly)
    if abs(area6) < 1e-12:
        return (
            sum(p[0] for p in poly) / max(1, len(poly)),
            sum(p[1] for p in poly) / max(1, len(poly)),
        )
    cx = cy = 0.0
    for i, p in enumerate(poly):
        q = poly[(i + 1) % len(poly)]
        k = p[0] * q[1] - q[0] * p[1]
        cx += (p[0] + q[0]) * k
        cy += (p[1] + q[1]) * k
    return cx / area6, cy / area6


def convex_hull(points: Iterable[Point2]) -> list[Point2]:
    pts = sorted(set((round(float(x), 9), round(float(y), 9)) for x, y in points))
    if len(pts) <= 2:
        return pts

    lower: list[Point2] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 1e-9:
            lower.pop()
        lower.append(p)
    upper: list[Point2] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 1e-9:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def rotate_polygon(poly: Sequence[Point2], angle: float) -> list[Point2]:
    c, s = math.cos(angle), math.sin(angle)
    return [(c * x - s * y, s * x + c * y) for x, y in poly]


def translate_polygon(poly: Sequence[Point2], dx: float, dy: float) -> list[Point2]:
    return [(x + dx, y + dy) for x, y in poly]


def bounds(poly: Sequence[Point2]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def point_in_polygon(point: Point2, poly: Sequence[Point2], include_boundary: bool = True) -> bool:
    x, y = point
    inside = False
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        if abs(cross(a, b, point)) < 1e-8 and min(a[0], b[0]) - 1e-8 <= x <= max(a[0], b[0]) + 1e-8 and min(a[1], b[1]) - 1e-8 <= y <= max(a[1], b[1]) + 1e-8:
            return include_boundary
        if (a[1] > y) != (b[1] > y):
            xhit = (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]
            if x < xhit:
                inside = not inside
    return inside


def _orientation(a: Point2, b: Point2, c: Point2) -> int:
    value = cross(a, b, c)
    if abs(value) < 1e-9:
        return 0
    return 1 if value > 0.0 else -1


def segments_intersect(a: Point2, b: Point2, c: Point2, d: Point2) -> bool:
    o1, o2, o3, o4 = _orientation(a, b, c), _orientation(a, b, d), _orientation(c, d, a), _orientation(c, d, b)
    if o1 * o2 < 0 and o3 * o4 < 0:
        return True
    for p, q, r, o in ((a, b, c, o1), (a, b, d, o2), (c, d, a, o3), (c, d, b, o4)):
        if o == 0 and min(p[0], q[0]) - 1e-9 <= r[0] <= max(p[0], q[0]) + 1e-9 and min(p[1], q[1]) - 1e-9 <= r[1] <= max(p[1], q[1]) + 1e-9:
            return True
    return False


def point_segment_distance(p: Point2, a: Point2, b: Point2) -> float:
    vx, vy = b[0] - a[0], b[1] - a[1]
    denom = vx * vx + vy * vy
    if denom < 1e-15:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / denom))
    return math.hypot(p[0] - (a[0] + t * vx), p[1] - (a[1] + t * vy))


def polygons_distance(a: Sequence[Point2], b: Sequence[Point2]) -> float:
    if point_in_polygon(a[0], b) or point_in_polygon(b[0], a):
        return 0.0
    best = math.inf
    for i, p in enumerate(a):
        p2 = a[(i + 1) % len(a)]
        for j, q in enumerate(b):
            q2 = b[(j + 1) % len(b)]
            if segments_intersect(p, p2, q, q2):
                return 0.0
            best = min(best, point_segment_distance(p, q, q2), point_segment_distance(q, p, p2))
    return best


def orientation_candidates(poly: Sequence[Point2], maximum: int | None = 6) -> list[float]:
    """Continuous, geometry-derived angles rather than 90-degree-only rotations."""
    candidates = [0.0, math.pi * 0.5]
    edges = []
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        edges.append(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2, a, b))
    # Tiny embossed-label corners can create dozens of irrelevant orientations.
    # The longest outline edges carry the useful continuous candidates.
    for _length2, a, b in sorted(edges, reverse=True, key=lambda item: item[0]):
        edge_angle = math.atan2(b[1] - a[1], b[0] - a[0])
        candidates.append((-edge_angle) % math.pi)
        candidates.append((math.pi * 0.5 - edge_angle) % math.pi)
    unique: list[float] = []
    for angle in candidates:
        if not any(abs(((angle - prior + math.pi * 0.5) % math.pi) - math.pi * 0.5) < 1e-6 for prior in unique):
            unique.append(angle)
    return unique if maximum is None else unique[:maximum]


def can_fit_rotated(poly: Sequence[Point2], width: float, height: float) -> bool:
    for angle in orientation_candidates(poly, None):
        r = rotate_polygon(poly, angle)
        x0, y0, x1, y1 = bounds(r)
        if x1 - x0 <= width + 1e-7 and y1 - y0 <= height + 1e-7:
            return True
    return False


def line_intersection(
    point_a: Point2, dir_a: Point2, point_b: Point2, dir_b: Point2
) -> Point2 | None:
    """Intersect two parameterised lines; None when they are near-parallel."""
    denominator = dir_a[0] * dir_b[1] - dir_a[1] * dir_b[0]
    if abs(denominator) < 1e-12:
        return None
    dx, dy = point_b[0] - point_a[0], point_b[1] - point_a[1]
    t = (dx * dir_b[1] - dy * dir_b[0]) / denominator
    return (point_a[0] + dir_a[0] * t, point_a[1] + dir_a[1] * t)


def offset_line(a: Point2, b: Point2, normal: Point2, distance: float) -> tuple[Point2, Point2]:
    """Return the segment a-b displaced by `distance` along `normal`."""
    return (
        (a[0] + normal[0] * distance, a[1] + normal[1] * distance),
        (b[0] + normal[0] * distance, b[1] + normal[1] * distance),
    )


def polygon_is_simple(poly: Sequence[Point2]) -> bool:
    """True when no two non-adjacent edges of the closed polygon cross."""
    count = len(poly)
    if count < 3:
        return False
    for i in range(count):
        a, b = poly[i], poly[(i + 1) % count]
        for j in range(i + 1, count):
            if j == i or (j + 1) % count == i or j == (i + 1) % count:
                continue
            if segments_intersect(a, b, poly[j], poly[(j + 1) % count]):
                return False
    return True


def triangulate_simple_polygon(poly: Sequence[Point2]) -> list[tuple[int, int, int]]:
    """Ear-clip a simple polygon into triangles of original vertex indices."""
    count = len(poly)
    if count < 3:
        return []
    indices = list(range(count))
    if polygon_area(poly) < 0.0:
        indices.reverse()
    triangles: list[tuple[int, int, int]] = []
    guard = 0
    while len(indices) > 3 and guard < count * count + 16:
        guard += 1
        clipped = False
        for position in range(len(indices)):
            previous = indices[position - 1]
            current = indices[position]
            following = indices[(position + 1) % len(indices)]
            if cross(poly[previous], poly[current], poly[following]) <= 1e-12:
                continue
            if any(
                other not in (previous, current, following)
                and point_in_polygon(poly[other], (poly[previous], poly[current], poly[following]))
                for other in indices
            ):
                continue
            triangles.append((previous, current, following))
            indices.pop(position)
            clipped = True
            break
        if not clipped:
            break
    if len(indices) == 3:
        triangles.append((indices[0], indices[1], indices[2]))
    return triangles


@dataclass
class Part2D:
    key: str
    polygon: list[Point2]
    payload: object = None
    _forms: object = None


@dataclass
class Placement:
    part: Part2D
    angle: float
    x: float
    y: float
    shift_x: float
    shift_y: float
    polygon: list[Point2]


@dataclass
class Plate:
    placements: list[Placement] = field(default_factory=list)


def _sample_vertices(poly: Sequence[Point2], maximum: int = 4) -> list[Point2]:
    if len(poly) <= maximum:
        return list(poly)
    return [poly[round(i * len(poly) / maximum) % len(poly)] for i in range(maximum)]


def oriented_box(center: Point2, u: Point2, v: Point2, width: float, height: float) -> list[Point2]:
    """Four corners of a rectangle described by a center and two unit axes."""
    hw, hh = width * 0.5, height * 0.5
    return [
        (center[0] + u[0] * hw * su + v[0] * hh * sv, center[1] + u[1] * hw * su + v[1] * hh * sv)
        for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1))
    ]


def convex_shapes_overlap(a: Sequence[Point2], b: Sequence[Point2]) -> bool:
    """True when two convex polygons share any area."""
    if not a or not b:
        return False
    for i in range(len(a)):
        for j in range(len(b)):
            if segments_intersect(a[i], a[(i + 1) % len(a)], b[j], b[(j + 1) % len(b)]):
                return True
    return point_in_polygon(a[0], b) or point_in_polygon(b[0], a)


MAXIMUM_CANDIDATES = 2400


def _candidate_positions(plate: Plate, moving: Sequence[Point2], w: float, h: float, margin: float, gap: float) -> list[Point2]:
    x_values = {margin}
    y_values = {margin}
    explicit: set[Point2] = {(round(margin, 6), round(margin, 6))}
    interlock_points: set[Point2] = set()
    mobile_samples = _sample_vertices(moving)
    for placed in plate.placements:
        x0, y0, x1, y1 = bounds(placed.polygon)
        x_values.update((x1 + gap, x0 - gap - w))
        y_values.update((y1 + gap, y0 - gap - h))
        explicit.update(((round(x1 + gap, 6), round(max(margin, y0), 6)), (round(max(margin, x0), 6), round(y1 + gap, 6))))
        # A small set of vertex-to-vertex candidates lets triangular/angled parts
        # interlock; final acceptance still uses polygon distance, not rectangles.
        for fixed in _sample_vertices(placed.polygon):
            for mobile in mobile_samples:
                for dx, dy in ((gap, 0.0), (-gap, 0.0), (0.0, gap), (0.0, -gap)):
                    x = fixed[0] - mobile[0] + dx
                    y = fixed[1] - mobile[1] + dy
                    if x >= margin - 1e-7 and y >= margin - 1e-7:
                        interlock_points.add((round(x, 6), round(y, 6)))
    # Never truncate the rectangular skyline grid: doing so can strand the
    # upper rows of an otherwise usable plate. Interlocking candidates are a
    # bounded quality refinement after that complete grid.
    # The skyline grid is the product of every placed part's x and y edges, so
    # it grows quadratically with the number of parts already on the plate.
    # Bottom-left-first ordering means a generous cap costs almost no quality
    # while keeping a full plate from taking minutes to fill. Only the grid
    # rows that can reach the cap are built; later rows would sort after it.
    grid_x = sorted({round(x, 6) for x in x_values if x >= margin - 1e-7})
    grid_y = sorted({round(y, 6) for y in y_values if y >= margin - 1e-7})
    rows: list[Point2] = []
    for y in grid_y:
        if len(rows) >= MAXIMUM_CANDIDATES:
            break
        rows.extend((x, y) for x in grid_x)
    ordered_base = sorted(set(rows) | explicit, key=lambda p: (p[1], p[0]))[:MAXIMUM_CANDIDATES]
    in_grid_x, in_grid_y = set(grid_x), set(grid_y)
    ordered_interlock = [
        point
        for point in sorted(interlock_points, key=lambda p: (p[1], p[0]))
        if point not in explicit and not (point[0] in in_grid_x and point[1] in in_grid_y)
    ][:160]
    return ordered_base + ordered_interlock


def _edge_axes(poly: Sequence[Point2]) -> list[Point2]:
    axes = []
    count = len(poly)
    for index in range(count):
        ax, ay = poly[index]
        bx, by = poly[(index + 1) % count]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length > 1e-12:
            axes.append((-dy / length, dx / length))
    return axes


def _first_clear_candidate(
    moving: Sequence[Point2],
    candidates: list[Point2],
    placed: list[Sequence[Point2]],
    gap: float,
) -> int | None:
    """Index of the first candidate that keeps `moving` at least `gap` from every placed part.

    All parts are convex, so the separating-axis test over both parts' edge
    normals is exact for overlap and gives a lower bound on their distance.
    Every candidate is tested against every placed part in one vectorised pass;
    only the few whose bound falls between touching and `gap` get the exact
    polygon distance. The answer is identical to testing each candidate in
    turn, which made nesting a 300-part model take a quarter of an hour.
    """
    if not candidates:
        return None
    if not placed:
        return 0
    import numpy

    positions = numpy.asarray(candidates, dtype=float)
    shape = numpy.asarray(moving, dtype=float)
    own_axes = _edge_axes(moving)
    rejected = numpy.zeros(len(candidates), dtype=bool)
    unsure = numpy.zeros((len(placed), len(candidates)), dtype=bool)
    for index, polygon in enumerate(placed):
        axes = numpy.asarray(own_axes + _edge_axes(polygon), dtype=float)
        if not len(axes):
            continue
        mine = shape @ axes.T
        theirs = numpy.asarray(polygon, dtype=float) @ axes.T
        shift = positions @ axes.T
        separation = numpy.maximum(
            theirs.min(axis=0) - (mine.max(axis=0) + shift),
            (mine.min(axis=0) + shift) - theirs.max(axis=0),
        ).max(axis=1)
        rejected |= separation <= 1e-9
        unsure[index] = separation < gap - 1e-6
    open_candidates = numpy.flatnonzero(~rejected)
    if not len(open_candidates):
        return None
    doubtful_any = unsure[:, open_candidates].any(axis=0)
    for candidate, doubtful in zip(open_candidates.tolist(), doubtful_any.tolist()):
        if doubtful:
            x, y = candidates[candidate]
            moved = translate_polygon(moving, x, y)
            if any(
                polygons_distance(moved, placed[index]) < gap - 1e-6
                for index in numpy.flatnonzero(unsure[:, candidate]).tolist()
            ):
                continue
        return candidate
    return None


def _oriented_forms(part: Part2D, exhaustive: bool = False) -> list[tuple[float, list[Point2], float, float, float, float]]:
    """Rotated, origin-normalized copies of one part, built once and reused.

    Every plate re-tests the same handful of orientations for the same part, so
    rebuilding them per attempt dominated packing on large models. The
    exhaustive list matches what `can_fit_rotated` considers, so a panel that
    only clears the bed in one unusual rotation is still placeable.
    """
    slot = 1 if exhaustive else 0
    cached = getattr(part, "_forms", None)
    if cached is None:
        cached = [None, None]
        part._forms = cached
    if cached[slot] is None:
        forms = []
        for angle in orientation_candidates(part.polygon, None if exhaustive else 8):
            rotated = rotate_polygon(part.polygon, angle)
            rx0, ry0, rx1, ry1 = bounds(rotated)
            forms.append(
                (angle, translate_polygon(rotated, -rx0, -ry0), rx1 - rx0, ry1 - ry0, rx0, ry0)
            )
        cached[slot] = forms
    return cached[slot]


def _try_place(
    part: Part2D,
    plate: Plate,
    bed_x: float,
    bed_y: float,
    margin: float,
    gap: float,
    exhaustive: bool = False,
) -> Placement | None:
    usable_area = (bed_x - 2.0 * margin) * (bed_y - 2.0 * margin)
    occupied_area = sum(abs(polygon_area(item.part.polygon)) for item in plate.placements)
    if occupied_area + abs(polygon_area(part.polygon)) > usable_area + 1e-7:
        return None
    best: tuple[tuple[float, float, float, float, float], Placement] | None = None
    # Cached once per call; recomputing these inside the candidate loop was the
    # dominant cost when a plate carried many parts.
    placed_bounds = [bounds(item.polygon) for item in plate.placements]
    skyline_x = max([margin] + [item[2] for item in placed_bounds])
    skyline_y = max([margin] + [item[3] for item in placed_bounds])
    placed_polygons = [item.polygon for item in plate.placements]
    for angle, normalized, w, h, rx0, ry0 in _oriented_forms(part, exhaustive):
        if w > bed_x - 2.0 * margin + 1e-7 or h > bed_y - 2.0 * margin + 1e-7:
            continue
        candidates = [
            (x, y)
            for x, y in _candidate_positions(plate, normalized, w, h, margin, gap)
            if x + w <= bed_x - margin + 1e-7 and y + h <= bed_y - margin + 1e-7
        ]
        chosen = _first_clear_candidate(normalized, candidates, placed_polygons, gap)
        if chosen is None:
            continue
        x, y = candidates[chosen]
        moved = translate_polygon(normalized, x, y)
        used_x = max(skyline_x, x + w)
        used_y = max(skyline_y, y + h)
        # Prefer the orientation and position that grows the occupied
        # rectangle least. Ties favor a low, wide skyline that leaves a
        # simple region for the next descending-size part.
        occupied_area = (used_x - margin) * (used_y - margin)
        score = (occupied_area, used_y, used_x, y, x)
        placement = Placement(part, angle, x, y, x - rx0, y - ry0, moved)
        if best is None or score < best[0]:
            best = (score, placement)
        if used_x <= skyline_x + 1e-7 and used_y <= skyline_y + 1e-7:
            # Fits entirely inside the space already committed; no other
            # orientation can score better, so stop looking.
            return placement
    return None if best is None else best[1]


def smallest_extent(poly: Sequence[Point2]) -> tuple[float, float]:
    """Smallest bounding-box footprint over the candidate rotations (longer side first)."""
    best = None
    for angle in orientation_candidates(poly, None):
        x0, y0, x1, y1 = bounds(rotate_polygon(poly, angle))
        w, h = sorted((x1 - x0, y1 - y0), reverse=True)
        if best is None or w * h < best[0] * best[1]:
            best = (w, h)
    return best or (0.0, 0.0)


def pack_parts(
    parts: Sequence[Part2D],
    bed_x: float,
    bed_y: float,
    margin: float,
    gap: float,
    progress=None,
) -> list[Plate]:
    usable_x, usable_y = bed_x - 2.0 * margin, bed_y - 2.0 * margin
    if usable_x <= 0.0 or usable_y <= 0.0:
        raise ValueError("The bed safety margin leaves no usable print area.")
    ordered = sorted(parts, key=lambda p: abs(polygon_area(p.polygon)), reverse=True)
    for part in ordered:
        if not can_fit_rotated(part.polygon, usable_x, usable_y):
            w, h = smallest_extent(part.polygon)
            raise ValueError(
                f"Part {part.key} measures {w:.0f} x {h:.0f} mm including its keys and does not fit the "
                f"usable bed ({usable_x:.0f} x {usable_y:.0f} mm). Reduce the Maximum Dimension, the "
                "Safety Margin, or enable Auto-Split Oversize Panels."
            )

    # Fill one plate completely before opening the next: begin with the
    # largest remaining panel, then repeatedly scan downward in area for the
    # largest panel that can still fit in its best geometry-derived rotation.
    remaining = list(ordered)
    plates: list[Plate] = []
    while remaining:
        plate = Plate()
        first = remaining.pop(0)
        first_placement = _try_place(first, plate, bed_x, bed_y, margin, gap)
        if first_placement is None:
            # An empty bed is retried across every rotation rather than the
            # usual shortlist; this happens once per plate, so it costs nothing.
            first_placement = _try_place(first, plate, bed_x, bed_y, margin, gap, exhaustive=True)
        if first_placement is None:
            raise ValueError(f"Part {first.key} could not be placed on an empty print bed.")
        plate.placements.append(first_placement)
        if progress is not None:
            progress(1)

        # Placing a part only ever removes free space, so anything that has
        # already been refused by this plate cannot fit it later. Without this
        # the scan retried every past failure on every placement, which is what
        # made large models take minutes to nest.
        blocked: set[int] = set()
        while remaining:
            selected_index = None
            selected_placement = None
            for index, part in enumerate(remaining):
                if id(part) in blocked:
                    continue
                placement = _try_place(part, plate, bed_x, bed_y, margin, gap)
                if placement is None:
                    blocked.add(id(part))
                    continue
                selected_index = index
                selected_placement = placement
                break
            if selected_index is None or selected_placement is None:
                break
            plate.placements.append(selected_placement)
            remaining.pop(selected_index)
            if progress is not None:
                progress(1)
        plates.append(plate)
    return plates
