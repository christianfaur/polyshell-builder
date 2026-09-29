# SPDX-FileCopyrightText: 2026 Christian Faur
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Plain-text build report, kept in the .blend and written beside the exports.

The sidebar can only show a few short lines, and the system console is not
visible at all on macOS unless Blender was started from a terminal. Everything
a builder needs to know about a run -- which edges have no alignment and why,
which panels were thinned or trimmed -- goes here instead.
"""

from __future__ import annotations

import datetime
import os
import re

import bpy

TEXT_NAME = "Polyshell Report"
FILE_NAME = "polyshell_report.txt"


def addon_version() -> str:
    manifest = os.path.join(os.path.dirname(__file__), "blender_manifest.toml")
    try:
        with open(manifest, encoding="utf-8") as handle:
            match = re.search(r'^version\s*=\s*"([^"]+)"', handle.read(), re.MULTILINE)
        return match.group(1) if match else "unknown"
    except OSError:
        return "unknown"


def _settings_lines(settings) -> list[str]:
    return [
        f"Source object:        {settings.last_source_name or '-'}",
        f"Maximum dimension:    {settings.target_dimension_mm:.1f} mm",
        f"Thickness:            {settings.panel_thickness_mm:.2f} mm "
        f"({settings.thickness_preset.title()}; this mesh can carry {settings.mesh_thickness_limit_mm:.2f} mm)",
        f"Printer:              {settings.nozzle_mm:.2f} mm nozzle, {settings.layer_height_mm:.2f} mm layers, "
        f"{settings.fit_preset.title()} fit, auto-tune {'on' if settings.auto_tune else 'off'}",
        f"Joint clearance:      {settings.clearance_mm:.2f} mm",
        f"Key:                  {settings.tab_width_mm:.1f} x {settings.tab_depth_mm:.1f} x {settings.tab_height_mm:.1f} mm",
        f"Print bed:            {settings.bed_x_mm:.0f} x {settings.bed_y_mm:.0f} mm, margin {settings.bed_margin_mm:.1f} mm, "
        f"gap {settings.part_gap_mm:.1f} mm",
    ]


def write(settings, title: str, notes: list[str], stats: dict | None = None) -> str:
    """Replace the report text block and return its contents."""
    lines = [
        f"Polyshell Builder {addon_version()}  -  {title}",
        f"{datetime.datetime.now():%Y-%m-%d %H:%M}  |  Blender {bpy.app.version_string}",
        "",
    ]
    lines.extend(_settings_lines(settings))
    lines.append("")
    if stats:
        lines.append(
            f"Panels {stats['panels']}  |  splines {stats['splines']}  |  edges {stats['edges']}: "
            f"{stats['keyed_edges']} keyed, {stats['spline_edges']} with splines, "
            f"{len(stats['bare_edges'])} without alignment"
        )
        lines.append("")
    lines.extend(note for note in notes if note)
    if stats:
        sections = (
            ("Edges without any alignment feature (glue these by eye)", stats["bare_edges"]),
            ("Panels thinned below the set thickness where sharp folds or thin parts leave no room", stats["thinned"]),
            ("Panels trimmed where they would have hit another panel", stats["trimmed_corners"]),
            (
                "Trims that reach the printed outer face, blunting a very sharp fold or a crowded "
                "corner slightly (a thinner preset reduces them)",
                stats.get("surface_cuts", []),
            ),
            ("Overlaps that could not be trimmed (check these pairs when dry-fitting)", stats.get("corner_failures", [])),
            ("Labels with no room on their panel (use the unfolded map for these)", stats.get("dropped_labels", [])),
        )
        for heading, items in sections:
            if items:
                lines.extend(("", f"{heading}: {len(items)}"))
                lines.extend(f"  {item}" for item in items)
    text = "\n".join(lines) + "\n"
    block = bpy.data.texts.get(TEXT_NAME) or bpy.data.texts.new(TEXT_NAME)
    block.clear()
    block.write(text)
    return text


def save_to_folder(settings, text: str) -> str:
    from . import exporters

    path = os.path.join(exporters.ensure_output_directory(settings), FILE_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path
