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

from typing import Annotated, Any, Literal, Union

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
    bodies: list[int] | None = Field(
        None,
        description="Bodies this feature may affect, 1-based in creation order. "
        "Omit to leave Inventor's default, which is the first body only -- so a "
        "cut aimed at a second body needs this.",
    )

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
        EmbossOp,
        DraftOp,
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
