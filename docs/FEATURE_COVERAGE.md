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

**Covered today, 17 of the 53 collections:** Extrude, Revolve, Sweep, Loft,
Coil, Hole, Fillet, Chamfer, Shell, RectangularPattern, CircularPattern, Mirror,
Thread, Emboss, FaceDraft, Combine, Split. Work planes and material are covered
too and are not `Features` collections, so they sit outside the count. `boss` and
`rib` exist as recipe operations but are built from primitives, because neither
Inventor feature can be created through the API -- see below.

Seventeen of fifty-three flatters the gap in one direction and overstates it in
the other: the covered ones are the high-frequency core of solid
modelling, and a good half of what is missing is surfacing and repair work that a
text-to-part server has no business doing.

## Priority for what to add next

Ranked by how often the feature is reached for on the kind of part this server
already builds -- moulded enclosures and machined brackets -- not by how often it
appears across all of Inventor.

### Tier 1 -- done, two of them by hand

1. **FaceDraft.** *Added.* `{"op":"draft","faces":{...},"plane":"xy","angle":"2 deg"}`.
   Inventor builds this from a definition object rather than arguments:
   `CreateFaceDraftDefinition()`, then `SetFixedPlane(faces, plane, angle)`, then
   `Add`. This closes the gap where the DFM subsystem could measure draft and
   report it as a finding but nothing could add it.

2. **Rib.** *Added as a composite, because it cannot be added any other way.* `RibFeatures.CreateDefinition(curves, isRib, reversed, thickness)`
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

   So `{"op":"rib",...}` is built by hand instead: the rib's silhouette -- its top
   edge from `start` to `end`, dropped to `root` -- as a closed profile, extruded
   symmetrically about its plane by `thickness`. Exact against Inventor: 20.88000
   cm^3 for a 60 x 14 mm silhouette 2 mm thick, and 20.40000 for the same with the
   top falling from 20 to 12.

   **It has no draft, deliberately.** A moulded rib should thin as it rises, and a
   single planar silhouette pushed through a linear extrude cannot express that. An
   extrude's `taper` drafts across the *thickness* instead, which measurably *added*
   0.00154 cm^3 rather than releasing the rib -- so the knob was removed rather than
   left to mislead. Narrow the silhouette if the effect is needed, or wait for the
   real Rib feature.

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

### Tier 1b -- done since, all five verified against live Inventor

Volumes below are Inventor's, checked against hand arithmetic, and each recipe
also passes the simulator rehearsal.

10. **A closed profile that mixes arcs with lines.** Separate `line` and `arc`
    entities touched only by coordinate, so Inventor saw loose curves, offered no
    profile, and the extrude failed with a bare "Exception occurred." Endpoints
    within 1e-5 cm are now made coincident, which `shared_point_groups` turns
    into one shared point per corner. A stadium of two lines and two arcs
    extrudes to 1.114159 cm^3, exactly `(20x10 + pi x 25) x 4`.
11. **Variable fillets**, via `radius_end` on `fillet`. `AddSimple` only does a
    constant radius, so this uses `CreateFilletDefinition` +
    `AddVariableRadiusEdgeSet` + `Add`. A 40x20x10 block filleted 3 to 8 on its
    four vertical edges comes out at 7.714333 cm^3.
12. **Multi-body cut targeting**, via `bodies` on `extrude`, which sets
    `ExtrudeDefinition.AffectedBodies`. This closes the defect that an `extrude`
    cut only ever affected the primary body: two 20x20x10 blocks with an 8 bore
    through the second measure 7.497345 cm^3, where before the cut removed
    nothing at all.
13. **Sweep along a path** and **loft with guide rails** needed no work -- both
    were already implemented and are confirmed here: 1.9792 cm^3 for a 6 circle
    swept 70 mm, and 13.6136 cm^3 for a 30-to-10 loft over 40 mm.

### Tier 2 -- frequently wanted, no current workaround

5. **SketchDrivenPattern.** Pattern by sketch points. Rectangular and circular
   patterns cover the regular cases; anything irregular currently has to be
   enumerated by hand.
6. **Thicken.** Turning a surface into a wall. `ThickenFeatures.Add` is public.
   `FaceOffsetFeatures` only exposes `_Add`, and Inventor's leading underscore
   means internal, so plain face offset is not on the table.
7. **MoveFace.** `MoveFaceFeatures` has `Add` and `CreateDefinition`, so this is
   buildable, and it is the only route to changing imported geometry -- the
   server reads STEP for DFM analysis and can then alter nothing, because
   translated geometry has no parameters.

Not tier 2 after all, checked against the type library rather than the docs:
**Lip, SnapFit, Grill, Rest** and **DirectEdit** are all read-only collections
with no `Add` of any kind, so no amount of work here would make them buildable.
They belong to Inventor's UI, not its API.

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
* **`hole` still only drills the primary body.** `extrude` can now be aimed with
  `bodies`, but `HoleFeatures` was not given the same treatment, so a hole in a
  second body still needs an `extrude` cut.

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

   *Easier to see now.* The simulator's ledger counts material in pieces, so it
   predicts both walls where Inventor drills one. The divergence check reports
   that as a disagreement instead of the two errors cancelling into a plausible
   number.

2. ~~**The simulator's `shell` does not update `document.slabs`.**~~ *Fixed.* The
   slab list is now a signed ledger: a shell records the cavity it hollowed out,
   a cut records the prism it swept, a hole records its bore, and the list is read
   in creation order so material put back into a void counts again. A cut is
   charged the material it meets rather than its whole swept shape -- the
   enclosure's cable entry costs 0.36 cm^3 against the 5.04 it used to, which is
   what the hand calculation in `examples/expected/enclosure_base.json` says it
   should be. The same feature also rounds a filleted prism's recorded outline,
   because a shell measures that outline and square corners made the cavity
   10.5 mm^2 too big. The part went from 10.7% below the hand-derived volume to
   0.002% above it, the remainder being the polygon that stands in for an arc.
   `tests/test_volume_ledger.py` holds it there.

   *Confirmed live*, Inventor 2027.1 on 2026-09-03: the enclosure measured
   within 0.0005 cm^3 of the hand figure, and the moulded housing -- the other
   shelled part, and the one nobody had derived -- went from 15.96% below
   Inventor to 0.63%. The simulator is now within 1% of a live build on all
   eleven shipped examples; `examples/expected/README.md` has the table.

3. **`save_part` fails with a bare "Exception occurred" when that path is already
   open** in Inventor from an earlier build. Since each rebuild leaves another
   document open, saving over the same path fails on the second attempt onward and
   says nothing useful. Fix: name the conflict, and offer to close or version.

4. **`capture_view` orientation names do not describe what you get.** On a part
   built on XY and extruded in +Z, `front` and `back` return top and bottom views,
   and `top` returns a side elevation with **Z rendered inverted** -- which reads
   as upside-down text that is not upside down. Anything checking its own work
   from a render can be misled; coordinates must be measured instead.
