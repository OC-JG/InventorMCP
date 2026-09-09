# Changelog

Notable changes, newest first. Dates are when the work landed, not a release.

## Unreleased

### Measured

- **`move_face` and `sketch_driven_pattern` both build in Inventor, and every
  fixture in `examples/calibration/` now has a real number beside it.**
  *(2026-09-08, Inventor 2027.1.)*

  | fixture | derived | Inventor | |
  |---|---|---|---|
  | `lifted_face` | +6.4000 cm³ | **+6.4000** | 0.0% |
  | `lifted_face`, `lift` doubled | +12.8000 cm³ | **+12.8000** | 0.0% |
  | `widened_wall` | +0.2400 cm³ | **+0.2400** | 0.0% |
  | `widened_wall`, `grow` doubled | +0.4800 cm³ | **+0.4800** | 0.0% |
  | `spread_pockets` | −1.2000 cm³ | **−1.2000** | 0.0% |

  **The doubling rows are the reading a volume alone cannot give.** The distance
  in each `move_face` fixture is a driving parameter, and doubling it doubled the
  change -- so the expression reaches Inventor's own dimension and the feature is
  parametric in fact rather than in name. That is defect 11's lesson, which cost
  four runs to learn the first time and one fixture to check here.
  `PREDICTED["move_face"]` came down 0.50 → 0.02, the fifth and last operation
  to come off this file's placeholder.

  **What `spread_pockets` did not answer, by its own design.** The occurrence
  count is the only thing about that operation still unknown -- whether Inventor
  places an occurrence on the reference point as well -- and two of its three
  possible answers are the same volume. The run ruled out the third (−1.6000)
  and separated neither of the others: the finished part reads `Plate`, `Slot`,
  `Spread`, because a sketch-driven pattern is **one feature holding its
  occurrences**, so the check's feature count cannot count occurrences.
  `feature.Occurrences.Count` is what would, and nothing here has read it. A
  limitation of the check rather than a finding, and the note it prints now says
  where the answer is.

  Three failed runs came before this, none of them about arithmetic: a wrong
  call shape, a setter name that was a fourth spelling, and an argument order
  that put the distance second. Every one was settled by reading the live
  object's `ITypeInfo` rather than by another attempt, which is the whole
  argument of `docs/INVENTOR_SETUP.md` in one operation.

- **`thicken` ran against Inventor 2027.1 and came out working — the other
  three Phase 3 surfaces did not.** *(2026-09-07.)* One live run, four
  operations, and the value of it was not in the one that passed.

  **The side is what the shared table said.** `examples/calibration/
  thinned_wall.json` -- one wall thinned 1 mm from behind -- removed
  **-0.2400 cm^3** against -0.2400 derived, and left the plate 79 mm wide. So a
  `negative` layer lies behind the face where the material is, and
  `THICKEN_SHARE` in `backend/base.py` has it right. The fixture was shaped so
  the three ways it could go were three different numbers, because a tolerance
  cannot catch being wrong about a side: defect 5's `trim` kept the opposite
  half of a part while measuring 1.2% out.

  **The corners close, and the simulator was 1.7% low.**
  `examples/calibration/thickened_walls.json` -- four walls grown 1 mm outward
  -- came back at **+1.4640 cm^3** where the four layers sum to 1.4400.
  Inventor fills the 1 x 1 x 6 mm notch at each corner: 4 x 6 mm^3 is 0.0240,
  and the sum is exact. That fixture shipped *reporting* which of two defensible
  answers Inventor gave rather than asserting one, and this is what that was
  for -- a check that had picked one would have been inventing the answer it
  then confirmed.

  `_thicken_corners` in the mock now derives the term rather than fudging it:
  two selected faces whose normals are perpendicular share an edge, and the
  notch is a square of the layer's own reach swept along it, `(share x t)^2 x h`.
  A `symmetric` layer reaching half as far gets a quarter of the corner. The
  sign is `+` in both directions, which looks wrong and is not -- growing walls
  leaves gaps to fill, thinning them makes the layers overlap so the union
  subtracts *less*. Both fixtures now agree with Inventor to four decimals and
  `PREDICTED["thicken"]` came down **0.50 -> 0.02**, an extrude's tolerance,
  because what is left is exact prism arithmetic.

- **Reading the signature first was worth the second it cost, and the reason is
  uncomfortable.** `ThickenFeatures.Add(Faces, Distance, ExtentDirection,
  Operation, [AutomaticFaceChain], [CreateVerticalSurfaces],
  [AutomaticBlending])`.

  `CreateThickenDefinition` does not exist on this release. The backend tried it
  first on the reasoning that a definition's properties are *named* and so
  cannot be filled in the wrong order -- sound reasoning about a method that has
  never existed. And there is no `IsOffset` argument: slot 4 is
  `AutomaticFaceChain`, and the `False` passed there for the offset mode's sake
  was landing on a flag where `False` is *also* correct, because chaining would
  extend the selection past the faces the selector named. **A value passed for a
  wrong reason that happens to be right is not a measurement**, and only reading
  the signature told the two apart.

  One measured call needs no guard, so the attempt list and the factor-of-four
  result check are gone with it -- that guard existed because a variant and two
  enum integers can be misordered without raising, and `_call_named` puts the
  argument names at the call site where a permutation is not possible.

### Fixed

- **The first probe could not read anything, and the error blamed Inventor.**
  *(2026-09-08.)* `python scripts/probe_definitions.py` on the CAD machine
  answered `app.FileManager` -> `AttributeError: <unknown>.FileManager`, and
  then died on `document.ComponentDefinition` with *"the application called an
  interface that was marshalled for a different thread"*.

  One cause for both. Inventor's API is apartment-threaded and the connection
  fix pins the backend to a single COM apartment (`backend/com/marshal.py`),
  routing every public method onto it -- so a *tool* never has to think about
  this, because a tool only gets plain data back. **A probe is the exception**:
  its whole job is to hold a live object and ask what it offers, and the proxy
  hands that object back across the thread boundary where it is already dead.
  The `AttributeError` is the worse of the two symptoms, because it reads
  exactly like a property this release does not have. It has it.

  The repository already said so -- `describe_feature` in `backend/base.py`:
  "reading the properties *there* and returning numbers is the only way to ask
  what Inventor actually built" -- and two sibling probes already carried the
  helpers for it. The probe now runs entirely on the apartment and only text
  comes back, and the helpers live in `scripts/apartment.py` instead of being
  copied a third time.

- **The drawing surface stopped at its first call, on the argument nobody
  checked.** *(2026-09-07.)* `new_drawing` was the one call in the drawing
  surface described as carrying no risk at all: `Documents.Add` is measured and
  `kDrawingDocumentObject` has been in the constants table since before anything
  used it. Both true, and the live run answered *"Creating the drawing document
  failed: Exception occurred."*

  `Documents.Add` takes a **path**. The shipped recipe says `"ISO.idw"`. A bare
  filename is not a path, so Inventor refused and named nothing -- the error
  this project has spent the most effort learning not to produce.

  A bare name is what somebody means, though, and Inventor keeps its templates
  in a folder it knows. `_drawing_template` resolves one against those folders
  and their immediate subfolders, takes an absolute path as given, and where it
  finds nothing refuses with **every path it tried** rather than the last. One
  level, not a walk: a template found four folders deep is as likely to be
  somebody's saved copy as the one they meant. The result detail reports which
  template was used and where it came from, replacing a `sheet_from` that only
  said "the template" or "Inventor's default".

  **Which folders, though, was wrong in the first fix -- measured 2026-09-08.**
  It asked `FileManager.TemplatesPath`, on the reasonable-sounding basis that a
  file manager knows where files are. 2027.1's does not have that property: the
  probe got `AttributeError: <unknown>.TemplatesPath` from the same object that
  answered `GetTemplateFile` on the line above, so a real absence rather than
  the apartment-threading artefact that looks identical. Inventor keeps those
  paths on the *project*. `_template_folders` now asks three things in order of
  how well each is established, strongest first: **the folder Inventor's own
  default template is in** (`GetTemplateFile`, measured working, and it follows
  the active project), then the active project's `TemplatesPath`, then
  `FileManager`'s for a release that grows one.

  That first source matters more than it sounds. On the machine this serves the
  default drawing template is a Shared-drive *project* folder rather than the
  Inventor install -- and a **.dwg** rather than an .idw. A project can put its
  templates anywhere, so the folder has to be asked for rather than assumed.

  **The listing then settled the shipped recipe.** The active project's
  templates folder holds `Standard.idw`, `Standard.dwg` and a house
  `OCB_Standard.idw`, and one level down under `Metric\` are `ISO.idw`,
  `DIN.idw`, `BSI.idw`, `JIS.idw`, `GB`, `GOST` and `ANSI (mm)` -- so
  `"ISO.idw"` resolves here, from the subfolder search rather than the folder
  itself. Had this walked only the top level it would still be failing.

  **And with a real path to a real `ISO.idw` it still failed** -- the same bare
  "Exception occurred", on the third run. *(Fixed 2026-09-08, still unverified.)*
  The template dates from an older Inventor and wants **migrating**:
  interactively that is a dialog, and through the API it is silence. Migrating a
  file is opening it and saving it, so `_drawing_from` does that on a failure and
  retries the `Add` once.

  Three things about the shape of that, because it writes to a file this project
  does not own -- here a company template on a shared drive. It happens **on
  failure rather than on the way past**, so a working template is never
  rewritten as a side effect of making a drawing. It saves **only if Inventor
  marks the document dirty**, so a template that was already current is left
  exactly as it was. And `template_migrated` in the result detail says which
  happened, so a run that modified a shared file says so rather than being
  silently helpful. The retry is once: if a migrated template still will not
  make a drawing then migration was not the reason, and a loop would turn one
  bare "Exception occurred" into several.

  The lesson is not about templates. Both causes were in what the call was
  *given*; the enum and the method were fine all three times. "Carries no risk
  at all" was a claim about a call, and a call is its arguments too.

- **`sketch_driven_pattern` was calling a signature this release does not
  have.** *(2026-09-07.)* Inventor's wrapper answered "Add() takes from 1 to 2
  positional arguments but 5 were given". The measured signature is
  **`SketchDrivenPatternFeatures.Add(Definition)`** -- one object -- so the
  three named arguments through `_patterned` could never have worked, and
  `_patterned` is not what builds this. It now creates a definition, sets the
  compute type on it (`kAdjustToModelCompute`, the same measurement that made
  patterning a hole work at all) and calls `Add(definition)`.

  **And on 2026-09-08 the definition was found, by asking the live object's own
  `ITypeInfo` rather than the type library.** The library publishes no factory
  and no definition class; the object lists
  **`CreateDefinition(ParentFeatures, Sketch, BasePoint, ReferenceFaces)`** with
  the last two optional, and it produces a definition carrying `ParentFeatures`, `Sketch`, `BasePoint`, `ComputeType`
  (default 47361, settable -- which is where the `kAdjustToModelCompute`
  measurement goes), `Operation`, `ReferenceFaces`, `AffectedBodies`,
  `AffectedOccurrences` and a read-only `PatternOfBody`.

  So the attempt list is gone: the backend makes that one call, named through
  `_call_named` -- `BasePoint` supplied because a recipe always names a point
  and a centroid is not something the simulator has, `ReferenceFaces` left to
  Inventor because nothing in a recipe says it.
  `CreateSketchDrivenPatternDefinition` is measured absent and is gone rather
  than kept as a fallback. **The COM half of this operation is measured now**;
  what is unmeasured is what the part comes out as -- the occurrence count on
  the reference point.

  A wrong argument order still cannot pass silently -- a feature collection, a
  sketch and a sketch point are three different COM types -- which is why trying
  the factory's arguments was safe where guessing `thicken`'s were not.

- **`move_face`'s setter is a fourth spelling, found by asking the object.**
  *(2026-09-07, then 2026-09-08.)* The first run answered "Nothing on this
  release's MoveFaceDefinition would take a direction and a distance" --
  `SetDirectionAndDistance`, `SetDirectionMove` and
  `SetDirectionAndDistanceMoveData` are all absent -- and `--search
  MoveFaceType` found nothing at all, because the classes those properties
  return are not published in the type library.

  `ITypeInfo` on the live definition gave the whole interface:
  `Faces` (get/put), a **read-only** `MoveFaceType`, a `MoveFaceTypeDefinition`
  that is **`None`** on a fresh definition, a settable `AutomaticBlending`,
  `Copy`, and three setters -- **`SetDirectionAndDistanceMoveType` (3
  arguments, none optional)**, `SetPlanarMoveType` (3, one optional) and
  `SetFreeMoveType` (1).

  Three consequences. The candidate list is gone, replaced by the measured
  name: three reasonable, narrow guesses were unanimously wrong, and one live
  read settled it. **The type is implied rather than assigned** -- calling a
  setter is what makes a definition that kind, so the earlier plan of setting a
  `MoveFaceType` and then filling in its child object was reaching for a shape
  this API does not have; deliberately *not* setting the type turned out right
  for a reason nobody had. And **the third argument is now the only unread
  thing in the call.**

  **A second probe run then named every parameter**, from the same `GetNames`
  call that gives the arity -- which the probe had been discarding by reading
  only `[0]`:

      SetDirectionAndDistanceMoveType(Distance, Direction, DirectionReversed)
      SetPlanarMoveType(PointOne, PointTwo, Plane)
      SetFreeMoveType(Transformation)

  **The distance comes first.** Not the direction-then-distance every version of
  this code assumed. A swap raises rather than building something wrong -- the
  distance is an expression string and the direction is a COM object -- but "it
  would have raised" is a poor substitute for knowing, so the order lives in
  `_MOVE_FACE_SETTER_ARGUMENTS` as data, a test pins it, and
  `_check_move_face_arguments` compares it against what the live object reports
  before every call. A measurement stated in code and never checked against the
  thing measured is the drift this repository writes tests about; here the thing
  measured can simply be asked.

  **And `DirectionReversed` is what `flip` was waiting for.** It went in as
  `-(expression)` for as long as no reversal property had been read; now it is
  the boolean the API provides, and the distance reaches Inventor exactly as the
  caller wrote it -- which is the whole point of carrying expressions rather
  than numbers. The feature detail reports `flip_via` so a part built either way
  says which mechanism carried it.

  The two excluded setters are measured to be what the exclusion assumed:
  point-to-point and a transformation matrix. Neither could have accepted a
  direction and a distance by accident, so keeping the candidate list narrow was
  right -- provably rather than presumably.

  `_parameter_names` had an off-by-one on the way in -- `GetNames` puts the
  member's own name first, so returning the tuple whole made `[2]` read as the
  third argument while being the second. It drops the member name now, and
  `tests/test_move_face.py` pins which end of the tuple is which against fake
  type information, because the fix is tuple arithmetic and the arithmetic was
  wrong.

  **So every argument of every call in this operation is measured, and nothing
  about it has built a part.** That distinction is the point of the section in
  `INVENTOR_SETUP.md` this still sits in.

### Added

- **`scripts/probe_definitions.py`**, which asks live COM objects what they
  offer because the type library will not. *(2026-09-07.)* Its real answer is
  **`ITypeInfo`**: makepy generates a module per type library, so an object
  whose class the library does not publish has no wrapper to read -- but the
  object still answers `GetTypeInfo`, and that names its members, tells a
  property read from a property write, and says how many arguments each takes.
  That is the one thing `com_signatures.py` cannot do, because it reads the
  library rather than the object. `dir()` is printed beside it, and again
  through dynamic dispatch, because the two disagree in a way that matters.

  It asks this of `MoveFaceFeatures`, `MoveFaceDefinition`, whatever
  `MoveFaceType`/`MoveFaceTypeDefinition` return, `SketchDrivenPatternFeatures`
  and its definition -- and prints `FileManager`'s template paths with a listing
  of the `.idw` files actually installed, which is what the drawing failure
  turned on. It builds one scratch plate and closes it; nothing is saved. Off
  Windows it exits with `BackendUnavailableError` and touches nothing.

- **`scripts/apartment.py`**, holding the two things a probe needs to get onto
  the thread that owns Inventor's objects. *(2026-09-08.)* There were two
  identical copies of them in sibling probes and a third was about to be
  written.

  The probe itself needed another pass after that, its own fault rather than
  Inventor's: it read `desc.cParams` off a `PyFUNCDESC`, which carries `args`
  and `cParamsOpt` and no `cParams`, and the `AttributeError` killed the run
  after one line of output. Every member is now read through `getattr` so a
  name and its kind print even when the argument count cannot be worked out,
  and each of the three sections is wrapped so one failure does not throw away
  what the others learned -- exactly the lesson `probe_sweep_and_pattern.py`
  already records about itself.

  It exists because a CAD seat is the scarce thing here -- `INVENTOR_SETUP.md`
  counts six sessions and four defects for one work axis -- and two of the four
  remaining unmeasured surfaces are blocked on the same unpublished shape.

### Changed
- **The published Inventor 2027 API reference was read against the whole
  repository, and four unmeasured COM paths moved onto the calls it documents.**
  A curated extraction of Autodesk's API User's Manual and Reference Manual
  (`help.autodesk.com/cloudhelp/2027/ENU/Inventor-API/files/<Object>_<Member>.htm`,
  read 2026-09-08) confirmed almost everything this repository had measured --
  centimetres and radians, the measured enum values (forty-eight of the
  fifty-one in the fallback table agree exactly; the three render styles are not
  on the pages read), no both-directions hole extent, no `AffectedBodies` on a
  hole, sketch orientation unpublished and therefore measured -- and settled a
  handful of things that had been guessed.

  Three of those four have since **run** on Inventor 2027.1, and the live reads
  under *Measured* above are the account that stands where the two differ: a
  measurement outranks a published signature, and a published signature outranks
  a guess. What the reference contributed, kept because a run does not supply it:

  - **Drawing dimension retrieval** no longer retrieves every model dimension
    and asks each *drawing* dimension for its parameter, a property nothing
    documents and the one fact the design was said to rest on. It follows the
    published 2026.1 pair: `Sheet.GetRetrievableAnnotations2(view)` returns the
    *model's* dimension constraints, whose `Parameter` is documented, so the
    asked-for ones are chosen there and only those go to
    `Sheet.RetrieveAnnotations2` -- one at a time, so each drawing dimension is
    known by the parameter that went in, and remembered so `read_drawing` names
    it from memory rather than from the sheet. The names the old code tried
    (`RetrieveDimensions`, `AddRetrievedDimensions`) do not exist in 2027; they
    stay as the fallback for an older seat. Nothing here has run: the sheet the
    live acceptance made is the run that would reach it.
  - **`thicken`** is the one where the page arrived *after* the measurement and
    agreed with it: `ThickenFeatures.Add(Faces, Distance, ExtentDirection,
    Operation, [AutomaticFaceChain], [CreateVerticalSurfaces],
    [AutomaticBlending])`, and a table saying Thicken has no definition object,
    which is why the `CreateThickenDefinition` the backend once tried first
    could never have existed. Three sources agree here -- the type library, the
    reference and a built part -- and no other Phase 3 surface has all three.
  - **`thread`** calls the published `ThreadFeatures.Add(Face, StartEdge,
    ThreadInfo, [DirectionReversed], [FullDepth], ...)` instead of a
    `CreateThreadDefinition` that exists on no release, with a `ThreadInfo`
    from the published `ThreadFeatures.CreateStandardThreadInfo(Internal,
    RightHanded, ThreadType, ThreadDesignation, Class)` first -- absent from the
    2027.1 makepy wrapper, like `WorkPoints.AddByPoint` was, so called late-bound
    -- and the measured `HoleFeatures.CreateTapInfo`, whose result the
    `HoleTapInfo` page says derives from `StandardThreadInfo`, second. The
    class follows the table and the side (`2B`, `6g`). **Still the one Phase 3
    surface with nothing measured about it**: the builder refuses it and no run
    has reached the call.
  - **`move_face`** got two guards from the page that the live read does not
    give, either side of a signature the two agree on exactly
    (`SetDirectionAndDistanceMoveType(Distance, Direction,
    [DirectionReversed])`). A **sketch line is refused as a direction**, because
    the page allows a work axis, a linear edge or a planar face and nothing
    else. And `MoveFaceType` is **read back before `Add`** -- the page says a
    new definition starts at `kFreeMoveType`, so a setter that was accepted
    without changing the type would build a move defined by nothing. The
    `MoveFaceTypeEnum` values are in the fallback table.
  - **`sketch_driven_pattern`**'s published `CreateDefinition(ParentFeatures,
    Sketch, [BasePoint], [ReferenceFaces])` and one-object `Add(definition)` are
    the same call the live `ITypeInfo` read then named, arguments and optionality
    included -- the two sources agreeing on a shape the type library does not
    publish at all. The three-argument `Add` the backend made before could never
    have worked.
  - **Edge convexity** gains Inventor's own answer, `SurfaceBody.ConvexEdges` /
    `ConcaveEdges`, read once per `select` and keyed by `Edge.TransientKey` --
    placed *behind* the measured boundary-loop method, so it decides only where
    the loops decline (a circular edge, which could never ask for `convex`
    before) and disagrees only in a log line. `convexity_from: "body"` says
    when it did.

  The rule the four follow is written down in `docs/DECISIONS.md`: a published
  signature outranks a guess and not a measurement. `split` is the case it
  ruled the other way -- the reference names `TrimSolid(SplitTool, Body,
  [RemovePositiveSide])` as the documented call and the measured `SplitPart`
  with its inverted side argument stays, because moving a measured path onto an
  unmeasured one risks exactly the quiet side inversion defect 5 was.

- **`rebuild` names a sick feature's health status.** `HealthStatusEnum` is not
  in 2027.1's type library, so the fallback table now carries its fourteen
  published members (`kUpToDateHealth` 11778 -- the value seven verified
  features had reported, so the page agrees with the measurement --
  `kInErrorHealth` 11781, `kCannotComputeHealth` 11783, ...) and each entry in
  `rebuild`'s report carries `status` beside `health_status`. A suppressed
  feature reporting `kSuppressedHealth` is no longer an error. The drawing
  surface's orientation and style names (`kBottomViewOrientation`,
  `kIsoTopLeftViewOrientation`, `kHiddenLineRemovedDrawingViewStyle`, ...) and
  the two missing `ConstraintStatusEnum` members are in the table from the same
  pages, marked as published rather than measured. The 2026-09-08 live run then
  checked all of them against the seat's own library: **78 of 78 values that
  2027.1 publishes agree**, and the absences are the ones the table exists for.

- **`_batch` calls `Document.Update2` and logs when Inventor says a compute
  failed.** `Update2([AcceptErrorsAndContinue]) As Boolean` is documented to
  return False when any entity failed to compute; `Update` returns nothing,
  which is why a feature that could not build was only ever found by the volume
  it failed to move. Logged rather than raised -- every caller already measures
  -- and `Update` is the fallback on a release without it.

### Fixed

- **A promotion the simulator performed happily failed on Inventor, on the
  word.** *(Measured 2026-09-08 on 2027.1: "The feature 'Block' has no drivable
  property 'taper'.")* The recipe field is `taper` and Inventor's
  `ExtrudeDefinition` calls that property `TaperAngle`. The simulator matches a
  promotion against its own feature detail, keyed by the recipe's field names,
  so `taper` was right there; the COM backend matched Inventor's property names
  by normalised equality, so it was nowhere. Two self-consistent halves
  accepting different words, which is defect 5's shape and now defect 13.

  `PROMOTION_ALIASES` in `backend/base.py` holds the words that differ and
  **both** backends resolve through it, so either vocabulary works on either
  side -- above the two rather than copied into each, for the reason
  `THICKEN_SHARE` is. A name that merely *starts with* the request is tried
  second and only when exactly one of them is there, so `counterbore` asks
  which rather than choosing between a diameter and a depth, and a pattern's
  `count` is not read as a counterbore. The refusal lists what the feature does
  carry, and `promote_parameters` reports an error's hint instead of dropping
  it: the unhelpful refusal is what turned a wrong word into a spent seat.
  `definition.Extent` is searched too, since an extrude's taper is on its
  definition and its distance is not.

### Changed

- **A view direction means what Inventor means by it, and the naming follows
  its Y-up convention.** *(Measured 2026-09-08, decided 2026-09-09.)* One base
  view per direction of a 120 x 80 x 8 mm plate: `front` and `rear` span XY --
  the plan -- `top` and `bottom` span XZ, `left` and `right` span YZ with Z
  across. Every recipe here models Z-up, so the same word named two different
  views, and the project's tables said the elevation while Inventor drew the
  plan. Recorded as defect 4 since `capture_view` was measured and left there,
  because a screenshot in the wrong orientation is a nuisance; a *drawing* in
  the wrong orientation is a wrong drawing that looks like a right one.

  No enum remap reconciles them -- `left` and `right` are on the plane they
  already agree on, turned a quarter turn inside it, and no orientation enum
  turns a view. So the choice was Inventor's naming or a camera-built
  vocabulary of our own, and Inventor's won: a recipe asking for `front` gets
  what a person placing a base view by hand gets, `capture_view` and a sheet of
  the same part agree, and both directions of the round trip measure the same
  axes. The cost is written down rather than discovered, in the schema field,
  the Skill and the guide: a plate modelled flat has its plan as its front
  view. `docs/DECISIONS.md` has the reasoning.

  `VIEW_AXES` in `backend/base.py` is now the single copy, above the three
  places that each had one -- writing a sheet, reading one back, and the
  simulator measuring an extent. **The reading side deliberately kept its own
  table**: a `DrawingReading`'s `kind` is the view as the *sheet* labels it,
  the ISO vocabulary where FRONT is an elevation, and sharing one table would
  make every supplier's FRONT view reconstruct as a plan.
  `drafting._VIEW_KINDS` translates at that one boundary and transposes the
  extent for `left` and `right`, whose planes agree and whose axis order does
  not. `tests/test_view_axes.py` holds the two tables and the translation
  against each other, because the failure would be silent: a wrong translation
  does not raise, it assigns 80 mm to the axis that is 8.

  Still open, and now the shared half of defects 4 and 16: **which way is up
  inside the plane.** An extent is a size, so a rotated or mirrored view spans
  the same, and `capture_view`'s `top` renders Z inverted. `place_view` reports
  each view's camera -- eye, target, up vector -- and `--only view-directions`
  prints all seven, so the reading exists and nothing has been concluded from
  it.

### Measured

- **A drawing's dimensions retrieve, and each one is known by the parameter it
  came from.** *(2026-09-08, Inventor 2027.1.)* The fact the whole
  choose-then-retrieve design rests on, and the first evidence for it: two of
  two retrieved dimensions came back named. What it cost to get there was one
  indirection -- `GetRetrievableAnnotations2` offers annotations driven by
  *model* parameters (`d0, d1, d4 ...`), and the user parameter a recipe asks
  for is what that model parameter's **expression** is, so matching on the name
  found nothing on any part this server builds.

  Two more things the same run said about a retrieved dimension. It answers
  neither `ModelDimension.Parameter.Expression` nor `Parameter.Expression`, so
  its expression is the text on the sheet -- `'120,00'`, in the seat's own
  decimal separator; nothing parses that, since a value comes from `ModelValue`.
  And four parameters went missing because the shipped recipe asked for them on
  the wrong views: a retrieval can only offer what a view *shows*, and `thk` is
  a distance along Z that no view of the XY plane can dimension. Defect 16's
  naming decision reaching into the shipped example, which is what it cost.
  Defects 18 and 19.

- **Inventor does not put an occurrence of a sketch-driven pattern on the
  reference point.** *(2026-09-08, Inventor 2027.1.)* The last unmeasured
  thing about that operation, and it took a calibration rather than another
  fixture. `PatternElements` read **4** on a pattern of four points -- which is
  both answers at once: the seed plus three copies, or four copies with one
  landing on the reference. So the same collection was read on a *rectangular*
  pattern of three instances, where the total is not in doubt: it answered
  **3**, so the collection counts the seed, so four is seed-plus-three. The
  recipe's assumption holds, four points describe four pockets, and the
  `elsewhere` filter in the mock's `sketch_driven_pattern` is right.

  A number read off an API is not a measurement until you know what it counts,
  and what told us was a different operation whose answer was already certain.

- **A drawing view's `front` is the plan, not the elevation** -- defect 16, and
  defect 4 on a second API. *(2026-09-08: a 120 x 80 x 8 mm plate's `front`
  view spans 12 x 8 cm and its `top` spans 12 x 0.8.)* Inventor's view names
  are Y-up: its front view looks down Z and shows the XY plane. Every recipe
  here is Z-up. So `_VIEW_ORIENTATIONS` hands each name to the enum that spells
  it the same way and the sheet comes out a quarter turn wrong -- which for a
  *drawing* is a wrong drawing that looks like a right one.

  Not remapped yet, deliberately: two readings cannot rewrite a table of seven,
  and a partly-remapped table leaves some views wrong with nothing to say
  which. `live_acceptance.py --only view-directions` places one base view per
  direction on one sheet, of a block whose three dimensions all differ, and
  reports what each shows -- the whole table in one run. And an extent cannot
  settle which way is *up* inside the plane, since a rotated or mirrored view
  has the same one.

### Fixed

- **The drawing check hid the diagnosis it had been given.** *(2026-09-08.)*
  Retrieval reported "0 of 0 dimensions" three times, and `build_drawing` had
  caught each view's reason into `findings` -- which route ran, how many
  annotations were offered, which parameters they named -- while the acceptance
  check printed none of them. It prints every finding before it asserts
  anything now. And the one retrieval path that could return an empty list with
  no reason at all -- a legacy route that ran, raised nothing and put no
  dimension on the sheet -- raises instead, naming the route and what was asked
  for. `0 of 0` was never a measurement of anything.

- **A promoted angle went into a millimetre parameter.** *(Measured 2026-09-08
  on 2027.1, the run after the property-name fix: "Inventor refused the
  expression '1.5 deg' for 'draft_a' (units 'mm')".)* `set_parameter` defaults
  to millimetres and a taper's expression is an angle. The unit comes off the
  property being promoted now -- `Parameter.Units` through
  `unit_from_inventor`, falling back to a length because every other promotable
  property is one. The simulator resolved the expression's own dimension and
  had always reported `deg`, so this is the same divergence as the property
  name, one layer down.

- **Not one drawing view could be placed, and the reason was never in the
  drawing.** *(Measured 2026-09-08: three views, three refusals, each "Placing
  the view failed: Exception occurred." and nothing else.)* A drawing view is a
  *reference to a model file* -- the sheet records which document it draws and
  re-reads it on every open -- and the part had only ever existed in memory,
  because `build_drawing` builds it and nothing saved it.

  `place_view` checks that before the call and refuses by name, since
  Inventor's own answer cannot. `build_drawing` and
  `build_drawing_from_recipe` take a **`part_path`** saying where to put the
  part, a separate argument rather than something derived from the recipe's
  name because writing a file is the caller's decision -- a part that already
  has a path keeps it and nothing is overwritten. And the base-view call
  reports everything it was given when it fails: the model's file, the position
  in centimetres beside the sheet's own size, the scale and the two enum names.
  Each has been a candidate cause and none is visible in "Exception occurred".
  The retrieval design is still unmeasured, and that is why: with no views,
  `GetRetrievableAnnotations2` was never reached.

### Added

- **`describe_feature` counts a pattern's occurrences**, which is the reading
  `examples/calibration/spread_pockets.json` was built to get and the one its
  successful run could not give. A sketch-driven pattern is *one* feature
  holding its occurrences, so the finished part reads `Plate`, `Slot`,
  `Spread` whether Inventor placed three of them or four, and two of the three
  possible answers are the same volume.

  The property name is unmeasured, so `Occurrences` is asked first and
  `PatternElements` second and the answer carries which one replied. A release
  keeping them somewhere else reports *nothing* rather than zero -- a feature
  with no occurrences and one whose occurrences could not be read are different
  facts, and reporting the second as the first is how a check passes by
  measuring nothing. Only pattern features are asked, so a number does not
  appear under that name on a feature where it would mean something else.
  It lands under **`pattern_elements`** rather than `occurrences`, because that
  word already means two things here: a rectangular pattern's feature detail
  counts every instance *including* the seed, and a sketch-driven pattern's
  counts the copies alone. A number read off Inventor is a third thing whose
  meaning depends on the release, so it gets its own key.

  **Which the run then proved was the right worry.** The read came back 4 on a
  pattern of four points -- both answers at once: the seed plus three copies,
  or four copies with one on the reference. So `_seed_is_counted` calibrates
  it against a *rectangular* pattern of three instances, where the total is
  not in doubt: 3 means the collection counts the seed, 2 means it counts only
  the copies, and the sketch-driven expectation is derived from that. An
  uncalibrated release concludes nothing rather than reporting a defect.
- **A `work_plane` with `kind: "angle"` or `"tangent"` built an offset plane on
  Inventor and reported success.** `WorkPlaneOp` has offered four kinds since it
  was written; the COM backend read `kind` only to spot `midplane` and fell
  through to `AddByPlaneAndOffset` for the rest, so an angled plane came out
  parallel to its base and every sketch on it was drawn in the wrong place. The
  simulator files every plane against its base whatever the kind, so the
  rehearsal agreed with the build. Defect 12 in `docs/FEATURE_COVERAGE.md`.
  Refused now on the COM side with the published
  `WorkPlanes.AddByLinePlaneAndAngle(WorkAxis, WorkPlane, Angle, Boolean)`
  named in the hint, and warned about at rehearsal -- `_KNOWN_BROKEN_FIELDS`
  gained a per-value form, `_KNOWN_BROKEN_VALUES`, because `kind` is set on
  every work plane and broken for two of its four values. The fix proper needs
  a schema field for the axis an angled plane turns about; the roadmap has it.

- **Two probe scripts read live COM objects from the wrong thread, and would
  have failed only on a machine with Inventor.** `scripts/probe_hole.py` called
  `backend._doc`, `backend._require_app` and `backend._sketch` from `main()` and
  then poked what came back; `scripts/probe_convexity.py` took its edges out of
  `backend._topology` and did the same. The backend is pinned to one COM
  apartment — `_pinned` in `inventor_mcp/backend/__init__.py` routes every method
  onto one worker thread — so a live object handed back across that boundary is
  already dead. Touching it raises "the application called an interface that was
  marshalled for a different thread", or a bare
  `AttributeError: <unknown>.SomeProperty`, which reads exactly like a property
  the release does not have. Neither shows up off Windows: both scripts exit with
  `BackendUnavailableError` before reaching the fault.

  Both now do all of their live-object reading inside one `probe()` handed to
  `on_thread`, printing from in there and returning only text and numbers. What
  they measure is unchanged.

  The two helpers this needs — `raw(backend)` to get past the proxy and
  `on_thread(backend, work)` to run a closure on the apartment — existed in three
  places: copied into `probe_sweep_and_pattern.py` and
  `probe_import_and_properties.py`, and inlined again inside
  `probe_hole_styles.py`. They now live once, in **`scripts/apartment.py`**,
  whose docstring is the explanation and whose `__main__` self-check proves both
  against the real proxy with no Inventor involved.

  `Attempts` — the try-it-and-print-it helper both of those probes use — was
  duplicated the same way, and both copies kept a dict of outcomes that nothing
  ever read. It now lives once in **`scripts/attempts.py`**, without the dead
  dict, and both probes describe an outcome the better of the two ways: the
  version that tries `FullFileName`, `DisplayName`, `Name` and `Value` in turn,
  rather than `Name` or the bare type name.

- **`promote_parameters` claimed the part was unchanged; it now measures it.**
  The result carried a sentence — "each promotion holds the property's current
  value, so the part is the same shape it was" — and nothing checked it. The
  tool looped over the entries calling `promote_parameter`, synced parameters,
  and never rebuilt or measured. Almost certainly true, and "almost certainly"
  is what the chamfer estimate that was out by a factor of two also was.

  It matters more than most claims here: promotion is offered as a safe thing to
  do to a part nobody described, usually one imported from STEP, and it is what
  makes the DFM loop able to drive an imported part at all. A promotion that
  moved geometry by a rounding step would be invisible and would become the
  baseline every later round is compared against.

  `identical_geometry` is now the measurement:

  ```json
  "identical_geometry": {
    "volume_before": 19.0625, "volume_after": 19.0625, "volume_moved": 0.0,
    "tolerance": 0.0005, "same": true,
    "bounding_box_before": [...], "bounding_box_after": [...]
  }
  ```

  Four things it was worth being careful about:

  - **Two rebuilds bracket the promotions, not one per entry.** Without the
    second, the "after" reading is of the part as it was and the check could
    only ever say "same" — a measurement that cannot fail. Without the first,
    the baseline is whatever state the caller left the document in, which
    `measure`'s own note says can be dirty, and a stale "before" against a fresh
    "after" reports a difference the promotions did not cause. Per entry would
    say no more and this tool promotes a dozen at a time. A rebuild that reports
    trouble is attached to the result rather than quietly read as proof.
  - **The tolerance is 5.0e-4** — cm^3 for the volume, cm (5 um) for the box —
    which is `scripts/live_acceptance.py`'s existing figure, chosen so a missing
    9 mm hole (0.382 cm^3) cannot hide in it, rather than a new number invented
    here.
  - **A drift is reported, not refused.** Refusing a promotion partway through
    leaves a part part-promoted, which is worse than a promotion whose effect is
    stated. `same: false` comes with both readings and says the difference is a
    fault to chase, not a tolerance to widen.
  - **`null` is a third answer.** A backend that will not report mass properties
    gets `same: null, measured: false` — "I could not measure" is not "it did
    not change".

  The simulator can exercise the comparison and does, on both paths; it cannot
  be evidence for the claim, since its `promote_parameter` edits a dictionary
  and it reports no centre of mass. `live_acceptance.py --only promotion` takes
  the centroid reading too — the one that catches material moving while the
  volume holds still — and `docs/INVENTOR_SETUP.md` records what a live run has
  to confirm.

- **The server would not start from the shipped `.mcp.json`, and no client could
  say why.** Connecting to Inventor failed from Claude and from the DFM tools at
  once, with every client reporting the same four words — `Connection closed` —
  and the suite green throughout.

  The project-scoped `.mcp.json` launched `python -m inventor_mcp` with a bare
  `python`. An MCP client resolves that without the shell's `PATH` and without
  any virtualenv the shell had active, so it was not the `.venv` that
  `install.ps1` fills and registers by absolute path. That interpreter had no
  `mcp` installed; `inventor_mcp/__main__.py` imported the server at module
  level, so `ModuleNotFoundError` was raised before a byte of protocol was
  written and the process exited. The traceback went to stderr, which a client
  discards. `CONNECTION_CLOSED` is all that was left, and it is the same message
  for a missing dependency, the wrong interpreter, a half-finished install and a
  genuine crash.

  The README already warned, in as many words, that a bare `"python"` is the
  commonest reason an MCP server shows as failed. Two facts that had to agree,
  and nothing was checking one against the other.

  Three changes, and the second is the one that matters next time:

  - `.mcp.json` now runs **`scripts/serve.py`**, which needs only a Python — any
    Python, standard library only — and re-executes the server on the first
    interpreter that can import it, preferring the repository's own `.venv`. A
    config file cannot know which `python` a client will find; this one does not
    have to.
  - **`python -m inventor_mcp --doctor`** walks the whole chain — interpreter,
    SDK (naming which server class the shim found), pydantic, pywin32, the
    backend `auto` picks, whether the server assembles, Node, the analyser — and
    prints the repair for anything that is not `ok`. It imports none of the
    things it reports on, so it runs on the install where the server does not,
    and it does not connect to Inventor: a diagnostic that changes what it is
    diagnosing is not one. Dependent checks are *skipped* rather than allowed
    their own verdict, so a missing pydantic is named once instead of four times
    as a broken backend, a broken server and a missing analyser.
  - `__main__.py` no longer imports the server at module level, and answers
    `--doctor` before it tries: that import *was* the silent failure, and the
    doctor's whole job is to run where it fails. A startup failure now names the
    interpreter and the package directory on stderr, because the interpreter is
    nearly always the answer.

  Found while writing the launcher, and worth recording because the fix
  reintroduced the bug it was fixing: a virtualenv's `bin/python` is a symlink to
  the interpreter it was built from, so `Path(venv).resolve() ==
  Path(sys.executable).resolve()` is **true** for a venv that is emphatically not
  the interpreter now running. Identifying candidates by resolved path sent the
  server down the in-process path on the very Python that could not import it.
  The launcher carries "is this the running one" as a flag rather than deriving
  it, and `tests/test_connection_failures.py` builds exactly that symlink.

  `install.ps1`'s final check is now `--doctor` rather than its own inline
  script, so the install and the diagnosis cannot disagree about what working
  means.

- **A healthy server that cannot reach Inventor read as an unexplained
  failure.** On the machine with the seat, every line of the report above came
  back `ok` — interpreter, SDK, pydantic, pywin32, backend, server, Node,
  analyser — which is exactly what ruled the server out and left the COM
  connection as the only thing between the two. The doctor stopped short of it
  on purpose, and that made the one remaining link the one it said nothing
  about.

  `--doctor --connect` is that link. It **attaches** to a session already open —
  `GetActiveObject`, never `Dispatch`, asserted off the syntax tree because the
  docstring explaining the rule contains the word it forbids — so it can neither
  launch Inventor nor take a licence to answer a question about whether Inventor
  was running. Off by default for the same reason the rest of the doctor never
  connects, and because its answer means nothing until Inventor is up.

  What it buys is the HRESULT, and three repairs that `connect` reports
  identically: an unregistered `Inventor.Application` (a repair, not a
  reinstall); `E_ACCESSDENIED`, which is the two processes sitting at different
  Windows integrity levels — the running-object table is per level, so an
  elevated Inventor is invisible to an unelevated server and the reverse; and
  `MK_E_UNAVAILABLE`, nothing in the table at all, which is a **warning** rather
  than a failure, because Inventor being closed is not a broken install and a
  doctor that exits non-zero on a healthy machine teaches people to ignore its
  exit code.

  The elevation mismatch is the one that fits "it worked yesterday". Nothing
  about the install has to change for it to start: somebody launching Inventor
  as administrator once is enough.

  **The first version of that check accused a working install.** It probed the
  ProgID with `pythoncom.CLSIDFromProgID`, which does not exist. The
  `AttributeError` was swallowed by a broad `except` and reported as
  ``` `Inventor.Application` is not registered on this machine ``` — on a
  machine whose Inventor was registered and working, with a hint recommending an
  install repair that would have fixed nothing. Confident, wrong, and pointed at
  the wrong component: the failure mode a diagnostic exists to prevent, produced
  by the diagnostic.

  Two things were wrong, and the API name was the smaller one. The real defect
  was a probe whose own breakage was indistinguishable from the fault it looked
  for. So the question is answered from the registry — `winreg`, standard
  library, and what "registered" actually *means* here, with no API name to
  guess — and the answer is three-valued: registered, definitely not registered,
  and **could not tell**. Only a definite no fails the check; a probe that
  cannot answer says so. `check_inventor` branches on `present is False` rather
  than `not present`, because `None` is falsey and that spelling is the same bug
  again — which is asserted off the syntax tree, since both spellings pass every
  behavioural test.

  `CurVer` is reported alongside, so a session that is registered but not
  running reads as "no running Inventor session to attach to (registered as
  Inventor.Application.28)" — which rules the registration out on the spot
  instead of leaving it as the next thing to suspect.

- **Eight green lines, and the client still could not start the server.** On the
  machine with the seat, every check passed — interpreter, SDK, pydantic,
  pywin32, backend, server, Node, analyser, and a live attach to Inventor
  2027.1 — while the connection went on failing. The reason is that all of them
  describe **the interpreter running the doctor**, which is the one somebody
  typed an absolute path to. A client launches a different command, out of a
  config file, with no shell and no virtualenv, and nothing was checking that
  command. A report of eight `ok` lines was consistent with a client that could
  not start the server at all, which makes it a report that answered a question
  nobody was asking.

  The `clients` check closes it. It finds the configs a client actually reads —
  `%APPDATA%\Claude\claude_desktop_config.json`, the macOS and Linux
  equivalents, and this repository's `.mcp.json` — picks out the entries whose
  command runs *this* server (matched on the command, not on the key being
  called `inventor`, since it may be registered under any name and a config
  naming a different server is none of its business), and launches each one with
  `--doctor` appended. It is not a model of the client's launch; it is the
  launch, and the thing launched answers for its own health. A marker in the
  child's environment stops the probe probing itself.

  Three details earned their own tests. A config that exists and will not parse
  is a *finding*, not an absence — the client cannot read it either, and the
  symptom is identical to the server never having been registered. The quoted
  reason is the child's own verdict rather than the first or last line of its
  output, because a failing launcher's last line is the closing advice
  ("`Then: ... --doctor`"), which says nothing out of context and was what the
  first version printed. And a failing `clients` check no longer prints "the
  server will not start", which is simply false when it starts from the path
  just typed and sends somebody to reinstall a package that was never the
  problem.

  Two earlier tests had to be split apart: they asserted that a healthy install
  reports no failures, which stopped being the same claim once a check could
  fail for the machine's configuration rather than the install's health. The
  report's completeness and the exit code it implies are now separate
  assertions, and `doctor_from` exists so the verdict can be tested against
  findings chosen for the purpose.

- **Starting is not serving, and `clients` was only checking that it started.**
  With every line green and the desktop config naming an absolute virtualenv
  interpreter, the connection still failed — and `ok` on that line meant only
  "the process started, imported everything and exited 0". A server that does
  all of that and then fails or hangs answering `initialize` is
  indistinguishable from the outside: the client reports `CONNECTION_CLOSED`,
  which is what it says about a server it never heard from. The gap between
  starting and serving was the last place the fault could be hiding, and
  nothing was looking there.

  So the probe now speaks MCP over stdio: `initialize`, one line, one answer.
  Raw JSON-RPC rather than the SDK's own client, because what is in doubt is
  the wire, and an SDK client talking to an SDK server is blind to precisely
  the mismatch worth finding. It distinguishes a crash (quoting the stderr the
  client discards) from a **hang** — different faults, one symptom, different
  repairs — and from a stdout that is not clean, which a stray `print` or a
  logging handler left on stdout will do and which makes a client drop the
  connection over output the server thought was harmless. On success it reports
  the protocol version the two ends settled on.

  **The hang check hung.** It closed the stream before terminating the child,
  and closing waits on the buffer lock that the blocked `readline()` holds —
  so it returned the correct answer after 120 seconds against a child that
  slept for 120. The wording was right and the timeout did nothing; against a
  real hung server the doctor would never have come back. Kill first, close
  second, and the test asserts the elapsed time rather than only the message,
  because the message was never what was broken. That one ordering was also the
  whole of a 132-second test file, now 14.

  A `no_client_probe` fixture keeps the tests that only need the *shape* of a
  report from launching servers to get it.

- **The report was green about a machine that cannot do the job.** On a host
  with no Windows there is no CAD seat and cannot be, and "everything the server
  needs is here" is true of the package while being wildly misleading about the
  machine — the whole purpose of this server is driving real Inventor. Not a
  fault to repair but the wrong machine, so `--doctor` now states it above the
  verdict, where it cannot be read past. `README.md` gains the table of which
  Claude can reach a CAD seat and which cannot.

- **A web session could not start the server at all.** A fresh container clones
  the repository and installs nothing, so `inventor_mcp` does not import,
  `pytest` collects nothing, and the `inventor` server in `.mcp.json` exits
  before it can speak — `CONNECTION_CLOSED`, the same four words, from a
  different cause. `.claude/hooks/session-start.sh` installs the package into a
  `.venv`, which fixes both halves in one place: `pip install -e .` into the
  container's own Python fails outright on a distro-managed package pip will not
  uninstall, and `scripts/serve.py` looks for `.venv` first, so the
  dependencies landing there is also what lets `.mcp.json` start a working
  server. It runs only when `CLAUDE_CODE_REMOTE` is set — a hook that rebuilds
  an environment under somebody's feet is a hook that breaks their setup — skips
  the Windows-only `inventor` extra, and treats a private submodule it cannot
  fetch as a warning rather than a failure.

- **The one thing the `clients` probe cannot see, reported anyway.**
  `.mcp.json` names a bare `"python"`, and has to: the file is shared and the
  interpreter is somewhere different on every machine. So the command is
  resolved through `PATH`, the probe launches it from a shell where that
  resolves, and a GUI-launched client — Claude Code inside the desktop app, an
  IDE extension — is handed the environment Windows gives GUI processes
  instead. On Windows the first `python` is frequently the WindowsApps execution
  alias, which resolves in a console and not reliably outside one. A bare
  command therefore probes green here and can still fail there, silently, which
  is exactly the original symptom. The check now warns while still reporting
  that the launch worked: reporting only what it could test would be a claim it
  cannot support.

- **Two cross-platform faults in the session hook's own tests, found by
  reading the diff rather than by CI.** `st_mode & 0o111` asserted the execute
  bit against the *filesystem*, and CPython on Windows derives those bits from
  the file extension — .exe, .bat, .cmd, .com — not from anything stored. So a
  `.sh` reports no exec bit there however it was committed: a test that passed
  on Linux, failed on the Windows machine with the CAD seat (where
  `install.ps1` installs pytest precisely so the suite can run), and asked the
  wrong question on both. What has to be executable is the file **git ships** to
  the container that runs it, so that is what is asserted — `git ls-files -s`,
  expecting `100755`.

  And `.gitattributes` said only `* text=auto`, which hands a Windows checkout
  CRLF. `bash` then takes the trailing `\r` as part of the command and fails
  naming neither the file nor the reason. `*.sh text eol=lf` pins it, with a
  test holding the rule in place. Latent rather than live — the hook runs in a
  Linux container, where the checkout is LF either way — but it was a trap set
  for whoever next runs one of these from a Windows clone.

- **A `.venv` that was also the running interpreter got a redundant subprocess.**
  Found by a test that broke once the session hook created one: `candidates()`
  deduped `sys.executable` behind the venv entry and left it flagged
  not-current, so the launcher spawned a copy of the interpreter it was already
  inside. Harmless and wrong. The earlier assertion ("the running interpreter is
  tried last") was the wrong shape for the property; it is now stated as *it is
  always a candidate and always flagged as itself*.

  Recorded for what it rules out: this SDK negotiates `2024-11-05`,
  `2025-03-26`, `2025-06-18` and its own latest, all four answered, so an
  unpinned `mcp>=1.2` moving under a working install is **not** what breaks a
  client that speaks an older protocol. That is asserted rather than assumed,
  since the dependency floor lets the SDK change without anything here
  changing. Twenty-one tests hold the pair of facts together: what `.mcp.json`
  launches, that the README's warning still stands, that nothing importable-only
  on a healthy install sits at the top of `__main__` or `preflight`, and that the
  explanation goes to stderr rather than the stream carrying the protocol.

- **Five defects in the drawing layer, found by asking for a drawing of all
  eleven shipped parts.** None of them by writing a test first. The fixture the
  drawing tests were built on is a plate with four well-behaved parameters, and
  every one of these needed a part shaped some other way. `ARCHITECTURE.md`
  already argues for keeping the examples executable — two bugs were found by
  writing them rather than by writing tests — and this is that argument paying
  out again, at five for eleven.

  * **A count resolved as a length.** The worst-behaved of the five. Asking to
    dimension the belt pulley's `lighten_count` of 5 resolved it as a *length*
    and put a **50 mm dimension** on the sheet — a number that appears nowhere
    on the part or the drawing. It was caught at all only because that part
    happens to have no 50 mm number either. A parameter that is not a length or
    an angle is now refused with the reason: a count belongs in a callout, and
    the dimension to state is the spacing.
  * **An angle was stated in radians and labelled as an angle.** The moulded
    housing's 1.5 degree draft came out as **0.02618** — its value in radians —
    because an angle was divided by the *length* factor. `compare` then read
    0.02618 as degrees and reported the sheet as stating a number the part does
    not have, so a correct drawing was reported wrong and the cause was a unit.
    The ledger now states an angle in the sheet's own `angle_units`, and the
    reading converts to degrees, which is what `compare` compares in.
  * **A counterbore's own sizes could not be dimensioned.** The hole feature
    recorded its diameter and its style and *not* its counterbore's diameter or
    depth, or its countersink's, so nothing could retrieve them — and those are
    dimensions any drawing of the cover plate carries. The cover plate went from
    6 of its 10 parameters dimensionable to all 10.
  * **Building did not check what rehearsing checked.** A caller who went
    straight to `build_drawing_from_recipe` got a category error reported as a
    dimension that did not arrive — which is what a typo looks like too, and the
    two have different fixes. The refusal now happens before any sheet exists.
  * **"Not on the sheet" did not say why.** The commonest reason is an
    *intermediate parameter*: `pipe_bend`'s `wall` appears only in `tube_od =
    tube_id + 2 * wall`, and `moulded_housing`'s `boss_wall` only in `boss_d =
    boss_hole_d + 2 * boss_wall`. Both drive the part; no dimension states
    either, because the sketch says `tube_od`. So the warning now names the
    parameter that *is* stated — "dimension `tube_od` instead" — and says
    separately when a parameter drives nothing at all, since those two have
    different fixes.

  The sweep is kept as a test rather than thrown away: every shipped part is
  drawn, and what each one cannot state is held as data with a reason. Three of
  the five above would have been caught on the day they landed by that test
  existing.

### Added
- **Projected views, which is what makes the projection angle mean anything.**
  Until now every view was a base view at a position the recipe gave, and
  `DrawingRecipe.projection` was recorded and applied to nothing — the sheet
  stated a convention it did not follow. That is worse than picking either
  convention, because a reader trusts the projection symbol.

  A view with a `parent` is projected from it and takes its parent's scale. It
  is deliberately **not** positioned by hand: giving both `parent` and `at` is
  refused, because where a projected view lands *is* what first and third angle
  mean. Third angle draws the top view above the front view and the right-hand
  view to the right; first angle puts both on the opposite side. The two are
  mirror images about the parent, and a test asserts exactly that rather than
  restating the table.

  An isometric view is exempt from the flip. It is not a projection of
  anything, so neither convention has an opinion about where it goes, and
  negating its corner in first angle would move it for no reason.

  **On Inventor this is a different call that names no direction at all.**
  `AddProjectedView` takes a parent, a position and a style — Inventor is told a
  place and *infers* which way the view faces, the reverse of a base view. So
  the angle is applied before the call, in `drafting.projected_position`, which
  is the one place the recipe's `projection` does any work.

  That has a useful consequence for the live run: **the direction check is
  sharper for a projected view than a base one.** Nothing asserted a projected
  view's direction, so what the sheet reports back is Inventor's own answer to a
  question only the layout asked. A projected top view reading as anything but
  `top` would mean the convention implemented here and the one Inventor applies
  are not the same — and since first angle is the third-angle table negated and
  nothing else distinguishes them, negating it is the whole fix.

  The layout is readable from a rehearsal too, so the angle's effect can be
  checked before any sheet is made, and the shipped drawing is now a real
  three-view first-angle sheet: one base view with the top view projected
  *below* it.

- **PDF export.** `export_model` offers `pdf`, and it is the format a drawing is
  actually sent in — a sheet exportable only as DWG needs Inventor at the other
  end to read. It is a drawing format rather than a part one: a part has no
  sheet to print, and `export`'s written-but-not-there check is what reports
  both that and a machine whose PDF translator add-in is disabled, since
  Inventor answers success either way.

### Added
- **The sheet gets made: four drawing methods on both backends.** `new_drawing`,
  `place_view`, `retrieve_dimensions` and `read_drawing` on the `Backend` ABC —
  so the two implementations cannot drift, which the compiler enforces rather
  than anybody's discipline. The simulator's half is measured and tested; **the
  COM half has never executed**, and it is the largest unmeasured surface in the
  project.

  `build_drawing_from_recipe` places the views, retrieves the dimensions the
  recipe asks for, then **reads the sheet back and checks that**. Reading back
  what you just asked for proves nothing; reading back what is *there* is the
  test, and it is what the round trip has needed all along.

  **Dimensions are retrieved from the model, not placed by geometry**, and that
  is the design decision worth arguing. The roadmap imagined "dimensions placed
  against the geometry a named parameter drives", which means working out which
  two drawing curves a parameter drives — the guessing a recipe exists to avoid.
  The parts this server builds make a better route available: every sketch
  dimension carries a parameter's expression and every driven feature value is a
  named parameter, so Inventor's own retrieve-model-dimensions produces
  dimensions that *are* the parameters, and a dimension on the sheet cannot then
  disagree with the part.

  The price is that retrieval brings *every* model dimension onto the view, so
  the asked-for ones must be kept and the rest removed — which needs a retrieved
  dimension to name the parameter it came from. **Nothing here has ever held a
  `DrawingDimension`**, so that is the single fact the whole approach rests on
  and the first thing a live run must settle. `retrieve_dimensions` deletes what
  it retrieved and fails loudly if none of them will say, rather than leaving a
  sheet carrying every dimension the model happens to hold.

  **No enum value is guessed anywhere in it.** The view orientations and styles
  are named — `kFrontViewOrientation`, `kHiddenLineRemovedDrawingViewStyle` —
  and `_k` reads their values from the type library, raising a message that names
  the fix when it cannot. A wrong name raises; a wrong number is not possible.
  That is a better starting position than the extrude extents had, where 32 of
  51 fallback values turned out wrong.

  **Two checks exist only because the sheet is read back**, and neither could be
  static:

  * **a parameter with no model dimension cannot be retrieved.** Inventor can
    place a dimension only if the model holds one, so a parameter driving
    neither a sketch dimension nor a feature value has nothing to retrieve. No
    static check could know that — the parameter exists and resolves perfectly
    well;
  * **a view that reports facing a way it was not asked to.** Defect 4's
    drawing-shaped cousin: `capture_view`'s `front` returns a top view on a part
    built on XY, and a drawing view reaches Inventor through a similarly-named
    enum. `read_drawing` asks the *sheet* for each view's orientation and extent
    rather than remembering the request, which is the only way that check could
    work.

### Changed
- **Pointing the drawing schema at a shipped part found the thing worth knowing,
  and it took a real recipe to find it.** Asking to dimension the mounting
  plate's `edge_margin` does **not** put 12 mm on the sheet. Dimensions are
  retrieved, and that model never states 12 anywhere — the margin exists only as
  a hole spacing of `plate_w - 2 * edge_margin`, so the sheet carries **96 mm**.
  The holes are pinned either way and the drawing is correct; the number the
  recipe named is simply not the number a reader sees.

  Three consequences, all landed here:

  * `build_drawing` **says so** rather than leaving it to be noticed, and the
    warning names the expression that came back;
  * an **exact match is preferred** over one that merely refers. `plate_w` is
    referenced by the outline's width *and* by that same hole spacing, so
    without a preference the answer was whichever the iteration reached first —
    a drawing asking for the plate's width would sometimes have got its hole
    pitch;
  * the rehearsal and the built sheet **disagree about it on purpose**, and a
    test pins that. The rehearsal resolves `edge_margin` to 12, because that is
    what the parameter is worth; only a sheet that has been made can say which
    dimension came back. That difference is the whole reason the round trip
    reads the sheet rather than trusting the request.

  Also from that first run: looking only at sketch dimensions was the
  simulator's first answer about what can be retrieved, and it reported a
  plate's *thickness* as impossible to dimension — the one dimension a plate
  drawing certainly carries. Inventor retrieves feature dimensions as readily as
  sketch ones, an extrude's distance being a parameter in the model browser, so
  the retrieval reads both.

### Added
- **Phase 3 opens: `DrawingRecipe`, a second root beside `PartRecipe`.** A
  drawing describes *views of* a solid rather than a solid, which is a different
  noun at the top of the document — the first thing in this project that
  `PartRecipe` could not be stretched to cover.

  `{"name": ..., "sheet": "a3", "projection": "first_angle", "views": [...]}`,
  and each view carries `direction`, where it sits on the sheet, its scale, and
  **`dimension` — which of the part's parameters it states.** That last is the
  differentiator and the reason the schema is shaped this way: a drawing
  generator has to decide which dimensions matter, and the field's tools infer
  it from the geometry and reach 80–90%. A recipe does not have to infer
  anything, because the part's parameters *are* the design intent. Naming them
  is a statement the author already made when they wrote the part.

  **Nothing is drawn.** No `DrawingDocument`, no views placed, no title block —
  that API has not been read here and is the largest single piece left in the
  project. What landed is the description of a drawing and the rehearsal of one,
  which is the half that needs no Inventor and is worth having on its own for
  the same reason `validate_recipe` is.

  **`drafting.py` is the new module, and the naming matters.** `drawing.py`
  *reads* — a `DrawingReading` is what somebody wrote down looking at a sheet.
  `drafting.py` *produces* — it works out what a sheet would say and puts that
  through `drawing.compare`, the same function the reading direction uses,
  reused rather than reimplemented. Only its vocabulary is translated.

  **What the ledger finds on its own is the point.** A drawing can be wrong in
  ways the part cannot correct, and the one that matters is **a number the part
  states that the sheet never gives** — an under-dimensioned drawing. It is the
  only drawing fault that is invisible when you look at the sheet, because every
  dimension on it is correct, and a factory cannot make what a drawing does not
  say. It falls out of `compare`'s `invented` list read the other way round: a
  number the model asserts and the drawing does not give is not an invention on
  anybody's part when the drawing is the thing being produced.

  Reported as a **warning rather than a failure**, because it can be deliberate
  — a value a general note covers, or one the reader is meant to derive — and a
  check that refused a legitimate sheet would teach the reader to ignore it.
  Alongside it, a second and independent reading of the same defect: which axes
  of the part's overall size no dimension on the sheet states. Worked out from
  the dimensions rather than from the views, deliberately, because a produced
  view's extent is whatever the part is — so asking a view what it shows would
  be asking the part about itself.

  A value the part *derives* from numbers the sheet does state is not counted.
  `examples/drawings/mounting_plate.json` is the case in point: the plate drives
  its four holes from `edge_margin`, the sheet states that, and the 96 mm pitch
  the part computes comes back under `derived` rather than as a fault. A drawing
  stating the pitch and omitting the margin would pin the same holes and be
  reported as leaving the margin undimensioned — both defensible on paper, only
  one matching the model, which is exactly what this check makes visible.

  Two tools: `rehearse_drawing_recipe` and `drawing_recipe_schema`, taking the
  count to thirty-two. And a shipped drawing of a shipped part, because a schema
  nobody has aimed at a whole eleven-parameter recipe is one whose gaps are
  still hiding — the argument `ARCHITECTURE.md` already makes about keeping the
  examples executable.

  Everything below the schema is reused unchanged, which was the second Phase 3
  item: `resolve.Resolver` seeded from the part's rehearsal resolves each
  dimension, so it carries the expression that produced it, and a sheet can be
  dimensioned in inches from a part modelled in millimetres.

### Added
- **`sketch_driven_pattern`: the last of Tier 2, and the only pattern here that
  places its occurrences.** Copies features to a sketch's points, for a layout
  that follows nothing in particular. The seed sits on `reference` and the
  occurrences go on the other points, so N points describe N of the feature.

  **Two findings changed the shape of this one, and both are worth more than the
  operation.**

  **The gap was narrower than Tier 2 claimed.** It said anything irregular "has
  to be enumerated by hand" — but `hole` already takes a list of points and
  `boss` a list of positions, so an irregular set of holes or bosses never
  needed a pattern at all. What was genuinely missing is patterning a feature
  whose definition is *not* already a list of positions: a pocket, a rib, a
  filleted detail. That makes this the **smallest in value** of the three Tier 2
  items, which is the opposite of what the roadmap's ordering implied, and both
  that file and `ARCHITECTURE.md` now say so.

  **And it was the largest in thought, for the reason the roadmap missed.** It
  is the first operation whose entire input is a set of *positions*, put to a
  simulator that counted pattern occurrences without ever placing them — the
  `ponytail` on `_repeat`. A version that counted the points it was handed could
  not tell a correct recipe from one whose points all miss the part. So this one
  places them.

  **Placement is exact, which is why it is affordable here and not for the other
  three.** A `_Slab` is an outline in one of three origin planes plus a sweep
  along the normal, so translating one is shifting the outline by the move's
  in-plane components and the near and far by the out-of-plane one. A rotation
  or a reflection is only representable that way in special cases. So
  `rectangular_pattern`, `circular_pattern` and `mirror` still count, `_repeat`'s
  marker now says which callers it is talking about, and doing the other three
  is a separate change with a constraint worth writing down: the three shipped
  examples that pattern or mirror agree with Inventor to **0.003%** on
  `seed × extra`, so a placement path has to reproduce that rather than improve
  on it.

  **What the placement bought, as numbers:**

  * a cut driven through where an occurrence went is measured against what the
    pattern left. A 6 mm hole through a copied 4 mm pocket in a 10 mm plate is
    charged 6 mm of material (0.1696 cm³), where the ledger would otherwise have
    said 10 mm (0.2827);
  * the part's bounding box grows to where the copied *prism* is, not to where
    the whole box would be if it were moved — a 10 mm pad copied 200 mm along a
    100 mm plate reaches 205 mm, not 250. That was wrong in the first draft and
    the test that caught it now says why;
  * an occurrence of a *cutting* seed standing over air is reported, and
    `rehearse` turns it into a warning. Deliberately narrow: only asked where
    every feature that added material recorded prisms for it, because
    `_material_spans` answers None both for "no material here" and for "this
    part was never modelled as prisms", and reporting the second as a miss would
    fire a warning on a correct recipe.

  **Slabs now carry the name of the feature that created them.** `source` could
  not answer "which prisms are the seed's" — a part with two extrudes has two
  sets of slabs both saying `extrude` — and a pattern that copies a seed's
  prisms has to know. Each copy is attributed to the *pattern* rather than the
  seed, so patterning a pattern copies the occurrences and a second pattern of
  the same seed does not find them. An unattributed prism is not copied, which
  today means a shell's cavity, and a shell is not a thing anyone patterns.

  **`PREDICTED` is 0.02, not the placeholder**, and the inconsistency beside the
  other two unmeasured operations is deliberate. Its arithmetic is the rule the
  other patterns use and is already measured at 0.02 on the pulley and the
  threaded boss. What is unmeasured is a semantic question instead — whether
  Inventor also places an occurrence on the reference point — which is an
  off-by-one *occurrence*, 33% on a three-point pattern. A tight tolerance
  reports that; 0.5 would hide it.

  That question's wrong answers include one that leaves the volume *unchanged*:
  a duplicate landing exactly on the seed. So `--only sketch-driven-pattern`
  prints the finished part's feature list rather than only measuring it, and
  `examples/calibration/spread_pockets.json` records all three readings.

### Changed
- **Phase 2 is complete.** `move_face`, `thicken` and `sketch_driven_pattern`
  all landed on 2026-09-07, so the roadmap item covering the three is ticked —
  with each one's live measurement as its own open item below it, the same split
  the work axis got, because a tick covering unmeasured COM is the claim that
  file exists not to make.

### Added
- **`thicken`: a layer on faces, each along its own normal.** Tier 2's second
  item, and it closes half of what that item asked for. The half it closes is
  the wall-thickness one, and the property that matters is the normal: a box's
  four walls point four ways, so `positive` + `join` grows all four outward in
  one operation where a single named direction would push two out and two in.
  That is what makes it a different operation from `move_face` rather than a
  spelling of it, and it is the DFM wall remedy -- `dfm/discover.py` has counted
  a thicken feature's thickness as evidence of the `wall` role since before one
  could be built.

  `{"op":"thicken","faces":{...},"thickness":"wall","direction":"positive",
  "operation":"join"}`. `negative` + `cut` thins the walls instead; `symmetric`
  does half either way.

  **The half it does not close is the headline one -- turning a surface into a
  wall -- and the reason is not the schema.** *No operation in this server
  creates a surface.* `extrude`, `loft` and `sweep` all produce solids, so the
  only surface a part here could hold is one that arrived through
  `import_geometry`, and thickening that would work today. Inventor's offset
  mode is absent for the same reason: it *produces* a surface and nothing
  downstream could take one. Recorded in `FEATURE_COVERAGE.md` under Tier 1c
  rather than left as an implied gap.

  **Exact per planar face, first-order on a curved one, and charged either
  way.** A planar face's area times the layer is a prism. A cylinder of radius r
  thickened by t gains `pi*((r+t)^2 - r^2)*h` where `area*t` is `2*pi*r*h*t`, so
  the missing term is `pi*t^2*h` -- second order, and small while the layer is
  thin next to the radius, which is what a wall is. That it is charged at all is
  the opposite of what `move_face` does with the same face, and the difference
  is real: there the move's direction is arbitrary relative to the face so the
  dot product has no answer, while here the direction *is* the face's own
  normal.

  **What is unmeasured about this one is a side and a corner, not a number.**
  `THICKEN_SHARE` says the layer is a slab swept from the face, the operation is
  a boolean, and a face's normal points out of the solid -- so the outward half
  is air, the inward half is material, and `positive` + `cut` and `negative` +
  `join` therefore do *nothing at all*. That is sound set algebra and it is
  silent on whether Inventor means the same side by "negative". So:

  * the two cancelling pairs are **warned about at rehearsal rather than
    refused**. A refusal would prevent the run that settles the question, which
    is the mistake the `shell` `both` enum made -- that refusal was right and it
    hid the fact that nothing had ever exercised the path;
  * the table lives in `backend/base.py`, above both backends, rather than as a
    copy in each. Two self-consistent halves disagreeing about which side
    something goes on is exactly how defect 5 survived three runs, and one table
    cannot disagree with itself;
  * `thinned_wall.json` is shaped so its three possible outcomes are three
    different numbers -- **-0.2400** confirms the table, **0.0000** says the
    layer landed outside the solid and the side is inverted, anything positive
    says something else again. A tolerance cannot catch a side: the `trim`
    inversion was 1.2% apart while keeping the opposite half of the part;
  * `thickened_walls.json` carries the corner question. Four walls grown 1 mm
    outward leave a 1 x 1 x 6 mm notch at each corner belonging to no wall, so
    the answer is **1.4400 cm^3** if Inventor leaves them and **1.4640** if it
    closes them. `--only thicken` reports which rather than asserting one:
    nobody has measured it, and a check that picked one would be inventing the
    answer it then confirms.

  **And one risk is handled in the code rather than left to a run.**
  `ThickenFeatures.Add` takes a face collection, a variant distance and two enum
  *integers*, so a wrong argument order need not raise the way `_profiles`'s two
  forms cannot mislead -- Inventor would accept a thickness of 20,481
  (`kNewBodyOperation`) and build a part the size of a house, successfully. So
  `CreateThickenDefinition` is tried first where a release has it, because a
  definition's properties are named and cannot be filled in the wrong order;
  `Add`'s arguments are never permuted, only its trailing optional
  `VerifyResults`; and the result is measured against the area-times-thickness
  prediction and refused outside a factor of four, with the feature deleted
  rather than left in the part. The factor is deliberately enormous: it catches
  20,481 cm and nothing subtler, because the subtler end is the divergence
  check's job.

  `_topology_collection` gained a sibling, `_topology_selection`, returning the
  matched faces alongside the collection so that prediction comes out of the
  same select as the faces themselves. Two selects would be two chances to match
  differently.

### Added
- **`move_face`: the one way to change geometry this server did not build.**
  Tier 2's third item, taken first of the three because it is the only one that
  adds a *kind* of reach rather than a feature. `import_geometry` could read a
  STEP part for DFM analysis and then alter nothing about it: translated
  geometry has no sketches and no parameters, so every other operation in the
  schema has nothing to take hold of.

  `{"op":"move_face","faces":{...},"direction":"z","distance":"lift"}` --
  faces picked by the same selectors as a fillet, a direction that is anything
  `resolve_axis` accepts, and a distance that is an expression like every other
  length here. `flip` is the only way to reverse it, so there is one spelling
  per move rather than two that have to agree.

  **The arithmetic is a dot product rather than an estimate**, which is the
  reason this operation can be predicted at all: a planar face of area A
  translated by v changes the solid by exactly `A*(v.n)`, its own normal doing
  the projecting. A face pushed along its normal changes the part by area times
  distance; one slid along its own plane changes nothing; and both come out of
  the same expression rather than a rule about which faces count. The two new
  calibration fixtures say +6.4000 and +0.2400 cm^3, each agreeing with the
  hand derivation to the digit, so they are predictions a live run can break in
  the sense `drafted_block` was.

  Only the direction-and-distance move is offered. Inventor's planar drag and
  rotate-about-a-line are deliberately absent: this is the shape a wall
  thickness or a clearance is expressed in, and the other two have no
  predictable result to check against.

  **What the simulator will not answer for, it says so about.** A cylindrical
  face has no single normal here, so the dot product has nothing to project
  onto, and the step declares itself an estimate -- which is what `rehearse`
  reads to leave it out of the divergence comparison, the same seam a trimmed
  revolve uses. A tolerance loose enough to cover a number nobody has is loose
  enough to cover a fault.

  **The COM half has never executed, and unusually the signature is not known
  either.** Every other call in that backend was read off a type library before
  it was written; here `FEATURE_COVERAGE.md` records only that
  `MoveFaceFeatures` has `Add` and `CreateDefinition`, and not what the
  definition's setter is called. So the backend tries three spellings, each of
  which can only mean direction-and-distance, and names every one it tried when
  none work -- rather than one guess that comes back as a bare "Exception
  occurred". A free-drag or point-to-point setter is deliberately not among
  them: one of those accepting a direction and a distance by accident is the
  quietly wrong part these notes exist to prevent. `PREDICTED["move_face"]` is
  at the placeholder 0.50 accordingly; the arithmetic would justify an
  extrude's 0.02 and nothing has run.

  `scripts/live_acceptance.py --only move-face` is that run, and it takes three
  readings per fixture rather than one, because defect 11 cost four runs to the
  belief that a feature which builds and measures right must be parametric: the
  magnitude against the derivation, the *sign* (a magnitude-only check passes a
  face that moved the right distance the wrong way), and the driving parameter
  doubled. `docs/INVENTOR_SETUP.md` has the ordered list, and says to read the
  real signature with `scripts/com_signatures.py --search MoveFace` before
  spending a seat on the check.

  One approximation, marked as one: the ledger is not updated, only the volume
  and the moved face's own position. So a cut driven through a moved face is
  charged the thickness the part had before the move, and
  `tests/test_move_face.py` pins that rather than leaving it to the comment, so
  whoever fixes it is sent to the `ponytail:` that says it is broken.

### Fixed
- **Three documentation facts that had gone stale, found while adding the
  above** rather than by a test, which is the point of noting them.
  `FEATURE_COVERAGE.md` said seventeen collections covered and listed
  seventeen names; the count and the list are now checked against each other
  (`test_the_coverage_count_matches_the_list_it_introduces`), because that
  sentence is two spellings of one fact and the rule from `DECISIONS.md`
  applies to it. `ARCHITECTURE.md` still located `PREDICTED` in `builder.py`,
  which the Phase 1 split moved to `rehearsal.py`, and still said coil, draft,
  emboss and split had never been compared against Inventor, which stopped
  being true on 2026-09-03. `examples/calibration/README.md` said the same
  about `split` three paragraphs above its own table showing it at 0.0%.

  Also: the calibration README's table regex read its operation column as
  `[a-z]+`, so `move_face` was the first row it would have silently declined to
  check. Both columns take an underscore now.

### Fixed
- **The DFM loop's baseline was measured on whatever state the caller left the
  document in.** Audited after defect 9, and this was the one real gap: each
  round already applied its changes, called `rebuild`, read that rebuild's
  health report to decide whether the values stood, and only then measured —
  but **round 0 had no rebuild before it**, and a baseline taken on an
  un-rebuilt model makes every improvement afterwards a comparison against the
  wrong part.

  Reachable rather than theoretical. Three routes deliver such a document:
  `promote_parameters` edits expressions and never rebuilds, `import_geometry`
  builds a part outside the `_batch` that supplies `document.Update()`, and
  `set_parameters` accepts `rebuild=False`.

  `measure` now rebuilds for itself, rather than each route being audited and
  trusted to stay disciplined — the reasoning `apply_parameter` records for the
  freeze guard. The round loop's explicit rebuild stays where it is, because its
  return value decides whether the round is undone and it has to happen before
  the export rather than as part of it. A round rebuilds twice, and the second
  is worth its cost beside writing an STL and running the analyser over it.

  What the audit found sound: both undo paths are already followed by a rebuild,
  which matters because the document is the deliverable; the loop holds no
  topology handles across a measurement; and `measure` analyses an exported STL
  rather than Inventor's mass properties, so there is no cached number to go
  stale — the staleness would have been in the file.

  All 53 mutating calls on the COM backend were checked. Every public one that
  changes geometry runs inside `_batch`. Two findings recorded in `DFM.md`
  rather than changed on a guess: **`promote_parameters` reports
  `identical_geometry` as a claim and not a measurement** (fixed above), and
  **`set_parameters(rebuild=False)` no longer skips all regeneration** — since
  defect 9 put `set_parameter` inside `_batch`, that flag now skips the explicit
  `Rebuild()` and its health report while an `Update()` happens regardless.
  A deliberate consequence, and it errs toward measurements being current.

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
