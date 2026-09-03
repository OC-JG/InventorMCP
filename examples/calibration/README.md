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

Three of the four have been measured since. `split` has not: the run that tried
found the two backends disagreeing about which side of the plane a trim throws
away, which has to be settled before any number means anything.

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
| `stepped_split` | `split` | −15.5429 cm³ | −20.8000 cm³ | *see below* |
| `stepped_split_negative` | `split` | −11.6571 cm³ | not yet run | |

Measured on Inventor 2027.1, 2026-09-03. Three of the four tolerances in
`PREDICTED` were set from that run, each deliberately looser than the run alone
would justify, for reasons recorded beside the table in
`inventor_mcp/rehearsal.py`: `coil` 0.15, `draft` 0.20, `emboss` 0.40.

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

  `split` therefore keeps its placeholder tolerance. Its run came back 25.3%
  apart and that figure is worth nothing: the simulator kept the wrong amount
  and Inventor kept the wrong side, so it is two unrelated errors compounding.

  `stepped_split_negative` is the same part cut the same way with
  `remove_positive` false, and it exists to tell two explanations apart. If it
  removes 6.4 where its partner removed 20.8, the flag does control the side and
  its sense is simply inverted, which is one line in the COM backend. If it also
  removes 20.8, the flag is not reaching Inventor at all and the trim keeps
  whichever side it likes, which is a different bug with a different fix.

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
