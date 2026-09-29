# SPDX-FileCopyrightText: 2026 Christian Faur
#
# SPDX-License-Identifier: GPL-3.0-or-later

from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

from . import presets


def _retune(self, _context) -> None:
    """Recompute every derived dimension after a headline input changes."""
    presets.apply(self)


class PSB_Settings(PropertyGroup):
    # ---------------------------------------------------------------- sculpture
    target_dimension_mm: FloatProperty(
        name="Maximum Dimension (mm)",
        description="Largest finished sculpture dimension in millimeters",
        default=500.0,
        min=10.0,
        soft_max=5000.0,
        update=_retune,
    )
    thickness_preset: EnumProperty(
        name="Panel Thickness",
        description=(
            "Thicker panels widen the mitered glue face between neighbors, which "
            "is what actually holds the assembled angle"
        ),
        items=(
            ("ECONOMY", "Economy", "Thinnest usable skin; least filament, least angular control"),
            ("STANDARD", "Standard", "Balanced skin scaled to the sculpture size"),
            ("RIGID", "Rigid (1/4 in)", "6.35 mm; widest glue face and room for full-depth alignment features"),
            ("CUSTOM", "Custom", "Set the thickness by hand"),
        ),
        default="STANDARD",
        update=_retune,
    )
    panel_thickness_mm: FloatProperty(
        name="Skin Thickness (mm)",
        description="Panel depth; alignment and label geometry is added above its interior",
        default=4.0,
        min=0.6,
        soft_max=10.0,
        update=_retune,
    )
    auto_triangulate: BoolProperty(
        name="Auto-Triangulate Bent Faces",
        description=(
            "Split faces that are not flat or not convex into triangles, without changing "
            "the source mesh. Off: such faces stop validation so you can fix them by hand"
        ),
        default=True,
    )
    configure_scene_units: BoolProperty(
        name="Display Scene in Millimeters",
        description="Set this scene to Metric millimeter display so generated physical dimensions read correctly in Blender",
        default=True,
    )

    # ------------------------------------------------------------------ printer
    auto_tune: BoolProperty(
        name="Auto-Tune From Printer",
        description=(
            "Derive clearances, key sizes, label sizes, and reliefs from the sculpture "
            "size, nozzle, and layer height. Turn off to edit every value by hand"
        ),
        default=True,
        update=_retune,
    )
    nozzle_mm: FloatProperty(
        name="Nozzle (mm)", default=0.4, min=0.1, soft_max=1.2, precision=2, update=_retune
    )
    layer_height_mm: FloatProperty(
        name="Layer Height (mm)", default=0.2, min=0.05, soft_max=0.4, precision=2, update=_retune
    )
    fit_preset: EnumProperty(
        name="Joint Fit",
        items=(
            ("SNUG", "Snug", "Tight press fit; needs a well-calibrated printer"),
            ("STANDARD", "Standard", "Slides together by hand and stays put"),
            ("LOOSE", "Loose", "Extra room for glue and for printers that swell corners"),
        ),
        default="STANDARD",
        update=_retune,
    )

    # ------------------------------------------------------------------- joints
    clearance_mm: FloatProperty(name="Joint Clearance (mm)", default=0.18, min=0.0, soft_max=0.6, precision=2)
    tab_width_mm: FloatProperty(name="Key Width (mm)", default=11.0, min=3.0, soft_max=25.0)
    tab_depth_mm: FloatProperty(name="Key Depth (mm)", default=6.4, min=2.0, soft_max=15.0)
    tab_height_mm: FloatProperty(name="Key Height (mm)", default=3.0, min=0.6, soft_max=6.0)
    cradle_wall_mm: FloatProperty(name="Pocket Wall (mm)", default=1.2, min=0.6, soft_max=3.0)
    short_edge_mm: FloatProperty(name="2-Key Threshold (mm)", default=80.0, min=10.0)
    long_edge_mm: FloatProperty(name="3-Key Threshold (mm)", default=150.0, min=20.0)
    min_joint_angle_deg: FloatProperty(
        name="Minimum Printable Angle",
        description="A crossing key must rise at least this steeply to print without support",
        default=40.0,
        min=5.0,
        max=85.0,
    )
    max_cradle_lift_mm: FloatProperty(
        name="Maximum Cradle Lift (mm)",
        description="Tallest support ramp allowed under a crossing key before the edge switches to a spline",
        default=10.0,
        min=0.0,
        soft_max=25.0,
    )

    # ------------------------------------------------------------------ splines
    use_splines: BoolProperty(
        name="Splines On Inward Edges",
        description=(
            "Inward-folding edges have no shared interior volume, so no printed key can "
            "cross them without support. Add guide rails plus a separate printed spline instead"
        ),
        default=True,
    )
    spline_width_mm: FloatProperty(name="Spline Width (mm)", default=7.0, min=3.0, soft_max=20.0)
    spline_reach_mm: FloatProperty(name="Spline Reach (mm)", default=12.0, min=4.0, soft_max=40.0)
    spline_height_mm: FloatProperty(name="Spline Height (mm)", default=4.4, min=2.0, soft_max=8.0)

    # ------------------------------------------------------------ shell relief
    edge_relief_mm: FloatProperty(
        name="Outer Edge Relief (mm)",
        description=(
            "Height of the square rim held before the miter starts. Keeps the first "
            "layers of the exterior edge printable instead of tapering to a feather"
        ),
        default=0.6,
        min=0.0,
        soft_max=1.5,
        precision=2,
    )
    corner_relief_mm: FloatProperty(
        name="Corner Relief (mm)",
        description=(
            "Largest distance a mitered corner may run from its mesh vertex before it is "
            "cut back, so acute corners end blunt instead of in a fragile spike. Hidden "
            "overlaps between panels at a vertex are trimmed exactly regardless"
        ),
        default=2.4,
        min=0.3,
        soft_max=20.0,
    )
    min_dihedral_deg: FloatProperty(
        name="Sharpest Miter (deg)",
        description="Dihedral angles below this are treated as this value so razor edges cannot produce runaway miters",
        default=25.0,
        min=5.0,
        max=90.0,
    )

    # ------------------------------------------------------------------- labels
    panel_label_size_mm: FloatProperty(name="Panel Label Height (mm)", default=7.0, min=3.0, soft_max=16.0)
    edge_label_size_mm: FloatProperty(name="Joint Label Height (mm)", default=4.5, min=3.0, soft_max=10.0)
    label_relief_mm: FloatProperty(
        name="Label Relief (mm)",
        description="Raised letter height above the interior skin; 0.6 mm gives three layers at 0.2 mm layer height",
        default=0.6,
        min=0.3,
        soft_max=1.5,
    )
    label_font_path: StringProperty(
        name="Bold Font File",
        description="Optional TTF or OTF file; leave blank to use Arial Bold or a platform bold-sans fallback",
        subtype="FILE_PATH",
        default="",
    )
    resolved_label_font: StringProperty(default="", options={"HIDDEN"})
    label_defaults_version: IntProperty(default=0, options={"HIDDEN"})
    mesh_thickness_limit_mm: FloatProperty(default=0.0, options={"HIDDEN"})
    # The limit as a fraction of the sculpture size, so rescaling the sculpture
    # rescales the limit instead of reusing a number measured at another size.
    mesh_thickness_limit_ratio: FloatProperty(default=0.0, options={"HIDDEN"})

    # ---------------------------------------------------------------- print bed
    bed_x_mm: FloatProperty(name="Bed X (mm)", default=256.0, min=20.0)
    bed_y_mm: FloatProperty(name="Bed Y (mm)", default=256.0, min=20.0)
    bed_margin_mm: FloatProperty(name="Safety Margin (mm)", default=3.0, min=0.0)
    part_gap_mm: FloatProperty(name="Part Gap (mm)", default=2.0, min=0.0)
    auto_split_oversize: BoolProperty(
        name="Auto-Split Oversize Panels",
        description="Non-destructively add panel seams until every piece fits the usable print bed",
        default=True,
    )

    # ------------------------------------------------------------------- output
    output_directory: StringProperty(name="Output Folder", subtype="DIR_PATH", default="//Polyshell_Output/")
    stl_prefix: StringProperty(name="STL Prefix", default="polyshell")
    pdf_filename: StringProperty(name="PDF Name", default="polyshell_assembly.pdf")
    exploded_distance_percent: FloatProperty(name="Explode Distance", default=8.0, min=0.0, max=40.0, subtype="PERCENTAGE")

    show_advanced: BoolProperty(name="Show Advanced Settings", default=False)

    # ---------------------------------------------------------- edge overrides
    override_tab_count: EnumProperty(
        name="Key Count",
        items=(
            ("-1", "Auto", "Use edge-length thresholds"),
            ("0", "0", "Disable keys"),
            ("1", "1", "One key"),
            ("2", "2", "Two keys"),
            ("3", "3", "Three keys"),
        ),
        default="-1",
    )
    override_flip: BoolProperty(name="Flip Male / Female", default=False)
    override_disabled: BoolProperty(name="Disable Edge", default=False)
    override_offset_1: FloatProperty(name="Key 1 Slide", default=0.0, min=-0.40, max=0.40, subtype="FACTOR")
    override_offset_2: FloatProperty(name="Key 2 Slide", default=0.0, min=-0.40, max=0.40, subtype="FACTOR")
    override_offset_3: FloatProperty(name="Key 3 Slide", default=0.0, min=-0.40, max=0.40, subtype="FACTOR")

    # -------------------------------------------------------------------- state
    last_source_name: StringProperty(default="", options={"HIDDEN"})
    status: StringProperty(name="Status", default="Select a watertight low-poly mesh, then validate.")
    panel_count: IntProperty(default=0, options={"HIDDEN"})
    plate_count: IntProperty(default=0, options={"HIDDEN"})
    spline_count: IntProperty(default=0, options={"HIDDEN"})
    skipped_joint_count: IntProperty(default=0, options={"HIDDEN"})
