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
real redesign. Thirteen of the fourteen `ponytail:` markers (the repository's
word for a deliberate approximation) live in `backend/mock/`, and the worst of
them is structural rather than local: `document.volume` is one scalar,
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

### Phase 1 — foundations *(in progress)*

The four restructures, plus the two debts that were only ever going to be paid
by running against a real Inventor.

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
- [ ] **Live calibration of `PREDICTED`.** Four entries — `coil`, `draft`,
      `emboss`, `split` — sit at a placeholder 0.5 because they have never been
      compared with Inventor. Run `scripts/live_acceptance.py` on a Windows
      machine with Inventor, record the deltas, set tolerances from evidence.
      *Needs the owner's machine; cannot be done from a Linux container.*
- [x] **Drift tests are the rule** (restructure 2). *(2026-09-03.)* Written
      down in `DECISIONS.md` as "a fact stated twice needs a test that the two
      agree", with the six drifts that earned it and the corollary that the
      answer is usually the test rather than removing the duplication.

### Phase 2 — the coil-shaped additions

Each is one request type, one abstract method, two implementations, roughly
350 lines across the same five files. Ordered by how often the lack of it has
actually bitten.

- [ ] **Work axis and work point.** `AxisSpec` already accepts `work_axis`;
      nothing creates one, so a circular pattern can only turn about an origin
      axis. Unblocks every off-centre bolt circle.
- [ ] **`hole` gains `bodies`**, the multi-body targeting `extrude` already
      has.
- [ ] **A both-directions extent on `hole`**, or a warning when a through
      hole's axis re-enters material it did not cut — defect 1 in
      `FEATURE_COVERAGE.md`.
- [ ] **Sketch-driven pattern, thicken, move face** — Tier 2 in
      `FEATURE_COVERAGE.md`, all with public `Add` methods.
- [ ] **`save_part` names the conflict** when the path is already open —
      defect 3.

### Phase 3 — drawings

The market's 2026 feature, and a gap in the whole open-source field.

- [ ] A **`DrawingRecipe` root** beside `PartRecipe`: sheet and template,
      views by direction, and — the differentiator — **which parameters to
      dimension**. DraftAid guesses which dimensions matter and reaches
      80–90%; a recipe *knows*, because the parameters are the design intent.
- [ ] Everything below the schema (resolution, expressions, units) is reused
      unchanged; the drawing's dimension values are `Resolved` like every
      other number.
- [ ] **The round trip is the test.** Build the part, generate the drawing,
      run `check_against_drawing` on the result, and every dimension has to
      reconcile with the part it was drawn from. Measured, not assumed.
- [ ] Simulator support is a drawing *ledger* — which views, which dimensions,
      which parameters reached them — not a renderer.

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
