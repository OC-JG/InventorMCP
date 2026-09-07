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

**Covered today, 20 of the 53 collections:** Extrude, Revolve, Sweep, Loft,
Coil, Hole, Fillet, Chamfer, Shell, RectangularPattern, CircularPattern, Mirror,
Thread, Emboss, FaceDraft, Combine, Split, MoveFace, Thicken,
SketchDrivenPattern. Work planes, work axes, work points and material are
covered too and are not `Features`
collections, so they sit outside the count. `boss` and
`rib` exist as recipe operations but are built from primitives, because neither
Inventor feature can be created through the API -- see below. **MoveFace,
Thicken and SketchDrivenPattern are the three on that list whose COM calls have
never executed** -- all added 2026-09-07, measured in the simulator, unmeasured
live, and kept in the count rather than out of it because the schema offers them
to a caller either way. Tier 1c below says what that means, and for Thicken it
also says which half of Inventor's feature is reachable at all.

Twenty of fifty-three flatters the gap in one direction and overstates it in
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

### Tier 1c -- landed in the simulator, unmeasured against Inventor

7. **MoveFace.** *Added 2026-09-07 as `{"op":"move_face",...}`. Exact in the
   simulator; its COM half has never executed.* Taken ahead of the other two
   Tier 2 items because it is the only one that adds a kind of reach rather than
   a feature: it is the one route to changing imported geometry, and the server
   could read a STEP part for DFM analysis and then alter nothing about it,
   because translated geometry has no sketches and no parameters for anything
   else here to take hold of.

   **The arithmetic is a dot product, not an estimate**, which is why this can
   be a prediction rather than a measurement: a planar face of area A translated
   by v changes the solid by `A*(v.n)`, its own normal doing the projecting. So a
   face pushed along its normal changes the part by area times distance, and one
   slid along its own plane changes nothing, both out of the same expression.
   `examples/calibration/lifted_face.json` and `widened_wall.json` say +6.4000
   and +0.2400 cm^3, each agreeing with the hand derivation to the digit.

   Exact while the moved face keeps its area, which is true of a wall on a prism
   and is what a wall-thickness or clearance move is. It is not true of a face
   bounded by a fillet or a draft, and the `ponytail` on the simulator's
   `move_face` says so along with the other limit: the ledger is not updated, so
   a cut driven through a moved face is charged the thickness the part had
   before the move.

   Only the direction-and-distance move is offered. Inventor's planar drag and
   rotate-about-a-line are deliberately absent: this is the shape a wall
   thickness or a clearance is expressed in, and it is the one whose result the
   rehearsal can predict at all.

   **What is unmeasured here is unusual, and worth naming exactly.** Every other
   COM call in this server was read off a type library before it was written.
   This one was not: what was recorded above is that `MoveFaceFeatures` has
   `Add` and `CreateDefinition`, and *not* what the definition's setter is
   called. So the backend tries spellings, each of which can only mean direction
   and distance, and names every one it tried when none work -- rather than one
   guess that comes back as a bare "Exception occurred". A free-drag or
   point-to-point setter is deliberately not among them: one of those accepting
   a direction and a distance by accident is exactly the quietly wrong part this
   file exists to catch.

   **A live run on 2027.1 on 2026-09-07 said all three are absent**, and the
   type library will not answer the follow-up. `MoveFaceFeatures.Add` takes one
   Definition, `CreateDefinition` produces one, and `MoveFaceDefinition` carries
   a `MoveFaceType` and a `MoveFaceTypeDefinition` whose classes are published
   nowhere -- `--search MoveFaceType` finds nothing at all. The likely shape is
   Inventor's usual one, a definition holding a type and the type holding its own
   parameters, so the same narrow setter list is now tried on the child object
   as well; the type is deliberately *not* set on the way past, because which
   `MoveFaceType` value means direction-and-distance is a semantic claim nothing
   has read and a wrong one could be accepted. The refusal prints what both
   objects offered, so one run answers the question.
   `scripts/probe_definitions.py` asks it directly, and
   `docs/INVENTOR_SETUP.md` has the rest.

6. **Thicken.** *Added 2026-09-07 as `{"op":"thicken",...}`, and it closes half
   of what this item asked for.* The half it closes is the wall-thickness one: a
   layer of material added to or removed from faces, each along **its own**
   normal, which is what makes it a different operation from `move_face` rather
   than a spelling of it. A box's four walls point four ways, so `positive` +
   `join` grows all four outward in one operation where a single named direction
   would push two out and two in. `negative` + `cut` thins them. That is the DFM
   wall remedy, and `dfm/discover.py` has expected a thicken feature's thickness
   to count as evidence of the `wall` role since before one could be built.

   **The half it does not close is the headline one: turning a surface into a
   wall.** Not because of the schema -- because *no operation in this server
   creates a surface*. `extrude`, `loft` and `sweep` all produce solids; there is
   no surface body anywhere, and Inventor's `kSurfaceOperation` is not offered
   for that reason. So the only surface a part here could hold is one that
   arrived through `import_geometry`, and thickening it would work today if it
   did. Inventor's offset mode is absent for the same reason: it *produces* a
   surface, and nothing downstream could take one. `FaceOffsetFeatures` only
   exposes `_Add`, and Inventor's leading underscore means internal, so plain
   face offset was never on the table either.

   **Exact per planar face, first-order on a curved one.** A planar face's area
   times the layer is a prism; a cylinder of radius r thickened by t gains
   `pi*((r+t)^2 - r^2)*h` where `area*t` is `2*pi*r*h*t`, so the missing term is
   `pi*t^2*h` -- second order, and small while the layer is thin next to the
   radius, which is what a wall is. It is charged rather than declined, unlike
   the same face under `move_face`, and the difference is real: there the move's
   direction is arbitrary relative to the face so the dot product has no answer,
   while here the direction *is* the face's own normal.

   **The side and the corner were the unmeasured parts, and both were measured
   on Inventor 2027.1 on 2026-09-07.** `THICKEN_SHARE` in `backend/base.py` says
   which side of a face a `negative` layer lies on, and therefore that
   `positive` + `cut` and `negative` + `join` do nothing at all -- the layer is
   where the material already is not, or already is. That was sound set algebra
   about a boolean against a slab and silent on whether Inventor agreed, so the
   two cancelling pairs are warned about at rehearsal rather than refused: a
   refusal would have prevented the run that settled it.
   `examples/calibration/thinned_wall.json` isolated the side and removed
   **-0.2400 cm^3** against -0.2400 derived, so the table has it right. A
   tolerance could not have caught being wrong about a side, which is what
   defect 5 was; three distinguishable numbers could.

   The corner was the other one, and it went the other way. Four walls grown
   outward leave a 1 x 1 x 6 mm notch at each corner belonging to no wall, so
   the answer was 1.4400 cm^3 if Inventor left them and 1.4640 if it closed
   them. `examples/calibration/thickened_walls.json` came back at **1.4640**:
   Inventor closes them, and the simulator was 1.7% low on every multi-face
   thicken until `_thicken_corners` added `(share x t)^2 x h` per shared edge.
   Both fixtures now agree with Inventor to four decimals and
   `PREDICTED["thicken"]` is 0.02, an extrude's tolerance.

   **The COM call is measured too, and it was not what the code assumed.**
   `ThickenFeatures.Add(Faces, Distance, ExtentDirection, Operation,
   [AutomaticFaceChain], [CreateVerticalSurfaces], [AutomaticBlending])`. There
   is no `CreateThickenDefinition` on this release, so the definition route the
   backend tried first was reaching for a method that has never existed; and
   there is no `IsOffset` argument, so the `False` passed in slot 4 for the
   offset mode's sake was landing on `AutomaticFaceChain` -- where `False` is
   also correct, because chaining would extend the selection past the faces the
   selector named. It worked for a reason that was not the reason given. The
   factor-of-four result guard that existed because a variant and two enum
   *integers* can be misordered without raising is gone with the attempt list:
   `_call_named` puts the argument names at the call site, and a permutation is
   not possible when the names are there.

5. **SketchDrivenPattern.** *Added 2026-09-07 as
   `{"op":"sketch_driven_pattern",...}`. The simulator **places** its
   occurrences, which no other pattern here does; the COM call has never
   executed.*

   **The gap was narrower than this entry used to claim.** It said anything
   irregular "has to be enumerated by hand", and that reads as a bigger absence
   than it was: `hole` already takes a list of points and `boss` a list of
   positions, so an irregular set of holes or bosses needs no pattern at all --
   put the points in one sketch and drill them in one operation. What was
   genuinely missing is patterning a feature whose definition is *not* already a
   list of positions: a pocket, a rib, a filleted detail. That is the real gap
   and it is the smallest of the three items in this tier, which is the opposite
   of what the ordering implied.

   **It is the only pattern that places its occurrences rather than counting
   them**, and the reason is what its input is. `_repeat` charges the seed's
   volume once per extra occurrence and records no prism, which is exact while
   the copies neither overlap nor run off the part -- and the three shipped
   examples that pattern or mirror agree with Inventor to 0.003% on exactly
   that. But a rectangular pattern's count and spacing already say it did
   something, while this one is handed positions and nothing else, so a version
   that counted them could not tell a correct recipe from one whose points all
   miss the part.

   Placement is exact because a translation is: a `_Slab` is an outline in one
   of three origin planes plus a sweep along the normal, so shifting one is
   shifting those. A rotation or a reflection is only representable that way in
   special cases, which is why `circular_pattern` and `mirror` still count --
   and why doing them is a separate change that has to *reproduce* the 0.003%
   rather than improve on it.

   Two things fall out of the placement, and they are what paid for it. A cut
   driven through where an occurrence went is measured against what the pattern
   left rather than what the seed started with -- a 6 mm hole through a copied
   4 mm pocket in a 10 mm plate is charged 6 mm of material, not 10. And an
   occurrence of a *cutting* seed that stands over air is reported, which is a
   recipe whose points are in the wrong place. That second check is deliberately
   narrow: it is only asked where every feature that added material recorded
   prisms for it, because `_material_spans` answers None both for "no material
   here" and for "this part's material was never modelled as prisms", and
   reporting the second as a miss would fire a warning on a correct recipe.

   Slabs now carry the name of the feature that created them. `source` could
   not answer "which prisms are the seed's" -- a part with two extrudes has two
   sets of slabs both saying "extrude" -- and a pattern that copies a seed's
   prisms has to know. An unattributed prism is not copied, which today means a
   shell's cavity, and a shell is not a thing anyone patterns.

   `PREDICTED["sketch_driven_pattern"]` is **0.02 rather than the placeholder**,
   which looks inconsistent beside `move_face` and is not. Its arithmetic is the
   rule the other patterns use and is measured. What is unmeasured is whether
   Inventor also places an occurrence on the reference point -- an off-by-one
   *occurrence*, 33% on a three-point pattern -- and a tight tolerance reports
   that where 0.5 would hide it.

   **The COM call was run on 2027.1 on 2026-09-07 and rejected, on its shape.**
   `SketchDrivenPatternFeatures.Add` takes **one Definition**, not the
   collection, sketch and point this passed through `_patterned`, so the call
   could never have worked here. The backend now builds a definition, sets the
   compute type on it and calls `Add(definition)`. Where the definition comes
   from is not in the type library at all -- `--search SketchDrivenPattern`
   publishes `Add` and nothing else -- so two factory spellings are tried and
   the refusal prints what the live collection offered.
   `scripts/probe_definitions.py` asks it directly, together with `move_face`'s
   unpublished definition objects.

### Tier 2 -- frequently wanted, no current workaround

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

* ~~**No work axis or work point.**~~ *Closed 2026-09-03 in the simulator, and
  unmeasured against Inventor.* `work_point` and `work_axis` are recipe
  operations now, and `work_axis` with `kind: "normal_to_plane"` is the thing a
  bolt circle off the origin needs -- an axis standing perpendicular to the face
  it patterns, stated in that face's own coordinates. The three COM calls it
  rests on (`WorkPoints.AddByPoint`, `WorkAxes.AddByTwoPoints`,
  `WorkAxes.AddByLine`) have never executed; the roadmap keeps that as its own
  open item rather than letting this tick cover it. Note also that the original
  reason recorded here -- that a pattern could only turn about an origin axis --
  was **wrong**: `resolve_axis` has always taken a named sketch line. What is
  true is that no line on a face can be that face's pattern axis, which is
  defect 7 below.
* **No sketch fillet or chamfer.** Corner rounding has to happen as a model
  feature, which is often not where it belongs.
* **No project geometry or sketch offset**, so a sketch cannot reference the edges
  of the solid it sits on.
* **`hole` still only drills the primary body — on Inventor.** *Measured
  2026-09-07 and the answer is no.* The simulator honours `bodies` on a `hole`
  exactly as on an `extrude`, and it was written on 2026-09-03 expecting the COM
  side to follow. It cannot: **Inventor 2027.1's `HoleFeature` has no
  `AffectedBodies` property at all.** Setting it raises `object has no attribute
  'AffectedBodies'`, which is the hard error the backend was already written to
  treat as one — a hole on the wrong body takes real material out of a part that
  looks finished, so failing loudly was the right call and is what happened.

  The reason is structural rather than a version quirk: `extrude` is aimed
  through an `ExtrudeDefinition` *before* the feature exists, and
  `HoleFeatures.Add...` makes the feature in a single call, so there is nothing
  to aim. `rehearse` now warns on `hole` + `bodies`
  (`_KNOWN_BROKEN_FIELDS`) so a caller learns at rehearsal rather than on a CAD
  seat, and names the substitutes: an `extrude` cut carrying `bodies`, which is
  measured and works, or `combine` with operation `cut`.

  The schema keeps the field, because the simulator honours it and a recipe
  written for a later Inventor should still rehearse. If a release ever grows
  the property, the acceptance check stops skipping and the warning should go.

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

   *Warned about since 2026-09-03, and not fixed.* The two fixes offered above
   are not equally available: **Inventor's hole extent has no both-directions
   option** -- Distance, Through All and To, with Through All taking a side --
   so there is nothing to reach for, and the roadmap's first suggestion is not
   implementable rather than merely unimplemented. So the second one is what
   landed. The simulator was already counting the pieces of material each
   drill axis crosses in order to decide which way the hole goes; a count above
   one is the whole of the condition, and `rehearse` now says so, names the
   `extrude` cut with `direction: "symmetric"` as the substitute, and warns that
   the step will diverge on volume too.

   It fires on the reproduction and on none of the eleven shipped examples --
   including the enclosure this defect was found on, which has been built with
   the substitute since. A warning that cries on a correct recipe is worse than
   no warning, so the quiet cases are tested as carefully as the loud one.

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

3. ~~**`save_part` fails with a bare "Exception occurred" when that path is already
   open**~~ in Inventor from an earlier build. Since each rebuild leaves another
   document open, saving over the same path fails on the second attempt onward and
   says nothing useful. Fix: name the conflict, and offer to close or version.

   *Fixed 2026-09-07, and the fix is not a better message.* Inventor's refusal
   is a bare "Exception occurred" with nothing in the ErrorManager, so there is
   nothing to translate -- but the conflict is **knowable before the write**, so
   that is where it is answered. `Backend.refuse_a_path_another_document_holds`
   asks `list_documents` whether any other open document occupies the target
   path, and refuses with the filename, the handle holding it, and both ways out:
   `close_part(document=...)`, or a different name.

   Two things make this better than a tool-layer check would have been.
   **`list_documents` reads Inventor's own `Documents` collection on the COM
   backend**, so it sees a file the *user* opened in the UI as well as one this
   session opened -- and the session's own registry cannot. And **the guard sits
   on `Backend` rather than in either implementation**, so both are held to it
   and no caller routes around it, which is the reasoning `apply_parameter`
   records for the freeze guard: a rule enforced on one path is not a rule.

   Saving in place is not checked, because it cannot collide, and neither is
   saving onto the path the document is already at -- an in-place save written
   longhand. That case is checked *before* the listing rather than left to the
   id comparison, because on COM the ids are exactly what cannot be relied on:
   `document_path`'s own note records that an id-to-id match over that listing
   once matched nothing at all. Paths are compared through `abspath` and
   `normcase`, so two names for one file collide.

   `tests/test_saving.py` holds it, including that the mock and COM backends both
   ask the guard and neither carries a copy of it. The live half is unmeasured,
   as ever: what a COM run has to confirm is that Inventor accepts the save once
   the named document is closed.

   *Amended 2026-09-07, from what the first live run showed about cost.* The
   guard originally asked `list_documents`, and that connection reported **1033
   open documents** behind an assembly. On the COM backend that listing reads
   six properties per document, scans the held handles by COM identity for each,
   **and registers every document it did not recognise** -- so one save would
   have minted a thousand session handles and left the next call comparing a
   million COM identities. The guard now asks `document_at_path`, one narrow
   question each backend answers as cheaply as it can: on COM, one
   `FullFileName` read per document on the miss path and nothing else, with the
   display name and the held-handle scan paid only for an actual match.

   That also surfaced a case the first version could not name. A document open
   because the **user** opened it in Inventor's UI has no session handle, so
   `document_at_path` returns no id and the refusal says "opened outside this
   session" and tells the caller to close it in Inventor -- rather than offering
   `close_part(document=None)`, which would be worse than saying nothing.

4. **`capture_view` orientation names do not describe what you get.** On a part
   built on XY and extruded in +Z, `front` and `back` return top and bottom views,
   and `top` returns a side elevation with **Z rendered inverted** -- which reads
   as upside-down text that is not upside down. Anything checking its own work
   from a render can be misled; coordinates must be measured instead.

5. **A `trim` split threw away the opposite side to the one documented.**
   *Fixed and confirmed live, 2026-09-03: all three fixtures now agree with
   Inventor to four decimal places, on the side and on the amount.* The
   schema says `remove_positive` discards "the side the plane's normal points
   at". Inventor did the reverse: a 27.2 cm^3 part cut at z = 12 with
   `remove_positive: true` should lose the 6.4 cm^3 above the plane, and it lost
   the 20.8 below. Every trimmed part kept the wrong half, and the volume
   reported was correct for the half it kept, so nothing raised.

   Three runs of one part found it and then narrowed it. True and false gave
   exactly complementary results, so the flag reached Inventor and did choose
   the side. The same cut made by the XY origin plane -- whose normal is +Z by
   definition and so cannot have been built backwards -- still kept the wrong
   half, which ruled out the remaining alternative that an offset work plane
   pointed the other way. That put the fault at the call site:
   `SplitFeatures.SplitPart`'s second argument says which side to *keep*, and
   the code passed it which side to remove. It is now inverted there.

   The simulator was wrong too, differently, and is also fixed. Its share of
   the volume came from where the plane fell in the *bounding box*, on the
   assumption that a part is spread evenly either side of a cut -- 11.657 cm^3
   kept where the answer is 20.8. It now clips the ledger's prisms at the plane,
   which is exact for a prismatic part, and takes the trimmed-off half out of
   the ledger so later cuts are not measured against a part that no longer
   exists. Where the ledger cannot answer -- a revolve, a sweep, a loft -- the
   old estimate stands and says so.

   A third thing was wrong on the way past: a work plane's axis was read from
   its *name* rather than from the plane it was built on, so every work plane
   not called xy, xz or yz was treated as horizontal. A part trimmed at a plane
   offset from YZ was cut across Z instead of X.

   `PREDICTED["split"]` is 0.05 now, measured from those three. A trimmed
   revolve, sweep or loft is excluded from the comparison rather than covered by
   it: the ledger has no prisms to clip there, the simulator says outright that
   its number is a fallback, and the rehearsal declines to compare a step that
   says so.

6. **The divergence check could not see a cut that took the right amount off
   the wrong side.** *Fixed 2026-09-03.* Found while fixing defect 5 and worth
   more than it. On `origin_plane_split` the simulator reported 19.4286 cm^3
   removed and Inventor 19.2 -- 1.2% apart, while keeping *opposite halves of
   the part*. Every tolerance in `PREDICTED` passed it, because the comparison
   was of volumes moved and nothing else: it catches a cut that missed and a
   fillet on the wrong edge, and was blind to a mirrored outcome whenever the
   two halves were near enough in size.

   Every operation now records `centre_shift_mm`, where the bounding box's
   centre moved, and `compare_to_rehearsal` reports an operation whose two runs
   sent it opposite ways. The rule is deliberately narrow -- a sign flip with
   both sides past a millimetre, rather than the two shifts agreeing within a
   tolerance -- because the simulator's box is synthesised from sketch extents
   and is only approximate for a revolve, a sweep or a loft. Approximate enough
   that a millimetre or two says nothing; never so wrong that it reverses the
   direction a part's centre travelled. So it catches the mirror and not every
   positional disagreement, and a wider rule would need calibrating against a
   live Inventor the way the volume tolerances were.

   A second thing had to be fixed to make it work at all: the simulator's trim
   left `document.bounds` alone, so a trimmed part measured the size it had been
   before the cut -- and with the box unchanged its centre could not move, which
   is the only signal that distinguishes keeping this half from keeping the
   other.

7. **A circular pattern about an axis lying in the patterned face's own plane
   passes every check and means nothing.** Found on 2026-09-03 while checking
   whether the roadmap's reason for wanting a work axis was true. A
   `circular_pattern` turns about an axis perpendicular to the face it patterns;
   a sketch line lies *in* its own sketch plane; so a plate sketched on XY with
   its pattern axis given as a line drawn on XY is asking Inventor to revolve
   the holes about an axis lying flat in the plate. `validate_recipe` reports no
   findings, `check_recipe` reports no findings, and the simulator returns
   `ok: true` with a plausible volume, because the mock's `_repeat` accounts for
   occurrences by multiplying the seed's volume delta and never looks at the
   axis at all.

   The `work_axis` operation added the same day gives the correct thing to
   reach for, and its schema and cheat-sheet entries both say why a sketch line
   cannot serve. That makes the mistake avoidable, not detectable: the recipe
   above is still accepted.

   Fixing it properly means the simulator placing occurrences rather than
   counting them, which is the `ponytail` already recorded on `_repeat` -- the
   occurrence moves volume but records no prism, so the ledger knows about the
   seed and not the copies. Placing them would also let the divergence check's
   `centre_shift_mm` catch a pattern about the wrong axis, which is the same
   signal that caught the `trim` inversion. That is a ledger-sized change and
   is not attempted here.

   *Warned about since 2026-09-07, and not fixed.* The ledger change above is
   still the fix; what landed is `rehearse` saying so where it can be certain,
   the same shape as defect 1. It is a **warning rather than a finding**,
   because the honest limit of a static check is that a pattern about an
   in-plane axis is meaningless as a bolt circle and a legitimate way to write a
   180-degree flip, and nothing static can tell which was meant. So it names
   both substitutes -- `work_axis` with `kind: "normal_to_plane"`, or `mirror`
   -- and says outright that the volume will look right, which is the reason the
   defect survived being looked at.

   Certain in three ways, and quiet otherwise. It fires when the axis is an
   origin axis lying in the seed's plane (`x` or `y` under a plate sketched on
   XY -- the cheapest way to make the mistake, since `axis` defaults to `"z"`),
   when it is a sketch line on that plane, and when it is one on a work plane
   offset from it, following the offset chain however long. It declines on an
   angled work plane -- **which the simulator gets wrong**, since
   `mock.work_plane` files every work plane against an origin base whatever its
   `kind`, so inheriting that would report a correct angled recipe as a fault --
   on a revolved seed, whose geometry does not sit in its sketch plane, on a
   `two_points` work axis, on an `edge:` handle, and on a pattern whose axis is
   right for one seed and wrong for another.

   Only `extrude` and `hole` seed a judgement, because only for those does the
   sketch plane really describe the resulting faces. `tests/test_pattern_axis.py`
   holds both directions, including that no shipped example or calibration
   fixture fires it.

8. ~~**The recipe's sketch labels never reached Inventor**, so every lookup by
   label searched for a name nothing assigns.~~ *Found and fixed 2026-09-07, on
   the first live run of `live_acceptance.py --only work-geometry`, Inventor
   2027.1.*

   `build_sketch` on the COM backend creates each entity and sets
   `Construction`, `HoleCenter` and `Centerline` on it. It has never read
   `primitive.label`. The labels lived only in the `SketchPlan`, on the Python
   side -- and three places searched Inventor's own `SketchPoints` and
   `SketchLines` for an entity whose `Name` equalled one:

   * `_carrier_point`, which is how every `work_point` and every
     `normal_to_plane` work axis is placed;
   * `work_axis` with `kind: "sketch_line"`;
   * `_resolve_axis`, which is how a **revolve** finds a named sketch line.

   The third is the one worth dwelling on, because it long predates the work
   geometry and `docs/ROADMAP.md` recorded it as *measured*: "an off-centre bolt
   circle was already buildable via a throwaway sketch on a perpendicular plane
   carrying a line in that plane's own coordinates. That was measured before
   anything was written, and it builds clean." It builds clean on the
   **simulator**, which reads the plan's labels directly. It cannot have built
   on Inventor. The measurement was taken in a session with no Inventor to
   reach, and the word for that is not measured. The roadmap now carries the
   correction under the original sentence rather than a rewrite.

   **What the live run actually said**, and why nothing else caught it:

       [FAIL] work-geometry: WorkPoints.AddByPoint runs
              op 2 (work_point): The carrier sketch did not keep a point
              named '__work_point__'.

   The error was accurate and blamed the wrong thing: the point was never given
   that name, so it could not have been kept. Every offline test passed, because
   the mock resolves labels from the plan; and the whole COM path is
   `# pragma: no cover`, so no coverage gap showed either. It took a CAD seat,
   which is the argument for the acceptance run in one line.

   The fix keeps the entity Inventor hands back at creation --
   `_entities_by_label` builds a label-to-entity map from what `build_sketch`
   already collected, and `_labelled_entity` reads it -- so nothing depends on
   whether a sketch entity's `Name` can be assigned at all, which nothing here
   has measured. All three callers keep the old name search as a fallback, so a
   stale handle is no worse than the behaviour it replaces and the error then
   names both routes. `tests/test_sketch_labels.py` holds the bookkeeping, and a
   test fails if any of the three call sites stops asking.

   **Still unmeasured**: whether `AddByPoint` then works. The run never reached
   it. That is the roadmap's open item, not this one.

9. ~~**A parameter change rebuilt nothing, so every measurement after it was of
   the part as it had been.**~~ *Found and fixed 2026-09-07, third live run.*

   `set_parameter` set the expression and returned. `document.Update()` is
   called from `_batch`, and **`set_parameter` was the only mutating call in the
   COM backend that did not run inside one** -- so Inventor kept the old
   geometry and `mass_properties`, `topology_counts` and every export read it.

   Found by measurement, not by reading: the work-axis bolt circle's centre of
   mass moved **0.00000 mm** when `bolt_x` went 30 to 45, against a figure
   derived beforehand of 0.18640. The carrier sketch was reported
   `fully_constrained=True` with its one driving dimension in place, so the
   parametric chain was sound and nothing was rebuilding it.

   **It reaches much further than work axes.** `set_parameters` is the tool
   whose whole promise is "change a driving dimension; the model updates", and
   the DFM loop's argument for itself is that it *acts, rebuilds and
   re-measures* rather than reporting and handing off. A loop that drove a
   parameter and then re-measured was reading the part it started with.

   The fix is the existing mechanism, not a new one: the edit runs inside
   `self._batch(document)`, exactly as every feature call does. `check_work_geometry`
   now also reads the parameter back before judging the geometry, so the three
   reasons a measurement can sit still -- the parameter never took, the model
   never rebuilt, the axis was not really driven -- can be told apart next time.

10. **Inventor refuses a parameter name it can read as a unit.** *Measured
    2026-09-07 and not a defect in this repository, but it produced a bare
    "Exception occurred" and cost a run.*

    Asking Inventor 2027.1 for nine names in one document: it took `bolt_x`,
    `PCD`, `pcd_1`, `bolt_pcd`, `dia`, `pitch` and `bolt_spacing`, and refused
    **`cd`** and **`pcd`**. `cd` is the candela; `pcd` is the pico-candela. So
    the rule is an SI prefix plus a unit symbol, and it is **case-sensitive** --
    which is why `PCD` is fine and `pcd` is not.

    That is a far wider set than a list of names could cover: `mm`, `ms`, `kg`,
    `ncd`, `kA` and many more are all names Inventor will decline, and this
    server's unit table does not know candela at all, so it cannot detect them
    in advance. What it can do is stop the failure being a mystery, and
    `_diagnose_parameter`'s hint now names the cause and says to lengthen,
    underscore or capitalise the name. The acceptance run keeps the probe as a
    regression check, so a release that changes its mind shows up there rather
    than in somebody's recipe.

11. ~~**The carrier point never left the origin, so every `normal_to_plane` work
    axis ran through the origin.**~~ *Found and fixed 2026-09-07, fourth live
    run. The one that hid behind three earlier failures.*

    `_carrier_point` created its point as `PPoint("point1", construction=True)`
    -- **with no position** -- and relied on a driving dimension to place it at
    `at`. Every other `PPoint` in `geometry.py` is created *at* its coordinates
    and then dimensioned to hold it there; this call was the only exception.

    Built at (0, 0), Inventor infers a coincidence with the projected origin,
    that coincidence pins both degrees of freedom, and the dimension meant to
    place the point cannot move it. So the point stayed on the origin, and so
    did the axis through it.

    **Why it took four runs.** A bolt circle about an axis on the origin is
    *symmetric*: six holes evenly spaced around the origin have their centroid
    at the origin, so the centre of mass does not move when the driving
    parameter does -- which is exactly the reading the check was written to
    interpret as "the axis is parametric in name only". It said so three times
    and was pointing at the wrong thing. Two other faults were fixed on the way
    to it (defect 8, the labels; defect 9, the missing rebuild), both real,
    neither the cause.

    The fourth run is what separated them, and only because it printed one more
    number: **the volume changed while the centre of mass did not.** The pilot
    hole is an ordinary sketch point, built at its real position, so it moved as
    asked; the pattern axis did not. A parameter that moves some geometry and
    not the rest is not a parametric failure, and that is what said the axis
    itself was in the wrong place.

    The sign matters too, and is the second reason this cannot be left to a
    dimension: the dimensions are `abs()` of each coordinate, so from the origin
    a dimension of 30 says nothing about which side. The position carries the
    sign and nothing else does.

    `check_work_geometry` now also builds the same bolt circle about the created
    axis and about `z` and requires the two to measure *apart*. That is the
    assertion that would have caught this on the first run, and the volumes
    would not have: they agree to six decimals whether the axis is right or
    wrong.

    *Confirmed fixed on the fifth run, 2026-09-07.* The two patterns measured
    **0.37273 mm apart**, so the created axis is genuinely off-centre; and the
    bolt circle moved **0.12263 mm** when `bolt_x` went 30 to 45.

    **That figure was read as a failure and was the correct answer.** The check
    predicted 0.18640, from one line of arithmetic -- six bores removing
    1.17810 cm^3 centred on the circle, moving 15 mm, out of a remaining
    94.82190 cm^3 -- and that line has a precondition nobody had written down:
    every hole is on the plate. At `bolt_x` 45 the hole at theta=0 sits at
    x = 60, exactly the plate edge, and Inventor cuts away half of it. Deriving
    the clipped case by hand -- five whole bores plus a half-disc whose centroid
    sits 4r/3pi inside the edge -- gives **0.12263 mm**, which is Inventor's
    figure to five decimal places.

    So the work axis is measured as parametric, and the last thing standing
    between the check and a clean pass was its own constant. The geometry now
    runs `bolt_x` 20 to 35, which keeps every hole on the plate, and
    `_bolt_circle_prediction` refuses to return a figure at all when a hole
    would be clipped, naming the reason. A prediction whose assumptions are not
    met is not a looser prediction; it is a different question's answer.

    *Confirmed on the corrected geometry, sixth run: **0.18636 mm measured
    against 0.18636 derived**, twelve of twelve checks passing.* And the run
    reports the volume **unchanged** across the move, which is the independent
    signal that the precondition held -- identical material removed at both
    positions means nothing was clipped at either. Three readings agreeing on
    one mechanism is what makes this a measurement rather than a number that
    came out close.
