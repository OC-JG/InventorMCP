"""The declarative part-recipe schema.

A *recipe* is the intermediate representation between natural language and
Inventor.  A language model writes JSON that conforms to these models; the
builder replays it against a backend.  Because it is plain data it can be
validated, diffed, stored, replayed and edited -- which is what makes the
result parametric rather than a one-shot script.

Every model forbids unknown fields on purpose: a typo should come back as a
precise validation error the model can fix, not be silently ignored.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .units import ANGLE_UNIT_NAMES, LENGTH_UNIT_NAMES

# ``20`` (a number in the recipe's units) or ``"width / 2"`` (an expression).
ValueSpec = Union[float, str]

# Coordinates are values too: a centre may be ``[0, "box_h - lid_t"]`` so that
# moving a parameter moves the geometry with it.
Point2D = Annotated[list[ValueSpec], Field(min_length=2, max_length=2)]
Point3D = Annotated[list[ValueSpec], Field(min_length=3, max_length=3)]

Name = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[^\s].*$")]

LengthUnit = Literal[LENGTH_UNIT_NAMES]  # type: ignore[valid-type]
AngleUnit = Literal[ANGLE_UNIT_NAMES]  # type: ignore[valid-type]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class ParameterSpec(Base):
    """A named user parameter -- the thing that makes the model parametric."""

    name: Name = Field(description="Parameter name, e.g. 'plate_width'.")
    value: ValueSpec = Field(
        description="Number in the recipe's units, or an expression such as "
        "'plate_width / 2' or '25 mm'."
    )
    comment: str = Field("", description="Free text shown in Inventor's parameter table.")
    unit: str | None = Field(
        None,
        description="Unit for a bare number. Defaults to the recipe's length unit; "
        "use 'ul' for a unitless count and 'deg' for an angle.",
    )
    key: bool = Field(False, description="Mark as a key parameter in Inventor.")

    @field_validator("name")
    @classmethod
    def _valid_identifier(cls, value: str) -> str:
        if not value.replace("_", "a").isalnum() or value[0].isdigit():
            raise ValueError(
                "Parameter names must be letters, digits and underscores, and may not "
                f"start with a digit (got {value!r})."
            )
        return value


# ---------------------------------------------------------------------------
# Sketch entities
# ---------------------------------------------------------------------------

Locate = Literal["none", "origin", "fix"]


class EntityBase(Base):
    name: str | None = Field(
        None,
        description="Optional label so later operations can refer to this entity "
        "(revolve axes, hole centres, mirror lines).",
    )
    construction: bool = Field(
        False, description="Construction geometry: drives constraints but is not part of a profile."
    )
    centerline: bool = Field(False, description="Mark as a centerline (usable as a revolve axis).")
    locate: Locate = Field(
        "origin",
        description="How to lock the entity's position: 'origin' adds dimensions from the "
        "sketch origin, 'fix' applies a ground constraint, 'none' leaves it free.",
    )
    dimension: bool = Field(
        True, description="Add driving dimension constraints for this entity's size."
    )


class LineEntity(EntityBase):
    type: Literal["line"] = "line"
    start: Point2D
    end: Point2D
    length: ValueSpec | None = Field(None, description="Optional driving length dimension.")
    angle: ValueSpec | None = Field(None, description="Optional driving angle from the X axis.")


class PolylineEntity(EntityBase):
    type: Literal["polyline"] = "polyline"
    points: Annotated[list[Point2D], Field(min_length=2)]
    closed: bool = True


class RectangleEntity(EntityBase):
    type: Literal["rectangle"] = "rectangle"
    center: Point2D | None = Field(None, description="Centre of the rectangle.")
    corner: Point2D | None = Field(None, description="Lower-left corner; alternative to `center`.")
    width: ValueSpec = Field(description="Size along X.")
    height: ValueSpec = Field(description="Size along Y.")

    @model_validator(mode="after")
    def _one_anchor(self) -> "RectangleEntity":
        if (self.center is None) == (self.corner is None):
            raise ValueError("Give exactly one of `center` or `corner`.")
        return self


class CircleEntity(EntityBase):
    type: Literal["circle"] = "circle"
    center: Point2D = [0.0, 0.0]
    diameter: ValueSpec | None = None
    radius: ValueSpec | None = None

    @model_validator(mode="after")
    def _one_size(self) -> "CircleEntity":
        if (self.diameter is None) == (self.radius is None):
            raise ValueError("Give exactly one of `diameter` or `radius`.")
        return self


class ArcEntity(EntityBase):
    type: Literal["arc"] = "arc"
    center: Point2D = [0.0, 0.0]
    radius: ValueSpec
    start_angle: float = Field(0.0, description="Start angle in degrees, measured from +X.")
    end_angle: float = Field(90.0, description="End angle in degrees, measured from +X.")


class EllipseEntity(EntityBase):
    type: Literal["ellipse"] = "ellipse"
    center: Point2D = [0.0, 0.0]
    major: ValueSpec = Field(description="Full length of the major axis.")
    minor: ValueSpec = Field(description="Full length of the minor axis.")
    rotation: float = Field(0.0, description="Rotation of the major axis in degrees.")


class SlotEntity(EntityBase):
    type: Literal["slot"] = "slot"
    center: Point2D = [0.0, 0.0]
    length: ValueSpec = Field(description="Centre-to-centre distance between the end arcs.")
    width: ValueSpec = Field(description="Slot width (diameter of the end arcs).")
    angle: float = Field(0.0, description="Slot orientation in degrees.")


class PolygonEntity(EntityBase):
    type: Literal["polygon"] = "polygon"
    center: Point2D = [0.0, 0.0]
    sides: CountSpec = Field(6, description="3 to 120; may be an expression.")
    size: ValueSpec = Field(description="Across-corners or across-flats distance, see `fit`.")
    fit: Literal["circumscribed", "inscribed"] = Field(
        "inscribed",
        description="'inscribed' treats `size` as across-corners, 'circumscribed' as across-flats.",
    )
    rotation: float = Field(0.0, description="Rotation in degrees.")


class PointEntity(EntityBase):
    type: Literal["point"] = "point"
    position: Point2D = [0.0, 0.0]
    hole_center: bool = Field(True, description="Tag as a hole centre point.")


class GridEntity(EntityBase):
    """A rectangular grid of points -- the usual way to lay out a bolt pattern."""

    type: Literal["point_grid"] = "point_grid"
    center: Point2D = [0.0, 0.0]
    columns: CountSpec = Field(2, description="1 to 200; may be an expression.")
    rows: CountSpec = Field(2, description="1 to 200; may be an expression.")
    x_spacing: ValueSpec = 10.0
    y_spacing: ValueSpec = 10.0


class BoltCircleEntity(EntityBase):
    type: Literal["bolt_circle"] = "bolt_circle"
    center: Point2D = [0.0, 0.0]
    diameter: ValueSpec = Field(description="Pitch circle diameter.")
    count: CountSpec = Field(4, description="1 to 200; may be an expression.")
    start_angle: float = 0.0


SketchEntity = Annotated[
    Union[
        LineEntity,
        PolylineEntity,
        RectangleEntity,
        CircleEntity,
        ArcEntity,
        EllipseEntity,
        SlotEntity,
        PolygonEntity,
        PointEntity,
        GridEntity,
        BoltCircleEntity,
    ],
    Field(discriminator="type"),
]


ConstraintType = Literal[
    "horizontal",
    "vertical",
    "coincident",
    "collinear",
    "parallel",
    "perpendicular",
    "tangent",
    "concentric",
    "equal",
    "symmetric",
    "fix",
]


class ConstraintSpec(Base):
    """An explicit geometric constraint between named sketch entities."""

    type: ConstraintType
    entities: Annotated[list[str], Field(min_length=1, max_length=3)]


class DimensionSpec(Base):
    """An explicit driving dimension between named sketch entities."""

    type: Literal["distance", "horizontal_distance", "vertical_distance", "radius", "diameter", "angle"]
    entities: Annotated[list[str], Field(min_length=1, max_length=3)]
    value: ValueSpec
    name: str | None = Field(None, description="Rename the created dimension parameter.")


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------

SelectorFilter = Literal[
    "all",
    "top",
    "bottom",
    "front",
    "back",
    "left",
    "right",
    "vertical",
    "horizontal",
    "circular",
    "linear",
    "planar",
    "cylindrical",
    "largest",
    "smallest",
    "outer",
    "convex",
    "concave",
]


class Selector(Base):
    """Picks edges or faces without needing Inventor's internal indices.

    Filters compose: ``{"feature": "Body", "filter": "vertical", "limit": 4}``
    means "the four vertical edges created by the feature named Body".
    """

    kind: Literal["edge", "face"] = "edge"
    feature: str | None = Field(
        None, description="Restrict to topology created by this feature (name or handle)."
    )
    filter: SelectorFilter = "all"
    near: Point3D | None = Field(
        None, description="Prefer entities closest to this model-space point (recipe units)."
    )
    within: float | None = Field(
        None, ge=0, description="Only accept entities within this distance of `near`."
    )
    min_length: float | None = Field(None, ge=0, description="Edges at least this long.")
    max_length: float | None = Field(None, ge=0, description="Edges at most this long.")
    ids: list[str] | None = Field(
        None, description="Explicit handles, normally taken from a previous `select` call."
    )
    limit: int | None = Field(None, ge=1, description="Keep at most this many, best match first.")

    @model_validator(mode="after")
    def _within_needs_near(self) -> "Selector":
        if self.within is not None and self.near is None:
            raise ValueError("`within` requires `near`.")
        return self


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

BooleanOp = Literal["join", "cut", "intersect", "new_body"]
#: A whole number, which may be written as an expression of parameters so that
#: "one bolt per 60 mm of pitch circle" is sayable and a count is revisable the
#: way a length is. Bounds are checked when it is resolved, since a string
#: cannot be range-checked before its parameters are known.
CountSpec = Union[int, str]

Direction = Literal["positive", "negative", "symmetric"]

#: Which way a hole is drilled, relative to its sketch plane's own normal --
#: the same meaning `direction` has on an extrude. "auto" is the sensible
#: default and what almost every recipe wants: a hole placed on a face is
#: drilled into the part, and the backend can see which side that is.
HoleDirection = Literal["auto", "positive", "negative"]
PlaneRef = str  # "xy" | "xz" | "yz" | "face:<handle>" | "plane:<name>" | a work-plane name
AxisRef = str  # "x" | "y" | "z" | sketch-entity name | edge handle


class OpBase(Base):
    name: str | None = Field(None, description="Name for the created feature or sketch.")


class SketchOp(OpBase):
    op: Literal["sketch"] = "sketch"
    plane: PlaneRef = Field(
        "xy",
        description="'xy' | 'xz' | 'yz', a named work plane, or 'face:<handle>' from `select`.",
    )
    offset: ValueSpec | None = Field(
        None, description="Offset the sketch plane by this distance before sketching."
    )
    entities: list[SketchEntity] = Field(default_factory=list)
    constraints: list[ConstraintSpec] = Field(default_factory=list)
    dimensions: list[DimensionSpec] = Field(default_factory=list)


class ExtrudeOp(OpBase):
    op: Literal["extrude"] = "extrude"
    sketch: str | None = Field(None, description="Sketch name; defaults to the most recent sketch.")
    profiles: list[int] | Literal["all", "outer"] = Field(
        "all", description="Which closed profiles of the sketch to use (0-based indices)."
    )
    distance: ValueSpec | None = None
    extent: Literal["distance", "through_all", "to_next", "all"] = "distance"
    direction: Direction = "positive"
    operation: BooleanOp = "join"
    taper: ValueSpec | None = Field(None, description="Draft angle, e.g. '3 deg'.")

    @model_validator(mode="after")
    def _distance_required(self) -> "ExtrudeOp":
        if self.extent == "distance" and self.distance is None:
            raise ValueError("`distance` is required when extent is 'distance'.")
        return self


class RevolveOp(OpBase):
    op: Literal["revolve"] = "revolve"
    sketch: str | None = None
    profiles: list[int] | Literal["all", "outer"] = "all"
    axis: AxisRef = Field("x", description="'x'|'y'|'z', or the name of a sketch line.")
    angle: ValueSpec | None = Field(None, description="Omit for a full 360 degree revolve.")
    direction: Direction = "positive"
    operation: BooleanOp = "join"


class SweepOp(OpBase):
    op: Literal["sweep"] = "sweep"
    profile_sketch: str = Field(description="Sketch holding the closed profile.")
    path_sketch: str = Field(description="Sketch holding the open or closed path.")
    operation: BooleanOp = "join"


class LoftOp(OpBase):
    op: Literal["loft"] = "loft"
    sketches: Annotated[list[str], Field(min_length=2)]
    operation: BooleanOp = "join"
    rails: list[str] = Field(default_factory=list)


class HoleOp(OpBase):
    op: Literal["hole"] = "hole"
    sketch: str | None = Field(
        None, description="Sketch containing the hole-centre points; defaults to the last sketch."
    )
    points: list[str] = Field(
        default_factory=list,
        description="Named point entities to use. Empty means every hole-centre point in the sketch.",
    )
    diameter: ValueSpec = Field(description="Nominal (drill) diameter.")
    depth: ValueSpec | None = Field(None, description="Blind depth. Omit for a through hole.")
    through_all: bool = True
    direction: HoleDirection = Field(
        "auto",
        description="Which way to drill, along the sketch plane's normal "
        "('positive'), against it ('negative'), or into whichever side the "
        "material is on ('auto', the default and almost always right).",
    )
    style: Literal["drilled", "counterbore", "countersink", "spotface"] = "drilled"
    cbore_diameter: ValueSpec | None = None
    cbore_depth: ValueSpec | None = None
    csink_diameter: ValueSpec | None = None
    csink_angle: ValueSpec = "90 deg"
    tap: str | None = Field(None, description="Thread designation to tap, e.g. 'M6x1'.")
    bottom_angle: ValueSpec = Field("118 deg", description="Drill point angle for blind holes.")

    @model_validator(mode="after")
    def _depth_consistency(self) -> "HoleOp":
        if self.depth is not None:
            object.__setattr__(self, "through_all", False)
        if self.style in ("counterbore", "spotface") and (
            self.cbore_diameter is None or self.cbore_depth is None
        ):
            raise ValueError(f"style '{self.style}' needs `cbore_diameter` and `cbore_depth`.")
        if self.style == "countersink" and self.csink_diameter is None:
            raise ValueError("style 'countersink' needs `csink_diameter`.")
        return self


class FilletOp(OpBase):
    op: Literal["fillet"] = "fillet"
    edges: Selector = Field(default_factory=lambda: Selector(kind="edge"))
    radius: ValueSpec


class ChamferOp(OpBase):
    op: Literal["chamfer"] = "chamfer"
    edges: Selector = Field(default_factory=lambda: Selector(kind="edge"))
    distance: ValueSpec
    distance2: ValueSpec | None = None
    angle: ValueSpec | None = None

    @model_validator(mode="after")
    def _one_style(self) -> "ChamferOp":
        if self.distance2 is not None and self.angle is not None:
            raise ValueError("Give at most one of `distance2` or `angle`.")
        return self


class ShellOp(OpBase):
    op: Literal["shell"] = "shell"
    faces: Selector = Field(
        default_factory=lambda: Selector(kind="face"),
        description="Faces to remove. An empty result gives a hollow body with no opening.",
    )
    thickness: ValueSpec
    direction: Literal["inside", "outside", "both"] = "inside"


class RectangularPatternOp(OpBase):
    op: Literal["rectangular_pattern"] = "rectangular_pattern"
    features: list[str] = Field(
        default_factory=list, description="Features to pattern. Empty means the previous feature."
    )
    axis1: AxisRef = "x"
    count1: CountSpec = Field(2, description="1 to 1000; may be an expression.")
    spacing1: ValueSpec = 10.0
    axis2: AxisRef | None = None
    count2: CountSpec = Field(1, description="1 to 1000; may be an expression.")
    spacing2: ValueSpec | None = None
    flip1: bool = False
    flip2: bool = False


class CircularPatternOp(OpBase):
    op: Literal["circular_pattern"] = "circular_pattern"
    features: list[str] = Field(default_factory=list)
    axis: AxisRef = "z"
    count: CountSpec = Field(4, description="1 to 1000; may be an expression.")
    angle: ValueSpec = "360 deg"
    fitted: bool = Field(True, description="Spread occurrences evenly over `angle`.")


class MirrorOp(OpBase):
    op: Literal["mirror"] = "mirror"
    features: list[str] = Field(default_factory=list)
    plane: PlaneRef = "yz"


class WorkPlaneOp(OpBase):
    op: Literal["work_plane"] = "work_plane"
    kind: Literal["offset", "midplane", "angle", "tangent"] = "offset"
    base: PlaneRef = "xy"
    second: PlaneRef | None = Field(None, description="Second plane for a midplane.")
    offset: ValueSpec = 10.0
    angle: ValueSpec = "45 deg"


class ThreadOp(OpBase):
    op: Literal["thread"] = "thread"
    faces: Selector = Field(default_factory=lambda: Selector(kind="face"))
    designation: str = Field("M6x1", description="Thread designation, e.g. 'M6x1' or '1/4-20 UNC'.")
    internal: bool = True
    depth: ValueSpec | None = None


class MaterialOp(OpBase):
    op: Literal["material"] = "material"
    material: str = Field("Steel", description="Material library name as it appears in Inventor.")
    appearance: str | None = None


Operation = Annotated[
    Union[
        SketchOp,
        ExtrudeOp,
        RevolveOp,
        SweepOp,
        LoftOp,
        HoleOp,
        FilletOp,
        ChamferOp,
        ShellOp,
        RectangularPatternOp,
        CircularPatternOp,
        MirrorOp,
        WorkPlaneOp,
        ThreadOp,
        MaterialOp,
    ],
    Field(discriminator="op"),
]


# ---------------------------------------------------------------------------
# The recipe
# ---------------------------------------------------------------------------


class PartRecipe(Base):
    """A complete, replayable description of a parametric part."""

    name: str = Field("Part", description="Part name, also used as the default file name.")
    description: str = Field("", description="What the part is, in one line.")
    units: LengthUnit = Field("mm", description="Unit for bare numbers in this recipe.")
    angle_units: AngleUnit = Field("deg", description="Unit for bare angle numbers.")
    material: str | None = None
    parameters: list[ParameterSpec] = Field(default_factory=list)
    operations: list[Operation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_parameter_names(self) -> "PartRecipe":
        seen: set[str] = set()
        for parameter in self.parameters:
            key = parameter.name.lower()
            if key in seen:
                raise ValueError(f"Duplicate parameter name {parameter.name!r}.")
            seen.add(key)
        return self

    @model_validator(mode="after")
    def _has_work(self) -> "PartRecipe":
        if not self.operations:
            raise ValueError("A recipe needs at least one operation.")
        return self


def recipe_json_schema() -> dict:
    """The recipe JSON Schema, published as an MCP resource."""
    return PartRecipe.model_json_schema()
