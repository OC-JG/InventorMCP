"""The neutral intermediate representation produced from a sketch spec.

High-level entities ("a 40x25 rectangle centred on the origin") expand into
primitive geometry plus the geometric constraints and driving dimensions that
make it parametric.  That expansion happens once, in :mod:`inventor_mcp.geometry`,
and both backends consume the result.  Keeping it backend-neutral is what lets
the mock backend verify constraint bookkeeping that would otherwise only be
observable inside Inventor.

All coordinates in this module are in Inventor database units (centimetres);
all sizes travel as *expression strings* so that Inventor evaluates them and
the model stays driven by its parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Sequence


class PointRef(str, Enum):
    """Which point of an entity a constraint or dimension attaches to."""

    SELF = "self"
    START = "start"
    END = "end"
    CENTER = "center"
    MID = "mid"


@dataclass(frozen=True)
class Ref:
    """A reference to a primitive, optionally to one of its points."""

    entity: str
    point: PointRef = PointRef.SELF

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return self.entity if self.point is PointRef.SELF else f"{self.entity}.{self.point.value}"


#: The sketch origin point, always available.
ORIGIN = Ref("__origin__", PointRef.SELF)


@dataclass
class Primitive:
    """Base class for a piece of sketch geometry."""

    id: str
    construction: bool = False
    centerline: bool = False
    label: str | None = None  # user-facing name from the recipe


@dataclass
class PLine(Primitive):
    start: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] = (0.0, 0.0)

    @property
    def length(self) -> float:
        return ((self.end[0] - self.start[0]) ** 2 + (self.end[1] - self.start[1]) ** 2) ** 0.5


@dataclass
class PCircle(Primitive):
    center: tuple[float, float] = (0.0, 0.0)
    radius: float = 1.0


@dataclass
class PArc(Primitive):
    center: tuple[float, float] = (0.0, 0.0)
    radius: float = 1.0
    start_angle: float = 0.0  # radians
    end_angle: float = 1.5707963267948966  # radians


@dataclass
class PEllipse(Primitive):
    center: tuple[float, float] = (0.0, 0.0)
    major_radius: float = 1.0
    minor_radius: float = 0.5
    rotation: float = 0.0  # radians


@dataclass
class PPoint(Primitive):
    position: tuple[float, float] = (0.0, 0.0)
    hole_center: bool = False


ConstraintKind = Literal[
    "horizontal",
    "vertical",
    "horizontal_align",  # two points share a Y coordinate
    "vertical_align",  # two points share an X coordinate
    "coincident",
    "collinear",
    "parallel",
    "perpendicular",
    "tangent",
    "concentric",
    "equal",
    "symmetric",
    "midpoint",
    "ground",
]

DimensionKind = Literal["distance", "horizontal", "vertical", "radius", "diameter", "angle"]


@dataclass(frozen=True)
class Constraint:
    kind: ConstraintKind
    refs: tuple[Ref, ...]

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.kind}({', '.join(str(r) for r in self.refs)})"


@dataclass(frozen=True)
class Dimension:
    """A driving dimension.

    ``expression`` is handed to Inventor verbatim, which is what keeps the
    model parametric; ``value`` is the same thing pre-evaluated in database
    units so backends can position dimension text and so the mock backend can
    do real geometry.
    """

    kind: DimensionKind
    refs: tuple[Ref, ...]
    expression: str
    value: float
    name: str | None = None
    text_offset: tuple[float, float] = (0.0, 0.0)


@dataclass
class SketchPlan:
    """Everything needed to build one sketch."""

    name: str | None = None
    plane: str = "xy"
    offset_expression: str | None = None
    offset_value: float = 0.0
    primitives: list[Primitive] = field(default_factory=list)
    constraints: list[Constraint] = field(default_factory=list)
    dimensions: list[Dimension] = field(default_factory=list)
    #: user-visible entity name -> primitive id(s)
    labels: dict[str, list[str]] = field(default_factory=dict)
    #: primitive ids that are hole centres, in creation order
    hole_centers: list[str] = field(default_factory=list)

    def add(self, primitive: Primitive, label: str | None = None) -> Primitive:
        self.primitives.append(primitive)
        if label:
            primitive.label = label
            self.labels.setdefault(label, []).append(primitive.id)
        if isinstance(primitive, PPoint) and primitive.hole_center:
            self.hole_centers.append(primitive.id)
        return primitive

    def constrain(self, kind: ConstraintKind, *refs: Ref) -> None:
        self.constraints.append(Constraint(kind, tuple(refs)))

    def dimension(
        self,
        kind: DimensionKind,
        refs: Sequence[Ref],
        expression: str,
        value: float,
        *,
        name: str | None = None,
        text_offset: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        self.dimensions.append(
            Dimension(kind, tuple(refs), expression, value, name, text_offset)
        )

    def by_id(self, primitive_id: str) -> Primitive:
        for primitive in self.primitives:
            if primitive.id == primitive_id:
                return primitive
        raise KeyError(primitive_id)

    def resolve_label(self, label: str) -> list[Primitive]:
        return [self.by_id(pid) for pid in self.labels.get(label, [])]

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for primitive in self.primitives:
            key = type(primitive).__name__[1:].lower()
            counts[key] = counts.get(key, 0) + 1
        return {
            "geometry": counts,
            "constraints": len(self.constraints),
            "dimensions": len(self.dimensions),
            "named": sorted(self.labels),
        }
