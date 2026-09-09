"""The backend contract.

Two implementations exist:

* :class:`~inventor_mcp.backend.com.backend.ComBackend` drives a live Autodesk
  Inventor session over COM (Windows only).
* :class:`~inventor_mcp.backend.mock.backend.MockBackend` keeps an in-memory
  model.  It is what the test suite runs against and what lets a recipe be
  written and checked on a machine with no Inventor installed.

Requests reaching a backend are already fully resolved: every length is in
centimetres, every angle in radians, and every driving value carries the
expression string Inventor should store alongside it.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Sequence

from ..errors import DocumentError
from ..plan import SketchPlan


def _same_file_key(path: str) -> str:
    """A form of *path* that compares equal for two names of the same file.

    Absolute, so a relative name matches the absolute one it resolves to, and
    case-folded through ``normcase`` because the machine that matters here runs
    Windows -- where `Bracket.ipt` and `bracket.ipt` are one file and comparing
    them raw would miss the collision this is looking for. ``normcase`` also
    settles the separators, so a path written with forward slashes matches the
    backslashed one Inventor hands back.

    Deliberately not ``realpath``: resolving symlinks needs the file to exist,
    and the interesting case is a path being written for the first time.
    """
    return os.path.normcase(os.path.abspath(path))


def _clean(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    return value


@dataclass
class Info:
    def as_dict(self) -> dict[str, Any]:
        return {key: _clean(value) for key, value in asdict(self).items() if value is not None}


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


@dataclass
class AppInfo(Info):
    backend: str
    connected: bool
    version: str | None = None
    build: str | None = None
    visible: bool | None = None
    documents: int = 0
    note: str | None = None


@dataclass
class DocInfo(Info):
    id: str
    name: str
    kind: str = "part"
    path: str | None = None
    units: str = "mm"
    angle_units: str = "deg"
    active: bool = False
    modified: bool = False
    #: Anything worth saying about how this document came to be -- which import
    #: route worked, what arrived in it. Absent unless there is something to say,
    #: so every existing result is unchanged.
    detail: dict[str, Any] | None = None


@dataclass
class ParamInfo(Info):
    name: str
    expression: str
    value: float
    units: str
    kind: str = "user"
    comment: str = ""
    key: bool = False
    consumed_by: list[str] = field(default_factory=list)


@dataclass
class SketchInfo(Info):
    id: str
    name: str
    plane: str
    entities: int = 0
    constraints: int = 0
    dimensions: int = 0
    profiles: int = 0
    hole_centers: int = 0
    fully_constrained: bool | None = None
    degrees_of_freedom: int | None = None
    #: Constraints Inventor inferred for itself, so ours were not needed. Benign.
    inferred_constraints: int = 0
    #: Constraints Inventor refused as dependent on the others. The sketch still
    #: closes, but a degree of freedom is left in it.
    refused_constraints: int = 0
    #: Dimensions Inventor accepted *and* stored an expression for. These are
    #: the only ones that actually drive anything.
    driving_dimensions: int = 0
    #: Dimensions Inventor refused, or would not store an expression for. The
    #: sketch survives with a degree of freedom left in it.
    refused_dimensions: int = 0
    #: Recipe parameters that reached at least one driving dimension. A sketch
    #: with closed loops and none of these is not parametric, however many
    #: dimensions it appears to carry.
    driven_parameters: list[str] = field(default_factory=list)
    #: Expressions the planner had to drop, so a run names the parameter that
    #: did not reach the model.
    undriven_expressions: list[str] = field(default_factory=list)
    #: Where the sketch's own axes point in model space, as measured, and the
    #: transform applied to the recipe's coordinates to suit them. A plane's
    #: internal orientation is not derivable from its name, and getting it wrong
    #: moves geometry silently, so what was measured is worth reporting.
    axes: str | None = None


@dataclass
class FeatureInfo(Info):
    id: str
    name: str
    kind: str
    suppressed: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class TopoInfo(Info):
    """An edge or face that a selector matched."""

    id: str
    kind: Literal["edge", "face"]
    description: str
    feature: str | None = None
    midpoint: tuple[float, float, float] | None = None
    normal: tuple[float, float, float] | None = None
    direction: tuple[float, float, float] | None = None
    length: float | None = None
    area: float | None = None
    geometry: str | None = None
    #: "convex" (an outside corner) or "concave" (an inside one), when it can
    #: be determined. ``None`` means unknown, which never matches either filter.
    convexity: str | None = None
    #: How that was decided -- "loops" is exact, "sampled" is a heuristic that
    #: a face with a hole in it can fool. Worth showing, because a wrong
    #: convexity puts a fillet on the wrong edge with nothing else to see.
    convexity_from: str | None = None


@dataclass
class MassProps(Info):
    volume: float
    area: float
    mass: float | None = None
    density: float | None = None
    material: str | None = None
    center_of_mass: tuple[float, float, float] | None = None
    #: Where the centroid came from, when one is reported at all. Read it: the
    #: simulator has no centroid to give and says so here rather than handing
    #: back the bounding box's centre, which ignores every void the part has and
    #: so does not move when a bolt circle does. ``None`` from a backend that
    #: never said.
    center_of_mass_from: str | None = None
    bounding_box: tuple[float, float, float, float, float, float] | None = None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


@dataclass
class Driven:
    """A value plus the expression that drives it."""

    expression: str
    value: float

    def as_dict(self) -> dict[str, Any]:
        return {"expression": self.expression, "value": self.value}


@dataclass
class AxisSpec:
    """Where a revolve or pattern gets its axis from."""

    kind: Literal["work_axis", "sketch_line", "edge"]
    value: str  # "x" | "y" | "z", a sketch entity label, or an edge handle
    sketch: str | None = None


@dataclass
class ResolvedSelector:
    """A :class:`~inventor_mcp.schema.Selector` with lengths converted to cm."""

    kind: Literal["edge", "face"] = "edge"
    feature: str | None = None
    filter: str = "all"
    near: tuple[float, float, float] | None = None
    within: float | None = None
    min_length: float | None = None
    max_length: float | None = None
    ids: list[str] | None = None
    limit: int | None = None


@dataclass
class ExtrudeRequest:
    """A profile swept into a solid, and where the sweep stops.

    `extent` decides which of the remaining fields carries the answer:
    `distance` needs `distance`, `to` needs `to`, `from_to` needs both `to` and
    `start`. The schema has already refused the combinations that say neither,
    so a backend can trust the pairing.
    """

    sketch: str
    distance: Driven | None = None
    profiles: Sequence[int] | str = "all"
    extent: str = "distance"
    direction: str = "positive"
    operation: str = "join"
    taper: Driven | None = None
    bodies: Sequence[int] = ()
    #: Where a `to` or `from_to` extent stops: a plane reference, a work
    #: plane's name, or a `face:` handle. Named `to` and `start` rather than
    #: `to` and `from` because `from` is a Python keyword -- the recipe says
    #: `from`, which is what a reader of a recipe wants, and the alias lives in
    #: the schema.
    to: str | None = None
    start: str | None = None
    name: str | None = None


@dataclass
class RevolveRequest:
    sketch: str
    axis: AxisSpec
    angle: Driven | None = None
    profiles: Sequence[int] | str = "all"
    direction: str = "positive"
    operation: str = "join"
    name: str | None = None


@dataclass
class SweepRequest:
    profile_sketch: str
    path_sketch: str
    operation: str = "join"
    name: str | None = None


@dataclass
class LoftRequest:
    sketches: Sequence[str] = ()
    rails: Sequence[str] = ()
    operation: str = "join"
    name: str | None = None


@dataclass
class HoleRequest:
    sketch: str
    diameter: Driven
    #: 0-based indices into the sketch's hole-centre points, in creation order.
    #: Empty means every hole centre in the sketch.
    point_indices: Sequence[int] = ()
    depth: Driven | None = None
    through_all: bool = True
    #: "auto" | "positive" | "negative", relative to the sketch plane's normal.
    direction: str = "auto"
    style: str = "drilled"
    cbore_diameter: Driven | None = None
    cbore_depth: Driven | None = None
    csink_diameter: Driven | None = None
    csink_angle: Driven | None = None
    bottom_angle: Driven | None = None
    #: Thread designation to tap, e.g. "M6x1". When given, Inventor takes the
    #: drill size from its own thread table and `diameter` no longer governs it.
    tap: str | None = None
    #: Which thread table, e.g. "ANSI Metric M Profile". Derived from the
    #: designation when omitted.
    tap_type: str | None = None
    tap_class: str | None = None
    tap_right_handed: bool = True
    tap_full_depth: bool = True
    #: Bodies the hole may affect, 1-based in creation order. Empty leaves
    #: Inventor's default, which is the first body only.
    bodies: Sequence[int] = ()
    name: str | None = None


@dataclass
class FilletRequest:
    edges: ResolvedSelector
    radius: Driven
    radius_end: Driven | None = None
    name: str | None = None


@dataclass
class CoilRequest:
    sketch: str
    axis: AxisSpec
    profiles: Sequence[int] | str = "all"
    pitch: Driven | None = None
    height: Driven | None = None
    revolutions: Driven | None = None
    taper: Driven | None = None
    operation: str = "join"
    clockwise: bool = True
    reverse_axis: bool = False
    spiral: bool = False
    name: str | None = None


@dataclass
class ChamferRequest:
    edges: ResolvedSelector
    distance: Driven
    distance2: Driven | None = None
    angle: Driven | None = None
    name: str | None = None


@dataclass
class ShellRequest:
    faces: ResolvedSelector
    thickness: Driven
    direction: str = "inside"
    name: str | None = None


@dataclass
class EmbossRequest:
    sketch: str
    depth: Driven
    style: str = "engrave"          # "engrave" cuts in, "raise" stands proud
    flip: bool = False
    name: str | None = None


@dataclass
class DraftRequest:
    faces: ResolvedSelector
    plane: str
    angle: Driven
    flip: bool = False
    name: str | None = None


@dataclass
class MoveFaceRequest:
    """Faces of an existing solid, and where they go.

    `distance` is always positive and `flip` is the only way to reverse it, so
    there is one spelling of a given move rather than two that have to agree.
    """

    faces: ResolvedSelector
    direction: AxisSpec
    distance: Driven
    flip: bool = False
    name: str | None = None


#: What one unit of `area * thickness` does to a solid, per thicken direction
#: and operation. Set algebra rather than a table of Inventor's behaviour: the
#: layer is a slab swept from the face along its own normal, the operation is a
#: boolean against the solid, and a face's normal points out of it. So the
#: outward half of the slab is air and the inward half is material:
#:
#: * `positive`/`join` -- the whole slab is air, and joining adds all of it: +1.
#: * `negative`/`cut` -- the whole slab is material, and cutting takes it: -1.
#: * `symmetric` -- half either side, so a join adds the outward half and a cut
#:   removes the inward one: +/-0.5.
#: * `positive`/`cut` -- the slab is air; there is nothing there to remove: 0.
#: * `negative`/`join` -- the slab is material; a union with material changes
#:   nothing: 0.
#:
#: It lives here, above both backends, rather than in each of them. The two
#: disagreeing about which side a layer goes on is the `trim` inversion again --
#: defect 5, which survived three runs because each half was self-consistent --
#: and one table cannot disagree with itself. What is *not* settled by set
#: algebra is whether Inventor's "negative" means this side, which is what
#: `live_acceptance.py --only thicken` is for.
THICKEN_SHARE: dict[tuple[str, str], float] = {
    ("positive", "join"): 1.0,
    ("positive", "cut"): 0.0,
    ("negative", "join"): 0.0,
    ("negative", "cut"): -1.0,
    ("symmetric", "join"): 0.5,
    ("symmetric", "cut"): -0.5,
}


#: What a recipe calls a dimensioned feature property, against what Inventor
#: calls it. Keyed by the recipe's word.
#:
#: This lives here for the same reason `THICKEN_SHARE` does: the two backends
#: disagreeing about a word is defect 5's shape again. A promotion is asked for
#: in a caller's words, the simulator matches them against its own feature
#: detail -- which is keyed by the recipe's field names, so `taper` works --
#: and Inventor's `ExtrudeDefinition` calls that property `TaperAngle`. So the
#: promotion the simulator performed happily failed on the seat, measured on
#: 2027.1 on 2026-09-08: *"The feature 'Block' has no drivable property
#: 'taper'."* One table cannot disagree with itself.
#:
#: Only the words that differ. `thickness`, `radius`, `distance`, `depth`,
#: `angle`, `count` and the pattern's `x_count` family already match Inventor's
#: own spelling once case and underscores are ignored, and both backends ignore
#: both.
#: Which of the part's axes a view of each direction shows [across, up], as
#: indices into (X, Y, Z). `iso` is absent: it shows all three foreshortened,
#: which is not two numbers.
#:
#: **Inventor's convention, which is Y-up, and that is a decision rather than
#: an inheritance.** Measured on 2027.1, 2026-09-08, by placing one base view
#: per direction of a 120 x 80 x 8 mm block and reading what each spans:
#: `front` and `rear` show XY, `top` and `bottom` show XZ, `left` and `right`
#: show YZ with **Z across and Y up**. So Inventor's front view looks down Z.
#:
#: This project models Z-up -- every recipe sketches on XY and extrudes
#: upward -- and this table used to say so: front meant XZ, the elevation. The
#: two cannot be reconciled by picking different orientation enums, because
#: `left` needs a quarter turn *within* the same plane and no enum does that.
#: So the naming follows Inventor: a recipe asking for `front` gets what a
#: person placing a base view by hand gets, both halves of the round trip
#: measure the same axes, and the price is that a plate modelled flat has its
#: plan as its front view. `docs/DECISIONS.md` records the choice and
#: `docs/FEATURE_COVERAGE.md` defect 16 the measurement behind it.
#:
#: One table, above the three places that had a copy of it -- `drafting.py`
#: writing a sheet, `drawing.py` reading one back, and the simulator measuring
#: a view's extent. Three copies of one fact is how defect 5 survived three
#: runs.
VIEW_AXES: dict[str, tuple[int, int]] = {
    "front": (0, 1), "rear": (0, 1),
    "top": (0, 2), "bottom": (0, 2),
    "left": (2, 1), "right": (2, 1),
}


PROMOTION_ALIASES: dict[str, str] = {
    "taper": "TaperAngle",
    "diameter": "HoleDiameter",
    "cbore_diameter": "CounterboreDiameter",
    "cbore_depth": "CounterboreDepth",
    "csink_diameter": "CountersinkDiameter",
    "csink_angle": "CountersinkAngle",
    "bottom_angle": "BottomTipAngle",
}


def promotion_synonyms(prop: str) -> list[str]:
    """Every word *prop* may be spelled as, flattened, for matching either way.

    Both backends match a requested property name against names they hold, and
    the two hold different vocabularies: Inventor's property names on one side
    and the recipe's field names on the other. So each side flattens what it
    holds and asks whether it is one of these -- which makes `taper` and
    `TaperAngle` the same request in both directions, rather than in whichever
    direction somebody remembered.
    """
    def flatten(text: str) -> str:
        return text.strip().lower().replace("_", "")

    wanted = flatten(prop)
    if not wanted:
        return []
    words = [wanted]
    for word, spelling in PROMOTION_ALIASES.items():
        pair = (flatten(word), flatten(spelling))
        if wanted in pair:
            words.extend(name for name in pair if name not in words)
    return words


@dataclass
class ThickenRequest:
    """Faces, a layer thickness, and which side of them it goes on.

    `thickness` is always positive; `direction` is the only thing that says
    which way, so a given layer has one spelling rather than two.
    """

    faces: ResolvedSelector
    thickness: Driven
    direction: str = "positive"
    operation: str = "join"
    name: str | None = None


# ---------------------------------------------------------------------------
# Drawings
# ---------------------------------------------------------------------------


@dataclass
class ViewInfo(Info):
    """A view placed on a sheet."""

    id: str
    name: str
    #: "front" | "rear" | "top" | "bottom" | "left" | "right" | "iso".
    direction: str
    #: Where the view's centre sits on the sheet, in cm.
    at: tuple[float, float] = (0.0, 0.0)
    scale: float = 1.0
    style: str = "hidden_line_removed"
    #: What the view spans on the sheet, in cm, if the backend can say.
    extent: tuple[float, float] | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class DimensionInfo(Info):
    """A dimension on a sheet, as the sheet has it.

    `parameter` is what makes a retrieved dimension worth retrieving: it is the
    model parameter the dimension came from, so a sheet read back says which of
    the part's numbers it states rather than only which numbers appear on it.
    None where the dimension is not a model dimension, or where the backend
    cannot say which parameter drove it.
    """

    id: str
    #: In cm for a length, radians for an angle -- the backend's own units.
    value: float
    #: "linear" | "diameter" | "radius" | "angle".
    kind: str = "linear"
    view: str | None = None
    parameter: str | None = None
    expression: str | None = None
    reference: bool = False


@dataclass
class DrawingContents(Info):
    """A sheet as read back off it, which is the point of reading it back.

    Deliberately not a `DrawingReading`: that is a pydantic model in the recipe
    layer, and the backend contract is dataclasses all the way down.
    `drafting.py` turns one of these into a reading, so the same conversion
    serves a sheet the simulator made and a sheet Inventor made.
    """

    views: list[ViewInfo] = field(default_factory=list)
    dimensions: list[DimensionInfo] = field(default_factory=list)
    sheet: str = "a3"
    #: Anything worth saying about how the sheet came to be -- which retrieval
    #: route worked, what was dropped. Absent when there is nothing to say.
    detail: dict[str, Any] | None = None


@dataclass
class ViewRequest:
    """A base view of a part, on a drawing sheet.

    `part_doc_id` is a document rather than a file: a drawing is normally made
    of the part that is already open, and requiring a saved path first would
    make the commonest case the awkward one.
    """

    part_doc_id: str
    name: str
    #: "front" | "rear" | "top" | "bottom" | "left" | "right" | "iso".
    direction: str = "front"
    #: Sheet position of the view's centre, in cm. Always given: where a
    #: projected view goes is worked out from its parent and the sheet's
    #: projection angle before it reaches here, so that the rule lives in one
    #: place and both backends get the same answer from it.
    at: tuple[float, float] = (0.0, 0.0)
    scale: float = 1.0
    style: str = "hidden_line_removed"
    #: Name of the view this is projected from, or None for a base view. A
    #: projected view is a different Inventor call and inherits its parent's
    #: scale, so the two cannot be collapsed into one request.
    parent: str | None = None


@dataclass
class RetrieveRequest:
    """Which of a part's model dimensions to bring onto a view.

    Retrieval rather than placement, and the reason is the parts this server
    builds. Every sketch dimension it creates carries a parameter's expression,
    so Inventor's own "retrieve model dimensions" produces dimensions that *are*
    the parameters -- where placing a dimension by geometry would mean working
    out which two edges on the view are the ones a parameter drives, which is
    the guessing a recipe exists to avoid.

    `parameters` is what to keep. Everything else retrieved is removed again,
    because a sheet carrying every dimension the model happens to hold is not a
    drawing anybody dimensioned.
    """

    view: str
    parameters: Sequence[str] = ()
    reference: Sequence[str] = ()


@dataclass
class CombineRequest:
    base: int
    tools: Sequence[int]
    operation: str = "join"
    keep_tools: bool = False
    name: str | None = None


@dataclass
class SplitRequest:
    tool: str
    style: str = "trim"
    remove_positive: bool = True
    name: str | None = None


@dataclass
class RectangularPatternRequest:
    features: Sequence[str]
    axis1: AxisSpec
    count1: int
    spacing1: Driven
    axis2: AxisSpec | None = None
    count2: int = 1
    spacing2: Driven | None = None
    flip1: bool = False
    flip2: bool = False
    name: str | None = None


@dataclass
class CircularPatternRequest:
    features: Sequence[str]
    axis: AxisSpec
    count: int
    angle: Driven
    fitted: bool = True
    name: str | None = None


@dataclass
class SketchDrivenPatternRequest:
    """Features, the sketch whose points position them, and which point the seed is on.

    Points are 0-based indices into the sketch's hole-centre points, in creation
    order, exactly as `HoleRequest` carries them -- the same entities lay out a
    set of holes and a set of occurrences, and resolving names to indices is the
    builder's job in both cases. Empty means every hole centre in the sketch.
    """

    sketch: str
    point_indices: Sequence[int] = ()
    #: Which hole centre the seed already sits on, indexed the same way. The
    #: occurrences go on the others.
    reference_index: int = 0
    features: Sequence[str] = ()
    name: str | None = None


@dataclass
class MirrorRequest:
    features: Sequence[str]
    plane: str
    name: str | None = None


@dataclass
class WorkPlaneRequest:
    """A datum plane. `kind` decides which fields carry the answer.

    `axis` is present for `kind == "angle"` and absent otherwise: a plane
    turned about one of its own directions is a different plane from the same
    plane turned about the other, so there is nothing to default to. The schema
    refuses the combinations that say neither.
    """

    kind: str = "offset"
    base: str = "xy"
    second: str | None = None
    offset: Driven | None = None
    angle: Driven | None = None
    #: An `AxisSpec` for an angled plane's axis, resolved the way a pattern's
    #: is -- an origin axis, a named work axis, or a sketch line.
    axis: "AxisSpec | None" = None
    name: str | None = None


@dataclass
class WorkPointRequest:
    plane: str = "xy"
    at: tuple[Driven, Driven] = ()  # type: ignore[assignment]
    offset: Driven | None = None
    name: str | None = None


@dataclass
class WorkAxisRequest:
    """Where a work axis comes from.

    ``kind`` decides which of the remaining fields carries the answer, and the
    schema has already refused the combinations that name none of them.
    """

    kind: str = "normal_to_plane"
    plane: str = "xy"
    at: tuple[Driven, Driven] = ()  # type: ignore[assignment]
    points: Sequence[str] = ()
    line: str | None = None
    sketch: str | None = None
    name: str | None = None


@dataclass
class ThreadRequest:
    faces: ResolvedSelector
    designation: str = "M6x1"
    internal: bool = True
    depth: Driven | None = None
    name: str | None = None


@dataclass
class ExportRequest:
    path: str
    format: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScreenshotRequest:
    path: str
    orientation: str = "iso"
    width: int = 1200
    height: int = 900
    display_mode: str = "shaded"


# ---------------------------------------------------------------------------
# The backend interface
# ---------------------------------------------------------------------------


class Backend(ABC):
    """Everything the tool layer is allowed to ask of Inventor."""

    name: str = "backend"

    # -- session -----------------------------------------------------------
    @abstractmethod
    def connect(self, *, visible: bool = True, create: bool = True) -> AppInfo: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def info(self) -> AppInfo: ...

    # -- documents ---------------------------------------------------------
    @abstractmethod
    def new_part(self, name: str, *, template: str | None = None, units: str = "mm",
                 angle_units: str = "deg") -> DocInfo: ...

    @abstractmethod
    def open_document(self, path: str) -> DocInfo: ...

    @abstractmethod
    def list_documents(self) -> list[DocInfo]: ...

    @abstractmethod
    def activate_document(self, doc_id: str) -> DocInfo: ...

    @abstractmethod
    def save_document(self, doc_id: str, path: str | None = None) -> DocInfo: ...

    @abstractmethod
    def close_document(self, doc_id: str, *, save: bool = False) -> None: ...

    def topology_counts(self, doc_id: str) -> dict[str, int]:
        """How many faces and edges the solid has, cheaply.

        Read after every operation so a result can say whether the topology
        moved as well as the volume: a cut that changed the volume but added no
        faces, or a fillet that added faces and removed nothing, is worth
        knowing about at the point it happened. A backend that cannot answer
        returns nothing rather than a guess.
        """
        return {}

    @abstractmethod
    def set_material(self, doc_id: str, material: str, appearance: str | None = None) -> DocInfo: ...

    # -- parameters --------------------------------------------------------
    @abstractmethod
    def set_parameter(self, doc_id: str, name: str, expression: str, *, units: str = "mm",
                      comment: str = "", key: bool = False) -> ParamInfo: ...

    @abstractmethod
    def list_parameters(self, doc_id: str, *, include_model: bool = False) -> list[ParamInfo]: ...

    @abstractmethod
    def delete_parameter(self, doc_id: str, name: str) -> None: ...

    # -- sketches ----------------------------------------------------------
    @abstractmethod
    def build_sketch(self, doc_id: str, plan: SketchPlan) -> SketchInfo: ...

    @abstractmethod
    def list_sketches(self, doc_id: str) -> list[SketchInfo]: ...

    # -- features ----------------------------------------------------------
    @abstractmethod
    def extrude(self, doc_id: str, request: ExtrudeRequest) -> FeatureInfo: ...

    @abstractmethod
    def revolve(self, doc_id: str, request: RevolveRequest) -> FeatureInfo: ...

    @abstractmethod
    def sweep(self, doc_id: str, request: SweepRequest) -> FeatureInfo: ...

    @abstractmethod
    def coil(self, doc_id: str, request: CoilRequest) -> FeatureInfo: ...

    @abstractmethod
    def loft(self, doc_id: str, request: LoftRequest) -> FeatureInfo: ...

    @abstractmethod
    def hole(self, doc_id: str, request: HoleRequest) -> FeatureInfo: ...

    @abstractmethod
    def fillet(self, doc_id: str, request: FilletRequest) -> FeatureInfo: ...

    @abstractmethod
    def chamfer(self, doc_id: str, request: ChamferRequest) -> FeatureInfo: ...

    @abstractmethod
    def shell(self, doc_id: str, request: ShellRequest) -> FeatureInfo: ...

    @abstractmethod
    def rectangular_pattern(self, doc_id: str, request: RectangularPatternRequest) -> FeatureInfo: ...

    @abstractmethod
    def circular_pattern(self, doc_id: str, request: CircularPatternRequest) -> FeatureInfo: ...

    @abstractmethod
    def sketch_driven_pattern(self, doc_id: str,
                              request: SketchDrivenPatternRequest) -> FeatureInfo: ...

    @abstractmethod
    def mirror(self, doc_id: str, request: MirrorRequest) -> FeatureInfo: ...

    @abstractmethod
    def work_plane(self, doc_id: str, request: WorkPlaneRequest) -> FeatureInfo: ...

    @abstractmethod
    def work_point(self, doc_id: str, request: WorkPointRequest) -> FeatureInfo: ...

    @abstractmethod
    def work_axis(self, doc_id: str, request: WorkAxisRequest) -> FeatureInfo: ...

    @abstractmethod
    def draft(self, doc_id: str, request: DraftRequest) -> FeatureInfo: ...

    @abstractmethod
    def move_face(self, doc_id: str, request: MoveFaceRequest) -> FeatureInfo: ...

    @abstractmethod
    def thicken(self, doc_id: str, request: ThickenRequest) -> FeatureInfo: ...

    # -- drawings ----------------------------------------------------------
    @abstractmethod
    def new_drawing(self, name: str, *, template: str | None = None,
                    sheet: str = "a3", units: str = "mm") -> DocInfo: ...

    @abstractmethod
    def place_view(self, doc_id: str, request: ViewRequest) -> ViewInfo: ...

    @abstractmethod
    def retrieve_dimensions(self, doc_id: str,
                            request: RetrieveRequest) -> list[DimensionInfo]: ...

    @abstractmethod
    def read_drawing(self, doc_id: str) -> DrawingContents: ...

    @abstractmethod
    def combine(self, doc_id: str, request: CombineRequest) -> FeatureInfo: ...

    @abstractmethod
    def split(self, doc_id: str, request: SplitRequest) -> FeatureInfo: ...

    @abstractmethod
    def emboss(self, doc_id: str, request: EmbossRequest) -> FeatureInfo: ...

    @abstractmethod
    def thread(self, doc_id: str, request: ThreadRequest) -> FeatureInfo: ...

    # -- model state -------------------------------------------------------
    @abstractmethod
    def list_features(self, doc_id: str) -> list[FeatureInfo]: ...

    @abstractmethod
    def suppress_feature(self, doc_id: str, name: str, suppressed: bool) -> FeatureInfo: ...

    @abstractmethod
    def delete_feature(self, doc_id: str, name: str) -> None: ...

    @abstractmethod
    def rename_feature(self, doc_id: str, name: str, new_name: str) -> FeatureInfo: ...

    @abstractmethod
    def select(self, doc_id: str, selector: ResolvedSelector) -> list[TopoInfo]: ...

    @abstractmethod
    def mass_properties(self, doc_id: str) -> MassProps: ...

    @abstractmethod
    def rebuild(self, doc_id: str) -> dict[str, Any]: ...

    # -- undo --------------------------------------------------------------
    # Not abstract: a backend that cannot undo says so by returning None, and
    # the caller carries on without a net rather than refusing to build.
    def begin_transaction(self, doc_id: str, name: str) -> str | None:
        """Start a unit of work that can be abandoned whole, or None if it cannot.

        Opt-in, because the default behaviour is deliberately the opposite:
        a half-built part is evidence, and deleting the evidence to leave a
        clean document has cost more debugging time than it has saved. What
        makes it worth having at all is that some failures cannot be undone
        any other way -- a hole consumes its sketch, so there is nothing left
        to retry with unless the whole thing is rolled back.
        """
        return None

    def commit_transaction(self, handle: str) -> None:
        """Keep the work. Idempotent, and silent if the handle is unknown."""

    def abort_transaction(self, handle: str) -> bool:
        """Undo everything since :meth:`begin_transaction`. False if it could not."""
        return False

    def import_geometry(self, path: str, *, name: str | None = None) -> DocInfo:
        """Read a translated format -- STEP, IGES, SAT -- into a new part.

        What arrives is a solid body and, in general, no features and no
        parameters: a translated file carries geometry and not the history that
        made it. That matters more here than it looks, because the DFM loop
        drives parameters, so a part imported this way can be measured and
        cannot be improved. The caller is expected to say so rather than run a
        loop that reports "nothing is left that a parameter change answers" and
        sounds like success.
        """
        raise NotImplementedError(
            f"The {self.name} backend cannot import translated geometry."
        )

    def refuse_a_path_another_document_holds(self, doc_id: str, path: str | None) -> None:
        """Refuse a Save As onto a path some other open document already occupies.

        Defect 3 in ``docs/FEATURE_COVERAGE.md``. Inventor will not write a file
        it already has open, and says so with a bare "Exception occurred" and
        nothing in the ErrorManager -- so the second save of a rebuild, onto the
        path the first one wrote, failed and named neither the file nor the
        document holding it. Rebuilding leaves the earlier document open, which
        makes this the normal case rather than an unusual one.

        Asked *before* the write rather than translated after it, because the
        conflict is knowable and Inventor's own refusal is not readable. It
        lives here rather than in either backend so both are held to it and no
        caller routes around it -- the reasoning ``apply_parameter`` records for
        the freeze guard: a rule enforced in one path is not a rule.

        The question goes to :meth:`document_at_path`, which sees every open
        document and not only the ones this session opened -- a file the user
        opened in Inventor's UI collides just as hard. Saving in place (no
        ``path``, which both backends read as ``Save``) cannot collide and is not
        checked; neither is saving onto the path this document is already at,
        which is an in-place save written out longhand and must stay allowed
        however the ids compare -- so it is settled from the document's own path
        first, never by comparing ids.
        """
        if not path:
            return
        target = _same_file_key(path)
        own = self.document_path(doc_id)
        if own and target == _same_file_key(own):
            return
        holder = self.document_at_path(path)
        if holder is None:
            return
        other_id, other_name = holder
        if other_id == doc_id:
            # Its own path after all, by a route `document_path` could not
            # answer. Reporting a document as blocking itself would be worse
            # than the bare exception this replaces.
            return
        where = f" as document {other_id!r} ({other_name})" if other_id else (
            f" as {other_name}, opened outside this session")
        remedy = (f"Close that document first (`close_part(document={other_id!r})`)"
                  if other_id else
                  f"Close {other_name} in Inventor")
        raise DocumentError(
            f"{os.path.basename(path)} is already open in this Inventor "
            f"session,{where}.",
            hint=f"Inventor will not write a file it has open. {remedy}, or save "
                 "this one under another name -- a revision suffix on the path is "
                 "the usual answer when the open copy is still wanted.",
        )

    def list_work_geometry(self, doc_id: str) -> dict[str, list[str]]:
        """The work planes, axes and points this part holds, by name.

        Separate from ``list_features`` because the two backends disagree about
        whether work geometry *is* a feature, and the disagreement is real
        rather than a bug in one of them: the mock keeps everything in one list,
        while Inventor keeps work geometry in ``WorkPlanes``, ``WorkAxes`` and
        ``WorkPoints`` and ``ComponentDefinition.Features`` does not include it.
        Measured 2026-09-07, where ``list_features`` on a part carrying a work
        point returned only the extrude.

        Reconciling them needs a fact nothing here has measured -- whether
        Inventor's own origin planes and axes sit in those collections, and how a
        created one is told from them -- so this reports what is there and the
        acceptance run prints it. A guess would go into the listing that
        ``edit_feature`` and the DFM loop both trust.
        """
        raise NotImplementedError(
            f"The {self.name} backend cannot list work geometry."
        )

    def document_at_path(self, path: str) -> tuple[str | None, str] | None:
        """Which open document occupies *path*, as ``(session id, name)``.

        The id is ``None`` for a document this session did not open -- one the
        user opened in Inventor's UI -- which is a real case and needs naming
        differently, since there is no handle to close by.

        A separate method from ``list_documents`` because the cost matters and
        the listing's is unbounded. On the COM backend that listing reads six
        properties per document, scans held handles by COM identity for each,
        and **registers every document it did not recognise**; against a session
        with an assembly open -- 1033 documents on the machine this was found on
        -- a save would have cost a thousand registrations and a million
        identity comparisons on the next call. This asks one question instead,
        so a backend can answer it with one property read per document and
        register nothing. The default is the honest slow version, correct for
        any backend whose listing is cheap.
        """
        target = _same_file_key(path)
        for info in self.list_documents():
            if info.path and _same_file_key(info.path) == target:
                return info.id, info.name
        return None

    def document_path(self, doc_id: str) -> str | None:
        """Where this document lives on disk, or ``None`` if nowhere yet.

        Asked of the document itself rather than matched out of
        ``list_documents``: on the COM backend that listing identifies documents
        by Python wrapper identity, and late binding hands back a fresh wrapper
        per call, so an id-to-id match over it never matches anything -- which
        silently lost the sidecar (and the freezes in it) for any part whose
        path was not passed in explicitly.
        """
        return None

    def promote_parameter(self, doc_id: str, feature: str, prop: str,
                          name: str) -> dict[str, Any]:
        """Give one driven property a named parameter, in place.

        An .ipt "without parameters" is not parameterless -- every dimension in
        it is a model parameter with a value; what is missing is names. So
        nothing is re-authored: a user parameter is created at the property's
        current value, and the property's expression is rewritten to reference
        it. The feature tree, the sketches and the constraints stay exactly as
        they are, and the part becomes drivable.

        Value-preserving by construction: the geometry after the promotion is
        the geometry before it, which is what makes this safe to do to a part
        somebody handed over.
        """
        raise NotImplementedError(
            f"The {self.name} backend cannot promote a dimension to a parameter."
        )

    def feature_dependencies(self, doc_id: str, name: str) -> dict[str, Any] | None:
        """The user parameters that drive one feature, or ``None`` for "cannot say".

        Freezing a feature is a promise that its geometry stays put, and the
        loop changes geometry by changing parameters -- so the promise is kept
        by pinning every parameter that reaches the feature: its own driven
        properties, and the dimensions of the sketches it consumes. ``None``
        means this backend cannot trace that, which the caller must report
        loudly: a feature "frozen" without its parameters pinned is protected
        from deletion and not from being reshaped.
        """
        return None

    def read_declaration(self, doc_id: str) -> dict[str, Any] | None:
        """The DFM declaration kept inside the document, if there is one.

        Optional. A backend that cannot store one returns ``None``, which reads
        as "nobody asked this part" rather than "this part says nothing is
        frozen" -- a distinction that decides whether a freeze is honoured.
        """
        return None

    def write_declaration(self, doc_id: str, declaration: dict[str, Any]) -> None:
        """Keep the DFM declaration inside the document, so it travels with it."""
        raise NotImplementedError(
            f"The {self.name} backend cannot store a declaration in the document."
        )

    def describe_feature(self, doc_id: str, name: str) -> dict[str, Any]:
        """Every property of one feature that can be read, as plain data.

        Plain data because a live COM object cannot leave the thread that made
        it: the backend is pinned to one apartment, so a caller that reaches into
        a returned feature gets "the application called an interface that was
        marshalled for a different thread". Reading the properties *there* and
        returning numbers is the only way to ask what Inventor actually built.
        """
        raise NotImplementedError(
            f"The {self.name} backend cannot describe a feature's properties."
        )

    # -- escape hatch ------------------------------------------------------
    def run_script(self, doc_id: str | None, code: str) -> dict[str, Any]:
        """Run Python against the live API, for what the recipe cannot say.

        Not abstract, and refuses by default: a backend that has no live API to
        reach has nothing to offer here, and pretending otherwise would let a
        script "succeed" against nothing.
        """
        raise NotImplementedError(
            f"The {self.name} backend has no live Inventor API to run a script against."
        )

    # -- output ------------------------------------------------------------
    @abstractmethod
    def export(self, doc_id: str, request: ExportRequest) -> dict[str, Any]: ...

    @abstractmethod
    def screenshot(self, doc_id: str, request: ScreenshotRequest) -> dict[str, Any]: ...
