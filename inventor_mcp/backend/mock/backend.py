"""An in-memory Inventor stand-in.

The mock backend exists for two reasons.  It is what the test suite runs
against, and it lets a recipe be written, validated and dry-run on a machine
where Inventor is not installed -- so a model can be checked for
"does this sketch close?", "how many vertical edges will the fillet catch?",
"is the wall thickness larger than the plate?" before anyone opens Inventor.

It is deliberately *not* a geometry kernel.  Bodies are tracked as a bounding
box, a volume estimate and the list of prisms that made them, and topology is
synthesised from the sketch loops that produced it.  That is enough to exercise
selectors and catch the mistakes that matter, and the reported numbers are
labelled as estimates everywhere they surface.

The prism list earns its keep on the one question a bounding box answers badly:
how thick is the part *here*.  An L-section's box is as deep as the upright is
tall, so a through-cut in its base was charged fifteen times the material it
removes -- and a mirror of that cut doubled the error.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Iterable, Sequence

from ...errors import DocumentError, FeatureError, ParameterError, SelectionError, SketchError
from ...expressions import UnitContext, evaluate, referenced_parameters
from ...geometry import (
    clip_to_box,
    inset_area,
    loop_area,
    loop_points,
    plan_bounds,
    polygon_centroid,
    profile_loops,
)
from ...plan import PArc, PCircle, PEllipse, PLine, PPoint, SketchPlan
from ...units import Dim, Quantity, from_internal, lookup_unit
from ..base import (
    AppInfo,
    AxisSpec,
    Backend,
    ChamferRequest,
    CircularPatternRequest,
    DocInfo,
    ExportRequest,
    ExtrudeRequest,
    FeatureInfo,
    FilletRequest,
    HoleRequest,
    LoftRequest,
    MassProps,
    MirrorRequest,
    ParamInfo,
    RectangularPatternRequest,
    ResolvedSelector,
    RevolveRequest,
    ScreenshotRequest,
    ShellRequest,
    SketchInfo,
    SweepRequest,
    ThreadRequest,
    TopoInfo,
    WorkPlaneRequest,
)

#: Plane name -> (axis index and sign for sketch u, sketch v and the plane
#: normal; normal vector).  These are the axes a *recipe* means.  Inventor's
#: own XZ plane runs its first axis along -X; the COM backend mirrors the
#: geometry on the way in so that both backends put a recipe's coordinates in
#: the same place.
_PLANES: dict[
    str, tuple[tuple[tuple[int, float], tuple[int, float], tuple[int, float]],
               tuple[float, float, float]]
] = {
    "xy": (((0, 1.0), (1, 1.0), (2, 1.0)), (0.0, 0.0, 1.0)),
    "xz": (((0, 1.0), (2, 1.0), (1, 1.0)), (0.0, 1.0, 0.0)),
    "yz": (((1, 1.0), (2, 1.0), (0, 1.0)), (1.0, 0.0, 0.0)),
}

#: Rough densities in kg/cm^3 for the handful of materials worth guessing at.
_DENSITY = {
    "steel": 7.85e-3,
    "stainless steel": 8.0e-3,
    "aluminum": 2.70e-3,
    "aluminium": 2.70e-3,
    "abs plastic": 1.06e-3,
    "abs": 1.06e-3,
    "pla": 1.24e-3,
    "brass": 8.5e-3,
    "titanium": 4.51e-3,
    "nylon": 1.15e-3,
}


def map3d(plane: str, u: float, v: float, w: float) -> tuple[float, float, float]:
    """Map a sketch coordinate onto model space for *plane*."""
    axes, _ = _PLANES[plane]
    coords = [0.0, 0.0, 0.0]
    for value, (axis, sign) in zip((u, v, w), axes):
        coords[axis] = value * sign
    return (coords[0], coords[1], coords[2])


def plane_normal(plane: str) -> tuple[float, float, float]:
    return _PLANES[plane][1]


@dataclass
class _Topo:
    id: str
    kind: str
    description: str
    feature: str
    geometry: str
    midpoint: tuple[float, float, float]
    normal: tuple[float, float, float] | None = None
    direction: tuple[float, float, float] | None = None
    length: float | None = None
    area: float | None = None
    consumed: bool = False
    #: "convex" | "concave" | None. Set where the simulator can actually tell:
    #: an edge running along an extrusion at a corner of its profile is convex
    #: or concave exactly as that corner turns. Everything else stays None and
    #: matches neither filter, as on the live backend.
    convexity: str | None = None

    def to_info(self) -> TopoInfo:
        return TopoInfo(
            id=self.id,
            kind=self.kind,  # type: ignore[arg-type]
            description=self.description,
            feature=self.feature,
            midpoint=self.midpoint,
            normal=self.normal,
            direction=self.direction,
            length=self.length,
            area=self.area,
            geometry=self.geometry,
            convexity=self.convexity,
            convexity_from="profile corner" if self.convexity else None,
        )


@dataclass
class _Sketch:
    id: str
    name: str
    plan: SketchPlan
    loops: list[list[str]]
    consumed_by: str | None = None
    #: The origin plane this sketch really lies on, and how far along its normal.
    #: A sketch on a named work plane carries the plane's own offset, which the
    #: plan does not know about -- without this the flanged shaft was built from
    #: z=0 rather than from the top of its flange, and came out 12 mm short.
    base_plane: str = "xy"
    offset: float = 0.0


@dataclass
class _Slab:
    """A prism the part is made of: a 2D profile swept along its plane normal.

    Recorded so that "how thick is the part here" can be answered from the
    geometry that built it rather than from a bounding box. An L-section's box
    is 90 mm deep where the L itself is 6, and a through-cut charged the box.
    """

    plane: str
    #: The loop's vertices in sketch coordinates, closed.
    outline: list[tuple[float, float]]
    #: Where the sweep starts and ends along the plane's normal.
    near: float
    far: float

    def interval_along(self, axis: int,
                       through: tuple[float, float, float]) -> tuple[float, float] | None:
        """This prism's extent along *axis* over the point *through*, if it covers it.

        Two cases, because a prism is a profile and a length. When *axis* is the
        sweep direction the extent is simply the sweep, provided the point lies
        inside the profile. When *axis* lies in the sketch plane the extent is
        the profile's own reach along that sketch direction at the point's other
        coordinate -- a vertical scan of the outline -- provided the point lies
        within the sweep.
        """
        (u_axis, u_sign), (v_axis, v_sign), (w_axis, w_sign) = _PLANES[self.plane][0]
        u = through[u_axis] * u_sign
        v = through[v_axis] * v_sign
        w = through[w_axis] * w_sign

        if axis == w_axis:
            if not _inside(self.outline, u, v):
                return None
            low, high = min(self.near, self.far), max(self.near, self.far)
            return (low * w_sign, high * w_sign) if w_sign > 0 else (high * w_sign, low * w_sign)

        if not min(self.near, self.far) <= w <= max(self.near, self.far):
            return None
        if axis == u_axis:
            reach = _scan(self.outline, v, across=True)
            sign = u_sign
        elif axis == v_axis:
            reach = _scan(self.outline, u, across=False)
            sign = v_sign
        else:  # pragma: no cover - a plane's three axes are all accounted for
            return None
        if reach is None:
            return None
        low, high = reach
        return (low * sign, high * sign) if sign > 0 else (high * sign, low * sign)


@dataclass
class _Feature:
    id: str
    name: str
    kind: str
    suppressed: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    #: How much this feature changed the volume, in cm^3, signed. Recorded so a
    #: mirror or a pattern of it can say what *its* occurrences do: an
    #: occurrence repeats whatever the seed did, and without this the simulator
    #: had to report a mirrored cut as removing nothing.
    volume_delta: float | None = None


@dataclass
class _Document:
    id: str
    name: str
    units: str = "mm"
    angle_units: str = "deg"
    #: The DFM declaration kept in the document, as Inventor keeps one in a
    #: custom property. ``None`` means nobody has asked this part, which is not
    #: the same as this part saying nothing is frozen.
    declaration: dict[str, Any] | None = None
    path: str | None = None
    material: str | None = None
    modified: bool = False
    parameters: dict[str, ParamInfo] = field(default_factory=dict)
    sketches: list[_Sketch] = field(default_factory=list)
    features: list[_Feature] = field(default_factory=list)
    work_planes: dict[str, tuple[str, float]] = field(default_factory=dict)
    topology: list[_Topo] = field(default_factory=list)
    volume: float = 0.0
    bounds: list[float] | None = None  # xmin, ymin, zmin, xmax, ymax, zmax
    #: The prisms the solid is made of, in creation order. Only joined extrudes
    #: are recorded -- enough to answer "how thick is the part here", which is
    #: what a through-all cut has to know.
    slabs: list[_Slab] = field(default_factory=list)

    def find_sketch(self, name: str) -> _Sketch:
        for sketch in self.sketches:
            if sketch.name == name or sketch.id == name:
                return sketch
        known = ", ".join(s.name for s in self.sketches) or "(none)"
        raise SketchError(f"No sketch named {name!r}.", hint=f"Sketches in this part: {known}.")

    def find_feature(self, name: str) -> _Feature:
        for feature in self.features:
            if feature.name == name or feature.id == name:
                return feature
        known = ", ".join(f.name for f in self.features) or "(none)"
        raise FeatureError(f"No feature named {name!r}.", hint=f"Features: {known}.")


class MockBackend(Backend):
    """A dependency-free stand-in for a live Inventor session."""

    name = "mock"

    def __init__(self) -> None:
        self._connected = False
        self._documents: dict[str, _Document] = {}
        self._active: str | None = None
        self._ids = count(1)
        self._transactions: dict[str, tuple[str, _Document]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # -- helpers -----------------------------------------------------------
    def _next(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"

    def _doc(self, doc_id: str | None) -> _Document:
        key = doc_id or self._active
        if key is None:
            raise DocumentError("No document is open.", hint="Call `new_part` first.")
        document = self._documents.get(key)
        if document is None:
            for candidate in self._documents.values():
                if candidate.name == key:
                    return candidate
            raise DocumentError(f"Unknown document {key!r}.")
        return document

    def _record(self, action: str, **payload: Any) -> None:
        self.calls.append((action, payload))

    def _feature_name(self, document: _Document, requested: str | None, kind: str) -> str:
        if requested:
            if any(f.name == requested for f in document.features):
                raise FeatureError(f"A feature named {requested!r} already exists.")
            return requested
        base = kind.capitalize()
        index = 1
        while any(f.name == f"{base}{index}" for f in document.features):
            index += 1
        return f"{base}{index}"

    def _expand_bounds(self, document: _Document, points: Iterable[tuple[float, float, float]]) -> None:
        for point in points:
            if document.bounds is None:
                document.bounds = [point[0], point[1], point[2], point[0], point[1], point[2]]
            else:
                for axis in range(3):
                    document.bounds[axis] = min(document.bounds[axis], point[axis])
                    document.bounds[axis + 3] = max(document.bounds[axis + 3], point[axis])

    # -- session -----------------------------------------------------------
    def connect(self, *, visible: bool = True, create: bool = True) -> AppInfo:
        self._connected = True
        self._record("connect", visible=visible, create=create)
        return self.info()

    def disconnect(self) -> None:
        self._connected = False
        self._record("disconnect")

    def info(self) -> AppInfo:
        return AppInfo(
            backend=self.name,
            connected=self._connected,
            version="mock",
            visible=False,
            documents=len(self._documents),
            note="Simulated session: geometry is approximated and nothing is written to Inventor.",
        )

    # -- documents ---------------------------------------------------------
    def new_part(self, name: str, *, template: str | None = None, units: str = "mm",
                 angle_units: str = "deg") -> DocInfo:
        doc_id = self._next("doc")
        document = _Document(id=doc_id, name=name, units=units, angle_units=angle_units)
        self._documents[doc_id] = document
        self._active = doc_id
        self._record("new_part", name=name, units=units, template=template)
        return self._doc_info(document)

    def open_document(self, path: str) -> DocInfo:
        # A file that is already open comes back as the document it already is,
        # the way Inventor behaves -- minting a second handle for the same file
        # split the session's knowledge in two, and the new handle had no freeze
        # guard.
        for document in self._documents.values():
            if document.path == path:
                self._active = document.id
                self._record("open_document", path=path, already_open=True)
                return self._doc_info(document)
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        info = self.new_part(name)
        document = self._doc(info.id)
        document.path = path
        self._record("open_document", path=path)
        return self._doc_info(document)

    def import_geometry(self, path: str, *, name: str | None = None) -> DocInfo:
        """Stand in for reading a STEP file, without pretending to have read one.

        The simulator has no translator and no way to invent the geometry a real
        STEP file carries, so what it produces is a part with the shape of an
        import -- one base feature, no parameters, no volume -- and says as much.
        That is enough to exercise everything around the import: that the loop
        notices there is nothing to drive, that the declaration is resolved, that
        the right thing is reported. It is not enough to measure, and claiming a
        volume here would be inventing the one number the whole analysis rests
        on.
        """
        stem = name or path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        info = self.new_part(stem)
        document = self._doc(info.id)
        document.path = path
        document.features.append(_Feature(
            id=self._next("feat"), name="Imported", kind="base",
            detail={"from": path,
                    "note": "the simulator cannot read translated geometry"},
        ))
        self._record("import_geometry", path=path)
        out = self._doc_info(document)
        out.detail = {
            "imported": True,
            "parametric": False,
            "route": "the simulator, which has no translator",
            "bodies": 0,
            "note": "No geometry was read. Connect to Inventor to import a real file.",
        }
        return out

    def document_path(self, doc_id: str) -> str | None:
        return self._doc(doc_id).path

    def read_declaration(self, doc_id: str) -> dict[str, Any] | None:
        return self._doc(doc_id).declaration

    def write_declaration(self, doc_id: str, declaration: dict[str, Any]) -> None:
        document = self._doc(doc_id)
        document.declaration = dict(declaration)
        document.modified = True
        self._record("write_declaration", keys=sorted(declaration))

    def list_documents(self) -> list[DocInfo]:
        return [self._doc_info(document) for document in self._documents.values()]

    def activate_document(self, doc_id: str) -> DocInfo:
        document = self._doc(doc_id)
        self._active = document.id
        return self._doc_info(document)

    def save_document(self, doc_id: str, path: str | None = None) -> DocInfo:
        document = self._doc(doc_id)
        document.path = path or document.path or f"{document.name}.ipt"
        document.modified = False
        self._record("save_document", path=document.path)
        return self._doc_info(document)

    def close_document(self, doc_id: str, *, save: bool = False) -> None:
        document = self._doc(doc_id)
        self._documents.pop(document.id, None)
        if self._active == document.id:
            self._active = next(iter(self._documents), None)
        self._record("close_document", save=save)

    def set_material(self, doc_id: str, material: str, appearance: str | None = None) -> DocInfo:
        document = self._doc(doc_id)
        document.material = material
        self._record("set_material", material=material, appearance=appearance)
        return self._doc_info(document)

    def _doc_info(self, document: _Document) -> DocInfo:
        return DocInfo(
            id=document.id,
            name=document.name,
            path=document.path,
            units=document.units,
            angle_units=document.angle_units,
            active=document.id == self._active,
            modified=document.modified,
        )

    # -- parameters --------------------------------------------------------
    def set_parameter(self, doc_id: str, name: str, expression: str, *, units: str = "mm",
                      comment: str = "", key: bool = False) -> ParamInfo:
        document = self._doc(doc_id)
        # Stored values are in display units; expressions evaluate in database units.
        known = {
            parameter.name: Quantity(
                parameter.value * lookup_unit(parameter.units).factor,
                lookup_unit(parameter.units).dim,
            )
            for parameter in document.parameters.values()
            if parameter.name != name
        }
        try:
            result = evaluate(expression, known, UnitContext(document.units, document.angle_units))
        except Exception as exc:  # pragma: no cover - re-raised with context
            raise ParameterError(f"Parameter {name!r}: {exc}") from exc

        unit = units if lookup_unit(units).dim is result.dim else _unit_for(result.dim, units)
        info = ParamInfo(
            name=name,
            expression=expression,
            value=round(from_internal(result.value, unit), 9),
            units=unit,
            kind="user",
            comment=comment,
            key=key,
        )
        document.parameters[name] = info
        document.modified = True
        self._reevaluate(document)
        self._record("set_parameter", name=name, expression=expression)
        return document.parameters[name]

    def _reevaluate(self, document: "_Document") -> None:
        """Recompute every parameter that reads another one.

        A parametric model's whole point is that moving a driver moves what
        depends on it. This used to evaluate an expression once, at the moment it
        was set, and keep the number -- so ``rib_t = wall_t * 0.45`` stayed where
        it started for ever after the wall moved, and the simulator disagreed
        with Inventor about every dependent parameter in the document. Which
        matters most exactly where it is least visible: the DFM loop writes its
        ratio fixes as expressions *so that* they follow the wall, and a
        simulator that does not follow them would have been rehearsing a model
        nobody was going to get.

        Resolved by repeated passes rather than a topological sort: one pass
        settles one level of the chain, so as many passes as there are
        parameters covers any depth, and a circular reference simply stops
        changing instead of recursing. Inventor refuses circular references
        anyway.
        """
        for _ in range(len(document.parameters) + 1):
            moved = False
            for info in list(document.parameters.values()):
                if not isinstance(info.expression, str):
                    continue
                try:
                    reads = referenced_parameters(info.expression)
                except Exception:
                    continue
                if not reads:
                    continue
                known = {
                    other.name: Quantity(
                        other.value * lookup_unit(other.units).factor,
                        lookup_unit(other.units).dim,
                    )
                    for other in document.parameters.values()
                    if other.name != info.name
                }
                try:
                    result = evaluate(
                        info.expression, known,
                        UnitContext(document.units, document.angle_units),
                    )
                except Exception:
                    # A parameter whose driver has been deleted, or a unit that
                    # no longer works out. Left at its last good value; the
                    # rebuild is where that gets reported.
                    continue
                value = round(from_internal(result.value, info.units), 9)
                if value != info.value:
                    info.value = value
                    moved = True
            if not moved:
                break

    def list_parameters(self, doc_id: str, *, include_model: bool = False) -> list[ParamInfo]:
        document = self._doc(doc_id)
        return list(document.parameters.values())

    def delete_parameter(self, doc_id: str, name: str) -> None:
        document = self._doc(doc_id)
        if name not in document.parameters:
            raise ParameterError(f"No parameter named {name!r}.")
        del document.parameters[name]

    # -- sketches ----------------------------------------------------------
    def build_sketch(self, doc_id: str, plan: SketchPlan) -> SketchInfo:
        document = self._doc(doc_id)
        base_plane = plan.plane.split(":")[0]
        if base_plane not in _PLANES and plan.plane not in document.work_planes:
            raise SketchError(
                f"Unknown sketch plane {plan.plane!r}.",
                hint="Use 'xy', 'xz', 'yz', a work plane name, or 'face:<handle>' from `select`.",
            )
        name = plan.name or f"Sketch{len(document.sketches) + 1}"
        if any(sketch.name == name for sketch in document.sketches):
            raise SketchError(f"A sketch named {name!r} already exists.")

        loops = profile_loops(plan)
        base, offset = self._plane_and_offset(document, plan)
        sketch = _Sketch(id=self._next("sk"), name=name, plan=plan, loops=loops,
                         base_plane=base, offset=offset)
        document.sketches.append(sketch)
        document.modified = True
        self._record("build_sketch", name=name, plane=plan.plane, **plan.summary())

        free = _degrees_of_freedom(plan)
        return _sketch_info(sketch)

    def _plane_and_offset(self, document: _Document,
                          plan: SketchPlan) -> tuple[str, float]:
        """Which origin plane a sketch really lies on, and how far along it.

        A work plane contributes its own offset on top of any the sketch asks
        for; a `face:` reference has no cheap answer, so it falls back to XY.
        """
        named = plan.plane.split(":")[0]
        if named in _PLANES:
            return named, plan.offset_value
        if plan.plane in document.work_planes:
            base, offset = document.work_planes[plan.plane]
            return base, offset + plan.offset_value
        return "xy", plan.offset_value

    def list_sketches(self, doc_id: str) -> list[SketchInfo]:
        document = self._doc(doc_id)
        return [_sketch_info(sketch) for sketch in document.sketches]

    def topology_counts(self, doc_id: str) -> dict[str, int]:
        document = self._doc(doc_id)
        counts = {"faces": 0, "edges": 0}
        for topo in document.topology:
            if topo.consumed:
                continue
            counts["faces" if topo.kind == "face" else "edges"] += 1
        return counts

    # -- features ----------------------------------------------------------
    def extrude(self, doc_id: str, request: ExtrudeRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        sketch = document.find_sketch(request.sketch)
        loops = _selected_loops(sketch, request.profiles)
        if not loops:
            raise FeatureError(
                f"Sketch {sketch.name!r} has no closed profile to extrude.",
                hint="Every profile must be a closed loop of non-construction geometry.",
            )

        plane = sketch.base_plane
        if request.extent == "distance":
            assert request.distance is not None
            distance = request.distance.value
        else:
            # Measured over the profile itself: how thick the part is *there*,
            # which for a slot at the end of an L-bracket's base is the base and
            # not the whole 90 mm the bounding box would charge for.
            centre = _loop_center(sketch.plan, loops[0])
            distance = _through_all_distance(
                document, plane, over=map3d(plane, centre[0], centre[1], sketch.offset))

        area = _net_area(sketch, loops)
        name = self._feature_name(document, request.name, "extrusion")
        signed = area * distance
        was = document.volume
        if request.operation == "cut":
            document.volume = max(document.volume - signed, 0.0)
        else:
            document.volume += signed
        moved = document.volume - was

        self._synthesise_extrude_topology(document, sketch, loops, plane, distance,
                                          request.direction, name,
                                          cut=request.operation == "cut")
        if request.operation != "cut":
            self._record_slabs(document, sketch, loops, plane, distance, request.direction)

        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="extrude",
            volume_delta=moved,
            detail={
                "sketch": sketch.name,
                "operation": request.operation,
                "extent": request.extent,
                "distance": request.distance.as_dict() if request.distance else None,
                # Recorded because it is the only thing on a built feature that
                # says which parameter drives the draft, and role discovery on a
                # part nobody described reads exactly that.
                "taper": request.taper.as_dict() if request.taper else None,
                "profiles": len(loops),
                "profile_area_cm2": round(area, 6),
            },
        )
        document.features.append(feature)
        sketch.consumed_by = name
        document.modified = True
        self._record("extrude", name=name, sketch=sketch.name, operation=request.operation)
        return _feature_info(feature)

    def _record_slabs(self, document: _Document, sketch: _Sketch,
                      loops: Sequence[Sequence[str]], plane: str,
                      distance: float, direction: str) -> None:
        """Remember the prisms this extrude added, for later thickness queries."""
        if direction == "negative":
            near, far = -distance, 0.0
        elif direction == "symmetric":
            near, far = -distance / 2, distance / 2
        else:
            near, far = 0.0, distance
        near += sketch.offset
        far += sketch.offset
        for loop in loops:
            outline = loop_points(sketch.plan, loop)
            if len(outline) >= 3:
                document.slabs.append(
                    _Slab(plane=plane, outline=outline, near=near, far=far))

    def _synthesise_extrude_topology(
        self,
        document: _Document,
        sketch: _Sketch,
        loops: Sequence[Sequence[str]],
        plane: str,
        distance: float,
        direction: str,
        feature_name: str,
        cut: bool = False,
    ) -> None:
        normal = plane_normal(plane)
        offset = sketch.offset
        if direction == "negative":
            near, far = -distance, 0.0
        elif direction == "symmetric":
            near, far = -distance / 2, distance / 2
        else:
            near, far = 0.0, distance
        near += offset
        far += offset

        minx, miny, maxx, maxy = plan_bounds(sketch.plan)
        for depth in (near, far):
            for corner in ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)):
                self._expand_bounds(document, [map3d(plane, corner[0], corner[1], depth)])

        for loop_index, loop in enumerate(loops):
            area = loop_area(sketch.plan, loop)
            for depth, label in ((near, "start"), (far, "end")):
                center = _loop_center(sketch.plan, loop)
                document.topology.append(
                    _Topo(
                        id=self._next("face"),
                        kind="face",
                        description=f"{feature_name} {label} cap of profile {loop_index}",
                        feature=feature_name,
                        geometry="planar",
                        midpoint=map3d(plane, center[0], center[1], depth),
                        normal=tuple(component * (1 if label == "end" else -1) for component in normal),  # type: ignore[arg-type]
                        area=area,
                    )
                )
            corners = _corner_convexity(loop_points(sketch.plan, loop), inverted=cut)
            for primitive_id in loop:
                primitive = sketch.plan.by_id(primitive_id)
                self._synthesise_side(document, plane, primitive, near, far,
                                      feature_name, corners)

    def _synthesise_side(
        self,
        document: _Document,
        plane: str,
        primitive: Any,
        near: float,
        far: float,
        feature_name: str,
        corners: dict[tuple[float, float], str] | None = None,
    ) -> None:
        height = abs(far - near)
        if isinstance(primitive, PCircle):
            document.topology.append(
                _Topo(
                    id=self._next("face"),
                    kind="face",
                    description=f"{feature_name} cylindrical face",
                    feature=feature_name,
                    geometry="cylindrical",
                    midpoint=map3d(plane, primitive.center[0], primitive.center[1], (near + far) / 2),
                    area=2 * math.pi * primitive.radius * height,
                )
            )
            for depth, label in ((near, "start"), (far, "end")):
                document.topology.append(
                    _Topo(
                        id=self._next("edge"),
                        kind="edge",
                        description=f"{feature_name} circular edge ({label})",
                        feature=feature_name,
                        geometry="circular",
                        midpoint=map3d(plane, primitive.center[0], primitive.center[1], depth),
                        direction=None,
                        length=2 * math.pi * primitive.radius,
                    )
                )
            return

        if isinstance(primitive, PLine):
            mid_u = (primitive.start[0] + primitive.end[0]) / 2
            mid_v = (primitive.start[1] + primitive.end[1]) / 2
            document.topology.append(
                _Topo(
                    id=self._next("face"),
                    kind="face",
                    description=f"{feature_name} planar side face",
                    feature=feature_name,
                    geometry="planar",
                    midpoint=map3d(plane, mid_u, mid_v, (near + far) / 2),
                    normal=_side_normal(plane, primitive),
                    area=primitive.length * height,
                )
            )
            for depth, label in ((near, "start"), (far, "end")):
                document.topology.append(
                    _Topo(
                        id=self._next("edge"),
                        kind="edge",
                        description=f"{feature_name} straight edge ({label})",
                        feature=feature_name,
                        geometry="linear",
                        midpoint=map3d(plane, mid_u, mid_v, depth),
                        direction=_edge_direction(plane, primitive),
                        length=primitive.length,
                    )
                )
            # The edge running along the extrusion direction at the segment
            # start. This is the one edge the simulator can classify: it sits at
            # a corner of the profile, and the corner's turn decides it.
            document.topology.append(
                _Topo(
                    id=self._next("edge"),
                    kind="edge",
                    description=f"{feature_name} side edge",
                    feature=feature_name,
                    geometry="linear",
                    midpoint=map3d(plane, primitive.start[0], primitive.start[1], (near + far) / 2),
                    direction=plane_normal(plane),
                    length=height,
                    convexity=(corners or {}).get(_corner_key(primitive.start)),
                )
            )
            return

        if isinstance(primitive, PArc):
            mid_angle = (primitive.start_angle + primitive.end_angle) / 2
            mid_u = primitive.center[0] + primitive.radius * math.cos(mid_angle)
            mid_v = primitive.center[1] + primitive.radius * math.sin(mid_angle)
            sweep = abs(primitive.end_angle - primitive.start_angle)
            document.topology.append(
                _Topo(
                    id=self._next("face"),
                    kind="face",
                    description=f"{feature_name} cylindrical side face",
                    feature=feature_name,
                    geometry="cylindrical",
                    midpoint=map3d(plane, mid_u, mid_v, (near + far) / 2),
                    area=primitive.radius * sweep * height,
                )
            )
            for depth, label in ((near, "start"), (far, "end")):
                document.topology.append(
                    _Topo(
                        id=self._next("edge"),
                        kind="edge",
                        description=f"{feature_name} arc edge ({label})",
                        feature=feature_name,
                        geometry="circular",
                        midpoint=map3d(plane, mid_u, mid_v, depth),
                        length=primitive.radius * sweep,
                    )
                )

    def revolve(self, doc_id: str, request: RevolveRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        sketch = document.find_sketch(request.sketch)
        loops = _selected_loops(sketch, request.profiles)
        if not loops:
            raise FeatureError(f"Sketch {sketch.name!r} has no closed profile to revolve.")
        angle = request.angle.value if request.angle else 2 * math.pi
        # Pappus's theorem: the swept volume is the profile's area times the
        # distance its *centroid* travels. A cut is clipped to the body first --
        # a groove profile is drawn to overshoot so the cut certainly breaks
        # through, and charging the overshoot as removed material is the
        # difference between a pulley of 65.6 cm^3 and one of 68.0.
        window = (_revolve_window(document, sketch, request.axis)
                  if request.operation == "cut" else None)
        area, radius = _pappus(sketch, loops, request.axis, window)
        volume = area * radius * angle
        was = document.volume
        if request.operation == "cut":
            document.volume = max(document.volume - volume, 0.0)
        else:
            document.volume += volume
        moved = document.volume - was

        if request.operation != "cut":
            self._expand_for_revolve(document, sketch, request.axis)

        name = self._feature_name(document, request.name, "revolution")
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="revolve",
            volume_delta=moved,
            detail={
                "sketch": sketch.name,
                "axis": request.axis.value,
                "angle_deg": round(math.degrees(angle), 4),
                "operation": request.operation,
            },
        )
        document.features.append(feature)
        document.topology.append(
            _Topo(
                id=self._next("face"),
                kind="face",
                description=f"{name} revolved face",
                feature=name,
                geometry="cylindrical",
                midpoint=(0.0, 0.0, 0.0),
                area=2 * math.pi * radius * math.sqrt(max(area, 0.0)),
            )
        )
        document.modified = True
        self._record("revolve", name=name, sketch=sketch.name)
        return _feature_info(feature)

    def sweep(self, doc_id: str, request: SweepRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        profile = document.find_sketch(request.profile_sketch)
        path = document.find_sketch(request.path_sketch)
        area = _net_area(profile, profile.loops)
        length = _path_length(path.plan)
        was = document.volume
        document.volume += area * length if request.operation != "cut" else -area * length
        document.volume = max(document.volume, 0.0)
        moved = document.volume - was
        if request.operation != "cut":
            self._expand_for_sweep(document, profile, path)
        name = self._feature_name(document, request.name, "sweep")
        feature = _Feature(self._next("feat"), name, "sweep", volume_delta=moved,
                           detail={"profile": profile.name, "path": path.name})
        document.features.append(feature)
        return _feature_info(feature)

    def loft(self, doc_id: str, request: LoftRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        sketches = [document.find_sketch(name) for name in request.sketches]
        areas = [_net_area(sketch, sketch.loops) for sketch in sketches]
        # The mean area times the distance between the outermost sections. The
        # mean area on its own was being added as if it were a volume, so a
        # 70 mm duct came out at a quarter of its size and the units did not
        # even agree.
        offsets = [sketch.offset for sketch in sketches]
        span = abs(max(offsets) - min(offsets))
        was = document.volume
        document.volume += (sum(areas) / max(len(areas), 1)) * span
        moved = document.volume - was
        if span:
            self._expand_for_loft(document, sketches)
        name = self._feature_name(document, request.name, "loft")
        feature = _Feature(self._next("feat"), name, "loft", volume_delta=moved,
                           detail={"sections": [s.name for s in sketches]})
        document.features.append(feature)
        return _feature_info(feature)

    def hole(self, doc_id: str, request: HoleRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        sketch = document.find_sketch(request.sketch)
        centers = _hole_points(sketch, request.point_indices)
        if not centers:
            raise FeatureError(
                f"Sketch {sketch.name!r} contains no hole-centre points.",
                hint="Add `point`, `point_grid` or `bolt_circle` entities to the sketch.",
            )
        plane = sketch.base_plane
        radius = request.diameter.value / 2
        if request.depth:
            depth = request.depth.value
        else:
            first = centers[0]
            depth = _through_all_distance(
                document, plane, over=map3d(plane, first[0], first[1], sketch.offset))
        removed = (math.pi * radius**2 * depth + _style_volume(request, radius)) * len(centers)
        was = document.volume
        document.volume = max(document.volume - removed, 0.0)
        moved = document.volume - was

        name = self._feature_name(document, request.name, "hole")
        for index, (u, v) in enumerate(centers):
            document.topology.append(
                _Topo(
                    id=self._next("face"),
                    kind="face",
                    description=f"{name} bore {index}",
                    feature=name,
                    geometry="cylindrical",
                    midpoint=map3d(plane, u, v, -depth / 2),
                    area=2 * math.pi * radius * depth,
                )
            )
            document.topology.append(
                _Topo(
                    id=self._next("edge"),
                    kind="edge",
                    description=f"{name} bore rim {index}",
                    feature=name,
                    geometry="circular",
                    midpoint=map3d(plane, u, v, 0.0),
                    length=2 * math.pi * radius,
                )
            )

        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="hole",
            volume_delta=moved,
            detail={
                "count": len(centers),
                "diameter": request.diameter.as_dict(),
                "style": request.style,
                "through_all": request.through_all,
                "tap": request.tap,
                # The simulator has no thread table, so a tapped hole is sized
                # by the recipe's diameter. Inventor sizes it from the table,
                # which is why the recipe should give the tap-drill diameter.
                "tap_sized_by": "the recipe's diameter" if request.tap else None,
            },
        )
        document.features.append(feature)
        document.modified = True
        self._record("hole", name=name, count=len(centers))
        return _feature_info(feature)

    def fillet(self, doc_id: str, request: FilletRequest) -> FeatureInfo:
        return self._edge_treatment(doc_id, "fillet", request.edges, request.radius.value,
                                    request.name, {"radius": request.radius.as_dict()})

    def chamfer(self, doc_id: str, request: ChamferRequest) -> FeatureInfo:
        return self._edge_treatment(doc_id, "chamfer", request.edges, request.distance.value,
                                    request.name, {"distance": request.distance.as_dict()})

    def _edge_treatment(
        self,
        doc_id: str,
        kind: str,
        selector: ResolvedSelector,
        size: float,
        requested_name: str | None,
        detail: dict[str, Any],
    ) -> FeatureInfo:
        document = self._doc(doc_id)
        matches = self._match(document, selector)
        if not matches:
            raise SelectionError(
                f"The {kind} selector matched no edges.",
                hint="Run `select_topology` with the same selector to see what is available.",
                selector=selector.__dict__,
            )
        name = self._feature_name(document, requested_name, kind)
        total_length = sum(match.length or 0.0 for match in matches)
        # r^2 (1 - pi/4) per unit length: the corner left outside a quarter
        # circle inscribed in a square. On an outside corner that material goes
        # away; on an inside corner the same amount is added, and the simulator
        # subtracted either way -- which is why the angle bracket read 1.4 cm^3
        # light with a correct fillet in it.
        #
        # The sign comes from the selector rather than from the shape: the
        # simulator synthesises topology from sketch loops and genuinely cannot
        # see which side the material is on, so it takes the recipe's word. A
        # recipe that asks for "concave" and gets a convex edge is a mistake only
        # Inventor can catch.
        adds = selector.filter == "concave"
        corner = total_length * size * size * 0.2146
        was = document.volume
        document.volume = max(document.volume + (corner if adds else -corner), 0.0)
        for match in matches:
            match.consumed = True
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind=kind,
            volume_delta=document.volume - was,
            detail={**detail, "edges": len(matches), "edge_ids": [m.id for m in matches],
                    "volume_note": ("added, since the selector asks for a concave edge"
                                    if adds else "removed, as on an outside corner")},
        )
        document.features.append(feature)
        document.modified = True
        self._record(kind, name=name, edges=len(matches))
        return _feature_info(feature)

    def shell(self, doc_id: str, request: ShellRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        if document.volume <= 0:
            raise FeatureError("Nothing to shell: the part has no solid body yet.")
        openings = self._match(document, request.faces) if request.faces.ids or request.faces.filter != "all" else []
        removed, how = self._hollow_out(document, request, openings)
        document.volume -= removed
        name = self._feature_name(document, request.name, "shell")
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="shell",
            volume_delta=-removed,
            detail={
                "thickness": request.thickness.as_dict(),
                "direction": request.direction,
                "removed_faces": len(openings),
                "volume_from": how,
            },
        )
        document.features.append(feature)
        document.modified = True
        self._record("shell", name=name, removed_faces=len(openings))
        return _feature_info(feature)

    def _repeat(self, document: _Document, targets: Sequence[_Feature],
                extra: int) -> tuple[float, str]:
        """Apply *extra* more copies of what *targets* did to the volume.

        An occurrence does whatever its seed did, so a mirrored slot cut removes
        the same again. The simulator used to say "occurrence volume is not
        estimated" and leave the total unchanged, which made a mirrored cut look
        like a cut that had failed -- the angle bracket read 16 cm^3 heavy, and
        anything reasoning from a rehearsal inherited that.

        This is exact while occurrences do not overlap each other or run off the
        part, which is the normal case and the only one a pattern is usually
        asked for. It says so rather than implying more.
        """
        if extra <= 0:
            return 0.0, "no additional occurrences"
        unknown = [target.name for target in targets if target.volume_delta is None]
        if unknown:
            return 0.0, ("volume not modelled: nothing was recorded for "
                         + ", ".join(unknown))
        seed = sum(target.volume_delta or 0.0 for target in targets)
        was = document.volume
        document.volume = max(document.volume + seed * extra, 0.0)
        moved = document.volume - was
        return moved, (f"{extra} more occurrence(s) of {moved:+.4f} cm^3 in total, "
                       "assuming they do not overlap each other or the part's edge")

    def _hollow_out(self, document: _Document, request: ShellRequest,
                    openings: Sequence[_Topo]) -> tuple[float, str]:
        """How much a shell takes out, exactly where that is knowable.

        A shelled prism is the outline inset by the wall thickness, swept: the
        cavity's cross-section is an exact function of the outline, and the only
        question is how much of the sweep it spans -- one wall thickness less for
        each face left in place. That makes the commonest shell (a box with its
        top removed) predictable rather than estimated, which is what lets a live
        one be checked against it.

        Anything else falls back to the old estimate -- the surface area times the
        thickness -- and says so, because a shell of a revolved or swept body is
        not a prism and pretending otherwise would be worse than approximating.
        """
        thickness = request.thickness.value
        if len(document.slabs) == 1 and request.direction == "inside":
            slab = document.slabs[0]
            inner = inset_area(slab.outline, thickness)
            sweep = abs(slab.far - slab.near)
            if inner is not None and sweep > 0:
                # An opening perpendicular to the sweep is a cap: it leaves the
                # cavity running all the way to that end.
                normal = plane_normal(slab.plane)
                caps = sum(
                    1 for face in openings
                    if face.normal is not None
                    and abs(sum(a * b for a, b in zip(face.normal, normal))) > 0.9
                )
                depth = sweep - thickness * max(2 - caps, 0)
                if depth > 0:
                    return (min(inner * depth, document.volume),
                            "the outline inset by the wall thickness, swept")
        surface = sum(match.area or 0.0 for match in document.topology if match.kind == "face")
        return (max(document.volume - surface * thickness, 0.0),
                "estimated from the surface area: this body is not a single prism")

    def rectangular_pattern(self, doc_id: str, request: RectangularPatternRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        targets = _pattern_targets(document, request.features)
        occurrences = request.count1 * request.count2
        name = self._feature_name(document, request.name, "pattern")
        moved, why = self._repeat(document, targets, occurrences - 1)
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="rectangular_pattern",
            volume_delta=moved,
            detail={
                "features": [t.name for t in targets],
                "count1": request.count1,
                "count2": request.count2,
                "occurrences": occurrences,
                "volume_note": why,
            },
        )
        document.features.append(feature)
        self._record("rectangular_pattern", name=name, occurrences=occurrences)
        return _feature_info(feature)

    def circular_pattern(self, doc_id: str, request: CircularPatternRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        targets = _pattern_targets(document, request.features)
        name = self._feature_name(document, request.name, "pattern")
        moved, why = self._repeat(document, targets, request.count - 1)
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="circular_pattern",
            volume_delta=moved,
            detail={
                "features": [t.name for t in targets],
                "count": request.count,
                "angle_deg": round(math.degrees(request.angle.value), 4),
                "volume_note": why,
            },
        )
        document.features.append(feature)
        self._record("circular_pattern", name=name, count=request.count)
        return _feature_info(feature)

    def mirror(self, doc_id: str, request: MirrorRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        targets = _pattern_targets(document, request.features)
        name = self._feature_name(document, request.name, "mirror")
        moved, why = self._repeat(document, targets, 1)
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="mirror",
            volume_delta=moved,
            detail={"features": [t.name for t in targets], "plane": request.plane,
                    "volume_note": why},
        )
        document.features.append(feature)
        self._record("mirror", name=name)
        return _feature_info(feature)

    def _expand_for_sweep(self, document: _Document, profile: Any, path: Any) -> None:
        """Grow the bounds to cover the path, widened by the profile's reach."""
        plane = path.base_plane
        try:
            low_u, low_v, high_u, high_v = plan_bounds(path.plan)
            p_low_u, p_low_v, p_high_u, p_high_v = plan_bounds(profile.plan)
        except Exception:  # pragma: no cover - an empty sketch cannot sweep
            return
        # Half the profile's own size, not its distance from the origin: a tube
        # profile drawn out at radius 45 is 20 mm across, and treating 55 as its
        # reach inflated the elbow's bounding box to 200 mm square.
        reach = max(p_high_u - p_low_u, p_high_v - p_low_v) / 2
        for u in (low_u - reach, high_u + reach):
            for v in (low_v - reach, high_v + reach):
                for w in (-reach, reach):
                    self._expand_bounds(document, [map3d(plane, u, v, w)])

    def _expand_for_loft(self, document: _Document, sketches: Sequence[Any]) -> None:
        """Grow the bounds to cover every section, at its own offset."""
        for sketch in sketches:
            try:
                low_u, low_v, high_u, high_v = plan_bounds(sketch.plan)
            except Exception:  # pragma: no cover
                continue
            for u in (low_u, high_u):
                for v in (low_v, high_v):
                    self._expand_bounds(
                        document, [map3d(sketch.base_plane, u, v, sketch.offset)])

    def _expand_for_revolve(self, document: _Document, sketch: Any, axis: Any) -> None:
        """Grow the bounds by the ring a revolve actually sweeps.

        Not by a cube: revolving an annular section 16 mm wide about Z reaches
        80 mm across and 16 mm high, and calling it 80 cubed made a pulley look
        like a ball. The bounding box feeds the rehearsal's "does this cut reach
        the part" check, so an over-estimate there is a missed warning.
        """
        plane = sketch.base_plane
        try:
            low_u, low_v, high_u, high_v = plan_bounds(sketch.plan)
        except Exception:  # pragma: no cover - an empty sketch cannot revolve
            return
        turning = {"x": 0, "y": 1, "z": 2}.get(getattr(axis, "value", axis))
        corners = [map3d(plane, u, v, 0.0)
                   for u in (low_u, high_u) for v in (low_v, high_v)]
        if turning is None:  # a sketch line or an edge: no cheap answer
            reach = max(abs(value) for corner in corners for value in corner)
            self._expand_bounds(document, [
                (x, y, z) for x in (-reach, reach)
                for y in (-reach, reach) for z in (-reach, reach)])
            return

        others = [index for index in range(3) if index != turning]
        along = [corner[turning] for corner in corners]
        radius = max(
            math.dist([corner[index] for index in others], [0.0, 0.0])
            for corner in corners
        )
        for low in (min(along), max(along)):
            for first in (-radius, radius):
                for second in (-radius, radius):
                    point = [0.0, 0.0, 0.0]
                    point[turning] = low
                    point[others[0]], point[others[1]] = first, second
                    self._expand_bounds(document, [tuple(point)])

    def work_plane(self, doc_id: str, request: WorkPlaneRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        base = request.base.split(":")[0]
        if base not in _PLANES and request.base not in document.work_planes:
            raise FeatureError(f"Unknown base plane {request.base!r}.")
        name = self._feature_name(document, request.name, "workplane")
        offset = request.offset.value if request.offset else 0.0
        document.work_planes[name] = (base if base in _PLANES else document.work_planes[request.base][0], offset)
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="work_plane",
            detail={"kind": request.kind, "base": request.base,
                    "offset": request.offset.as_dict() if request.offset else None},
        )
        document.features.append(feature)
        self._record("work_plane", name=name)
        return _feature_info(feature)

    def thread(self, doc_id: str, request: ThreadRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        matches = self._match(document, request.faces)
        if not matches:
            raise SelectionError("The thread selector matched no cylindrical faces.")
        name = self._feature_name(document, request.name, "thread")
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="thread",
            detail={"designation": request.designation, "faces": len(matches),
                    "internal": request.internal},
        )
        document.features.append(feature)
        return _feature_info(feature)

    # -- model state -------------------------------------------------------
    def list_features(self, doc_id: str) -> list[FeatureInfo]:
        return [_feature_info(feature) for feature in self._doc(doc_id).features]

    def suppress_feature(self, doc_id: str, name: str, suppressed: bool) -> FeatureInfo:
        feature = self._doc(doc_id).find_feature(name)
        feature.suppressed = suppressed
        return _feature_info(feature)

    def delete_feature(self, doc_id: str, name: str) -> None:
        document = self._doc(doc_id)
        feature = document.find_feature(name)
        document.features.remove(feature)
        document.topology = [topo for topo in document.topology if topo.feature != feature.name]

    def rename_feature(self, doc_id: str, name: str, new_name: str) -> FeatureInfo:
        document = self._doc(doc_id)
        feature = document.find_feature(name)
        if any(other.name == new_name for other in document.features):
            raise FeatureError(f"A feature named {new_name!r} already exists.")
        for topo in document.topology:
            if topo.feature == feature.name:
                topo.feature = new_name
        feature.name = new_name
        return _feature_info(feature)

    def select(self, doc_id: str, selector: ResolvedSelector) -> list[TopoInfo]:
        document = self._doc(doc_id)
        return [match.to_info() for match in self._match(document, selector)]

    def _match(self, document: _Document, selector: ResolvedSelector) -> list[_Topo]:
        candidates = [
            topo
            for topo in document.topology
            if topo.kind == selector.kind and not (topo.consumed and selector.kind == "edge")
        ]
        if selector.ids:
            wanted = set(selector.ids)
            found = [topo for topo in candidates if topo.id in wanted]
            missing = wanted - {topo.id for topo in found}
            if missing:
                raise SelectionError(
                    f"Unknown or already-consumed topology handles: {sorted(missing)}.",
                    hint="Handles change whenever the model rebuilds; re-run `select_topology`.",
                )
            return found

        if selector.feature:
            candidates = [topo for topo in candidates if topo.feature == selector.feature]

        candidates = [topo for topo in candidates if _passes_filter(topo, selector.filter, document)]

        if selector.min_length is not None:
            candidates = [t for t in candidates if (t.length or t.area or 0.0) >= selector.min_length]
        if selector.max_length is not None:
            candidates = [t for t in candidates if (t.length or t.area or 0.0) <= selector.max_length]

        if selector.near is not None:
            near = selector.near
            candidates.sort(key=lambda t: math.dist(t.midpoint, near))
            if selector.within is not None:
                candidates = [t for t in candidates if math.dist(t.midpoint, near) <= selector.within]
        elif selector.filter == "largest":
            candidates.sort(key=lambda t: -(t.area or t.length or 0.0))
        elif selector.filter == "smallest":
            candidates.sort(key=lambda t: (t.area or t.length or 0.0))

        if selector.limit is not None:
            candidates = candidates[: selector.limit]
        return candidates

    def mass_properties(self, doc_id: str) -> MassProps:
        document = self._doc(doc_id)
        area = sum(topo.area or 0.0 for topo in document.topology if topo.kind == "face")
        density = _DENSITY.get((document.material or "").lower())
        bounds = tuple(document.bounds) if document.bounds else None
        return MassProps(
            volume=document.volume,
            area=area,
            mass=document.volume * density if density else None,
            density=density,
            material=document.material,
            center_of_mass=_bounds_center(document.bounds),
            bounding_box=bounds,  # type: ignore[arg-type]
        )

    def rebuild(self, doc_id: str) -> dict[str, Any]:
        document = self._doc(doc_id)
        return {
            "rebuilt": True,
            "simulated": True,
            "features": len(document.features),
            "sketches": len(document.sketches),
            "errors": [],
            "note": "The simulator records the new parameter values but does not re-solve "
                    "geometry, so sizes reported after a parameter change are stale. "
                    "Connect to Inventor for a real rebuild.",
        }

    # -- undo --------------------------------------------------------------
    def begin_transaction(self, doc_id: str, name: str) -> str | None:
        """Copy the document aside, so an abort can put it back.

        A real rollback rather than a stub: Inventor's transactions are one of
        the few things the simulator can model exactly, which is what lets the
        rollback path be tested at all.
        """
        document = self._doc(doc_id)
        handle = self._next("txn")
        self._transactions[handle] = (document.id, copy.deepcopy(document))
        self._record("begin_transaction", handle=handle, name=name)
        return handle

    def commit_transaction(self, handle: str) -> None:
        self._transactions.pop(handle, None)
        self._record("commit_transaction", handle=handle)

    def abort_transaction(self, handle: str) -> bool:
        entry = self._transactions.pop(handle, None)
        self._record("abort_transaction", handle=handle)
        if entry is None:
            return False
        doc_id, snapshot = entry
        self._documents[doc_id] = snapshot
        return True

    def describe_feature(self, doc_id: str, name: str) -> dict[str, Any]:
        """The simulator's own record of a feature, in the same shape.

        It has no COM properties to read, so what comes back is what it stored --
        enough for a caller to exercise the path without Inventor.
        """
        feature = self._doc(doc_id).find_feature(name)
        described: dict[str, Any] = {
            "name": feature.name,
            "kind": feature.kind,
            "simulated": True,
        }
        if feature.volume_delta is not None:
            described["volume_change_cm3"] = round(feature.volume_delta, 6)
        for key, value in (feature.detail or {}).items():
            if isinstance(value, (bool, int, float, str)) or value is None:
                described[key] = value
        return described

    # -- escape hatch ------------------------------------------------------
    def run_script(self, doc_id: str | None, code: str) -> dict[str, Any]:
        """Run *code* against the simulator's own model.

        Implemented so the tool layer around the escape hatch can be tested at
        all, and because a script that only reads is often answerable here. What
        it exposes is the simulator's dataclasses, not Inventor's API, so a
        script written against `application` fails rather than misleads.
        """
        import io
        from contextlib import redirect_stdout

        document = self._doc(doc_id) if doc_id else None
        scope: dict[str, Any] = {
            "document": document,
            "component": document,
            "backend": self,
            "application": None,
            "app": None,
            "result": None,
        }
        printed = io.StringIO()
        with redirect_stdout(printed):
            exec(code, scope)  # noqa: S102 - the whole point of this method
        report: dict[str, Any] = {
            "ran": True,
            "simulated": True,
            "printed": printed.getvalue(),
            "note": "this ran against the simulator's own model, not Inventor's API",
        }
        if scope.get("result") is not None:
            report["result"] = _describe_value(scope["result"])
        if document is not None:
            report["volume_cm3"] = round(document.volume, 6)
        return report

    # -- output ------------------------------------------------------------
    def export(self, doc_id: str, request: ExportRequest) -> dict[str, Any]:
        document = self._doc(doc_id)
        self._record("export", path=request.path, format=request.format)
        return {
            "written": False,
            "simulated": True,
            "path": request.path,
            "format": request.format,
            "note": "The mock backend does not write CAD files; connect to Inventor to export.",
            "document": document.name,
        }

    def screenshot(self, doc_id: str, request: ScreenshotRequest) -> dict[str, Any]:
        self._record("screenshot", path=request.path)
        return {
            "written": False,
            "simulated": True,
            "path": request.path,
            "note": "The mock backend cannot render; connect to Inventor for images.",
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit_for(dim: Dim, fallback: str) -> str:
    return {Dim.LENGTH: fallback, Dim.ANGLE: "deg", Dim.UNITLESS: "ul", Dim.MASS: "kg"}.get(dim, "ul")


def _feature_info(feature: _Feature) -> FeatureInfo:
    return FeatureInfo(
        id=feature.id,
        name=feature.name,
        kind=feature.kind,
        suppressed=feature.suppressed,
        detail=feature.detail,
    )


def _selected_loops(sketch: _Sketch, profiles: Sequence[int] | str) -> list[list[str]]:
    if profiles == "all":
        return sketch.loops
    if profiles == "outer":
        if not sketch.loops:
            return []
        return [max(sketch.loops, key=lambda loop: loop_area(sketch.plan, loop))]
    indices = list(profiles)  # type: ignore[arg-type]
    try:
        return [sketch.loops[index] for index in indices]
    except IndexError as exc:
        raise FeatureError(
            f"Sketch {sketch.name!r} has {len(sketch.loops)} profile(s); "
            f"index {indices} is out of range."
        ) from exc


def _net_area(sketch: _Sketch, loops: Sequence[Sequence[str]]) -> float:
    """The material a profile encloses: outer loops less the holes inside them.

    Nesting is decided by containment, not by size. Taking the largest loop as
    the outer boundary and every other loop as a hole in it is right for a plate
    with holes and wrong for the case that reads identically: four separate
    bosses in one sketch came out as one boss with three holes punched in it,
    which is an area of zero, which is a feature that silently built nothing.
    """
    if not loops:
        return 0.0
    outlines = [loop_points(sketch.plan, loop) for loop in loops]
    total = 0.0
    for index, loop in enumerate(loops):
        area = loop_area(sketch.plan, loop)
        if not area:
            continue
        depth = sum(
            1 for other, outline in enumerate(outlines)
            if other != index and _encloses(outline, outlines[index])
        )
        # Even depth is material, odd depth is a hole in it -- the even-odd rule,
        # which handles a boss inside a pocket inside a plate as well as it
        # handles a plate with holes.
        total += -area if depth % 2 else area
    return max(total, 0.0)


def _encloses(outer: Sequence[tuple[float, float]],
              inner: Sequence[tuple[float, float]]) -> bool:
    """Whether *inner* lies within *outer*, by where its vertices fall.

    The vertices rather than a single interior point, because a ring's outer
    boundary encloses the centre of its own hole: asking whether the outer
    circle's centroid is inside the inner circle says yes, and a washer then
    counts both of its loops as holes and comes out with no area at all.
    """
    if len(outer) < 3 or len(inner) < 3:
        return False
    within = sum(1 for u, v in inner if _inside(outer, u, v))
    return within * 2 > len(inner)


def _loop_center(plan: SketchPlan, loop: Sequence[str]) -> tuple[float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for primitive_id in loop:
        primitive = plan.by_id(primitive_id)
        if isinstance(primitive, PLine):
            xs.extend([primitive.start[0], primitive.end[0]])
            ys.extend([primitive.start[1], primitive.end[1]])
        elif isinstance(primitive, (PCircle, PArc, PEllipse)):
            xs.append(primitive.center[0])
            ys.append(primitive.center[1])
    if not xs:
        return (0.0, 0.0)
    return (sum(xs) / len(xs), sum(ys) / len(ys))


#: How far a corner has to turn before it counts as one, in radians. Below this
#: the polygon is following a curve or crossing a tangent junction, where there
#: is no edge to classify -- a slot's straight-to-arc join is smooth.
_CORNER = math.radians(1.0)


def _corner_key(point: Sequence[float]) -> tuple[float, float]:
    """A position rounded enough to match the same vertex twice."""
    return (round(point[0], 7), round(point[1], 7))


def _corner_convexity(outline: Sequence[tuple[float, float]],
                      *, inverted: bool = False) -> dict[tuple[float, float], str]:
    """Which corners of a profile turn out and which turn in.

    An extruded profile's corner becomes an edge running along the extrusion,
    and that edge is convex or concave exactly as the corner turns: in a
    counter-clockwise loop a left turn is an outside corner and a reflex vertex
    is an inside one. This is the one convexity question the simulator can
    answer from what it has, rather than declining as it does for everything
    else -- and it is the question that put a fillet on the wrong edge twice.

    *inverted* for a cut: a pocket's corners are the opposite way round from a
    boss's, since the material is outside the profile rather than inside.
    """
    count = len(outline)
    if count < 3:
        return {}
    signed = 0.0
    for current, following in zip(outline, list(outline[1:]) + [outline[0]]):
        signed += current[0] * following[1] - following[0] * current[1]
    if signed == 0:  # pragma: no cover - a degenerate loop has no corners
        return {}
    anticlockwise = signed > 0

    corners: dict[tuple[float, float], str] = {}
    for index in range(count):
        previous = outline[index - 1]
        here = outline[index]
        following = outline[(index + 1) % count]
        incoming = (here[0] - previous[0], here[1] - previous[1])
        outgoing = (following[0] - here[0], following[1] - here[1])
        first = math.hypot(*incoming)
        second = math.hypot(*outgoing)
        if first == 0 or second == 0:
            continue
        cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
        dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
        if abs(math.atan2(cross, dot)) < _CORNER:
            continue  # a tangent join or a sampled curve: no edge here
        turns_left = cross > 0
        convex = turns_left == anticlockwise
        if inverted:
            convex = not convex
        corners[_corner_key(here)] = "convex" if convex else "concave"
    return corners


def _side_normal(plane: str, line: PLine) -> tuple[float, float, float]:
    dx = line.end[0] - line.start[0]
    dy = line.end[1] - line.start[1]
    length = math.hypot(dx, dy) or 1.0
    # Outward normal of a counter-clockwise loop.
    return map3d(plane, dy / length, -dx / length, 0.0)


def _edge_direction(plane: str, line: PLine) -> tuple[float, float, float]:
    dx = line.end[0] - line.start[0]
    dy = line.end[1] - line.start[1]
    length = math.hypot(dx, dy) or 1.0
    return map3d(plane, dx / length, dy / length, 0.0)


def _describe_value(value: Any) -> Any:
    """A script's return value in something JSON can carry."""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_describe_value(item) for item in value[:50]]
    if isinstance(value, dict):
        return {str(key): _describe_value(item) for key, item in list(value.items())[:50]}
    described: dict[str, Any] = {"type": type(value).__name__}
    for name in ("name", "kind", "volume", "id", "expression", "value"):
        attribute = getattr(value, name, None)
        if isinstance(attribute, (str, int, float, bool)):
            described[name] = attribute
    return described


def _style_volume(request: HoleRequest, radius: float) -> float:
    """What one hole's *style* removes on top of the plain bore, in cm^3.

    The bore is counted over the full depth elsewhere, so only the material
    outside it counts here -- a counterbore of 10 mm over a 5.5 mm hole takes
    away an annulus, not a whole cylinder. Counting the whole cylinder is what
    this used to do, and it made every counterbored plate lighter than it is.
    """
    if request.style in ("counterbore", "spotface"):
        if not (request.cbore_diameter and request.cbore_depth):
            return 0.0
        outer = request.cbore_diameter.value / 2
        return math.pi * max(outer**2 - radius**2, 0.0) * request.cbore_depth.value
    if request.style == "countersink":
        if not request.csink_diameter:
            return 0.0
        outer = request.csink_diameter.value / 2
        if outer <= radius:
            return 0.0
        # The angle Inventor takes is the full included angle, so the cone's
        # half-angle is what relates the radii to the depth.
        half = (request.csink_angle.value if request.csink_angle else math.pi / 2) / 2
        slope = math.tan(half)
        if slope <= 0:
            return 0.0
        height = (outer - radius) / slope
        frustum = math.pi * height / 3 * (outer**2 + outer * radius + radius**2)
        return max(frustum - math.pi * radius**2 * height, 0.0)
    return 0.0


def _inside(outline: Sequence[tuple[float, float]], u: float, v: float) -> bool:
    """Whether (u, v) is inside the closed polygon *outline*, by ray crossing."""
    inside = False
    count = len(outline)
    for index in range(count):
        (u1, v1), (u2, v2) = outline[index], outline[(index + 1) % count]
        if (v1 > v) != (v2 > v):
            span = v2 - v1
            if span == 0:  # pragma: no cover - guarded by the test above
                continue
            crossing = u1 + (v - v1) / span * (u2 - u1)
            if u < crossing:
                inside = not inside
    return inside


def _scan(outline: Sequence[tuple[float, float]], at: float,
          *, across: bool) -> tuple[float, float] | None:
    """Where the polygon reaches, along one sketch axis, at a fixed other one.

    ``across=False`` scans the v extent at ``u = at`` -- how tall the profile is
    at that horizontal position, which for an L-section at the far end of the
    base is the thickness of the base. ``across=True`` is the same question the
    other way round.
    """
    hits: list[float] = []
    count = len(outline)
    for index in range(count):
        first, second = outline[index], outline[(index + 1) % count]
        fixed1, fixed2 = (first[1], second[1]) if across else (first[0], second[0])
        free1, free2 = (first[0], second[0]) if across else (first[1], second[1])
        if (fixed1 > at) == (fixed2 > at):
            continue
        span = fixed2 - fixed1
        if span == 0:  # pragma: no cover - guarded above
            continue
        hits.append(free1 + (at - fixed1) / span * (free2 - free1))
    if len(hits) < 2:
        return None
    return min(hits), max(hits)


def _through_all_distance(document: _Document, plane: str = "xy",
                         over: tuple[float, float, float] | None = None) -> float:
    """How much *material* a through-feature passes through, along the normal.

    The body's whole span along that axis is the wrong answer for anything but a
    plate. The angle bracket is 90 mm tall and its base 6 mm thick, so slots cut
    down through the base were charged 90 mm of material and the simulator
    reported the part 36 cm^3 light -- a fifteen-fold error inside a number that
    looked plausible, and a mirror of that cut doubled it.

    Given *over* -- a model-space point the feature passes through -- the answer
    is measured off the prisms that actually built the part: see
    :func:`_material_interval`. Without it, or when no prism covers that point,
    the span stands as before. The span is an over-estimate rather than an
    under-estimate, which is the right way round for a cut: too much material
    removed shows up as a volume that is obviously wrong, where too little looks
    like a cut that worked.
    """
    if not document.bounds:
        return 1.0
    normal = plane_normal(plane)
    axis = max(range(3), key=lambda index: abs(normal[index]))
    spans = [document.bounds[index + 3] - document.bounds[index] for index in range(3)]
    if over is not None:
        interval = _material_interval(document, axis, over)
        if interval is not None:
            low, high = interval
            if high - low > 0:
                return high - low
    if spans[axis] > 0:
        return spans[axis]
    return max(spans) or 1.0


def _material_interval(document: _Document, axis: int,
                       through: tuple[float, float, float]) -> tuple[float, float] | None:
    """The extent of material along *axis* over the point *through*, or None.

    Built from the prisms the part is made of: every joined extrude is a 2D
    profile swept along its plane's normal, so asking "how thick is the part
    here" is a question about those profiles rather than about a bounding box.
    An L-section's bounding box is 90 mm deep where the L itself is 6, which is
    exactly the difference that mattered.

    Returns None when no prism covers the point -- a revolve, a sweep or a loft
    is not recorded this way, and inventing an answer for one would be worse
    than falling back to the span.

    Cuts are not subtracted from the prisms, so two through-cuts in the same
    place are each charged the full thickness. That over-removes, which is the
    safer direction: a volume that is obviously too small gets looked at, where
    a cut credited with removing nothing looks like a cut that worked.
    """
    intervals: list[tuple[float, float]] = []
    for slab in document.slabs:
        interval = slab.interval_along(axis, through)
        if interval is not None:
            intervals.append(interval)
    if not intervals:
        return None
    return min(low for low, _ in intervals), max(high for _, high in intervals)


def _hole_points(sketch: _Sketch, indices: Sequence[int]) -> list[tuple[float, float]]:
    plan = sketch.plan
    if indices:
        try:
            wanted = [plan.hole_centers[index] for index in indices]
        except IndexError as exc:
            raise FeatureError(
                f"Sketch {sketch.name!r} has {len(plan.hole_centers)} hole centre(s); "
                f"index {max(indices)} is out of range."
            ) from exc
    else:
        wanted = list(plan.hole_centers)
    points: list[tuple[float, float]] = []
    for primitive_id in wanted:
        primitive = plan.by_id(primitive_id)
        if isinstance(primitive, PPoint):
            points.append(primitive.position)
    return points


def _radial_axis(sketch: _Sketch, axis: AxisSpec) -> int | None:
    """Which of the sketch's own axes measures radius, 0 for u and 1 for v.

    None when the answer is not clear: a revolve about the sketch plane's own
    normal is degenerate, and an axis given as a sketch line or an edge is not
    resolved here.
    """
    if axis.kind != "work_axis" or axis.value not in ("x", "y", "z"):
        return None
    wanted = "xyz".index(axis.value)
    (u_axis, _), (v_axis, _), _ = _PLANES[sketch.base_plane][0]
    if wanted == v_axis:
        return 0
    if wanted == u_axis:
        return 1
    return None


def _revolve_window(document: _Document, sketch: _Sketch,
                    axis: AxisSpec) -> tuple[float, float, float, float] | None:
    """The part of the sketch plane the body occupies, in sketch coordinates.

    A cut outside this removes nothing, so clipping to it is what makes a
    revolved cut's volume the material it actually takes away.
    """
    radial = _radial_axis(sketch, axis)
    if radial is None or not document.bounds:
        return None
    along = "xyz".index(axis.value)
    reach = max(
        max(abs(document.bounds[index]), abs(document.bounds[index + 3]))
        for index in range(3) if index != along
    )
    low, high = document.bounds[along], document.bounds[along + 3]
    if radial == 0:
        return (-reach, reach, low, high)
    return (low, high, -reach, reach)


def _pappus(sketch: _Sketch, loops: Sequence[Sequence[str]], axis: AxisSpec,
            window: tuple[float, float, float, float] | None
            ) -> tuple[float, float]:
    """The profile's area and its centroid's distance from the axis.

    The centroid is the area centroid, not the bounding box's centre. Pappus
    needs the real one: a triangular groove profile has its centroid a third of
    the way from base to apex, and the box centre put the pulley's groove 2.6%
    out in a direction nothing would have questioned.
    """
    radial = _radial_axis(sketch, axis)
    total_area = 0.0
    moment = 0.0
    for index, loop in enumerate(loops):
        points = loop_points(sketch.plan, loop)
        if window is not None:
            points = clip_to_box(points, *window)
        centroid = polygon_centroid(points)
        if centroid is None:
            continue
        area = abs(sum(
            points[i][0] * points[(i + 1) % len(points)][1]
            - points[(i + 1) % len(points)][0] * points[i][1]
            for i in range(len(points))
        )) / 2
        # An inner loop is a hole in the profile, so it takes area away and
        # takes its own moment with it.
        sign = 1.0 if index == 0 or len(loops) == 1 else -1.0
        distance = abs(centroid[radial]) if radial is not None else abs(centroid[0])
        total_area += sign * area
        moment += sign * area * distance
    if total_area <= 0:
        return (0.0, 0.0)
    return (total_area, moment / total_area)


def _pattern_targets(document: _Document, names: Sequence[str]) -> list[_Feature]:
    if names:
        return [document.find_feature(name) for name in names]
    if not document.features:
        raise FeatureError("There is no feature to pattern yet.")
    return [document.features[-1]]


#: Patterns and mirrors copy geometry the simulator does not track well enough to
#: re-integrate, so their volume contribution is reported as unmodelled rather
#: than guessed at.
_VOLUME_NOT_MODELLED = "occurrence volume is not estimated by the simulator"


def _bounds_center(bounds: list[float] | None) -> tuple[float, float, float] | None:
    if not bounds:
        return None
    return (
        (bounds[0] + bounds[3]) / 2,
        (bounds[1] + bounds[4]) / 2,
        (bounds[2] + bounds[5]) / 2,
    )


def _path_length(plan: Any) -> float:
    """How far a sweep travels, in cm.

    Lines only was the previous answer, so an arc path contributed nothing and
    a swept elbow came out with no volume at all.
    """
    total = 0.0
    for primitive in plan.primitives:
        if primitive.construction:
            continue
        if isinstance(primitive, PLine):
            total += primitive.length
        elif isinstance(primitive, PArc):
            sweep = abs(primitive.end_angle - primitive.start_angle) % (2 * math.pi)
            total += primitive.radius * (sweep or 2 * math.pi)
        elif isinstance(primitive, PCircle):
            total += 2 * math.pi * primitive.radius
    return total


def _sketch_info(sketch: Any) -> SketchInfo:
    """One account of a sketch, so building and listing cannot disagree.

    The simulator does no solving, so it can never refuse a dimension -- but
    *which parameters reach a dimension* is a property of the plan, and that is
    the half that says whether the sketch is parametric at all.
    """
    plan = sketch.plan
    free = _degrees_of_freedom(plan)
    return SketchInfo(
        id=sketch.id,
        name=sketch.name,
        plane=plan.plane,
        entities=len(plan.primitives),
        constraints=len(plan.constraints),
        dimensions=len(plan.dimensions),
        profiles=len(sketch.loops),
        hole_centers=len(plan.hole_centers),
        fully_constrained=free <= 0,
        degrees_of_freedom=max(free, 0),
        driving_dimensions=len(plan.dimensions),
        driven_parameters=sorted(
            {name
             for dimension in plan.dimensions
             for name in referenced_parameters(dimension.expression)}
        ),
        undriven_expressions=list(plan.undriven_expressions),
    )


def _passes_filter(topo: _Topo, filter_name: str, document: _Document) -> bool:
    if filter_name in ("convex", "concave"):
        # Known only where a profile corner decides it -- an edge running along
        # an extrusion. Everything else is None and matches neither, which is
        # what the live backend does too: "matched no edges" can mean "could not
        # tell", and that is worth saying rather than quietly matching all of
        # them. It used to accept either, so a recipe asking for the one concave
        # edge on an angle bracket got whichever edge happened to be first.
        return topo.convexity == filter_name
    if filter_name in ("all", "outer"):
        return True
    if filter_name in ("circular", "linear", "planar", "cylindrical"):
        return topo.geometry == filter_name
    if filter_name in ("largest", "smallest"):
        return True

    axis_map = {
        "top": (2, 1),
        "bottom": (2, -1),
        "front": (1, -1),
        "back": (1, 1),
        "right": (0, 1),
        "left": (0, -1),
    }
    if filter_name in axis_map:
        axis, sign = axis_map[filter_name]
        if topo.kind == "face":
            if topo.normal is None:
                return False
            return topo.normal[axis] * sign > 0.9
        if topo.midpoint is None or document.bounds is None:
            return False
        target = document.bounds[axis + 3] if sign > 0 else document.bounds[axis]
        return abs(topo.midpoint[axis] - target) < 1e-6

    if filter_name == "vertical":
        if topo.kind == "edge":
            return topo.direction is not None and abs(topo.direction[2]) > 0.9
        return topo.normal is not None and abs(topo.normal[2]) < 0.1
    if filter_name == "horizontal":
        if topo.kind == "edge":
            return topo.direction is not None and abs(topo.direction[2]) < 0.1
        return topo.normal is not None and abs(topo.normal[2]) > 0.9
    return True


def _degrees_of_freedom(plan: SketchPlan) -> int:
    """A cheap DOF estimate so a sketch can be flagged as under-constrained.

    Each primitive contributes its free parameters, each constraint removes
    its usual count, and each dimension removes one.  It is an estimate, not a
    solver -- Inventor is the authority once connected.
    """
    dof = 0
    for primitive in plan.primitives:
        if isinstance(primitive, PLine):
            dof += 4
        elif isinstance(primitive, PCircle):
            dof += 3
        elif isinstance(primitive, PArc):
            dof += 5
        elif isinstance(primitive, PEllipse):
            dof += 5
        elif isinstance(primitive, PPoint):
            dof += 2

    removals = {
        "coincident": 2,
        "midpoint": 2,
        "concentric": 2,
        "ground": 0,
        "symmetric": 2,
    }
    for constraint in plan.constraints:
        if constraint.kind == "ground":
            dof = 0
            continue
        dof -= removals.get(constraint.kind, 1)
    dof -= len(plan.dimensions)
    return dof
