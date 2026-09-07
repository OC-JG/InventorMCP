# Roadmap

Where this project goes next, and why it is not going somewhere else.

`ARCHITECTURE.md` says what the code is. `DECISIONS.md` says why it behaves as
it does. This document answers the question that was put after a night-long
audit of both: *knowing what we know now, would we build it the same way?* The
answer is no rebuild, four restructures, and five phases. The reasoning is kept
here alongside the plan because the plan will be read by someone who wants to
skip the restructures and start on the interesting phase, and the reasoning is
what says they cannot.

Dates in this file are when a thing was decided or landed, not promises.

## Why this is not a rebuild

**Every defect the audit found broke a rule this repository had already
written down.** None broke a rule it was missing. The chamfer estimate that was
off by a factor of two broke "measure, don't assume". A recipe with `inf` in a
parameter building silently broke "prefer a loud failure". The shell that
raised a false divergence broke "calling a correct thing a fault teaches the
reader to ignore the field". CI red on `main` for eight consecutive runs broke
the paragraph in `DECISIONS.md` that records the same thing happening for
sixteen. When the design is right and execution slips in the gaps *between*
enforced seams, the fix is tests at the seams, not a new architecture. Phase 0
and Phase 1 are almost entirely that.

**Three things here are better than anything in the field** (see *The
landscape*, below):

- **`Resolved(expression, value)` all the way down.** A dimension in the
  finished Inventor part still knows which parameter drove it. The nearest
  open-source peer's recipe is Python, which is more expressive and loses every
  parameter at STEP export; the commercial copilots generate a macro and have no
  checkable intermediate at all. A JSON recipe whose expressions reach Inventor
  dimensions is the one thing nobody else has.
- **`Backend` as an ABC with an abstract method per operation.** Measured cost
  of a new operation is five files, the same five every time, and the live and
  simulated implementations have not drifted once across eighteen operations.
  That is construction, not discipline.
- **The simulator as oracle.** Rehearse offline, build live, compare the
  per-operation volume deltas. Nothing in the catalogue of 598 AI-for-CAD
  projects does this.

**A rebuild would throw away the part that cannot be re-derived.**
`INVENTOR_SETUP.md` records facts like "`kNewBodyOperation` is 20485",
"`AddByCenterStartEndPoint` always sweeps counter-clockwise" and
"`BossFeatures` has no `Add`". Each of those cost a live run against a real
Inventor to learn. No amount of first-principles design recovers a fact about
what a closed-source COM server actually does.

## What changes, and why

Four restructures. One is a genuine redesign of a single module; the other
three are hygiene the code had earned the right to skip until it stopped being
able to.

**1. The simulator's volume model becomes a per-body ledger.** This is the only
real redesign. Seventeen of the eighteen `ponytail:` markers (the repository's
word for a deliberate approximation) live in `backend/mock/` — it was thirteen
of fourteen when this was written, and the sentence said so until 2026-09-03,
which is what put this file under `test_roadmap_still_true.py` — and the worst
of them was structural rather than local: `document.volume` is one scalar,
`document.slabs` is a list that only `extrude` appends to and nothing ever
subtracts from, and every operation's estimate was bolted on separately. So a
through-cut after a shell is charged against the pre-shell solid — a 26×
over-count on the enclosure example's cable bore, recorded as defect 2 in
`FEATURE_COVERAGE.md`. The replacement is a ledger of signed prisms per body
that cuts, shells and holes actually remove material from, with volume reported
**per body and never aggregated**, because an inverted body cancels a sound one
and a sum hides it. Same `Backend` interface, same tests, one module.

**2. Every duplication gets a drift test the day it is created.** The ABC
enforces that the two backends agree. Nothing enforced that the docs agree with
the code, the cheat-sheet with the schema, `PREDICTED` with the ponytails, the
Python floor with the CI matrix, or that a green DFM job actually ran the
analyser. The audit added a test for each (`test_docs_still_true.py`,
`test_supported_pythons.py`, the DFM sentinel in `conftest.py`, the divergence
tests). The standing rule from here: a fact stated in two places without a test
that they agree does not merge.

**3. The recipe cheat-sheet is served once, not three times.** Three tool
descriptions carry the same nine-kilobyte guide, 98% identical, at a cost of
roughly 4,600 tokens on every request before the caller has said anything. One
tool keeps the full text; the others point at it and at the
`inventor://recipe/guide` resource. `ARCHITECTURE.md` records the trade-off —
a model that has to fetch a schema will guess — so the copy stays in the tool
list rather than moving to the resource, on `build_part_from_recipe`, the tool
a caller is looking for when it wants a part made.

**4. `builder.py` is split along the seams it already has.** At 1,199 lines it
does resolution, dispatch, static checks, rehearsal, divergence and warnings,
and `_apply_one` alone is 238 lines. It is not broken. It is where drawings and
assemblies will land, and it should be three files before that happens, not
after. Static checks (`check_recipe`, undriven parameters) and rehearsal
(`rehearse`, `PREDICTED`, `compare_to_rehearsal`, the reach check) move out;
`builder.py` keeps building and re-exports the rest so nothing importing it
notices.

## What stays, despite temptation

- **JSON as the recipe, not Python.** Python would be more expressive and
  shorter to write. It would also be impossible to check before commitment,
  and `check_recipe` refusing a parameter that drives nothing is the property
  the peer project spends a two-hundred-line repair loop compensating for.
  Expressions inside JSON are the deliberate middle.
- **No geometry kernel in the simulator.** The argument in `DECISIONS.md`
  holds: the live build *is* the kernel, and a second kernel would be a second
  set of disagreements. The simulator estimates volumes so a live build can be
  checked against a prediction; it does not need to be right about shape, only
  honest about how far its numbers are trusted. The ledger in restructure 1
  makes those numbers better without making the simulator a kernel.
- **COM, and nothing else.** There is no other way into Inventor. The fallback
  enum table and `scripts/dump_constants.py` are the price and stay the price.
- **The escape hatch stays off by default.** `run_inventor_script` is what
  lets "unsupported" be a statement about the recipe rather than about the
  server, and it stays behind the machine owner's explicit switch.
- **The read direction stays.** `drawing.py` and `check_against_drawing` go
  2D→3D — a drawing is read, not traced — and the research field agrees that
  is the harder and more useful direction. Producing drawings (Phase 3) is
  added beside it, not instead of it.

## The landscape, September 2026

Recorded because the roadmap's ordering depends on it. Sources are the
`awesome-ai4cad` catalogue (598 entries, read in full), the `text-to-cad`
repository (read in full), and search snippets for the commercial products,
whose sites were unreachable from the environment the audit ran in — those
claims are theirs, not measured.

- **MecAgent** is a natural-language-to-macro copilot over SolidWorks, CATIA,
  Inventor, Fusion and Creo: it generates a VBA or API script and runs it,
  which is this project's escape hatch as a product. It has since added
  drawing generation claiming ISO/ASME compliance, automatic GD&T and
  auto-formatted title blocks, output as PDF or native.
- **DraftAid** is the pure 3D→2D product: a SolidWorks and Inventor plugin,
  STEP in, DWG/DXF/IDW/PDF out, described by its makers as "80–90% complete
  upon generation". That number is the honest bar for the category — a human
  finishes the drawing — and it is the number Phase 3 has to beat, or at least
  explain.
- **The vendors ship drawing generation in 2026.** SOLIDWORKS 2026 lists
  AI-powered drawing generation; Autodesk Fusion AI lists automated drawings.
  What `ARCHITECTURE.md` filed under "dear" is what every vendor is building
  this year.
- **Open-source peers** are a dozen MCP servers (FreeCAD, CadQuery, build123d,
  multiCAD, gNucleus, Onshape's own FeatureScript server). None produces a
  dimensioned drawing. The `text-to-cad` DXF skill is explicit that it emits
  flat patterns, not drawings: "a cut file is a toolpath, not a document".
- **The research runs the other way.** Drawing-Recode, CAD2Program, Ortho2CAD
  and SOV-CAD all go 2D→3D. `check_against_drawing` is in that company.
- **The benchmarks describe this test suite.** ParamCAD-AgentBench
  (kernel-validated parametric models), CADEngBench (parametric perturbations,
  functional edits, DFM checks) and MUSE (manufacturable, functional,
  assemblable). `examples/expected/` plus the divergence check plus the DFM
  loop is a private instance of the same shape, and the public ones are the
  external yardstick to run against.

**Three things taken from `text-to-cad`**, the closest peer, which states the
same philosophy in a different accent ("only checks that actually ran"; refuse
an ambiguous label and list the candidates):

1. **Mandatory snapshot review.** "Deterministic checks passing is not a
   reason to skip." Their modelling notes list six traps that pass validation
   and only a render finds. This project has `capture_view` and no rule that
   it runs — Phase 1.
2. **A reference that names the wrong document is a hard error**, never a
   silent fallback to whatever is open. Topology handles here are
   per-document and expire on rebuild; the same trap exists.
3. **Volume per solid, never aggregated.** Folded into restructure 1.

**What this project has that the peer does not:** parameters that survive into
the finished part; a DFM loop that acts, rebuilds and re-measures rather than
reporting and handing off; and a live build to measure against.

## The phases

Ordered by dependency, not desire. Each phase's items are the acceptance
criteria; an item is done when the thing it names is true in the repository,
and this file is updated in the same commit.

### Phase 0 — stop the bleeding *(done, 2026-09-03)*

- [x] Merge the audit branch so `main` is green.
- [x] A required status check on `main`, so a red run blocks rather than
      accumulates. This is a repository setting, not a file; it cannot be
      verified from the tree and is recorded here on the owner's word.

### Phase 1 — foundations *(done, 2026-09-03)*

The four restructures, plus the two debts that were only ever going to be paid
by running against a real Inventor. Both have since been paid: an
acceptance run on Inventor 2027.1 (2026-09-03, 63 of 64 checks passing) put the
simulator within 1% of a live build on all eleven shipped examples and confirmed
the volume ledger's hand-derived figure for the enclosure, and a calibration run
put a measured number on all four placeholder tolerances.

Both runs were worth more for what they broke than for what they confirmed. The
acceptance run's one failure was four enum names the table had that Inventor has
never had, one of which had been quietly refusing every `shell` with
`direction: "both"` since it was written. The calibration run found every `trim`
split keeping the wrong half of the part — and then, in the course of proving
that, found that the divergence check could not have caught it, because it
compared how much an operation moved and never where. Three defects, none of
which anybody was looking for, all from running the thing against reality and
reading the numbers.

- [x] **Cheat-sheet served once** (restructure 3), pinned by a test that the
      duplicate does not come back. *(2026-09-03; tool-list bytes 35,206 →
      17,040.)*
- [x] **Per-body volume ledger in the simulator** (restructure 1). *(2026-09-03.)*
      The enclosure went from 41.874424 cm³ to 46.897289 against the hand-derived
      46.896177, so from 10.7% below to 0.002% above; every other example held
      its recorded expectation to the digit; defect 2 in `FEATURE_COVERAGE.md`
      is closed and defect 1 is now visible rather than cancelling out. No
      expected volume changed — only the note in
      `examples/expected/enclosure_base.json` that had described the bug.
- [x] **`builder.py` split** (restructure 4) into `checks.py`, `rehearsal.py`
      and building, with `builder.py` re-exporting so no import changes.
      *(2026-09-03; 1,199 lines became 763 + 183 + 409, moved verbatim.)*
- [x] **Snapshot policy.** *(2026-09-03.)* `MODELLING_NOTES`, the Skill, the
      `capture_view` tool description and the `model_this_part` prompt all say
      to render the part before reporting it finished, and to ask for the
      isometric view until defect 4 in `FEATURE_COVERAGE.md` (orientation names
      do not describe what you get) has been measured and fixed. A test fails
      if that defect is marked fixed and the policy still works around it.
- [x] **Live calibration of `PREDICTED`.** *Done 2026-09-03.* The acceptance
      run could not reach these at all, because no shipped example used those
      operations, so `examples/calibration/` now
      holds an instrument for each and `live_acceptance.py --only calibration`
      prints what the simulator predicted beside what Inventor did. Measured:
      `coil` 0.2% out, `draft` 2.1%, `emboss` 17.5%. Tolerances set to 0.15,
      0.20 and 0.40 — each looser than its run alone would justify, for the
      reasons recorded beside the table. The drafted block was a prediction and
      Inventor matched it to four decimals: 4.6178 against a derived 4.6179.
      **`split` took three runs and two fixes**, because the reason it had no
      number was a bug rather than an absence: Inventor was trimming the
      opposite side to the one the schema documents, and the simulator was
      taking the right side but the wrong amount. Both fixed — defect 5 — and
      the three fixtures then agreed to four decimal places, so it is 0.05. A
      trimmed revolve is excluded from the comparison rather than covered by a
      loose number, because the ledger cannot answer there and says so.
- [x] **Give the divergence check a sense of direction** — defect 6, found on
      the way to the calibration above and fixed the same day *(2026-09-03)*.
      It compared volumes moved and nothing
      else, so a cut that took the right amount off the wrong side read as a
      pass: the run that exposed the split inversion was 1.2% apart while
      keeping the opposite half of the part. Every operation now records where
      the bounding box's centre went, and a pair of runs that sent it opposite
      ways is reported whatever the volumes say. Narrow on purpose: a sign flip
      past a millimetre, not an agreement within a tolerance, because the
      simulator's box is approximate for a revolve and exact only for prisms.
- [x] **Drift tests are the rule** (restructure 2). *(2026-09-03.)* Written
      down in `DECISIONS.md` as "a fact stated twice needs a test that the two
      agree", with the drifts that earned it and the corollary that the
      answer is usually the test rather than removing the duplication.

      **Including this file, which was the last one exempt from it**
      *(2026-09-03)*. The rule was written here and not applied here, and the
      file had drifted in three ways: the `ponytail:` count above, four
      calibrated tolerances quoted with nothing holding them against
      `PREDICTED`, and a ticked item dated "the same day" without saying which.
      `tests/test_roadmap_still_true.py` now holds the countable claims, and
      says in its own docstring which claims it deliberately leaves alone —
      dated measurements, the landscape section, and the phase count, which is a
      framing choice rather than a fact.

### Phase 2 — the coil-shaped additions

Each is one request type, one abstract method, two implementations, roughly
350 lines across the same five files. Ordered by how often the lack of it has
actually bitten.

- [x] **Work axis and work point.** *(2026-09-03. Simulator measured and
      tested; the three COM calls are unmeasured -- see below.)* `AxisSpec`
      already accepted `work_axis` and nothing created one.

      **The reason given here was wrong, and checking it was worth more than
      taking it.** This item read "so a circular pattern can only turn about an
      origin axis", which is false: `resolve_axis` has always resolved named
      sketch lines and the COM backend passes whatever it resolves straight to
      `AxisEntity`, so an off-centre bolt circle was already buildable via a
      throwaway sketch on a *perpendicular* plane carrying a line in that
      plane's own coordinates. That was measured before anything was written,
      and it builds clean.

      ~~and it builds clean~~ **— against the simulator, and only there.** The
      first live run, 2026-09-07 on Inventor 2027.1, showed the workaround
      cannot have built clean on Inventor at all: the COM backend never wrote
      the recipe's labels onto Inventor's sketch entities, so resolving a named
      sketch line searched for a name nothing assigns. Defect 8 in
      `FEATURE_COVERAGE.md`. The paragraph above is left standing with this
      correction under it rather than rewritten, because the mistake it made is
      the one worth keeping visible: "measured" was written of a simulator run
      in a session with no Inventor to reach, in the same file that exists to
      keep that distinction.

      The real reason is geometric and sharper: a circular pattern turns about
      an axis perpendicular to the face it patterns, a sketch line lies *in* its
      own sketch plane, and so no line drawn on a plate's face can ever be that
      plate's bolt-circle axis. The workaround therefore asks the caller to do
      the axis mapping in their head, on a plane they are not otherwise using.
      `work_axis` with `kind: "normal_to_plane"` says it directly, in the
      plane's own coordinates, with `at` carrying expressions like every other
      number here.

      Probing the false claim also turned up **defect 7**: the recipe that gets
      this wrong -- a pattern axis lying in the patterned face's own plane --
      passes `check_recipe`, passes `validate_recipe` and returns `ok: true`
      from the simulator, because `_repeat` counts occurrences without ever
      reading the axis. `work_axis` makes that mistake avoidable; it does not
      make it detectable, and the note in `FEATURE_COVERAGE.md` says what would.
      *A warning for it landed on 2026-09-07 -- see the item below.*
- [x] **Warn about a pattern axis lying in the patterned face** — defect 7,
      the item above's leftover. *(2026-09-07.)* A warning rather than a
      finding, and the reason is the limit of a static check rather than
      caution: a pattern about an in-plane axis is meaningless as a bolt circle
      and a legitimate way to write a 180-degree flip, and nothing static tells
      the two apart. So it fires only where the geometry is certain -- an origin
      axis lying in the seed's plane, a sketch line on that plane, or one on a
      work plane offset from it -- and declines on the four cases where an
      answer was available and would have been wrong.

      One of those four is worth recording here rather than only in
      `FEATURE_COVERAGE.md`: **the simulator would have given the wrong answer
      and the check does not take it.** `mock.work_plane` files every work plane
      against an origin base whatever its `kind`, so it believes an angled plane
      is parallel to its base; a check that read the simulator's own table would
      report a correct angled-plane recipe as a fault. The plane chain is walked
      from the recipe instead, honouring `offset` and nothing else.

      This does not close defect 7. The fix is still the simulator placing
      occurrences rather than counting them, which is the `ponytail` on
      `_repeat` and a ledger-sized change; the warning makes the mistake
      *visible*, where `work_axis` only made it avoidable.
- [x] **Measure the work axis against a live Inventor.** *Done 2026-09-07,
      Inventor 2027.1, after six runs and four defects. Twelve of twelve checks
      pass.* The three COM calls execute; a `normal_to_plane` axis is genuinely
      off-centre (the same bolt circle about it and about `z` measure 0.37273 mm
      apart); and it tracks its driving parameter -- **0.18636 mm of
      centre-of-mass movement against a derived 0.18636**, on geometry chosen so
      the derivation is exact.

      Three independent readings agree, which is why this is a tick rather than
      a number that came out close. The magnitude matches the derivation; the
      discriminator against `z` says the axis is not on the origin; and the
      volume is **unchanged** across the parameter move, which is what says no
      hole was clipped and the derivation's precondition therefore held. The
      fifth run's 0.12263 was the same axis measured on geometry where one hole
      crossed the plate edge, and reproducing that figure by hand is what
      established the axis was right before the check could say so.

      That agreement is with the *clipped* derivation, and the story of it is
      the item's real lesson. The check predicted 0.18640 from one line of
      arithmetic whose precondition -- every hole on the plate -- was never
      written down. At `bolt_x` 45 one hole sits exactly on the plate edge and
      Inventor halves it, correctly. So the run reported a failure, the part was
      right, and the number to distrust was the one this repository had derived
      rather than the one Inventor measured. The geometry moved inboard and
      `_bolt_circle_prediction` now refuses to hand back a figure when a hole
      would be clipped.

      Four defects came out of getting here, none of them the thing being
      measured: **8**, the recipe's sketch labels never reaching Inventor;
      **9**, a parameter change rebuilding nothing; **10**, Inventor refusing a
      parameter name it reads as a unit; and **11**, the carrier point never
      leaving the origin. Defect 11 was the one that mattered and it hid behind
      its own symmetry for three runs, while the check said "parametric in name
      only" and pointed at the wrong thing each time.

      **How it went, run by run**, kept because a tick is the least
      informative thing this item produced. It was split from the one above
      rather than left inside it, because a tick that covered unmeasured COM
      would be the kind of claim this file exists not to make; at that point
      `WorkPoints.AddByPoint`, `WorkAxes.AddByTwoPoints` and
      `WorkAxes.AddByLine` had never executed, and the test that mattered was
      never "did it run" but whether the bolt circle moves when the driving
      parameter does.

      **Run once, 2026-09-07, Inventor 2027.1: it failed on the first check
      and found defect 8 instead.** `live_acceptance.py --only work-geometry`
      runs all five unmeasured Phase 2 behaviours -- the three calls above, the
      hole aimed with `bodies`, and the save conflict's remedy -- and asserts
      the bolt circle's centre-of-mass shift against 0.18640 mm derived
      beforehand rather than merely checking that the pattern ran.

      It got no further than `WorkPoints.AddByPoint`, and not because of that
      call: the carrier sketch's point could not be found, because **the COM
      backend never wrote the recipe's labels onto Inventor's sketch entities**
      and three lookups searched for a name nothing assigns. Defect 8, fixed the
      same day by keeping the entity Inventor hands back at creation. The four
      checks below it skipped by design, which is what that ordering is for.

      **Second run, same day, with the labels fixed and the type library
      readable: all three calls execute.** `WorkPoints.AddByPoint`,
      `WorkAxes.AddByTwoPoints` and `WorkAxes.AddByLine` are measured as
      existing and working on 2027.1 -- and the type library does not generate a
      module for any of them, so late binding is what makes them reachable,
      which is the argument `INVENTOR_SETUP.md` already makes for it.

      **This still stays open, because "it runs" was never the test.** The
      measurement that matters -- whether the bolt circle moves when its
      driving parameter does -- was blocked by something else entirely:
      Inventor refused the parameter name `pcd` with a bare "Exception
      occurred", while taking `bolt_x` in the same recipe, and nothing in
      `RESERVED_NAMES` or the unit table explains it. The recipe now uses a name
      Inventor took, and the run carries a probe over candidate names so the
      next one says which it declines and why the shape of the name matters.

      **Third run: the reason the bolt circle would not move was not the work
      axis at all.** `set_parameter` was the only mutating call in the COM
      backend outside `_batch`, and `_batch` is what calls `document.Update()`
      -- so a parameter change rebuilt nothing and every measurement afterwards
      was of the part as it had been. Defect 9, and it reaches the DFM loop and
      `set_parameters` rather than only this check. The carrier sketch was
      `fully_constrained=True` with its driving dimension in place all along,
      which is what said the parametric chain was sound and something else was
      wrong. Fixed; unmeasured until the next run.

      That run also settled the parameter name: Inventor **refuses a name it can
      read as a unit** -- `cd` is the candela, `pcd` the pico-candela -- and is
      case-sensitive about it, so `PCD` is accepted. Defect 10.

      And one of the failures was this file's own instrument, not Inventor: the
      work point read as missing because the check reached into
      `ComponentDefinition` from the script's thread and got "the application
      called an interface that was marshalled for a different thread" --
      precisely the failure `describe_feature` already records. It is a backend
      method now, `list_work_geometry`, implemented on both.

      **Fourth run: the cause, at last -- and it was never the three calls.**
      `_carrier_point` created its sketch point with no position and left a
      driving dimension to place it. Inventor infers a coincidence with the
      projected origin for a point built at (0, 0), that pins both degrees of
      freedom, and the dimension cannot move it -- so the carrier point stayed on
      the origin and every `normal_to_plane` axis ran through the origin.
      Defect 11.

      **It hid behind its own symmetry for three runs.** A bolt circle about an
      origin axis has its centroid *at* the origin, so the centre of mass does
      not move when the parameter does -- exactly the reading this item told the
      check to interpret as "parametric in name only". The check said so three
      times while pointing at the wrong thing, and the two faults fixed on the
      way (defects 8 and 9) were both real and neither the cause.

      What separated them was one more number: **the volume changed while the
      centre of mass did not.** The pilot hole moved as asked; the axis did not.
      A parameter that moves some geometry and not the rest is not a parametric
      failure. The check now also builds the same bolt circle about `z` and
      requires the two to measure apart -- the assertion that would have caught
      this on the first run, where the volumes would not have: they agree to six
      decimals either way.

      Two more things the run measured, neither of them what it was aimed at:
      **`hole` + `bodies` cannot work on Inventor** -- 2027.1's `HoleFeature`
      has no `AffectedBodies` at all -- and **work geometry does not appear in
      `list_features`** on the COM backend, because that walks
      `ComponentDefinition.Features` and Inventor keeps work planes, axes and
      points in their own collections. The mock puts all of it in one list, so
      the two backends disagree; the fix needs one fact nobody has measured
      (whether Inventor's *origin* planes and axes sit in those same
      collections, and how a created one is told from them), so the run now
      prints the collections' contents rather than guessing.
- [x] **`hole` gains `bodies`**, the multi-body targeting `extrude` already
      has. *(2026-09-03.)* Every piece it needed was already parameterised by
      body -- `charge`, `_through_all_distance`, `_material_spans` and
      `_Slab.body` -- so the simulator side was threading an argument that four
      functions were already waiting for, and `extrude`'s inline body check
      became a shared `_aimed_body` rather than a second copy.

      Two things are worth knowing. **A hole is aimed after it is built, not
      before**: unlike `extrude` there is no definition object to put
      `AffectedBodies` on, because `HoleFeatures.Add...` makes the feature in
      one call, so the COM backend sets it on the finished feature and treats a
      release that refuses as a hard error -- the hole exists either way, and one
      on the wrong body has taken real material out of a part that looks
      finished. Unmeasured, like the work axis. **And the total volume cannot
      show that aiming worked**: both test blocks are 8 cm³ and
      `_through_all_distance` deliberately falls back to the bounding box over a
      point no prism covers, so a bore aimed at the wrong body is still charged
      full depth and the totals agree to the digit. Per body they do not, which
      is the ledger's reason for never aggregating.
- [x] ~~**A both-directions extent on `hole`**~~, **or a warning when a through
      hole's axis re-enters material it did not cut** — defect 1 in
      `FEATURE_COVERAGE.md`. *(2026-09-03, the second of the two.)*

      The first is struck through rather than left open, because it is not
      implementable: **Inventor's hole extent has no both-directions option.**
      Distance, Through All and To, and Through All takes a side. There is
      nothing to add a knob to, so the item was offering a choice between a fix
      and a workaround without knowing it.

      The warning was nearly free, which is the part worth recording. The
      simulator already counted the separate pieces of material each drill axis
      crosses -- it needs them to decide which way the hole goes -- and a count
      above one *is* the condition, exactly. So `rehearse` reports it, names the
      substitute (`extrude`, `direction: "symmetric"`, `extent: "through_all"`)
      and says the step will diverge on volume as well, since the simulator
      charges every wall the axis meets and Inventor drills one.

      Fires on the reproduction, and on none of the eleven shipped examples --
      the enclosure included, the part this defect was found on, which has used
      the substitute since. Both directions are tested: a warning that fires on
      a correct recipe teaches the reader to ignore the field.
- [x] **Sketch-driven pattern, thicken, move face** — Tier 2 in
      `FEATURE_COVERAGE.md`, all with public `Add` methods. *(All three landed
      2026-09-07. Measured in the simulator; none of the three COM calls has
      executed, and each has its own item below for that — the same split the
      work axis got, for the same reason.)*

      **The ordering in this item was wrong, and it is worth saying how.** It
      reads as three equal-sized additions of the `coil` shape, and
      `ARCHITECTURE.md` said so too. Two of them were: `move_face` and `thicken`
      are five files each. The third is the smallest in *value* and was the
      largest in thought, and both halves of that are worth recording.

      Smallest in value because the gap was narrower than Tier 2 claimed. It
      said anything irregular "has to be enumerated by hand", when `hole`
      already takes a list of points and `boss` a list of positions — so
      irregular holes and bosses never needed a pattern. What was missing is
      patterning a feature that is not already position-shaped: a pocket, a rib.

      Largest in thought because it is the first operation whose entire input is
      *positions*, put to a simulator that counted occurrences without placing
      them — the `ponytail` on `_repeat` and the open half of defect 7. So it
      places them, alone among the four patterns, and the reason it can is that
      a translation is exact in the ledger where a rotation and a reflection are
      not. That leaves the ledger deliberately asymmetric, with the reason
      written where both halves are.

      **`move_face` landed on 2026-09-07 and this item stays open for the
      other two.** It was taken first of the three because it is the only one
      of them that adds a *kind* of reach rather than a feature: a translated
      STEP body has no sketches and no parameters, so `import_geometry` could
      read a part for DFM and then change nothing about it, and every other
      operation in the schema needs geometry it created itself. Tier 2's own
      note says as much.

      Measured in the simulator and exactly, which is the part worth recording:
      a planar face of area A translated by v changes the solid by `A*(v·n̂)`,
      so the arithmetic is a dot product rather than an estimate, and a face
      slid along its own plane changes nothing for the same reason it changes
      `A*d` when pushed along its normal. Both fixtures agree with the hand
      derivation to the digit — `lifted_face` +6.4000 cm³ and `widened_wall`
      +0.2400 — which makes them predictions a live run can break rather than
      numbers to be copied down.

      **The COM half has never executed, and unusually the signature is not
      known either.** Every other call in that backend was read off a type
      library before it was written; here `FEATURE_COVERAGE.md` records only
      that `MoveFaceFeatures` has `Add` and `CreateDefinition`, and not what the
      definition's setter is called. So the backend tries three spellings that
      can only mean direction-and-distance and names every one it tried when
      none work, rather than one guess that fails as "Exception occurred".
      `PREDICTED["move_face"]` is at the placeholder 0.50 accordingly — the
      arithmetic would justify an extrude's 0.02 and nothing has run.

      **`thicken` landed the same day, and it closes half of what this item
      asked for.** Tier 2 described it as "turning a surface into a wall", and
      that half is not reachable at all — not because of the schema, but
      because *no operation in this server creates a surface*. Everything here
      builds solids, so the only surface a part could hold is one that arrived
      through `import_geometry`, and thickening that would work today. What did
      land is the wall-thickness half: a layer added to or removed from faces,
      each along **its own** normal, which is what makes it a different
      operation from `move_face` rather than a spelling of it — a box's four
      walls point four ways, so `positive` + `join` grows all four outward in
      one operation where a single named direction would push two out and two
      in. That is the DFM wall remedy, and `dfm/discover.py` has counted a
      thicken feature's thickness as evidence of the `wall` role since before
      one could be built.

      **Its uncertainty was a side and a corner rather than a number**, which
      is what made it worth reading twice — and both were measured the same day
      (see the item further down). The arithmetic is exact per planar face and
      first-order on a curved one. But it rested on `THICKEN_SHARE` in
      `backend/base.py`, which says which side of a face a `negative` layer
      lies on — sound set algebra about a boolean against a slab, and silent on
      whether Inventor agreed. A tolerance cannot catch being wrong about a
      side: defect 5's `trim` was 1.2% apart while keeping the opposite half of
      the part. So `thinned_wall` was shaped so that the three ways it could go
      were three different numbers, and `thickened_walls` reported which of two
      defensible corner answers Inventor gives rather than asserting the one
      the simulator summed. It gave the other one, and the simulator gained the
      corner term. One table lives above both backends rather than a copy in
      each, because two self-consistent halves disagreeing is exactly how
      defect 5 survived three runs.

      One more thing was handled in the code rather than left to a run, and the
      run then made it unnecessary: `ThickenFeatures.Add` takes a variant and
      two enum *integers*, so a wrong argument order need not raise — Inventor
      would accept a thickness of 20,481 and build a part the size of a house.
      Until the signature was read there was a preferred definition route (named
      properties cannot be misordered), a never-permuted argument list, and a
      result measured against the area-times-thickness prediction and refused
      outside a factor of four with the feature deleted. The definition route
      turned out not to exist and the signature turned out to be readable, so
      the guard and the attempt list are both gone: `_call_named` puts the names
      at the call site, and a permutation is not possible when the names are
      there.
- [ ] **Measure `sketch_driven_pattern` against a live Inventor** — attempted
      2026-09-07, and the call's *shape* was wrong. Inventor's wrapper answered
      "Add() takes from 1 to 2 positional arguments but 5 were given":
      `SketchDrivenPatternFeatures.Add` takes **one Definition**, so the three
      named arguments through `_patterned` could never have worked here. It now
      builds a definition, sets the compute type on it, and calls
      `Add(definition)`.

      Where the definition comes from is not published — `--search
      SketchDrivenPattern` gives `Add` and nothing else, no factory and no
      definition class — so two spellings are tried and the refusal prints what
      the live collection offered. `python scripts/probe_definitions.py` asks it
      directly, in the same pass as `move_face`'s.

      Two things from before the run still hold. Its arguments are three
      different COM types so a wrong order raises, which is why trying a
      factory's arguments is safe where guessing `thicken`'s were not; and its
      arithmetic is the rule the other patterns already confirm at 0.02. What
      still needs a seat is one semantic question whose answer is a *count*
      rather than a volume — whether Inventor also places an occurrence on the
      reference point — and two of its three readings are the same volume, so
      the check prints the feature list.
- [x] **Measure `thicken` against a live Inventor** — *(2026-09-07, Inventor
      2027.1.)* Two questions, one fixture each, and neither was the magnitude:
      on a single planar face `thicken` and `move_face` coincide by
      construction, so what needed a seat was which side a `negative` layer lies
      on and what Inventor does with the corner notches four grown walls leave
      behind. Both answered, and the second one changed the simulator.

      **The side is what the table said.** `thinned_wall` removed −0.2400 cm³
      against −0.2400 derived, so a `negative` layer lies behind the face where
      the material is and `THICKEN_SHARE` in `backend/base.py` has it right.
      Three readings were distinguishable and it came back the first.

      **The corners close, and the mock was 1.7% low.** `thickened_walls` came
      back +1.4640 cm³ where the four layers sum to 1.4400: Inventor fills the
      1 × 1 × 6 mm notch at each corner, 4 × 6 mm³ = 0.0240, and the sum is
      exact. `_thicken_corners` now derives `(share × t)² × h` per pair of
      selected faces with perpendicular normals, both fixtures agree to four
      decimals, and `PREDICTED["thicken"]` came down 0.50 → 0.02. This is the
      item working as intended: the fixture was shipped reporting *which of two*
      rather than asserting one, because a check that picked one would have been
      inventing the answer it then confirmed.

      **And reading the signature first was worth the second it cost.**
      `ThickenFeatures.Add(Faces, Distance, ExtentDirection, Operation,
      [AutomaticFaceChain], [CreateVerticalSurfaces], [AutomaticBlending])`.
      `CreateThickenDefinition` does not exist on this release, so the route the
      backend tried first was reaching for a method that never has; and there is
      no `IsOffset` argument, so the `False` passed in slot 4 for the offset
      mode's sake was landing on `AutomaticFaceChain`, where `False` also
      happens to be right. A value passed for a wrong reason that happens to be
      right is not a measurement, and only the signature told the two apart. The
      attempt list and the factor-of-four result guard are both gone with it.
- [ ] **Measure `move_face` against a live Inventor** — attempted 2026-09-07 and
      it did not build, which narrowed the question rather than answering it.
      Split from the item above for the reason the work axis was: a tick
      covering unmeasured COM is the claim this file exists not to make.

      **What the run eliminated.** `MoveFaceFeatures.Add` takes one Definition,
      `CreateDefinition` produces one, and none of the three candidate setters —
      `SetDirectionAndDistance`, `SetDirectionMove`,
      `SetDirectionAndDistanceMoveData` — is on it. The type library will not
      answer the follow-up either: `MoveFaceDefinition` carries a `MoveFaceType`
      and a `MoveFaceTypeDefinition` whose classes are published nowhere, and
      `--search MoveFaceType` finds nothing at all.

      **So the next reading is a live probe, not another signature**: `python
      scripts/probe_definitions.py` asks the objects themselves what they offer,
      because `dir()` on a live COM object answers what the type library will
      not. The same narrow setter list is now tried on the child object too, and
      the refusal prints what both objects offered — so one run answers it even
      if the widening misses.

      What the run then has to answer is in `INVENTOR_SETUP.md`, and the short
      version is unchanged: "it built" proves nothing — the two fixtures are
      shaped so that a selector reaching a different face, a direction read
      relative to the face rather than the model, and a distance expression that
      never reaches Inventor's dimension each show up as a different wrong
      number.
- [x] **`save_part` names the conflict** when the path is already open —
      defect 3. *(2026-09-07.)* The item said "names the conflict" and the fix
      turned out not to be a message at all: Inventor's refusal to overwrite a
      file it has open is a bare "Exception occurred" with nothing in the
      ErrorManager, so there was nothing to translate. The conflict is knowable
      *before* the write, so it is refused there — with the filename, the
      document handle holding it, and both ways out.

      Two things worth recording. **The check belongs below the tool layer**, and
      not only for the usual reason that a rule enforced on one path is not a
      rule: `list_documents` reads Inventor's own `Documents` collection on the
      COM backend, so it sees a file the *user* opened in the UI, which the
      session's own registry never can. So the guard lives on `Backend` and both
      implementations inherit it. **And the own-path case is checked before the
      listing rather than by comparing ids**, because on COM the ids are exactly
      what cannot be trusted — `document_path`'s note records an id-to-id match
      over that listing once matching nothing at all — and an in-place save
      written longhand has to keep working however the ids compare. Both are
      pinned by tests, one of them against a backend whose ids never match.

      *Measured live 2026-09-07:* all three save checks pass on 2027.1 -- the
      first save writes the file, the second is refused by name, and closing the
      holder makes the path writable. So the remedy the hint puts in front of a
      caller is real, which was the half a test suite could not answer.

### Phase 3 — drawings

The market's 2026 feature, and a gap in the whole open-source field.

- [x] A **`DrawingRecipe` root** beside `PartRecipe`: sheet and template,
      views by direction, and — the differentiator — **which parameters to
      dimension**. DraftAid guesses which dimensions matter and reaches
      80–90%; a recipe *knows*, because the parameters are the design intent.
      *(2026-09-07. In `schema.py` beside `PartRecipe`, with `DrawingViewSpec`
      carrying `dimension` as a list of parameter names — or expressions of
      them, for the dimension a sheet states and the model derives.)*
- [x] Everything below the schema (resolution, expressions, units) is reused
      unchanged; the drawing's dimension values are `Resolved` like every
      other number. *(2026-09-07; `resolve.Resolver` seeded from the part's
      rehearsal, so a dimension carries the expression that produced it and a
      sheet can be read in inches from a part modelled in millimetres.)*
- [ ] **The round trip is the test.** Build the part, generate the drawing,
      run `check_against_drawing` on the result, and every dimension has to
      reconcile with the part it was drawn from. Measured, not assumed.

      **Half of this is done and it is the half that needed no Inventor.**
      `drafting.rehearse_drawing` turns the ledger into a `DrawingReading` and
      puts it through `drawing.compare` — the same function the reading
      direction uses, reused rather than reimplemented, with only its
      vocabulary turned round. What is left is the *generate* step: nothing
      creates a `DrawingDocument` yet, so no sheet has been produced and
      nothing has been read back off one.
- [x] Simulator support is a drawing *ledger* — which views, which dimensions,
      which parameters reached them — not a renderer. *(2026-09-07, in
      `drafting.py`. No picture, deliberately: the simulator's job is to say how
      far a prediction is trusted, not to be a second renderer.)*

      **What the ledger finds on its own is worth more than it sounds.** A
      drawing can be wrong in ways the part cannot correct, and the one that
      matters is a number the part states that the sheet never gives — an
      under-dimensioned drawing. It is the only drawing fault that is invisible
      when you look at the sheet, because every dimension on it is correct. It
      falls out of `compare`'s `invented` list read in the other direction: a
      number the model asserts and the drawing does not give is not an invention
      when the drawing is the thing being produced, it is a dimension nobody
      asked for.
- [x] **Produce the sheet.** *(2026-09-07. Four `Backend` methods —
      `new_drawing`, `place_view`, `retrieve_dimensions`, `read_drawing` — on
      both backends, with the simulator's half measured and tested and the COM
      half never executed.)*

      **Dimensions are retrieved, not placed**, and that is the design decision
      worth recording. The item above imagined "dimensions placed against the
      geometry a named parameter drives", which would mean working out which two
      drawing curves a parameter drives — the guessing a recipe exists to avoid.
      The parts this server builds make a better route available: every sketch
      dimension carries a parameter's expression and every driven feature value
      is a named parameter, so Inventor's own retrieve-model-dimensions produces
      dimensions that *are* the parameters, and a dimension on the sheet cannot
      then disagree with the part.

      The price is that retrieval brings *every* model dimension onto the view,
      so the asked-for ones have to be kept and the rest removed — which
      requires asking a retrieved dimension which parameter it came from.
      **Nothing here has ever held a `DrawingDimension`**, so that is the one
      fact the whole approach rests on and the first thing a live run must
      settle. If it cannot be asked, the design goes back to placing against
      geometry, which is a much larger piece of work.

      What the simulator's half is worth on its own: it catches a parameter that
      drives nothing and so has *no model dimension to retrieve*, which no
      static check could know, because the parameter exists and resolves
      perfectly well. And it closed the round trip's shape — the sheet is read
      back and checked, rather than the request being trusted.
- [x] **Projected views, so the projection angle means something.**
      *(2026-09-07.)* Until this, every view was a base view at a position the
      recipe gave, and `DrawingRecipe.projection` was recorded and applied to
      nothing — the sheet stated a convention it did not follow, which is worse
      than either convention because a reader trusts the symbol.

      A view with a `parent` is projected from it, takes its parent's scale, and
      is **not** positioned by hand: giving both `parent` and `at` is refused,
      because where a projected view lands is what first and third angle *mean*.
      Third angle draws the top view above the front view; first angle below,
      and the right-hand view swaps sides with it. The two are mirror images
      about the parent and a test says so. An isometric is exempt from the flip:
      it is not a projection of anything, so neither convention has an opinion
      and negating its corner would move it for no reason.

      Also here: **PDF export**, which is the format a drawing is actually sent
      in — a sheet exportable only as DWG needs Inventor at the other end.
- [ ] **Measure the drawing surface against a live Inventor** — attempted
      2026-09-07 and it stopped at the first call, on the argument nobody had
      checked. `new_drawing` was the one call in the surface said to carry no
      risk: `Documents.Add` is measured and `kDrawingDocumentObject` has been in
      the constants table for months. Both true, and the run answered "Creating
      the drawing document failed: Exception occurred." `Documents.Add` takes a
      *path*; the shipped recipe says `"ISO.idw"`; a bare filename is not a
      path. `_drawing_template` now resolves a bare name against
      `FileManager.TemplatesPath` and its immediate subfolders -- an ISO install
      keeps `ISO.idw` one level down, under a locale or a `Metric` -- and
      failing that refuses with every path it tried — **unverified**, and the first thing the next run reaches. The
      lesson is not about templates: two calls were measured, the argument
      between them was not, and "carries no risk" was a claim about a call
      rather than about a call and its arguments.

      `GeneralDimension` and `DrawingDimensions` have no generated module to
      read, which is not the same as their being absent — makepy generates what
      a document has needed, and no drawing document had been opened. So the
      signature reading and the run are the same step here.

      The ordered list is in `INVENTOR_SETUP.md`, and three of its checks are
      ones the simulator can never be evidence for: whether a view's *extent*
      agrees with the part (in the simulator the extent is computed from the
      part, so the check compares it with itself); whether a direction's name
      describes what you get — defect 4's drawing-shaped cousin, where
      `capture_view`'s `front` returns a top view; and whether Inventor's idea of
      first and third angle is the one implemented here, which a *projected*
      view answers in a way a base view cannot, because nothing asserted its
      direction.

### Phase 4 — assemblies

The dear one, for the reason `ARCHITECTURE.md` gives: an assembly is
constraints between components, and a constraint here carries an expression.

- [ ] An **`AssemblyRecipe`** whose components are part recipes or files and
      whose mates have offsets that are expressions — `plate_t + shim`, not
      `8.5`.
- [ ] `geometry.py`'s discipline one level up: a mate that no parameter drives
      is refused, exactly as an undriven sketch dimension is now.
- [ ] The session already holds many documents and the COM backend already
      names an `AssemblyDocument`; the plumbing is in place, the schema and
      the constraint solver's error reporting are the work.

### Phase 5 — sheet metal

A parallel feature set with its own document subtype, its own rules, and a
simulator that has to learn what a bend is. The same five-file shape, forty
times. Not started until Phases 1–3 are done, because every one of those forty
will land in the split `builder.py`, not the current one.

### Throughout

- Run against **ParamCAD-AgentBench** and **CADEngBench** as the external
  yardstick, alongside `examples/expected/`.
- Every live run regenerates `examples/expected/` where the arithmetic has been
  checked, and only there.
- `scripts/dump_constants.py` runs against every new Inventor release before
  anything else does.

## Keeping this file true

This is the document most likely to rot, because a roadmap is a list of things
that are not yet so. The rule is the one `DECISIONS.md` applies to every other
document: a roadmap that has drifted is worse than none. So a phase item is
ticked in the same commit that makes it true, an item that is abandoned is
struck through with a sentence saying why rather than deleted, and a new phase
is added below the last one rather than reshuffling what is here.
