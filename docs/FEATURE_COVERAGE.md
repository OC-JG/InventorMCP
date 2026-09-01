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

**Covered today (17):** Extrude, Revolve, Sweep, Loft, Hole, Fillet, Chamfer,
Shell, RectangularPattern, CircularPattern, Mirror, Thread, Emboss, FaceDraft,
Combine, Split, plus work planes and material, which are not `Features`
collections. `boss` exists as an operation but is built from primitives -- see
below.

That is 14 of 53 by count, but the count flatters the gap in one direction and
overstates it in the other: the covered ones are the high-frequency core of solid
modelling, and a good half of what is missing is surfacing and repair work that a
text-to-part server has no business doing.

## Priority for what to add next

Ranked by how often the feature is reached for on the kind of part this server
already builds -- moulded enclosures and machined brackets -- not by how often it
appears across all of Inventor.

### Tier 1 -- done, except one

1. **FaceDraft.** *Added.* `{"op":"draft","faces":{...},"plane":"xy","angle":"2 deg"}`.
   Inventor builds this from a definition object rather than arguments:
   `CreateFaceDraftDefinition()`, then `SetFixedPlane(faces, plane, angle)`, then
   `Add`. This closes the gap where the DFM subsystem could measure draft and
   report it as a finding but nothing could add it.

2. **Rib.** **Not added -- Inventor refuses it and I could not find the shape it
   wants.** `RibFeatures.CreateDefinition(curves, isRib, reversed, thickness)`
   succeeds and returns a `RibDefinition`; `RibFeatures.Add(definition)` then fails
   with `E_INVALIDARG` for every combination tried. What was ruled out:

   * `ProfileCurves` is an `ObjectCollection` of sketch curves, not a `Profile` --
     passing a `Profile` gives a type mismatch, passing the collection does not, so
     the type is right.
   * Profile geometry: a line above the part, a line crossing it, a vertical line,
     and a diagonal touching it. Verified in 3D that the line really was where it
     was meant to be (Z=2 above a plate spanning Z=0..0.6), so this is not a
     sketch-plane axis mix-up.
   * `DirectionReversed` both ways; `ExtendProfile` on; `SetToNextExtent()`,
     `SetFiniteExtent(...)`, and leaving the default (`kToNextRibExtent`) alone.
   * `Thickness` as a number and as an expression string.
   * `AffectedBody`, which defaults to `None` and looked like the answer, set to the
     part's only body.

   Fourteen-plus combinations, all `E_INVALIDARG`. Whatever the missing piece is,
   it is not in the parameter list or the obvious definition properties. Next thing
   to try is recording the Inventor UI creating a rib and reading back what the
   resulting `RibDefinition` differs in.

3. **Boss.** *Added as a composite, because it cannot be added any other way.*
   `BossFeatures` is a read-only collection on this build -- it exposes only
   `Type`, `Application`, `Item`, `Count` and `_NewEnum`, with no `Add` and no
   `Create`. So `{"op":"boss",...}` expands, in the recipe, into a sketch of
   circles, a join extrude and a hole. It builds identical geometry and stays
   parametric; it simply is not a Boss feature in the browser and cannot be edited
   as one.

4. **Combine and Split.** *Added.* `combine` needs a second body, which comes from
   an extrude with `operation: "new_body"` -- note `kNewBodyOperation` is 20485.
   `split` maps a style onto Inventor's separate calls: `trim` to `SplitPart`,
   `split` to `SplitBody`, `faces` to `SplitFaces`.

### Tier 2 -- frequently wanted, no current workaround

Rib from tier 1 belongs here too until the `Add` refusal is understood.

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
