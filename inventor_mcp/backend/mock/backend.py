"""An in-memory Inventor stand-in.

The mock backend exists for two reasons.  It is what the test suite runs
against, and it lets a recipe be written, validated and dry-run on a machine
where Inventor is not installed -- so a model can be checked for
"does this sketch close?", "how many vertical edges will the fillet catch?",
"is the wall thickness larger than the plate?" before anyone opens Inventor.

It is deliberately *not* a geometry kernel.  Bodies are tracked as a bounding
box plus a volume estimate, and topology is synthesised from the sketch loops
that produced it.  That is enough to exercise selectors and catch the mistakes
that matter, and the reported numbers are labelled as estimates everywhere
they surface.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Iterable, Sequence

from ...errors import DocumentError, FeatureError, ParameterError, SelectionError, SketchError
from ...expressions import UnitContext, evaluate
from ...geometry import loop_area, plan_bounds, profile_loops
from ...expressions import referenced_parameters
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
        )


@dataclass
class _Sketch:
    id: str
    name: str
    plan: SketchPlan
    loops: list[list[str]]
    consumed_by: str | None = None


@dataclass
class _Feature:
    id: str
    name: str
    kind: str
    suppressed: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Document:
    id: str
    name: str
    units: str = "mm"
    angle_units: str = "deg"
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
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        info = self.new_part(name)
        document = self._doc(info.id)
        document.path = path
        self._record("open_document", path=path)
        return self._doc_info(document)

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
        self._record("set_parameter", name=name, expression=expression)
        return info

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
        sketch = _Sketch(id=self._next("sk"), name=name, plan=plan, loops=loops)
        document.sketches.append(sketch)
        document.modified = True
        self._record("build_sketch", name=name, plane=plan.plane, **plan.summary())

        free = _degrees_of_freedom(plan)
        return _sketch_info(sketch)

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

        plane = sketch.plan.plane if sketch.plan.plane in _PLANES else "xy"
        if request.extent == "distance":
            assert request.distance is not None
            distance = request.distance.value
        else:
            distance = _through_all_distance(document, plane)

        area = _net_area(sketch, loops)
        name = self._feature_name(document, request.name, "extrusion")
        signed = area * distance
        if request.operation == "cut":
            document.volume = max(document.volume - signed, 0.0)
        else:
            document.volume += signed

        self._synthesise_extrude_topology(document, sketch, loops, plane, distance, request.direction, name)

        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="extrude",
            detail={
                "sketch": sketch.name,
                "operation": request.operation,
                "extent": request.extent,
                "distance": request.distance.as_dict() if request.distance else None,
                "profiles": len(loops),
                "profile_area_cm2": round(area, 6),
            },
        )
        document.features.append(feature)
        sketch.consumed_by = name
        document.modified = True
        self._record("extrude", name=name, sketch=sketch.name, operation=request.operation)
        return _feature_info(feature)

    def _synthesise_extrude_topology(
        self,
        document: _Document,
        sketch: _Sketch,
        loops: Sequence[Sequence[str]],
        plane: str,
        distance: float,
        direction: str,
        feature_name: str,
    ) -> None:
        normal = plane_normal(plane)
        offset = sketch.plan.offset_value
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
            for primitive_id in loop:
                primitive = sketch.plan.by_id(primitive_id)
                self._synthesise_side(document, plane, primitive, near, far, feature_name)

    def _synthesise_side(
        self,
        document: _Document,
        plane: str,
        primitive: Any,
        near: float,
        far: float,
        feature_name: str,
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
            # The edge running along the extrusion direction at the segment start.
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
        area = _net_area(sketch, loops)
        angle = request.angle.value if request.angle else 2 * math.pi
        # Pappus's theorem about the sketch-space centroid distance to the axis.
        radius = _centroid_radius(sketch, loops, request.axis)
        volume = area * radius * angle
        if request.operation == "cut":
            document.volume = max(document.volume - volume, 0.0)
        else:
            document.volume += volume

        plane = sketch.plan.plane if sketch.plan.plane in _PLANES else "xy"
        minx, miny, maxx, maxy = plan_bounds(sketch.plan)
        reach = max(abs(minx), abs(maxx), abs(miny), abs(maxy))
        for corner in (-reach, reach):
            for other in (-reach, reach):
                for depth in (-reach, reach):
                    self._expand_bounds(document, [map3d(plane, corner, other, depth)])

        name = self._feature_name(document, request.name, "revolution")
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="revolve",
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
        length = sum(
            primitive.length for primitive in path.plan.primitives if isinstance(primitive, PLine)
        )
        document.volume += area * length if request.operation != "cut" else -area * length
        document.volume = max(document.volume, 0.0)
        name = self._feature_name(document, request.name, "sweep")
        feature = _Feature(self._next("feat"), name, "sweep",
                           detail={"profile": profile.name, "path": path.name})
        document.features.append(feature)
        return _feature_info(feature)

    def loft(self, doc_id: str, request: LoftRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        sketches = [document.find_sketch(name) for name in request.sketches]
        areas = [_net_area(sketch, sketch.loops) for sketch in sketches]
        document.volume += sum(areas) / max(len(areas), 1)
        name = self._feature_name(document, request.name, "loft")
        feature = _Feature(self._next("feat"), name, "loft",
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
        plane = sketch.plan.plane if sketch.plan.plane in _PLANES else "xy"
        radius = request.diameter.value / 2
        depth = request.depth.value if request.depth else _through_all_distance(document, plane)
        removed = math.pi * radius**2 * depth * len(centers)
        if request.style in ("counterbore", "spotface") and request.cbore_diameter and request.cbore_depth:
            removed += (
                math.pi
                * (request.cbore_diameter.value / 2) ** 2
                * request.cbore_depth.value
                * len(centers)
            )
        document.volume = max(document.volume - removed, 0.0)

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
            detail={
                "count": len(centers),
                "diameter": request.diameter.as_dict(),
                "style": request.style,
                "through_all": request.through_all,
                "tap": request.tap,
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
        # Removing (fillet) or adding (chamfer) a prism of roughly this size.
        document.volume = max(document.volume - total_length * size * size * 0.2146, 0.0)
        for match in matches:
            match.consumed = True
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind=kind,
            detail={**detail, "edges": len(matches), "edge_ids": [m.id for m in matches]},
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
        surface = sum(match.area or 0.0 for match in document.topology if match.kind == "face")
        removed = max(document.volume - surface * request.thickness.value, 0.0)
        document.volume -= removed
        name = self._feature_name(document, request.name, "shell")
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="shell",
            detail={
                "thickness": request.thickness.as_dict(),
                "direction": request.direction,
                "removed_faces": len(openings),
            },
        )
        document.features.append(feature)
        document.modified = True
        self._record("shell", name=name, removed_faces=len(openings))
        return _feature_info(feature)

    def rectangular_pattern(self, doc_id: str, request: RectangularPatternRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        targets = _pattern_targets(document, request.features)
        occurrences = request.count1 * request.count2
        name = self._feature_name(document, request.name, "pattern")
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="rectangular_pattern",
            detail={
                "features": [t.name for t in targets],
                "count1": request.count1,
                "count2": request.count2,
                "occurrences": occurrences,
                "note": _VOLUME_NOT_MODELLED,
            },
        )
        document.features.append(feature)
        self._record("rectangular_pattern", name=name, occurrences=occurrences)
        return _feature_info(feature)

    def circular_pattern(self, doc_id: str, request: CircularPatternRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        targets = _pattern_targets(document, request.features)
        name = self._feature_name(document, request.name, "pattern")
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="circular_pattern",
            detail={
                "features": [t.name for t in targets],
                "count": request.count,
                "angle_deg": round(math.degrees(request.angle.value), 4),
                "note": _VOLUME_NOT_MODELLED,
            },
        )
        document.features.append(feature)
        self._record("circular_pattern", name=name, count=request.count)
        return _feature_info(feature)

    def mirror(self, doc_id: str, request: MirrorRequest) -> FeatureInfo:
        document = self._doc(doc_id)
        targets = _pattern_targets(document, request.features)
        name = self._feature_name(document, request.name, "mirror")
        feature = _Feature(
            id=self._next("feat"),
            name=name,
            kind="mirror",
            detail={"features": [t.name for t in targets], "plane": request.plane,
                    "note": _VOLUME_NOT_MODELLED},
        )
        document.features.append(feature)
        self._record("mirror", name=name)
        return _feature_info(feature)

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
    """Outer loop area minus the loops nested inside it."""
    if not loops:
        return 0.0
    areas = [loop_area(sketch.plan, loop) for loop in loops]
    outer = max(areas)
    return max(outer - (sum(areas) - outer), 0.0)


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


def _through_all_distance(document: _Document, plane: str = "xy") -> float:
    """How far a through-feature must travel: the body's span along the plane normal."""
    if not document.bounds:
        return 1.0
    normal = plane_normal(plane)
    axis = max(range(3), key=lambda index: abs(normal[index]))
    span = document.bounds[axis + 3] - document.bounds[axis]
    if span > 0:
        return span
    spans = [document.bounds[i + 3] - document.bounds[i] for i in range(3)]
    return max(spans) or 1.0


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


def _centroid_radius(sketch: _Sketch, loops: Sequence[Sequence[str]], axis: AxisSpec) -> float:
    minx, miny, maxx, maxy = plan_bounds(sketch.plan)
    if axis.kind == "work_axis" and axis.value in ("x", "y", "z"):
        # Distance from the sketch centroid to the axis, in sketch space.
        return abs((miny + maxy) / 2) if axis.value == "x" else abs((minx + maxx) / 2)
    return max(abs(minx), abs(maxx))


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
        # The simulator synthesises topology from sketch loops and has no notion
        # of which side the material is on, so it accepts either rather than
        # claiming an answer. Inventor decides for real.
        return True
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
