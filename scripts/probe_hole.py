"""Find out which way Inventor drills a sketch-placed hole, and prove it.

The angle bracket's upright holes build without complaint and remove nothing,
in a case where the geometry is not in doubt: the sketch is on the YZ origin
plane at x=0, its axes measure as u->+Y v->+Z so the centres land at
(0, +-15, 55), and the part occupies x 0..90 with a face right there at x=0.
Five hypotheses about why have all been wrong, so this stops guessing and tests
the whole matrix.

It also answers two questions the failure raised on its own. A hole feature
consumes its sketch, so deleting the feature to retry the other way leaves
nothing to retry with -- which is why the second attempt errored and why there
is no sketch in the tree afterwards. If a feature's direction can be flipped in
place instead, the retry never has to delete anything.

    python scripts/probe_hole.py
    python scripts/probe_hole.py --keep-open
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inventor_mcp.backend.com import backend as com  # noqa: E402
from inventor_mcp.builder import apply_operation  # noqa: E402
from inventor_mcp.plan import PPoint, SketchPlan  # noqa: E402
from inventor_mcp.schema import Operation, PartRecipe  # noqa: E402
from inventor_mcp.session import Session  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402

#: A block occupying x 0..20, y -25..25, z 0..70 -- the bracket's upright,
#: without the base, so a sketch on the YZ origin plane sits flat on its face.
BLOCK = {
    "name": "HoleProbe",
    "units": "mm",
    "operations": [
        {"op": "sketch", "name": "Face", "plane": "xy", "entities": [
            {"type": "rectangle", "corner": [0, -25], "width": 20, "height": 50}]},
        {"op": "extrude", "name": "Block", "sketch": "Face", "distance": 70,
         "direction": "positive"},
    ],
}

#: Each trial gets its own height so a hole that works cannot mask the next one.
TRIALS = [
    ("blind 6 mm, positive", "positive", False, 5.5),
    ("blind 6 mm, negative", "negative", False, 4.5),
    ("through all, positive", "positive", True, 3.5),
    ("through all, negative", "negative", True, 2.5),
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args(argv)

    session = Session(backend_kind="inventor")
    backend = session.ensure_backend()
    info = backend.connect(visible=True, create=True)
    print(f"Inventor {info.version}\n")

    recipe = PartRecipe.model_validate(BLOCK)
    doc = backend.new_part(recipe.name, units=recipe.units, angle_units=recipe.angle_units)
    context = session.register(doc, recipe.units, recipe.angle_units)
    for op in TypeAdapter(list[Operation]).validate_python(BLOCK["operations"]):
        apply_operation(session, context, op)

    document = backend._doc(context.doc_id)
    app = backend._require_app()
    transient = app.TransientGeometry
    print("Built a block spanning x 0..20, y -25..25, z 0..70 mm.")
    print(f"Volume: {com._solid_volume(document):.4f} cm^3   "
          "(a 9 mm hole 6 mm deep should remove 0.3817 cm^3)\n")

    print("=" * 74)
    print("Where a point sketched on the YZ origin plane actually goes")
    print("=" * 74)
    plan = SketchPlan(name="Probe0", plane="yz")
    plan.add(PPoint(id="p", position=(-1.5, 5.5), hole_center=True))
    backend.build_sketch(context.doc_id, plan)
    sketch = backend._sketch(context.doc_id, "Probe0")
    axes = com._sketch_axes(sketch, transient)
    normal = com._cross(*axes) if axes else None
    print(f"  sketch axes            u->{axes[0]} v->{axes[1]}" if axes
          else "  sketch axes            unmeasurable")
    print(f"  sketch normal          {normal}")
    print(f"  Inventor's point count {int(sketch.SketchPoints.Count)}  "
          f"(the plan asked for 1)")
    for index in range(1, int(sketch.SketchPoints.Count) + 1):
        point = sketch.SketchPoints.Item(index)
        flag = bool(getattr(point, "HoleCenter", False))
        try:
            two = point.Geometry
            three = sketch.SketchToModelSpace(two)
            where = (f"sketch ({two.X * 10:.1f}, {two.Y * 10:.1f}) -> model "
                     f"({three.X * 10:.1f}, {three.Y * 10:.1f}, {three.Z * 10:.1f}) mm")
        except Exception as exc:
            where = f"could not read it ({type(exc).__name__}: {exc})"
        print(f"    point {index}: hole_centre={flag!s:5} {where}")

    print("\n" + "=" * 74)
    print("Which way it drills")
    print("=" * 74)
    features = document.ComponentDefinition.Features.HoleFeatures
    worked: list[str] = []
    for label, direction, through, height in TRIALS:
        plan = SketchPlan(name=f"S_{direction}_{int(through)}", plane="yz")
        plan.add(PPoint(id="p", position=(-1.5, height), hole_center=True))
        backend.build_sketch(context.doc_id, plan)
        sketch = backend._sketch(context.doc_id, plan.name)

        centers = app.TransientObjects.CreateObjectCollection()
        for index in range(1, int(sketch.SketchPoints.Count) + 1):
            point = sketch.SketchPoints.Item(index)
            if bool(getattr(point, "HoleCenter", False)):
                centers.Add(point)

        before = com._solid_volume(document)
        enum = backend._k(
            "kPositiveExtentDirection" if direction == "positive"
            else "kNegativeExtentDirection")
        try:
            placement = features.CreateSketchPlacementDefinition(centers)
            if through:
                feature = features.AddDrilledByThroughAllExtent(placement, "9 mm", enum)
            else:
                feature = features.AddDrilledByDistanceExtent(
                    placement, "9 mm", "6 mm", enum)
        except Exception as exc:
            print(f"  {label:24} refused: {com._com_message(exc)}")
            continue
        com._recompute(document)
        after = com._solid_volume(document)
        moved = None if (before is None or after is None) else after - before
        verdict = ("no change" if moved is not None and abs(moved) < 1e-6
                   else f"{moved:+.4f} cm^3")
        print(f"  {label:24} {verdict}")
        if moved is not None and moved < -1e-6:
            worked.append(label)
            # Leave the one that works; undo the rest so later trials start clean.
            continue
        com._delete_quietly(feature)
        still = backend._sketches.get(context.doc_id, {})
        try:
            alive = int(sketch.SketchPoints.Count) >= 0
        except Exception:
            alive = False
        print(f"    after deleting the feature, its sketch is "
              f"{'still usable' if alive else 'GONE -- a retry has nothing to use'}")
        del still

    print("\n" + "=" * 74)
    print("Can a hole's direction be changed without deleting it")
    print("=" * 74)
    plan = SketchPlan(name="Flip", plane="yz")
    plan.add(PPoint(id="p", position=(-1.5, 1.5), hole_center=True))
    backend.build_sketch(context.doc_id, plan)
    sketch = backend._sketch(context.doc_id, "Flip")
    centers = app.TransientObjects.CreateObjectCollection()
    centers.Add(sketch.SketchPoints.Item(1))
    before = com._solid_volume(document)
    try:
        placement = features.CreateSketchPlacementDefinition(centers)
        feature = features.AddDrilledByDistanceExtent(
            placement, "9 mm", "6 mm", backend._k("kPositiveExtentDirection"))
    except Exception as exc:
        print(f"  could not create one to try: {com._com_message(exc)}")
        feature = None
    if feature is not None:
        for name, route in (("HoleFeature.ExtentDirection", lambda: feature),
                            ("HoleFeature.Definition.ExtentDirection",
                             lambda: feature.Definition)):
            try:
                target = route()
                current = target.ExtentDirection
                target.ExtentDirection = backend._k("kNegativeExtentDirection")
                com._recompute(document)
                after = com._solid_volume(document)
                moved = None if (before is None or after is None) else after - before
                print(f"  {name:42} settable, was {current}, "
                      f"volume now {moved:+.4f} cm^3"
                      if moved is not None else f"  {name:42} settable")
            except Exception as exc:
                print(f"  {name:42} -- ({type(exc).__name__})")

    print(f"\n  what removed material: {', '.join(worked) or 'nothing did'}")
    if not args.keep_open:
        backend.close_document(context.doc_id, save=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
