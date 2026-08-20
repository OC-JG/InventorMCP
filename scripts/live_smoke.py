"""Exercise the COM backend against a live Inventor, outside of any MCP client.

Run this on Windows with Inventor installed:

    python scripts/live_smoke.py                     # builds examples/mounting_plate.json
    python scripts/live_smoke.py examples/hex_standoff.json
    python scripts/live_smoke.py --keep-open --export out

It prints one line per step so a failure names the exact operation that broke,
which is what makes a COM error actionable.  Nothing is saved unless --export
is given, and the document is closed at the end unless --keep-open is passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inventor_mcp.backend.base import ExportRequest, ScreenshotRequest  # noqa: E402
from inventor_mcp.builder import apply_operation, apply_parameter  # noqa: E402
from inventor_mcp.schema import PartRecipe  # noqa: E402
from inventor_mcp.session import Session  # noqa: E402

OK = "  ok  "
FAIL = " FAIL "


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipe", nargs="?", default=str(ROOT / "examples" / "mounting_plate.json"))
    parser.add_argument("--backend", default="inventor", choices=["inventor", "mock"])
    parser.add_argument("--export", metavar="DIR", help="Write STEP, STL and a PNG here.")
    parser.add_argument("--keep-open", action="store_true", help="Leave the part open in Inventor.")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="Keep going after a failure. Off by default, because a failed "
                             "parameter makes every later step fail for the same reason.")
    parser.add_argument("--verbose", action="store_true", help="Traceback for every failure.")
    parser.add_argument("--topology", action="store_true",
                        help="List the finished part's edges and faces with their positions. "
                             "Use it to check what a selector would actually match.")
    args = parser.parse_args(argv)

    recipe = PartRecipe.model_validate(json.loads(Path(args.recipe).read_text()))
    print(f"Recipe: {recipe.name} ({len(recipe.parameters)} parameters, "
          f"{len(recipe.operations)} operations) from {args.recipe}")

    session = Session(backend_kind=args.backend)
    failures: list[str] = []

    def step(label: str, fn):
        if failures and not args.continue_on_error:
            print(f"[ skip ] {label}")
            return None
        try:
            result = fn()
        except Exception as exc:
            print(f"[{FAIL}] {label}\n         {type(exc).__name__}: {exc}")
            hint = getattr(exc, "hint", None)
            if hint:
                print(f"         hint: {hint}")
            # Only the first failure gets a traceback; the rest are usually
            # consequences of it and the noise buries the real cause.
            if args.verbose or not failures:
                traceback.print_exc(limit=6)
            failures.append(label)
            return None
        print(f"[{OK}] {label}")
        return result

    backend = session.ensure_backend()
    info = step("connect", lambda: backend.connect(visible=True, create=True))
    if info is None:
        return 1
    print(f"         backend={backend.name} version={info.version} "
          f"documents={info.documents}")
    if backend.name == "mock":
        print("\nNOTE: running against the simulator, not Inventor. "
              "Use --backend inventor to see why the live connection failed.")

    doc = step("new_part", lambda: backend.new_part(
        recipe.name, units=recipe.units, angle_units=recipe.angle_units))
    if doc is None:
        return 1
    context = session.register(doc, recipe.units, recipe.angle_units)

    if recipe.material:
        step(f"material {recipe.material!r}",
             lambda: backend.set_material(context.doc_id, recipe.material))

    for spec in recipe.parameters:
        step(f"parameter {spec.name} = {spec.value!r}",
             lambda spec=spec: apply_parameter(session, context, spec))

    def volume_now():
        """Quietly, so a failure here never masquerades as a failed operation."""
        try:
            return backend.mass_properties(context.doc_id).volume
        except Exception:
            return None

    previous_volume = None
    for index, op in enumerate(recipe.operations):
        label = f"op {index}: {op.op}" + (f" ({op.name})" if op.name else "")
        result = step(label, lambda op=op: apply_operation(session, context, op))
        if result and op.op != "sketch":
            # A cut that removes nothing looks exactly like a cut that worked,
            # unless you watch the volume.  That is how the angle bracket
            # spent a while cutting slots through empty air.
            volume = volume_now()
            if volume is not None:
                change = "" if previous_volume is None else f"  ({volume - previous_volume:+.4f})"
                print(f"         volume: {volume:.4f} cm^3{change}")
                previous_volume = volume
        if result and op.op == "sketch":
            print(f"         entities={result.get('entities')} "
                  f"constraints={result.get('constraints')} "
                  f"dimensions={result.get('dimensions')} "
                  f"profiles={result.get('profiles')} "
                  f"fully_constrained={result.get('fully_constrained')}")
            axes = result.get("axes")
            if axes:
                print(f"         sketch axes: {axes}")
            inferred = result.get("inferred_constraints") or 0
            refused = result.get("refused_constraints") or 0
            if inferred:
                print(f"         {inferred} constraint(s) Inventor had already made")
            if refused:
                print(f"         WARNING: {refused} constraint(s) refused - this sketch "
                      "keeps a degree of freedom")

    properties = step("mass_properties", lambda: backend.mass_properties(context.doc_id))
    if properties:
        box = properties.bounding_box
        if box:
            size = [round((box[i + 3] - box[i]) * 10, 3) for i in range(3)]
            # Where the part sits, not just how big it is: a mirrored sketch
            # plane moves a part without changing any of its spans.
            extents = " x ".join(
                f"{box[i] * 10:.1f}..{box[i + 3] * 10:.1f}" for i in range(3))
            print(f"         bounding box (mm): {size}   at {extents}")
        print(f"         volume: {properties.volume:.4f} cm^3   mass: {properties.mass}")

    if args.topology:
        from inventor_mcp.backend.base import ResolvedSelector

        for kind in ("face", "edge"):
            try:
                matches = backend.select(context.doc_id, ResolvedSelector(kind=kind))
            except Exception as exc:
                print(f"[{FAIL}] listing {kind}s: {exc}")
                continue
            print(f"\n{len(matches)} {kind}(s), positions in mm:")
            for match in sorted(matches, key=lambda m: m.midpoint or (0, 0, 0)):
                where = ", ".join(f"{c * 10:8.2f}" for c in (match.midpoint or (0, 0, 0)))
                extra = ""
                if match.normal:
                    extra = "  normal " + ",".join(f"{c:5.2f}" for c in match.normal)
                elif match.direction:
                    extra = "  along  " + ",".join(f"{c:5.2f}" for c in match.direction)
                if match.convexity:
                    extra += f"  {match.convexity}"
                # Positions are printed in mm, so sizes are too: mm for an
                # edge's length, mm^2 for a face's area.
                if kind == "face":
                    size = f"{(match.area or 0) * 100:10.1f} mm2"
                else:
                    size = f"{(match.length or 0) * 10:10.2f} mm "
                print(f"    {match.id:>8}  {match.geometry:<12} ({where}) {size}{extra}")

    if args.export:
        out = Path(args.export).resolve()
        out.mkdir(parents=True, exist_ok=True)
        for fmt in ("step", "stl"):
            step(f"export {fmt}", lambda fmt=fmt: backend.export(
                context.doc_id, ExportRequest(path=str(out / recipe.name), format=fmt)))
        step("screenshot", lambda: backend.screenshot(
            context.doc_id, ScreenshotRequest(path=str(out / f"{recipe.name}.png"))))
        step("save", lambda: backend.save_document(
            context.doc_id, str(out / f"{recipe.name}.ipt")))
        print(f"         output in {out}")

    if not args.keep_open and args.export:
        step("close", lambda: backend.close_document(context.doc_id, save=False))

    print()
    if failures:
        print(f"{len(failures)} step(s) failed:")
        for label in failures:
            print(f"  - {label}")
        if not args.continue_on_error:
            print("\nStopped at the first failure. Re-run with --continue-on-error "
                  "to see how far it gets, or --verbose for full tracebacks.")
        return 1
    print("All steps passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
