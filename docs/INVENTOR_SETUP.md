# Running against a live Inventor

The COM backend needs Windows, a licensed Autodesk Inventor, and `pywin32`.
Everything else in this repository runs anywhere.

```powershell
py -m pip install -e ".[inventor]"
py -m inventor_mcp --backend inventor
```

`--backend inventor` fails loudly if Inventor cannot be reached. `--backend auto`
falls back to the simulator instead, which is convenient day to day but can hide a
broken connection — use `inventor` when you mean it, and check `connect`'s reply:

```json
{"backend": "inventor", "simulated": false, "connected": true, "version": "2025"}
```

## How the connection is made

`connect` tries `GetActiveObject("Inventor.Application")` first, so an already-open
session is reused and you can watch the model appear. If none is running and
`start_if_needed` is true, it falls back to `Dispatch`, which launches Inventor.
Startup takes tens of seconds on a cold machine; the first `connect` may be slow.

The backend then calls `gencache.EnsureDispatch` to generate the early-bound type
library wrapper. That is what makes enum values exact for your installed version.
It is best-effort — see below if it fails.

## Early vs late binding

pywin32 can generate an early-bound wrapper from Inventor's type library. This
server generates it -- that is where exact enum values come from -- but then
talks to Inventor **late-bound**, resolving members by name at call time.

That is not a stylistic preference. On Inventor 2027.1 with Python 3.14 the
generated wrapper produced a string of failures on calls that were
demonstrably valid:

* `Documents.Add` handed back the generic `Document` interface, so
  `ComponentDefinition` raised `AttributeError`
* `GeometricConstraints.AddCoincident` and `AddMidpoint` returned `E_INVALIDARG`
  on ordinary sketch points
* `Profiles.AddForSolid` returned `E_INVALIDARG` on a sketch that extruded
  perfectly well by hand

Late binding costs one name lookup per call and avoids all of it. To go back:

```powershell
set INVENTOR_MCP_BINDING=early
```

If you hit a call that behaves differently between the two, that is worth an
issue -- include the Inventor version and the failing operation.

## Enum constants

Inventor's API takes enum values (`kJoinOperation`, `kVerticalDim`, …) whose numbers
come from its type library.

`inventor_mcp/backend/com/constants.py` resolves each name from the type library
when it is available and from a fallback table when it is not. The fallback table is
a convenience for machines where the pywin32 cache cannot be generated — a read-only
`gen_py` directory, a roaming profile, or a stale cache after an Inventor upgrade.

**The symptom to match**, because it names neither Inventor nor the cache and
cost the 2026-09-07 acceptance run its whole type library:

    Could not add module (IID('{D98A091D-...}'), 0, 1, 0) - <class 'AttributeError'>:
    module 'win32com.gen_py.D98A091D-...x0x1x0' has no attribute 'CLSIDToClassMap'

That is a corrupt cache, not a missing Inventor — `connect` succeeded and
reported 2027.1 in the same breath. Every `k...` constant then falls back to the
table and says so on stderr, and `scripts/com_signatures.py` cannot start at all.
Clear it as below and re-run; if the fallback warnings are still printed, nothing
in that run's enum-dependent behaviour is measured.

**If a feature is created with the wrong behaviour** — a cut that joins, a vertical
dimension that comes out aligned — a fallback value is the first suspect. Fix the
type library rather than the table:

```powershell
# Delete the stale generated cache and let it regenerate
Remove-Item -Recurse "$env:LOCALAPPDATA\Temp\gen_py"
py -c "import win32com.client; win32com.client.gencache.EnsureDispatch('Inventor.Application')"
```

If regeneration is impossible in your environment, correct the value in `FALLBACK`
for your Inventor version and open an issue with the version number.

## What to check first on a new machine

The recipe layer is covered by tests; the COM calls are not, because they need a
CAD seat. Work through the examples in order — each one exercises a different part
of the API surface:

| Example | Exercises |
|---|---|
| `mounting_plate.json` | Sketch geometry, constraints, dimensions, extrude, fillet by selector, hole from a point grid |
| `hex_standoff.json` | Polygon construction-circle constraints, tapped hole, chamfer on circular edges |
| `cover_plate.json` | Counterbored and countersunk holes, with a volume derived by hand first |
| `flanged_shaft.json` | Offset work plane, stacked extrusions, bolt circle, through bore, chamfer |
| `enclosure_base.json` | Shell with a removed face, cut extrude on a second plane |
| `angle_bracket.json` | Polyline profile, symmetric extrude, slot, mirror, sketch on XZ |
| `belt_pulley.json` | **revolve**, revolve cut, **circular pattern** — never run live |
| `pipe_bend.json` | **sweep** along an arc path — never run live |
| `duct_transition.json` | **loft** between a circle and a square — never run live |
| `threaded_boss.json` | **thread**, **rectangular pattern** — never run live |

The last four exist to find out whether the five unproven operations work. Each
isolates one, so a failure names the operation rather than blocking the rest.
The simulator builds all four and its volumes agree with a hand calculation:
the pulley at 68.006 cm³, the elbow at 7.994 by Pappus (an annulus swept a
quarter turn -- the pipe has a real bore now), the duct at 32.43 hollow (its
wall comes from a cut loft), the boss at 16.201.

Build each one, then in Inventor:

1. Open the parameter table (`fx`). Every dimension should show an **expression**,
   not a number. If dimensions show plain numbers, the expression is not reaching
   `DimensionConstraint.Parameter.Expression`.
2. Check the browser for sketch icons showing an under-constrained state. A
   correctly built sketch from this server should be fully constrained.
3. Change a parameter and rebuild. The part should move coherently. Geometry that
   jumps or flips indicates a missing locating constraint.

## What has been verified

`mounting_plate.json` builds end to end on **Inventor 2027.1** (Windows, Python
3.14, pywin32), including export and capture. That covers:

connect · new part · material · user parameters with expressions · sketch
geometry, constraints and driving dimensions · profile · extrude · fillet by
selector · hole from a point grid · mass properties · STEP · STL · PNG · save

Inventor reported 75.0185 cm³ against the simulator's 75.018498 cm³, with an
identical bounding box.

`hex_standoff.json` builds too, adding the polygon entity, a tapped hole and a
chamfer on circular edges. Its hexagon keeps one degree of freedom — see
"Known-shaky areas" below. Its tapped hole was a *plain* hole when that run was
made — the tap was recorded and dropped — so the part it builds now is not the
part that run produced. Nothing was recorded from it, so there is no stale
number, but the volume will differ once the thread is really cut.

`enclosure_base.json` builds, adding a shell with a removed face and a cut
extrude on a second plane. It contains no work plane, despite an earlier version
of this page saying so — the only `work_plane` in the repository is in
`flanged_shaft.json`.

`angle_bracket.json` builds, at 43.1999 cm³: the L-section less two 1.4617 cm³
slots and two 0.3817 cm³ holes, plus the 0.6867 cm³ its inside-corner fillet
adds back. That covers the polyline profile, a symmetric extrude, a slot, a
mirror, a sketch on XZ, blind holes on YZ, and a fillet chosen by concavity.

`flanged_shaft.json` builds, at 93.6305 cm³, adding an offset work plane and a
sketch on it, stacked extrusions, a bolt circle, a through bore and a chamfer.
Every figure in both matched a hand calculation to five significant figures —
the chamfer via Pappus's theorem on the centroid radius, which is how the
selector picking two edges instead of one was caught.

**All five examples now build end to end.** Not yet exercised against Inventor:
revolve, sweep, loft, patterns and threads — no shipped recipe reaches them, so
a sixth example is what would prove them.

Some notes from getting there, which are the sort of thing that costs an
afternoon:

* Feature methods take **typed collections**. `EdgeCollection` for fillets and
  chamfers, `FaceCollection` for shells and threads. A generic
  `ObjectCollection` holds the same objects and is refused as a type mismatch.
* Arguments typed `VT_I4` are **enums, not flags**. `ExtentDirection` takes
  `kPositiveExtentDirection`, not `True`.
* Optional-with-default arguments are safer passed explicitly;
  `Profiles.AddForSolid()` failed until `Combine` was supplied.
* Inventor **infers coincident constraints** from coordinates as geometry is
  created, then rejects an explicit duplicate as invalid. Build the shared point
  once and hand it to both entities. *The published `Sketch_Overview` says the
  opposite* -- "The API provides no constraint inference", and that two `Add`
  calls given the same `Point2d` make two separate points that `AddCoincident`
  will not merge. What was measured here (the refusals above, and defect 11's
  carrier point pinned to the projected origin) reads as inference; the two
  accounts are recorded side by side rather than reconciled, because the code
  does the one thing both agree on: pass the `SketchPoint` Inventor handed back
  rather than its coordinates. That covers standalone sketch points as
  well as chained curve endpoints: a bolt circle's construction lines each end
  on a hole centre, and asking for those six coincidences explicitly was refused
  every time — while Inventor's own hole tool populated from that same sketch
  quite happily.
* **A refused *dimension* is survivable too, if the planner added it.** It used
  to raise, so one dimension Inventor called redundant killed the whole sketch —
  which is why polyline profiles shipped carrying no dimensions at all and could
  not be revised. Dimensions the recipe asked for are still required and a
  refusal is fatal; dimensions the planner added to remove a degree of freedom
  are optional, and a refusal leaves the sketch exactly as it was before. The
  required ones are applied first, so an author's dimension claims its degree of
  freedom before a generated one can spend it. A dimension Inventor accepts but
  will not store an expression for is deleted and counted as refused: a frozen
  number has spent the degree of freedom and drives nothing, which is worse than
  not having it.
* **A refused constraint is judged by the sketch it leaves behind**, not by its
  kind. Coincidence used to be treated as always-fatal, on the reasoning that
  without it the geometry is not joined. True of a profile sketch, false of a
  sketch of hole centres, which never had a profile to lose. A sketch has failed
  only if the recipe drew a closed loop and no profile came out of it.
* `PlanarSketch.OriginPoint` cannot be constrained against. Project the origin
  work point into the sketch first.
* **A sketch has no degrees-of-freedom count, and no `FullyConstrained`
  property either.** `ConstraintStatus` is the only answer Inventor gives, and
  it is a `ConstraintStatusEnum`: `kFullyConstrained` 51713,
  `kUnderConstrained` 51714, `kOverConstrained` 51715, `kUnknown` 51716.
  Measured on 2027.1 two ways. `python scripts/com_signatures.py PlanarSketch`
  prints the class's whole property list: `ConstraintStatus` is there, and
  nothing named for DOF or freedom is. Then a live sketch through late binding,
  because the makepy wrapper raising `AttributeError` is not proof on its own —
  it declares a narrower interface than the object answers to. A circle alone
  read 51714; its centre grounded on the projected origin, still 51714; with a
  diameter dimension added, 51713.

  The two values the backend compares against are in the fallback table, so
  `python scripts/dump_constants.py` checks them against Inventor's own type
  library along with everything else.

  The only `GetDegreesOfFreedom` in the API is `ComponentOccurrence`'s — an
  assembly occurrence's rigid-body freedoms, unrelated to sketch constraint
  solving. So `SketchInfo.degrees_of_freedom` is filled by the simulator, which
  estimates it because it does no solving, and left `None` by the COM backend,
  which will not invent one. `fully_constrained` is the field both backends
  fill. This asymmetry is deliberate; do not go looking for the count again.

  The COM backend looked for `FullyConstrained` and `IsFullyConstrained` until
  this was measured. Neither exists, so it returned `None` for every sketch on
  every version and the flag never arrived at all — a silent one, since `None`
  is also the honest answer when a version genuinely cannot say.
* A midpoint constraint moves the *point* onto the line, so the grounded sketch
  origin can never be that point.
* **A hole's `ExtentDirection` runs opposite to an extrude's.** Measured with
  `scripts/probe_hole.py`: on a plane whose normal is +X with material at x > 0,
  `kNegativeExtentDirection` removed exactly a 9 mm × 6 mm hole and
  `kPositiveExtentDirection` removed nothing. That is Inventor being sensible on
  its own terms — a hole is drilled *into* the face you placed it on — but it is
  the opposite of what `direction` means everywhere else in a recipe, so the
  backend absorbs it (`_HOLE_ALONG_NORMAL`).
* **There is no second chance at a hole's direction**, so it is chosen before
  drilling rather than corrected afterwards. A hole consumes its sketch, so the
  feature cannot be deleted and rebuilt — the retry has no centres left to place
  itself on, which is why the bracket's second attempt errored on top of its
  first and left no sketch in the tree. And neither `HoleFeature.ExtentDirection`
  nor its `Definition` is writable, so it cannot be reversed in place either.
  Both routes were measured; both are closed. `direction: "auto"` — the default —
  compares the part's centre of mass against the sketch plane and drills towards
  the material.
* **A cut that meets no material still reports success.** Inventor builds the
  feature, changes nothing, and returns it. Two of the angle bracket's three
  geometry bugs hid behind an `ok` line that way. Cut extrudes and holes now
  compare the volume before and after and refuse to pass off a no-op as a
  feature; a hole that finds nothing is retried the other way first.
* **A sketch plane's axes do not follow its name, and are now measured rather
  than assumed.** The XZ plane runs its first axis along model -X: a profile
  drawn from 0 to 90 came out spanning -90 to 0. The YZ plane differs again, and
  that put the angle bracket's upright holes off the part entirely — they drilled
  air in both directions. Neither is derivable from the plane's name and both are
  silent, so guessing cost a round trip each time. The backend now creates the
  sketch, asks it via `SketchToModelSpace` where its own axes point, and
  transforms the plan to suit before drawing anything (`_sketch_axes`,
  `_orientation_matrix`, `SketchPlan.reoriented`). That covers any signed
  permutation, so an offset work plane and an axis-aligned face get the same
  treatment for free. `_MIRRORED_PLANES` survives only as the fallback for when
  the measurement fails, and the measured axes are reported on every sketch
  result — `live_smoke.py` prints them.

## Not measured at all: the Phase 2 COM

Everything else in this file was learned by running against a real Inventor.
This section is the exception, and is kept separate for that reason: `work_point`
and `work_axis` were written on 2026-09-03 in a session with no Inventor to
reach, so **three COM calls in `backend/com/backend.py` have never executed**.
The simulator side is measured and tested; the live side is a proposal.

Those three have since been measured -- see *Running all five*, below, and the
six runs it took. **`move_face`, `thicken`, `sketch_driven_pattern` and the
whole drawing surface, all added 2026-09-07, are the section's current
occupants** and are a step worse than they were: those three had signatures read
off a type library, and these do not. Each has its own subsection at the end,
and the drawing one is much the largest -- it is four calls rather than one, and
one fact it rests on has never been asked of Inventor at all.

**A third kind of evidence arrived on 2026-09-08**, and it is worth placing
between the two this file already distinguishes. Autodesk's published 2027
reference (`help.autodesk.com/cloudhelp/2027/ENU/Inventor-API`, read as a
curated extraction) gives argument lists for calls this file had only guessed
at -- `ThickenFeatures.Add`, `ThreadFeatures.Add`, the drawing retrieval pair
-- and values for an enum the type library does not carry. A published
signature is better than a guess and worse than a measurement: it is what
Autodesk says the call takes, on a page Autodesk edits in place, about a
release that may not be the one installed. `docs/DECISIONS.md` records the rule
that came out of it; the short form is that unmeasured code moved to the
published call, measured code kept its route and gained the published one as a
fallback, and every entry below says which it got.

What a live run has to confirm, in this order:

1. **`WorkPoints.AddByPoint(sketchPoint)`** — that it exists, takes a sketch
   point, and does not need a second `Construction` argument. Everything else
   here depends on it.
2. **`WorkAxes.AddByTwoPoints(first, second)`** — that it takes two
   `WorkPoint` objects rather than transient `Point`s.
3. **`WorkAxes.AddByLine(sketchLine)`** — that a sketch line is acceptable
   where the docs say `Line`.

Then the thing worth checking beyond "did it run": build
`{"op": "work_axis", "plane": "xy", "at": [30, 0]}` on a plate, pattern a hole
about it, **and change the driving parameter**. The axis is built from two work
points that share the caller's expressions, so the bolt circle should move with
the parameter. If it does not, the expressions are not reaching the carrier
sketch's dimensions and the feature is parametric in name only.

Why it is built the way it is, rather than the shorter way: the obvious
implementation is to offset two origin planes and intersect them
(`WorkAxes.AddByTwoPlanes`), which is fewer calls and uses only primitives this
file has already verified. It was rejected because it needs the sign of an
origin plane's normal, which nothing here has measured, and **a wrong sign there
builds a part that looks right** — the same failure as the `trim` inversion in
defect 5, which survived three runs precisely because the volume was correct for
the half it kept. The carrier-sketch route instead puts every position through
`build_sketch`, which measures a sketch's own axes rather than deducing them
from a plane's name, so a call that behaves differently raises instead.

### The other two, added the same way

Both were written without an Inventor to reach and both are listed here rather
than left in the changelog, because this is the file a person reads before
spending a CAD seat.

4. **A hole is aimed *after* it is built.** Unlike `extrude` there is no
   definition object to put `AffectedBodies` on -- `HoleFeatures.Add...` makes
   the feature in one call -- so the backend sets it on the finished feature and
   treats a release that refuses as a hard error. What a run has to confirm is
   the refusal path as much as the success one: the hole exists either way, and
   one on the wrong body has taken real material out of a part that looks
   finished. **The total volume cannot show it worked**, either: two equal
   blocks stay equal whichever one is bored, and only the per-body figures
   differ.
5. **A save onto a path another open document holds is refused before the
   write** -- defect 3. Nothing new is called; the check reads `list_documents`,
   which is `app.Documents`, and Inventor's own refusal is the bare "Exception
   occurred" this replaces. What a run has to confirm is the *other* half: that
   Inventor accepts the save once the named document is closed, so the remedy in
   the hint is real. Worth confirming too that `Documents` reports a path for a
   document the user opened in the UI, since that is the case a session-registry
   check could not have covered.

### Running all five

    python scripts/live_acceptance.py --only work-geometry

`check_work_geometry` runs them in the order above and stops after the first if
it fails, because the two work-axis routes are built on `AddByPoint` and a
failure there explains every later one. It skips outright on `--backend mock`:
the simulator implements all five and would pass itself, which is worse than not
running.

Three of its checks needed a part designed so the answer is visible at all, and
the reasoning is worth knowing before reading a result:

* **The bolt circle is judged by where the centre of mass went**, against a
  figure derived beforehand. Six 5 mm bores through a 120x80x10 plate remove
  1.17810 cm^3 centred on the circle, so moving that centre 15 mm shifts the
  remaining 94.82190 cm^3 by **0.18640 mm**. Zero means the expressions never
  reached the carrier sketch's dimensions and the axis is parametric in name
  only; a different non-zero figure means it moved somewhere other than where
  `bolt_x` put it. "It built" proves neither, because `_repeat` counts
  occurrences and never reads the axis.
* **The two blocks in the hole-targeting check are different thicknesses on
  purpose**, 10 mm and 6 mm. `FEATURE_COVERAGE.md` notes that a total volume
  cannot show which body was bored, and for equal blocks it cannot -- the same
  bore either way is the same volume. Unequal ones make the total say, without a
  per-body figure the `Backend` contract does not expose.
* **The save check confirms the remedy, not the refusal.** The refusal is
  offline logic that `tests/test_saving.py` already holds; what needs Inventor is
  that the file is writable once the named document is closed, because that is
  the sentence the hint puts in front of a caller.

The recipes are the shipped ones' patterns, not the unit tests'. The bolt hole is
`through_all` with no `direction`, because that is what every shipped example does
and what an acceptance run has actually measured; the unit test for the same
recipe says `direction: "negative"`, which has never run against Inventor and
would risk failing this check on the drill direction while reading as a fault in
the work axis.

For defect 4, `scripts/com_signatures.py` now reads the whole `Camera` interface.
If it reports the eye and the up vector, the orientation names can be measured as
numbers rather than judged by looking at renders -- which is what `check_views`
says is missing before any of it can be asserted. **Asked on 2026-09-07 and the
type library has no `Camera` module**, so that route is closed: measuring the
orientations needs a live probe against the object, the way
`scripts/probe_convexity.py` works, not a signature read.

### What the 2026-09-07 runs measured

The same read is worth recording for the three calls this section is about:
`com_signatures.py` lists only `WorkPoints.AddAtCentroid` and
`WorkAxes.AddByAnalyticEdge`, and **none of `AddByPoint`, `AddByTwoPoints` or
`AddByLine` appears in the generated wrapper at all** -- yet all three execute.
makepy writes a module per interface it generates and skips members; late
binding asks the object rather than the wrapper, which is the reason this server
uses it, and this is the clearest case of it so far. A missing signature is not
evidence of a missing call.

What the second run left open, and what to do about each:

* **Inventor refuses a parameter name it can read as a unit.** *Answered.* It
  took `bolt_x`, `PCD`, `pcd_1`, `bolt_pcd`, `dia`, `pitch` and `bolt_spacing`,
  and refused **`cd`** and **`pcd`** -- the candela and the pico-candela. So the
  rule is an SI prefix plus a unit symbol, and it is **case-sensitive**, which
  is why `PCD` is accepted where `pcd` is not. Wider than any list could cover
  (`mm`, `ms`, `kg`, `ncd`, `kA`...), and this server's unit table does not know
  candela, so it cannot pre-empt them; `_diagnose_parameter`'s hint names the
  cause instead. The probe stays in the run as a regression check.
* **The carrier sketches print `horizontal_align(__origin__, point1) was
  refused`, and it does not matter.** *Answered:* every carrier sketch comes out
  `fully_constrained=True` with its one driving dimension in place, so the
  refusal is Inventor declining a constraint it had already inferred -- exactly
  what `DECISIONS.md` says usually happens -- and the "keeps a degree of
  freedom" in that message is over-claiming. Worth knowing precisely because a
  carrier point free to move would be a work axis that does not track its
  parameter, and this rules that out.
* **A parameter change rebuilt nothing.** *Found on the third run and fixed --
  defect 9.* `set_parameter` was the only mutating call outside `_batch`, and
  `_batch` is what calls `document.Update()`, so the geometry stayed as it was
  and every measurement afterwards read the old part. Reaches the DFM loop and
  `set_parameters`, not only this check.
* **The carrier point never left the origin** -- defect 11, the one that
  actually stopped the bolt circle moving, found on the fourth run. It hid
  behind its own symmetry: a bolt circle about an origin axis has its centroid
  *at* the origin, so the centre of mass does not move when the parameter does,
  which reads exactly like the parametric failure this section told you to look
  for. Three runs said so and pointed at the wrong thing.
* **And then the prediction was wrong rather than the part.** The fifth run
  measured 0.12263 mm against a derived 0.18640 and reported a failure. The
  derivation assumed six whole holes; at `bolt_x` 45 the hole at theta=0 sits at
  x = 60, exactly the plate edge, and Inventor halves it. Hand-deriving the
  clipped case gives 0.12263 mm -- Inventor's figure to five decimals. **When a
  measurement disagrees with a derivation here, check the derivation's
  preconditions before the part.** The geometry now runs `bolt_x` 20 to 35,
  which keeps every hole on the plate, and `_bolt_circle_prediction` refuses to
  return a figure at all when one would be clipped.

### Where it ended up

`--only work-geometry` passes **twelve of twelve** on 2027.1, with one
deliberate skip (`hole` + `bodies`, which Inventor cannot do). What to expect,
so a future run has something to compare against:

    parameter names Inventor took: bolt_x, PCD, pcd_1, bolt_pcd, dia, pitch, bolt_spacing
    parameter name REFUSED: pcd ... cd
    work_planes holds 3: ['YZ Plane', 'XZ Plane', 'XY Plane']
    work_axes   holds 3: ['X Axis', 'Y Axis', 'Z Axis']
    work_points holds 2: ['Center Point', 'Datum']
    carrier sketch Datum_carrier fully_constrained=True, dimensions=1, refused_constraints=0
    centre of mass moved 0.18636 mm on bolt_x 20 -> 35, derived 0.18636
    about the axis vs about Z, centres of mass 0.37273 mm apart

The three numbers at the bottom are the ones that matter and they check each
other: the magnitude against the derivation, the discriminator saying the axis
is not on the origin, and -- printed as a note -- the volume unchanged across
the move, which is what says no hole was clipped and the derivation therefore
applied. Any two of those agreeing while the third does not is worth stopping
for.
* **`hole` + `bodies` is not available on 2027.1.** `HoleFeature` has no
  `AffectedBodies`; see the gap list in `FEATURE_COVERAGE.md`. The acceptance
  check skips it with that reason rather than failing every run, and `rehearse`
  warns on the field.
* **Work geometry is absent from `list_features`, and the missing fact is now
  measured.** The COM backend walks `ComponentDefinition.Features`; Inventor
  keeps work planes, axes and points in their own collections, and the mock puts
  them all in one list. On a part carrying one created work point, 2027.1
  reports:

      work_planes  3: ['YZ Plane', 'XZ Plane', 'XY Plane']
      work_axes    3: ['X Axis', 'Y Axis', 'Z Axis']
      work_points  2: ['Center Point', 'Datum']

  So **Inventor's origin geometry does sit in those collections** -- three
  planes, three axes and a `Center Point` -- and a created one is simply the
  extra entry. That is what a `list_features` fix needed and did not have.
  Filtering by name would be the wrong way to use it (a user can rename an
  origin plane, and a localised Inventor names them differently); what the
  numbers show is that a *positional* rule -- the first three, three and one --
  matches this release, and that wants confirming on another before anything
  relies on it.


### `move_face`, where the signature was unknown and is now published

Added 2026-09-07, in another session with no Inventor to reach, with a gap the
five above did not have: the definition object was measured to exist and its
setter was not, so the backend tried three spellings and named them all. **The
`MoveFaceDefinition` page was read on 2026-09-08** and settles the name and
the shape, though not the argument list:

* the setter is **`SetDirectionAndDistanceMoveType`** -- "the move is defined
  using a direction and a distance along the direction" -- beside
  `SetFreeMoveType` (a matrix) and `SetPlanarMoveType` (two points and an
  optional plane). None of the three spellings tried before exists;
* `MoveFaceType` is a property that **starts at `kFreeMoveType`** when the
  definition is created and the setter moves it to
  `kDirectionAndDistanceMoveType` (91393; the enum is in the fallback table).
  So the backend reads it back before `Add` and refuses a definition the setter
  left at free-move -- a move defined by no matrix, which is the part that
  builds and is wrong;
* `Faces` and `AutomaticBlending` are properties, and
  `MoveFaceFeatures.CreateDefinition` is the accessor the page names, so the
  longer factory spelling the backend also tried is gone.

What the page does not give is the setter's argument list, so the call is
`SetDirectionAndDistanceMoveType(direction, distance)` -- a COM object and an
expression string, so a swap is a type mismatch -- and a third argument with a
meaningful default is still the first thing to look for.

**So start by reading it, not by running the check:**

    python scripts/com_signatures.py MoveFaceDefinition
    python scripts/dump_constants.py --find MoveFaceType

Then:

    python scripts/live_acceptance.py --only move-face

What the run has to answer, in this order:

1. **`MoveFaceFeatures.CreateDefinition(faces)`** -- that it takes a
   `FaceCollection` and nothing else.
2. **`SetDirectionAndDistanceMoveType`'s argument list**, and whether the
   distance goes in as an expression string. Every length in this server reaches
   Inventor as an expression so the dimension keeps its parameter; a setter that
   insists on a number would take that away and is worth knowing about. The
   `MoveFaceType` read-back says whether the setter took at all.
3. **Whether a negative distance is accepted.** `flip` is passed as
   `-(expression)` rather than through a reversal property, because no such
   property has been read. If Inventor refuses it, the fix is that property, and
   this is the cheapest thing in the run to get wrong without noticing -- a
   refusal is loud, but a *silently ignored* sign is a face that moves the wrong
   way, and the fixtures below are what catch it.

And then the thing worth checking beyond "did it run", which for this operation
is not one number but three. `check_move_face` builds
`examples/calibration/lifted_face.json` and `widened_wall.json`, whose true
answers are exact rather than estimated -- a prism's face keeps its area as it
translates, so the solid changes by exactly area times distance:

* **+6.4000 cm^3** for the plate whose top face rises 2 mm (32 cm^2 x 0.2 cm),
  and the distance is the parameter `lift`, so **changing `lift` has to change
  the volume**. That is the defect 11 lesson applied before it can be repeated:
  a feature can build, measure right, and be parametric in name only.
* **+0.2400 cm^3** for the plate with one side wall pushed 1 mm out
  (40 x 6 x 1 mm). Its face is picked out of four by a selector rather than
  being the only cap, so it fails if the COM selector reaches a different face
  than the simulator's; and it moves along an axis that is not the extrude's
  own, so it fails if Inventor reads the direction relative to the face rather
  than to the model. Its figure is deliberately small beside the 19.2 cm^3 plate
  it sits on, so a move that took the whole wall with it is a large fraction
  rather than a rounding error.
* **A sign, on both.** Both moves add material. A negative delta means the
  faces went the other way, which is the `flip` question above and is the
  failure a magnitude-only check would pass.

If a fixture disagrees, the answer is a fault to find and not a tolerance to
widen: these are derivations rather than estimates.
`PREDICTED["move_face"]` sits at the placeholder 0.50 and should come down to an
extrude's 0.02 once a run agrees, rather than to something in between.


### `thicken`, where the risk is a side and an argument order

Added 2026-09-07, in the same session as `move_face`. Nothing has run. **The
signature has been read since**, on 2026-09-08 and off the published reference
rather than a type library:

    ThickenFeatures.Add(Faces, Distance, ExtentDirection, Operation,
                        [AutomaticFaceChain], [CreateVerticalSurfaces],
                        [AutomaticBlending]) As ThickenFeature

`Faces` is a `FaceCollection` or a `WorkSurface`; the three trailing Booleans
default False; and the reference's table of which features have a definition
object says Thicken has **none** -- so the `CreateThickenDefinition` the
backend used to try first could never have existed, and the sixth-argument
`True` it fell back to was going into `CreateVerticalSurfaces`, which adds
side faces nothing here predicts. Both are gone. What remains sharper than
`move_face`, and is still handled in the code rather than left for a run:

**1. A wrong argument order need not raise.** The distance is a *variant* -- a
number or an expression string -- and the enums are integers. So an order that
was wrong would not be the type mismatch that makes `_profiles`'s two forms
safe to try: Inventor would accept a thickness of 20,481 (`kNewBodyOperation`)
and build a part the size of a house, successfully. The order is the published
one now, which makes this unlikely rather than impossible, and:

* `Add`'s arguments are **never permuted**. What is tried twice is the three
  optional Booleans, left to their documented defaults and then passed as
  False -- the same optional-with-a-default problem `AddForSolid` had, and
  appending flags at their default cannot change what the earlier arguments
  mean.
* And the result is measured. The backend predicts `area * thickness` from the
  faces it selected -- it reads `Face.Evaluator.Area` anyway for the selectors --
  and refuses anything outside a **factor of four** of that, deleting the feature
  rather than leaving it in the part. Four is deliberately enormous: it catches a
  thickness of 20,481 cm and nothing subtler, because the subtler end is the
  divergence check's job.

**2. Which side a `negative` layer lies on is a claim about Inventor.**
`THICKEN_SHARE` in `backend/base.py` says the layer is a slab swept from the
face, the operation is a boolean, and a face's normal points out of the solid --
so the outward half is air and the inward half is material, and therefore:

| direction | join | cut |
|---|---|---|
| `positive` | +area x t | nothing to remove |
| `negative` | already material | -area x t |
| `symmetric` | +area x t/2 | -area x t/2 |

That is set algebra, and it is sound *given* that Inventor means the same thing
by "negative". Nothing here has measured that. The two cells that do nothing are
warned about at rehearsal rather than refused, deliberately: a refusal would
prevent the run that settles the question, which is the mistake the `shell`
`both` enum made -- the refusal was right and it hid the fact that nothing had
ever exercised the path.

**What the run has to answer**, in this order:

1. **Which form builds it** -- `Add` with the four published arguments, or
   with the three optional Booleans passed explicitly. The feature detail
   reports `built_by`, so a successful run says which; a failure on both names
   Inventor's message for each.
2. **The side**, via `examples/calibration/thinned_wall.json`. One wall thinned
   1 mm from behind should remove **0.2400 cm^3** and leave the plate 79 mm
   wide. Three outcomes are distinguishable: **-0.2400** confirms the table,
   **0.0000** says Inventor puts a `negative` layer outside the solid so there
   was nothing to cut, and any positive figure says something else again. This
   is the reading that matters, and it is defect 5's lesson taken in advance --
   a `trim` kept the wrong half of a part for as long as the feature existed, and
   one of the runs that found it was 1.2% apart, inside every tolerance, because
   the volume was right for the half it kept. A tolerance cannot catch a side.
   Different numbers can.
3. **The corners**, via `examples/calibration/thickened_walls.json`. Four walls
   grown 1 mm outward: the layers do not meet, and the 1 x 1 x 6 mm notch at
   each corner belongs to no wall. So **1.4400 cm^3** if Inventor leaves the
   notches and **1.4640** if it closes them -- 4 x 6 mm^3 apart, 1.7%. This is
   reported rather than asserted: nobody has measured which, and a check that
   picked one would be inventing the answer it then confirms. If it turns out to
   be the closed one, the simulator is 1.7% low on every multi-face thicken and
   should gain the corner term.

    python scripts/com_signatures.py ThickenFeatures
    python scripts/live_acceptance.py --only thicken

Read the installed signature first anyway, as with `move_face`: it costs a
second, and a release whose `Add` differs from the published one is exactly
what `com_signatures.py` exists to catch. The factor-of-four guard stays until a
run agrees with the prediction.

**One thing a run cannot answer, because it is not about Inventor.** The half of
Inventor's Thicken that turns a *surface* into a wall is unreachable here, and
not because of the schema: no operation in this server creates a surface, so the
only surface a part could hold is one that arrived through `import_geometry`.
Thickening that would work today. `docs/FEATURE_COVERAGE.md` records it under
Tier 1c rather than as a gap in this file, since it is a fact about this server.


### `sketch_driven_pattern`, where the question is a count

The third and the lowest-risk of the three, and worth reading for what it is
*not* worried about as much as for what it is.

**It was calling the wrong shape, and the published page said so** (read
2026-09-08): `SketchDrivenPatternFeatures.Add(Definition As
SketchDrivenPatternDefinition)`. The backend had passed a collection, a sketch
and a point straight to `Add`, which could never have worked. It now calls
`CreateDefinition(parents, sketch, reference)` -- whose argument list is *not*
on the pages read, so those three are the assumption and a wrong count or order
is a type mismatch rather than a part built wrongly -- then sets `ComputeType`
on the definition and calls `Add(definition)`. The compute-type question is the
one the other two patterns settled on 2027.1: a pattern of a hole fails outright
until each occurrence is recomputed. `python scripts/com_signatures.py
SketchDrivenPatternFeatures SketchDrivenPatternDefinition` is the read that
settles the factory's arguments and whether the definition carries a
`ComputeType` at all.

**Its arithmetic is not new either.** An occurrence does whatever its seed did,
which is the rule `rectangular_pattern` and `circular_pattern` use and which the
pulley and the threaded boss confirm at 0.02. `PREDICTED` is 0.02 accordingly,
not the placeholder the other two unmeasured operations sit at.

**What a run has to settle is a semantic question, and its answer is a count.**
Does Inventor also place an occurrence on the reference point? The recipe
assumes not: the seed sits on the reference and the other points get one
occurrence each, so a sketch of N points describes a part with N of the feature
on it. Two of the three possible answers are the same volume:

| what comes back | what it means |
|---|---|
| -1.2000 cm^3, four pockets | the assumption holds |
| -1.2000 cm^3, **five** features | the reference was patterned onto itself; the duplicate lands exactly on the seed and removes nothing extra |
| -1.6000 cm^3 | five occurrences, the fifth somewhere unaccounted for |

    python scripts/com_signatures.py SketchDrivenPatternFeatures
    python scripts/live_acceptance.py --only sketch-driven-pattern

The check measures the part *and prints its feature list*, because that middle
row is invisible to a volume. If it turns out to be the middle row, two things
change together: this section, and the `elsewhere` filter in the mock's
`sketch_driven_pattern` that excludes the reference.

**One thing this run cannot check, because it is the simulator's own.** The mock
*places* the occurrences -- it is the only pattern here that does -- so a cut
through where an occurrence went is measured against what the pattern left, and
an occurrence of a cutting seed standing over air is reported. Both are held by
`tests/test_sketch_driven_pattern.py`, and neither needs Inventor: they are
claims about the ledger, not about the API. What Inventor decides is only how
many occurrences there are and where -- and if the count is wrong, everything
the placement then says is wrong with it.


### Drawings, the largest unmeasured surface in the project

Added 2026-09-07. Four `Backend` methods -- `new_drawing`, `place_view`,
`retrieve_dimensions`, `read_drawing` -- implemented on both backends, with the
simulator's half measured and tested and the COM half never executed. What makes
this different from the three above is not only its size:

**One of the four carries no risk at all.** `new_drawing` is `new_part` with a
different enum: `Documents.Add` is measured, and `kDrawingDocumentObject` has
been in the constants table since before anything used it. If the rest of this
fails, that call will not be why.

**No enum value is guessed anywhere in it.** The view orientations and styles
are referred to by their documented *names* -- `kFrontViewOrientation`,
`kHiddenLineRemovedDrawingViewStyle` -- and `_k` reads their values from the
type library, raising a message that names the fix when it cannot. So a wrong
name raises and a wrong number is not possible. That is a better position than
the extrude extents were in before they were measured, where 32 of 51 fallback
values turned out wrong.

**And one fact underneath it had never been asked of Inventor** -- until the
published reference answered it from the other side.

#### The fact the whole approach rested on, and what replaced it

Dimensions are **retrieved** from the model, not placed by geometry. The reason
is the parts this server builds: every sketch dimension it creates carries a
parameter's expression, and every driven feature value is a named parameter, so
Inventor's own retrieve-model-dimensions produces dimensions that *are* the
parameters. Placing a dimension by geometry would mean working out which two
drawing curves a parameter drives, which is exactly the guessing a recipe exists
to avoid.

As written on 2026-09-07, retrieval brought *every* model dimension onto the
view and the asked-for ones were kept by asking each *drawing* dimension which
model parameter it came from -- a property of `DrawingDimension` that nothing
documents, tried under four spellings. That was the one fact the design rested
on, and the reference (read 2026-09-08) says the two names it tried the
retrieval under, `RetrieveDimensions` and `AddRetrievedDimensions`, do not
exist in 2027 at all. What exists instead, new in 2026.1, turns the question
round:

    Sheet.GetRetrievableAnnotations2(View, [SketchAndFeatureDimensions],
                                     [ModelObject], [DesignView]) As ObjectCollection
    Sheet.RetrieveAnnotations2(ViewOrSketch, [AnnotationsToRetrieve]) As ObjectsEnumerator

The first returns the **model's** `DimensionConstraint` and `FeatureDimension`
objects (or their proxies) that could be retrieved into the view, before any
retrieval happens. A `DimensionConstraint.Parameter` *is* documented. So the
backend now chooses on the model side, by parameter name, and hands only the
chosen ones to the second call -- one at a time, so the drawing dimension that
comes back is known by the parameter that went in, is remembered against the
document, and `read_drawing` names it from that memory. No property of a
`DrawingDimension` is relied on for anything this session placed; the four
property paths survive only for a dimension somebody else put on the sheet.
Nothing is placed and deleted again. The old routes survive as a fallback for a
release older than 2026.1, with their original failure mode.

Two things are still unmeasured, and a run should read them in this order:

    python scripts/com_signatures.py Sheet
    python scripts/com_signatures.py DimensionConstraint FeatureDimension

* whether `GetRetrievableAnnotations2` is in the installed release's wrapper at
  all (it is a 2026.1 method, and 2027.1 is later, so it should be), and what
  it returns for a part built here -- a `DimensionConstraintProxy` names its
  parameter through `NativeObject`, and the backend tries that too;
* ~~whether a `FeatureDimension` names a `Parameter` the same way~~ -- *settled
  on paper the same day*: the `FeatureDimension` page lists `Parameter`
  ("the parameter associated with the dimension") and `FeatureDimensionProxy.
  NativeObject`, which are the two paths `_model_parameter_name` reads. What is
  left for a seat is whether `GetRetrievableAnnotations2` hands feature
  dimensions back at all for a part built here.

The outcome that would have sent this back to the drawing board -- a retrieved
dimension unable to name its parameter -- can no longer happen, because nothing
asks it to. What replaces it as the worst case is the pair being absent, and
that is a version fact rather than a design one.

#### What the run has to answer, in order

1. **`new_drawing`** -- that a drawing document is created and a template given
   as a path is honoured. Lowest risk; everything below needs it.
2. **`DrawingViews.AddBaseView(Model, Position, Scale, ViewOrientation,
   ViewStyle)`** -- the argument order is Inventor's documented one and is a
   proposal. Passed by name through `_call_named`, so the positions are readable
   at the call site.
2b. **`DrawingViews.AddProjectedView(ParentView, Position, ViewStyle)`**, which
   is a different call and takes **no orientation and no scale**. Which way a
   projected view faces is decided by where it sits relative to its parent and
   by the sheet's projection angle -- Inventor is told a place and infers the
   direction, the reverse of a base view. So the angle is applied *before* the
   call, in `drafting.projected_position`, and this is the one place
   `DrawingRecipe.projection` does any work.

   That makes the direction check in item 3 **sharper for a projected view than
   for a base one**: nothing was asserted about its direction, so what the sheet
   reports back is Inventor's own answer to a question only the layout asked. A
   projected top view that reads as anything but `top` means the convention this
   project implements and the one Inventor applies are not the same -- and since
   `_THIRD_ANGLE_STEP` is negated for first angle and nothing else distinguishes
   the two, one run on each convention settles it.
3. **Whether a direction's name describes what you get.** This is defect 4's
   drawing-shaped cousin and the reason `read_drawing` reports a view's extent
   and its orientation *as the sheet has them* rather than as they were
   requested. `capture_view`'s orientation names do not describe what they
   return -- `front` gives a top view on a part built on XY -- and a drawing
   view reaches Inventor through a similarly-named enum. `build_drawing` warns
   when a view reports facing a way it was not asked to, and that warning can
   only come from the sheet.
4. **Retrieval**, per the section above: that `GetRetrievableAnnotations2`
   offers the part's dimension constraints, that they name their parameters,
   and that `RetrieveAnnotations2` puts the chosen ones on the view. The
   legacy names are the fallback and are not expected to exist on 2027.
5. **`read_drawing`** -- that a sheet can be walked and its dimensions read
   with values. Everything the round trip concludes comes through here.

    python scripts/live_acceptance.py --only drawing

#### What a live run would prove that the simulator cannot

The simulator implements all four and is worth trusting about the *recipe*: it
catches a parameter that drives nothing and so has no dimension to retrieve, and
it holds the sheet against the part. Two things it cannot be evidence for, and
they are worth separating:

* **the overall-size check.** A built sheet's view extents are Inventor's own,
  measured off the view it placed, so comparing them with the part is a real
  check. In the simulator the extent is *computed from* the part's bounding box,
  so there the same check compares the part with itself and can only fail if the
  scale arithmetic is wrong. `drafting.reading_of` says so at the point where it
  matters;
* **the direction check** in item 3. The simulator honours the direction it is
  given by construction, so it will never report a view facing the wrong way.
  That check exists entirely for the live half, and it is the *projected* views
  it matters most for: theirs is the direction nobody asserted.

And one thing neither can answer, because it is not about the API. **PDF export
went in with this** -- `export_model` offers `pdf` now, and it is the format a
drawing is actually sent in, since a sheet exportable only as DWG needs Inventor
at the other end to read. Whether the PDF translator add-in is enabled is a
per-machine fact rather than a release fact, and `export`'s
written-but-not-there check is what reports it: Inventor answers success and no
file appears.

## Known-shaky areas

These are the parts of the COM backend most likely to need adjustment, and why:

- **`sweep`** is measured now. `AddUsingPath` wants a `Path`, and
  `Features.CreatePath(curve)` is the only thing that makes one:
  `Profiles.AddForSurface` returns a `Profile` and the sweep rejects it with
  "Type mismatch". The curve matters as much as the method — a sketch of "one
  arc" holds the arc *and three points*, because the origin is projected in
  whenever a constraint references it, so the first *entity* is very likely a
  point. `_first_curve` picks geometry instead.
- **`shell`** uses `CreateShellDefinition`. Older releases expose a direct
  `ShellFeatures.Add(faces, thickness, direction)` instead.
- **`direction: "both"` splits the wall across the original face**, half in and
  half out, and the enum is `kBothSidesShellDirection` (41219) -- not
  `kBothShellDirection`, which is a name no release has and which the constants
  table asked for until 2026-09-03, so this direction refused on every machine
  rather than only on one with an unreadable type library.

  Measured, and exactly: a 60x40x20 box with its top removed and a 2 mm wall
  both ways leaves an outer solid grown 1 mm on the four sides and the base and
  a cavity inset 1 mm, so 6.2 x 4.2 x 2.1 less 5.8 x 3.8 x 1.9 is 12.808 cm^3
  and the shell removed 35.192. Inventor removed 35.1920.
  `examples/calibration/shelled_both_ways.json` is that part.
- **A pattern of a hole needs `kAdjustToModelCompute`.** Measured: patterning a
  boss works with the default compute type, and patterning a hole fails outright
  — with the boss or alone — until each occurrence is recomputed rather than
  copied. That is what the settings mean: identical compute copies faces, which
  is valid only where the copy lands on the geometry it came from, and a blind
  hole's second occurrence has no material to remove until the boss beneath it
  exists. The pulley's through-holes in a flat disc pattern happily with the
  default, which is why this took a while to see. Both patterns now recompute
  first and fall back to the default, reporting which built the feature.
- **`rectangular_pattern` with a second axis** put the compute type where the
  *spacing* type belongs, which shifted every argument after it and made the
  second axis land in `XDirectionStartPoint`. Named arguments are used now, so
  the optional slots between the two axes are left to the wrapper's defaults
  rather than filled with a guess. A single-axis pattern was never affected.
- **`thread`** has never built on 2027.1 and is refused by the builder. The
  backend used to call a `CreateThreadDefinition` that exists on no release;
  since 2026-09-08 it follows the published `ThreadFeatures.Add(Face, StartEdge,
  ThreadInfo, [DirectionReversed], [FullDepth], [ThreadDepth], [ThreadOffset])`,
  with `StartEdge` an edge of the threaded face. Where the `ThreadInfo` comes
  from is published as well, on a per-member page read the same day:
  `ThreadFeatures.CreateStandardThreadInfo(Internal, RightHanded, ThreadType,
  ThreadDesignation, Class) As StandardThreadInfo`, with `ThreadType` a sheet
  name in `Thread.xls` and `Class` such as `2B` or `6g`. The 2027.1 makepy
  wrapper does not list it -- the shape `WorkPoints.AddByPoint` had, which
  executed regardless -- so it is called late-bound in that order, first; the
  measured `HoleFeatures.CreateTapInfo`, whose `HoleTapInfo` the pages say
  derives from `StandardThreadInfo`, is the fallback with `Internal` set.
  Neither has run, so `thread` stays in `_KNOWN_BROKEN` and a `hole` with `tap`
  is the route that works. `python scripts/live_acceptance.py --only threading`
  is where this gets settled.
- **A tapped hole is cut to the thread's minor diameter**, `D - 1.0825 x pitch`
  for ISO metric — 6.6469 mm for M8x1.25, measured from the removed volume to
  four decimal places. That is narrower than the 6.75 mm tapping drill, so a
  recipe giving the drill size will be told the two disagree.
- **A blind hole's depth is measured to the shoulder**, and the drill point goes
  beyond it, so a pointed hole removes the full cylinder *plus* the cone.
- **A tap designation must carry its pitch.** `M8x1.25` is accepted and `M8` is
  refused. `ANSI Metric M Profile`, `ISO Metric profile` and
  `ANSI Unified Screw Threads` all work; `NPT` and `BSP` were refused with the
  designations tried, so their format is still unknown here.
- **`HealthStatusEnum` is not in the type library at all** on this release, so a
  feature's health could not be translated by name from Inventor. 11778 is what
  seven just-built, individually verified features all reported, so it was
  treated as healthy on that evidence and nothing else. *The published page
  agrees* (read 2026-09-08): 11778 is `kUpToDateHealth`, and the other
  thirteen members -- `kInErrorHealth` 11781, `kCannotComputeHealth` 11783,
  `kSuppressedHealth` 11784 and so on -- are in the fallback table from that
  page, so `rebuild` now names a sick feature's status beside its number. On
  this release the table is the only source for them and `dump_constants.py`
  will report every one as "not in this type library", which is expected.
- **Hole styles** go through Inventor's own hole methods, one per combination of
  style and extent (`AddCBoreByThroughAllExtent` and its seven siblings). Their
  argument order was taken from another project's field notes rather than
  measured here — the extent-direction enum comes *before* the counterbore's own
  dimensions, which is not how it reads — so the backend reads `HoleType` back
  off the finished feature and refuses rather than reporting a counterbore it
  cannot see. A plain drilled hole is exempt: it claims nothing beyond removing
  material, which is already checked, and what `HoleType` reads back for one has
  never been measured here — so the holes that work today cannot start failing
  over an enum. `python scripts/probe_hole_styles.py` settles the order and the enum
  values in one run, and `examples/cover_plate.json` has a hand-derived volume
  that catches a hole built as the wrong shape.
- **Edge convexity is decided from the boundary loops**, which is exact: a
  face's boundary runs anticlockwise about its outward normal, so the material
  lies to the left of the loop, and whether that direction faces into the
  neighbouring face's normal is the answer. Getting there needed two facts the
  API does not give. `EdgeUse.Face` and `EdgeUse.EdgeUseLoop` do not exist on
  2027.1 (`Parent` is the whole `SurfaceBody`) — and **`IsParamReversed` does
  not mean "runs against the loop"**: both uses of an edge report `False`, so
  trusting it made the two faces contradict each other on all 24 edges of the
  probe's test part, and the method answered nothing at all while looking
  healthy. `EdgeUse.Next` supplies both: the following edge lies on the same
  face and shares exactly one face with ours, which names the face; it also
  meets ours at one vertex, and a loop runs *towards* the vertex it shares with
  the edge that follows, which gives the direction.
  Sampling a point on each face (`Face.PointOnFace`) is kept only for bodies
  with no edge uses at all. It is not allowed to overrule a loop that looked and
  declined: the sample is arbitrary, and on a face with an inner loop it can land
  across the hole and invert the answer. That cost a run — drilling the bracket's
  upright put two inner loops in the face beside its L-junction and moved the
  "inside corner" fillet onto a 56 mm convex edge, removing 0.7634 cm³ where the
  right edge adds 0.6867. Every match reports which method decided it, and
  `live_smoke.py --topology` prints it whenever it was not the exact one.
- **Convexity is never known for a circular edge**, because a full circle has no
  single tangent and the sampler needs planar normals on both sides. That is
  exactly where you want it: the `flanged_shaft` chamfer asked for `circular`
  with `limit: 2` and got the shaft's free end *and* its flange junction, one
  convex and one concave, which removed and re-added the same 0.0884 cm³ for a
  net change of nothing. `near` says which end is meant; `convex` cannot, yet.

  *A route to "yet" arrived 2026-09-08.* `SurfaceBody.ConvexEdges` and
  `ConcaveEdges` are documented read-only `EdgeCollection`s -- Inventor's own
  classification, in one property. The backend now reads both once per
  `select` and keys them by `Edge.TransientKey` (documented valid while the
  document is unchanged, which is the lifetime of one selection), and asks them
  **only where the loops decline** -- so a circular edge can get an answer, and
  a match reports `convexity_from: "body"` when it did. Where both the loops
  and the body answer and disagree, the loops win and a warning is logged;
  that log line is what a live run should look for, because it is the evidence
  that would justify putting Inventor's own answer first. Unmeasured: whether
  the collections classify circular edges at all, and whether they agree with
  the loops on the 24-edge probe part. `scripts/probe_convexity.py` prints the
  loop and sampled verdicts per edge and should gain the body's beside them.
- **Face normals** are read via `GetNormalAtParam` with `IsParamReversed` applied.
  If `top`/`bottom` selectors pick the wrong faces, that is where to look.
- **`FullyConstrained`** is not exposed under that name on 2027.1, so sketches
  report `null` for it rather than true or false.
- **Polygons keep one degree of freedom.** A regular polygon is built as a
  construction circle, vertices coincident with it, and `n - 1` equal-length
  edges. Inventor refuses the last of those equalities — whether they are
  chained around the loop or all measured against the first edge, it is always
  the constraint involving the *closing* edge that is rejected, so it considers
  that edge's length already determined. The geometry is created regular and
  the circle is dimensioned, so the part is correct; the sketch is simply not
  fully constrained. `refused_constraints` on the sketch result reports it.

None of these affect the mock backend or the recipe format.

## Exporting

Export goes through `Document.SaveAs` with a corrected extension, rather than
looking up translator add-in GUIDs, which move between releases. The written file is
then checked on disk; if Inventor reports success but nothing appears, the format's
translator add-in is probably disabled (Tools → Add-Ins).

Formats: `step stl iges sat dwg dxf obj 3mf ipt`.

## Performance

Sketch construction is wrapped in a batch that disables `ScreenUpdating` and defers
`Document.Update()` until the sketch is complete. A large sketch built with redraw on
can take an order of magnitude longer.

Topology handles returned by `select_topology` are regenerated on each call and are
invalid after any rebuild. Do not cache them across operations; re-select instead.

## Security note

Inventor automation runs with the permissions of the logged-in user and can open,
modify and overwrite files. Run the server against a scratch directory when driving
it from an autonomous agent, and be aware that `open_part` and `save_part` take
arbitrary paths.

## The live acceptance run

`scripts/live_acceptance.py` is the one command worth running after any change
that could touch geometry. It builds every example and checks the volume, face
count, edge count and bounding span against `examples/expected/`, then runs the
things that can only be answered by a real Inventor:

- **a parameter edit moves the geometry** — widen `base_len` to 120 and the
  bracket's X span must follow. If it stays at 90 the outline is not driven by
  its parameters, which is the failure the whole project exists to prevent.
- **every hole style builds as the style asked for** — one hole of each style
  through one block, with the volume removed checked against what the geometry
  says. The backend already refuses a style Inventor will not confirm, so a
  failure here means either the argument order is wrong on this release or the
  shape is wrong despite the right label. `scripts/probe_hole_styles.py` prints
  the same cases with the enum values and the thread tables, which is what to
  run next.
- **a failed build rolls back** — Inventor's `TransactionManager` is asked to
  undo a build that broke halfway, and the volume has to come back. Written
  against the simulator, which copies the document aside; whether an abort
  restores a *consumed sketch*, which is the failure rollback exists for, has
  never been checked.
- **Inventor from a pool of threads** — sixteen calls from eight threads, which
  is what an MCP client does and what nothing had ever done here.
- **a promotion leaves the part the shape it was** (`--only promotion`) —
  `promote_parameters` measures this for itself now rather than asserting it,
  and the simulator cannot be evidence for it. There, `promote_parameter` edits
  a dictionary rather than an Inventor expression, so nothing could move; and it
  reports no centre of mass at all, so the reading that catches material moving
  *without* the volume changing has never been taken. A live run is what
  confirms two things: that Inventor re-evaluates the expression a promotion
  wrote back to the same volume and box, within the 5.0e-4 cm^3 the rest of this
  script uses; and that the centroid does not shift — a tapered face that came
  back at a different angle would show up there and nowhere else. It matters
  more than its size suggests, because promotion is offered as a safe thing to
  do to a part nobody described, and a drift of a rounding step would silently
  become the baseline every later DFM round is compared against.
- **the enum fallback table** — compares every entry against Inventor's own type
  library and prints a corrected block to paste in.

Run them in that order if time is short: the parameter edit is the premise of
the project, the hole styles are the newest untested code, and the rest have
never had a chance to be wrong yet.

```powershell
python scripts\live_acceptance.py                    # check
python scripts\live_acceptance.py --record           # reseed expectations
python scripts\live_acceptance.py --only bracket     # one example
```

It exits non-zero on any failure, so it can gate a change rather than being
read. Two examples have no captured volume yet; the first run seeds them, and a
seeded number is only as good as the arithmetic behind it — check it before
trusting it, or it becomes a regression test for whatever it happened to build.
`cover_plate` is the exception: its volume was derived by hand and written down
before any live run, so its first run is a real check.

`--backend mock` runs the same script without Inventor. Most checks skip, but the
example loop still prints how far the simulator's volume is from Inventor's
recorded one, which is the cheapest measure of how good the rehearsal oracle is.
It reads 0.0013 cm^3 on the angle bracket and 0.0469 on the flanged shaft.
