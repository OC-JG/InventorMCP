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
