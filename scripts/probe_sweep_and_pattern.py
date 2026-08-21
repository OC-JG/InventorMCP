"""The last two operations that fail with nothing to read.

A sweep cannot make a path out of its sketch, and a rectangular pattern raises
"Exception occurred" with an empty error manager. Both have argument orders that
match the generated signatures, so the arguments are not obviously the problem
and reasoning has run out. This tries each one several ways in one run and prints
which succeed:

    python scripts/probe_sweep_and_pattern.py
    python scripts/probe_sweep_and_pattern.py --only pattern

Every attempt is undone before the next, so each is measured against the same
part rather than against the wreckage of the one before.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inventor_mcp.builder import apply_operation  # noqa: E402
from inventor_mcp.schema import ExtrudeOp, SketchOp  # noqa: E402
from inventor_mcp.session import Session  # noqa: E402


def raw(backend):
    """The backend itself, behind the marshalling proxy."""
    return getattr(backend, "unmarshalled", backend)


def on_thread(backend, work):
    """Run *work* on the apartment that owns Inventor's objects."""
    worker = getattr(backend, "marshalling_thread", None)
    return worker.call(work) if worker is not None else work()


def report(attempts: list[tuple[str, bool, str]]) -> None:
    for label, ok, detail in attempts:
        print(f"  {'ok     ' if ok else 'refused'} {label}")
        if detail:
            print(f"            {detail}")


def census(sketch) -> list[str]:
    """What is actually in a sketch, collection by collection.

    Written because `SketchEntities.Item(1)` is not reliably a curve -- it
    includes points, and this project projects the origin into a sketch whenever
    a constraint references it. A path sketch of "one arc" may hold three things.
    """
    lines = []
    for name in ("SketchEntities", "SketchArcs", "SketchLines", "SketchCircles",
                 "SketchPoints", "SketchSplines"):
        collection = getattr(sketch, name, None)
        if collection is None:
            continue
        try:
            total = int(collection.Count)
        except Exception:
            continue
        if total:
            lines.append(f"{name}={total}")
    entities = getattr(sketch, "SketchEntities", None)
    if entities is not None:
        for index in range(1, min(int(entities.Count), 6) + 1):
            item = entities.Item(index)
            lines.append(f"  entity {index}: {type(item).__name__}"
                         + ("  (construction)" if getattr(item, "Construction", False) else ""))
    return lines


# ---------------------------------------------------------------------------
# The pattern
# ---------------------------------------------------------------------------


def probe_pattern(session, backend) -> None:
    print("\n=== rectangular_pattern")
    document = backend.new_part("PatternProbe", units="mm")
    context = session.register(document, "mm", "deg")
    apply_operation(session, context, SketchOp(
        name="Base", plane="xy",
        entities=[{"type": "rectangle", "center": [0, 0], "width": 120, "height": 40}]))
    apply_operation(session, context, ExtrudeOp(name="Plate", sketch="Base", distance=8))
    apply_operation(session, context, SketchOp(
        name="Round", plane="xy", offset=8,
        entities=[{"type": "circle", "center": [-22.5, 0], "diameter": 30}]))
    apply_operation(session, context, ExtrudeOp(name="Boss", sketch="Round", distance=25))
    backend.set_parameter(context.doc_id, "grid_pitch", "45", units="mm")

    def attempt() -> list[tuple[str, bool, str]]:
        inner = raw(backend)
        component = inner._doc(context.doc_id).ComponentDefinition
        features = component.Features
        patterns = features.RectangularPatternFeatures
        boss = None
        for index in range(1, int(features.Count) + 1):
            if str(features.Item(index).Name) == "Boss":
                boss = features.Item(index)
        parents = inner._app.TransientObjects.CreateObjectCollection()
        parents.Add(boss)
        axis = component.WorkAxes.Item(1)

        def edge_along_x():
            """A linear edge of the plate, as an alternative direction entity."""
            body = component.SurfaceBodies.Item(1)
            for index in range(1, int(body.Edges.Count) + 1):
                edge = body.Edges.Item(index)
                try:
                    direction = edge.Geometry.Direction
                except Exception:
                    continue
                if abs(direction.X) > 0.9:
                    return edge
            return None

        cases = [
            ("spacing as a parameter name", lambda: patterns.Add(
                parents, axis, True, 2, "grid_pitch")),
            ("spacing as an expression with units", lambda: patterns.Add(
                parents, axis, True, 2, "45 mm")),
            ("spacing as a number in cm", lambda: patterns.Add(
                parents, axis, True, 2, 4.5)),
            ("count as a string", lambda: patterns.Add(
                parents, axis, True, "2", "45 mm")),
            ("direction reversed", lambda: patterns.Add(
                parents, axis, False, 2, "45 mm")),
            ("a linear edge as the direction", lambda: patterns.Add(
                parents, edge_along_x(), True, 2, "45 mm")),
        ]
        answers = []
        for label, build in cases:
            try:
                feature = build()
            except Exception as exc:
                answers.append((label, False, inner._explain(exc)))
                continue
            answers.append((label, True, f"built {feature.Name}"))
            try:
                feature.Delete()
            except Exception as exc:  # pragma: no cover
                answers.append((f"{label}: could not undo it", False, str(exc)[:80]))
        return answers

    try:
        report(on_thread(backend, attempt))
    except Exception:
        traceback.print_exc(limit=4)
    try:
        backend.close_document(context.doc_id, save=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def probe_sweep(session, backend) -> None:
    print("\n=== sweep")
    document = backend.new_part("SweepProbe", units="mm")
    context = session.register(document, "mm", "deg")
    apply_operation(session, context, SketchOp(
        name="Path", plane="xy",
        entities=[{"type": "arc", "center": [0, 0], "radius": 45,
                   "start_angle": 0, "end_angle": 90}]))
    apply_operation(session, context, SketchOp(
        name="Profile", plane="yz",
        entities=[{"type": "circle", "center": [45, 0], "diameter": 20}]))

    def attempt() -> list[tuple[str, bool, str]]:
        inner = raw(backend)
        component = inner._doc(context.doc_id).ComponentDefinition
        features = component.Features
        path_sketch = inner._sketch(context.doc_id, "Path")
        profile_sketch = inner._sketch(context.doc_id, "Profile")

        print("\n  what is in the path sketch:")
        for line in census(path_sketch):
            print(f"    {line}")

        from inventor_mcp.backend.com.backend import _first_curve

        curve = _first_curve(path_sketch)
        print(f"    first curve: {type(curve).__name__}")

        answers: list[tuple[str, bool, str]] = []
        paths: list[tuple[str, object]] = []
        for label, build in (
            ("Features.CreatePath(first curve)", lambda: features.CreatePath(curve)),
            ("Profiles.AddForSurface(first curve)",
             lambda: path_sketch.Profiles.AddForSurface(curve)),
            ("Profiles.AddForSurface() with no argument",
             lambda: path_sketch.Profiles.AddForSurface()),
        ):
            try:
                made = build()
            except Exception as exc:
                answers.append((label, False, inner._explain(exc)))
                continue
            answers.append((label, True, type(made).__name__))
            paths.append((label, made))

        join = inner._k("kJoinOperation")
        for label, path in paths:
            profile = profile_sketch.Profiles.AddForSolid()
            try:
                feature = features.SweepFeatures.AddUsingPath(profile, path, join)
            except Exception as exc:
                answers.append((f"AddUsingPath with {label}", False, inner._explain(exc)))
                continue
            answers.append((f"AddUsingPath with {label}", True, str(feature.Name)))
            try:
                feature.Delete()
            except Exception:  # pragma: no cover
                pass
        return answers

    try:
        report(on_thread(backend, attempt))
    except Exception:
        traceback.print_exc(limit=4)
    try:
        backend.close_document(context.doc_id, save=False)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", default=[], choices=["sweep", "pattern"])
    args = parser.parse_args(argv)

    try:
        session = Session(backend_kind="inventor")
        backend = session.ensure_backend()
        info = backend.connect(visible=True, create=True)
    except Exception as exc:
        print(f"Could not reach Inventor: {exc}")
        return 1
    print(f"Inventor {info.version}")

    if not args.only or "pattern" in args.only:
        probe_pattern(session, backend)
    if not args.only or "sweep" in args.only:
        probe_sweep(session, backend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
