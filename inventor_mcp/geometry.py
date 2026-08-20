"""Expanding high-level sketch entities into constrained, dimensioned geometry.

This is the part that earns the word *parametric*.  A recipe says "a 40 x 25
rectangle centred on the origin"; this module turns that into four lines, the
eight geometric constraints that make them a rectangle, a construction
diagonal that pins the centre, and two driving dimensions whose expressions
are the caller's parameters.  Feed the result to Inventor and dragging a
parameter reshapes the part; feed it to the mock backend and the same
bookkeeping is checked without Inventor running.
"""

from __future__ import annotations

import ast
import logging
import math
from itertools import count
from typing import Iterable, Sequence

from .errors import ExpressionError, SketchError
from .expressions import _parse
from .plan import (
    ORIGIN,
    Constraint,
    Primitive,
    PArc,
    PCircle,
    PEllipse,
    PLine,
    PPoint,
    PointRef,
    Ref,
    SketchPlan,
)
from .resolve import Resolved, Resolver, _is_simple
from .schema import (
    ArcEntity,
    BoltCircleEntity,
    CircleEntity,
    ConstraintSpec,
    DimensionSpec,
    EllipseEntity,
    GridEntity,
    LineEntity,
    PointEntity,
    PolygonEntity,
    PolylineEntity,
    RectangleEntity,
    SketchOp,
    SlotEntity,
)

#: Coordinates closer than this (in cm) are treated as the same point.
TOL = 1.0e-7

#: A gap smaller than this is aligned rather than dimensioned.  TOL is a
#: nanometre: the right test for "is this segment axis-aligned", and the wrong
#: one for "is this worth asking Inventor to hold", since a sub-micron driving
#: dimension sits below the solver's own tolerance and is a redundancy
#: candidate rather than a constraint.
DIM_MIN = 1.0e-4


logger = logging.getLogger(__name__)


class _Ids:
    def __init__(self) -> None:
        self._counters: dict[str, count[int]] = {}

    def next(self, prefix: str) -> str:
        counter = self._counters.setdefault(prefix, count(1))
        return f"{prefix}{next(counter)}"


def _at_origin(value: float) -> bool:
    return abs(value) <= TOL


def _negate(source: str) -> str:
    """*source* with its sign flipped, kept readable where it safely can be.

    Dropping a leading minus is the negation only when that minus governs the
    whole expression.  ``-a`` negates to ``a``, but ``-a + 2`` negates to
    ``a - 2`` and emphatically not to ``a + 2`` -- which is what dropping the
    character gives, and what shipped: with ``a = 100 mm`` the recipe meant
    -98 mm and Inventor was driven to +102 mm, four millimetres out, while the
    simulator kept the right number and agreed with nobody.

    Whether the minus governs the whole expression is a question about the
    parse tree, so ask the parser rather than the first character.  Where it
    does not, wrap: ``-(...)`` is arithmetic Inventor certainly understands,
    which the previous fallback of ``abs(...)`` never was.
    """
    source = source.strip()
    try:
        node = _parse(source).body
    except ExpressionError:  # pragma: no cover - resolution parsed it already
        return f"-({source})"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        # A top-level unary minus means the text really does start with one.
        return source[1:].strip()
    return f"-({source})"


def _magnitude(resolved: Resolved) -> Resolved:
    """The same value as a positive quantity, since dimensions are unsigned."""
    if resolved.value >= 0:
        return resolved
    return Resolved(_negate(resolved.expression), -resolved.value, resolved.dim)


def _anchor(
    resolver: Resolver, point: Sequence[float | int | str]
) -> tuple[tuple[float, float], Resolved, Resolved]:
    """A placement point in cm, plus the expressions that should drive it.

    Keeping the expressions means a centre written as ``[0, "box_h - lid_t"]``
    stays tied to those parameters instead of freezing into a number.
    """
    x, y = resolver.coordinates(point)
    return (x.value, y.value), _magnitude(x), _magnitude(y)


# ---------------------------------------------------------------------------
# Locating geometry relative to the sketch origin
# ---------------------------------------------------------------------------


def _locate(
    plan: SketchPlan,
    resolver: Resolver,
    point: Ref,
    position: tuple[float, float],
    mode: str,
    *,
    entity: Ref | None = None,
    x_expression: Resolved | None = None,
    y_expression: Resolved | None = None,
) -> None:
    """Pin *point* relative to the origin using constraints and dimensions.

    A coordinate that is zero becomes an alignment constraint (which costs no
    dimension and cannot flip sign); a non-zero coordinate becomes a driving
    distance dimension.  Dimensions are unsigned in Inventor, so the geometry
    is created on the correct side and the solver keeps it there.
    """
    if mode == "none":
        return
    if mode == "fix":
        plan.constrain("ground", entity or point)
        return

    x, y = position
    if _at_origin(x) and _at_origin(y):
        plan.constrain("coincident", point, ORIGIN)
        return

    if _at_origin(x):
        plan.constrain("vertical_align", ORIGIN, point)
    else:
        dim = x_expression or resolver.literal_length(abs(x))
        plan.dimension("horizontal", (ORIGIN, point), dim.expression, abs(dim.value),
                       text_offset=(x / 2, -0.4))

    if _at_origin(y):
        plan.constrain("horizontal_align", ORIGIN, point)
    else:
        dim = y_expression or resolver.literal_length(abs(y))
        plan.dimension("vertical", (ORIGIN, point), dim.expression, abs(dim.value),
                       text_offset=(-0.4, y / 2))


# ---------------------------------------------------------------------------
# Entity expansion
# ---------------------------------------------------------------------------


def _plan_line(plan: SketchPlan, ids: _Ids, resolver: Resolver, spec: LineEntity) -> None:
    start, start_x, start_y = _anchor(resolver, spec.start)
    end = resolver.point2d(spec.end)
    if math.dist(start, end) <= TOL:
        raise SketchError("A line needs two distinct points.", entity=spec.name or "line")

    line = plan.add(
        PLine(ids.next("line"), spec.construction, spec.centerline, start=start, end=end),
        spec.name,
    )
    ref = Ref(line.id)

    if spec.dimension:
        dx, dy = end[0] - start[0], end[1] - start[1]
        if _at_origin(dy):
            plan.constrain("horizontal", ref)
        elif _at_origin(dx):
            plan.constrain("vertical", ref)
        elif spec.angle is not None:
            angle = resolver.angle(spec.angle, "line angle")
            plan.dimension("angle", (ref,), angle.expression, angle.value)

        length_spec = spec.length if spec.length is not None else math.dist(start, end) / resolver.scalar_length(1)
        length = resolver.length(length_spec, "line length", positive=True)
        plan.dimension(
            "distance",
            (Ref(line.id, PointRef.START), Ref(line.id, PointRef.END)),
            length.expression,
            length.value,
            text_offset=(0.0, 0.4),
        )

    _locate(plan, resolver, Ref(line.id, PointRef.START), start, spec.locate, entity=ref,
            x_expression=start_x, y_expression=start_y)


def _plan_polyline(plan: SketchPlan, ids: _Ids, resolver: Resolver, spec: PolylineEntity) -> None:
    first, first_x, first_y = _anchor(resolver, spec.points[0])
    # The expressions are kept, not just the numbers: they are what ends up
    # driving the outline, and resolver.point2d throws them away.
    coords = [resolver.coordinates(point) for point in spec.points]
    points = [first] + [(x.value, y.value) for x, y in coords[1:]]
    if spec.closed and math.dist(points[0], points[-1]) <= TOL:
        points, coords = points[:-1], coords[:-1]
    if len(points) < 2:
        raise SketchError("A polyline needs at least two distinct points.")

    segments = [(index, index + 1) for index in range(len(points) - 1)]
    if spec.closed:
        segments.append((len(points) - 1, 0))

    lines = []
    for start, end in segments:
        if math.dist(points[start], points[end]) <= TOL:
            raise SketchError("A polyline may not contain a zero-length segment.")
        lines.append(
            plan.add(
                PLine(ids.next("line"), spec.construction, spec.centerline,
                      start=points[start], end=points[end]),
                spec.name,
            )
        )

    for previous, current in zip(lines, lines[1:]):
        plan.constrain("coincident", Ref(previous.id, PointRef.END), Ref(current.id, PointRef.START))
    if spec.closed:
        plan.constrain("coincident", Ref(lines[-1].id, PointRef.END), Ref(lines[0].id, PointRef.START))

    if spec.dimension:
        kinds: list[str | None] = []
        for line in lines:
            dx, dy = line.end[0] - line.start[0], line.end[1] - line.start[1]
            if _at_origin(dy):
                kinds.append("horizontal")
                plan.constrain("horizontal", Ref(line.id))
            elif _at_origin(dx):
                kinds.append("vertical")
                plan.constrain("vertical", Ref(line.id))
            else:
                kinds.append(None)
                if abs(dx) <= DIM_MIN or abs(dy) <= DIM_MIN:
                    logger.warning(
                        "Polyline segment %s is within a micron of axis-aligned but not "
                        "aligned (dx=%.3g dy=%.3g cm); it will be treated as oblique, "
                        "which changes how the outline is dimensioned.",
                        line.id, dx, dy)
        _dimension_rails(plan, coords, segments, kinds, lines)

    _locate(plan, resolver, Ref(lines[0].id, PointRef.START), points[0], spec.locate,
            x_expression=first_x, y_expression=first_y)


def _dimension_rails(
    plan: SketchPlan,
    coords: Sequence[tuple[Resolved, ...]],
    segments: Sequence[tuple[int, int]],
    kinds: Sequence[str | None],
    lines: Sequence[PLine],
) -> None:
    """Drive an axis-aligned outline from its recipe's own expressions.

    A polyline used to carry no dimensions at all, so an L-section's
    ``base_len`` and ``upright_h`` were evaluated once when the recipe was
    written and then thrown away: the profile could not be revised, which is
    the one thing a parametric model is for.

    The count has to be exact.  Emitting a dimension per vertex duplicates what
    the horizontal and vertical constraints already say, and Inventor refuses a
    redundant dimension -- so the rule counts what is genuinely free and
    dimensions only that.

    A *rail* is a set of vertices that a chain of constraints already forces to
    share one coordinate: a horizontal segment equates its two vertices' Y, a
    vertical segment equates their X.  Every rail after the first on each axis
    is one free coordinate, so it gets one dimension, measured back to the rail
    holding vertex 0 -- the corner ``_locate`` pins, so every measurement
    chains from a known point.  Nothing else is emitted, ever.
    """
    count = len(coords)
    # parents[0] groups vertices sharing an X coordinate, parents[1] a Y.
    parents = [{index: index for index in range(count)} for _ in range(2)]

    def find(axis: int, index: int) -> int:
        while parents[axis][index] != index:
            parents[axis][index] = parents[axis][parents[axis][index]]
            index = parents[axis][index]
        return index

    for (start, end), kind in zip(segments, kinds):
        # A horizontal segment fixes both ends' Y; a vertical one fixes their X.
        axis = 1 if kind == "horizontal" else 0 if kind == "vertical" else None
        if axis is None:
            continue  # an oblique segment constrains neither, so it frees none
        first, second = find(axis, start), find(axis, end)
        if first != second:
            parents[axis][first] = second

    def vertex_ref(index: int) -> Ref:
        if index < len(lines):
            return Ref(lines[index].id, PointRef.START)
        return Ref(lines[-1].id, PointRef.END)

    joined = {frozenset(segment): index for index, segment in enumerate(segments)}

    for axis in (0, 1):
        datum = find(axis, 0)
        rails: dict[int, list[int]] = {}
        for index in range(count):
            rails.setdefault(find(axis, index), []).append(index)

        for root, members in sorted(rails.items(), key=lambda item: min(item[1])):
            if root == datum:
                continue
            # Measure whichever pair spans least across the page, preferring two
            # ends of one segment: a length on a line along its own axis is the
            # shape _plan_rectangle already ships and Inventor already accepts.
            here, there = min(
                ((a, b) for a in rails[datum] for b in members),
                key=lambda pair: (
                    round(abs(coords[pair[0]][1 - axis].value
                              - coords[pair[1]][1 - axis].value), 9),
                    0 if frozenset(pair) in joined else 1,
                    pair,
                ),
            )
            _drive_rail(plan, coords, lines, joined, axis, here, there, vertex_ref)


def _drive_rail(
    plan: SketchPlan,
    coords: Sequence[tuple[Resolved, ...]],
    lines: Sequence[PLine],
    joined: dict,
    axis: int,
    here: int,
    there: int,
    vertex_ref,
) -> None:
    """One dimension, or one alignment where there is no distance to drive."""
    from_, to = coords[here][axis], coords[there][axis]
    segment = joined.get(frozenset((here, there)))
    if segment is not None:
        line = lines[segment]
        refs = ((Ref(line.id, PointRef.START), Ref(line.id, PointRef.END))
                if (here, there) == (segment, segment + 1) or here < there
                else (Ref(line.id, PointRef.END), Ref(line.id, PointRef.START)))
    else:
        refs = (vertex_ref(here), vertex_ref(there))

    kind = "horizontal" if axis == 0 else "vertical"
    gap = to.value - from_.value
    if from_.expression.strip() == to.expression.strip() or abs(gap) <= DIM_MIN:
        # Nothing to drive: the two coordinates are the same expression, or the
        # same number to within the solver's own tolerance. An alignment costs
        # the identical degree of freedom and cannot be refused as redundant.
        plan.constrain("vertical_align" if axis == 0 else "horizontal_align", *refs)
        if from_.expression.strip() != to.expression.strip():
            plan.undriven_expressions.append(to.expression)
        return

    if _at_origin(from_.value):
        driving = _magnitude(to)
    elif _at_origin(to.value):
        driving = _magnitude(from_)
    else:
        high, low = (to, from_) if gap > 0 else (from_, to)
        subtrahend = low.expression.strip()
        if not _is_simple(subtrahend):
            subtrahend = f"({subtrahend})"
        driving = Resolved(f"{high.expression.strip()} - {subtrahend}",
                           high.value - low.value, high.dim)

    across = (coords[here][1 - axis].value + coords[there][1 - axis].value) / 2
    middle = (from_.value + to.value) / 2
    offset = (middle, across - 0.4) if axis == 0 else (across - 0.4, middle)
    plan.dimension(kind, refs, driving.expression, abs(driving.value),
                   text_offset=offset, optional=True)


def _plan_rectangle(plan: SketchPlan, ids: _Ids, resolver: Resolver, spec: RectangleEntity) -> None:
    width = resolver.length(spec.width, "rectangle width", positive=True)
    height = resolver.length(spec.height, "rectangle height", positive=True)

    if spec.center is not None:
        (cx, cy), anchor_x, anchor_y = _anchor(resolver, spec.center)
    else:
        (x0, y0), anchor_x, anchor_y = _anchor(resolver, spec.corner)  # type: ignore[arg-type]
        cx, cy = x0 + width.value / 2, y0 + height.value / 2

    half_w, half_h = width.value / 2, height.value / 2
    corners = [
        (cx - half_w, cy - half_h),
        (cx + half_w, cy - half_h),
        (cx + half_w, cy + half_h),
        (cx - half_w, cy + half_h),
    ]

    lines = [
        plan.add(
            PLine(ids.next("line"), spec.construction, spec.centerline, start=corners[i], end=corners[(i + 1) % 4]),
            spec.name,
        )
        for i in range(4)
    ]
    for previous, current in zip(lines, lines[1:] + lines[:1]):
        plan.constrain("coincident", Ref(previous.id, PointRef.END), Ref(current.id, PointRef.START))
    plan.constrain("horizontal", Ref(lines[0].id))
    plan.constrain("horizontal", Ref(lines[2].id))
    plan.constrain("vertical", Ref(lines[1].id))
    plan.constrain("vertical", Ref(lines[3].id))

    if spec.dimension:
        plan.dimension(
            "horizontal",
            (Ref(lines[0].id, PointRef.START), Ref(lines[0].id, PointRef.END)),
            width.expression,
            width.value,
            text_offset=(0.0, -0.5),
        )
        plan.dimension(
            "vertical",
            (Ref(lines[3].id, PointRef.END), Ref(lines[3].id, PointRef.START)),
            height.expression,
            height.value,
            text_offset=(-0.5, 0.0),
        )

    if spec.locate == "none":
        return
    if spec.locate == "fix":
        for line in lines:
            plan.constrain("ground", Ref(line.id))
        return

    if spec.center is not None:
        # A construction diagonal gives the rectangle a centre we can constrain.
        diagonal = plan.add(
            PLine(ids.next("cline"), construction=True, start=corners[0], end=corners[2])
        )
        plan.constrain("coincident", Ref(diagonal.id, PointRef.START), Ref(lines[0].id, PointRef.START))
        plan.constrain("coincident", Ref(diagonal.id, PointRef.END), Ref(lines[2].id, PointRef.START))
        # The centre gets its own point even when it sits on the origin: a
        # midpoint constraint moves the *point* onto the line, and the sketch
        # origin is grounded, so it cannot be the one that moves.
        center_point = plan.add(PPoint(ids.next("cpoint"), construction=True, position=(cx, cy)))
        plan.constrain("midpoint", Ref(center_point.id), Ref(diagonal.id))
        _locate(plan, resolver, Ref(center_point.id), (cx, cy), "origin",
                x_expression=anchor_x, y_expression=anchor_y)
    else:
        _locate(plan, resolver, Ref(lines[0].id, PointRef.START), corners[0], "origin",
                x_expression=anchor_x, y_expression=anchor_y)


def _plan_circle(plan: SketchPlan, ids: _Ids, resolver: Resolver, spec: CircleEntity) -> None:
    if spec.diameter is not None:
        size = resolver.length(spec.diameter, "circle diameter", positive=True)
        radius_value, kind = size.value / 2, "diameter"
    else:
        size = resolver.length(spec.radius, "circle radius", positive=True)  # type: ignore[arg-type]
        radius_value, kind = size.value, "radius"

    center, anchor_x, anchor_y = _anchor(resolver, spec.center)
    circle = plan.add(
        PCircle(ids.next("circle"), spec.construction, spec.centerline, center=center, radius=radius_value),
        spec.name,
    )
    if spec.dimension:
        plan.dimension(kind, (Ref(circle.id),), size.expression, size.value,
                       text_offset=(radius_value * 0.7, radius_value * 0.7))
    _locate(plan, resolver, Ref(circle.id, PointRef.CENTER), center, spec.locate,
            entity=Ref(circle.id), x_expression=anchor_x, y_expression=anchor_y)


def _plan_arc(plan: SketchPlan, ids: _Ids, resolver: Resolver, spec: ArcEntity) -> None:
    radius = resolver.length(spec.radius, "arc radius", positive=True)
    center, anchor_x, anchor_y = _anchor(resolver, spec.center)
    start_angle = math.radians(spec.start_angle)
    end_angle = math.radians(spec.end_angle)
    if abs(end_angle - start_angle) <= 1.0e-9:
        raise SketchError("An arc needs a non-zero sweep (start_angle must differ from end_angle).")

    arc = plan.add(
        PArc(
            ids.next("arc"),
            spec.construction,
            spec.centerline,
            center=center,
            radius=radius.value,
            start_angle=start_angle,
            end_angle=end_angle,
        ),
        spec.name,
    )
    if spec.dimension:
        plan.dimension("radius", (Ref(arc.id),), radius.expression, radius.value,
                       text_offset=(radius.value * 0.7, radius.value * 0.7))
    _locate(plan, resolver, Ref(arc.id, PointRef.CENTER), center, spec.locate, entity=Ref(arc.id),
            x_expression=anchor_x, y_expression=anchor_y)


def _plan_ellipse(plan: SketchPlan, ids: _Ids, resolver: Resolver, spec: EllipseEntity) -> None:
    major = resolver.length(spec.major, "ellipse major axis", positive=True)
    minor = resolver.length(spec.minor, "ellipse minor axis", positive=True)
    if minor.value > major.value:
        raise SketchError("An ellipse's minor axis must not exceed its major axis.")
    center, anchor_x, anchor_y = _anchor(resolver, spec.center)
    ellipse = plan.add(
        PEllipse(
            ids.next("ellipse"),
            spec.construction,
            spec.centerline,
            center=center,
            major_radius=major.value / 2,
            minor_radius=minor.value / 2,
            rotation=math.radians(spec.rotation),
        ),
        spec.name,
    )
    _locate(plan, resolver, Ref(ellipse.id, PointRef.CENTER), center, spec.locate,
            entity=Ref(ellipse.id), x_expression=anchor_x, y_expression=anchor_y)


def _plan_slot(plan: SketchPlan, ids: _Ids, resolver: Resolver, spec: SlotEntity) -> None:
    length = resolver.length(spec.length, "slot length", positive=True)
    width = resolver.length(spec.width, "slot width", positive=True)
    angle = math.radians(spec.angle)
    center, anchor_x, anchor_y = _anchor(resolver, spec.center)

    half = length.value / 2
    radius = width.value / 2
    axis = (math.cos(angle), math.sin(angle))
    normal = (-math.sin(angle), math.cos(angle))

    c1 = (center[0] - axis[0] * half, center[1] - axis[1] * half)
    c2 = (center[0] + axis[0] * half, center[1] + axis[1] * half)

    def offset(point: tuple[float, float], sign: float) -> tuple[float, float]:
        return (point[0] + normal[0] * radius * sign, point[1] + normal[1] * radius * sign)

    upper = plan.add(
        PLine(ids.next("line"), spec.construction, start=offset(c1, 1), end=offset(c2, 1)), spec.name
    )
    lower = plan.add(
        PLine(ids.next("line"), spec.construction, start=offset(c2, -1), end=offset(c1, -1)), spec.name
    )
    arc1 = plan.add(
        PArc(ids.next("arc"), spec.construction, center=c1, radius=radius,
             start_angle=angle + math.pi / 2, end_angle=angle + 3 * math.pi / 2),
        spec.name,
    )
    arc2 = plan.add(
        PArc(ids.next("arc"), spec.construction, center=c2, radius=radius,
             start_angle=angle - math.pi / 2, end_angle=angle + math.pi / 2),
        spec.name,
    )
    centerline = plan.add(PLine(ids.next("cline"), construction=True, start=c1, end=c2))

    plan.constrain("coincident", Ref(upper.id, PointRef.START), Ref(arc1.id, PointRef.START))
    plan.constrain("coincident", Ref(upper.id, PointRef.END), Ref(arc2.id, PointRef.END))
    plan.constrain("coincident", Ref(lower.id, PointRef.START), Ref(arc2.id, PointRef.START))
    plan.constrain("coincident", Ref(lower.id, PointRef.END), Ref(arc1.id, PointRef.END))
    plan.constrain("coincident", Ref(centerline.id, PointRef.START), Ref(arc1.id, PointRef.CENTER))
    plan.constrain("coincident", Ref(centerline.id, PointRef.END), Ref(arc2.id, PointRef.CENTER))
    plan.constrain("tangent", Ref(upper.id), Ref(arc1.id))
    plan.constrain("tangent", Ref(upper.id), Ref(arc2.id))
    plan.constrain("tangent", Ref(lower.id), Ref(arc1.id))
    plan.constrain("tangent", Ref(lower.id), Ref(arc2.id))
    plan.constrain("equal_radius", Ref(arc1.id), Ref(arc2.id))

    if _at_origin(math.sin(angle)):
        plan.constrain("horizontal", Ref(centerline.id))
    elif _at_origin(math.cos(angle)):
        plan.constrain("vertical", Ref(centerline.id))

    if spec.dimension:
        plan.dimension(
            "distance",
            (Ref(centerline.id, PointRef.START), Ref(centerline.id, PointRef.END)),
            length.expression,
            length.value,
            text_offset=(0.0, radius + 0.4),
        )
        plan.dimension("diameter", (Ref(arc1.id),), width.expression, width.value,
                       text_offset=(-radius, radius))

    if spec.locate == "none":
        return
    if spec.locate == "fix":
        plan.constrain("ground", Ref(centerline.id))
        return
    center_point = plan.add(PPoint(ids.next("cpoint"), construction=True, position=center))
    plan.constrain("midpoint", Ref(center_point.id), Ref(centerline.id))
    _locate(plan, resolver, Ref(center_point.id), center, "origin",
            x_expression=anchor_x, y_expression=anchor_y)


def _plan_polygon(plan: SketchPlan, ids: _Ids, resolver: Resolver, spec: PolygonEntity) -> None:
    size = resolver.length(spec.size, "polygon size", positive=True)
    center, anchor_x, anchor_y = _anchor(resolver, spec.center)
    rotation = math.radians(spec.rotation)
    sides = spec.sides

    if spec.fit == "inscribed":
        guide_radius = size.value / 2  # across corners
        vertex_radius = guide_radius
    else:
        guide_radius = size.value / 2  # across flats
        vertex_radius = guide_radius / math.cos(math.pi / sides)

    vertices = [
        (
            center[0] + vertex_radius * math.cos(rotation + 2 * math.pi * i / sides),
            center[1] + vertex_radius * math.sin(rotation + 2 * math.pi * i / sides),
        )
        for i in range(sides)
    ]

    lines = [
        plan.add(
            PLine(ids.next("line"), spec.construction, start=vertices[i], end=vertices[(i + 1) % sides]),
            spec.name,
        )
        for i in range(sides)
    ]
    for previous, current in zip(lines, lines[1:] + lines[:1]):
        plan.constrain("coincident", Ref(previous.id, PointRef.END), Ref(current.id, PointRef.START))

    guide = plan.add(PCircle(ids.next("cguide"), construction=True, center=center, radius=guide_radius))
    if spec.fit == "inscribed":
        for line in lines:
            plan.constrain("coincident", Ref(line.id, PointRef.START), Ref(guide.id))
    else:
        for line in lines:
            plan.constrain("tangent", Ref(line.id), Ref(guide.id))
    # Equal edges leave exactly one degree of freedom: the polygon's rotation.
    # Measured against the first edge rather than chained around the loop --
    # the same count, but the closing pair is never constrained to each other,
    # which Inventor's redundancy detection objects to.
    for other in lines[1:]:
        plan.constrain("equal_length", Ref(lines[0].id), Ref(other.id))

    if spec.dimension:
        plan.dimension("diameter", (Ref(guide.id),), size.expression, size.value,
                       text_offset=(guide_radius * 0.7, guide_radius * 0.7))

    normalised = math.degrees(rotation) % 180.0
    if abs(normalised) < 1e-6 or abs(normalised - 180.0) < 1e-6:
        plan.constrain("horizontal_align", Ref(guide.id, PointRef.CENTER), Ref(lines[0].id, PointRef.START))
    elif abs(normalised - 90.0) < 1e-6:
        plan.constrain("vertical_align", Ref(guide.id, PointRef.CENTER), Ref(lines[0].id, PointRef.START))

    _locate(plan, resolver, Ref(guide.id, PointRef.CENTER), center, spec.locate,
            entity=Ref(guide.id), x_expression=anchor_x, y_expression=anchor_y)


def _plan_point(plan: SketchPlan, ids: _Ids, resolver: Resolver, spec: PointEntity) -> None:
    position, anchor_x, anchor_y = _anchor(resolver, spec.position)
    point = plan.add(
        PPoint(ids.next("point"), spec.construction, position=position, hole_center=spec.hole_center),
        spec.name,
    )
    _locate(plan, resolver, Ref(point.id), position, spec.locate,
            x_expression=anchor_x, y_expression=anchor_y)


def _plan_grid(plan: SketchPlan, ids: _Ids, resolver: Resolver, spec: GridEntity) -> None:
    x_spacing = resolver.length(spec.x_spacing, "grid x_spacing", positive=True)
    y_spacing = resolver.length(spec.y_spacing, "grid y_spacing", positive=True)
    center = resolver.point2d(spec.center)

    x0 = center[0] - (spec.columns - 1) * x_spacing.value / 2
    y0 = center[1] - (spec.rows - 1) * y_spacing.value / 2

    grid: list[list[PPoint]] = []
    for row in range(spec.rows):
        row_points = []
        for column in range(spec.columns):
            position = (x0 + column * x_spacing.value, y0 + row * y_spacing.value)
            row_points.append(
                plan.add(
                    PPoint(ids.next("point"), spec.construction, position=position,
                           hole_center=True),
                    spec.name,
                )
            )
        grid.append(row_points)

    for row_points in grid:
        for previous, current in zip(row_points, row_points[1:]):
            plan.constrain("horizontal_align", Ref(previous.id), Ref(current.id))
    for column in range(spec.columns):
        column_points = [grid[row][column] for row in range(spec.rows)]
        for previous, current in zip(column_points, column_points[1:]):
            plan.constrain("vertical_align", Ref(previous.id), Ref(current.id))

    if spec.dimension:
        if spec.columns > 1:
            plan.dimension(
                "horizontal",
                (Ref(grid[0][0].id), Ref(grid[0][1].id)),
                x_spacing.expression,
                x_spacing.value,
                text_offset=(0.0, -0.4),
            )
        if spec.rows > 1:
            plan.dimension(
                "vertical",
                (Ref(grid[0][0].id), Ref(grid[1][0].id)),
                y_spacing.expression,
                y_spacing.value,
                text_offset=(-0.4, 0.0),
            )

    # Centring the grid parametrically only works when the centre is the origin;
    # otherwise the corner is located numerically.
    if spec.locate == "origin" and _at_origin(center[0]) and _at_origin(center[1]):
        half_x = resolver_half(resolver, x_spacing, spec.columns - 1)
        half_y = resolver_half(resolver, y_spacing, spec.rows - 1)
        _locate(
            plan,
            resolver,
            Ref(grid[0][0].id),
            (x0, y0),
            "origin",
            x_expression=half_x,
            y_expression=half_y,
        )
    else:
        _locate(plan, resolver, Ref(grid[0][0].id), (x0, y0), spec.locate)


def resolver_half(resolver: Resolver, spacing: Resolved, steps: int) -> Resolved | None:
    """``steps * spacing / 2`` as an expression, or ``None`` when it is zero."""
    if steps <= 0:
        return None
    if steps == 2:
        return Resolved(spacing.expression, spacing.value, spacing.dim)
    return Resolved(
        f"({spacing.expression}) * {steps} / 2", spacing.value * steps / 2, spacing.dim
    )


def _plan_bolt_circle(plan: SketchPlan, ids: _Ids, resolver: Resolver, spec: BoltCircleEntity) -> None:
    diameter = resolver.length(spec.diameter, "bolt circle diameter", positive=True)
    center, anchor_x, anchor_y = _anchor(resolver, spec.center)
    radius = diameter.value / 2
    start = math.radians(spec.start_angle)
    step = 2 * math.pi / spec.count

    guide = plan.add(PCircle(ids.next("cguide"), construction=True, center=center, radius=radius))
    if spec.dimension:
        plan.dimension("diameter", (Ref(guide.id),), diameter.expression, diameter.value,
                       text_offset=(radius * 0.7, radius * 0.7))
    _locate(plan, resolver, Ref(guide.id, PointRef.CENTER), center, spec.locate,
            entity=Ref(guide.id), x_expression=anchor_x, y_expression=anchor_y)

    radials: list[str] = []
    for index in range(spec.count):
        angle = start + index * step
        position = (center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle))
        point = plan.add(
            PPoint(ids.next("point"), spec.construction, position=position, hole_center=True),
            spec.name,
        )
        plan.constrain("coincident", Ref(point.id), Ref(guide.id))
        radial = plan.add(PLine(ids.next("cline"), construction=True, start=center, end=position))
        plan.constrain("coincident", Ref(radial.id, PointRef.START), Ref(guide.id, PointRef.CENTER))
        plan.constrain("coincident", Ref(radial.id, PointRef.END), Ref(point.id))
        radials.append(radial.id)

    if not spec.dimension:
        return

    # Anchor the first spoke, then chain equal angular steps around the circle.
    first_angle = math.degrees(start) % 360.0
    if abs(first_angle % 180.0) < 1e-6:
        plan.constrain("horizontal", Ref(radials[0]))
    elif abs(first_angle % 180.0 - 90.0) < 1e-6:
        plan.constrain("vertical", Ref(radials[0]))
    else:
        reference = plan.add(
            PLine(ids.next("cline"), construction=True, start=center, end=(center[0] + radius, center[1]))
        )
        plan.constrain("coincident", Ref(reference.id, PointRef.START), Ref(guide.id, PointRef.CENTER))
        plan.constrain("horizontal", Ref(reference.id))
        anchor = resolver.literal_angle(start)
        plan.dimension("angle", (Ref(reference.id), Ref(radials[0])), anchor.expression, start)

    if spec.count > 1:
        step_expression = resolver.literal_angle(step)
        for previous, current in zip(radials, radials[1:]):
            plan.dimension(
                "angle", (Ref(previous), Ref(current)), step_expression.expression, step
            )


_PLANNERS = {
    LineEntity: _plan_line,
    PolylineEntity: _plan_polyline,
    RectangleEntity: _plan_rectangle,
    CircleEntity: _plan_circle,
    ArcEntity: _plan_arc,
    EllipseEntity: _plan_ellipse,
    SlotEntity: _plan_slot,
    PolygonEntity: _plan_polygon,
    PointEntity: _plan_point,
    GridEntity: _plan_grid,
    BoltCircleEntity: _plan_bolt_circle,
}


# ---------------------------------------------------------------------------
# Explicit constraints and dimensions from the recipe
# ---------------------------------------------------------------------------

_POINT_SUFFIX = {
    "start": PointRef.START,
    "end": PointRef.END,
    "center": PointRef.CENTER,
    "centre": PointRef.CENTER,
    "mid": PointRef.MID,
}


def _resolve_ref(plan: SketchPlan, token: str) -> Ref:
    """Turn ``"axis"`` or ``"axis.end"`` into a :class:`Ref`."""
    token = token.strip()
    if token in ("origin", "__origin__"):
        return ORIGIN
    label, _, suffix = token.partition(".")
    point = PointRef.SELF
    if suffix:
        if suffix not in _POINT_SUFFIX:
            raise SketchError(
                f"Unknown point {suffix!r} on {label!r}.",
                hint="Use .start, .end or .center.",
            )
        point = _POINT_SUFFIX[suffix]
    ids = plan.labels.get(label)
    if not ids:
        known = ", ".join(sorted(plan.labels)) or "(none named)"
        raise SketchError(
            f"No sketch entity named {label!r}.",
            hint=f"Named entities in this sketch: {known}.",
        )
    if len(ids) > 1 and point is PointRef.SELF:
        # A named group (a rectangle, say) -- take the first primitive.
        return Ref(ids[0], point)
    return Ref(ids[0], point)


def _apply_constraints(plan: SketchPlan, specs: Iterable[ConstraintSpec]) -> None:
    for spec in specs:
        refs = tuple(_resolve_ref(plan, token) for token in spec.entities)
        kind: str = spec.type
        if kind == "fix":
            for ref in refs:
                plan.constrain("ground", ref)
            continue
        if kind == "equal":
            # Inventor has no single "equal": lines match on length, curves on
            # radius. The plan knows which is meant, so it records which.
            primitive = plan.by_id(refs[0].entity)
            kind = "equal_radius" if isinstance(primitive, (PCircle, PArc)) else "equal_length"
        plan.constraints.append(Constraint(kind, refs))  # type: ignore[arg-type]


_DIMENSION_KINDS = {
    "distance": "distance",
    "horizontal_distance": "horizontal",
    "vertical_distance": "vertical",
    "radius": "radius",
    "diameter": "diameter",
    "angle": "angle",
}


def _apply_dimensions(plan: SketchPlan, resolver: Resolver, specs: Iterable[DimensionSpec]) -> None:
    for spec in specs:
        refs = tuple(_resolve_ref(plan, token) for token in spec.entities)
        kind = _DIMENSION_KINDS[spec.type]
        resolved = (
            resolver.angle(spec.value, f"{spec.type} dimension")
            if kind == "angle"
            else resolver.length(spec.value, f"{spec.type} dimension")
        )
        plan.dimension(kind, refs, resolved.expression, resolved.value, name=spec.name)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def plan_sketch(spec: SketchOp, resolver: Resolver) -> SketchPlan:
    """Expand a sketch operation into geometry, constraints and dimensions."""
    plan = SketchPlan(name=spec.name, plane=spec.plane)
    if spec.offset is not None:
        offset = resolver.length(spec.offset, "sketch plane offset")
        plan.offset_expression = offset.expression
        plan.offset_value = offset.value

    ids = _Ids()
    for entity in spec.entities:
        planner = _PLANNERS.get(type(entity))
        if planner is None:  # pragma: no cover - the discriminated union covers this
            raise SketchError(f"Unsupported sketch entity type {type(entity).__name__}.")
        planner(plan, ids, resolver, entity)  # type: ignore[operator]

    _apply_constraints(plan, spec.constraints)
    _apply_dimensions(plan, resolver, spec.dimensions)
    return plan


def profile_loops(plan: SketchPlan) -> list[list[str]]:
    """Group non-construction geometry into closed loops.

    Used to report how many profiles a sketch offers before extruding, and by
    the mock backend to estimate areas.  Loops are found by walking shared
    endpoints; a circle or ellipse is a loop on its own.
    """
    loops: list[list[str]] = []
    segments: list[tuple[str, tuple[float, float], tuple[float, float]]] = []

    for primitive in plan.primitives:
        if primitive.construction or primitive.centerline:
            continue
        if isinstance(primitive, (PCircle, PEllipse)):
            loops.append([primitive.id])
        elif isinstance(primitive, PLine):
            segments.append((primitive.id, primitive.start, primitive.end))
        elif isinstance(primitive, PArc):
            start = (
                primitive.center[0] + primitive.radius * math.cos(primitive.start_angle),
                primitive.center[1] + primitive.radius * math.sin(primitive.start_angle),
            )
            end = (
                primitive.center[0] + primitive.radius * math.cos(primitive.end_angle),
                primitive.center[1] + primitive.radius * math.sin(primitive.end_angle),
            )
            segments.append((primitive.id, start, end))

    remaining = {segment[0]: segment for segment in segments}
    while remaining:
        first_id = next(iter(remaining))
        _, start, end = remaining.pop(first_id)
        loop = [first_id]
        current = end
        while math.dist(current, start) > 1e-5:
            for candidate_id, (_, c_start, c_end) in list(
                (key, value) for key, value in remaining.items()
            ):
                if math.dist(current, c_start) <= 1e-5:
                    current = c_end
                elif math.dist(current, c_end) <= 1e-5:
                    current = c_start
                else:
                    continue
                loop.append(candidate_id)
                del remaining[candidate_id]
                break
            else:
                loop = []
                break
        if loop:
            loops.append(loop)
    return loops


def _sample(primitive: Primitive, reverse: bool) -> list[tuple[float, float]]:
    """Points along a primitive from its start to its end, optionally reversed."""
    if isinstance(primitive, PLine):
        points = [primitive.start, primitive.end]
    elif isinstance(primitive, PArc):
        sweep = primitive.end_angle - primitive.start_angle
        steps = max(4, int(abs(sweep) / 0.1))
        points = [
            (
                primitive.center[0] + primitive.radius * math.cos(primitive.start_angle + sweep * i / steps),
                primitive.center[1] + primitive.radius * math.sin(primitive.start_angle + sweep * i / steps),
            )
            for i in range(steps + 1)
        ]
    else:  # pragma: no cover - circles and ellipses are single-primitive loops
        return []
    return list(reversed(points)) if reverse else points


def loop_area(plan: SketchPlan, loop: Sequence[str]) -> float:
    """Area enclosed by a closed loop, in cm^2 (arcs are sampled).

    Segments are chained end-to-end before the shoelace sum, because the loop
    walker returns them in connection order but not necessarily in a consistent
    direction -- and a polygon assembled from mis-oriented segments crosses
    itself and reports nonsense.
    """
    if len(loop) == 1:
        primitive = plan.by_id(loop[0])
        if isinstance(primitive, PCircle):
            return math.pi * primitive.radius**2
        if isinstance(primitive, PEllipse):
            return math.pi * primitive.major_radius * primitive.minor_radius
        return 0.0

    points: list[tuple[float, float]] = []
    cursor: tuple[float, float] | None = None
    for index, primitive_id in enumerate(loop):
        primitive = plan.by_id(primitive_id)
        forward = _sample(primitive, False)
        if not forward:
            continue
        if cursor is None:
            following = _sample(plan.by_id(loop[(index + 1) % len(loop)]), False)
            if following:
                joins = [following[0], following[-1]]
                reverse = min(math.dist(forward[0], p) for p in joins) < min(
                    math.dist(forward[-1], p) for p in joins
                )
            else:  # pragma: no cover - defensive
                reverse = False
        else:
            reverse = math.dist(cursor, forward[0]) > math.dist(cursor, forward[-1])
        segment = list(reversed(forward)) if reverse else forward
        points.extend(segment[:-1])
        cursor = segment[-1]

    area = 0.0
    for current, following_point in zip(points, points[1:] + points[:1]):
        area += current[0] * following_point[1] - following_point[0] * current[1]
    return abs(area) / 2.0


def plan_bounds(plan: SketchPlan) -> tuple[float, float, float, float]:
    """Axis-aligned bounds of the model geometry, in cm."""
    xs: list[float] = []
    ys: list[float] = []
    for primitive in plan.primitives:
        if primitive.construction:
            continue
        if isinstance(primitive, PLine):
            xs.extend([primitive.start[0], primitive.end[0]])
            ys.extend([primitive.start[1], primitive.end[1]])
        elif isinstance(primitive, PCircle):
            xs.extend([primitive.center[0] - primitive.radius, primitive.center[0] + primitive.radius])
            ys.extend([primitive.center[1] - primitive.radius, primitive.center[1] + primitive.radius])
        elif isinstance(primitive, PArc):
            xs.extend([primitive.center[0] - primitive.radius, primitive.center[0] + primitive.radius])
            ys.extend([primitive.center[1] - primitive.radius, primitive.center[1] + primitive.radius])
        elif isinstance(primitive, PEllipse):
            xs.extend([primitive.center[0] - primitive.major_radius, primitive.center[0] + primitive.major_radius])
            ys.extend([primitive.center[1] - primitive.major_radius, primitive.center[1] + primitive.major_radius])
        elif isinstance(primitive, PPoint):
            xs.append(primitive.position[0])
            ys.append(primitive.position[1])
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))
