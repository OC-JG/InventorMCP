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
| `flanged_shaft.json` | Offset work plane, stacked extrusions, bolt circle, blind holes |
| `enclosure_base.json` | Shell with a removed face, cut extrude on a second plane |
| `angle_bracket.json` | Polyline profile, symmetric extrude, slot, mirror, sketch on XZ |

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
"Known-shaky areas" below.

`enclosure_base.json` builds, adding a shell, an offset work plane and a sketch
on it. `angle_bracket.json` builds, adding the polyline profile, a symmetric
extrude, a slot, a mirror and a sketch on XZ.

Not yet exercised against Inventor: revolve, sweep, loft, patterns and threads.
Those live in the remaining example — run it and report what breaks.

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
  created, then rejects an explicit duplicate as invalid. Build chained curves
  from the previous curve's `SketchPoint` instead.
* `PlanarSketch.OriginPoint` cannot be constrained against. Project the origin
  work point into the sketch first.
* A midpoint constraint moves the *point* onto the line, so the grounded sketch
  origin can never be that point.
* **A cut that meets no material still reports success.** Inventor builds the
  feature, changes nothing, and returns it. Two of the angle bracket's three
  geometry bugs hid behind an `ok` line that way. Cut extrudes and holes now
  compare the volume before and after and refuse to pass off a no-op as a
  feature; a hole that finds nothing is retried the other way first.
* **The XZ plane's first axis runs along model -X.** A profile drawn from 0 to 90
  in sketch X came out spanning -90 to 0, which silently put a second sketch's
  features on the wrong side of the part. A recipe's coordinates mean the axes
  the plane is named after, on every plane alike, so the COM backend reflects
  the plan's first axis on the way in (`SketchPlan.mirrored_u`) and the geometry
  lands where it was asked for. `_MIRRORED_PLANES` in the COM backend is the
  list; add to it if another release orients a plane differently.

## Known-shaky areas

These are the parts of the COM backend most likely to need adjustment, and why:

- **`sweep`** uses `Profiles.AddForSurface` on the first path entity. A multi-segment
  path may need the whole sketch passed instead.
- **`shell`** uses `CreateShellDefinition`. Older releases expose a direct
  `ShellFeatures.Add(faces, thickness, direction)` instead.
- **`thread`** creates a definition from the first selected face only.
- **Hole styles** other than plain drilled (counterbore, countersink, tapped) are
  carried through the recipe and reported, but the COM path currently creates a
  drilled hole. The recipe records the intent, so upgrading this does not change
  any recipe.
- **Edge convexity** is decided from the boundary loops: a face's boundary runs
  anticlockwise about its outward normal, so the material lies to the left of
  the loop, and whether that direction faces into the neighbouring face's normal
  is the answer. That needs `Edge.EdgeUses` and `EdgeUse.IsParamReversed`, for
  which makepy generates no module on 2027.1 — late binding asks the object
  rather than the wrapper, so it may work anyway. Where it does not, the answer
  falls back to sampling a point on each face (`Face.PointOnFace`), and that
  point is arbitrary: on a face with an inner loop — the top of a plate with a
  slot through it — it can land on the far side of the edge and flip the answer.
  Six of the bracket's eight slot-opening edges came back `convex` and two
  `concave`, for geometry identical by symmetry. Run
  `scripts/probe_convexity.py` to see which path this machine takes; while it is
  the fallback, narrow a `concave`/`convex` selector with `min_length` or `near`.
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
