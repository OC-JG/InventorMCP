"""Rehearsing a drawing: what a sheet would say, held against the part.

`drawing.py` is the other direction and the older one. It reads a drawing
somebody else produced into a :class:`~inventor_mcp.drawing.DrawingReading` and
checks a recipe against it, which is the 2D-to-3D direction `DECISIONS.md`
argues for. This module is 3D-to-2D: given a
:class:`~inventor_mcp.schema.DrawingRecipe` and the part it draws, it works out
what the sheet would state and then puts that through the *same* comparison.

**Nothing here draws anything.** It produces a ledger -- which views, which
dimensions, which parameters reached them -- and no picture, for the reason the
roadmap gives: the simulator's job is to say how far a prediction is trusted,
not to be a second renderer. Producing the sheet in Inventor is separate work
and is not written.

What makes the ledger worth having on its own is that a drawing can be wrong in
ways the part cannot correct. The two that matter:

* a dimension the sheet states that the part does not realise -- an expression
  whose value is nothing the model asserts;
* **a number the part asserts that the sheet never states**, which is an
  under-dimensioned drawing. A factory cannot make what a drawing does not say,
  and this is the one failure that is invisible when you look at the sheet:
  every dimension on it is correct.

Both come out of `drawing.compare`, which already computes them for the reading
direction. It is reused rather than reimplemented, and only its vocabulary is
translated: what `compare` calls `invented` -- a number the model asserts and
the drawing does not give -- is, when the drawing is the thing being produced,
a dimension somebody forgot to ask for.
"""

from __future__ import annotations

from typing import Any

from .drawing import DrawingDimension, DrawingReading, DrawingView, compare
from .errors import InventorMCPError
from .resolve import Resolver
from .schema import DrawingRecipe, DrawingViewSpec, PartRecipe
from .units import Dim, Quantity, to_internal

#: A drawing view's direction mapped onto the kind a `DrawingReading` uses.
#: `rear` and `iso` are spelled differently on the two sides, which is the whole
#: of the difference -- and the reading's own `_overall_from` keys off these, so
#: a wrong mapping there would silently stop the overall-size check working.
_VIEW_KINDS: dict[str, str] = {
    "front": "front", "rear": "rear", "top": "top", "bottom": "bottom",
    "left": "left", "right": "right", "iso": "isometric",
}

#: Which of the part's axes a view of each direction shows across and up. The
#: same table `drawing._overall_from` uses, for the same purpose: an `iso` view
#: shows all three and pins none, so it is absent rather than guessed at.
_VIEW_AXES: dict[str, tuple[int, int]] = {
    "front": (0, 2), "rear": (0, 2),
    "top": (0, 1), "bottom": (0, 1),
    "left": (1, 2), "right": (1, 2),
}


def rehearse_drawing(recipe: DrawingRecipe, part: PartRecipe) -> dict[str, Any]:
    """What this drawing would say about this part, and whether the two agree.

    The part is rehearsed first, because the comparison needs *resolved*
    numbers: a view asks to dimension `plate_w` and the answer is 120 mm only
    once the part's parameters have been evaluated. That is the same argument
    `drawing.compare` makes for taking a rehearsal rather than a recipe, and it
    is why this takes both recipes rather than a drawing alone.
    """
    from .builder import rehearse

    rehearsed = rehearse(part)
    report: dict[str, Any] = {
        "ok": rehearsed.get("ok", False),
        "drawing": {
            "name": recipe.name,
            "sheet": recipe.sheet,
            "projection": recipe.projection,
            "scale": recipe.scale,
            "template": recipe.template,
            "views": len(recipe.views),
        },
        "findings": [],
        "warnings": [],
        "rehearsed_the_part": rehearsed.get("ok", False),
    }
    if not rehearsed.get("ok", False):
        report["ok"] = False
        report["findings"].append({
            "where": "the part",
            "error": "the part does not rehearse, so there is nothing to draw",
            "hint": "Fix the part first: `validate_recipe` reports what is wrong "
                    "with it. A drawing checked against a part that does not "
                    "build would be checking against nothing.",
        })
        report["part"] = {"findings": rehearsed.get("findings")}
        return report

    resolver = _resolver_for(recipe, part, rehearsed)
    ledger, findings = _ledger(recipe, resolver)
    report["ledger"] = ledger
    report["findings"].extend(findings)
    if findings:
        report["ok"] = False
        return report

    if not recipe.template:
        report["warnings"].append({
            "where": "the sheet",
            "warning": "no template, so the sheet has no title block",
            "why": "A drawing without a title block has no part number, no "
                   "revision and no scale on it, which is not something a "
                   "factory can work from however good the views are.",
        })
    report["warnings"].extend(_views_that_dimension_nothing(recipe))

    reading = as_reading(recipe, ledger)
    round_trip = _translated(compare(reading, rehearsed), recipe)
    report["round_trip"] = round_trip
    report["warnings"].extend(_axes_the_sheet_leaves_unstated(ledger, rehearsed))

    # Surfaced in `warnings` as well as inside the round trip, because that is
    # the field a caller reads first and an under-dimensioned sheet is the
    # finding this whole module is for. A warning rather than a failure: it can
    # be deliberate -- a value covered by a general note, or one the reader is
    # meant to derive -- and a check that refused a legitimate sheet would teach
    # the reader to ignore it.
    if round_trip["undimensioned_count"]:
        names = ", ".join(entry["model"] for entry in round_trip["undimensioned"])
        report["warnings"].append({
            "where": "the sheet",
            "warning": f"{round_trip['undimensioned_count']} number(s) the part "
                       f"states are not dimensioned: {names}",
            "why": "A factory cannot make what the drawing does not say. This is "
                   "the one drawing fault that is invisible on the sheet, because "
                   "every dimension that is there is correct. Add them to a "
                   "view's `dimension`, or leave them if a note covers them.",
        })
    if not round_trip["ok"]:
        report["ok"] = False
    return report


def as_reading(recipe: DrawingRecipe, ledger: dict[str, Any]) -> DrawingReading:
    """The ledger as a :class:`~inventor_mcp.drawing.DrawingReading`.

    So that the produced drawing goes through the same comparison an inspected
    one does. The views carry no `extent`: an extent is what a view *shows*, and
    the only source for it here would be the part itself -- which would make the
    overall-size check compare the part against the part and pass always. What
    the sheet establishes about the part's size is worked out from the dimensions
    it states instead, by :func:`_axes_the_sheet_leaves_unstated`.
    """
    dimensions = [
        DrawingDimension(
            value=entry["value"],
            label=entry["label"],
            kind=entry["kind"],
            view=view["name"],
            reference=entry["reference"],
        )
        for view in ledger["views"]
        for entry in view["dimensions"]
    ]
    return DrawingReading(
        title=recipe.name,
        units=recipe.units,
        projection=recipe.projection,
        scale=recipe.scale,
        views=[
            DrawingView(name=view["name"], kind=_VIEW_KINDS[view["direction"]])
            for view in ledger["views"]
        ],
        dimensions=dimensions,
        notes=list(recipe.notes),
        # A produced sheet states nothing it was not asked to state, so a
        # recipe with no dimensions at all has to be recorded as such rather
        # than failing the reading's own "say what you could not see" rule.
        unreadable=[] if dimensions else ["nothing was asked to be dimensioned"],
    )


def _resolver_for(recipe: DrawingRecipe, part: PartRecipe,
                  rehearsed: dict[str, Any]) -> Resolver:
    """A resolver that knows the part's parameters, reading in the sheet's units.

    The units are the *drawing's*, not the part's: a sheet may be dimensioned in
    inches from a part modelled in millimetres, and a bare number in a drawing
    recipe means what the drawing says it means. The parameters come from the
    rehearsal rather than from the part recipe because they are resolved there --
    a parameter whose value is an expression of another one has a number only
    after the part has been rehearsed.
    """
    kinds = rehearsed.get("parameter_dimensions") or {}
    resolver = Resolver(length_unit=recipe.units, angle_unit=recipe.angle_units)
    for name, value in (rehearsed.get("parameters") or {}).items():
        dim = Dim.ANGLE if kinds.get(name) == "angle" else Dim.LENGTH
        resolver.declare(name, Quantity(value, dim))
    return resolver


def _ledger(recipe: DrawingRecipe,
            resolver: Resolver) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Every view and every dimension it states, resolved -- and what would not resolve.

    A parameter named here that the part does not declare is a finding rather
    than a warning: the sheet would carry a dimension of nothing, and there is
    no version of that which is what the author meant.
    """
    per_unit = to_internal(1.0, recipe.units).value
    views: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    dimensioned: set[str] = set()

    for view in recipe.views:
        entries: list[dict[str, Any]] = []
        for spec, is_reference in ((view.dimension, False), (view.reference, True)):
            for entry in spec:
                try:
                    resolved = _resolve_dimension(resolver, entry, view)
                except InventorMCPError as exc:
                    findings.append({
                        "where": f"view {view.name!r}",
                        "error": str(exc),
                        "hint": getattr(exc, "hint", None) or
                        "Dimension a parameter the part declares, or an "
                        "expression of them. The part's parameters are what a "
                        "drawing is for: they are the numbers somebody chose.",
                    })
                    continue
                angle = resolved.dim == Dim.ANGLE
                entries.append({
                    "label": entry,
                    "expression": resolved.expression,
                    "value": round(resolved.value / (1.0 if angle else per_unit), 6),
                    "kind": "angle" if angle else "linear",
                    "reference": is_reference,
                })
                if not is_reference:
                    dimensioned.add(entry)
        views.append({
            "name": view.name,
            "direction": view.direction,
            "scale": _scale_of(resolver, view),
            "dimensions": entries,
        })

    return {
        "views": views,
        "units": recipe.units,
        "dimensions": sum(len(view["dimensions"]) for view in views),
        "parameters_dimensioned": sorted(dimensioned),
    }, findings


def _resolve_dimension(resolver: Resolver, entry: str, view: DrawingViewSpec) -> Any:
    """One dimension entry, as a length or -- failing that -- as an angle.

    Tried in that order rather than asked of the caller, because a drawing
    dimensions a countersink's 90 degrees beside a plate's 120 mm and making
    somebody label which is which would be asking them to restate what the
    parameter already knows.

    The length attempt is what reports the failure, and deliberately: an entry
    that names nothing fails both ways, and "expected a length" is the more
    useful of the two messages on a sheet whose dimensions are overwhelmingly
    lengths. So the angle attempt's own error is discarded and the length one
    re-raised.
    """
    where = f"dimension {entry!r} on view {view.name!r}"
    try:
        return resolver.length(entry, where)
    except InventorMCPError as exc:
        try:
            return resolver.angle(entry, where)
        except InventorMCPError:
            raise exc from None


def _scale_of(resolver: Resolver, view: DrawingViewSpec) -> float:
    """The view's scale as a number, resolved like any other."""
    return round(resolver.unitless(view.scale, f"scale of view {view.name!r}").value, 6)


def _views_that_dimension_nothing(recipe: DrawingRecipe) -> list[dict[str, Any]]:
    """Views carrying no dimension at all.

    Not an error, and `iso` is excluded outright: an isometric view is there to
    show what the part looks like and dimensioning one is bad practice. Any
    other empty view is a view somebody drew and then did not use, which is
    worth a word.
    """
    idle = [view.name for view in recipe.views
            if view.direction != "iso" and not view.dimension and not view.reference]
    if not idle:
        return []
    return [{
        "where": "views",
        "warning": f"{', '.join(idle)} dimension nothing",
        "why": "A view with no dimensions on it shows the shape and states no "
               "size. That is what an `iso` view is for and this is not one -- "
               "either dimension it or drop it from the sheet.",
    }]


def _axes_the_sheet_leaves_unstated(ledger: dict[str, Any],
                                    rehearsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Axes of the part whose overall size no dimension on the sheet gives.

    The cheapest check there is, in the words `drawing.compare` uses for the
    other direction: a part the right shape and the wrong size passes everything
    else. Here it has to be done from the dimensions rather than from the views,
    because a produced view's extent is whatever the part is -- so asking a view
    what it shows would be asking the part about itself.

    Deliberately weak: it asks whether *some* stated dimension equals the part's
    extent along each axis, not which one, and it says nothing about how the
    dimension is laid out. A drawing can state 120 mm as a hole spacing and pin
    nothing, and this would not know. It catches the axis nobody thought about,
    which is the common case and the one worth catching.
    """
    span = (rehearsed.get("result") or {}).get("span_mm")
    if not span:
        return []
    per_unit = to_internal(1.0, ledger["units"]).value
    stated = [entry["value"] * per_unit * 10
              for view in ledger["views"] for entry in view["dimensions"]]
    unstated = [
        "XYZ"[axis] for axis, extent in enumerate(span)
        if not any(abs(extent - value) <= 5.0e-2 for value in stated)
    ]
    if not unstated:
        return []
    return [{
        "where": "the sheet",
        "warning": f"nothing states the part's overall {', '.join(unstated)}",
        "why": "The part measures " + " x ".join(f"{value:g}" for value in span) +
               " mm, and no dimension on this sheet gives that size on "
               f"{'that axis' if len(unstated) == 1 else 'those axes'}. A part the "
               "right shape and the wrong size passes every other check.",
    }]


#: What `drawing.compare`'s findings mean when the drawing is being produced
#: rather than read. The computation is identical and the reading is not: a
#: number the model asserts that the sheet does not state is not an invention on
#: anybody's part, it is a dimension nobody asked for.
_TRANSLATED = {
    "missing": "states_what_the_part_does_not_have",
    "invented": "undimensioned",
}


def _translated(comparison: dict[str, Any], recipe: DrawingRecipe) -> dict[str, Any]:
    """`compare`'s report, with its vocabulary turned round."""
    out: dict[str, Any] = {"ok": comparison["ok"], "matched": comparison["matched"]}
    for key, renamed in _TRANSLATED.items():
        out[renamed] = comparison.get(key) or []
    if comparison.get("derived"):
        out["derived"] = comparison["derived"]
    out["warnings"] = [
        warning for warning in comparison.get("warnings") or []
        # The reading direction warns when nobody recorded the projection angle.
        # A produced sheet always has one -- `DrawingRecipe.projection` has no
        # "unknown" -- so that warning cannot apply and would only confuse.
        if "projection" not in warning.get("warning", "")
        and "overall size" not in warning.get("warning", "")
    ]
    for entry in out["undimensioned"]:
        entry["why"] = (
            "The part states this number and the sheet does not, so a factory "
            "reading the drawing could not reproduce it. Add it to a view's "
            "`dimension` list, or accept it as deliberately unstated -- a "
            "dimension the part derives from others it does state is reported "
            "under `derived` instead and is not this."
        )
    out["undimensioned_count"] = len(out["undimensioned"])
    out["sheet"] = recipe.name
    return out
