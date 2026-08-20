"""Find out how to decide an edge's convexity on this Inventor, and prove it.

The current test samples an arbitrary point on each face adjacent to the edge
(`Face.PointOnFace`) and asks which side of the edge it falls on.  On a face
with a hole in it -- the bottom of a plate with a pocket -- that point can land
on the far side, and the answer flips.  On the angle bracket six of the eight
slot-opening edges came back convex and the other two concave, for geometry
that is identical by symmetry.

Deciding it from the edge's loop orientation is exact rather than a sample:
a face's boundary runs anticlockwise seen from outside, so the material lies to
the left of the loop, and whether that direction faces into the neighbouring
face's normal is the whole answer.  That needs the orientation, which
`EdgeUse.IsParamReversed` carries -- if this release exposes it, which is what
this script is for.  makepy generates no module for `EdgeUse`, but late binding
does not care about that, so ask the live object.

It builds a plate with a blind pocket, where every edge's answer is known from
its position, and scores each candidate against the truth.

    python scripts/probe_convexity.py
    python scripts/probe_convexity.py --keep-open
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inventor_mcp.backend.base import ResolvedSelector  # noqa: E402
from inventor_mcp.backend.com import backend as com  # noqa: E402
from inventor_mcp.builder import apply_operation  # noqa: E402
from inventor_mcp.schema import Operation, PartRecipe  # noqa: E402
from inventor_mcp.session import Session  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402

#: A 60x40x10 plate with a 20x10x4 pocket in the middle of its underside.
#: Chosen because it has both kinds of edge and because one of its faces -- the
#: underside -- has an inner loop, which is the case that breaks the sampler.
PART = {
    "name": "ConvexityProbe",
    "units": "mm",
    "operations": [
        {"op": "sketch", "name": "Plate", "plane": "xy", "entities": [
            {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]},
        {"op": "extrude", "name": "Body", "sketch": "Plate", "distance": 10,
         "direction": "positive"},
        {"op": "sketch", "name": "Pocket", "plane": "xy", "entities": [
            {"type": "rectangle", "center": [0, 0], "width": 20, "height": 10}]},
        {"op": "extrude", "name": "Cavity", "sketch": "Pocket", "distance": 4,
         "operation": "cut", "direction": "positive"},
    ],
}

#: The pocket's footprint in cm, with a hair of slack for floating point.
POCKET_X, POCKET_Y, FLOOR = 1.0 + 1e-4, 0.5 + 1e-4, 1e-4


def truth(midpoint: tuple[float, float, float]) -> str:
    """What the answer has to be, from where the edge is.

    An edge belongs to the pocket if it sits over the pocket's footprint. Those
    are inside corners -- except the ring where the pocket opens onto the
    underside, which is a 90-degree wedge of material like any other outside
    corner.
    """
    x, y, z = midpoint
    in_pocket = abs(x) <= POCKET_X and abs(y) <= POCKET_Y
    return "concave" if in_pocket and z > FLOOR else "convex"


def attributes(obj, names: list[str]) -> dict[str, str]:
    """Which of *names* this live object actually answers to."""
    found = {}
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception as exc:
            found[name] = f"-- ({type(exc).__name__})"
            continue
        try:
            found[name] = f"ok  {type(value).__name__} = {value!r:.60}"
        except Exception:
            found[name] = f"ok  {type(value).__name__}"
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args(argv)

    session = Session(backend_kind="inventor")
    backend = session.ensure_backend()
    info = backend.connect(visible=True, create=True)
    print(f"Inventor {info.version}")

    recipe = PartRecipe.model_validate(PART)
    doc = backend.new_part(recipe.name, units=recipe.units, angle_units=recipe.angle_units)
    context = session.register(doc, recipe.units, recipe.angle_units)
    for op in TypeAdapter(list[Operation]).validate_python(PART["operations"]):
        apply_operation(session, context, op)
    print("Built a 60x40x10 plate with a 20x10x4 pocket in its underside.\n")

    matches = backend.select(context.doc_id, ResolvedSelector(kind="edge"))
    entries = [(m, backend._topology[m.id]["object"]) for m in matches
               if m.midpoint is not None]
    if not entries:
        print("No edges came back with a position; nothing to score.")
        return 1

    # What the API actually offers, asked of a real edge rather than of makepy.
    print("=" * 72)
    print("What a live Edge answers to")
    print("=" * 72)
    sample = next((raw for info_, raw in entries if info_.geometry == "linear"),
                  entries[0][1])
    for name, result in attributes(sample, ["EdgeUses", "Faces", "Geometry",
                                            "Evaluator", "StartVertex", "TangentiallyConnectedEdges"]).items():
        print(f"  Edge.{name:28} {result}")
    uses = com._edge_uses(sample)
    print(f"\n  Edge.EdgeUses -> {'unavailable' if uses is None else f'{len(uses)} use(s)'}")
    if uses:
        for name, result in attributes(uses[0], ["IsParamReversed", "Face", "EdgeUseLoop",
                                                 "Edge", "Parent", "Next", "Previous"]).items():
            print(f"  EdgeUse.{name:25} {result}")
        faces = sample.Faces
        candidates = [faces.Item(i) for i in range(1, int(faces.Count) + 1)]
        resolved = [com._use_face(use, candidates) for use in uses]
        named = sum(1 for face in resolved if face is not None)
        print(f"\n  {named} of {len(uses)} edge uses resolved to a face by walking "
              "the loop")
        if named == len(uses):
            distinct = len({com._face_key(face) for face in resolved})
            print(f"  and they name {distinct} distinct face(s) "
                  f"{'-- correct' if distinct == 2 else '-- WRONG, they should differ'}")
        loop = getattr(uses[0], "EdgeUseLoop", None)
        if loop is not None:
            for name, result in attributes(loop, ["Face", "IsOuterEdgeLoop", "EdgeUses"]).items():
                print(f"  EdgeUseLoop.{name:21} {result}")

    # And whether either candidate can be trusted.
    print("\n" + "=" * 72)
    print("Every edge, against the answer its position demands")
    print("=" * 72)
    print(f"  {'edge':>8}  {'midpoint (mm)':>26}  {'truth':<8} {'sampled':<9} {'by loop':<9}")
    score = {"sampled": 0, "loop": 0}
    unknown = {"sampled": 0, "loop": 0}
    notes: dict[str, int] = {}
    for info_, raw in sorted(entries, key=lambda entry: entry[0].midpoint):
        expected = truth(info_.midpoint)
        sampled = com._convexity_from_samples(raw)
        loop = com._convexity_from_loops(raw)
        if loop is None:
            uses = com._edge_uses(raw)
            if uses is None:
                reason = "no edge uses on this edge"
            elif com._edge_direction(raw) is None:
                reason = "not a straight edge, so it has no single tangent"
            elif any(com._use_face(use, [raw.Faces.Item(i)
                                        for i in range(1, int(raw.Faces.Count) + 1)])
                     is None for use in uses):
                reason = "no route from an edge use back to its face"
            else:
                reason = "the uses disagreed, or the faces meet smoothly"
            notes[reason] = notes.get(reason, 0) + 1
        for key, answer in (("sampled", sampled), ("loop", loop)):
            if answer == expected:
                score[key] += 1
            elif answer is None:
                unknown[key] += 1
        position = "(" + ", ".join(f"{c * 10:7.2f}" for c in info_.midpoint) + ")"
        flag = "" if sampled == expected else "   <-- sampled is wrong"
        if loop is not None and loop != expected:
            flag += "   <-- loop is wrong"
        print(f"  {info_.id:>8}  {position:>26}  {expected:<8} "
              f"{str(sampled):<9} {str(loop):<9}{flag}")

    total = len(entries)
    print(f"\n  of {total} edges: sampled got {score['sampled']} right "
          f"({unknown['sampled']} unknown), loop got {score['loop']} right "
          f"({unknown['loop']} unknown)")
    for note, count in sorted(notes.items(), key=lambda kv: -kv[1]):
        print(f"    {count:>3} x {note}")

    if not args.keep_open:
        backend.close_document(context.doc_id, save=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
