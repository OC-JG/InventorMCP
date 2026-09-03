"""Replaying a recipe against a backend.

Everything the tool layer does to the model funnels through :func:`apply_operation`,
whether it arrived as one operation from a granular tool or as the tenth step of
a whole-part recipe.  One code path means the incremental and declarative ways
of working cannot drift apart.

Two neighbours were split out of this file when it reached 1,200 lines doing
five jobs, before drawings and assemblies arrive to make it worse:

* ``checks.py`` -- what can be said about a recipe without building anything;
* ``rehearsal.py`` -- building it in the simulator, and holding a live build up
  against that.

Both are re-exported from here, so ``from inventor_mcp.builder import rehearse``
keeps working; see the bottom of this file.
"""

from __future__ import annotations

from typing import Any, Sequence

from .backend.base import (
    AxisSpec,
    Backend,
    ChamferRequest,
    CoilRequest,
    CircularPatternRequest,
    Driven,
    CombineRequest,
    DraftRequest,
    EmbossRequest,
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
    SplitRequest,
    SweepRequest,
    ThreadRequest,
    WorkAxisRequest,
    WorkPlaneRequest,
    WorkPointRequest,
)
from .errors import FeatureError, ParameterError, RecipeError
from .dfm.freeze import guard_for_recipe
from .expressions import RESERVED_NAMES
from .geometry import plan_sketch
from .plan import PLine
from .resolve import Resolved, Resolver
from .schema import (
    ChamferOp,
    CoilOp,
    CircularPatternOp,
    CombineOp,
    DraftOp,
    EmbossOp,
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
    SplitOp,
    SketchOp,
    SweepOp,
    ThreadOp,
    WorkAxisOp,
    WorkPlaneOp,
    WorkPointOp,
)
from .session import DocumentContext, Session
from .units import Quantity


def _driven(resolved: Resolved | None) -> Driven | None:
    return None if resolved is None else Driven(resolved.expression, resolved.value)


def _driven_pair(resolved: Sequence[Resolved]) -> tuple[Driven, Driven]:
    """A two-component point that keeps each component's expression.

    ``Resolver.point2d`` throws the expressions away, which is the right answer
    for sketch geometry the backend places by coordinate. Work geometry is
    referred to by later operations, so `[bolt_x, bolt_y]` has to reach Inventor
    as those two parameters and not as the numbers they came to.
    """
    first, second = resolved
    return (Driven(first.expression, first.value), Driven(second.expression, second.value))


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
    # A work axis created here is checked before the sketches, because its name
    # is an explicit reference to one thing while a sketch label is a search.
    if token in context.work_axes:
        return AxisSpec(kind="work_axis", value=token)

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
    axes = ", ".join(sorted(context.work_axes)) or "(none)"
    raise FeatureError(
        f"Cannot resolve {reference!r} as an axis.",
        hint="Use 'x', 'y', 'z', the name of a work axis or a sketch line, or "
        f"'edge:<handle>'. Work axes created here: {axes}. "
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
    # Where the part went, as well as how much of it there is. A volume says
    # how much an operation moved and cannot say which side it moved it from:
    # a trim that keeps the wrong half of a part removes a plausible amount and
    # reports a plausible number. The bounding box's centre has a direction, and
    # the two halves send it opposite ways.
    if "at_mm" in after and "at_mm" in before:
        shift = [round((after["at_mm"][axis] + after["at_mm"][axis + 3]) / 2
                       - (before["at_mm"][axis] + before["at_mm"][axis + 3]) / 2, 3)
                 for axis in range(3)]
        if any(abs(value) > 1e-6 for value in shift):
            report["centre_shift_mm"] = shift
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
            bodies=tuple(op.bodies or ()),
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

    if isinstance(op, CoilOp):
        sketch_name, _ = context.sketch_plan(op.sketch)
        request = CoilRequest(
            sketch=sketch_name,
            axis=resolve_axis(context, op.axis, sketch_name),
            profiles=op.profiles,
            pitch=_driven(resolver.length(op.pitch, "coil pitch", positive=True))
            if op.pitch is not None else None,
            height=_driven(resolver.length(op.height, "coil height", positive=True))
            if op.height is not None else None,
            # unitless, not count: a coil of 1.75 turns is ordinary, and
            # `count` refuses a fraction on purpose.
            revolutions=_driven(resolver.unitless(op.revolutions, "coil revolutions"))
            if op.revolutions is not None else None,
            taper=_driven(resolver.angle(op.taper, "coil taper"))
            if op.taper is not None else None,
            operation=op.operation,
            clockwise=op.clockwise,
            reverse_axis=op.reverse_axis,
            spiral=op.spiral,
            name=op.name,
        )
        return _record(context, backend.coil(context.doc_id, request), "coil")

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
            radius_end=_driven(resolver.length(op.radius_end, "fillet end radius", positive=True))  # type: ignore[arg-type]
            if op.radius_end is not None
            else None,
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

    if isinstance(op, WorkPointOp):
        request = WorkPointRequest(
            plane=op.plane,
            at=_driven_pair(resolver.coordinates(op.at, "work point")),
            offset=_driven(resolver.length(op.offset, "work point offset")),
            name=op.name,
        )
        info = backend.work_point(context.doc_id, request)
        context.work_points.add(info.name)
        return _record(context, info, "work_point")

    if isinstance(op, WorkAxisOp):
        request = WorkAxisRequest(
            kind=op.kind,
            plane=op.plane,
            at=_driven_pair(resolver.coordinates(op.at, "work axis")),
            points=list(op.points),
            line=op.line,
            sketch=op.sketch or (context.last_sketch if op.kind == "sketch_line" else None),
            name=op.name,
        )
        info = backend.work_axis(context.doc_id, request)
        context.work_axes.add(info.name)
        return _record(context, info, "work_axis")

    if isinstance(op, ThreadOp):
        request = ThreadRequest(
            faces=resolve_selector(op.faces, resolver, kind="face"),
            designation=op.designation,
            internal=op.internal,
            depth=_driven(resolver.length(op.depth, "thread depth")) if op.depth else None,
            name=op.name,
        )
        return _record(context, backend.thread(context.doc_id, request), "thread")

    if isinstance(op, EmbossOp):
        request = EmbossRequest(
            sketch=op.sketch,
            depth=_driven(resolver.length(op.depth, "emboss depth", positive=True)),  # type: ignore[arg-type]
            style=op.style,
            flip=op.flip,
            name=op.name,
        )
        return _record(context, backend.emboss(context.doc_id, request), "emboss")

    if isinstance(op, DraftOp):
        request = DraftRequest(
            faces=resolve_selector(op.faces, resolver, kind="face"),
            plane=op.plane,
            angle=_driven(resolver.angle(op.angle, "draft angle")),  # type: ignore[arg-type]
            flip=op.flip,
            name=op.name,
        )
        return _record(context, backend.draft(context.doc_id, request), "draft")

    if isinstance(op, CombineOp):
        request = CombineRequest(
            base=op.base,
            tools=list(op.tools),
            operation=op.operation,
            keep_tools=op.keep_tools,
            name=op.name,
        )
        return _record(context, backend.combine(context.doc_id, request), "combine")

    if isinstance(op, SplitOp):
        request = SplitRequest(
            tool=op.tool,
            style=op.style,
            remove_positive=op.remove_positive,
            name=op.name,
        )
        return _record(context, backend.split(context.doc_id, request), "split")

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
            # Imported here rather than at the top: `rehearsal` imports this
            # module, and a build is the one place that needs it back.
            from .rehearsal import compare_to_rehearsal, rehearse

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



























    # No warning about which way a fillet or chamfer moved the volume. The
    # simulator takes the sign from the selector -- it cannot see which side the
    # material is on -- so a complaint here would only be repeating the recipe
    # back. That check belongs on the live path, where convexity is measured.


# ---------------------------------------------------------------------------
# Where the rest of this file went
# ---------------------------------------------------------------------------

#: Names this module used to define, and the module that defines them now. They
#: are still reachable from here because they were part of this module's surface
#: for the whole of its life and a split is not a reason to break a caller.
#:
#: Resolved lazily, through the module ``__getattr__`` below, because
#: ``rehearsal`` imports this module: asking for it at import time would be a
#: circle. A caller writing new code should import from the module that owns the
#: name -- that is the point of having moved them.
_MOVED = {
    "check_recipe": "checks",
    "_undriven_parameters": "checks",
    "compare_to_rehearsal": "rehearsal",
    "rehearse": "rehearsal",
    "PREDICTED": "rehearsal",
    "NOTICEABLE": "rehearsal",
    "TOUCHING": "rehearsal",
    "_KNOWN_BROKEN": "rehearsal",
    "_NOT_MODELLED": "rehearsal",
    "_SUBTRACTIVE": "rehearsal",
    "_MUST_MOVE": "rehearsal",
    "_MEASURES_MATERIAL": "rehearsal",
    "_profile_reaches_the_part": "rehearsal",
    "_removes_material": "rehearsal",
    "_sketches_that_cut": "rehearsal",
    "_warn_about": "rehearsal",
    "_divergence_reason": "rehearsal",
}


def __getattr__(name: str) -> Any:
    where = _MOVED.get(name)
    if where is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(f".{where}", __package__), name)
