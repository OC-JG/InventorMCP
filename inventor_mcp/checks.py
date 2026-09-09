"""What can be said about a recipe without building anything.

Split out of ``builder.py``, which had grown to do resolution, dispatch, static
checking, rehearsal and divergence in one file. These are the checks that need
no backend at all: an expression that will not evaluate, a sketch nothing
creates, a profile that is not closed, a parameter that drives nothing. They are
the cheapest thing a caller can run and the first thing they should.

Nothing here touches a `Session` or a `Backend`. What needs one is in
``rehearsal.py``, which runs the recipe against the simulator; what actually
builds is in ``builder.py``.
"""

from __future__ import annotations

from typing import Any

from .backend.base import AxisSpec
from .builder import resolve_axis, resolve_parameter
from .errors import ParameterError, RecipeError
from .expressions import RESERVED_NAMES
from .geometry import plan_sketch
from .resolve import Resolver
from .schema import (
    ChamferOp,
    CircularPatternOp,
    ExtrudeOp,
    FilletOp,
    HoleOp,
    PartRecipe,
    RevolveOp,
    ShellOp,
    SketchOp,
    WorkAxisOp,
    WorkPlaneOp,
)
from .session import DocumentContext
from .units import Quantity


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


#: Which origin axis stands perpendicular to each origin plane. A
#: `circular_pattern` wants exactly this axis for the plane it patterns, which
#: is what makes the wrong answer recognisable.
_PLANE_NORMALS = {"xy": "z", "xz": "y", "yz": "x"}


def _parallel_origin_plane(reference: str, recipe: PartRecipe) -> str | None:
    """Which origin plane *reference* is parallel to, or ``None`` if unknowable.

    Only two cases are certain from a recipe. An origin plane is parallel to
    itself, and a work plane built with ``kind: "offset"`` is parallel to
    whatever it was offset from, however long the chain. Everything else is
    left alone deliberately:

    * ``angle`` and ``tangent`` work planes are not parallel to their base, and
      a ``midplane`` is only parallel to its two if those two are parallel to
      each other.
    * ``face:<handle>`` names geometry that does not exist until something is
      built, so no static answer exists.

    Note that the *simulator's own table* is looser than this.
    ``mock.work_plane`` files every work plane in ``work_planes`` against an
    origin base whatever its ``kind``, so reading that mapping alone would
    treat an angled or tangent plane as parallel to its base -- and a check
    that did would report a correct angled-plane recipe as a fault. Since
    2026-09-09 the mock also records *why* such a plane cannot be located, in
    ``unplaceable_planes`` beside it, so the information is there; this still
    walks the recipe rather than the simulator, because a static check has to
    answer before anything has been rehearsed.
    """
    seen: set[str] = set()
    current = reference
    while True:
        named = current.split(":")[0].lower()
        if named in _PLANE_NORMALS:
            return named
        if current in seen:  # a work plane offset from itself; the backend refuses it
            return None
        seen.add(current)
        for op in recipe.operations:
            if isinstance(op, WorkPlaneOp) and op.name == current and op.kind == "offset":
                current = op.base
                break
        else:
            return None


def _work_axis_direction(op: WorkAxisOp, recipe: PartRecipe,
                         planes: dict[str, str],
                         last_sketch: str | None) -> tuple[str, str] | None:
    """Where a ``work_axis`` points, worked out as the operation is walked.

    Deliberately not deferred to the pattern that uses it. A ``sketch_line``
    axis with no ``sketch`` takes the most recent sketch, and "most recent"
    means at the moment the axis was created -- later sketches are not
    candidates. Reading it at the pattern would name whichever sketch happened
    to be last by then.
    """
    if op.kind == "normal_to_plane":
        normal = _PLANE_NORMALS.get(_parallel_origin_plane(op.plane, recipe) or "")
        return ("along", normal) if normal else None
    if op.kind == "sketch_line":
        plane = planes.get(op.sketch or last_sketch or "")
        return ("in_plane", plane) if plane else None
    # `two_points` is left alone on purpose. Whether the axis lies in a plane
    # depends on where its two work points sit, and a guess that came out wrong
    # would fire on a correct recipe -- which teaches the reader to ignore the
    # field, the thing this repository refuses to do.
    return None


def _axis_direction(spec: AxisSpec, planes: dict[str, str],
                    work_axes: dict[str, tuple[str, str] | None]
                    ) -> tuple[str, str] | None:
    """What is known about where a resolved pattern axis points.

    Returns ``("along", "x"|"y"|"z")`` when the direction itself is known, or
    ``("in_plane", <plane reference>)`` when only the plane the axis lies in is
    -- which is all a sketch line ever tells you, and all this check needs.
    ``None`` means nothing certain, and nothing certain means no warning.
    """
    if spec.kind == "edge":
        return None  # an edge handle names geometry, not a recipe fact
    if spec.kind == "sketch_line":
        plane = planes.get(spec.sketch or "")
        return ("in_plane", plane) if plane else None
    if spec.value in ("x", "y", "z"):
        return ("along", spec.value)
    return work_axes.get(spec.value)


def _pattern_axes_in_the_patterned_plane(
        recipe: PartRecipe, context: DocumentContext) -> list[dict[str, Any]]:
    """Circular patterns turning about an axis parallel to the face they pattern.

    Defect 7 in ``docs/FEATURE_COVERAGE.md``. A ``circular_pattern`` turns about
    an axis perpendicular to the face it patterns; a sketch line lies *in* its
    own sketch plane; so a plate sketched on XY whose pattern axis is a line
    drawn on XY is asking Inventor to revolve the holes about an axis lying flat
    in the plate. Nothing caught it: ``check_recipe`` and ``validate_recipe``
    both pass it, and the simulator returns ``ok: true`` with a plausible volume
    because ``_repeat`` multiplies the seed's volume delta and never reads the
    axis at all.

    It is a **warning rather than a finding**, and the reason is the honest
    limit of a static check: a pattern about an in-plane axis is meaningless as
    a bolt circle and yet a legitimate way to write a 180-degree flip, and
    nothing here can tell which was meant. Detecting it properly -- so a wrong
    axis moves no material and the divergence check's ``centre_shift_mm`` sees
    it -- needs the simulator placing occurrences rather than counting them,
    which is the ``ponytail`` already recorded on ``_repeat``.

    Where it stays quiet, and why each is deliberate:

    * **Only ``extrude`` and ``hole`` seed a judgement.** For those the profile
      lies in the sketch plane and the feature runs normal to it, so the faces
      really are parallel to that plane. A ``revolve``'s geometry does not sit
      in its sketch plane at all -- the belt pulley's section is drawn on XZ and
      revolved about Z -- so treating a sketch plane as a face plane there would
      report the shipped example as a fault.
    * **A seed whose plane is unknown, an ``edge:`` axis, a ``two_points`` work
      axis, and an axis lying in a plane merely perpendicular to the seed's**
      all yield no answer rather than a guess. In the last case the axis could
      equally be the correct one; only a line's own direction would say.
    * **Several seeds are only reported when every one of them agrees**, so a
      pattern whose axis is right for one feature and wrong for another is left
      to the caller.
    """
    planes: dict[str, str] = {}          # sketch name -> its plane reference
    seen_plans: dict[str, Any] = {}      # the sketches that exist by this point
    #: work-axis name -> where it points, resolved when the axis was created.
    #: A name with ``None`` against it is a work axis whose direction is not
    #: knowable, which still has to be registered: `resolve_axis` prefers an
    #: explicit work axis over a sketch label of the same name.
    work_axes: dict[str, tuple[str, str] | None] = {}
    face_planes: dict[str, str] = {}     # feature name -> plane its faces are parallel to
    last_sketch: str | None = None
    last_feature: str | None = None
    warnings: list[dict[str, Any]] = []

    for index, op in enumerate(recipe.operations):
        if isinstance(op, SketchOp):
            name = op.name or f"Sketch{len(planes) + 1}"
            planes[name] = op.plane
            if name in context.plans:
                seen_plans[name] = context.plans[name]
            last_sketch = name
            continue

        if isinstance(op, WorkAxisOp) and op.name:
            work_axes[op.name] = _work_axis_direction(op, recipe, planes, last_sketch)

        if isinstance(op, CircularPatternOp):
            targets = op.features or ([last_feature] if last_feature else [])
            seeds = {face_planes[name] for name in targets if name in face_planes}
            direction = _axis_direction(
                _resolved_axis(context, seen_plans, set(work_axes), op.axis),
                planes, work_axes,
            ) if seeds else None
            complaint = _axis_lies_in(direction, seeds, recipe) if direction else None
            if complaint is not None:
                where = f"operation {index} ({op.op}" + (f", {op.name}" if op.name else "") + ")"
                warnings.append({
                    "where": where,
                    "warning": f"the pattern axis {op.axis!r} {complaint}",
                    "why": "A circular pattern turns about an axis perpendicular to "
                           "the face it patterns, so this one revolves the feature "
                           "out of the material rather than around it. The simulator "
                           "cannot see it -- `_repeat` counts occurrences and never "
                           "reads the axis -- so the volume will look right.",
                    "instead": "Use `work_axis` with `kind: \"normal_to_plane\"` and "
                               "the plane the feature was built on, which puts the "
                               "axis perpendicular to it and takes `at` as "
                               "expressions. A 180-degree flip is a `mirror`.",
                })

        plane = _face_plane_of(op, planes, last_sketch)
        if plane is not None and op.name:
            face_planes[op.name] = plane
        # Every operation past a sketch becomes "the previous feature" for a
        # pattern that names none, and an unnamed one leaves nothing to look up
        # -- the backend invents its name. So it clears the slot rather than
        # leaving the last *named* feature standing in for it, which would judge
        # a pattern against a seed it has nothing to do with.
        last_feature = op.name or None

    return warnings


def _face_plane_of(op: Any, planes: dict[str, str],
                   last_sketch: str | None) -> str | None:
    """The plane an operation's new faces are parallel to, where that is certain.

    Only an extrude and a hole qualify -- see
    :func:`_pattern_axes_in_the_patterned_plane` for why a revolve does not.
    """
    if not isinstance(op, (ExtrudeOp, HoleOp)):
        return None
    return planes.get(op.sketch or last_sketch or "")


def _resolved_axis(context: DocumentContext, seen_plans: dict[str, Any],
                   seen_axes: set[str], reference: str) -> AxisSpec:
    """Resolve *reference* the way the builder will, against what exists yet.

    The narrowed context matters: resolving against the finished document would
    let a sketch line created *after* the pattern claim the label, and the
    warning would then name a sketch the pattern never saw.

    An unresolvable axis comes back as an ``edge`` spec, which this check reads
    as "nothing certain". That is not swallowing the error -- ``apply_operation``
    resolves the same reference moments later and reports the failure properly,
    with the list of candidates. Raising here would only replace that message
    with a worse one from a diagnostic.
    """
    narrowed = DocumentContext(
        doc_id=context.doc_id, name=context.name,
        plans=dict(seen_plans), work_axes=set(seen_axes),
    )
    try:
        return resolve_axis(narrowed, reference)
    except Exception:
        return AxisSpec(kind="edge", value=reference)


def _axis_lies_in(direction: tuple[str, str], seeds: set[str],
                  recipe: PartRecipe) -> str | None:
    """How the axis is wrong for every one of *seeds*, or ``None`` if it is not.

    Both arms answer "is this axis parallel to the patterned face", and both
    decline when the seed plane cannot be reduced to an origin plane.
    """
    reduced = {plane: _parallel_origin_plane(plane, recipe) for plane in seeds}
    if not reduced or any(value is None for value in reduced.values()):
        return None

    kind, subject = direction
    if kind == "along":
        wrong = [plane for plane, origin in reduced.items()
                 if _PLANE_NORMALS[origin] != subject]
        if len(wrong) != len(reduced):
            return None
        planes = ", ".join(sorted(wrong))
        return (f"runs along {subject.upper()}, which lies in {planes} -- the plane "
                f"the patterned feature was built on")

    axis_plane = _parallel_origin_plane(subject, recipe)
    if axis_plane is None or any(origin != axis_plane for origin in reduced.values()):
        return None
    # Naming the work plane and the origin plane it reduces to is worth the
    # words when they differ, and reads as a stutter when they do not.
    where = subject if subject.lower() == axis_plane else f"{subject!r}, parallel to {axis_plane}"
    return (f"lies in {where} -- the plane the patterned feature was built on, "
            "so the axis is flat in the face rather than perpendicular to it")
