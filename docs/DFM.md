# Manufacturability, in a closed loop

The [OnlyCat DFM tool](https://github.com/OC-JG/DFM) loads a mesh, measures it,
and scores how manufacturable the part is by injection moulding: wall thickness
and uniformity, draft, ribs and bosses, undercuts and the tooling they imply,
sink risk, shrinkage and warpage, flow length from a chosen gate.

This server can take that verdict, enact the parts of it that really are
parameter changes, rebuild, and ask the tool again. A finding is closed by a
measurement, not by an assertion that it has been addressed.

```
 an .ipt ──copy──► bracket_v2.ipt ─┐
 a recipe ──build──────► part ─────┼──export──► STL ──analyse──► findings
 a STEP file ──import──► part ─────┘                                │
 an .stl ─────────────────────────────────────────────────► findings│
                                    ▲                               │
                                    └── rebuild ◄── set parameters ◄┘
```

Hand over a part and it works on the **next version of the file** —
`bracket.ipt` becomes `bracket_v2.ipt` — so the original cannot be changed by
anything that goes wrong, and the two can be compared afterwards. A STEP file is
imported and measured; it carries geometry and not the history that made it, so
there is nothing to drive and that is said rather than discovered. An `.stl` is
analysed with no Inventor involved at all.

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
improve_for_manufacture(path="bracket.ipt", rounds=3)   # the whole thing
```

or in steps:

```
check_manufacture(path="bracket.ipt")   # measure; changes nothing
discover_dfm_roles()                    # what the part says it means
declare_dfm(roles={...}, frozen=[...])  # correct it, and remember it
improve_for_manufacture(rounds=3)       # change, rebuild, measure again
```

`examples/moulded_housing.json` is a part built to exercise all of this: a
drafted, shelled housing with floor ribs and screw bosses, deliberately wrong in
two ways a parameter answers, with the pilot hole for the M3 screw and the cable
entry frozen because the screw and the connector decide those, not the moulding.

---

## Getting the analyser

The analysis runs headlessly through the DFM tool's own modules. No browser is
involved, and nothing here re-implements a rule.

The analyser ships with this repository as the `dfm/` git submodule, pinned to
the version the drift tests ran against:

```bash
git clone --recurse-submodules https://github.com/OC-JG/InventorMCP
# or, in an existing clone:
git submodule update --init dfm
```

Nothing to configure: the submodule is found first, then a `dfm` checkout
alongside this repository, and `INVENTOR_MCP_DFM_ROOT` (or `DFM_ROOT`, or the
`dfm_root` argument) overrides both — for pointing a run at a newer analyser
before moving the pin. Updating the pin is deliberate:

```bash
git -C dfm pull && git add dfm && git commit   # and run pytest: the drift
                                               # tests check the targets against
                                               # the new rules before you commit
```

Node 18 or newer is needed; no `npm install` is. The same checkout is the
browser tool — open `dfm/dfm-tool.html`.

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

### Declared, discovered, or neither — but never guessed

The rule used to be "declared, never guessed", and a part handed over as a file
declares nothing. So there is now a third way to settle a role, and the
distinction between it and guessing is the whole of it.

**Evidence.** A shell feature takes its thickness from somewhere. Whatever that
expression reads *is* the wall — not because it resembles one but because the
shell is built from it. Same for an extrude's taper and the draft. That is a
measurement of the model, and it is as good as somebody saying so. It comes back
marked `discovered`, with what it was read from, because it is a claim a person
may want to check:

```jsonc
"wall": { "parameter": "wall_t", "from": "discovered",
          "evidence": "the shell feature Cavity takes its wall thickness from it" }
```

**A spelling.** `wall`, `wall_t`, `t`, `thk`, `WallThickness`. A table of these
gets most parts right, and the ones it gets wrong are indistinguishable from the
ones it gets right until a loop has already thinned the wrong dimension. So a
likely-looking name is **offered and never applied** — it comes back under
`suggestions`, with the call that would accept it, and nothing acts on it.

**Two answers is not an answer.** Two shells reading two different parameters is
not a wall; it is two walls and a question. Reported, unmapped, with the call
that settles it.

A role nothing settles stays unmapped, and that is a useful answer rather than a
gap: a check judged on a role nothing supplies is judged on the analyser's own
default, which can report a rib too thick on a part that has no ribs. That gets
said.

`discover_dfm_roles` shows all of it without changing anything.

A check whose figures no role supplies is judging the analyser's own defaults
against your wall, which can report a rib too thick on a part with no ribs. That
gets said rather than acted on. For a part with neither ribs nor bosses, switch
the check off:

```jsonc
"settings": { "checks": { "ribs": false } }
```

---

## Handing over a file

| You give it | What happens | Can the loop improve it? |
|---|---|---|
| `.ipt` | opened as the next version, so the original is untouched | yes, if the part has parameters |
| `.stp` `.step` `.igs` `.iges` `.sat` `.x_t` | imported as a solid body | **no** — a translated file has no parameters |
| `.stl` | analysed directly; Inventor is not started | no |
| nothing | the part already open is used | yes |

The middle row is the one worth reading twice. A STEP file carries geometry and
not the history that made it, so there is no wall parameter to change — every
finding still applies, and none of them can be acted on. `check_manufacture`
measures it; `improve_for_manufacture` refuses with that reason rather than
running a loop that reports "nothing left to change" and sounds like success.

The same is true of an `.ipt` somebody made by importing a STEP file and never
parameterised, which is why whether a part can be driven is reported as a **count
of its user parameters** rather than inferred from its extension.

### Versions

`bracket.ipt` → `bracket_v2.ipt` → `bracket_v3.ipt`, keeping whatever separator,
case and zero-padding the last one used. Nothing is ever overwritten: two runs an
hour apart would otherwise land on the same name and the second would destroy the
first, including a copy somebody had already reviewed.

The copy is made by copying the file, not by opening the original and saving it
elsewhere — a filesystem copy cannot modify what it copies, where an open-and-
save-elsewhere leaves a window in which it could.

### Where the declaration lives

Whatever was worked out about a part is written back in two places, so the next
run starts from the same reading rather than inferring it again:

- **in the part**, as a custom iProperty. Travels through a rename and a move,
  and is visible in Inventor's iProperties dialog under Custom.
- **beside the part**, as `bracket.dfm.json`. Works everywhere, including the
  simulator, and is readable and correctable by a person.

A versioned copy carries the sidecar with it. One that had forgotten which
parameter was the wall would rediscover it, and might discover something else.

`declare_dfm` is how to correct it:

```
declare_dfm(roles={"rib_thickness": "rib_t"}, frozen=["bore_d"], material="abs")
```

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

## Comparing versions

The question on the second pass is not "is this manufacturable" but "is it better
than it was", and a versioned part is what makes that answerable:

```
compare_manufacture(before="bracket.json", after="bracket_v3.json")
```

`improve_for_manufacture` does it for you between its first and last round, under
`what_moved`.

Both go through the DFM tool's own `compareRuns` rather than a diff written here,
and the reason is the caveats. It knows which direction is better for each
measurement, and it refuses to let a score movement read as progress when it was
not: changing the material, changing the mode, or running a different set of
checks each raise a caveat above the diff, and two records with the same triangle
count are flagged as possibly the same geometry twice.

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
