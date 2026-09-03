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

from .builder import resolve_parameter
from .errors import ParameterError, RecipeError
from .expressions import RESERVED_NAMES
from .geometry import plan_sketch
from .resolve import Resolver
from .schema import (
    ChamferOp,
    ExtrudeOp,
    FilletOp,
    HoleOp,
    PartRecipe,
    RevolveOp,
    ShellOp,
    SketchOp,
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
