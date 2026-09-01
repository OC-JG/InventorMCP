# What Inventor offers, what this server covers, and what to add next

Inventor 2027 exposes **53 feature collections** on `ComponentDefinition.Features`.
The list below is the real one, read out of the installed type library rather than
from memory:

```
AliasFreeform BendPart Boss BoundaryPatch Chamfer CircularPattern Client Coil
Combine CoreCavity Decal DeleteFace DirectEdit Emboss Extend Extrude FaceDraft
FaceOffset Fillet Finish Freeform Grill Hole Knit Lip Loft Mark MidSurface Mirror
MoveFace Move NonParametricBase RectangularPattern Reference ReplaceFace Rest
Revolve Rib RuleFillet RuledSurface Sculpt Shell Simplify SketchDrivenPattern
Slot SnapFit Split Sweep Thicken Thread Trim Unwrap iFeatures
```

**Covered today (14):** Extrude, Revolve, Sweep, Loft, Hole, Fillet, Chamfer,
Shell, RectangularPattern, CircularPattern, Mirror, Thread, Emboss, plus work
planes and material, which are not `Features` collections.

That is 14 of 53 by count, but the count flatters the gap in one direction and
overstates it in the other: the covered ones are the high-frequency core of solid
modelling, and a good half of what is missing is surfacing and repair work that a
text-to-part server has no business doing.

## Priority for what to add next

Ranked by how often the feature is reached for on the kind of part this server
already builds -- moulded enclosures and machined brackets -- not by how often it
appears across all of Inventor.

### Tier 1 -- the coherence gaps

These are the ones where the server can already *measure* or *imply* something it
cannot *build*, which is the worst kind of gap.

1. **FaceDraft.** The DFM subsystem measures draft, reports insufficient draft as
   a finding, and can drive a `draft` parameter -- but there is no way to add a
   draft feature. Today the only draft available is the `taper` on an extrude,
   which means draft has to be designed in from the first operation or not at all.
   This is the single largest inconsistency in the tool.

2. **Rib.** Close to every moulded part has ribs. Building one by hand today means
   a sketch, an extrude, and getting the thickness-to-wall ratio right yourself --
   exactly the ratio the DFM analyser then complains about.

3. **Boss.** Inventor's plastic-part boss places the post, the hole, the fillet and
   optional stiffening ribs as one feature. The PCB enclosure built in this
   session hand-rolled five bosses as circles, a join extrude and a hole; a `boss`
   op would have been one line and would have carried the moulding intent with it.

4. **Combine and Split.** Booleans between solid bodies. Needed for the obvious
   next thing anyone asks for after a tray -- a matching lid -- and for cutting a
   part in half to look inside it. Currently there is no way to make a second body
   interact with the first.

### Tier 2 -- frequently wanted, no current workaround

5. **SketchDrivenPattern.** Pattern by sketch points. Rectangular and circular
   patterns cover the regular cases; anything irregular currently has to be
   enumerated by hand.
6. **Coil.** Springs and helical forms. No approximation exists.
7. **Thicken / FaceOffset.** Offsetting a face or turning a surface into a wall.
8. **MoveFace / DirectEdit.** The server imports STEP for DFM analysis and then
   cannot change anything about it, because imported geometry has no parameters.
   Direct edit is the only route to modifying translated geometry.
9. **Lip, SnapFit, Grill, Rest.** The rest of Inventor's plastic-part set. Narrow,
   but this server's centre of gravity is exactly the parts that use them.

### Tier 3 -- real, but not for this tool yet

RuleFillet, BoundaryPatch, Knit, Sculpt, RuledSurface, MidSurface (surfacing);
CoreCavity (mould tooling); Decal, Unwrap, Mark; iFeatures; Freeform and
AliasFreeform; the repair set (Simplify, DeleteFace, ReplaceFace, Trim, Extend,
NonParametricBase). Assemblies and sheet metal remain out of scope by design.

## Gaps that are not feature collections

Found by using the server rather than by reading its API surface:

* **No work axis or work point.** Only work planes exist. A circular pattern about
  anything other than an origin axis has nowhere to point.
* **No sketch fillet or chamfer.** Corner rounding has to happen as a model
  feature, which is often not where it belongs.
* **No project geometry or sketch offset**, so a sketch cannot reference the edges
  of the solid it sits on.

## Defects worth fixing, with evidence

Each of these was hit while building real parts, and each passed
`validate_recipe` with no findings and no warnings.

1. **A `hole` with `through_all` drills the near wall only.** Across a hollow box
   it stops at the first wall rather than exiting the far side. On the PCB
   enclosure this produced one cable route where two were asked for, and the only
   evidence was a volume 39 mm^3 -- exactly one wall's worth -- above prediction.
   An `extrude` cut with `direction: symmetric` is the working substitute today.
   Fix: support a both-directions extent on holes, or warn when a through hole's
   axis re-enters material it did not cut.

2. **The simulator's `shell` does not update `document.slabs`.** Slabs record the
   prisms an extrude added and are what `_through_all_distance` measures against,
   so after a shell every through-cut is charged against the pre-shell solid. The
   enclosure's cable bore was costed at the full 105 mm box length instead of two
   2 mm walls -- a 26x over-count on that feature. Anything reasoning from a
   rehearsal of a hollow part inherits this.

3. **`save_part` fails with a bare "Exception occurred" when that path is already
   open** in Inventor from an earlier build. Since each rebuild leaves another
   document open, saving over the same path fails on the second attempt onward and
   says nothing useful. Fix: name the conflict, and offer to close or version.

4. **`capture_view` orientation names do not describe what you get.** On a part
   built on XY and extruded in +Z, `front` and `back` return top and bottom views,
   and `top` returns a side elevation with **Z rendered inverted** -- which reads
   as upside-down text that is not upside down. Anything checking its own work
   from a render can be misled; coordinates must be measured instead.
