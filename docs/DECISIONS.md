# Decisions

Why this server behaves the way it does, in the cases where a reasonable person
would expect the opposite. Each of these was decided once, for a reason, and the
reason is easy to forget and then quietly reverse.

Kept separate from `INVENTOR_SETUP.md`, which is about what Inventor does. This
is about what *we* chose.

## The recipe is the product, not the geometry

A model that builds the right shape and cannot be revised has failed, however
correct the picture looks. So the schema is declarative data, every size is an
expression, and a request to change the part is a parameter edit rather than new
geometry.

This is the reason for several things that would otherwise look like
over-engineering: `check_recipe` refuses a parameter that drives nothing, sketch
dimensions are reported with which parameters actually reached them, and
`live_acceptance.py` treats "widen `base_len` and watch the span follow" as the
headline check. A recipe whose numbers were evaluated when it was written and
then thrown away builds one correct part and is worthless afterwards.

## Measure, don't assume

Every non-sketch operation reports the volume it moved, and a cut that removed
nothing is an error rather than a success. This came from four separate bugs
that each survived several live runs: slots cut through empty air, holes drilled
past the part, a fillet on the wrong edge twice, and a hole style recorded and
dropped. All four reported success. The volume was the witness every time.

The same instinct runs through the COM backend: sketch axes are measured with
`SketchToModelSpace` rather than derived from the plane's name, a hole's drilling
side is decided from where the material is, edge convexity comes from the
boundary loops rather than a sampled point, and a hole's style is read back off
the finished feature. Where the answer genuinely cannot be measured, the code
says "unknown" and a filter matching unknown matches nothing — an honest refusal
beats a heuristic that is right most of the time, because the times it is wrong
look exactly like the times it is right.

## A failed build is left where it stopped

Rollback is opt-in, which surprises people. The half-built part is the best
evidence there is about what went wrong: three of the geometry bugs above were
found by looking at one, and tidying the document away would have cost each of
them another run.

`rollback_on_error` exists for the cases where the part matters more than the
diagnosis — appending to something that already works — and for the one failure
that cannot be recovered otherwise: a hole feature consumes its sketch, so
without a transaction there is nothing left to retry with.

The escape hatch inverts this default, on purpose. A recipe that half-runs
leaves evidence worth reading; an arbitrary script that half-runs leaves a part
nothing can be reasoned from, and there is no recipe to compare it against.

## The simulator is a first-class implementation, not a stub

It is what the test suite runs against, what lets a recipe be written on a
machine with no CAD seat, and — since it became accurate — the oracle a live
build is checked against. So a wrong answer in it is a bug of the same weight as
a wrong answer in the COM backend, and several were: a mirrored cut that removed
nothing, a through-cut charged the bounding box, a fillet subtracted whichever
way the edge turned, a counterbore counted as a whole cylinder, a loft adding an
area as if it were a volume.

The rule that catches these is: **an estimate may be imprecise, but it may not
be wrong in a way that changes the sign or the order of magnitude.** "Occurrence
volume is not estimated" sounded like a caveat and was a wrong answer — it made
a mirror that worked look identical to a cut that met no material, which is the
exact confusion the volume reporting exists to prevent.

Where the simulator genuinely cannot answer, it declines: a revolve records no
prism, so a through-cut through one falls back to the span rather than inventing
a thickness, and a shell whose cavity it could not compute records no cavity at
all rather than one of about the right size in about the right place.

The same rule decides where a volume is *kept*. It is kept per body and added up
only to report the part, because a total is exactly what hides the error: a cut
that removes more than the body it was aimed at holds used to be paid for out of
another body's material, and the sum stayed plausible. Each body now stops at
nothing on its own, and the difference between what an operation asked to remove
and what it actually removed is what the operation reports moving.

## A drawing is read, not traced

Importing a drawing's outlines would give exact geometry with no parameters,
which is the one thing this project exists not to produce. So the route is to
read what the drawing *says* into a `DrawingReading`, and let those numbers be
the recipe's parameters.

The reading is recorded separately from the recipe rather than going straight to
one, and that separation is the whole design: a drawing is redundantly specified
on purpose — three views each constraining the others — so one can be checked
against the other. A dimension that reaches no parameter was misread; a literal
the drawing never gives was invented.

`derived` is a separate category from `invented` for a reason. A 96 mm hole
spacing on a 120 mm plate with a 12 mm margin is exactly what a parametric model
should produce, and the first draft reported it as suspicious. Calling a correct
thing a fault teaches the reader to ignore the field.

## Refuse rather than guess

A fractional pattern count is an error, not something to round: four and a half
holes is a mistake in the recipe, and quietly building four or five produces a
part nobody asked for. An enum whose value another project's field notes dispute
refuses rather than guessing, because a wrong dimension-orientation enum makes
an aligned dimension where a horizontal one was meant and the part is wrong in a
way no error reports. A hole style Inventor will not confirm is refused rather
than reported.

The general form: prefer a loud failure to a plausible wrong answer, *especially*
where the wrong answer would still build something.

## Say what is not supported

Assemblies, drawings and sheet metal are not supported, and the modelling notes
tell a model to say so rather than approximating one as a part. Operations that
have never run against a live Inventor are listed as unproven in both the Skill
and the docs, with tests keeping the two lists in step.

The escape hatch is the honest answer to "but I need something you don't do" —
and it is absent unless the machine's owner turns it on, because a tool that is
not registered cannot be talked into being used, which a tool that is present
and refusing can.

## A DFM fix is closed by a measurement, not by having been made

The manufacturability loop changes a parameter, rebuilds, exports, and runs the
analyser again. Nothing else would do. A change that was applied is not a finding
that was answered, and the difference between the two is invisible in the moment:
the report says the same thing either way. So every round records which findings
actually went, which stayed, and which appeared — and a round whose change was
made while its finding stayed is called out by name rather than being spent three
more times.

The corollary is that a finding the analyser cannot measure is a finding the loop
will not touch. Corner radii are the case: the tool advises on them because an
STL carries no B-rep topology to measure them from, so a fillet change could
never be confirmed. A loop that cannot see the result of its own change is
guessing, and this one refuses to.

## Ratio fixes are written as expressions, not numbers

A rib that should be 45% of the wall becomes `wall_t * 0.45`, never `0.9 mm`.

The finding would close either way, today. Written as a number it closes until
someone edits the wall, at which point the check breaks again and nobody
connects the two events. Written as a ratio it is a property of the model, and
the next wall change carries the ribs with it.

Which makes this the one place an automated pass leaves a model *more*
parametric than it found it. That is worth protecting: it is the opposite of what
an optimiser usually does, and the tempting simplification — just set the number,
it is what the check reads — undoes it.

It also means the ordering matters. A rib ratio judged against the wall the part
has today can be wrong about the part the same pass is about to make: a Ø5.2 mm
boss is too wide for a 2 mm wall and comfortable on a 2.8 mm one. So every
decision downstream of the wall is taken against the wall that is coming.

## Key geometry is enforced where parameters change, and follows the expressions

Two things about the freeze that both look like over-engineering and are not.

**It lives in `apply_parameter`, not in the loop that motivated it.** A guarantee
implemented inside one loop ends the moment anything else edits a parameter — a
tool call, a script, a later feature — and the report would still say the key
geometry had been protected. So `set_parameters` refuses too, and getting through
requires saying `override_frozen=True`.

**Depending on a frozen value is the same as changing it.** Freeze `seal_face` at
`plate_t - gasket_crush` and nothing will touch `seal_face` — while `plate_t`
sits there, unlisted, moving the sealing face for anyone who edits it. So the
protection follows the expressions transitively, and the refusal names the chain
rather than just saying no. The reverse is deliberately not true: a parameter
that *reads* a frozen value is free to move, because reading is not changing.

Resolved against the model's live expressions rather than the recipe's. The
recipe is a snapshot from build time, and this loop rewrites literals into
expressions as it goes — a guard resolved against the snapshot would work out the
dependencies from a table that had since moved, and report a freeze it had not
enforced.

## Evidence is not a spelling, and a spelling is not evidence

"Declared, never guessed" was the rule, and a part handed over as a file
declares nothing. So role discovery had to exist, and the line it draws is the
whole point of it.

A shell feature takes its thickness from somewhere. Whatever that expression
reads *is* the wall — not because it resembles one, but because the shell is
built from it. That is a measurement of the model, no weaker than somebody
saying so, and it is reported with what it was read from because it is a claim a
person may want to check.

A parameter *called* `wall_t` is a different thing entirely. A table of likely
spellings gets most parts right, and the ones it gets wrong are
indistinguishable from the ones it gets right until a loop has already thinned
the wrong dimension. So a likely name is offered with the call that would accept
it, and nothing acts on it.

Two candidates map nothing. Two shells reading two different parameters is not a
wall; it is two walls and a question, and answering it by picking one would be
exactly the guess this avoids.

The same discipline decides what to do when a stronger source disagrees with the
evidence. A recipe mapping the wall to one parameter while the part's only shell
reads another: the recipe wins, and the disagreement is reported. Silently
preferring the recipe would be right and would also hide the more interesting
fact.

## The loop works on a copy, and the copy is named the way a person names one

Changing the file somebody handed over is wrong twice: their work is gone, and
there is nothing left to compare the result against. So `bracket.ipt` becomes
`bracket_v2.ipt`, keeping whatever separator, case and zero-padding the last one
used — because `bracket_v002` beside `bracket_v3` sorts wrong in every file
browser there is, and a version list that sorts wrong is one somebody reads in
the wrong order.

Nothing is ever overwritten. Two runs an hour apart would otherwise land on the
same name and the second would destroy the first, including a copy somebody had
already reviewed. That is a worse failure than refusing to run.

The copy is a filesystem copy rather than an open-and-save-elsewhere. A copy
cannot modify what it copies; opening the original and saving it under a new name
leaves a window in which it could.

## Whether a part can be driven is a count, not an inference

A translated file — STEP, IGES, SAT — carries geometry and not the history that
made it, so it arrives with a solid body and no parameters. Every DFM finding
still applies to it and none of them can be acted on, which is worth saying
plainly rather than discovering at the end of a loop that reports "nothing is
left that a parameter change answers" and reads like success.

But the test is not the extension. An `.ipt` somebody made by importing a STEP
file and never parameterised has exactly the same problem, and an assembly
imported from a multi-body STEP has a different one. So what gets reported is a
count of the part's user parameters and what actually arrived in the document —
measured, the way everything else here is.

## A duplicated threshold is contained by a test, not by care

The DFM tool states its thresholds as literals inside its rules and does not
export them, so the *targets* this project aims at — 45% of the wall, a tenth
above the material floor — are its own reading of bands stated elsewhere. That is
a duplication, and duplications drift.

The answer is not to be careful. It is that `tests/test_dfm_targets.py` puts
every target through the real engine and requires the check to come back clean,
with negative controls proving each margin is still needed. If a threshold moves,
that file fails and names the check. And in the meantime the loop re-measures, so
a target that has gone stale shows up as a proposal that did not work rather than
as a part quietly changed for nothing.

Where the numbers *can* travel, they do: material wall bands and required draft
angles come across from the tool's own table as numbers, because the alternative
was parsing them out of display strings like `"1.2–3.5 mm"` — which breaks
silently the day someone changes a dash.

## A fact stated twice needs a test that the two agree

The abstract base class is the model. `Backend` has an abstract method per
operation, so the live and simulated implementations cannot drift apart -- not
by discipline, by construction -- and across eighteen operations they never
have. Every duplication in this repository that had nothing enforcing it drifted
instead, and each cost a real failure:

* the recipe cheat-sheet against the schema: `coil` was implemented in both
  backends, verified against a live spring to 0.2%, tested, and mentioned in
  neither the cheat-sheet nor the Skill;
* the README's tool table against the registered tools: two tools missing, and
  "all five examples" beside a directory of eleven;
* `requires-python` against the CI matrix against what the code needs: 3.10
  declared, 3.11 the lowest leg, 3.12 what one f-string needed, and eight red
  runs on `main`;
* the DFM thresholds against the analyser's own, which is the case the section
  below this one is about;
* `PREDICTED` against the approximations it is supposed to watch: the four
  roughest -- each marked `ponytail:` by its own author -- had no entry at all,
  so the divergence check was silent in exactly the places somebody had flagged
  as least trustworthy;
* the DFM job's green tick against whether the analyser ran at all;
* the Skill's list of rehearsal warnings against the warnings a rehearsal
  emits: introduced as the ways a recipe passes every schema check and still
  builds the wrong part, which makes it a list claiming to be complete, and
  three of the six were missing -- two of them added to it by hand after they
  had already shipped unlisted;
* **the roadmap against the tree, which is the one that proves the rule is not
  yet a habit.** `ROADMAP.md` is where restructure 2 -- this rule -- was written
  down, and it was itself the last document not under it. It said thirteen of
  fourteen `ponytail:` markers lived in `backend/mock/` when there were sixteen,
  fifteen of them there; it quoted four calibrated tolerances that nothing held
  against `PREDICTED`; and one ticked item said "fixed the same day" without
  saying which day. Found on 2026-09-03 by going looking, not by anything
  failing, which is exactly the failure mode this list describes.

So the rule, and it is not "be careful": **a fact stated in two places does not
merge without a test that they still say the same thing.** The tests are cheap
-- most of them read one file and one Python object and compare -- and they fail
in the place the drift happened, naming it. `tests/test_docs_still_true.py`,
`tests/test_supported_pythons.py`, `tests/test_dfm_targets.py`,
`tests/test_roadmap_still_true.py`, the `dfm-unavailable:` sentinel in
`tests/conftest.py`, and the check that the snapshot policy and the defect it
works around still agree are the ones in place now.

What a drift test has to earn, learned from writing the roadmap's: it must be
able to fail, and it must fail *narrowly*. Each of the roadmap's claims was
mutated in turn -- fourteen markers for fifteen, nine examples for eleven, a
retuned tolerance, five restructures for four -- and each mutation was caught by
exactly one test. A drift test nothing can break is decoration, and one that
breaks in five places when a paragraph is re-flowed will be deleted by the next
person who re-flows a paragraph. Three of these tests were wrong when first
written, in that direction, and calling five sound entries undated is the same
false-positive habit the run warnings are written to avoid.

A corollary worth stating because it is the tempting way out: the answer to a
duplication is usually the test, not the removal. The cheat-sheet is duplicated
into a tool description *on purpose*, because a model that has to fetch a schema
before it can write anything will guess instead. What is not allowed is having
it in two places and nothing checking.

## Documentation that has drifted is worse than none

Because it is believed. So the Skill's factual claims are pinned by tests: that
unknown convexity matches nothing, that a recipe's X is model X on every plane,
that the operations it calls unproven are the ones the docs call unproven, and
that every JSON example in it builds with no warnings and no undriven
parameters. That last one caught a Skill example containing Python's `True`
where JSON needs `true` — few-shot material that would have been copied.

The same rule applies to CI: a workflow whose result nobody reads is worse than
no workflow, because it looks like coverage. Every run on this branch failed at
`pip install` for sixteen commits before anyone opened the logs.

## A derivation has preconditions, and they belong beside it

*Added 2026-09-07, from the work-axis acceptance run.*

The run measured a bolt circle's centre of mass moving 0.12263 mm where this
repository had derived 0.18640, and reported a failure. The part was correct.
The derivation -- six bores removing 1.17810 cm^3 centred on the circle, moved
15 mm, out of a remaining 94.82190 cm^3 -- is one line of arithmetic, and it
holds only while every hole is on the plate. At the position the check drove to,
one hole sat exactly on the plate edge and Inventor cut away half of it. Hand
deriving *that* case reproduced Inventor's figure to five decimal places.

So the rule, which is the volume ledger's rule one level up: **a number this
repository derives is only as good as the assumptions it does not state, and an
unstated assumption reads as a fault in Inventor.** Where a check compares
against a derived figure, the derivation's preconditions are computed and the
check refuses to compare when they do not hold -- `_bolt_circle_prediction`
returns a reason instead of a number. A prediction whose assumptions are not met
is not a looser prediction; it is a different question's answer.

The corollary is about reading results. Four runs of this check reported
"parametric in name only" and were pointing at the wrong thing three times: at
the labels, at the missing rebuild, and finally at a carrier point stuck on the
origin whose symmetry produced exactly the reading the message described. A
check's message says what it concluded, not what it measured. The numbers it
prints alongside are the evidence, and the one that broke the deadlock was
noticing that the volume moved while the centre of mass did not.

## A published signature outranks a guess and not a measurement

*Added 2026-09-08, from reading Autodesk's 2027 API reference against this
repository.*

Until then this repository knew two kinds of fact about Inventor's API: what a
live run had measured, and what a type library had been read to say. The
reference -- the User's Manual and Reference Manual, extracted page by page with
every unpublished signature marked as such -- is a third kind, and it had to be
placed. It is what Autodesk says a call takes, on pages Autodesk edits in place,
about a release that need not be the one installed. Its own verification pass
found three of its thirty-seven spot-checked claims wrong before it was read
here, and it lists members "read-only" that this repository has measured
building.

So the rule, applied to every change the reading produced: **a path that has
never run against an Inventor moves to the published call outright; a path that
has been measured keeps its measured route first and gains the published one
behind it.** `thicken` and the drawing retrieval had never run, and each was
guessing at a signature the reference gives -- one of them at a method
(`CreateThickenDefinition`) the reference says does not exist and another at a
property of `DrawingDimension` nothing documents -- so both were rewritten to
the documented call, and stay unmeasured. Edge convexity is measured through the
boundary loops, so `SurfaceBody.ConvexEdges` -- Inventor's own answer, and the
better one on paper -- was placed *behind* the loops, where it can decide only
what they decline and can disagree only in a log line. `split` uses a
`SplitPart` whose keep-side argument was inverted on evidence from three runs;
the reference names `TrimSolid(SplitTool, Body, [RemovePositiveSide])` as the
documented 2027 call with the side stated plainly, and the code was **not**
changed, because the risk of moving a measured path onto an unmeasured one is
precisely a quiet side inversion, and defect 5 is what that costs.

The corollary is about what a published number is worth in the enum table.
Forty-eight of the table's fifty-one measured values agree with the published
pages, which makes the pages a second source and the table better evidenced
than it was. The fourteen `HealthStatusEnum` values and the drawing-view names
added from the pages are marked as coming from them, `describe()` reports them
as "fallback" like any other unmeasured entry, and `dump_constants.py` will say
they are not in 2027.1's type library -- which is true, and is the reason they
had to come from somewhere else.


## A view direction means what Inventor means by it

*Measured 2026-09-08, decided 2026-09-09.*

Place one base view per direction of a 120 x 80 x 8 mm plate and read what each
spans: `front` and `rear` give 12 x 8 cm, `top` and `bottom` give 12 x 0.8,
`left` and `right` give 0.8 x 8. **Inventor's view names are Y-up** -- its front
view looks down Z and shows the XY plane. Every recipe here models Z-up: sketch
on XY, extrude upward. So the same word named two different views, and the
project's own tables said the elevation while Inventor drew the plan.

That mismatch had been recorded since `capture_view` was measured -- defect 4,
"the orientation names do not describe what you get" -- and left, because a
screenshot in the wrong orientation is a nuisance. A *drawing* in the wrong
orientation is a wrong drawing that looks like a right one, so the drawing
surface forced the question.

**It cannot be fixed by picking different enums.** Mapping `front` onto
`kTopViewOrientation` corrects four of the six; `left` and `right` are already
on the YZ plane and turned a quarter turn *inside* it, and no orientation enum
turns a view. The real alternatives were:

* place every view with `kArbitraryViewOrientation` and a camera built from a
  Z-up convention, so `front` means the elevation as the recipe's own
  vocabulary says; or
* let the recipe's words mean what Inventor means by them.

The second was chosen. A recipe asking for `front` now gets exactly what a
person placing a base view by hand on the same seat gets; `capture_view` and a
drawing sheet of one part agree with each other; and both directions of the
round trip measure the same axes, which is what makes the overall-size check
evidence rather than arithmetic. The cost is stated rather than discovered: a
plate modelled flat has its plan as its front view, which reads backwards, and
the schema's field description, the Skill and the guide all say so in those
words.

The camera route is not wrong -- it is more work resting on an unmeasured
mechanism, to buy a vocabulary that then disagrees with every sheet Inventor
draws unaided. Should it ever be built, it should be built for `capture_view`
in the same pass, because the two surfaces are the same quarter turn.

**One table, and one translation.** `VIEW_AXES` in `backend/base.py` is the
single copy of which axes a direction shows, above the three places that each
had their own -- writing a sheet, reading one back, and the simulator measuring
an extent. Three copies of one fact is how defect 5 survived three runs.

The reading side is the exception, and on purpose. A `DrawingReading`'s `kind`
is the view *as the sheet labels it*: the ISO drafting vocabulary a person reads
with, where FRONT is an elevation. Sharing one table would make a supplier's
FRONT view reconstruct as a plan, so `drafting._VIEW_KINDS` translates at the
one boundary where the vocabularies meet -- and transposes the extent for
`left` and `right`, whose planes agree and whose axis order does not. Two
tables here are two facts, not two copies of one.

**What the decision did not settle, the cameras did.** An extent is a size, so
it says which plane a view shows and never which way is up inside it. Read the
views' cameras and both fall out: every one has +Y up the screen except the top
and bottom pair, which look down and up the Y axis -- where Y cannot be up --
and put -Z and +Z there. So `capture_view`'s `top` rendering Z inverted, the
observation defect 4 recorded and could not explain, is Inventor being
consistently Y-up. The decision to follow that naming is the same decision
either way; what changed is that the last unexplained part of it has a
measurement.

It also settled how to *read* a view back. `DrawingView.ViewOrientationType`
answered nothing on any of the seven views on 2027.1, so a direction read off
the sheet comes from the camera, with the enum behind it and the detail saying
which answered. A camera says more than an orientation enum can: an enum names
a view and a camera says where it looks from and which way is up. That the
better evidence was also the only readable evidence is luck, and worth
recording as such.
