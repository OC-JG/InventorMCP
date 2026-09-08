"""Ask live objects what they offer, because the type library will not say.

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
  folder, and this prints what that folder holds, so the fix can be checked
  rather than hoped for.

**The answer it goes after is `GetTypeInfo`.** makepy generates a module per
type library, so an object whose class the library does not publish has no
wrapper to read -- but the object still answers `ITypeInfo`, and that names its
real members and how many arguments each takes. That is the one thing
`com_signatures.py` cannot do, because it reads the library rather than the
object.

Everything runs on the apartment that owns the objects -- see `apartment.py`,
which is the module that had to exist after the first version of this probe
reached across the thread and got told `FileManager` does not exist.

Read-only apart from one scratch part, which it closes. Nothing is saved.

    python scripts/probe_definitions.py
    python scripts/probe_definitions.py --keep-open
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from apartment import on_thread, raw  # noqa: E402
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

#: `FUNCDESC.invkind`, so a property read and a property write are told apart
#: from a method -- which is the difference between "set this" and "call this".
INVOKE = {1: "method", 2: "get", 4: "put", 8: "putref"}


def typeinfo(obj: Any) -> None:
    """What the object's own `ITypeInfo` says it has.

    The point of the whole probe. A generated wrapper reflects a *declaration*;
    this reflects what the object in hand actually implements, which for
    `MoveFaceTypeDefinition` is the only source there is.
    """
    ole = getattr(obj, "_oleobj_", None)
    if ole is None:
        print("      (no _oleobj_: not a COM object)")
        return
    try:
        info = ole.GetTypeInfo()
        attr = info.GetTypeAttr()
    except Exception as exc:
        print(f"      GetTypeInfo raises {type(exc).__name__}: {exc}")
        return
    print(f"      {attr.cFuncs} function(s), {attr.cVars} variable(s)")
    for index in range(attr.cFuncs):
        try:
            desc = info.GetFuncDesc(index)
            name = info.GetNames(desc.memid)[0]
        except Exception:
            continue
        if name in BORING:
            continue
        kind = INVOKE.get(desc.invkind, str(desc.invkind))
        print(f"      {name:34s} {kind:6s} {desc.cParams} arg(s)")
    for index in range(attr.cVars):
        try:
            name = info.GetNames(info.GetVarDesc(index).memid)[0]
        except Exception:
            continue
        print(f"      {name:34s} field")


def members(obj: Any, label: str, dynamic: Any) -> None:
    """Everything this live object answers to, three ways.

    `dir()` is listed as well as the type information because they disagree in
    a way that matters: `dir()` on a makepy wrapper shows the generated class,
    which is how a live shell once came back describing nothing but
    `HealthStatus`. `dynamic` is the COM backend's `_dynamic`, passed in so this
    file does not import from inside the backend twice over.
    """
    print(f"\n--- {label}")
    if obj is None:
        print("    (nothing to ask: the call above did not produce one)")
        return
    print(f"    python type: {type(obj).__name__}")
    print("    its own type info:")
    typeinfo(obj)
    loose = dynamic(obj)
    if type(loose).__name__ != type(obj).__name__:
        print(f"    the same object through dynamic dispatch "
              f"({type(loose).__name__}):")
        typeinfo(loose)
    names = sorted(n for n in dir(obj) if not n.startswith("_") and n not in BORING)
    if names:
        print("    dir():")
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception as exc:
            print(f"      {name:34s} raises {type(exc).__name__}")
            continue
        print(f"      {name:34s} "
              f"{'method' if callable(value) else repr(value)[:56]}")


def offers(obj: Any, label: str, *names: str) -> None:
    """Whether each name exists on a live object, and what it is."""
    print(f"\n--- {label}: does it have...")
    for name in names:
        try:
            attribute = getattr(obj, name)
        except Exception:
            attribute = None
        print(f"      {name:34s} "
              f"{'ABSENT' if attribute is None else 'method' if callable(attribute) else 'property'}")


def probe_templates(backend: Any) -> None:
    """Where Inventor keeps its templates, and which drawing ones are there.

    `Documents.Add` takes a path and the shipped drawing recipe says
    `"ISO.idw"`, so `_drawing_template` resolves a bare name against this folder
    and one level below it. This listing is what says whether that can work on
    this machine -- and it is the reading the drawing pass died for want of.
    """
    print("=" * 70)
    print("THE DRAWING TEMPLATE")
    print("=" * 70)
    manager = backend._require_app().FileManager
    drawing_type = backend._k("kDrawingDocumentObject")
    print(f"    kDrawingDocumentObject   {drawing_type}")
    try:
        print(f"    GetTemplateFile          {manager.GetTemplateFile(drawing_type)}")
    except Exception as exc:
        print(f"    GetTemplateFile raises: {exc}")
    folder = ""
    for name in ("TemplatesPath", "DesignDataPath", "WorkspacePath"):
        try:
            value = getattr(manager, name)
        except Exception as exc:
            print(f"    {name:24s} raises: {type(exc).__name__}: {exc}")
            continue
        print(f"    {name:24s} {value}")
        if name == "TemplatesPath":
            folder = str(value)
    if not folder:
        return
    if not os.path.isdir(folder):
        print(f"\n    TemplatesPath is not a readable directory: {folder!r}")
        return
    print("\n    drawing templates (this folder and one level down):")
    seen = 0
    places = [folder] + sorted(entry.path for entry in os.scandir(folder)
                               if entry.is_dir())
    for place in places:
        try:
            found = sorted(name for name in os.listdir(place)
                           if name.lower().endswith(".idw"))
        except OSError:
            continue
        for name in found:
            print(f"        {os.path.join(place, name)}")
            seen += 1
    if not seen:
        print("        none -- which is worth knowing on its own")


def probe_move_face(component: Any, transients: Any, dynamic: Any) -> None:
    """`MoveFaceDefinition`, and whatever `MoveFaceTypeDefinition` hands back.

    The definition is made from *a* face rather than a chosen one. Which face
    does not matter: what is being asked is what the definition object offers,
    not what moving that face would do, and one shot at a CAD seat is worth
    spending on the question rather than on a selector this probe never uses.
    """
    print("\n" + "=" * 70)
    print("MOVE FACE")
    print("=" * 70)
    features = component.Features.MoveFaceFeatures
    members(features, "MoveFaceFeatures (live)", dynamic)
    offers(features, "MoveFaceFeatures", "CreateDefinition",
           "CreateMoveFaceDefinition", "Add", "AddByDirection", "AddByFreeDrag",
           "AddByPlanarMove")

    faces = transients.CreateObjectCollection()
    faces.Add(component.SurfaceBodies.Item(1).Faces.Item(1))
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
    members(definition, "MoveFaceDefinition (live)", dynamic)
    if definition is None:
        return
    for name in ("MoveFaceType", "MoveFaceTypeDefinition"):
        try:
            value = getattr(definition, name)
        except Exception as exc:
            print(f"\n    {name} raises: {type(exc).__name__}: {exc}")
            continue
        print(f"\n    {name} = {value!r}")
        if not isinstance(value, (int, float, str, bool, type(None))):
            members(value, f"{name} (the object it hands back)", dynamic)


def probe_sketch_driven(component: Any, transients: Any, dynamic: Any) -> None:
    """The definition `SketchDrivenPatternFeatures.Add` wants, if it can be made.

    Three argument shapes are tried, widest first, because the backend supplies
    all three: Inventor's own dialog offers the seed's centroid or a point you
    pick, and a recipe always names a point. A wrong order cannot pass silently
    -- a feature collection, a sketch and a sketch point are three different COM
    types -- which is why trying these is safe where guessing `thicken`'s
    argument order was not.
    """
    print("\n" + "=" * 70)
    print("SKETCH DRIVEN PATTERN")
    print("=" * 70)
    patterns = component.Features.SketchDrivenPatternFeatures
    members(patterns, "SketchDrivenPatternFeatures (live)", dynamic)
    offers(patterns, "SketchDrivenPatternFeatures", "CreateDefinition",
           "CreateSketchDrivenPatternDefinition", "Add")

    parents = transients.CreateObjectCollection()
    parents.Add(component.Features.Item(int(component.Features.Count)))
    sketch = component.Sketches.Item("Spots")
    reference = sketch.SketchPoints.Item(1)
    made = None
    for name in ("CreateDefinition", "CreateSketchDrivenPatternDefinition"):
        factory = getattr(patterns, name, None)
        if factory is None:
            continue
        for label, arguments in (
                ("(parents, sketch, point)", (parents, sketch, reference)),
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
    members(made, "SketchDrivenPatternDefinition (live)", dynamic)


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

    document = backend.new_part("DefinitionProbe", units="mm")
    context = session.register(document, "mm", "deg")
    for op in PLATE:
        apply_operation(session, context, op)

    def everything() -> None:
        """All the probing, on the apartment. Nothing live leaves this function."""
        from inventor_mcp.backend.com.backend import _dynamic

        inner = raw(backend)
        probe_templates(inner)
        component = inner._doc(context.doc_id).ComponentDefinition
        transients = inner._require_app().TransientObjects
        probe_move_face(component, transients, _dynamic)
        probe_sketch_driven(component, transients, _dynamic)

    on_thread(backend, everything)

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
