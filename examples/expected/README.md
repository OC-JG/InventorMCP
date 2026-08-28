# Live expectations

One file per example, holding what Inventor measured. `scripts/live_acceptance.py`
compares against these and `--record` rewrites them.

Three were taken from live runs on Inventor 2027.1 and checked against a hand
calculation before being written down:

| Example | Volume | Checked against |
|---|---|---|
| `mounting_plate` | 75.0185 cm³ | the simulator's 75.018498, six significant figures |
| `angle_bracket` | 43.1999 cm³ | 46.2 body − 2×1.4617 slots − 2×0.3817 holes + 0.6867 fillet |

The bracket is worth a note: the simulator used to report 27.15 cm³ for it, and
now reports 43.2012 — within 0.003% of Inventor. It got there by fixing three
things it had been getting wrong rather than approximating: a mirrored cut
removed nothing, a through-cut was charged the bounding box rather than the
6 mm base it passes through, and a fillet on an inside corner was subtracted
instead of added. Each of those was a wrong answer wearing the clothes of an
estimate.
| `flanged_shaft` | 93.6305 cm³ | π·40²·12 flange + π·12.5²·90 shaft − π·5²·102 bore − 6 bolt holes − chamfer by Pappus |

Four more are the other way round: their volumes were **derived by hand first**
and written down before any live run, so the first run is a real check rather
than a recording of whatever the code happened to build.

| Example | Volume | Derived from |
|---|---|---|
| `cover_plate` | 56.255104 cm³ | 60 cm³ plate − 4×(0.34212 bore + 0.40142 counterbore annulus) − (0.50265 bore + 0.26808 cone) |
| `pipe_bend` | 22.206610 cm³ | Pappus: π·1.0² cm² × (π/2)·4.5 cm travelled by the centroid |
| `threaded_boss` | 33.872087 cm³ | 2 × π·1.5²·2.5 bosses − one flat-bottomed π·0.51²·1.8 tap hole |
| `belt_pulley` | 68.006231 cm³ | 78.615215 blank − 5.519604 groove (clipped at the 40 mm rim) − 5 × 1.017876 lightening holes |

Deriving those three found more than it recorded: the pulley was drilling its
own bore twice, a revolved cut was being charged for the air it overshoots, and
Pappus was using the bounding box's centre instead of the centroid. A number
worth writing down is worth working out.

The counterbore term is an *annulus*, not a cylinder: the bore through it is
already counted. Counting the whole cylinder is a mistake the simulator used to
make, and it made every counterbored plate lighter than it is.

`hex_standoff`, `enclosure_base` and `duct_transition` have never had their
volumes captured live, and none is analytically tractable here -- a loft's
cross-section does not interpolate linearly in area, so its volume is not the
mean section times the span. The first run seeds them; **check the arithmetic before trusting the number**, or
it becomes a regression test for whatever it happened to build.
