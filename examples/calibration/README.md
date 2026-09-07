# Calibration recipes

Instruments, not parts. Each one exists to put a measured number on an estimate
the simulator has never had checked against Inventor.

`PREDICTED` in `inventor_mcp/rehearsal.py` says how far each operation's
simulated volume is trusted, and it is what decides whether a live build's
disagreement with its rehearsal is reported as a divergence. Four entries sat at
a placeholder 0.5 — `coil`, `draft`, `emboss` and `split` — because no shipped
example used those operations, so no acceptance run had ever compared one with
Inventor. At 0.5 the check would wave through a fillet applied to the wrong
edge, which is the thing it exists to catch.

All four have been measured since, on 2026-09-03. `split` was the last and the
hardest: the run that first tried found the two backends disagreeing about which
side of the plane a trim throws away, and that had to be settled before any
number could mean anything.

**`move_face` and `thicken` are the fifth and sixth, added 2026-09-07, and they
are at the placeholder for a different reason from the other four: not that no
example reaches them, but that their COM halves have never executed at all.** So
there is no Inventor column for their four fixtures yet.
`docs/INVENTOR_SETUP.md` says what a run has to confirm for each.

**`spread_pockets` is here for a third reason and is not calibrating a
tolerance at all.** `sketch_driven_pattern`'s COM half has never run either,
but its arithmetic is the rule the other two patterns use and is already
measured at 0.02, so it sits at 0.02 rather than the placeholder. What its
fixture asks is a *semantic* question -- whether Inventor also places an
occurrence on the reference point -- and the answer is an occurrence count
rather than a volume. It is kept here because this is where the instruments
live, not because a number needs fitting.

Run them with:

```
python scripts/live_acceptance.py --only calibration
```

It prints, per operation, what the simulator predicted and what Inventor did.
It passes or fails on nothing but whether the part builds: the run is the
measurement, and what it prints is what the table should then say. Re-run it
after any change to the simulator's estimates, and after any new Inventor
release — these are one release's numbers, not facts about the API.

## What each one isolates

Every recipe puts the operation under test last, with nothing before it but
extrudes the simulator gets exactly right, so the whole difference belongs to
the operation being measured.

| Recipe | Operation | Simulator | Inventor | Apart |
|---|---|---|---|---|
| `coil_spring` | `coil` | +3.3377 cm³ | +3.3310 cm³ | 0.2% |
| `drafted_block` | `draft` | −4.7167 cm³ | −4.6178 cm³ | 2.1% |
| `engraved_plate` | `emboss` | −0.0662 cm³ | −0.0803 cm³ | 17.5% |
| `stepped_split` | `split` | −6.4000 cm³ | −6.4000 cm³ | 0.0% |
| `stepped_split_negative` | `split` | −20.8000 cm³ | −20.8000 cm³ | 0.0% |
| `origin_plane_split` | `split` | −8.0000 cm³ | −8.0000 cm³ | 0.0% |
| `shelled_both_ways` | `shell` | −35.1920 cm³ | −35.1920 cm³ | 0.0% |
| `lifted_face` | `move_face` | +6.4000 cm³ | not yet run | — |
| `widened_wall` | `move_face` | +0.2400 cm³ | not yet run | — |
| `thickened_walls` | `thicken` | +1.4640 cm³ | +1.4640 cm³ | 0.0% |
| `thinned_wall` | `thicken` | −0.2400 cm³ | −0.2400 cm³ | 0.0% |
| `spread_pockets` | `sketch_driven_pattern` | −1.2000 cm³ | not yet run | — |

Measured on Inventor 2027.1 -- the first seven rows on 2026-09-03 and the two
`thicken` rows on 2026-09-07, when `thicken` became the fifth entry to come off
the placeholder. The `move_face` and `sketch_driven_pattern` rows are still
unrun: both of those calls failed on the 2026-09-07 pass before any number came
out of them, for reasons recorded in `docs/INVENTOR_SETUP.md`. All four of the original tolerances in `PREDICTED` now
come from that run rather than from a placeholder: `coil` 0.15, `draft`
0.20, `emboss` 0.40, `split` 0.05. Each is deliberately looser than its own
measurement, for reasons recorded beside the table in
`inventor_mcp/rehearsal.py` — a tolerance is for catching a feature that did
something else entirely, not for certifying arithmetic.

The three split rows read 0.0% because both backends were fixed and then
re-run. They disagreed on the side *and* the amount when this directory was
created, and that history is below, because it is the useful part.

## What the run confirmed, and what it found

Two of the four were worked out by hand first, so they were predictions the run
could break.

- **`drafted_block` was right to four decimal places.** The drafted solid is a
  frustum; integrating its section from the parting plane gives 72 − 45k + 9k²
  for k = 2·tan 3°, so the true removal is **4.6179 cm³**. Inventor removed
  **4.6178**. The simulator takes 4.7167, so it reads 2.1% high, exactly as the
  arithmetic said it would, and its ponytail says why: the wedge assumes every
  drafted face spans the full pull height.

- **`stepped_split` was right about the number and wrong about the side, and so
  was the simulator — differently.** Below z = 12 there is 19.2 cm³ of base and
  1.6 of boss, so a trim discarding the positive side should remove **6.4 cm³**
  and keep 20.8. Inventor removed **20.8**: it kept the positive side and threw
  away the negative one. The schema says `remove_positive` means "discard the
  side the plane's normal points at", and the simulator implements that, so the
  two backends disagree about which side goes. Recorded as defect 5 in
  `docs/FEATURE_COVERAGE.md`.

  `split` therefore kept its placeholder tolerance at that point. That run came
  back 25.3% apart and the figure was worth nothing: the simulator kept the
  wrong amount and Inventor kept the wrong side, so it was two unrelated errors
  compounding.

  `stepped_split_negative` is the same part cut the same way with
  `remove_positive` false, and it answered the first question on the same day:
  Inventor removed **6.4** where its partner removed 20.8, exactly
  complementary. So the flag reaches Inventor and controls the side, and it is
  not that a trim keeps whichever half it likes.

  What that leaves is two explanations calling for opposite fixes in different
  files. Either Inventor's second argument to `SplitPart` means *keep* the
  positive side where the code reads it as *remove* it — one inversion in the
  COM backend — or the offset work plane's normal points the other way, in
  which case both backends are right about the flag and disagree about the
  plane, and the fix belongs in the simulator instead.

  **`origin_plane_split` settled it.** An origin plane has no construction to
  get wrong: XY's normal is +Z by definition, so there was no offset plane left
  in the picture to have been built backwards. The part straddles z = 0 with
  19.2 cm³ below and 8.0 above, trimmed by XY itself with `remove_positive:
  true`, and Inventor removed the **19.2 below**. That exonerates the offset
  plane and puts the inversion at the call site: Inventor's second argument to
  `SplitPart` says which side to *keep*, where the code read it as which side to
  remove. Both halves of the fault are fixed — the flag is inverted where it
  reaches Inventor, and the simulator's share comes from the ledger instead of
  the bounding box — and the run after that agreed to four decimal places on
  all three fixtures, on the side and on the amount. `PREDICTED["split"]` is
  0.05 now, from those three, with headroom for the fillet and draft
  adjustments the ledger's share is applied to rather than for any error seen.

  A trimmed revolve, sweep or loft is excluded rather than covered: the ledger
  has no prisms to clip there, so the share falls back to the bounding box, the
  simulator says outright that the number is an estimate, and the rehearsal
  declines to compare it. A tolerance loose enough to cover that fallback would
  be loose enough to cover a fault.

  The worst part of that run was not the bug. On `origin_plane_split` the
  simulator said 19.4286 and Inventor said 19.2 — **1.2% apart, while keeping
  opposite halves of the part**. Any tolerance in the table would have passed
  it. Comparing volumes catches a cut that missed and a fillet on the wrong
  edge, and is blind to a cut that took the right amount off the wrong side
  whenever the two halves are near enough in size.

## The two nobody has run

`lifted_face` and `widened_wall` are the instruments for `move_face`, and they
are the only fixtures here whose *true* answer is exact rather than estimated. A
rectangular prism's face keeps its area as it translates, so the solid changes by
exactly area times distance: 32 cm² × 0.2 cm is 6.4 cm³ for the lifted cap, and
4 cm² × 0.6 cm × 0.1 cm — 40 × 6 × 1 mm — is 0.24 cm³ for the widened wall. The
simulator says 6.4000 and 0.2400, from a dot product of the move against each
face's own normal, which is the same arithmetic arrived at the same way.

So these two are predictions Inventor can break, in the sense `drafted_block`
was, and not measurements to be copied down. What the run is worth is not the
number:

- **`lifted_face` asks whether the parametric chain reaches the feature.** The
  distance is the parameter `lift`, and the lesson of defect 11 is that a work
  axis can be built, measured, and still be parametric in name only. Change
  `lift` and the volume has to change with it.
- **`widened_wall` asks two questions its partner cannot.** Its face is picked
  out of four by a selector rather than by being the only cap, so it fails if
  the COM selector reaches a different face than the simulator's; and it moves
  along an axis that is not the extrude's own, so it fails if Inventor reads the
  direction relative to the face rather than to the model. Its 0.24 cm³ is
  deliberately small beside the 19.2 cm³ plate it sits on: a move that took the
  whole wall with it is then a large fraction rather than a rounding error.

Neither is tractable as an estimate to loosen. If a run disagrees on either, the
answer is a fault to find rather than a tolerance to widen — which is why
`PREDICTED["move_face"]` should come down from 0.50 to something like an
extrude's 0.02 the moment a run agrees, rather than being split down the middle.

What both leave untouched is the assumption underneath the arithmetic: that the
moved face keeps its area. It does on a prism, which is what these measure. It
does not on a face bounded by a fillet or a draft, and the simulator's
`ponytail` on `move_face` says so. Nothing here calibrates that case, and a
fixture for it cannot go in this directory as it stands, because the rule these
recipes are checked against is that nothing before the operation under test may
be looser than an extrude — and a fillet is 0.30.

## The two that asked a question rather than measured an estimate

*Both answered on 2026-09-07, Inventor 2027.1, and both fixtures now agree with
Inventor to four decimals.*

**The side is confirmed.** `thinned_wall` removed **−0.2400 cm³** against
−0.2400 derived, so a `negative` layer does lie behind the face and
`THICKEN_SHARE` has it right. The three-way reading was what made that a
measurement rather than a number coming out close: 0.0000 would have meant the
layer landed outside the solid, and any positive figure something else again.

**And the corners close.** `thickened_walls` came back **+1.4640** where the sum
of the four layers is 1.4400. Inventor fills the 1 × 1 × 6 mm notch where two
layers meet, so the simulator's figure was 1.7% low on any multi-face thicken —
and it is not low any more. Two faces whose normals are perpendicular share an
edge running along the cross product of the two, and the notch is the layer's
own thickness squared swept along that edge: `t² × h` per shared edge, four of
them, 0.024 cm³. `_thicken_corners` derives it and the two figures now agree.

The sign is the same either way, which looks wrong and is not. Growing four
walls leaves gaps to fill; thinning four walls makes the layers *overlap* at the
corners, so the union is the sum minus the overlap — and since the change is
negative, subtracting less means adding. Both come out as `+corners`.

`PREDICTED["thicken"]` is 0.02 now, the same as an extrude and a shell, because
what is left is exact prism arithmetic.

The original reasoning follows, because the way these two fixtures were shaped
is why the answers were distinguishable at all.

`thickened_walls` and `thinned_wall` are the instruments for `thicken`, and
neither is really calibrating arithmetic. The arithmetic is exact per face — a
planar face's area times the layer — and on a *single* planar face `thicken` and
`move_face` come out identical, which is worth knowing before reading either
fixture: thickening the top face 2 mm and moving it 2 mm produce the same solid
and the same 6.4 cm³. Two independently written operations agreeing is a
cross-check and not a second measurement.

What is genuinely unmeasured is elsewhere, and each fixture isolates one of it.

- **`thickened_walls` asked about the corners.** Four walls grown 1 mm outward
  is the case a single named direction cannot express, and it is where the
  layers stop being independent. They do not meet: the +X wall's layer covers
  x 40→41 over y −20→20, the +Y wall's covers y 20→21 over x −40→40, and the
  1 × 1 × 6 mm notch at each corner belongs to neither. So the answer was
  **1.4400 cm³** if Inventor left those notches and **1.4640** if it closed them
  — 4 × 6 mm³ apart, or 1.7%. The simulator said 1.4400 because summing face
  areas was what it could defend, not because anybody knew. **Inventor closes
  them: 1.4640.** Both candidates were written down before the run, which is the
  only reason the answer took one run instead of three.

- **`thinned_wall` asks about the side**, which no magnitude reveals.
  `THICKEN_SHARE` in `backend/base.py` says a `negative` layer lies behind the
  face, in the material, so cutting it removes 0.24 cm³ and leaves the plate 79
  mm wide. That is sound set algebra about a boolean against a slab, and it is
  silent on whether Inventor means the same side. The three outcomes are
  distinguishable: **−0.2400** confirms the table, **0.0000** says the layer
  landed outside the solid and the side is inverted, and any positive figure
  says something else again.

  This is defect 5's lesson applied before it can be repeated. A `trim` kept the
  wrong half of a part for as long as the feature existed, and one of the runs
  that found it was 1.2% apart — inside every tolerance in the table — because
  the volume was correct for the half it kept. A tolerance cannot catch being
  wrong about a side. A fixture whose three outcomes are different numbers can.

Neither fixture's number should be copied into `PREDICTED` on agreement alone:
`thicken` should go to an extrude's 0.02 once the side is confirmed and the
corner question answered, since what is left after that is exact.

## The one that counts features rather than measuring them

`spread_pockets` is the instrument for `sketch_driven_pattern`, and its volume
is the least interesting thing about it. A plate with one 0.4 cm³ pocket, that
pocket copied to three more points: −1.2000 cm³, by the same rule the pulley's
circular pattern and the threaded boss's rectangular one already confirm at
0.02. Nothing there needs measuring.

**What needs measuring is whether Inventor puts an occurrence on the reference
point as well.** The recipe assumes not: the seed sits at (−35, −20), that point
is named as the reference, and the other three get one occurrence each, so four
points describe four pockets. Three readings, and two of them are the same
volume:

| what comes back | what it means |
|---|---|
| −1.2000 cm³, four pockets | the recipe's assumption holds |
| −1.2000 cm³, **five** features | the reference is patterned onto itself, and the duplicate removes nothing extra because it lands exactly on the seed |
| −1.6000 cm³ | five occurrences with the fifth somewhere unaccounted for |

That middle row is why `--only sketch-driven-pattern` counts the features on the
finished part instead of only measuring it. A duplicate feature sitting exactly
on its seed is invisible to a volume and would ship as a part with a redundant
feature in its browser — harmless on this plate, and not harmless on a pattern
somebody later edits.

## The path that could not run at all

`shelled_both_ways` is not calibrating an estimate so much as proving a feature
exists. A shell with `direction: "both"` splits the wall either side of the
original surface, and until 2026-09-03 it could not be built: the enum it needs
is `kBothSidesShellDirection`, the constants table asked for
`kBothShellDirection`, and Inventor has never had a name of that spelling, so
the server refused rather than guessing. The refusal was right and it hid the
fact that nothing had ever exercised the path.

The run pinned down what `both` means, and the answer is exact. The wall
straddles the original face, half in and half out, so a 60×40×20 box with a 2 mm
wall leaves an outer solid grown 1 mm on the four sides and the base and a
cavity inset 1 mm: 6.2 × 4.2 × 2.1 less 5.8 × 3.8 × 1.9 is 12.808 cm³, and the
shell removes **35.192**. Inventor removed 35.1920.

The simulator was 13.6% low when this fixture was written, because its exact
branch was the inside case and `both` fell to the surface-area estimate. It is
exact now, for all three directions: the only difference between them is where
the wall sits relative to the face, and offsetting an outline outward was the
one thing missing. `outside` puts the whole wall beyond the face, so the
original solid becomes the void; `both` straddles it.

Worth noting what that 13.6% did while it lasted. It sat inside the 0.35 a shell
was allowed, so nothing was ever reported — the right outcome for an
approximation that admits it is one, and also the reason a case nobody could run
stayed wrong without anybody noticing.

That tolerance is 0.02 now, the same as an extrude's, because what made it wide
was never the arithmetic but the fallback it had to cover. A body that is not a
single prism, and a shell opened through a side rather than an end, both declare
themselves estimates and are left out of the comparison instead.

The other unreachable path was `capture_view` in hidden-line mode, which asked
for `kHiddenLineRendering`, another name no release has. That one did not
refuse: it caught the failure and rendered in whatever mode the view was
already in, so the picture was in the wrong style and reported as a success.
There is no recipe for it because it produces an image rather than a volume;
`live_acceptance.py --only views` is the check, and it asserts that each mode
was actually applied and that the three modes do not render identical files.
Run on 2026-09-03: all three applied, all three wrote different files. The five
orientations wrote five different files too, so the camera does move — but that
says only that the names do *something*, not that they do what they say, and
defect 4 stands until somebody looks at the pictures.

`coil_spring` and `engraved_plate` are not tractable here: a helix's turns
interfere in a way Pappus does not model, and a glyph's area is whatever the
font says it is. Those two are measurements rather than predictions, and the
second is the worst of the four as expected.

## Why they are not in `examples/`

That directory is the shipped set: parts a person might want, each named in the
README and each with a recorded expectation. These are apparatus. Keeping them
apart also keeps them out of the example count the documentation tests check,
and out of `scripts/live_acceptance.py`'s `examples` group, so a calibration run
is something asked for rather than something that happens.
