# Demonstration script

Three demonstrations in one sitting, about eighteen minutes. The first takes a spoken
description to a parametric Inventor part and then changes it by changing a number. The
second takes a moulded part, measures how well it will mould, and lets the tool fix what
a parameter can fix while refusing to touch what a person decided. The third puts that
same verdict back in front of the analyser's own panel, so the score on screen is the
factory's number rather than ours.

The numbers below were measured on **Inventor 2027.1** and then reproduced on
**Inventor 2026.1**, where a full acceptance run passed every check it compared and the
improvement loop reached the same score by the same four changes. Section
["Inventor 2026"](#inventor-2026) records what that run settled and the one thing it did
not. Read it before the dry run, not after.

The presenter-facing text is in quote blocks. The tool calls and expected values under
each step are for checking the run afterwards, not for reading aloud.

---

## Before you start

### 1. The machine

| | |
|---|---|
| Windows, with Autodesk Inventor installed and licensed | required |
| Node 18 or newer on `PATH` | required for part two only |
| The `dfm/` submodule checked out | required for part two only |
| A second monitor, or Inventor tiled beside the Claude window | strongly advised |

### 2. In this order

```powershell
cd C:\path\to\InventorMCP

# 1. the analyser and the package
git submodule update --init dfm
.venv\Scripts\activate
pip install -e ".[inventor,dev]"

# 2. the analyser's one dependency
node --version                       # must print v18 or higher

# 3. nothing must be pointing the analyser somewhere else
echo $env:INVENTOR_MCP_DFM_ROOT      # must be empty
echo $env:DFM_ROOT                   # must be empty
echo $env:INVENTOR_MCP_BINDING       # must be empty, NOT "early"

# 4. the offline half
pytest -q

# 5. the enum cache, which is version-specific and you have changed version
Remove-Item -Recurse -ErrorAction SilentlyContinue "$env:LOCALAPPDATA\Temp\gen_py"
py -c "import win32com.client; win32com.client.gencache.EnsureDispatch('Inventor.Application')"

# 6. what Inventor actually says about feature health on this release
python scripts\dump_constants.py --find Health
```

Step 6 was the one that mattered on an unverified release. 2026.1 has since been run and
its feature health reads correctly, so this is now a cheap confirmation rather than a
gate. See ["Inventor 2026"](#inventor-2026).

### 3. Start Inventor by hand

Launch Inventor yourself and leave a blank part open before anyone is watching. `connect`
tries `GetActiveObject("Inventor.Application")` first, so it attaches to the session
already on screen rather than launching a cold one, which takes tens of seconds. Close
every other document, so the parts this demonstration builds are the only ones in the
window.

Put Inventor where the audience can see it and leave it in front. Nothing in the server
raises Inventor's window above the Claude window — Windows z-order is not the COM API's
business — but nothing steals focus back either.

### 4. The five-minute dry run

This proves the whole path without giving the demonstration. Both commands drive a real
Inventor.

```powershell
python scripts\live_acceptance.py --only angle_bracket parameter-edit
```

Proves part one. It builds `examples/angle_bracket.json`, checks the volume against the
recorded 43.1999 cm³ within 5×10⁻⁴, sets `base_len` to 120, rebuilds, and asserts the X
span landed within 0.01 mm of 120. Expect every line to read `[ ok ]`. The line

```
         volume 43.1999 -> 52.1999 cm^3
```

is the demonstration's payoff, printed.

```powershell
python scripts\live_acceptance.py --only dfm
```

Proves part two. It builds the housing, runs three rounds of the improvement loop, and
asserts that a finding it acted on actually cleared when measured again, that the frozen
M3 pilot hole and cable entry came out the size they went in, that the derived boss
diameter followed its wall down, and that the part was not left worse than it started.
The notes it prints include the score and grade at each end, which tells you what numbers
to expect on the day.

If either command reports `[skip]` rather than `[ ok ]`, read the reason. A skip is not a
pass.

Delete the parts both runs left open before the demonstration.

---

## Running order

| | | Minutes |
|---|---|---|
| 1 | Connect | 0:30 |
| 2 | Say the bracket | 1:30 |
| 3 | Watch it build | 1:00 |
| 4 | Look at the parameters in Inventor | 1:30 |
| 5 | Change one number | 1:00 |
| 6 | Check the prediction | 0:30 |
| 7 | Save and put the pictures side by side | 0:30 |
| 8 | Build the housing | 1:30 |
| 9 | Say what may not move | 0:30 |
| 10 | Measure it, change nothing | 1:30 |
| 11 | Run the loop | 3:00 |
| 12 | Read what it refused | 1:00 |
| 13 | Look at the parameters again | 0:30 |
| 14 | Close | 0:30 |
| 15 | Find what the loop wrote | 0:30 |
| 16 | Load the improved part in the panel | 0:30 |
| 17 | Fill the panel in from `analyser_input` | 1:00 |
| 18 | Compare against round zero | 1:00 |

Steps 11, 12 and 18 are the ones to protect if you are running short. Drop step 4, then
step 13, then the whole of part three -- it is the best ending and the most expendable
middle.

---

# Part one — a sentence becomes a part you can revise

## Step 1 — Connect

> "Connect to Inventor."

**The audience sees** nothing yet. Inventor is already open with a blank part.

**In the reply**, `"backend": "inventor"`, `"simulated": false`, and a `version` string.
Read the version out — it is the first and only place the audience learns which release
this is running against.

```
connect(backend="inventor", visible=true, start_if_needed=true)
  → {"backend": "inventor", "simulated": false, "version": "...", "documents": 1}
```

`backend` must be passed explicitly. The shipped `.mcp.json` launches the server with
`--backend auto`, and `connect`'s own default is `"auto"`, whose behaviour when Inventor
is unreachable is a silent fall-back to the simulator. Asking for `"inventor"` by name
makes it raise instead.

## Step 2 — Say the bracket

> "I need a steel L-bracket — base 90 long, upright 70 tall, 6 thick, 50 wide. Two 20 × 9
> adjustment slots in the base, 30 apart. Pair of M8 clearance holes in the upright,
> 30 apart, 15 down from the top. Round the inside corner off at 8."

Say it the way an engineer dictates: sizes, then features, then the finishing radius.
Do not repeat units after the first one.

**The audience sees** Claude write a recipe and validate it before touching the CAD seat.
Point at the validation: it is free, it needs no Inventor, and it rehearses the whole
build in a simulator so a wrong part is caught before a CAD seat is spent on it.

**Read the rehearsal number out loud before the build runs.** That turns the next step
into a prediction met rather than a number produced.

```
validate_recipe(recipe={…})
  → ok: true, warnings: [], findings: []
  → result.volume_cm3 = 43.201212
  → result.span_mm    = [90.0, 50.0, 70.0]
```

If the rehearsal says anything else, Claude has written a different bracket. **Stop and
look.** Two figures to recognise:

- **43.964619** means the two M8 holes in the upright are missing. The skill's worked
  example of this bracket does not include them, so this is the likely miss. Ask again:
  *"You have left out the two M8 holes in the upright."*
- A `warnings` entry saying a parameter *drives nothing*, or that a sketch *does not
  reach the part*, means a valid recipe that builds the wrong object. Fix it here. It
  costs nothing.

## Step 3 — Watch it build

**The audience sees** the bracket appear in Inventor feature by feature — eight steps:
section, body, slot sketch, slot cut, mirrored slot, hole sketch, two holes, fillet. Each
one pops into existence whole rather than being drawn, because the backend suppresses
screen updating within each burst and calls `Update()` on the way out.

**Point at the returned JSON, not the geometry.** Every operation carries a `measured`
block, because Inventor reports success for operations that did nothing. Two of this
bracket's three historical geometry bugs hid behind a green line.

```
build_part_from_recipe(recipe={…})
  → ok: true, errors: [], no `divergence` key
  → mass_properties.volume ≈ 43.1999

  operations[].measured.volume_change_cm3, in order:
    Body            +46.2000   (first measurement, nothing to compare)
    SlotCut          -1.4610
    SlotPair         -1.4611
    UprightFixings   -0.7634   (two Ø9 holes 6 deep: 2 × π·4.5²·6 mm³)
    InsideCorner     +0.6867
```

The fillet being **positive** is the tell that the `concave` selector found the inside
corner. A fillet on an outside corner removes material.

The absence of a `divergence` key is the second thing worth saying: the build was
rehearsed in the simulator first and every operation moved the volume the simulator said
it would.

```
measure_part()
  → volume_cm3   43.1999      (tolerance ±0.0005)
  → bounding_box.min  [0, -25, 0] mm
  → bounding_box.max  [90, 25, 70] mm
  → bounding_box.size [90, 50, 70] mm
  → mass_kg      ≈ 0.339      (steel at 7.85 g/cm³)

capture_view(path="C:\Demo\bracket_90.png", orientation="iso")
  → written: true
```

Run `capture_view` whether or not Inventor is visible. It calls `Document.Activate()` and
fits the camera, and the PNG lands in the chat, so the audience sees the bracket even if
the Inventor window is buried.

Check the volume. Mention the mass. 7.85 g/cm³ is the simulator's figure for steel and is
what Inventor's library steel normally carries, but which "Steel" resolves to in a 2026
library has not been checked here.

## Step 4 — Look at the parameters inside Inventor

This is the step that convinces engineers, and it is three clicks in Inventor's own
interface. Do it yourself, in the CAD window, not through Claude.

**Manage tab → Parameters panel → Parameters** (the *fx* button).

> "These aren't notes about the model. This is the model."

**The audience sees** two sections in the dialog.

- **User Parameters** — all ten by their own names. Read the **Equation** column, not
  Model Value. `hole_z` reads `upright_h - 15 mm` with a nominal value of 55: a parameter
  defined by another parameter, stored in the file. Type `120` into `base_len`'s cell and
  press Enter and the bracket rebuilds under the dialog. Worth doing once, because it
  shows that what was built is an ordinary Inventor part with no residue of how it got
  there. Put it back to 90 afterwards.
- **Model Parameters** (`d0`, `d1`, `d2`…) — the sketch dimensions themselves. Their
  Equation column reads `base_len`, `thk`, `upright_h`, `slot_pitch / 2`. This is the
  difference between a parametric model and a shape. A traced or imported body has none
  of these.

**The Model browser** on the left reads `Section` · `Body` · `Slots` · `SlotCut` ·
`SlotPair` · `UprightHoles` · `UprightFixings` · `InsideCorner` — the recipe's own names,
not `Extrusion1`, `Extrusion2`, `Hole1`. Someone can maintain this file.

Optional, and the single most persuasive screen in the demonstration if the wording is the
same on 2026: **Tools → Options → Document Settings → Units → Modeling Dimension Display
→ Display as expression**, then double-click `Section` in the browser. Every dimension on
the profile now reads as its parameter name instead of a number. If the menu wording
differs, right-click any dimension → **Dimension Properties** shows the expression in a
dialog instead.

```
inspect_part(include_model_parameters=true)      # optional, for the chat record
  → parameters: all ten with their expressions
```

Do not claim the absence of a `warnings` array proves the sketches are fully constrained.
On a live Inventor `FullyConstrained` is not exposed under that name, so sketches report
`null` and the warning cannot fire either way. It is absent because Inventor will not say,
not because it said yes.

## Step 5 — Change one number

> "Base needs to be 120, not 90."

Say nothing about the slots. That is the entire point.

Before the call returns, say the prediction out loud:

> "The volume should go up by exactly nine cubic centimetres, because all that has changed
> is a 30 by 6 by 50 block of base plate. Nothing else in the part depends on the base
> length."

**The audience sees** the base shoot out to 120, **the two slots travel 30 mm outboard
with it**, and the upright, its two M8 holes and the 8 mm gusset radius sit completely
still.

The slots relocate rather than stretch. Their centre is written `base_len - 25`, so they
stay 25 mm from the free end of the base, which is where you want fixing slots. At 90 the
pair spans x = 50.5 to 79.5; at 120 it spans 80.5 to 109.5 — entirely clear of where it
was. "Some things move, some things don't, and both are right" is a better story than
"everything scales", and it is the correct engineering intent.

```
set_parameters(parameters=[{"name": "base_len", "value": 120}], rebuild=true)
  → rebuild.errors: []
  → bounding_box.size = [120.0, 50.0, 70.0]
  → mass_properties.volume ≈ 52.1999
```

## Step 6 — Check the prediction

```
measure_part()
  → volume_cm3   52.1999      (43.1999 + exactly 9.0000)
  → bounding_box [0, -25, 0] → [120, 25, 70] mm
  → mass_kg      ≈ 0.410

capture_view(path="C:\Demo\bracket_120.png", orientation="iso")
```

| | base_len 90 | base_len 120 |
|---|---|---|
| Volume, live | **43.1999 cm³** | **52.1999 cm³** |
| Volume, rehearsal | 43.201212 cm³ | 52.201212 cm³ |
| Size | [90, 50, 70] mm | [120, 50, 70] mm |
| Mass at 7.85 g/cm³ | ≈ 0.339 kg | ≈ 0.410 kg |

Tolerance on the volume is **±5×10⁻⁴ cm³** — the project's own figure, loose enough for
Inventor's rounding and tight enough that a missing 9 mm hole (0.382 cm³) or a fillet on
the wrong edge (0.687 cm³) cannot hide inside it. Tolerance on the X span is ±0.01 mm.

Do not quote face and edge counts. The simulator says 16 and 29; no live topology count
for this part has ever been recorded, so there is no measured number to promise.

## Step 7 — Save, and put the two pictures side by side

> "Save it."

```
save_part(path="C:\Demo\AngleBracket.ipt")
```

Optional encore, only if everything so far has landed cleanly. **This one is not
live-verified**, so treat it as a bonus rather than part of the script:

> "And take the upright to 90."

`hole_z` is `upright_h - 15`, so the upright grows and the two M8 holes ride up with it,
staying 15 mm below the top. It shows a derived parameter recomputing rather than a
position expression evaluating.

### One honest caveat, if somebody asks

Two numbers in this bracket are literals rather than parameters: the `25` in
`base_len - 25` (the slot setback from the free end) and the `±15` that places the two
upright holes. Neither affects what you have just shown — the slots still track
`base_len` — but the hole spacing you dictated as "30 apart" is not itself driveable in
this recipe. That is the right answer to give, and it is not a bug.

---

# Part two — a part that has to be moulded

Part one was text to model. Part two names a file, because the point here is not the
authoring — it is what happens after the part exists.

## Step 8 — Build the housing

> "Build me the moulded housing from `examples/moulded_housing.json` — check the recipe
> first."

**The audience sees** a drafted, shelled ABS box appear: 100 × 70 × 30, two floor ribs,
four screw bosses, a cable slot through one end. It looks like the inside of a small
enclosure, because it is one.

```
validate_recipe(recipe=<moulded_housing.json>)
  → ok: true, warnings: [], findings: []

build_part_from_recipe(recipe=<same>, rollback_on_error=true)
  → ok: true
  → 16 parameters, 13 operations
```

**Do not quote a volume for this part.** The rehearsal reports 46.229078 cm³ and that is
still not a prediction of what Inventor will measure: the simulator ignores the draft
taper on the outer walls, and this part is drafted throughout. Its cable cut is right
now — 0.36 cm³, the two 2.5 mm end walls the slot passes through, measured off the
ledger of prisms rather than charged the whole 12 × 6 × 100 mm sweep, which is where
7.2 cm³ of the old number came from. The rehearsal's job on this part is the empty
`warnings` list, not the volume.

The build also installs the recipe's own freeze. `boss_hole_d`, `cable_w` and `cable_h`
are declared frozen in the recipe's `dfm` block, and from this moment `set_parameters`
refuses them too — not only the improvement loop.

```
capture_view(path="C:\Demo\before.png", orientation="iso")
```

## Step 9 — Say what may not move

> "Before you touch anything: the M3 pilot hole `boss_hole_d` and the cable entry
> `cable_w`/`cable_h` are set by the screw and the connector, and the outer taper
> `draft_a` sets the rim the lid seals against. None of those may move. Protect them."

The order matters and is worth saying out loud: **the freeze comes before the
measurement.** The preview in the next step has to honour it too, or
`check_manufacture` promises a change that `improve_for_manufacture` then refuses — a
preview that contradicts the act it previews.

```
protect_geometry(parameters=["draft_a"])          # the other three are already frozen
  → key_geometry.declared = ["boss_hole_d", "cable_w", "cable_h", "draft_a"]
```

Passing all four is fine; a freeze is additive and nothing can remove one.

The engineering justification for freezing `draft_a` is checkable on the model: the outer
block is tapered by `draft_a` from a fixed 100 × 70 base, so at 30 mm tall a change from
1.5° to 1.68° moves the rim by about 0.09 mm per side. The rim is where the lid seals.

## Step 10 — Measure it, change nothing

> "Now measure it for injection moulding in ABS, SPI-B1. Don't change anything — just tell
> me the score and what you'd want to change."

**The audience sees** the part exported to a mesh, the analyser run over it, and a score
and grade come back with every finding itemised. Nothing in Inventor changes.

```
check_manufacture()                # no path: work on the open document
  → score, grade, critical_findings, budget, findings[]
  → checks[]              per check: {key, name, status, severity, detail, weight, deduction}
  → would_change.changes[]        the four ratio fixes, each with from/to/target/why
  → would_change.not_acted_on[]   the frozen draft refusal
  → key_geometry, read_the_part_as, mesh_confidence, stl, report
```

**Reference run on 2027.1: 64, MAJOR REWORK.**

What that 64 is made of, against the analyser's own weights (wall 22, draft 18, sink 13,
flow 12, ribs 11, warp 11, undercut 10, finish 3, summing to 100; a minor finding costs a
quarter of its weight, a major a half, a critical all of it):

| Check | Round 0 | Why |
|---|---|---|
| **draft** | critical, 18 | The ribs, bosses and cable cut carry no taper, so more than a quarter of the side-wall area sits at 0°. |
| **sink** | major, ~6.5 | Mass gathering at the rib roots and boss roots. |
| **ribs** | major, ~5.5 | Four ratios out of band at once — see below. |
| **undercut** | minor–critical | The cable slot is a through-slot normal to the pull. |
| **warp** | minor, ~2.75 | ABS shrinkage against this section. |
| wall | clean | 2.5 mm sits inside ABS's 1.2–3.5 band. |
| flow | info, 0 | No gate chosen yet. Not a defect; the loop supplies one. |

The four rib and boss ratios, which are arithmetic off the recipe and will reproduce
exactly:

| | Now | Band | Verdict |
|---|---|---|---|
| rib thickness / wall | 1.8 / 2.5 = **0.72** | 0.4–0.5 wanted; 0.6–0.8 is structural-only | warn, major |
| rib height / rib thickness | 12 / 1.8 = **6.67×** | cap is 3× | escalates to major |
| rib root radius / wall | 0.4 / 2.5 = **0.16** | 0.25–0.40 | escalates to minor |
| boss wall / wall | 2.25 / 2.5 = **0.90** | 0.7 ceiling | escalates to major |

The required draft is **0.68°** — ABS's 0.5° plus 0.18° for the SPI-B1 texture. The part
declares 1.5°, which passes on the stated angle. It is the **mesh** that fails the draft
check, not the number.

Read `mesh_confidence` before moving on. If the mesh is not trustworthy the loop will
decline to change anything, correctly, and say so.

## Step 11 — Run the loop

> "Go on then — fix what a parameter can fix, rebuild it, and measure it again each round.
> Anything you can't fix, tell me why."

**The audience sees** the housing rebuild in Inventor: the ribs get thinner and much
shorter, the bosses shrink, and the part is re-exported and re-measured. This takes a
minute or two per round. Say what is happening while it runs — the point is that a
finding is closed by a *measurement*, not by an assertion that it was addressed.

```
improve_for_manufacture(rounds=4)
  → score.start / score.end / score.change
  → grade.start / grade.end
  → rounds[]: each with score, grade, findings, changes, cleared, did_not_clear, report
  → needs_a_person[]   the refusals, accumulated across every round
  → notes              includes the gate note
  → what_moved         the analyser's own before/after comparison
  → not_saved          the model is changed and nothing is on disk yet
  → stopped_because
```

**Reference run: 64 → 85, MAJOR REWORK → PRODUCTION READY, in one round.**

The four changes, written as expressions rather than numbers so the ratio is a property
of the model rather than of one moment in its history:

| Parameter | From | To | Becomes | Alters function? |
|---|---|---|---|---|
| `rib_t` | 1.8 | `wall * 0.45` | 1.125 | no |
| `rib_h` | 12 | `rib_t * 2.5` | 2.8125 | **yes** |
| `rib_r` | 0.4 | `wall * 0.3` | 0.75 | no |
| `boss_wall` | 2.25 | `wall * 0.6` | 1.5 | no |

Two consequences worth pointing at:

- **`ribs` clears outright.** 0.45 is inside the 0.4–0.5 band, 2.5× is exactly the cap,
  0.30 is inside 0.25–0.40, and 0.60 clears the 0.7 sink ceiling while still leaving
  1.5 mm of boss wall against the 1.375 mm a Ø5.5 boss wants around an M3 screw. All
  arithmetic, all reproducible.
- **The `rib_h` change is the one that carries the grade.** It is the only change flagged
  `changes_function: true`. Dropping the ribs from 12 mm to 2.81 mm removes most of the
  undrafted rib side-wall area, which is what moves the draft check out of *critical* —
  and a part with one critical finding is capped at MINOR REWORK however good its score.
  85 is unreachable while that check stays critical.

**Expect it to stop, and expect the reason to be a good one.** Round 2's proposal is
empty — the ratios are already at target, and the draft is frozen — so:

```
stopped_because: "nothing is left that a parameter change answers; what remains needs a
                  person"
```

That is the ending you want on screen. Say it as a success, because it is one.

## Step 12 — Read what it refused

> "What moved?"

This is the beat the whole demonstration exists for.

**The audience sees** `needs_a_person[]`, and inside it the draft entry:

```json
{ "check": "draft",
  "not_acted_on": "frozen",
  "role": "draft",
  "severity": "critical",
  "why": "The draft angle needs to increase. draft_a is declared as key geometry.",
  "frozen": { "parameter": "draft_a",
              "reason": "declared as key geometry",
              "via": [],
              "explanation": "draft_a is declared as key geometry" } }
```

> "It wanted to raise the draft to 1.68 degrees. It didn't, because I told it that angle
> sets the sealing rim. It says what it wanted and why it didn't do it — and it kept
> saying so through every later round, rather than dropping the refusal once it had a
> clean proposal."

Then the second, quieter freeze, which is different in kind:

> "And look at the bosses. It wanted the boss wall down from 2.25 to 1.5. The pilot hole
> for the M3 screw is frozen — so it did not overwrite the diameter. It read the
> expression, saw that `boss_d` is written as `boss_hole_d + 2 * boss_wall`, and let the
> diameter follow the wall down. Boss wall 2.25 to 1.5, boss diameter 7.0 to 5.5, pilot
> hole still exactly 2.5. The boss resized around the screw."

That is legal because the protection follows what a frozen value is *computed from*, not
what reads it. A parameter that uses a frozen value is free to move; changing it does not
disturb what it read.

Worth saying out loud: the guarantee is not the loop's. It is enforced where parameters
change, so `set_parameters` refuses a frozen parameter too, and getting through takes
`override_frozen=true`, said deliberately.

Everything else left open — the draft geometry, sink, the undercut on the cable slot,
warp — is in `needs_a_person[]` with the tool's own wording for each. None of them is one
number. Sink is mass behind a surface, fixed by coring it out. An undercut is a tooling
decision, answered by a slide or a lifter or by stepping the parting line. Read one of
them out; the wording is better than a paraphrase.

```
read_dfm_report(path=<rounds[-1].report>)
  → checks[]   per-check verdicts for the FINAL state
```

This step is mandatory if you want a "wall ✓, ribs ✓, draft ✗" table on screen at the
end. The loop's own output carries finding *keys* only; the per-check verdicts live in
`check_manufacture.checks[]` and in the report file, not in the loop result.

## Step 13 — Look at the parameters again

> "Save it and show me the inside."

Open Inventor's Parameters dialog again. `rib_t` now reads `wall * 0.45` in the Equation
column, `rib_h` reads `rib_t * 2.5`, `boss_wall` reads `wall * 0.6`, and each carries the
comment `DFM round 1: ribs`. `boss_hole_d` still reads `2.5`.

> "It didn't write numbers in. It wrote the ratio the check tests. Change the wall next
> year and the ribs keep their relationship to it instead of breaking it."

```
save_part(path="C:\Demo\MouldedHousing.ipt")
capture_view(path="C:\Demo\after.png", orientation="iso")
```

The loop leaves the model changed and unsaved when it is working on a document you already
had open — it says so in `not_saved` — so the save is not optional if you want the file.

## Step 14 — Close

Put `before.png` and `after.png` side by side.

> "Two things happened here that are worth separating. It made four changes and proved
> each of them by measuring the part again — a fix closed by measurement rather than by
> assertion. And it left four things alone: three because I told it they were decided by
> the screw, the connector and the seal, and one because no single number answers it. It
> said which was which."

---

# Part three — the same verdict, in the DFM tool's own panel

Three minutes. Optional, and the thing people remember, because up to here the score has
been a number in a chat window.

The browser tool is the same checkout: `dfm\dfm-tool.html`, one self-contained file,
opened from the filesystem. It wants a connection for its 3D viewer; if the room has no
internet, run `cd dfm && node build.js --vendor` beforehand and `node build.js` afterwards
to restore the committed file.

## Step 15 — Find what the loop wrote

The loop reports its own workspace and, for every round, the mesh as well as the report:

```
improve_for_manufacture(...)
  → workspace        C:\dfm-demo\MouldedHousing.ipt-20260828-103207-118455
  → analyser_input   {wallThk: 2.5, ribThk: …, bossOD: …, material: "abs", …}
  → rounds[0].stl    …\round-0.stl     rounds[0].report  …\round-0.json
  → rounds[N].stl    …\round-N.stl     rounds[N].report  …\round-N.json
```

**Take the last round that does not carry a `reverted` key.** A round that made the part
worse is kept as history and its values were put back, so its mesh is not what is in
Inventor. Call that round *N*.

Pass `workspace="C:\dfm-demo"` to `improve_for_manufacture` at step 11 so this directory
is somewhere you can find rather than under whatever the server's working directory
happens to be.

## Step 16 — Load the improved part

Open `dfm\dfm-tool.html`. Click **`Start over`** -- the panel remembers its last settings
in browser storage, and you want the same baseline every time.

Drop **`round-N.stl`** on the **`Drop STL or STEP file`** zone.

**The audience sees** the housing in the viewer, and a strip giving triangle count, volume
and area.

## Step 17 — Tell it what the loop was told

Set **Polymer** to `ABS` and **Surface finish** to `SPI B-1 (600-grit paper)`. Leave
**Pull direction** on `+Z`. Expand **`Manual specification`** and type in the values from
`analyser_input` -- nominal wall, min and max wall, draft angle, rib thickness, rib
height, rib base radius, boss OD, boss wall, undercuts.

**This step is not optional and it is the one trap in the demonstration.** The loop feeds
the analyser the part's own parameter values; a fresh panel uses the tool's defaults. Skip
this and the browser will contradict the loop on stage, by several points, for a reason
nothing on screen explains. It is why the loop now reports `analyser_input` at all -- it
is exactly this form, filled in.

Click **`▶ Run analysis`**.

**The audience sees** the score, the grade, a strip per check and the findings list.
**It should equal `round-N.json`'s score.** If it does not, one field above is wrong;
`↓ Export JSON` and diff its `input` block against the report's to find which.

## Step 18 — "And here is what moved"

Click **`⇄ Compare with JSON`** and choose **`round-0.json`** -- the untouched part, from
before the loop ran.

**The audience sees**, under *Compared with a previous run*: an arrow and `64 → 85`, a
headline sentence naming which checks resolved, the two runs labelled by name and
material, then a row per check that moved (`critical → none`) and a row per measurement
with before, after and delta.

That is the loop's own claim, re-derived by the tool that made the original judgement,
from files on disk, with nothing from this repository in the path except the mesh.

> "The score didn't come from us. It came from the same analyser the factory report comes
> from. All we did was drive the parameters and hand it the part again."

**One caveat may appear:** *"Both runs have N triangles — this may be the same geometry
twice rather than a revision."* Inventor's tessellation of planar faces does not change
when a dimension does, so identical counts are expected here and the heuristic is simply
wrong. The volume row directly beneath it proves the geometry moved. Say so plainly; it
is a five-line fix upstream and a good note to end on.

## Without Inventor, or without the browser

Same story, no CAD seat, for a rehearsal or a laptop:

```bash
node tests/dfm_shapes.mjs dfm before.stl hollowFrustum 20 30 0.5 2
node tests/dfm_shapes.mjs dfm after.stl  hollowFrustum 20 30 3   2
```

Analysed with the part's own numbers those score **77 MINOR REWORK** and **100 PRODUCTION
READY**. Drop `after.stl` on the panel, run it, then compare against a report generated
from `before.stl` with `inventor_mcp/dfm/headless.mjs`.

And with no browser at all, `compare_manufacture(before=…, after=…)` runs the same
comparison and returns the same headline.

---

## Recovery

The five most likely failures, with the symptom as it will actually appear.

### A. Connected to the simulator

**Likelihood: high.** The shipped `.mcp.json` runs `--backend auto`, and `connect`'s own
default is `auto`. If Inventor is unreachable you get the mock, which builds the whole
bracket, returns 43.2012, and produces no CAD at all.

**Symptom** — `"backend": "mock"` and `"simulated": true` in the connect reply, and
`"simulated": true` on every result afterwards. `capture_view` and `export_model` return
`"written": false` with a note saying the mock does not write CAD files. In part two the
loop fails outright with `FileNotFoundError: No STL was written to … The mock backend does
not write CAD files -- connect to Inventor to run the DFM loop.`

**Fix** — call `connect(backend="inventor")` explicitly, which raises
`ConnectionFailedError` with a hint instead of falling back.

**Prevention** — start Inventor by hand before anyone is watching, and run the dry run on
the morning of.

### B. The part opens behind the Claude window

**Likelihood: high. It is not a failure and it looks like one.**

**Symptom** — the build reports success and nothing appears to happen.

**Fix** — Alt-Tab, or click Inventor on the taskbar. The model is there and finished.

Do not reach for `activate_part` expecting this. It calls `Document.Activate()`, which
fixes the wrong *document* being in front *inside* Inventor — a leftover `Part1` from an
earlier run — and does not raise the Inventor window.

**Prevention** — Inventor on a second monitor or tiled beside Claude, in front, with every
other document closed. And run `capture_view` right after each build regardless: it
activates the document, fits the camera, and puts the PNG in the chat.

### C. Claude authors a different bracket

**Likelihood: low, but it is the risk peculiar to a text-to-model demonstration.**

**Symptom** — step 2's rehearsal returns something other than `43.201212` /
`[90.0, 50.0, 70.0]`, or emits a warning. The two warnings that matter are *"X, Y drive
nothing"* — a parameter declared and never referenced, so the part is not revisable
through it — and *"sketch 'X' does not reach the part"*.

**Fix** — the rehearsal is free, instant and needs no Inventor. Correct the description
and validate again before building. **Never skip step 2 to save time on stage.** It is the
only thing between a plausible-looking build and a wrong part.

The specific miss to expect is **43.964619**, which is this bracket without its two M8
upright holes. The skill's worked example of this bracket covers the L-section, the slots,
the mirror and the fillet but not the holes, so that is where Claude will drift. Ask for
them again by name.

### D. The `concave` fillet selector matches no edges

**Likelihood: low-moderate on an unverified release.** Convexity is derived by walking the
edge uses on each face, and a different release can expose different members.

**Symptom** — the build stops at `stopped_at: "operation 7 (fillet)"`, or the selector
reports that it matched no edges. Note that "matched no edges" means it could not decide,
not that the edge is absent.

**Fix — carry on.** `rollback_on_error` is off by default and deliberately so: the part is
left standing with the section, body, both slots and both holes, which is a complete
bracket minus one radius. **The parameter demonstration does not depend on the fillet at
all.** Go straight to step 5. The volume without the fillet is
43.1999 − 0.6867 = **42.5132 cm³**, so the number is still checkable, and after the change
it is 51.5132.

**Pre-check**, on the built bracket, before the audience:

```
select_topology(selector={"kind": "edge", "filter": "concave", "min_length": 40, "limit": 1})
  → count: 1
```

`count: 1` means you are fine.

### E. A hole drills air

**Likelihood: low.** It was found and fixed on this very part, which is why it is worth
recognising. The YZ plane's real axis orientation once put the bracket's upright holes off
the part entirely. Sketch axes are now measured via `SketchToModelSpace` rather than
assumed.

**Symptom** — `UprightFixings` reports a volume change of 0 instead of −0.7634, or the
build refuses with "the hole built but removed no material".

**Fix — do not retry the hole.** A hole consumes its sketch, so the feature cannot be
deleted and rebuilt: the retry has no centres left to place itself on. Rebuild the whole
recipe from scratch with `rollback_on_error: true`.

### F. The loop reverts every round

**Likelihood: low. It was the predicted 2026 risk and it did not happen** -- the loop ran
on 2026.1 and reached 85 in one round -- but the mechanism is still worth recognising,
because it is what an unrecognised feature health status would look like on any release
nobody has driven yet.

**Symptom** -- round 1 comes back `reverted` with
`stopped_because: "the change broke the rebuild, so it was undone"`, while the geometry in
Inventor is visibly correct.

**Fix** -- judge the rebuild by the geometry. Call `measure_part`: if the volume and span
moved as they should, the features are fine and the health integers are being
misread. Part two then still works as `check_manufacture` alone -- the score, every
finding, the four proposed changes with their `from`, `to` and `why`, and the
frozen-draft refusal in `would_change.not_acted_on` are all in that one call. What you
lose is the model actually changing. Afterwards, `python scripts\dump_constants.py
--find Health` on that seat names the integer, which is a five-minute contribution back
to this repository.

---

## Inventor 2026

The numbers in this script were measured on **2027.1**. They were then reproduced on
**Inventor 2026.1**, and that run is the reason most of this section is short.

### What the 2026.1 run settled

A full `scripts\live_acceptance.py` run passed every check it compared. The parts that
matter to this demonstration:

- **The bracket.** 43.1999 cm³, to the same five-hundredths of a cubic millimetre as
  2027.1. The `concave` fillet selector found the inside corner, so recovery D does not
  apply here.
- **The parameter edit.** `base_len` 90 to 120, span followed, volume 43.1999 to 52.1999,
  and -- the line that mattered most -- *the rebuild left no feature in error*. That was
  the predicted headline risk: an unrecognised `HealthStatus` integer putting healthy
  features into `rebuild.errors`. It did not happen. 2026.1 reports what the backend
  already knows how to read.
- **The loop.** 64.0 MAJOR REWORK to 85.0 PRODUCTION READY in one round, by the same four
  changes in the same order, clearing `ribs` and correctly refusing the frozen draft. Step
  11's reference numbers are 2026.1 numbers as well as 2027.1 numbers.
- **The freezes.** The M3 pilot hole and the cable entry came out the size they went in,
  and the derived boss diameter followed its wall down.
- **The enum table.** Forty-seven of fifty-one values matched 2026.1's own type library
  exactly.

So steps 1 to 14 have all been executed on this release. What follows is not a list of
worries; it is the residue.

### The four enum names that were never Inventor's, and are now

`kBothShellDirection`, `kHiddenLineRendering`, `kFlatHoleBottom` and `kAngleHoleBottom`
were absent from both releases' type libraries under those names, and three of them were
out of their own numbering family besides -- which is exactly how `kThroughAllExtent` once
came to be Inventor's `kToNextExtent`: an extrude that stopped at the next face while
every report said "through all". They were made to refuse rather than resolve.

On 2026-09-03 Inventor 2027.1 was asked what it *does* call them, and all four turned out
to be wrong **names** rather than wrong values, which is why looking a value up could never
have fixed them. A third shell direction is `kBothSidesShellDirection`, 41219, sitting in
the family exactly where it should. There is no hidden-line render style at all;
`kWireframeWithHiddenEdgesRendering` (8712) is what the words describe. The two hole-bottom
names do not exist under any name and nothing read them, so they are gone from the table
rather than refused: an entry for a name Inventor does not have is a fiction with a number
attached.

They are therefore marked disputed and **refuse rather than guess**. What that costs is
narrow and nothing in this script touches it:

| Name | Reached by | In this demonstration? |
|---|---|---|
| `kBothShellDirection` | a shell with `direction: "both"` | no -- the housing shells inward |
| `kHiddenLineRendering` | `capture_view(display="hidden_line")` | no -- every capture is `iso`, shaded |
| `kFlatHoleBottom` | nothing today | no |
| `kAngleHoleBottom` | nothing today | no |

If somebody asks for one of those during the demonstration, the server declines and names
the reason. That is the intended behaviour and it is a better answer than a shell offset
the wrong way.

To settle them on this seat:

```powershell
python scripts\dump_constants.py --find Shell
python scripts\dump_constants.py --find Render
python scripts\dump_constants.py --find Bottom
```

Each lists every enum in Inventor's own library whose name contains that word, and marks
where the table disagrees. Paste the real names and values into `FALLBACK` in
`inventor_mcp/backend/com/constants.py`, take them off `SUSPECT`, and they stop refusing.

### The one warning the run printed

On `hex_standoff`, which is not part of this script:

```
Sketch Hex: equal_length(line1, line6) was refused (Exception occurred.);
the sketch keeps a degree of freedom.
```

The part still measured correctly -- it is dimensioned enough without that constraint --
but a constraint Inventor refused is a real difference, not noise, and it has not been
reproduced on 2027.1 for comparison. It touches nothing in this demonstration.

### What to still do before the demonstration

Two things, both because the cache is version-specific rather than because 2026 is
suspect.

1. **Regenerate the pywin32 cache if the machine has ever run a different Inventor.**
   A stale `gen_py` produces features created with the wrong behaviour -- a cut that
   joins, a vertical dimension that comes out aligned -- and the signature on this bracket
   is `SlotCut`'s `measured.volume_change_cm3` coming back positive or zero instead of
   -1.461, or a `divergence` block naming that operation.

   ```powershell
   Remove-Item -Recurse "$env:LOCALAPPDATA\Temp\gen_py"
   py -c "import win32com.client; win32com.client.gencache.EnsureDispatch('Inventor.Application')"
   ```

   Then restart the MCP server and reconnect.

2. **Confirm `INVENTOR_MCP_BINDING` is not set to `early`.** The server generates the
   cache for its enum values but deliberately talks late-bound, because early binding
   produced a string of failures on calls that were demonstrably valid.

Read the `version` string out at step 1 regardless. It costs nothing and it means that if
anything does go wrong later, everyone already knows which release it was.

## What was corrected against the repository

Both designs were checked line by line against the files. Everything below was changed
before it went into this script.

**Verified and kept as stated.** The bracket's rehearsal figures — 43.201212 cm³ and
`[90.0, 50.0, 70.0]` at 90, 52.201212 cm³ and `[120.0, 50.0, 70.0]` at 120, an increment
of exactly 9.000000 — were reproduced by running `rehearse()` offline on the shipped
recipe. The per-operation volume changes (Body +46.2000, SlotCut −1.46105, SlotPair
−1.461051, UprightFixings −0.763407, InsideCorner +0.68672) match. The recorded live
volume 43.1999 and its derivation, the 5×10⁻⁴ tolerance and the 0.01 mm span tolerance are
all as quoted. On the DFM side, the ABS limits (wallLo 1.2, wallHi 3.5, draftMin 0.5), the
SPI-B1 texture depth 0.003 and the resulting 0.68° required draft, all eight check weights
and the four severity factors, the grade floor that caps a part with one critical finding
at MINOR REWORK, all four rib and boss thresholds, the four remedy constants (0.45, 2.5,
0.30, 0.60) and the 1.0° draft margin were read from `dfm/src/rules/engine.js`,
`scoring.js`, `core/materials.js`, `core/finishes.js` and `inventor_mcp/dfm/remedy.py` and
confirmed. The frozen-draft refusal string, the boss-follows-its-wall path, the freeze
enforcement in `builder.apply_parameter`, and every tool argument name and output key used
above were checked against the source.

**Corrected.**

1. **The skill's worked example of the bracket does not include the upright holes.** It
   carries eight parameters and six operations; the shipped recipe carries ten and eight.
   The claim that the demonstration sentence "maps one-to-one onto a worked example the
   model has already loaded" is only true of the L-section, the slots, the mirror and the
   fillet. The two M8 holes and `hole_z` must be dictated and must be checked for. The
   no-holes rehearsal figure, **43.964619**, was computed and is now given in step 2 as the
   specific miss to recognise.

2. **`inspect_part` warnings prove nothing about constraint on a live seat.** The claim
   that an absent `warnings` array means every sketch is fully constrained is wrong:
   `FullyConstrained` is not exposed under that name on Inventor, sketches report `null`,
   and the warning only fires on an explicit `False`. Step 4 now says so.

3. **The health-status fallback does not exist.** `_healthy_statuses()` is annotated
   `set[int] | None` but has no path that returns `None`; it always returns
   `{0, 11778} ∪ anything resolvable`. So the backend's `uninterpreted_health` branch and
   the loop's tolerance of "untranslated statuses" are both unreachable in practice, and
   an unrecognised integer goes straight into `rebuild.errors` -- which `_rebuild_unhappy`
   treats as a broken rebuild, so **the improvement loop would revert every round on a
   part that rebuilt correctly.** This was written up as the headline 2026 risk. The
   2026.1 acceptance run then answered it: the parameter edit reported *the rebuild left
   no feature in error*, and the loop ran to 85 in one round. The reasoning stands for the
   next unverified release. The prediction, for this one, was wrong.

4. **`dfm_capabilities` does not check Node.** It calls `find_dfm_root()` only. A missing
   Node surfaces as `DfmUnavailable` when an analysis actually runs, not in the
   capabilities report. `node --version` is now an explicit line in the setup checklist.

5. **`stopped_at` indices are zero-based.** `"operation 7 (fillet)"` is correct for this
   recipe, but by arithmetic rather than by the fillet being the seventh operation — it is
   the eighth.

6. **The housing's rehearsal volume is not a prediction.** 46.229078 cm³ was computed
   offline, and the simulator ignores the draft taper, which this part has on every outer
   wall. The cable cut is no longer part of the discrepancy: it now removes 0.36 cm³, the
   two end walls, where it used to remove 7.2. Step 8 says not to quote a volume for this
   part and gives the reason.

7. **The bracket's upright hole spacing is a literal.** `±15` in the `UprightHoles` sketch
   is not driven by any parameter, so the "30 apart" in the spoken description is not
   revisable. Added to the caveat in step 7 alongside the already-noted `25` setback.

8. **`.dfm` layout.** The loop writes into a per-run subdirectory
   `./.dfm/<PartName>-<timestamp>/round-N.json`, not into `./.dfm` directly;
   `check_manufacture` writes a timestamped file into `./.dfm` itself. The 79 empty
   directories in the repository are consistent with runs that never reached an export.

9. **Argument names.** `select_topology` takes its selector as `selector=`, and
   `improve_for_manufacture`'s `working_copy` has no effect when no `path` is given. Both
   corrected in the call listings.

**Not adopted, and why.** Designer 2's suggested variant — `save_part` then `declare_dfm`
then `improve_for_manufacture(path=…)` to demonstrate versioned files — is sound but adds
a moving part to a fifteen-minute run for a beat the audience will not miss. Designer 1's
`flanged_shaft` fallback is sound but was cut for the same reason: with two demonstrations
already, a third part is time the DFM loop needs.