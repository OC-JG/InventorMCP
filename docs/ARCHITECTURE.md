# Architecture

## The problem

Autodesk Inventor's automation API is a COM interface with a few thousand members,
opaque error messages, and enum constants that vary by version. Asking a language
model to drive it directly produces code that is plausible and wrong: sketches that
never close, dimensions that are numbers rather than expressions, and fillets applied
to whichever edge happened to be first in the collection.

Worse, "wrong" usually means *silently* wrong. A part built from hard-coded numbers
looks correct in a screenshot and is useless the moment somebody asks for a
variant — which is the entire point of CAD.

## The shape of the solution

Put a checkable intermediate representation between the language and the API.

```
description ──► recipe ──► static checks ──► plan ──► backend ──► Inventor
                  │                            │
             schema.py                    geometry.py
             (what to build)          (how it is constrained)
```

Four properties fall out of that:

1. **Validation before commitment.** A recipe is data, so it can be checked without
   a CAD seat: undefined parameters, dimensional errors, open profiles, references
   to sketches no earlier operation creates.
2. **One code path.** Whether an operation arrives as step 7 of a recipe or as a
   one-off `apply_operations` call, it goes through `builder.apply_operation`. The
   incremental and declarative ways of working cannot drift apart.
3. **Two backends, one contract.** The mock backend is not a test double bolted on
   afterwards; it is a peer implementation, which is what makes the whole thing
   testable on a machine with no Inventor.
4. **Parametric by construction.** Values travel as `Resolved(expression, value)`
   pairs all the way down. The backend writes the *expression* onto the Inventor
   parameter and uses the *value* only to position geometry.

## The layers

### `units.py` — one place that knows about centimetres

Inventor's API speaks database units: cm, radians, kg. Users speak mm, inches and
degrees. Every conversion happens here, and `Dim` tracks which physical dimension a
quantity has so the evaluator can do algebra on it.

### `expressions.py` — a small dimensional algebra

Expressions are parsed to a restricted Python AST and walked by a visitor that
carries `(length, angle, mass)` exponents alongside each number. That is what makes
`10 mm * 10 mm` an area, `sqrt(area)` a length, and `10 mm + 5 deg` an error.

Two design notes:

- The evaluator exists to *reject* and to *predict*, not to compute the model.
  Inventor evaluates the real expression; we evaluate a copy so we can catch
  nonsense early and so the mock backend can do arithmetic.
- `UnitContext` lets a bare number adopt the document's units when it is added to a
  dimensioned one, because `width - 16` is how engineers actually write expressions
  and how Inventor reads them. Without it, most real recipes would be rejected.

Security is a side effect rather than the goal, but it matters: recipe text may be
model-generated, and `eval` on it would be indefensible. No attribute access, no
calls outside a maths whitelist, no comprehensions, and a length cap.

### `schema.py` — the contract with the caller

Pydantic models with `extra="forbid"` throughout. A typo should come back as a
precise field-level error the caller can fix, not be silently dropped. Operations
and sketch entities are discriminated unions, so `{"op": "bevel"}` fails at the
union rather than deep inside a builder.

Coordinates are `ValueSpec` too, not floats — a centre written as
`[0, "box_h - lid_t"]` stays tied to its parameters.

### `geometry.py` — where "parametric" is actually earned

This is the part with the most engineering in it. A `rectangle` in a recipe becomes:

- four lines,
- six coincident constraints (four corners plus a construction diagonal),
- two horizontals and two verticals,
- a midpoint constraint pinning the diagonal's centre to the origin,
- two driving dimensions carrying the caller's expressions.

Every entity is expanded so that it ends up with the degrees of freedom a human
would have removed. A polygon uses a construction circle with vertices coincident
on it and `n - 1` equal-edge constraints, which leaves exactly one DOF — its
rotation — and does not over-constrain, which Inventor rejects outright.

Locating is deliberate: a zero coordinate becomes an *alignment constraint* (free,
and cannot flip sign) while a non-zero one becomes a driving dimension. Inventor
dimensions are unsigned, so geometry is created on the correct side and the solver
keeps it there; negative offsets have their expression converted to a magnitude.

### `plan.py` — the backend-neutral sketch IR

`SketchPlan` holds primitives, constraints and dimensions in database units with
expression strings attached. Because both backends consume it, the mock can verify
constraint bookkeeping that would otherwise only be observable inside Inventor —
that a slot really does get four tangencies, that a rectangle's centre really is
pinned.

### `backend/` — two implementations of one interface

`Backend` is an ABC. Requests reaching it are fully resolved: lengths in cm, angles
in radians, each driving value carrying its expression.

**`com/`** drives Inventor. Two things there are worth knowing about:

- *Enum resolution.* Inventor's enums come from its type library. When pywin32 can
  generate the early-bound wrapper, values are read from the installed version,
  which is always right. When it cannot — a read-only `gen_py`, a locked-down
  profile, a version change — a fallback table is used. `Constants.describe()`
  reports which source each value came from, so a mismatch is diagnosable instead
  of mysterious.
- *Export.* `Document.SaveAs` is used rather than hunting translator add-in GUIDs,
  which vary between releases; the result is verified on disk and a missing file is
  reported as a probable disabled translator rather than as success.

**`mock/`** keeps an in-memory model. It is honest about being an approximation:
volumes are analytic estimates, topology is synthesised from the sketch loops that
produced each feature, and every response is flagged `"simulated": true`. It is
useful precisely because it models the things that are easy to get wrong — profile
closure, how many vertical edges a fillet will catch, whether a through-hole crosses
the thickness rather than the length.

### `tools/` — the MCP surface

Thirty tools rather than one per feature type -- thirty-one when the escape
hatch is on. Feature creation goes through `apply_operations` with the same
operation objects a recipe uses, which keeps the tool list small and means there
is one syntax to learn instead of two. Most of the count is not modelling: seven
document-lifecycle tools and eleven manufacturability ones.

Every tool is wrapped by `guard`, which turns exceptions into
`{"ok": false, "error": ..., "message": ..., "hint": ...}`. A caller that gets a
structured error can fix its own input; one that gets a stack trace cannot.

The recipe cheat-sheet is embedded in the descriptions of the three tools that take
recipes, rather than only in a resource. A model that has to fetch a schema before
it can write anything will often guess instead.

That has a price worth knowing: the three copies are 98% identical and come to
27 kB, about 8,400 tokens of the tool list, of which roughly 4,600 is the
duplication. It is paid on every request to a client that has this server
enabled, whether or not the conversation is about CAD. Since the cheat-sheet was
written, `skills/inventor-parametric-modelling/` has come to carry the same
material and loads only when it is relevant, so the argument for paying it three
times is weaker than when the decision was made. Left alone for now, because the
Skill is a Claude-specific mechanism and the tool descriptions are not.

## Things deliberately left out

- **Assemblies.** Parts first. The recipe schema has no `iam` concept and adding one
  properly means constraints between components, which is a second design problem.
- **Producing drawings.** No drawing views, no sheets, no title blocks -- the
  same reasoning as assemblies. *Reading* one is a different thing and is
  supported: `check_against_drawing` compares a `DrawingReading` against a
  recipe, and `drawing.py` holds the schema for it. The distinction is the point
  of the design, and `docs/DECISIONS.md` argues it under "A drawing is read, not
  traced": tracing a drawing's outlines gives exact geometry and no parameters,
  which is the one thing this project exists not to produce.
- **Sheet metal.** A different feature set with its own rules, and none of it is
  reachable from the recipe schema.
- **A real geometry kernel in the mock.** The point of the mock is fast feedback on
  the mistakes that recipes actually make, not an independent CAD system.

## Testing

The whole suite runs without Inventor.

The shipped examples are part of the suite: each must parse, pass the static checks,
build, and produce a solid body, and every declared parameter must be referenced or
commented. That last check has already caught one dead parameter. Because the
examples are also the documentation, a schema change that breaks them breaks the
docs in the same commit.

Two bugs were found by writing the examples rather than by writing tests: bare
numbers in mixed arithmetic were rejected, and the mock's YZ-plane axis mapping put
the plane offset on the wrong axis. Both now have regression tests. That is the
argument for keeping examples executable.
