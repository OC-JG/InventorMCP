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

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Sequence

from ..plan import SketchPlan


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
    sketch: str
    distance: Driven | None = None
    profiles: Sequence[int] | str = "all"
    extent: str = "distance"
    direction: str = "positive"
    operation: str = "join"
    taper: Driven | None = None
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
    name: str | None = None


@dataclass
class FilletRequest:
    edges: ResolvedSelector
    radius: Driven
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
class MirrorRequest:
    features: Sequence[str]
    plane: str
    name: str | None = None


@dataclass
class WorkPlaneRequest:
    kind: str = "offset"
    base: str = "xy"
    second: str | None = None
    offset: Driven | None = None
    angle: Driven | None = None
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
    def mirror(self, doc_id: str, request: MirrorRequest) -> FeatureInfo: ...

    @abstractmethod
    def work_plane(self, doc_id: str, request: WorkPlaneRequest) -> FeatureInfo: ...

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

    # -- output ------------------------------------------------------------
    @abstractmethod
    def export(self, doc_id: str, request: ExportRequest) -> dict[str, Any]: ...

    @abstractmethod
    def screenshot(self, doc_id: str, request: ScreenshotRequest) -> dict[str, Any]: ...
