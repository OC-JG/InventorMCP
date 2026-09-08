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
six runs it took. **The whole drawing surface, added 2026-09-07, is what is left
of this section.**

The other three that arrived with it have gone: `thicken` on 2026-09-07,
`move_face` and `sketch_driven_pattern` on 2026-09-08. They started a step worse
off than the work geometry had been -- those five had signatures somebody had
read off a type library, and these three did not -- and what closed the gap was
`ITypeInfo` on the live objects, which answers for a class the type library does
not publish at all. Their subsections below are records now rather than
warnings, and they are kept because what each one got wrong before the read is
the argument for reading.

The drawing surface stays, and it is much the largest: four calls rather than
one, one fact it rests on has still never been asked of Inventor, and **three
runs have not got past creating the document** -- a bare filename where a path
was wanted, then a template from an older release wanting migration. Neither
failure was in the call.

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


### `move_face`, measured argument by argument, then measured against a part

Added 2026-09-07 in a session with no Inventor to reach, **run against Inventor
2027.1 the same day, where it failed**, taken apart over two probe runs the day
after, and then **built and measured on the first run that reached it**:

| fixture | derived | Inventor | |
|---|---|---|---|
| `lifted_face` | +6.4000 cm^3 | **+6.4000** | 0.0% |
| `lifted_face`, `lift` doubled | +12.8000 cm^3 | **+12.8000** | 0.0% |
| `widened_wall` | +0.2400 cm^3 | **+0.2400** | 0.0% |
| `widened_wall`, `grow` doubled | +0.4800 cm^3 | **+0.4800** | 0.0% |

`PREDICTED["move_face"]` came down 0.50 -> 0.02 on the strength of it, and the
doubling rows are the reading a volume alone cannot give: **the distance
expression reaches Inventor's own dimension**, so the feature is parametric in
fact and not in name. That is defect 11's lesson, which cost four runs to learn
the first time and one fixture to check here.

Four attempts and three failures to get there, none of them about arithmetic.
The failures were the useful kind: each eliminated something the code had
guessed. What follows is that record, kept because what a guess got wrong is
the argument for reading.

**And two of the checks around that call came off Autodesk's published pages
rather than a probe**, read the same day. `MoveFaceDefinition
_SetDirectionAndDistanceMoveType.htm` gives the same signature the probe read,
distance first included -- two independent sources for the one argument order
every earlier version of this code had backwards -- and adds what a probe
cannot: the Direction is a `WorkAxis`, a linear `Edge` or a planar `Face`, so a
sketch line is refused with the reason before Inventor is asked. And
`MoveFaceDefinition.htm` says a fresh definition starts at `kFreeMoveType`,
which is why the type is read back before `Add`: a setter accepted without
changing it would build a move defined by nothing, and that is the failure mode
that builds and is wrong.

**What the type library says** (`python scripts/com_signatures.py --search
MoveFace`):

* `MoveFaceFeatures.Add(Definition)` -- one argument, a definition object. So
  there is no direct-arguments route at all, which the backend had not assumed
  but had left room for.
* `MoveFaceDefinition` exists, and among its properties are **`MoveFaceType`**
  and **`MoveFaceTypeDefinition`**.

**What the run said**: *"Nothing on this release's `MoveFaceDefinition` would
take a direction and a distance."* All three candidates --
`SetDirectionAndDistance`, `SetDirectionMove`, `SetDirectionAndDistanceMoveData`
-- are absent. The backend named every attempt and its error rather than
guessing again, which is what that list was for.

**The type library would not answer the follow-up, and the live object did.**
`--search MoveFaceType` comes back *"Nothing in the type library mentions
'MoveFaceType'"* -- the properties are named on `MoveFaceDefinition`, and the
classes they return are not published as classes the search can reach. So
`scripts/probe_definitions.py` asked the object instead, on 2026-09-08, and
`ITypeInfo` gave the whole interface:

| member | kind | arguments |
|---|---|---|
| `Faces` | get/put | a `FaceCollection` |
| `MoveFaceType` | **get only** | 91395 on a fresh definition |
| `MoveFaceTypeDefinition` | get only | **`None`** on a fresh definition |
| `SetDirectionAndDistanceMoveType` | method | **`(Distance, Direction, DirectionReversed)`** |
| `SetPlanarMoveType` | method | `(PointOne, PointTwo, Plane)`, the last optional |
| `SetFreeMoveType` | method | `(Transformation)` |
| `AutomaticBlending` | get/put | `True` by default |
| `Copy` | method | 0 |

Four things fall out of that table.

**The setter is a fourth spelling.** `SetDirectionAndDistanceMoveType`, which
none of the three guesses came near. They were reasonable, narrow, and
unanimously wrong -- which is what one live read cost nothing to settle, and is
the whole argument of this file in one line.

**The type is not assigned, it is implied.** `MoveFaceType` is read-only and
`MoveFaceTypeDefinition` is `None` until a setter has been called, so calling
one of the three *makes* the definition that kind. The earlier design -- set a
`MoveFaceType`, then fill in the child object -- was reaching for a shape this
API does not have, and deliberately not setting the type turned out to be right
for a reason nobody had.

**The distance comes first.** `(Distance, Direction, DirectionReversed)`, not
the direction-then-distance every version of this code assumed. A swap raises
rather than building something wrong -- the distance is an expression string
and the direction is a COM object -- but "it would have raised" is a poor
substitute for knowing, so the order lives in
`_MOVE_FACE_SETTER_ARGUMENTS` as data, `tests/test_move_face.py` pins it, and
`_check_move_face_arguments` compares it against what the live object reports
before every call. A measurement stated in code and never checked against the
thing measured is the drift this repository writes tests about; here the thing
measured can simply be asked.

**And `DirectionReversed` is what `flip` was waiting for.** It used to go in as
`-(expression)`, because no reversal property had been read. Now it goes in as
the boolean the API provides and the distance reaches Inventor exactly as the
caller wrote it -- which is the whole point of carrying expressions rather than
numbers. The feature detail reports `flip_via` so a part built either way says
which mechanism carried it.

The two setters that were excluded are measured to be what the exclusion
assumed: `SetPlanarMoveType(PointOne, PointTwo, Plane)` is point-to-point and
`SetFreeMoveType(Transformation)` takes a matrix. Neither could have taken a
direction and a distance by accident, so keeping the candidate list narrow was
right -- and now provably rather than presumably.

**And then it built.** Every argument of every call in this operation was
measured before a part was ever made from it, which is the order this file
argues for -- one probe run costs a second and a failed build costs a session.

    python scripts/probe_definitions.py

It asks the objects themselves what they offer, because the type library will
not say. The reading that matters is **`ITypeInfo`**: makepy generates a module
per type library, so an object whose class the library does not publish has no
wrapper to read -- but the object still answers `GetTypeInfo`, and that names
its members, separates a property read from a property write, and says how many
arguments each takes. `dir()` is printed beside it, and again through dynamic
dispatch, because a makepy wrapper's `dir()` reflects a declaration rather than
the object -- which is how a live shell once came back describing nothing but
`HealthStatus`. It does the same for `SketchDrivenPatternFeatures` (below, the
same shape of failure) and lists the `.idw` templates actually installed while
it is there. It needs the Windows machine; on any other platform it exits with
`BackendUnavailableError` and touches nothing.

**A probe has to run *on* the apartment, and getting that wrong costs a
session.** The first version of this one reached into the backend for `app` and
the document and poked them from the calling thread. Inventor's API is
apartment-threaded, the backend is pinned to one apartment by
`backend/com/marshal.py`, and a live COM object cannot leave the thread that
made it: `document.ComponentDefinition` answered "the application called an
interface that was marshalled for a different thread", and `app.FileManager`
answered `AttributeError: <unknown>.FileManager` -- which reads exactly like a
property 2027.1 does not have. It has it, and that is the worse of the two
symptoms for the obvious reason. `scripts/apartment.py` holds the two helpers
for doing it properly; `describe_feature` in `backend/base.py` is the same
lesson on the server side, and it was written down before this happened.

One thing from the earlier design was still open until the run: **the distance
has to go in as an expression string.** Every length in this server reaches
Inventor as an expression so the dimension keeps its parameter, and a setter
that insisted on a number would have taken that away silently -- the signature
says `Distance` and says nothing about what it accepts. Doubling `lift` doubling
the volume is what settles it: the expression is in the dimension.

The fixtures, and what each of them was shaped to catch: `check_move_face` builds
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
extrude's 0.02 once a run agrees, rather than to something in between --
`thicken`, in the next subsection, is what that looks like once it has happened.


### `thicken`, measured: the side, the corners and one accidental argument

Written 2026-09-07 with no Inventor to reach, and **measured against Inventor
2027.1 the same day**. It is the one of the four Phase 3 surfaces that came out
of that run working, so this subsection is a record rather than a warning. What
it was worried about was a side and an argument order; both are now answered,
and the answers were not quite what the code assumed.

**The signature, read rather than guessed:**

    ThickenFeatures.Add(Faces, Distance, ExtentDirection, Operation,
                        [AutomaticFaceChain], [CreateVerticalSurfaces],
                        [AutomaticBlending])

Two corrections fell out of that one line.

Autodesk's published page for `ThickenFeatures.Add`, read the day after
(2026-09-08), gives that signature exactly -- arguments, optionality and the
absent definition object -- so the measurement has a second source behind it.
It is the only Phase 3 surface where the type library, the reference and a
built part all agree.

* **`CreateThickenDefinition` does not exist.** `ThickenFeatures` offers `Add`
  and nothing else. The backend used to try a definition route first, on the
  reasoning that a definition's properties are *named* and so cannot be filled
  in the wrong order -- sound reasoning about a method this release has never
  had. It is gone, and with it the attempt list and the `factor of four` result
  guard that existed because a misordered variant-and-two-enums need not raise.
  One measured call needs no guard: a permutation is not possible when the
  argument names are at the call site, which is what `_call_named` puts there.
* **There is no `IsOffset` argument, and slot 4 is `AutomaticFaceChain`.** The
  old call passed `False` there for the offset mode's sake -- the offset mode
  produces a surface body and nothing in this server can hold one. `False` is
  also the right value for `AutomaticFaceChain`, because chaining extends the
  selection to tangent-connected faces and a recipe that named four walls meant
  four. So the call worked, for a reason that was not the reason given. **A
  value passed for a wrong reason that happens to be right is not a
  measurement**, and only reading the signature told the two apart. The comment
  at the call says so, since the next person to see `False` there will want to
  know which it is.

**The side is confirmed.** `examples/calibration/thinned_wall.json` -- one wall
of a plate thinned 1 mm from behind -- removed **-0.2400 cm^3** against -0.2400
derived, and left the plate 79 mm wide. Three outcomes were distinguishable and
this was the one that says `THICKEN_SHARE` in `backend/com/backend.py`'s shared
table has it right: a face's normal points out of the solid, so a `negative`
layer lies *behind* the face where the material is, and cutting it removes that
much. (`0.0000` would have said Inventor puts a `negative` layer outside the
solid, so there was nothing to cut; a positive figure, something else again.)
This is the reading that mattered. Defect 5's `trim` kept the wrong half of a
part for as long as the feature existed and one of the runs that found it was
1.2% apart -- inside every tolerance, because the volume was right for the half
it kept. A tolerance cannot catch a side. Different numbers can.

**The corners close, and the simulator was 1.7% low.**
`examples/calibration/thickened_walls.json` -- four walls grown 1 mm outward --
came back at **+1.4640 cm^3** where the sum of the four layers is 1.4400. The
layers do not meet: there is a 1 x 1 x 6 mm notch at each of the four corners
belonging to no wall, 4 x 6 mm^3 = 0.0240, and 1.4400 + 0.0240 is 1.4640
exactly. **Inventor fills them.**

That was reported rather than asserted, precisely so the run could answer it,
and the answer changed the simulator: `_thicken_corners` in the mock adds
`(share x t)^2 x h` for each pair of selected faces whose normals are
perpendicular, `h` being how far the part runs along the edge they share. Both
fixtures now agree with Inventor to four decimals, and `PREDICTED["thicken"]`
came down from the placeholder 0.50 to **0.02** -- an extrude's tolerance,
because what is left is exact prism arithmetic.

The sign is `+` in both directions, which looks wrong and is not: growing four
walls leaves gaps to fill, so the union is the sum of the layers *plus* the
notches; thinning four walls makes the layers *overlap*, so the union is the sum
*minus* the overlap -- and the change being negative, subtracting less means
adding. What the term is still approximate about is adjacency: any two
perpendicular selected faces are counted, which is exactly the edges of a convex
box and one edge too many on the inside of an L. That is a smaller error than
ignoring corners was.

**One thing the run could not answer, because it is not about Inventor.** The
half of Inventor's Thicken that turns a *surface* into a wall is unreachable
here, and not because of the schema: no operation in this server creates a
surface, so the only surface a part could hold is one that arrived through
`import_geometry`. Thickening that would work today.
`docs/FEATURE_COVERAGE.md` records it under Tier 1c rather than as a gap in this
file, since it is a fact about this server.


### `sketch_driven_pattern`, measured, with the count still open

The third and the lowest-risk of the three, and worth reading for what it is
*not* worried about as much as for what it is. **Built and measured on
2026-09-08 at -1.2000 cm^3 exactly** -- and that was never the interesting part.
The occurrence count still is, and the run did not settle it; the last
subsection here says why not, and it is a limitation of the check rather than a
finding.

**The first run rejected the call's shape, not its arguments.** On Inventor
2027.1, 2026-09-07, Inventor's wrapper answered *"Add() takes from 1 to 2
positional arguments but 5 were given"*. The measured signature is
**`SketchDrivenPatternFeatures.Add(Definition)`** -- one object, the same shape
`move_face` turned out to have -- so the three-argument call through `_patterned`
could never have worked here, and `_patterned` is not what builds it.

**Where the definition comes from was measured the next day, by asking the live
object rather than the library.** `--search SketchDrivenPattern` publishes `Add`
and nothing else -- no factory, no definition class -- but the object's own
`ITypeInfo` lists:

    CreateDefinition(ParentFeatures, Sketch, BasePoint, ReferenceFaces)
    Add(Definition)

with the last two optional -- the names coming from the same `GetNames` call
that gives the arity, which the probe was discarding until it was fixed to read
past `[0]`. `BasePoint` is supplied and `ReferenceFaces` is not: a recipe always
names a point and a centroid is not something the simulator has, so a default
that used one could not be rehearsed, while nothing in a recipe says reference
faces at all. The definition also carries a settable **`ComputeType`** (default
47361), which is where the `kAdjustToModelCompute` measurement now goes.

That call is what the backend makes, named through `_call_named`. **The COM half
of this operation is measured**; what is left is what the part comes out as,
below.

**Its arguments cannot be silently misordered**, which is why trying a factory's
arguments is safe where guessing `thicken`'s were not. A feature collection, a
sketch and a sketch point are three different COM types, so a wrong order is a
type mismatch rather than a part built wrongly -- `thicken`'s
variant-and-two-enums is the opposite case, and it had a factor-of-four result
guard for exactly that reason until its signature was read.

**The compute type still applies.** Measured on 2027.1: patterning a hole fails
outright until the compute type is `kAdjustToModelCompute`, because identical
compute copies faces and a blind hole's second occurrence has nothing to remove
until the boss beneath it exists. It is set on the definition rather than passed
as an argument now, and a release without the property gets its own default with
the detail saying so.

**And the published page names that same call**, read 2026-09-08 alongside the
probe: `CreateDefinition(ParentFeatures As ObjectCollection, Sketch As Object,
[BasePoint] As Variant, [ReferenceFaces] As Variant)` and `Add(Definition As
SketchDrivenPatternDefinition)`, with `ComputeType` listed on the definition.
Two sources reached the same interface independently, one by reading Autodesk's
reference and one by asking the object -- for a class the type library does not
publish at all, which is the case where a single source is least comfortable.

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

    python scripts/live_acceptance.py --only sketch-driven-pattern

**The 2026-09-08 run gave the volume and not the count**, and the fixture's own
design is what predicted that: -1.2000 exactly, with the finished part reading
`Plate`, `Slot`, `Spread`. A sketch-driven pattern is **one feature holding its
occurrences**, so counting features cannot count occurrences -- the check prints
the feature list, which distinguishes nothing between the first two rows.

That is this check's limitation rather than a finding about Inventor, and the
note it prints says where the answer is instead: open the pattern in Inventor's
browser and count, or read `feature.Occurrences.Count`, which nothing here has
read. If it turns out to be the middle row, two things change together: this
section, and the `elsewhere` filter in the mock's `sketch_driven_pattern` that
excludes the reference.

**One thing this run cannot check, because it is the simulator's own.** The mock
*places* the occurrences -- it is the only pattern here that does -- so a cut
through where an occurrence went is measured against what the pattern left, and
an occurrence of a cutting seed standing over air is reported. Both are held by
`tests/test_sketch_driven_pattern.py`, and neither needs Inventor: they are
claims about the ledger, not about the API. What Inventor decides is only how
many occurrences there are and where -- and if the count is wrong, everything
the placement then says is wrong with it.


### Drawings, the last unmeasured surface in the project

Added 2026-09-07. Four `Backend` methods -- `new_drawing`, `place_view`,
`retrieve_dimensions`, `read_drawing` -- implemented on both backends, with the
simulator's half measured and tested and the COM half never executed. It is the
only one of the four 2026-09-07 surfaces still here, and what makes it different
from the three that have gone is not only its size.

**One of the four was said to carry no risk at all, and it is what three runs
have now failed on.** The claim was that `new_drawing` is `new_part` with a
different enum: `Documents.Add` is measured, `kDrawingDocumentObject` has been
in the constants table since before anything used it, so if the rest failed that
call would not be why. Three times the rest never got the chance -- *"Creating
the drawing document failed: Exception occurred."*

Both causes were in what the call was **given**, and the enum and the method
were fine every time. That is the lesson worth keeping from this: "carries no
risk at all" was a claim about a call, and a call is its arguments too.

**Second cause first, because it is the more interesting one: the template
wanted migrating.** With a resolved path to a real `ISO.idw`, `Documents.Add`
still answered a bare "Exception occurred" -- Inventor's least helpful failure,
and the one this project has spent the most effort learning not to pass on. The
template dates from an older release, and interactively that is a migration
dialog; through the API it is silence. Migrating a file is opening it and saving
it, so `_drawing_from` does exactly that on a failure and retries the `Add`
once.

Three things about the shape of that, because it writes to somebody else's file.
It happens **on failure rather than on the way past** -- a template is a file
somebody else owns, here a company one on a shared drive, and rewriting it is
not a side effect to have while creating a drawing. It saves **only if Inventor
marks the document dirty**, so a template that was already current is left
exactly as it was. And `template_migrated` in the result detail says which
happened, so a run that modified a shared file says so rather than being
silently helpful. The retry is once: if a migrated template still will not make
a drawing then migration was not the reason, and a loop would turn one bare
"Exception occurred" into several.

**First cause: the template was a bare name.** `Documents.Add` takes a *path*,
the shipped drawing recipe says `"ISO.idw"`, and a bare filename is not a path. So
Inventor refused and named nothing, which is the error this file has spent the
most effort learning not to produce. `_drawing_template` now resolves a bare
name against the folders Inventor itself uses and their immediate subfolders --
a name is what somebody means -- and where it still cannot find one it refuses
with every path it tried.

Which folders was wrong on the first attempt, and measured on 2026-09-08:
`FileManager.TemplatesPath` does not exist on 2027.1. It raises
`AttributeError: <unknown>.TemplatesPath` from the same object that answers
`GetTemplateFile` -- a real absence, wearing the same clothes as the
apartment-threading artefact above. Inventor keeps those paths on the
*project*, and the strongest source needs no unread property at all: **the
folder `GetTemplateFile` returns its answer from**, which on this machine is
`G:\...\oc-berlioz\Templates\Standard.dwg` -- a Shared-drive project folder
rather than the Inventor install, and a .dwg rather than an .idw. A project can
put its templates anywhere, so the folder is asked for rather than assumed. **Unverified**: the fix has not
been run, and it is the first thing the next run reaches.

The lesson is not about templates. Two calls in that sentence were measured and
the argument between them was not, and "carries no risk at all" was a claim
about a call rather than about a call *and its arguments*.

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


### One CAD session, in order

Four surfaces were written in one session with no Inventor to reach:
`move_face`, `thicken`, `sketch_driven_pattern` and the whole drawing layer.
Each has its own subsection above saying what it rests on. This is the order to
take them in, because a CAD seat is the scarce thing and the work axis is what
happens otherwise -- **six sessions, four defects, none of them the thing being
measured.**

**Read the signatures first.** Each of these answers in a second what a run
narrows down over several, and three of the four calls were written without a
signature in front of anybody:

    python scripts/com_signatures.py --search MoveFace
    python scripts/com_signatures.py ThickenFeatures
    python scripts/com_signatures.py SketchDrivenPatternFeatures
    python scripts/com_signatures.py GeneralDimension
    python scripts/com_signatures.py DrawingDimensions

**All five were read on 2026-09-07**, and between them they turned one of the
four surfaces into a working one and told the other three what they were
actually up against. `ThickenFeatures` answered completely. `MoveFace` and
`SketchDrivenPattern` answered *"the argument is a Definition"* and then stopped
-- neither definition's class is published, so `--search MoveFaceType` finds
nothing at all. `GeneralDimension` and `DrawingDimensions` have no generated
module to read, which is not the same as their being absent: makepy generates
what a document has needed, and no drawing document had been opened.

**So the reading that is next is a live probe rather than another signature:**

    python scripts/probe_definitions.py

It asks the objects themselves, through their own `ITypeInfo` -- which names the
members of a class the type library does not publish, and is the reading
`com_signatures.py` cannot make. `MoveFaceFeatures`, `MoveFaceDefinition`,
whatever `MoveFaceTypeDefinition` returns, and `SketchDrivenPatternFeatures`
and its definition; plus `FileManager`'s template paths and the `.idw` files
actually installed, which is what the drawing failure turned on. Both backends'
refusals now carry a `dir()` listing of their own, so a run that fails answers
the question too.

**Then one run:**

    python scripts/live_acceptance.py --only unmeasured

which expands to the four groups in the order above and prints the list again.

**Two answers would change a design rather than fix a call**, and they are worth
looking for before anything else in the output:

1. **Can a retrieved `DrawingDimension` name the model parameter it came from?**
   If not, retrieve-and-filter cannot work and dimensioning goes back to placing
   against `DrawingCurve` geometry -- a different and much larger piece of work.
   Still open: the first run did not reach a dimension, because the drawing
   document did not open.
2. ~~**Does `thicken`'s `negative` mean the side `THICKEN_SHARE` says?**~~
   **Answered on 2026-09-07: yes.** `thinned_wall` removed -0.2400 cm^3 against
   -0.2400 derived, so a `negative` layer lies behind the face where the
   material is. This was the reading worth shaping fixtures around -- a
   tolerance cannot catch being wrong about a side; defect 5's `trim` was 1.2%
   out while keeping the opposite half of the part -- and `thinned_wall` was
   shaped so the three possible answers were three different numbers. It came
   back the first one.

**And three checks the simulator can never be evidence for**, so their first
real reading is this run:

* a view's **extent** against the part -- in the simulator the extent is
  computed *from* the part, so the check compares it with itself;
* whether a view **direction's name describes what you get** -- defect 4 on a
  different API, where `capture_view`'s `front` returns a top view;
* whether Inventor's **first and third angle** are the ones implemented here,
  which only a *projected* view can answer, because nothing asserted its
  direction.

**What cannot be run anywhere but that machine.** Worth stating plainly, because
it is the reason all of the above is still outstanding: the COM backend is the
only route to a real Inventor, `pywin32` exists only on Windows, and
`--backend auto` resolves to the simulator wherever it does not. A session on
another platform can write this code, test the half that needs no CAD, and get
no further -- which is exactly how these four surfaces came to be written
unmeasured, and is not a defect in any of them.

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
