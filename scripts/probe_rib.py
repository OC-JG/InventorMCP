r"""Rib, one more time, with the definition members the published page gives.

`RibFeatures.CreateDefinition(curves, isRib, reversed, thickness)` succeeds and
`RibFeatures.Add(definition)` has refused every definition it was handed --
fourteen-plus combinations, all `E_INVALIDARG`, recorded as gap 2 in
`docs/FEATURE_COVERAGE.md`. What every one of those attempts had in common is
what this probe fixes: **nobody had the definition's member list.** The
`RibFeatures_Add` page, read 2026-09-08, is `Add(Definition As RibDefinition)`
with no Remarks, so the refusal is about the definition's *state* and not about
the call -- and the `RibDefinition` page names four things none of the fourteen
touched:

* `IsRib` -- and its published meaning, which is the point. True projects the
  profile **lateral** to the sketch plane; False projects it **normal** to the
  plane. A rib drawn on a plane perpendicular to the plate wants True; one
  drawn on the plate's own face wants False. Every earlier attempt drew a line
  on a perpendicular plane, which is the True case, and passed `isRib` as a
  positional argument to `CreateDefinition` without knowing which way round it
  meant. **That is the first thing this probe separates.**
* `SetThicknessPlane(RibThicknessPlaneEnum)` --
  `kRibThicknessAtSketchPlane` or `kRibThicknessAtRoot`. A definition whose
  thickness plane has never been set may be the state `Add` is refusing.
* `ThicknessDirection`.
* `DraftAngle`, which is the one thing the composite rib this server ships
  deliberately cannot express: a moulded rib should thin as it rises, and a
  single planar silhouette pushed through a linear extrude cannot say that.

So this walks a matrix rather than guessing: two profiles (one on a
perpendicular plane, one on the plate's own top face), both values of `IsRib`,
each thickness plane, with and without a draft. Every attempt is reported with
what Inventor said, and a successful one is **measured and then undone**, so
the run answers "does it build" and "what does it build" together and leaves
the part as it was.

The composite rib stays whatever this says. It is measured and exact -- 20.88000
cm^3 for a 60 x 14 mm silhouette 2 mm thick -- and moving a measured route onto
an unmeasured one is what `docs/DECISIONS.md` refuses. What a success here buys
is a *second* route with draft on it, and its own item.

Nothing is saved.

    python scripts/probe_rib.py
    python scripts/probe_rib.py --keep-open
"""

from __future__ import annotations

import argparse
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
from probe_definitions import members  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402

_OPERATION = TypeAdapter(Operation)

#: A plate spanning Z = 0 to 6 mm, and two open profiles for a rib to be built
#: from. `Upright` is a line on XZ, perpendicular to the plate and standing on
#: it -- the geometry every earlier attempt used, which the published `IsRib`
#: meaning says is the *lateral* case. `Flat` is a line on the plate's own top
#: face, which is the normal case, and which nothing has tried.
#:
#: Both are single lines rather than closed profiles on purpose: Inventor's Rib
#: takes an **open** profile and finds its own extent against the part, which
#: is the whole difference between it and the composite this server ships.
SETUP = [
    _OPERATION.validate_python(
        {"op": "sketch", "name": "S", "plane": "xy", "entities": [
            {"type": "rectangle", "center": [0, 0], "width": 80, "height": 60}]}),
    _OPERATION.validate_python(
        {"op": "extrude", "name": "Plate", "sketch": "S", "distance": 6}),
    _OPERATION.validate_python(
        {"op": "sketch", "name": "Upright", "plane": "xz", "entities": [
            {"type": "line", "name": "web", "start": [-30, 6], "end": [30, 20],
             "locate": "none", "dimension": False}]}),
    _OPERATION.validate_python(
        {"op": "work_plane", "name": "Top", "kind": "offset", "base": "xy",
         "offset": 6}),
    _OPERATION.validate_python(
        {"op": "sketch", "name": "Flat", "plane": "Top", "entities": [
            {"type": "line", "name": "spine", "start": [-30, 0], "end": [30, 0],
             "locate": "none", "dimension": False}]}),
]

#: The two thickness planes the published `RibThicknessPlaneEnum` names, and
#: "leave it alone", which is what all fourteen earlier attempts did.
THICKNESS_PLANES = (None, "kRibThicknessAtSketchPlane", "kRibThicknessAtRoot")


def _volume(document: Any) -> float:
    try:
        return float(document.ComponentDefinition.MassProperties.Volume)
    except Exception:
        return float("nan")


def _curves(component: Any, transients: Any, sketch_name: str) -> Any:
    """The sketch's lines as an `ObjectCollection`.

    An `ObjectCollection` and not a `Profile`, which gap 2 already established
    is the right type: a `Profile` gives a type mismatch and this does not.
    """
    sketch = component.Sketches.Item(sketch_name)
    collection = transients.CreateObjectCollection()
    for index in range(1, int(sketch.SketchLines.Count) + 1):
        collection.Add(sketch.SketchLines.Item(index))
    return collection


def read_the_definition(inner: Any, component: Any, transients: Any,
                        dynamic: Any) -> None:
    """What a `RibDefinition` actually holds, which is the missing fact.

    Printed before anything is attempted, because it is worth having even if
    every attempt below fails: fourteen `E_INVALIDARG`s were spent guessing at
    a member list, and one listing ends that.
    """
    print("=" * 70)
    print("WHAT A RibDefinition HOLDS")
    print("=" * 70)
    ribs = component.Features.RibFeatures
    members(ribs, "RibFeatures (live)", dynamic)
    made = None
    try:
        made = ribs.CreateDefinition(
            _curves(component, transients, "Upright"), True, False, 0.2)
        print("\n    CreateDefinition(curves, True, False, 0.2) -> a definition")
    except Exception as exc:
        print(f"\n    CreateDefinition(curves, True, False, 0.2) raises: {exc}")
    members(made, "RibDefinition (live)", dynamic)
    for name in THICKNESS_PLANES[1:]:
        try:
            print(f"    {name:32s} = {inner._k(name)}")
        except Exception as exc:
            print(f"    {name:32s} unresolvable: {type(exc).__name__}: {exc}")


def attempt(inner: Any, document: Any, component: Any, transients: Any, *,
            sketch: str, is_rib: bool, thickness_plane: str | None,
            draft: float | None, extend: bool | None) -> None:
    """One combination, reported with what Inventor said and what it built.

    A success is measured and then **undone**, so the matrix keeps running
    against the same part and a later combination is not judged against an
    earlier one's material.
    """
    label = (f"{sketch:7s} IsRib={str(is_rib):5s} "
             f"plane={thickness_plane or 'default':26s} "
             f"draft={'none' if draft is None else f'{draft} rad':9s} "
             f"extend={'default' if extend is None else str(extend)}")
    ribs = component.Features.RibFeatures
    try:
        definition = ribs.CreateDefinition(
            _curves(component, transients, sketch), is_rib, False, 0.2)
    except Exception as exc:
        print(f"    {label}  CreateDefinition raises: {exc}")
        return
    notes: list[str] = []
    if thickness_plane is not None:
        try:
            definition.SetThicknessPlane(inner._k(thickness_plane))
        except Exception as exc:
            print(f"    {label}  SetThicknessPlane raises: {exc}")
            return
    if draft is not None:
        try:
            definition.DraftAngle = draft
        except Exception as exc:
            notes.append(f"DraftAngle refused ({type(exc).__name__})")
    if extend is not None:
        try:
            definition.ExtendProfile = extend
        except Exception as exc:
            notes.append(f"ExtendProfile refused ({type(exc).__name__})")
    before = _volume(document)
    try:
        feature = ribs.Add(definition)
    except Exception as exc:
        extra = f"  [{'; '.join(notes)}]" if notes else ""
        print(f"    {label}  Add raises: {exc}{extra}")
        return
    after = _volume(document)
    print(f"    {label}  *** BUILT ***  volume {before:.5f} -> {after:.5f} "
          f"(+{after - before:.5f} cm^3)"
          + (f"  [{'; '.join(notes)}]" if notes else ""))
    try:
        feature.Delete()
        print("          ...and deleted again, so the matrix continues clean")
    except Exception as exc:
        print(f"          !!! could not delete it: {exc} -- every reading after "
              "this one includes it")


def walk_the_matrix(inner: Any, document: Any, component: Any,
                    transients: Any) -> None:
    """Both profiles, both `IsRib` values, each thickness plane, with a draft.

    Ordered so the cheapest discriminator comes first. If `IsRib` is what the
    fourteen got wrong, the second line of this answers it and everything after
    is detail.
    """
    print("\n" + "=" * 70)
    print("EVERY COMBINATION THE PUBLISHED MEMBERS OPEN")
    print("=" * 70)
    for sketch in ("Upright", "Flat"):
        for is_rib in (True, False):
            for thickness_plane in THICKNESS_PLANES:
                attempt(inner, document, component, transients,
                        sketch=sketch, is_rib=is_rib,
                        thickness_plane=thickness_plane, draft=None, extend=None)
    print("\n--- and with a draft and an extended profile, on both profiles")
    for sketch in ("Upright", "Flat"):
        for is_rib in (True, False):
            attempt(inner, document, component, transients, sketch=sketch,
                    is_rib=is_rib, thickness_plane="kRibThicknessAtRoot",
                    draft=0.0349, extend=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args(argv)

    session = Session(backend_kind="inventor")
    backend = session.ensure_backend()
    info = backend.connect(visible=True, create=True)
    print("=" * 70)
    print(f"Inventor {info.version} -- rib, with the definition members read")
    print("=" * 70)

    document = backend.new_part("RibProbe", units="mm")
    context = session.register(document, "mm", "deg")
    for op in SETUP:
        apply_operation(session, context, op)

    def everything() -> None:
        """All of it on the apartment. Nothing live leaves this function."""
        from inventor_mcp.backend.com.backend import _dynamic

        inner = raw(backend)
        live = inner._doc(context.doc_id)
        component = live.ComponentDefinition
        transients = inner._require_app().TransientObjects
        # Separately, so one exception does not throw away what the other
        # already learned -- the lesson `probe_definitions.py` records, and the
        # member listing is worth having even if every attempt fails.
        for label, work in (
                ("the definition's members",
                 lambda: read_the_definition(inner, component, transients, _dynamic)),
                ("the matrix",
                 lambda: walk_the_matrix(inner, live, component, transients))):
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
    print("Paste all of the above back. If any line says *** BUILT ***, gap 2 "
          "in docs/FEATURE_COVERAGE.md has an answer and the volume beside it "
          "says what a real Rib does that the composite does not. If every "
          "line refuses, the member listing above is still the thing fourteen "
          "attempts were missing, and it says what is left to try.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
