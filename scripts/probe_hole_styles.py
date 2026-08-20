"""Ask Inventor what its hole methods actually take, one style at a time.

Every hole style is now built through Inventor's own hole feature, and the
argument order for those methods was taken from another project's field notes
rather than measured here. A wrong order can still *build* -- Inventor coerces
what it can -- so the backend reads the style back off the finished feature and
refuses to report a counterbore it cannot see. When that refusal fires, this is
the script that settles why:

    python scripts/probe_hole_styles.py
    python scripts/probe_hole_styles.py --only counterbore tap

It builds one 60x60x12 block and puts one hole of each style through it,
reporting for each:

* the method called and whether named arguments were accepted;
* the ``HoleTypeEnum`` value Inventor reports, against the value the constants
  table expects;
* the volume removed, against what the geometry says it should be -- which is
  what catches a counterbore that built as a plain hole *and* read back with the
  right enum;
* for a tap, the drill diameter Inventor chose from its thread table.

Nothing is saved. Paste the output into the issue or the commit message; the
numbers are the useful part.
"""

from __future__ import annotations

import argparse
import math
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inventor_mcp.backend.base import Driven, HoleRequest  # noqa: E402
from inventor_mcp.backend.com import holes  # noqa: E402
from inventor_mcp.builder import apply_operation  # noqa: E402
from inventor_mcp.schema import ExtrudeOp, SketchOp  # noqa: E402
from inventor_mcp.session import Session  # noqa: E402

#: The block every hole goes through, in mm.
BLOCK = 60.0
THICK = 12.0

#: One case per style, with the volume the geometry says it should remove, in
#: cm^3. Written out rather than computed in a loop so each number can be read
#: and disagreed with.
BORE = 6.6
CASES = [
    {
        "name": "drilled_through",
        "style": "drilled",
        "through_all": True,
        "removes": math.pi * (BORE / 20) ** 2 * (THICK / 10),
    },
    {
        "name": "drilled_blind",
        "style": "drilled",
        "through_all": False,
        "depth": 6.0,
        "removes": math.pi * (BORE / 20) ** 2 * 0.6,
    },
    {
        "name": "drilled_blind_pointed",
        "style": "drilled",
        "through_all": False,
        "depth": 6.0,
        "bottom_angle": 118.0,
        # A 118 degree drill point leaves a cone at the bottom rather than a
        # flat, so it removes *less* than the cylinder: the tip is shallower.
        "removes": math.pi * (BORE / 20) ** 2 * 0.6
        - math.pi * (BORE / 20) ** 2 * ((BORE / 20) / math.tan(math.radians(59))) / 3,
    },
    {
        "name": "counterbore_through",
        "style": "counterbore",
        "through_all": True,
        "cbore_diameter": 11.0,
        "cbore_depth": 6.6,
        "removes": math.pi * (BORE / 20) ** 2 * (THICK / 10)
        + math.pi * ((1.1 / 2) ** 2 - (BORE / 20) ** 2) * 0.66,
    },
    {
        "name": "spotface_through",
        "style": "spotface",
        "through_all": True,
        "cbore_diameter": 16.0,
        "cbore_depth": 1.0,
        "removes": math.pi * (BORE / 20) ** 2 * (THICK / 10)
        + math.pi * ((1.6 / 2) ** 2 - (BORE / 20) ** 2) * 0.1,
    },
    {
        "name": "countersink_through",
        "style": "countersink",
        "through_all": True,
        "csink_diameter": 13.2,
        "csink_angle": 90.0,
        # 90 degrees included, so the cone is as deep as (R - r).
        "removes": math.pi * (BORE / 20) ** 2 * (THICK / 10)
        + (
            math.pi * ((1.32 / 2) - (BORE / 20)) / 3
            * ((1.32 / 2) ** 2 + (1.32 / 2) * (BORE / 20) + (BORE / 20) ** 2)
            - math.pi * (BORE / 20) ** 2 * ((1.32 / 2) - (BORE / 20))
        ),
    },
    {
        "name": "tapped_through",
        "style": "drilled",
        "through_all": True,
        "tap": "M8x1.25",
        # Inventor picks the drill from its thread table, so this is the tapping
        # size for M8x1.25 and the point of the check is whether it agrees.
        "diameter": 6.75,
        "removes": math.pi * (0.675 / 2) ** 2 * (THICK / 10),
    },
]

#: How far the removed volume may differ before it counts as the wrong shape,
#: in cm^3. A counterbore built as a plain hole is out by 0.24, so this is loose
#: enough for a thread's helix and tight enough to catch that.
TOLERANCE = 0.02


def block(session, backend):
    """A fresh 60 x 60 x 12 block for the holes to go through."""
    document = backend.new_part("HoleProbe", units="mm")
    context = session.register(document, "mm", "deg")
    apply_operation(session, context, SketchOp(
        name="Block", plane="xy",
        entities=[{"type": "rectangle", "center": [0, 0],
                   "width": BLOCK, "height": BLOCK}],
    ))
    apply_operation(session, context, ExtrudeOp(
        name="Body", sketch="Block", distance=THICK))
    return context


def request_for(case: dict, index: int) -> HoleRequest:
    """One case as a request, with the hole placed clear of the others."""
    def mm(value: float | None) -> Driven | None:
        return None if value is None else Driven(f"{value} mm", value / 10)

    def deg(value: float | None) -> Driven | None:
        return None if value is None else Driven(f"{value} deg", math.radians(value))

    return HoleRequest(
        sketch=f"Centre{index}",
        diameter=mm(case.get("diameter", BORE)),
        depth=mm(case.get("depth")),
        through_all=bool(case["through_all"]),
        style=case["style"],
        cbore_diameter=mm(case.get("cbore_diameter")),
        cbore_depth=mm(case.get("cbore_depth")),
        csink_diameter=mm(case.get("csink_diameter")),
        csink_angle=deg(case.get("csink_angle")),
        bottom_angle=deg(case.get("bottom_angle")),
        tap=case.get("tap"),
        name=case["name"],
    )


def volume(session, context) -> float | None:
    """The current volume in cm^3, or None if it cannot be read."""
    try:
        return float(session.backend.mass_properties(context.doc_id).volume)
    except Exception:
        return None


def probe(session, backend, context, case: dict, index: int) -> bool:
    """Build one hole and report what Inventor made of it."""
    print(f"\n--- {case['name']}")
    if case.get("bottom_angle") and backend.name == "mock":
        print("  skipped: the simulator does not model a drill point's cone")
        return True
    # Each hole gets its own centre sketch, laid out along X so they do not
    # overlap: a hole that fails is easier to look at than a hole that merged.
    offset = (index - len(CASES) / 2 + 0.5) * BLOCK / (len(CASES) + 1)
    apply_operation(session, context, SketchOp(
        name=f"Centre{index}", plane="xy",
        entities=[{"type": "point", "position": [offset, 0], "hole_center": True}],
    ))

    request = request_for(case, index)
    before = volume(session, context)
    try:
        info = backend.hole(context.doc_id, request)
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        hint = getattr(exc, "hint", None)
        if hint:
            print(f"  hint: {hint}")
        return False

    detail = info.detail or {}
    print(f"  method   {detail.get('method') or 'n/a (the simulator calls nothing)'}")
    after = volume(session, context)
    removed = None if before is None or after is None else before - after
    wanted = case["removes"]
    if removed is None:
        print("  volume   could not be measured")
        ok = False
    else:
        drift = removed - wanted
        verdict = "ok" if abs(drift) <= TOLERANCE else "WRONG SHAPE"
        print(f"  removed  {removed:.4f} cm^3   expected {wanted:.4f}   "
              f"({drift:+.4f})  {verdict}")
        ok = abs(drift) <= TOLERANCE

    feature = _find(backend, context, case["name"])
    if feature is not None and backend.name != "mock":
        from inventor_mcp.backend.com.backend import _hole_diameter

        reported = getattr(feature, "HoleType", None)
        expected = None
        try:
            expected = backend._k(holes.STYLE_ENUM[case["style"]])
        except Exception:
            pass
        print(f"  HoleType {reported}   table says "
              f"{holes.STYLE_ENUM[case['style']]} = {expected}")
        if case.get("tap"):
            print(f"  Tapped   {getattr(feature, 'Tapped', None)}")
            drilled = _hole_diameter(feature)
            if drilled is not None:
                print(f"  drilled  {drilled * 10:.4f} mm   recipe said "
                      f"{case.get('diameter', BORE)}")
    for note in detail.get("notes") or []:
        print(f"  note     {note}")
    return ok


def _find(backend, context, name: str):
    """The raw COM feature, so its properties can be read back directly."""
    try:
        features = backend._doc(context.doc_id).ComponentDefinition.Features
        for index in range(1, int(features.Count) + 1):
            feature = features.Item(index)
            if str(feature.Name) == name:
                return feature
    except Exception:
        return None
    return None


def thread_tables(backend, context) -> None:
    """Which thread tables and designations this installation will accept."""
    print("\n--- CreateTapInfo")
    if backend.name == "mock":
        print("  skipped: the simulator has no thread table")
        return
    features = backend._doc(context.doc_id).ComponentDefinition.Features.HoleFeatures
    for thread_type, designation, thread_class in [
        ("ANSI Metric M Profile", "M8x1.25", "6H"),
        ("ANSI Metric M Profile", "M8", "6H"),
        ("ISO Metric profile", "M8x1.25", "6H"),
        ("ANSI Unified Screw Threads", "1/4-20 UNC", "2B"),
        ("NPT", "1/8", "-"),
        ("BSP", "G1/4", "-"),
    ]:
        try:
            features.CreateTapInfo(True, thread_type, designation, thread_class, True)
            print(f"  ok      {thread_type!r:32} {designation!r:14} {thread_class!r}")
        except Exception as exc:
            message = str(exc).splitlines()[0][:90]
            print(f"  refused {thread_type!r:32} {designation!r:14} {thread_class!r}"
                  f"\n            {message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", default=[],
                        help="Run only cases whose name contains one of these. "
                             "'tap' also selects the thread-table probe.")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--backend", default="inventor", choices=["inventor", "mock"],
                        help="'mock' exercises this script's plumbing and case "
                             "set-up without Inventor, which is worth doing before "
                             "spending a live run on a typo. It does not check the "
                             "geometry: the simulator computes these volumes from "
                             "the same reasoning the expectations do.")
    args = parser.parse_args(argv)

    def wanted(name: str) -> bool:
        return not args.only or any(part.lower() in name.lower() for part in args.only)

    session = Session(backend_kind=args.backend)
    backend = session.ensure_backend()
    try:
        info = backend.connect(visible=True, create=True)
    except Exception as exc:
        print(f"Could not reach Inventor: {exc}")
        return 1
    print(f"Inventor {info.version} via the {backend.name} backend")
    print(f"Block {BLOCK:.0f} x {BLOCK:.0f} x {THICK:.0f} mm, bore {BORE} mm")

    context = block(session, backend)
    results: list[tuple[str, bool]] = []
    for index, case in enumerate(CASES):
        if not wanted(case["name"]):
            continue
        try:
            results.append((case["name"], probe(session, backend, context, case, index)))
        except Exception:
            results.append((case["name"], False))
            traceback.print_exc(limit=4)

    if wanted("tap"):
        try:
            thread_tables(backend, context)
        except Exception:
            traceback.print_exc(limit=3)

    print("\n" + "=" * 70)
    for name, ok in results:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    print("=" * 70)
    if not args.keep_open:
        try:
            backend.close_document(context.doc_id, save=False)
        except Exception:
            pass
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
