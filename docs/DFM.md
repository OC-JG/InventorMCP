# Manufacturability, in a closed loop

The [OnlyCat DFM tool](https://github.com/OC-JG/DFM) loads a mesh, measures it,
and scores how manufacturable the part is by injection moulding: wall thickness
and uniformity, draft, ribs and bosses, undercuts and the tooling they imply,
sink risk, shrinkage and warpage, flow length from a chosen gate.

This server can take that verdict, enact the parts of it that really are
parameter changes, rebuild, and ask the tool again. A finding is closed by a
measurement, not by an assertion that it has been addressed.

```
recipe ──build──► part ──export──► STL ──analyse──► findings
  ▲                                                    │
  └──────── rebuild ◄── set parameters ◄── propose ◄────┘
```

---

## The short version

```jsonc
// in the recipe
"dfm": {
  "parameters": {          // which parameter means what to the analyser
    "wall": "wall",
    "draft": "draft_a",
    "rib_thickness": "rib_t",
    "boss_wall": "boss_wall"
  },
  "frozen": ["boss_hole_d", "cable_w"],   // key geometry: do not change these
  "settings": { "material": "abs", "surfaceFinish": "spi-b1" }
}
```

```
build_part_from_recipe(recipe=...)     # or open an existing part
check_manufacture()                    # measure, and say what would change
improve_for_manufacture(rounds=3)      # change it, rebuild, measure again
```

`examples/moulded_housing.json` is a part built to exercise all of this: a
drafted, shelled housing with floor ribs and screw bosses, deliberately wrong in
two ways a parameter answers, with the pilot hole for the M3 screw and the cable
entry frozen because the screw and the connector decide those, not the moulding.

---

## Getting the analyser

The analysis runs headlessly through the DFM tool's own modules. No browser is
involved, and nothing here re-implements a rule.

```bash
git clone https://github.com/OC-JG/DFM.git
export INVENTOR_MCP_DFM_ROOT=/path/to/DFM     # or DFM_ROOT
```

A `dfm` directory alongside this repository is found without being told. Node 18
or newer is needed; no `npm install` is.

`dfm_capabilities` reports where the analyser was found, or says precisely what
is missing.

### Why this works at all

The DFM tool's analysis is pure: `analyseMesh` and `runDFM` take every input as
an argument and touch no DOM, which is what lets the tool run them in a Web
Worker. Its own unit tests import those modules straight into Node. So
`inventor_mcp/dfm/headless.mjs` calls the same functions the page calls, and the
answer it produces is the tool's answer rather than something resembling it.

Checked rather than assumed: on the tool's own `hollowFrustum(20, 30, 3, 2)`
fixture with clean inputs, the bridge returns a score of 100 out of a budget of
100 — which is what `test/unit.mjs` asserts for that part.
`tests/test_dfm_targets.py` asserts it on every run.

STL only. The tool reads STEP too, but through a 6 MB OpenCascade WASM module
fetched from a CDN, and Inventor writes STL perfectly well.

---

## Roles: telling the analyser what a parameter means

Several of the tool's checks are judged on numbers a person types into a panel —
nominal wall, rib thickness, boss diameter — rather than measured off the mesh.
The rib ratios in particular are computed against the *declared* wall, so a wall
figure that is merely close makes every one of them slightly wrong.

A model already knows all of those exactly. Declaring the map means those checks
run against the part instead of against somebody's recollection of it.

| Role | Supplies | Is |
|---|---|---|
| `wall` | `wallThk` | the nominal wall thickness |
| `draft` | `draftAngle` | the draft angle on the side walls |
| `rib_thickness` | `ribThk` | the thickness of a rib |
| `rib_height` | `ribH` | the height of a rib above the wall |
| `rib_fillet` | `ribRadius` | the fillet at a rib's root |
| `boss_od` | `bossOD` | a boss's outside diameter |
| `boss_wall` | `bossWall` | the wall thickness of a boss |

`wall` is worth more than the rest together: the rib, boss and corner guidelines
are all fractions of it.

The map is **declared, never guessed**. A role with no parameter is not an error
— the finding comes back named as one nobody can act on — because inferring
which parameter is "the wall" from its spelling is how a loop ends up thinning
the wrong thing.

A check whose figures no role supplies is judging the analyser's own defaults
against your wall, which can report a rib too thick on a part with no ribs. That
gets said rather than acted on. For a part with neither ribs nor bosses, switch
the check off:

```jsonc
"settings": { "checks": { "ribs": false } }
```

---

## Key geometry

An improvement loop is a machine for changing dimensions until a number stops
rising. Left alone it will thin the wall that seals against a gasket, shorten the
boss that sets a stack height, or open out the bore a bearing presses into —
every one of those a real way to raise a DFM score and a broken part.

Three ways to say what it may not touch:

```jsonc
{ "name": "seal_face", "value": "plate_t - gasket_crush", "frozen": true }
```
```jsonc
"dfm": { "frozen": ["boss_hole_d", "seal_*"], "frozen_features": ["Gasket"] }
```
```
protect_geometry(parameters=["bore_d"])
improve_for_manufacture(freeze=["bore_d"])
```

Three properties matter more than the list itself.

**It is enforced where parameters change, not where the loop runs.** A guarantee
that only holds inside one loop ends the moment anything else edits a parameter —
a tool call, a script, a later feature — and the report would still say the key
geometry had been protected. So `set_parameters` refuses a frozen parameter too,
and getting through takes `override_frozen=True`, said out loud.

**Depending on a frozen value is the same as changing it.** If `seal_face` is
frozen at `plate_t - gasket_crush`, then editing `plate_t` moves the frozen face
just as surely as editing it directly — and `plate_t` would not appear in any
frozen list, so every report would say the freeze had held. The protection
therefore follows the expressions: everything a frozen parameter is computed
from is protected too, transitively, and the refusal names the chain.

The reverse is deliberately not true. A parameter that *reads* a frozen value —
`clearance = bore_d + 0.2` — is free to move. Reading is not changing.

**A freeze is additive.** A run may be handed more to protect, never less.
Taking something out means editing the recipe, which is reviewable, rather than
passing a flag in the moment.

Note that `frozen` and Inventor's own `key` flag are different things: `key`
marks a parameter as prominent in Inventor's parameter table, `frozen` stops
automated changes.

---

## What gets changed, and what does not

Three checks are answerable by a parameter:

| Finding | What changes |
|---|---|
| wall under the material's minimum, or over its maximum | the wall parameter, by what the wall measured short |
| declared draft under what the material and finish require | the draft parameter, to required + 1° |
| rib or boss ratio outside its band | that parameter, as a fraction of the wall |

Everything else is reported with the tool's own wording and a reason for not
touching it:

| Finding | Why not |
|---|---|
| undercut | a tooling decision — a slide, a lifter, a stepped parting line, or a feature that releases |
| sink | mass behind a surface; cored out or moved, which is geometry |
| warp | shrinkage and shape together; evening the section, the gate, or a different grade |
| wall transitions | blended over a distance — geometry, and advisory on a mesh anyway |
| finish compatibility | a specification choice, not something about the part |
| flow length | where the gate goes, and how even the section is |
| corner radii | **cannot be measured from a mesh at all**, so a change could never be verified |

That last one is the honest limit. The tool advises on corner radii because an
STL carries no B-rep face topology to measure them from. A loop that cannot see
the result of its own change is guessing, so it does not make the change.

A finding nobody mentions reads as a finding everybody assumes was handled, so
refusals accumulate across rounds rather than being replaced by the last clean
proposal, and they come back under `needs_a_person`.

### Ratios come out as expressions

A rib that should be 45% of the wall becomes `wall_t * 0.45`, not `0.9 mm`.

The relationship is then structurally true rather than true until someone edits
the wall, and a later wall change carries the ribs with it instead of quietly
re-breaking the check. It makes the model *more* parametric than it was, which is
the opposite of what an optimiser usually does to one.

### A measured finding is corrected, not assigned

The wall parameter and the wall the checks measure need not be the same number —
a shell thickness, a boss wall and the thinnest section of a part can all differ.
So what is known is how far the measurement is out, and that much is what the
parameter moves. Assigning the target outright would set a 2 mm parameter to
1.32 mm on a part whose wall measured 0.5 mm and was already too thin.

It converges because the loop measures again, not because the first correction
was right.

### Changes that alter function are marked

A thinner wall, a shorter rib and a narrower boss all change what the part does
rather than only how well it moulds. They are applied by default — a loop that
reports without acting is not a loop — flagged as `changes_function` in the
result, and held back by `include_functional=False`. Freezing them is the
intended control, and it is enforced rather than advised.

---

## When it stops, and why

It always says which:

- nothing is left that a parameter change answers;
- a round made the score worse — and the values are put back, because the
  document is the deliverable and a described regression is still a worse part;
- a change repeats one already made, which is going round rather than forward;
- a change went in and the finding it answers is still reported, which is what a
  drifted target looks like;
- the round limit.

Each round records what it applied, which findings cleared, which appeared, and
which did not clear. That last column is the one to read: it is the difference
between a fix and a claim.

An untrustworthy mesh stops the loop before anything is changed. A score
computed on an inch-scaled or open mesh is arithmetic, not a judgement about the
part, and changing a part on the strength of one is changing it for no reason.

---

## The one duplication, and how it is contained

The DFM tool states its thresholds as literals inside its rules — "the 0.8×
ceiling", "below ABS minimum (1.2 mm)" — and does not export them. Material
limits do come across as numbers, in a `material_limits` block the bridge adds
from the tool's own table. The *targets* do not: aim at 45% of the wall rather
than 80%, a tenth above the material floor rather than exactly on it. Those are
choices about where inside a band to sit, and they live in
`inventor_mcp/dfm/remedy.py`.

Which means they can drift out of agreement with the check they are trying to
satisfy. Two things answer that, and neither is care:

**The loop re-measures.** A stale target shows up as a proposal that did not
work — reported, with the finding still listed — rather than as a part quietly
changed for nothing.

**`tests/test_dfm_targets.py` puts every target through the real engine** and
requires the check to come back clean, on meshes with known answers. It carries
negative controls too, so a margin that has become unnecessary is also caught:
the material floor alone still fails, the required draft alone still fails, and
adjusting only the boss wall still cannot satisfy both boss guidelines. If a
threshold moves in the DFM tool, that file fails and names the check.

It skips without Node or a checkout of the analyser. That is a real gap in
coverage rather than a tidy one, so it reports as skipped with the reason, and
the reason names where it looked.

In CI it skips by default, because the analyser is a separate private
repository. The `dfm` job runs it when a `DFM_REPO_TOKEN` secret with read
access to that repository exists, and prints a notice saying what is not being
checked when it does not. Locally the sibling checkout is found without being
told, so `pytest` runs the whole thing.

---

## Reading a report from the browser

The usual way a person meets this tool is with it open in a browser. Export the
JSON and hand it over:

```
read_dfm_report(path="OnlyCat-DFM-A3F9K.json")
```

No Inventor connection, no Node, no re-analysis. With a part open it says which
parameters would change and which are refused; without one it reads the findings
alone and says so rather than guessing at a model it cannot see.

---

## Two-shot and FPC

The report's two-shot block is read and passed through, and no rule acts on it.
An adhesion failure is answered by a different material pair, a shrinkage
differential by a different grade or a different interface, and coverage is
usually a mesh alignment problem — none of them a dimension of shot one. The
same goes for FPC overmoulding, except for one place where it does bite: an FPC
insert raises the floor under the wall check, because the overmould has to
contain the flex plus cover on both faces. The wall fix aims at that floor when
it is the higher one, so it does not propose a wall the check still fails.
