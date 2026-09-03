# Live expectations

One file per example, holding what Inventor measured. `scripts/live_acceptance.py`
compares against these, and `--record` rewrites them.

A number here is one of three things, and the difference matters more than the
value. **Derived** means somebody worked it out from the recipe before any live
run, so the run is a real check. **Confirmed** means a live run then agreed.
**Recorded** means the run wrote down what it found and nothing compared it to
anything, which makes it a regression baseline and not a verification.

`tests/test_expectations_are_documented.py` holds every number below to the
file it describes, because a table of numbers beside the numbers it describes
is a duplication, and this one had drifted: two of the derivations were for a
different part than the one they named.

## Derived first, then confirmed live

| Example | Volume | Derived from |
|---|---|---|
| `cover_plate` | 56.255104 cm³ | 60 cm³ plate − 4×(0.34212 bore + 0.40142 counterbore annulus) − (0.50265 bore + 0.26808 cone) |
| `pipe_bend` | 7.994380 cm³ | Pappus: π·(1.0² − 0.8²) cm² of annulus × (π/2)·4.5 cm travelled by the centroid |
| `threaded_boss` | 70.855424 cm³ | 38.4 plate + 2 × π·1.5²·2.5 bosses − 2 × flat-bottomed π·0.50528²·1.8 tap holes |
| `belt_pulley` | 68.006231 cm³ | 78.615215 blank − 5.519604 groove (clipped at the 40 mm rim) − 5 × 1.017876 lightening holes |
| `enclosure_base` | 46.896177 cm³ | the term-by-term derivation in the file itself |

The counterbore term is an *annulus*, not a cylinder: the bore through it is
already counted. Counting the whole cylinder is a mistake the simulator used to
make, and it made every counterbored plate lighter than it is.

Deriving these found more than it recorded. The pulley was drilling its own bore
twice, a revolved cut was being charged for the air it overshoots, and Pappus was
using the bounding box's centre instead of the centroid. A number worth writing
down is worth working out.

`enclosure_base` is the one that paid for the exercise. Its 46.896177 was derived
term by term, the simulator disagreed by 10.7%, and that is how the worst error in
the simulator's volume model was found rather than argued about: a cut after a
shell was charged against the solid the part had been before it was hollowed.
Inventor has since been asked and agrees with the hand figure to within the
0.0005 cm³ the acceptance run compares at.

## Measured live, then checked against a hand calculation

| Example | Volume | Checked against |
|---|---|---|
| `mounting_plate` | 75.018500 cm³ | the simulator's 75.018492, six significant figures |
| `angle_bracket` | 43.199900 cm³ | 46.2 body − 2×1.4617 slots − 2×0.3817 holes + 0.6867 fillet |
| `flanged_shaft` | 93.630500 cm³ | π·40²·12 flange + π·12.5²·90 shaft − π·5²·102 bore − 6 bolt holes − chamfer by Pappus |

The bracket is worth a note: the simulator used to report 27.15 cm³ for it and now
reports 43.2012, within 0.003% of Inventor. It got there by fixing three things it
had been getting wrong rather than by approximating better: a mirrored cut removed
nothing, a through-cut was charged the bounding box rather than the 6 mm base it
passes through, and a fillet on an inside corner was subtracted instead of added.
Each of those was a wrong answer wearing the clothes of an estimate.

## Recorded, never derived

Regression baselines. None is analytically tractable here, so none of them
verifies anything: each says what this recipe built on one release.

| Example | Volume | Why there is no hand figure |
|---|---|---|
| `duct_transition` | 32.649634 cm³ | a loft's sections do not interpolate linearly in area, so the volume is not the mean section times the span |
| `moulded_housing` | 46.523405 cm³ | every outer wall is drafted, so the part is a frustum with ribs and bosses on it |
| `hex_standoff` | 1.046600 cm³ | the chamfer across the hex prism's end meets three faces at each corner |

**Check the arithmetic before trusting a seeded number**, or it becomes a
regression test for whatever the code happened to build.

## How close the simulator is

All eleven examples against Inventor 2027.1, 2026-09-03. This is the number that
says how much a rehearsal is worth, so it is worth re-running and disagreeing
with rather than reading once.

| Example | Inventor | Simulator | Apart |
|---|---|---|---|
| `mounting_plate` | 75.0185 | 75.018492 | 0.00% |
| `belt_pulley` | 68.0062 | 68.006231 | 0.00% |
| `cover_plate` | 56.2551 | 56.255104 | 0.00% |
| `pipe_bend` | 7.9944 | 7.994380 | 0.00% |
| `threaded_boss` | 70.8552 | 70.855439 | 0.00% |
| `enclosure_base` | 46.8962 | 46.897289 | +0.002% |
| `angle_bracket` | 43.1999 | 43.201218 | +0.003% |
| `flanged_shaft` | 93.6305 | 93.626922 | −0.004% |
| `duct_transition` | 32.6496 | 32.477039 | −0.53% |
| `moulded_housing` | 46.5234 | 46.229078 | −0.63% |
| `hex_standoff` | 1.0466 | 1.037631 | −0.86% |

The three at the bottom are the three the simulator says outright it cannot do
exactly, and they are the three with no hand figure for the same reason. The
housing was 15.96% out before the volume ledger landed, for the reason
`enclosure_base` explains.
