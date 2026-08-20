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

## Things worth knowing before you start

- Lengths are in the document's units (mm by default), angles in degrees. The
  server converts to Inventor's internal centimetres and radians.
- A closed profile is required for a solid feature. A sketch of hole centres has
  no profile by design and that is not an error.
- Regular polygons keep one degree of freedom: Inventor refuses the closing
  equal-length constraint. The geometry is right and dimensioned; the sketch is
  just not fully constrained.
- Counterbore, countersink and tapped hole styles are accepted and recorded but
  the current COM path drills a plain hole. Do not promise a user a tapped hole.
- Revolve, sweep, loft, patterns and threads are written but have never been run
  against a live Inventor. Treat a success there with suspicion and check the
  `measured` block hard.
- Assemblies, drawings and sheet metal are not supported at all.
