# SPDX-FileCopyrightText: 2026 Christian Faur
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Polyshell Builder Blender extension.

The version lives in blender_manifest.toml only.
"""


from . import operators, properties, ui


_CLASSES = (
    properties.PSB_Settings,
    operators.PSB_OT_validate,
    operators.PSB_OT_generate,
    operators.PSB_OT_pack,
    operators.PSB_OT_export_stl,
    operators.PSB_OT_export_pdf,
    operators.PSB_OT_build_all,
    operators.PSB_OT_load_edge_override,
    operators.PSB_OT_apply_edge_override,
    operators.PSB_OT_clear_edge_override,
    ui.PSB_PT_main,
)


def register():
    import bpy

    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.polyshell_builder = bpy.props.PointerProperty(type=properties.PSB_Settings)


def unregister():
    import bpy

    if hasattr(bpy.types.Scene, "polyshell_builder"):
        del bpy.types.Scene.polyshell_builder
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
