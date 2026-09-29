# SPDX-FileCopyrightText: 2026 Christian Faur
#
# SPDX-License-Identifier: GPL-3.0-or-later

import textwrap

import bpy
from bpy.types import Panel

from . import presets, report


def _wrapped(layout, text: str, width_px: float, icon: str = "NONE") -> None:
    """Sidebar labels do not wrap; split long status lines to the panel width.

    Region widths are in device pixels, so the character width has to follow
    the interface scale (Retina displays and the Resolution Scale preference),
    or every line is cut short with an ellipsis.
    """
    scale = max(0.5, bpy.context.preferences.system.ui_scale)
    columns = max(20, int(width_px / (6.4 * scale)) - 2)
    first = True
    for paragraph in text.split("\n"):
        for line in textwrap.wrap(paragraph, columns) or [""]:
            layout.label(text=line, icon=icon if first else "NONE")
            first = False


class PSB_PT_main(Panel):
    bl_label = "Polyshell Builder"
    bl_idname = "PSB_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Polyshell"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.polyshell_builder

        status = layout.box()
        status.label(text="Status")
        column = status.column(align=True)
        column.scale_y = 0.85
        _wrapped(column, "\n".join(settings.status.split("\n")[:8]), context.region.width)
        if bpy.data.texts.get(report.TEXT_NAME):
            _wrapped(status, f"Full report: Text Editor > {report.TEXT_NAME}", context.region.width, icon="TEXT")
        if settings.panel_count:
            row = f"{settings.panel_count} panels  |  {settings.plate_count} plates"
            if settings.spline_count:
                row += f"  |  {settings.spline_count} splines"
            status.label(text=row)
        if settings.skipped_joint_count:
            status.label(text=f"{settings.skipped_joint_count} edges without alignment", icon="ERROR")

        source = layout.box()
        source.label(text="1. Sculpture", icon="MESH_DATA")
        source.prop(settings, "target_dimension_mm")
        row = source.row(align=True)
        row.label(text="Thickness")
        row.prop(settings, "thickness_preset", text="")
        row = source.row()
        row.enabled = settings.thickness_preset == "CUSTOM" or not settings.auto_tune
        row.prop(settings, "panel_thickness_mm")
        source.prop(settings, "auto_triangulate")
        source.prop(settings, "configure_scene_units")
        source.operator("polyshell_builder.validate", icon="CHECKMARK")

        printer = layout.box()
        printer.label(text="2. Printer", icon="TOOL_SETTINGS")
        row = printer.row(align=True)
        row.prop(settings, "nozzle_mm", text="Nozzle")
        row.prop(settings, "layer_height_mm", text="Layer")
        printer.prop(settings, "fit_preset", text="Fit")
        printer.prop(settings, "auto_tune")
        if settings.auto_tune:
            info = printer.column(align=True)
            info.scale_y = 0.8
            info.label(
                text=f"Clearance {settings.clearance_mm:.2f}  Key {settings.tab_width_mm:.0f}x"
                f"{settings.tab_depth_mm:.1f}x{settings.tab_height_mm:.1f} mm"
            )
            info.label(
                text=f"Labels {settings.panel_label_size_mm:.1f}/{settings.edge_label_size_mm:.1f} mm"
                f"  Corner relief {settings.corner_relief_mm:.1f} mm"
            )

        bed = layout.box()
        bed.label(text="3. Print Bed", icon="MESH_GRID")
        row = bed.row(align=True)
        row.prop(settings, "bed_x_mm", text="Bed X")
        row.prop(settings, "bed_y_mm", text="Bed Y")
        row = bed.row(align=True)
        row.prop(settings, "bed_margin_mm", text="Margin")
        row.prop(settings, "part_gap_mm", text="Gap")
        bed.prop(settings, "auto_split_oversize")
        bed.prop(settings, "use_splines")

        advanced = layout.box()
        advanced.prop(
            settings,
            "show_advanced",
            icon="TRIA_DOWN" if settings.show_advanced else "TRIA_RIGHT",
            emboss=False,
        )
        if settings.show_advanced:
            if settings.auto_tune:
                advanced.label(text="Auto-Tune sets the greyed values; turn it off to edit them.", icon="INFO")

            def tuned(box, name):
                row = box.row()
                row.enabled = not (settings.auto_tune and name in presets.TUNED)
                row.prop(settings, name)

            joints = advanced.box()
            joints.label(text="Glue Alignment", icon="MOD_BOOLEAN")
            tuned(joints, "clearance_mm")
            tuned(joints, "tab_width_mm")
            tuned(joints, "tab_depth_mm")
            tuned(joints, "tab_height_mm")
            tuned(joints, "cradle_wall_mm")
            tuned(joints, "short_edge_mm")
            tuned(joints, "long_edge_mm")
            joints.prop(settings, "min_joint_angle_deg")
            tuned(joints, "max_cradle_lift_mm")

            shell = advanced.box()
            shell.label(text="Shell Relief", icon="MOD_BEVEL")
            tuned(shell, "edge_relief_mm")
            tuned(shell, "corner_relief_mm")
            shell.prop(settings, "min_dihedral_deg")

            spline = advanced.box()
            spline.label(text="Splines", icon="LINKED")
            tuned(spline, "spline_width_mm")
            tuned(spline, "spline_reach_mm")
            tuned(spline, "spline_height_mm")

            labels = advanced.box()
            labels.label(text="Interior Labels", icon="FONT_DATA")
            tuned(labels, "panel_label_size_mm")
            tuned(labels, "edge_label_size_mm")
            tuned(labels, "label_relief_mm")
            labels.prop(settings, "label_font_path")
            if settings.resolved_label_font:
                labels.label(text=f"Using: {settings.resolved_label_font}", icon="CHECKMARK")

        if context.object and context.object.type == "MESH" and context.mode == "EDIT_MESH":
            override = layout.box()
            override.label(text="Selected Edge Override", icon="EDGESEL")
            override.prop(settings, "override_tab_count")
            override.prop(settings, "override_flip")
            override.prop(settings, "override_disabled")
            override.prop(settings, "override_offset_1")
            override.prop(settings, "override_offset_2")
            override.prop(settings, "override_offset_3")
            row = override.row(align=True)
            row.operator("polyshell_builder.load_edge_override", text="Load")
            row.operator("polyshell_builder.apply_edge_override", text="Apply")
            row.operator("polyshell_builder.clear_edge_override", text="Clear")

        actions = layout.box()
        actions.label(text="4. Generate", icon="OUTLINER_COLLECTION")
        actions.operator("polyshell_builder.generate", icon="MOD_SOLIDIFY")
        actions.operator("polyshell_builder.pack", icon="UV")

        output = layout.box()
        output.label(text="5. Output", icon="EXPORT")
        output.prop(settings, "output_directory", text="Folder")
        output.prop(settings, "stl_prefix", text="Prefix")
        output.prop(settings, "pdf_filename", text="PDF")
        output.prop(settings, "exploded_distance_percent", text="Explode")
        row = output.row(align=True)
        row.operator("polyshell_builder.export_stl", text="STL Plates")
        row.operator("polyshell_builder.export_pdf", text="PDF Guide")
        output.operator("polyshell_builder.build_all", icon="PLAY")
