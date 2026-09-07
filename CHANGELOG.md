# Changelog

Notable changes, newest first. Dates are when the work landed, not a release.

## Unreleased

### Changed
- **The work axis is confirmed, twelve of twelve.** Sixth live run, Inventor
  2027.1: **0.18636 mm measured against 0.18636 derived** — a prediction met
  head-on rather than a derivation reconstructed afterwards, which is what the
  fifth run's tick had rested on.

  Three readings agree on one mechanism, which is what makes it a measurement
  rather than a number that came out close: the magnitude matches the
  derivation; the same bolt circle about `z` measures 0.37273 mm away, so the
  axis is not on the origin; and the volume is **unchanged** across the
  parameter move, which is the independent signal that no hole was clipped and
  the derivation's precondition therefore held. That third note did not print on
  the fifth run — its absence was the clue that the geometry, not the axis, was
  the problem.

  `INVENTOR_SETUP.md` now records the expected output of the whole check, so a
  future run has something to compare against rather than only a pass count.
- **The save conflict's live half is measured.** All three save checks pass on
  2027.1: the first save writes the file, the second is refused by name, and
  closing the holder makes the path writable. So the remedy defect 3's hint puts
  in front of a caller is real — the half no test suite could answer.

### Changed
- **The work axis is measured, and the last failure was the prediction rather
  than the part.** Fifth live run, Inventor 2027.1: the same bolt circle about a
  created `normal_to_plane` axis and about `z` measured **0.37273 mm apart**, so
  the axis is genuinely off-centre, and it moved **0.12263 mm** when `bolt_x`
  went 30 → 45.

  The check called that a failure against a derived 0.18640, and the part was
  right. The derivation is one line — six bores removing 1.17810 cm³ centred on
  the circle, moved 15 mm, out of a remaining 94.82190 cm³ — and it holds only
  while every hole is on the plate. At `bolt_x` 45 the hole at θ=0 sits at
  x = 60, exactly the plate edge, and Inventor cuts away half of it. Deriving
  the clipped case by hand (five whole bores plus a half-disc whose centroid
  sits 4r/3π inside the edge) gives **0.12263 mm** — Inventor's figure to five
  decimal places.

  So the geometry now runs `bolt_x` 20 → 35, which keeps every hole on the
  plate, and `_bolt_circle_prediction` **refuses to return a figure** when one
  would be clipped, naming the reason instead. A prediction whose assumptions
  are not met is not a looser prediction; it is a different question's answer.
  `DECISIONS.md` records the rule and its corollary about reading results: this
  check reported "parametric in name only" four times and pointed at the wrong
  thing on three of them, and what broke the deadlock was noticing the volume
  had moved while the centre of mass had not.

  The roadmap item is ticked. Getting there cost four defects, none of them the
  thing being measured — **8** the sketch labels, **9** the missing rebuild,
  **10** the unit-like parameter name, **11** the carrier point on the origin —
  and the run history is kept under the tick, because a tick is the least
  informative thing the item produced.

### Fixed
- **Every `normal_to_plane` work axis ran through the origin** — defect 11,
  found on the fourth live run and the actual cause of a failure three earlier
  runs had misread. `_carrier_point` created its point as
  `PPoint("point1", construction=True)` — **with no position** — and left a
  driving dimension to place it at `at`. Every other `PPoint` in `geometry.py`
  is created at its coordinates and then dimensioned to hold it there; this was
  the only exception. Built at (0, 0), Inventor infers a coincidence with the
  projected origin, that coincidence pins both degrees of freedom, and the
  dimension cannot move the point. So the point stayed on the origin, and so did
  the axis through it.

  **It hid behind its own symmetry.** A bolt circle about an axis on the origin
  has its centroid at the origin, so the centre of mass does not move when the
  driving parameter does — which is precisely the reading the check was written
  to interpret as "the axis is parametric in name only". It reported that three
  times while pointing at the wrong thing. The two faults fixed on the way
  (defect 8, the sketch labels; defect 9, the missing rebuild) were both real
  and neither was the cause.

  What separated them was one more number: **the volume changed while the centre
  of mass did not.** The pilot hole is an ordinary sketch point, built at its
  real position, so it moved as asked; the pattern axis did not. A parameter
  that moves some geometry and not the rest is not a parametric failure, and
  that is what said the axis itself was misplaced.

  The sign is the second reason this cannot be left to a dimension: the
  dimensions are `abs()` of each coordinate, so from the origin a dimension of
  30 says nothing about which side.

  `check_work_geometry` now builds the same bolt circle about the created axis
  and about `z` and requires the two to measure apart — the assertion that would
  have caught this on the first run. The volumes would not have: they agree to
  six decimals whether the axis is right or wrong.

### Changed
- **The `list_features` divergence has its missing fact.** On a part carrying one
  created work point, 2027.1 reports `work_planes` as
  `['YZ Plane', 'XZ Plane', 'XY Plane']`, `work_axes` as
  `['X Axis', 'Y Axis', 'Z Axis']` and `work_points` as
  `['Center Point', 'Datum']` — so Inventor's origin geometry does sit in those
  collections and a created one is the extra entry. Recorded rather than acted
  on: filtering by name would break on a rename or a localised Inventor, and the
  positional rule the numbers suggest wants confirming on another release before
  `edit_feature` and the DFM loop rely on it.

### Fixed
- **A parameter change rebuilt nothing, so every measurement after it was of the
  part as it had been** — defect 9, found on the third live run.
  `document.Update()` is called from `_batch`, and `set_parameter` was **the
  only mutating call in the COM backend that did not run inside one**. It set
  the expression and returned; Inventor kept the old geometry, and
  `mass_properties`, `topology_counts` and every export read it.

  Found by measurement rather than by reading: the work-axis bolt circle's
  centre of mass moved 0.00000 mm when `bolt_x` went 30 → 45, against a figure
  derived beforehand of 0.18640. The carrier sketch came back
  `fully_constrained=True` with its one driving dimension in place, which is
  what said the parametric chain was sound and something else was wrong.

  **It reaches well beyond work axes.** `set_parameters` is the tool whose whole
  promise is "change a driving dimension; the model updates", and the DFM loop's
  argument for itself is that it acts, *rebuilds* and re-measures rather than
  reporting and handing off. A loop that drove a parameter and re-measured was
  reading the part it started with. The fix is the existing mechanism, not a new
  one: the edit runs inside `self._batch(document)` like every feature call.

  `check_work_geometry` now reads the parameter back before judging the
  geometry, so the three reasons a measurement can sit still — the parameter
  never took, the model never rebuilt, the axis was not really driven — can be
  told apart next time. Unmeasured until the next run.
- **The work-geometry check was reaching into COM from the wrong thread.** Its
  work-point lookup read `ComponentDefinition` from the script and got "the
  application called an interface that was marshalled for a different thread" —
  precisely the failure `describe_feature`'s docstring already records — which
  then read as a missing work point and cost a run. It is a backend method now,
  `list_work_geometry`, implemented on both backends: a COM object returned to a
  script is not a COM object the script may use.

### Added
- **`list_work_geometry`**, on both backends, reporting the work planes, axes
  and points a part holds. Separate from `list_features` because the two
  backends genuinely disagree there — the mock keeps everything in one list,
  Inventor keeps work geometry out of `ComponentDefinition.Features` — and
  reconciling them needs a fact nothing has measured: whether Inventor's own
  origin planes and axes sit in those collections and how a created one is told
  from them. So this reports what is there and the acceptance run prints it,
  rather than a guess going into the listing `edit_feature` and the DFM loop
  both trust.
- **Why Inventor refuses a parameter name, in the hint** — defect 10, measured.
  Asking 2027.1 for nine names in one document: it took `bolt_x`, `PCD`,
  `pcd_1`, `bolt_pcd`, `dia`, `pitch` and `bolt_spacing`, and refused `cd` and
  `pcd`. `cd` is the candela and `pcd` the pico-candela, so the rule is an SI
  prefix plus a unit symbol, **case-sensitively** — which is why `PCD` is fine.
  Wider than a list could cover (`mm`, `ms`, `kg`, `ncd`, `kA`…), and this
  server's unit table does not know candela, so it cannot pre-empt them.
  `_diagnose_parameter`'s hint names the cause and says to lengthen, underscore
  or capitalise; the acceptance run keeps the probe as a regression check, so a
  release that changes its mind shows up there rather than in somebody's recipe.

  Also answered on that run: the refused `horizontal_align` on every carrier
  sketch **does not matter** — they all come out `fully_constrained=True`, so
  Inventor was declining a constraint it had already inferred, and the "keeps a
  degree of freedom" in that message is over-claiming.

### Changed
- **What the first two live runs actually measured.** The work-geometry check
  ran twice on Inventor 2027.1 on 2026-09-07. After the sketch-label fix below,
  `WorkPoints.AddByPoint`, `WorkAxes.AddByTwoPoints` and `WorkAxes.AddByLine`
  **all execute** — and `com_signatures.py` lists none of them, only
  `AddAtCentroid` and `AddByAnalyticEdge`. makepy writes a module per interface
  and skips members, so late binding is what makes them reachable: a missing
  signature is not evidence of a missing call, which is the clearest case yet
  for the binding choice `INVENTOR_SETUP.md` argues.

  The roadmap item stays open, because "it runs" was never the test. Four things
  came out of the runs instead:

  * **Inventor refused the parameter name `pcd`** with a bare "Exception
    occurred", while taking `bolt_x` in the same recipe, and nothing in
    `RESERVED_NAMES` or the unit table explains it. That blocked the one
    measurement the check exists for — whether the bolt circle moves when its
    driving parameter does. The recipe now uses a name Inventor took, and the
    check probes a spread of candidates chosen to separate the possible reasons,
    so the next run says which names it declines.
  * **`hole` + `bodies` cannot work on Inventor.** 2027.1's `HoleFeature` has no
    `AffectedBodies` property at all. It is structural rather than a version
    quirk: `extrude` is aimed through a definition object before the feature
    exists, and `HoleFeatures.Add...` makes the feature in one call, so there is
    nothing to aim. `rehearse` warns on the field now
    (`_KNOWN_BROKEN_FIELDS`) and names the substitutes; the gap list records the
    measurement; the acceptance check skips it with that reason rather than
    failing every run. The hard error the backend already raised was the right
    behaviour — a hole on the wrong body takes real material out of a part that
    looks finished.
  * **Work geometry does not appear in `list_features`** on the COM backend,
    which walks `ComponentDefinition.Features` while Inventor keeps work planes,
    axes and points in their own collections. The mock puts them all in one list,
    so the two backends disagree. Not fixed by guessing: Inventor's *origin*
    planes and axes live in those same collections and nothing here has measured
    how to tell a created one from them, so the check now prints what the three
    collections hold and asks about the work point the way the code that needs
    it asks — by name, out of `WorkPoints`.
  * **The type library has no `Camera` module**, so defect 4's orientation names
    cannot be measured from a signature read after all. That needs a live probe
    against the object.

  The carrier sketches also print a refused `horizontal_align`, and the message
  claims a lost degree of freedom. That may be over-claiming — a constraint
  Inventor refuses is usually one it inferred for itself — and it matters here
  because a carrier point free to move is a work axis that does not track its
  parameter. Inventor gives no degree-of-freedom count, so the check now reports
  each carrier sketch's `fully_constrained`.

  `tests/test_coverage_gaps_still_true.py` gained a third state to describe this:
  a gap the schema closed and Inventor did not. It now holds three documents
  together — the bullet must stay open, the schema must still carry the field,
  and `_KNOWN_BROKEN_FIELDS` must say Inventor cannot do it — and fails if a
  field is warned about that the schema does not offer. The old two-state model
  called the honest bullet stale, which is how the change was noticed.

### Fixed
- **The simulator no longer invents a centre of mass** — `mass_properties`
  reported the bounding box's centre as the part's centroid, and a box centre is
  not an approximate centroid but a different quantity: it does not move for a
  void at all. The plate the work-geometry check below is built around reported
  `(0, 0, 0.5)` with its bolt circle 30 mm off-axis, and `(0, 0, 0.5)` again at
  45 mm.

  A real centroid does neither. **Derived, not measured** — from the same
  arithmetic that check already asserts against, and stated that way because no
  live run has reached it: six 5 mm bores remove 1.178097 cm^3 centred
  on the circle, so at 30 mm the centroid sits 0.37283 mm off the box centre in
  X, and moving the circle to 45 mm shifts it a further 0.18640 mm. The box
  centre reports zero and zero.

  That is worse than an approximation, because `check_work_geometry` judges the
  off-centre bolt-circle axis by exactly that shift and *skips*, with a note,
  when a backend reports no centroid. A backend reporting a constant one is not
  skipped: it fails, against a work axis that had done its job. So the number is
  gone rather than flagged, and `MassProps.center_of_mass_from` says which it is
  — the simulator's sentence points at `bounding_box` for the box centre, and
  the COM backend names Inventor's own `MassProperties`.

  Not computed from the volume ledger, though the signed prisms hold what a
  centroid needs. It would be real for a plate with drilled holes and wrong for
  this one: a `circular_pattern` moves volume without recording prisms of its
  own, so the ledger knows one bore of six and would produce a figure that moves
  by a sixth of the truth. Worth revisiting when a pattern records its
  occurrences, which is the open `ponytail` on `_repeat`; there is no caller for
  a centroid until then.

- **The roadmap's `ponytail:` count guard could not pass on Windows** — it keyed
  the counts by `str(path)` and looked one up by its forward-slash spelling, so
  the drift test that guards the count only ever reported drift.
- **The save guard no longer walks every open document.** It asked
  `list_documents`, and the first live connection reported **1033 open
  documents** behind an assembly. On the COM backend that listing reads six
  properties per document, scans the held handles by COM identity for each, and
  *registers every document it did not recognise* — so a single `save_part` with
  a path would have minted a thousand session handles and left the next call
  comparing a million COM identities. Caught by reading the connection's own
  reply, not by anything failing.

  The guard now asks `document_at_path`, one narrow question each backend
  answers as cheaply as it can: on COM, one `FullFileName` read per document on
  the miss path and nothing else, with the display name and the held-handle scan
  paid only for a real match. The rule itself stays shared on `Backend`; only
  the enumeration is per-backend, and a test holds that split.

  It also surfaced a case the first version could not name: a document open
  because the *user* opened it in Inventor's UI has no session handle, so the
  refusal now says "opened outside this session" and says to close it in
  Inventor, rather than offering `close_part(document=None)`.
- **The recipe's sketch labels never reached Inventor** — defect 8, found by the
  first live run of the work-geometry check (2026-09-07, Inventor 2027.1) and
  fixed the same day. `build_sketch` on the COM backend creates each entity and
  sets `Construction`, `HoleCenter` and `Centerline`; it has never read
  `primitive.label`. The labels lived only in the `SketchPlan`, on the Python
  side, while three places searched Inventor's own `SketchPoints` and
  `SketchLines` for an entity whose `Name` equalled one — a name no code
  assigns:

  * `_carrier_point`, which places every `work_point` and every
    `normal_to_plane` work axis;
  * `work_axis` with `kind: "sketch_line"`;
  * `_resolve_axis`, which is how a **revolve** finds a named sketch line.

  The run got no further than its first check and blamed the right place for the
  wrong reason: "the carrier sketch did not keep a point named
  `__work_point__`". It was never given that name. Nothing offline could have
  caught it — the mock resolves labels from the plan, so every test passed, and
  the whole COM path is `# pragma: no cover`, so no coverage gap showed either.

  The third caller predates the work geometry, and `docs/ROADMAP.md` recorded it
  as measured: an off-centre bolt circle "was already buildable via a throwaway
  sketch on a perpendicular plane... That was measured before anything was
  written, and it builds clean." It builds clean on the **simulator**. The
  measurement was taken in a session with no Inventor to reach. The roadmap now
  carries that correction under the original sentence rather than a rewrite,
  because the mistake is the one worth keeping visible.

  The fix keeps the entity Inventor hands back at creation: `_entities_by_label`
  builds a label-to-entity map from what `build_sketch` already collected, and
  `_labelled_entity` reads it. Nothing depends on whether a sketch entity's
  `Name` can be assigned, which nothing here has measured. All three callers
  keep the old name search as a fallback, so a stale handle is no worse than the
  behaviour it replaces and the error then names both routes.
  `tests/test_sketch_labels.py` holds the bookkeeping, and a test fails if any
  of the three call sites stops asking.

  **Still unmeasured**: whether `AddByPoint` then works. The run never reached
  it, so the roadmap item stays open.
- **`INVENTOR_SETUP.md` names the corrupt-`gen_py` symptom.** The same run could
  not read the type library —
  `module 'win32com.gen_py....' has no attribute 'CLSIDToClassMap'` — which put
  seven enums on unverified fallback values and stopped
  `scripts/com_signatures.py` starting, while `connect` succeeded and reported
  2027.1 in the same breath. The error names neither Inventor nor the cache, so
  the message is now quoted next to the fix that clears it.

### Added
- **An acceptance check for the five Phase 2 behaviours whose COM half has never
  executed** — `live_acceptance.py --only work-geometry`. `WorkPoints.AddByPoint`,
  `WorkAxes.AddByTwoPoints` and `WorkAxes.AddByLine`, the hole aimed with
  `bodies`, and the save conflict's remedy. **Nothing here has been measured**:
  this is the instrument, written on a machine with no Inventor to reach, and the
  roadmap item stays open until a seat runs it.

  What makes it worth more than "did it run": three of the checks needed a part
  designed so the answer is visible at all.

  * **The bolt circle is judged by where the centre of mass went**, against a
    figure derived beforehand. Six 5 mm bores through a 120x80x10 plate remove
    1.17810 cm^3 centred on the circle, so moving that centre 15 mm shifts the
    remaining 94.82190 cm^3 by 0.18640 mm. Zero means the expressions never
    reached the carrier sketch's dimensions and the axis is parametric in name
    only; a different non-zero figure means it moved somewhere other than where
    `bolt_x` put it. That the pattern built proves neither, because `_repeat`
    counts occurrences and never reads the axis.
  * **The two blocks in the hole-targeting check are different thicknesses**, 10
    mm against 6 mm. `FEATURE_COVERAGE.md` notes a total volume cannot show which
    body was bored, and for equal blocks it cannot — the same bore either way is
    the same volume. Unequal ones make the total say, with no per-body figure the
    `Backend` contract does not expose.
  * **The save check confirms the remedy rather than the refusal**, since the
    refusal is offline logic `tests/test_saving.py` already holds and what needs
    Inventor is that the path is writable once the holder is closed.

  It skips outright on `--backend mock`, because the simulator implements all
  five and would print five passes that say nothing about Inventor. Every recipe
  in it was validated and built against the simulator, and every line of it was
  executed there with the skip lifted, so a typo cannot wait for the CAD seat to
  surface. The bolt hole uses `through_all` with no `direction` — what every
  shipped example does and what an acceptance run has measured — rather than the
  `direction: "negative"` its unit test uses, which has never run against
  Inventor and would risk failing the check on the drill direction while reading
  as a fault in the work axis.

  `scripts/com_signatures.py` gains the three unverified calls, whose argument
  order has never been read from anything, and the `Camera` interface: defect 4's
  orientation names cannot be asserted until somebody measures what each one
  produces, and if `Camera` reports the eye and the up vector then they can be
  measured as numbers instead of judged by eye.

### Fixed
- **A save onto a path Inventor already has open is refused by name** — defect
  3, and the fix is not a better message. Inventor will not write a file it has
  open, and says so with a bare "Exception occurred" and nothing in the
  ErrorManager, so there was nothing to translate. Since each rebuild leaves the
  earlier document open, this was the *normal* case for a second save, and it
  named neither the file nor the document holding it.

  The conflict is knowable before the write, so that is where it is answered.
  `Backend.refuse_a_path_another_document_holds` asks `list_documents` whether
  another open document occupies the target path, and refuses with the filename,
  the handle holding it, and both ways out — `close_part(document=...)`, or a
  different name. Picking one of those for the caller would be guessing which
  copy they wanted.

  **`list_documents` is why this is not a tool-layer check.** On the COM backend
  it reads Inventor's own `Documents` collection, so it sees a file the *user*
  opened in the UI as well as one this session opened — which the session's own
  registry cannot, and which is the case the defect report came from. And the
  guard sits on `Backend` rather than in either implementation, so both are held
  to it and no caller routes around it: the reasoning `apply_parameter` records
  for the freeze guard.

  Saving in place is not checked, because it cannot collide, and neither is
  saving onto the path the document is already at. That last case is settled
  *before* the listing rather than by comparing ids, because on COM the ids are
  exactly what cannot be relied on — `document_path`'s own note records an
  id-to-id match over that listing once matching nothing at all — and an
  in-place save written longhand must keep working however they compare. Paths
  compare through `abspath` and `normcase`, so two names for one file collide
  and a Windows case difference does not hide one.

  `tests/test_saving.py` holds it, including that both backends ask the guard and
  neither carries a copy, and that the own-path case survives a backend whose ids
  never match. Three mutations were checked and each failed the test written for
  it. The live half is unmeasured, as with the rest of Phase 2's COM.

### Added
- **A pattern axis lying flat in the patterned face is warned about** — defect
  7, found on 2026-09-03 while checking whether the roadmap's reason for wanting
  a work axis was true, and open since. A `circular_pattern` turns about an axis
  perpendicular to the face it patterns; a sketch line lies *in* its own sketch
  plane; so a plate sketched on XY whose pattern axis is a line drawn on XY asks
  Inventor to revolve the holes about an axis lying flat in the plate. Nothing
  caught it — `check_recipe` passes it, `validate_recipe` passes it, and the
  simulator returns `ok: true` with a plausible volume, because `_repeat`
  multiplies the seed's volume delta and never reads the axis at all. The
  warning says that outright, because a reader checking volumes learns nothing.

  **A warning rather than a finding**, and the reason is the honest limit of a
  static check rather than caution: a pattern about an in-plane axis is
  meaningless as a bolt circle and a legitimate way to write a 180-degree flip,
  and nothing static tells the two apart. So it names both substitutes —
  `work_axis` with `kind: "normal_to_plane"`, or `mirror`.

  It fires on three certain shapes: an origin axis lying in the seed's plane
  (`x` or `y` under a plate sketched on XY, which is the cheapest way to make
  the mistake since `axis` defaults to `"z"`), a sketch line on that plane, and
  one on a work plane offset from it, following the chain however long. It
  declines on four where an answer was available and would have been wrong — an
  angled work plane, a revolved seed, a `two_points` work axis, and a pattern
  whose axis is right for one seed and wrong for another.

  **The simulator would have got one of those wrong and the check does not take
  its word.** `mock.work_plane` files every work plane against an origin base
  whatever its `kind`, so it believes an angled plane is parallel to its base;
  reading that table would have reported a correct angled-plane recipe as a
  fault. The plane chain is walked from the recipe instead, honouring `offset`
  and nothing else. Only `extrude` and `hole` seed a judgement, because only
  there does the sketch plane describe the resulting faces — a revolve's
  geometry does not sit in its sketch plane, and the shipped belt pulley is
  exactly that case.

  Fires on the reproduction and on none of the eleven shipped examples or seven
  calibration fixtures. `tests/test_pattern_axis.py` holds both directions, and
  the label resolution is narrowed to the sketches that existed when the pattern
  ran — resolving against the finished document lets a later sketch claim the
  name, and that mutation was checked to fail the test.

  This does not close defect 7: the fix is still the simulator placing
  occurrences rather than counting them. The warning makes the mistake visible,
  where `work_axis` only made it avoidable.

### Fixed
- **The gap list said two things a merge had just made false.** Merging the
  Phase 2 work-geometry branch closed two entries in `FEATURE_COVERAGE.md`'s
  *Gaps that are not feature collections* — work axis and work point, and `hole`
  drilling only the primary body — and neither bullet knew it. The list still
  told a reader to reach for an `extrude` cut where a `hole` with `bodies` now
  works. Both are struck through, with the note in each that the COM half is
  unmeasured, and `tests/test_coverage_gaps_still_true.py` holds the list to the
  schema in both directions: a gap declared open while the operation exists
  fails, and so does one struck through with nothing behind it.
- **The roadmap is under the drift rule it was written in.** `DECISIONS.md`'s
  rule is that a fact stated in two places does not merge without a test that
  they agree. `ROADMAP.md` is where that rule was written down, and was the last
  document exempt from it — and it had drifted three ways: it said thirteen of
  fourteen `ponytail:` markers lived in `backend/mock/` when there were sixteen,
  fifteen of them there; it quoted four calibrated tolerances with nothing
  holding them against `PREDICTED`; and one ticked item was dated "the same day"
  without saying which. Found by going looking, not by anything failing.

  `tests/test_roadmap_still_true.py` holds the countable claims, and its
  docstring says which claims it deliberately leaves alone — dated
  measurements, the landscape section, and the phase count, which is a framing
  choice rather than a fact.

  Each claim was then mutated to check the test could actually fail, and each
  mutation was caught by exactly one test. Three of the tests were wrong when
  first written — one called five sound entries undated by reading line by line
  where an item spans several — which is the same false-positive habit the run
  warnings are written to avoid, and is now recorded in `DECISIONS.md` as what a
  drift test has to earn.

### Added
- **A through hole that will drill the near wall only is warned about** —
  defect 1, open since it cost the PCB enclosure a cable route. Inventor's
  through-all extent stops where it first exits material, so a hole across a
  hollow box leaves the far wall solid and the part looks built.

  The roadmap offered two fixes and they were not equally available: **Inventor's
  hole extent has no both-directions option** — Distance, Through All and To,
  with Through All taking a side — so there is nothing to add a knob to. The
  warning is what landed, and it was nearly free: the simulator already counts
  the separate pieces of material each drill axis crosses, because it needs them
  to decide which way the hole goes, and a count above one *is* the condition.
  `rehearse` now reports it, names the substitute (`extrude` with
  `direction: "symmetric"`) and says the step will diverge on volume too, since
  the simulator charges every wall the axis meets.

  Fires on the reproduction and on none of the eleven shipped examples — the
  enclosure included, which has been built with the substitute since. Both
  directions are tested: a warning that fires on a correct recipe teaches the
  reader to ignore the field.
- **`hole` gains `bodies`**, the multi-body targeting `extrude` already had. A
  hole aimed at a second body used to land on the first, which removes real
  material from the wrong place and reports success.

  The simulator needed no new arithmetic: `charge`, `_through_all_distance`,
  `_material_spans` and `_Slab.body` were all already parameterised by body, so
  this threads an argument four functions were waiting for. `extrude`'s inline
  body-number check became a shared `_aimed_body` instead of a second copy.

  A hole is aimed *after* it is built, unlike an extrude: `HoleFeatures.Add...`
  makes the feature in one call, so there is no definition object to put
  `AffectedBodies` on. The COM backend sets it on the finished feature and
  treats a release that refuses as a hard error rather than a warning — the hole
  exists either way, and one on the wrong body has cut a part that looks
  finished. Unmeasured, for the same reason as the work axis below.

  Worth recording because a test went looking for it: the *total* volume cannot
  show that aiming worked. `_through_all_distance` deliberately falls back to
  the bounding box over a point no prism covers, so a bore aimed at the wrong
  body is still charged its full depth and two runs agree to the digit. Per body
  they do not, which is the ledger's reason for never aggregating.
- **Work axis and work point.** A named axis in space, so a circular pattern can
  turn about something other than an origin axis, and a named point to hang one
  on. `kind: "normal_to_plane"` is the bolt-circle case and the default:
  perpendicular to a plane, through a point given in that plane's own
  coordinates, with `at` carrying expressions like every other number here.
  `two_points` and `sketch_line` name geometry that already exists.

  **The roadmap's reason for wanting this was wrong, and checking it first was
  worth more than the feature.** It said a circular pattern could only turn
  about an origin axis. It never could: `resolve_axis` has always resolved named
  sketch lines, so an off-centre bolt circle was already buildable through a
  throwaway sketch on a perpendicular plane -- measured before a line was
  written, and it builds clean. The real reason is geometric: the axis must
  stand perpendicular to the face being patterned, a sketch line lies flat in
  its own sketch plane, and so the workaround asks the caller to do the axis
  mapping in their head on a plane they are not otherwise using.

  Probing that claim turned up **defect 7**: the recipe that gets it wrong --
  a pattern axis lying in the patterned face's own plane -- passes
  `check_recipe`, passes `validate_recipe`, and returns `ok: true` with a
  plausible volume, because the simulator's `_repeat` counts occurrences and
  never reads the axis. This operation makes the mistake avoidable, not
  detectable; `docs/FEATURE_COVERAGE.md` records what detecting it would take.

  **The COM side is unmeasured.** Written in a session with no Inventor to
  reach, so `WorkPoints.AddByPoint`, `WorkAxes.AddByTwoPoints` and
  `WorkAxes.AddByLine` have never executed. The simulator side is measured and
  tested (32 tests). The shorter implementation -- offsetting two origin planes
  and intersecting them -- was rejected for needing the sign of an origin
  plane's normal, which nothing here has measured and which would fail the way
  the `trim` inversion did: silently, with a part that looks right.
  `docs/INVENTOR_SETUP.md` says what a live run must confirm and in what order.
- **Draft, combine, split and boss.** Four more operations, and one that could not
  be built at all.

  * `draft` tapers existing faces about a parting plane. The DFM subsystem has
    always been able to measure draft and complain about it; until now nothing
    could add any. Verified against Inventor: 0.1505 cm^3 off a plate's four walls
    at 2 degrees, where the simulator predicts 0.1508.
  * `combine` booleans one body into another, and `split` cuts the part with a
    plane -- `trim` to throw a side away, `split` to keep both, `faces` to divide
    only the faces it crosses.
  * `boss` places a mounting post with a pilot down it. **Inventor's own Boss
    cannot be created through the API** -- `BossFeatures` exposes no `Add` and no
    `Create` -- so this expands in the recipe into a sketch, a join extrude and a
    hole. Same geometry, still parametric, but it is not a Boss in the browser.
    Expanding at recipe level rather than in the builder means `validate_recipe`
    rehearses exactly what gets built.

  * `rib` is built by hand for the same reason: `RibFeatures.CreateDefinition`
    succeeds and `RibFeatures.Add` then refuses with `E_INVALIDARG` across every
    combination of profile geometry, direction, extent, thickness type and
    `AffectedBody` tried -- fourteen and counting, all written up in
    `docs/FEATURE_COVERAGE.md`. So a rib is its silhouette -- the top edge from
    `start` to `end`, dropped to `root` -- extruded symmetrically about its plane.
    Exact against Inventor at 20.88000 cm^3 flat-topped and 20.40000 sloped.

    It has no draft knob. A moulded rib should thin as it rises, which a planar
    silhouette and a linear extrude cannot do; an extrude's `taper` drafts across
    the thickness instead and measurably *added* 0.00154 cm^3 rather than releasing
    the rib, so the knob was removed rather than left to mislead.

### Added
- **Text and emboss.** A `text` sketch entity and an `emboss` operation, so a
  part can carry a real font-rendered name rather than an approximation of one.
  `{"type":"text","text":"OnlyCat","height":8,"font":"Arial","bold":true}` in a
  sketch on the face to be marked, then
  `{"op":"emboss","sketch":"Name","depth":0.5,"style":"engrave"}`.

  Three things worth knowing, all learned from Inventor refusing them first:

  * `TextBoxes.AddFitted` takes exactly two arguments on this build. Rotation and
    justification are properties set afterwards, not arguments.
  * Inventor defaults text to left-justified from its anchor, so a centred-looking
    recipe ran the text off the edge of the face. The default here is `center`.
  * An emboss whose profile leaves the face is refused with a bare "Exception
    occurred". That is now caught and reported as what it actually is, quoting the
    rendered size of the text in millimetres.

  `AddEmbossFromFace`/`AddEngraveFromFace` take a `TopFaceColor` where a taper
  angle might be expected, so there is deliberately no draft on an emboss. A
  moulded part that needs drafted lettering wants a tapered extrude cut.

  The simulator charges text a share of the font size squared, calibrated against
  what Inventor really removed at three sizes -- within about 3% for Arial, and
  frankly approximate for anything else.

### Fixed
- The cheatsheet said `slot.length` without saying it is measured centre to
  centre, so a "20 x 9 slot" built 29 long. The schema always said so; the
  cheatsheet, which is what gets read, did not.
- The cheatsheet recommended the `concave` edge filter for rounding an inside
  corner without mentioning that it also matches the wall of every slot and
  pocket cut so far. On a bracket with two slots it matched five edges, not one.

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
