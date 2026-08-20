# Inventor MCP

An [MCP](https://modelcontextprotocol.io) server that turns a description of a part
into a **parametric** Autodesk Inventor model — one whose dimensions are driven by
named parameters and expressions, so that "make it 20 mm wider" is a parameter edit
rather than a rebuild.

```
"A 120 × 80 × 8 mm aluminium mounting plate, 10 mm corner radii,
 four M6 clearance holes 12 mm in from each edge."
```

becomes a part with six named parameters, two sketches, and an extrude / fillet /
hole tree — with every dimension driven by an expression:

| Parameter | Expression | Drives |
|---|---|---|
| `plate_w` | `120 mm` | sketch width |
| `plate_d` | `80 mm` | sketch height |
| `thk` | `8 mm` | extrude distance |
| `hole_d` | `6.6 mm` | hole diameter |
| `edge_margin` | `12 mm` | — |
| `corner_r` | `10 mm` | fillet radius |

and the hole grid spaced at `plate_w - 2 * edge_margin`, so widening the plate moves
the holes with it.

---

## Install

The server runs on your own machine, next to Inventor. Inventor's automation API is
Windows-only, so **for real geometry this must be a Windows machine with Inventor
installed**; everything else (writing recipes, validating them, the test suite) runs
anywhere.

```powershell
git clone https://github.com/OC-JG/InventorMCP
cd InventorMCP
py -m venv .venv
.venv\Scripts\activate
pip install -e ".[inventor]"     # on macOS/Linux: pip install -e .
```

Check it starts before wiring it into anything:

```powershell
python -m inventor_mcp --backend mock --help
pytest                            # 350 tests, no Inventor needed
```

Python 3.10+.

## Add it to Claude

### Claude Code

From inside the cloned repo, with the virtualenv active:

```bash
claude mcp add inventor -- python -m inventor_mcp --backend auto
```

The repo also ships a project-scoped [`.mcp.json`](.mcp.json), so if you open this
directory with Claude Code it will offer to enable the server for you. Check it with:

```bash
claude mcp list
```

### Claude Desktop

Edit the config file — create it if it does not exist:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "inventor": {
      "command": "C:\\path\\to\\InventorMCP\\.venv\\Scripts\\python.exe",
      "args": ["-m", "inventor_mcp", "--backend", "auto"]
    }
  }
}
```

Restart Claude Desktop, then look for the tools icon in the message box.

> **Use the absolute path to the `python.exe` inside your virtualenv.** Claude
> Desktop launches the server without your shell's `PATH`, so a bare `"python"` is
> the single most common reason a server shows as failed. On Windows, note the
> doubled backslashes — JSON requires them.

### Any other MCP client

The server speaks stdio by default:

```bash
inventor-mcp --backend auto            # stdio
inventor-mcp --transport streamable-http
```

`--backend` takes `auto` (Inventor when available, otherwise the simulator),
`inventor` (fail loudly if Inventor is unreachable) or `mock` (always simulate).
Every flag has an environment variable too: `INVENTOR_MCP_BACKEND`,
`INVENTOR_MCP_TRANSPORT`, `INVENTOR_MCP_LOG_LEVEL`.

### Check it worked

Ask Claude to call `connect`. A healthy live connection replies:

```json
{"backend": "inventor", "simulated": false, "connected": true, "version": "2025"}
```

If it says `"backend": "mock"`, Inventor was not reachable and you are talking to the
simulator — useful, but it will not produce a real part. Run with
`--backend inventor` to see why it failed rather than falling back silently.

Then try: *"Model a 120 x 80 x 8 mm aluminium mounting plate with 10 mm corner radii
and four M6 clearance holes 12 mm in from each edge."*

---

## How it works

The server does not ask a language model to write Inventor API calls. It asks for a
**part recipe** — a JSON document describing parameters and operations — and replays
that against Inventor itself.

```
description  ──►  recipe (JSON)  ──►  validate  ──►  build  ──►  .ipt / STEP / STL
                       ▲                  │
                       └── fix findings ◄─┘
```

That indirection is what buys the useful properties:

- **It is checkable.** `validate_recipe` catches undefined parameters, unit errors,
  open profiles and dangling references *before* Inventor is involved, and needs no
  Inventor at all.
- **It is parametric.** Every size travels as an expression string that Inventor
  stores on the dimension, not as a number baked into geometry.
- **It is re-runnable.** The same recipe rebuilds the part, and a recipe is small
  enough to keep in version control next to the code that generated it.

### Recipe example

```json
{
  "name": "MountingPlate",
  "units": "mm",
  "parameters": [
    {"name": "plate_w", "value": 120},
    {"name": "plate_d", "value": 80},
    {"name": "thk", "value": 8},
    {"name": "hole_d", "value": 6.6, "comment": "M6 clearance"},
    {"name": "edge_margin", "value": 12},
    {"name": "corner_r", "value": 10}
  ],
  "operations": [
    {"op": "sketch", "name": "Body", "plane": "xy", "entities": [
      {"type": "rectangle", "center": [0, 0], "width": "plate_w", "height": "plate_d"}]},
    {"op": "extrude", "name": "Plate", "sketch": "Body", "distance": "thk"},
    {"op": "fillet", "edges": {"filter": "vertical"}, "radius": "corner_r"},
    {"op": "sketch", "name": "Holes", "plane": "xy", "entities": [
      {"type": "point_grid", "center": [0, 0], "columns": 2, "rows": 2,
       "x_spacing": "plate_w - 2 * edge_margin",
       "y_spacing": "plate_d - 2 * edge_margin"}]},
    {"op": "hole", "sketch": "Holes", "diameter": "hole_d", "through_all": true}
  ]
}
```

More in [`examples/`](examples/): a flanged shaft, a hex standoff, a shelled
enclosure, an angle bracket and a counterbored cover plate. Each one is
exercised by the test suite, so they cannot drift out of date.

### Sketches come out constrained

A `rectangle` is not four loose lines. It expands to four lines, six coincident
constraints, two horizontals, two verticals, a construction diagonal whose midpoint
pins the centre, and two driving dimensions carrying your expressions. A `polygon`
gets a construction circle, vertices coincident with it and equal-length edges —
leaving exactly one degree of freedom, its rotation. A `slot` gets four tangencies
and equal end radii.

This matters: an under-constrained sketch moves unpredictably when a parameter
changes, which is precisely the thing a parametric model exists to avoid. Sketches
are reported with a `fully_constrained` flag and `inspect_part` warns about the ones
that are not.

### Selecting edges and faces

Fillets and chamfers need to name topology, and topology has no stable names. Rather
than guessing indices, selectors describe intent:

```json
{"kind": "edge", "feature": "Extrusion1", "filter": "vertical", "limit": 4}
{"kind": "face", "filter": "top"}
{"kind": "edge", "filter": "circular", "near": [0, 0, 25], "within": 5}
```

`select_topology` previews what a selector matches — handles, sizes, positions —
before it is committed to a feature. Handles are valid only until the next rebuild,
and the server says so on every response that contains them.

---

## Working without Inventor

The server ships a second backend that simulates a session in memory. It is not a
geometry kernel — bodies are a bounding box plus a volume estimate, and topology is
synthesised from the sketch loops that produced it — but it is enough to:

- author and validate a recipe end to end,
- check that profiles close and that a selector will match the four edges you meant,
- see approximate mass, volume and bounding box,
- run the whole test suite on any OS.

Everything it returns is flagged `"simulated": true`, exports report
`"written": false`, and a parameter change reports that geometry was *not* re-solved.
It will not quietly pretend to be Inventor.

```bash
inventor-mcp --backend mock
```

---

## Tools

| Tool | What it is for |
|---|---|
| `connect` | Attach to Inventor, or to the simulator |
| `session_status` | Backend, open documents, active part and what it contains |
| `new_part` / `open_part` / `save_part` / `close_part` / `activate_part` | Document lifecycle |
| `part_recipe_schema` | Full JSON Schema plus the quick reference |
| `validate_recipe` | Static checks; no Inventor needed |
| `build_part_from_recipe` | The main text-to-model entry point |
| `apply_operations` | Append operations to an open part |
| `set_parameters` | Change driving dimensions and rebuild |
| `edit_feature` | Suppress, unsuppress, rename, delete |
| `rebuild_part` | Force a rebuild and report failures |
| `inspect_part` | Parameters, sketches, feature tree, size |
| `select_topology` | Preview what a selector matches |
| `measure_part` | Bounding box, volume, area, mass, centre of mass |
| `export_model` | STEP, STL, IGES, SAT, DWG, DXF, OBJ, 3MF |
| `capture_view` | Render a PNG |

Two prompts are published as well: `model_this_part` and `revise_part`.

Errors come back as data, not exceptions:

```json
{
  "ok": false,
  "error": "expression_error",
  "message": "Unknown parameter 'thicknes'.",
  "hint": "Declare it with `set_parameter` first. Known parameters: plate_w, plate_d, thk."
}
```

---

## Expressions

Values may be numbers (in the recipe's units) or expressions:

```
"plate_w / 2"        "1.5 in"        "wall * 2 + 3 mm"      "sqrt(2) * bolt_pcd / 2"
"sin(30 deg) * r"    "max(t, 3 mm)"  "flange_d - 16"
```

The evaluator tracks dimensions, so `10 mm * 10 mm` is an area, `50 mm / 10 mm` is a
count, and `10 mm + 5 deg` is rejected with an explanation. A bare number added to a
dimensioned one takes the document's units — `flange_d - 16` means 16 mm in a
millimetre part — which is how Inventor reads it too.

Expressions are parsed from a restricted AST: no attribute access, no calls other
than a maths whitelist, no comprehensions, and a length cap.

Units: `mm cm m micron in ft mil` for length, `deg rad grad` for angle, `ul` for
unitless counts. Inventor's own database units (cm, radians) never leak into the
tool surface.

---

## Layout

```
inventor_mcp/
  schema.py        recipe models (pydantic) - the contract with the caller
  expressions.py   restricted-AST evaluator with dimensional algebra
  units.py         display units <-> Inventor database units
  resolve.py       a recipe value -> (expression string, evaluated number)
  geometry.py      entities -> primitives + constraints + driving dimensions
  plan.py          the backend-neutral sketch IR
  builder.py       replays a recipe against a backend; static checks
  session.py       open documents and what the server remembers about them
  backend/
    base.py        the contract both backends satisfy
    com/           live Inventor over COM (Windows)
    mock/          in-memory simulator
  tools/           the MCP tool surface
  server.py        assembly: tools, resources, prompts, CLI
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for why it is split that way, and
[docs/INVENTOR_SETUP.md](docs/INVENTOR_SETUP.md) for the Windows/COM specifics.

## Development

```bash
pip install -e ".[dev]"
pytest                      # 350 tests, no Inventor required
```

### Status

The recipe layer, expression evaluator, geometry expansion, selectors, tool surface
and simulator are covered by the test suite and run on any platform.

**All five examples build end to end against Inventor 2027.1**, and every volume
matches a hand calculation to five significant figures or better. Between them
they cover parameters with expressions, constrained sketches on all three origin
planes and on an offset work plane, extrudes and cuts, revolved-free profiles
from polylines and slots and bolt circles, blind and through holes, fillets and
chamfers chosen by selector, mirroring, shells, mass properties, and export to
STEP, STL and PNG.

Not yet exercised live: revolve, sweep, loft, patterns, threads and the
counterbore, countersink and tapped hole styles. Recipes now reach all of them —
`belt_pulley`, `pipe_bend`, `duct_transition`, `threaded_boss` and
`cover_plate` — so what is missing is a run, not a recipe. See
[docs/INVENTOR_SETUP.md](docs/INVENTOR_SETUP.md) for what is confirmed, what is
not, and the Inventor API quirks that cost the most time getting there.

## Inventor versions

Driven against **Inventor 2027.1** on Windows with Python 3.14 and pywin32, and
nothing else. Every quirk recorded in
[docs/INVENTOR_SETUP.md](docs/INVENTOR_SETUP.md) was measured there.

Earlier versions are likely to need different enum values, and the COM backend
reads them from Inventor's own type library first, so a machine with a working
pywin32 cache should be fine. The fallback table is *not* verified — where its
value is disputed the server now refuses rather than guessing, and
`scripts/dump_constants.py` prints what Inventor actually says. Reports from
2022–2026 are welcome and are the fastest way to widen this.

## Licence

MIT — see [LICENSE](LICENSE).
