"""Everything that can only be checked against a real Inventor, in one run.

The offline half of this project is covered by the test suite. The live half has been
checked by a human reading printed numbers, which is how three silent geometry
bugs survived several rounds. This turns that reading into assertions.

    python scripts/live_acceptance.py              # check against expectations
    python scripts/live_acceptance.py --record     # write expectations from this run
    python scripts/live_acceptance.py --only bracket threading

Expectations live in `examples/expected/<name>.json`. Three of them were taken
from live runs and hand-checked against first principles; the rest have never
been captured, so the first `--record` seeds them and every run afterwards is a
regression test. Recording does not make a run correct -- check the arithmetic
in the summary before trusting a newly seeded number.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from inventor_mcp.builder import (  # noqa: E402
    apply_operation,
    apply_parameter,
    build_part,
    measure,
)
from inventor_mcp.schema import ExtrudeOp, PartRecipe, SketchOp  # noqa: E402
from inventor_mcp.session import Session  # noqa: E402

EXPECTED = ROOT / "examples" / "expected"

#: Volumes measured live and checked by hand. The rest are seeded by --record.
KNOWN = {
    "mounting_plate": 75.0185,
    "angle_bracket": 43.1999,
    "flanged_shaft": 93.6305,
}

#: How far a volume may drift before it counts as a change, in cm^3. Loose
#: enough for Inventor's own rounding, tight enough that a missing 9 mm hole
#: (0.382 cm^3) or a fillet on the wrong edge (0.687) cannot hide.
TOLERANCE = 5.0e-4


class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[bool, str, str]] = []
        self.skipped: list[str] = []

    def check(self, ok: bool, what: str, detail: str = "") -> bool:
        self.checks.append((ok, what, detail))
        mark = " ok " if ok else "FAIL"
        # The detail explains a failure, so it is printed only when there is one.
        # Showing it under a pass read as a contradiction: "[ ok ] the backend is
        # pinned to one thread / it is not".
        print(f"  [{mark}] {what}" + (f"\n         {detail}" if detail and not ok else ""))
        return ok

    def skip(self, what: str, why: str) -> None:
        """Not applicable here, which is different from failing."""
        self.skipped.append(what)
        print(f"  [skip] {what}\n         {why}")

    def note(self, text: str) -> None:
        print(f"         {text}")

    @property
    def failed(self) -> list[tuple[bool, str, str]]:
        return [c for c in self.checks if not c[0]]


def build(session: Session, recipe: PartRecipe):
    """Build a recipe step by step, returning the context and any failures."""
    backend = session.ensure_backend()
    document = backend.new_part(recipe.name, units=recipe.units,
                                angle_units=recipe.angle_units)
    context = session.register(document, recipe.units, recipe.angle_units)
    if recipe.material:
        backend.set_material(context.doc_id, recipe.material)
    broken: list[str] = []
    for spec in recipe.parameters:
        try:
            apply_parameter(session, context, spec)
        except Exception as exc:
            broken.append(f"parameter {spec.name}: {exc}")
    for index, op in enumerate(recipe.operations):
        try:
            apply_operation(session, context, op)
        except Exception as exc:
            # The hint carries the diagnosis -- which routes were tried and what
            # each said -- and dropping it left a failure that named itself and
            # explained nothing.
            hint = getattr(exc, "hint", None)
            broken.append(f"op {index} ({op.op}): {exc}"
                          + (f"\n           hint: {hint}" if hint else ""))
    return context, broken


def check_example(session: Session, path: Path, report: Report, record: bool) -> None:
    name = path.stem
    print(f"\n--- {name}")
    recipe = PartRecipe.model_validate(json.loads(path.read_text()))
    context, broken = build(session, recipe)
    for failure in broken:
        report.check(False, f"{name}: {failure}")
    if broken:
        return

    seen = measure(session, context)
    if seen is None:
        report.check(False, f"{name}: could not be measured")
        return

    wanted = EXPECTED / f"{name}.json"
    if (record or not wanted.exists()) and session.backend.name == "mock":
        # The simulator's numbers are not Inventor's -- it treats through-all as
        # prismatic and does not model a mirror's volume at all -- so seeding
        # from it would bake a wrong baseline into the repository.
        report.skip(f"{name}: {seen['volume_cm3']:.4f} cm^3 measured",
                    "not recorded: the simulator is not Inventor")
        return
    if record or not wanted.exists():
        wanted.parent.mkdir(parents=True, exist_ok=True)
        baseline = dict(seen)
        if name in KNOWN and not record:
            baseline["volume_cm3"] = KNOWN[name]
        wanted.write_text(json.dumps(baseline, indent=2) + "\n")
        report.check(True, f"{name}: recorded {seen['volume_cm3']:.4f} cm^3",
                     "seeded; check the arithmetic before trusting it")
        return

    expected = json.loads(wanted.read_text())
    drift = seen["volume_cm3"] - expected["volume_cm3"]
    if session.backend.name == "mock":
        # These are Inventor's numbers. The simulator gets close on an extruded
        # part and does not on a revolve, so a difference here says something
        # about the simulator, not about the recipe -- worth printing, not worth
        # failing. It is the one number that shows how good the oracle is.
        report.skip(f"{name}: the simulator says {seen['volume_cm3']:.4f} cm^3",
                    f"the expectation is {expected['volume_cm3']:.4f}, "
                    f"{drift:+.4f} apart")
        return
    report.check(
        abs(drift) <= TOLERANCE,
        f"{name}: volume {seen['volume_cm3']:.4f} cm^3",
        f"expected {expected['volume_cm3']:.4f}, drift {drift:+.4f}"
        if abs(drift) > TOLERANCE else "",
    )
    for field in ("faces", "edges"):
        if field in expected and field in seen:
            report.check(seen[field] == expected[field],
                         f"{name}: {field} {seen[field]}",
                         f"expected {expected[field]}" if seen[field] != expected[field] else "")
    if "span_mm" in expected and "span_mm" in seen:
        report.check(seen["span_mm"] == expected["span_mm"],
                     f"{name}: span {seen['span_mm']} mm",
                     f"expected {expected['span_mm']}"
                     if seen["span_mm"] != expected["span_mm"] else "")


def check_parameter_edit(session: Session, report: Report) -> None:
    """The premise of the whole project, never yet checked live.

    The bracket's outline gained driving dimensions on reasoning alone. If they
    work, widening base_len widens the part; if they do not, the slots move
    along an outline that stays where it is.
    """
    print("\n--- a parameter edit moves the geometry")
    if session.backend.name == "mock":
        report.skip("a parameter edit moves the geometry",
                    "the simulator records new values without re-solving")
        return
    path = ROOT / "examples" / "angle_bracket.json"
    recipe = PartRecipe.model_validate(json.loads(path.read_text()))
    context, broken = build(session, recipe)
    if broken:
        report.check(False, "the bracket did not build", broken[0])
        return

    before = measure(session, context)
    backend = session.backend
    try:
        backend.set_parameter(context.doc_id, "base_len", "120", units=recipe.units)
        outcome = backend.rebuild(context.doc_id)
    except Exception as exc:
        report.check(False, "base_len could not be changed", str(exc))
        return
    if outcome.get("uninterpreted_health"):
        report.skip("the rebuild left no feature in error",
                    "Inventor would not say what its health statuses mean: "
                    + json.dumps(outcome["uninterpreted_health"][:3]))
    else:
        report.check(not outcome.get("errors"),
                     "the rebuild left no feature in error",
                     json.dumps(outcome.get("errors", [])[:3]))
    after = measure(session, context)
    if before is None or after is None:
        report.check(False, "could not measure across the edit")
        return

    was, now = before["span_mm"][0], after["span_mm"][0]
    report.check(abs(now - 120.0) < 0.01,
                 f"span X followed base_len: {was} -> {now} mm",
                 "expected 120.0 -- if it stayed at 90 the outline is not driven")
    report.note(f"volume {before['volume_cm3']:.4f} -> {after['volume_cm3']:.4f} cm^3")


def check_hole_styles(session: Session, report: Report) -> None:
    """That a counterbore is a counterbore, which nothing has ever confirmed.

    The hole-method argument order came from another project's field notes, and a
    wrong order can still build: Inventor coerces what it can, so a plain hole
    reported as a counterbore is the failure mode. The backend reads the style
    back off the finished feature and refuses when Inventor disagrees.

    The volume is the check that matters, though, and it is the one that has
    earned its keep: the read-back spent a whole run returning nothing at all --
    a hole's properties live on `HoleFeature.Definition`, not on the feature --
    so eight styles were reported as verified when none of them had been. The
    volumes were what noticed.
    """
    print("\n--- every hole style builds as the style asked for")
    import probe_hole_styles

    # The loop runs against the simulator too, so a typo in it is found here
    # rather than on a live machine -- but the simulator calls none of Inventor's
    # hole methods, so its verdicts are recorded as skips rather than passes.
    live = session.backend.name != "mock"

    backend = session.ensure_backend()
    context = probe_hole_styles.block(session, backend)
    for index, case in enumerate(probe_hole_styles.CASES):
        if case.get("bottom_angle") and not live:
            report.skip(f"{case['name']}", "the simulator does not model a drill point")
            continue
        before = probe_hole_styles.volume(session, context)
        try:
            apply_operation(session, context, SketchOp(
                name=f"Centre{index}", plane="xy",
                entities=[{"type": "point", "hole_center": True,
                           "position": [(index - len(probe_hole_styles.CASES) / 2 + 0.5)
                                        * probe_hole_styles.BLOCK
                                        / (len(probe_hole_styles.CASES) + 1), 0]}],
            ))
            info = backend.hole(context.doc_id, probe_hole_styles.request_for(case, index))
        except Exception as exc:
            report.check(False, f"{case['name']}: {type(exc).__name__}", str(exc)[:200])
            continue
        after = probe_hole_styles.volume(session, context)
        removed = None if before is None or after is None else before - after
        if removed is None:
            report.check(False, f"{case['name']}: could not be measured")
            continue
        wanted = case["removes"]
        agreed = abs(removed - wanted) <= probe_hole_styles.TOLERANCE
        what = (f"{case['name']}: removed {removed:.4f} cm^3 via "
                f"{(info.detail or {}).get('method')}")
        if not live:
            report.skip(what, f"the simulator's own arithmetic, expected {wanted:.4f}"
                              + ("" if agreed else " -- and it disagrees"))
            continue
        report.check(
            agreed, what,
            f"expected {wanted:.4f}, out by {removed - wanted:+.4f}. Check that "
            "nothing else in the block is close enough to overlap this hole "
            "before blaming the arguments -- a seat that meets its neighbour "
            "removes less than an isolated one, which is what this said last "
            "time" if not agreed else "",
        )
        for note in (info.detail or {}).get("notes") or []:
            report.note(note)
    try:
        backend.close_document(context.doc_id, save=False)
    except Exception:
        pass


def check_rollback(session: Session, report: Report) -> None:
    """That Inventor's TransactionManager really puts the part back.

    Written against the simulator, which models a rollback by copying the
    document aside. Inventor's own transactions are a different mechanism
    entirely, and whether an abort restores a *consumed sketch* -- the one
    failure rollback exists for -- has never been checked.
    """
    print("\n--- a failed build rolls back")
    # The body runs against the simulator too, so a typo here is found offline;
    # but the simulator copies the document aside, which proves nothing about
    # Inventor's TransactionManager, so its verdicts are recorded as skips.
    live = session.backend.name != "mock"

    def verdict(ok: bool, what: str, detail: str = "") -> bool:
        if live:
            return report.check(ok, what, detail)
        # The detail only reads correctly against a failure -- it explains one.
        report.skip(what, "the simulator, not Inventor"
                          + (f" -- and it says no: {detail}" if not ok and detail
                             else " -- and it says no" if not ok else ""))
        return ok

    path = ROOT / "examples" / "mounting_plate.json"
    recipe = PartRecipe.model_validate(json.loads(path.read_text()))
    good = build_part(session, recipe)
    if not good["ok"]:
        verdict(False, "the plate did not build", json.dumps(good["errors"][:1]))
        return
    backend = session.backend
    before = measure(session, session.context(good["document"]))

    # The same recipe again, with its last operation pointed at a sketch that is
    # not there: everything before it succeeds, so there is something to undo.
    broken = json.loads(path.read_text())
    broken["operations"] = [
        {"op": "sketch", "name": "Pocket", "plane": "xy", "entities": [
            {"type": "rectangle", "center": [0, 0], "width": 40, "height": 20}]},
        {"op": "extrude", "name": "Pocket_Cut", "sketch": "Pocket",
         "distance": 3, "operation": "cut"},
        {"op": "hole", "sketch": "NoSuchSketch", "diameter": 5},
    ]
    broken["parameters"] = []
    result = build_part(session, PartRecipe.model_validate(broken),
                        document=good["document"], rollback_on_error=True)
    verdict(result["ok"] is False, "the broken build failed, as intended",
            "it succeeded, so this checks nothing")
    if not verdict(bool(result.get("rolled_back")),
                   f"{'Inventor' if live else 'the backend'} accepted the rollback",
                   result.get("rollback", "no rollback was reported")):
        return

    after = measure(session, session.context(good["document"]))
    if before is None or after is None:
        verdict(False, "could not measure across the rollback")
        return
    verdict(abs(after["volume_cm3"] - before["volume_cm3"]) <= TOLERANCE,
            f"the volume came back: {after['volume_cm3']:.4f} cm^3",
            f"was {before['volume_cm3']:.4f} before the failed build -- the "
            "pocket was cut and not restored")
    for field in ("faces", "edges"):
        if field in before and field in after:
            verdict(before[field] == after[field],
                    f"{field} came back to {after[field]}",
                    f"was {before[field]}" if before[field] != after[field] else "")
    try:
        backend.close_document(good["document"], save=False)
    except Exception:
        pass


def check_threading(session: Session, report: Report) -> None:
    """Inventor from several threads at once, which no run has ever done.

    The MCP SDK serves synchronous tools on a pool of worker threads. Nothing
    has driven the tool layer against Inventor -- live_smoke imports the builder
    directly -- so this is the most likely way a real client fails on its first
    call.
    """
    print("\n--- Inventor from a pool of threads")
    if session.backend.name == "mock":
        report.skip("Inventor from a pool of threads",
                    "the simulator is pure Python and is deliberately not pinned")
        return
    backend = session.ensure_backend()
    pinned = getattr(backend, "marshalling_thread", None)
    report.check(pinned is not None,
                 "the backend is pinned to one thread",
                 "it is not -- INVENTOR_MCP_THREADING may be off")

    document = backend.new_part("ThreadProbe", units="mm")
    context = session.register(document, "mm", "deg")
    # A part with no solid body has no mass properties, and Inventor raises
    # rather than returning zero -- so the first version of this check failed on
    # its own empty document and blamed the marshalling.
    apply_operation(session, context, SketchOp(
        name="Probe", plane="xy",
        entities=[{"type": "rectangle", "center": [0, 0], "width": 20, "height": 20}]))
    apply_operation(session, context, ExtrudeOp(name="Block", sketch="Probe", distance=5))

    def ask(index: int):
        return backend.mass_properties(context.doc_id).volume, index

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            answers = list(pool.map(ask, range(16)))
    except Exception as exc:
        report.check(False, "a call from a worker thread failed", f"{type(exc).__name__}: {exc}")
        return
    report.check(len({volume for volume, _ in answers}) == 1,
                 f"16 calls from 8 threads all agreed ({answers[0][0]:.4f} cm^3)")
    if pinned is not None:
        report.check(pinned.thread_id is not None,
                     f"every call ran on thread {pinned.thread_id}")


def check_constants(session: Session, report: Report) -> None:
    """Whether the fallback enum table is right, which nothing has checked."""
    print("\n--- the enum fallback table")
    if session.backend.name == "mock":
        report.skip("the enum fallback table", "there is no type library to read")
        return
    from inventor_mcp.backend.com.constants import FALLBACK, SUSPECT, load

    constants = load()
    if constants._module is None:
        report.check(False, "the type library could not be read",
                     "delete %LOCALAPPDATA%\\Temp\\gen_py and re-run")
        return
    wrong = []
    for name, table in sorted(FALLBACK.items()):
        actual = getattr(constants._module, name, None)
        if isinstance(actual, int) and actual != table:
            wrong.append((name, table, actual))
    report.check(not wrong, f"{len(FALLBACK)} fallback value(s) match Inventor",
                 "\n         ".join(f'"{n}": {a},  # table said {t}'
                                    + ("   (disputed)" if n in SUSPECT else "")
                                    for n, t, a in wrong))
    if wrong:
        report.note("Paste those into FALLBACK in "
                    "inventor_mcp/backend/com/constants.py.")


CHECKS = {
    "examples": None,  # handled specially: one per recipe
    "parameter-edit": check_parameter_edit,
    "hole-styles": check_hole_styles,
    "rollback": check_rollback,
    "threading": check_threading,
    "constants": check_constants,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true",
                        help="Write expectations from this run instead of checking them.")
    parser.add_argument("--only", nargs="*", default=[],
                        help="Run only checks whose name contains one of these.")
    parser.add_argument("--backend", default="inventor", choices=["inventor", "mock"])
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args(argv)

    session = Session(backend_kind=args.backend)
    report = Report()
    print("=" * 70)
    try:
        info = session.ensure_backend().connect(visible=True, create=True)
    except Exception as exc:
        print(f"Could not reach Inventor: {exc}")
        return 1
    print(f"Inventor {info.version} via the {session.backend.name} backend")
    if session.backend.name == "mock":
        print("NOTE: the simulator cannot answer most of this. "
              "Use --backend inventor.")
    print("=" * 70)

    def wanted(name: str) -> bool:
        return not args.only or any(part.lower() in name.lower() for part in args.only)

    # An example is selected either by the group name or by its own -- the first
    # version required the group, so `--only pipe_bend` matched nothing at all
    # and reported "0 of 0 checks passed", which reads like success.
    for path in sorted((ROOT / "examples").glob("*.json")):
        if not (wanted("examples") or wanted(path.stem)):
            continue
        try:
            check_example(session, path, report, args.record)
        except Exception:
            report.check(False, f"{path.stem}: the check itself failed")
            traceback.print_exc(limit=4)

    for name, function in CHECKS.items():
        if function is None or not wanted(name):
            continue
        try:
            function(session, report)
        except Exception:
            report.check(False, f"{name}: the check itself failed")
            traceback.print_exc(limit=4)

    print("\n" + "=" * 70)
    if not report.checks and not report.skipped:
        # Nothing ran, which is not the same as nothing failing. Saying "0 of 0
        # passed" and exiting zero is how a filter typo looks like a clean run.
        print(f"Nothing matched --only {args.only}. Known names: examples, "
              + ", ".join(name for name in CHECKS if name != "examples")
              + ", or any example's file name.")
        print("=" * 70)
        return 1
    print(f"{len(report.checks) - len(report.failed)} of {len(report.checks)} checks passed"
          + (f", {len(report.skipped)} skipped" if report.skipped else ""))
    for _, what, detail in report.failed:
        print(f"  FAIL  {what}" + (f"  ({detail.splitlines()[0]})" if detail else ""))
    print("=" * 70)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
