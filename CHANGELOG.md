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

### Fixed
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
