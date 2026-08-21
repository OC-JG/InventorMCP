# Changelog

Notable changes, newest first. Dates are when the work landed, not a release.

## Unreleased

### Added
- Every non-sketch operation returns a `measured` block — volume, its change,
  face and edge counts, and the bounding span when it moves — so the model can
  tell that its last step did nothing. Inventor reports success for operations
  that changed nothing at all, and this is how you see it.
- Polyline profiles now carry driving dimensions, so an L-section is revisable.
  Previously they carried constraints only and their parameters drove nothing.
- Dimensions have a soft-failure path: those the planner adds to remove a
  degree of freedom are optional, and Inventor refusing one no longer kills the
  whole sketch. The recipe's own dimensions stay required and go first.
- A sketch plane's axes are measured (`SketchToModelSpace`) rather than assumed,
  so a recipe's `x` means model X on every plane. Any signed permutation is
  handled, which covers offset work planes and axis-aligned faces for free.
- Edge convexity is decided from the boundary loops, which is exact, rather than
  from a sampled point on each face, which a face with a hole in it can fool.
- Every Inventor call runs on one dedicated thread. The API is
  apartment-threaded and `CoInitialize` is per thread, while the MCP SDK runs
  synchronous tools on a pool of workers.
- `skills/inventor-parametric-modelling` — the hard-won knowledge as an Agent
  Skill, where a model reads it, with tests keeping its claims true.
- `scripts/probe_convexity.py`, `scripts/probe_hole.py`,
  `scripts/dump_constants.py` — diagnostics that measure rather than guess.
- `--edit NAME=VALUE` on `scripts/live_smoke.py`: change a parameter, rebuild,
  and report what moved. A parameter that moves nothing is reported as a
  failure, since that is the whole premise.
- CI for the offline half, on Linux and Windows.
- A `LICENSE` file, which `pyproject.toml` had been claiming for a while.

- Counts are parametric. `count`, `count1`, `count2`, `sides`, `rows` and
  `columns` accept an expression, so a pattern's count can be driven by a
  parameter the way its spacing already was. A fractional result is refused
  rather than rounded.
- `validate_recipe` rehearses the build in the simulator instead of only
  checking the schema, and warns about a cut whose profile misses the part or a
  parameter that drives nothing.
- Four recipes for the operations no shipped example reached: `belt_pulley`
  (revolve, circular pattern), `pipe_bend` (sweep), `duct_transition` (loft),
  `threaded_boss` (thread, rectangular pattern).
- `skills/.../references/standard-parts.md` — hex nut, washer and standoff
  templates, with the across-flats trap and the DIN/ISO disagreement recorded.
- Hole styles are built rather than recorded. `counterbore`, `spotface`,
  `countersink` and `tap` reach Inventor's own hole methods — eight of them, one
  per style and extent — through a dispatch that can be tested offline against a
  recorder instead of discovered on a live machine. The finished feature's
  `HoleType` is read back, and a style Inventor does not confirm is refused
  rather than reported, because a wrong argument order can still build and would
  otherwise pass as a counterbore.
- `bottom_angle` now reaches the model, and defaults to nothing: a blind hole
  gets Inventor's own flat bottom unless a drill point is asked for. It used to
  default to 118° and be dropped, which is the worst of both.
- `examples/cover_plate.json` — counterbored and countersunk holes, with its
  volume derived by hand and written to `examples/expected/` *before* any live
  run, so the first run checks the geometry rather than recording it.
- **The simulator is used as an oracle for the live build.** Now that it
  predicts an extruded part to within a rounding error, `build_part_from_recipe`
  rehearses first and reports any operation whose live volume change disagrees
  with the prediction. Deltas are compared rather than totals, so one wrong
  operation does not flag every one after it, and each kind of operation gets
  the tolerance its model deserves -- two percent for a prism or a hole, thirty
  for a fillet's corner estimate, nothing at all for a thread. A fillet on the
  wrong edge, a cut on the wrong side and a hole that met no material each
  shipped here at least once; all three announce themselves this way.
- A worked drawing pair: `examples/drawings/cover_plate.json` is a full reading
  of a sheet and `examples/cover_plate.json` is the recipe that satisfies it,
  checked by the test suite so the documentation cannot drift from what works.
- An escape hatch, off unless the machine's owner turns it on.
  `INVENTOR_MCP_ESCAPE_HATCH=on` registers `run_inventor_script`, which runs
  Python against the live API for what a recipe has no words for -- sheet metal,
  iLogic, drawing views. Without the variable the tool is not registered, so the
  model cannot see that it exists: a tool that is absent cannot be talked into
  being used, which a tool that is present and refusing can. There is no sandbox
  and no pretence of one; a script that raises is rolled back by default and
  every call is logged with the code it ran.
- Opt-in rollback. `rollback_on_error` on `build_part_from_recipe` and
  `apply_operations` wraps the work in one of Inventor's own transactions and
  aborts it if anything fails. Off by default, because a half-built part is the
  best evidence there is about what went wrong — three of the geometry bugs
  fixed here were found by looking at one. What makes it worth having is the
  failure that cannot be recovered otherwise: a hole consumes its sketch, so
  without a transaction there is nothing left to retry with. The simulator
  implements it exactly, by copying the document aside, so the path is tested
  rather than assumed.
- `scripts/probe_hole_styles.py` — one hole of every style through one block, with the
  method called, the enum read back, the volume removed against what the
  geometry says, and which thread tables `CreateTapInfo` will accept.

### Measured on Inventor 2027.1
- Every hole style now builds to its predicted volume exactly: counterbore
  0.8120, spotface 0.5774, countersink 0.5611, pointed blind 0.2279, all to four
  decimal places against geometry worked out by hand. The seat depths that
  looked wrong were the probe overlapping its own cases.
- **A tapped hole is cut to the thread's minor diameter**, `D - 1.0825 x pitch`
  for ISO metric: 6.6469 mm for M8x1.25, from the removed volume to four decimal
  places. Not the 6.75 mm tapping drill.
- **A tap designation must carry its pitch**: `M8x1.25` accepted, `M8` refused.
  Metric and unified tables work; NPT and BSP were refused.
- **`ThreadFeatures` has no `CreateThreadDefinition`.** Its only method is
  `Add(Face, StartEdge, ThreadInfo, ...)` and nothing named for threads creates
  a `ThreadInfo`, so the `thread` operation cannot work yet. Use a `hole` with
  `tap`, which is measured and does.
- **There is no `HealthStatusEnum` in the type library**, so a feature's health
  cannot be translated by name. 11778 is treated as healthy because seven
  just-built, individually verified features all reported it -- evidence rather
  than a table entry.

### Fixed
- **A two-axis rectangular pattern put the compute type in the spacing type's
  slot.** The measured signature has `XSpacingType` and `XDirectionStartPoint`
  *between* the two axes, so `kAdjustToModelCompute` at index 5 shifted every
  argument after it and the second axis landed in `XDirectionStartPoint`. That
  was the bare "Exception occurred" with nothing in Inventor's error manager.
  Optional slots in the middle of a signature are now left to the wrapper's own
  defaults via named arguments, rather than filled with a guess.
- `threaded_boss` used the `thread` operation, which cannot work on 2027.1. It
  now does what it was always describing -- a real tapped hole, cut to the M12
  minor diameter -- and patterns the boss *and* its hole along the plate, so the
  result stays one solid. Hand-derived at 70.855424 cm^3 and confirmed to six
  figures.
- A recipe using an operation known not to work is warned about by
  `validate_recipe` before it is built, with the route that does work. The
  alternative is a live run failing on something already known.
- A build failure in `live_acceptance.py` dropped the exception's hint, which is
  where the diagnosis lives -- the sweep reported that it could not make a path
  and said nothing about the two routes it had tried.
- **A hole's properties are on `HoleFeature.Definition`, not on the feature.**
  So the style read-back returned nothing for every hole, `verify` answered
  "cannot tell", and a run reported eight verified styles having verified none of
  them -- with a failure message that said "the style read back correctly"
  because that string sat beside a check that never ran. Both are fixed: the
  properties are read from the definition, and the message no longer asserts
  something it has not established.
- **The hole-style probe was measuring its own layout.** It put seven cases
  7.5 mm apart in a 60 mm block, and a 16 mm spotface spans 8 mm either side, so
  every seat overlapped its neighbours and removed less material than an isolated
  one -- by amounts that scaled with seat diameter, which is what finally gave it
  away. The block is 160 mm now and the script refuses to run if the spacing
  could let two cases meet.
- **A blind hole's depth is measured to the shoulder, and the drill point goes
  beyond it.** The expectation had the tip *inside* the depth, so a pointed hole
  was predicted to remove less than a flat-bottomed one when it removes more.
  Inventor gives 0.2279 cm^3 where cylinder-plus-cone predicts 0.227884.
- **Thirty-two of the fifty-one enum values in the fallback table were wrong**,
  measured against Inventor 2027.1. Most were not slightly wrong but from another
  numbering family: `kDrilledHole` is 21505, not 39169. One was the quiet kind of
  dangerous -- the table's `kThroughAllExtent` (20740) is Inventor's real
  `kToNextExtent`, so a through-all extrude would have stopped at the next face
  and built a part nothing reports as wrong. None of it ever fired, because the
  type library has been readable on every machine this has run on, which is luck
  rather than design. The disputes recorded against another project's field notes
  are settled: they were right about the dimension orientations.
- **A correct rebuild was reported as three features in error.** The
  "healthy" `HealthStatusEnum` values were hard-coded as `{0, 15873}`, and 15873
  is `kPartEdgeFilter` -- a number from a different enum. The bracket widened
  from 90 to 120 mm and gained exactly the 9 cm^3 of base that implies, while the
  report called it sick. The value is now asked of Inventor by name, and a status
  that cannot be translated is reported as *uninterpreted* rather than as an
  error: a number nobody can read is not evidence of anything.
- The threading check asked a part with no solid body for its mass properties,
  which Inventor refuses -- so it failed on its own empty document and blamed the
  marshalling. It builds a block first.
- `live_acceptance.py` printed a failure's explanation under passing checks too,
  so "the backend is pinned to one thread" was followed by "it is not".
- **CI had never once passed.** Every run since it was added failed at
  `pip install -e ".[dev]"`, on all three Pythons and on Windows, because
  `license = { text = "MIT" }` is rejected outright by setuptools 77 and later
  when `license-files` is also given -- PEP 639 wants an SPDX string. So the
  offline suite the workflow exists to run had never been run by it, and nobody
  had read the logs. Fixed, and verified by installing into a fresh virtual
  environment exactly as the workflow does before pushing.
- Holes drilled the wrong way and removed nothing. Inventor's extent enum runs
  opposite to an extrude's for a hole; the side is now chosen from where the
  material is, before the feature is built, because a hole consumes its sketch
  and cannot be rebuilt the other way round.
- A cut or hole that meets no material is no longer reported as a success.
- The expression normaliser dropped brackets under a unary minus, so `-(a + 2)`
  reached Inventor as `-a + 2` — a different number.
- `_magnitude` deleted a leading minus to make a dimension unsigned, which is
  the negation only when that minus governs the whole expression.
- Mirroring an arc left the endpoint references in its constraints pointing at
  the wrong end, which could not fail loudly.
- A refused coincident constraint no longer fails a sketch on the strength of
  its kind; the question is asked of the sketch that came out.
- Standalone sketch points and a circle's centre join the point-sharing scheme,
  removing eleven redundant constraints from a bolt circle.
- Errors are sanitised on the way out, so a COM failure no longer ships an
  absolute path — a user name, a project directory, a network share.
- Disputed enum fallback values refuse rather than guess.
- **Separate profiles in one sketch were counted as holes in each other.** The
  largest loop was taken as the outer boundary and every other loop as a hole in
  it, which is right for a plate with holes and wrong for the case that reads
  identically -- four circular bosses in one sketch came out as one boss with
  three holes punched in it, an area of zero, a feature that silently built
  nothing. Nesting is now decided by containment, by the even-odd rule, so a
  boss inside a pocket inside a plate counts once. Circles and ellipses are
  sampled into polygons for that test, which they were not before, and a ring's
  loops are told apart by their vertices rather than by an interior point --
  a washer's outer boundary encloses the centre of its own hole.
- **A shelled box was estimated from its surface area**, which put the shipped
  enclosure at 59.1 cm^3 where the geometry says 43.6. A shelled prism is the
  outline inset by the wall thickness and swept, and the inset area of any simple
  polygon is exact: `A - P*d + d^2 * sum(tan(turn/2))`, which gives the rounded
  100x70 outline's cavity to seven figures. A body that is not one prism -- a
  revolve, a sweep -- falls back to the old estimate and says so in the feature's
  detail rather than implying more.
- **The oracle would have cried wolf on a hollow part.** The simulator has no
  booleans, so a cut into a shelled box removes a whole prism there where
  Inventor removes only the walls it meets -- the enclosure's cable entry is 5.04
  cm^3 against a real 0.36. Steps after a shell are marked unpredictable in the
  rehearsal and skipped by the comparison, which is the difference between a
  check worth reading and one that faults correct recipes.
- **A revolved cut was charged for the material it overshoots.** A groove
  profile is drawn past the rim on purpose, so the cut certainly breaks
  through; the simulator swept the whole profile and took 2.6 cm^3 of air off
  the belt pulley. A cut is now clipped to the body's own extent about the axis
  before Pappus is applied.
- **Pappus was using the bounding box's centre, not the centroid.** A triangular
  groove profile has its centroid a third of the way from base to apex, so the
  pulley's groove was 2.6% out in a direction nothing would have questioned. A
  rectangular profile is the case where the two agree, which is why the flanged
  shaft never showed it.
- **The belt pulley drilled its own bore twice.** The revolved blank starts at
  the bore radius, so the final hole removed nothing -- which the live backend
  now refuses outright, and rightly. The redundant operation is gone, and the
  example's volume was derived by hand before any live run.
- A drawing check reported a **countersink angle as an invented 15.7 mm
  length**. An angle is 1.5708 in Inventor's units and was being compared
  against the drawing's millimetres; angles are now checked separately against
  the model's angle parameters, and a drawing angle the model never declares is
  reported as missing rather than passing unnoticed. A symmetric pitch also
  matches the half-spacing a centred grid is really driven by, since a drawing
  gives the pitch and refusing to see it there faults a correct model.
- The simulator counted a counterbore as a whole cylinder rather than an
  annulus over the bore it already counted, and ignored a countersink entirely.
  Every counterbored plate came out lighter than it is.
- **A mirror or a pattern changed no volume at all.** An occurrence does
  whatever its seed did, so a mirrored slot cut removes the same again -- the
  simulator said "occurrence volume is not estimated" and left the total alone,
  which made a mirrored cut indistinguishable from a cut that had failed. Each
  feature now records what it did to the volume and an occurrence repeats it.
- **A through-all feature was charged the body's whole span** along the cut
  axis. Right for a plate, wrong for everything else: the angle bracket is 90 mm
  tall with a 6 mm base, so its base slots were charged 90 mm of material. The
  distance is now measured over the profile from the prisms that built the part,
  which is exact for an extruded body and falls back to the span for a revolve,
  sweep or loft rather than guessing. Between this and the mirror fix the
  bracket went from 27.15 cm^3 to 41.28 against Inventor's 43.20.
- **A fillet was subtracted whichever way the edge turned.** On an inside corner
  a fillet *adds* the same material an outside corner loses, so the bracket's
  correct fillet made it 1.4 cm^3 light. The sign now comes from the selector,
  since the simulator cannot see which side the material is on and the recipe
  has said which it means.
- **`concave` and `convex` matched every edge in the simulator**, which is worse
  than matching none: a recipe asking for the one inside corner on a bracket got
  whichever edge happened to be created first. An edge running along an
  extrusion sits at a corner of its profile, and the corner's turn decides it
  exactly -- so those are classified, and everything else stays unknown and
  matches neither, as on the live backend. Tangent joins are not corners, so a
  slot's straight-to-arc junction is correctly no edge at all.

  Together these put the angle bracket at 43.2012 cm^3 against Inventor's
  43.1999 -- from 27.15 at the start. What is left is the slot arcs' sampling.
- Four simulator answers that were wrong rather than approximate: a sketch on a
  named work plane ignored the plane's offset (the flanged shaft was built 12 mm
  low), a sweep summed only straight path segments so an arc path had no length,
  a loft added a mean area as if it were a volume, and a revolve expanded the
  bounds to a cube so a ring reported as a ball. `plan_bounds` also treated
  every arc as its whole circle, which matters because the bounding box is what
  decides whether a cut reaches the part.

### Known limits
- Revolve, sweep, loft, patterns and threads are written but unproven: no
  shipped recipe reaches them.
- The hole methods' argument order came from another project's field notes, not
  from measurement here. It is verified at run time against the feature Inventor
  builds, so a wrong order fails rather than lying; `scripts/probe_hole_styles.py`
  settles it.
- Assemblies, drawings and sheet metal are not supported.
- Regular polygons keep one degree of freedom; Inventor refuses the closing
  equal-length constraint.
