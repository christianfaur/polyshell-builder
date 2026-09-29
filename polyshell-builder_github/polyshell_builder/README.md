# Polyshell Builder 1.0.0

Polyshell Builder (formerly Panel Forge) turns a closed low-poly sculpture mesh into
flat, individually printable panels that glue back together at exactly the right
angles. Each panel is a mitered shell with printed alignment keys, labelled on its
hidden face, nested onto print beds, and exported as binary STL plates with a PDF
assembly guide.

## Compatibility

- Tested with Blender 5.2 LTS and Blender 4.5 LTS.
- STL files are written in millimetres; import them into a slicer at 100% scale.
- One sculpture per .blend file: generating from a second object replaces the first
  object's generated panels, plates, and splines.

## Install

1. Open **Edit > Preferences > Extensions**.
2. From the drop-down menu at the top right, choose **Install from Disk...** and select
   `polyshell_builder-1.0.0.zip`.
3. Open the 3D View sidebar (**N**) and choose the **Polyshell** tab.

## What the source mesh needs

- **Closed and manifold**: every edge is shared by exactly two faces. Validation names
  the problem and how to find it (Select > Select All by Trait > Non Manifold).
- **Consistent winding**: all faces wound the same way. Inside-out meshes are detected
  and handled; mixed winding is reported (fix with Mesh > Normals > Recalculate Outside).
- **Flat, convex faces**: with **Auto-Triangulate Bent Faces** on (the default), faces
  that are bent or concave are split into triangles for the build. The source mesh is
  never changed.
- **Object transforms are respected**; unapplied rotation and scale are fine. Modifiers
  are not evaluated, so apply any modifier whose result you want built.
- A few dozen to a few hundred faces is the intended range. Each face becomes one panel
  (or several, if it is larger than the print bed).

## Workflow

1. Select the sculpture in Object Mode.
2. **1. Sculpture**: set the finished **Maximum Dimension** and a thickness preset, then
   **Validate & Preview**. Validation measures how thick this particular mesh can be
   built before sharp folds consume small panels, and holds the skin to that.
3. **2. Printer**: nozzle, layer height, and joint fit. With **Auto-Tune From Printer**
   on, every clearance, key, label, and relief size is derived from these (the derived
   values are shown greyed under **Show Advanced Settings**).
4. **3. Print Bed**: bed size, safety margin, and part gap. **Auto-Split Oversize
   Panels** adds seams to any face too large for the bed.
5. **Build & Export Everything** runs every step and writes all outputs. The steps can
   also be run one at a time: **Generate Printable Panels**, **Nest onto Print Plates**,
   **STL Plates**, **PDF Guide**.
6. Read the build report (below) before printing.

## Outputs

Everything is written to the **Output Folder** (default `//Polyshell_Output/`, next to
the .blend file; if the .blend has never been saved, `Polyshell_Output` in your home
folder).

| file | contents |
| --- | --- |
| `polyshell_plate_001.stl` ... | one binary STL per print bed, exterior faces down, interior features up; plates left over from an earlier, larger export are deleted |
| `polyshell_assembly.pdf` | exploded view, unfolded assembly map, spline index |
| `polyshell_report.txt` | build report; the same text is kept in the .blend as the Text Editor block **Polyshell Report** |

The **build report** lists the settings used, alignment coverage, and every edge left
without an alignment feature, every panel made thinner than the set thickness, every
panel trimmed at a crowded corner, and every label that did not fit its panel.

## Thickness is the main lever on accuracy

Two glued panels hold their angle through their shared mitered face, whose width is
`thickness / sin(dihedral / 2)`. Thickness therefore sets angular accuracy almost by
itself. The **Rigid (1/4 in)** preset uses 6.35 mm and gives a glue face about 2.6x wider
than a 2.4 mm skin, at the cost of print time and filament.

## Shell construction

Each panel is built as three stacked outlines:

| height | outline |
| --- | --- |
| `0` | printed outer face |
| `edge relief` | top of a short square rim; the miter starts here |
| `thickness` | interior face, corners cut back by the corner relief |

- **Outer edge relief** keeps the first layers of every exterior edge square, so the
  miter does not taper to a feather thinner than one extrusion width. The rim is set
  back by exactly the miter at its own height, so it never stands past the glue plane;
  a panel with a very sharp fold gets a lower rim rather than a wide groove.
- **Corner relief** bounds how far a mitered corner may run from its mesh vertex, so
  acute corners end blunt instead of in a fragile spike.
- The interior outline is the intersection of half-planes: one per mitered edge, one per
  corner relief, and one for each face that meets the panel only at a vertex.
- Where the sculpture is thinner than two skins (an ear, a tail), each panel is made no
  deeper than half the distance to the wall opposite it, so the two sides cannot collide.
  Those panels are listed in the report as thinned.
- Where more than three panels meet at a vertex, their hidden inner corners can still
  occupy the same space. Polyshell Builder finds every overlapping pair and removes the shared
  volume from one of them with an exact boolean, which takes out hidden interior
  material only. The exceptions are folds sharper than the Sharpest Miter setting (25
  degrees by default), whose miter is capped, and saddle-shaped corners: there a trim can
  blunt the visible edge slightly, and the report lists where.
- A panel whose interior would be consumed by the miters of a ring of sharp folds is
  made shallower on its own. Its miter plane does not depend on depth, so it still meets
  its neighbours exactly; the glue face just gets narrower.

## Alignment

Every shared edge gets one of two treatments, chosen automatically.

**Printed keys**: a male key on one panel crosses the inner corner and seats in an open
three-sided cradle on its neighbour. Used wherever the key can rise steeply enough to
print without support. Key position and size are retried before an edge is given up.

**Rails and a loose spline**: used where keys cannot work, mainly on inward-folding
edges. Where two panels fold past 180 degrees their interiors diverge, so no key printed
on one panel can reach the other without support material. Instead, guide rails are
printed on the panels and a separate spline bridges the joint. The spline is a flat
profile extruded straight up, so it prints without support at any angle and its fixed
bend sets the dihedral directly: a solid wedge up to 90 degrees, an open V-bar beyond.

Every key, cradle, rail, spline, and raised label is checked against every other panel
and part in its assembled position, with a small clearance, before it is kept. A feature
that would collide is moved, shrunk, or dropped, never printed.

A spline is identified by a short **type code**, `S1`...`S9`, then `SA`...`SZ`, stamped
on the part and printed beside the matching edge on both panels. Codes are assigned in
order of bend angle; splines sharing a code are the same part, so any one fits any edge
marked with that code. Codes start with a letter and panel IDs never do, so the two
cannot be confused.

Edges folding beyond about 270 degrees are reported at validation: their glue face is
forced to overhang by the dihedral itself, and the fix is to soften the fold in the mesh.

## Labels

- Panel IDs use the sequence `1`-`9`, `A`-`Z`, then `1A`-`1Z`, `2A`, and onward.
- Every edge is labelled with the ID of the panel that joins it; spline edges also carry
  the spline code.
- Labels are raised on the interior face and always read the right way round from
  inside the sculpture. IDs that read differently upside down (such as `6` and `9`) get
  an underline.
- Labels claim their space before and after the joint features, and shrink rather than
  overlap. A label that cannot fit at all is listed in the report; the unfolded map
  always carries every ID.
- Labels use a bold TrueType font. Arial Bold is found automatically where installed;
  any TTF or OTF can be chosen instead.

## Assembly PDF

- **Exploded view**. Up to 60 panels: one see-through view with IDs of panels facing you
  in black and far-side IDs in gray. Larger models: solid views from the front, the
  back, and (when it shows panels the others cannot) the underside, labelling only the
  panels you can actually see. IDs never overprint.
- **Unfolded map** on landscape Tabloid sheets, about 85 panels per sheet. Every cut
  edge carries the ID of the panel it reconnects to; a seam that continues on another
  sheet also names that sheet. Seams that take a spline are drawn in red with the
  spline code.
- **Spline index**: every code with its bend angle, quantity, and the panel pairs it
  joins.

## Selected-edge overrides

Enter Edit Mode on the original source mesh and select one or more edges. The sidebar
can override the automatic key count, swap which panel carries the male key, disable an
edge (for a closure cap left open until last), and slide each of up to three keys.
Click **Apply**, return to Object Mode, and regenerate. **Load** reads an edge's current
values and **Clear** returns selected edges to automatic.

## Printing notes

- Print every plate as exported: exterior face on the bed, interior features upward.
  Nothing needs support; walls on inward folds lean out at most 60 degrees from vertical.
- Splines are nested on their own plates and print flat as nested.
- Dry-fit several connected panels before gluing, and check the report for edges
  without alignment.

## Known limitations

- Some edges cannot carry any alignment feature: very short edges, crowded corners, and
  folds too sharp for a key or spline. The report lists each one with the reason; glue
  those by eye.
- The exploded views cannot show every panel of a complex sculpture (panels hidden in
  folds or facing sideways). Every panel is on the unfolded map.
- Panels trimmed at crowded corners can keep a few zero-area faces or zero-thickness
  fins inside the sculpture. They hold no material and slicers ignore them, though some
  report them as repaired.
- Large models keep Blender busy for the length of the build; progress is shown in the
  status bar. A 276-panel sculpture takes about half a minute.
- Blender 4.5's boolean is less robust than 5.2's: now and then it cannot trim a small
  overlap that 5.2 trims. The report lists any such pair.

Inspect the generated geometry and the slicer preview before committing to a large
batch.

## Changelog

### 1.0.0

First public release, as Polyshell Builder.


- Renamed from Panel Forge to Polyshell Builder. Uninstall Panel Forge before installing.
  Edge overrides saved by Panel Forge are still read, and its generated collections are
  cleared on the next build.

Correctness:

- **Assembled clearance checking.** Every key, cradle, rail, spline, and label is now
  tested against every other panel and part in its assembled position before it is
  kept. On a 254-panel squirrel, 0.2.4 had 67 features cutting into neighbouring panels
  by up to 3.2 mm and 156 features colliding with each other.
- **Overlapping panels.** Where more than three panels meet, hidden inner corners could
  overlap by over a millimetre, and panels on opposite sides of thin parts reached into
  each other. Panels are now kept to half the local wall thickness, kept behind their
  corner neighbours' faces, and any remaining overlap is removed with an exact boolean.
- **Rims at sharp folds.** On folds sharper than about 67 degrees the square rim stood
  past the glue plane into the neighbouring panel, by up to 1.8 mm at the sharpest fold
  allowed. Such panels now get a lower rim instead.
- **Panels that only just fit the bed.** Keys stick out past the panel edge, so a face
  that fitted the bed could still fail at nesting after a full build. Faces are now
  split with room for their keys.
- **Watertight panels on every test shape.** Fixed shells with open edges on split
  cylinder and cone caps, inside-out key prisms, and zero-area faces on non-planar
  quads. The cause was a clipping plane computed from two nearly coplanar faces, which
  is pure rounding noise and could cut a panel anywhere.
- Guide rails for splines on outward folds are printed on one panel only; rails on both
  panels collided inside the V.
- Splines for folds of 90 degrees or less are solid wedges; the open V profile could cut
  into the panel it rests on.
- Keys and cradles on joints sharper than 90 degrees use a wedge profile that stays
  inside the gap between the panels.
- Panel IDs are no longer crowded out by joint features on small panels.
- Edge overrides now apply only to the edges they were set on. Previously the first
  override set every other edge in the mesh to zero keys.
- Thickness is re-derived from the preset whenever the mesh or size changes, instead of
  staying at the lowest value any earlier mesh needed.

Robustness:

- **Auto-Triangulate Bent Faces** (on by default) handles faces that are not flat or
  not convex, and faces with a vertex in the middle of an edge.
- Auto-split handles large many-sided faces such as cylinder caps, which used to stop
  with "triangulate manually".
- Raised letters are always closed, clean solids: the "Q" of Blender's built-in font
  (used where Arial is not installed) came out open, and Blender 4.5 left zero-area
  faces in "4" and "T".
- STL plates are written directly as binary STL, independent of Blender's STL exporter,
  selection, and mode. Plates left over from an earlier, larger export are deleted.
- An unsaved .blend writes to `Polyshell_Output` in the home folder instead of an
  unwritable location; unwritable folders are reported.
- Every failure is reported in the sidebar in plain words; unexpected internal errors
  are written to the build report with details for a bug report.
- Much faster on large models. Nesting tests every candidate position at once instead of
  one at a time, and collision checks only visit nearby parts. A 276-panel squirrel
  builds and exports in 28 s instead of 214 s, and a 320-panel sphere in 87 s instead of
  26 minutes, with identical plate layouts.
- Loose vertices no longer distort the sculpture's size.

Usability:

- Build report as a text file and a Text Editor block.
- Exploded views that never overprint IDs, with front, back, and underside views for
  large models.
- Map sheets name the sheet where a cut seam continues.
- Derived settings are shown greyed while Auto-Tune owns them.
- Default corner relief scales with thickness (60%, 1.2-4 mm).

### 0.2.4

- The spline type code moved to the spline's outward face; on an end face it held the
  spline 0.42 mm off its guide rails.

### 0.2.3

- Splines are identified by two-character type codes (`S1`, `SA`, ...) in order of bend
  angle.

### 0.2.2

- Nesting about ten times faster; unfolded map paginated at about 85 panels per sheet;
  spline seams in red on the map; panel labels no longer overlap.

### 0.2.1

- Robustness pass on a rough 148-panel sculpture: shallower panels where sharp folds
  consume the interior, a 60-degree wall-lean limit, and watertight panels throughout.

### 0.2.0

- Edge labels no longer mirrored; half-plane corner construction; rail-and-spline
  joints for inward folds; printer-driven auto-tuning and thickness presets.

### 0.1.2 - 0.1.5

- TrueType labels, compact panel IDs, automatic splitting of oversize faces, nesting
  largest-first, translucent exploded view and Tabloid unfolded map.
