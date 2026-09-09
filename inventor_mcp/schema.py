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

from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .dfm.roles import ROLE_NAMES as DFM_ROLE_NAMES
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
    frozen: bool = Field(
        False,
        description="This value is key geometry: automated changes -- the DFM "
        "improvement loop above all -- must not touch it. A sealing face, a "
        "bearing bore, a mating pitch. Anything a frozen value is computed from "
        "is protected too, since changing that would move it just as surely.",
    )

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
    corners: ValueSpec | None = Field(
        None,
        description="Radius to round every corner to. On an open polyline the "
        "two ends are left square, because there is no corner there to round. "
        "An inward corner -- the notch in an L -- is rounded too, with its arc "
        "turning the other way.",
    )
    chamfers: ValueSpec | None = Field(
        None,
        description="Distance to cut off every corner instead of rounding it: "
        "that much off each of the two edges, which is what a drafter means by "
        "a chamfer. Square corners only -- both edges along X or Y -- because "
        "an oblique corner's chamfer is dimensioned as a length and an angle, "
        "and the two spans written here would bake in the angle the corner "
        "happens to have now.",
    )

    @model_validator(mode="after")
    def _one_way_to_ease_a_corner(self) -> "PolylineEntity":
        if self.corners is not None and self.chamfers is not None:
            raise ValueError(
                "Give `corners` (a radius) or `chamfers` (a distance), not both: "
                "a corner is rounded or cut off, and this rounds or cuts every "
                "corner of the outline."
            )
        return self


class RectangleEntity(EntityBase):
    type: Literal["rectangle"] = "rectangle"
    center: Point2D | None = Field(None, description="Centre of the rectangle.")
    corner: Point2D | None = Field(None, description="Lower-left corner; alternative to `center`.")
    width: ValueSpec = Field(description="Size along X.")
    height: ValueSpec = Field(description="Size along Y.")
    corners: ValueSpec | None = Field(
        None,
        description="Radius to round every corner to, as an expression like any "
        "other length -- so the corners follow the parameter that sets them. "
        "The alternative is a `fillet` on the extruded edges afterwards, which "
        "is a different thing: this rounds the PROFILE, so the rounding is part "
        "of the shape being swept and survives whatever the profile is used for.",
    )
    chamfers: ValueSpec | None = Field(
        None,
        description="Distance to cut off every corner instead of rounding it: "
        "that much off each of the two edges, which is what a drafter means by "
        "a chamfer. An expression like any other, so the chamfers follow the "
        "parameter that sets them.",
    )

    @model_validator(mode="after")
    def _one_anchor(self) -> "RectangleEntity":
        if (self.center is None) == (self.corner is None):
            raise ValueError("Give exactly one of `center` or `corner`.")
        if self.corners is not None and self.chamfers is not None:
            raise ValueError(
                "Give `corners` (a radius) or `chamfers` (a distance), not both: "
                "a corner is rounded or cut off, and this rounds or cuts all four."
            )
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


class TextEntity(EntityBase):
    """A run of text, for embossing or engraving a name onto a face.

    Inventor renders this with a real font, so it is not a set of curves the
    planner can constrain: it is positioned and sized, and that is all. Feed the
    sketch to an `emboss` operation to turn it into geometry.
    """

    type: Literal["text"] = "text"
    text: str = Field(description="The string to write. Single line.", min_length=1)
    position: Point2D = Field(
        [0.0, 0.0],
        description="Anchor. This is the TOP of the text, not its baseline: text hangs "
        "below it by roughly 1.3 x `height`.",
    )
    height: ValueSpec = Field(
        5.0,
        description="Font size, which is roughly the cap height. The rendered box is taller.",
    )
    font: str = Field("Arial", description="Font family name as installed on this machine.")
    bold: bool = False
    italic: bool = False
    align: Literal["left", "center", "right"] = Field(
        "center", description="Which end of the text `position` refers to."
    )
    rotation: float = Field(0.0, description="Rotation in degrees, anticlockwise from +X.")


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
        TextEntity,
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
AxisRef = str  # "x" | "y" | "z" | sketch-entity name | work-axis name | edge handle
PointRef = str  # a work-point name


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
    extent: Literal["distance", "through_all", "to_next", "to", "from_to", "all"] = "distance"
    direction: Direction = "positive"
    operation: BooleanOp = "join"
    taper: ValueSpec | None = Field(None, description="Draft angle, e.g. '3 deg'.")
    to: PlaneRef | None = Field(
        None,
        description="Where an extent of 'to' or 'from_to' stops: 'xy'|'xz'|'yz', "
        "a named work plane, or 'face:<handle>' from `select`. \"Up to the "
        "underside of the lid\" is how a boss is really specified, and saying it "
        "this way means nobody has to derive the distance -- and nobody has to "
        "re-derive it when the lid moves.",
    )
    from_: PlaneRef | None = Field(
        None,
        alias="from",
        description="Where an extent of 'from_to' starts, in the same "
        "vocabulary as `to`. The profile then bounds neither end: both come "
        "from the model, so the feature follows both when either moves.",
    )
    bodies: list[int] | None = Field(
        None,
        description="Bodies this feature may affect, 1-based in creation order. "
        "Omit to leave Inventor's default, which is the first body only -- so a "
        "cut aimed at a second body needs this.",
    )

    @model_validator(mode="after")
    def _the_extent_carries_what_it_needs(self) -> "ExtrudeOp":
        if self.extent == "distance" and self.distance is None:
            raise ValueError("`distance` is required when extent is 'distance'.")
        if self.extent == "to" and not self.to:
            raise ValueError("`to` is required when extent is 'to': a plane, a "
                             "work plane's name, or 'face:<handle>'.")
        if self.extent == "from_to" and not (self.to and self.from_):
            raise ValueError("`from` and `to` are both required when extent is "
                             "'from_to'.")
        if self.to and self.extent not in ("to", "from_to"):
            raise ValueError(f"`to` means nothing to an extent of "
                             f"{self.extent!r}; use extent 'to' or 'from_to'.")
        if self.from_ and self.extent != "from_to":
            raise ValueError(f"`from` means nothing to an extent of "
                             f"{self.extent!r}; use extent 'from_to'.")
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
    diameter: ValueSpec = Field(
        description="Nominal (drill) diameter. For a tapped hole give the tap-drill "
        "diameter: Inventor takes the real one from its thread table, and the "
        "server reports it back so the two can be compared."
    )
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
    tap: str | None = Field(
        None,
        description="Thread designation to tap, e.g. 'M6x1' or '1/4-20'. Inventor "
        "takes the drill size from its own thread table, so `diameter` stops "
        "governing the bore when this is given.",
    )
    tap_type: str | None = Field(
        None,
        description="Which thread table, as Inventor names it: 'ANSI Metric M "
        "Profile', 'ANSI Unified Screw Threads', 'NPT', 'BSP'. Derived from the "
        "designation when omitted.",
    )
    tap_class: str | None = Field(
        None, description="Thread class, e.g. '6H' or '2B'. Defaults by thread type."
    )
    tap_right_handed: bool = True
    tap_full_depth: bool = Field(
        True, description="Thread the whole depth of the hole rather than part of it."
    )
    bottom_angle: ValueSpec | None = Field(
        None,
        description="Drill point angle for a blind hole, e.g. '118 deg'. Omit for "
        "a flat bottom, which is what Inventor's own hole dialog gives.",
    )
    bodies: list[int] | None = Field(
        None,
        description="Bodies this hole may affect, 1-based in creation order. "
        "Omit to leave Inventor's default, which is the first body only -- so a "
        "hole aimed at a second body needs this.",
    )

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
    radius_end: ValueSpec | None = Field(
        None,
        description="Radius at the far end of each edge, for a variable fillet. "
        "Omit for a constant radius. Which end is which follows the edge's own "
        "direction, which is Inventor's to decide -- check the result and swap "
        "the two radii if it came out the other way round.",
    )


class CoilOp(OpBase):
    """A helical sweep of a sketch profile about an axis: springs, threads, flutes.

    Inventor takes the extent three ways -- pitch and height, pitch and
    revolutions, or revolutions and height -- so give exactly two of the three
    and the server picks the matching call.
    """

    op: Literal["coil"] = "coil"
    sketch: str | None = Field(None, description="Sketch name; defaults to the most recent.")
    profiles: list[int] | Literal["all", "outer"] = "all"
    axis: AxisRef = Field("z", description="'x'|'y'|'z', or the name of a sketch line.")
    pitch: ValueSpec | None = Field(None, description="Rise per revolution.")
    height: ValueSpec | None = Field(None, description="Overall rise.")
    revolutions: CountSpec | float | None = Field(None, description="Number of turns.")
    taper: ValueSpec | None = Field(None, description="Taper angle, e.g. '2 deg'.")
    operation: BooleanOp = "join"
    clockwise: bool = Field(True, description="Right-hand helix when true.")
    reverse_axis: bool = Field(False, description="Run the coil the other way along the axis.")
    spiral: bool = Field(
        False,
        description="A flat spiral in the profile's plane rather than a helix. "
        "Needs pitch and revolutions.",
    )

    @model_validator(mode="after")
    def _two_of_three(self) -> "CoilOp":
        given = [n for n, v in (("pitch", self.pitch), ("height", self.height),
                                ("revolutions", self.revolutions)) if v is not None]
        if self.spiral:
            if self.pitch is None or self.revolutions is None:
                raise ValueError("A spiral needs `pitch` and `revolutions`.")
            return self
        if len(given) != 2:
            raise ValueError(
                "Give exactly two of `pitch`, `height` and `revolutions`; "
                f"got {given or 'none'}."
            )
        return self


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
    axis: AxisRef = Field(
        "z",
        description="'x'|'y'|'z', a named work axis, or the name of a sketch line. A bolt "
        "circle away from the origin wants a `work_axis`: the axis has to stand "
        "perpendicular to the face being patterned, and a sketch line never can.",
    )
    count: CountSpec = Field(4, description="1 to 1000; may be an expression.")
    angle: ValueSpec = "360 deg"
    fitted: bool = Field(True, description="Spread occurrences evenly over `angle`.")


class SketchDrivenPatternOp(OpBase):
    """Copy features to a set of sketch points, wherever they are.

    The irregular pattern. `rectangular_pattern` and `circular_pattern` cover
    the regular cases and a bolt circle; this one takes positions as data, so a
    layout that follows nothing in particular -- mounting points dictated by
    somebody else's PCB -- is expressible.

    **Read this before reaching for it, because the alternative is often
    better.** `hole` already takes a list of points and `boss` a list of
    positions, so an irregular set of holes or bosses needs no pattern at all:
    put the points in one sketch and drill them in one operation. What this adds
    is patterning a feature whose definition is *not* already a list of
    positions -- a pocket, a rib, a filleted detail -- which is the narrower and
    real gap.

    The seed is assumed to sit on one of the points, named by `reference`, and
    the occurrences go on the others. That is one point per occurrence plus the
    seed's own, which means a sketch of N points describes a part with N of the
    feature on it.
    """

    op: Literal["sketch_driven_pattern"] = "sketch_driven_pattern"
    features: list[str] = Field(
        default_factory=list,
        description="Features to copy. Empty means the most recent one.",
    )
    sketch: str | None = Field(
        None,
        description="Sketch holding the positions; defaults to the most recent sketch.",
    )
    points: list[str] = Field(
        default_factory=list,
        description="Named point entities to use. Empty means every hole-centre "
        "point in the sketch -- a `point`, `point_grid` or `bolt_circle`.",
    )
    reference: str | None = Field(
        None,
        description="The point the seed feature already sits on; the occurrences go "
        "on the others. Defaults to the first point in the sketch.",
    )

class MirrorOp(OpBase):
    op: Literal["mirror"] = "mirror"
    features: list[str] = Field(default_factory=list)
    plane: PlaneRef = "yz"


class WorkPlaneOp(OpBase):
    """A datum plane, for sketching on somewhere the origin planes are not.

    `kind` decides which of the remaining fields carries the answer:

    * `offset` -- `base` and `offset`, the common case;
    * `midplane` -- `base` and `second`, halfway between them;
    * `angle` -- `base`, `axis` and `angle`: the base plane turned about the
      axis. **`axis` has to be given**, because Inventor's call for this is
      `WorkPlanes.AddByLinePlaneAndAngle(axis, plane, angle)` and there is no
      axis to guess: a plane can be turned about either of its own directions
      and they are different planes. Until 2026-09-09 this operation quietly
      built an *offset* plane for an angled request -- defect 12 -- which is
      why the field is required rather than defaulted;
    * `tangent` -- `base` and `face`: perpendicular to the base plane and
      touching a cylindrical face along one line. **`face` has to be given**
      and has to match exactly one *cylindrical* face, because that is what
      Inventor's `WorkPlanes.AddByPlaneAndTangent(plane, face)` takes; a
      planar face has no tangent plane to find and is refused with the
      geometry it turned out to be.
    """

    op: Literal["work_plane"] = "work_plane"
    kind: Literal["offset", "midplane", "angle", "tangent"] = "offset"
    base: PlaneRef = "xy"
    second: PlaneRef | None = Field(None, description="Second plane for a midplane.")
    offset: ValueSpec = 10.0
    angle: ValueSpec = "45 deg"
    axis: AxisRef | None = Field(
        None,
        description="Which axis an angled plane turns about: 'x', 'y', 'z', a "
        "named work axis, or a sketch line. Required for kind 'angle' and "
        "meaningless for the others -- a plane turned about one of its "
        "directions is a different plane from the same plane turned about the "
        "other, so there is nothing to default to.",
    )
    face: Selector | None = Field(
        None,
        description="The cylindrical face a tangent plane touches, picked the "
        "way a fillet picks its edges. Required for kind 'tangent' and "
        "meaningless for the others. It has to resolve to exactly one face: "
        "'the plane tangent to these two bosses' is not a plane.",
    )

    #: The field each kind needs and no other kind may carry. `offset`,
    #: `second` and `angle` are not in here because they have defaults that are
    #: harmless when ignored; `axis` and `face` name *geometry*, and a
    #: reference silently thrown away is how somebody learns the wrong lesson
    #: about their own recipe.
    _REQUIRED_REFERENCE: ClassVar[dict[str, str]] = {
        "angle": "axis", "tangent": "face",
    }

    @model_validator(mode="after")
    def _each_kind_carries_its_own_reference(self) -> "WorkPlaneOp":
        wanted = self._REQUIRED_REFERENCE.get(self.kind)
        # What is *there* and means nothing is reported before what is absent.
        # A recipe carrying both faults -- `kind: "tangent"` with an `axis` --
        # has almost certainly named the wrong kind, and "axis means nothing to
        # a tangent plane" says that where "a tangent plane needs a face" reads
        # as though the axis were fine.
        if self.axis and wanted != "axis":
            raise ValueError(
                f"`axis` means nothing to a {self.kind!r} work plane; only an "
                "'angle' plane turns about one."
            )
        if self.face and wanted != "face":
            raise ValueError(
                f"`face` means nothing to a {self.kind!r} work plane; only a "
                "'tangent' plane touches one."
            )
        if wanted == "axis" and not self.axis:
            raise ValueError(
                "An 'angle' work plane needs `axis`: which axis to turn the base "
                "plane about. Use 'x', 'y' or 'z', a work axis, or a sketch line."
            )
        if wanted == "face" and not self.face:
            raise ValueError(
                "A 'tangent' work plane needs `face`: the cylindrical face it "
                "touches. Give a selector, the way a fillet names its edges."
            )
        return self


class WorkPointOp(OpBase):
    """A named point in space, placed in a plane's own 2D coordinates.

    It exists to be referred to: a work axis through two of them, or a
    dimension measured to one. `at` and `offset` are expressions like every
    other number here, so a point put at `[bolt_x, bolt_y]` moves when the
    parameter does.
    """

    op: Literal["work_point"] = "work_point"
    plane: PlaneRef = Field(
        "xy", description="Plane the point is placed on: 'xy' | 'xz' | 'yz' or a named work plane."
    )
    at: Point2D = Field([0.0, 0.0], description="Where on that plane, in the plane's coordinates.")
    offset: ValueSpec = Field(
        0.0, description="Lift the point off the plane along its normal."
    )


class WorkAxisOp(OpBase):
    """An axis in space, for a circular pattern that does not turn about the origin.

    A bolt circle centred anywhere but the origin needs one. The reason is
    geometric rather than incidental: a `circular_pattern` turns about an axis
    perpendicular to the face it patterns, a sketch line lies *in* its own
    sketch plane, and so no line drawn on a plate's face can ever be that
    plate's bolt-circle axis. Before this operation the only way round it was a
    throwaway sketch on a perpendicular plane carrying a line in that plane's
    coordinates -- which works, and asks the caller to do the axis mapping in
    their head.

    `normal_to_plane` is the bolt-circle case and the default: perpendicular to
    a plane, through a point given in that plane's coordinates. The other two
    name geometry that already exists -- two work points, or a sketch line.
    """

    op: Literal["work_axis"] = "work_axis"
    kind: Literal["normal_to_plane", "two_points", "sketch_line"] = "normal_to_plane"
    plane: PlaneRef = Field(
        "xy", description="For `normal_to_plane`: the plane the axis stands perpendicular to."
    )
    at: Point2D = Field(
        [0.0, 0.0],
        description="For `normal_to_plane`: where the axis crosses that plane, in its coordinates.",
    )
    points: list[PointRef] = Field(
        default_factory=list,
        description="For `two_points`: exactly two work-point names the axis runs through.",
    )
    line: str | None = Field(
        None, description="For `sketch_line`: the name of a sketch line to lie along."
    )
    sketch: str | None = Field(
        None, description="For `sketch_line`: which sketch holds it; defaults to the most recent."
    )

    @model_validator(mode="after")
    def _kind_has_what_it_needs(self) -> "WorkAxisOp":
        """Refuse a work axis whose kind was given without the geometry it names.

        A `two_points` axis with no points, or a `sketch_line` with no line,
        would otherwise fall back to the `normal_to_plane` default and build an
        axis somewhere nobody asked for -- the quiet wrong answer this schema
        exists to refuse.
        """
        if self.kind == "two_points" and len(self.points) != 2:
            raise ValueError(
                f"A 'two_points' work axis needs exactly two work-point names, "
                f"got {len(self.points)}."
            )
        if self.kind == "sketch_line" and not self.line:
            raise ValueError("A 'sketch_line' work axis needs `line` set to a named sketch line.")
        if self.kind != "two_points" and self.points:
            raise ValueError(f"`points` only means something on a 'two_points' axis, not {self.kind!r}.")
        if self.kind != "sketch_line" and self.line:
            raise ValueError(f"`line` only means something on a 'sketch_line' axis, not {self.kind!r}.")
        return self


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


class EmbossOp(OpBase):
    """Raise or sink a sketch profile -- usually text -- on a face.

    `engrave` cuts into the part, `raise` adds material standing off it. Depth is
    measured from the sketch plane, so put the sketch on the face being marked.
    The profile must fit inside that face: text running off the edge is refused.

    Inventor's emboss-from-face takes no draft angle, so there is no `taper` here;
    a moulded part that needs drafted lettering wants a tapered extrude cut instead.
    """

    op: Literal["emboss"] = "emboss"
    sketch: str = Field(description="Sketch holding the profile or text to emboss.")
    depth: ValueSpec = Field(0.5, description="How deep to engrave, or how far to raise.")
    style: Literal["engrave", "raise"] = "engrave"
    flip: bool = Field(
        False, description="Reverse which side of the sketch plane the emboss goes."
    )


class DraftOp(OpBase):
    """Taper faces away from a parting plane so a moulded part can leave the tool.

    This is the standalone draft feature, as opposed to the `taper` on an extrude:
    it can be applied to faces that already exist, which is what makes it useful
    on a part whose walls were built before anyone thought about the tooling.
    """

    op: Literal["draft"] = "draft"
    faces: Selector = Field(
        default_factory=lambda: Selector(kind="face"),
        description="Faces to draft. Usually the vertical walls.",
    )
    plane: PlaneRef = Field(
        "xy", description="The parting plane. Faces are tapered about their edge on it."
    )
    angle: ValueSpec = Field("1 deg", description="Draft angle. Moulding usually wants 1-3 deg.")
    flip: bool = Field(False, description="Reverse the pull direction.")


class MoveFaceOp(OpBase):
    """Translate faces of a solid that already exists, along a direction.

    The only operation here that changes geometry it did not create, which is
    what makes it the one way into imported material: a translated STEP body has
    no sketches and no parameters, so every other operation in this schema has
    nothing to take hold of. `import_geometry` could read a part for DFM and
    then change nothing about it; this is the answer to that.

    Inventor's move-face feature also offers a planar drag and a rotation about
    a line. Only the direction-and-distance move is here, and deliberately: it
    is the one a wall thickness or a clearance is expressed in, and it is the
    one whose result is predictable -- a planar face of area A moved by `d`
    along its own normal changes the part by exactly `A*d`, which is a number
    the rehearsal can check Inventor against. A free drag is not.

    `distance` is an expression like every other length, so a face moved by
    `wall_t` moves when that parameter does -- on a part built here. On imported
    geometry there is no parameter to drive and the expression is worth only the
    number it evaluates to, which is the honest limit of this operation on the
    material it exists for.
    """

    op: Literal["move_face"] = "move_face"
    faces: Selector = Field(
        default_factory=lambda: Selector(kind="face"),
        description="Faces to move. A wall's outer face, a boss's top.",
    )
    direction: AxisRef = Field(
        "z",
        description="Which way they go: 'x'|'y'|'z', a named work axis, the name of a "
        "sketch line, or 'edge:<handle>'.",
    )
    distance: ValueSpec = Field(
        1.0, description="How far along `direction`. Always positive -- use `flip` to reverse."
    )
    flip: bool = Field(False, description="Move against `direction` rather than along it.")


class ThickenOp(OpBase):
    """Add or remove a layer of material on faces, each along its own normal.

    The wall-thickness operation. Where `move_face` translates faces along one
    direction the caller names, this thickens each face along its *own* normal,
    which is what "make every wall 0.5 mm thicker" means and what a single
    direction cannot say: the four walls of a box point four different ways.

    **What each direction and operation pair does to a solid**, which is set
    algebra rather than an Inventor quirk -- the layer is a slab swept from the
    face and the operation is a boolean, so:

    * `positive` + `join` grows the part by area times thickness. The usual one.
    * `negative` + `cut` removes that much from behind the faces. Thinning.
    * `symmetric` straddles the face, so half the layer is already material and
      the net change is half the thickness either way.
    * `positive` + `cut` and `negative` + `join` change **nothing** -- the layer
      is where the material already is not, or already is. They are warned
      about at rehearsal rather than refused, because that is a claim about
      Inventor nobody here has measured yet.

    Inventor's offset mode, its `intersect` and `new_body` operations and its
    surface output are all deliberately absent. Offset and surface output both
    produce a surface body, and this server has no surface anywhere -- nothing
    downstream could take one. `intersect` on a layer outside the solid leaves
    nothing at all, which is a way to delete a part rather than an operation.

    The other half of what Inventor's Thicken does -- **turning a surface into
    a wall** -- is out of reach for the same reason as the offset mode, and not
    because of this schema: no operation here creates a surface, so the only
    surface a part could hold is one that arrived through `import_geometry`.
    """

    op: Literal["thicken"] = "thicken"
    faces: Selector = Field(
        default_factory=lambda: Selector(kind="face"),
        description="Faces to thicken. Each grows along its own normal.",
    )
    thickness: ValueSpec = Field(
        1.0, description="How thick a layer. Always positive -- `direction` says which way."
    )
    direction: Direction = Field(
        "positive",
        description="'positive' outward along each face's normal, 'negative' inward, "
        "'symmetric' half either side.",
    )
    operation: Literal["join", "cut"] = Field(
        "join", description="Add the layer or remove it."
    )

class RibOp(OpBase):
    """A rib: a thin web standing on the part, in a plane you choose.

    Inventor's own Rib feature will not take a definition through the API --
    `RibFeatures.Add` refuses every one `CreateDefinition` produces -- so this is
    built by hand from the rib's silhouette and a symmetric extrude. It is the
    same geometry and stays parametric; it is simply not a Rib in the browser.

    The rib is described by its top edge (`start` to `end`, in the sketch plane's
    coordinates) and the level its foot sits at (`root`). Those four corners are
    the silhouette, which is then thickened either side of the plane. A sloped
    top is fine -- give `start` and `end` different heights.

    There is no draft here. A moulded rib should thin as it rises, and a single
    silhouette pushed through a linear extrude cannot express that -- an extrude's
    `taper` drafts across the thickness instead, which measurably *adds* material
    rather than releasing the rib. Narrow the silhouette if you need the effect.
    """

    op: Literal["rib"] = "rib"
    plane: PlaneRef = Field("xz", description="The plane the rib lies in.")
    start: Point2D = Field(description="One end of the rib's top edge, in plane coordinates.")
    end: Point2D = Field(description="The other end of the top edge.")
    root: ValueSpec = Field(
        0.0, description="Height of the rib's foot, where it meets the part."
    )
    thickness: ValueSpec = Field(2.0, description="Total thickness, centred on the plane.")


class CombineOp(OpBase):
    """Boolean one solid body into another.

    Needs more than one body, which means an earlier `extrude` with
    `operation: "new_body"`. Bodies are numbered in creation order, 1-based.
    """

    op: Literal["combine"] = "combine"
    base: int = Field(1, description="Body to keep, 1-based in creation order.", ge=1)
    tools: list[int] = Field(
        default_factory=lambda: [2], description="Bodies to combine into the base."
    )
    operation: Literal["join", "cut", "intersect"] = "join"
    keep_tools: bool = Field(False, description="Leave the tool bodies in place afterwards.")


class SplitOp(OpBase):
    """Cut the part with a plane -- to open it up, or to make a lid from a base.

    `trim` throws one side away and is how a tray and its lid come from one solid.
    `split` keeps both halves as separate bodies. `faces` only divides the faces it
    crosses, leaving one solid, which is what you want before drafting each side of
    a parting line differently.
    """

    op: Literal["split"] = "split"
    tool: PlaneRef = Field("xy", description="Plane to cut with. A work plane name also works.")
    style: Literal["trim", "split", "faces"] = "trim"
    remove_positive: bool = Field(
        True, description="For `trim`, discard the side the plane's normal points at."
    )


class BossOp(OpBase):
    """A mounting post with a hole down it, at one or more positions.

    Inventor's own Boss feature cannot be created through the API -- the
    `BossFeatures` collection is read-only -- so this builds the same geometry from
    a circle, a join extrude and a hole. That means it appears in the browser as
    those features rather than as a Boss, and it is not editable as one.
    """

    op: Literal["boss"] = "boss"
    positions: list[Point2D] = Field(
        default_factory=lambda: [[0.0, 0.0]], description="Boss centres on `plane`."
    )
    plane: PlaneRef = Field("xy", description="Plane the bosses stand on.")
    diameter: ValueSpec = Field(6.0, description="Outside diameter of the post.")
    height: ValueSpec = Field(10.0, description="How far the post stands off the plane.")
    hole_diameter: ValueSpec | None = Field(
        None, description="Pilot hole diameter. Omit for a solid post."
    )
    hole_depth: ValueSpec | None = Field(
        None, description="Pilot depth. Defaults to 80% of the height."
    )
    tap: str | None = Field(
        None, description="Thread designation, e.g. 'M3x0.5'. Give the tapping drill as "
        "`hole_diameter`."
    )


Operation = Annotated[
    Union[
        SketchOp,
        ExtrudeOp,
        RevolveOp,
        SweepOp,
        CoilOp,
        LoftOp,
        HoleOp,
        FilletOp,
        ChamferOp,
        ShellOp,
        RectangularPatternOp,
        CircularPatternOp,
        SketchDrivenPatternOp,
        MirrorOp,
        WorkPlaneOp,
        WorkPointOp,
        WorkAxisOp,
        ThreadOp,
        EmbossOp,
        DraftOp,
        MoveFaceOp,
        ThickenOp,
        RibOp,
        CombineOp,
        SplitOp,
        BossOp,
        MaterialOp,
    ],
    Field(discriminator="op"),
]


# ---------------------------------------------------------------------------
# The recipe
# ---------------------------------------------------------------------------


class DfmSpec(Base):
    """How this part is judged for manufacture, and what may not be changed.

    Wholly optional: a recipe without this block builds exactly as before. With
    it, the DFM loop can read the model's own parameters instead of asking
    somebody to retype them, and knows what it is not allowed to move.
    """

    parameters: dict[str, Name] = Field(
        default_factory=dict,
        description="Which parameter plays which role in the manufacturability "
        "assessment, as {role: parameter}. Roles: " + ", ".join(DFM_ROLE_NAMES)
        + ". Declaring 'wall' matters most -- the rib, boss and corner guidelines "
        "are all fractions of the nominal wall.",
    )
    frozen: list[str] = Field(
        default_factory=list,
        description="Parameters an automated change may not touch, over and above "
        "those marked frozen individually. A '*' glob is allowed, so 'seal_*' "
        "protects a family.",
    )
    frozen_features: list[str] = Field(
        default_factory=list,
        description="Features an automated change may not suppress, delete or edit.",
    )
    settings: dict[str, Any] = Field(
        default_factory=dict,
        description="Settings for the DFM analyser, using its own names: "
        "'material' ('abs', 'pp', 'pc', ...), 'surfaceFinish', 'moldType', and the "
        "'checks' to run. Anything not given here keeps the tool's own default.",
    )

    @field_validator("parameters")
    @classmethod
    def _known_roles(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - set(DFM_ROLE_NAMES))
        if unknown:
            raise ValueError(
                f"Unknown DFM role(s) {unknown}. Known roles: {list(DFM_ROLE_NAMES)}."
            )
        return value


def _boss_depth(op: "BossOp") -> ValueSpec:
    """How deep a boss's pilot goes when the recipe does not say.

    Four fifths of the post, which leaves a floor under the hole rather than
    breaking through into whatever the boss is standing on.
    """
    if op.hole_depth is not None:
        return op.hole_depth
    if isinstance(op.height, (int, float)):
        return float(op.height) * 0.8
    return f"({op.height}) * 0.8"


class PartRecipe(Base):
    """A complete, replayable description of a parametric part."""

    name: str = Field("Part", description="Part name, also used as the default file name.")
    description: str = Field("", description="What the part is, in one line.")
    units: LengthUnit = Field("mm", description="Unit for bare numbers in this recipe.")
    angle_units: AngleUnit = Field("deg", description="Unit for bare angle numbers.")
    material: str | None = None
    parameters: list[ParameterSpec] = Field(default_factory=list)
    operations: list[Operation] = Field(default_factory=list)
    dfm: DfmSpec | None = Field(
        None,
        description="Manufacturability: which parameter means what, and which are "
        "key geometry that must not be changed automatically.",
    )

    @model_validator(mode="after")
    def _expand_bosses(self) -> "PartRecipe":
        """Turn every `boss` and `rib` into the features that actually build one.

        Neither can be created through Inventor's API -- `BossFeatures` has no
        `Add` at all, and `RibFeatures.Add` refuses every definition it is given --
        so a boss is a post, a join extrude and a hole, and a rib is a silhouette
        and a symmetric extrude. Expanding here rather than in the builder means
        `validate_recipe` rehearses exactly what will be built, and the operation
        list a caller gets back is the truth about what went into the part.
        """
        if not any(isinstance(op, (BossOp, RibOp)) for op in self.operations):
            return self
        expanded: list[Operation] = []
        for index, op in enumerate(self.operations):
            if isinstance(op, RibOp):
                stem = op.name or f"Rib{index + 1}"
                expanded.append(SketchOp(
                    name=f"{stem}Profile", plane=op.plane,
                    entities=[PolylineEntity(points=[
                        list(op.start), list(op.end),
                        [op.end[0], op.root], [op.start[0], op.root],
                    ], closed=True)],
                ))
                expanded.append(ExtrudeOp(
                    name=stem, sketch=f"{stem}Profile", distance=op.thickness,
                    operation="join", direction="symmetric",
                ))
                continue
            if not isinstance(op, BossOp):
                expanded.append(op)
                continue
            stem = op.name or f"Boss{index + 1}"
            expanded.append(SketchOp(
                name=f"{stem}Profiles", plane=op.plane,
                entities=[CircleEntity(center=list(point), diameter=op.diameter)
                          for point in op.positions],
            ))
            expanded.append(ExtrudeOp(
                name=stem, sketch=f"{stem}Profiles", distance=op.height,
                operation="join", direction="positive",
            ))
            if op.hole_diameter is None:
                continue
            expanded.append(WorkPlaneOp(
                name=f"{stem}Top", kind="offset", base=op.plane, offset=op.height,
            ))
            expanded.append(SketchOp(
                name=f"{stem}Pilots", plane=f"{stem}Top",
                entities=[PointEntity(position=list(point)) for point in op.positions],
            ))
            expanded.append(HoleOp(
                name=f"{stem}Holes", sketch=f"{stem}Pilots",
                diameter=op.hole_diameter, depth=_boss_depth(op),
                direction="negative", tap=op.tap,
            ))
        object.__setattr__(self, "operations", expanded)
        return self

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
    def _dfm_names_exist(self) -> "PartRecipe":
        """A role pointing at a parameter that is not there is a typo, and it
        would otherwise surface much later as "no parameter is declared for
        'wall'" while the recipe plainly declares one."""
        if self.dfm is None:
            return self
        known = {parameter.name.lower() for parameter in self.parameters}
        for role, name in self.dfm.parameters.items():
            if name.lower() not in known:
                raise ValueError(
                    f"The DFM role {role!r} points at a parameter {name!r} that this "
                    f"recipe does not declare."
                )
        for pattern in self.dfm.frozen:
            if "*" in pattern or "?" in pattern:
                continue
            if pattern.lower() not in known:
                raise ValueError(
                    f"{pattern!r} is listed as frozen but is not a parameter of this "
                    f"recipe. Use a glob such as 'seal_*' to protect names that do "
                    f"not exist yet."
                )
        return self

    @model_validator(mode="after")
    def _has_work(self) -> "PartRecipe":
        if not self.operations:
            raise ValueError("A recipe needs at least one operation.")
        return self


def recipe_json_schema() -> dict:
    """The recipe JSON Schema, published as an MCP resource."""
    return PartRecipe.model_json_schema()


# ---------------------------------------------------------------------------
# Drawings
# ---------------------------------------------------------------------------

#: Which way a base view looks at the part. The names are the ones a drawing
#: uses, and they are checked against the part rather than trusted: defect 4 in
#: `docs/FEATURE_COVERAGE.md` is `capture_view`'s orientation names not
#: describing what they return, and a drawing view is a different API reached
#: the same way -- through a name nobody here has measured.
ViewDirection = Literal["front", "rear", "top", "bottom", "left", "right", "iso"]


class DrawingViewSpec(Base):
    """One view on the sheet, and which of the part's parameters it dimensions.

    `dimension` is the differentiator and the reason this schema exists. A
    drawing generator has to decide which dimensions matter, and the field's
    tools guess -- reaching 80-90% by inference from the geometry. A recipe does
    not have to guess: the part's parameters *are* its design intent, so naming
    them is saying "these are the numbers that matter", which is a statement the
    author already made when they wrote the part.

    So an entry here is normally a parameter name. An expression of parameters
    is accepted too, for the dimension a drawing states that the model derives
    -- an overall width of `plate_w + 2 * wall`, say -- and it is resolved
    through the same evaluator as every other number here.
    """

    name: Name = Field(description="The view's label on the sheet: 'FRONT', 'TOP'.")
    direction: ViewDirection = Field(
        "front",
        description="Which way this view looks at the part, in INVENTOR's own "
        "naming, which is Y-up: 'front' shows the XY plane, 'top' shows XZ, "
        "'left' and 'right' show YZ. So a plate sketched on XY and extruded "
        "upward -- which is how every recipe here models -- has its PLAN as "
        "its front view and its edge as its top view. Measured on 2027.1; the "
        "alternative was a vocabulary that disagreed with the sheet Inventor "
        "actually draws. `capture_view` uses the same names the same way.",
    )
    at: Point2D | None = Field(
        None,
        description="Where on the sheet the view's centre goes, in sheet units. "
        "For a base view; a projected view is positioned by `parent` and `gap` "
        "instead, and giving both is refused.",
    )
    parent: str | None = Field(
        None,
        description="Name of the view this is projected from. A projected view "
        "inherits its parent's scale and stays aligned with it, which is how a "
        "multi-view drawing is actually built -- and which side it lands on is "
        "decided by the sheet's projection angle rather than by a position.",
    )
    gap: ValueSpec = Field(
        60.0,
        description="For a projected view: how far from the parent's centre, in "
        "sheet units.",
    )
    scale: ValueSpec = Field(
        1.0,
        description="View scale as a factor: 0.5 is half size. Written on the "
        "sheet as a ratio; given here as a number so it can be an expression.",
    )
    dimension: list[str] = Field(
        default_factory=list,
        description="Parameters to dimension on this view, or expressions of them. "
        "A parameter named here must exist in the part.",
    )
    reference: list[str] = Field(
        default_factory=list,
        description="The same, for dimensions shown in brackets: they restate "
        "something fixed elsewhere and drive nothing.",
    )
    style: Literal["hidden_line", "hidden_line_removed", "shaded"] = Field(
        "hidden_line_removed", description="How the view is drawn."
    )

    @model_validator(mode="after")
    def _positioned_one_way_or_the_other(self) -> "DrawingViewSpec":
        """A projected view is not positioned by hand, and that is the point.

        Where a projected view lands is what first and third angle *mean*: the
        top view goes below the front view in first angle and above it in third.
        A recipe that gave a projected view an explicit position would be
        deciding that for itself, and the sheet's stated projection angle would
        then be a label on a layout that need not match it -- which is worse
        than either convention, because a reader trusts the symbol.
        """
        if self.parent is not None and self.at is not None:
            raise ValueError(
                f"View {self.name!r} is projected from {self.parent!r} and also "
                "given a position. Which side a projected view lands on is "
                "decided by the sheet's projection angle; drop `at`, or drop "
                "`parent` to place it by hand as a base view.")
        return self

    @model_validator(mode="after")
    def _projected_views_are_not_the_front(self) -> "DrawingViewSpec":
        """`front` and `rear` are not projected from anything here.

        A front view is the one everything else is projected *from*, and a rear
        view is two projections away -- Inventor will place one and where it goes
        is a drafting convention this project has not measured. Both are
        available as base views, which is how a second one would be drawn
        anyway.
        """
        if self.parent is not None and self.direction in ("front", "rear"):
            raise ValueError(
                f"A {self.direction!r} view is not projected from another view. "
                "A front view is what the others project from; place it, and a "
                "rear view, as base views with `at`.")
        return self


class DrawingRecipe(Base):
    """A description of a drawing *of* a part, which is a different noun.

    The second root in this schema, and the reason there had to be one:
    `PartRecipe` describes a solid, and a drawing describes views of one. Nothing
    below the schema changes -- the resolver, the expression evaluator and the
    unit table are the same, and a dimension's value is `Resolved` like every
    other number, carrying the expression that produced it.

    What this does *not* do is draw anything. It says what a drawing should say,
    which is enough to check it against the part before a CAD seat is spent on
    it -- the same argument `validate_recipe` makes for a part. Producing the
    sheet in Inventor is a separate piece of work and is not written yet.
    """

    name: str = Field("Drawing", description="Drawing name, also the default file name.")
    description: str = Field("", description="What the drawing is for, in one line.")
    part: str | None = Field(
        None,
        description="Name of the part this draws. Informational: the part is "
        "supplied to the rehearsal as its own recipe.",
    )
    units: LengthUnit = Field(
        "mm", description="Unit the dimensions on this sheet are read in."
    )
    angle_units: AngleUnit = Field("deg", description="Unit for angle dimensions.")
    sheet: Literal["a0", "a1", "a2", "a3", "a4", "custom"] = Field(
        "a3", description="Sheet size. 'custom' needs `sheet_size`."
    )
    sheet_size: Point2D | None = Field(
        None, description="For 'custom': [width, height] in `units`."
    )
    template: str | None = Field(
        None,
        description="A .dwg or .idw template carrying the title block: a full "
        "path, or a bare filename to look for in Inventor's own templates "
        "folder and one level below it. Without one the sheet has no title "
        "block, which is not a drawing anybody can send to a factory.",
    )
    projection: Literal["first_angle", "third_angle"] = Field(
        "third_angle",
        description="Which side of the front view the right-hand view goes on. "
        "There is no 'unknown' here: a drawing being *produced* has to pick one, "
        "and getting it wrong mirrors the part for whoever reads it.",
    )
    scale: str = Field("1:1", description="The sheet's stated scale, as written.")
    views: Annotated[list[DrawingViewSpec], Field(min_length=1)] = Field(
        description="At least one view. A drawing with no view is a title block."
    )
    notes: list[str] = Field(
        default_factory=list,
        description="The note block: finishes, general tolerances, 'ALL FILLETS R2 "
        "UNLESS STATED'.",
    )

    @model_validator(mode="after")
    def _custom_sheets_have_a_size(self) -> "DrawingRecipe":
        if self.sheet == "custom" and self.sheet_size is None:
            raise ValueError("A 'custom' sheet needs `sheet_size` as [width, height].")
        if self.sheet != "custom" and self.sheet_size is not None:
            raise ValueError(
                f"`sheet_size` only means something on a 'custom' sheet, not {self.sheet!r}."
            )
        return self

    @model_validator(mode="after")
    def _view_names_are_distinct(self) -> "DrawingRecipe":
        """Two views with one name cannot both be referred to, or told apart.

        A dimension is reported against the view it appears on, so duplicate
        names would make the report ambiguous about a sheet that is itself fine
        -- which is worse than refusing it.
        """
        seen = [view.name for view in self.views]
        repeated = sorted({name for name in seen if seen.count(name) > 1})
        if repeated:
            raise ValueError(f"Two or more views share a name: {', '.join(repeated)}.")
        return self
