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
    "equal_length",  # two lines are the same length
    "equal_radius",  # two arcs or circles share a radius
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

    def shared_point_groups(self) -> dict[tuple[str, str], tuple[str, str]]:
        """Endpoints that coincidence makes into one point, keyed to a group.

        A backend can honour these structurally -- by building the next curve
        from the previous curve's endpoint -- instead of creating two points
        and constraining them together.  That produces a cleaner sketch, and
        avoids asking Inventor for a constraint it has already inferred from
        the coordinates, which it rejects as invalid rather than ignoring.
        """
        parent: dict[tuple[str, str], tuple[str, str]] = {}

        def find(key: tuple[str, str]) -> tuple[str, str]:
            parent.setdefault(key, key)
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        shareable = (PointRef.START, PointRef.END)
        for constraint in self.constraints:
            if constraint.kind != "coincident" or len(constraint.refs) != 2:
                continue
            first, second = constraint.refs
            if first.entity == ORIGIN.entity or second.entity == ORIGIN.entity:
                continue
            if first.point not in shareable or second.point not in shareable:
                continue
            a, b = (first.entity, first.point.value), (second.entity, second.point.value)
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_a] = root_b
        return {key: find(key) for key in parent}

    def mirrored_u(self) -> "SketchPlan":
        """A copy with the sketch's first axis reversed.

        Inventor's XZ plane runs its horizontal axis along -X, so a profile
        drawn from 0 to 90 comes out spanning -90 to 0.  A recipe that says
        "x from 0 to 90" means model +X, and a sketch plane's internal
        orientation is not something the author should have to know -- so the
        geometry is mirrored on the way in and lands where it was asked for.

        Lengths, radii, and the angles between lines all survive a reflection,
        so constraints and dimensions carry over unchanged -- except for which
        *end* of an arc they name. Reflecting reverses an arc's sweep, so its
        start and end swap places, and a coincidence that named the old start
        has to name the new end or it points at the far side of the arc. That
        is not a loud failure: the backend sees both references in one shared
        point group and skips the constraint, so Inventor never gets the
        chance to refuse it, and the sketch is quietly built wrong.
        """
        import copy
        import math as _math

        mirrored = copy.deepcopy(self)
        for primitive in mirrored.primitives:
            if isinstance(primitive, PLine):
                primitive.start = (-primitive.start[0], primitive.start[1])
                primitive.end = (-primitive.end[0], primitive.end[1])
            elif isinstance(primitive, PArc):
                primitive.center = (-primitive.center[0], primitive.center[1])
                # Reflecting the axis turns an angle t into pi - t, and reverses
                # the sweep, so the endpoints swap to keep the arc going the
                # same way round its centre.
                start, end = primitive.start_angle, primitive.end_angle
                primitive.start_angle = _math.pi - end
                primitive.end_angle = _math.pi - start
            elif isinstance(primitive, PEllipse):
                primitive.center = (-primitive.center[0], primitive.center[1])
                primitive.rotation = _math.pi - primitive.rotation
            elif isinstance(primitive, PCircle):
                primitive.center = (-primitive.center[0], primitive.center[1])
            elif isinstance(primitive, PPoint):
                primitive.position = (-primitive.position[0], primitive.position[1])

        # A line's start stays its start -- the point simply moves to its own
        # mirror image. Only arcs swap ends, so only arcs are remapped.
        swept = {p.id for p in mirrored.primitives if isinstance(p, PArc)}
        other_end = {PointRef.START: PointRef.END, PointRef.END: PointRef.START}

        def remap(ref: Ref) -> Ref:
            if ref.entity in swept and ref.point in other_end:
                return Ref(ref.entity, other_end[ref.point])
            return ref

        mirrored.constraints = [
            Constraint(c.kind, tuple(remap(r) for r in c.refs))
            for c in mirrored.constraints
        ]
        mirrored.dimensions = [
            Dimension(d.kind, tuple(remap(r) for r in d.refs), d.expression, d.value,
                      d.name, (-d.text_offset[0], d.text_offset[1]))
            for d in mirrored.dimensions
        ]
        return mirrored

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
