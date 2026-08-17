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
2. `validate_recipe` -- static check of a recipe. Free, needs no Inventor, catches
   unit mistakes, unclosed profiles and missing references. Use it before building.
3. `build_part_from_recipe` -- creates the part: parameters first, then sketches and features.
4. `inspect_part` / `select_topology` / `measure_part` -- see what actually exists.
5. `set_parameters` -- change a driving dimension and the model updates. This is the
   point of the whole thing: iterate on parameters, not on geometry.
6. `export_model` / `capture_view` -- STEP/STL/IGES out, or a PNG to look at.

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
    {"type":"polygon","center":[0,0],"sides":6,"size":"af","fit":"circumscribed"}
    {"type":"point","position":[20,0]}                          // a hole centre
    {"type":"point_grid","center":[0,0],"columns":3,"rows":2,"x_spacing":30,"y_spacing":20}
    {"type":"ellipse","center":[0,0],"major":40,"minor":20}
    {"type":"bolt_circle","center":[0,0],"diameter":"pcd","count":6}
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
hole        {"op":"hole","sketch":"Holes","diameter":"hole_d","through_all":true,
             "style":"drilled|counterbore|countersink","tap":"M6x1"}
fillet      {"op":"fillet","edges":{"filter":"vertical"},"radius":"corner_r"}
chamfer     {"op":"chamfer","edges":{"filter":"top"},"distance":1}
shell       {"op":"shell","faces":{"kind":"face","filter":"top"},"thickness":"wall"}
patterns    {"op":"rectangular_pattern","features":["Hole1"],"axis1":"x","count1":4,"spacing1":25}
            {"op":"circular_pattern","features":["Hole1"],"axis":"z","count":6}
            {"op":"mirror","features":["Rib"],"plane":"yz"}
work_plane  {"op":"work_plane","name":"Top","kind":"offset","base":"xy","offset":"height"}
material    {"op":"material","material":"Aluminum 6061"}

SELECTORS pick edges and faces without magic indices:
  {"kind":"edge|face", "feature":"Extrusion1", "filter":"...", "near":[x,y,z],
   "within":5, "min_length":10, "limit":4, "ids":["edge12"]}
  filters: all, top, bottom, front, back, left, right, vertical, horizontal,
           circular, linear, planar, cylindrical, largest, smallest
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
"""
