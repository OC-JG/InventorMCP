"""Reference text served to the model: how to write a part recipe.

This is the highest-leverage prose in the server, so it is kept dense and
example-first.  The full JSON Schema is available from `part_recipe_schema`
when a detail is missing here.
"""

from __future__ import annotations

SERVER_INSTRUCTIONS = """\
Turns a description of a part into a real parametric Autodesk Inventor model.

Workflow:
1. `connect` -- picks a live Inventor session, or a simulator if Inventor is absent.
2. `validate_recipe` -- checks the recipe AND rehearses it in the simulator, reporting
   what each operation would do to the part. Free, needs no Inventor. Read its
   `warnings`: a cut whose profile misses the part, or a parameter that drives no
   geometry, is a valid recipe that builds the wrong thing. Always run it first.
3. `build_part_from_recipe` -- creates the part: parameters first, then sketches and features.
4. `inspect_part` / `select_topology` / `measure_part` -- see what actually exists.
5. `set_parameters` -- change a driving dimension and the model updates. This is the
   point of the whole thing: iterate on parameters, not on geometry.
6. `export_model` / `capture_view` -- STEP/STL/IGES out, or a PNG to look at.
7. `check_manufacture` / `improve_for_manufacture` -- for a moulded part, measure how
   manufacturable it is by injection moulding and improve it in a closed loop: change
   a parameter, rebuild, measure again. `dfm_capabilities` says whether the analyser
   is available and which formats it takes.

   Both take a `path`, so the part need not have been built here. An .ipt is worked
   on as the next version of the file, leaving the original alone. A STEP file is
   imported and measured but cannot be improved -- translated geometry has no
   parameters to drive. An .stl is analysed with no Inventor at all.

   For a part nobody described, `discover_dfm_roles` works out which parameter is the
   wall from the part's own features and says what it read that from; `declare_dfm`
   corrects it and remembers it in the part. Anything it lists under `suggestions`
   was matched by name alone and has NOT been used.

   Freeze the dimensions the design depends on FIRST -- a sealing face, a bearing
   bore, a pilot hole for a self-tapping screw -- with `frozen: true` on the
   parameter, `declare_dfm` or `protect_geometry`. The loop is a machine for changing
   dimensions until a number stops rising, and every one of those is a legitimate way
   to raise a DFM score and a broken part.

Working from a 2D drawing? Read it into a `reading` first (`drawing_reading_schema`),
write a recipe whose parameters are its dimensions, then `check_against_drawing`.
A drawing is a specification, not a picture: tracing its outlines gives geometry
with no parameters, and reading its dimensions gives you the model.

Model the part the way a mechanical engineer would: name the driving dimensions as
parameters, then write every size as an expression of them. A model whose numbers are
hard-coded is not parametric and cannot be revised.
"""

RECIPE_CHEATSHEET = """\
A recipe is JSON: {name, units, parameters[], operations[]}.

`units` ("mm" default, also cm/m/in/ft) is what bare numbers mean. A value may
always be a string expression instead: "plate_w / 2", "1.5 in", "wall * 2 + 3 mm".
Coordinates take expressions too -- {"center": [0, "box_h - lid_t"]} -- so moving a
parameter moves the geometry with it. Counts (sides, rows, count) are plain integers.

PARAMETERS -- declare the driving dimensions first, then reference them everywhere:
  {"name": "plate_w", "value": 120, "comment": "overall width"}
  {"name": "hole_d",  "value": "M6_clear", "unit": "mm"}
  {"name": "n_holes", "value": 4, "unit": "ul"}          // ul = unitless count
  {"name": "draft",   "value": 3, "unit": "deg"}

OPERATIONS -- executed in order; each has an optional "name" used to refer back.

sketch      {"op":"sketch","name":"Base","plane":"xy","offset":null,"entities":[...]}
            plane: "xy" | "xz" | "yz" | a work-plane name | "face:<handle>" from select_topology
  entities (each may set "name", "construction", "dimension", "locate"):
    {"type":"rectangle","center":[0,0],"width":"plate_w","height":"plate_h"}
    {"type":"rectangle","corner":[0,0],"width":40,"height":20}
    {"type":"circle","center":[0,0],"diameter":"bore_d"}       // or "radius"
    {"type":"line","start":[0,0],"end":[50,0],"name":"axis","construction":true}
    {"type":"polyline","points":[[0,0],[40,0],[40,10],[0,25]],"closed":true}
    {"type":"arc","center":[0,0],"radius":12,"start_angle":0,"end_angle":180}
    {"type":"slot","center":[0,0],"length":30,"width":8,"angle":0}
       // `length` is CENTRE-TO-CENTRE, so that slot is 38 long overall.
       // For an overall length L, give length = L - width.
    {"type":"polygon","center":[0,0],"sides":6,"size":"af","fit":"circumscribed"}
    {"type":"point","position":[20,0]}                          // a hole centre
    {"type":"point_grid","center":[0,0],"columns":3,"rows":2,"x_spacing":30,"y_spacing":20}
    {"type":"ellipse","center":[0,0],"major":40,"minor":20}
    {"type":"bolt_circle","center":[0,0],"diameter":"pcd","count":6}
    {"type":"text","text":"OnlyCat","position":[0,0],"height":8,"font":"Arial",
     "bold":false,"align":"center","rotation":0}      // feed this to `emboss`
       // `position` is the TOP of the text, not the baseline -- it hangs below by
       // about 1.3 x height. Text must fit inside the face or the emboss is refused.
  Entities are auto-constrained and driven by dimensions built from your
  expressions; "locate":"none" leaves an entity floating if you want to
  constrain it yourself with the optional "constraints" and "dimensions" lists.

extrude     {"op":"extrude","sketch":"Base","distance":"thickness",
             "operation":"join|cut|intersect|new_body","direction":"positive|negative|symmetric",
             "extent":"distance|through_all|to_next","taper":"3 deg"}
revolve     {"op":"revolve","sketch":"Profile","axis":"axis","angle":"180 deg"}
             axis: "x"|"y"|"z" or the name of a sketch line
sweep       {"op":"sweep","profile_sketch":"P","path_sketch":"Path"}
loft        {"op":"loft","sketches":["S1","S2"]}
coil        {"op":"coil","sketch":"Wire","axis":"z","pitch":10,"revolutions":8}
             A helix: springs, a moulded thread, a drill's flutes. Sketch the
             section in a plane containing the axis, offset from it by the coil
             radius. Give exactly two of `pitch`, `height` and `revolutions` --
             Inventor has a separate call per pair and the third is derived.
             `spiral:true` gives a flat spiral instead. `operation:"cut"` cuts
             the helix out, which is how a thread form is made.
hole        {"op":"hole","sketch":"Holes","diameter":"hole_d","through_all":true,
             "style":"drilled|counterbore|spotface|countersink","tap":"M6x1"}
             counterbore/spotface need cbore_diameter and cbore_depth;
             countersink needs csink_diameter (csink_angle is the included angle).
             A through hole drills whichever way finds material, so `direction`
             only matters for a blind hole (one with a `depth`). A blind hole
             gets a flat bottom unless you give `bottom_angle`.
             With `tap`, Inventor takes the drill size from its own thread
             table, so give `diameter` as the tapping drill.
fillet      {"op":"fillet","edges":{"filter":"vertical"},"radius":"corner_r"}
chamfer     {"op":"chamfer","edges":{"filter":"top"},"distance":1}
emboss      {"op":"emboss","sketch":"Name","depth":0.5,"style":"engrave|raise"}
             Marks a face with text or a closed profile. Put the sketch on the face
             being marked -- depth is measured from the sketch plane. `engrave` cuts
             in, `raise` stands proud. The profile must fit INSIDE that face: text
             overrunning the edge is refused with no explanation. No draft angle --
             use a tapered extrude cut if drafted lettering is needed.
shell       {"op":"shell","faces":{"kind":"face","filter":"top"},"thickness":"wall"}
draft       {"op":"draft","faces":{"filter":"vertical"},"plane":"xy","angle":"2 deg"}
             Tapers faces about their edge on a parting plane, so a moulded part can
             leave the tool. Unlike an extrude's `taper` this works on faces that
             already exist, which is what you want on walls built before the tooling
             was thought about.
boss        {"op":"boss","positions":[[20,0],[-20,0]],"plane":"Top","diameter":6,
             "height":10,"hole_diameter":2.5,"tap":"M3x0.5"}
             A mounting post with a pilot down it. Inventor's own Boss cannot be made
             through the API, so this expands into a sketch, a join extrude and a
             hole -- it appears in the browser as those, not as a Boss. The pilot
             defaults to 4/5 of the height so it does not break through.
rib         {"op":"rib","plane":"xz","start":[-30,20],"end":[30,20],"root":6,
             "thickness":2}
             A web standing on the part. Inventor's Rib feature refuses every
             definition the API can build, so this is the silhouette -- the top edge
             from `start` to `end`, dropped to `root` -- thickened symmetrically
             about the plane. Not a Rib in the browser, same geometry. Keep it under
             about 0.6 of the wall it meets or the wall will sink opposite it. No
             draft: a rib should thin as it rises, which a silhouette plus a linear
             extrude cannot do -- narrow the silhouette instead.
combine     {"op":"combine","base":1,"tools":[2],"operation":"join|cut|intersect"}
             Booleans one body into another. Needs a second body, which means an
             earlier extrude with "operation":"new_body". Bodies are 1-based in
             creation order.
split       {"op":"split","tool":"Mid","style":"trim|split|faces","remove_positive":true}
             Cuts the part with a plane. `trim` throws a side away -- this is how a
             lid comes off a tray. `split` keeps both halves as bodies. `faces` only
             divides the faces the plane crosses.
patterns    {"op":"rectangular_pattern","features":["Hole1"],"axis1":"x","count1":4,"spacing1":25}
            {"op":"circular_pattern","features":["Hole1"],"axis":"z","count":6}
            {"op":"mirror","features":["Rib"],"plane":"yz"}
work_plane  {"op":"work_plane","name":"Top","kind":"offset","base":"xy","offset":"height"}
material    {"op":"material","material":"Aluminum 6061"}

SELECTORS pick edges and faces without magic indices:
  {"kind":"edge|face", "feature":"Extrusion1", "filter":"...", "near":[x,y,z],
   "within":5, "min_length":10, "limit":4, "ids":["edge12"]}
  filters: all, top, bottom, front, back, left, right, vertical, horizontal,
           circular, linear, planar, cylindrical, largest, smallest,
           concave (an inside corner), convex (an outside one)
  `concave` matches EVERY inside edge, which after a cut includes the walls of
  every slot and pocket, not just the corner you meant. Round before you cut, or
  narrow the selector -- and run `select_topology` first to see the real count.
  Prefer `concave` over guessing a `near` point for "round the inside corner":
  it does not depend on which way a sketch plane happens to face.
  Run `select_topology` with a selector first to see exactly what it matches.

WORKED EXAMPLE -- a bolted mounting plate:
{
  "name": "MountingPlate", "units": "mm",
  "parameters": [
    {"name": "plate_w", "value": 120}, {"name": "plate_d", "value": 80},
    {"name": "thk", "value": 8}, {"name": "hole_d", "value": 6.6},
    {"name": "edge_margin", "value": 12}, {"name": "corner_r", "value": 10}
  ],
  "operations": [
    {"op":"sketch","name":"Body","plane":"xy","entities":[
      {"type":"rectangle","center":[0,0],"width":"plate_w","height":"plate_d"}]},
    {"op":"extrude","name":"Plate","sketch":"Body","distance":"thk"},
    {"op":"fillet","edges":{"filter":"vertical"},"radius":"corner_r"},
    {"op":"sketch","name":"Holes","plane":"xy","entities":[
      {"type":"point_grid","center":[0,0],"columns":2,"rows":2,
       "x_spacing":"plate_w - 2 * edge_margin","y_spacing":"plate_d - 2 * edge_margin"}]},
    {"op":"hole","sketch":"Holes","diameter":"hole_d","through_all":true}
  ]
}
"""

RECIPE_POINTER = (
    "The recipe format -- parameters, every sketch entity, every operation and a "
    "worked example -- is written out once, on `build_part_from_recipe`, and is "
    "also the `inventor://recipe/guide` resource and what `part_recipe_schema` "
    "returns. It is not repeated here."
)
"""The sentence the other recipe-taking tools carry instead of the cheat-sheet.

The cheat-sheet used to be embedded in three tool descriptions, 98% identical,
which cost about 4,600 tokens of every request to a client with this server
enabled before the caller had said a word. A client sees every tool description
at once, so one copy in the list is as visible as three; the copy that stays is
on the tool a caller is looking for when it wants to make a part. This pointer
is what the other two say so a reader of either knows where to look.
"""

MODELLING_NOTES = """\
Practical notes that save a rebuild:

* Put the sketch that defines the overall shape on "xy" and extrude in +Z. Every
  selector filter ("top", "vertical", ...) is expressed in model space, so a
  consistent orientation keeps them meaning what they say.
* Cut before you round. Fillets and chamfers consume edges; adding them early
  means later cuts land on curved faces and selectors stop matching.
* A hole belongs in its own sketch of points. Do not draw circles and cut them
  unless you specifically want a plain bore with no hole feature behind it.
* Reuse parameters in expressions rather than repeating numbers: a plate whose
  hole spacing is "plate_w - 2 * edge_margin" survives a change of width.
* `validate_recipe` costs nothing and catches most mistakes. Run it first.
* Handles from `select_topology` are only valid until the model next rebuilds.
* A failed build leaves the part where it stopped, because that is usually what
  explains the failure. Pass `rollback_on_error` when the part matters more than
  the diagnosis -- and to retry a hole, which consumes its sketch and cannot be
  retried any other way.
* Assemblies, drawings and sheet metal are genuinely not supported. If a request
  needs one, say so rather than approximating it as a part. There is an escape
  hatch for reaching Inventor's API directly, but it is off unless the machine's
  owner has set INVENTOR_MCP_ESCAPE_HATCH=on; if `run_inventor_script` is not in
  your tool list, it is off, and telling the user how to turn it on is more use
  than a workaround.
"""
