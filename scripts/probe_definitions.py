r"""Ask live objects what they offer, because the type library will not say.

The 2026-09-07 pass measured `thicken` and left three things failing, and two of
them failed for the same reason: **the feature takes a *definition* object, and
makepy generates no module for what that definition holds.** So
`com_signatures.py` answers "no generated module in this type library", which is
not proof of anything -- late binding asks the object rather than the wrapper,
and this asks the object.

**The 2026-09-08 run settled two of the three.** What is left is the one this
file is named for:

* **`sketch_driven_pattern`: done.** `SketchDrivenPatternFeatures` really does
  have a `CreateDefinition` -- 4 arguments, 2 optional -- that the type library
  does not publish, and `CreateDefinition(parents, sketch, point)` produces a
  definition carrying `ParentFeatures`, `Sketch`, `BasePoint`, `ComputeType`,
  `Operation`, `ReferenceFaces`, `AffectedBodies`, `AffectedOccurrences` and a
  read-only `PatternOfBody`. `Add(Definition)` takes it from there. The backend
  now makes that exact call, so this section only confirms it.
* **The drawing template: done, and it was never `FileManager`.** That object
  has `GetTemplateFile` and none of `TemplatesPath`, `DesignDataPath` or
  `WorkspacePath` -- those are the *project's*, on
  `DesignProjectManager.ActiveDesignProject`. The active project's templates
  folder holds `Standard.idw` and a house `OCB_Standard.idw`, with `ISO.idw`,
  `DIN.idw`, `BSI.idw` and the rest one level down under `Metric\`, so a bare
  `"ISO.idw"` does resolve -- from the subfolder search rather than the folder
  itself.
* **`move_face`: the setter is found, and one argument of it is still unread.**
  `MoveFaceFeatures` has exactly `Add(Definition)` and `CreateDefinition(1
  argument)`, and that argument is a **`FaceCollection`** -- handed a generic
  `ObjectCollection` it answers "Type mismatch". `MoveFaceDefinition` then
  offers `Faces`, a **read-only** `MoveFaceType` (91395), a
  `MoveFaceTypeDefinition` that is `None` until a type is set, a settable
  `AutomaticBlending` (default True), and three setters:

      SetDirectionAndDistanceMoveType   3 arguments
      SetPlanarMoveType                 3 arguments, 1 optional
      SetFreeMoveType                   1 argument

  So the type is not assigned and then filled in; calling one of those *makes*
  it the type. None of the three names guessed before this was read
  (`SetDirectionAndDistance`, `SetDirectionMove`,
  `SetDirectionAndDistanceMoveData`) was the real one, which is the argument for
  reading rather than guessing in one line.

  **What is left is the third argument**, and that is what this run is for: the
  first version of `typeinfo` read only `GetNames(memid)[0]`, throwing away the
  parameter names that come back in the same tuple. It prints them now.

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
import traceback
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
        # One try around the whole member, not around the lookup alone: the
        # first version guarded `GetFuncDesc` and then read `desc.cParams`
        # outside the guard, which does not exist -- `PyFUNCDESC` carries
        # `args` and `cParamsOpt` -- so an AttributeError of mine killed the
        # run after printing one line. A probe that dies on its own formatting
        # wastes the seat it was spending.
        try:
            desc = info.GetFuncDesc(index)
            named = info.GetNames(desc.memid)
            name = named[0]
        except Exception as exc:
            print(f"      function {index} unreadable: {type(exc).__name__}: {exc}")
            continue
        if name in BORING:
            continue
        # The name and the kind are what this is for, so they are printed even
        # if the argument count cannot be worked out. `getattr` throughout
        # because a guessed attribute is what broke the last run: `cParams` is
        # not on `PyFUNCDESC` at all.
        kind = INVOKE.get(getattr(desc, "invkind", None), str(getattr(desc, "invkind", "?")))
        args = getattr(desc, "args", None)
        optional = getattr(desc, "cParamsOpt", 0) or 0
        if args is None:
            counted = "argument count unavailable"
        else:
            counted = f"{len(args)} arg(s)"
            if optional:
                counted += f", {optional} optional"
        # `GetNames` returns the member's name *and its parameters' names*, and
        # the first version of this read only `[0]`. That threw away the answer
        # to the question the probe existed for: 2026-09-08 found
        # `SetDirectionAndDistanceMoveType` and reported "3 arg(s)" without
        # saying what the third one is, which is a whole round trip for a slice.
        parameters = ", ".join(named[1:]) if len(named) > 1 else ""
        signature = f"({parameters})" if parameters else ""
        print(f"      {name:34s} {kind:6s} {counted:26s} {signature}")
    for index in range(attr.cVars):
        try:
            print(f"      {info.GetNames(info.GetVarDesc(index).memid)[0]:34s} field")
        except Exception as exc:
            print(f"      variable {index} unreadable: {type(exc).__name__}: {exc}")


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
    app = backend._require_app()
    manager = app.FileManager
    drawing_type = backend._k("kDrawingDocumentObject")
    print(f"    kDrawingDocumentObject   {drawing_type}")
    folders: list[str] = []
    try:
        default = str(manager.GetTemplateFile(drawing_type))
        print(f"    GetTemplateFile          {default}")
        folders.append(os.path.dirname(default))
    except Exception as exc:
        print(f"    GetTemplateFile raises: {exc}")
    # `FileManager.TemplatesPath` does not exist on 2027.1 -- measured, and it
    # raises the same `<unknown>.X` AttributeError that reaching across the
    # apartment does, which is why this asks the *project* as well. Inventor
    # documents these three as the project's, and the first probe asked the
    # wrong object.
    for owner, label in ((manager, "FileManager"),
                         (_project(app), "ActiveDesignProject")):
        if owner is None:
            print("    ActiveDesignProject      unreachable")
            continue
        for name in ("TemplatesPath", "DesignDataPath", "WorkspacePath"):
            try:
                value = getattr(owner, name)
            except Exception as exc:
                print(f"    {label}.{name:20s} raises: {type(exc).__name__}: {exc}")
                continue
            print(f"    {label}.{name:20s} {value}")
            if name == "TemplatesPath" and str(value):
                folders.append(str(value))
    _list_templates(folders)


def _project(app: Any) -> Any:
    """The active design project, which is where Inventor keeps its paths."""
    try:
        return app.DesignProjectManager.ActiveDesignProject
    except Exception as exc:
        print(f"    DesignProjectManager raises: {type(exc).__name__}: {exc}")
        return None


def _list_templates(folders: list[str]) -> None:
    """Which drawing templates are actually on this machine, and where.

    Both extensions, because the default this machine gave back is a **.dwg**
    -- `Standard.dwg`, not `Standard.idw` -- and the shipped recipe asks for
    `"ISO.idw"`. Whether that file exists here is the whole question, and
    listing only one extension would have answered half of it.
    """
    seen: set[str] = set()
    places: list[str] = []
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            if folder:
                print(f"\n    not a readable directory: {folder!r}")
            continue
        for place in [folder] + sorted(entry.path for entry in os.scandir(folder)
                                       if entry.is_dir()):
            key = os.path.normcase(os.path.abspath(place))
            if key not in seen:
                seen.add(key)
                places.append(place)
    if not places:
        print("\n    no template folder could be found at all")
        return
    print("\n    drawing templates (each folder and one level down):")
    found = 0
    for place in places:
        try:
            names = sorted(name for name in os.listdir(place)
                           if name.lower().endswith((".idw", ".dwg")))
        except OSError:
            continue
        for name in names:
            print(f"        {os.path.join(place, name)}")
            found += 1
    if not found:
        print("        none -- which is worth knowing on its own")


def probe_move_face(component: Any, transients: Any, dynamic: Any) -> None:
    """`MoveFaceDefinition`, and whatever `MoveFaceTypeDefinition` hands back.

    The definition is made from *a* face rather than a chosen one. Which face
    does not matter: what is being asked is what the definition object offers,
    not what moving that face would do, and one shot at a CAD seat is worth
    spending on the question rather than on a selector this probe never uses.

    **What the collection is, though, matters a great deal**, and the 2026-09-08
    run was wasted on getting it wrong: `CreateObjectCollection` gave
    `CreateDefinition` a generic `ObjectCollection` and Inventor answered "Type
    mismatch", so the definition was never reached. `CreateFaceCollection` is
    the one it wants. The backend has always used it -- `_new_collection` picks
    `CreateFaceCollection` for a face selector -- which is why the live
    acceptance run got a definition and *then* failed on the setter, while this
    probe failed a step earlier. A probe that does not do what the code does is
    measuring the probe.
    """
    print("\n" + "=" * 70)
    print("MOVE FACE")
    print("=" * 70)
    features = component.Features.MoveFaceFeatures
    members(features, "MoveFaceFeatures (live)", dynamic)
    offers(features, "MoveFaceFeatures", "CreateDefinition", "Add")

    faces = transients.CreateFaceCollection()
    faces.Add(component.SurfaceBodies.Item(1).Faces.Item(1))
    # `CreateMoveFaceDefinition` is measured absent (2026-09-08), so only the
    # real name is tried. Two collection types are, though: a face collection
    # is what the type mismatch above says it wants, and an object collection
    # is what failed -- kept as the second attempt so a release that changed its
    # mind would say so rather than looking like a different fault.
    definition = None
    for label, collection in (("a FaceCollection", faces),
                              ("an ObjectCollection", None)):
        if collection is None:
            collection = transients.CreateObjectCollection()
            collection.Add(component.SurfaceBodies.Item(1).Faces.Item(1))
        try:
            definition = features.CreateDefinition(collection)
            print(f"\n    CreateDefinition({label}) -> a definition")
            break
        except Exception as exc:
            print(f"\n    CreateDefinition({label}) raises: {exc}")
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
    offers(patterns, "SketchDrivenPatternFeatures", "CreateDefinition", "Add")

    parents = transients.CreateObjectCollection()
    parents.Add(component.Features.Item(int(component.Features.Count)))
    sketch = component.Sketches.Item("Spots")
    reference = sketch.SketchPoints.Item(1)
    made = None
    try:
        made = patterns.CreateDefinition(parents, sketch, reference)
        print("\n    CreateDefinition(parents, sketch, point) -> a definition")
    except Exception as exc:
        print(f"\n    CreateDefinition(parents, sketch, point) raises: {exc}")
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
        component = inner._doc(context.doc_id).ComponentDefinition
        transients = inner._require_app().TransientObjects
        # Each section separately, so one exception does not throw away what the
        # others already learned. That is the lesson `probe_sweep_and_pattern`
        # records -- "the sweep probe died on a call I had not wrapped and
        # reported none of the three it had already made" -- and this probe
        # repeated it on 2026-09-08 with a bad attribute in its own formatting.
        for label, work in (
                ("the drawing template", lambda: probe_templates(inner)),
                ("move face", lambda: probe_move_face(component, transients, _dynamic)),
                ("sketch driven pattern",
                 lambda: probe_sketch_driven(component, transients, _dynamic))):
            try:
                work()
            except Exception:
                print(f"\n!!! probing {label} failed; the rest still follows")
                traceback.print_exc(limit=6, file=sys.stdout)

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
