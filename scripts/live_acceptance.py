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
import math
import shutil
import sys
import traceback
from collections.abc import Sequence
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

#: Recipes that exist only to put a number on an estimate nobody has measured.
#: Kept out of `examples/` proper: they are instruments, not parts anybody wants.
CALIBRATION = ROOT / "examples" / "calibration"

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
        self.recorded: list[str] = []

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

    def seeded(self, what: str, why: str) -> None:
        """A baseline was written, not checked. Neither a pass nor a failure."""
        self.recorded.append(what)
        print(f"  [seed] {what}\n         {why}")

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
        # Not report.check. Seeding a baseline compares nothing -- it writes down
        # whatever this run produced and agrees with it -- so counting it as a
        # passed check makes a first run on a new machine read as a verification
        # it has not performed. "65 of 65 checks passed" said exactly that about
        # two examples that had never been checked against anything.
        report.seeded(f"{name}: recorded {seen['volume_cm3']:.4f} cm^3",
                      "nothing was compared; check the arithmetic before trusting it")
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


def _families(names: list[str]) -> list[str]:
    """Search stems for `--find`, from the names themselves.

    ``kBothShellDirection`` is worth asking about as "Shell": the enum is there,
    and it is the member this release spells differently. Printing the whole name
    would find nothing, which is what the run already told us.
    """
    stems = []
    for name in names:
        body = name[1:] if name.startswith("k") else name
        for word in ("Shell", "Bottom", "Rendering", "Render", "Direction",
                     "Hole", "Extent", "Operation", "Dim", "Surface", "Curve"):
            if word in body and word not in stems:
                stems.append(word)
                break
        else:
            if body not in stems:
                stems.append(body)
    return stems


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
    wrong, absent = [], []
    for name, table in sorted(FALLBACK.items()):
        actual = getattr(constants._module, name, None)
        if not isinstance(actual, int):
            absent.append(name)
        elif actual != table:
            wrong.append((name, table, actual))
    # Counted, not assumed. The first version tested only `actual != table` and
    # printed len(FALLBACK) either way, so a name this release does not have at
    # all -- where `actual` is None and there is nothing to compare -- passed,
    # and the line read "51 fallback value(s) match Inventor" on a machine where
    # four of them had never been read. That is the failure mode this whole
    # script exists to remove.
    checked = len(FALLBACK) - len(absent)
    report.check(not wrong, f"{checked} of {len(FALLBACK)} fallback value(s) "
                 f"match Inventor",
                 "\n         ".join(f'"{n}": {a},  # table said {t}'
                                    + ("   (disputed)" if n in SUSPECT else "")
                                    for n, t, a in wrong))
    if wrong:
        report.note("Paste those into FALLBACK in "
                    "inventor_mcp/backend/com/constants.py.")
    report.check(not absent,
                 "every fallback name exists in this release's type library",
                 "this release has no such name, so the table value is what would "
                 "be used and it has never been verified here:\n         "
                 + ", ".join(absent)
                 + "\n         Ask Inventor what it does call them: "
                 + " ".join(f"python scripts/dump_constants.py --find {stem}"
                            for stem in _families(absent)))


def check_dfm(session: Session, report: Report) -> None:
    """The DFM loop, end to end, on the housing that was built to exercise it.

    This is the only check here that needs something outside Inventor: a checkout
    of the DFM analyser and Node. Both are skipped clearly rather than silently
    when absent -- a check that passes because it did not run is worse than no
    check.

    What is being verified is the loop's central claim, which is not "the score
    went up" but "the findings it acted on are gone when measured again". So the
    assertions are about what the second measurement says, and about the part
    still being the part: the frozen pilot hole for the M3 screw has to come out
    the same size it went in.
    """
    from inventor_mcp.dfm.loop import current_parameters, improve
    from inventor_mcp.dfm.runner import DfmUnavailable, find_dfm_root

    if session.backend.name == "mock":
        report.skip("dfm: the loop",
                    "the simulator does not write CAD files, and the analyser needs "
                    "a mesh -- run with --backend inventor")
        return
    try:
        root = find_dfm_root()
    except DfmUnavailable as exc:
        report.skip("dfm: the analyser", f"{exc.message} {exc.hint or ''}")
        return
    if shutil.which("node") is None:
        report.skip("dfm: node", "Node is not installed, and the analyser is JavaScript")
        return
    report.note(f"DFM analyser at {root}")

    recipe = PartRecipe.model_validate(
        json.loads((ROOT / "examples" / "moulded_housing.json").read_text(encoding="utf-8"))
    )
    built = build_part(session, recipe, against_rehearsal=False)
    if not report.check(bool(built.get("ok")), "dfm: the housing builds",
                        str(built.get("errors"))[:200]):
        return
    context = session.context()

    before, _ = current_parameters(session, context)
    workspace = ROOT / ".dfm"
    result = improve(session, context, rounds=3, workspace=str(workspace))

    first, last = result.rounds[0], result.rounds[-1]
    report.note(f"dfm: {first.score} {first.grade} -> {last.score} {last.grade}"
                f" over {len(result.rounds) - 1} round(s); stopped because "
                f"{result.stopped_because}")
    for entry in result.rounds[1:]:
        for change in entry.applied:
            report.note(f"  round {entry.number}: {change.parameter} "
                        f"{change.was} -> {change.expression}  ({change.check})")
        if entry.cleared:
            report.note(f"  round {entry.number}: cleared {', '.join(entry.cleared)}")
        if entry.persisted:
            report.note(f"  round {entry.number}: still reported "
                        f"{', '.join(entry.persisted)}")

    report.check(first.score is not None, "dfm: the analyser produced a score",
                 str(first.score))

    # The housing is deliberately wrong in ways a parameter answers: the boss
    # wall is 0.9x the nominal wall, and a rib is 0.72x it.
    report.check("ribs" in first.findings, "dfm: the ribs check finds the boss wall",
                 f"findings were {first.findings}")

    acted = [c for entry in result.rounds[1:] for c in entry.applied]
    report.check(bool(acted), "dfm: it changed at least one parameter",
                 result.stopped_because)

    cleared = {key for entry in result.rounds[1:] for key in entry.cleared}
    answered = {c.check for c in acted}
    report.check(
        not answered or bool(cleared & answered),
        "dfm: a finding it acted on actually cleared when measured again",
        f"acted on {sorted(answered)}, cleared {sorted(cleared)}",
    )

    after, _ = current_parameters(session, context)
    report.check(
        after.get("boss_hole_d") == before.get("boss_hole_d"),
        "dfm: the frozen M3 pilot hole is untouched",
        f"{before.get('boss_hole_d')} -> {after.get('boss_hole_d')}",
    )
    report.check(
        after.get("cable_w") == before.get("cable_w"),
        "dfm: the frozen cable entry is untouched",
        f"{before.get('cable_w')} -> {after.get('cable_w')}",
    )
    report.check(
        (last.score or 0) >= (first.score or 0),
        "dfm: the part is not left worse than it started",
        f"{first.score} -> {last.score}",
    )

    # The boss diameter is derived from the boss wall, so fixing the wall has to
    # have resized the boss with it. A boss that did not move is an expression
    # that was overwritten somewhere.
    if after.get("boss_wall") != before.get("boss_wall"):
        report.check(
            after.get("boss_d") != before.get("boss_d"),
            "dfm: the derived boss diameter followed its wall",
            f"boss_wall {before.get('boss_wall')} -> {after.get('boss_wall')}, "
            f"boss_d {before.get('boss_d')} -> {after.get('boss_d')}",
        )

    outstanding = [d for d in result.outstanding if d.reason in ("decision", "unverifiable")]
    if outstanding:
        report.note("dfm: left for a person -- "
                    + "; ".join(f"{d.check} ({d.reason})" for d in outstanding))


def check_from_a_file(session: Session, report: Report) -> None:
    """Handing over a file, which is the path a person actually takes.

    Four things that cannot be checked without Inventor, and all four are things
    this project has never done before: whether a filesystem copy of an .ipt
    opens (a copy keeps the original's internal identity, and Inventor might
    notice), whether an opened part reports its real units, whether a custom
    property survives a save and a reopen, and whether role discovery has
    anything to read on a live feature.
    """
    if session.backend.name == "mock":
        report.skip("file: the whole check",
                    "needs real files and a real translator -- run with "
                    "--backend inventor")
        return

    from inventor_mcp.dfm.declaration import Declaration, read_sidecar
    from inventor_mcp.dfm.sources import discover_for, remember, resolve
    from inventor_mcp.versioning import versions_of, working_copy

    recipe = PartRecipe.model_validate(
        json.loads((ROOT / "examples" / "moulded_housing.json").read_text(encoding="utf-8"))
    )
    built = build_part(session, recipe, against_rehearsal=False)
    if not report.check(bool(built.get("ok")), "file: the housing builds",
                        str(built.get("errors"))[:200]):
        return
    context = session.context()

    room = ROOT / ".dfm"
    room.mkdir(parents=True, exist_ok=True)
    original = room / "handed_over.ipt"
    try:
        session.backend.save_document(context.doc_id, str(original))
    except Exception as exc:
        report.check(False, "file: saving a part to a known path", str(exc)[:160])
        return
    report.check(original.is_file(), "file: the part is on disk", str(original))

    # What the part says about itself, before anything is declared.
    found = discover_for(session, context)
    report.check(
        found.declaration.roles.get("wall") == "wall",
        "file: the shell names the wall on a live part",
        f"discovered {found.declaration.roles}; evidence "
        f"{found.declaration.evidence.get('wall', '(none)')}",
    )
    report.check(
        found.declaration.roles.get("draft") == "draft_a",
        "file: the extrude's taper names the draft",
        f"evidence {found.declaration.evidence.get('draft', '(none)')}",
    )
    if found.ambiguous:
        report.note(f"file: ambiguous roles {sorted(found.ambiguous)}")
    report.note(f"file: suggested by name, not used: {found.suggestions}")

    # A declaration written into the part, and read back after a round trip.
    declaration = Declaration(roles={"wall": "wall"}, frozen=["boss_hole_d"],
                              settings={"material": "abs"})
    written = remember(session, context, declaration, path=original)
    report.note(f"file: remembered {written}")
    report.check(bool(written.get("sidecar")), "file: the sidecar is written",
                 str(written.get("sidecar")))
    stored = written.get("in_the_part")
    if stored is None:
        report.skip("file: a declaration inside the part",
                    "the backend did not offer to store one")
    else:
        report.check(bool(stored), "file: a declaration goes into the part",
                     str(written.get("why_not", ""))[:160])

    session.backend.close_document(context.doc_id, save=True)
    session.forget(context.doc_id)

    # The working copy, and whether Inventor will open one.
    try:
        copy = working_copy(original)
    except Exception as exc:
        report.check(False, "file: making a working copy", str(exc)[:160])
        return
    # The NEXT version, not a fixed name: leftovers from an earlier run mean
    # v3 or v9, and refusing to reuse a name is the tool working, not failing.
    import re as _re
    report.check(bool(_re.fullmatch(r"handed_over_v\d+\.ipt", copy.name)),
                 "file: the copy is a fresh next version", copy.name)
    report.check(read_sidecar(copy) is not None,
                 "file: the declaration travels with the copy",
                 f"looked beside {copy}")

    try:
        info = session.backend.open_document(str(copy))
    except Exception as exc:
        report.check(False, "file: Inventor opens a filesystem copy", str(exc)[:200])
        return
    reopened = session.register(info, info.units, info.angle_units)
    session.sync_parameters(reopened.doc_id)
    report.check(True, "file: Inventor opens a filesystem copy",
                 f"{info.name} units={info.units}")
    if info.detail:
        report.note(f"file: on opening -- {info.detail}")

    parameters = session.backend.list_parameters(reopened.doc_id)
    report.check(len(parameters) > 5, "file: the copy has its parameters",
                 f"{len(parameters)} found")

    resolved, _ = resolve(session, reopened, path=copy)
    report.check(resolved.roles.get("wall") == "wall",
                 "file: the reopened part still knows what its wall is",
                 f"{resolved.roles} from {resolved.origin}")

    # Freezing a feature pins its parameters, traced on the live model. The
    # housing's Bosses are driven by boss_h directly and boss_inset through
    # their sketch, so both have to come back pinned.
    traced = session.backend.feature_dependencies(reopened.doc_id, "Bosses")
    if traced is None:
        report.check(False, "file: the backend traces a feature's parameters",
                     "feature_dependencies returned None on the live backend")
    else:
        report.note(f"file: Bosses is driven by {traced['parameters']}")
        report.check("boss_h" in traced["parameters"],
                     "file: a frozen feature pins its own driven property",
                     str(traced["parameters"]))
        report.check("boss_inset" in traced["parameters"],
                     "file: and the dimensions of the sketch it consumes",
                     str(traced["parameters"]))
    report.check("boss_hole_d" in resolved.frozen,
                 "file: and what it may not change",
                 f"frozen {resolved.frozen}")
    report.note(f"file: read the roles from {set(resolved.origin.values())}")
    report.note(f"file: versions on disk -- "
                f"{[p.name for p in versions_of(original)]}")

    session.backend.close_document(reopened.doc_id, save=False)
    report.note(f"file: delete {room} when you are done")


def check_import(session: Session, report: Report) -> None:
    """Importing a STEP file, which needs a STEP file to import.

    Written round the fact that this repository has no STEP file to ship: one is
    exported from the housing first, so the check is self-contained, and a
    round trip through STEP is a fair test of the import anyway.
    """
    if session.backend.name == "mock":
        report.skip("import: STEP", "the simulator has no translator")
        return

    from inventor_mcp.backend.base import ExportRequest

    recipe = PartRecipe.model_validate(
        json.loads((ROOT / "examples" / "moulded_housing.json").read_text(encoding="utf-8"))
    )
    built = build_part(session, recipe, against_rehearsal=False)
    if not report.check(bool(built.get("ok")), "import: a part to export",
                        str(built.get("errors"))[:160]):
        return
    context = session.context()
    room = ROOT / ".dfm"
    room.mkdir(parents=True, exist_ok=True)
    step = room / "handed_over.stp"
    try:
        session.backend.export(context.doc_id, ExportRequest(path=str(step),
                                                             format="step"))
    except Exception as exc:
        report.check(False, "import: exporting a STEP to import back",
                     str(exc)[:160])
        return
    report.check(step.is_file(), "import: a STEP file exists", str(step))
    session.backend.close_document(context.doc_id, save=False)
    session.forget(context.doc_id)

    try:
        info = session.backend.import_geometry(str(step))
    except Exception as exc:
        report.check(False, "import: Inventor reads the STEP file",
                     getattr(exc, "hint", None) or str(exc)[:300])
        return
    imported = session.register(info, info.units, info.angle_units)
    session.sync_parameters(imported.doc_id)
    detail = info.detail or {}
    report.check(True, "import: Inventor reads the STEP file",
                 f"route: {detail.get('route')}")
    report.note(f"import: what arrived -- {detail}")
    report.check(detail.get("document_kind") == "part",
                 "import: it came in as a part rather than an assembly",
                 str(detail.get("document_kind")))
    report.check((detail.get("bodies") or 0) >= 1,
                 "import: there is a solid body in it", str(detail.get("bodies")))
    report.check(detail.get("parametric") is False,
                 "import: and it is honest that there is nothing to drive",
                 f"parameters: {detail.get('parameters')}")
    session.backend.close_document(imported.doc_id, save=False)
    report.note(f"import: delete {step} when you are done")


def check_calibration(session: Session, report: Report) -> None:
    """Put a measured number on the four estimates that have never had one.

    ``PREDICTED`` in ``inventor_mcp/rehearsal.py`` says how far each operation's
    simulated volume is trusted, and four entries -- coil, draft, emboss and
    split -- sit at a placeholder 0.5 because no shipped recipe uses them, so no
    acceptance run has ever compared one against Inventor. A tolerance nobody
    measured is not a tolerance; at 0.5 it would wave through a fillet applied to
    the wrong edge.

    Each recipe in ``examples/calibration/`` isolates one of them as its last
    operation, with nothing before it but extrudes the simulator gets exactly
    right, so the whole difference belongs to the operation under test. This
    prints that difference. It does not pass or fail on it: the first run is the
    measurement, and what it prints is what the table should then say.
    """
    from inventor_mcp.rehearsal import PREDICTED, rehearse

    if session.backend.name == "mock":
        # Otherwise this compares the simulator with itself, agrees to the digit,
        # and prints a full set of 0.0% divergences -- which reads exactly like a
        # calibration that went well.
        report.skip("calibration: not run",
                    "the simulator would only be comparing itself with itself. "
                    "Use --backend inventor.")
        return

    recipes = sorted(CALIBRATION.glob("*.json"))
    if not recipes:
        report.skip("calibration: no recipes", f"nothing in {CALIBRATION}")
        return

    for path in recipes:
        print(f"\n--- calibration: {path.stem}")
        recipe = PartRecipe.model_validate(json.loads(path.read_text()))
        predicted = rehearse(recipe)
        if not predicted.get("ok"):
            report.check(False, f"{path.stem}: the recipe rehearses",
                         str(predicted.get("findings"))[:300])
            continue

        try:
            live = _live_deltas(session, recipe)
        except Exception as exc:
            hint = getattr(exc, "hint", None)
            report.check(False, f"{path.stem}: it builds in Inventor",
                         f"{type(exc).__name__}: {exc}"
                         + (f"\n         hint: {hint}" if hint else ""))
            continue
        report.check(True, f"{path.stem}: it builds in Inventor")

        rehearsed = _rehearsed_deltas(predicted.get("steps") or [])
        for index, op in enumerate(recipe.operations):
            if op.op not in PREDICTED or index not in live or index not in rehearsed:
                continue
            want, got = rehearsed[index], live[index]
            gap = got - want
            # Measured against the live figure, because that is the true one: an
            # estimate 44% below the truth is 44% wrong, not 79% wrong.
            fraction = abs(gap) / abs(got) if got else float("inf")
            suggested = max(round(fraction * 1.5, 2), 0.02)
            report.note(
                f"{op.op}: simulator {want:+.4f}, Inventor {got:+.4f} cm^3, "
                f"{gap:+.4f} apart -- {fraction:.1%} of the live figure, against a "
                f"PREDICTED entry of {PREDICTED[op.op]:.2f}, which would become "
                f"{suggested:.2f} on this one run")


def _rehearsed_deltas(steps: Sequence[dict]) -> dict[int, float]:
    """Per-operation volume change, from a rehearsal's steps."""
    deltas: dict[int, float] = {}
    running = 0.0
    for step in steps:
        volume = (step.get("measured") or {}).get("volume_cm3")
        if volume is None:
            continue
        deltas[step["index"]] = volume - running
        running = volume
    return deltas


def _live_deltas(session: Session, recipe: PartRecipe) -> dict[int, float]:
    """The same, measured in Inventor after every operation.

    Differenced from a running total rather than read off the finished part,
    because a total says nothing about which operation moved what -- and taken
    the same way on both sides, so the two columns are the same quantity.

    Unlike `build`, this lets the first failure raise. A calibration run wants
    the operation under test or nothing: a part that half built produces a
    difference belonging to no operation in particular.
    """
    deltas: dict[int, float] = {}
    running = 0.0
    document = session.backend.new_part(
        recipe.name, units=recipe.units, angle_units=recipe.angle_units)
    context = session.register(document, recipe.units, recipe.angle_units)
    try:
        for spec in recipe.parameters:
            apply_parameter(session, context, spec)
        for index, op in enumerate(recipe.operations):
            apply_operation(session, context, op)
            seen = measure(session, context)
            if seen is None or "volume_cm3" not in seen:
                continue
            deltas[index] = seen["volume_cm3"] - running
            running = seen["volume_cm3"]
    finally:
        session.backend.close_document(context.doc_id, save=False)
        session.forget(context.doc_id)
    return deltas


def check_work_geometry(session: Session, report: Report) -> None:
    """The five Phase 2 behaviours whose COM half has never executed.

    ``docs/INVENTOR_SETUP.md`` has the list and the order, and the order matters:
    ``WorkPoints.AddByPoint`` is what the two work-axis routes are built on, so a
    failure there explains every later one and the rest are worth nothing until
    it passes.

    Written on a machine with no Inventor to reach, which is the whole reason
    this check exists rather than an assertion in the test suite -- and which is
    also why nothing here reads a COM member the repository has not already
    called. Each check does its work through the recipe layer, so what is being
    confirmed is the behaviour the caller gets, not a signature.

    Three of these needed a part designed so the answer is visible at all:

    * **The bolt circle is judged by where the centre of mass went**, not by
      whether the pattern ran. `_repeat` counts occurrences and never reads the
      axis, so "it built" proves nothing; and the question that matters is not
      "did it run" but whether the axis moves when its driving parameter does.
      Six holes on a circle centred at ``bolt_x`` pull the centre of mass with
      them, so changing ``bolt_x`` has to move it. If it does not, the
      expressions never reached the carrier sketch's dimensions and the feature
      is parametric in name only.
    * **The two blocks for the hole-targeting check are deliberately different
      thicknesses.** The note in `FEATURE_COVERAGE.md` says a total volume cannot
      show that aiming worked, and for two equal blocks it cannot -- the same
      bore either way is the same volume. Ten millimetres against six makes the
      total say which body was bored, without a per-body API the `Backend`
      contract does not have.
    * **The save check confirms the remedy, not the refusal.** The refusal is
      offline logic and `tests/test_saving.py` holds it; what needs a live
      Inventor is that the file is writable once the named document is closed,
      because that is the sentence the hint puts in front of a caller.
    """
    if session.backend.name == "mock":
        # The simulator implements all five and agrees with itself. Running it
        # here would print five passes that say nothing about Inventor, which is
        # worse than not running: the point of this check is the COM half.
        report.skip("work-geometry: not run",
                    "the simulator implements all five and would pass itself. "
                    "Use --backend inventor.")
        return

    plate = [
        {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
            {"type": "rectangle", "center": [0, 0], "width": 120, "height": 80}]},
        {"op": "extrude", "name": "Body", "sketch": "Outline", "distance": 10},
    ]

    # 0. Which parameter names Inventor will take. The 2026-09-07 run refused
    #    `pcd` -- "Inventor refused the parameter 'pcd' = '30 mm': Exception
    #    occurred." -- while `bolt_x` in the same recipe was accepted, and
    #    nothing in `RESERVED_NAMES` or the unit table explains it. Guessing
    #    would be how a wrong entry gets into a table nobody measured, so this
    #    asks. Cheap: one document, one parameter each, no geometry.
    _probe_parameter_names(session, report)

    # 1. WorkPoints.AddByPoint -- everything below depends on it.
    recipe = PartRecipe.model_validate({
        "name": "WorkPointProbe", "units": "mm",
        "parameters": [{"name": "datum_x", "value": 30}],
        "operations": plate + [
            {"op": "work_point", "name": "Datum", "plane": "xy", "at": ["datum_x", 0]},
        ]})
    context, broken = build(session, recipe)
    first_ok = report.check(not broken, "work-geometry: WorkPoints.AddByPoint runs",
                            (broken[0] if broken else "")[:400])
    if context:
        # Not via `list_features`: on 2026-09-07 that returned ['Body'] alone,
        # because the COM backend walks `ComponentDefinition.Features` and
        # Inventor keeps work geometry in `WorkPlanes`, `WorkAxes` and
        # `WorkPoints` instead. The mock puts all of it in one list, so the two
        # disagree -- recorded as a divergence rather than papered over, and it
        # cannot be fixed by guessing, because Inventor's *origin* planes and
        # axes live in those same collections and nothing here has measured how
        # to tell them apart. So the facts needed to fix it are printed below.
        found = _work_geometry(session, context, report)
        report.check("Datum" in found.get("work_points", []),
                     "work-geometry: the work point is in WorkPoints, by name",
                     f"work_points holds {found.get('work_points')}. Nothing "
                     "downstream could reference it either -- `two_points` "
                     "resolves a work point by name out of that collection.")
        _report_carrier_sketches(session, context, report)
        session.backend.close_document(context.doc_id, save=False)
        session.forget(context.doc_id)
    if not first_ok:
        report.skip("work-geometry: the four checks below",
                    "AddByPoint is what they are built on; fix it first. "
                    "docs/INVENTOR_SETUP.md has the order and why.")
        return

    # 2. WorkAxes.AddByTwoPoints, via `normal_to_plane`, which builds two work
    #    points and runs an axis through them.
    for kind, extra in (("normal_to_plane", {"plane": "xy", "at": ["bolt_x", 0]}),
                        ("sketch_line", {"sketch": "Aim", "line": "Spoke"})):
        operations = list(plate)
        if kind == "sketch_line":
            operations.append(
                {"op": "sketch", "name": "Aim", "plane": "xz", "entities": [
                    {"type": "line", "name": "Spoke",
                     "start": ["bolt_x", 0], "end": ["bolt_x", 40]}]})
        operations.append({"op": "work_axis", "name": "BoltAxis", "kind": kind, **extra})
        recipe = PartRecipe.model_validate({
            "name": f"WorkAxis_{kind}", "units": "mm",
            "parameters": [{"name": "bolt_x", "value": 30}],
            "operations": operations})
        context, broken = build(session, recipe)
        call = ("WorkAxes.AddByTwoPoints" if kind == "normal_to_plane"
                else "WorkAxes.AddByLine")
        report.check(not broken, f"work-geometry: {call} runs ({kind})",
                     (broken[0] if broken else "")[:400])
        if context:
            session.backend.close_document(context.doc_id, save=False)
            session.forget(context.doc_id)

    # 3. The one that matters: does the bolt circle move with its parameter?
    # `pcd` is deliberately not the name here: Inventor refused it on
    # 2026-09-07 and the probe above is what will say why. The measurement this
    # check exists for must not be blocked on an unexplained name.
    # bolt_x 20 -> 35, not 30 -> 45. At 45 the hole at theta=0 sits exactly on
    # the plate edge (45 + 15 = 60, and the plate spans -60..60), so Inventor
    # cuts away half of it -- which is a *correct* build whose centre of mass
    # moves 0.12263 mm rather than the 0.18640 a prediction assuming six whole
    # holes gives. It measured 0.12263 and was read as a failure for one run.
    # 20 -> 35 keeps every hole on the plate, so the derivation below is exact,
    # and `_bolt_circle_prediction` refuses to hand back a figure if a later
    # edit breaks that again.
    recipe = PartRecipe.model_validate({
        "name": "OffCentreBoltCircle", "units": "mm",
        "parameters": [{"name": "bolt_x", "value": 20},
                       {"name": "bolt_spacing", "value": 30}],
        "operations": plate + [
            {"op": "work_axis", "name": "BoltAxis", "plane": "xy", "at": ["bolt_x", 0]},
            {"op": "sketch", "name": "Pilot", "plane": "xy", "entities": [
                {"type": "point", "position": ["bolt_x + bolt_spacing / 2", 0]}]},
            # `through_all` with no `direction`, which is what every shipped
            # example does and what the acceptance run has actually measured.
            # The unit test for this recipe says `direction: "negative"`, and
            # that has never run against Inventor -- copying it into a live
            # instrument would risk failing this check on the drill direction
            # and reading as a fault in the work axis.
            {"op": "hole", "name": "Bolt1", "sketch": "Pilot", "diameter": 5,
             "through_all": True},
            {"op": "circular_pattern", "name": "BoltCircle", "features": ["Bolt1"],
             "axis": "BoltAxis", "count": 6, "angle": "360 deg"},
        ]})
    context, broken = build(session, recipe)
    if broken:
        report.check(False, "work-geometry: the off-centre bolt circle builds",
                     broken[0][:400])
    else:
        report.check(True, "work-geometry: the off-centre bolt circle builds")
        before = session.backend.mass_properties(context.doc_id)
        try:
            apply_parameter(session, context, _spec("bolt_x", 35))
            after = session.backend.mass_properties(context.doc_id)
        except Exception as exc:
            report.check(False, "work-geometry: bolt_x can be changed",
                         f"{type(exc).__name__}: {exc}")
        else:
            # Three things can make the centre of mass sit still, and they want
            # different fixes: the parameter never took, the model never
            # rebuilt, or the axis is not really driven by it. Read the
            # parameter back first, so a failure below cannot be blamed on the
            # wrong one. The 2026-09-07 run could not tell them apart, and the
            # answer turned out to be the middle one -- `set_parameter` was the
            # only mutating call outside `_batch`, so nothing called
            # `document.Update()` and every later measurement was of the part
            # as it had been.
            readback = next(
                (p for p in session.backend.list_parameters(context.doc_id)
                 if p.name == "bolt_x"), None)
            report.check(
                readback is not None and abs(readback.value - 35.0) < 1e-6,
                "work-geometry: bolt_x reads back as 35 after being set",
                f"read back {readback.value if readback else None}. If this "
                "fails the parameter never took, and the centre-of-mass check "
                "below is measuring the wrong thing.")
            if before.volume == after.volume:
                report.note("work-geometry: the volume did not change either, "
                            "which is expected -- moving a bolt circle removes "
                            "the same material from somewhere else.")
            moved = _centre_shift_mm(before, after)
            if moved is None:
                # Not a failure of the work axis: a backend that reports no
                # centre of mass cannot answer this, and saying "the bolt circle
                # did not move" would blame the wrong thing.
                report.skip("work-geometry: the bolt circle moves when bolt_x does",
                            "this backend reported no centre of mass, so there is "
                            "nothing to compare. Inventor's MassProperties does.")
            else:
                predicted, invalid = _bolt_circle_prediction(20.0, 35.0)
                if invalid:
                    # The derivation has a precondition and it is not satisfied,
                    # so there is no figure to compare against. Saying so beats
                    # comparing to a number that does not describe the part --
                    # which is what happened when a hole landed on the plate
                    # edge and its half-cut build read as a parametric failure.
                    report.check(False,
                                 "work-geometry: the prediction is valid for "
                                 "this geometry", invalid)
                else:
                    report.check(
                        abs(moved - predicted) < 0.01 * predicted,
                        "work-geometry: the bolt circle moves when bolt_x does",
                        f"centre of mass moved {moved:.5f} mm against a derived "
                        f"{predicted:.5f}. Zero means the expressions never "
                        "reached the carrier sketch's dimensions, so the axis is "
                        "parametric in name only. A different non-zero figure "
                        "means it moved somewhere other than where bolt_x put "
                        "it -- and check the prediction before the part: a hole "
                        "crossing the plate edge changes the right answer.")
                    report.note(f"work-geometry: centre of mass moved "
                                f"{moved:.5f} mm on bolt_x 20 -> 35, derived "
                                f"{predicted:.5f}")
        session.backend.close_document(context.doc_id, save=False)
        session.forget(context.doc_id)

    # 3b. The discriminator that would have caught defect 11 on the first run.
    #     A bolt circle about an axis at (bolt_x, 0) and the same one about Z
    #     must not measure the same: about Z the six holes sit evenly around the
    #     origin, so their centroid *is* the origin. That symmetry is exactly
    #     what hid a work axis stuck on the origin behind a centre of mass that
    #     never moved -- three runs of reading "0.00000 mm" as a parametric
    #     failure, when the axis was in the wrong place from the start.
    _compare_axis_against_z(session, report, plate)

    # 4. A hole aimed at the second body, which is aimed after it is built.
    recipe = PartRecipe.model_validate({
        "name": "AimedHole", "units": "mm", "operations": [
            {"op": "sketch", "name": "First", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 20, "height": 20}]},
            {"op": "extrude", "name": "BlockA", "sketch": "First", "distance": 10},
            {"op": "sketch", "name": "Second", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [40, 0], "width": 20, "height": 20}]},
            {"op": "extrude", "name": "BlockB", "sketch": "Second", "distance": 6,
             "operation": "new_body"},
            {"op": "sketch", "name": "Pilot", "plane": "xy", "entities": [
                {"type": "point", "position": [40, 0]}]},
            {"op": "hole", "name": "Bore", "sketch": "Pilot", "diameter": 8,
             "through_all": True, "bodies": [2]},
        ]})
    context, broken = build(session, recipe)
    if broken and "AffectedBodies" in broken[0]:
        # Measured 2026-09-07: Inventor 2027.1's HoleFeature has no
        # AffectedBodies at all, so this is a gap in Inventor's API rather than
        # a regression to re-report on every run. `rehearse` now warns about
        # the field, and `FEATURE_COVERAGE.md` records the measurement. A
        # standing FAIL here would train a reader to skip the summary.
        report.skip("work-geometry: a hole aimed with `bodies` builds",
                    "Inventor 2027.1 has no HoleFeature.AffectedBodies -- "
                    "measured, recorded, and warned about by `rehearse`. Use an "
                    "`extrude` cut with `bodies`. Re-check on a new release: if "
                    "this starts passing, the warning should go.")
    elif broken:
        report.check(False, "work-geometry: a hole aimed with `bodies` builds",
                     broken[0][:400])
    else:
        report.check(True, "work-geometry: a hole aimed with `bodies` builds")
        volume = session.backend.mass_properties(context.doc_id).volume
        # 20x20x10 + 20x20x6 = 6.4 cm^3, less a 8 mm bore through whichever
        # block it landed on: 0.3016 through the 6 mm one, 0.5027 through 10 mm.
        through_b, through_a = 6.4 - 0.301593, 6.4 - 0.502655
        report.check(
            abs(volume - through_b) < 1e-3,
            "work-geometry: the bore went through the second body",
            f"volume {volume:.6f} cm^3; aimed at the 6 mm block that is "
            f"{through_b:.6f}, through the 10 mm one it is {through_a:.6f}. The "
            "blocks are different thicknesses precisely so the total can tell "
            "them apart -- equal ones cannot, which is the note in "
            "FEATURE_COVERAGE.md.")
        session.backend.close_document(context.doc_id, save=False)
        session.forget(context.doc_id)

    # 5. The save conflict's remedy: writable once the holder is closed.
    into = ROOT / ".acceptance"
    into.mkdir(exist_ok=True)
    target = into / "save_conflict.ipt"
    first = session.backend.new_part("SaveConflictA", units="mm")
    session.register(first, "mm", "deg")
    second = session.backend.new_part("SaveConflictB", units="mm")
    session.register(second, "mm", "deg")
    try:
        session.backend.save_document(first.id, str(target))
        report.check(target.is_file(), "work-geometry: the first save writes the file",
                     str(target))
        try:
            session.backend.save_document(second.id, str(target))
        except Exception as exc:
            report.check("already open" in str(exc),
                         "work-geometry: the second save is refused by name",
                         f"{type(exc).__name__}: {exc}")
        else:
            report.check(False, "work-geometry: the second save is refused by name",
                         "it was allowed, so the guard did not see the conflict")
        session.backend.close_document(first.id, save=False)
        session.forget(first.id)
        try:
            session.backend.save_document(second.id, str(target))
        except Exception as exc:
            report.check(False,
                         "work-geometry: closing the holder makes the path writable",
                         f"{type(exc).__name__}: {exc}. The hint tells a caller to "
                         "close the document and try again; if this fails, that "
                         "advice is wrong.")
        else:
            report.check(True,
                         "work-geometry: closing the holder makes the path writable")
        report.note(f"work-geometry: delete {into} when you are done")
    finally:
        for handle in (first.id, second.id):
            try:
                session.backend.close_document(handle, save=False)
            except Exception:
                pass
            session.forget(handle)


def _bolt_circle_prediction(start_mm: float,
                            end_mm: float) -> tuple[float, str | None]:
    """How far the centre of mass should move, and whether the sum applies.

    Six 5 mm bores through a 120x80x10 plate remove
    ``6 * pi * 0.25^2 * 1.0 = 1.17810 cm^3`` centred on the bolt circle, so
    moving that centre shifts the remaining ``94.82190 cm^3`` in proportion.
    One line of arithmetic -- **as long as every hole is on the plate.**

    That precondition was unstated for a run and cost one. At ``bolt_x`` 45 the
    hole at theta=0 sits at x = 60, exactly the plate edge, and Inventor cuts
    away half of it: a correct build whose centre of mass moves 0.12263 mm, not
    the 0.18640 the sum above gives. Hand-deriving the clipped case afterwards
    reproduced Inventor's figure to five decimals, which is what said the part
    was right and the prediction wrong.

    So this returns a reason instead of a number when a hole would leave the
    plate. A prediction whose assumptions are not met is not a looser
    prediction; it is a different question's answer.
    """
    half_w, half_h, thickness = 6.0, 4.0, 1.0
    r_hole, r_circle, count = 0.25, 1.5, 6
    for bolt_x in (start_mm / 10.0, end_mm / 10.0):
        for index in range(count):
            angle = 2 * math.pi * index / count
            x = bolt_x + r_circle * math.cos(angle)
            y = r_circle * math.sin(angle)
            if x + r_hole > half_w or abs(y) + r_hole > half_h:
                return 0.0, (
                    f"at bolt_x {bolt_x * 10:.0f} mm a hole reaches "
                    f"x={(x + r_hole) * 10:.1f}, y={(abs(y) + r_hole) * 10:.1f} "
                    f"mm and the plate is 120x80, so Inventor clips it and the "
                    "one-line derivation no longer describes the part. Move the "
                    "bolt circle in, or derive the clipped case.")
    bores = count * math.pi * r_hole ** 2 * thickness
    plate = 12.0 * 8.0 * thickness
    return bores * (end_mm - start_mm) / (plate - bores), None


def _compare_axis_against_z(session: Session, report: Report,
                            plate: list[dict]) -> None:
    """The same bolt circle about a created axis and about Z, measured apart.

    A work axis that silently sits on the origin is indistinguishable from a
    correct one by volume, and its centre of mass does not move when the
    parameter does -- which reads as "the axis is not parametric" and sent three
    runs looking in the wrong place. Patterning about Z gives exactly the
    symmetric result such an axis produces, so if the two agree, the axis is not
    where it was asked to be.
    """
    def bolt_circle(axis: str):
        recipe = PartRecipe.model_validate({
            "name": f"BoltCircle_{axis}", "units": "mm",
            "parameters": [{"name": "bolt_x", "value": 30},
                           {"name": "bolt_spacing", "value": 30}],
            "operations": plate + [
                {"op": "work_axis", "name": "BoltAxis", "plane": "xy",
                 "at": ["bolt_x", 0]},
                {"op": "sketch", "name": "Pilot", "plane": "xy", "entities": [
                    {"type": "point", "position": ["bolt_x + bolt_spacing / 2", 0]}]},
                {"op": "hole", "name": "Bolt1", "sketch": "Pilot", "diameter": 5,
                 "through_all": True},
                {"op": "circular_pattern", "name": "Ring", "features": ["Bolt1"],
                 "axis": axis, "count": 6, "angle": "360 deg"},
            ]})
        context, broken = build(session, recipe)
        if broken:
            return context, None
        return context, session.backend.mass_properties(context.doc_id)

    contexts = []
    try:
        about_axis_context, about_axis = bolt_circle("BoltAxis")
        contexts.append(about_axis_context)
        about_z_context, about_z = bolt_circle("z")
        contexts.append(about_z_context)
        if about_axis is None or about_z is None:
            report.check(False, "work-geometry: both bolt circles build",
                         "one of the two did not, so there is nothing to compare")
            return
        gap = _centre_shift_mm(about_axis, about_z)
        if gap is None:
            # No centroid to compare, so this says nothing -- and a backend that
            # reports none is not a backend whose axis is wrong.
            report.skip("work-geometry: an off-centre axis is not the same as Z",
                        "this backend reported no centre of mass. Inventor's "
                        "MassProperties does.")
            return
        report.check(
            gap > 1e-3,
            "work-geometry: an off-centre axis is not the same as Z",
            f"the two centres of mass are {gap:.5f} mm apart. Zero means the "
            "created axis behaved exactly like Z through the origin -- the "
            "signature of a carrier point that never left the origin, which is "
            "defect 11. Volumes: "
            f"{about_axis.volume:.6f} about the axis, {about_z.volume:.6f} "
            "about Z; those can agree even when the axis is wrong, which is why "
            "this compares position.")
        report.note(f"work-geometry: about the axis vs about Z, centres of mass "
                    f"{gap:.5f} mm apart")
    finally:
        for context in contexts:
            if context is None:
                continue
            try:
                session.backend.close_document(context.doc_id, save=False)
            except Exception:
                pass
            session.forget(context.doc_id)


def _probe_parameter_names(session: Session, report: Report) -> None:
    """Which parameter names Inventor accepts, one document, one at a time.

    The 2026-09-07 run refused ``pcd`` and took ``bolt_x`` in the same recipe,
    with a bare "Exception occurred" and nothing in `RESERVED_NAMES` or the unit
    table to explain it. The candidates below are chosen to separate the
    possible reasons rather than to find something that works: whether the
    length matters, whether a unit-like fragment does (``cd`` is candela, ``d``
    is Inventor's own model-parameter prefix), and whether the same letters
    inside a longer name are refused too.

    Reported, never failed on: this is a measurement of Inventor, and a name it
    declines is a fact to record rather than a fault in this repository.
    """
    # Answered on 2026-09-07: it took bolt_x, PCD, pcd_1, bolt_pcd, dia, pitch
    # and bolt_spacing, and refused `cd` and `pcd`. `cd` is the candela and
    # `pcd` the pico-candela, so Inventor refuses a name it can read as a unit,
    # SI prefix included, and is case-sensitive about it. Kept as a regression
    # check rather than deleted: the rule is now in
    # `_why_a_name_is_refused`'s hint, and a release that changed its mind
    # should show up here rather than in somebody's recipe.
    candidates = ["bolt_x", "pcd", "PCD", "pcd_1", "bolt_pcd", "cd", "dia",
                  "pitch", "bolt_spacing"]
    expected_refusals = {"pcd", "cd"}
    backend = session.ensure_backend()
    document = backend.new_part("ParameterNames", units="mm")
    context = session.register(document, "mm", "deg")
    accepted, refused = [], []
    try:
        for name in candidates:
            try:
                apply_parameter(session, context, _spec(name, 30))
            except Exception as exc:
                refused.append(f"{name} ({_first_line(exc)})")
            else:
                accepted.append(name)
        report.note(f"parameter names Inventor took: {', '.join(accepted) or '(none)'}")
        for entry in refused:
            report.note(f"parameter name REFUSED: {entry}")
        names = {entry.split(" ", 1)[0] for entry in refused}
        report.check(
            names == expected_refusals,
            "work-geometry: Inventor still refuses exactly the unit-like names",
            f"refused {sorted(names)}, expected {sorted(expected_refusals)}. "
            "`cd` is the candela and `pcd` the pico-candela; the rule measured "
            "on 2027.1 is that a name Inventor can read as a unit is refused, "
            "SI prefix included, case-sensitively. A change here means "
            "`_why_a_name_is_refused` needs rewriting.")
    finally:
        backend.close_document(context.doc_id, save=False)
        session.forget(context.doc_id)


def _work_geometry(session: Session, context, report: Report) -> dict[str, list[str]]:
    """What the part holds in WorkPlanes, WorkAxes and WorkPoints, by name.

    Through the backend, not by reaching into `ComponentDefinition` from here.
    The first version of this did the latter and got exactly the error
    `describe_feature`'s docstring warns about -- "the application called an
    interface that was marshalled for a different thread" -- which then read as
    a missing work point and cost a run. A COM object returned to a script is
    not a COM object the script may use.
    """
    try:
        found = session.backend.list_work_geometry(context.doc_id)
    except Exception as exc:
        report.note(f"work-geometry: could not list the collections ({exc})")
        return {}
    for key, names in found.items():
        report.note(f"work-geometry: {key} holds {len(names)}: {names}")
    return found


def _report_carrier_sketches(session: Session, context, report: Report) -> None:
    """Whether the carrier sketches came out fully constrained.

    The 2026-09-07 run printed ``horizontal_align(__origin__, point1) was
    refused`` for every carrier sketch, and the message goes on to say the sketch
    keeps a degree of freedom. That may be over-claiming -- `DECISIONS.md`
    records that a constraint Inventor refuses is usually one it inferred for
    itself -- and it matters here more than usual: a carrier point free to move
    is a work axis that does not track its parameter, which is the whole point
    of the check below. Inventor gives no degree-of-freedom count, so
    `fully_constrained` is the answer available.
    """
    try:
        sketches = session.backend.list_sketches(context.doc_id)
    except Exception as exc:
        report.note(f"work-geometry: could not list sketches ({exc})")
        return
    for info in sketches:
        if "_carrier" not in info.name:
            continue
        report.note(f"work-geometry: carrier sketch {info.name} "
                    f"fully_constrained={info.fully_constrained}, "
                    f"dimensions={info.dimensions}, "
                    f"refused_constraints={info.refused_constraints}")


def _first_line(exc: Exception) -> str:
    return str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__


def _spec(name: str, value: float):
    from inventor_mcp.schema import ParameterSpec

    return ParameterSpec(name=name, value=value)


def _centre_shift_mm(before, after) -> float | None:
    """How far the centre of mass moved, in mm, or ``None`` if unreported."""
    first, second = before.center_of_mass, after.center_of_mass
    if not first or not second:
        return None
    return 10.0 * sum((a - b) ** 2 for a, b in zip(first, second)) ** 0.5


def check_views(session: Session, report: Report) -> None:
    """Every display mode and orientation `capture_view` offers, actually applied.

    `hidden_line` asked Inventor for `kHiddenLineRendering`, a name no release
    has, and `capture_view` caught the failure and rendered in whatever mode the
    view was already in without saying so -- a picture in the wrong style, of
    the right part, reported as a success. Both halves are fixed: the name is
    `kWireframeWithHiddenEdgesRendering` and a refused mode now comes back as
    `display_mode_applied: false`. This is what checks that on a real Inventor.

    Orientations are captured too, but only reported. Defect 4 in
    `docs/FEATURE_COVERAGE.md` records that their names do not describe what you
    get -- `front` returns a top view -- and until somebody measures what each
    one actually produces there is nothing here to assert. The file sizes are
    printed because two orientations rendering byte-identical images would say
    the camera never moved.
    """
    from inventor_mcp.backend.base import ScreenshotRequest

    if session.backend.name == "mock":
        report.skip("views: not run",
                    "the simulator cannot render, and says so rather than "
                    "writing a file. Use --backend inventor.")
        return

    recipe = PartRecipe.model_validate({
        "name": "ViewCheck", "units": "mm", "operations": [
            {"op": "sketch", "name": "Base", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]},
            {"op": "extrude", "name": "Block", "sketch": "Base", "distance": 20},
            {"op": "sketch", "name": "Pocket", "plane": "xy", "offset": 20, "entities": [
                {"type": "circle", "center": [0, 0], "diameter": 20}]},
            {"op": "extrude", "name": "Bore", "sketch": "Pocket", "distance": 10,
             "direction": "negative", "operation": "cut"},
        ]})
    context, broken = build(session, recipe)
    if broken:
        report.check(False, "views: the test part builds", broken[0][:300])
        return

    into = ROOT / ".views"
    into.mkdir(exist_ok=True)
    sizes: dict[str, int] = {}
    try:
        for mode in ("shaded", "hidden_line", "wireframe"):
            path = into / f"mode_{mode}.png"
            outcome = session.backend.screenshot(context.doc_id, ScreenshotRequest(
                path=str(path), orientation="iso", display_mode=mode))
            report.check(
                bool(outcome.get("display_mode_applied")),
                f"views: display mode {mode!r} was applied",
                str(outcome.get("note"))[:200])
            report.check(bool(outcome.get("written")) and path.is_file(),
                         f"views: {mode!r} wrote a file", str(path))
            if path.is_file():
                sizes[mode] = path.stat().st_size

        # Two modes producing an identical file means one of them did nothing,
        # which is exactly the failure the flag above is meant to have ended.
        report.check(len(set(sizes.values())) == len(sizes),
                     "views: each display mode rendered something different",
                     f"file sizes: {sizes}")

        for orientation in ("iso", "front", "top", "right", "back"):
            path = into / f"view_{orientation}.png"
            session.backend.screenshot(context.doc_id, ScreenshotRequest(
                path=str(path), orientation=orientation, display_mode="shaded"))
            if path.is_file():
                report.note(f"views: {orientation} -> {path.stat().st_size} bytes")
        report.note("views: look at them before trusting the orientation names -- "
                    "defect 4 says they do not describe what you get")
        report.note(f"views: delete {into} when you are done")
    finally:
        session.backend.close_document(context.doc_id, save=False)
        session.forget(context.doc_id)


CHECKS = {
    "examples": None,  # handled specially: one per recipe
    "parameter-edit": check_parameter_edit,
    "dfm": check_dfm,
    "file": check_from_a_file,
    "import": check_import,
    "hole-styles": check_hole_styles,
    "rollback": check_rollback,
    "threading": check_threading,
    "constants": check_constants,
    "calibration": check_calibration,
    "work-geometry": check_work_geometry,
    "views": check_views,
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
    if not report.checks and not report.skipped and not report.recorded:
        # Nothing ran, which is not the same as nothing failing. Saying "0 of 0
        # passed" and exiting zero is how a filter typo looks like a clean run.
        print(f"Nothing matched --only {args.only}. Known names: examples, "
              + ", ".join(name for name in CHECKS if name != "examples")
              + ", or any example's file name.")
        print("=" * 70)
        return 1
    print(f"{len(report.checks) - len(report.failed)} of {len(report.checks)} checks passed"
          + (f", {len(report.skipped)} skipped" if report.skipped else "")
          + (f", {len(report.recorded)} recorded" if report.recorded else ""))
    for _, what, detail in report.failed:
        print(f"  FAIL  {what}" + (f"  ({detail.splitlines()[0]})" if detail else ""))
    if report.recorded:
        print("\nRecorded, not checked -- this run wrote the baseline it would "
              "have compared against:")
        for what in report.recorded:
            print(f"  seed  {what}")
        print("  Check the arithmetic, commit the file, and the next run is a "
              "regression test.")
    print("=" * 70)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
