# SPDX-FileCopyrightText: 2026 Christian Faur
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import traceback

import bmesh
import bpy
from bpy.types import Operator

from . import exporters, geometry, report


_EDGE_LAYERS = {
    "count": ("int", "psb_tab_count"),
    "flip": ("int", "psb_flip"),
    "disabled": ("int", "psb_disabled"),
    "offset_1": ("float", "psb_offset_1"),
    "offset_2": ("float", "psb_offset_2"),
    "offset_3": ("float", "psb_offset_3"),
}
# Problems a user can act on. Anything else is a bug and gets a traceback in
# the report; packing errors arrive here already converted to PolyshellError.
_EXPECTED_ERRORS = (geometry.PolyshellError, OSError)


def _set_status(settings, message: str) -> None:
    settings.status = message


def _run(operator: Operator, context, work) -> set[str]:
    """Run an operator body, turning every failure into a readable message.

    Expected problems (a bad mesh, an unwritable folder) are reported as they
    are. Anything else is a bug: the user gets a one-line explanation and the
    full traceback goes to the report and the system console, rather than a raw
    Python traceback in a popup.
    """
    settings = context.scene.polyshell_builder
    window_manager = context.window_manager
    try:
        return work(settings)
    except _EXPECTED_ERRORS as exc:
        message = str(exc)
        _set_status(settings, message)
        operator.report({"ERROR"}, message)
        return {"CANCELLED"}
    except Exception as exc:  # noqa: BLE001 - last line of defence for users
        detail = traceback.format_exc()
        print(detail)
        report.write(settings, "Polyshell Builder internal error", [f"{type(exc).__name__}: {exc}", "", detail])
        message = (
            f"Polyshell Builder hit an internal error ({type(exc).__name__}). Details are in the "
            f"'{report.TEXT_NAME}' text block; please include them in a bug report."
        )
        _set_status(settings, message)
        operator.report({"ERROR"}, message)
        return {"CANCELLED"}
    finally:
        try:
            window_manager.progress_end()
        except (AttributeError, RuntimeError):
            pass
        workspace = getattr(context, "workspace", None)
        if workspace is not None:
            workspace.status_text_set(None)


def _progress(context, low: float, high: float):
    """Callback mapping a 0..1 stage fraction onto the cursor progress and status bar."""
    window_manager = context.window_manager
    workspace = getattr(context, "workspace", None)

    def update(fraction: float, message: str) -> None:
        value = low + (high - low) * max(0.0, min(1.0, fraction))
        try:
            window_manager.progress_update(int(value * 100))
        except (AttributeError, RuntimeError):
            pass
        if workspace is not None:
            workspace.status_text_set(f"Polyshell Builder: {message} ({int(value * 100)}%)")

    return update


def _begin(context) -> None:
    try:
        context.window_manager.progress_begin(0, 100)
    except (AttributeError, RuntimeError):
        pass


def _prepare_model(context, settings):
    model = geometry.validate_source(context, settings)
    original_oversize = geometry.oversized_faces(model, settings)
    added_panels = 0
    if original_oversize and settings.auto_split_oversize:
        model, added_panels = geometry.auto_split_oversized_model(model, settings)
    remaining_oversize = geometry.oversized_faces(model, settings)
    return model, original_oversize, remaining_oversize, added_panels


def _active_edit_edge(context):
    obj = context.active_object
    if obj is None or obj.type != "MESH" or obj.mode != "EDIT":
        raise geometry.PolyshellError("Select one or more source edges in Edit Mode.")
    if obj.get("psb_kind"):
        raise geometry.PolyshellError("Edge overrides belong on the original source mesh, not a generated part.")
    bm = bmesh.from_edit_mesh(obj.data)
    active = bm.select_history.active
    if not isinstance(active, bmesh.types.BMEdge) or not active.select:
        active = next((edge for edge in bm.edges if edge.select), None)
    if active is None:
        raise geometry.PolyshellError("Select at least one source edge.")
    return obj, bm, active


def _layer(bm, kind: str, name: str, create: bool, default=0):
    """Edge layer by name.

    Without `create`, a layer saved under the add-on's earlier name (Panel
    Forge) stands in for a missing one. A new layer takes that layer's values,
    or `default` on every edge when there is none.
    """
    collection = bm.edges.layers.int if kind == "int" else bm.edges.layers.float
    layer = collection.get(name)
    if layer is not None:
        return layer
    legacy_name = geometry.legacy_attribute_name(name)
    if not create:
        return collection.get(legacy_name)
    collection.new(name)
    # Adding a layer can move the others, so look both up again.
    layer = collection.get(name)
    legacy = collection.get(legacy_name)
    for edge in bm.edges:
        edge[layer] = edge[legacy] if legacy is not None else default
    return layer


def _override_layers(bm):
    for key, (kind, name) in _EDGE_LAYERS.items():
        _layer(bm, kind, name, True, -1 if key == "count" else 0)
    ints = bm.edges.layers.int
    fresh = ints.get(geometry.OVERRIDE_FLAG) is None
    if fresh:
        ints.new(geometry.OVERRIDE_FLAG)
    layers = {key: _layer(bm, kind, name, False) for key, (kind, name) in _EDGE_LAYERS.items()}
    flag = ints.get(geometry.OVERRIDE_FLAG)
    if fresh:
        legacy_flag = ints.get(geometry.legacy_attribute_name(geometry.OVERRIDE_FLAG))
        for edge in bm.edges:
            if legacy_flag is not None:
                edge[flag] = edge[legacy_flag]
                continue
            # Scenes from before overrides were flagged: mark every edge that
            # already carries a real override so it keeps working.
            touched = (
                edge[layers["count"]] > 0
                or edge[layers["flip"]]
                or edge[layers["disabled"]]
                or any(abs(edge[layers[key]]) > 1e-9 for key in ("offset_1", "offset_2", "offset_3"))
            )
            edge[flag] = int(bool(touched))
    return layers, flag


def _validation_summary(model, original_oversize, oversize, added_panels) -> tuple[str, str]:
    if oversize:
        return (
            f"{len(oversize)} faces are too large for the usable bed once their keys are added; they are "
            "highlighted red in PSB_Reference.\n"
            "Enable Auto-Split Oversize Panels, reduce the Maximum Dimension, or split them by hand.",
            "WARNING",
        )
    lines = []
    if original_oversize and added_panels:
        lines.append(
            f"Auto-split {len(original_oversize)} oversize faces into {len(model.faces)} printable panels "
            f"(+{added_panels}); the source mesh is unchanged."
        )
    else:
        lines.append(f"Validated {len(model.faces)} panels. Every face fits the bed.")
    inward = geometry.inward_edge_count(model)
    lines.append(
        f"{inward} of {len(model.adjacency)} edges fold inward and will need splines."
        if inward
        else f"All {len(model.adjacency)} edges can take printed keys."
    )
    steep = geometry.steep_miter_edges(model)
    if steep:
        lines.append(
            f"{len(steep)} edges fold past 270 deg; their glue faces will overhang. "
            "Soften those folds in the source mesh."
        )
    lines.extend(model.warnings)
    return "\n".join(lines), "INFO"


class PSB_OT_validate(Operator):
    bl_idname = "polyshell_builder.validate"
    bl_label = "Validate & Preview"
    bl_description = "Scale a non-destructive preview, auto-tune thickness for this mesh, and flag faces that cannot fit the bed"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        def work(settings):
            model, original_oversize, oversize, added_panels = _prepare_model(context, settings)
            reference = geometry.create_reference(model, oversize)
            settings.last_source_name = model.source.name
            settings.panel_count = len(model.faces)
            settings.plate_count = 0
            settings.spline_count = 0
            settings.skipped_joint_count = 0
            summary, level = _validation_summary(model, original_oversize, oversize, added_panels)
            if oversize:
                reference.hide_set(False)
            _set_status(settings, summary)
            self.report({level}, summary.split("\n")[0])
            geometry.schedule_outliner_collapse()
            return {"FINISHED"}

        return _run(self, context, work)


def _generate(context, settings, low: float, high: float):
    model, original_oversize, oversize, added_panels = _prepare_model(context, settings)
    reference = geometry.create_reference(model, oversize)
    if oversize:
        reference.hide_set(False)
        raise geometry.PolyshellError(
            f"Stopped: {len(oversize)} faces do not fit the usable print bed with their keys "
            "(highlighted red in PSB_Reference). "
            "Enable Auto-Split Oversize Panels or reduce the Maximum Dimension."
        )
    panels, splines, notes, stats = geometry.generate_panels(model, settings, _progress(context, low, high))
    reference.hide_set(True)
    settings.last_source_name = model.source.name
    settings.panel_count = len(panels)
    settings.plate_count = 0
    settings.spline_count = len(splines)
    settings.skipped_joint_count = len(stats["bare_edges"])
    head = []
    if original_oversize and added_panels:
        head.append(f"Auto-split {len(original_oversize)} oversize faces into {added_panels} additional panels.")
    head.extend(model.warnings)
    return panels, splines, head + notes, stats


class PSB_OT_generate(Operator):
    bl_idname = "polyshell_builder.generate"
    bl_label = "Generate Printable Panels"
    bl_description = "Create mitered shells, glue-alignment keys, cradles, splines, and interior labels"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        def work(settings):
            _begin(context)
            panels, splines, notes, stats = _generate(context, settings, 0.0, 1.0)
            summary = f"Generated {len(panels)} panels and {len(splines)} splines."
            report.write(settings, summary, notes, stats)
            _set_status(settings, "\n".join([summary] + notes))
            if bpy.ops.object.select_all.poll():
                bpy.ops.object.select_all(action="DESELECT")
            if panels:
                panels[0].select_set(True)
                context.view_layer.objects.active = panels[0]
            self.report({"INFO"}, summary)
            geometry.schedule_outliner_collapse()
            return {"FINISHED"}

        return _run(self, context, work)


class PSB_OT_pack(Operator):
    bl_idname = "polyshell_builder.pack"
    bl_label = "Nest onto Print Plates"
    bl_description = "Nest panels onto print beds using geometry-derived rotations; splines go on their own plates"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        def work(settings):
            _begin(context)
            plates = exporters.create_print_plates(settings, _progress(context, 0.0, 1.0))
            settings.plate_count = len(plates)
            panels = len(geometry.get_panel_objects())
            splines = len(geometry.get_spline_objects())
            message = f"Nested {panels} panels" + (f" and {splines} splines" if splines else "") + f" onto {len(plates)} plates."
            _set_status(settings, message)
            self.report({"INFO"}, message)
            geometry.schedule_outliner_collapse()
            return {"FINISHED"}

        return _run(self, context, work)


class PSB_OT_export_stl(Operator):
    bl_idname = "polyshell_builder.export_stl"
    bl_label = "Export STL Plates"
    bl_description = "Write one millimeter-scale STL per print plate, replacing plates left over from earlier exports"

    def execute(self, context):
        def work(settings):
            paths, removed = exporters.export_stl_plates(settings)
            message = f"Exported {len(paths)} STL plates to:\n{os.path.dirname(paths[0]) if paths else ''}"
            if removed:
                message += f"\nRemoved {removed} plate files left over from an earlier, larger export."
            _set_status(settings, message)
            self.report({"INFO"}, f"Exported {len(paths)} STL files.")
            return {"FINISHED"}

        return _run(self, context, work)


class PSB_OT_export_pdf(Operator):
    bl_idname = "polyshell_builder.export_pdf"
    bl_label = "Export PDF Guide"
    bl_description = "Write the exploded view, unfolded assembly map sheets, and spline index"

    def execute(self, context):
        def work(settings):
            path = exporters.export_assembly_pdf(settings)
            _set_status(settings, f"Exported assembly PDF:\n{path}")
            self.report({"INFO"}, "Exported the assembly PDF.")
            return {"FINISHED"}

        return _run(self, context, work)


class PSB_OT_build_all(Operator):
    bl_idname = "polyshell_builder.build_all"
    bl_label = "Build & Export Everything"
    bl_description = "Validate, generate, nest, and export STL plates, the PDF guide, and a build report"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        def work(settings):
            _begin(context)
            panels, splines, notes, stats = _generate(context, settings, 0.0, 0.55)
            plates = exporters.create_print_plates(settings, _progress(context, 0.55, 0.95))
            stls, removed = exporters.export_stl_plates(settings)
            pdf = exporters.export_assembly_pdf(settings)
            settings.plate_count = len(plates)
            summary = (
                f"Complete: {len(panels)} panels, {len(splines)} splines, "
                f"{len(plates)} plates, {len(stls)} STL files."
            )
            extra = [f"Removed {removed} plate files left over from an earlier, larger export."] if removed else []
            text = report.write(settings, summary, notes + extra, stats)
            report.save_to_folder(settings, text)
            _set_status(settings, "\n".join([summary] + notes + extra + [f"Output: {os.path.dirname(pdf)}"]))
            self.report({"INFO"}, "Polyshell Builder build and export complete.")
            geometry.schedule_outliner_collapse()
            return {"FINISHED"}

        return _run(self, context, work)


class PSB_OT_load_edge_override(Operator):
    bl_idname = "polyshell_builder.load_edge_override"
    bl_label = "Load Edge Override"
    bl_description = "Load override values from the active selected source edge"

    def execute(self, context):
        def work(settings):
            _obj, bm, edge = _active_edit_edge(context)
            values = dict(geometry._DEFAULT_OVERRIDE)
            flag = bm.edges.layers.int.get(geometry.OVERRIDE_FLAG) or bm.edges.layers.int.get(
                geometry.legacy_attribute_name(geometry.OVERRIDE_FLAG)
            )
            overridden = flag is None or bool(edge[flag])
            if overridden:
                for key, (kind, name) in _EDGE_LAYERS.items():
                    layer = _layer(bm, kind, name, False)
                    if layer is not None:
                        values[key] = edge[layer]
                if flag is None and int(values["count"]) == 0:
                    values["count"] = -1
            settings.override_tab_count = str(max(-1, min(3, int(values["count"]))))
            settings.override_flip = bool(values["flip"])
            settings.override_disabled = bool(values["disabled"])
            settings.override_offset_1 = values["offset_1"]
            settings.override_offset_2 = values["offset_2"]
            settings.override_offset_3 = values["offset_3"]
            self.report({"INFO"}, f"Loaded edge {edge.index} " + ("override." if overridden else "(automatic)."))
            return {"FINISHED"}

        return _run(self, context, work)


class PSB_OT_apply_edge_override(Operator):
    bl_idname = "polyshell_builder.apply_edge_override"
    bl_label = "Apply Edge Override"
    bl_description = "Apply these values to every selected source edge"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        def work(settings):
            obj, bm, _edge = _active_edit_edge(context)
            layers, flag = _override_layers(bm)
            selected = [edge for edge in bm.edges if edge.select]
            for edge in selected:
                edge[layers["count"]] = int(settings.override_tab_count)
                edge[layers["flip"]] = int(settings.override_flip)
                edge[layers["disabled"]] = int(settings.override_disabled)
                edge[layers["offset_1"]] = settings.override_offset_1
                edge[layers["offset_2"]] = settings.override_offset_2
                edge[layers["offset_3"]] = settings.override_offset_3
                edge[flag] = 1
            bmesh.update_edit_mesh(obj.data)
            self.report({"INFO"}, f"Applied overrides to {len(selected)} edges.")
            return {"FINISHED"}

        return _run(self, context, work)


class PSB_OT_clear_edge_override(Operator):
    bl_idname = "polyshell_builder.clear_edge_override"
    bl_label = "Clear Edge Override"
    bl_description = "Restore automatic settings on every selected source edge"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        def work(settings):
            obj, bm, _edge = _active_edit_edge(context)
            layers, flag = _override_layers(bm)
            selected = [edge for edge in bm.edges if edge.select]
            for edge in selected:
                edge[layers["count"]] = -1
                edge[layers["flip"]] = 0
                edge[layers["disabled"]] = 0
                edge[layers["offset_1"]] = 0.0
                edge[layers["offset_2"]] = 0.0
                edge[layers["offset_3"]] = 0.0
                edge[flag] = 0
            bmesh.update_edit_mesh(obj.data)
            self.report({"INFO"}, f"Cleared overrides on {len(selected)} edges.")
            return {"FINISHED"}

        return _run(self, context, work)
