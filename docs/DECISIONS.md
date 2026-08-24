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
a thickness.

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
