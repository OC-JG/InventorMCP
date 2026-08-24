"""Replaying a recipe against a backend.

Everything the tool layer does to the model funnels through :func:`apply_operation`,
whether it arrived as one operation from a granular tool or as the tenth step of
a whole-part recipe.  One code path means the incremental and declarative ways
of working cannot drift apart.
"""

from __future__ import annotations

from typing import Any, Sequence

from .backend.base import (
    AxisSpec,
    Backend,
    ChamferRequest,
    CircularPatternRequest,
    Driven,
    ExtrudeRequest,
    FeatureInfo,
    FilletRequest,
    HoleRequest,
    LoftRequest,
    MirrorRequest,
    RectangularPatternRequest,
    ResolvedSelector,
    RevolveRequest,
    ShellRequest,
    SweepRequest,
    ThreadRequest,
    WorkPlaneRequest,
)
from .errors import FeatureError, ParameterError, RecipeError
from .dfm.freeze import guard_for_recipe
from .expressions import RESERVED_NAMES
from .geometry import plan_sketch
from .plan import PLine
from .resolve import Resolved, Resolver
from .schema import (
    ChamferOp,
    CircularPatternOp,
    ExtrudeOp,
    FilletOp,
    HoleOp,
    LoftOp,
    MaterialOp,
    MirrorOp,
    Operation,
    ParameterSpec,
    PartRecipe,
    RectangularPatternOp,
    RevolveOp,
    Selector,
    ShellOp,
    SketchOp,
    SweepOp,
    ThreadOp,
    WorkPlaneOp,
)
from .session import DocumentContext, Session
from .units import Quantity


def _driven(resolved: Resolved | None) -> Driven | None:
    return None if resolved is None else Driven(resolved.expression, resolved.value)


# ---------------------------------------------------------------------------
# Selectors and axes
# ---------------------------------------------------------------------------


def resolve_selector(selector: Selector, resolver: Resolver, *, kind: str | None = None) -> ResolvedSelector:
    """Convert a recipe selector into backend units (cm)."""
    return ResolvedSelector(
        kind=kind or selector.kind,  # type: ignore[arg-type]
        feature=selector.feature,
        filter=selector.filter,
        near=resolver.point3d(selector.near) if selector.near else None,
        within=resolver.scalar_length(selector.within) if selector.within is not None else None,
        min_length=resolver.scalar_length(selector.min_length) if selector.min_length is not None else None,
        max_length=resolver.scalar_length(selector.max_length) if selector.max_length is not None else None,
        ids=list(selector.ids) if selector.ids else None,
        limit=selector.limit,
    )


def resolve_axis(context: DocumentContext, reference: str, sketch_hint: str | None = None) -> AxisSpec:
    """Work out whether an axis reference means an origin axis, a sketch line or an edge."""
    token = reference.strip()
    if token.lower() in ("x", "y", "z"):
        return AxisSpec(kind="work_axis", value=token.lower())
    if token.startswith("edge:"):
        return AxisSpec(kind="edge", value=token.split(":", 1)[1])

    candidates = [sketch_hint] if sketch_hint else []
    candidates += [name for name in reversed(list(context.plans)) if name != sketch_hint]
    for sketch_name in candidates:
        if sketch_name is None:
            continue
        plan = context.plans.get(sketch_name)
        if plan is None:
            continue
        for primitive in plan.resolve_label(token):
            if isinstance(primitive, PLine):
                return AxisSpec(kind="sketch_line", value=token, sketch=sketch_name)

    named = sorted({label for plan in context.plans.values() for label in plan.labels})
    raise FeatureError(
        f"Cannot resolve {reference!r} as an axis.",
        hint="Use 'x', 'y', 'z', the name of a sketch line, or 'edge:<handle>'. "
        f"Named sketch entities available: {', '.join(named) or '(none)'}.",
    )


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def resolve_parameter(resolver: Resolver, spec: ParameterSpec, unit: str) -> Resolved:
    """Evaluate a parameter's value in the unit the caller asked for."""
    if spec.unit is None and isinstance(spec.value, str):
        # No unit given: let the expression's own units decide (an angle stays an angle).
        return resolver.auto(spec.value, spec.name)
    return resolver.in_unit(spec.value, unit, spec.name)


def apply_parameter(session: Session, context: DocumentContext, spec: ParameterSpec,
                    *, override_frozen: bool = False) -> dict[str, Any]:
    """Declare or change one parameter.

    The freeze is enforced here rather than in the DFM loop that motivated it. A
    guarantee that only holds inside one loop is not a guarantee: the next thing
    to edit a parameter -- a tool call, a script, a later feature -- would walk
    straight through it, and the report would still say the key geometry had been
    protected. Overriding is allowed and has to be asked for by name.
    """
    if not override_frozen and context.frozen is not None:
        context.frozen.refuse(spec.name)
    if spec.name in RESERVED_NAMES:
        raise ParameterError(
            f"{spec.name!r} is reserved (it is a function or constant in expressions).",
            hint="Pick another name, e.g. by adding a prefix.",
        )
    unit = spec.unit or context.units
    resolved = resolve_parameter(context.resolver, spec, unit)
    info = session.backend.set_parameter(
        context.doc_id,
        spec.name,
        resolved.expression,
        units=unit,
        comment=spec.comment,
        key=spec.key,
    )
    context.resolver.declare(spec.name, Quantity(resolved.value, resolved.dim))
    return info.as_dict()


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def measure(session: Session, context: DocumentContext) -> dict[str, Any] | None:
    """What the part is, right now, in the few numbers worth comparing."""
    try:
        properties = session.backend.mass_properties(context.doc_id)
    except Exception:
        return None
    seen: dict[str, Any] = {"volume_cm3": round(properties.volume, 6)}
    box = properties.bounding_box
    if box:
        seen["span_mm"] = [round((box[axis + 3] - box[axis]) * 10, 3) for axis in range(3)]
        seen["at_mm"] = [round(value * 10, 3) for value in box]
    try:
        seen.update(session.backend.topology_counts(context.doc_id))
    except Exception:
        pass
    return seen


def _changed(before: dict[str, Any] | None,
             after: dict[str, Any] | None) -> dict[str, Any] | None:
    """What one operation did, in a form the model can act on.

    An operation reporting only that it ran is the failure mode this project
    keeps hitting: a cut that met no material, a hole drilled past the part and
    a fillet on the wrong edge all reported success. The volume was the witness
    every time -- so every operation now carries it, rather than leaving it to a
    human reading a script afterwards.
    """
    if after is None:
        return None
    report: dict[str, Any] = {"volume_cm3": after["volume_cm3"]}
    if "faces" in after:
        report["faces"], report["edges"] = after["faces"], after["edges"]
    if before is None:
        report["note"] = "first measurement, nothing to compare"
        return report

    moved = after["volume_cm3"] - before["volume_cm3"]
    report["volume_change_cm3"] = round(moved, 6)
    if abs(moved) < 1e-9:
        report["note"] = "the volume did not change"
    for key, label in (("faces", "faces"), ("edges", "edges")):
        if key in after and key in before and after[key] != before[key]:
            report[f"{label}_change"] = after[key] - before[key]
    if "span_mm" in after and "span_mm" in before and after["span_mm"] != before["span_mm"]:
        report["span_mm_was"] = before["span_mm"]
        report["span_mm"] = after["span_mm"]
    return report


def apply_operation(session: Session, context: DocumentContext, op: Operation) -> dict[str, Any]:
    """Execute one recipe operation and update the session's memory of the part."""
    result = _apply_one(session, context, op)
    if op.op == "sketch":
        return result  # a sketch adds no material, so there is nothing to weigh
    before, after = context.last_measurement, measure(session, context)
    context.last_measurement = after
    report = _changed(before, after)
    if report is not None:
        result["measured"] = report
    return result


def _apply_one(session: Session, context: DocumentContext, op: Operation) -> dict[str, Any]:
    backend: Backend = session.backend
    resolver = context.resolver

    if isinstance(op, SketchOp):
        plan = plan_sketch(op, resolver)
        info = backend.build_sketch(context.doc_id, plan)
        context.remember_sketch(info.name, plan)
        return {"op": "sketch", **info.as_dict()}

    if isinstance(op, ExtrudeOp):
        sketch_name, _ = context.sketch_plan(op.sketch)
        request = ExtrudeRequest(
            sketch=sketch_name,
            distance=_driven(resolver.length(op.distance, "extrude distance", positive=True))
            if op.distance is not None
            else None,
            profiles=op.profiles,
            extent="through_all" if op.extent in ("through_all", "all") else op.extent,
            direction=op.direction,
            operation=op.operation,
            taper=_driven(resolver.angle(op.taper, "extrude taper")) if op.taper else None,
            name=op.name,
        )
        return _record(context, backend.extrude(context.doc_id, request), "extrude")

    if isinstance(op, RevolveOp):
        sketch_name, _ = context.sketch_plan(op.sketch)
        request = RevolveRequest(
            sketch=sketch_name,
            axis=resolve_axis(context, op.axis, sketch_name),
            angle=_driven(resolver.angle(op.angle, "revolve angle")) if op.angle is not None else None,
            profiles=op.profiles,
            direction=op.direction,
            operation=op.operation,
            name=op.name,
        )
        return _record(context, backend.revolve(context.doc_id, request), "revolve")

    if isinstance(op, SweepOp):
        profile_name, _ = context.sketch_plan(op.profile_sketch)
        path_name, _ = context.sketch_plan(op.path_sketch)
        request = SweepRequest(profile_name, path_name, op.operation, op.name)
        return _record(context, backend.sweep(context.doc_id, request), "sweep")

    if isinstance(op, LoftOp):
        names = [context.sketch_plan(name)[0] for name in op.sketches]
        request = LoftRequest(names, [context.sketch_plan(r)[0] for r in op.rails], op.operation, op.name)
        return _record(context, backend.loft(context.doc_id, request), "loft")

    if isinstance(op, HoleOp):
        sketch_name, plan = context.sketch_plan(op.sketch)
        request = HoleRequest(
            sketch=sketch_name,
            diameter=_driven(resolver.length(op.diameter, "hole diameter", positive=True)),  # type: ignore[arg-type]
            point_indices=_hole_indices(plan, op.points, sketch_name),
            depth=_driven(resolver.length(op.depth, "hole depth", positive=True)) if op.depth else None,
            through_all=op.through_all and op.depth is None,
            direction=op.direction,
            style=op.style,
            cbore_diameter=_driven(resolver.length(op.cbore_diameter, "counterbore diameter"))
            if op.cbore_diameter is not None
            else None,
            cbore_depth=_driven(resolver.length(op.cbore_depth, "counterbore depth"))
            if op.cbore_depth is not None
            else None,
            csink_diameter=_driven(resolver.length(op.csink_diameter, "countersink diameter"))
            if op.csink_diameter is not None
            else None,
            csink_angle=_driven(resolver.angle(op.csink_angle, "countersink angle")),
            bottom_angle=_driven(resolver.angle(op.bottom_angle, "drill point angle"))
            if op.bottom_angle is not None
            else None,
            tap=op.tap,
            tap_type=op.tap_type,
            tap_class=op.tap_class,
            tap_right_handed=op.tap_right_handed,
            tap_full_depth=op.tap_full_depth,
            name=op.name,
        )
        return _record(context, backend.hole(context.doc_id, request), "hole")

    if isinstance(op, FilletOp):
        request = FilletRequest(
            edges=resolve_selector(op.edges, resolver, kind="edge"),
            radius=_driven(resolver.length(op.radius, "fillet radius", positive=True)),  # type: ignore[arg-type]
            name=op.name,
        )
        return _record(context, backend.fillet(context.doc_id, request), "fillet")

    if isinstance(op, ChamferOp):
        request = ChamferRequest(
            edges=resolve_selector(op.edges, resolver, kind="edge"),
            distance=_driven(resolver.length(op.distance, "chamfer distance", positive=True)),  # type: ignore[arg-type]
            distance2=_driven(resolver.length(op.distance2, "chamfer second distance"))
            if op.distance2 is not None
            else None,
            angle=_driven(resolver.angle(op.angle, "chamfer angle")) if op.angle is not None else None,
            name=op.name,
        )
        return _record(context, backend.chamfer(context.doc_id, request), "chamfer")

    if isinstance(op, ShellOp):
        request = ShellRequest(
            faces=resolve_selector(op.faces, resolver, kind="face"),
            thickness=_driven(resolver.length(op.thickness, "shell thickness", positive=True)),  # type: ignore[arg-type]
            direction=op.direction,
            name=op.name,
        )
        return _record(context, backend.shell(context.doc_id, request), "shell")

    if isinstance(op, RectangularPatternOp):
        request = RectangularPatternRequest(
            features=_pattern_features(context, op.features),
            axis1=resolve_axis(context, op.axis1),
            count1=resolver.count(op.count1, "pattern count", maximum=1000),
            spacing1=_driven(resolver.length(op.spacing1, "pattern spacing", positive=True)),  # type: ignore[arg-type]
            axis2=resolve_axis(context, op.axis2) if op.axis2 else None,
            count2=resolver.count(op.count2, "second pattern count", maximum=1000),
            spacing2=_driven(resolver.length(op.spacing2, "pattern spacing"))
            if op.spacing2 is not None
            else None,
            flip1=op.flip1,
            flip2=op.flip2,
            name=op.name,
        )
        return _record(context, backend.rectangular_pattern(context.doc_id, request), "rectangular_pattern")

    if isinstance(op, CircularPatternOp):
        request = CircularPatternRequest(
            features=_pattern_features(context, op.features),
            axis=resolve_axis(context, op.axis),
            count=resolver.count(op.count, "pattern count", maximum=1000),
            angle=_driven(resolver.angle(op.angle, "pattern angle")),  # type: ignore[arg-type]
            fitted=op.fitted,
            name=op.name,
        )
        return _record(context, backend.circular_pattern(context.doc_id, request), "circular_pattern")

    if isinstance(op, MirrorOp):
        request = MirrorRequest(
            features=_pattern_features(context, op.features), plane=op.plane, name=op.name
        )
        return _record(context, backend.mirror(context.doc_id, request), "mirror")

    if isinstance(op, WorkPlaneOp):
        request = WorkPlaneRequest(
            kind=op.kind,
            base=op.base,
            second=op.second,
            offset=_driven(resolver.length(op.offset, "work plane offset")),
            angle=_driven(resolver.angle(op.angle, "work plane angle")),
            name=op.name,
        )
        return _record(context, backend.work_plane(context.doc_id, request), "work_plane")

    if isinstance(op, ThreadOp):
        request = ThreadRequest(
            faces=resolve_selector(op.faces, resolver, kind="face"),
            designation=op.designation,
            internal=op.internal,
            depth=_driven(resolver.length(op.depth, "thread depth")) if op.depth else None,
            name=op.name,
        )
        return _record(context, backend.thread(context.doc_id, request), "thread")

    if isinstance(op, MaterialOp):
        info = backend.set_material(context.doc_id, op.material, op.appearance)
        return {"op": "material", "material": op.material, **info.as_dict()}

    raise RecipeError(f"Unsupported operation {type(op).__name__}.")  # pragma: no cover


def _record(context: DocumentContext, info: FeatureInfo, op_name: str) -> dict[str, Any]:
    context.remember_feature(info.name)
    return {"op": op_name, **info.as_dict()}


def _pattern_features(context: DocumentContext, names: Sequence[str]) -> list[str]:
    if names:
        return list(names)
    if context.last_feature is None:
        raise FeatureError(
            "There is no feature to pattern yet.",
            hint="Create the feature first, or name it explicitly in `features`.",
        )
    return [context.last_feature]


def _hole_indices(plan: Any, names: Sequence[str], sketch_name: str) -> list[int]:
    if not names:
        return []
    order = {primitive_id: index for index, primitive_id in enumerate(plan.hole_centers)}
    indices: list[int] = []
    for name in names:
        ids = plan.labels.get(name)
        if not ids:
            known = ", ".join(sorted(plan.labels)) or "(none)"
            raise FeatureError(
                f"Sketch {sketch_name!r} has no entity named {name!r}.",
                hint=f"Named entities in that sketch: {known}.",
            )
        matched = [order[pid] for pid in ids if pid in order]
        if not matched:
            raise FeatureError(
                f"{name!r} in sketch {sketch_name!r} is not a hole-centre point.",
                hint="Use a `point`, `point_grid` or `bolt_circle` entity for hole centres.",
            )
        indices.extend(matched)
    return sorted(set(indices))


# ---------------------------------------------------------------------------
# Whole-recipe execution
# ---------------------------------------------------------------------------


def build_part(
    session: Session,
    recipe: PartRecipe,
    *,
    document: str | None = None,
    stop_on_error: bool = True,
    rollback_on_error: bool = False,
    against_rehearsal: bool = True,
) -> dict[str, Any]:
    """Create (or extend) a part from a complete recipe.

    ``rollback_on_error`` is off by default and deliberately so: a half-built
    part is the best evidence there is about what went wrong, and throwing it
    away to leave a tidy document has cost more debugging time here than it has
    saved. Turn it on when the part matters more than the diagnosis -- appending
    to something that already works, or retrying a hole, which consumes its
    sketch and so cannot be retried any other way.

    ``against_rehearsal`` rehearses the recipe in the simulator first and reports
    any operation whose live volume change disagrees with the prediction. It
    costs a few milliseconds and it is how a fillet on the wrong edge announces
    itself without a human reading the numbers.
    """
    backend = session.backend

    if document is None:
        info = backend.new_part(recipe.name, units=recipe.units, angle_units=recipe.angle_units)
        context = session.register(info, recipe.units, recipe.angle_units)
        created = True
    else:
        context = session.context(document)
        context.units = recipe.units
        context.angle_units = recipe.angle_units
        context.resolver.length_unit = recipe.units
        context.resolver.angle_unit = recipe.angle_units
        created = False

    context.recipe = recipe.model_dump(mode="json", exclude_defaults=True)

    results: dict[str, Any] = {
        "document": context.doc_id,
        "name": recipe.name,
        "units": recipe.units,
        "parameters": [],
        "operations": [],
        "errors": [],
    }

    handle = None
    if rollback_on_error:
        handle = backend.begin_transaction(context.doc_id, f"Build {recipe.name}")
        if handle is None:
            results["rollback"] = (
                "not available from this backend, so the part is left as it is"
            )

    def finish() -> dict[str, Any]:
        """Commit or roll back, and say which, before handing the result over."""
        results["ok"] = not results["errors"]
        if handle is None:
            return results
        if results["ok"]:
            backend.commit_transaction(handle)
            return results
        results["rolled_back"] = backend.abort_transaction(handle)
        if results["rolled_back"]:
            # The parameter and operation entries are kept deliberately: they are
            # the record of how far the build got, which is the only thing left
            # to reason from once the geometry is gone.
            results["applied_then_undone"] = True
            results["rollback"] = (
                "every feature and sketch from this call was undone, so nothing "
                "under `operations` exists any more"
                + (", and the part document is empty" if created
                   else "; the part is as it was before the call")
            )
        else:
            results["rollback"] = (
                "the rollback itself failed, so the part is in whatever state "
                "the last operation left it -- inspect it before building on it"
            )
        return results

    for spec in recipe.parameters:
        try:
            results["parameters"].append(apply_parameter(session, context, spec))
        except Exception as exc:
            entry = {"parameter": spec.name, "error": str(exc)}
            results["errors"].append(entry)
            if stop_on_error:
                results["stopped_at"] = f"parameter {spec.name}"
                return finish()

    # Only now: declaring a frozen parameter is what puts it there, so a guard
    # installed before this loop would refuse the very statement that froze it.
    # And widened, never replaced: extending an open document with a second
    # recipe must not drop a freeze somebody added in the meantime with
    # `protect_geometry` -- no source may remove protection, this one included.
    held = (list(context.frozen.as_dict()["declared"]),
            list(context.frozen.features)) if context.frozen is not None else ([], [])
    context.frozen = guard_for_recipe(context.recipe, extra=held[0],
                                      extra_features=held[1])

    if recipe.material:
        try:
            backend.set_material(context.doc_id, recipe.material)
        except Exception as exc:
            results["errors"].append({"material": recipe.material, "error": str(exc)})

    for index, op in enumerate(recipe.operations):
        try:
            results["operations"].append({"index": index, **apply_operation(session, context, op)})
        except Exception as exc:
            results["errors"].append(
                {"index": index, "op": getattr(op, "op", "?"), "error": str(exc),
                 "details": getattr(exc, "hint", None)}
            )
            if stop_on_error:
                results["stopped_at"] = f"operation {index} ({getattr(op, 'op', '?')})"
                return finish()

    try:
        properties = backend.mass_properties(context.doc_id)
        results["mass_properties"] = properties.as_dict()
    except Exception:  # pragma: no cover - a part with no solid yet
        pass

    if against_rehearsal:
        if document is not None:
            results["divergence_note"] = (
                "not checked against a rehearsal: these operations were appended to "
                "an existing part, and the rehearsal starts from an empty one"
            )
        elif backend.name == "mock":
            results["divergence_note"] = (
                "not checked against a rehearsal: this *is* the simulator, so it "
                "would only be comparing it with itself"
            )
        else:
            try:
                predicted = rehearse(recipe)
            except Exception:  # pragma: no cover - a rehearsal must never break a build
                predicted = {}
            if predicted.get("rehearsed"):
                divergence = compare_to_rehearsal(
                    results["operations"], predicted.get("steps") or [])
                if divergence:
                    results["divergence"] = divergence
                    results["divergence_note"] = (
                        "the simulator predicted a different volume change for these "
                        "operations. It is an estimate, not a measurement, but it "
                        "predicted the rest of this part correctly -- so read these "
                        "before trusting the result."
                    )
    return finish()


#: How closely the simulator should predict each operation's volume change, as a
#: fraction of it. An operation missing from here is not compared at all.
#:
#: The tight entries are arithmetic the simulator does exactly: a prism is an
#: area times a length, a hole is a cylinder, an occurrence repeats its seed. The
#: loose ones are estimates -- Pappus for a revolve, a mean section for a loft, a
#: corner prism for a fillet -- and are here to catch a feature that did
#: something else entirely rather than to check the arithmetic.
PREDICTED = {
    "extrude": 0.02,
    "hole": 0.02,
    "mirror": 0.02,
    "rectangular_pattern": 0.02,
    "circular_pattern": 0.02,
    "revolve": 0.15,
    "sweep": 0.25,
    "loft": 0.35,
    "shell": 0.35,
    "fillet": 0.30,
    "chamfer": 0.30,
}

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


def check_recipe(recipe: PartRecipe) -> dict[str, Any]:
    """Static checks that need no backend: expressions, references, closure."""
    resolver = Resolver(recipe.units, recipe.angle_units)
    findings: list[dict[str, Any]] = []
    plans: dict[str, Any] = {}
    features: list[str] = []
    last_sketch: str | None = None

    for spec in recipe.parameters:
        try:
            if spec.name in RESERVED_NAMES:
                raise ParameterError(f"{spec.name!r} is a reserved name.")
            resolved = resolve_parameter(resolver, spec, spec.unit or recipe.units)
            resolver.declare(spec.name, Quantity(resolved.value, resolved.dim))
        except Exception as exc:
            findings.append({"where": f"parameter {spec.name}", "error": str(exc)})

    for index, op in enumerate(recipe.operations):
        where = f"operation {index} ({op.op})"
        try:
            if isinstance(op, SketchOp):
                plan = plan_sketch(op, resolver)
                name = op.name or f"Sketch{len(plans) + 1}"
                plans[name] = plan
                last_sketch = name
            elif isinstance(op, (ExtrudeOp, RevolveOp, HoleOp)):
                target = op.sketch or last_sketch
                if target is None or target not in plans:
                    raise RecipeError(
                        f"{op.op} refers to sketch {target!r}, which no earlier operation creates."
                    )
                if isinstance(op, ExtrudeOp) and op.distance is not None:
                    resolver.length(op.distance, "extrude distance", positive=True)
                if isinstance(op, HoleOp):
                    resolver.length(op.diameter, "hole diameter", positive=True)
                    if not plans[target].hole_centers:
                        raise RecipeError(f"Sketch {target!r} has no hole-centre points.")
            elif isinstance(op, FilletOp):
                resolver.length(op.radius, "fillet radius", positive=True)
            elif isinstance(op, ChamferOp):
                resolver.length(op.distance, "chamfer distance", positive=True)
            elif isinstance(op, ShellOp):
                resolver.length(op.thickness, "shell thickness", positive=True)
            if op.name:
                if op.name in features:
                    raise RecipeError(f"Two operations are both named {op.name!r}.")
                features.append(op.name)
        except Exception as exc:
            findings.append({"where": where, "error": str(exc), "hint": getattr(exc, "hint", None)})

    profiles = {
        name: len([loop for loop in _loops(plan)]) for name, plan in plans.items()
    }
    for name, plan_count in profiles.items():
        if plan_count == 0 and any(
            isinstance(op, (ExtrudeOp, RevolveOp)) and (op.sketch or "") == name
            for op in recipe.operations
        ):
            findings.append({
                "where": f"sketch {name}",
                "error": "No closed profile: an extrude or revolve of this sketch will fail.",
                "hint": "Check that the geometry forms a closed loop and is not construction only.",
            })

    return {
        "ok": not findings,
        "findings": findings,
        # What each parameter was written as, so a value can be told apart from
        # a value derived from other values.
        "parameter_expressions": {
            spec.name: str(spec.value) for spec in recipe.parameters
        },
        "sketches": {
            name: plan.summary() | {
                "profiles": profiles[name],
                # The resolved value behind each expression, which is what a
                # drawing can be compared against: the recipe says
                # "plate_w - 2 * edge_margin" and the drawing says 96.
                "driving": {d.expression: d.value for d in plan.dimensions},
            }
            for name, plan in plans.items()
        },
        "parameters": {name: q.value for name, q in resolver.known().items()},
        # What kind of quantity each one is, because a 90 degree angle is 1.5708
        # in Inventor's units and would otherwise look like a 15.7 mm length to
        # anything comparing numbers -- a drawing check reported exactly that.
        "parameter_dimensions": {
            name: q.dim.value for name, q in resolver.known().items()
        },
    }


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
    #: A shell makes the part hollow, and the simulator has no booleans -- so
    #: every later cut removes a whole prism here where Inventor removes only the
    #: walls it meets. Those steps are marked so nothing downstream compares them
    #: and calls a correct model wrong.
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
        if hollow:
            step["predictable"] = False
            step["why_not"] = ("the part is hollow and the simulator has no "
                               "booleans, so this removes a whole prism here "
                               "where Inventor removes only the walls it meets")
        # A cut loft hollows a part as surely as a shell does -- the duct
        # transition is the pattern -- and the simulator has no booleans either
        # way, so a later cut on the hollowed body gets the same "predicted
        # loosely" treatment rather than a guaranteed false divergence.
        hollow = hollow or op.op == "shell" or (
            op.op == "loft" and getattr(op, "operation", "join") == "cut")
        steps.append(step)
        _warn_about(report["warnings"], where, op, outcome)

        target = getattr(op, "sketch", None) or context.last_sketch
        subtractive = op.op in _SUBTRACTIVE or getattr(op, "operation", None) == "cut"
        box = [value / 10 for value in was["at_mm"]] if "at_mm" in was else None
        if subtractive and target in context.plans and not _profile_reaches_the_part(
                context.plans[target], box):
            report["warnings"].append({
                "where": where,
                "warning": f"sketch {target!r} does not reach the part",
                "why": "Its geometry lies entirely outside the part's bounding box "
                       "in its own plane, so this will cut empty air. The simulator "
                       "cannot see this -- it has no booleans -- but the bounding "
                       "boxes can.",
            })

    report["steps"] = steps
    report["rehearsed"] = True
    report["result"] = measure(session, context)
    report["warnings"].extend(_undriven_parameters(recipe, context))
    return report


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


#: How far apart two things may be and still count as touching, in cm. A cut
#: exactly on a face is a legitimate design, so the test has to be generous.
TOUCHING = 1.0e-4


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
    # No warning about which way a fillet or chamfer moved the volume. The
    # simulator takes the sign from the selector -- it cannot see which side the
    # material is on -- so a complaint here would only be repeating the recipe
    # back. That check belongs on the live path, where convexity is measured.


def _undriven_parameters(recipe: PartRecipe,
                         context: DocumentContext) -> list[dict[str, Any]]:
    """Parameters that were declared and then drive nothing.

    A recipe can name every dimension and still hard-code the geometry, which
    builds the right shape once and cannot be revised. That is the failure the
    whole project exists to prevent, so it is worth saying out loud.
    """
    from .expressions import referenced_parameters

    def named_in(text: Any) -> set[str]:
        """Parameters an arbitrary recipe value refers to, if it is an expression.

        Plenty of strings in a recipe are not: a thread designation like
        "M5x0.8", a material, a filter name. Those refer to no parameter, so an
        unparseable string contributes nothing rather than raising.
        """
        if isinstance(text, str):
            try:
                return referenced_parameters(text)
            except Exception:
                return set()
        if isinstance(text, dict):
            return set().union(*(named_in(value) for value in text.values())) \
                if text else set()
        if isinstance(text, (list, tuple)):
            return set().union(*(named_in(item) for item in text)) if text else set()
        return set()

    driven: set[str] = set()
    for plan in context.plans.values():
        for dimension in plan.dimensions:
            driven |= named_in(dimension.expression)
    for spec in recipe.parameters:
        driven |= named_in(spec.value)
    for op in recipe.operations:
        driven |= named_in(op.model_dump())

    declared = [spec.name for spec in recipe.parameters]
    idle = [name for name in declared if name not in driven]
    if not idle:
        return []
    return [{
        "where": "parameters",
        "warning": f"{', '.join(idle)} drive nothing",
        "why": "Declared but never referenced, so changing them moves no geometry. "
               "Write the sizes that depend on them as expressions -- "
               '"width": "plate_w" rather than "width": 120.',
    }]


def _loops(plan: Any) -> list[list[str]]:
    from .geometry import profile_loops

    return profile_loops(plan)
