# SPDX-FileCopyrightText: 2026 Christian Faur
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Derive every joint, label, and relief dimension from four plain inputs.

Sculpture size, nozzle, layer height, and a fit preference are things a user
actually knows. Key depths, miter limits, and corner reliefs are not, so they
are computed here instead of being guessed in the sidebar.
"""

from __future__ import annotations


FIT_CLEARANCE = {"SNUG": 0.35, "STANDARD": 0.45, "LOOSE": 0.62}

# Thickness drives everything structural. The mitered glue face between two
# panels is `thickness / sin(dihedral / 2)` wide, so it is the dominant lever on
# how accurately a glued pair holds its angle.
THICKNESS_RULES = {
    "ECONOMY": (0.005, 2.0, 5.0),
    "STANDARD": (0.008, 3.0, 8.0),
    "RIGID": (0.0, 6.35, 6.35),
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def recommended_thickness(target_mm: float, mode: str) -> float:
    rule = THICKNESS_RULES.get(mode)
    if rule is None:
        return 0.0
    factor, low, high = rule
    if factor <= 0.0:
        return low
    return round(_clamp(max(50.0, target_mm) * factor, low, high), 2)


def recommended(
    target_mm: float,
    nozzle_mm: float,
    layer_mm: float,
    fit: str,
    thickness_mm: float,
) -> dict[str, float]:
    """Return every derived dimension for one sculpture and printer."""
    size = max(50.0, target_mm)
    nozzle = max(0.1, nozzle_mm)
    layer = max(0.05, layer_mm)
    thickness = max(0.4, thickness_mm)
    clearance = round(nozzle * FIT_CLEARANCE.get(fit, 0.45), 2)
    return {
        "clearance_mm": clearance,
        "tab_width_mm": round(_clamp(size * 0.022, 8.0, 18.0), 1),
        "tab_depth_mm": round(_clamp(thickness * 1.6, 3.5, 9.0), 1),
        "tab_height_mm": round(_clamp(thickness * 0.75, 1.5, 4.0), 1),
        "cradle_wall_mm": round(max(3.0 * nozzle, 1.2), 2),
        "short_edge_mm": round(_clamp(size * 0.16, 30.0, 200.0), 0),
        "long_edge_mm": round(_clamp(size * 0.30, 60.0, 400.0), 0),
        "panel_label_size_mm": round(_clamp(size * 0.014, 5.0, 14.0), 1),
        "edge_label_size_mm": round(_clamp(size * 0.009, 3.5, 9.0), 1),
        # Three layers of relief is the shallowest a slicer reliably renders as
        # solid raised type rather than dropping it.
        "label_relief_mm": round(max(3.0 * layer, 0.45), 2),
        # Blunt the knife edge for the first few layers so the exterior corner
        # prints square instead of tapering below one extrusion width.
        "edge_relief_mm": round(min(_clamp(3.0 * layer, 0.3, 0.9), thickness * 0.45), 2),
        # Bounds how far a mitered corner may run from its mesh vertex, so acute
        # corners end blunt instead of in a fragile spike. Collisions between
        # panels at a vertex are removed exactly afterwards, so this can stay
        # small and leave the glue faces and joint space as long as possible.
        "corner_relief_mm": round(_clamp(thickness * 0.6, 1.2, 4.0), 1),
        "max_cradle_lift_mm": round(_clamp(thickness * 2.5, 4.0, 14.0), 1),
        "spline_width_mm": round(_clamp(size * 0.014, 6.0, 12.0), 1),
        # Long enough to grip the rails and to carry its two-character code.
        "spline_reach_mm": round(
            _clamp(max(thickness * 3.0, _clamp(size * 0.009, 3.5, 9.0) * 2.4), 10.0, 26.0), 1
        ),
        # Arm depth; also how tall the guide rails stand.
        "spline_height_mm": round(_clamp(thickness * 0.9, 4.4, 6.5), 1),
    }


# Derived values auto-tune owns; everything else stays under the user's control.
TUNED = (
    "clearance_mm", "tab_width_mm", "tab_depth_mm", "tab_height_mm", "cradle_wall_mm",
    "short_edge_mm", "long_edge_mm", "panel_label_size_mm", "edge_label_size_mm",
    "label_relief_mm", "edge_relief_mm", "corner_relief_mm", "max_cradle_lift_mm",
    "spline_width_mm", "spline_reach_mm", "spline_height_mm",
)


def apply(settings) -> None:
    """Re-derive thickness and every tuned value from the headline inputs.

    Thickness comes from the preset, then is held to what validation measured
    this particular mesh can carry. It is always recomputed from the preset
    rather than only ever lowered, so switching to a gentler model, or scaling
    one up, restores the full thickness instead of inheriting the last clamp.
    """
    if not settings.auto_tune or settings.get("_psb_tuning"):
        return
    settings["_psb_tuning"] = True
    try:
        if settings.thickness_preset != "CUSTOM":
            thickness = recommended_thickness(settings.target_dimension_mm, settings.thickness_preset)
            ratio = settings.mesh_thickness_limit_ratio
            if ratio > 0.0:
                thickness = min(thickness, round(ratio * settings.target_dimension_mm, 2))
            settings.panel_thickness_mm = thickness
        for name, value in recommended(
            settings.target_dimension_mm,
            settings.nozzle_mm,
            settings.layer_height_mm,
            settings.fit_preset,
            settings.panel_thickness_mm,
        ).items():
            setattr(settings, name, value)
    finally:
        settings["_psb_tuning"] = False
