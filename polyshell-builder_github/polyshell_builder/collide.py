# SPDX-FileCopyrightText: 2026 Christian Faur
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Assembled-clearance checks between convex parts, in double precision.

Every key, rail, spline, and label is a convex solid, and every panel body is
tested through its convex hull. Hulls are never smaller than the real part, so
a placement that clears a hull is guaranteed to clear the part itself.

Blender's own vectors and BVH trees are single precision. At sculpture scale
that is only good to a few hundredths of a millimeter, which is the same order
as the clearances being checked, so everything here uses plain Python floats.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

Point3 = tuple[float, float, float]


def _sub(a: Point3, b: Point3) -> Point3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Point3, b: Point3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Point3, b: Point3) -> Point3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _neg(a: Point3) -> Point3:
    return (-a[0], -a[1], -a[2])


def _length_squared(a: Point3) -> float:
    return _dot(a, a)


def _triple(a: Point3, b: Point3, c: Point3) -> Point3:
    """(a x b) x c"""
    return _cross(_cross(a, b), c)


def _support(points: list[Point3], direction: Point3) -> Point3:
    best = points[0]
    best_value = _dot(best, direction)
    for point in points[1:]:
        value = _dot(point, direction)
        if value > best_value:
            best, best_value = point, value
    return best


def intersects(a: list[Point3], b: list[Point3], margin: float = 0.0) -> bool:
    """True when conv(a) and conv(b) come within `margin` of each other.

    GJK on the Minkowski difference, with `b` inflated by a sphere of radius
    `margin`. Non-convergence is reported as a collision, so the answer is
    never optimistic.
    """
    if not a or not b:
        return False

    def support(direction: Point3) -> Point3:
        length = _dot(direction, direction) ** 0.5
        pa = _support(a, direction)
        pb = _support(b, _neg(direction))
        point = _sub(pa, pb)
        if margin > 0.0 and length > 0.0:
            scale = margin / length
            point = (point[0] + direction[0] * scale, point[1] + direction[1] * scale, point[2] + direction[2] * scale)
        return point

    centre_a = tuple(sum(p[i] for p in a) / len(a) for i in range(3))
    centre_b = tuple(sum(p[i] for p in b) / len(b) for i in range(3))
    direction = _sub(centre_a, centre_b)
    if _length_squared(direction) < 1e-18:
        direction = (1.0, 0.0, 0.0)
    simplex = [support(direction)]
    direction = _neg(simplex[0])
    for _iteration in range(96):
        if _length_squared(direction) < 1e-24:
            return True
        point = support(direction)
        if _dot(point, direction) < 0.0:
            return False
        simplex.append(point)
        hit, simplex, direction = _next_simplex(simplex)
        if hit:
            return True
    return True


def _next_simplex(simplex: list[Point3]) -> tuple[bool, list[Point3], Point3]:
    if len(simplex) == 2:
        b, a = simplex
        ab, ao = _sub(b, a), _neg(a)
        if _dot(ab, ao) > 0.0:
            direction = _triple(ab, ao, ab)
            if _length_squared(direction) < 1e-24:
                # the origin lies on the segment
                return True, simplex, direction
            return False, [b, a], direction
        return False, [a], ao
    if len(simplex) == 3:
        c, b, a = simplex
        ab, ac, ao = _sub(b, a), _sub(c, a), _neg(a)
        abc = _cross(ab, ac)
        if _dot(_cross(abc, ac), ao) > 0.0:
            if _dot(ac, ao) > 0.0:
                return False, [c, a], _triple(ac, ao, ac)
            return _next_simplex([b, a])
        if _dot(_cross(ab, abc), ao) > 0.0:
            return _next_simplex([b, a])
        side = _dot(abc, ao)
        if abs(side) < 1e-18:
            return True, simplex, abc
        if side > 0.0:
            return False, [c, b, a], abc
        return False, [b, c, a], _neg(abc)
    d, c, b, a = simplex
    ab, ac, ad, ao = _sub(b, a), _sub(c, a), _sub(d, a), _neg(a)
    abc, acd, adb = _cross(ab, ac), _cross(ac, ad), _cross(ad, ab)
    if _dot(abc, ao) > 0.0:
        return _next_simplex([c, b, a])
    if _dot(acd, ao) > 0.0:
        return _next_simplex([d, c, a])
    if _dot(adb, ao) > 0.0:
        return _next_simplex([b, d, a])
    return True, simplex, ao


def _bounds(points: list[Point3]) -> tuple[Point3, Point3]:
    return (
        (min(p[0] for p in points), min(p[1] for p in points), min(p[2] for p in points)),
        (max(p[0] for p in points), max(p[1] for p in points), max(p[2] for p in points)),
    )


def _boxes_near(a, b, pad: float) -> bool:
    return all(a[0][i] - pad <= b[1][i] and b[0][i] - pad <= a[1][i] for i in range(3))


@dataclass
class Solid:
    owner: int
    kind: str
    points: list[Point3]
    box: tuple[Point3, Point3] = field(default=None)
    # Bodies sort before features, then in the order they were added, so a
    # query reports the same first collision whichever grid cells it visits.
    order: tuple[int, int] = (0, 0)

    def __post_init__(self):
        if self.box is None:
            self.box = _bounds(self.points)


class Clearance:
    """Every body and every placed feature in assembly coordinates.

    Solids are bucketed in a coarse grid so a query only visits its own
    neighbourhood; testing each candidate against every part in a 300-panel
    model made large builds take tens of minutes.
    """

    CELL = 24.0

    def __init__(self, margin: float = 0.05):
        self.margin = margin
        self.bodies: dict[int, Solid] = {}
        self.features: list[Solid] = []
        self._grid: dict[tuple[int, int, int], list[Solid]] = {}
        self._serial = itertools.count()

    def _cells(self, box: tuple[Point3, Point3], pad: float):
        low = [math.floor((box[0][axis] - pad) / self.CELL) for axis in range(3)]
        high = [math.floor((box[1][axis] + pad) / self.CELL) for axis in range(3)]
        return itertools.product(*(range(low[axis], high[axis] + 1) for axis in range(3)))

    def _insert(self, solid: Solid) -> None:
        for cell in self._cells(solid.box, 0.0):
            self._grid.setdefault(cell, []).append(solid)

    def _nearby(self, box: tuple[Point3, Point3], pad: float) -> list[Solid]:
        found: dict[int, Solid] = {}
        for cell in self._cells(box, pad):
            for solid in self._grid.get(cell, ()):
                found[id(solid)] = solid
        return sorted(found.values(), key=lambda solid: solid.order)

    def set_body(self, owner: int, points: list[Point3]) -> None:
        previous = self.bodies.get(owner)
        if previous is not None:
            for cell in self._cells(previous.box, 0.0):
                members = self._grid.get(cell)
                if members and previous in members:
                    members.remove(previous)
        solid = Solid(owner, "body", points, order=(0, next(self._serial)))
        self.bodies[owner] = solid
        self._insert(solid)

    def blocked(
        self,
        points: list[Point3],
        host: int,
        skip_bodies: set[int] = frozenset(),
        lift: dict[int, Point3] | None = None,
        margin: float | None = None,
        ignore_host_features: bool = False,
    ) -> str:
        """Why a candidate solid would collide, or '' when it is clear.

        `skip_bodies` are bodies the part is built into; `lift` gives, per body
        the part is designed to rest on, a small offset that separates the
        intended contact so only genuine penetration is reported.
        `ignore_host_features` lets a last-resort label crowd its own panel's
        rails and key roots, never anything belonging to another panel.
        """
        margin = self.margin if margin is None else margin
        box = _bounds(points)
        pad = margin + 0.5
        for solid in self._nearby(box, pad):
            if not _boxes_near(box, solid.box, pad):
                continue
            if solid.kind == "body":
                owner = solid.owner
                if owner in skip_bodies:
                    continue
                if lift and owner in lift:
                    offset = lift[owner]
                    candidate = [(p[0] + offset[0], p[1] + offset[1], p[2] + offset[2]) for p in points]
                    if intersects(candidate, solid.points, 0.0):
                        return f"body:{owner}"
                    continue
                if intersects(points, solid.points, margin):
                    return f"body:{owner}"
                continue
            if ignore_host_features and solid.owner == host:
                continue
            if intersects(points, solid.points, margin):
                return f"{solid.kind}:{solid.owner}"
        return ""

    def add(self, owner: int, kind: str, points: list[Point3]) -> None:
        solid = Solid(owner, kind, points, order=(1, next(self._serial)))
        self.features.append(solid)
        self._insert(solid)
