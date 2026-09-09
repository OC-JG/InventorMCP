r"""Do topology handles survive a rebuild? Ask the ReferenceKeyManager.

A handle from `select_topology` is a name for a live COM object, and a rebuild
throws that object away. Until 2026-09-09 the COM backend handed the dead
object back: the docstrings said handles expire and nothing enforced it, so
what a caller got was not a refusal but a pointer at geometry that no longer
existed. That is fixed -- every use goes through `_live`, which probes the
object, rebinds it from a reference key if it has one, and refuses if neither
works -- and this probe is what says whether the *rebinding* half actually
functions on an installed Inventor.

**The call is published and the marshalling is not.** `Document.
ReferenceKeyManager` has `CreateKeyContext()`, and an entity has
`GetReferenceKey(KeyContext)` -- where the key is a byte-array `[out]`
parameter in the type library, which pywin32 usually turns into the return
value. Usually. Then `BindKeyToObject(key, context)` takes it back. The
published rule that shapes the backend is that **a B-Rep key needs the context
it was made with**, which is why the context is kept per document rather than
made per call: a context created and thrown away is a key that can never be
used.

So this run answers four things in order, and stops being interesting as soon
as one fails:

1. whether `ReferenceKeyManager` exists on this release, and what it offers;
2. whether `GetReferenceKey` gives anything, and what shape the thing is;
3. whether `BindKeyToObject` gives back the same face **before** any rebuild;
4. whether it still does **after** a parameter change and a rebuild -- which is
   the only question that matters, since a key that only works while nothing
   has moved is a key with no use.

It also prints `Face.CreatedByFeature` for every face, because that is the
other half of the same roadmap item and the backend reads it now: the simulator
has answered "which feature made this" since it was written and the live side
never did.

Nothing is saved.

    python scripts/probe_reference_keys.py
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
from pydantic import TypeAdapter  # noqa: E402

_OPERATION = TypeAdapter(Operation)

#: A plate with a parameter to move, so the rebuild in step 4 is a real one --
#: the faces are genuinely re-made rather than left alone.
PLATE = [
    _OPERATION.validate_python(
        {"op": "sketch", "name": "S", "plane": "xy", "entities": [
            {"type": "rectangle", "center": [0, 0], "width": "plate_w", "height": 40}]}),
    _OPERATION.validate_python(
        {"op": "extrude", "name": "Plate", "sketch": "S", "distance": 10}),
]


def _text(value: Any) -> str:
    try:
        return str(value)
    except Exception as exc:
        return f"<unreadable: {type(exc).__name__}: {exc}>"


def _shape(value: Any) -> str:
    """What a returned key actually is, since that is the unmeasured part."""
    kind = type(value).__name__
    try:
        return f"{kind} of length {len(value)}"
    except Exception:
        return kind


def probe(inner: Any, doc_id: str, dynamic: Any) -> None:
    document = inner._doc(doc_id)
    component = document.ComponentDefinition
    body = component.SurfaceBodies.Item(1)

    print("=" * 70)
    print("1. IS THERE A ReferenceKeyManager AT ALL")
    print("=" * 70)
    try:
        manager = document.ReferenceKeyManager
    except Exception as exc:
        print(f"    Document.ReferenceKeyManager raises: {type(exc).__name__}: {exc}")
        print("    Nothing below can work; handles stay valid-until-rebuild on "
              "this release, and `_live` refusing them is then the whole fix.")
        return
    print(f"    got one: {type(manager).__name__}")
    for name in sorted(n for n in dir(manager) if not n.startswith("_")):
        try:
            value = getattr(manager, name)
        except Exception as exc:
            print(f"      {name:30s} raises {type(exc).__name__}")
            continue
        print(f"      {name:30s} {'method' if callable(value) else _text(value)[:40]}")

    print("\n" + "=" * 70)
    print("2. A KEY FOR THE TOP FACE, AND WHAT SHAPE IT IS")
    print("=" * 70)
    try:
        context = manager.CreateKeyContext()
    except Exception as exc:
        print(f"    CreateKeyContext raises: {type(exc).__name__}: {exc}")
        return
    print(f"    CreateKeyContext() -> {_text(context)} ({type(context).__name__})")

    faces = [body.Faces.Item(index) for index in range(1, int(body.Faces.Count) + 1)]
    print(f"\n    the body has {len(faces)} faces; CreatedByFeature for each:")
    for index, face in enumerate(faces, start=1):
        try:
            made = face.CreatedByFeature
            print(f"      face {index}: {_text(made.Name)}")
        except Exception as exc:
            print(f"      face {index}: CreatedByFeature raises "
                  f"{type(exc).__name__}: {exc}")

    # The highest face, which is the one a parameter change will not move in Z
    # -- so "did we get the same face back" is a fair question after a rebuild.
    def height(face: Any) -> float:
        try:
            return float(face.Evaluator.RangeBox.MaxPoint.Z)
        except Exception:
            return -1.0

    target = max(faces, key=height)
    print(f"\n    picked the top face at Z = {height(target):.4f} cm")
    key = None
    for label, call in (
            ("GetReferenceKey(context)", lambda: target.GetReferenceKey(context)),
            ("GetReferenceKey()", lambda: target.GetReferenceKey()),
    ):
        try:
            key = call()
        except Exception as exc:
            print(f"    {label} raises: {type(exc).__name__}: {exc}")
            continue
        print(f"    {label} -> {_shape(key)}")
        break
    if key is None:
        print("    No key, so nothing can be rebound. `_reference_key` returning "
              "None is then the honest answer and handles stay "
              "valid-until-rebuild -- which is what the docs have always said.")
        return

    print("\n" + "=" * 70)
    print("3. BIND IT BACK, WITH NOTHING CHANGED")
    print("=" * 70)
    try:
        same = manager.BindKeyToObject(key, context)
        print(f"    BindKeyToObject -> {type(same).__name__} at "
              f"Z = {height(same):.4f} cm "
              f"({'the same face' if abs(height(same) - height(target)) < 1e-9 else 'A DIFFERENT FACE'})")
    except Exception as exc:
        print(f"    BindKeyToObject raises: {type(exc).__name__}: {exc}")
        print("    If this is a type mismatch, the key came back in a shape "
              "pywin32 will not hand straight back -- which is the whole "
              "unmeasured part. The shape printed above is the thing to read.")
        return

    print("\n" + "=" * 70)
    print("4. AND AFTER A REBUILD, WHICH IS THE ONLY QUESTION THAT MATTERS")
    print("=" * 70)
    print("    widening the plate from 60 to 90 mm...")
    try:
        inner.set_parameter(doc_id, "plate_w", "90 mm")
        inner.rebuild(doc_id)
    except Exception as exc:
        print(f"    the rebuild itself failed: {type(exc).__name__}: {exc}")
        return
    print(f"    the stored face object is "
          f"{'still live' if height(target) > 0 else 'DEAD, as expected'}")
    try:
        rebound = manager.BindKeyToObject(key, context)
    except Exception as exc:
        print(f"    BindKeyToObject raises after the rebuild: "
              f"{type(exc).__name__}: {exc}")
        print("    So a key does not survive a rebuild on this release, and "
              "`durable` should not be reported as True for one. That is a "
              "finding: the backend would then be claiming a durability it "
              "does not have.")
        return
    print(f"    BindKeyToObject -> {type(rebound).__name__} at "
          f"Z = {height(rebound):.4f} cm")
    print("\n    *** If that Z is still the plate's thickness, a handle can be "
          "rebound after a rebuild and `durable: true` is earned. ***")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args(argv)

    session = Session(backend_kind="inventor")
    backend = session.ensure_backend()
    info = backend.connect(visible=True, create=True)
    print("=" * 70)
    print(f"Inventor {info.version} -- do topology handles survive a rebuild?")
    print("=" * 70)

    document = backend.new_part("KeyProbe", units="mm")
    context = session.register(document, "mm", "deg")
    backend.set_parameter(context.doc_id, "plate_w", "60 mm")
    for op in PLATE:
        apply_operation(session, context, op)

    def everything() -> None:
        from inventor_mcp.backend.com.backend import _dynamic

        try:
            probe(raw(backend), context.doc_id, _dynamic)
        except Exception:
            print("\n!!! the probe failed; what it printed before this still stands")
            traceback.print_exc(limit=6, file=sys.stdout)

    on_thread(backend, everything)

    print("\n" + "=" * 70)
    if not args.keep_open:
        backend.close_document(context.doc_id, save=False)
        session.forget(context.doc_id)
        print("The scratch part is closed and nothing was saved.")
    else:
        print("Left open, as asked.")
    print("Paste all of the above back. Step 4 is the one that decides whether "
          "`TopoInfo.durable` is a claim this server has earned; steps 1 to 3 "
          "say where it stops if it has not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
