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
the pulley at 67.909 cm³, the elbow at 22.207 by Pappus, the duct at 186.460,
the boss at 16.201.

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
  once and hand it to both entities. That covers standalone sketch points as
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

## Known-shaky areas

These are the parts of the COM backend most likely to need adjustment, and why:

- **`sweep`** uses `Profiles.AddForSurface` on the first path entity. A multi-segment
  path may need the whole sketch passed instead.
- **`shell`** uses `CreateShellDefinition`. Older releases expose a direct
  `ShellFeatures.Add(faces, thickness, direction)` instead.
- **`thread`** creates a definition from the first selected face only.
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
