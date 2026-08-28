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
    #: True for a dimension the planner added to remove a degree of freedom
    #: rather than because the recipe asked for it. Inventor refusing one of
    #: these is survivable -- the sketch keeps a degree of freedom, which is
    #: what it had before the dimension existed. A required one failing is not.
    optional: bool = False


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
    #: expressions the planner had to drop, so a run can say which parameter
    #: did not reach the model rather than leaving it to be noticed later
    undriven_expressions: list[str] = field(default_factory=list)

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
        optional: bool = False,
    ) -> None:
        self.dimensions.append(
            Dimension(kind, tuple(refs), expression, value, name, text_offset, optional)
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

        def shareable(ref: Ref) -> tuple[str, str] | None:
            """The point this reference names, if a backend can share it.

            A curve's start or end is one.  So is a standalone sketch point,
            which is how a bolt circle joins each construction line to the
            hole centre at its far end -- Inventor infers that coincidence as
            the line is drawn and then refuses ours, which used to fail the
            whole sketch even though the sketch was perfectly usable.
            """
            if ref.entity == ORIGIN.entity:
                return None
            if ref.point in (PointRef.START, PointRef.END):
                return (ref.entity, ref.point.value)
            if ref.point in (PointRef.SELF, PointRef.CENTER):
                try:
                    primitive = self.by_id(ref.entity)
                except KeyError:
                    return None
                # A standalone point is a point; so is a circle's centre, which
                # is how a bolt circle's construction lines all start together.
                if ref.point is PointRef.SELF and isinstance(primitive, PPoint):
                    return (ref.entity, PointRef.SELF.value)
                if ref.point is PointRef.CENTER and isinstance(primitive, PCircle):
                    return (ref.entity, PointRef.CENTER.value)
            return None

        for constraint in self.constraints:
            if constraint.kind != "coincident" or len(constraint.refs) != 2:
                continue
            a, b = (shareable(ref) for ref in constraint.refs)
            if a is None or b is None:
                continue
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_a] = root_b
        return {key: find(key) for key in parent}

    def reoriented(self, matrix: tuple[float, float, float, float]) -> "SketchPlan":
        """A copy with its coordinates put through *matrix*, given as (a, b, c, d).

        A sketch plane's own axes need not run the way its name suggests.
        Inventor's XZ plane runs its first axis along model -X, so a profile
        drawn from 0 to 90 came out spanning -90 to 0; its YZ plane orders its
        axes differently again, which put the angle bracket's upright holes off
        the part entirely.  None of that is something a recipe author should
        have to know, so the backend measures where the sketch's axes actually
        point and hands the geometry over pre-transformed, to land where it was
        asked for.

        The matrix is the one that takes what the recipe means to what this
        sketch needs::

            u' = a*u + b*v
            v' = c*u + d*v

        Lengths, radii and the angles between lines survive any rotation or
        reflection, so constraints and dimensions carry over -- except for
        which *end* of an arc they name.  A reflection reverses an arc's sweep,
        so its start and end swap places, and a coincidence that named the old
        start has to name the new end or it points at the far side of the arc.
        That cannot fail loudly: the backend sees both references in one shared
        point group and skips the constraint, so Inventor never gets the chance
        to refuse it and the sketch is quietly built wrong.
        """
        import copy
        import math as _math

        a, b, c, d = matrix
        reflects = (a * d - b * c) < 0

        def move(point: tuple[float, float]) -> tuple[float, float]:
            u, v = point
            return (a * u + b * v, c * u + d * v)

        def direction(angle: float) -> float:
            """Where a radius pointing at *angle* points once transformed."""
            u, v = move((_math.cos(angle), _math.sin(angle)))
            return _math.atan2(v, u)

        out = copy.deepcopy(self)
        for primitive in out.primitives:
            if isinstance(primitive, PLine):
                primitive.start = move(primitive.start)
                primitive.end = move(primitive.end)
            elif isinstance(primitive, PArc):
                primitive.center = move(primitive.center)
                start, end = direction(primitive.start_angle), direction(primitive.end_angle)
                # A reflection turns the sweep round, so the endpoints swap to
                # keep the arc going the same way about its centre.
                primitive.start_angle, primitive.end_angle = (
                    (end, start) if reflects else (start, end)
                )
            elif isinstance(primitive, PEllipse):
                primitive.center = move(primitive.center)
                primitive.rotation = direction(primitive.rotation)
            elif isinstance(primitive, PCircle):
                primitive.center = move(primitive.center)
            elif isinstance(primitive, PPoint):
                primitive.position = move(primitive.position)

        # A line's start stays its start: the point simply moves to its image.
        # Only arcs swap ends, and only under a reflection.
        swept = (
            {p.id for p in out.primitives if isinstance(p, PArc)} if reflects else set()
        )
        other_end = {PointRef.START: PointRef.END, PointRef.END: PointRef.START}

        def remap(ref: Ref) -> Ref:
            if ref.entity in swept and ref.point in other_end:
                return Ref(ref.entity, other_end[ref.point])
            return ref

        # Under a swap the two axes trade places, so a "horizontal" dimension
        # would measure the other one -- the right number on the wrong axis,
        # which is the quietest kind of wrong. The words have to swap with the
        # axes they name.
        swapped = abs(a) < 0.5
        other_axis = {"horizontal": "vertical", "vertical": "horizontal",
                      "horizontal_align": "vertical_align",
                      "vertical_align": "horizontal_align"}

        def renamed(kind: str) -> str:
            return other_axis.get(kind, kind) if swapped else kind

        out.constraints = [
            Constraint(renamed(k.kind), tuple(remap(r) for r in k.refs))
            for k in out.constraints
        ]
        out.dimensions = [
            Dimension(renamed(dim.kind), tuple(remap(r) for r in dim.refs),
                      dim.expression, dim.value, dim.name, move(dim.text_offset),
                      dim.optional)
            for dim in out.dimensions
        ]
        return out

    def mirrored_u(self) -> "SketchPlan":
        """A copy with the sketch's first axis reversed."""
        return self.reoriented((-1.0, 0.0, 0.0, 1.0))

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
