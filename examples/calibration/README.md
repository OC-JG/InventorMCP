# Calibration recipes

Instruments, not parts. Each one exists to put a measured number on an estimate
the simulator has never had checked against Inventor.

`PREDICTED` in `inventor_mcp/rehearsal.py` says how far each operation's
simulated volume is trusted, and it is what decides whether a live build's
disagreement with its rehearsal is reported as a divergence. Four entries sit at
a placeholder 0.5 — `coil`, `draft`, `emboss` and `split` — because no shipped
example uses those operations, so no acceptance run has ever compared one with
Inventor. At 0.5 the check would wave through a fillet applied to the wrong
edge, which is the thing it exists to catch.

Run them with:

```
python scripts/live_acceptance.py --only calibration
```

It prints, per operation, what the simulator predicted and what Inventor did.
It passes or fails on nothing but whether the part builds: the first run is the
measurement, and what it prints is what the table should then say.

## What each one isolates

Every recipe puts the operation under test last, with nothing before it but
extrudes the simulator gets exactly right, so the whole difference belongs to
the operation being measured.

| Recipe | Operation | Simulator's estimate | What Inventor does |
|---|---|---|---|
| `coil_spring` | `coil` | profile area × helix arc length, 3.3377 cm³ | sweeps the real helix |
| `drafted_block` | `draft` | wedge of ½·area·height·tan θ, −4.7167 cm³ | tilts the faces |
| `engraved_plate` | `emboss` | glyph area from cap height and letter count, −0.0662 cm³ | cuts the real outlines |
| `stepped_split` | `split` | the share of the *bounding box* below the plane, −15.5429 cm³ | trims the solid |

Two of the four can be worked out by hand, so they are predictions rather than
curiosities, and the run either confirms the arithmetic or finds something:

- **`drafted_block`** is a frustum once drafted. Integrating its section from
  the parting plane gives 72 − 45k + 9k² cm³ for k = 2·tan 3°, which is 67.3821
  against the 72 it started at, so the true removal is **4.6179** where the
  simulator takes 4.7167. It should read about **2% high**, and its ponytail
  says why: the wedge assumes every drafted face spans the full pull height.
- **`stepped_split`** is deliberately not spread evenly either side of the cut,
  which is exactly what the trim estimate assumes. Below z = 12 there is 19.2 cm³
  of base and 1.6 of boss, so **20.8** is kept, where the bounding-box share of
  1.2/2.8 keeps **11.6571**. It should read about **44% low**.

`coil_spring` and `engraved_plate` are not tractable here. A helix's turns
interfere with each other in a way Pappus does not model, and a glyph's area is
whatever the font says it is. Those two are measurements, and the second should
be the worst of the four.

## Why they are not in `examples/`

That directory is the shipped set: parts a person might want, each named in the
README and each with a recorded expectation. These are apparatus. Keeping them
apart also keeps them out of the example count the documentation tests check,
and out of `scripts/live_acceptance.py`'s `examples` group, so a calibration run
is something asked for rather than something that happens.
