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

import math
from typing import Any, Sequence

from .backend.base import (
    DrawingContents,
    RetrieveRequest,
    ViewInfo,
    ViewRequest,
)
from .drawing import DrawingDimension, DrawingReading, DrawingView, compare
from .errors import ExpressionError, InventorMCPError, RecipeError
from .resolve import Resolver
from .schema import DrawingRecipe, DrawingViewSpec, PartRecipe
from .session import Session
from .units import Dim, Quantity, from_internal, to_internal

#: A recipe's view direction, mapped onto how a *reading* would label the same
#: view, and whether the extent has to be transposed to get there.
#:
#: **The one place the two view vocabularies meet, and it used to be an
#: identity map because nobody had measured that they differ.** A recipe's
#: `direction` is Inventor's naming, which is Y-up: measured on 2027.1,
#: 2026-09-08, `front` shows the XY plane -- the plan of a part modelled flat.
#: A reading's `kind` is the view as the *sheet labels it*, which is the ISO
#: drafting vocabulary a person reads with, where FRONT is the elevation. So
#: Inventor's front is a reading's top, Inventor's top is a reading's front,
#: and the pairs stay pairs.
#:
#: `left` and `right` need more than a rename. Both vocabularies put them on
#: the YZ plane and disagree about which way round it is: Inventor spans Z
#: across and Y up, a reading has Y across and Z up. So the extent is
#: transposed for those two and for nothing else.
#:
#: Getting this wrong silently stops the overall-size check working, because
#: `drawing._overall_from` reconstructs the part's bounding box from the kind
#: and the extent together. `docs/DECISIONS.md` records why the recipe follows
#: Inventor and the reading does not.
_VIEW_KINDS: dict[str, tuple[str, bool]] = {
    "front": ("top", False),
    "rear": ("bottom", False),
    "top": ("front", False),
    "bottom": ("rear", False),
    "left": ("left", True),
    "right": ("right", True),
    "iso": ("isometric", False),
}


def _read_view(view: ViewInfo, per_unit: float
               ) -> tuple[str, list[float] | None]:
    """One placed view as a reading would record it: its kind and its extent.

    The scale comes out here too -- a view drawn at 1:2 spans half what the
    part measures, and the reading is of the *part*.
    """
    extent = ([value / per_unit / view.scale for value in view.extent]
              if view.extent else None)
    kind, values = _as_read(view.direction, extent)
    return kind, ([round(value, 4) for value in values] if values else None)


def _as_read(direction: str, extent: Sequence[float] | None
             ) -> tuple[str, list[float] | None]:
    """How a reading would label this view, and its extent in the reading's order."""
    kind, transposed = _VIEW_KINDS.get(direction, ("front", False))
    if extent is None:
        return kind, None
    values = list(extent)
    if transposed and len(values) == 2:
        values = [values[1], values[0]]
    return kind, values


#: Which way a projected view sits from its parent in **third angle**, as a unit
#: step of (across, up). Third angle is the ASME convention and the one this
#: schema defaults to: a view is drawn on the side you would stand to see it, so
#: the top view goes above the front view and the right-hand view goes right.
#:
#: First angle -- ISO, European -- is the same table negated: you draw the view
#: on the far side, so the top view goes *below*. That is the whole of the
#: difference between the two conventions and the whole reason a sheet has to
#: state which it uses. Reading one as the other mirrors the part, and the part
#: is not what is wrong.
_THIRD_ANGLE_STEP: dict[str, tuple[float, float]] = {
    "top": (0.0, 1.0),
    "bottom": (0.0, -1.0),
    "right": (1.0, 0.0),
    "left": (-1.0, 0.0),
    # An isometric goes in the free corner rather than on an axis: it is not a
    # projection of anything and no convention governs it. Up and to the right
    # is where a drafter puts one, and it is the same corner either way -- so
    # this entry is deliberately unaffected by the projection angle below.
    "iso": (1.0, 1.0),
}


def projected_position(direction: str, projection: str,
                       parent: tuple[float, float],
                       gap: float) -> tuple[float, float]:
    """Where a projected view's centre goes, in the parent's own units.

    The one place `DrawingRecipe.projection` does any work, and the reason the
    field is not decorative. Everything else records it -- the reading carries
    it so a reader knows which convention the sheet uses -- and this is what
    makes the sheet actually follow it.

    `iso` is exempt from the flip on purpose: an isometric view is not a
    projection of anything, so neither convention has an opinion about where it
    goes, and negating its corner in first angle would move it for no reason.
    """
    step = _THIRD_ANGLE_STEP.get(direction)
    if step is None:  # pragma: no cover - the schema refuses the other directions
        raise RecipeError(f"A {direction!r} view cannot be projected from another.")
    if projection == "first_angle" and direction != "iso":
        step = (-step[0], -step[1])
    return (parent[0] + step[0] * gap, parent[1] + step[1] * gap)


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
    ledger, findings = _ledger(recipe, resolver, rehearsed)
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
            # `DrawingReading` compares an angle in degrees -- `compare` calls
            # `math.degrees` on the model's own radians -- so an angle stated on
            # the sheet in gradians or radians is converted here rather than
            # handed over in the sheet's own units and silently mismatched.
            value=(_in_degrees(entry) if entry["kind"] == "angle"
                   else entry["value"]),
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
            DrawingView(name=view["name"],
                        kind=_as_read(view["direction"], None)[0])
            for view in ledger["views"]
        ],
        dimensions=dimensions,
        notes=list(recipe.notes),
        # A produced sheet states nothing it was not asked to state, so a
        # recipe with no dimensions at all has to be recorded as such rather
        # than failing the reading's own "say what you could not see" rule.
        unreadable=[] if dimensions else ["nothing was asked to be dimensioned"],
    )


def _in_degrees(entry: dict[str, Any]) -> float:
    """A ledger angle in degrees, whatever unit the sheet states it in.

    `DrawingReading` has no angle unit of its own: `compare` turns the model's
    radians into degrees and compares the two as numbers, so a reading has to
    speak degrees. The sheet may not -- `DrawingRecipe.angle_units` is the
    caller's -- and this is the one place the two meet.
    """
    return round(to_internal(entry["value"], entry.get("units") or "deg").value
                 * 180.0 / math.pi, 6)


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


def _layout(recipe: DrawingRecipe,
            resolver: Resolver) -> dict[str, tuple[float, float]]:
    """Where every view lands, in the sheet's own units, without making a sheet.

    So that the projection angle's effect is readable from a rehearsal. It is
    the same rule `build_drawing` applies and the same order -- parents before
    the views projected from them -- because two ways of working out a layout
    would be two layouts.
    """
    at: dict[str, tuple[float, float]] = {}
    per_unit = to_internal(1.0, recipe.units).value
    for view in _parents_first(recipe):
        if view.parent is None:
            at[view.name] = tuple(  # type: ignore[assignment]
                round(value / per_unit, 4) for value in _sheet_position(resolver, view))
            continue
        gap = resolver.length(view.gap, f"gap of view {view.name!r}").value / per_unit
        at[view.name] = tuple(  # type: ignore[assignment]
            round(value, 4) for value in projected_position(
                view.direction, recipe.projection, at.get(view.parent, (0.0, 0.0)), gap))
    return at


#: Which quantities a drawing can state as a dimension. Anything else is a
#: category error rather than a hard case: a *count* is not a length, and asking
#: to dimension one used to resolve it as though it were -- the belt pulley's
#: `lighten_count` of 5 came out as a 50 mm dimension nobody had drawn, and the
#: only reason it was caught at all is that the part has no such number either.
#: Found by pointing a drawing at every shipped part.
_DIMENSIONABLE = {Dim.LENGTH, Dim.ANGLE}


def _refuse_what_cannot_be_a_dimension(
        recipe: DrawingRecipe, rehearsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Requested dimensions naming a parameter no drawing could state.

    Only bare parameter names are checked. An expression is resolved and its own
    dimension checked there, and one mixing a count into a length -- `pitch *
    holes` -- is a length by then and perfectly drawable.
    """
    kinds = rehearsed.get("parameter_dimensions") or {}
    findings: list[dict[str, Any]] = []
    for view in recipe.views:
        for entry in list(view.dimension) + list(view.reference):
            kind = kinds.get(entry.strip())
            if kind is None or Dim(kind) in _DIMENSIONABLE:
                continue
            described = "unitless -- a count or a ratio" if kind == "unitless" else kind
            findings.append({
                "where": f"view {view.name!r}",
                "error": f"{entry!r} is {described}, so it cannot be a dimension.",
                "hint": "A drawing states lengths and angles. A count belongs in "
                        "a note or a callout -- '4 HOLES EQUALLY SPACED' -- and "
                        "the dimension to state is the spacing itself.",
            })
    return findings


def _ledger(recipe: DrawingRecipe, resolver: Resolver,
            rehearsed: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Every view and every dimension it states, resolved -- and what would not resolve.

    A parameter named here that the part does not declare is a finding rather
    than a warning: the sheet would carry a dimension of nothing, and there is
    no version of that which is what the author meant.
    """
    per_unit = to_internal(1.0, recipe.units).value
    layout = _layout(recipe, resolver)
    views: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = _refuse_what_cannot_be_a_dimension(
        recipe, rehearsed)
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
                    # Angles are radians internally, so an angle divided by the
                    # *length* factor came out as radians labelled `angle` --
                    # 1.5 deg reported as 0.02618, which `compare` then read as
                    # 0.02618 degrees and called a number the part does not
                    # have. Found by pointing a drawing at every shipped part.
                    "value": round(
                        from_internal(resolved.value, recipe.angle_units) if angle
                        else resolved.value / per_unit, 6),
                    "kind": "angle" if angle else "linear",
                    "units": recipe.angle_units if angle else recipe.units,
                    "reference": is_reference,
                })
                if not is_reference:
                    dimensioned.add(entry)
        views.append({
            "name": view.name,
            "direction": view.direction,
            "at": list(layout[view.name]),
            "projected_from": view.parent,
            "scale": (_parent_scale(recipe, resolver, view.parent) if view.parent
                      else _scale_of(resolver, view)),
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


def _translated(comparison: dict[str, Any], recipe: DrawingRecipe, *,
                sized: bool = False) -> dict[str, Any]:
    """`compare`'s report, with its vocabulary turned round.

    `sized` says whether the reading carried view extents. It decides one
    warning's fate and not by taste: "no overall size to check the model
    against" is a real finding about a sheet whose views give no size, and it is
    noise on a ledger that was never going to give one -- `as_reading` supplies
    no extents on purpose, so the warning would fire every time and mean
    nothing.
    """
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
        and (sized or "overall size" not in warning.get("warning", ""))
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


def build_drawing(session: Session, recipe: DrawingRecipe, part: PartRecipe, *,
                  part_doc_id: str | None = None,
                  part_path: str | None = None) -> dict[str, Any]:
    """Make the drawing, then read it back off the sheet and check it.

    The round trip, and the order matters: the sheet is read from the backend
    rather than reported from the recipe, so a dimension the retrieval could not
    find is absent from the check and a view whose direction did not mean what
    its name said has an extent that says so. Reading back what you just asked
    for proves nothing; reading back what is *there* is the test.

    The part is built first unless `part_doc_id` names one already open. Both
    are useful: a drawing of a part built in the same call is the ordinary case,
    and a drawing of a part somebody has open is what you want when the part
    took a minute to build.

    **A drawing view is a reference to a model file, so the part has to be on
    disk.** `part_path` is where to save it, and it is a separate argument
    rather than something worked out from the recipe's name because writing a
    file is the caller's decision to make: a part that is already saved is left
    exactly where it is, and one that is not is saved only when a path was
    given. Without it Inventor refuses the first view -- measured on 2027.1,
    2026-09-08, three views refused in a row with nothing but "Exception
    occurred", which is the failure this argument exists to make impossible.
    """
    from .builder import build_part

    backend = session.backend
    report: dict[str, Any] = {
        "ok": True,
        "name": recipe.name,
        "units": recipe.units,
        "views": [],
        "findings": [],
        "warnings": [],
    }
    if part_doc_id is None:
        built = build_part(session, part)
        report["part"] = {"ok": built["ok"], "document": built.get("document"),
                          "errors": built.get("errors")}
        if not built["ok"]:
            report["ok"] = False
            report["findings"].append({
                "where": "the part",
                "error": "the part did not build, so there is nothing to draw",
                "hint": "The part's own errors are under `part`. A drawing of a "
                        "part that failed would be a sheet of empty views.",
            })
            return report
        part_doc_id = built["document"]
    else:
        report["part"] = {"ok": True, "document": part_doc_id, "reused": True}

    # On disk before any view is placed, because a view references a file. Only
    # when a path was given and only when the part has none: a part somebody
    # already saved keeps its own location, and nothing here writes over it.
    saved_to = _on_disk(backend, part_doc_id, part_path)
    if saved_to is not None:
        report["part"]["path"] = saved_to

    # Checked before any sheet exists, and it has to be here rather than left to
    # the caller having rehearsed: a category error like dimensioning a count
    # would otherwise reach the retrieval, find nothing, and be reported as a
    # dimension that did not arrive -- which is what a typo looks like too. The
    # two have different fixes, so they need different answers.
    rehearsed = rehearse_the_part(session, part, part_doc_id)
    refused = _refuse_what_cannot_be_a_dimension(recipe, rehearsed)
    if refused:
        report["ok"] = False
        report["findings"].extend(refused)
        return report

    document = backend.new_drawing(
        recipe.name, template=recipe.template, sheet=recipe.sheet, units=recipe.units)
    report["document"] = document.id
    report["sheet"] = document.as_dict().get("detail")

    resolver = _resolver_for(recipe, part, rehearsed)
    at: dict[str, tuple[float, float]] = {}
    for view in _parents_first(recipe):
        try:
            request = _view_request(recipe, resolver, view, part_doc_id, at)
            placed = backend.place_view(document.id, request)
            at[view.name] = request.at
        except Exception as exc:
            report["ok"] = False
            report["findings"].append({
                "where": f"view {view.name!r}",
                "error": str(exc),
                "hint": getattr(exc, "hint", None),
            })
            continue
        entry: dict[str, Any] = {"view": placed.as_dict(), "dimensions": []}
        report["views"].append(entry)
        if not view.dimension and not view.reference:
            continue
        try:
            entry["dimensions"] = [
                info.as_dict() for info in backend.retrieve_dimensions(
                    document.id,
                    RetrieveRequest(view=view.name,
                                    parameters=list(view.dimension),
                                    reference=list(view.reference)))
            ]
        except Exception as exc:
            report["ok"] = False
            report["findings"].append({
                "where": f"view {view.name!r}",
                "error": str(exc),
                "hint": getattr(exc, "hint", None),
            })

    contents = backend.read_drawing(document.id)
    report["read_back"] = contents.as_dict()
    report["warnings"].extend(_dimensions_that_did_not_reach_the_sheet(
        recipe, contents, rehearsed))
    report["warnings"].extend(_views_that_are_not_what_they_asked_for(recipe, contents))
    report["warnings"].extend(_dimensions_that_state_something_else(contents))
    reading = reading_of(recipe, contents)
    report["round_trip"] = _translated(
        compare(reading, rehearsed), recipe,
        sized=any(view.extent for view in reading.views))
    if not report["round_trip"]["ok"]:
        report["ok"] = False
    return report


def rehearse_the_part(session: Session, part: PartRecipe,
                      part_doc_id: str) -> dict[str, Any]:
    """The part's rehearsal, for the comparison to have resolved numbers to use.

    Rehearsed in the simulator rather than measured off the built part, and the
    reason is what the comparison is for: it asks whether the sheet states the
    numbers the *recipe* says the part has. Measuring the built part instead
    would fold a modelling fault into a drawing check and report it in the wrong
    place -- `build_part`'s own divergence check is what catches that.
    """
    from .builder import rehearse

    return rehearse(part)


def reading_of(recipe: DrawingRecipe, contents: DrawingContents) -> DrawingReading:
    """A sheet that exists, as a `DrawingReading`.

    The counterpart of :func:`as_reading`, which reads a ledger the sheet was
    never made from. This one reads the sheet itself, so a dimension the
    retrieval could not place is simply not here.
    """
    per_unit = to_internal(1.0, recipe.units).value
    dimensions = [
        DrawingDimension(
            # Degrees for an angle, for the reason `_in_degrees` gives: a
            # backend states an angle in radians and a reading compares degrees.
            value=round(entry.value * 180.0 / math.pi if entry.kind == "angle"
                        else entry.value / per_unit, 6),
            label=entry.parameter or entry.expression or entry.id,
            kind=entry.kind,
            view=entry.view,
            reference=entry.reference,
        )
        for entry in contents.dimensions
    ]
    return DrawingReading(
        title=recipe.name,
        units=recipe.units,
        projection=recipe.projection,
        scale=recipe.scale,
        views=[
            # The extent comes off the sheet, so the overall-size check has
            # something to compare -- and how much that is worth depends on
            # which backend drew it, which is worth being exact about. On
            # Inventor the size is Inventor's, measured from the view it
            # actually placed, and the check is real. On the simulator the
            # extent is computed from the part's own bounding box, so there the
            # check compares the part with itself and can only fail if the
            # scale arithmetic is wrong. `as_reading` supplies no extent at all
            # rather than that weaker version.
            #
            # Both go through `_as_read` together, because the label and the
            # order of the two numbers are one answer: a left view's extent
            # means Y-then-Z to a reading and came off Inventor as Z-then-Y.
            DrawingView(name=view.name, kind=_read_view(view, per_unit)[0],
                        extent=_read_view(view, per_unit)[1])
            for view in contents.views
        ],
        dimensions=dimensions,
        notes=list(recipe.notes),
        unreadable=[] if dimensions else ["the sheet carries no dimension"],
    )


def _parents_first(recipe: DrawingRecipe) -> list[DrawingViewSpec]:
    """The views in an order where every parent comes before its children.

    A projected view's position is worked out from where its parent actually
    went, so the parent has to have gone somewhere first. Recipe order is
    respected within that, and a view whose parent is unknown or circular is
    left in place rather than dropped -- `place_view` refuses it with a message
    naming the sheet's views, which is more use than this quietly reordering
    around a mistake.
    """
    remaining = list(recipe.views)
    ordered: list[DrawingViewSpec] = []
    placed: set[str] = set()
    while remaining:
        ready = [view for view in remaining
                 if view.parent is None or view.parent in placed]
        if not ready:
            # A parent that does not exist, or two views projected from each
            # other. Hand the rest over in recipe order and let the backend say
            # so by name.
            ordered.extend(remaining)
            break
        for view in ready:
            ordered.append(view)
            placed.add(view.name)
            remaining.remove(view)
    return ordered


def _on_disk(backend: Any, part_doc_id: str, part_path: str | None) -> str | None:
    """Where the part is on disk, saving it to *part_path* if it is nowhere.

    Returns the path the drawing will reference, or None where the question
    could not be answered -- a backend that does not report a path, which is
    not a reason to refuse to draw.

    A part that already has a file is never re-saved to a new one. Somebody who
    passes `part_path` while drawing a part they opened from elsewhere means
    "put it here if it is nowhere", not "move it".
    """
    try:
        where = backend.document_path(part_doc_id)
    except Exception:
        return None
    if where:
        return str(where)
    if not part_path:
        return None
    return str(backend.save_document(part_doc_id, part_path).path or part_path)


def _view_request(recipe: DrawingRecipe, resolver: Resolver, view: DrawingViewSpec,
                  part_doc_id: str, at: dict[str, tuple[float, float]]) -> ViewRequest:
    """One view's request, with a projected view's position worked out.

    A projected view also takes its *parent's* scale rather than its own: that
    is what Inventor does, and a request carrying a scale the parent does not
    have would be refused by both backends. Saying so here means the recipe's
    own `scale` on a projected view is ignored rather than fought over -- and
    the schema says as much.
    """
    scale = _scale_of(resolver, view)
    if view.parent is None:
        return ViewRequest(
            part_doc_id=part_doc_id, name=view.name, direction=view.direction,
            at=_sheet_position(resolver, view), scale=scale, style=view.style)
    parent_at = at.get(view.parent, (0.0, 0.0))
    gap = resolver.length(view.gap, f"gap of view {view.name!r}").value
    return ViewRequest(
        part_doc_id=part_doc_id, name=view.name, direction=view.direction,
        at=projected_position(view.direction, recipe.projection, parent_at, gap),
        scale=_parent_scale(recipe, resolver, view.parent),
        style=view.style, parent=view.parent,
    )


def _parent_scale(recipe: DrawingRecipe, resolver: Resolver, name: str) -> float:
    """The scale of the named view, since a projected view inherits it."""
    for view in recipe.views:
        if view.name == name:
            return (_parent_scale(recipe, resolver, view.parent)
                    if view.parent else _scale_of(resolver, view))
    return 1.0


def _sheet_position(resolver: Resolver, view: DrawingViewSpec) -> tuple[float, float]:
    """Where a base view goes, in cm, resolved like every other length here.

    A base view without `at` lands at the sheet's origin. Not the sheet's
    centre, which would be the friendlier answer and needs a sheet size the
    template decides -- so it would be a guess dressed as a convenience.
    """
    if view.at is None:
        return (0.0, 0.0)
    across, up = view.at
    return (resolver.length(across, f"position of view {view.name!r}").value,
            resolver.length(up, f"position of view {view.name!r}").value)


def _dimensions_that_did_not_reach_the_sheet(
        recipe: DrawingRecipe, contents: DrawingContents,
        rehearsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Parameters the recipe asked to dimension that the sheet does not carry.

    The check that only exists because the sheet is read back. A retrieval can
    come up empty for a reason nothing static could know: Inventor can only
    retrieve a dimension the model actually holds, so a parameter that drives no
    sketch dimension and no feature value has nothing to retrieve -- and the
    caller has asked for something no sheet can show.
    """
    asked = {name for view in recipe.views
             for name in list(view.dimension) + list(view.reference)}
    arrived = {entry.parameter for entry in contents.dimensions if entry.parameter}
    absent = sorted(asked - arrived)
    if not absent:
        return []
    feeds = _parameters_they_feed(absent, rehearsed)
    return [{
        "where": "the sheet",
        "warning": f"asked for but not on the sheet: {', '.join(absent)}",
        "why": "A dimension can only be retrieved if the model holds one, and a "
               "parameter can drive geometry without any dimension stating it."
               + (" " + "; ".join(
                   f"{name} drives geometry only through {', '.join(through)}, so "
                   f"no dimension mentions it -- dimension {through[0]} instead"
                   for name, through in feeds.items()) if feeds else "")
               + (" The rest drive no sketch dimension and no feature value at "
                  "all, which is a warning on the part as well."
                  if len(feeds) < len(absent) else ""),
    }]


def _parameters_they_feed(absent: Sequence[str],
                          rehearsed: dict[str, Any]) -> dict[str, list[str]]:
    """For each missing name, the other parameters whose expressions use it.

    The commonest reason a requested dimension never arrives, and it took
    pointing a drawing at every shipped part to see it: `pipe_bend`'s `wall`
    appears only in `tube_od = tube_id + 2 * wall`, and `moulded_housing`'s
    `boss_wall` only in `boss_d = boss_hole_d + 2 * boss_wall`. Both drive the
    part, neither is stated by any dimension -- the sketch says `tube_od` -- so
    there is nothing to retrieve and the useful answer is the name of the
    parameter that *is* stated.
    """
    from .expressions import referenced_parameters

    expressions = rehearsed.get("parameter_expressions") or {}
    feeds: dict[str, list[str]] = {}
    for name in absent:
        through = []
        for other, expression in expressions.items():
            if other == name or not isinstance(expression, str):
                continue
            try:
                if name in referenced_parameters(expression):
                    through.append(other)
            except Exception:
                continue
        if through:
            feeds[name] = sorted(through)
    return feeds


def _mentions_a_parameter_other_than(expression: str, parameter: str) -> bool:
    """Whether *expression* is a formula naming something other than *parameter*.

    The line between "the sheet states a different number from the one asked
    for" and "the sheet shows a number, as drawings do". A dimension's text is
    a number and a locale's decimal separator; an expression is names and
    operators. Only the second is worth warning about.
    """
    from .expressions import referenced_parameters

    try:
        names = referenced_parameters(expression)
    except ExpressionError:
        # Not parseable as an expression, so it is text: a number, a locale's
        # decimal separator, whatever Inventor put on the sheet. Narrow on
        # purpose -- the first version caught `Exception`, which swallowed the
        # `NameError` from this import being missing and turned the whole
        # warning off. A bare except is how a check stops checking silently.
        return False
    return bool(names) and set(names) != {parameter}


def _dimensions_that_state_something_else(
        contents: DrawingContents) -> list[dict[str, Any]]:
    """Dimensions retrieved for a parameter whose value is not that parameter's.

    Not a fault, and worth saying anyway. Retrieval can only offer dimensions the
    model *holds*, so a parameter the model never states on its own comes back as
    the dimension it drives: the mounting plate expresses an `edge_margin` of 12
    as a hole spacing of `plate_w - 2 * edge_margin`, and a drawing asking to
    dimension the margin gets 96 mm. The sheet is right, the holes are pinned,
    and the number the author named is nowhere on it -- which is exactly the
    thing somebody should be told rather than left to notice.

    **It has to state a formula, not merely a number, and that took a live run
    to notice.** On Inventor 2027.1 a retrieved dimension answers neither
    `ModelDimension.Parameter.Expression` nor `Parameter.Expression`, so the
    expression falls back to the text on the sheet -- `'120,00'` for a 120 mm
    plate, in whatever decimal separator the seat is set to. That is never the
    parameter's name, so the first version of this warned about **every**
    dimension on every live sheet, which is the fastest way to make a warning
    ignored. It fires only where the expression references parameters and they
    are not the one asked for; a bare number references none.

    Under the choose-then-retrieve route this should now never fire at all,
    because an annotation is only chosen when its expression *is* the wanted
    parameter. It stays for the legacy retrieve-then-filter fallback and for a
    sheet read back that this session did not place.
    """
    indirect = [
        f"{entry.parameter} is stated as {entry.expression!r}"
        for entry in contents.dimensions
        if entry.parameter and entry.expression
        and entry.expression.strip() != entry.parameter
        and _mentions_a_parameter_other_than(entry.expression, entry.parameter)
    ]
    if not indirect:
        return []
    return [{
        "where": "the sheet",
        "warning": "a dimension states the value its parameter drives rather "
                   "than the parameter: " + "; ".join(indirect),
        "why": "Retrieval can only place a dimension the model holds. Where a "
               "parameter is never stated on its own -- a margin expressed as a "
               "spacing, say -- the sheet carries the dimension it drives "
               "instead. The geometry is pinned either way; what changes is "
               "which number a reader of the drawing sees, and it is not the one "
               "the recipe named.",
    }]


def _views_that_are_not_what_they_asked_for(
        recipe: DrawingRecipe, contents: DrawingContents) -> list[dict[str, Any]]:
    """Views the sheet reports as facing a different way than was asked.

    Worth its own check because of defect 4: `capture_view`'s orientation names
    do not describe what they return, and a drawing view reaches Inventor
    through a similarly-named enum. If the same thing is true here, this is what
    says so -- and it says it from the sheet rather than from the request, which
    is the only way it could.
    """
    wanted = {view.name: view.direction for view in recipe.views}
    wrong = [
        f"{view.name} asked for {wanted[view.name]} and reports {view.direction}"
        for view in contents.views
        if view.name in wanted and view.direction not in (wanted[view.name], "unknown")
    ]
    if not wrong:
        return []
    return [{
        "where": "views",
        "warning": "a view is not facing the way it was asked to: " + "; ".join(wrong),
        "why": "Read off the sheet rather than from the request. Defect 4 is the "
               "same failure on `capture_view`, where `front` returns a top view, "
               "so a drawing view's orientation enum meaning something else is "
               "exactly the thing worth checking. The part is not wrong; the "
               "sheet shows it from somewhere else.",
    }]
