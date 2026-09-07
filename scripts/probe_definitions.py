"""Ask three live objects what they offer, because the type library will not say.

The 2026-09-07 pass measured `thicken` and left three things failing, and two of
them failed for the same reason: **the feature takes a *definition* object, and
makepy generates no module for what that definition holds.** So
`com_signatures.py` answers "no generated module in this type library", which is
not proof of anything -- late binding asks the object rather than the wrapper,
and this asks the object.

What the type library did say, and what it left open:

* **`MoveFaceFeatures.Add(Definition)`**, and `MoveFaceDefinition` has
  properties `Faces`, `MoveFaceType` and **`MoveFaceTypeDefinition`** -- and one
  method, `Copy`. So the direction and the distance live on that third object,
  whose class is not generated. The backend's three guessed setters
  (`SetDirectionAndDistance` and friends) are not on the definition and never
  were; `--search MoveFaceType` returns nothing at all.
* **`SketchDrivenPatternFeatures.Add(Definition)`** -- one argument, where the
  backend passed five. No `CreateDefinition` appears in the wrapper, which
  again is not proof: `MoveFaceFeatures` shows no such method either and the
  live one produced a definition perfectly well, which is how that failure got
  as far as the setter.
* **The drawing document.** `Documents.Add` was handed the recipe's
  `"ISO.idw"` verbatim -- a bare name is not a path -- and answered "Exception
  occurred". That is fixed by resolving a name against Inventor's own templates
  folder, and this prints what that folder and the default template actually
  are, so the fix can be checked rather than hoped for.

Read-only apart from one scratch part, which it closes. Nothing is saved.

    python scripts/probe_definitions.py
    python scripts/probe_definitions.py --keep-open
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inventor_mcp.backend.base import ResolvedSelector  # noqa: E402
from inventor_mcp.builder import apply_operation  # noqa: E402
from inventor_mcp.schema import Operation  # noqa: E402
from inventor_mcp.session import Session  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402

#: A plate, so there are faces to hand a definition and a feature to pattern.
PLATE = TypeAdapter(list[Operation]).validate_python([
    {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 80, "height": 40}]},
    {"op": "extrude", "name": "Plate", "sketch": "Outline", "distance": 6},
    {"op": "sketch", "name": "Pocket", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [-25, -10], "width": 10, "height": 10}]},
    {"op": "extrude", "name": "Slot", "sketch": "Pocket", "distance": 3,
     "operation": "cut"},
    {"op": "sketch", "name": "Spots", "plane": "xy", "entities": [
        {"type": "point", "name": "home", "position": [-25, -10]},
        {"type": "point", "name": "a", "position": [25, 10]}]},
])

#: Names worth not printing: every COM object carries these and they say nothing
#: about the interface being probed.
BORING = {"Application", "Parent", "Type", "CLSID", "coclass_clsid"}


def members(obj: object, label: str) -> None:
    """Everything this live object answers to, which is the point of asking it."""
    print(f"\n--- {label}")
    if obj is None:
        print("    (nothing to ask: the call above did not produce one)")
        return
    print(f"    python type: {type(obj).__name__}")
    names = sorted(
        name for name in dir(obj)
        if not name.startswith("_") and name not in BORING
    )
    if not names:
        print("    dir() is empty -- a pure late-bound wrapper. Try the names "
              "below one at a time instead.")
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception as exc:  # pragma: no cover - that is the answer
            print(f"    {name:32s} raises: {type(exc).__name__}")
            continue
        kind = "method" if callable(value) else repr(value)[:60]
        print(f"    {name:32s} {kind}")


def probe(obj: object, label: str, *names: str) -> None:
    """Whether each name exists on a live object, and what it is."""
    print(f"\n--- {label}: does it have...")
    for name in names:
        attribute = getattr(obj, name, None)
        if attribute is None:
            print(f"    {name:32s} ABSENT")
        else:
            print(f"    {name:32s} {'method' if callable(attribute) else 'property'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args(argv)

    session = Session(backend_kind="inventor")
    backend = session.ensure_backend()
    info = backend.connect(visible=True, create=True)
    print("=" * 70)
    print(f"Inventor {info.version} -- probing the objects makepy skipped")
    print("=" * 70)

    app = backend._require_app()  # noqa: SLF001 - a probe is allowed to reach in

    # 3 first, because it needs no part and answers the drawing failure.
    print("\n" + "=" * 70)
    print("THE DRAWING TEMPLATE")
    print("=" * 70)
    try:
        drawing_type = backend._k("kDrawingDocumentObject")  # noqa: SLF001
        print(f"    kDrawingDocumentObject   {drawing_type}")
        print(f"    GetTemplateFile          {app.FileManager.GetTemplateFile(drawing_type)}")
    except Exception as exc:
        print(f"    GetTemplateFile raises: {exc}")
    folder = ""
    for name in ("TemplatesPath", "DesignDataPath", "WorkspacePath"):
        try:
            value = getattr(app.FileManager, name)
        except Exception as exc:
            print(f"    {name:24s} raises: {type(exc).__name__}")
            continue
        print(f"    {name:24s} {value}")
        if name == "TemplatesPath":
            folder = str(value)
    # Which drawing templates are actually there, and where. `_drawing_template`
    # resolves a bare name against this folder and one level below it, and
    # `"ISO.idw"` is what the shipped drawing recipe asks for -- so this is the
    # listing that says whether that recipe can work on this machine.
    if folder:
        print("\n    drawing templates found (one level deep):")
        seen = 0
        for path in sorted(Path(folder).glob("*.idw")) + sorted(
                Path(folder).glob("*/*.idw")):
            print(f"        {path}")
            seen += 1
        if not seen:
            print("        none -- which is worth knowing on its own")

    document = backend.new_part("DefinitionProbe", units="mm")
    context = session.register(document, "mm", "deg")
    for op in PLATE:
        apply_operation(session, context, op)
    component = backend._doc(context.doc_id).ComponentDefinition  # noqa: SLF001

    print("\n" + "=" * 70)
    print("MOVE FACE")
    print("=" * 70)
    features = component.Features.MoveFaceFeatures
    members(features, "MoveFaceFeatures (live)")
    probe(features, "MoveFaceFeatures", "CreateDefinition", "CreateMoveFaceDefinition",
          "Add", "AddByDirection", "AddByFreeDrag", "AddByPlanarMove")

    faces = backend._topology_collection(  # noqa: SLF001
        context.doc_id, ResolvedSelector(kind="face", filter="top", limit=1))
    definition = None
    for name in ("CreateDefinition", "CreateMoveFaceDefinition"):
        factory = getattr(features, name, None)
        if factory is None:
            continue
        try:
            definition = factory(faces)
            print(f"\n    {name}(faces) -> a definition")
            break
        except Exception as exc:
            print(f"\n    {name}(faces) raises: {exc}")
    members(definition, "MoveFaceDefinition (live)")
    if definition is not None:
        for name in ("MoveFaceType", "MoveFaceTypeDefinition"):
            try:
                value = getattr(definition, name)
            except Exception as exc:
                print(f"\n    {name} raises: {type(exc).__name__}: {exc}")
                continue
            print(f"\n    {name} = {value!r}")
            if not isinstance(value, (int, float, str, bool, type(None))):
                members(value, f"{name} (the object it hands back)")

    print("\n" + "=" * 70)
    print("SKETCH DRIVEN PATTERN")
    print("=" * 70)
    patterns = component.Features.SketchDrivenPatternFeatures
    members(patterns, "SketchDrivenPatternFeatures (live)")
    probe(patterns, "SketchDrivenPatternFeatures",
          "CreateDefinition", "CreateSketchDrivenPatternDefinition", "Add")
    made = None
    parents = app.TransientObjects.CreateObjectCollection()
    parents.Add(component.Features.Item(int(component.Features.Count)))
    sketch = backend._sketch(context.doc_id, "Spots")  # noqa: SLF001
    reference = backend._sketch_point(sketch, 0)  # noqa: SLF001
    for name in ("CreateDefinition", "CreateSketchDrivenPatternDefinition"):
        factory = getattr(patterns, name, None)
        if factory is None:
            continue
        # Widest first, because the backend supplies all three: Inventor's own
        # dialog offers the seed's centroid or a point you pick, and a recipe
        # always names a point. A wrong order cannot pass silently here -- the
        # three are three different COM types.
        for label, arguments in (("(parents, sketch, point)",
                                  (parents, sketch, reference)),
                                 ("(parents, sketch)", (parents, sketch)),
                                 ("(parents)", (parents,))):
            try:
                made = factory(*arguments)
                print(f"\n    {name}{label} -> a definition")
                break
            except Exception as exc:
                print(f"\n    {name}{label} raises: {exc}")
        if made is not None:
            break
    members(made, "SketchDrivenPatternDefinition (live)")

    print("\n" + "=" * 70)
    if not args.keep_open:
        backend.close_document(context.doc_id, save=False)
        session.forget(context.doc_id)
        print("The scratch part is closed and nothing was saved.")
    else:
        print("Left open, as asked.")
    print("Paste all of the above back. The two questions it answers are what "
          "MoveFaceTypeDefinition wants, and how to make a sketch-driven "
          "pattern definition.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
