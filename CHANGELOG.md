# Changelog

Notable changes, newest first. Dates are when the work landed, not a release.

## Unreleased

### Added
- **A closed manufacturability loop.** The OnlyCat DFM tool measures a mesh and
  says what is wrong with the part for injection moulding; this takes that
  verdict, enacts the parts of it that really are parameter changes, rebuilds,
  and asks the tool again. Five tools: `check_manufacture`,
  `improve_for_manufacture`, `read_dfm_report`, `protect_geometry`,
  `dfm_capabilities`. See `docs/DFM.md`.

  A finding is closed by a measurement rather than by an assertion, so every
  round records which findings actually cleared, which stayed and which
  appeared. A round whose change went in while its finding stayed is called out
  rather than spent three more times.

  The analyser runs headlessly through its own modules, not a browser: its
  analysis is pure -- `analyseMesh` and `runDFM` take every input as an argument
  and touch no DOM, which is what lets the tool run them in a worker -- so
  `inventor_mcp/dfm/headless.mjs` calls the same functions the page calls.
  Verified rather than assumed: on the tool's own `hollowFrustum(20, 30, 3, 2)`
  fixture with clean inputs the bridge returns 100 out of a budget of 100, which
  is what its `test/unit.mjs` asserts for that part.

  Ratio fixes come out as expressions: a rib becomes `wall_t * 0.45`, not
  `0.9 mm`, so the relationship survives the next wall change instead of quietly
  re-breaking the check. Which makes the ordering matter -- a Ø5.2 mm boss is too
  wide for a 2 mm wall and comfortable on a 2.8 mm one -- so every decision
  downstream of the wall is taken against the wall the same pass is setting.

  Findings no parameter answers are reported, not attempted: an undercut is a
  tooling decision, a sink is cored out, and a corner radius cannot be measured
  from a mesh at all, so a change to one could never be verified.
- **The loop takes a file.** `improve_for_manufacture(path="bracket.ipt")` works
  on `bracket_v2.ipt` and leaves the original alone -- changing the file
  somebody handed over is wrong twice, because their work is gone and there is
  nothing left to compare against. Version names keep whatever separator, case
  and zero-padding the last one used, and nothing is ever overwritten: two runs
  an hour apart would otherwise land on the same name and the second would
  destroy the first. The copy is a filesystem copy rather than an
  open-and-save-elsewhere, because a copy cannot modify what it copies.

  A STEP, IGES, SAT or Parasolid file is imported as a solid body and can be
  measured but not improved: translated geometry carries no history, so there
  are no parameters to drive. That is reported as a *count* of the part's user
  parameters rather than inferred from its extension -- an .ipt somebody made
  by importing a STEP file and never parameterised has the same problem. An
  .stl goes straight to the analyser with no Inventor at all.
- **Role discovery, from evidence.** A part handed over as a file declares
  nothing, so `discover_dfm_roles` works out which parameter is which from what
  the part's features actually read: a shell feature takes its thickness from
  somewhere, and whatever that expression reads *is* the wall -- not by
  resemblance but by construction. Reported with what it was read from, because
  it is a claim somebody may want to check.

  What it will not do is read a spelling. A table of likely names gets most
  parts right and the ones it gets wrong are indistinguishable from the ones it
  gets right until a loop has thinned the wrong dimension, so a likely name is
  offered with the call that would accept it and nothing acts on it. Two shells
  reading two different parameters map nothing: that is not a wall, it is two
  walls and a question.
- **Comparing versions.** `compare_manufacture(before=..., after=...)` says what
  moved between two runs, and `improve_for_manufacture` does it for its own first
  and last round under `what_moved`. Through the DFM tool's own `compareRuns`
  rather than a diff written here, because it knows which direction is better for
  each measurement and it raises a caveat where a score moved for a reason other
  than the part -- a material change, a different set of checks, or two records
  with the same triangle count.
- **A declaration that stays with the part**, in a custom iProperty inside it
  and a `bracket.dfm.json` beside it, so the next run starts from the same
  reading and a versioned copy does not arrive having forgotten which parameter
  was the wall. `declare_dfm` writes it; five sources are ranked -- what you say
  now, the recipe, the part itself, the sidecar, discovery -- and freezes are
  unioned across all five so no source can take protection off.
- **Key geometry**, which is the other half of that: `frozen: true` on a
  parameter, a `dfm.frozen` list that accepts globs, `frozen_features`, and
  `protect_geometry`. Enforced in `apply_parameter` rather than in the loop --
  a guarantee that holds only inside one loop ends the moment anything else
  edits a parameter, and the report would still say the geometry was protected.
  `set_parameters` therefore refuses too, and `override_frozen=True` is how you
  say otherwise.

  Depending on a frozen value counts as changing it. Freeze `seal_face` at
  `plate_t - gasket_crush` and `plate_t` is protected as well, transitively,
  with the refusal naming the chain -- otherwise the freeze holds on paper while
  anyone editing `plate_t` moves the sealing face. The reverse is deliberately
  not true: a parameter that reads a frozen value may move, because reading is
  not changing.
- `examples/moulded_housing.json` -- a drafted, shelled housing with floor ribs
  and screw bosses, deliberately wrong in two ways a parameter answers, with the
  M3 pilot hole and the cable entry frozen because the screw and the connector
  decide those rather than the moulding.
- `tests/test_dfm_targets.py` -- the drift alarm for the one duplication in the
  integration. The DFM tool states its thresholds as literals inside its rules
  and does not export them, so the targets aimed at here are this project's own
  reading of those bands. Every one is put through the real engine and required
  to come back clean, with negative controls proving each margin is still needed:
  the material floor alone still fails, the required draft alone still fails, and
  adjusting only the boss wall still cannot satisfy both boss guidelines.
- A `dfm` check in `scripts/live_acceptance.py`, which runs the whole loop on the
  housing and asserts the frozen pilot hole comes out the size it went in.

### Fixed
- **`_feature_kind` read `type(feature).__name__`**, which is `CDispatch` under
  late binding -- and late is this project's default. Every feature on a live
  part reported its kind as the name of a pywin32 wrapper class, so anything
  reasoning about kinds was reasoning about nothing. It now asks `Object.Type`,
  a documented property of every Inventor object, and answers "unknown" rather
  than guessing when it cannot be read.
- **`open_document` registered every opened part as millimetres and degrees.**
  Right for most parts and 25.4 times wrong for an inch-authored one -- wrong in
  the direction where a bare number in a later edit builds something a fortieth
  of the size it should be. It now asks the document, and says so when the
  document will not answer. (Values this server sends always carry their own
  unit, so expressions were never affected; a bare number in a later edit was.)
- **`Session.register` replaced a context outright**, so handing a path to a
  tool for a part already on screen silently dropped its recipe, its sketch
  plans and its freeze guard -- turning a protected part into an unprotected one
  without saying anything.
- **`headless.mjs` was missing from the wheel**, so an installed copy had a
  manufacturability loop that could not run and nothing to say about why.
- **An explicitly named analyser path fell through to a different checkout**,
  analysing the part against rules the caller had not asked for and reporting
  success.
- **The simulator evaluated an expression once, when it was set, and kept the
  number.** `rib_t = wall_t * 0.45` stayed where it started for ever after the
  wall moved, so the simulator disagreed with Inventor about every dependent
  parameter in a document -- and it matters most exactly where it is least
  visible, since the DFM loop writes its ratio fixes as expressions *so that*
  they follow the wall. Dependents are now recomputed on every parameter change.

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
- **A pattern of a hole needs each occurrence recomputed, not copied.** Measured
  on 2027.1: a boss patterns with the default compute type and a hole does not,
  with the boss or alone, until the compute type is `kAdjustToModelCompute`. That
  is exactly what the two settings mean -- identical compute copies faces, and a
  blind hole's second occurrence has no material to remove until the boss beneath
  it has been computed too. The belt pulley's through-holes in a flat disc
  pattern happily with the default, which is why this was not obvious: identical
  compute is right when every occurrence really is identical. Both pattern
  operations now recompute first and fall back, reporting which route built the
  feature.
- **The sweep was failing on the wrong thing entirely.** `AddUsingPath` wants a
  `Path` object, and `Features.CreatePath(curve)` is the only thing that makes
  one -- `Profiles.AddForSurface` returns a `Profile`, which the sweep rejects
  with "Type mismatch". That route was the fallback here and could never have
  worked, so it is gone: a fallback known to be wrong only adds a second
  confusing error to the first. The curve matters as much as the method, because
  a sketch of "one arc" holds the arc and three points -- the origin is projected
  in whenever a constraint references it -- so `SketchEntities.Item(1)` had three
  chances in four of being a point.
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
