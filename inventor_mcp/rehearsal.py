"""Building the recipe in the simulator, and comparing a live build against it.

Split out of ``builder.py``. Two jobs that are really one: :func:`rehearse`
builds the whole recipe in the simulator and reports what each operation would
do, and :func:`compare_to_rehearsal` holds a live Inventor build up against that
report and says where the two disagreed. The tolerances they are judged by --
:data:`PREDICTED` -- live here too, because a tolerance kept away from the thing
it judges is a tolerance nobody updates.

This module imports from ``builder``; ``builder`` does not import it, except
inside :func:`builder.build_part`, which needs a rehearsal to compare against.
That one deferred import is what keeps the two files from depending on each
other in a circle.
"""

from __future__ import annotations

from typing import Any, Sequence

from .builder import apply_operation, apply_parameter, measure
from .checks import _undriven_parameters, check_recipe
from .schema import Operation, PartRecipe
from .session import DocumentContext


#: How closely the simulator should predict each operation's volume change, as a
#: fraction of it. An operation missing from here is not compared at all.
#:
#: The tight entries are arithmetic the simulator does exactly: a prism is an
#: area times a length, a hole is a cylinder, an occurrence repeats its seed. The
#: loose ones are estimates -- Pappus for a revolve, the frustum rule for a loft,
#: a corner prism for a fillet -- and are here to catch a feature that did
#: something else entirely rather than to check the arithmetic.
#:
#: The last four are the ones worth explaining. Sweeping the simulator's marked
#: approximations turned up that all three of them -- the draft wedge, the split
#: fraction, the emboss ink heuristic -- were absent from this table, and so was
#: the coil's arc length. The guard had holes in exactly the places their author
#: had written "this is approximate". They were set to half: a placeholder that
#: tolerates a coarse estimate and still catches both classes that matter,
#: because `_divergence_reason` keys on a sign flip and on a change where none
#: was predicted, and any tolerance under 1.0 catches those.
#:
#: Three of the four have since been measured, on Inventor 2027.1 on 2026-09-03,
#: by the recipes in `examples/calibration/` -- one per operation, each isolating
#: it as the last step so the whole difference belongs to it. What was measured,
#: and what it was set to:
#:
#: * `coil` was 0.2% out on a spring whose pitch clears its wire twice over.
#:   Set to 0.15, not 0.02: the estimate ignores what happens where consecutive
#:   turns meet, so a spring wound tight will be worse and nobody has measured
#:   one. 0.15 is what a revolve gets, which is the same kind of arithmetic.
#: * `draft` was 2.1% out, and exactly so: the drafted block is a frustum, the
#:   integral says 4.6179 cm^3 and Inventor said 4.6178. Set to 0.20 rather than
#:   0.03, because the wedge assumes every drafted face spans the full pull
#:   height -- true of the wall that was measured, and not of a boss.
#: * `emboss` was 17.5% out on nine capitals of Arial at 8 mm. Set to 0.40: a
#:   glyph's area is whatever the font says it is, and one string in one face at
#:   one size says very little about the next one.
#:
#: Each is loosened deliberately beyond what its run showed. A tolerance is for
#: catching a feature that did something else entirely, not for certifying the
#: arithmetic, and a false alarm teaches the reader to ignore the field.
#:
#: `split` stays at the placeholder. Its run came back 25.3% apart, and that
#: number is worth nothing: the simulator kept the wrong *amount* and Inventor
#: kept the wrong *side*, so the figure is two unrelated errors compounding.
#: See defect 5 in `docs/FEATURE_COVERAGE.md`. Setting a tolerance from it would
#: enshrine an inversion.
#:
#: Nothing already here was retuned. The chamfer and the loft became much more
#: accurate in the same pass and their entries stayed put: one live datapoint
#: each is not a basis for a tolerance, and tightening on a hunch is the mistake
#: this table exists to catch.
PREDICTED = {
    "extrude": 0.02,
    "hole": 0.02,
    "mirror": 0.02,
    "rectangular_pattern": 0.02,
    "circular_pattern": 0.02,
    "revolve": 0.15,
    "coil": 0.15,
    "draft": 0.20,
    "sweep": 0.25,
    "fillet": 0.30,
    "chamfer": 0.30,
    "loft": 0.35,
    "shell": 0.35,
    "emboss": 0.40,
    # Not measured: see above. The one run that tried disagreed about which side
    # a trim discards, which has to be settled before this number means anything.
    "split": 0.50,
}


#: Operations that measure themselves against the simulator's ledger of prisms
#: rather than being charged their whole swept shape. An `extrude` cut asks how
#: much material lies inside its sweep, which is what makes the enclosure's cable
#: entry 0.36 cm^3 of wall instead of 5.04 cm^3 of box, so a hollow part does not
#: put it beyond prediction. A hole is charged its full depth, a draft its whole
#: face, an emboss its whole area; on a hollow part those are over-estimates and
#: the rehearsal says so rather than letting the divergence check fault a correct
#: recipe.
_MEASURES_MATERIAL = {"extrude"}


#: Below this, in cm^3, a difference is not worth reporting whatever the
#: fraction says: a 2 mm chamfer moves about a thousandth of a cm^3, and a
#: percentage of nearly nothing is noise.
NOTICEABLE = 5.0e-3


def compare_to_rehearsal(built: Sequence[dict[str, Any]],
                         rehearsed: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Where Inventor disagreed with the simulator, operation by operation.

    The simulator is now accurate enough on extruded parts to be used as an
    oracle: it predicted the angle bracket to within 0.003%. So a live operation
    whose volume change differs from the rehearsed one by more than that kind of
    operation warrants is evidence that Inventor did something the recipe did not
    ask for -- a fillet on the wrong edge, a cut on the wrong side, a hole that
    met no material. Every one of those shipped at least once, and every one of
    them would have shown up here.

    Deltas are compared rather than totals, so an error in one operation does not
    then flag every operation after it.
    """
    findings: list[dict[str, Any]] = []
    expected = {step["index"]: step for step in rehearsed}
    for step in built:
        tolerance = PREDICTED.get(step.get("op") or "")
        if tolerance is None:
            continue
        counterpart = expected.get(step["index"], {})
        if counterpart.get("predictable") is False:
            continue
        want = (counterpart.get("measured") or {})
        got = step.get("measured") or {}
        predicted, actual = want.get("volume_change_cm3"), got.get("volume_change_cm3")
        if predicted is None or actual is None:
            continue
        off = actual - predicted
        allowed = max(abs(predicted) * tolerance, NOTICEABLE)
        if abs(off) <= allowed:
            continue
        findings.append({
            "index": step["index"],
            "op": step.get("op"),
            "name": step.get("name"),
            "rehearsed_cm3": round(predicted, 6),
            "measured_cm3": round(actual, 6),
            "off_by_cm3": round(off, 6),
            "why": _divergence_reason(step.get("op") or "", predicted, actual),
        })
    return findings


def _divergence_reason(op: str, predicted: float, actual: float) -> str:
    """What a difference of this shape usually means."""
    if abs(actual) < NOTICEABLE and abs(predicted) >= NOTICEABLE:
        return ("Inventor changed nothing where the recipe implies a change. For a "
                "cut or a hole this is a profile that met no material; for a "
                "pattern or mirror it is the wrong feature named.")
    if (predicted < 0) != (actual < 0):
        return ("It moved the volume the other way. A cut that adds is on the "
                "wrong side of its profile; a fillet that removes where one was "
                "expected to add is on a convex edge rather than the inside "
                "corner, which is the mistake that has cost the most time here.")
    if abs(actual) > abs(predicted):
        return ("It changed more than the geometry implies -- a cut reaching "
                "further than intended, or a selector catching more edges than "
                "the one that was meant.")
    return ("It changed less than the geometry implies. Check the extent and the "
            "selector: a partial overlap looks like this.")


#: Operations whose whole purpose is to remove material. One of these that
#: changes nothing has missed the part -- the single most common way a recipe is
#: wrong, and the one that used to survive all the way to a live run.
_SUBTRACTIVE = {"hole", "shell"}


#: Operations that repeat an existing feature, so a volume that does not move
#: means the wrong feature was named.
_MUST_MOVE = {"mirror", "rectangular_pattern", "circular_pattern"}


#: Operations that cannot work on the Inventor this project has measured, with
#: what to do instead. A recipe is warned before it is built rather than after,
#: because the alternative is a live run that fails on something already known.
_KNOWN_BROKEN = {
    "thread": (
        "Inventor 2027.1's ThreadFeatures has no CreateThreadDefinition -- its "
        "only method is Add(Face, StartEdge, ThreadInfo, ...) and nothing in the "
        "type library named for threads creates a ThreadInfo. Use a `hole` with "
        "`tap` instead, which is measured and works: Inventor cuts the thread's "
        "minor diameter and records the designation on the feature."
    ),
}


#: Simulator gaps, so a rehearsal does not report them as recipe faults. A
#: thread is cosmetic and moves no volume in Inventor either. Patterns and
#: mirrors used to be here, back when an occurrence's volume was not modelled --
#: they are now, so a mirror that changes nothing is a real complaint.
_NOT_MODELLED = {"thread"}


def rehearse(recipe: PartRecipe) -> dict[str, Any]:
    """Build the recipe in the simulator and report what it would do.

    Static checks catch a malformed recipe; they cannot catch a well-formed one
    that builds the wrong part. A cut whose profile misses the material, a
    parameter that drives nothing, a selector that matches no edge -- all of
    those are only visible once something is built, and all of them have
    reached a live Inventor at least once before being noticed.

    The simulator can see every one of them, in milliseconds, on any machine.
    So a rehearsal is worth far more than a schema check: write a recipe, run
    it here, read the complaints, and only then spend a CAD seat on it.
    """
    from .backend.mock.backend import MockBackend
    from .session import Session

    static = check_recipe(recipe)
    report: dict[str, Any] = {
        "ok": static["ok"],
        "findings": list(static["findings"]),
        "warnings": [],
        "sketches": static["sketches"],
        "parameters": static["parameters"],
        "parameter_expressions": static["parameter_expressions"],
        "parameter_dimensions": static["parameter_dimensions"],
        "rehearsed": False,
    }
    if not static["ok"]:
        report["hint"] = "Fix the findings first; the rehearsal needs a valid recipe."
        return report

    session = Session(backend_kind="mock")
    session._backend = MockBackend()
    session.backend.connect()
    document = session.backend.new_part(
        recipe.name, units=recipe.units, angle_units=recipe.angle_units)
    context = session.register(document, recipe.units, recipe.angle_units)

    for spec in recipe.parameters:
        try:
            apply_parameter(session, context, spec)
        except Exception as exc:
            report["findings"].append({"where": f"parameter {spec.name}", "error": str(exc)})
            report["ok"] = False
            return report

    steps: list[dict[str, Any]] = []
    #: A shell makes the part hollow, and the simulator has no booleans. An
    #: `extrude` copes: it asks the simulator's ledger of prisms how much
    #: material lies inside its sweep, so a cut through the wall of a shelled box
    #: is charged the wall. Nothing else does, and every other feature on a
    #: hollow part is charged its whole shape. Those steps are marked so nothing
    #: downstream compares them and calls a correct model wrong.
    hollow = False
    for index, op in enumerate(recipe.operations):
        where = f"operation {index} ({op.op}" + (f", {op.name}" if op.name else "") + ")"
        if op.op in _KNOWN_BROKEN:
            report["warnings"].append({
                "where": where,
                "warning": f"`{op.op}` does not work on the Inventor this was "
                           "measured against",
                "why": _KNOWN_BROKEN[op.op],
            })
        # Where the part was before this operation: a cut has to be judged
        # against what it was aimed at, not against what it left behind.
        was = measure(session, context) or {}
        try:
            outcome = apply_operation(session, context, op)
        except Exception as exc:
            report["findings"].append({
                "where": where, "error": str(exc), "hint": getattr(exc, "hint", None),
            })
            report["ok"] = False
            report["steps"] = steps
            report["rehearsed"] = True
            report["hint"] = ("The recipe is valid but does not build. The simulator "
                              "stopped here, so Inventor would too.")
            return report
        step: dict[str, Any] = {"index": index, "op": op.op, "name": op.name}
        if "measured" in outcome:
            step["measured"] = outcome["measured"]
        if hollow and op.op not in _MEASURES_MATERIAL:
            step["predictable"] = False
            step["why_not"] = ("the part is hollow and the simulator has no "
                               "booleans, so this is charged its whole shape "
                               "where Inventor takes only the material it meets")
        # A cut loft hollows a part as surely as a shell does -- the duct
        # transition is the pattern -- and the simulator has no booleans either
        # way, so a later cut on the hollowed body gets the same "predicted
        # loosely" treatment rather than a guaranteed false divergence.
        hollow = hollow or op.op == "shell" or (
            op.op == "loft" and getattr(op, "operation", "join") == "cut")
        steps.append(step)
        _warn_about(report["warnings"], where, op, outcome)

        box = [value / 10 for value in was["at_mm"]] if "at_mm" in was else None
        if _removes_material(op):
            for target in _sketches_that_cut(op, context):
                if target in context.plans and not _profile_reaches_the_part(
                        context.plans[target], box):
                    report["warnings"].append({
                        "where": where,
                        "warning": f"sketch {target!r} does not reach the part",
                        "why": "Its geometry lies entirely outside the part's bounding "
                               "box in its own plane, so this will cut empty air. The "
                               "simulator cannot see this -- it has no booleans -- but "
                               "the bounding boxes can.",
                    })

    report["steps"] = steps
    report["rehearsed"] = True
    report["result"] = measure(session, context)
    report["warnings"].extend(_undriven_parameters(recipe, context))
    return report


def _removes_material(op: Operation) -> bool:
    """Whether this operation is meant to take material away.

    ``operation: "cut"`` covers most of it; the two that say it another way are
    a hole, which is always a cut, and an engraved emboss, whose knob is
    ``style`` -- so an engrave that missed the part was the one cut nothing
    checked. ``shell`` removes material too and is deliberately absent: it has no
    profile to miss with.
    """
    if getattr(op, "operation", None) == "cut":
        return True
    if op.op == "hole":
        return True
    return op.op == "emboss" and getattr(op, "style", None) == "engrave"


def _sketches_that_cut(op: Operation, context: DocumentContext) -> list[str]:
    """Which sketches' geometry decides where *op* takes material from.

    Only sketches the operation actually consumes. It used to be
    ``getattr(op, "sketch", None) or context.last_sketch`` for everything, and
    an operation with no sketch of its own fell through to whichever one
    happened to be last -- so a shell was reported as "sketch 'Unrelated' does
    not reach the part", naming a sketch it has nothing to do with. A warning
    that fires on a correct recipe is worse than no warning; the reader learns
    to skip the field.

    A sweep contributes both of its sketches. The profile decides the section
    and the path decides where it goes, and either one placed away from the
    material is a cut through air.
    """
    if op.op == "sweep":
        return [name for name in (getattr(op, "profile_sketch", None),
                                  getattr(op, "path_sketch", None)) if name]
    if op.op == "loft":
        return [name for name in getattr(op, "sketches", ()) or () if name]
    if "sketch" in type(op).model_fields:
        named = getattr(op, "sketch", None) or context.last_sketch
        return [named] if named else []
    return []


#: How far apart two things may be and still count as touching, in cm. A cut
#: exactly on a face is a legitimate design, so the test has to be generous.
TOUCHING = 1.0e-4


def _profile_reaches_the_part(plan: Any, box: Sequence[float] | None) -> bool:
    """Whether a sketch's geometry overlaps the part at all, in its own plane.

    The simulator cannot answer this: it has no booleans, so it subtracts a
    cut's prismatic volume whether or not the profile meets anything. But the
    question is answerable from the bounding boxes alone, and answering it
    conservatively -- only ever reporting a *certain* miss -- catches the single
    most expensive class of mistake in this project's history. The bracket's
    slots cut empty air for three rounds.
    """
    from .backend.mock.backend import _PLANES, map3d
    from .geometry import plan_bounds

    if box is None or plan.plane.lower() not in _PLANES:
        return True  # a work plane or a face: no cheap answer, so do not guess
    try:
        low_u, low_v, high_u, high_v = plan_bounds(plan)
    except Exception:
        return True
    if low_u > high_u or low_v > high_v:
        return True

    # Which model axes the sketch's own two run along.
    corners = [map3d(plan.plane, low_u, low_v, 0.0), map3d(plan.plane, high_u, high_v, 0.0)]
    normal = _PLANES[plan.plane.lower()][1]
    for axis in range(3):
        if normal[axis]:
            continue  # the extrusion direction; a cut may travel any distance along it
        near, far = sorted((corners[0][axis], corners[1][axis]))
        if far < box[axis] - TOUCHING or near > box[axis + 3] + TOUCHING:
            return False
    return True


def _warn_about(warnings: list[dict[str, Any]], where: str, op: Operation,
                outcome: dict[str, Any]) -> None:
    """Complaints about an operation that ran but probably did the wrong thing."""
    measured = outcome.get("measured")
    if measured is None or op.op in _NOT_MODELLED:
        return
    moved = measured.get("volume_change_cm3")
    if moved is None:
        return

    subtractive = op.op in _SUBTRACTIVE or getattr(op, "operation", None) == "cut"
    if subtractive and abs(moved) < 1e-9:
        warnings.append({
            "where": where,
            "warning": "removed no material",
            "why": "Inventor will build the feature and change nothing. Check the "
                   "plane and the coordinates against the part's bounding box.",
        })
    elif subtractive and moved > 0:
        warnings.append({
            "where": where,
            "warning": f"added {moved:.4f} cm3 instead of removing material",
            "why": "A cut that grows the part is on the wrong side of its profile.",
        })
    elif not subtractive and abs(moved) < 1e-9 and op.op in _MUST_MOVE:
        warnings.append({
            "where": where,
            "warning": "added no material",
            "why": "An occurrence repeats whatever its seed did, so a pattern or "
                   "mirror of a feature that changed the part should change it "
                   "again. Check that `features` names the right feature.",
        })
