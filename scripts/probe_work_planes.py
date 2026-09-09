r"""What arguments does each `WorkPlanes.Add...` really take?

`AddByPlaneAndTangent(plane, face)` was written from the published reference
and refused on the first live run, 2026-09-09: **"Work plane failed: Parameter
not optional."** That is COM's message for a required argument nobody passed,
so the call takes more than the two the page was read as giving -- and there is
no point guessing which, because a work plane built from a guessed argument is
defect 12's shape all over again: something that builds and is not what was
asked for.

So this asks the object. One listing of `WorkPlanes`' own type information
gives every `Add...` overload with its argument *names* and how many are
optional, which settles four things at once:

* `AddByPlaneAndTangent` -- what the third argument is called, and whether
  there is a fourth;
* `AddByLinePlaneAndAngle`, which the angled plane calls positionally on the
  grounds that the order is documented and the names are not. If the names are
  right there in the type info, that reasoning was wrong and the call should
  name them;
* `AddByPlaneAndOffset` and `AddByTwoPlanes`, which have been called for a
  year and never had their signatures read either;
* whatever else this release offers, which is the cheapest way to find out
  what a `work_plane` kind could be added next.

`WorkAxes` and `WorkPoints` are listed too, for the same money: their three
calls were measured as *working* in 2026-09-07's run without anybody reading
their argument lists, and a signature read is one fewer thing resting on a
run that happened to pass.

Read-only. Nothing is built and nothing is saved.

    python scripts/probe_work_planes.py
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from apartment import on_thread, raw  # noqa: E402
from inventor_mcp.session import Session  # noqa: E402
from probe_definitions import members  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args(argv)

    session = Session(backend_kind="inventor")
    backend = session.ensure_backend()
    info = backend.connect(visible=True, create=True)
    print("=" * 70)
    print(f"Inventor {info.version} -- what the work-geometry calls really take")
    print("=" * 70)

    document = backend.new_part("WorkPlaneProbe", units="mm")
    context = session.register(document, "mm", "deg")

    def everything() -> None:
        from inventor_mcp.backend.com.backend import _dynamic

        inner = raw(backend)
        component = inner._doc(context.doc_id).ComponentDefinition
        # Separately, so one failing collection does not throw away the others.
        for label, attribute in (("WorkPlanes", "WorkPlanes"),
                                 ("WorkAxes", "WorkAxes"),
                                 ("WorkPoints", "WorkPoints")):
            try:
                members(getattr(component, attribute), f"{label} (live)", _dynamic)
            except Exception:
                print(f"\n!!! listing {label} failed; the rest still follows")
                traceback.print_exc(limit=6, file=sys.stdout)

    on_thread(backend, everything)

    print("\n" + "=" * 70)
    if not args.keep_open:
        backend.close_document(context.doc_id, save=False)
        session.forget(context.doc_id)
        print("The scratch part is closed and nothing was saved.")
    else:
        print("Left open, as asked.")
    print("Paste all of the above back. The line to look for is "
          "AddByPlaneAndTangent -- how many arguments, what they are called, "
          "and how many are optional. `AddByLinePlaneAndAngle` is the second: "
          "if its parameter names are listed, the angled plane should be "
          "naming them rather than trusting an argument order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
