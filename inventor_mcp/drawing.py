"""Reading a 2D drawing into something a model can be checked against.

A drawing is not a picture of a part; it is a *specification* of one, and the
difference matters here. Tracing its outlines would give geometry with no
parameters, which is the thing this project exists not to produce. Reading its
dimensions gives you the driving values, and those become parameters.

So the flow is: look at the drawing, write down what it *says* as a
:class:`DrawingReading`, then write a recipe whose parameters are those values.
The reading is worth writing down separately rather than going straight to a
recipe, because it can then be checked against the model:

* every dimension on the drawing must reach the model, or it was misread or
  ignored;
* every number the model asserts should appear on the drawing, or it was
  invented;
* the part's overall size must agree with what the views show.

A drawing is redundantly specified on purpose -- three views of one part, each
constraining the others -- and that redundancy is exactly what makes these
checks possible.
"""

from __future__ import annotations

from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .units import to_internal

#: How close two lengths must be to count as the same dimension, in cm.
#: Generous enough for a value read off a drawing to a tenth of a millimetre.
MATCHES = 5.0e-3


class DrawingDimension(BaseModel):
    """One dimension as the drawing states it."""

    model_config = ConfigDict(extra="forbid")

    value: float = Field(description="The number on the drawing, in the reading's units.")
    label: str = Field(description="What it measures, in the drawing's own words.")
    kind: Literal["linear", "diameter", "radius", "angle", "thickness", "pitch"] = "linear"
    view: str | None = Field(None, description="Which view it appears on.")
    reference: bool = Field(
        False,
        description="A reference dimension -- bracketed, or marked REF. It restates "
        "something already fixed elsewhere, so it drives nothing and is not "
        "expected to appear in the model as its own parameter.",
    )
    tolerance: str | None = Field(
        None, description="As written: '±0.1', 'H7', '+0.2/-0'. Recorded, not modelled."
    )
    count: int | None = Field(
        None, ge=1, description="For a repeated feature: '4 × ⌀6.6' is count 4."
    )


class DrawingView(BaseModel):
    """One view on the sheet."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="As labelled: 'FRONT', 'SECTION A-A', 'DETAIL B'.")
    kind: Literal[
        "front", "rear", "top", "bottom", "left", "right",
        "section", "detail", "isometric", "auxiliary",
    ] = "front"
    shows: str | None = Field(
        None, description="What this view establishes, in a phrase."
    )
    extent: list[float] | None = Field(
        None,
        min_length=2,
        max_length=2,
        description="The overall size the view shows, [across, up], in the "
        "reading's units. Two of these from perpendicular views pin the part's "
        "bounding box, which is the cheapest check there is.",
    )


class DrawingReading(BaseModel):
    """What a drawing says, written down before any modelling.

    Filling this in is the act of reading the drawing. It is deliberately not a
    recipe: it records the specification, and the recipe is a separate claim
    about how to build something that meets it. Keeping them apart is what lets
    one be checked against the other.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, description="The part name or number.")
    units: Literal["mm", "cm", "m", "in", "ft"] = Field(
        "mm", description="What the numbers on the drawing are in."
    )
    projection: Literal["first_angle", "third_angle", "unknown"] = Field(
        "unknown",
        description="First angle (ISO/European) or third angle (ASME/US). It "
        "decides which side of the front view the right-hand view is drawn on, "
        "so getting it wrong mirrors the part. Look for the projection symbol; "
        "say 'unknown' rather than guessing.",
    )
    scale: str | None = Field(None, description="As written: '1:1', '2:1'.")
    material: str | None = None
    views: list[DrawingView] = Field(default_factory=list)
    dimensions: list[DrawingDimension] = Field(default_factory=list)
    notes: list[str] = Field(
        default_factory=list,
        description="Everything in the note block: finishes, tolerances, "
        "'ALL FILLETS R2 UNLESS STATED', heat treatment.",
    )
    overall: list[float] | None = Field(
        None,
        min_length=3,
        max_length=3,
        description="The part's overall size [x, y, z] in the reading's units, "
        "if the views give it. Checked against the model's bounding box.",
    )
    unreadable: list[str] = Field(
        default_factory=list,
        description="Anything on the sheet you could not make out. Say so here "
        "rather than guessing -- a missed dimension is recoverable, an invented "
        "one is not.",
    )

    @model_validator(mode="after")
    def _at_least_something(self) -> "DrawingReading":
        if not self.dimensions and not self.unreadable:
            raise ValueError(
                "A reading with no dimensions and nothing marked unreadable is "
                "not a reading. Record what the drawing says, or say what you "
                "could not see."
            )
        return self

    def driving(self) -> list[DrawingDimension]:
        """The dimensions that should end up driving the model."""
        return [d for d in self.dimensions if not d.reference and d.kind != "pitch"]

    def in_cm(self, value: float) -> float:
        return to_internal(value, self.units).value


def compare(reading: DrawingReading, rehearsal: dict[str, Any]) -> dict[str, Any]:
    """Check a rehearsed recipe against the drawing it claims to implement.

    Takes the output of :func:`inventor_mcp.builder.rehearse` rather than a
    recipe, because the comparison needs *resolved* numbers: a recipe says
    ``"plate_w - 2 * edge_margin"`` and the drawing says 96.
    """
    report: dict[str, Any] = {
        "ok": True,
        "matched": [],
        "missing": [],
        "invented": [],
        "warnings": [],
    }

    if reading.projection == "unknown":
        report["warnings"].append({
            "warning": "the projection angle was not read",
            "why": "First and third angle put the right-hand view on opposite "
                   "sides of the front view, so reading one as the other mirrors "
                   "the part. Look for the projection symbol near the title block.",
        })
    for item in reading.unreadable:
        report["warnings"].append({
            "warning": f"unreadable on the drawing: {item}",
            "why": "The model cannot be complete while this is unknown.",
        })

    asserted = _numbers_the_model_asserts(rehearsal)
    for dimension in reading.driving():
        if dimension.kind == "angle":
            continue  # angles are not in the length pool; compared separately
        wanted = reading.in_cm(dimension.value)
        near = [name for name, value in asserted.items() if abs(value - wanted) <= MATCHES]
        if near:
            report["matched"].append({
                "drawing": f"{dimension.label} = {dimension.value} {reading.units}",
                "model": sorted(near),
            })
        else:
            report["ok"] = False
            report["missing"].append({
                "drawing": f"{dimension.label} = {dimension.value} {reading.units}",
                "why": "No parameter or driving dimension in the model has this "
                       "value. It was either misread or left out.",
            })

    wanted_values = [reading.in_cm(d.value) for d in reading.dimensions]
    derived = _derived_from_parameters(rehearsal)
    for name, value in sorted(asserted.items()):
        if any(abs(value - other) <= MATCHES for other in wanted_values):
            continue
        if name in derived:
            # Computed from values that *are* on the drawing, which is exactly
            # what a parametric model should do. Not a finding.
            report.setdefault("derived", []).append({
                "model": name,
                "value_in_drawing_units": round(value / reading.in_cm(1.0), 4),
            })
            continue
        report["invented"].append({
            "model": name,
            "value_in_drawing_units": round(value / reading.in_cm(1.0), 4),
            "why": "A literal the drawing does not give. Either read it off the "
                   "drawing, or write it as an expression of values that are.",
        })

    box = _overall_from(reading)
    measured = (rehearsal.get("result") or {}).get("span_mm")
    if box and measured:
        for axis, (wanted_mm, got_mm) in enumerate(zip(box, measured)):
            if abs(wanted_mm - got_mm) > MATCHES * 10:
                report["ok"] = False
                report["missing"].append({
                    "drawing": f"overall {'XYZ'[axis]} = {wanted_mm} mm",
                    "why": f"The model measures {got_mm} mm on that axis.",
                })
    elif not box:
        report["warnings"].append({
            "warning": "no overall size to check the model against",
            "why": "Give `overall`, or an `extent` on two perpendicular views. "
                   "It is the cheapest check there is: a part the right shape and "
                   "the wrong size passes everything else.",
        })
    return report


def _numbers_the_model_asserts(rehearsal: dict[str, Any]) -> dict[str, float]:
    """Every length the model states, in cm, keyed by where it came from."""
    found: dict[str, float] = {}
    for name, value in (rehearsal.get("parameters") or {}).items():
        found[f"parameter {name}"] = value
    for name, summary in (rehearsal.get("sketches") or {}).items():
        for expression, value in (summary.get("driving") or {}).items():
            found[f"{name}: {expression}"] = value
    return found


def _derived_from_parameters(rehearsal: dict[str, Any]) -> set[str]:
    """The asserted numbers that are computed rather than written down.

    A parametric model is supposed to derive values: a hole spacing of 96 from
    a 120 plate and a 12 margin is right, and reporting it as an invention
    would teach the reader to ignore the field. What matters is a bare literal
    the drawing never gives.
    """
    from .expressions import referenced_parameters

    def references(text: str) -> bool:
        try:
            return bool(referenced_parameters(text))
        except Exception:
            return False

    computed: set[str] = set()
    for name, expression in (rehearsal.get("parameter_expressions") or {}).items():
        if references(expression):
            computed.add(f"parameter {name}")
    for sketch, summary in (rehearsal.get("sketches") or {}).items():
        for expression in (summary.get("driving") or {}):
            if references(expression):
                computed.add(f"{sketch}: {expression}")
    return computed


def _overall_from(reading: DrawingReading) -> list[float] | None:
    """The part's overall size in mm, from `overall` or from two views."""
    if reading.overall:
        return [reading.in_cm(value) * 10 for value in reading.overall]

    #: Which model axes a view's [across, up] correspond to.
    axes = {
        "front": (0, 2), "rear": (0, 2),
        "top": (0, 1), "bottom": (0, 1),
        "left": (1, 2), "right": (1, 2),
    }
    spans: dict[int, float] = {}
    for view in reading.views:
        pair = axes.get(view.kind)
        if pair is None or view.extent is None:
            continue
        for axis, value in zip(pair, view.extent):
            spans.setdefault(axis, reading.in_cm(value) * 10)
    if len(spans) == 3:
        return [spans[0], spans[1], spans[2]]
    return None
