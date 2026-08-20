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


def apply_parameter(session: Session, context: DocumentContext, spec: ParameterSpec) -> dict[str, Any]:
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
            bottom_angle=_driven(resolver.angle(op.bottom_angle, "drill point angle")),
            tap=op.tap,
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
) -> dict[str, Any]:
    """Create (or extend) a part from a complete recipe."""
    backend = session.backend

    if document is None:
        info = backend.new_part(recipe.name, units=recipe.units, angle_units=recipe.angle_units)
        context = session.register(info, recipe.units, recipe.angle_units)
    else:
        context = session.context(document)
        context.units = recipe.units
        context.angle_units = recipe.angle_units
        context.resolver.length_unit = recipe.units
        context.resolver.angle_unit = recipe.angle_units

    context.recipe = recipe.model_dump(mode="json", exclude_defaults=True)

    results: dict[str, Any] = {
        "document": context.doc_id,
        "name": recipe.name,
        "units": recipe.units,
        "parameters": [],
        "operations": [],
        "errors": [],
    }

    for spec in recipe.parameters:
        try:
            results["parameters"].append(apply_parameter(session, context, spec))
        except Exception as exc:
            entry = {"parameter": spec.name, "error": str(exc)}
            results["errors"].append(entry)
            if stop_on_error:
                results["stopped_at"] = f"parameter {spec.name}"
                results["ok"] = False
                return results

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
                results["ok"] = False
                return results

    try:
        properties = backend.mass_properties(context.doc_id)
        results["mass_properties"] = properties.as_dict()
    except Exception:  # pragma: no cover - a part with no solid yet
        pass
    results["ok"] = not results["errors"]
    return results


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
        "sketches": {name: plan.summary() | {"profiles": profiles[name]} for name, plan in plans.items()},
        "parameters": {name: q.value for name, q in resolver.known().items()},
    }


#: Operations whose whole purpose is to remove material. One of these that
#: changes nothing has missed the part -- the single most common way a recipe is
#: wrong, and the one that used to survive all the way to a live run.
_SUBTRACTIVE = {"hole", "shell"}

#: Simulator gaps, so a rehearsal does not report them as recipe faults. It
#: does not model an occurrence's volume, so a pattern or a mirror legitimately
#: shows no change here even when Inventor would remove plenty.
_NOT_MODELLED = {"mirror", "rectangular_pattern", "circular_pattern"}


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
    for index, op in enumerate(recipe.operations):
        where = f"operation {index} ({op.op}" + (f", {op.name}" if op.name else "") + ")"
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
        step = {"index": index, "op": op.op, "name": op.name}
        if "measured" in outcome:
            step["measured"] = outcome["measured"]
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
    # No warning about which way a fillet or chamfer moved the volume. The
    # simulator has no notion of which side the material is on, so it models
    # every fillet as subtractive -- an inside-corner fillet, which really adds
    # material, looks like a mistake here. That check belongs on the live path,
    # where convexity is known, and a rehearsal that cries wolf on a correct
    # recipe is worse than one that stays quiet.


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
