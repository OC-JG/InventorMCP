# Standard parts

Parametric templates for the fasteners people ask for most. Each is driven by
the dimensions the standard tabulates, so a different size is a parameter change
rather than a new recipe.

> **Check the table before you ship a part.** The geometry below is verified; the
> numbers are a convenience and standards genuinely disagree. A DIN 934 M10 nut
> is 17 mm across the flats; the ISO 4032 M10 that supersedes it is 16 mm. Same
> for M12 (19 vs 18) and M14 (22 vs 21). If it matters which standard you are
> holding to, look the size up rather than trusting this page.

## The hexagon trap

A hexagon can be given by the distance across its **flats** (what a spanner
grips, and what every fastener standard tabulates as `s`) or across its
**corners**. They differ by a factor of cos 30° = 0.866, so getting it the wrong
way round makes an M16 nut that a 24 mm spanner will not fit.

Say which you mean with `fit`:

```json
{"type": "polygon", "sides": 6, "size": "across_flats", "fit": "circumscribed"}
```

- `"circumscribed"` — `size` is across the **flats**. Use this for fasteners.
- `"inscribed"` — `size` is across the **corners**.

Measured in the simulator: `size: 24, fit: "circumscribed"` gives 24.000 mm
across the flats and 27.713 mm across the corners, which is 24 / cos 30°. The
flats come out on the **Y** axis and the corners on X.

## Hex nut, DIN 934

`s` across flats, `m` height, thread `d`.

| Thread | s | m |
|---|---|---|
| M5 | 8 | 4.7 |
| M6 | 10 | 5.2 |
| M8 | 13 | 6.8 |
| M10 | 17 | 8.4 |
| M12 | 19 | 10.8 |
| M16 | 24 | 14.8 |
| M20 | 30 | 18.0 |

```json
{
  "name": "HexNut_M16", "units": "mm", "material": "Steel",
  "parameters": [
    {"name": "across_flats", "value": 24, "comment": "DIN 934 s for M16"},
    {"name": "height", "value": 14.8, "comment": "DIN 934 m"},
    {"name": "thread_d", "value": 16}
  ],
  "operations": [
    {"op": "sketch", "name": "Hex", "plane": "xy", "entities": [
      {"type": "polygon", "center": [0, 0], "sides": 6,
       "size": "across_flats", "fit": "circumscribed"}]},
    {"op": "extrude", "name": "Body", "sketch": "Hex", "distance": "height"},
    {"op": "sketch", "name": "Bore", "plane": "xy", "entities": [
      {"type": "point", "position": [0, 0]}]},
    {"op": "hole", "name": "Thread", "sketch": "Bore", "diameter": "thread_d",
     "through_all": true, "tap": "M16x2"}
  ]
}
```

Two things this recipe does not do, and you should say so rather than implying
otherwise:

- **The chamfer is missing.** A real DIN 934 nut has a *conical* chamfer on both
  faces. Inventor's `chamfer` feature would knock the same width off each of the
  twelve prism edges and leave twelve flat facets, which is the wrong shape. The
  right way is a `revolve` with `operation: "cut"` and a triangular profile —
  but `revolve` has never been run against a live Inventor here, so it is not in
  the template yet.
- **The tap is recorded, not cut.** The recipe carries `"tap": "M16x2"` and the
  COM backend currently drills a plain hole. The thread is in the model's intent
  and not in its geometry.

## Plain washer, DIN 125 A

`d1` bore, `d2` outside diameter, `s` thickness.

| Thread | d1 | d2 | s |
|---|---|---|---|
| M5 | 5.3 | 10 | 1.0 |
| M6 | 6.4 | 12 | 1.6 |
| M8 | 8.4 | 16 | 1.6 |
| M10 | 10.5 | 20 | 2.0 |
| M12 | 13.0 | 24 | 2.5 |
| M16 | 17.0 | 30 | 3.0 |
| M20 | 21.0 | 37 | 3.0 |

```json
{
  "name": "Washer_M8", "units": "mm", "material": "Steel",
  "parameters": [
    {"name": "bore", "value": 8.4, "comment": "DIN 125 d1 for M8"},
    {"name": "outside", "value": 16, "comment": "d2"},
    {"name": "thickness", "value": 1.6, "comment": "s"}
  ],
  "operations": [
    {"op": "sketch", "name": "Disc", "plane": "xy", "entities": [
      {"type": "circle", "center": [0, 0], "diameter": "outside"}]},
    {"op": "extrude", "name": "Body", "sketch": "Disc", "distance": "thickness"},
    {"op": "sketch", "name": "Centre", "plane": "xy", "entities": [
      {"type": "point", "position": [0, 0]}]},
    {"op": "hole", "name": "Bore", "sketch": "Centre", "diameter": "bore",
     "through_all": true}
  ]
}
```

## Hex standoff / spacer

Not a standard part, but the commonest thing to be asked for after the two
above, and the one where the hexagon trap actually bites.

```json
{
  "name": "HexStandoff", "units": "mm", "material": "Brass",
  "parameters": [
    {"name": "across_flats", "value": 8},
    {"name": "length", "value": 20},
    {"name": "thread_d", "value": 4.2, "comment": "tapping size for M5"},
    {"name": "chamfer_size", "value": 0.5}
  ],
  "operations": [
    {"op": "sketch", "name": "Hex", "plane": "xy", "entities": [
      {"type": "polygon", "center": [0, 0], "sides": 6,
       "size": "across_flats", "fit": "circumscribed"}]},
    {"op": "extrude", "name": "Body", "sketch": "Hex", "distance": "length"},
    {"op": "chamfer", "name": "Ends", "distance": "chamfer_size",
     "edges": {"filter": "horizontal"}},
    {"op": "sketch", "name": "Centre", "plane": "xy", "entities": [
      {"type": "point", "position": [0, 0]}]},
    {"op": "hole", "name": "Tapped", "sketch": "Centre", "diameter": "thread_d",
     "through_all": true, "tap": "M5x0.8"}
  ]
}
```

The chamfer takes `{"filter": "horizontal"}`, which is every edge lying in a
horizontal plane — both hex ends in one feature. Chamfering them in two calls
would work here and is still the wrong habit: edge indices renumber after each
feature, so a second call on a part that exposed them would be picking different
edges than the ones you looked at.

A note on the hexagon's constraints: a regular polygon here is built as a
construction circle with the vertices on it and `n - 1` equal-length edges.
Inventor refuses the last of those equalities whichever way they are chained, so
the sketch keeps one degree of freedom. The geometry is regular and the circle is
dimensioned, so the part is correct and revisable; the sketch icon just will not
show as fully constrained.
