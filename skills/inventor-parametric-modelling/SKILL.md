---
name: inventor-parametric-modelling
description: Build parametric Autodesk Inventor parts through the InventorMCP server. Use whenever the user wants to create, revise, measure or export an Inventor part (.ipt) — brackets, plates, shafts, flanges, standoffs, enclosures; adding holes, fillets, chamfers, slots, patterns, shells; or asks to change a dimension and rebuild. Also use when diagnosing "the cut removed nothing", "the fillet is on the wrong edge", "the hole did not drill", "the selector matched no edges", or a sketch that will not close. Triggers on part recipes, driving dimensions, `mcp__inventor__*` tools, and any request phrased as "make it 20 mm wider".
---

# Parametric parts in Inventor, through InventorMCP

## What this server is for

It turns a description of a part into a **parametric** Inventor model: every size
is an expression of a named parameter, so revising the part is a parameter edit
rather than a rebuild. A model whose numbers are hard-coded builds a shape and
then cannot be changed — that is the failure this exists to prevent.

So: name the driving dimensions first, then write every size as an expression of
them. `"width": "plate_w"`, not `"width": 120`.

## The loop

1. `connect` — a live Inventor if there is one, otherwise the simulator.
2. **`validate_recipe` — always, before building.** It does not merely check the
   schema: it builds the whole recipe in the simulator and reports what each
   operation would do. Free, instant, needs no Inventor.
3. `build_part_from_recipe` — parameters first, then sketches and features.
4. Read what came back (see below). Do not assume it worked.
5. `set_parameters` — change a driving dimension; the model updates.
6. `export_model` / `capture_view`.

### What to read in the rehearsal

`warnings` is the important field. A recipe can pass every schema check and
still build the wrong part, and these are the ways it does:

- **`sketch 'X' does not reach the part`** — the profile lies outside the part
  entirely, so the cut will meet nothing. Fix the plane or the coordinates.
- **`removed no material`** — same conclusion, reached from the volume.
- **`a, b drive nothing`** — those parameters are declared and never referenced,
  so the part is not revisable through them. Write the sizes that depend on them
  as expressions.

Then read `steps` and check each `volume_change_cm3` against what you meant. A
9 mm hole 6 mm deep removes π×4.5²×6 = 0.382 cm³. If the rehearsal says
something else, work out why before spending a CAD seat on it.

Two things the rehearsal cannot tell you, because the simulator has no
booleans and no notion of which side the material is on:

- whether a cut that *overlaps* the part removes the right amount;
- which way a fillet or chamfer will move the volume. It models every fillet as
  subtractive, so an inside-corner fillet looks wrong here and is not.

## Read the report on every operation

Every non-sketch operation returns a `measured` block:

```json
{"volume_cm3": 43.2766, "faces": 14, "edges": 28,
 "volume_change_cm3": -1.4617}
```

**This is the single most useful thing on the response.** Inventor reports success
for operations that did nothing at all, and this is how you tell:

- A cut, hole or shell with `"note": "the volume did not change"` **did not work**.
  Its profile missed the material. Do not carry on as though it built.
- A cut whose volume went *up*, or a fillet whose volume went *down*, is on the
  wrong edge or the wrong side. An inside-corner fillet adds material; an
  outside-corner fillet removes it.
- `faces` and `edges` moving tells you the topology changed even when the volume
  barely did.

Check the number against what you expect. A 9 mm hole 6 mm deep removes
π×4.5²×6 = 382 mm³ = 0.382 cm³. If the report says something else, something
else happened.

## From a sentence to a recipe

The hard part is not the JSON, it is deciding **which numbers are the driving
dimensions**. Name the ones a person would put on a drawing; write everything
else as an expression of those. That decision is what makes the part revisable.

### "A 120 × 80 × 8 mounting plate, four M6 clearance holes 12 mm in from the edges, corners rounded 10"

Driving dimensions: the two overall sizes, the thickness, the hole size, the
edge margin, the corner radius. Six numbers, all of which a person would
dimension. The hole *spacing* is not one of them — it follows from the plate
size and the margin, so it is an expression.

```json
{
  "name": "MountingPlate", "units": "mm", "material": "Aluminum",
  "parameters": [
    {"name": "plate_w", "value": 120, "comment": "overall width"},
    {"name": "plate_d", "value": 80, "comment": "overall depth"},
    {"name": "thk", "value": 8},
    {"name": "hole_d", "value": 6.6, "comment": "M6 clearance"},
    {"name": "edge_margin", "value": 12, "comment": "hole centre to edge"},
    {"name": "corner_r", "value": 10}
  ],
  "operations": [
    {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
      {"type": "rectangle", "center": [0, 0], "width": "plate_w", "height": "plate_d"}]},
    {"op": "extrude", "name": "Plate", "sketch": "Outline", "distance": "thk"},
    {"op": "fillet", "name": "Corners", "edges": {"filter": "vertical"}, "radius": "corner_r"},
    {"op": "sketch", "name": "Holes", "plane": "xy", "entities": [
      {"type": "point_grid", "center": [0, 0], "columns": 2, "rows": 2,
       "x_spacing": "plate_w - 2 * edge_margin",
       "y_spacing": "plate_d - 2 * edge_margin"}]},
    {"op": "hole", "name": "Fixings", "sketch": "Holes", "diameter": "hole_d",
     "through_all": true}
  ]
}
```

Note `"x_spacing": "plate_w - 2 * edge_margin"`. Written as `96` it would build
the same plate and then be wrong the moment the plate got wider. And
`{"filter": "vertical"}` for the corners rather than four edge indices, because
a box's four upright edges are exactly what "vertical" means.

### "An L-bracket: base 90 long, upright 70 tall, 6 thick, 50 wide. Two 20 × 9 slots in the base, 30 apart. Round the inside corner 8"

An L-section is a **polyline** profile, extruded. Its six vertices are all
expressions of `base_len`, `upright_h` and `thk` — that is what makes the
outline revisable, and writing them as numbers is the commonest way to produce
a part that looks right and cannot be changed.

```json
{
  "name": "AngleBracket", "units": "mm", "material": "Steel",
  "parameters": [
    {"name": "base_len", "value": 90}, {"name": "upright_h", "value": 70},
    {"name": "width", "value": 50}, {"name": "thk", "value": 6},
    {"name": "slot_len", "value": 20}, {"name": "slot_w", "value": 9},
    {"name": "slot_pitch", "value": 30}, {"name": "fillet_r", "value": 8}
  ],
  "operations": [
    {"op": "sketch", "name": "Section", "plane": "xz", "entities": [
      {"type": "polyline", "closed": true, "points": [
        [0, 0], ["base_len", 0], ["base_len", "thk"],
        ["thk", "thk"], ["thk", "upright_h"], [0, "upright_h"]]}]},
    {"op": "extrude", "name": "Body", "sketch": "Section", "distance": "width",
     "direction": "symmetric"},
    {"op": "sketch", "name": "Slots", "plane": "xy", "entities": [
      {"type": "slot", "center": ["base_len - 25", "slot_pitch / 2"],
       "length": "slot_len", "width": "slot_w", "angle": 0}]},
    {"op": "extrude", "name": "SlotCut", "sketch": "Slots", "extent": "through_all",
     "operation": "cut", "direction": "positive"},
    {"op": "mirror", "name": "SlotPair", "features": ["SlotCut"], "plane": "xz"},
    {"op": "fillet", "name": "InsideCorner", "radius": "fillet_r",
     "edges": {"kind": "edge", "filter": "concave", "min_length": 40, "limit": 1}}
  ]
}
```

Three choices worth copying:

- **The section is on `xz`** so the extrusion runs along Y and gives the width.
  Draw the profile as you would on paper; the server handles the plane's real
  orientation.
- **One slot, then a mirror.** Two hand-placed slots would need their positions
  keeping in step by hand; a mirror keeps them symmetric by construction.
- **The inside corner is selected by `concave`**, not by a point. `min_length: 40`
  excludes the 20 mm slot edges, and `limit: 1` takes the one that remains.

### Revising it

```
set_parameters([{"name": "base_len", "value": 120}])
```

The base lengthens, the slots move with it, the upright and the fillet stay put.
Nothing is rebuilt. If any of that does not happen, some number in the recipe
was written as a literal.

## From a drawing to a model

A drawing is a **specification**, not a picture. Tracing its outlines gives
geometry with no parameters, which is the one thing this server exists not to
produce. Reading its dimensions gives you the driving values, and those become
the parameters.

So do it in two steps, and keep them apart:

1. **Read the drawing** into a reading — its views, its dimensions, its
   projection angle, its notes. `drawing_reading_schema` gives the shape.
2. **Write a recipe whose parameters are those dimensions**, then call
   `check_against_drawing` to check one against the other.

The reading is worth writing down separately rather than going straight to a
recipe, because a drawing is *redundantly* specified on purpose — three views of
one part, each constraining the others — and that redundancy is what lets the
model be checked:

- **`missing`** — a dimension on the drawing that no parameter or driving
  dimension in the model has. It was misread or left out. This is the commonest
  drawing-reading mistake and it is invisible without the check, because the
  model is perfectly self-consistent and simply not the part on the sheet.
- **`invented`** — a bare literal in the model that the drawing never gives.
  Either read it off the drawing or write it as an expression of values that
  are.
- **`derived`** — computed from drawing dimensions. This is what a parametric
  model *should* do: a 96 mm hole spacing on a 120 mm plate with a 12 mm margin
  is right, and it is reported separately so it does not read as a fault.
- The **overall size** is checked against the views. A part the right shape and
  the wrong size passes every other check.

Two things to be careful about when reading:

- **The projection angle.** First angle (ISO, European) and third angle (ASME,
  US) put the right-hand view on opposite sides of the front view, so reading
  one as the other mirrors the part. Look for the projection symbol near the
  title block, and say `unknown` rather than guessing — the check warns when you
  do, which is better than a silently mirrored model.
- **What you cannot make out.** Put it in `unreadable`. A missed dimension is
  recoverable; an invented one is not, because nothing downstream can tell it
  from a real one.

Reference dimensions — bracketed, or marked REF — restate something already
fixed, so mark them `reference: true` and they are not expected to appear as
their own parameter.

Two conveniences worth knowing, so a correct model is not reported as wrong:

- **Angles are checked against angle parameters**, in degrees, not thrown in
  with the lengths. Give a countersink's included angle as `"kind": "angle"`.
- **A symmetric pitch matches the half the model drives.** A drawing gives a
  bolt pitch of 76 mm; a centred grid is driven by 38 mm from the centre line.
  The check finds it and says so.

`examples/drawings/cover_plate.json` is a full reading, and
`examples/cover_plate.json` is the recipe that satisfies it — counterbored bolt
holes, a countersunk centre hole, and every dimension on the sheet reaching a
parameter. Read the pair before writing your first one.

## Select by intent, never by index

Edge and face indices renumber after every feature, so an index captured before
a fillet is meaningless after it. This server never exposes them. Say what you
mean:

```json
{"filter": "concave", "min_length": 40, "limit": 1}   // the inside corner
{"filter": "vertical"}                                // the upright edges
{"filter": "top"}                                     // the face pointing +Z
{"filter": "circular", "near": [0, 0, 102], "limit": 1}
```

- `concave` / `convex` is how you say "the inside corner" without knowing which
  way the sketch plane faces. It is exact where Inventor exposes the boundary
  loops, and **unknown** where it does not — and unknown matches nothing, so a
  selector that returns "matched no edges" is telling you it could not decide,
  not that the edge is absent. Narrow with `min_length` or `near`.
- `validate_recipe` can check these offline now, for the edges that run along an
  extrusion: the profile's corner decides whether that edge is an inside or an
  outside one, and the simulator works it out the same way. Edges it cannot
  place stay unknown there too, so a rehearsal that says "matched no edges" is
  worth reading rather than dismissing as a limit of the simulator.
- Convexity is never known for a **circular** edge. Use `near` to pick between a
  shaft's free end and its shoulder — they are both circular and only one is the
  one you mean.
- `limit` without `near` picks arbitrarily. If two edges match and you want one
  specific one, say where it is.

## Coordinates mean what they say

A recipe's `x` is model X on every plane, including `xz` and `yz`. The server
measures each sketch plane's real orientation and compensates, so you do not need
to know that Inventor's XZ plane runs its first axis along −X. Every sketch
result reports what it measured:

```
sketch axes: u->-X v->+Z, u reversed
```

If that line says something unexpected, believe it over your mental model.

## What makes a sketch parametric

The sketch result tells you whether it worked:

```
entities=6 constraints=13 dimensions=4 driving=4 refused=0
driven by: base_len, thk, upright_h
```

- `driven by` lists the parameters that actually reach a dimension. **If a sketch
  has a profile and this is empty, the outline is not parametric** — it was built
  from numbers and will not move when the parameters change. The server warns
  about exactly this.
- `refused` counts dimensions Inventor rejected as redundant. The sketch survives
  with a degree of freedom left; it is worth knowing but not usually worth
  fixing.
- Write coordinates as expressions and this takes care of itself:
  `[["base_len", 0], ["base_len", "thk"]]`, not `[[90, 0], [90, 6]]`.

## Failure modes, and what they actually mean

| What you see | What it means |
|---|---|
| `the volume did not change` on a cut | The profile missed the part. Check the plane and the coordinates against `measure_part`'s bounding box. |
| `The hole built but removed no material` | The centres are not over material. The server already tried both sides. |
| `The selector matched no edges` | Often convexity could not be decided, not that nothing is there. Add `min_length` or `near`. |
| `has closed loops in the recipe but no profile` | The geometry is not joined — coordinates that do not quite meet. |
| A fillet that removes material | It is on a convex edge. An inside corner adds. |
| `refused N constraint(s)` | Inventor had already inferred them. Benign. |

## Revising a part

This is the point of the whole thing, so prefer it over rebuilding:

```
set_parameters([{"name": "base_len", "value": 120}])
```

Then check `measure_part`: the span that should have grown should have grown. If
nothing moved, the geometry was never driven by that parameter — go back and look
at the sketch's `driven by` list.

## Standard parts

`references/standard-parts.md` holds parametric templates for hex nuts, washers
and standoffs, with the dimension tables and the hexagon trap spelt out — a
hexagon given across its corners rather than its flats is 15% oversize, which is
an M16 nut a 24 mm spanner will not fit. Read that file when asked for a
fastener rather than deriving one.

## Things worth knowing before you start

- Lengths are in the document's units (mm by default), angles in degrees. The
  server converts to Inventor's internal centimetres and radians.
- A closed profile is required for a solid feature. A sketch of hole centres has
  no profile by design and that is not an error.
- Regular polygons keep one degree of freedom: Inventor refuses the closing
  equal-length constraint. The geometry is right and dimensioned; the sketch is
  just not fully constrained.
- **Hole styles are built, and checked.** `counterbore`, `spotface`,
  `countersink` and `tap` reach Inventor's own hole feature. Because a wrong
  argument order can still build — producing a plain hole that would be reported
  as a counterbore — the server reads the style back off the finished feature and
  fails rather than reporting a counterbore it cannot see. A *plain* hole is
  never refused on that ground — it claims nothing beyond removing material,
  which is checked directly — so the holes that already worked cannot break to
  guard a claim nobody made. A tapped hole takes
  its drill size from Inventor's thread table, so give `diameter` as the
  *tapping drill* (nominal less the pitch for coarse metric); the server reports
  the size Inventor actually used and says so when the two disagree. None of the
  styles has yet been built against a live Inventor — the read-back is what makes
  that survivable rather than silent, so treat a first success with the same
  suspicion as the operations below.
- **Read `divergence` if the build reports it.** Every build is rehearsed in the
  simulator first, and an operation whose live volume change disagrees with the
  prediction is listed there with both numbers. The simulator predicts an
  extruded part to within a rounding error, so a disagreement usually means
  Inventor did something the recipe did not ask for — the wrong edge, the wrong
  side, or nothing at all. Do not report a part as finished with a divergence
  unexplained.
- **A failed build leaves the part where it stopped**, on purpose: the
  half-built part is usually what explains the failure. Pass
  `rollback_on_error: true` to `build_part_from_recipe` or `apply_operations`
  when the part matters more than the diagnosis — appending to something that
  already works, or retrying a hole, which consumes its sketch and so cannot be
  retried any other way. The report says whether the rollback happened; if it
  says the rollback itself failed, stop and look at the part before building on
  it.
- Revolve, sweep, loft, patterns and threads are written but have never been run
  against a live Inventor. Treat a success there with suspicion and check the
  `measured` block hard. `examples/belt_pulley.json`, `pipe_bend.json`,
  `duct_transition.json` and `threaded_boss.json` each isolate one of them.
- **Counts are parametric too.** `count`, `count1`, `count2`, `sides`, `rows`
  and `columns` accept an expression, so `"count": "bolts_per_side * 2"` works
  and a count is revisable like a length. A fractional result is refused rather
  than rounded, since 4.5 holes is a mistake.
- Assemblies, drawings and sheet metal are not supported at all.
