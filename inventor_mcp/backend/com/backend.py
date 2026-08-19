"""The live Autodesk Inventor backend, driven over COM.

Requires Windows, ``pywin32`` and an installed Inventor.  Importing this module
elsewhere raises :class:`~inventor_mcp.errors.BackendUnavailableError` so the
server can fall back to the mock backend instead of failing to start.

Two conventions run through the whole file:

* Numbers handed to Inventor are already in database units (cm, radians).
* Anything the user should be able to change later is set as an *expression*
  string on the created parameter, not as a number.  That is what keeps the
  resulting part parametric rather than a dumb solid.
"""

from __future__ import annotations

import math
import os
import logging
from contextlib import contextmanager
from itertools import count
from typing import Any, Iterator, Sequence

from ...errors import (
    BackendUnavailableError,
    ConnectionFailedError,
    DocumentError,
    ExportError,
    FeatureError,
    InventorMCPError,
    ParameterError,
    SelectionError,
    SketchError,
)
from ...plan import PArc, PCircle, PEllipse, PLine, PPoint, PointRef, Ref, SketchPlan
from ...units import from_internal, inventor_symbol
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
from .constants import (
    BOOLEAN_OPERATIONS,
    DISPLAY_MODES,
    EXTENT_DIRECTIONS,
    SHELL_DIRECTIONS,
    VIEW_ORIENTATIONS,
    Constants,
    load,
)

logger = logging.getLogger("inventor_mcp.com")

try:  # pragma: no cover - exercised only on Windows
    import pythoncom  # type: ignore[import-not-found]
    import win32com.client  # type: ignore[import-not-found]

    _WIN32_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - the common case off Windows
    pythoncom = None  # type: ignore[assignment]
    win32com = None  # type: ignore[assignment]
    _WIN32_IMPORT_ERROR = exc


#: File extensions Inventor can write directly through ``SaveAs``.
EXPORT_EXTENSIONS = {
    "step": ".stp",
    "stp": ".stp",
    "iges": ".igs",
    "igs": ".igs",
    "stl": ".stl",
    "sat": ".sat",
    "dwg": ".dwg",
    "dxf": ".dxf",
    "obj": ".obj",
    "3mf": ".3mf",
    "ipt": ".ipt",
}


#: ``HealthStatusEnum`` values meaning "up to date, nothing to report".  Any other
#: value is surfaced verbatim rather than translated, because the numbering is
#: version-specific and a wrong gloss is worse than none.
_HEALTHY_STATUSES = {0, 15873}


#: Sketch planes whose first axis runs opposite to the model axis they are
#: named after.  Measured on Inventor 2027.1: a profile drawn from 0 to 90 in
#: sketch X on the XZ plane comes out spanning -90 to 0 in model X.
_MIRRORED_PLANES = {"xz"}

#: DocumentTypeEnum -> the specific COM interface that carries its members.
_DOCUMENT_INTERFACES = {
    12290: "PartDocument",
    12291: "AssemblyDocument",
    12292: "DrawingDocument",
    12293: "PresentationDocument",
}


#: How COM members are resolved.  "late" looks them up by name at call time;
#: "early" uses the wrapper pywin32 generates from the type library.
BINDING_MODES = ("late", "early")


def resolve_binding(binding: str | None = None) -> str:
    """Pick a binding mode from the argument, the environment, or the default.

    Late binding is the default: the generated early-bound wrapper mis-marshals
    several Inventor calls, and the cost of late binding is one name lookup per
    call.  ``INVENTOR_MCP_BINDING=early`` restores the old behaviour.
    """
    chosen = (binding or os.environ.get("INVENTOR_MCP_BINDING") or "late").strip().lower()
    return chosen if chosen in BINDING_MODES else "late"


def _as_late_bound(obj: Any) -> Any:  # pragma: no cover - Windows only
    """Re-wrap a COM object so members resolve by name at call time.

    pywin32 can generate an early-bound wrapper from Inventor's type library,
    and that wrapper marshals several calls in a way Inventor rejects outright
    -- ``Documents.Add`` handing back the generic ``Document`` interface,
    ``AddCoincident`` and ``AddForSolid`` failing with E_INVALIDARG on
    arguments that are demonstrably valid.  Late binding sidesteps the whole
    class of problem at the cost of a name lookup per call.
    """
    if win32com is None:
        return obj
    try:
        return win32com.client.dynamic.Dispatch(obj._oleobj_)
    except Exception:
        return obj


def _specialise(document: Any) -> Any:  # pragma: no cover - Windows only
    """Return *document* as its specific interface rather than plain ``Document``.

    ``Documents.Add`` and ``Documents.Item`` are declared as returning the
    generic ``Document``.  Late binding papers over that, but the early-bound
    wrapper pywin32 generates takes the declaration literally -- so
    ``ComponentDefinition``, which lives on ``PartDocument``, raises
    AttributeError and every subsequent call fails.  Cast once, here, and the
    rest of the backend can assume the real interface.
    """
    if win32com is None:
        return document
    try:
        target = _DOCUMENT_INTERFACES.get(int(document.DocumentType))
    except Exception:
        target = None
    if target:
        try:
            return win32com.client.CastTo(document, target)
        except Exception:
            pass
    # Last resort: dynamic dispatch resolves members by name at call time.
    try:
        return win32com.client.dynamic.Dispatch(document._oleobj_)
    except Exception:
        return document


def _supports_construction(primitive: Any) -> bool:
    """Whether Inventor lets this entity be marked as construction geometry.

    Construction is a property of curves -- it says "this shape is here to
    drive constraints, not to form a profile".  A sketch point forms no
    profile in the first place, so the flag is meaningless on one and setting
    it is rejected outright.
    """
    return not isinstance(primitive, PPoint)


def _same_com_object(first: Any, second: Any) -> bool:  # pragma: no cover - Windows only
    """True when two wrappers point at the same underlying COM object."""
    if first is second:
        return True
    if pythoncom is None:
        return False
    try:
        return (
            first._oleobj_.QueryInterface(pythoncom.IID_IUnknown)
            == second._oleobj_.QueryInterface(pythoncom.IID_IUnknown)
        )
    except Exception:
        return False


def _com_message(exc: Exception) -> str:
    """Pull the readable part out of a ``pythoncom.com_error``."""
    info = getattr(exc, "excepinfo", None)
    if isinstance(info, tuple) and len(info) > 2 and info[2]:
        return str(info[2]).strip()
    args = getattr(exc, "args", ())
    if len(args) > 1 and args[1]:
        return str(args[1]).strip()
    return str(exc)


class ComBackend(Backend):
    """Drives a running Inventor session."""

    name = "inventor"

    def __init__(self, binding: str | None = None) -> None:
        self.binding = resolve_binding(binding)
        if win32com is None:
            raise BackendUnavailableError(
                "The Inventor backend needs Windows and pywin32.",
                hint="Install with `pip install inventor-mcp[inventor]` on a machine with "
                "Autodesk Inventor, or run the server with --backend mock to work offline.",
                import_error=str(_WIN32_IMPORT_ERROR),
            )
        self._app: Any = None
        self._constants: Constants = Constants(None)
        self._documents: dict[str, Any] = {}
        self._sketches: dict[str, dict[str, Any]] = {}
        self._topology: dict[str, dict[str, Any]] = {}
        self._ids = count(1)

    # -- plumbing ----------------------------------------------------------
    def _next(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"

    def _require_app(self) -> Any:
        if self._app is None:
            raise ConnectionFailedError(
                "Not connected to Inventor.", hint="Call `connect` first."
            )
        return self._app

    def _k(self, name: str) -> int:
        return self._constants.resolve(name)

    @contextmanager
    def _batch(self, document: Any) -> Iterator[None]:
        """Suspend redraw and deferred updates while a burst of edits runs."""
        app = self._require_app()
        previous = None
        try:
            previous = app.ScreenUpdating
            app.ScreenUpdating = False
        except Exception:  # pragma: no cover - older builds expose this differently
            previous = None
        try:
            yield
        finally:
            if previous is not None:
                try:
                    app.ScreenUpdating = previous
                except Exception:
                    pass
            try:
                document.Update()
            except Exception:
                pass

    @contextmanager
    def _translate_errors(self, what: str, error_type: type[InventorMCPError] = FeatureError) -> Iterator[None]:
        try:
            yield
        except InventorMCPError:
            raise
        except Exception as exc:  # pragma: no cover - depends on live Inventor
            raise error_type(f"{what} failed: {self._explain(exc)}") from exc

    # -- session -----------------------------------------------------------
    def connect(self, *, visible: bool = True, create: bool = True) -> AppInfo:  # pragma: no cover
        pythoncom.CoInitialize()
        app = None
        try:
            app = win32com.client.GetActiveObject("Inventor.Application")
        except Exception:
            if not create:
                raise ConnectionFailedError(
                    "No running Inventor session was found.",
                    hint="Start Inventor, or call `connect` with create=true.",
                )
        if app is None:
            try:
                app = win32com.client.Dispatch("Inventor.Application")
            except Exception as exc:
                raise ConnectionFailedError(
                    f"Could not start Inventor: {_com_message(exc)}",
                    hint="Check that Autodesk Inventor is installed and licensed on this machine.",
                ) from exc

        try:
            app.Visible = bool(visible)
        except Exception:
            pass

        # Generating the type-library cache is what gives us exact enum values.
        # It is independent of how we then talk to Inventor, and Inventor is a
        # single-instance server, so this attaches to the session already open.
        try:
            win32com.client.gencache.EnsureDispatch("Inventor.Application")
        except Exception:
            pass

        if self.binding == "late":
            app = _as_late_bound(app)

        self._app = app
        self._constants = load(app)
        return self.info()

    def disconnect(self) -> None:  # pragma: no cover - Windows only
        self._app = None
        self._documents.clear()
        self._sketches.clear()
        self._topology.clear()
        if pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    def info(self) -> AppInfo:  # pragma: no cover - Windows only
        if self._app is None:
            return AppInfo(backend=self.name, connected=False)
        return AppInfo(
            backend=self.name,
            connected=True,
            version=str(getattr(self._app, "SoftwareVersion", None) and
                        self._app.SoftwareVersion.DisplayVersion),
            build=str(getattr(self._app.SoftwareVersion, "BuildIdentifier", "")),
            visible=bool(self._app.Visible),
            documents=int(self._app.Documents.Count),
        )

    # -- documents ---------------------------------------------------------
    def _doc(self, doc_id: str | None) -> Any:  # pragma: no cover - Windows only
        if doc_id is None:
            if len(self._documents) == 1:
                return next(iter(self._documents.values()))
            app = self._require_app()
            document = app.ActiveDocument
            if document is None:
                raise DocumentError("No document is open in Inventor.")
            return _specialise(document)
        document = self._documents.get(doc_id)
        if document is None:
            raise DocumentError(
                f"Unknown document handle {doc_id!r}.",
                hint="Call `list_documents` for the handles this session knows about.",
            )
        return document

    def _register(self, document: Any, units: str, angle_units: str) -> DocInfo:  # pragma: no cover
        doc_id = self._next("doc")
        self._documents[doc_id] = document
        self._sketches[doc_id] = {}
        return DocInfo(
            id=doc_id,
            name=str(document.DisplayName),
            path=str(document.FullFileName) or None,
            units=units,
            angle_units=angle_units,
            active=True,
        )

    def new_part(self, name: str, *, template: str | None = None, units: str = "mm",
                 angle_units: str = "deg") -> DocInfo:  # pragma: no cover - Windows only
        app = self._require_app()
        with self._translate_errors("Creating the part document", DocumentError):
            part_type = self._k("kPartDocumentObject")
            path = template or app.FileManager.GetTemplateFile(part_type)
            document = _specialise(app.Documents.Add(part_type, path, True))
            try:
                document.DisplayName = name
            except Exception:
                pass
            self._apply_units(document, units, angle_units)
        return self._register(document, units, angle_units)

    def _apply_units(self, document: Any, units: str, angle_units: str) -> None:  # pragma: no cover
        try:
            unit_of_measure = document.UnitsOfMeasure
            unit_of_measure.LengthUnits = unit_of_measure.GetTypeFromString(inventor_symbol(units))
            unit_of_measure.AngleUnits = unit_of_measure.GetTypeFromString(inventor_symbol(angle_units))
        except Exception:
            # Not fatal: every value we send carries an explicit unit anyway.
            pass

    def open_document(self, path: str) -> DocInfo:  # pragma: no cover - Windows only
        app = self._require_app()
        if not os.path.exists(path):
            raise DocumentError(f"No such file: {path}")
        with self._translate_errors("Opening the document", DocumentError):
            document = _specialise(app.Documents.Open(path, True))
        return self._register(document, "mm", "deg")

    def list_documents(self) -> list[DocInfo]:  # pragma: no cover - Windows only
        app = self._require_app()
        known = {id(document): doc_id for doc_id, document in self._documents.items()}
        results: list[DocInfo] = []
        for index in range(1, int(app.Documents.Count) + 1):
            document = _specialise(app.Documents.Item(index))
            doc_id = known.get(id(document))
            if doc_id is None:
                doc_id = self._next("doc")
                self._documents[doc_id] = document
                self._sketches.setdefault(doc_id, {})
            results.append(
                DocInfo(
                    id=doc_id,
                    name=str(document.DisplayName),
                    path=str(document.FullFileName) or None,
                    kind=_document_kind(document),
                    active=document is app.ActiveDocument,
                    modified=bool(document.Dirty),
                )
            )
        return results

    def activate_document(self, doc_id: str) -> DocInfo:  # pragma: no cover - Windows only
        document = self._doc(doc_id)
        document.Activate()
        return DocInfo(id=doc_id, name=str(document.DisplayName), active=True)

    def save_document(self, doc_id: str, path: str | None = None) -> DocInfo:  # pragma: no cover
        document = self._doc(doc_id)
        with self._translate_errors("Saving", DocumentError):
            if path:
                os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
                document.SaveAs(path, False)
            else:
                document.Save()
        return DocInfo(
            id=doc_id,
            name=str(document.DisplayName),
            path=str(document.FullFileName) or None,
            modified=False,
        )

    def close_document(self, doc_id: str, *, save: bool = False) -> None:  # pragma: no cover
        document = self._doc(doc_id)
        if save:
            document.Save()
        document.Close(not save)
        self._documents.pop(doc_id, None)
        self._sketches.pop(doc_id, None)

    def set_material(self, doc_id: str, material: str,
                     appearance: str | None = None) -> DocInfo:  # pragma: no cover
        document = self._doc(doc_id)
        tried: list[str] = []
        asset = _find_asset(self._require_app(), document, material, "material", tried)
        if asset is not None:
            try:
                document.ActiveMaterial = asset
            except Exception as exc:
                raise DocumentError(
                    f"Found material {material!r} but could not apply it: {_com_message(exc)}"
                ) from exc
        else:
            # Older documents expose the material by name on the component
            # definition rather than as an asset.
            try:
                document.ComponentDefinition.Material = material
                tried.append("ComponentDefinition.Material")
            except Exception as exc:
                raise DocumentError(
                    f"No material named {material!r} is available to this document.",
                    hint="Use the exact name from Inventor's material browser, e.g. "
                    "'Aluminum 6061' rather than 'aluminium'. Looked in: "
                    f"{', '.join(tried) or 'no asset collection was reachable'}.",
                ) from exc

        if appearance:
            appearance_asset = _find_asset(
                self._require_app(), document, appearance, "appearance", []
            )
            if appearance_asset is not None:
                try:
                    document.ActiveAppearance = appearance_asset
                except Exception:
                    pass
        return DocInfo(id=doc_id, name=str(document.DisplayName), kind="part")

    # -- parameters --------------------------------------------------------
    def set_parameter(self, doc_id: str, name: str, expression: str, *, units: str = "mm",
                      comment: str = "", key: bool = False) -> ParamInfo:  # pragma: no cover
        document = self._doc(doc_id)
        parameters = document.ComponentDefinition.Parameters
        with self._translate_errors(f"Setting parameter {name!r}", ParameterError):
            existing = _find_parameter(parameters, name)
            if existing is not None:
                parameter = existing
                parameter.Expression = expression
            else:
                parameter = self._add_parameter(parameters, name, expression, units)
            if comment:
                parameter.Comment = comment
            try:
                document.ComponentDefinition.Parameters.KeyParameters  # noqa: B018
                parameter.IsKey = bool(key)
            except Exception:
                pass
        return _parameter_info(parameter)

    def _add_parameter(self, parameters: Any, name: str, expression: str,
                       units: str) -> Any:  # pragma: no cover - Windows only
        """Create a user parameter, reporting exactly what Inventor refused."""
        symbol = inventor_symbol(units)
        try:
            return parameters.UserParameters.AddByExpression(name, expression, symbol)
        except Exception as exc:
            first = self._explain(exc)

        # Inventor's expression parser is stricter than ours in ways that are
        # not always obvious; creating the parameter by value and then assigning
        # the expression gets a second, clearer error out of it if it is genuine.
        try:
            parameter = parameters.UserParameters.AddByValue(name, 0.0, symbol)
        except Exception as exc:
            raise ParameterError(
                f"Inventor refused the parameter {name!r} = {expression!r} "
                f"(units {symbol!r}): {first}",
                hint=self._diagnose_parameter(parameters, name, symbol),
            ) from exc
        try:
            parameter.Expression = expression
        except Exception as exc:
            try:
                parameter.Delete()
            except Exception:
                pass
            raise ParameterError(
                f"Inventor refused the expression {expression!r} for {name!r} "
                f"(units {symbol!r}): {self._explain(exc)}",
                hint=f"AddByExpression also refused it: {first}",
            ) from exc
        logger.info("Parameter %s was created by value because AddByExpression "
                    "refused it (%s).", name, first)
        return parameter

    def _diagnose_parameter(self, parameters: Any, name: str,
                            symbol: str) -> str:  # pragma: no cover - Windows only
        """Say whether it is the name Inventor objects to, or something else.

        Creating a throwaway parameter with a name Inventor cannot object to
        separates "this identifier is unacceptable" from "the document will not
        take parameters at all", which otherwise look identical.
        """
        probe = "inventor_mcp_probe"
        try:
            created = parameters.UserParameters.AddByValue(probe, 0.0, symbol)
        except Exception as exc:
            return (f"A throwaway parameter failed too ({_com_message(exc)}), so the "
                    "document is rejecting parameters rather than this name.")
        try:
            created.Delete()
        except Exception:
            pass
        return (f"A throwaway parameter with the same units succeeded, so Inventor is "
                f"objecting to the name {name!r} itself. Try renaming it.")

    def list_parameters(self, doc_id: str, *,
                        include_model: bool = False) -> list[ParamInfo]:  # pragma: no cover
        document = self._doc(doc_id)
        parameters = document.ComponentDefinition.Parameters
        results = [
            _parameter_info(parameters.UserParameters.Item(index))
            for index in range(1, int(parameters.UserParameters.Count) + 1)
        ]
        if include_model:
            model = parameters.ModelParameters
            results.extend(
                _parameter_info(model.Item(index), kind="model")
                for index in range(1, int(model.Count) + 1)
            )
        return results

    def delete_parameter(self, doc_id: str, name: str) -> None:  # pragma: no cover
        document = self._doc(doc_id)
        parameter = _find_parameter(document.ComponentDefinition.Parameters, name)
        if parameter is None:
            raise ParameterError(f"No parameter named {name!r}.")
        with self._translate_errors(f"Deleting parameter {name!r}", ParameterError):
            parameter.Delete()

    # -- sketches ----------------------------------------------------------
    def build_sketch(self, doc_id: str, plan: SketchPlan) -> SketchInfo:  # pragma: no cover
        document = self._doc(doc_id)
        component = document.ComponentDefinition
        app = self._require_app()
        transient = app.TransientGeometry

        if plan.plane.lower() in _MIRRORED_PLANES:
            plan = plan.mirrored_u()
        plane = self._resolve_plane(document, plan.plane, plan.offset_expression)
        with self._batch(document):
            with self._translate_errors("Creating the sketch", SketchError):
                sketch = component.Sketches.Add(plane, False)
                if plan.name:
                    sketch.Name = plan.name

            objects: dict[str, Any] = {}
            # Endpoints that coincidence joins are built as one shared point
            # rather than two points plus a constraint: Inventor infers the
            # coincidence from the coordinates anyway and then refuses ours.
            groups = plan.shared_point_groups()
            shared: dict[tuple[str, str], Any] = {}
            with self._translate_errors("Adding sketch geometry", SketchError):
                for primitive in plan.primitives:
                    objects[primitive.id] = self._add_primitive(
                        sketch, transient, primitive, groups, shared
                    )

            inferred: list[str] = []
            refused: list[str] = []
            with self._translate_errors("Applying sketch constraints", SketchError):
                for constraint in plan.constraints:
                    if _is_structural(constraint, groups):
                        continue
                    outcome, note = self._add_constraint(sketch, objects, constraint)
                    if outcome == "inferred":
                        inferred.append(note)
                    elif outcome == "refused":
                        refused.append(note)
            if inferred:
                logger.info("Sketch %s: Inventor had already applied %d constraint(s): %s",
                            sketch.Name, len(inferred), "; ".join(inferred[:5]))

            with self._translate_errors("Applying sketch dimensions", SketchError):
                for dimension in plan.dimensions:
                    self._add_dimension(sketch, transient, objects, dimension)

        self._sketches.setdefault(doc_id, {})[sketch.Name] = sketch
        profiles = _count_profiles(sketch)
        return SketchInfo(
            id=self._next("sk"),
            name=str(sketch.Name),
            plane=plan.plane,
            entities=len(plan.primitives),
            constraints=len(plan.constraints),
            dimensions=len(plan.dimensions),
            profiles=profiles,
            hole_centers=len(plan.hole_centers),
            fully_constrained=_fully_constrained(sketch),
            inferred_constraints=len(inferred),
            refused_constraints=len(refused),
        )

    def _add_primitive(self, sketch: Any, transient: Any, primitive: Any,
                       groups: dict[tuple[str, str], Any] | None = None,
                       shared: dict[tuple[str, str], Any] | None = None) -> Any:  # pragma: no cover
        groups = groups or {}
        shared = shared if shared is not None else {}

        def anchor(which: str, position: tuple[float, float]) -> Any:
            """An existing shared point if one has been made, else a location."""
            group = groups.get((primitive.id, which))
            if group is not None and group in shared:
                return shared[group]
            return transient.CreatePoint2d(*position)

        def remember(entity: Any, which: str, attribute: str) -> None:
            group = groups.get((primitive.id, which))
            if group is not None and group not in shared:
                try:
                    shared[group] = getattr(entity, attribute)
                except Exception:  # pragma: no cover - version-specific
                    pass

        if isinstance(primitive, PLine):
            entity = sketch.SketchLines.AddByTwoPoints(
                anchor("start", primitive.start), anchor("end", primitive.end)
            )
            remember(entity, "start", "StartSketchPoint")
            remember(entity, "end", "EndSketchPoint")
        elif isinstance(primitive, PCircle):
            entity = sketch.SketchCircles.AddByCenterRadius(
                transient.CreatePoint2d(*primitive.center), primitive.radius
            )
        elif isinstance(primitive, PArc):
            start = _polar(primitive.center, primitive.radius, primitive.start_angle)
            end = _polar(primitive.center, primitive.radius, primitive.end_angle)
            entity = sketch.SketchArcs.AddByCenterStartEndPoint(
                transient.CreatePoint2d(*primitive.center),
                anchor("start", start),
                anchor("end", end),
            )
            remember(entity, "start", "StartSketchPoint")
            remember(entity, "end", "EndSketchPoint")
        elif isinstance(primitive, PEllipse):
            major_axis = transient.CreateUnitVector2d(
                math.cos(primitive.rotation), math.sin(primitive.rotation)
            )
            entity = sketch.SketchEllipses.Add(
                transient.CreatePoint2d(*primitive.center),
                major_axis,
                primitive.major_radius,
                primitive.minor_radius,
            )
        elif isinstance(primitive, PPoint):
            entity = sketch.SketchPoints.Add(
                transient.CreatePoint2d(*primitive.position), primitive.hole_center
            )
        else:
            raise SketchError(f"Cannot create {type(primitive).__name__} in Inventor.")

        if getattr(primitive, "construction", False) and _supports_construction(primitive):
            try:
                entity.Construction = True
            except Exception as exc:  # pragma: no cover - version-specific
                logger.info("Could not mark %s as construction geometry: %s",
                            primitive.id, _com_message(exc))
        if getattr(primitive, "centerline", False):
            try:
                entity.Centerline = True
            except Exception:
                pass
        return entity

    def _constraint_collections(self, sketch: Any) -> list[Any]:  # pragma: no cover
        """The constraints collection, early-bound first then late-bound.

        The generated early-bound wrapper marshals some of these calls in a way
        Inventor rejects; resolving the method by name at call time avoids it.
        """
        collection = sketch.GeometricConstraints
        collections = [collection]
        if win32com is not None:
            try:
                collections.append(win32com.client.dynamic.Dispatch(collection._oleobj_))
            except Exception:
                pass
        return collections

    def _explain(self, exc: Exception | None) -> str:  # pragma: no cover - Windows only
        """Inventor's own account of the failure, when it has one."""
        return self._explain_text(_com_message(exc) if exc is not None else "no error reported")

    def _explain_text(self, message: str) -> str:  # pragma: no cover - Windows only
        """Append Inventor's last error message, which its excepinfo omits."""
        try:
            detail = str(self._app.ErrorManager.LastErrorMessage or "").strip() or None
        except Exception:
            detail = None
        return f"{message} ({detail})" if detail and detail not in message else message

    def _origin_point(self, sketch: Any) -> Any:  # pragma: no cover - Windows only
        """A sketch point at the origin that constraints can actually target.

        ``PlanarSketch.OriginPoint`` looks like the obvious choice, but Inventor
        refuses to constrain against it -- it is a marker for where the sketch
        sits, not a point participating in the sketch.  Projecting the part's
        origin work point produces a real, associative SketchPoint that behaves
        like any other.
        """
        try:
            component = sketch.Parent
            return sketch.AddByProjectingEntity(component.WorkPoints.Item(1))
        except Exception as exc:
            logger.info("Could not project the origin into %s (%s); "
                        "falling back to a grounded point.", sketch.Name, _com_message(exc))

        # Not associative, but it pins geometry to the origin just as well.
        app = self._require_app()
        point = sketch.SketchPoints.Add(app.TransientGeometry.CreatePoint2d(0.0, 0.0), False)
        try:
            sketch.GeometricConstraints.AddGround(point)
        except Exception:  # pragma: no cover - version-specific
            pass
        return point

    def _entity(self, sketch: Any, objects: dict[str, Any], ref: Ref) -> Any:  # pragma: no cover
        if ref.entity == "__origin__":
            # Resolved once per sketch, on first use, and cached alongside the
            # sketch's other entities so it lives exactly as long as they do.
            origin = objects.get("__origin__")
            if origin is None:
                origin = self._origin_point(sketch)
                objects["__origin__"] = origin
            return origin
        target = objects.get(ref.entity)
        if target is None:
            raise SketchError(f"Internal error: sketch entity {ref.entity!r} was not created.")
        if ref.point is PointRef.SELF:
            return target
        if ref.point is PointRef.START:
            return target.StartSketchPoint
        if ref.point is PointRef.END:
            return target.EndSketchPoint
        if ref.point is PointRef.CENTER:
            return target.CenterSketchPoint
        raise SketchError(f"Unsupported point reference {ref.point.value!r}.")

    def _apply_constraint(self, constraints: Any, kind: str, targets: list[Any]) -> None:
        """Dispatch one constraint onto a ``GeometricConstraints`` collection."""
        if kind == "horizontal":
            constraints.AddHorizontal(targets[0])
        elif kind == "vertical":
            constraints.AddVertical(targets[0])
        elif kind == "horizontal_align":
            constraints.AddHorizontalAlign(targets[0], targets[1])
        elif kind == "vertical_align":
            constraints.AddVerticalAlign(targets[0], targets[1])
        elif kind == "coincident":
            constraints.AddCoincident(targets[0], targets[1])
        elif kind == "collinear":
            constraints.AddCollinear(targets[0], targets[1])
        elif kind == "parallel":
            constraints.AddParallel(targets[0], targets[1])
        elif kind == "perpendicular":
            constraints.AddPerpendicular(targets[0], targets[1])
        elif kind == "tangent":
            constraints.AddTangent(targets[0], targets[1])
        elif kind == "concentric":
            constraints.AddConcentric(targets[0], targets[1])
        elif kind == "equal_length":
            constraints.AddEqualLength(targets[0], targets[1])
        elif kind == "equal_radius":
            constraints.AddEqualRadius(targets[0], targets[1])
        elif kind == "symmetric":
            constraints.AddSymmetry(targets[0], targets[1], targets[2])
        elif kind == "midpoint":
            constraints.AddMidpoint(targets[0], targets[1])
        elif kind == "ground":
            constraints.AddGround(targets[0])
        else:
            raise SketchError(f"Unsupported constraint {kind!r}.")

    def _add_constraint(self, sketch: Any, objects: dict[str, Any],
                        constraint: Any) -> tuple[str, str]:  # pragma: no cover
        """Apply one geometric constraint, reporting what became of it.

        Returns ``("applied", ...)``, ``("inferred", ...)`` when Inventor had
        already made the same constraint itself, or ``("refused", ...)`` when it
        rejected one as dependent on the others.  Those three are genuinely
        different: only the last leaves a degree of freedom behind, and only a
        failed *structural* constraint is fatal.
        """
        targets = [self._entity(sketch, objects, ref) for ref in constraint.refs]
        kind = constraint.kind
        where = f"{kind}({', '.join(str(ref) for ref in constraint.refs)})"

        if kind == "coincident" and _same_com_object(targets[0], targets[1]):
            return ("inferred", f"{where}: already the same point")

        first_error: Exception | None = None
        for collection in self._constraint_collections(sketch):
            try:
                self._apply_constraint(collection, kind, targets)
                return ("applied", where)
            except SketchError:
                raise
            except Exception as exc:
                first_error = first_error or exc

        # Inventor infers some constraints from the coordinates as geometry is
        # created, and then rejects an explicit duplicate. That is only benign
        # if the constraint really is there, so check rather than assume.
        if _already_constrained(sketch, kind, targets):
            return ("inferred", f"{where}: Inventor had already applied it")

        if kind in _STRUCTURAL_KINDS:
            raise SketchError(
                f"Could not apply {where}: {self._explain(first_error)}",
                hint="Without it the sketch geometry is not joined, so no profile can "
                "be built from it.",
            )

        # Everything else refines a sketch that is already closed. Inventor
        # sometimes rejects one as dependent on the constraints around it; that
        # leaves the sketch usable but with a degree of freedom still in it, so
        # it is reported rather than treated as fatal.
        logger.warning("Sketch %s: %s was refused (%s); the sketch keeps a degree of "
                       "freedom.", sketch.Name, where, self._explain(first_error))
        return ("refused", where)

    def _add_dimension(self, sketch: Any, transient: Any, objects: dict[str, Any],
                       dimension: Any) -> None:  # pragma: no cover
        dimensions = sketch.DimensionConstraints
        targets = [self._entity(sketch, objects, ref) for ref in dimension.refs]
        text = transient.CreatePoint2d(*_text_point(dimension))

        if dimension.kind in ("distance", "horizontal", "vertical"):
            orientation = {
                "distance": "kAlignedDim",
                "horizontal": "kHorizontalDim",
                "vertical": "kVerticalDim",
            }[dimension.kind]
            created = dimensions.AddTwoPointDistance(
                targets[0], targets[1], self._k(orientation), text
            )
        elif dimension.kind == "radius":
            created = dimensions.AddRadius(targets[0], text)
        elif dimension.kind == "diameter":
            created = dimensions.AddDiameter(targets[0], text)
        elif dimension.kind == "angle":
            if len(targets) < 2:
                raise SketchError("An angle dimension needs two lines.")
            created = dimensions.AddTwoLineAngle(targets[0], targets[1], text)
        else:
            raise SketchError(f"Unsupported dimension {dimension.kind!r}.")

        # The expression -- not the number -- is what makes the model parametric.
        created.Parameter.Expression = dimension.expression
        if dimension.name:
            created.Parameter.Name = dimension.name

    def list_sketches(self, doc_id: str) -> list[SketchInfo]:  # pragma: no cover
        document = self._doc(doc_id)
        sketches = document.ComponentDefinition.Sketches
        results = []
        for index in range(1, int(sketches.Count) + 1):
            sketch = sketches.Item(index)
            results.append(
                SketchInfo(
                    id=f"sk:{sketch.Name}",
                    name=str(sketch.Name),
                    plane=_sketch_plane_name(sketch),
                    entities=int(sketch.SketchEntities.Count),
                    constraints=int(sketch.GeometricConstraints.Count),
                    dimensions=int(sketch.DimensionConstraints.Count),
                    profiles=_count_profiles(sketch),
                    fully_constrained=_fully_constrained(sketch),
                )
            )
        return results

    def _sketch(self, doc_id: str, name: str) -> Any:  # pragma: no cover
        cached = self._sketches.get(doc_id, {}).get(name)
        if cached is not None:
            return cached
        document = self._doc(doc_id)
        sketches = document.ComponentDefinition.Sketches
        for index in range(1, int(sketches.Count) + 1):
            sketch = sketches.Item(index)
            if str(sketch.Name) == name:
                self._sketches.setdefault(doc_id, {})[name] = sketch
                return sketch
        raise SketchError(f"No sketch named {name!r} in this part.")

    # -- planes and axes ---------------------------------------------------
    def _resolve_plane(self, document: Any, reference: str,
                       offset_expression: str | None) -> Any:  # pragma: no cover
        component = document.ComponentDefinition
        base: Any
        key = reference.lower()
        if key in ("xy", "xz", "yz"):
            base = _origin_plane(component, key)
        elif reference.startswith("face:"):
            handle = reference.split(":", 1)[1]
            entry = self._topology.get(handle)
            if entry is None:
                raise SketchError(
                    f"Unknown face handle {handle!r}.",
                    hint="Face handles come from `select_topology` and expire on rebuild.",
                )
            base = entry["object"]
        else:
            base = _named_work_plane(component, reference)

        if not offset_expression:
            return base
        plane = component.WorkPlanes.AddByPlaneAndOffset(base, 0.0)
        try:
            plane.Definition.Offset.Expression = offset_expression
        except Exception:
            pass
        plane.Visible = False
        return plane

    def _resolve_axis(self, doc_id: str, axis: AxisSpec) -> Any:  # pragma: no cover
        document = self._doc(doc_id)
        component = document.ComponentDefinition
        if axis.kind == "work_axis":
            index = {"x": 1, "y": 2, "z": 3}.get(axis.value.lower())
            if index is None:
                return _named_work_axis(component, axis.value)
            return component.WorkAxes.Item(index)
        if axis.kind == "edge":
            entry = self._topology.get(axis.value)
            if entry is None:
                raise SelectionError(f"Unknown edge handle {axis.value!r}.")
            return entry["object"]
        sketch = self._sketch(doc_id, axis.sketch or "")
        for index in range(1, int(sketch.SketchLines.Count) + 1):
            line = sketch.SketchLines.Item(index)
            if str(getattr(line, "Name", "")) == axis.value:
                return line
        raise FeatureError(
            f"Sketch {axis.sketch!r} has no line named {axis.value!r} to revolve about.",
            hint="Give the sketch line a `name` in the recipe and reference it here.",
        )

    # -- features ----------------------------------------------------------
    def _profiles(self, sketch: Any, selection: Sequence[int] | str) -> Any:  # pragma: no cover
        """Build a profile from the sketch's closed loops.

        ``AddForSolid``'s ``Combine`` flag is optional-with-a-default, which is
        not always marshalled cleanly, so it is passed explicitly before being
        left to the default -- and the collection is tried late-bound too.
        """
        failures: list[str] = []
        attempts = [
            (collection, arguments)
            for collection in _distinct(sketch.Profiles, _as_late_bound(sketch.Profiles))
            for arguments in ((True,), ())
        ]
        for profiles, arguments in attempts:
            try:
                profile = profiles.AddForSolid(*arguments)
            except Exception as exc:
                failures.append(_com_message(exc))
                continue
            if int(profile.Count) > 0:
                return profile
            failures.append("Inventor returned a profile with no closed loop in it.")
            try:
                profile.Delete()
            except Exception:
                pass

        raise FeatureError(
            f"No usable profile in sketch {sketch.Name!r}: {self._explain_text(failures[0])}",
            hint="A solid feature needs a closed loop of non-construction geometry. "
            f"This sketch contains {_describe_sketch(sketch)}.",
        )

    def extrude(self, doc_id: str, request: ExtrudeRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        sketch = self._sketch(doc_id, request.sketch)
        features = document.ComponentDefinition.Features.ExtrudeFeatures
        before = _solid_volume(document) if request.operation == "cut" else None
        with self._batch(document), self._translate_errors("Extrude"):
            profile = self._profiles(sketch, request.profiles)
            definition = features.CreateExtrudeDefinition(
                profile, self._k(BOOLEAN_OPERATIONS[request.operation])
            )
            direction = self._k(EXTENT_DIRECTIONS[request.direction])
            if request.extent == "through_all":
                definition.SetThroughAllExtent(direction)
            elif request.extent == "to_next":
                definition.SetToNextExtent(direction)
            else:
                assert request.distance is not None
                definition.SetDistanceExtent(request.distance.expression, direction)
            if request.taper is not None:
                definition.TaperAngle = request.taper.expression
            feature = features.Add(definition)
            # A cut that meets no material builds without complaint and leaves
            # the part exactly as it was.  Saying so is the whole point: an
            # `ok` on a cut that did nothing is worse than a failure, because
            # it sends you looking at the wrong operation.
            if request.operation == "cut" and not _removed_material(document, before):
                _delete_quietly(feature)
                raise FeatureError(
                    f"The cut from sketch {request.sketch!r} removed no material.",
                    hint="Its profile does not overlap the part. Check the sketch "
                    "plane and the coordinates against the part's bounding box, "
                    "and check `direction` -- a cut runs one way from its plane.",
                )
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "extrude", {
            "sketch": request.sketch,
            "operation": request.operation,
            "distance": request.distance.as_dict() if request.distance else None,
        })

    def revolve(self, doc_id: str, request: RevolveRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        sketch = self._sketch(doc_id, request.sketch)
        axis = self._resolve_axis(doc_id, request.axis)
        features = document.ComponentDefinition.Features.RevolveFeatures
        operation = self._k(BOOLEAN_OPERATIONS[request.operation])
        with self._batch(document), self._translate_errors("Revolve"):
            profile = self._profiles(sketch, request.profiles)
            if request.angle is None:
                feature = features.AddFull(profile, axis, operation)
            else:
                feature = features.AddByAngle(
                    profile,
                    axis,
                    request.angle.expression,
                    self._k(EXTENT_DIRECTIONS[request.direction]),
                    operation,
                )
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "revolve", {"sketch": request.sketch, "axis": request.axis.value})

    def sweep(self, doc_id: str, request: SweepRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        profile_sketch = self._sketch(doc_id, request.profile_sketch)
        path_sketch = self._sketch(doc_id, request.path_sketch)
        features = document.ComponentDefinition.Features.SweepFeatures
        with self._batch(document), self._translate_errors("Sweep"):
            profile = profile_sketch.Profiles.AddForSolid()
            path = path_sketch.Profiles.AddForSurface(path_sketch.SketchEntities.Item(1))
            feature = features.AddUsingPath(
                profile, path, self._k(BOOLEAN_OPERATIONS[request.operation])
            )
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "sweep", {"path": request.path_sketch})

    def loft(self, doc_id: str, request: LoftRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        app = self._require_app()
        features = document.ComponentDefinition.Features.LoftFeatures
        with self._batch(document), self._translate_errors("Loft"):
            definition = features.CreateLoftDefinition(
                app.TransientObjects.CreateObjectCollection(),
                self._k(BOOLEAN_OPERATIONS[request.operation]),
            )
            for name in request.sketches:
                definition.Sections.Add(self._sketch(doc_id, name).Profiles.AddForSolid())
            feature = features.Add(definition)
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "loft", {"sections": list(request.sketches)})

    def hole(self, doc_id: str, request: HoleRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        app = self._require_app()
        sketch = self._sketch(doc_id, request.sketch)
        features = document.ComponentDefinition.Features.HoleFeatures

        centers = app.TransientObjects.CreateObjectCollection()
        wanted = set(request.point_indices)
        hole_index = 0
        for index in range(1, int(sketch.SketchPoints.Count) + 1):
            point = sketch.SketchPoints.Item(index)
            if not bool(getattr(point, "HoleCenter", False)):
                continue
            if not wanted or hole_index in wanted:
                centers.Add(point)
            hole_index += 1
        if centers.Count == 0:
            raise FeatureError(
                f"Sketch {request.sketch!r} has no hole-centre points.",
                hint="Add `point`, `point_grid` or `bolt_circle` entities to the sketch.",
            )

        # ExtentDirection is an enum, not a boolean: passing True/False sends 1
        # or 0, which are not values of PartFeatureExtentDirectionEnum.
        requested = self._k(EXTENT_DIRECTIONS.get(request.direction, "kNegativeExtentDirection"))
        opposite = self._k(
            "kPositiveExtentDirection"
            if request.direction == "negative"
            else "kNegativeExtentDirection"
        )
        # Which way a sketch plane faces is not something the caller should have
        # to reason about, so a hole that finds no material is retried the other
        # way.  Flipping a blind hole preserves its depth -- the depth is what
        # the recipe meant, the side of the plane is not.
        directions = [requested, opposite]

        feature = None
        failures: list[str] = []
        flipped = False
        before = _solid_volume(document)
        with self._batch(document), self._translate_errors("Hole"):
            placement = features.CreateSketchPlacementDefinition(centers)
            for index, direction in enumerate(directions):
                try:
                    if request.through_all:
                        feature = features.AddDrilledByThroughAllExtent(
                            placement, request.diameter.expression, direction
                        )
                    else:
                        assert request.depth is not None
                        feature = _call_by_keyword(
                            features.AddDrilledByDistanceExtent,
                            [
                                # Named, because the trailing arguments of this
                                # family are not in the order they read: the
                                # counterbore variant puts ExtentDirection at
                                # index 3 and the bottom-tip arguments after it.
                                {
                                    "PlacementDefinition": placement,
                                    "DiameterOrTapInfo": request.diameter.expression,
                                    "Depth": request.depth.expression,
                                    "ExtentDirection": direction,
                                },
                            ],
                            positional=(
                                placement, request.diameter.expression,
                                request.depth.expression, direction,
                            ),
                        )
                except Exception as exc:
                    failures.append(_com_message(exc))
                    continue
                # Inventor is happy to drill into thin air and call it a
                # success, so the feature only counts if the part got smaller.
                if _removed_material(document, before):
                    if index:
                        flipped = True
                        logger.info("Hole in %s drilled the opposite way: the sketch "
                                    "plane faces away from the material.", request.sketch)
                    break
                failures.append("the hole removed no material")
                _delete_quietly(feature)
                feature = None
            if feature is None:
                raise FeatureError(
                    f"Could not drill the hole: {self._explain_text(failures[0])}",
                    hint="Check that the hole centres sit over material and that the "
                    "diameter is smaller than the surrounding geometry.",
                )
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "hole", {
            "count": int(centers.Count),
            "diameter": request.diameter.as_dict(),
            "style": request.style,
            "flipped": flipped,
        })

    def _new_collection(self, kind: str) -> Any:  # pragma: no cover - Windows only
        """A collection of the type Inventor expects for *kind*.

        Feature methods are typed: fillets and chamfers take an
        ``EdgeCollection``, shells and threads a ``FaceCollection``.  A generic
        ``ObjectCollection`` holds the same objects but is refused as a type
        mismatch, which is what it looks like when a fillet will not build.
        """
        transient = self._require_app().TransientObjects
        factory = {"edge": "CreateEdgeCollection", "face": "CreateFaceCollection"}.get(kind)
        if factory:
            creator = getattr(transient, factory, None)
            if creator is not None:
                try:
                    return creator()
                except Exception:  # pragma: no cover - version-specific
                    pass
        return transient.CreateObjectCollection()

    def _topology_collection(self, doc_id: str, selector: ResolvedSelector, *,
                             required: bool = True) -> Any:  # pragma: no cover
        matches = self.select(doc_id, selector)
        if not matches and required:
            raise SelectionError(
                f"The selector matched no {selector.kind}s.",
                hint="Call `select_topology` with the same selector to see the alternatives.",
                selector=selector.__dict__,
            )
        collection = self._new_collection(selector.kind)
        for match in matches:
            collection.Add(self._topology[match.id]["object"])
        return collection

    #: AddSimple's trailing options, in declaration order. They are
    #: optional-with-a-default, and leaving them out makes pywin32 send a
    #: missing-variant that Inventor rejects as a type mismatch -- the same
    #: thing that broke AddForSolid. These are Inventor's own UI defaults.
    _FILLET_OPTIONS = (
        False,  # AllFillets
        False,  # AllRounds
        True,   # AutomaticEdgeChain
        True,   # RollAlongSharpEdges
        True,   # RollingBallWherePossible
        False,  # PreserveAllFeatures
    )

    def fillet(self, doc_id: str, request: FilletRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        edges = self._topology_collection(doc_id, request.edges)
        features = document.ComponentDefinition.Features.FilletFeatures

        failures: list[str] = []
        with self._batch(document), self._translate_errors("Fillet"):
            feature = None
            # An expression keeps the fillet parameter-driven; a plain number is
            # the fallback if this version will not take one there.
            for radius, described in ((request.radius.expression, "expression"),
                                      (request.radius.value, "value")):
                try:
                    feature = features.AddSimple(edges, radius, *self._FILLET_OPTIONS)
                except Exception as exc:
                    failures.append(f"radius as {described}: {_com_message(exc)}")
                    continue
                if described == "value" and not _set_radius_expression(
                    feature, request.radius.expression
                ):
                    logger.warning(
                        "Fillet radius is a fixed %s rather than the expression %r: "
                        "this feature will not follow the parameter.",
                        request.radius.value, request.radius.expression,
                    )
                break
            if feature is None:
                raise FeatureError(
                    f"Could not create the fillet: {self._explain_text(failures[0])}",
                    hint="; ".join(failures),
                )
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "fillet", {
            "edges": int(edges.Count), "radius": request.radius.as_dict()
        })

    def chamfer(self, doc_id: str, request: ChamferRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        edges = self._topology_collection(doc_id, request.edges)
        features = document.ComponentDefinition.Features.ChamferFeatures
        with self._batch(document), self._translate_errors("Chamfer"):
            if request.distance2 is not None:
                feature = features.AddUsingTwoDistances(
                    edges, None, request.distance.expression, request.distance2.expression
                )
            elif request.angle is not None:
                feature = features.AddUsingDistanceAndAngle(
                    edges, None, request.distance.expression, request.angle.expression
                )
            else:
                feature = features.AddUsingDistance(edges, request.distance.expression)
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "chamfer", {
            "edges": int(edges.Count), "distance": request.distance.as_dict()
        })

    def shell(self, doc_id: str, request: ShellRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        features = document.ComponentDefinition.Features.ShellFeatures
        # An empty face collection is meaningful here: it hollows the body out
        # without opening it.
        faces = (
            self._topology_collection(doc_id, request.faces, required=False)
            if request.faces.ids or request.faces.filter != "all"
            else self._new_collection("face")
        )
        with self._batch(document):
            try:
                definition = features.CreateShellDefinition(
                    faces, request.thickness.expression,
                    self._k(SHELL_DIRECTIONS[request.direction]),
                )
                feature = features.Add(definition)
            except Exception as exc:
                raise FeatureError(
                    f"Shell failed: {self._explain(exc)}",
                    hint=f"{int(faces.Count)} face(s) were selected to open, thickness "
                    f"{request.thickness.expression!r}, direction {request.direction!r}. "
                    "A thickness larger than the smallest local wall, or a face set that "
                    "does not bound the body, will both refuse.",
                ) from exc
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "shell", {
            "removed_faces": int(faces.Count), "thickness": request.thickness.as_dict()
        })

    def _feature_collection(self, doc_id: str, names: Sequence[str]) -> Any:  # pragma: no cover
        document = self._doc(doc_id)
        app = self._require_app()
        features = document.ComponentDefinition.Features
        collection = app.TransientObjects.CreateObjectCollection()
        if names:
            for name in names:
                collection.Add(_find_feature(features, name))
        else:
            if int(features.Count) == 0:
                raise FeatureError("There is no feature to pattern yet.")
            collection.Add(features.Item(int(features.Count)))
        return collection

    def rectangular_pattern(self, doc_id: str,
                            request: RectangularPatternRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        parents = self._feature_collection(doc_id, request.features)
        axis1 = self._resolve_axis(doc_id, request.axis1)
        features = document.ComponentDefinition.Features.RectangularPatternFeatures
        with self._batch(document), self._translate_errors("Rectangular pattern"):
            if request.axis2 is not None and request.count2 > 1 and request.spacing2 is not None:
                feature = features.Add(
                    parents, axis1, not request.flip1, request.count1, request.spacing1.expression,
                    self._k("kAdjustToModelCompute"),
                    self._resolve_axis(doc_id, request.axis2), not request.flip2,
                    request.count2, request.spacing2.expression,
                )
            else:
                feature = features.Add(
                    parents, axis1, not request.flip1, request.count1, request.spacing1.expression
                )
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "rectangular_pattern", {
            "count1": request.count1, "count2": request.count2
        })

    def circular_pattern(self, doc_id: str,
                         request: CircularPatternRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        parents = self._feature_collection(doc_id, request.features)
        axis = self._resolve_axis(doc_id, request.axis)
        features = document.ComponentDefinition.Features.CircularPatternFeatures
        with self._batch(document), self._translate_errors("Circular pattern"):
            feature = features.Add(
                parents, axis, True, request.count, request.angle.expression, request.fitted
            )
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "circular_pattern", {"count": request.count})

    def mirror(self, doc_id: str, request: MirrorRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        parents = self._feature_collection(doc_id, request.features)
        plane = self._resolve_plane(document, request.plane, None)
        features = document.ComponentDefinition.Features.MirrorFeatures
        with self._batch(document), self._translate_errors("Mirror"):
            feature = features.Add(
                parents, plane, False, self._k("kAdjustToModelCompute")
            )
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "mirror", {"plane": request.plane})

    def work_plane(self, doc_id: str, request: WorkPlaneRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        component = document.ComponentDefinition
        base = self._resolve_plane(document, request.base, None)
        with self._batch(document), self._translate_errors("Work plane"):
            if request.kind == "midplane" and request.second:
                plane = component.WorkPlanes.AddByTwoPlanes(
                    base, self._resolve_plane(document, request.second, None)
                )
            else:
                plane = component.WorkPlanes.AddByPlaneAndOffset(base, 0.0)
                if request.offset is not None:
                    plane.Definition.Offset.Expression = request.offset.expression
            if request.name:
                plane.Name = request.name
            plane.Visible = False
        return FeatureInfo(id=f"wp:{plane.Name}", name=str(plane.Name), kind="work_plane",
                           detail={"base": request.base})

    def thread(self, doc_id: str, request: ThreadRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        faces = self._topology_collection(doc_id, request.faces)
        features = document.ComponentDefinition.Features.ThreadFeatures
        with self._batch(document), self._translate_errors("Thread"):
            definition = features.CreateThreadDefinition(
                faces.Item(1), request.internal, request.designation
            )
            feature = features.Add(definition)
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "thread", {"designation": request.designation})

    # -- model state -------------------------------------------------------
    def list_features(self, doc_id: str) -> list[FeatureInfo]:  # pragma: no cover
        document = self._doc(doc_id)
        features = document.ComponentDefinition.Features
        results = []
        for index in range(1, int(features.Count) + 1):
            feature = features.Item(index)
            results.append(
                FeatureInfo(
                    id=f"feat:{feature.Name}",
                    name=str(feature.Name),
                    kind=_feature_kind(feature),
                    suppressed=bool(feature.Suppressed),
                )
            )
        return results

    def suppress_feature(self, doc_id: str, name: str, suppressed: bool) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        feature = _find_feature(document.ComponentDefinition.Features, name)
        feature.Suppressed = suppressed
        document.Update()
        return FeatureInfo(id=f"feat:{name}", name=name, kind=_feature_kind(feature),
                           suppressed=suppressed)

    def delete_feature(self, doc_id: str, name: str) -> None:  # pragma: no cover
        document = self._doc(doc_id)
        _find_feature(document.ComponentDefinition.Features, name).Delete()
        document.Update()

    def rename_feature(self, doc_id: str, name: str, new_name: str) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        feature = _find_feature(document.ComponentDefinition.Features, name)
        feature.Name = new_name
        return FeatureInfo(id=f"feat:{new_name}", name=new_name, kind=_feature_kind(feature))

    def select(self, doc_id: str, selector: ResolvedSelector) -> list[TopoInfo]:  # pragma: no cover
        document = self._doc(doc_id)
        component = document.ComponentDefinition
        if int(component.SurfaceBodies.Count) == 0:
            raise SelectionError("The part has no solid body yet, so there is nothing to select.")

        if selector.ids:
            missing = [handle for handle in selector.ids if handle not in self._topology]
            if missing:
                raise SelectionError(
                    f"Unknown topology handles: {missing}.",
                    hint="Handles expire whenever the model rebuilds; re-run `select_topology`.",
                )
            return [self._topology[handle]["info"] for handle in selector.ids]

        source_faces: list[Any] = []
        source_edges: list[Any] = []
        if selector.feature:
            feature = _find_feature(component.Features, selector.feature)
            source_faces = list(_iterate(feature.Faces))
            source_edges = [edge for face in source_faces for edge in _iterate(face.Edges)]
        else:
            for index in range(1, int(component.SurfaceBodies.Count) + 1):
                body = component.SurfaceBodies.Item(index)
                source_faces.extend(_iterate(body.Faces))
                source_edges.extend(_iterate(body.Edges))

        candidates = source_faces if selector.kind == "face" else source_edges
        results: list[TopoInfo] = []
        seen: set[int] = set()
        for entity in candidates:
            key = id(entity)
            if key in seen:
                continue
            seen.add(key)
            info = self._describe(entity, selector.kind)
            if info is not None:
                results.append(info)

        results = [info for info in results if _com_passes_filter(info, selector.filter)]
        if selector.min_length is not None:
            results = [i for i in results if (i.length or i.area or 0.0) >= selector.min_length]
        if selector.max_length is not None:
            results = [i for i in results if (i.length or i.area or 0.0) <= selector.max_length]
        if selector.near is not None:
            near = selector.near
            results.sort(key=lambda i: math.dist(i.midpoint or (0, 0, 0), near))
            if selector.within is not None:
                results = [i for i in results
                           if math.dist(i.midpoint or (0, 0, 0), near) <= selector.within]
        elif selector.filter == "largest":
            results.sort(key=lambda i: -(i.area or i.length or 0.0))
        elif selector.filter == "smallest":
            results.sort(key=lambda i: (i.area or i.length or 0.0))
        elif selector.limit is not None:
            # Without an ordering, `limit` would keep whatever Inventor happened
            # to return first. Largest first is both reproducible and usually
            # what "the one big edge" means.
            results.sort(key=lambda i: (-(i.area or i.length or 0.0), i.midpoint or (0, 0, 0)))
        if selector.limit is not None:
            results = results[: selector.limit]
        return results

    def _describe(self, entity: Any, kind: str) -> TopoInfo | None:  # pragma: no cover
        handle = self._next("edge" if kind == "edge" else "face")
        try:
            evaluator = entity.Evaluator
            box = entity.Evaluator.RangeBox
            midpoint = (
                (box.MinPoint.X + box.MaxPoint.X) / 2,
                (box.MinPoint.Y + box.MaxPoint.Y) / 2,
                (box.MinPoint.Z + box.MaxPoint.Z) / 2,
            )
        except Exception:
            return None

        info: TopoInfo
        if kind == "edge":
            length = _edge_length(entity)
            geometry = _curve_type(entity)
            direction = _edge_direction(entity)
            convexity = _edge_convexity(entity)
            info = TopoInfo(
                id=handle,
                kind="edge",
                description=f"{geometry} edge",
                midpoint=midpoint,
                direction=direction,
                length=length,
                geometry=geometry,
                convexity=convexity,
            )
        else:
            try:
                area = float(evaluator.Area)
            except Exception:
                area = None  # type: ignore[assignment]
            geometry = _surface_type(entity)
            normal = _face_normal(entity)
            info = TopoInfo(
                id=handle,
                kind="face",
                description=f"{geometry} face",
                midpoint=midpoint,
                normal=normal,
                area=area,
                geometry=geometry,
            )
            direction = None

        self._topology[handle] = {"object": entity, "info": info, "direction": direction}
        return info

    def mass_properties(self, doc_id: str) -> MassProps:  # pragma: no cover
        document = self._doc(doc_id)
        component = document.ComponentDefinition
        properties = component.MassProperties
        box = component.RangeBox
        material = None
        try:
            material = str(document.ActiveMaterial.DisplayName)
        except Exception:
            pass
        return MassProps(
            volume=float(properties.Volume),
            area=float(properties.Area),
            mass=float(properties.Mass),
            material=material,
            center_of_mass=(
                float(properties.CenterOfMass.X),
                float(properties.CenterOfMass.Y),
                float(properties.CenterOfMass.Z),
            ),
            bounding_box=(
                float(box.MinPoint.X), float(box.MinPoint.Y), float(box.MinPoint.Z),
                float(box.MaxPoint.X), float(box.MaxPoint.Y), float(box.MaxPoint.Z),
            ),
        )

    def rebuild(self, doc_id: str) -> dict[str, Any]:  # pragma: no cover
        document = self._doc(doc_id)
        self._topology.clear()
        with self._translate_errors("Rebuild"):
            document.Rebuild()
        errors = []
        try:
            features = document.ComponentDefinition.Features
            for index in range(1, int(features.Count) + 1):
                feature = features.Item(index)
                status = getattr(feature, "HealthStatus", None)
                if status is None or int(status) in _HEALTHY_STATUSES:
                    continue
                errors.append({
                    "feature": str(feature.Name),
                    "health_status": int(status),
                    "suppressed": bool(getattr(feature, "Suppressed", False)),
                })
        except Exception:
            pass
        return {"rebuilt": True, "errors": errors}

    # -- output ------------------------------------------------------------
    def export(self, doc_id: str, request: ExportRequest) -> dict[str, Any]:  # pragma: no cover
        document = self._doc(doc_id)
        fmt = request.format.lower()
        if fmt not in EXPORT_EXTENSIONS:
            raise ExportError(
                f"Unsupported export format {request.format!r}.",
                hint="Supported: " + ", ".join(sorted(set(EXPORT_EXTENSIONS))),
            )
        path = os.path.abspath(request.path)
        expected = EXPORT_EXTENSIONS[fmt]
        if not path.lower().endswith(expected) and not path.lower().endswith(f".{fmt}"):
            path += expected
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self._translate_errors(f"Exporting to {fmt.upper()}", ExportError):
            document.SaveAs(path, True)
        if not os.path.exists(path):
            raise ExportError(
                f"Inventor reported success but {path} was not written.",
                hint="The translator add-in for this format may be disabled in Inventor.",
            )
        return {"written": True, "path": path, "format": fmt,
                "bytes": os.path.getsize(path)}

    def screenshot(self, doc_id: str, request: ScreenshotRequest) -> dict[str, Any]:  # pragma: no cover
        app = self._require_app()
        document = self._doc(doc_id)
        document.Activate()
        path = os.path.abspath(request.path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self._translate_errors("Capturing the view", ExportError):
            view = app.ActiveView
            camera = view.Camera
            orientation = VIEW_ORIENTATIONS.get(request.orientation)
            if orientation:
                camera.ViewOrientationType = self._k(orientation)
            camera.Fit()
            camera.ApplyWithoutTransition()
            mode = DISPLAY_MODES.get(request.display_mode)
            if mode:
                try:
                    view.DisplayMode = self._k(mode)
                except Exception:
                    pass
            view.SaveAsBitmap(path, request.width, request.height)
        return {"written": os.path.exists(path), "path": path,
                "width": request.width, "height": request.height}


# ---------------------------------------------------------------------------
# Small COM helpers
# ---------------------------------------------------------------------------


def _iterate(collection: Any) -> Iterator[Any]:  # pragma: no cover - Windows only
    for index in range(1, int(collection.Count) + 1):
        yield collection.Item(index)


def _polar(center: tuple[float, float], radius: float, angle: float) -> tuple[float, float]:
    return (center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle))


def _text_point(dimension: Any) -> tuple[float, float]:
    """Somewhere near the geometry, so dimension text does not stack on the origin."""
    return (dimension.text_offset[0], dimension.text_offset[1])


def _origin_plane(component: Any, key: str) -> Any:  # pragma: no cover - Windows only
    index = {"yz": 1, "xz": 2, "xy": 3}[key]
    return component.WorkPlanes.Item(index)


def _named_work_plane(component: Any, name: str) -> Any:  # pragma: no cover - Windows only
    planes = component.WorkPlanes
    for index in range(1, int(planes.Count) + 1):
        if str(planes.Item(index).Name) == name:
            return planes.Item(index)
    raise SketchError(
        f"No work plane named {name!r}.",
        hint="Use 'xy', 'xz', 'yz', or create one with the `work_plane` operation first.",
    )


def _named_work_axis(component: Any, name: str) -> Any:  # pragma: no cover - Windows only
    axes = component.WorkAxes
    for index in range(1, int(axes.Count) + 1):
        if str(axes.Item(index).Name) == name:
            return axes.Item(index)
    raise FeatureError(f"No work axis named {name!r}.")


def _asset_collection(container: Any, kind: str) -> Any | None:  # pragma: no cover
    """The asset collection on a document or library, whatever it is called here.

    Which collections a ``Document`` exposes varies between Inventor releases,
    so probe rather than assume; a missing collection is a normal outcome, not
    an error.
    """
    names = ("MaterialAssets", "Assets") if kind == "material" else ("AppearanceAssets", "Assets")
    for name in names:
        collection = getattr(container, name, None)
        if collection is None:
            continue
        try:
            int(collection.Count)
        except Exception:
            continue
        return collection
    return None


def _find_asset(app: Any, document: Any, name: str, kind: str,
                tried: list[str]) -> Any | None:  # pragma: no cover
    """Find a material or appearance asset by display name.

    Assets already in the document win; otherwise the active libraries are
    searched and the match copied in, which Inventor requires before it can be
    made active.  ``tried`` collects what was actually searched so a failure
    can say where it looked.
    """
    wanted = name.strip().lower()

    local = _asset_collection(document, kind)
    if local is not None:
        tried.append("document assets")
        for index in range(1, int(local.Count) + 1):
            asset = local.Item(index)
            if str(asset.DisplayName).strip().lower() == wanted:
                return asset

    libraries = getattr(app, "AssetLibraries", None)
    if libraries is None:
        return None
    tried.append("asset libraries")
    for index in range(1, int(libraries.Count) + 1):
        library = libraries.Item(index)
        assets = _asset_collection(library, kind)
        if assets is None:
            continue
        for asset_index in range(1, int(assets.Count) + 1):
            asset = assets.Item(asset_index)
            if str(asset.DisplayName).strip().lower() == wanted:
                try:
                    return asset.CopyTo(document)
                except Exception:
                    return asset
    return None


def _find_parameter(parameters: Any, name: str) -> Any | None:  # pragma: no cover - Windows only
    try:
        return parameters.Item(name)
    except Exception:
        return None


def _find_feature(features: Any, name: str) -> Any:  # pragma: no cover - Windows only
    try:
        return features.Item(name)
    except Exception:
        available = ", ".join(str(features.Item(i).Name) for i in range(1, int(features.Count) + 1))
        raise FeatureError(
            f"No feature named {name!r}.", hint=f"Features in this part: {available or '(none)'}."
        ) from None


def _parameter_info(parameter: Any, kind: str = "user") -> ParamInfo:  # pragma: no cover
    units = str(parameter.Units)
    try:
        value = from_internal(float(parameter.Value), units)
    except Exception:
        value = float(parameter.Value)
    return ParamInfo(
        name=str(parameter.Name),
        expression=str(parameter.Expression),
        value=value,
        units=units,
        kind=kind,
        comment=str(getattr(parameter, "Comment", "") or ""),
    )


def _feature_info(feature: Any, kind: str, detail: dict[str, Any]) -> FeatureInfo:  # pragma: no cover
    return FeatureInfo(
        id=f"feat:{feature.Name}",
        name=str(feature.Name),
        kind=kind,
        suppressed=bool(getattr(feature, "Suppressed", False)),
        detail={key: value for key, value in detail.items() if value is not None},
    )


def _document_kind(document: Any) -> str:  # pragma: no cover - Windows only
    mapping = {12290: "part", 12291: "assembly", 12292: "drawing", 12293: "presentation"}
    return mapping.get(int(document.DocumentType), "unknown")


def _feature_kind(feature: Any) -> str:  # pragma: no cover - Windows only
    return str(type(feature).__name__).replace("Feature", "").lower() or "feature"


#: Constraint kinds Inventor infers on its own while geometry is created.
_INFERRED_KINDS = {"coincident", "horizontal", "vertical", "tangent"}

#: Kinds that join geometry into a closed loop.  Without one of these there is
#: no profile and the sketch is useless, so a failure is fatal.  Every other
#: kind refines a sketch that already closes.
_STRUCTURAL_KINDS = {"coincident"}


def _already_constrained(sketch: Any, kind: str, targets: list[Any]) -> bool:  # pragma: no cover
    """True when the sketch already carries this exact constraint.

    Used only after a failed call, to tell "Inventor beat us to it" apart from
    "this constraint could not be applied" -- which look identical from the
    return code but mean opposite things for the resulting sketch.
    """
    if kind not in _INFERRED_KINDS:
        return False
    try:
        constraints = sketch.GeometricConstraints
        total = int(constraints.Count)
    except Exception:
        return False

    wanted = [target for target in targets if target is not None]
    for index in range(1, total + 1):
        try:
            existing = constraints.Item(index)
        except Exception:
            continue
        entities = [
            getattr(existing, name, None) for name in ("EntityOne", "EntityTwo", "Entity", "Line")
        ]
        entities = [entity for entity in entities if entity is not None]
        if len(entities) < len(wanted):
            continue
        if all(any(_same_com_object(entity, target) for entity in entities) for target in wanted):
            return True
    return False


def _is_structural(constraint: Any, groups: dict[tuple[str, str], Any]) -> bool:
    """True when the geometry was built to satisfy this constraint already."""
    if constraint.kind != "coincident" or len(constraint.refs) != 2:
        return False
    first, second = constraint.refs
    keys = [(ref.entity, ref.point.value) for ref in (first, second)]
    return all(key in groups for key in keys) and groups[keys[0]] == groups[keys[1]]


def _set_radius_expression(feature: Any, expression: str) -> bool:  # pragma: no cover
    """Put the driving expression back on a fillet created from a number."""
    for getter in (
        lambda: feature.FilletEdgeSets.Item(1).Radius,
        lambda: feature.Radius,
    ):
        try:
            getter().Expression = expression
            return True
        except Exception:
            continue
    return False


def _call_by_keyword(method: Any, keyword_sets: list[dict[str, Any]],
                     positional: tuple[Any, ...]) -> Any:  # pragma: no cover - Windows only
    """Call *method* by name where possible, falling back to positional.

    Argument order in Inventor's hole methods is not what it reads like, and
    getting it wrong puts a string where an enum belongs. Naming the arguments
    removes the guesswork; late-bound dispatch does not accept keywords, so a
    positional call stands behind it.
    """
    errors: list[str] = []
    for keywords in keyword_sets:
        try:
            return method(**keywords)
        except TypeError as exc:  # unknown or missing parameter name
            errors.append(str(exc))
    return method(*positional)


def _distinct(*objects: Any) -> list[Any]:
    """The given objects with duplicates dropped, preserving order."""
    unique: list[Any] = []
    for obj in objects:
        if obj is not None and not any(obj is seen for seen in unique):
            unique.append(obj)
    return unique


def _describe_sketch(sketch: Any) -> str:  # pragma: no cover - Windows only
    """A census of what Inventor thinks is in the sketch.

    Reported when a profile cannot be built, because "no closed loop" and
    "everything got marked as construction" look identical from the outside.
    """
    parts: list[str] = []
    for name in ("SketchLines", "SketchArcs", "SketchCircles", "SketchEllipses", "SketchPoints"):
        collection = getattr(sketch, name, None)
        if collection is None:
            continue
        try:
            total = int(collection.Count)
        except Exception:
            continue
        if not total:
            continue
        construction = 0
        for index in range(1, total + 1):
            try:
                if bool(collection.Item(index).Construction):
                    construction += 1
            except Exception:
                pass
        label = name.replace("Sketch", "").lower()
        parts.append(f"{total} {label}" + (f" ({construction} construction)" if construction else ""))
    return ", ".join(parts) or "no geometry at all"


def _count_profiles(sketch: Any) -> int:  # pragma: no cover - Windows only
    try:
        profile = sketch.Profiles.AddForSolid(True)
    except Exception as exc:
        # Not fatal here -- a sketch of hole centres has no profile by design --
        # but worth saying out loud, since a silent zero looks like the same thing.
        logger.info("Sketch %s: no profile available (%s); contains %s",
                    getattr(sketch, "Name", "?"), _com_message(exc), _describe_sketch(sketch))
        return 0
    count_ = int(profile.Count)
    try:
        profile.Delete()
    except Exception:
        pass
    return count_


def _fully_constrained(sketch: Any) -> bool | None:  # pragma: no cover - Windows only
    """``None`` when this Inventor version does not expose the flag."""
    for name in ("FullyConstrained", "IsFullyConstrained"):
        value = getattr(sketch, name, None)
        if isinstance(value, bool):
            return value
    return None


def _sketch_plane_name(sketch: Any) -> str:  # pragma: no cover - Windows only
    try:
        return str(sketch.PlanarEntity.Name)
    except Exception:
        return "unknown"


def _curve_type(edge: Any) -> str:  # pragma: no cover - Windows only
    try:
        name = str(type(edge.Geometry).__name__).lower()
    except Exception:
        return "unknown"
    if "line" in name:
        return "linear"
    if "circle" in name or "arc" in name:
        return "circular"
    if "ellipse" in name:
        return "elliptical"
    return "spline"


def _surface_type(face: Any) -> str:  # pragma: no cover - Windows only
    try:
        name = str(type(face.Geometry).__name__).lower()
    except Exception:
        return "unknown"
    for key in ("plane", "cylinder", "cone", "sphere", "torus"):
        if key in name:
            return {"plane": "planar", "cylinder": "cylindrical"}.get(key, key)
    return "spline"


def _face_normal(face: Any) -> tuple[float, float, float] | None:  # pragma: no cover
    """The outward normal of a planar face.

    Read from the surface geometry, which is a plain property, rather than
    through the evaluator's parameter round-trip -- that returned nothing
    usable and took the top/bottom face filters down with it.  Curved faces
    have no single normal, so they get ``None``.
    """
    try:
        normal = face.Geometry.Normal
        vector = (float(normal.X), float(normal.Y), float(normal.Z))
    except Exception:
        return None
    try:
        if bool(face.IsParamReversed):
            vector = (-vector[0], -vector[1], -vector[2])
    except Exception:
        pass
    return vector


def _edge_length(edge: Any) -> float | None:  # pragma: no cover - Windows only
    """Length of an edge, in centimetres.

    The curve evaluator's parameter extents were returning nothing usable, so
    the geometry is measured directly: vertex to vertex for a line, and the
    circumference for a full circle.
    """
    try:
        evaluator = edge.Evaluator
        extents = evaluator.GetParamExtents()
        if isinstance(extents, (tuple, list)) and len(extents) >= 2:
            length = evaluator.GetLengthAtParam(float(extents[0]), float(extents[1]))
            if isinstance(length, (tuple, list)):
                length = length[-1]
            if length:
                return float(length)
    except Exception:
        pass

    try:
        start, stop = edge.StartVertex.Point, edge.StopVertex.Point
        return math.dist(
            (float(start.X), float(start.Y), float(start.Z)),
            (float(stop.X), float(stop.Y), float(stop.Z)),
        ) or None
    except Exception:
        pass

    try:  # a closed circle has no distinct vertices
        return 2 * math.pi * float(edge.Geometry.Radius)
    except Exception:
        return None


def _face_point(face: Any) -> tuple[float, float, float] | None:  # pragma: no cover
    """A point Inventor guarantees lies on the face, not merely near it."""
    try:
        point = face.PointOnFace
        return (float(point.X), float(point.Y), float(point.Z))
    except Exception:
        return None


def _cross(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _edge_uses(edge: Any) -> list[Any] | None:  # pragma: no cover - Windows only
    """The edge's two uses, one per adjacent face, or None if unavailable.

    makepy generates no module for ``EdgeUse``, but late binding asks the
    object rather than the wrapper, so this can still work where the generated
    signature suggests it cannot.
    """
    try:
        uses = edge.EdgeUses
        return [uses.Item(index) for index in range(1, int(uses.Count) + 1)]
    except Exception:
        return None


def _use_face(use: Any) -> Any | None:  # pragma: no cover - Windows only
    """The face an edge use belongs to, whichever way round this release says it."""
    for path in ("Face", "EdgeUseLoop.Face", "Parent.Face"):
        target = use
        try:
            for step in path.split("."):
                target = getattr(target, step)
        except Exception:
            continue
        if target is not None:
            return target
    return None


def _convexity_from_loops(edge: Any) -> str | None:  # pragma: no cover - Windows only
    """Convexity from the orientation of the faces' boundary loops.

    A face's boundary runs anticlockwise about its outward normal, so the
    face's material lies to the left of the loop direction -- which is
    ``normal x tangent``.  If that direction points *into* the neighbouring
    face's outward normal the two faces close over the material and the edge
    is an inside corner; if it points away, an outside one.

    This is exact rather than sampled, and it is asked of both faces so that a
    disagreement is reported as "don't know" instead of a coin toss.
    """
    uses = _edge_uses(edge)
    if uses is None or len(uses) != 2:
        return None
    tangent = _edge_direction(edge)
    if tangent is None:  # a circle has no single direction
        return None

    verdicts = set()
    for index, use in enumerate(uses):
        face, other = _use_face(use), _use_face(uses[1 - index])
        if face is None or other is None:
            return None
        normal, other_normal = _face_normal(face), _face_normal(other)
        if normal is None or other_normal is None:
            return None
        try:
            reversed_ = bool(use.IsParamReversed)
        except Exception:
            return None
        forward = tuple(-c for c in tangent) if reversed_ else tangent
        alignment = sum(a * b for a, b in zip(_cross(normal, forward), other_normal))
        if abs(alignment) < 1e-9:  # tangent faces meet smoothly
            return None
        verdicts.add("concave" if alignment > 0 else "convex")

    if len(verdicts) != 1:
        logger.debug("The two edge uses disagree about convexity; leaving it unknown.")
        return None
    return verdicts.pop()


def _edge_convexity(edge: Any, _unused: Any = None) -> str | None:  # pragma: no cover
    """Whether an edge is an outside corner or an inside one.

    Decided from the boundary loops where the release exposes them, because
    that is exact.  Otherwise fall back to sampling: for each of the two
    adjacent faces, take the direction from the edge towards a point on that
    face and test it against the *other* face's outward normal.

    Sampling is only as good as the sample.  ``Face.PointOnFace`` returns an
    arbitrary interior point, and on a face with an inner loop -- the underside
    of a plate with a pocket in it -- that point can lie on the far side of the
    edge and invert the answer.  On the angle bracket it put two of the eight
    slot-opening edges in the concave set and the other six in the convex one,
    for geometry that is identical by symmetry.
    """
    decided = _convexity_from_loops(edge)
    if decided is not None:
        return decided
    return _convexity_from_samples(edge)


def _convexity_from_samples(edge: Any) -> str | None:  # pragma: no cover
    """The fallback: which side of the edge a sampled point on each face is on.

    Local to the edge, which matters -- an earlier version compared against the
    body's centre and got an L-section wrong, because the centre of a
    re-entrant part's bounding box is not inside the material -- but only as
    reliable as ``Face.PointOnFace``.  See ``_edge_convexity``.
    """
    try:
        faces = edge.Faces
        if int(faces.Count) != 2:
            return None
        first, second = faces.Item(1), faces.Item(2)
        box = edge.Evaluator.RangeBox
        on_edge = (
            (float(box.MinPoint.X) + float(box.MaxPoint.X)) / 2,
            (float(box.MinPoint.Y) + float(box.MaxPoint.Y)) / 2,
            (float(box.MinPoint.Z) + float(box.MaxPoint.Z)) / 2,
        )
    except Exception:
        return None

    normals = (_face_normal(first), _face_normal(second))
    points = (_face_point(first), _face_point(second))
    if any(item is None for item in normals + points):
        return None

    alignment = 0.0
    for point, other_normal in ((points[0], normals[1]), (points[1], normals[0])):
        towards = [a - b for a, b in zip(point, on_edge)]  # type: ignore[arg-type]
        length = math.sqrt(sum(c * c for c in towards)) or 1.0
        alignment += sum(a * b for a, b in zip(towards, other_normal)) / length  # type: ignore[arg-type]

    if abs(alignment) < 1e-6:
        return None
    return "concave" if alignment > 0 else "convex"


def _solid_volume(document: Any) -> float | None:
    """The current volume, or None if it cannot be measured.

    Used to tell "the feature built" from "the feature did something".
    Inventor reports success for a cut that meets no material, so success is
    not on its own evidence that anything happened.
    """
    try:
        return float(document.ComponentDefinition.MassProperties.Volume)
    except Exception:
        return None


def _removed_material(document: Any, before: float | None) -> bool:
    """Whether the part has got smaller since *before*.

    When the volume cannot be read the answer is True: an unmeasurable part
    must not make a feature that really worked look like a failure. The
    tolerance is a millionth of a cubic centimetre, well under any real cut
    and well over Inventor's own rounding.
    """
    if before is None:
        return True
    after = _solid_volume(document)
    if after is None:
        return True
    return before - after > 1e-6


def _delete_quietly(feature: Any) -> None:  # pragma: no cover
    """Undo a feature that built but did nothing, before trying another way."""
    try:
        feature.Delete()
    except Exception:
        logger.debug("Could not delete the no-op feature; it stays in the tree.")


def _edge_direction(edge: Any) -> tuple[float, float, float] | None:  # pragma: no cover
    try:
        geometry = edge.Geometry
        direction = geometry.Direction
        return (float(direction.X), float(direction.Y), float(direction.Z))
    except Exception:
        return None


def _com_passes_filter(info: TopoInfo, filter_name: str) -> bool:  # pragma: no cover
    """Whether a matched entity satisfies *filter_name*.

    A filter that cannot be evaluated returns False rather than True. Falling
    through to "yes" turns "the top face" into "every face", which is how a
    shell came to be handed all ten faces of a box to open.
    """
    if filter_name in ("convex", "concave"):
        return info.convexity == filter_name
    if filter_name in ("all", "largest", "smallest", "outer"):
        return True
    if filter_name in ("circular", "linear", "planar", "cylindrical", "elliptical"):
        return info.geometry == filter_name

    axis_map = {"top": (2, 1), "bottom": (2, -1), "front": (1, -1),
                "back": (1, 1), "right": (0, 1), "left": (0, -1)}
    if filter_name in axis_map:
        if info.normal is None:
            return False
        axis, sign = axis_map[filter_name]
        return info.normal[axis] * sign > 0.9

    if filter_name == "vertical":
        if info.kind == "face":
            return info.normal is not None and abs(info.normal[2]) < 0.1
        return info.direction is not None and abs(info.direction[2]) > 0.9
    if filter_name == "horizontal":
        if info.kind == "face":
            return info.normal is not None and abs(info.normal[2]) > 0.9
        return info.direction is not None and abs(info.direction[2]) < 0.1
    return True
