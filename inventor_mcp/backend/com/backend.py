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

import json
import logging
import math
import os
from contextlib import contextmanager
from itertools import count
from typing import Any, Callable, Iterator, Sequence

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
from ...expressions import referenced_parameters
from ...geometry import profile_loops
from ...plan import PArc, PCircle, PEllipse, PLine, PPoint, PText, PointRef, Ref, SketchPlan
from ...units import from_internal, inventor_symbol, unit_from_inventor
from ..base import (
    AppInfo,
    AxisSpec,
    Backend,
    ChamferRequest,
    CoilRequest,
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
    CombineRequest,
    DraftRequest,
    EmbossRequest,
    ShellRequest,
    SplitRequest,
    SketchInfo,
    SweepRequest,
    ThreadRequest,
    TopoInfo,
    WorkPlaneRequest,
)
from . import holes
from .constants import (
    BOOLEAN_OPERATIONS,
    DISPLAY_MODES,
    EXTENT_DIRECTIONS,
    SHELL_DIRECTIONS,
    TEXT_ALIGNMENT,
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


#: ``HealthStatusEnum`` values meaning "up to date, nothing to report", used only
#: when Inventor cannot be asked. This used to be ``{0, 15873}``, and 15873 is
#: Inventor 2027.1's ``kPartEdgeFilter`` -- a number from another enum entirely.
#: The result was a correct rebuild reported as three features in error: the
#: bracket widened from 90 to 120 mm and gained exactly the 9 cm^3 of base the
#: extra length implies, while the report said it was sick.
#:
#: So the value is now asked of the type library by name, and when that cannot be
#: done the statuses are reported as *uninterpreted* rather than as errors. A
#: number nobody can translate is not evidence of anything.
_HEALTHY_STATUS_NAMES = ("kUpToDateHealth",)

#: Status values seen on features that are demonstrably fine. This is a
#: measurement, not a table entry: Inventor 2027.1's type library contains no
#: HealthStatusEnum at all -- `dump_constants.py --find Health` returns nothing
#: and `--value 11778` names no enum -- so there is no name to ask for on this
#: release. What there is instead is evidence: seven holes, each just built and
#: each verified against its own geometry to four decimal places, every one of
#: them reporting 11778.
#:
#: That is enough to stop calling it an error. It is not enough to call it
#: "up to date" rather than, say, "up to date with a warning", so the value is
#: listed here as observed rather than translated, and re-checking it on another
#: release means re-running the probe.
_OBSERVED_HEALTHY = {11778}


#: Sketch planes whose first axis runs opposite to the model axis they are
#: named after.  Measured on Inventor 2027.1: a profile drawn from 0 to 90 in
#: sketch X on the XZ plane comes out spanning -90 to 0 in model X.  Only used
#: when the sketch's axes cannot be measured -- see ``_orientation_matrix``.
_MIRRORED_PLANES = {"xz"}

#: What a recipe's (u, v) mean for a plane facing each model axis: the axes
#: named in the plane's own name, in that order.  Keyed by normal rather than
#: by name so that an offset work plane, and a sketch on an axis-aligned face,
#: follow the same rule as `xy` / `xz` / `yz` do.  tests/test_planes.py asserts
#: this is the same convention the simulator's ``map3d`` implements.
_RECIPE_AXES: dict[int, tuple[tuple[float, ...], tuple[float, ...]]] = {
    0: ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),  # normal along X -> the YZ plane
    1: ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),  # normal along Y -> the XZ plane
    2: ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),  # normal along Z -> the XY plane
}

_IDENTITY = (1.0, 0.0, 0.0, 1.0)

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


#: kOverConstrainedConstraintStatus, from Inventor's own ConstraintStatusEnum.
_OVER_CONSTRAINED = 51715


def _over_constrained(sketch: Any) -> bool:  # pragma: no cover - Windows only
    """Whether the sketch, as last solved, is over-constrained."""
    try:
        return int(sketch.ConstraintStatus) == _OVER_CONSTRAINED
    except Exception:
        return False


def _dimension_count(sketch: Any) -> int:  # pragma: no cover - Windows only
    """How many dimension constraints the sketch holds, or -1 if it will not say."""
    try:
        return int(sketch.DimensionConstraints.Count)
    except Exception:
        return -1


def _undimension(sketch: Any, established: int) -> bool:  # pragma: no cover - Windows only
    """Delete the dimensions added since *established*.  True if the sketch recovered."""
    if established < 0:
        return False
    try:
        dimensions = sketch.DimensionConstraints
        while int(dimensions.Count) > established:
            dimensions.Item(int(dimensions.Count)).Delete()
    except Exception:
        return False
    _solve(sketch)
    return not _over_constrained(sketch)


def _solve(sketch: Any) -> None:  # pragma: no cover - Windows only
    """Make Inventor solve the sketch now, so the next dimension is judged live.

    Inventor only spots a redundant dimension against a solved sketch.  Left
    unsolved it takes every dimension without complaint, and the sketch then
    refuses to give any feature a profile -- ``ExtrudeFeatures.Add`` raises a
    bare "Exception occurred." with nothing in the ErrorManager to explain it.
    Solving between generated dimensions turns that into an ordinary refusal
    that ``refused_dimensions`` can report.
    """
    try:
        sketch.Solve()
    except Exception:
        pass


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


def _dynamic(value: Any) -> Any:  # pragma: no cover - Windows only
    """*value* through dynamic dispatch, so every member resolves by name.

    "Late binding by default" turns out to be a default pywin32 quietly
    overrides: once a makepy cache exists for the type library -- and running
    anything early-bound once creates it -- plain ``Dispatch`` hands back the
    generated wrappers forever after. Those take interface declarations
    literally, so ``Features.Item()`` is a generic ``PartFeature`` with no
    ``Thickness``, no ``TaperAngle`` and no ``Definition``, and a property read
    through it reports nothing.

    Measured on the machine this serves: ``describe_feature`` on a live shell
    returned only ``HealthStatus`` and ``Suppressed``, so role discovery --
    which reads the shell's thickness expression for evidence -- found no roles
    at all and fell back to offering names. Through dynamic dispatch the same
    reads work regardless of what the cache holds.
    """
    if win32com is None:
        return value
    try:
        return win32com.client.dynamic.Dispatch(value._oleobj_)
    except Exception:
        return value


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
        self._transactions: dict[str, Any] = {}
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
            # Asked, because it is not always a part: a multi-body STEP can open
            # as an assembly, and calling that "part" in the same result whose
            # detail says "assembly" is a contradiction somebody has to notice.
            kind=_document_kind(document),
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
        # Inventor hands back the already-open document for a file it has open,
        # and minting a second handle for it split the session's knowledge in
        # two: the new context had no recipe, no plans and no freeze guard, so
        # reopening a protected part produced an unprotected handle to the same
        # geometry. Matched by file name, because wrapper identity is useless
        # under late binding -- each call returns a fresh wrapper.
        # realpath as well as normcase: an 8.3 short name, a junction or a
        # subst'd drive letter is the same file, and Inventor reports the long
        # canonical form -- matching the alias against it minted a second
        # handle for a document that was already open.
        opened = os.path.normcase(os.path.realpath(path))
        for known_id, known in self._documents.items():
            try:
                held = os.path.normcase(str(known.FullFileName))
            except Exception:
                continue
            if held and held == opened:
                self._documents[known_id] = document
                length, angle, read = self._document_units(document)
                info = DocInfo(
                    id=known_id,
                    name=str(document.DisplayName),
                    path=str(document.FullFileName) or None,
                    kind=_document_kind(document),
                    units=length,
                    angle_units=angle,
                    active=True,
                )
                if not read:
                    # The same caveat a first open carries. The shortcut dropped
                    # it, so a reopen reported millimetres as though they had
                    # been measured rather than assumed.
                    info.detail = {"units_note": (
                        "This part would not say what units it is in, so it is "
                        "being treated as millimetres and degrees."
                    )}
                return info
        # Asked rather than assumed. This used to register every opened document
        # as millimetres and degrees, which is right for most parts and 25.4
        # times wrong for an inch-authored one -- and wrong in the direction
        # where a bare number in a later edit builds something a fortieth of the
        # size it should be.
        length, angle, read = self._document_units(document)
        info = self._register(document, length, angle)
        if not read:
            info.detail = dict(info.detail or {}, units_note=(
                "This part would not say what units it is in, so it is being "
                "treated as millimetres and degrees. Values sent from here always "
                "carry their own unit, so expressions are safe either way; a bare "
                "number in a later edit is what to be careful of."
            ))
        return info

    def _document_units(self, document: Any) -> tuple[str, str, bool]:  # pragma: no cover
        """What units this document is actually in, and whether it said.

        "Said" means *both* answered. One flag shared between the two let an
        angle that read fine vouch for a length that did not, and the unreadable
        length is the one that matters -- it is what a bare number in a later
        edit gets multiplied by.
        """
        length, angle = "mm", "deg"
        found = turned = None
        try:
            measure = document.UnitsOfMeasure
            found = unit_from_inventor(measure.GetStringFromType(measure.LengthUnits))
            turned = unit_from_inventor(measure.GetStringFromType(measure.AngleUnits))
        except Exception:
            return length, angle, False
        if found is not None:
            length = found
        if turned is not None:
            angle = turned
        return length, angle, found is not None and turned is not None

    #: Extensions Inventor reads through a translator rather than opening as its
    #: own file. A translated file carries geometry and not the history that made
    #: it, so what arrives has a solid body and no parameters.
    IMPORT_EXTENSIONS = {
        ".stp": "STEP", ".step": "STEP", ".igs": "IGES", ".iges": "IGES",
        ".sat": "ACIS", ".sab": "ACIS", ".x_t": "Parasolid", ".x_b": "Parasolid",
        ".jt": "JT", ".stpz": "STEP",
    }

    #: Inventor's STEP translator add-in, for the route that needs naming it.
    _STEP_TRANSLATOR = "{90AF7F40-0C01-11D5-8E83-0010B541CD80}"

    def import_geometry(self, path: str, *, name: str | None = None) -> DocInfo:  # pragma: no cover - Windows only
        """Read a translated file into a part, by whichever route this release takes.

        Three are tried, cheapest first, and the one that worked is reported
        rather than assumed -- the same discipline the pattern and sweep features
        needed, for the same reason: several routes are documented, they are not
        all present in every release, and a wrong guess here fails in a way that
        looks like the file being bad.

        1. ``Documents.Open``. Inventor's own file dialog accepts a .stp, and
           where this works it is the whole job. It can also produce an
           *assembly* from a multi-body STEP, which is reported rather than
           quietly analysed as though it were one part.
        2. ``ImportedComponents``, the associative route added in 2017. Needs a
           part to import into, so one is made -- and thrown away again if the
           import does not take, rather than left behind empty.
        3. The STEP translator add-in, which is how this was done before
           ``ImportedComponents`` existed.

        See ``scripts/probe_import_and_properties.py``, which is how to find out
        what this machine actually does before trusting any of it.
        """
        app = self._require_app()
        if not os.path.exists(path):
            raise DocumentError(f"No such file: {path}")
        kind = self.IMPORT_EXTENSIONS.get(os.path.splitext(path)[1].lower())

        # A file already imported comes back as the document it became -- the
        # same hole open_document had, still open for translated files, and
        # matched by the SOURCE path this time: a part made by
        # ImportedComponents is unsaved, so it has no FullFileName to match.
        source = os.path.normcase(os.path.realpath(path))
        remembered = getattr(self, "_imported_from", {})
        known_id = remembered.get(source)
        document = None
        if known_id is not None and known_id in self._documents:
            document = self._documents[known_id]
            try:
                # Activated, because the result is about to say active=True --
                # and asked at all because the user closing this document in
                # Inventor's own UI leaves the memo pointing at a dead COM
                # object. A stale memo falls through to a fresh import rather
                # than handing back a handle every later call would die on.
                document.Activate()
            except Exception:
                self._documents.pop(known_id, None)
                remembered.pop(source, None)
                document = None
        if document is not None:
            length, angle, _ = self._document_units(document)
            info = DocInfo(
                id=known_id, name=str(document.DisplayName),
                path=str(document.FullFileName) or None,
                kind=_document_kind(document), units=length, angle_units=angle,
                active=True,
            )
            info.detail = {"imported": True, "already_open": True,
                           "from": source}
            return info

        tried: list[str] = []

        def by_opening() -> Any:
            return _specialise(app.Documents.Open(path, True))

        def by_imported_component() -> Any:  # noqa: D401 - closure
            # Specialised BEFORE the ComponentDefinition read, not only on
            # return: under an early-bound cache Documents.Add hands back the
            # generic Document, which declares no ComponentDefinition, and the
            # read then failed -- silently demoting every import to the
            # file-writing Documents.Open route.
            holder = _specialise(app.Documents.Add(
                self._k("kPartDocumentObject"),
                app.FileManager.GetTemplateFile(self._k("kPartDocumentObject")),
                True,
            ))
            try:
                components = (holder.ComponentDefinition
                              .ReferenceComponents.ImportedComponents)
                definition = components.CreateDefinition(path)
                components.Add(definition)
            except Exception:
                # An empty part left open is worse than the failure itself: the
                # next call finds it as the active document and builds into it.
                try:
                    holder.Close(True)
                except Exception:
                    pass
                raise
            return holder

        def by_translator() -> Any:
            addin = app.ApplicationAddIns.ItemById(self._STEP_TRANSLATOR)
            # The add-in comes back as a generic ApplicationAddIn under a makepy
            # cache, which has no Open and no HasOpenOptions -- measured, and
            # exactly the trap _specialise exists for on documents.
            try:
                addin = win32com.client.CastTo(addin, "TranslatorAddIn")
            except Exception:
                addin = _dynamic(addin)
            if not bool(getattr(addin, "Activated", False)):
                addin.Activate()
            transients = app.TransientObjects
            medium = transients.CreateDataMedium()
            medium.FileName = path
            context = transients.CreateTranslationContext()
            context.Type = self._k("kFileBrowseIOMechanism")
            options = transients.CreateNameValueMap()
            try:
                addin.HasOpenOptions(medium, context, options)
            except Exception:
                pass
            return _specialise(addin.Open(medium, context, options))

        document = None
        route = None
        # ImportedComponents first, and the order is measured rather than
        # guessed. Documents.Open on a multi-body STEP produced an *assembly*,
        # and -- worse for a tool that promises not to touch what it was handed
        # -- it wrote a folder of translated .iam/.ipt files onto disk next to
        # the source. ImportedComponents put the same file into one fresh part
        # document and left the drive alone.
        for label, attempt in (
            ("ImportedComponents", by_imported_component),
            ("Documents.Open", by_opening),
            ("the STEP translator add-in", by_translator),
        ):
            try:
                document = attempt()
            except Exception as exc:
                tried.append(f"{label}: {self._explain(exc)}")
                continue
            if document is None:
                # Returning nothing without raising is a real outcome -- the
                # translator's Open hands its document back through an
                # out-parameter on some releases -- and a route that vanished
                # from the report would look like it was never tried.
                tried.append(f"{label}: returned nothing")
                continue
            route = label
            break

        if document is None:
            raise DocumentError(
                f"Inventor would not import {os.path.basename(path)}"
                + (f" as {kind}." if kind else "."),
                hint="Tried " + "; ".join(tried) + ". Run "
                     "scripts/probe_import_and_properties.py --step <file> to see "
                     "which route this release accepts.",
            )

        length, angle, _ = self._document_units(document)
        info = self._register(document, length, angle)
        remembered[source] = info.id
        self._imported_from = remembered
        info.detail = {
            "imported": True,
            "format": kind or "an unrecognised extension",
            "route": route,
            "rejected": tried,
            **self._what_arrived(info.id),
        }
        if route == "Documents.Open":
            info.detail["wrote_files"] = (
                "This route makes Inventor translate the file onto disk: expect "
                "a folder of .iam/.ipt files next to the source. Measured on "
                "2027.1; the ImportedComponents route, which avoids it, was "
                "refused first -- see `rejected`."
            )
        return info

    def _what_arrived(self, doc_id: str) -> dict[str, Any]:  # pragma: no cover - Windows only
        """What is actually in the imported document.

        Measured rather than assumed, and it decides what can be done next: the
        DFM loop drives parameters, so a count of zero is the whole answer about
        whether it can improve this part or only measure it.
        """
        out: dict[str, Any] = {}
        try:
            document = self._doc(doc_id)
        except Exception:
            return out
        out["document_kind"] = _document_kind(document)
        if out["document_kind"] != "part":
            out["note"] = (
                f"This came in as {out['document_kind']} rather than a part, which "
                f"a multi-body file will do. The analysis needs one part."
            )
            return out
        try:
            component = document.ComponentDefinition
        except Exception:
            return out
        for label, path in (("bodies", "SurfaceBodies"),
                            ("features", "Features"),
                            ("parameters", "Parameters.UserParameters")):
            target = component
            try:
                for step in path.split("."):
                    target = getattr(target, step)
                out[label] = int(target.Count)
            except Exception:
                out[label] = None
        if out.get("parameters") == 0:
            out["parametric"] = False
            out["note"] = (
                "Translated geometry, so there are no parameters to drive: this "
                "part can be measured for manufacturability and cannot be "
                "improved by the loop. Rebuild it as a recipe, or add the "
                "features you want to be able to change."
            )
        elif out.get("parameters") is None:
            # Could not be counted, which is not the same as counted and found.
            out["parametric"] = None
            out["note"] = ("The parameter count could not be read, so whether "
                           "this part can be driven is unknown here.")
        else:
            out["parametric"] = True
        return out

    # -- the declaration kept inside the document -------------------------

    #: The user-defined property set -- the one whose contents show under Custom
    #: in the iProperties dialog. Visible on purpose: somebody opening the part
    #: in Inventor should be able to see that something has been recorded about
    #: it, and what.
    _USER_PROPERTIES = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"

    #: The property name the declaration is stored under, plus a numbered suffix
    #: when it does not fit in one. Several of Inventor's string properties stop
    #: at 255 characters and a role map with a freeze list goes past that easily,
    #: so it is written in pieces rather than truncated -- a truncated freeze list
    #: is protection silently removed.
    _DECLARATION = "InventorMCP_DFM"
    _CHUNK = 200

    #: Property names a feature's shape may be driven through, read off the
    #: feature and its definition when tracing what a frozen feature depends on.
    #: A superset of _DESCRIBABLE's dimensioned entries on purpose: missing one
    #: here under-pins a frozen feature, which is protection that quietly is not.
    _DRIVING = (
        "Distance", "Depth", "Thickness", "Radius", "Angle", "TaperAngle",
        "Taper", "HoleDiameter", "CounterboreDiameter", "CounterboreDepth",
        "CountersinkDiameter", "CountersinkAngle", "SpotFaceDiameter",
        "SpotFaceDepth", "BottomTipAngle", "XSpacing", "YSpacing", "XCount",
        "YCount", "Count", "Spacing",
    )

    def promote_parameter(self, doc_id: str, feature: str, prop: str,
                          name: str) -> dict[str, Any]:  # pragma: no cover - Windows only
        document = self._doc(doc_id)
        held = _dynamic(_find_feature(document.ComponentDefinition.Features, feature))
        target = None
        read_from = None
        for holder in (held, getattr(held, "Definition", None)):
            if holder is None:
                continue
            holder = _dynamic(holder)
            for candidate in self._DRIVING:
                if candidate.lower().replace("_", "") != prop.lower().replace("_", ""):
                    continue
                try:
                    value = getattr(holder, candidate)
                except Exception:
                    continue
                if value is not None and hasattr(value, "Expression"):
                    target = value
                    read_from = candidate
                    break
            if target is not None:
                break
        if target is None:
            raise FeatureError(
                f"The feature {feature!r} has no drivable property {prop!r}.",
                hint="describe_feature lists what it carries.",
            )
        currently = str(target.Expression)
        # The new parameter holds exactly what the property held -- a literal
        # keeps its unit, a model-parameter reference keeps its reference -- so
        # the geometry after the promotion is the geometry before it.
        info = self.set_parameter(doc_id, name, currently)
        target.Expression = name
        return {
            "parameter": name,
            "value": info.value,
            "units": info.units,
            "was": currently,
            "now_drives": f"{feature}.{read_from}",
        }

    def feature_dependencies(self, doc_id: str, name: str) -> dict[str, Any] | None:  # pragma: no cover - Windows only
        document = self._doc(doc_id)
        feature = _dynamic(
            _find_feature(document.ComponentDefinition.Features, name))
        known = {
            info.name.lower(): info.name
            for info in self.list_parameters(doc_id)
        }
        via: dict[str, set[str]] = {}

        def note(expression: Any, where: str) -> None:
            if not isinstance(expression, str) or not expression.strip():
                return
            try:
                reads = referenced_parameters(expression)
            except Exception:
                return
            for read in reads:
                canonical = known.get(read.lower())
                if canonical is not None:
                    via.setdefault(canonical, set()).add(where)

        # 1. The feature's own driven properties, and its definition's.
        holders = [feature]
        definition = getattr(feature, "Definition", None)
        if definition is not None:
            holders.append(_dynamic(definition))
        for holder in holders:
            for attribute in self._DRIVING:
                try:
                    value = getattr(holder, attribute)
                except Exception:
                    continue
                note(getattr(value, "Expression", None), f"its {attribute}")

        # 2. Every parameter Inventor itself associates with the feature. This
        #    is the wide net: it includes the model parameters the feature
        #    consumes, whose expressions reference the user parameters.
        try:
            parameters = feature.Parameters
            for index in range(1, int(parameters.Count) + 1):
                parameter = parameters.Item(index)
                note(getattr(parameter, "Expression", None),
                     "a parameter Inventor associates with it")
                held = str(getattr(parameter, "Name", "") or "")
                if held.lower() in known:
                    via.setdefault(known[held.lower()], set()).add(
                        "a parameter Inventor associates with it")
        except Exception:
            pass

        # 3. The dimensions of the sketches it consumes, reached through its
        #    profile. Profile access differs per feature kind, so every route
        #    is tried and none is required.
        sketches = []
        for route in ("Profile", "Definition.Profile"):
            target = feature
            try:
                for step in route.split("."):
                    target = getattr(target, step)
                parent = getattr(target, "Parent", None)
                if parent is not None:
                    sketches.append(_dynamic(parent))
            except Exception:
                continue
        for sketch in sketches:
            label = str(getattr(sketch, "Name", "its sketch"))
            try:
                constraints = sketch.DimensionConstraints
                for index in range(1, int(constraints.Count) + 1):
                    dimension = constraints.Item(index)
                    parameter = getattr(dimension, "Parameter", None)
                    if parameter is not None:
                        note(getattr(parameter, "Expression", None),
                             f"a dimension of its sketch {label}")
            except Exception:
                continue

        return {
            "parameters": sorted(via, key=str.lower),
            "via": {parameter: sorted(where) for parameter, where in via.items()},
        }

    def document_path(self, doc_id: str) -> str | None:  # pragma: no cover - Windows only
        document = self._doc(doc_id)
        try:
            path = str(document.FullFileName)
        except Exception:
            return None
        return path or None

    def read_declaration(self, doc_id: str) -> dict[str, Any] | None:  # pragma: no cover - Windows only
        document = self._doc(doc_id)
        # Found, never created: `_property_set` adds the set when it is missing,
        # which is right for a write and wrong here -- a read that modifies the
        # document marks it dirty, and a dirty flag on a part nobody edited is a
        # save prompt nobody can explain.
        properties = self._find_property_set(document)
        if properties is None:
            return None
        pieces: list[str] = []
        for index in range(1, 100):
            name = self._DECLARATION if index == 1 else f"{self._DECLARATION}_{index}"
            try:
                value = properties.Item(name).Value
            except Exception:
                break
            if value is None:
                break
            pieces.append(str(value))
        if not pieces:
            return None
        text = "".join(pieces)
        try:
            loaded = json.loads(text)
        except ValueError:
            # Something is there and it is not what this wrote. The first
            # version returned a notes-only dict here, which downstream read as
            # a declaration with nothing frozen -- the one wrong default, since
            # somebody put that property there and it may be exactly the freeze
            # list that has been corrupted. Refusing is the honest answer.
            raise DocumentError(
                f"The {self._DECLARATION} property of this part holds "
                f"{len(text)} characters that are not a declaration this "
                f"project wrote, so what the part protects cannot be read.",
                hint="Look at the property in iProperties > Custom. Fix it, or "
                     "delete it and declare again with `declare_dfm` -- running "
                     "as though it were absent would ignore whatever it froze.",
            )
        return loaded if isinstance(loaded, dict) else None

    def write_declaration(self, doc_id: str, declaration: dict[str, Any]) -> None:  # pragma: no cover - Windows only
        document = self._doc(doc_id)
        text = json.dumps(declaration, separators=(",", ":"))
        chunks = [text[at:at + self._CHUNK] for at in range(0, len(text), self._CHUNK)] or [""]
        properties = self._property_set(document)
        for index, chunk in enumerate(chunks, start=1):
            name = self._DECLARATION if index == 1 else f"{self._DECLARATION}_{index}"
            try:
                properties.Item(name).Value = chunk
            except Exception:
                properties.Add(chunk, name)
        # A shorter declaration than last time leaves stale tail pieces, and
        # those would be read straight back and make the JSON unparseable.
        for index in range(len(chunks) + 1, len(chunks) + 20):
            name = f"{self._DECLARATION}_{index}"
            try:
                properties.Item(name).Delete()
            except Exception:
                break

    def _find_property_set(self, document: Any) -> Any | None:  # pragma: no cover - Windows only
        """The user-defined property set, or ``None`` -- never made here."""
        sets = document.PropertySets
        for key in (self._USER_PROPERTIES, "Inventor User Defined Properties"):
            try:
                return sets.Item(key)
            except Exception:
                continue
        return None

    def _property_set(self, document: Any) -> Any:  # pragma: no cover - Windows only
        """The user-defined property set, made if this part has not got one.

        For writes only. A read uses :meth:`_find_property_set`, because a read
        that creates the set modifies the document.
        """
        found = self._find_property_set(document)
        if found is not None:
            return found
        return document.PropertySets.Add("Inventor User Defined Properties")

    def list_documents(self) -> list[DocInfo]:  # pragma: no cover - Windows only
        app = self._require_app()
        results: list[DocInfo] = []
        for index in range(1, int(app.Documents.Count) + 1):
            document = _specialise(app.Documents.Item(index))
            # By COM identity, never by Python wrapper identity: every call to
            # Documents.Item mints a fresh wrapper, so `id(document)` matched
            # nothing, and every listing re-registered every open document under
            # a new handle -- the duplicate-handle bug open_document was cured
            # of, still running here and quietly undermining the cure.
            doc_id = None
            for held_id, held in self._documents.items():
                if _same_com_object(document, held):
                    doc_id = held_id
                    break
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
                    active=_same_com_object(document, app.ActiveDocument),
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
        try:
            if save:
                document.Save()
            document.Close(not save)
        finally:
            # Evicted even when Close raises: a document already closed in
            # Inventor's own UI dies on the Close call, and popping only after
            # success left the dead handle registered forever -- unevictable,
            # because the eviction was the very call that failed.
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

        plane = self._resolve_plane(document, plan.plane, plan.offset_expression)
        with self._batch(document):
            with self._translate_errors("Creating the sketch", SketchError):
                sketch = component.Sketches.Add(plane, False)
                if plan.name:
                    sketch.Name = plan.name

            # The sketch has to exist before its axes can be measured, and its
            # axes have to be known before any geometry goes in: a plane's
            # internal orientation is not derivable from its name, and guessing
            # it wrong puts the geometry somewhere else on the part without any
            # error to say so.
            measured = _sketch_axes(sketch, transient)
            orientation = _orientation_matrix(measured)
            if orientation is None and plan.plane.lower() in _MIRRORED_PLANES:
                orientation = (-1.0, 0.0, 0.0, 1.0)  # the measured XZ fallback
            if orientation is not None and orientation != _IDENTITY:
                plan = plan.reoriented(orientation)
            axes = _describe_orientation(measured, orientation)

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

            # The recipe's own dimensions go first and are required: a refusal
            # there means the part is not the one that was asked for. The
            # planner's own degree-of-freedom dimensions go second and are
            # optional, so an author's dimension claims its degree of freedom
            # before a generated one can spend it.
            driving: list[str] = []
            refused_dimensions: list[str] = []
            required = [d for d in plan.dimensions if not getattr(d, "optional", False)]
            optional = [d for d in plan.dimensions if getattr(d, "optional", False)]

            with self._translate_errors("Applying sketch dimensions", SketchError):
                for dimension in required:
                    outcome, note = self._add_dimension(
                        sketch, transient, objects, dimension)
                    if outcome == "applied":
                        driving.append(note)
                    else:
                        raise SketchError(
                            f"Inventor refused the dimension {note}.",
                            hint="It was asked for by the recipe, so the part cannot "
                            "be built without it. Check it is not already implied by "
                            "another dimension or constraint.",
                        )

            for dimension in optional:
                established = _dimension_count(sketch)
                try:
                    outcome, note = self._add_dimension(
                        sketch, transient, objects, dimension)
                except SketchError:
                    raise
                except Exception as exc:  # pragma: no cover - version-specific
                    outcome, note = "refused", f"{dimension.expression!r}: {exc}"
                # Inventor judges a dimension against a *solved* sketch, and an
                # unsolved one accepts a redundant dimension without complaint.
                # The sketch is then over-constrained, and hands out profiles
                # that no feature will take -- ExtrudeFeatures.Add raises a bare
                # "Exception occurred." with nothing in the ErrorManager to say
                # why.  So solve, and take a generated dimension straight back
                # out if it spent a degree of freedom that was already spent.
                _solve(sketch)
                if (outcome == "applied" and _over_constrained(sketch)
                        and _undimension(sketch, established)):
                    outcome = "refused"
                    note = f"{note} -- it would over-constrain the sketch"
                if outcome == "applied":
                    driving.append(note)
                else:
                    refused_dimensions.append(note)
            if refused_dimensions:
                logger.info("Sketch %s: Inventor refused %d dimension(s): %s",
                            sketch.Name, len(refused_dimensions),
                            "; ".join(refused_dimensions[:5]))

        self._sketches.setdefault(doc_id, {})[sketch.Name] = sketch
        profiles = _count_profiles(sketch)
        # Whether a refused constraint mattered is a question about the sketch
        # that came out, not about the constraint's kind. A coincidence Inventor
        # refuses is usually one it inferred for itself, and a sketch of hole
        # centres never had a profile to lose -- Inventor's own hole tool
        # populates from it happily. So ask the sketch instead of assuming: it
        # is broken only if the recipe drew a closed loop and no profile came
        # out of it.
        # Not `refused and ...`: a dimension the solver acted on can break a
        # loop just as a refused constraint can, so the question is asked of
        # the sketch either way.
        if profiles == 0 and profile_loops(plan):
            raise SketchError(
                f"Sketch {sketch.Name!r} has closed loops in the recipe but no "
                f"profile, after {len(refused)} refused constraint(s) and "
                f"{len(refused_dimensions)} refused dimension(s): "
                f"{'; '.join(refused[:2] + refused_dimensions[:2])}",
                hint="Its geometry is not joined. Check for coordinates that do "
                "not quite meet, or two entities Inventor considers already "
                "constrained to each other.",
            )
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
            driving_dimensions=len(driving),
            refused_dimensions=len(refused_dimensions),
            driven_parameters=_driven_parameters(plan, driving),
            undriven_expressions=list(plan.undriven_expressions),
            axes=axes,
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
                anchor("center", primitive.center), primitive.radius
            )
            remember(entity, "center", "CenterSketchPoint")
        elif isinstance(primitive, PArc):
            start = _polar(primitive.center, primitive.radius, primitive.start_angle)
            end = _polar(primitive.center, primitive.radius, primitive.end_angle)
            # AddByCenterStartEndPoint always sweeps counter-clockwise, so it
            # ignores the sign of the recipe's sweep.  An arc that runs backwards
            # (end_angle below start_angle) is therefore handed over back to
            # front, which traces the locus the recipe actually asked for -- get
            # this wrong and the arc bulges the other way, so a closed profile
            # self-intersects and the feature throws.  Swapping the remembered
            # attributes alongside keeps the plan's own "start" and "end"
            # pointing at the same corners for constraints.
            ends = [("start", start), ("end", end)]
            if primitive.end_angle < primitive.start_angle:
                ends.reverse()
            entity = sketch.SketchArcs.AddByCenterStartEndPoint(
                transient.CreatePoint2d(*primitive.center),
                anchor(*ends[0]),
                anchor(*ends[1]),
            )
            remember(entity, ends[0][0], "StartSketchPoint")
            remember(entity, ends[1][0], "EndSketchPoint")
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
        elif isinstance(primitive, PText):
            # Inventor owns the glyph outlines, so this is placed and styled
            # rather than constrained. The style override is how font, size and
            # weight travel -- FontSize is in database units, like everything else.
            escaped = (primitive.text.replace("&", "&amp;")
                       .replace("<", "&lt;").replace(">", "&gt;"))
            styled = (
                f'<StyleOverride Font="{primitive.font}" FontSize="{primitive.height}"'
                f'{" Bold=\"True\"" if primitive.bold else ""}'
                f'{" Italic=\"True\"" if primitive.italic else ""}'
                f">{escaped}</StyleOverride>"
            )
            try:
                # AddFitted takes exactly two arguments on this build -- passing a
                # rotation as a third is refused, so it is set as a property after.
                entity = sketch.TextBoxes.AddFitted(
                    transient.CreatePoint2d(*primitive.position), styled
                )
            except Exception as exc:
                raise SketchError(
                    f"Could not place the text {primitive.text!r}: {_com_message(exc)}",
                    hint=f"Font {primitive.font!r} must be installed on this machine.",
                ) from exc
            # Both are set after the fact: AddFitted takes neither, and Inventor
            # defaults to left-justified, which runs the text off the face.
            for prop, resolve in (
                ("HorizontalJustification",
                 lambda: self._k(TEXT_ALIGNMENT[primitive.align])),
                ("Rotation", lambda: primitive.rotation),
            ):
                if prop == "Rotation" and not primitive.rotation:
                    continue
                try:
                    setattr(entity, prop, resolve())
                except Exception:  # pragma: no cover - version-specific
                    logger.info("Could not set %s on text %s.", prop, primitive.id)
        elif isinstance(primitive, PPoint):
            # A standalone point can be the shared point of a group, which is
            # how a bolt circle's construction lines meet their hole centres.
            group = groups.get((primitive.id, PointRef.SELF.value))
            entity = shared.get(group) if group is not None else None
            if entity is None:
                entity = sketch.SketchPoints.Add(
                    transient.CreatePoint2d(*primitive.position), primitive.hole_center
                )
                if group is not None:
                    shared[group] = entity
            elif primitive.hole_center:
                try:
                    entity.HoleCenter = True
                except Exception as exc:  # pragma: no cover - version-specific
                    logger.info("Could not mark the shared point %s as a hole "
                                "centre: %s", primitive.id, _com_message(exc))
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

        # Everything else refines a sketch that is already closed. Inventor
        # sometimes rejects one as dependent on the constraints around it; that
        # leaves the sketch usable but with a degree of freedom still in it, so
        # it is reported rather than treated as fatal.
        logger.warning("Sketch %s: %s was refused (%s); the sketch keeps a degree of "
                       "freedom.", sketch.Name, where, self._explain(first_error))
        return ("refused", where)

    def _add_dimension(self, sketch: Any, transient: Any, objects: dict[str, Any],
                       dimension: Any) -> tuple[str, str]:  # pragma: no cover
        """Apply one driving dimension, reporting whether Inventor took it.

        Parallel to :meth:`_add_constraint`.  A dimension the planner added to
        remove a degree of freedom can be refused as redundant -- Inventor does
        that readily, as the polygon's closing equality shows -- and refusing
        one leaves the sketch exactly as it was before the dimension existed,
        which is survivable.  It used to raise, so a single redundant dimension
        killed the whole sketch; that is why polyline profiles carried none at
        all and could not be revised.

        An unsupported kind or a reference to an entity that was never created
        still raises: those are bugs in the planner, not Inventor's judgement.
        """
        dimensions = sketch.DimensionConstraints
        targets = [self._entity(sketch, objects, ref) for ref in dimension.refs]
        text = transient.CreatePoint2d(*_text_point(dimension))
        where = f"{dimension.kind} {dimension.expression!r}"

        try:
            created = self._create_dimension(dimensions, dimension, targets, text)
        except SketchError:
            raise
        except Exception as exc:
            return ("refused", f"{where}: {self._explain(exc)}")

        # The expression -- not the number -- is what makes the model
        # parametric. A dimension standing with a frozen number has consumed
        # the degree of freedom and drives nothing, which is worse than not
        # having it at all, so it is taken back out.
        try:
            created.Parameter.Expression = dimension.expression
            if dimension.name:
                created.Parameter.Name = dimension.name
        except Exception as exc:
            note = f"{where}: Inventor would not store the expression ({self._explain(exc)})"
            try:
                created.Delete()
            except Exception as removal:
                raise SketchError(
                    f"Dimension {where} was created but its expression could not be "
                    f"stored, and it could not be removed either ({removal}). The "
                    "sketch now holds a frozen number where a parameter should be.",
                    hint="Rebuild this sketch; the model is not parametric as it "
                    "stands.",
                ) from exc
            return ("refused", note)
        return ("applied", where)

    def _create_dimension(self, dimensions: Any, dimension: Any, targets: list[Any],
                          text: Any) -> Any:  # pragma: no cover
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
        return created

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
            if request.bodies:
                _aim_at_bodies(self._require_app(), document.ComponentDefinition,
                               definition, request.bodies)
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

    def coil(self, doc_id: str, request: CoilRequest) -> FeatureInfo:  # pragma: no cover
        """A helical sweep: springs, threads, and a drill's flutes.

        Inventor exposes the extent three ways and the recipe gives two of the
        three, so the matching call is chosen rather than converted -- pitch and
        height stays pitch and height, and Inventor does its own arithmetic.

        The trailing options are optional-with-a-default, which pywin32 sends as
        a missing variant that Inventor sometimes rejects; so they are passed
        explicitly first and dropped only if that fails, the same fallback the
        fillet uses.
        """
        document = self._doc(doc_id)
        sketch = self._sketch(doc_id, request.sketch)
        axis = self._resolve_axis(doc_id, request.axis)
        features = document.ComponentDefinition.Features.CoilFeatures
        operation = self._k(BOOLEAN_OPERATIONS[request.operation])
        before = _solid_volume(document) if request.operation == "cut" else None

        with self._batch(document), self._translate_errors("Coil"):
            profile = self._profiles(sketch, request.profiles)
            taper = request.taper.expression if request.taper else "0 deg"
            if request.spiral:
                calls = [("AddSpiral", (profile, axis, request.pitch.expression,
                                        request.revolutions.expression))]
            elif request.pitch is not None and request.height is not None:
                calls = [("AddByPitchAndHeight", (profile, axis,
                                                  request.pitch.expression,
                                                  request.height.expression))]
            elif request.pitch is not None:
                calls = [("AddByPitchAndRevolution", (profile, axis,
                                                      request.pitch.expression,
                                                      request.revolutions.expression))]
            else:
                calls = [("AddByRevolutionAndHeight", (profile, axis,
                                                       request.revolutions.expression,
                                                       request.height.expression))]

            failures: list[str] = []
            feature = None
            for name, head in calls:
                method = getattr(features, name, None)
                if method is None:
                    failures.append(f"{name} is not available on this build")
                    continue
                tails = ([(operation, request.reverse_axis, request.clockwise)]
                         if request.spiral else
                         [(operation, request.reverse_axis, request.clockwise, taper),
                          (operation, request.reverse_axis, request.clockwise),
                          (operation,)])
                for tail in tails:
                    try:
                        feature = method(*head, *tail)
                    except Exception as exc:
                        failures.append("%s with %d options: %s"
                                        % (name, len(tail), _com_message(exc)))
                        continue
                    break
                if feature is not None:
                    break
            if feature is None:
                raise FeatureError(
                    f"Could not create the coil: {self._explain_text(failures[0])}",
                    hint="A coil's profile must not touch or cross its axis, and "
                    "consecutive turns must not run into each other -- a pitch "
                    "smaller than the profile is the usual cause. " +
                    "; ".join(failures[:3]),
                )
            if request.operation == "cut" and not _removed_material(document, before):
                _delete_quietly(feature)
                raise FeatureError(
                    f"The coil cut from sketch {request.sketch!r} removed no material.",
                    hint="Its helix does not pass through the part. Check the axis, "
                    "the profile's distance from it, and the height.",
                )
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "coil", {
            "sketch": request.sketch,
            "axis": request.axis.value,
            "operation": request.operation,
            "pitch": request.pitch.as_dict() if request.pitch else None,
            "height": request.height.as_dict() if request.height else None,
            "revolutions": request.revolutions.as_dict() if request.revolutions else None,
        })

    def sweep(self, doc_id: str, request: SweepRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        profile_sketch = self._sketch(doc_id, request.profile_sketch)
        path_sketch = self._sketch(doc_id, request.path_sketch)
        features = document.ComponentDefinition.Features.SweepFeatures
        with self._batch(document), self._translate_errors("Sweep"):
            profile = profile_sketch.Profiles.AddForSolid()
            path, route = self._sweep_path(document, path_sketch)
            feature = features.AddUsingPath(
                profile, path, self._k(BOOLEAN_OPERATIONS[request.operation])
            )
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "sweep", {"path": request.path_sketch,
                                                "path_from": route})

    def _sweep_path(self, document: Any, sketch: Any) -> tuple[Any, str]:  # pragma: no cover
        """A ``Path`` object for a sweep, measured rather than guessed.

        ``AddUsingPath`` wants a ``Path``, and ``Features.CreatePath`` is the only
        thing that makes one. The obvious-looking alternative,
        ``Profiles.AddForSurface``, returns a ``Profile`` -- which the sweep
        rejects with "Type mismatch", measured on 2027.1. It used to be the
        fallback here and could never have worked, so it is gone: a fallback that
        is known to be wrong only adds a second confusing error to the first.

        The curve matters too. ``SketchEntities.Item(1)`` is not reliably one --
        this sketch of "an arc" holds the arc and three points -- so
        :func:`_first_curve` picks the geometry rather than whatever is first.
        """
        first = _first_curve(sketch)
        try:
            return document.ComponentDefinition.Features.CreatePath(first), "CreatePath"
        except Exception as exc:
            raise FeatureError(
                f"Could not make a path out of sketch {sketch.Name!r}: "
                f"{self._explain(exc)}",
                hint="A sweep path must be a single chain of connected curves, and "
                "the profile must sit on a plane perpendicular to it at one end. "
                "`scripts/probe_sweep_and_pattern.py` tries the alternatives.",
            ) from exc

    def loft(self, doc_id: str, request: LoftRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        app = self._require_app()
        features = document.ComponentDefinition.Features.LoftFeatures
        with self._batch(document), self._translate_errors("Loft"):
            # The sections go in *before* the definition is made. Creating it
            # from an empty collection and adding to `definition.Sections`
            # afterwards is the obvious reading of the API and does not work:
            # the loft failed with a bare "Exception occurred". The collection is
            # the definition's input, not a container it hands back.
            sections = app.TransientObjects.CreateObjectCollection()
            for name in request.sketches:
                sections.Add(self._sketch(doc_id, name).Profiles.AddForSolid())
            if int(sections.Count) < 2:
                raise FeatureError(
                    f"A loft needs at least two sections; {int(sections.Count)} "
                    "closed profile(s) were found.",
                    hint="Each sketch named in `sketches` must have one closed loop.",
                )
            definition = features.CreateLoftDefinition(
                sections, self._k(BOOLEAN_OPERATIONS[request.operation]))
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

        # Which way to drill, in the recipe's terms: along the sketch plane's
        # own normal, or against it.  "auto" is the usual case -- a hole placed
        # on a face is drilled into the part -- and the backend can see which
        # side that is, which the author should not have to work out.
        axes = _sketch_axes(sketch, app.TransientGeometry)
        normal = _cross(*axes) if axes else None
        along_normal, why = _drilling_side(request.direction, document, sketch, normal)
        extent = self._k(_HOLE_ALONG_NORMAL if along_normal else _HOLE_AGAINST_NORMAL)

        before = _solid_volume(document)
        notes: list[str] = []
        with self._batch(document), self._translate_errors("Hole"):
            placement = features.CreateSketchPlacementDefinition(centers)

            # A tap goes in where the diameter would: Inventor takes the drill
            # size from its own thread table, so the recipe's diameter stops
            # governing the bore and becomes a claim to check afterwards.
            size: Any = request.diameter.expression
            if request.tap:
                try:
                    size = holes.tap_info(features, request)
                except Exception as exc:
                    raise FeatureError(
                        f"Could not set up the {request.tap!r} thread: "
                        f"{self._explain_text(_com_message(exc))}",
                        hint="Inventor looks the designation up in its own thread "
                        "table, so it has to match one there exactly -- 'M6x1', not "
                        "'M6'. Give `tap_type` and `tap_class` if the defaults are "
                        "wrong for this table, or drop `tap` and add a `thread` "
                        "operation on the bore instead.",
                    ) from exc

            call = holes.plan_call(
                request, placement, extent, size,
                request.bottom_angle.expression if request.bottom_angle else None,
            )
            try:
                feature = holes.invoke(features, call)
            except Exception as exc:
                raise FeatureError(
                    f"Could not make the {request.style} hole: "
                    f"{self._explain_text(_com_message(exc))}",
                    hint=f"Called {call.describe()}. Check that the hole centres "
                    "sit over material, that the diameter is smaller than the "
                    "surrounding geometry, and that a counterbore or countersink "
                    "is wider than the bore it sits over.",
                ) from exc

            # Inventor coerces what it can, so a wrong argument order can build
            # a plain hole and report success.  Reading the type back off the
            # feature is the only thing that distinguishes "made a counterbore"
            # from "made something".
            agreed, verdict = holes.verify(feature, request, self._k)
            if agreed is False:
                raise FeatureError(
                    f"The hole built but is not what was asked for: {verdict}.",
                    hint=f"Called {call.describe()}. The argument order for this "
                    "family is probably wrong on this release -- run "
                    "`python scripts/probe_hole_styles.py` and paste its output.",
                )
            if agreed is None:
                notes.append(f"style not verified: {verdict}")

            # Inventor is happy to drill into thin air and call it a success, so
            # the feature only counts if the part got smaller.  There is no
            # second chance: a hole consumes its sketch, so the feature cannot
            # be deleted and rebuilt, and neither HoleFeature.ExtentDirection
            # nor its Definition is writable on 2027.1.  Hence choosing the
            # side up front rather than trying one and correcting.
            _recompute(document)
            after = _solid_volume(document)
            if not _removed_material(document, before):
                raise FeatureError(
                    "The hole built but removed no material.",
                    hint=self._explain_dry_hole(
                        document, sketch, centers, before, [after],
                        [f"drilled {'along' if along_normal else 'against'} the "
                         f"sketch normal, {why}"]),
                )

            if request.name:
                feature.Name = request.name
        detail: dict[str, Any] = {
            "count": int(centers.Count),
            "diameter": request.diameter.as_dict(),
            "style": request.style,
            "method": call.method,
            "drilled": ("along" if along_normal else "against") + " the sketch normal",
            "chose_by": why,
        }
        if request.tap:
            detail["tap"] = request.tap
            detail["tap_type"] = request.tap_type or holes.thread_type_for(request.tap)
            actual = _hole_diameter(feature)
            if actual is not None:
                detail["drilled_diameter_mm"] = round(actual * 10, 4)
                # The recipe's diameter did not reach the model, so a wrong one
                # would otherwise sit in the recipe looking authoritative.
                if abs(actual - request.diameter.value) > 5.0e-3:
                    notes.append(
                        f"Inventor cut {actual * 10:.4f} mm, not the "
                        f"{request.diameter.value * 10:.4f} mm the recipe gives. It "
                        "models the thread's minor diameter (D - 1.0825 x pitch for "
                        "ISO metric), which is narrower than the tapping drill -- so "
                        "give that if you want the two to agree. Either way the "
                        "recipe's diameter did not reach the model."
                    )
        if notes:
            detail["notes"] = notes
        return _feature_info(feature, "hole", detail)

    def _explain_dry_hole(self, document: Any, sketch: Any, centers: Any,
                          before: float | None, measured: list[float | None],
                          failures: list[str]) -> str:  # pragma: no cover
        """Say where the hole centres actually are, and what the volume did.

        A hole that builds and removes nothing has told us almost nothing about
        why. Three rounds of guessing at this cost three runs, so the failure
        now carries the measurements that would settle it: where each centre
        lands in model space, which way the sketch faces, what the volume did on
        each attempt, and how big the part is.
        """
        parts: list[str] = []
        try:
            positions = []
            for index in range(1, int(centers.Count) + 1):
                point = centers.Item(index)
                model = sketch.SketchToModelSpace(point.Geometry)
                positions.append(
                    f"({model.X * 10:.1f}, {model.Y * 10:.1f}, {model.Z * 10:.1f})")
            if positions:
                parts.append("centres at " + ", ".join(positions[:4]) + " mm")
        except Exception:
            parts.append("could not read the centres' model positions")

        axes = _sketch_axes(sketch, self._require_app().TransientGeometry)
        if axes is not None:
            normal = _cross(axes[0], axes[1])
            parts.append("the sketch faces "
                         f"({normal[0]:+.0f}, {normal[1]:+.0f}, {normal[2]:+.0f})")

        try:
            box = document.ComponentDefinition.RangeBox
            parts.append(
                "the part spans "
                + " x ".join(
                    f"{getattr(box.MinPoint, axis) * 10:.1f}.."
                    f"{getattr(box.MaxPoint, axis) * 10:.1f}"
                    for axis in "XYZ")
                + " mm")
        except Exception:
            pass

        readings = ", ".join("unreadable" if value is None else f"{value:.4f}"
                             for value in measured)
        parts.append(f"volume was {before if before is None else f'{before:.4f}'} cm^3 "
                     f"and stayed at {readings or 'no reading'} on "
                     f"{len(measured)} attempt(s)")
        if len(failures) > 1:
            parts.append(f"first refusal: {failures[0]}")
        return "; ".join(parts) + ". Check the centres lie on the part."

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

        if request.radius_end is not None:
            return self._variable_fillet(doc_id, document, features, request)

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

    #: Add's trailing options for a variable fillet. Edge chaining is off
    #: because every edge is named explicitly, and the smooth transition is
    #: Inventor's own default for a variable radius -- it also decides how much
    #: material comes away, which is what the simulator's estimate models.
    _VARIABLE_FILLET_OPTIONS = (
        False,  # AutomaticEdgeChain
        True,   # SmoothRadiusTransition
        True,   # RollAlongSharpEdges
        True,   # RollingBallWherePossible
        False,  # PreserveAllFeatures
    )

    def _variable_fillet(self, doc_id: str, document: Any, features: Any,
                         request: FilletRequest) -> FeatureInfo:  # pragma: no cover
        """A fillet whose radius runs from one value to another along each edge.

        ``AddSimple`` only does a constant radius, so this goes the long way
        round: a definition, an edge set, then ``Add``.  One set per edge --
        Inventor refuses a variable-radius set holding several edges, since the
        run from one radius to the other belongs to a single edge.

        Which end of the edge starts at which radius is Inventor's to decide and
        it does not say, so a fillet can come out the other way round; the
        schema says as much.
        """
        assert request.radius_end is not None
        matches = self.select(doc_id, request.edges)
        if not matches:
            raise SelectionError(
                "The fillet selector matched no edges.",
                hint="Call `select_topology` with the same selector to see the "
                "alternatives.",
                selector=request.edges.__dict__,
            )

        def defined(start: Any, end: Any) -> Any:
            definition = features.CreateFilletDefinition()
            for match in matches:
                edges = self._new_collection("edge")
                edges.Add(self._topology[match.id]["object"])
                definition.AddVariableRadiusEdgeSet(edges, start, end)
            return definition

        failures: list[str] = []
        with self._batch(document), self._translate_errors("Variable fillet"):
            feature = None
            # Expressions keep the fillet parameter-driven; plain numbers are
            # the fallback, the same order the constant-radius path uses.
            for start, end, described in (
                (request.radius.expression, request.radius_end.expression, "expressions"),
                (request.radius.value, request.radius_end.value, "values"),
            ):
                try:
                    feature = features.Add(defined(start, end),
                                           *self._VARIABLE_FILLET_OPTIONS)
                except Exception as exc:
                    failures.append(f"radii as {described}: {_com_message(exc)}")
                    continue
                break
            if feature is None:
                raise FeatureError(
                    f"Could not create the variable fillet: "
                    f"{self._explain_text(failures[0])}",
                    hint=f"{request.radius.expression} to "
                    f"{request.radius_end.expression} over {len(matches)} edge(s). "
                    "A radius that does not fit the faces around the edge is the "
                    "usual cause; " + "; ".join(failures),
                )
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "fillet", {
            "edges": len(matches),
            "radius": request.radius.as_dict(),
            "radius_end": request.radius_end.as_dict(),
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

    def emboss(self, doc_id: str, request: EmbossRequest) -> FeatureInfo:  # pragma: no cover
        """Raise or sink a sketch -- text or a closed profile -- on the part.

        Inventor exposes this as two separate calls rather than one with a flag,
        and the optional trailing arguments are not marshalled consistently
        across builds, so each shape is tried in turn and every refusal is kept
        for the error message.
        """
        document = self._doc(doc_id)
        sketch = self._sketch(doc_id, request.sketch)
        features = document.ComponentDefinition.Features.EmbossFeatures
        depth = request.depth.expression
        direction = self._k(
            EXTENT_DIRECTIONS["negative" if request.flip else "positive"]
        )
        with self._batch(document), self._translate_errors("Emboss"):
            profile = self._emboss_profile(sketch)
            add = (
                features.AddEngraveFromFace
                if request.style == "engrave"
                else features.AddEmbossFromFace
            )
            try:
                feature = add(profile, depth, direction)
            except Exception as exc:
                raise FeatureError(
                    f"Emboss failed: {self._explain(exc)}",
                    hint=self._emboss_hint(sketch, request),
                ) from exc
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "emboss", {
            "sketch": request.sketch,
            "style": request.style,
            "depth": request.depth.as_dict(),
        })

    def _emboss_hint(self, sketch: Any, request: EmbossRequest) -> str:  # pragma: no cover
        """Why an emboss usually refuses, said in terms of this sketch.

        Overwhelmingly it is that the text does not fit on the face it is being
        written on: Inventor reports that as a bare "Exception occurred", which
        sends you looking at the depth or the profile instead of the one thing
        that is actually wrong. The fitted extent is measurable, so it is quoted.
        """
        parts = [f"Sketch {sketch.Name!r}, style {request.style!r}, depth "
                 f"{request.depth.expression!r}."]
        try:
            boxes = sketch.TextBoxes
            for index in range(1, int(boxes.Count) + 1):
                box = boxes.Item(index)
                parts.append(
                    f"Text {box.Text!r} renders {float(box.FittedTextWidth) * 10:.1f} x "
                    f"{float(box.FittedTextHeight) * 10:.1f} mm from its anchor."
                )
        except Exception:
            pass
        parts.append(
            "An emboss whose profile runs off the edge of the face it is on is "
            "refused with no further explanation, so check the text fits before "
            "reaching for anything else -- shrink `height`, or move `position`."
        )
        return " ".join(parts)

    def _emboss_profile(self, sketch: Any) -> Any:  # pragma: no cover
        """A profile for an emboss, which unlike a solid feature may be text.

        ``AddForSolid`` returns an empty profile for a sketch whose only content
        is a text box -- there is no closed loop of curves in it -- and that empty
        profile is exactly what the emboss wants, so an empty result is accepted
        here where a solid feature would reject it.
        """
        has_text = int(getattr(sketch.TextBoxes, "Count", 0) or 0) > 0
        try:
            profile = sketch.Profiles.AddForSolid()
        except Exception as exc:
            raise FeatureError(
                f"No usable profile in sketch {sketch.Name!r}: {self._explain_text(_com_message(exc))}",
                hint="An emboss needs a text box or a closed profile.",
            ) from exc
        if int(profile.Count) == 0 and not has_text:
            raise FeatureError(
                f"Sketch {sketch.Name!r} has nothing to emboss.",
                hint=f"It contains {_describe_sketch(sketch)}. Add text or a closed profile.",
            )
        return profile


    def draft(self, doc_id: str, request: DraftRequest) -> FeatureInfo:  # pragma: no cover
        """Taper faces about their edge on a parting plane.

        The definition is built and then handed over, rather than passed as
        arguments, which is Inventor's usual shape for anything with options.
        """
        document = self._doc(doc_id)
        faces = self._topology_collection(doc_id, request.faces)
        if int(faces.Count) == 0:
            raise FeatureError(
                "No faces matched, so there is nothing to draft.",
                hint="Run `select_topology` with the same selector to see what it matches.",
            )
        plane = self._resolve_plane(document, request.plane, None)
        features = document.ComponentDefinition.Features.FaceDraftFeatures
        with self._batch(document), self._translate_errors("Draft"):
            definition = features.CreateFaceDraftDefinition()
            definition.SetFixedPlane(faces, plane, request.angle.expression)
            if request.flip:
                try:
                    definition.PullDirectionReversed = True
                except Exception:  # pragma: no cover - version-specific
                    logger.info("Could not reverse the draft pull direction.")
            try:
                feature = features.Add(definition)
            except Exception as exc:
                raise FeatureError(
                    f"Draft failed: {self._explain(exc)}",
                    hint=f"{int(faces.Count)} face(s) at {request.angle.expression!r} about "
                    f"{request.plane!r}. A face that does not meet the parting plane, or one "
                    "already tapered the other way, will refuse.",
                ) from exc
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "draft", {
            "faces": int(faces.Count),
            "plane": request.plane,
            "angle": request.angle.as_dict(),
        })

    def combine(self, doc_id: str, request: CombineRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        component = document.ComponentDefinition
        app = self._require_app()
        _check_bodies(component, [request.base, *request.tools])
        tools = app.TransientObjects.CreateObjectCollection()
        for index in request.tools:
            tools.Add(component.SurfaceBodies.Item(index))
        features = component.Features.CombineFeatures
        with self._batch(document), self._translate_errors("Combine"):
            try:
                feature = features.Add(
                    component.SurfaceBodies.Item(request.base),
                    tools,
                    self._k(BOOLEAN_OPERATIONS[request.operation]),
                    request.keep_tools,
                )
            except Exception as exc:
                raise FeatureError(
                    f"Combine failed: {self._explain(exc)}",
                    hint=f"Body {request.base} {request.operation} "
                    f"{list(request.tools)}. Bodies that do not touch cannot be cut or "
                    "intersected, only joined.",
                ) from exc
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "combine", {
            "base": request.base,
            "tools": list(request.tools),
            "operation": request.operation,
            "bodies_now": int(component.SurfaceBodies.Count),
        })

    def split(self, doc_id: str, request: SplitRequest) -> FeatureInfo:  # pragma: no cover
        """Cut the part with a plane.

        Inventor has a separate call per outcome rather than a mode flag, so the
        style picks the call: `trim` throws a side away, `split` leaves two
        bodies, `faces` only divides the faces the plane crosses.
        """
        document = self._doc(doc_id)
        component = document.ComponentDefinition
        tool = self._resolve_plane(document, request.tool, None)
        features = component.Features.SplitFeatures
        with self._batch(document), self._translate_errors("Split"):
            try:
                if request.style == "trim":
                    feature = features.SplitPart(tool, request.remove_positive)
                elif request.style == "split":
                    feature = features.SplitBody(tool, component.SurfaceBodies.Item(1))
                else:
                    feature = features.SplitFaces(tool, True)
            except Exception as exc:
                raise FeatureError(
                    f"Split failed: {self._explain(exc)}",
                    hint=f"Style {request.style!r} with {request.tool!r}. The plane has to "
                    "pass through the part; one that misses it entirely refuses.",
                ) from exc
            if request.name:
                try:
                    feature.Name = request.name
                except Exception:  # pragma: no cover - SplitFaces returns no namable feature
                    pass
        return _feature_info(feature, "split", {
            "tool": request.tool,
            "style": request.style,
            "remove_positive": request.remove_positive,
            "bodies_now": int(component.SurfaceBodies.Count),
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
                # The measured signature is
                #   Add(ParentFeatures, XDirectionEntity, NaturalXDirection,
                #       XCount, XSpacing, [XSpacingType], [XDirectionStartPoint],
                #       [YDirectionEntity], [NaturalYDirection], [YCount],
                #       [YSpacing], ...)
                # Slot 5 is the *spacing type*, not the compute type. Putting
                # kAdjustToModelCompute there shifted every argument after it by
                # one, so the second axis landed in XDirectionStartPoint -- which
                # is how a two-axis pattern failed with a bare "Exception
                # occurred" and nothing in Inventor's error manager to read.
                feature, compute = _patterned(features.Add, self._k, [
                    ("ParentFeatures", parents),
                    ("XDirectionEntity", axis1),
                    ("NaturalXDirection", not request.flip1),
                    ("XCount", request.count1),
                    ("XSpacing", request.spacing1.expression),
                    # These two sit between the axes and have no value this
                    # project knows; the wrapper's defaults are the right answer.
                    ("XSpacingType", DEFAULTED),
                    ("XDirectionStartPoint", DEFAULTED),
                    ("YDirectionEntity", self._resolve_axis(doc_id, request.axis2)),
                    ("NaturalYDirection", not request.flip2),
                    ("YCount", request.count2),
                    ("YSpacing", request.spacing2.expression),
                    ("YSpacingType", DEFAULTED),
                    ("YDirectionStartPoint", DEFAULTED),
                ])
            else:
                feature, compute = _patterned(features.Add, self._k, [
                    ("ParentFeatures", parents),
                    ("XDirectionEntity", axis1),
                    ("NaturalXDirection", not request.flip1),
                    ("XCount", request.count1),
                    ("XSpacing", request.spacing1.expression),
                ])
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "rectangular_pattern", {
            "count1": request.count1, "count2": request.count2, "compute": compute,
        })

    def circular_pattern(self, doc_id: str,
                         request: CircularPatternRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        parents = self._feature_collection(doc_id, request.features)
        axis = self._resolve_axis(doc_id, request.axis)
        features = document.ComponentDefinition.Features.CircularPatternFeatures
        with self._batch(document), self._translate_errors("Circular pattern"):
            feature, compute = _patterned(features.Add, self._k, [
                ("ParentFeatures", parents),
                ("AxisEntity", axis),
                ("NaturalAxisDirection", True),
                ("Count", request.count),
                ("Angle", request.angle.expression),
                ("FitWithinAngle", request.fitted),
            ])
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "circular_pattern",
                             {"count": request.count, "compute": compute})

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
                    kind=_feature_kind(feature, self._constants),
                    suppressed=bool(feature.Suppressed),
                )
            )
        return results

    def suppress_feature(self, doc_id: str, name: str, suppressed: bool) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        feature = _find_feature(document.ComponentDefinition.Features, name)
        feature.Suppressed = suppressed
        document.Update()
        return FeatureInfo(id=f"feat:{name}", name=name, kind=_feature_kind(feature, self._constants),
                           suppressed=suppressed)

    def delete_feature(self, doc_id: str, name: str) -> None:  # pragma: no cover
        document = self._doc(doc_id)
        _find_feature(document.ComponentDefinition.Features, name).Delete()
        document.Update()

    def rename_feature(self, doc_id: str, name: str, new_name: str) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        feature = _find_feature(document.ComponentDefinition.Features, name)
        feature.Name = new_name
        return FeatureInfo(id=f"feat:{new_name}", name=new_name,
                           kind=_feature_kind(feature, self._constants))

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
            convexity, decided_by = _edge_convexity(entity)
            info = TopoInfo(
                id=handle,
                kind="edge",
                description=f"{geometry} edge",
                midpoint=midpoint,
                direction=direction,
                length=length,
                geometry=geometry,
                convexity=convexity,
                convexity_from=decided_by,
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

    def topology_counts(self, doc_id: str) -> dict[str, int]:  # pragma: no cover
        try:
            bodies = self._doc(doc_id).ComponentDefinition.SurfaceBodies
            faces = edges = 0
            for index in range(1, int(bodies.Count) + 1):
                body = bodies.Item(index)
                faces += int(body.Faces.Count)
                edges += int(body.Edges.Count)
            return {"faces": faces, "edges": edges}
        except Exception:
            return {}

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

    # -- undo --------------------------------------------------------------
    def begin_transaction(self, doc_id: str, name: str) -> str | None:  # pragma: no cover
        """Open one of Inventor's own transactions over this document.

        Inventor's ``TransactionManager`` is what its own commands use, so an
        abort undoes everything the way Ctrl+Z would -- including the sketch a
        hole feature consumed, which nothing else can bring back.
        """
        document = self._doc(doc_id)
        app = self._require_app()
        try:
            transaction = app.TransactionManager.StartTransaction(document, name)
        except Exception as exc:
            logger.debug("Inventor would not start a transaction: %s", _com_message(exc))
            return None
        handle = self._next("txn")
        self._transactions[handle] = transaction
        return handle

    def commit_transaction(self, handle: str) -> None:  # pragma: no cover
        transaction = self._transactions.pop(handle, None)
        if transaction is None:
            return
        try:
            transaction.End()
        except Exception as exc:
            # The work is already in the document; only the grouping is lost, so
            # this is worth a log and not an error.
            logger.debug("Could not close transaction %s: %s", handle, _com_message(exc))

    def abort_transaction(self, handle: str) -> bool:  # pragma: no cover
        transaction = self._transactions.pop(handle, None)
        if transaction is None:
            return False
        try:
            transaction.Abort()
        except Exception as exc:
            logger.debug("Could not abort transaction %s: %s", handle, _com_message(exc))
            return False
        # Every handle held against the old topology is now stale, and a stale
        # handle that still resolves is worse than one that fails.
        self._topology.clear()
        return True

    def rebuild(self, doc_id: str) -> dict[str, Any]:  # pragma: no cover
        document = self._doc(doc_id)
        self._topology.clear()
        with self._translate_errors("Rebuild"):
            document.Rebuild()
        healthy = self._healthy_statuses()
        errors: list[dict[str, Any]] = []
        uninterpreted: list[dict[str, Any]] = []
        try:
            features = document.ComponentDefinition.Features
            for index in range(1, int(features.Count) + 1):
                feature = features.Item(index)
                status = getattr(feature, "HealthStatus", None)
                if status is None:
                    continue
                entry = {
                    "feature": str(feature.Name),
                    "health_status": int(status),
                    "suppressed": bool(getattr(feature, "Suppressed", False)),
                }
                if healthy is None:
                    uninterpreted.append(entry)
                elif int(status) not in healthy:
                    errors.append(entry)
        except Exception:
            pass
        report: dict[str, Any] = {"rebuilt": True, "errors": errors}
        if uninterpreted:
            report["uninterpreted_health"] = uninterpreted
            report["note"] = (
                "Inventor's HealthStatusEnum could not be read, so these statuses "
                "are reported without a verdict. Judge the rebuild by the geometry: "
                "a feature that really failed shows up in the volume."
            )
        return report

    def _healthy_statuses(self) -> set[int] | None:  # pragma: no cover - Windows only
        """Status values meaning "fine", or None if Inventor will not say.

        Asked by name rather than tabulated: the numbering is version-specific,
        and the previous hard-coded pair contained a value from a different enum
        altogether, which reported a correct rebuild as three sick features.
        """
        values = set(_OBSERVED_HEALTHY) | {0}
        for name in _HEALTHY_STATUS_NAMES:
            try:
                values.add(self._k(name))
            except Exception:
                # No name to ask for on this release, but the observed values
                # stand on their own evidence. Reporting every feature as sick
                # because an enum is missing would be the worse answer.
                continue
        return values

    #: Properties worth reading off a feature when asking what Inventor made.
    #: Deliberately a wide net over several feature kinds: a name that is not
    #: there is skipped, and the cost of asking is one failed lookup.
    _DESCRIBABLE = (
        "HoleDiameter", "Depth", "ExtentType", "HoleType", "Tapped", "FlatBottom",
        "HoleBottomType", "BottomTipAngle", "CounterboreDiameter",
        "CounterboreDepth", "CountersinkDiameter", "CountersinkAngle",
        "SpotFaceDiameter", "SpotFaceDepth", "Radius", "Distance", "Thickness",
        "Angle", "Operation", "Suppressed", "HealthStatus",
        # The draft. A built extrude's taper is the only thing on the feature
        # that names the parameter driving it, which is what role discovery on a
        # part nobody described has to read.
        "TaperAngle", "Taper",
    )

    def describe_feature(self, doc_id: str, name: str) -> dict[str, Any]:  # pragma: no cover
        """What Inventor says about one feature, as numbers rather than objects.

        Runs on the apartment that owns the objects, which is why it is a backend
        method rather than something a script does for itself. Reaching into a
        returned COM object from another thread fails with "the application
        called an interface that was marshalled for a different thread", and that
        is exactly how the first attempt to read a counterbore's real depth died.
        """
        document = self._doc(doc_id)
        feature = _find_feature(document.ComponentDefinition.Features, name)
        described: dict[str, Any] = {
            "name": str(getattr(feature, "Name", name)),
            "kind": _feature_kind(feature, self._constants),
        }
        # Through dynamic dispatch, because `Features.Item()` under a makepy
        # cache is a generic `PartFeature` that declares neither `Thickness`
        # nor `Definition` -- measured: a live shell described as nothing but
        # HealthStatus and Suppressed, and discovery starved. See `_dynamic`.
        feature = _dynamic(feature)
        # The feature *and* its definition: a hole's diameter, seat and bottom
        # all live on `HoleFeature.Definition`, which is why the first version of
        # this printed nothing but `Suppressed`.
        holders = [("", feature)]
        definition = getattr(feature, "Definition", None)
        if definition is not None:
            holders.append(("definition.", _dynamic(definition)))
        for prefix, holder in holders:
            for attribute in self._DESCRIBABLE:
                try:
                    raw = getattr(holder, attribute)
                except Exception:
                    continue
                value = _plain(raw)
                if value is not None:
                    described.setdefault(prefix + attribute, value)
        return described

    # -- escape hatch ------------------------------------------------------
    def run_script(self, doc_id: str | None, code: str) -> dict[str, Any]:  # pragma: no cover
        """Execute *code* against the live API, on the thread that owns it.

        A plain ``exec`` with the API objects in scope. There is no sandbox and
        no attempt at one: this runs in the server's own process, and anything
        that could restrict it could be undone by the code it is restricting.
        The protection is that the tool exposing this is not registered unless
        the machine's owner turns it on -- see ``inventor_mcp/tools/escape.py``.

        The Inventor objects have to be reached from here rather than passed in,
        because this must run on the apartment that created them, and this method
        is what the marshalling proxy routes there.
        """
        import io
        from contextlib import redirect_stdout

        app = self._require_app()
        document = self._doc(doc_id) if doc_id else None
        scope: dict[str, Any] = {
            "application": app,
            "app": app,
            "document": document,
            "component": document.ComponentDefinition if document is not None else None,
            "transient": app.TransientGeometry,
            "transient_objects": app.TransientObjects,
            "constants": self._constants,
            "k": self._k,
            "backend": self,
            "result": None,
        }
        printed = io.StringIO()
        with redirect_stdout(printed):
            exec(code, scope)  # noqa: S102 - the whole point of this method
        outcome = scope.get("result")
        report: dict[str, Any] = {"ran": True, "printed": printed.getvalue()}
        if outcome is not None:
            report["result"] = _describe_value(outcome)
        if document is not None:
            _recompute(document)
            volume = _solid_volume(document)
            if volume is not None:
                report["volume_cm3"] = round(volume, 6)
        return report

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


#: ``ObjectTypeEnum`` names for the feature kinds worth naming, and the short
#: name this project uses for each. Asked of the type library by name rather than
#: held as numbers, because the numbers move between releases -- 32 of the 51
#: entries in the fallback table turned out to be wrong when they were finally
#: measured, and one of them silently turned a through-all extrude into a
#: to-next.
_FEATURE_TYPES: dict[str, str] = {
    "kExtrudeFeatureObject": "extrude",
    "kRevolveFeatureObject": "revolve",
    "kSweepFeatureObject": "sweep",
    "kLoftFeatureObject": "loft",
    "kHoleFeatureObject": "hole",
    "kFilletFeatureObject": "fillet",
    "kChamferFeatureObject": "chamfer",
    "kShellFeatureObject": "shell",
    "kThickenFeatureObject": "thicken",
    "kRibFeatureObject": "rib",
    "kThreadFeatureObject": "thread",
    "kRectangularPatternFeatureObject": "rectangular_pattern",
    "kCircularPatternFeatureObject": "circular_pattern",
    "kMirrorFeatureObject": "mirror",
    # Measured: 2027.1's type library has no kDraftFeatureObject -- the face
    # draft feature's enum is this one.
    "kFaceDraftFeatureObject": "draft",
    "kSplitFeatureObject": "split",
    "kCoilFeatureObject": "coil",
    "kEmbossFeatureObject": "emboss",
    "kDeleteFaceFeatureObject": "delete_face",
    "kNonParametricBaseFeatureObject": "base",
}


def _feature_kind(feature: Any, constants: Any | None = None) -> str:  # pragma: no cover - Windows only
    """What kind of feature this is, asked of Inventor.

    This used to read ``type(feature).__name__``, which works under early
    binding and returns ``CDispatch`` under late -- and late is this project's
    default. So every feature on a live part reported its kind as the name of a
    pywin32 wrapper class, and anything reasoning about kinds was reasoning about
    nothing. ``Object.Type`` is a documented property of every Inventor object
    and says what the thing actually is.

    Falls back to the Python type name, and then to ``"unknown"``. Not to a
    guess: a kind nobody can read is worth saying so about, because the caller
    that cares is deciding whether a feature's thickness is a wall or a rib.
    """
    if constants is not None:
        try:
            actual = int(feature.Type)
        except Exception:
            actual = None
        if actual is not None:
            for name, short in _FEATURE_TYPES.items():
                try:
                    if constants.resolve(name) == actual:
                        return short
                except Exception:
                    continue
    typename = str(type(feature).__name__)
    # PartFeature is the early-bound cache's GENERIC wrapper for every feature,
    # the way CDispatch is late binding's -- measured live. Reading it as a kind
    # fabricated "part", which is not in the evidence table (so nothing mapped
    # wrongly) and not "unknown" either (so the property-alone offer never
    # fired): the feature's evidence just vanished.
    if typename and typename not in ("CDispatch", "DispatchBaseClass", "Dispatch",
                                     "PartFeature", "Feature"):
        return typename.replace("Feature", "").lower() or "unknown"
    return "unknown"


#: Constraint kinds Inventor infers on its own while geometry is created.
_INFERRED_KINDS = {"coincident", "horizontal", "vertical", "tangent"}

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


def _check_bodies(component: Any, indices: Sequence[int]) -> None:  # pragma: no cover
    """Reject a body number the part does not have, before Inventor is asked."""
    available = int(component.SurfaceBodies.Count)
    for index in indices:
        if index < 1 or index > available:
            raise FeatureError(
                f"There is no body {index}: the part has {available}.",
                hint="A second body comes from an `extrude` with "
                "operation 'new_body'.",
            )


def _aim_at_bodies(app: Any, component: Any, definition: Any,
                   indices: Sequence[int]) -> None:  # pragma: no cover
    """Point a feature definition at particular solid bodies.

    Inventor aims a new feature at the first body only, so a cut meant for the
    second one silently removes nothing -- which reads as a working recipe that
    built the wrong part.
    """
    _check_bodies(component, indices)
    collection = app.TransientObjects.CreateObjectCollection()
    for index in indices:
        collection.Add(component.SurfaceBodies.Item(index))
    try:
        definition.AffectedBodies = collection
    except Exception as exc:
        raise FeatureError(
            f"Could not aim the feature at body {list(indices)}: {_com_message(exc)}",
            hint="This Inventor build may not accept AffectedBodies on this "
            "feature. `combine` with operation 'cut' does the same job.",
        ) from exc


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


def _describe_value(value: Any) -> Any:  # pragma: no cover - Windows only
    """A script's return value in something JSON can carry.

    A COM object is not serialisable and its repr is not informative, so what
    goes back is its type and the few properties worth knowing rather than a
    string nobody can act on.
    """
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_describe_value(item) for item in value[:50]]
    if isinstance(value, dict):
        return {str(key): _describe_value(item) for key, item in list(value.items())[:50]}
    described: dict[str, Any] = {"type": type(value).__name__}
    for name in ("Name", "Type", "Count", "Value", "Expression", "Volume", "Area"):
        try:
            attribute = getattr(value, name)
        except Exception:
            continue
        if isinstance(attribute, (str, int, float, bool)):
            described[name] = attribute
    return described


def _plain(value: Any) -> Any:  # pragma: no cover - Windows only
    """A COM property as a number, a string or None -- never an object.

    A Parameter comes back as both, because the value says what was built and
    the expression says what drives it, and a counterbore whose depth reads
    0.6216 against an expression of "6.6 mm" is a different problem from one
    whose expression is wrong too.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    reading: dict[str, Any] = {}
    for attribute in ("Value", "Expression"):
        try:
            inner = getattr(value, attribute)
        except Exception:
            continue
        if isinstance(inner, (bool, int, float, str)):
            reading[attribute.lower()] = inner
    return reading or None


#: Marks an argument that should be left to the wrapper's own default.
DEFAULTED = object()


def _patterned(add: Any, resolve: Callable[[str], int],
               arguments: Sequence[tuple[str, Any]]) -> tuple[Any, str]:  # pragma: no cover
    """Create a pattern, recomputing each occurrence if copying will not do.

    Measured on 2027.1: patterning a boss works with the default compute type,
    and patterning a *hole* fails outright -- whether it goes with the boss or
    alone -- until the compute type is ``kAdjustToModelCompute``. That fits what
    the two settings mean. Identical compute copies faces, which is valid only
    where the copy lands on the same geometry it came from; a blind hole's second
    occurrence has to find material to remove, and there is none until the boss
    beneath it has been computed too.

    The pulley's through-holes in a flat disc pattern happily with the default,
    which is why this was not obvious sooner: identical compute is right when
    every occurrence really is identical.

    So: recompute first, since that is the answer that is correct more often, and
    fall back to the default if a release ever refuses it. Which one built the
    feature is reported, because a pattern that needed the slow path is worth
    knowing about on a large one.
    """
    routes = [("adjust to model", "kAdjustToModelCompute"), ("default", None)]
    failures: list[str] = []
    for label, enum in routes:
        try:
            extra = [] if enum is None else [("ComputeType", resolve(enum))]
            return _call_named(add, list(arguments) + extra), label
        except Exception as exc:
            failures.append(f"{label}: {_com_message(exc)}")
    raise FeatureError(
        "The pattern could not be created. Tried " + "; ".join(failures) + ".",
        hint="A pattern of a hole or a cut needs each occurrence recomputed, and "
        "each one needs material to act on. Check that every occurrence lands on "
        "the part -- `scripts/probe_sweep_and_pattern.py` tries the variations.",
    )


def _call_named(method: Any, arguments: Sequence[tuple[str, Any]]) -> Any:  # pragma: no cover
    """Call *method* with arguments in signature order, skipping the defaulted ones.

    Some of Inventor's methods put optional arguments *between* the ones that
    matter: ``RectangularPatternFeatures.Add`` has XSpacingType and
    XDirectionStartPoint sitting between the X axis and the Y axis. Positionally
    there is no way to skip them, and the wrong value there shifts every argument
    after it -- which is how a two-axis pattern failed with a bare "Exception
    occurred" and nothing in Inventor's error manager to read.

    Named arguments avoid the question, so they are tried first. The positional
    fallback puts ``None`` in the gaps, which is what a missing optional VARIANT
    looks like, and is reached only when the binding refuses keywords.
    """
    named = {name: value for name, value in arguments if value is not DEFAULTED}
    try:
        return method(**named)
    except TypeError:
        return method(*[None if value is DEFAULTED else value
                        for _, value in arguments])


#: The sketch collections that hold curves, in the order a path is looked for.
_CURVES = ("SketchArcs", "SketchLines", "SketchCircles", "SketchEllipses",
           "SketchSplines", "SketchEquationCurves")


def _first_curve(sketch: Any) -> Any:  # pragma: no cover - Windows only
    """The first real curve in a sketch, skipping its points.

    ``SketchEntities.Item(1)`` is not reliably a curve: it includes sketch
    points, and this project projects the origin into a sketch whenever a
    constraint references it, so a path sketch of one arc can easily answer with
    a point. Handing a point to ``CreatePath`` fails with "Exception occurred"
    and no further explanation, which is a long way from the cause.
    """
    for collection_name in _CURVES:
        collection = getattr(sketch, collection_name, None)
        if collection is None:
            continue
        try:
            total = int(collection.Count)
        except Exception:
            continue
        for index in range(1, total + 1):
            curve = collection.Item(index)
            if not bool(getattr(curve, "Construction", False)):
                return curve
    raise FeatureError(
        f"Sketch {getattr(sketch, 'Name', '?')!r} has no non-construction curve "
        "to use as a path.",
        hint="A sweep path needs real geometry: check that the sketch's entities "
        "are not all marked construction.",
    )


def _hole_diameter(feature: Any) -> float | None:  # pragma: no cover - Windows only
    """The bore Inventor actually drilled, in cm, or None if it will not say.

    Worth asking for a tapped hole: the drill size comes from Inventor's thread
    table rather than from the recipe, so the recipe's own diameter is a claim
    nothing has checked.
    """
    for name in ("HoleDiameter", "Diameter"):
        try:
            value = getattr(feature, name)
        except Exception:
            continue
        for read in (lambda: float(value.Value), lambda: float(value)):
            try:
                return read()
            except Exception:
                continue
    return None


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


def _face_key(face: Any) -> tuple[float, ...] | None:  # pragma: no cover - Windows only
    """A cheap identity for a face, since COM objects will not compare."""
    try:
        box = face.Evaluator.RangeBox
        return (
            round(float(box.MinPoint.X), 7), round(float(box.MinPoint.Y), 7),
            round(float(box.MinPoint.Z), 7), round(float(box.MaxPoint.X), 7),
            round(float(box.MaxPoint.Y), 7), round(float(box.MaxPoint.Z), 7),
            round(float(face.Evaluator.Area), 7),
        )
    except Exception:
        return None


def _unit(vector: Sequence[float]) -> tuple[float, ...] | None:
    length = math.sqrt(sum(component * component for component in vector))
    if length < 1e-12:
        return None
    return tuple(component / length for component in vector)


def _edge_ends(edge: Any) -> tuple[tuple[float, ...], ...] | None:  # pragma: no cover
    """The edge's two endpoints, in centimetres."""
    for route in (
        lambda: (edge.Geometry.StartPoint, edge.Geometry.EndPoint),
        lambda: (edge.StartVertex.Point, edge.StopVertex.Point),
    ):
        try:
            first, second = route()
            return (
                (float(first.X), float(first.Y), float(first.Z)),
                (float(second.X), float(second.Y), float(second.Z)),
            )
        except Exception:
            continue
    return None


def _use_face_and_tangent(
    use: Any, ends: tuple[tuple[float, ...], ...], candidates: Sequence[Any]
) -> tuple[Any, tuple[float, ...]] | None:  # pragma: no cover - Windows only
    """The face an edge use lies on, and the direction its loop runs.

    Neither piece comes from the API directly.  ``EdgeUse.Face`` does not exist
    on 2027.1 and ``Parent`` is the whole ``SurfaceBody``; and
    ``IsParamReversed`` does not mean "runs against the loop" -- both uses of
    an edge report False, so taking it at its word made every edge's two uses
    contradict each other and the exact method answered nothing at all.

    Both are available from the loop itself.  ``Next`` names the following use,
    whose edge lies on the same face and shares exactly one face with ours --
    which identifies the face.  That edge also shares exactly one *vertex* with
    ours, and a loop runs along an edge towards the vertex it shares with the
    edge that follows -- which gives the direction.
    """
    keys = [_face_key(face) for face in candidates]
    if len(keys) != 2 or any(key is None for key in keys) or keys[0] == keys[1]:
        return None

    neighbour = use
    for _ in range(4):
        try:
            neighbour = neighbour.Next
            following = neighbour.Edge
            faces = following.Faces
            touched = {
                _face_key(faces.Item(index)) for index in range(1, int(faces.Count) + 1)
            }
            other_ends = _edge_ends(following)
        except Exception:
            return None
        shared = [face for face, key in zip(candidates, keys) if key in touched]
        if len(shared) != 1 or other_ends is None:
            continue  # a neighbour touching both faces settles nothing

        meeting = [
            index for index, point in enumerate(ends)
            if any(math.dist(point, other) < 1e-7 for other in other_ends)
        ]
        if len(meeting) != 1:
            continue  # both ends met, or neither: try the next one round
        finish = ends[meeting[0]]
        begin = ends[1 - meeting[0]]
        tangent = _unit([end - start for start, end in zip(begin, finish)])
        if tangent is None:
            continue
        return shared[0], tangent
    return None


def _convexity_from_loops(edge: Any) -> str | None:  # pragma: no cover - Windows only
    """Convexity from the orientation of the faces' boundary loops.

    A face's boundary runs anticlockwise about its outward normal, so the
    face's material lies to the left of the loop -- which is
    ``normal x tangent``.  If that direction points *into* the neighbouring
    face's outward normal the two faces close over the material and the edge is
    an inside corner; if it points away, an outside one.

    Both faces are asked independently and have to agree, so anything the
    method cannot settle comes back as "don't know" rather than a coin toss.
    """
    uses = _edge_uses(edge)
    if uses is None or len(uses) != 2:
        return None
    ends = _edge_ends(edge)
    if ends is None or math.dist(*ends) < 1e-9:
        return None  # a closed curve has no endpoints to orient it by
    try:
        collection = edge.Faces
        faces = [collection.Item(index) for index in range(1, int(collection.Count) + 1)]
    except Exception:
        return None
    if len(faces) != 2:
        return None

    resolved = [_use_face_and_tangent(use, ends, faces) for use in uses]
    if any(item is None for item in resolved):
        return None
    if _face_key(resolved[0][0]) == _face_key(resolved[1][0]):
        return None  # both uses landed on the same face, so neither is trusted

    verdicts = set()
    for index, (face, tangent) in enumerate(resolved):
        normal = _face_normal(face)
        other_normal = _face_normal(resolved[1 - index][0])
        if normal is None or other_normal is None:
            return None
        alignment = sum(a * b for a, b in zip(_cross(normal, tangent), other_normal))
        if abs(alignment) < 1e-9:  # tangent faces meet smoothly
            return None
        verdicts.add("concave" if alignment > 0 else "convex")

    if len(verdicts) != 1:
        logger.debug("The two edge uses disagree about convexity; leaving it unknown.")
        return None
    return verdicts.pop()


def _edge_convexity(edge: Any, _unused: Any = None) -> tuple[str | None, str]:  # pragma: no cover
    """Whether an edge is an outside corner or an inside one, and how we know.

    The boundary loops give an exact answer, so they decide wherever they can.
    Sampling -- taking the direction from the edge towards a point on each
    adjacent face and testing it against the other face's normal -- is only as
    good as the sample: ``Face.PointOnFace`` returns an arbitrary interior
    point, and on a face with an inner loop it can lie on the far side of the
    edge and invert the answer.  Drilling the bracket's upright put two inner
    loops in the face beside its L-junction and moved the "inside corner"
    fillet onto a convex edge.

    So sampling is used only where the loops are not *available* at all -- an
    older release, a surface body -- and never to second-guess a loop that
    looked and declined.  An unknown convexity matches no filter, which
    surfaces as "the selector matched no edges": wrong, but visibly wrong,
    which a quietly mis-filleted corner is not.
    """
    decided = _convexity_from_loops(edge)
    if decided is not None:
        return (decided, "loops")
    if _edge_uses(edge) is not None:
        return (None, "loops declined")
    return (_convexity_from_samples(edge), "sampled")


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


def _driven_parameters(plan: SketchPlan, applied: Sequence[str]) -> list[str]:
    """Which of the recipe's parameters reached a dimension Inventor accepted.

    A sketch can carry dimensions and still not be parametric, if every one of
    them is a frozen number. This is the difference, and it is worth reporting
    rather than leaving to be discovered by editing a parameter and watching
    nothing move.
    """
    names: set[str] = set()
    stored = {note for note in applied}
    for dimension in plan.dimensions:
        if not any(repr(dimension.expression) in note for note in stored):
            continue
        try:
            names |= referenced_parameters(dimension.expression)
        except Exception:  # pragma: no cover - a malformed expression cannot drive
            continue
    return sorted(names)


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


def _sketch_axes(sketch: Any, transient: Any) -> tuple[tuple[float, ...], ...] | None:
    """Where the sketch's own two axes point in model space, measured.

    A plane's internal orientation is not derivable from its name -- Inventor's
    XZ plane runs its first axis along model -X, and its YZ plane orders its
    axes differently again -- so ask the sketch rather than assume.
    """
    try:
        def at(u: float, v: float) -> tuple[float, float, float]:
            point = sketch.SketchToModelSpace(transient.CreatePoint2d(u, v))
            return (float(point.X), float(point.Y), float(point.Z))

        origin, along_u, along_v = at(0.0, 0.0), at(1.0, 0.0), at(0.0, 1.0)
    except Exception as exc:
        logger.info("Could not measure the sketch's axes (%s); "
                    "falling back to the plane-name table.", exc)
        return None

    axes = tuple(
        tuple(far - near for far, near in zip(end, origin))
        for end in (along_u, along_v)
    )
    for axis in axes:
        if abs(math.sqrt(sum(c * c for c in axis)) - 1.0) > 1e-6:
            logger.info("The sketch's axes are not unit vectors (%s); ignoring them.", axes)
            return None
    return axes


def _orientation_matrix(
    axes: tuple[tuple[float, ...], ...] | None,
) -> tuple[float, float, float, float] | None:
    """The transform from what a recipe means to what this sketch needs.

    Returns None when the sketch's orientation cannot be reconciled with the
    recipe's convention -- a plane at some angle to the model axes, say -- in
    which case the coordinates are passed through as written, which is the only
    honest thing to do with a plane whose axes have no agreed meaning.
    """
    if axes is None:
        return None
    along_u, along_v = axes
    normal = _cross(along_u, along_v)
    facing = max(range(3), key=lambda index: abs(normal[index]))
    if abs(normal[facing]) < 0.999:  # not an axis-aligned plane
        logger.info("Sketch plane normal %s is not axis-aligned; "
                    "taking its coordinates as written.", normal)
        return None

    intended_u, intended_v = _RECIPE_AXES[facing]
    matrix = tuple(
        sum(a * b for a, b in zip(intended, axis))
        for axis in (along_u, along_v)
        for intended in (intended_u, intended_v)
    )
    snapped = tuple(round(value) for value in matrix)
    if any(abs(value - exact) > 1e-6 for value, exact in zip(matrix, snapped)):
        logger.info("Sketch axes %s do not line up with the model axes; "
                    "taking its coordinates as written.", axes)
        return None
    return (float(snapped[0]), float(snapped[1]), float(snapped[2]), float(snapped[3]))


def _describe_orientation(
    axes: tuple[tuple[float, ...], ...] | None,
    matrix: tuple[float, float, float, float] | None,
) -> str:
    """A one-line account of what was measured and what was done about it."""
    def vector(values: tuple[float, ...]) -> str:
        names = "XYZ"
        parts = [f"{'-' if value < 0 else '+'}{names[index]}"
                 for index, value in enumerate(values) if abs(value) > 0.5]
        return "".join(parts) or "?"

    if axes is None:
        return "not measurable" + ("" if matrix is None else ", using the plane-name table")
    seen = f"u->{vector(axes[0])} v->{vector(axes[1])}"
    if matrix is None:
        return f"{seen}, taken as written"
    if matrix == _IDENTITY:
        return f"{seen}, as the recipe means them"
    swapped = abs(matrix[0]) < 0.5
    flips = [name for name, value in (("u", matrix[0] or matrix[1]),
                                      ("v", matrix[2] or matrix[3])) if value < 0]
    change = "axes swapped" if swapped else ""
    if flips:
        change = ", ".join(filter(None, [change, f"{' and '.join(flips)} reversed"]))
    return f"{seen}, {change}"


#: Which Inventor extent enum drills *along* a sketch plane's own normal.
#: Measured with scripts/probe_hole.py on 2027.1: a point on the YZ origin
#: plane, whose normal is +X, with material at x > 0. kNegativeExtentDirection
#: removed 0.3817 cm^3 -- exactly a 9 mm hole 6 mm deep -- and
#: kPositiveExtentDirection removed nothing. So a hole's enum runs opposite to
#: an extrude's, where kPositiveExtentDirection builds along the normal. That
#: is Inventor being sensible on its own terms -- a hole is drilled *into* the
#: face you placed it on -- but it is the opposite of what `direction` means
#: everywhere else in a recipe, so the backend absorbs it here.
_HOLE_ALONG_NORMAL = "kNegativeExtentDirection"
_HOLE_AGAINST_NORMAL = "kPositiveExtentDirection"


def _drilling_side(requested: str, document: Any, sketch: Any,
                   normal: tuple[float, ...] | None) -> tuple[bool, str]:
    """Whether to drill along the sketch normal, and how that was decided.

    An explicit `positive` or `negative` in the recipe is obeyed.  Otherwise
    look for the material: a hole is drilled into the part, and which side the
    part is on is a question about the model, not about the author's intent.
    """
    if requested == "positive":
        return True, "the recipe asked for it"
    if requested == "negative":
        return False, "the recipe asked for it"

    if normal is None:
        return True, "the sketch's normal could not be measured"
    try:
        centroid = document.ComponentDefinition.MassProperties.CenterOfMass
        origin = sketch.SketchToModelSpace(
            sketch.Application.TransientGeometry.CreatePoint2d(0.0, 0.0))
        towards = sum(
            (getattr(centroid, axis) - getattr(origin, axis)) * component
            for axis, component in zip("XYZ", normal)
        )
    except Exception:
        return True, "the material could not be located, so along the normal"
    if abs(towards) < 1e-9:
        return True, "the plane runs through the middle of the part"
    return towards > 0, ("the part lies "
                         f"{'along' if towards > 0 else 'against'} the normal")


def _recompute(document: Any) -> None:  # pragma: no cover - Windows only
    """Bring the document up to date, so a volume reading is the current one."""
    try:
        document.Update()
    except Exception:
        logger.debug("Could not update the document before measuring it.")


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
