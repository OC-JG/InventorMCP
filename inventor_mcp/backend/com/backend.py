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
import re
import math
import os
from contextlib import contextmanager
from itertools import count
from typing import Any, Callable, Iterable, Iterator, Sequence

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
from ...plan import (
    ORIGIN,
    PArc,
    PCircle,
    PEllipse,
    PLine,
    PPoint,
    PText,
    PointRef,
    Ref,
    SketchPlan,
)
from ...units import from_internal, inventor_symbol, unit_from_inventor
from ..base import (
    _same_file_key,
    AppInfo,
    EXPORT_EXTENSIONS,
    EXPORT_OPTIONS,
    EXPORT_TRANSLATORS,
    AxisSpec,
    Backend,
    ChamferRequest,
    CoilRequest,
    CircularPatternRequest,
    DocInfo,
    Driven,
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
    MoveFaceRequest,
    ThickenRequest,
    THICKEN_SHARE,
    promotion_synonyms,
    SketchDrivenPatternRequest,
    DimensionInfo,
    DrawingContents,
    RetrieveRequest,
    ViewInfo,
    ViewRequest,
    EmbossRequest,
    ShellRequest,
    SplitRequest,
    SketchInfo,
    SweepRequest,
    ThreadRequest,
    TopoInfo,
    WorkAxisRequest,
    WorkPlaneRequest,
    WorkPointRequest,
)
from . import holes
from .constants import (
    BOOLEAN_OPERATIONS,
    DISPLAY_MODES,
    EXTENT_DIRECTIONS,
    HEALTH_STATUS_NAMES,
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
#:
#: Since 2026-09-08 the name resolves on every release, because the published
#: HealthStatusEnum page is in the fallback table: 2027.1's library has no such
#: enum, so the table is the only source there will be on it, and it agrees
#: with the measurement below -- 11778 is `kUpToDateHealth`. What that buys is
#: that a sick feature is now reported *by name* (`kInErrorHealth`,
#: `kCannotComputeHealth`) rather than as a bare number.
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


#: Where a pattern feature might keep its occurrences, strongest first.
#:
#: Unmeasured, and named rather than guessed at the call site so a run that
#: answers with the second one says so. `Occurrences` is what the roadmap and
#: `INVENTOR_SETUP.md` both name as the property that would settle the one open
#: question about `sketch_driven_pattern` -- whether Inventor places an
#: occurrence on the reference point as well -- and `PatternElements` is what
#: some of Inventor's pattern features call the same thing.
_OCCURRENCE_COLLECTIONS = ("Occurrences", "PatternElements")


def _occurrence_count(feature: Any) -> tuple[int | None, str | None]:  # pragma: no cover - Windows only
    """How many occurrences a pattern feature holds, and what said so.

    None when nothing answered, because a pattern is *one* feature holding its
    occurrences: counting the features on a part cannot count them, which is
    why the calibration check could measure `spread_pockets` exactly and still
    not answer what it was built to ask. A feature that is not a pattern
    answers nothing here, which is correct rather than an error.
    """
    for name in _OCCURRENCE_COLLECTIONS:
        try:
            collection = getattr(feature, name)
            if collection is None:
                continue
            return int(collection.Count), name
        except Exception:
            continue
    return None, None


def _promotion_candidates(prop: str, offered: Sequence[str]
                          ) -> tuple[list[str], list[str]]:
    """Which of Inventor's property names a requested *prop* could mean.

    Two lists, because the caller treats them differently. The first holds the
    names that *are* the request -- Inventor's own spelling of it, and the
    alias in `PROMOTION_ALIASES` where a recipe uses a different word -- and
    the first of those the object carries is the answer. The second holds
    names that merely start with the request, which is an inference: it is
    acted on only when exactly one of them is there, so `counterbore` is a
    question rather than a silent choice between a diameter and a depth.

    The stages have to stay in that order. `count` is a pattern's own property
    and also the start of `CounterboreDepth`, and a pattern's count is not a
    counterbore.

    Case and underscores are ignored throughout, because `taper_angle` and
    `TaperAngle` are the same request written twice.
    """
    def flatten(text: str) -> str:
        return text.strip().lower().replace("_", "")

    words = promotion_synonyms(prop)
    if not words:
        return [], []
    named = [name for name in offered if flatten(name) in words]
    inferred = [name for name in offered
                if name not in named
                and any(flatten(name).startswith(word) for word in words)]
    return named, inferred


def _move_face_type(definition: Any) -> int | None:  # pragma: no cover - Windows only
    """A move-face definition's `MoveFaceType`, or None if it will not say."""
    try:
        return int(definition.MoveFaceType)
    except Exception:
        return None


def _parameter_names(obj: Any, member: str) -> list[str]:
    """What a live COM object calls *member*'s parameters, or an empty list.

    Inventor's type *library* does not publish every class -- `move_face`'s and
    `sketch_driven_pattern`'s definitions are both absent from it, so
    `scripts/com_signatures.py` can say nothing about either -- but the object
    in hand still answers `ITypeInfo`, and `GetNames(memid)` returns a member's
    name followed by its parameters' names. That is the only source there is for
    what an argument *means*, as opposed to how many there are.

    **The member's own name is dropped**, so index 0 is the first parameter.
    `GetNames` puts the method name at the front and the first version of this
    returned the tuple whole, which made `names[2]` the *second* argument while
    reading as the third -- an off-by-one that would have passed a value into a
    slot nobody had identified. The whole point of resolving by name is to make
    that impossible, so the shape that invited it is gone.

    Empty on any failure rather than raising: a caller asking what a parameter
    is called is about to refuse politely, and a second exception on the way to
    a good error message would replace it with a worse one.
    """
    try:
        info = obj._oleobj_.GetTypeInfo()
        attr = info.GetTypeAttr()
    except Exception:
        return []
    for index in range(attr.cFuncs):
        try:
            desc = info.GetFuncDesc(index)
            names = info.GetNames(desc.memid)
        except Exception:
            continue
        if names and str(names[0]) == member:
            return [str(name) for name in names[1:]]
    return []


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
        #: doc_id -> sketch name -> recipe label -> the Inventor entity.
        #: `build_sketch` is handed a label per named primitive and Inventor
        #: hands back an entity per primitive; nothing was keeping the two
        #: together, so every lookup by label went looking for an Inventor
        #: *name* that no code had ever assigned. See `_labelled_entity`.
        self._sketch_entities: dict[str, dict[str, dict[str, Any]]] = {}
        self._topology: dict[str, dict[str, Any]] = {}
        #: One `ReferenceKeyManager` key context per document, or `False`
        #: for a document whose release would not make one. Kept rather
        #: than made per call because the published rule is that a B-Rep
        #: key needs the context it was made with to be bound back.
        self._key_contexts: dict[str, Any] = {}
        #: Drawing dimensions this session retrieved, by document, each with
        #: the model parameter it was retrieved for. See `_retrieve_by_parameter`.
        self._retrieved: dict[str, list[tuple[Any, str]]] = {}
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
            _update(document)

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
        self._sketch_entities.clear()
        self._topology.clear()
        self._key_contexts.clear()
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
        self._sketch_entities[doc_id] = {}
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
        """Give a value the feature already held a name, so it can be driven.

        **The property name a caller asks for is a recipe's name, not
        Inventor's**, and the two are not always the same word. A recipe says
        `taper`; Inventor's `ExtrudeDefinition` calls it `TaperAngle`. The
        simulator matches against its own detail dictionary, which is keyed by
        the recipe's field names, so `promote_parameter(..., "taper", ...)`
        worked there and failed here -- measured on 2027.1, 2026-09-08: *"The
        feature 'Block' has no drivable property 'taper'."* Two
        self-consistent halves disagreeing is how defect 5 survived three runs,
        so the resolution is `_promotion_candidates` and a test pins the words
        a recipe can use against the names Inventor answers to.

        The extent is the third place to look. An extrude's taper is on its
        definition and its *distance* is not: that is on
        `definition.Extent.Distance`, because the extent is its own object and
        a through-all extent has no distance at all.
        """
        document = self._doc(doc_id)
        held = _dynamic(_find_feature(document.ComponentDefinition.Features, feature))
        definition = getattr(held, "Definition", None)
        definition = None if definition is None else _dynamic(definition)
        extent = None if definition is None else getattr(definition, "Extent", None)
        holders = [holder for holder in
                   (held, definition, None if extent is None else _dynamic(extent))
                   if holder is not None]
        named, inferred = _promotion_candidates(prop, self._DRIVING)
        found = self._offered_parameters(holders, named)
        if len(found) > 1:
            # `taper` reaches both `Taper` and `TaperAngle`; they are one
            # request under two spellings, so the first one the object carries
            # is the answer rather than an ambiguity.
            found = dict([next(iter(found.items()))])
        if not found:
            # The inferred spelling, and only where it is unambiguous. Never a
            # silent pick: `counterbore` reaches both a diameter and a depth,
            # and promoting the wrong one names a value that drives something
            # else -- which then reads as the part changing by itself the next
            # time that parameter is set.
            found = self._offered_parameters(holders, inferred)
            if len(found) > 1:
                raise FeatureError(
                    f"{prop!r} could mean {' or '.join(sorted(found))} on "
                    f"{feature!r}, and promoting the wrong one names the wrong "
                    "value.",
                    hint="Ask for the property by Inventor's own name -- "
                    "`describe_feature` lists what this feature carries.",
                )
        if not found:
            raise FeatureError(
                f"The feature {feature!r} has no drivable property {prop!r}.",
                hint="It carries " + (
                    ", ".join(self._driving_properties(holders))
                    or "no drivable property this release could read")
                + ". `describe_feature` reads them with their values.",
            )
        read_from, target = next(iter(found.items()))
        currently = str(target.Expression)
        # The new parameter holds exactly what the property held -- a literal
        # keeps its unit, a model-parameter reference keeps its reference -- so
        # the geometry after the promotion is the geometry before it.
        #
        # **In the property's own units**, which is not a detail. `set_parameter`
        # defaults to millimetres, and a taper's expression is `1.5 deg`:
        # `UserParameters.AddByExpression('draft_a', '1.5 deg', 'mm')` is a
        # length parameter being handed an angle, and Inventor refuses it with
        # a bare "Exception occurred." Measured on 2027.1, 2026-09-08 -- the
        # second failure of the same promotion, after the property name.
        info = self.set_parameter(doc_id, name, currently,
                                  units=_parameter_units(target))
        target.Expression = name
        return {
            "parameter": name,
            "value": info.value,
            "units": info.units,
            "was": currently,
            "now_drives": f"{feature}.{read_from}",
        }

    def _offered_parameters(self, holders: list[Any],
                            names: Sequence[str]) -> dict[str, Any]:  # pragma: no cover - Windows only
        """Which of *names* these objects carry a settable parameter for.

        A property that is there but holds no `Expression` is not one of them:
        that is the difference between a dimension and a plain number, and only
        a dimension can be given a name to be driven by.
        """
        found: dict[str, Any] = {}
        for holder in holders:
            for candidate in names:
                if candidate in found:
                    continue
                try:
                    value = getattr(holder, candidate)
                except Exception:
                    continue
                if value is not None and hasattr(value, "Expression"):
                    found[candidate] = value
        return found

    def _driving_properties(self, holders: list[Any]) -> list[str]:  # pragma: no cover - Windows only
        """Every drivable property these objects carry, for a refusal's hint.

        A list of what is there beats "describe_feature lists what it carries"
        by exactly the round trip it saves, and the wrong-word case is the one
        that refusal exists for.
        """
        return list(self._offered_parameters(holders, self._DRIVING))

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

        # 4. Inventor's own graph, as a second source rather than a
        #    replacement. `Parameter.DrivenBy` and `Parameter.Dependents` are
        #    documented, and everything above this point reconstructs the same
        #    relation by *parsing expressions* -- which is the thing the freeze
        #    guard rests on, so a disagreement between the two is worth having
        #    rather than choosing between. The parser stays the answer: it
        #    works on the simulator too, and it is what `rehearse` uses before
        #    any seat is involved.
        graph = self._parameter_graph(doc_id, feature, known)
        answer: dict[str, Any] = {
            "parameters": sorted(via, key=str.lower),
            "via": {parameter: sorted(where) for parameter, where in via.items()},
        }
        if graph is not None:
            answer["inventors_graph"] = sorted(graph, key=str.lower)
            missed = sorted(graph - set(via), key=str.lower)
            extra = sorted(set(via) - graph, key=str.lower)
            if missed or extra:
                answer["graph_disagrees"] = {
                    "inventor_says_also": missed,
                    "expressions_say_also": extra,
                    "why_it_matters": (
                        "The freeze guard reads dependencies by parsing "
                        "expressions, and this is Inventor's own answer to the "
                        "same question. A parameter only Inventor lists is one "
                        "a freeze on this feature would not have protected; "
                        "one only the parser lists is a reference Inventor does "
                        "not count, which is usually a dimension that drives no "
                        "geometry. Neither is wrong on its face -- they are "
                        "different questions -- but a difference here is worth "
                        "reading before trusting a freeze."
                    ),
                }
        return answer

    def _parameter_graph(self, doc_id: str, feature: Any,
                         known: dict[str, str]) -> set[str] | None:  # pragma: no cover - Windows only
        """Which user parameters Inventor itself says this feature depends on.

        `Parameter.DrivenBy` is what a parameter is computed from and
        `Dependents` what is computed from it, both documented and neither
        measured here. None where the release will not answer at all, which is
        the difference between "Inventor says nothing depends on this" and
        "Inventor was not asked" -- and the caller reports the second as an
        absence rather than as agreement.
        """
        found: set[str] = set()
        answered = False
        try:
            parameters = feature.Parameters
            count = int(parameters.Count)
        except Exception:
            return None
        for index in range(1, count + 1):
            try:
                parameter = _dynamic(parameters.Item(index))
            except Exception:
                continue
            for route in ("DrivenBy", "Dependents"):
                try:
                    related = getattr(parameter, route)
                    total = int(related.Count)
                except Exception:
                    continue
                answered = True
                for step in range(1, total + 1):
                    try:
                        name = str(getattr(related.Item(step), "Name", "") or "")
                    except Exception:
                        continue
                    if name.lower() in known:
                        found.add(known[name.lower()])
        return found if answered else None

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

    def document_at_path(self, path: str) -> tuple[str | None, str] | None:  # pragma: no cover
        """Which open document occupies *path*, cheaply.

        One ``FullFileName`` read per open document and nothing else on the miss
        path, which is the normal one. ``list_documents`` cannot be used here:
        it reads six properties per document, scans the held handles by COM
        identity for each, and registers every document it did not recognise --
        so on the session this was found on, with 1033 documents open behind an
        assembly, a single save would have minted a thousand handles and left
        the next call comparing a million COM identities.

        Only a match costs more, and then only for that one document: its
        display name, and a scan of the *held* handles -- a handful -- to see
        whether this session has an id for it. No id means the user opened it in
        Inventor's UI, which the caller reports differently because there is no
        handle to close by.
        """
        app = self._require_app()
        target = _same_file_key(path)
        documents = app.Documents
        for index in range(1, int(documents.Count) + 1):
            document = documents.Item(index)
            try:
                full = str(document.FullFileName)
            except Exception:
                # An unsaved document has no file name on some releases and
                # raises rather than returning empty. It cannot hold a path.
                continue
            if not full or _same_file_key(full) != target:
                continue
            for held_id, held in self._documents.items():
                if _same_com_object(document, held):
                    return held_id, str(document.DisplayName)
            return None, str(document.DisplayName)
        return None

    def activate_document(self, doc_id: str) -> DocInfo:  # pragma: no cover - Windows only
        document = self._doc(doc_id)
        document.Activate()
        return DocInfo(id=doc_id, name=str(document.DisplayName), active=True)

    def save_document(self, doc_id: str, path: str | None = None) -> DocInfo:  # pragma: no cover
        document = self._doc(doc_id)
        # Before the write, not after: Inventor's refusal to overwrite a file it
        # has open is a bare "Exception occurred" that names nothing -- defect 3.
        self.refuse_a_path_another_document_holds(doc_id, path)
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
            self._sketch_entities.pop(doc_id, None)

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
        # `_batch` is what calls `document.Update()`, and until 2026-09-07 this
        # was the only mutating call outside one -- so a parameter change set the
        # expression and rebuilt nothing, and every measurement afterwards was of
        # the part as it had been. Measured: the work-axis bolt circle's centre of
        # mass moved 0.00000 mm on `bolt_x` 30 -> 45, against a derived 0.18640.
        # It reaches further than work axes: `set_parameters` is the tool that
        # says "change a driving dimension; the model updates", and the DFM loop
        # drives a parameter and then re-measures.
        with self._batch(document), self._translate_errors(
                f"Setting parameter {name!r}", ParameterError):
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
                f"objecting to the name {name!r} itself. {_why_a_name_is_refused(name)}")

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
    def _project_entities(self, sketch: Any, doc_id: str,
                          borrowed: Sequence[TopoInfo]) -> int:  # pragma: no cover
        """Project model entities into *sketch*, and make them bound material.

        `PlanarSketch.AddByProjectingEntity(Entity)` is the published call, one
        entity at a time -- there is no collection form. What it hands back is
        marked **`Reference = True`**, and reference geometry bounds no
        material: an extrude from a loop of it finds no profile. That flag is
        the one detail this whole feature turns on, so it is cleared, and a
        release that will not clear it is a hard error rather than a sketch
        that looks right and builds nothing.

        The projected curve stays *associative* either way -- it follows the
        edge it came from -- which is the point of projecting rather than
        copying coordinates.
        """
        added = 0
        with self._translate_errors("Projecting model geometry", SketchError):
            for match in borrowed:
                entity = self._live(doc_id, match.id)
                made = sketch.AddByProjectingEntity(entity)
                added += 1
                try:
                    if bool(made.Reference):
                        made.Reference = False
                except Exception as exc:
                    raise SketchError(
                        f"Projected {match.description} into the sketch and "
                        "could not clear its reference flag: "
                        f"{_com_message(exc)}",
                        hint="Reference geometry bounds no material, so a "
                        "profile built from it would come back empty and the "
                        "feature after it would find nothing to sweep. This is "
                        "refused rather than left, because the sketch would "
                        "look right.",
                    ) from exc
        return added

    def build_sketch(self, doc_id: str, plan: SketchPlan) -> SketchInfo:  # pragma: no cover
        document = self._doc(doc_id)
        component = document.ComponentDefinition
        app = self._require_app()
        transient = app.TransientGeometry

        plane = self._resolve_plane(doc_id, document, plan.plane, plan.offset_expression)
        # Resolved before the batch opens, so a selector that matches nothing
        # fails with the recipe's own message and no half-built sketch behind
        # it.
        borrowed = (self._topology_selection(doc_id, plan.project)[1]
                    if plan.project is not None else [])
        with self._batch(document):
            with self._translate_errors("Creating the sketch", SketchError):
                # `PlanarSketches.Add(PlanarEntity, UseFaceEdges)`: the second
                # argument projects the face's own outline at creation, which
                # is the only place it can be asked for -- there is no
                # after-the-fact call for a whole face. It is meaningless on
                # anything but a face, and the schema has already refused
                # `use_face_edges` on a plane that is not one.
                sketch = component.Sketches.Add(plane, bool(plan.use_face_edges))
                if plan.name:
                    sketch.Name = plan.name
            projected = _count_curves(sketch) if plan.use_face_edges else 0
            if borrowed:
                projected += self._project_entities(sketch, doc_id, borrowed)

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
        self._sketch_entities.setdefault(doc_id, {})[str(sketch.Name)] = \
            _entities_by_label(plan, objects)
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
        # What the refusals cost, asked of the finished sketch rather than
        # asserted when each one happened. Inventor has no degrees-of-freedom
        # count for a sketch (see below), so `ConstraintStatus` is the whole of
        # the evidence: fully constrained means every refusal was redundant.
        constrained = _fully_constrained(sketch, self._constants)
        if refused:
            names = ", ".join(refused)
            if constrained is True:
                logger.info(
                    "Sketch %s: %d constraint(s) refused (%s) and the sketch is "
                    "fully constrained, so each was redundant.",
                    sketch.Name, len(refused), names)
            elif constrained is False:
                logger.warning(
                    "Sketch %s: %d constraint(s) refused (%s) and the sketch is "
                    "NOT fully constrained, so a degree of freedom is left in "
                    "it. A parameter change can move geometry the recipe did "
                    "not mean to move.", sketch.Name, len(refused), names)
            else:
                logger.warning(
                    "Sketch %s: %d constraint(s) refused (%s) and this release "
                    "would not say whether the sketch is fully constrained, so "
                    "whether that cost a degree of freedom is unknown.",
                    sketch.Name, len(refused), names)
        return SketchInfo(
            id=self._next("sk"),
            name=str(sketch.Name),
            plane=plan.plane,
            entities=len(plan.primitives),
            constraints=len(plan.constraints),
            dimensions=len(plan.dimensions),
            profiles=profiles,
            hole_centers=len(plan.hole_centers),
            # `degrees_of_freedom` stays None here, and that is deliberate:
            # Inventor exposes no such count for a sketch. `ConstraintStatus`
            # is a four-value enum, and the only `GetDegreesOfFreedom` in the
            # whole API belongs to `ComponentOccurrence` -- an assembly
            # occurrence's rigid-body freedoms, nothing to do with a sketch.
            # Measured on 2027.1; see `_fully_constrained` and
            # docs/INVENTOR_SETUP.md. The simulator's number is an estimate it
            # can make because it does no solving; Inventor will not be asked
            # to guess one.
            fully_constrained=constrained,
            inferred_constraints=len(inferred),
            refused_constraints=len(refused),
            driving_dimensions=len(driving),
            refused_dimensions=len(refused_dimensions),
            driven_parameters=_driven_parameters(plan, driving),
            undriven_expressions=list(plan.undriven_expressions),
            # Counted from the sketch rather than from the plan: with
            # `use_face_edges` it is Inventor that decided how many curves the
            # face's outline came to, and a recipe that asked for an outline
            # and got nothing is exactly what this number is for.
            projected=projected,
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
            styled = _style_override(primitive)
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
        # sometimes rejects one as dependent on the constraints around it, so
        # it is reported rather than treated as fatal.
        #
        # **And nothing is claimed here about what it cost**, which is a
        # correction. This used to say "the sketch keeps a degree of freedom",
        # and printed that on every live run of `hex_standoff`: a hexagon's
        # sixth equal-length pair is redundant once the other five and the
        # across-flats dimension are in, so Inventor rejects it and the sketch
        # is fully constrained without it. A refusal *can* leave a freedom
        # behind and this is not the place to tell the two apart -- the sketch
        # is still being built, so a reading taken now would be of an
        # unfinished sketch. `build_sketch` asks the finished one, where the
        # answer means something.
        logger.info("Sketch %s: %s was refused (%s).",
                    sketch.Name, where, self._explain(first_error))
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
                    # No `degrees_of_freedom`: Inventor has no count to give.
                    # See the note on the other `SketchInfo` above.
                    fully_constrained=_fully_constrained(sketch, self._constants),
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
    def _resolve_plane(self, doc_id: str, document: Any, reference: str,
                       offset_expression: str | None) -> Any:  # pragma: no cover
        component = document.ComponentDefinition
        base: Any
        key = reference.lower()
        if key in ("xy", "xz", "yz"):
            base = _origin_plane(component, key)
        elif reference.startswith("face:"):
            handle = reference.split(":", 1)[1]
            # Through `_live`, so a handle from before a rebuild is rebound
            # from its reference key or refused -- not handed back dead, which
            # is what happened until 2026-09-09 while the hint here said
            # handles expire.
            base = self._live(doc_id, handle)
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

    def _labelled_entity(self, doc_id: str, sketch_name: str,
                         label: str) -> Any | None:  # pragma: no cover - Windows only
        """The Inventor entity a recipe label names, from what `build_sketch` kept.

        This exists because the three lookups that used to do this searched
        Inventor's own ``SketchPoints`` and ``SketchLines`` for an entity whose
        ``Name`` equalled the recipe's label -- and **nothing ever set those
        names**. ``_add_primitive`` sets ``Construction``, ``HoleCenter`` and
        ``Centerline`` and has never read ``primitive.label``, so the labels
        lived only in the `SketchPlan` on the Python side. Every such lookup was
        therefore searching for a name that could not be there. The simulator
        reads the plan's labels directly, which is why all of it passed offline
        and the first live run failed on the first check.

        Returning the handle Inventor gave us at creation avoids the question of
        whether a sketch entity's ``Name`` can be assigned at all, which nothing
        here has measured. Callers keep the old name search as a fallback: if a
        stored handle has gone stale it is no worse than the behaviour it
        replaces, and the error then names both routes.
        """
        return (self._sketch_entities.get(doc_id, {})
                .get(sketch_name, {})
                .get(label))

    def _resolve_axis(self, doc_id: str, axis: AxisSpec) -> Any:  # pragma: no cover
        document = self._doc(doc_id)
        component = document.ComponentDefinition
        if axis.kind == "work_axis":
            index = {"x": 1, "y": 2, "z": 3}.get(axis.value.lower())
            if index is None:
                return _named_work_axis(component, axis.value)
            return component.WorkAxes.Item(index)
        if axis.kind == "edge":
            return self._live(doc_id, axis.value)
        sketch = self._sketch(doc_id, axis.sketch or "")
        kept = self._labelled_entity(doc_id, str(sketch.Name), axis.value)
        if kept is not None:
            return kept
        for index in range(1, int(sketch.SketchLines.Count) + 1):
            line = sketch.SketchLines.Item(index)
            if str(getattr(line, "Name", "")) == axis.value:
                return line
        raise FeatureError(
            f"Sketch {axis.sketch!r} has no line named {axis.value!r} to revolve about.",
            hint="Give the sketch line a `name` in the recipe and reference it here. "
                 "This session did not keep an entity under that label either, so "
                 "the sketch was built before the label was recorded, or by a "
                 "different session.",
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
            elif request.extent == "to":
                # `SetToExtent(ToEntity, [ExtendToFace])`, published and
                # unmeasured. **No direction argument**: which way the sweep
                # runs is decided by where the target is, which is the point of
                # asking for a target rather than a distance -- so `direction`
                # is ignored here and the schema says so.
                #
                # `ExtendToFace` is left at Inventor's default. It says what to
                # do when the profile does not fully meet the target: extending
                # the *face* to catch it is a helpful guess that quietly makes a
                # feature bigger than the model justifies, and nothing here has
                # measured which way Inventor defaults.
                assert request.to is not None
                definition.SetToExtent(self._extent_target(doc_id, document, request.to))
            elif request.extent == "from_to":
                # `SetFromToExtent(FromFace, ExtendFromFace, ToFace,
                # ExtendToFace)`. The two booleans are documented without
                # brackets, so they are supplied rather than defaulted, and
                # False is the conservative pair: neither face is extended to
                # catch a profile that misses it. A feature that silently grew
                # to meet a face it did not reach is the kind of success this
                # server exists not to report.
                assert request.to is not None and request.start is not None
                definition.SetFromToExtent(
                    self._extent_target(doc_id, document, request.start), False,
                    self._extent_target(doc_id, document, request.to), False)
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
            "extent": request.extent,
            "distance": request.distance.as_dict() if request.distance else None,
            "to": request.to,
            "from": request.start,
        })

    def _extent_target(self, doc_id: str, document: Any,
                       reference: str) -> Any:  # pragma: no cover
        """What a `to` or `from_to` extent stops at.

        The same three vocabularies a sketch plane accepts -- an origin plane,
        a named work plane, a `face:` handle -- because "up to the underside of
        the lid" is a face as often as it is a plane, and a caller should not
        have to know which of the two the server wants. `SetToExtent` is
        documented to take a face, a work plane, a vertex or a work point, so
        the wider vocabulary is Inventor's own.
        """
        return self._resolve_plane(doc_id, document, reference, None)

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

            if request.bodies:
                # Unlike `extrude`, a hole is built by `HoleFeatures.Add...`
                # rather than from a definition object, so there is nothing to
                # set `AffectedBodies` on before the feature exists. It is set
                # afterwards instead, and a release that will not take it is a
                # hard error rather than a warning: the hole is built either
                # way, and one on the wrong body has removed real material from
                # a part that now looks finished. Unmeasured -- see the work
                # axis note in `docs/INVENTOR_SETUP.md` for the standing
                # caveat about calls written without an Inventor to try them.
                _aim_at_bodies(self._require_app(), document.ComponentDefinition,
                               feature, request.bodies)

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
            "bodies": list(request.bodies) or None,
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
        return self._topology_selection(doc_id, selector, required=required)[0]

    def _topology_selection(self, doc_id: str, selector: ResolvedSelector, *,
                            required: bool = True
                            ) -> tuple[Any, list[TopoInfo]]:  # pragma: no cover
        """The collection Inventor wants, and what was matched, from one select.

        Split out for `thicken`, which needs the faces' *areas* as well as the
        faces: what it predicts Inventor will do is the sum of those areas times
        the layer, and checking the result against that prediction is what makes
        a mis-called COM method loud. Two selects would be two chances to match
        differently, so both come out of one.
        """
        matches = self.select(doc_id, selector)
        if not matches and required:
            raise SelectionError(
                f"The selector matched no {selector.kind}s.",
                hint="Call `select_topology` with the same selector to see the alternatives.",
                selector=selector.__dict__,
            )
        collection = self._new_collection(selector.kind)
        for match in matches:
            collection.Add(self._live(doc_id, match.id))
        return collection, matches

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
                edges.Add(self._live(doc_id, match.id))
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
        plane = self._resolve_plane(doc_id, document, request.plane, None)
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

    #: How a move-face definition might be told a direction and a distance,
    #: tried in this order. Every other feature in this file names one call it
    #: was measured making; this one names three, because nobody has read the
    #: signature off a type library yet -- `docs/FEATURE_COVERAGE.md` records
    #: only that `MoveFaceFeatures` has `Add` and `CreateDefinition`, and not
    #: what the definition's setter is called.
    #:
    #: A list of attempts is the shape `_profiles` already uses for a call whose
    #: arguments Inventor accepts in more than one form, and it is safe here for
    #: a reason worth stating: the two arguments cannot be swapped silently. A
    #: direction is a COM object and a distance is an expression string, so a
    #: wrong order is a type mismatch rather than a part that builds wrongly.
    #: Every candidate here therefore means *direction and distance* and nothing
    #: else -- a free-drag or point-to-point setter takes different arguments
    #: with different meanings, and one of those accepting these two by accident
    #: is exactly the quietly wrong part this file refuses to risk.
    #: What a list cannot rule out is a *third* argument whose default means
    #: something, which is why `scripts/com_signatures.py --search MoveFace` is
    #: named in the failure and in `docs/INVENTOR_SETUP.md`.
    #: The setter and its arguments **in Inventor's order**, measured on 2027.1
    #: on 2026-09-08 by reading the live `MoveFaceDefinition`'s own type
    #: information -- name, arity and parameter names all from `ITypeInfo`,
    #: since the type library publishes no class for this object at all:
    #:
    #:     SetDirectionAndDistanceMoveType(Distance, Direction, DirectionReversed)
    #:
    #: Not a candidate list any more. The definition offers exactly three
    #: move-type setters and the other two are measured to be something else --
    #: `SetPlanarMoveType(PointOne, PointTwo, Plane)` is point-to-point and
    #: `SetFreeMoveType(Transformation)` takes a matrix -- so excluding them was
    #: right, and now provably rather than presumably.
    #:
    #: Two things here were guessed wrong before the read, and both would have
    #: cost a run. **The name**: none of `SetDirectionAndDistance`,
    #: `SetDirectionMove` or `SetDirectionAndDistanceMoveData` was it. **The
    #: order**: the distance comes *first*. A swap raises rather than building
    #: something wrong -- the distance is an expression string and the direction
    #: is a COM object -- but "it would have raised" is a poor substitute for
    #: knowing, which is why the order lives here as data and
    #: `tests/test_move_face.py` pins it.
    _MOVE_FACE_SETTER = "SetDirectionAndDistanceMoveType"

    #: In the measured order. `DirectionReversed` is what `flip` goes into --
    #: see `move_face`, which no longer negates the distance expression.
    _MOVE_FACE_SETTER_ARGUMENTS = ("Distance", "Direction", "DirectionReversed")

    def move_face(self, doc_id: str, request: MoveFaceRequest) -> FeatureInfo:  # pragma: no cover
        """Translate faces of an existing solid along a direction.

        **Measured against Inventor 2027.1 on 2026-09-08**, after three
        failures that were all about the call and none about the arithmetic.
        Both fixtures came in exact, and doubling each driving parameter
        doubled the change -- so the expression reaches Inventor's own
        dimension and the feature is parametric in fact rather than in name.

        The setter's name and argument order came off the live object's own
        `ITypeInfo`, and the published page says the same thing:
        `SetDirectionAndDistanceMoveType(Distance As Variant, Direction
        As Object, [DirectionReversed] As Boolean)` -- the distance *first*,
        as a value in centimetres or a string for which "a parameter for this
        value will be created", the direction a `WorkAxis`, a linear `Edge` or a
        planar `Face`, and the sign a flag. So the expression goes in as
        written, `flip` goes in as the flag rather than as a negated
        expression, and a sketch line is refused as a direction before Inventor
        is asked. The setter moves `MoveFaceType` from its initial
        `kFreeMoveType` to `kDirectionAndDistanceMoveType`, and that property is
        read back before `Add`, because a setter that was accepted and left the
        type alone would build a feature that moves nothing along nothing.

        The volume before and after is read and reported, because a move-face
        that moved nothing is this operation's version of a cut that met no
        material -- Inventor builds the feature either way. It is reported
        rather than raised on: a face slid along its own plane legitimately
        changes no volume, and the simulator is the half that knows which case
        this is.
        """
        document = self._doc(doc_id)
        faces = self._topology_collection(doc_id, request.faces)
        if int(faces.Count) == 0:
            raise FeatureError(
                "No faces matched, so there is nothing to move.",
                hint="Run `select_topology` with the same selector to see what it matches.",
            )
        direction = self._resolve_axis(doc_id, request.direction)
        if request.direction.kind not in ("work_axis", "edge"):
            raise FeatureError(
                f"A move-face direction has to be a work axis or an edge, not the "
                f"sketch line {request.direction.value!r}.",
                hint="The published page for SetDirectionAndDistanceMoveType says "
                "its Direction is a WorkAxis, a linear Edge or a planar Face. The "
                "live read gave the name and the order and says nothing about the "
                "types, so this one comes off the page. Use 'x', 'y', 'z', a named "
                "work_axis, or an `edge:` handle from select_topology.",
            )
        # The expression, unnegated. `flip` used to be `-(expression)` because
        # no reversal property had been read; the measured signature's third
        # argument is `DirectionReversed`, which is the API's own way of saying
        # it and leaves the dimension's expression as the caller wrote it.
        distance = request.distance.expression
        before = _solid_volume(document)
        features = document.ComponentDefinition.Features.MoveFaceFeatures
        with self._batch(document), self._translate_errors("MoveFace"):
            definition, made_by = self._move_face_definition(features, faces)
            setter = getattr(definition, self._MOVE_FACE_SETTER, None)
            if setter is None:
                raise FeatureError(
                    "This release's MoveFaceDefinition has no "
                    f"{self._MOVE_FACE_SETTER}.",
                    hint="Measured on 2027.1: that is the setter, taking three "
                    "arguments. Ask this release what it has instead with "
                    "`python scripts/probe_definitions.py`. What it offered "
                    "this time: " + self._move_face_offered(definition),
                )
            self._check_move_face_arguments(definition)
            was = _move_face_type(definition)
            try:
                _call_named(setter, list(zip(
                    self._MOVE_FACE_SETTER_ARGUMENTS,
                    (distance, direction, request.flip))))
            except Exception as exc:
                raise FeatureError(
                    f"{self._MOVE_FACE_SETTER} refused: {_com_message(exc)}",
                    hint=f"Called as ({distance!r}, the direction, "
                    f"{request.flip!r}) -- Inventor's own order, distance "
                    "first. `python scripts/probe_definitions.py` prints the "
                    "parameter names this release gives.",
                ) from exc
            self._require_direction_and_distance_type(definition, was)
            try:
                feature = features.Add(definition)
            except Exception as exc:
                raise FeatureError(
                    f"Move face failed: {self._explain(exc)}",
                    hint=f"{int(faces.Count)} face(s) {distance!r} along "
                    f"{request.direction.value!r}"
                    + (" reversed" if request.flip else "")
                    + f", with a definition from {made_by}. A move that would "
                    "make the solid "
                    "self-intersecting, or that carries a face away from the "
                    "neighbours it has to stretch, will refuse.",
                ) from exc
            if request.name:
                feature.Name = request.name
        after = _solid_volume(document)
        return _feature_info(feature, "move_face", {
            "faces": int(faces.Count),
            "direction": request.direction.value,
            "distance": request.distance.as_dict(),
            "flip": request.flip,
            # Which mechanism carried the flip, because it changed: the
            # expression used to be negated and now `DirectionReversed` does
            # it, so a part built before this reads the same and got there
            # differently.
            "flip_via": "DirectionReversed",
            "definition_from": made_by,
            "volume_change_cm3": (
                None if before is None or after is None else round(after - before, 6)
            ),
        })

    def _require_direction_and_distance_type(self, definition: Any,
                                             was: int | None) -> None:  # pragma: no cover
        """Refuse a definition the setter did not turn into a direction-and-distance move.

        The live read is what makes this checkable: `MoveFaceType` is read-only
        and reports 91395 (`kFreeMoveType`) on a fresh definition, and
        `MoveFaceTypeDefinition` is None until a setter has been called -- so
        calling one *makes* the definition that kind, and the type afterwards is
        the setter's own receipt. A definition still at free-move after a setter
        that raised nothing is a move defined by no matrix, and whatever `Add`
        made of it would not be the move that was asked for.

        Two ways this stays quiet rather than refusing. A release that will not
        report the type at all is let through -- the fixtures catch a wrong move
        by its volume, and refusing every move over a missing property would be
        the shell-`both` mistake again. And a definition that *already* read as
        direction-and-distance before the call is not evidence either way, so
        only a definition that started somewhere else and did not move is
        refused.
        """
        wanted = self._k("kDirectionAndDistanceMoveType")
        now = _move_face_type(definition)
        if now is None or now == wanted or was == wanted:
            return
        raise FeatureError(
            f"The move-face definition reports MoveFaceType {now}, not "
            f"kDirectionAndDistanceMoveType ({wanted}), after "
            f"{self._MOVE_FACE_SETTER} returned without complaint.",
            hint="Measured on 2027.1: a fresh definition reads 91395 "
            "(kFreeMoveType) and the setter is what changes it, so this says the "
            "setter did not take. `python scripts/probe_definitions.py` prints "
            "what this release's definition offers.",
        )

    def _check_move_face_arguments(self, definition: Any) -> None:  # pragma: no cover
        """Refuse if this release's setter does not take the measured arguments.

        `_MOVE_FACE_SETTER_ARGUMENTS` is a measurement, and a measurement stated
        in code and never checked against the thing measured is the drift this
        repository writes tests about. Here the thing measured can be asked
        directly: `ITypeInfo`'s `GetNames` returns a member's name followed by
        its parameters' names, so the recorded order and the live one are
        comparable at the moment of use.

        Belt and braces rather than the only guard -- the distance is an
        expression string and the direction is a COM object, so a swapped pair
        raises a type mismatch instead of building something wrong. What this
        catches is the case that would *not* raise: a release that reordered or
        renamed the reversal flag, where a boolean lands in a slot that means
        something else. That is a part built wrongly, and no tolerance catches
        it.

        A release that will not answer at all is allowed through. The refusal
        that matters is a *disagreement*; silence is what `_parameter_names`
        returns for a hostile object and for one whose type information is
        simply unavailable, and refusing on silence would break a release for
        being reticent.
        """
        parameters = _parameter_names(definition, self._MOVE_FACE_SETTER)
        if not parameters:
            return
        if tuple(parameters) != self._MOVE_FACE_SETTER_ARGUMENTS:
            raise FeatureError(
                f"This release's {self._MOVE_FACE_SETTER} takes "
                f"({', '.join(parameters)}), and this code was written against "
                f"({', '.join(self._MOVE_FACE_SETTER_ARGUMENTS)}).",
                hint="Refusing rather than calling it anyway: the third "
                "argument is a reversal flag, and a boolean accepted in a slot "
                "that means something else is a part built wrongly. Read what "
                "this release wants with `python scripts/probe_definitions.py` "
                "and update `_MOVE_FACE_SETTER_ARGUMENTS`.",
            )

    def _move_face_offered(self, definition: Any) -> str:  # pragma: no cover
        """What the live definition offers, as text, for a refusal to carry.

        The failure carries this so that **one live run is the probe**. The
        alternative is what happened on 2026-09-07: a run says "nothing would
        take a direction and a distance", the type library says nothing
        further, and another session on the CAD machine goes and asks. A CAD
        seat is the scarce thing in this project -- `docs/INVENTOR_SETUP.md`
        counts the round trips -- so the answer travels with the refusal.
        """
        try:
            names = sorted(n for n in dir(definition) if not n.startswith("_"))
        except Exception:  # pragma: no cover - hostile COM object
            names = []
        return ", ".join(names[:40]) or "nothing dir() could read"

    def _move_face_definition(self, features: Any, faces: Any) -> tuple[Any, str]:  # pragma: no cover
        """A `MoveFaceDefinition` for *faces*, and which call produced it.

        **Measured on 2027.1, 2026-09-08**: `MoveFaceFeatures` has exactly
        `Add(Definition)` and `CreateDefinition(1 argument)`, and that argument
        is a **`FaceCollection`** -- handed a generic `ObjectCollection` it
        answers "Type mismatch". `_new_collection` builds the right kind for a
        face selector, which is why this has always got a definition.

        `CreateMoveFaceDefinition` is still tried, and is measured absent. It
        stays only because it costs a `getattr` and Inventor does name some
        definition factories for their feature (`CreateShellDefinition`,
        `CreateFaceDraftDefinition`), so a release that renamed this one would
        keep working rather than refusing.
        """
        failures: list[str] = []
        for name in ("CreateDefinition",):
            factory = getattr(features, name, None)
            if factory is None:
                failures.append(f"{name}: MoveFaceFeatures has no such method")
                continue
            try:
                return factory(faces), name
            except Exception as exc:
                failures.append(f"{name}: {_com_message(exc)}")
        raise FeatureError(
            f"Could not create a move-face definition: {'; '.join(failures)}",
            hint="Read what this release really offers with `python "
            "scripts/com_signatures.py MoveFaceFeatures`.",
        )

    def thicken(self, doc_id: str, request: ThickenRequest) -> FeatureInfo:  # pragma: no cover
        """Add or remove a layer on faces, each along its own normal.

        **Measured against Inventor 2027.1 on 2026-09-07**, and the one of the
        four Phase 3 surfaces that came out of that run working. The signature
        was read first rather than guessed, off the type library and then
        confirmed the next day against Autodesk's published 2027 reference --
        three sources agreeing, which no other operation here has:

            ThickenFeatures.Add(Faces, Distance, ExtentDirection, Operation,
                                [AutomaticFaceChain], [CreateVerticalSurfaces],
                                [AutomaticBlending]) As ThickenFeature

        `Faces` is a `FaceCollection` or a `WorkSurface`; the three trailing
        Booleans default False; and there is no definition object for this
        feature at all, so the `CreateThickenDefinition` this used to try first
        could never have existed.

        The *side* was the other claim about Inventor, and the run settled it:
        `examples/calibration/thinned_wall.json` removed -0.2400 cm^3 against
        -0.2400 derived, so `THICKEN_SHARE` has it right and a `negative` layer
        does lie behind the face where the material is. The corners went the
        other way -- Inventor closes the notch where two grown walls meet, and
        `_thicken_corners` in the mock derives that term now. The prediction
        below is reported rather than enforced: the factor-of-four guard is gone
        with the guessing that needed it.
        """
        document = self._doc(doc_id)
        faces, matched = self._topology_selection(doc_id, request.faces)
        if int(faces.Count) == 0:
            raise FeatureError(
                "No faces matched, so there is nothing to thicken.",
                hint="Run `select_topology` with the same selector to see what it matches.",
            )
        share = THICKEN_SHARE[(request.direction, request.operation)]
        area = sum(info.area or 0.0 for info in matched)
        predicted = share * area * request.thickness.value
        before = _solid_volume(document)
        features = document.ComponentDefinition.Features.ThickenFeatures
        with self._batch(document), self._translate_errors("Thicken"):
            feature, made_by = self._add_thicken(features, faces, request)
            if request.name:
                feature.Name = request.name
            # No result guard. There was one -- a factor of four on the
            # area-times-thickness prediction, with the feature deleted -- and
            # it existed because a misordered variant-and-two-enums would have
            # built a part the size of a house rather than raising. Both halves
            # of that are settled: `_call_named` puts the argument names at the
            # call site, so a permutation is not possible, and both calibration
            # fixtures now agree with Inventor to four decimals. What is left is
            # the divergence check's job, where a disagreement is reported
            # rather than refused.
        return _feature_info(feature, "thicken", {
            "faces": int(faces.Count),
            "thickness": request.thickness.as_dict(),
            "direction": request.direction,
            "operation": request.operation,
            "area_cm2": round(area, 6),
            "predicted_cm3": round(predicted, 6),
            "built_by": made_by,
        })

    def _add_thicken(self, features: Any, faces: Any,
                     request: ThickenRequest) -> tuple[Any, str]:  # pragma: no cover
        """Build the thicken feature. One call, and its signature is measured.

        *Measured on Inventor 2027.1, 2026-09-07*, which turned a tower of
        attempts into a single call:

            ThickenFeatures.Add(Faces, Distance, ExtentDirection, Operation,
                                [AutomaticFaceChain], [CreateVerticalSurfaces],
                                [AutomaticBlending])

        Two things that were wrong before the read. **There is no
        `CreateThickenDefinition`** -- `ThickenFeatures` offers `Add` and
        nothing else -- so the definition route this tried first was reaching
        for something that has never existed. And **there is no `IsOffset`
        argument**: slot 4 is `AutomaticFaceChain`, and the `False` passed there
        for the offset mode's sake was right by accident. See the comment at the
        call.
        """
        return _call_named(features.Add, [
            ("Faces", faces),
            ("Distance", request.thickness.expression),
            ("ExtentDirection", self._k(EXTENT_DIRECTIONS[request.direction])),
            ("Operation", self._k(BOOLEAN_OPERATIONS[request.operation])),
            # False, and now for the right reason. This slot was passed `False`
            # believing it was an `IsOffset` flag -- the offset mode being the
            # thing this server cannot hold, since it produces a surface body.
            # The measured signature says there is no IsOffset argument at all
            # and slot 4 is `AutomaticFaceChain`. The call worked anyway, which
            # is the part worth recording: a value passed for a wrong reason
            # that happens to be right is not a measurement, and only reading
            # the signature told the two apart.
            #
            # False is still what a recipe wants. Chaining extends the selection
            # to tangent-connected faces, so a recipe naming four walls would
            # get however many the chain reaches -- and the selector said which
            # faces it meant.
            ("AutomaticFaceChain", False),
            # Inventor's own defaults for the last two. Nothing here has an
            # opinion about vertical surfaces or blending, and a guess would be
            # a guess whichever way it went.
            ("CreateVerticalSurfaces", DEFAULTED),
            ("AutomaticBlending", DEFAULTED),
        ]), "Add"

    # -- drawings ----------------------------------------------------------
    #: A view direction mapped onto Inventor's own orientation enum name. The
    #: *names* are documented; the values are never guessed -- `_k` reads them
    #: from the type library and raises a message naming the fix when it cannot,
    #: so nothing here can be off by a wrong number the way the extrude extents
    #: were before they were measured.
    #:
    #: `iso` is `kIsoTopLeftViewOrientation` rather than a bare "isometric":
    #: Inventor has four isometric corners and no default among them, so one has
    #: to be chosen. Top-left is the one Inventor's own base-view dialog offers
    #: first.
    _VIEW_ORIENTATIONS = {
        "front": "kFrontViewOrientation",
        "rear": "kBackViewOrientation",
        "top": "kTopViewOrientation",
        "bottom": "kBottomViewOrientation",
        "left": "kLeftViewOrientation",
        "right": "kRightViewOrientation",
        "iso": "kIsoTopLeftViewOrientation",
    }

    #: The same for the style. `hidden_line_removed` is the default a recipe
    #: gets, because it is what an engineering drawing is.
    _VIEW_STYLES = {
        "hidden_line": "kHiddenLineDrawingViewStyle",
        "hidden_line_removed": "kHiddenLineRemovedDrawingViewStyle",
        "shaded": "kShadedDrawingViewStyle",
    }

    def new_drawing(self, name: str, *, template: str | None = None,
                    sheet: str = "a3", units: str = "mm") -> DocInfo:  # pragma: no cover
        """A new drawing document, which is `new_part` with a different enum.

        **This was called the one call in the drawing surface that carried no
        risk, and it is the only one that has failed.** Twice, for two different
        reasons, and neither was the enum or the method:

        1. the recipe's `"ISO.idw"` was handed over verbatim and a bare filename
           is not a path -- `_drawing_template` resolves that now;
        2. with a real path to a real `ISO.idw`, `Documents.Add` still answered
           a bare *"Exception occurred"*, because **the template is from an
           older Inventor and wants migrating**. `_migrate_template` does what a
           person would: opens it and saves it.

        Both times the claim was about a *call* while the fault was in what the
        call was given. A template given here is the title block, and without
        one Inventor's default drawing template is used -- which has one, so a
        sheet is at least sendable.

        `sheet` is recorded and not applied. Inventor takes the sheet size from
        the template, and overriding it means finding the sheet and setting its
        size, which is one more unread call for a thing a template already
        decides. Recorded so the ledger and the sheet agree about what was
        asked for.
        """
        app = self._require_app()
        with self._translate_errors("Creating the drawing document", DocumentError):
            drawing_type = self._k("kDrawingDocumentObject")
            path, found = self._drawing_template(app, drawing_type, template)
            document, migrated = self._drawing_from(app, drawing_type, path)
            try:
                document.DisplayName = name
            except Exception:
                pass
        info = self._register(document, units, "deg")
        info.detail = {"sheet_asked_for": sheet, "template": path,
                       "template_from": found, "template_migrated": migrated}
        return info

    def _drawing_from(self, app: Any, drawing_type: int,
                      path: str) -> tuple[Any, bool]:  # pragma: no cover
        """A drawing made from *path*, migrating the template if that is why not.

        **Measured 2026-09-08.** With a resolved path to a real `ISO.idw`,
        `Documents.Add` answered *"Exception occurred"* and nothing else --
        Inventor's least helpful failure, and the one this project has spent the
        most effort learning not to pass on. The template dates from an older
        release and wants **migrating**, which interactively is a dialog and
        through the API is silence.

        Migrating a file is opening it and saving it, so that is what happens on
        a failure: `Documents.Open`, save if Inventor marks it dirty, close. Then
        the `Add` is tried once more.

        Three things about the shape of this.

        **It happens on failure, not on the way past.** The template is a file
        somebody else owns -- here a company one on a shared drive -- and
        rewriting it is not a side effect to have while creating a drawing.
        A template that opens without being dirtied is left exactly as it was,
        and `template_migrated` in the feature detail says which happened, so a
        run that modified a shared file says so rather than being silently
        helpful.

        **The retry is once.** If a migrated template still will not make a
        drawing, the reason is not migration, and a loop would turn one bare
        "Exception occurred" into several.

        **And the failure names both attempts**, because "Exception occurred"
        twice over with no path in it is what cost the two runs before this one.
        """
        try:
            return _specialise(app.Documents.Add(drawing_type, path, True)), False
        except Exception as first:
            try:
                migrated = self._migrate_template(app, path)
            except Exception as exc:
                raise DocumentError(
                    f"Inventor would not make a drawing from {path!r}, and "
                    f"would not migrate it either: {_com_message(exc)}",
                    hint="The first failure was "
                    f"{_com_message(first)!r}. A template from an older "
                    "release wants migrating, which is an explicit open and "
                    "save -- if it cannot be opened, open it in Inventor by "
                    "hand once and save it, or point `template` at one that is "
                    "already current.",
                ) from first
            try:
                return _specialise(app.Documents.Add(drawing_type, path, True)), migrated
            except Exception as exc:
                raise DocumentError(
                    f"Inventor would not make a drawing from {path!r}: "
                    f"{_com_message(exc)}",
                    hint="Tried twice, migrating the template in between "
                    f"({'it was saved' if migrated else 'it was already current'}"
                    "), so migration is not the reason. Open that template in "
                    "Inventor by hand to see what it says about itself.",
                ) from exc

    def _migrate_template(self, app: Any, path: str) -> bool:  # pragma: no cover
        """Open the template and save it, and say whether saving was needed.

        Which is all migrating a file is. The document is opened *invisibly* --
        this is maintenance on a file, not something to show somebody -- and
        saved only if Inventor marks it dirty, because a save it did not ask for
        rewrites a file that was already fine.

        `Dirty` unreadable is treated as dirty: the only reason to be here is
        that `Documents.Add` refused, so the file is a suspect already, and
        saving a current template costs a modification date where not saving an
        old one costs the run.
        """
        document = app.Documents.Open(path, False)
        try:
            try:
                dirty = bool(document.Dirty)
            except Exception:  # pragma: no cover - version-specific
                dirty = True
            if dirty:
                document.Save()
            return dirty
        finally:
            # True is "skip saving": whatever needed saving has been saved, and
            # a close that saves again on the way out would hide a failure.
            document.Close(True)

    def _drawing_template(self, app: Any, drawing_type: int,
                          template: str | None) -> tuple[str, str]:  # pragma: no cover
        r"""The template file to make the drawing from, and where it was found.

        **Measured on 2026-09-07: this is what the first live run failed on.**
        `Documents.Add` was handed the recipe's `template` verbatim, the shipped
        drawing said `"ISO.idw"`, and a bare filename is not a path -- so
        Inventor answered "Exception occurred" and named nothing, which is the
        error this project has spent the most time learning to avoid producing.

        A bare name is what somebody means, though, so it is resolved against
        the folders Inventor itself uses before being given up on -- and against
        their immediate subfolders too, because that is where the
        standards-specific templates live. One level, not a walk: a template
        found four folders deep is as likely to be somebody's saved copy as the
        one they meant.

        **Where those folders come from was itself wrong, and 2026-09-08
        measured it.** The first fix asked `FileManager.TemplatesPath`, on the
        reasonable-sounding basis that a file manager knows where files are. It
        does not have that property: the probe got
        `AttributeError: <unknown>.TemplatesPath` from the same object that
        answered `GetTemplateFile` in the line above -- so a real absence rather
        than the apartment-threading artefact that looks identical. Inventor
        keeps those paths on the *project*, not the file manager.

        So `_template_folders` asks three things in order of how well each is
        established, and the first is the strongest: **the folder Inventor's own
        default template is in**, which is `GetTemplateFile` measured working on
        2027.1 and needs no property nobody has read. On the machine this serves
        that is a Shared-drive *project* folder rather than the Inventor
        install, and its default is a `.dwg` rather than an `.idw` -- which is
        the other reason not to guess a path: a project can put its templates
        anywhere, and does.

        **Measured 2026-09-08, and this is what settles the shipped recipe.**
        The active project's `TemplatesPath` holds `Standard.idw`,
        `Standard.dwg` and a house `OCB_Standard.idw`, and one level down under
        `Metric\` are `ISO.idw`, `DIN.idw`, `BSI.idw`, `JIS.idw` and the rest --
        so `"ISO.idw"` does resolve here, from the subfolder search rather than
        the folder itself. Two folders deep would have been needed if this had
        walked only the top level, which is why it does not.

        The failure names every place it looked rather than just the last one.
        """
        if not template:
            return (str(app.FileManager.GetTemplateFile(drawing_type)),
                    "Inventor's default drawing template")
        if os.path.isfile(template):
            return (os.path.abspath(template), "the path given")
        tried = [os.path.abspath(template)]
        for folder, source in self._template_folders(app, drawing_type):
            candidate = os.path.join(folder, template)
            tried.append(candidate)
            if os.path.isfile(candidate):
                return (candidate, f"{source} ({folder})")
            try:
                inner = sorted(entry.path for entry in os.scandir(folder)
                               if entry.is_dir())
            except OSError:  # pragma: no cover - unreadable templates folder
                inner = []
            for sub in inner:
                candidate = os.path.join(sub, template)
                tried.append(candidate)
                if os.path.isfile(candidate):
                    return (candidate, f"a subfolder of {source} ({sub})")
        raise DocumentError(
            f"No such drawing template: {template!r}.",
            hint="Give a full path to a .idw or .dwg, or leave `template` out to "
            "use Inventor's default -- which has a title block, so a sheet made "
            "without one is still sendable. Looked in: " + ", ".join(tried),
        )

    def _template_folders(self, app: Any, drawing_type: int
                          ) -> list[tuple[str, str]]:  # pragma: no cover
        """Every folder a named template could reasonably be in, best first.

        Each is wrapped because each is a different kind of uncertain, and a
        property that is absent on this release must not stop the one that is
        not. In order:

        1. **The folder Inventor's own default drawing template is in.**
           `FileManager.GetTemplateFile` is measured working on 2027.1, so this
           needs nothing unread -- and it follows the active project, which is
           where the answer really lives.
        2. **The active project's `TemplatesPath`**, which is where Inventor
           documents these paths as living. Unread here, hence second.
        3. **`FileManager.TemplatesPath`**, which 2027.1 does not have. Kept
           only because a release that grows it would be free to use it, and
           dropping it would leave nothing recording that it was tried.
        """
        folders: list[tuple[str, str]] = []
        try:
            default = str(app.FileManager.GetTemplateFile(drawing_type))
        except Exception:  # pragma: no cover - version-specific
            default = ""
        if default:
            folders.append((os.path.dirname(default),
                            "the folder Inventor's own default template is in"))
        try:
            folders.append((str(app.DesignProjectManager.ActiveDesignProject
                                .TemplatesPath), "the active project's templates folder"))
        except Exception:  # pragma: no cover - version-specific
            pass
        try:
            folders.append((str(app.FileManager.TemplatesPath),
                            "FileManager's templates folder"))
        except Exception:  # pragma: no cover - absent on 2027.1, measured
            pass
        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for folder, source in folders:
            key = os.path.normcase(os.path.abspath(folder)) if folder else ""
            if not key or key in seen or not os.path.isdir(folder):
                continue
            seen.add(key)
            unique.append((folder, source))
        return unique

    def place_view(self, doc_id: str, request: ViewRequest) -> ViewInfo:  # pragma: no cover
        """A base view of a part, on this drawing's active sheet.

        **Measured on Inventor 2027.1, 2026-09-09**, on the second attempt: the
        first placed nothing at all, because a drawing view is a *reference to
        a model file* and the part had only ever existed in memory. That is
        checked before the call now and refused by name, since Inventor's own
        answer is "Exception occurred." and nothing else.

        `AddBaseView`'s arguments are passed by name through `_call_named` so
        the positions are readable at the call, and the two enums come from
        `_k`, so a wrong *name* raises and a wrong *number* is not possible.

        **What a direction's name means was the other measurement.** A view's
        extent, read back off the sheet, says which plane it shows: `front`
        spans XY -- Inventor's view names are Y-up -- so a plate modelled Z-up
        has its plan as its front view. A recipe's `direction` follows
        Inventor's naming by decision (`docs/DECISIONS.md`), which is why the
        extent is reported here and asserted in the acceptance check. What an
        extent cannot say is which way is *up* inside that plane, so the
        view's camera goes into the result detail beside it.

        **A projected view is a different call and does not name a direction at
        all.** `AddProjectedView` is told a position and infers which way the
        view faces from where it sits relative to its parent, which is the
        reverse of a base view -- so the projection angle has to be applied
        before the call, and `drafting.projected_position` is where that
        happens. It also means the direction check above is sharper for a
        projected view than a base one: nothing was asserted about the
        direction, so what the sheet reports is Inventor's own answer.
        """
        document = self._doc(doc_id)
        model = self._doc(request.part_doc_id)
        app = self._require_app()
        sheet = document.ActiveSheet
        if request.parent is None:
            # Asked before the call, because the call's own answer is "Exception
            # occurred." and nothing else -- measured 2026-09-08, three views
            # refused in a row with no reason given.
            #
            # A drawing view is a *reference* to a model file: the sheet stores
            # which document it draws and re-reads it on every open, which a
            # document that exists only in memory has no way to be. So a part
            # built in this session has to be saved before it can be drawn, and
            # saying that is the whole of this refusal.
            where = _document_path(model)
            if not where:
                raise FeatureError(
                    "The part has not been saved, so there is no file for a "
                    "drawing view to reference.",
                    hint="Save it first -- `save_part` -- and then build the "
                    "drawing. Inventor refuses AddBaseView on an unsaved "
                    "document with a bare \"Exception occurred.\", which is why "
                    "this is checked here rather than reported from there.",
                )
        with self._translate_errors("Placing the view"):
            position = app.TransientGeometry.CreatePoint2d(*request.at)
            if request.parent is None:
                try:
                    view = _call_named(sheet.DrawingViews.AddBaseView, [
                        ("Model", model),
                        ("Position", position),
                        ("Scale", float(request.scale)),
                        ("ViewOrientation",
                         self._k(self._VIEW_ORIENTATIONS[request.direction])),
                        ("ViewStyle", self._k(self._VIEW_STYLES[request.style])),
                    ])
                except Exception as exc:
                    # Everything the call was given, because Inventor's own
                    # answer here is "Exception occurred." and a refusal that
                    # names nothing is what costs the next run.
                    raise FeatureError(
                        f"Placing the base view failed: {self._explain(exc)}",
                        hint=self._view_call_detail(sheet, model, request),
                    ) from exc
            else:
                # A projected view takes no orientation and no scale: which way
                # it faces is decided by where it sits relative to its parent
                # and by the sheet's projection angle, and its scale is its
                # parent's. That is why `drafting.projected_position` works out
                # the position from the angle -- Inventor is told a place and
                # infers the direction, which is the reverse of a base view and
                # the reason the two are separate calls here.
                view = _call_named(sheet.DrawingViews.AddProjectedView, [
                    ("ParentView", self._drawing_view(document, request.parent)),
                    ("Position", position),
                    ("ViewStyle", self._k(self._VIEW_STYLES[request.style])),
                ])
            try:
                view.Name = request.name
            except Exception:  # pragma: no cover - version-specific
                logger.info("Could not name the drawing view %r.", request.name)
        # `direction` stays the *request*, because the caller lays out the
        # sheet by it and a projected view's position was worked out from it.
        # What Inventor says about the view it made goes in the detail beside
        # it: the orientation it reports, and the camera, which is the only
        # reading that settles which way is up inside the plane an extent
        # measures. See defect 16.
        camera = _view_camera(view)
        detail: dict[str, Any] = {
            "orientation_reported": _view_orientation_name(
                view, self._VIEW_ORIENTATIONS, self._k),
            "direction_measured": _direction_from_camera(camera),
        }
        if camera:
            detail["camera"] = camera
        return ViewInfo(
            id=request.name,
            name=str(getattr(view, "Name", request.name)),
            direction=request.direction,
            at=tuple(request.at),
            scale=float(getattr(view, "Scale", request.scale)),
            style=request.style,
            extent=_view_extent(view),
            detail=detail,
        )

    def _view_call_detail(self, sheet: Any, model: Any,
                          request: ViewRequest) -> str:  # pragma: no cover - Windows only
        """Everything `AddBaseView` was handed, for a refusal's hint.

        Each of these has been a candidate cause at some point and none of them
        is visible in "Exception occurred": whether the model has a file at
        all, whether the position is on the sheet (positions reach here in
        centimetres and a recipe writes millimetres, so a factor of ten puts a
        view a long way off an A3), and which orientation and style names were
        resolved.
        """
        parts = [f"model {_document_path(model) or 'UNSAVED'}"]
        parts.append(f"position {request.at[0]:.3f}, {request.at[1]:.3f} cm")
        try:
            parts.append(f"sheet {float(sheet.Width):.3f} x "
                         f"{float(sheet.Height):.3f} cm")
        except Exception:
            parts.append("sheet size unreadable")
        parts.append(f"scale {float(request.scale):g}")
        parts.append(self._VIEW_ORIENTATIONS[request.direction])
        parts.append(self._VIEW_STYLES[request.style])
        return ("Called with " + ", ".join(parts)
                + ". A position off the sheet, a model with no file, and an "
                "orientation this release spells differently all arrive as the "
                "same bare message, so check them against this list.")

    def retrieve_dimensions(self, doc_id: str,
                            request: RetrieveRequest) -> list[DimensionInfo]:  # pragma: no cover
        """Bring the asked-for model dimensions onto a view, and only those.

        **Retrieval rather than placement, and that is the design.** Every sketch
        dimension this server creates carries a parameter's expression and every
        driven feature value is a named parameter, so Inventor's own "retrieve
        model dimensions" produces dimensions that *are* the parameters. Placing
        a dimension by geometry would mean working out which two drawing curves
        a parameter drives, which is the guessing a recipe exists to avoid.

        **The filter runs before the retrieval, on the model side.** The first
        version of this retrieved every model dimension onto the view and then
        asked each *drawing* dimension which parameter it came from -- a
        question nothing documents an answer to, and the one fact the whole
        approach was said to rest on. The published 2027 reference (read
        2026-09-08) offers a better route, new in 2026.1:

            Sheet.GetRetrievableAnnotations2(View, [SketchAndFeatureDimensions],
                                             [ModelObject], [DesignView])
                As ObjectCollection
            Sheet.RetrieveAnnotations2(ViewOrSketch, [AnnotationsToRetrieve])
                As ObjectsEnumerator

        The first returns the *model's* `DimensionConstraint` and
        `FeatureDimension` objects (or their proxies) that could be retrieved
        into the view -- and a `DimensionConstraint.Parameter` is documented.
        So the asked-for ones are chosen there, by parameter name, and only
        those are handed to the second call. Nothing is placed and deleted
        again, and no drawing dimension is asked anything. Each is retrieved
        on its own so the dimension that comes back is known by the parameter
        that went in.

        A release without the pair falls back to the old retrieve-then-filter
        route, which keeps its original failure: if no retrieved dimension can
        name its parameter, everything is removed again and the error says so.
        """
        document = self._doc(doc_id)
        view = self._drawing_view(document, request.view)
        wanted = {name: False for name in request.parameters}
        wanted.update({name: True for name in request.reference})
        sheet = document.ActiveSheet
        with self._translate_errors("Retrieving dimensions"):
            offered = self._retrievable_annotations(sheet, view)
            if offered is not None:
                return self._retrieve_by_parameter(doc_id, sheet, view, offered, wanted,
                                                   request.view)
            retrieved = self._retrieve_onto(document, view)
            if not retrieved:
                # The one path that used to return an empty list and no reason:
                # a legacy route that ran, raised nothing, and put no dimension
                # on the sheet. "0 of 0" is what the caller then reports, which
                # names neither the route nor the cause.
                raise FeatureError(
                    f"The legacy retrieval route put no dimension at all onto "
                    f"view {request.view!r}, so none of {sorted(wanted)} could "
                    "be kept.",
                    hint="This release has no Sheet.GetRetrievableAnnotations2 "
                    "(2026.1 and later), so the older DrawingDimensions route "
                    "ran and found nothing to retrieve. Either the model holds "
                    "no dimension the view can show -- a parameter that drives "
                    "nothing has none, which the rehearsal warns about -- or "
                    "this release wants the call made differently. `python "
                    "scripts/com_signatures.py Sheet DrawingDimensions` says "
                    "what it offers.",
                )
            named = [(entry, self._dimension_parameter(entry)) for entry in retrieved]
            if retrieved and not any(name for _, name in named):
                for entry, _ in named:
                    _delete_quietly(entry)
                raise FeatureError(
                    f"{len(retrieved)} dimension(s) were retrieved onto view "
                    f"{request.view!r} and none of them could say which model "
                    "parameter it came from, so there is no way to keep the ones "
                    "this drawing asked for.",
                    hint="This is the fact the whole retrieve-and-filter approach "
                    "rests on -- see the drawing section of docs/INVENTOR_SETUP.md. "
                    "Read what a DrawingDimension really offers with `python "
                    "scripts/com_signatures.py GeneralDimension`. The retrieved "
                    "dimensions have been removed again rather than left on a "
                    "sheet nobody dimensioned.",
                )
            kept: list[DimensionInfo] = []
            for entry, name in named:
                if name is None or name not in wanted:
                    _delete_quietly(entry)
                    continue
                kept.append(self._dimension_info(entry, request.view, name, wanted[name]))
        return kept

    def _retrievable_annotations(self, sheet: Any, view: Any) -> list[Any] | None:  # pragma: no cover
        """What Inventor offers to retrieve into *view*, or None on a release without the call.

        `SketchAndFeatureDimensions` is left at its documented default of True:
        that is the mode that returns dimension constraints and feature
        dimensions, and False would return 3D annotations instead.
        """
        routine = getattr(sheet, "GetRetrievableAnnotations2", None)
        if routine is None:
            return None
        return _as_list(routine(view))

    def _retrieve_by_parameter(self, doc_id: str, sheet: Any, view: Any,
                               offered: Sequence[Any], wanted: dict[str, bool],
                               view_name: str) -> list[DimensionInfo]:  # pragma: no cover
        """Retrieve the offered annotations whose parameter the recipe asked for.

        One `RetrieveAnnotations2` call per annotation rather than one for the
        lot, so that each drawing dimension that comes back is known by the
        parameter that went in. That costs a COM round trip per dimension and
        buys the thing the old design could not have: no property of a
        `DrawingDimension` is relied on at all.

        What came back is remembered against the document, so that
        `read_drawing` can name a dimension by the parameter this session
        retrieved it for rather than by asking the sheet -- which is the only
        route left that rests on an undocumented property, and is now the
        fallback for a dimension nobody here placed.
        """
        read = [_model_parameter(item) for item in offered]
        named = [(item, name) for item, (name, _) in zip(offered, read)]
        chosen = _annotations_wanted(named, wanted, [held for _, held in read])
        if not chosen:
            on_offer = sorted(f"{name} = {held}" if held else str(name)
                              for name, held in read if name)
            raise FeatureError(
                f"Inventor offers {len(offered)} retrievable annotation(s) on view "
                f"{view_name!r} and none of them is driven by a parameter this "
                f"drawing asked for ({sorted(wanted)}).",
                hint=(f"What Inventor can retrieve here, as model parameter = "
                      f"expression: {on_offer}. A model dimension states an "
                      "asked-for parameter only when its expression IS that "
                      "parameter: `plate_w` states 120 and "
                      "`plate_w - 2 * edge_margin` states 96, which is neither "
                      "of the names in it. "
                      if on_offer else
                      "None of the offered annotations names a parameter at all, "
                      "which means the model-side `Parameter` property did not "
                      "answer -- `python scripts/com_signatures.py "
                      "DimensionConstraint FeatureDimension` says what it is "
                      "called on this release. ")
                + "A parameter that drives nothing has no model dimension to "
                "retrieve; the rehearsal warns about those before a sheet is made.",
            )
        app = self._require_app()
        kept: list[DimensionInfo] = []
        remembered = self._retrieved.setdefault(doc_id, [])
        for item, name in chosen:
            collection = app.TransientObjects.CreateObjectCollection()
            collection.Add(item)
            for entry in _as_list(sheet.RetrieveAnnotations2(view, collection)):
                remembered.append((entry, name))
                kept.append(self._dimension_info(entry, view_name, name, wanted[name]))
        return kept

    def _remembered_parameter(self, doc_id: str, entry: Any) -> str | None:  # pragma: no cover
        """The parameter this session retrieved *entry* for, if it did."""
        for placed, name in self._retrieved.get(doc_id, ()):
            if _same_com_object(placed, entry):
                return name
        return None

    def read_drawing(self, doc_id: str) -> DrawingContents:  # pragma: no cover
        """The sheet as Inventor now has it, which is what closes the round trip.

        Read off the sheet rather than reported from the request, and that is the
        entire point: a dimension the retrieval could not find is absent here,
        and a view whose direction did not mean what its name said has an extent
        that says so.
        """
        document = self._doc(doc_id)
        sheet = document.ActiveSheet
        views: list[ViewInfo] = []
        for index in range(1, int(sheet.DrawingViews.Count) + 1):
            view = sheet.DrawingViews.Item(index)
            camera = _view_camera(view)
            from_camera = _direction_from_camera(camera)
            labelled = _view_orientation_name(
                view, self._VIEW_ORIENTATIONS, self._k)
            direction = from_camera or labelled
            detail: dict[str, Any] = {
                "direction_from": "camera" if from_camera else "ViewOrientationType",
                "orientation_reported": labelled,
            }
            if camera:
                detail["camera"] = camera
            views.append(ViewInfo(
                id=str(getattr(view, "Name", index)),
                name=str(getattr(view, "Name", f"view{index}")),
                # Asked of the view rather than remembered from the request: a
                # sheet read back has to be able to disagree with what was asked
                # for, or reading it back proves nothing.
                #
                # **From the camera, because the orientation enum is not
                # readable on 2027.1** -- measured 2026-09-09 on all seven
                # directions, base and projected alike, where
                # `ViewOrientationType` answered nothing at all. The camera
                # does, and it says more: where the view looks from and which
                # way is up. The enum stays behind it for a release that has
                # it, and the detail says which answered, because a direction
                # derived from a camera and one Inventor labelled are different
                # kinds of evidence.
                direction=direction,
                at=_view_position(view),
                scale=float(getattr(view, "Scale", 1.0)),
                extent=_view_extent(view),
                detail=detail,
            ))
        dimensions: list[DimensionInfo] = []
        for index in range(1, int(sheet.DrawingDimensions.Count) + 1):
            entry = sheet.DrawingDimensions.Item(index)
            # Known by what went in, where this session retrieved it; asked of
            # the sheet only for a dimension nobody here placed.
            name = self._remembered_parameter(doc_id, entry) or self._dimension_parameter(entry)
            dimensions.append(self._dimension_info(
                entry, _dimension_view_name(entry), name, _is_reference(entry)))
        return DrawingContents(
            views=views, dimensions=dimensions,
            sheet=str(getattr(sheet, "Name", "")) or "a3",
            detail={"read_from": "the sheet"},
        )

    #: How a retrieved dimension might name the model parameter it came from,
    #: as attribute paths tried in order. Nothing in this repository has ever
    #: held a `DrawingDimension`, so these are documented property paths and a
    #: proposal -- and unlike an argument order, a wrong guess here cannot build
    #: anything wrongly: it either names a parameter or it does not.
    _DIMENSION_PARAMETER_PATHS = (
        ("ModelDimension", "Parameter", "Name"),
        ("ModelDimension", "Name"),
        ("Parameter", "Name"),
        ("ModelValue", "Parameter", "Name"),
    )

    def _dimension_parameter(self, entry: Any) -> str | None:  # pragma: no cover
        """Which model parameter this dimension came from, or None if it will not say."""
        for path in self._DIMENSION_PARAMETER_PATHS:
            current: Any = entry
            for step in path:
                current = getattr(current, step, None)
                if current is None:
                    break
            if isinstance(current, str) and current:
                return current
        return None

    def _dimension_info(self, entry: Any, view: str | None, parameter: str | None,
                        reference: bool) -> DimensionInfo:  # pragma: no cover
        return DimensionInfo(
            id=str(getattr(entry, "Name", "") or id(entry)),
            value=float(getattr(entry, "ModelValue", 0.0) or 0.0),
            kind=_dimension_kind(entry),
            view=view,
            parameter=parameter,
            expression=_dimension_expression(entry),
            reference=reference,
        )

    #: How a release *before 2026.1* might offer "retrieve the model's dimensions
    #: onto this view", tried in order. The published 2027 reference has neither
    #: name -- `Sheet.GetRetrievableAnnotations2` / `RetrieveAnnotations2` is the
    #: documented pair and is tried first -- so this is the fallback for an older
    #: Inventor, kept because every candidate takes the view and nothing else
    #: that could be misread: a wrong name raises and a wrong object is a type
    #: mismatch.
    _RETRIEVAL_ROUTES = ("RetrieveDimensions", "AddRetrievedDimensions")

    def _retrieve_onto(self, document: Any, view: Any) -> list[Any]:  # pragma: no cover
        """Every dimension retrieval put on the sheet, by whichever legacy route works.

        The returned collection is turned into a plain list immediately: the
        filter that follows deletes some of them, and deleting out of a live COM
        collection while iterating it is how a loop silently skips half its
        members.
        """
        dimensions = document.ActiveSheet.DrawingDimensions
        failures: list[str] = []
        for name in self._RETRIEVAL_ROUTES:
            route = getattr(dimensions, name, None)
            if route is None:
                failures.append(f"{name}: DrawingDimensions has no such method")
                continue
            try:
                result = route(view)
            except Exception as exc:
                failures.append(f"{name}: {_com_message(exc)}")
                continue
            return _as_list(result)
        raise FeatureError(
            "No route to retrieving this part's model dimensions onto the view: "
            "this release has no Sheet.GetRetrievableAnnotations2 (2026.1 and "
            "later), and the older names failed too: " + "; ".join(failures),
            hint="Read what this release offers with `python "
            "scripts/com_signatures.py Sheet DrawingDimensions`. Retrieval is how "
            "this server dimensions a drawing at all -- see the drawing section "
            "of docs/INVENTOR_SETUP.md for why, and what to do if the answer is "
            "that no such method exists.",
        )

    def _drawing_view(self, document: Any, name: str) -> Any:  # pragma: no cover
        sheet = document.ActiveSheet
        found = []
        for index in range(1, int(sheet.DrawingViews.Count) + 1):
            view = sheet.DrawingViews.Item(index)
            if str(getattr(view, "Name", "")) == name:
                return view
            found.append(str(getattr(view, "Name", index)))
        raise DocumentError(f"This sheet has no view named {name!r}.",
                            hint=f"Views on it: {', '.join(found) or '(none)'}.")

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
        tool = self._resolve_plane(doc_id, document, request.tool, None)
        features = component.Features.SplitFeatures
        with self._batch(document), self._translate_errors("Split"):
            try:
                if request.style == "trim":
                    # Inverted, and measured rather than reasoned. Inventor's
                    # second argument says which side to KEEP, where this read it
                    # as which side to remove, so every trim threw away the half
                    # the caller meant to keep -- and reported a volume that was
                    # correct for the half it kept, so nothing raised.
                    #
                    # Established on 2026-09-03 by cutting one part three ways:
                    # `remove_positive` true and false gave exactly complementary
                    # results, so the flag does reach Inventor and does choose the
                    # side; and the same cut made by the XY origin plane, whose
                    # normal is +Z by definition and so cannot have been built
                    # backwards, still kept the wrong half. That last one is what
                    # rules out the alternative -- an offset work plane pointing
                    # the other way -- and puts the fault here.
                    # See defect 5 in docs/FEATURE_COVERAGE.md.
                    feature = features.SplitPart(tool, not request.remove_positive)
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

    def sketch_driven_pattern(self, doc_id: str,
                              request: SketchDrivenPatternRequest
                              ) -> FeatureInfo:  # pragma: no cover
        """Copy features to a sketch's points.

        **Run against Inventor 2027.1 on 2026-09-07, where it failed**, and the
        failure was a shape rather than a detail: this went through `_patterned`
        with three named arguments and Inventor's wrapper answered *"Add() takes
        from 1 to 2 positional arguments but 5 were given"*. The measured
        signature is **`SketchDrivenPatternFeatures.Add(Definition)`** -- one
        object, like `move_face` -- so the three-argument call could never have
        worked on this release, and `_patterned` cannot be what makes it.

        The type library would not say where the definition comes from --
        `--search SketchDrivenPattern` publishes `Add` and nothing else, no
        factory and no definition class -- and the live object's own `ITypeInfo`
        did, the next day: `CreateDefinition` with 4 arguments, 2 of them
        optional, and `CreateDefinition(parents, sketch, point)` confirmed to
        produce one. `_sketch_driven_definition` has the detail. **So the whole
        call is measured now**, and what is still unmeasured is only what the
        part comes out as.

        Two things carried over from before the run, because they are still
        true. **A wrong argument order cannot pass silently**: the three are a
        feature collection, a sketch and a sketch point, which are three
        different COM types, so a misorder is a type mismatch rather than a part
        built wrongly -- that is why trying a factory's arguments was safe when
        guessing `thicken`'s were not. And **the compute type still matters**:
        measured on 2027.1, patterning a hole fails outright until the compute
        type is `kAdjustToModelCompute`, and there is no reason a sketch-driven
        pattern of a hole differs. The definition has a settable `ComputeType`
        -- also measured -- so that is where it goes.

        The question a *successful* run still has to settle is unchanged and is
        not about the signature: **whether Inventor puts an occurrence on the
        reference point as well**, which is an off-by-one occurrence in the
        volume and a duplicate feature sitting exactly on the seed.
        `docs/INVENTOR_SETUP.md` has the three readings that tell them apart.
        """
        document = self._doc(doc_id)
        parents = self._feature_collection(doc_id, request.features)
        sketch = self._sketch(doc_id, request.sketch)
        # Inventor's own dialog offers the seed's centroid or a point you pick,
        # and the recipe always names a point: a centroid is not something the
        # simulator has, so a default that used one could not be rehearsed.
        # See `_NO_CENTROID` in the mock.
        reference = self._sketch_point(sketch, request.reference_index)
        features = document.ComponentDefinition.Features.SketchDrivenPatternFeatures
        with self._batch(document), self._translate_errors("Sketch driven pattern"):
            definition, made_by = self._sketch_driven_definition(
                features, parents, sketch, reference)
            compute = self._pattern_compute(definition)
            feature = features.Add(definition)
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "sketch_driven_pattern", {
            "features": list(request.features),
            "sketch": request.sketch,
            "points": len(request.point_indices) or None,
            "reference_index": request.reference_index,
            "definition_from": made_by,
            "compute": compute,
        })

    def _sketch_driven_definition(self, features: Any, parents: Any, sketch: Any,
                                  reference: Any) -> tuple[Any, str]:  # pragma: no cover
        """A definition for the pattern, and which call produced it.

        **Measured on Inventor 2027.1, 2026-09-08**, by asking the live object's
        own `ITypeInfo` -- the only thing that would say, since the type library
        publishes `SketchDrivenPatternFeatures` with `Add` and nothing else:

            CreateDefinition(ParentFeatures, Sketch, BasePoint, ReferenceFaces)
            Add(Definition)

        with the last two optional. `BasePoint` is supplied and
        `ReferenceFaces` is not: Inventor's own dialog offers the seed's
        centroid *or* a point you pick, the recipe always names a point, and a
        centroid is not something the simulator has -- so a default that used
        one could not be rehearsed (see `_NO_CENTROID` in the mock). Reference
        faces are the other way round: nothing in a recipe says them, so
        Inventor's own default is the honest value.

        Named through `_call_named` because the names are measured now, from
        the same `GetNames` call that gives the arity -- an earlier version was
        positional on the belief that type information gives arity alone, which
        was a fact about how much of the answer had been read rather than about
        the API.

        Nothing can be silently misordered here in any case: a feature
        collection, a sketch and a sketch point are three different COM types,
        so a wrong order is a type mismatch rather than a part built wrongly.

        `CreateSketchDrivenPatternDefinition` was also tried, before this was
        read, on the grounds that Inventor names some definition factories for
        their feature. It is measured absent, so it is gone rather than kept as
        a fallback: an attempt list for a call whose signature is known is
        noise, and it hides which spelling is the real one.
        """
        factory = getattr(features, "CreateDefinition", None)
        if factory is None:
            try:
                offered = ", ".join(sorted(name for name in dir(features)
                                           if not name.startswith("_"))[:40])
            except Exception:  # pragma: no cover - hostile COM object
                offered = "nothing dir() could read"
            raise FeatureError(
                "This release's SketchDrivenPatternFeatures has no "
                "CreateDefinition, and `Add` takes only a definition.",
                hint="Measured on 2027.1: CreateDefinition(ParentFeatures, "
                "Sketch, BasePoint, ReferenceFaces), the last two optional. Ask "
                "this release with `python scripts/probe_definitions.py`. The "
                f"collection offers {offered}.",
            )
        return _call_named(factory, [
            ("ParentFeatures", parents),
            ("Sketch", sketch),
            ("BasePoint", reference),
            # Inventor's own default. A recipe never says which faces a pattern
            # is measured against, and a guess would be a guess either way.
            ("ReferenceFaces", DEFAULTED),
        ]), "CreateDefinition"

    def _pattern_compute(self, definition: Any) -> str:  # pragma: no cover
        """Ask for recomputed occurrences, and report whether it took.

        Same measurement as `_patterned`'s, applied to a definition instead of
        a call: on 2027.1 patterning a hole fails outright until the compute
        type is `kAdjustToModelCompute`, because identical compute copies faces
        and a blind hole's second occurrence has nothing to remove until the
        boss beneath it exists. Recompute is therefore what to ask for.

        2027.1's definition has a settable `ComputeType` -- measured
        2026-09-08, along with a default of 47361 for it -- so this is expected
        to take. A release without the property is still not an error: it gets
        its own default, and the detail says so rather than the code pretending
        it was set.
        """
        try:
            definition.ComputeType = self._k("kAdjustToModelCompute")
        except Exception:  # pragma: no cover - version-specific
            return "the definition's own default"
        return "adjust to model"

    def _sketch_point(self, sketch: Any, index: int) -> Any:  # pragma: no cover
        """The *index*-th hole-centre point of a sketch, counted as the plan counts.

        `SketchPoints` holds every point in creation order, hole centre or not,
        and the recipe's indices are into the hole centres alone -- the same
        indices `hole` uses, so the two operations agree about which point a
        caller meant. `HoleCenter` is the property that separates them, and a
        release that does not offer it falls back to every point rather than
        refusing: the two lists are the same whenever the sketch was built by
        this server, which puts nothing but hole centres in a positions sketch.
        """
        points = sketch.SketchPoints
        centres = []
        for position in range(1, int(points.Count) + 1):
            point = points.Item(position)
            try:
                if not bool(point.HoleCenter):
                    continue
            except Exception:  # pragma: no cover - version-specific
                pass
            centres.append(point)
        if not centres:
            centres = [points.Item(position)
                       for position in range(1, int(points.Count) + 1)]
        if not 0 <= index < len(centres):
            raise FeatureError(
                f"Sketch {sketch.Name!r} has {len(centres)} point(s) to pattern to; "
                f"there is no point {index} for the seed to sit on.",
                hint="Add `point`, `point_grid` or `bolt_circle` entities to the sketch.",
            )
        return centres[index]

    def mirror(self, doc_id: str, request: MirrorRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        parents = self._feature_collection(doc_id, request.features)
        plane = self._resolve_plane(doc_id, document, request.plane, None)
        features = document.ComponentDefinition.Features.MirrorFeatures
        with self._batch(document), self._translate_errors("Mirror"):
            feature = features.Add(
                parents, plane, False, self._k("kAdjustToModelCompute")
            )
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "mirror", {"plane": request.plane})

    #: The work-plane kinds this backend can build -- all four of them since
    #: 2026-09-09. `angle` was refused until that day and before 2026-09-08 it
    #: was worse than refused, because a recipe asking for an angled plane got
    #: an *offset* one and an `ok`, which is defect 12; `tangent` was refused
    #: for want of a schema field naming the cylinder, which `face` now is.
    _WORK_PLANE_KINDS = ("offset", "midplane", "angle", "tangent")

    def work_plane(self, doc_id: str, request: WorkPlaneRequest) -> FeatureInfo:  # pragma: no cover
        """A datum plane: offset from another, between two, or turned about an axis.

        The angled one is `WorkPlanes.AddByLinePlaneAndAngle(axis, plane,
        angle)` and the tangent one `WorkPlanes.AddByPlaneAndTangent(plane,
        face)`, both published and **unmeasured** -- the whole surface is,
        since no run has built a work plane of any kind except through a
        sketch. Both are called positionally on purpose: the argument *order*
        is documented and the parameter *names* are not, so naming them would
        be inventing the one part nobody has read.

        The tangent one takes the face this server resolved rather than one
        Inventor picked, and refuses anything but a single cylindrical face
        *before* the call. That is worth a sentence, because the alternative is
        the failure mode this whole file is arranged against: a planar face
        would go into `AddByPlaneAndTangent` and come back as "Exception
        occurred", which says nothing about what the recipe got wrong, where
        "the selector matched a planar face" says it exactly.

        The angle goes in as the resolved value and then again as the
        expression, which is the pattern the offset already uses: the value
        makes the geometry right whatever happens next, and the expression is
        what keeps it parametric. If the second step fails the plane is at the
        right angle and a log line says it will not follow its parameter --
        which is the honest half-success, and better than a plane at zero.
        """
        document = self._doc(doc_id)
        component = document.ComponentDefinition
        if request.kind not in self._WORK_PLANE_KINDS:  # pragma: no cover - all four build
            raise FeatureError(
                f"A {request.kind!r} work plane cannot be built on Inventor by this "
                "server yet.",
                hint="'offset', 'midplane', 'angle' and 'tangent' are implemented.",
            )
        base = self._resolve_plane(doc_id, document, request.base, None)
        touched = (self._one_cylindrical_face(doc_id, request.face, "A tangent work plane")
                   if request.kind == "tangent" else None)
        with self._batch(document), self._translate_errors("Work plane"):
            if request.kind == "tangent":
                assert touched is not None
                assert request.face is not None and request.face.near is not None
                # **Four arguments, none optional**, read off the collection's
                # own type information on 2026-09-10 after the two-argument
                # call answered "Parameter not optional":
                #
                #     AddByPlaneAndTangent(Plane, Face, ProximityPoint,
                #                          Construction)
                #
                # And `ProximityPoint` is not ceremony -- it is the answer to
                # the question this operation could not previously ask. A
                # cylinder has **two** tangent planes parallel to any given
                # plane, and Autodesk's own signature settles which by taking
                # a point near the wanted one. So the ambiguity the simulator
                # was written to be honest about is one Inventor makes the
                # caller resolve, and `face.near` -- required by the schema
                # for this kind -- is where the recipe resolves it.
                near = self._require_app().TransientGeometry.CreatePoint(
                    *request.face.near)
                plane = component.WorkPlanes.AddByPlaneAndTangent(
                    base, self._live(doc_id, touched.id), near, False)
            elif request.kind == "angle":
                if request.axis is None:  # pragma: no cover - the schema refuses it
                    raise FeatureError(
                        "An angled work plane needs an axis to turn about.",
                        hint="Give `axis` on the operation: 'x', 'y', 'z', a work "
                        "axis, or a sketch line.",
                    )
                axis = self._resolve_axis(doc_id, request.axis)
                assert request.angle is not None
                # `AddByLinePlaneAndAngle(Line, Plane, Angle, Construction)`,
                # four arguments and none optional -- read off the type
                # information on 2026-09-10, which also settled that the
                # parameter *names* are published after all. The three-argument
                # form built correctly on 2026-09-09 (pywin32 supplies a
                # missing variant for the trailing one and Inventor takes it),
                # so this change is safety rather than a fix: a trailing
                # argument left to a marshalling default is one nobody chose.
                plane = component.WorkPlanes.AddByLinePlaneAndAngle(
                    axis, base, request.angle.value, False)
                try:
                    plane.Definition.Angle.Expression = request.angle.expression
                except Exception:  # pragma: no cover - version-specific
                    logger.warning(
                        "Work plane %r is at %s but will not follow that "
                        "expression: its definition would not take one.",
                        request.name or plane.Name, request.angle.expression)
            elif request.kind == "midplane" and request.second:
                plane = component.WorkPlanes.AddByTwoPlanes(
                    base, self._resolve_plane(doc_id, document, request.second, None)
                )
            else:
                plane = component.WorkPlanes.AddByPlaneAndOffset(base, 0.0)
                if request.offset is not None:
                    plane.Definition.Offset.Expression = request.offset.expression
            if request.name:
                plane.Name = request.name
            plane.Visible = False
        detail: dict[str, Any] = {"base": request.base, "kind": request.kind}
        if request.kind == "angle" and request.axis is not None:
            detail["axis"] = request.axis.value
            detail["angle"] = request.angle.as_dict() if request.angle else None
        if touched is not None:
            detail["face"] = touched.id
            detail["face_description"] = touched.description
        return FeatureInfo(id=f"wp:{plane.Name}", name=str(plane.Name), kind="work_plane",
                           detail=detail)

    # -- work points and axes ---------------------------------------------
    #
    # None of the three COM calls below has been run against a real Inventor:
    # this feature was written in a cloud session with no Inventor to reach.
    # `docs/INVENTOR_SETUP.md` lists what a live run has to confirm. The shape
    # of the code is chosen so that being wrong is loud rather than quiet --
    # every position comes from `build_sketch`, which measures the sketch's own
    # axes instead of deducing them from a plane's name, so a call that does not
    # exist raises and a call that does puts the geometry where the recipe said.
    # The alternative -- offsetting origin planes and intersecting them -- needs
    # the sign of an origin plane's normal, which nothing here has measured, and
    # a wrong sign there would build a part that looked right.

    def _carrier_point(self, doc_id: str, plane: str, at: Sequence[Driven],
                       offset_expression: str | None, tag: str) -> Any:  # pragma: no cover
        """A work point at *at* on *plane*, via a sketch that carries it.

        The sketch is how the point stays parametric: its two driving dimensions
        are the caller's own expressions, so the point moves when the parameter
        does. ``AddByPoint`` is the only unmeasured step.
        """
        document = self._doc(doc_id)
        component = document.ComponentDefinition
        sketch_name = f"{tag}_carrier"
        plan = SketchPlan(name=sketch_name, plane=plane)
        if offset_expression:
            plan.offset_expression = offset_expression
        u, v = at
        # Created *at* its position, then dimensioned to hold it there -- which
        # is what every other `PPoint` in `geometry.py` does, and what this call
        # alone did not. Built at (0, 0) instead, Inventor infers a coincidence
        # with the projected origin, that coincidence pins both degrees of
        # freedom, and the dimension meant to place the point cannot move it.
        # So the carrier point stayed on the origin, the work axis ran through
        # the origin, and a bolt circle about it came out symmetric: the centre
        # of mass did not move by so much as a micron when `bolt_x` changed,
        # while the volume did, because the pilot hole -- an ordinary sketch
        # point, built at its real position -- moved as asked. Defect 11,
        # measured 2026-09-07.
        #
        # The dimensions are `abs()` of the coordinate, so the sign lives in the
        # position and nowhere else. That is the second reason this cannot be
        # left to the dimension: from the origin, a dimension of 30 says nothing
        # about which side.
        point = plan.add(
            PPoint("point1", construction=True, position=(u.value, v.value)),
            _CARRIER_LABEL,
        )
        for kind, driven, text in (("horizontal", u, (0.0, -0.4)), ("vertical", v, (-0.4, 0.0))):
            if abs(driven.value) < 1e-9:
                plan.constrain(
                    "vertical_align" if kind == "horizontal" else "horizontal_align",
                    ORIGIN, Ref(point.id),
                )
            else:
                plan.dimension(kind, (ORIGIN, Ref(point.id)), driven.expression,
                               abs(driven.value), text_offset=text)
        self.build_sketch(doc_id, plan)
        sketch = self._sketch(doc_id, sketch_name)
        sketch_point = (self._labelled_entity(doc_id, str(sketch.Name), _CARRIER_LABEL)
                        or _named_sketch_point(sketch, _CARRIER_LABEL))
        with self._translate_errors("Work point"):
            work_point = component.WorkPoints.AddByPoint(sketch_point)
            work_point.Visible = False
        return work_point

    def work_point(self, doc_id: str, request: WorkPointRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        offset = request.offset.expression if request.offset and request.offset.value else None
        with self._batch(document):
            point = self._carrier_point(
                doc_id, request.plane, request.at, offset, request.name or "wpt"
            )
            if request.name:
                point.Name = request.name
        return FeatureInfo(id=f"wpt:{point.Name}", name=str(point.Name), kind="work_point",
                           detail={"plane": request.plane,
                                   "at": [component.as_dict() for component in request.at]})

    def work_axis(self, doc_id: str, request: WorkAxisRequest) -> FeatureInfo:  # pragma: no cover
        document = self._doc(doc_id)
        component = document.ComponentDefinition
        axes = component.WorkAxes
        with self._batch(document), self._translate_errors("Work axis"):
            if request.kind == "sketch_line":
                sketch = self._sketch(doc_id, request.sketch or "")
                line = (self._labelled_entity(doc_id, str(sketch.Name), request.line or "")
                        or _named_sketch_line(sketch, request.line or ""))
                axis = axes.AddByLine(line)
            elif request.kind == "two_points":
                first, second = (_named_work_point(component, name) for name in request.points)
                axis = axes.AddByTwoPoints(first, second)
            else:
                # Perpendicular to the plane, through `at`: two points at the
                # same place in the plane's coordinates, one on it and one on a
                # parallel plane above it. The separation only has to be
                # non-zero -- the direction is the plane's normal whatever it is
                # -- so it is a literal, while `at` stays the caller's
                # expressions on both points and the axis tracks the parameter.
                tag = request.name or "wax"
                low = self._carrier_point(doc_id, request.plane, request.at, None, f"{tag}_a")
                high = self._carrier_point(doc_id, request.plane, request.at,
                                           _CARRIER_SEPARATION, f"{tag}_b")
                axis = axes.AddByTwoPoints(low, high)
            if request.name:
                axis.Name = request.name
            axis.Visible = False
        return FeatureInfo(id=f"wax:{axis.Name}", name=str(axis.Name), kind="work_axis",
                           detail={"kind": request.kind, "plane": request.plane,
                                   "measured_against_inventor": False})

    #: Thread tables that `hole` + `tap` has been measured to accept on 2027.1,
    #: in the order a designation is tried against them. A designation is
    #: refused by a table it does not belong to, which is what makes trying the
    #: next one safe -- the failure names the table, not the shape.
    _THREAD_TABLES = ("ISO Metric profile", "ANSI Metric M Profile",
                      "ANSI Unified Screw Threads")

    def thread(self, doc_id: str, request: ThreadRequest) -> FeatureInfo:  # pragma: no cover
        """A cosmetic thread on a cylindrical face, by the published call.

        **Refused before it gets here.** `rehearsal._KNOWN_BROKEN` still lists
        `thread`, because nothing below has run against an Inventor; the recipe
        route that works is a `hole` with `tap`. What changed on 2026-09-08 is
        that the published reference names the call this used to guess at:

            ThreadFeatures.Add(Face, StartEdge, ThreadInfo, [DirectionReversed],
                               [FullDepth], [ThreadDepth], [ThreadOffset])

        `Face` must be a cylinder or cone, `StartEdge` "must be an edge of the
        input face", and `ThreadInfo` a `StandardThreadInfo` for a cylinder.
        The `CreateThreadDefinition` this called before exists on no release.

        Where the `ThreadInfo` comes from is published too, since the per-member
        page was read on 2026-09-08:

            ThreadFeatures.CreateStandardThreadInfo(Internal, RightHanded,
                ThreadType, ThreadDesignation, Class) As StandardThreadInfo

        The 2027.1 makepy wrapper does not list it -- the shape
        `WorkPoints.AddByPoint` had, which executed regardless -- so it is called
        late-bound in that order. `HoleFeatures.CreateTapInfo`, measured and
        documented to make a `HoleTapInfo` that derives from
        `StandardThreadInfo`, is the fallback, with `Internal` set on the
        result. Every failure is named.
        """
        document = self._doc(doc_id)
        faces = self._topology_collection(doc_id, request.faces)
        if int(faces.Count) == 0:
            raise FeatureError(
                "No faces matched, so there is nothing to thread.",
                hint="Run `select_topology` with the same selector; a thread wants "
                "one cylindrical face.",
            )
        face = faces.Item(1)
        try:
            start_edge = face.Edges.Item(1)
        except Exception as exc:
            raise FeatureError(
                f"The face to thread has no edge to start from: {_com_message(exc)}",
                hint="ThreadFeatures.Add wants an edge of the threaded face as its "
                "StartEdge, and a face with no edges is not a cylinder.",
            ) from exc
        component = document.ComponentDefinition
        features = component.Features.ThreadFeatures
        full_depth = request.depth is None
        with self._batch(document), self._translate_errors("Thread"):
            info, info_from = self._thread_info(component.Features, request)
            arguments = [face, start_edge, info, False, full_depth]
            if request.depth is not None:
                arguments.append(request.depth.expression)
            try:
                feature = features.Add(*arguments)
            except Exception as exc:
                raise FeatureError(
                    f"Thread failed: {self._explain(exc)}",
                    hint=f"ThreadInfo came from {info_from}. The published "
                    "signature is Add(Face, StartEdge, ThreadInfo, "
                    "[DirectionReversed], [FullDepth], [ThreadDepth], "
                    "[ThreadOffset]); a refusal here most likely means the tap "
                    "info is not accepted where a StandardThreadInfo is wanted, "
                    "and `python scripts/com_signatures.py --search ThreadInfo` "
                    "is where to look next. `hole` + `tap` is the measured route.",
                ) from exc
            if request.name:
                feature.Name = request.name
        return _feature_info(feature, "thread", {
            "designation": request.designation,
            "internal": request.internal,
            "thread_info_from": info_from,
        })

    def _thread_info(self, features: Any, request: ThreadRequest) -> tuple[Any, str]:  # pragma: no cover
        """A `StandardThreadInfo` for the designation, and which call made it.

        The published `CreateStandardThreadInfo(Internal, RightHanded,
        ThreadType, ThreadDesignation, Class)` first, in that order. The class
        follows the table and the side, as the page's own examples do -- `2B`
        for an internal inch thread, `6g` for an external metric one. The
        measured `CreateTapInfo` is the fallback, its result documented to be a
        `StandardThreadInfo`, with `Internal` set because a tap info is internal
        by construction.
        """
        failures: list[str] = []
        standard = getattr(features.ThreadFeatures, "CreateStandardThreadInfo", None)
        if standard is None:
            failures.append("ThreadFeatures.CreateStandardThreadInfo: no such method on "
                            "this release")
        else:
            for table in self._THREAD_TABLES:
                try:
                    info = standard(bool(request.internal), True, table,
                                    request.designation, _thread_class(table, request.internal))
                except Exception as exc:
                    failures.append(f"CreateStandardThreadInfo({table!r}): {_com_message(exc)}")
                    continue
                return info, f"ThreadFeatures.CreateStandardThreadInfo [{table}]"

        tap = getattr(features.HoleFeatures, "CreateTapInfo", None)
        if tap is None:
            failures.append("HoleFeatures.CreateTapInfo: no such method on this release")
        else:
            for table in self._THREAD_TABLES:
                try:
                    info = tap(True, table, request.designation,
                               _thread_class(table, request.internal), True)
                except Exception as exc:
                    failures.append(f"CreateTapInfo({table!r}): {_com_message(exc)}")
                    continue
                try:
                    info.Internal = bool(request.internal)
                except Exception as exc:
                    failures.append(f"CreateTapInfo({table!r}): Internal is not settable "
                                    f"({_com_message(exc)})")
                    if not request.internal:
                        continue
                return info, f"HoleFeatures.CreateTapInfo [{table}]"
        raise FeatureError(
            f"Nothing on this release made a ThreadInfo for {request.designation!r}: "
            + "; ".join(failures),
            hint="A designation must carry its pitch (M8x1.25, not M8) and belong "
            "to one of the tables tried. If every table refused, the shape of the "
            "designation is the first thing to check; if the makers themselves are "
            "missing, `python scripts/com_signatures.py --search ThreadInfo` says "
            "what this release creates one with.",
        )

    # -- model state -------------------------------------------------------
    def list_work_geometry(self, doc_id: str) -> dict[str, list[str]]:  # pragma: no cover
        """The names in `WorkPlanes`, `WorkAxes` and `WorkPoints`.

        A backend method rather than something a script reads for itself, for
        the reason `describe_feature` records: reaching into a returned COM
        object from another thread fails with "the application called an
        interface that was marshalled for a different thread". The first
        version of the work-geometry acceptance check did exactly that and got
        exactly that error, which then read as a missing work point.

        It exists because `list_features` walks `ComponentDefinition.Features`
        and Inventor keeps work geometry elsewhere, so the two backends disagree
        about whether a work point is a feature. Fixing that needs to know
        whether Inventor's *origin* planes, axes and point sit in these same
        collections and how a created one is told from them, which nothing here
        has measured -- so this reports the names and the acceptance run prints
        them, rather than a guess going into the listing everything else trusts.
        """
        component = self._doc(doc_id).ComponentDefinition
        found: dict[str, list[str]] = {}
        for key, attribute in (("work_planes", "WorkPlanes"),
                               ("work_axes", "WorkAxes"),
                               ("work_points", "WorkPoints")):
            try:
                collection = getattr(component, attribute)
                found[key] = [str(collection.Item(index).Name)
                              for index in range(1, int(collection.Count) + 1)]
            except Exception as exc:
                found[key] = [f"<unreadable: {_com_message(exc)}>"]
        return found

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

    def _reference_key(self, doc_id: str, entity: Any) -> Any | None:  # pragma: no cover
        """A durable key for *entity*, or ``None`` if this release will not give one.

        `Entity.GetReferenceKey(KeyContext)` with a context from
        `Document.ReferenceKeyManager.CreateKeyContext()` -- published, and the
        published rule is the reason the context is kept per document rather
        than made per call: **a B-Rep key needs the context it was made with**
        to be bound back, so a context created and thrown away is a key that
        can never be used.

        **The manager exists and the marshalling is still unsettled**, which
        the 2026-09-09 run narrowed rather than answered. `ReferenceKeyManager`
        is there on 2027.1 with `CreateKeyContext` (returning 1),
        `BindKeyToObject`, `CanBindKeyToObject`, `KeyToString` and
        `StringToKey`. What would not go through was getting the key out of
        the face -- and the error named the reason:

            GetReferenceKey(context) -> TypeError: Objects for SAFEARRAYS must
                                        be sequences (of sequences), or a
                                        buffer object

        pywin32 had tried to marshal the integer context **as the byte
        array**, so the key is the *first* parameter and the context the
        second. That is what the two calls below try, in that order, and the
        second is the form the type library implies. Both are wrapped, because
        `None` here costs nothing: a handle with no key behaves exactly as
        every handle behaved before this existed, and `_live` still refuses a
        stale one rather than handing back a dead object.
        `scripts/probe_reference_keys.py` carries four spellings of the array
        argument and the `KeyToString` route, which is the run that settles it.
        """
        context = self._key_contexts.get(doc_id)
        if context is None:
            try:
                context = self._doc(doc_id).ReferenceKeyManager.CreateKeyContext()
            except Exception as exc:
                logger.debug("No ReferenceKeyManager on this release: %s", exc)
                self._key_contexts[doc_id] = False
                return None
            self._key_contexts[doc_id] = context
        if context is False:
            return None
        for attempt in (lambda: entity.GetReferenceKey(b"", context),
                        lambda: entity.GetReferenceKey(bytearray(), context)):
            try:
                return attempt()
            except Exception as exc:
                logger.debug("GetReferenceKey declined: %s", exc)
        return None

    def _live(self, doc_id: str, handle: str) -> Any:  # pragma: no cover
        """The entity *handle* names, rebound if the model has moved under it.

        Every use of a topology handle goes through here, and that is the
        point. Before 2026-09-09 the stored COM object was used directly, so a
        handle from before a rebuild handed back a **dead** object: the
        docstrings said handles expire and nothing enforced it, so what a
        caller got was not a refusal but a pointer at geometry that no longer
        existed. The DFM loop is the customer for the fix -- a finding points
        at faces, the loop changes a parameter and rebuilds, and the faces it
        pointed at are gone.

        Three outcomes, in order: the stored object still answers, so use it;
        it does not and a reference key rebinds it, so use that and keep it;
        or neither, and the handle is **refused** with what to do about it.
        """
        entry = self._topology.get(handle)
        if entry is None:
            raise SelectionError(
                f"Unknown topology handle {handle!r}.",
                hint="Handles come from `select_topology` and belong to one "
                "document. Run it again to get current ones.",
            )
        entity = entry.get("object")
        if entity is not None and _still_there(entity):
            return entity
        key, context = entry.get("key"), self._key_contexts.get(doc_id)
        if key is not None and context not in (None, False):
            try:
                rebound = self._doc(doc_id).ReferenceKeyManager.BindKeyToObject(
                    key, context)
            except Exception as exc:
                logger.info("Handle %s could not be rebound: %s", handle, exc)
            else:
                if rebound is not None and _still_there(rebound):
                    entry["object"] = rebound
                    logger.info("Handle %s was rebound after a rebuild.", handle)
                    return rebound
        raise SelectionError(
            f"Topology handle {handle!r} no longer points at anything.",
            hint="The model was rebuilt and this handle did not survive it"
            + ("" if key is not None else
               " -- and this release gave no reference key for it, so it could "
               "not be rebound")
            + ". Run `select_topology` again against the part as it is now.",
        )

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
        convexity = None
        if selector.kind == "edge":
            convexity = _convexity_index(
                component.SurfaceBodies.Item(index)
                for index in range(1, int(component.SurfaceBodies.Count) + 1))
        results: list[TopoInfo] = []
        seen: set[int] = set()
        for entity in candidates:
            key = id(entity)
            if key in seen:
                continue
            seen.add(key)
            info = self._describe(doc_id, entity, selector.kind, convexity)
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

    def _describe(self, doc_id: str, entity: Any, kind: str,
                  convexity_index: dict[str, set[int]] | None = None) -> TopoInfo | None:  # pragma: no cover
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
            convexity, decided_by = _edge_convexity(entity, convexity_index)
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

        # Which feature made it, from the published `Face.CreatedByFeature`.
        # The simulator has answered this since it was written and this
        # backend never did, so `TopoInfo.feature` was set on one side and
        # `None` on the other -- and it is the field that makes a DFM finding
        # sayable as "the faces of the boss" rather than as four indices.
        info.feature = _created_by(entity, kind)
        # A durable key, so the handle can be rebound after a rebuild. `None`
        # from a release that will not give one, which costs nothing: the
        # handle then behaves exactly as every handle behaved before this.
        key = self._reference_key(doc_id, entity)
        info.durable = key is not None
        self._topology[handle] = {"object": entity, "info": info,
                                  "direction": direction, "key": key}
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
            center_of_mass_from="Inventor's MassProperties",
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
                entry = self._health_entry(feature, int(status))
                if healthy is None:
                    uninterpreted.append(entry)
                elif int(status) in healthy:
                    continue
                elif entry["suppressed"] and entry["status"] == "kSuppressedHealth":
                    # A suppressed feature reports itself suppressed. That is
                    # the state asked for, not a fault in it.
                    continue
                else:
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

    @staticmethod
    def _health_entry(feature: Any, status: int) -> dict[str, Any]:
        """One feature's health, with the status translated where the table can.

        The name comes from the published HealthStatusEnum page rather than the
        type library, which does not carry the enum on 2027.1 -- so it is
        reported beside the number, not instead of it, and a value the page
        does not list reads as ``unrecognised`` rather than being guessed at.
        """
        return {
            "feature": str(feature.Name),
            "health_status": int(status),
            "status": HEALTH_STATUS_NAMES.get(int(status), "unrecognised"),
            "suppressed": bool(getattr(feature, "Suppressed", False)),
        }

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
        if "pattern" in str(described.get("kind", "")).lower():
            # Only for a pattern, and only because the count is a real open
            # question there: `spread_pockets` measured exactly and still could
            # not say whether Inventor puts an occurrence on the reference
            # point. Asking every feature would put a number under a name that
            # meant something else on whichever release has one.
            #
            # **Under `pattern_elements` rather than `occurrences`**, which is
            # not fussiness. `occurrences` is already spoken for twice in this
            # project and means different things: a rectangular pattern's
            # feature detail counts every instance *including* the seed, and a
            # sketch-driven pattern's counts the copies only. A number read off
            # Inventor is a third thing again -- what that release's collection
            # holds -- so it gets its own name and says which collection
            # answered, and the acceptance check calibrates what it counts
            # against a pattern whose total is not in doubt.
            count, from_where = _occurrence_count(feature)
            if count is not None:
                described["pattern_elements"] = count
                described["pattern_elements_from"] = from_where
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
        """Write the document out, through the translator add-in where there is one.

        Two routes, and which one ran is in the result.

        `TranslatorAddIn.SaveCopyAs(document, context, options, data)` is the
        one that **takes options**, fetched by `ApplicationAddIns.ItemById` on
        the ClassId GUID in `EXPORT_TRANSLATORS`. `Document.SaveAs` reaches no
        options at all: it hands the path to whichever translator claims the
        extension, which then uses whatever settings somebody last picked in
        its dialog -- so a STEP file comes out in whichever application
        protocol that was, and nothing says which.

        `SaveAs` stays as the fallback rather than being replaced, and that is
        deliberate: it is the route that has been measured, the GUIDs have not
        been, and a format with nothing to configure has nothing to gain. So a
        translator that cannot be found or refuses `SaveCopyAs` drops back to
        it, and the result says the options were not applied. **Options that
        were asked for and could not be passed are a hard error instead**,
        because a file quietly written with the wrong settings is worse than no
        file: the caller asked for AP 214 and would get a STEP file they had no
        reason to doubt.
        """
        document = self._doc(doc_id)
        fmt = request.format.lower()
        if fmt not in EXPORT_EXTENSIONS:
            raise ExportError(
                f"Unsupported export format {request.format!r}.",
                hint="Supported: " + ", ".join(sorted(set(EXPORT_EXTENSIONS))),
            )
        options = self._checked_export_options(fmt, request.options)
        path = os.path.abspath(request.path)
        expected = EXPORT_EXTENSIONS[fmt]
        if not path.lower().endswith(expected) and not path.lower().endswith(f".{fmt}"):
            path += expected
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        translator, why_not = self._translator(fmt)
        with self._translate_errors(f"Exporting to {fmt.upper()}", ExportError):
            if translator is not None:
                applied = self._save_copy_as(translator, document, path, options)
                route = "translator"
            else:
                if options:
                    raise ExportError(
                        f"The {fmt.upper()} translator add-in could not be "
                        f"reached, so these options cannot be passed: "
                        f"{', '.join(sorted(options))}.",
                        hint=f"{why_not} `Document.SaveAs` is the fallback and "
                        "it reaches no options -- it would write the file with "
                        "whatever settings were last used in the translator's "
                        "dialog, which is not what was asked for. Export "
                        "without options to take that route deliberately, or "
                        "run scripts/probe_translators.py to see which add-ins "
                        "this Inventor has and what their ClassIds really are.",
                    )
                document.SaveAs(path, True)
                applied = {}
                route = "SaveAs"
        if not os.path.exists(path):
            raise ExportError(
                f"Inventor reported success but {path} was not written.",
                hint="The translator add-in for this format may be disabled in Inventor.",
            )
        result: dict[str, Any] = {
            "written": True, "path": path, "format": fmt,
            "bytes": os.path.getsize(path), "route": route,
        }
        if applied:
            result["options_applied"] = applied
        if route == "SaveAs" and fmt in EXPORT_TRANSLATORS:
            result["note"] = (
                f"{why_not} So this file was written by `Document.SaveAs`, "
                "which uses whatever settings were last chosen in the "
                "translator's own dialog. The geometry is right; the settings "
                "are not this server's.")
        return result

    def _translator(self, fmt: str) -> tuple[Any, str | None]:  # pragma: no cover
        """The translator add-in for *fmt*, or ``None`` and why not.

        `ItemById` raises on a GUID no add-in has, which is what makes a wrong
        entry in `EXPORT_TRANSLATORS` loud rather than silent -- and the reason
        that table can carry values nobody here has measured. The exception's
        text goes into the reason, because "no add-in with that ClassId" and
        "the add-in is present and not activated" are different problems with
        different fixes and only Inventor can tell them apart.
        """
        guid = EXPORT_TRANSLATORS.get(fmt)
        if guid is None:
            return None, f"No translator add-in is recorded for {fmt!r}."
        app = self._require_app()
        try:
            translator = app.ApplicationAddIns.ItemById(guid)
        except Exception as exc:
            return None, (f"Inventor has no add-in with ClassId {guid}: "
                          f"{type(exc).__name__}: {exc}.")
        # **Through dynamic dispatch, measured 2026-09-09.** `ItemById` is
        # declared as returning an `ApplicationAddIn`, so the makepy wrapper
        # is that class -- and `HasSaveCopyAsOptions` and `SaveCopyAs` are
        # `TranslatorAddIn` members, which it does not have. Early-bound, the
        # object answered `AttributeError: ... has no attribute
        # 'HasSaveCopyAsOptions'` for all seven translators, which reads as
        # "this release has no options" and is nothing of the kind: the object
        # *is* a translator, the wrapper's declared type is not. This is the
        # same trap `_dynamic`'s own docstring records for `Features.Item`.
        translator = _dynamic(translator)
        try:
            if not bool(translator.Activated):
                translator.Activate()
        except Exception as exc:
            return None, (f"The {fmt.upper()} translator add-in is present and "
                          f"would not activate: {type(exc).__name__}: {exc}.")
        return translator, None

    #: `TranslationContext.Type` for a translator writing to a named file, as
    #: opposed to a stream held in memory. `kFileBrowseIOMechanism`.
    _FILE_BROWSE = "kFileBrowseIOMechanism"

    def _save_copy_as(self, translator: Any, document: Any, path: str,
                      options: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        """`SaveCopyAs` through *translator*, with *options* on its NameValueMap.

        The four arguments are all `TransientObjects` creations rather than
        anything this code invents, which is why they are built here and not
        cached: a `DataMedium` carries the filename and a `NameValueMap` the
        settings, and both are per-call.

        `HasSaveCopyAsOptions` is asked first and its answer is *reported*
        rather than acted on. A translator that says it has no options and then
        takes them is harmless; one that says it has them and ignores a name is
        the failure `_checked_export_options` guards, and this is the second
        half of that guard -- the result names every option that went in, so a
        file that came out wrong can be read against what was asked for.
        """
        app = self._require_app()
        transient = app.TransientObjects
        context = transient.CreateTranslationContext()
        context.Type = self._k(self._FILE_BROWSE)
        settings = transient.CreateNameValueMap()
        medium = transient.CreateDataMedium()
        medium.FileName = path
        offered = True
        try:
            offered = bool(translator.HasSaveCopyAsOptions(document, context, settings))
        except Exception as exc:  # pragma: no cover - version-specific
            logger.debug("HasSaveCopyAsOptions declined to answer: %s", exc)
        for name, value in options.items():
            _set_option(settings, name, value)
        translator.SaveCopyAs(document, context, settings, medium)
        return {"offered_options": offered, **options}

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
            # A display mode Inventor will not take used to be swallowed here,
            # which meant the picture came back in whatever mode the view was
            # already in and nothing said so. That is how `hidden_line` went
            # years asking for an enum name Inventor does not have: it never
            # raised, it just quietly rendered shaded.
            mode = DISPLAY_MODES.get(request.display_mode)
            refused: str | None = None
            if mode:
                try:
                    view.DisplayMode = self._k(mode)
                except Exception as exc:
                    refused = f"{type(exc).__name__}: {exc}"
            view.SaveAsBitmap(path, request.width, request.height)
        result: dict[str, Any] = {
            "written": os.path.exists(path), "path": path,
            "width": request.width, "height": request.height,
            "display_mode": request.display_mode,
            "display_mode_applied": bool(mode) and refused is None,
        }
        if refused is not None:
            result["note"] = (
                "Inventor would not take that display mode, so this picture is "
                f"in whatever mode the view was already in: {refused}")
        elif not mode:
            result["note"] = (
                f"No display mode is mapped for {request.display_mode!r}, so this "
                "picture is in whatever mode the view was already in.")
        return result


# ---------------------------------------------------------------------------
# Small COM helpers
# ---------------------------------------------------------------------------


def _thread_class(table: str, internal: bool) -> str:
    """The thread class the published examples give for a table and a side.

    `2B` and `2A` for the inch tables, `6H` and `6g` for the metric ones --
    capital for internal, as the standards write them. A table this cannot
    place is treated as metric, which the two metric tables tried here are.
    """
    if "unified" in table.lower():
        return "2B" if internal else "2A"
    return "6H" if internal else "6g"


def _iterate(collection: Any) -> Iterator[Any]:  # pragma: no cover - Windows only
    for index in range(1, int(collection.Count) + 1):
        yield collection.Item(index)


def _style_override(primitive: PText) -> str:
    """The formatted-text markup Inventor's ``TextBoxes.AddFitted`` takes.

    A module-level function so it can be tested off Windows.  It was written
    inline, with the attribute quotes escaped inside the f-string, and a
    backslash in an f-string expression is a syntax error before Python 3.12 --
    so this module did not *parse* on the oldest interpreter ``pyproject``
    claims, and ``--backend auto`` raised ``SyntaxError`` there instead of
    falling back to the simulator.  Concatenation avoids the escape entirely.
    """
    escaped = (primitive.text.replace("&", "&amp;")
               .replace("<", "&lt;").replace(">", "&gt;"))
    weight = ' Bold="True"' if primitive.bold else ""
    slant = ' Italic="True"' if primitive.italic else ""
    return (f'<StyleOverride Font="{primitive.font}" FontSize="{primitive.height}"'
            + weight + slant + f">{escaped}</StyleOverride>")


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


def _why_a_name_is_refused(name: str) -> str:
    """The measured reason Inventor turns down a parameter name, where it fits.

    Measured on 2026-09-07, Inventor 2027.1, by asking it for nine names in one
    document: it took ``bolt_x``, ``PCD``, ``pcd_1``, ``bolt_pcd``, ``dia``,
    ``pitch`` and ``bolt_spacing``, and refused ``cd`` and ``pcd``.

    ``cd`` is the candela and ``pcd`` is the pico-candela, so **Inventor refuses
    a name it can read as a unit, including one built from an SI prefix and a
    unit symbol** -- and it is case-sensitive, which is why ``PCD`` is fine. That
    is a far wider set than a list of names could cover: ``mm``, ``ms``, ``kg``,
    ``ncd``, ``mcd``, ``kA`` and many more are all names Inventor will decline,
    and this server's own unit table does not know candela at all, so it cannot
    detect them in advance. What it can do is stop the failure being a mystery.
    """
    lowered = name.lower()
    prefixes = "y z a f p n u m c d da h k M G T P E Z Y"
    if len(name) <= 4 and lowered == name:
        return (
            "Inventor refuses a name it can read as a unit, and it is "
            f"case-sensitive -- {name.upper()!r} may well be accepted where "
            f"{name!r} is not. Measured on 2027.1: 'cd' (candela) and 'pcd' "
            "(pico-candela) were both refused while 'PCD', 'pcd_1' and "
            "'bolt_pcd' were taken. A short lower-case name risks colliding "
            f"with a unit symbol or an SI prefix on one ({prefixes}), so "
            "lengthen it, add an underscore, or capitalise it."
        )
    return ("Inventor refuses a name it can read as a unit -- measured on "
            "2027.1 for 'cd' and 'pcd' -- so check the name against Inventor's "
            "unit symbols. Otherwise try lengthening it.")


#: The label the carrier sketch gives its one point, so it can be found again.
_CARRIER_LABEL = "__work_point__"

#: How far apart the two points defining a `normal_to_plane` axis sit. Any
#: non-zero separation gives the same axis, so this is a literal rather than an
#: expression -- there is no parameter it could sensibly track.
_CARRIER_SEPARATION = "10 mm"


def _entities_by_label(plan: SketchPlan, objects: dict[str, Any]) -> dict[str, Any]:
    """The Inventor entities `build_sketch` created, against the recipe's labels.

    ``objects`` is keyed by primitive id, which is what constraints and
    dimensions resolve through; a caller asking later has only the label the
    recipe wrote. Nothing was keeping the two together, so every lookup by label
    searched Inventor for a *name* no code assigns -- see `_labelled_entity`.

    A label can cover several primitives: a rectangle named "Outline" is four
    lines under one label. The first wins, which is the rule `resolve_axis`
    already applies when it takes the first `PLine` under a label. A primitive
    Inventor declined to create has no entry in ``objects`` and contributes
    nothing, rather than storing ``None`` for a caller to trip over.
    """
    by_label: dict[str, Any] = {}
    for primitive in plan.primitives:
        label = getattr(primitive, "label", None)
        entity = objects.get(primitive.id)
        if label and entity is not None and label not in by_label:
            by_label[label] = entity
    return by_label


def _named_sketch_point(sketch: Any, label: str) -> Any:  # pragma: no cover - Windows only
    points = sketch.SketchPoints
    for index in range(1, int(points.Count) + 1):
        point = points.Item(index)
        if str(getattr(point, "Name", "")) == label:
            return point
    raise FeatureError(
        f"The carrier sketch did not keep a point named {label!r}.",
        hint="Inventor renamed or dropped it; a work point cannot be placed without it.",
    )


def _named_sketch_line(sketch: Any, name: str) -> Any:  # pragma: no cover - Windows only
    lines = sketch.SketchLines
    for index in range(1, int(lines.Count) + 1):
        line = lines.Item(index)
        if str(getattr(line, "Name", "")) == name:
            return line
    raise FeatureError(
        f"Sketch {str(sketch.Name)!r} has no line named {name!r} to lie a work axis along.",
        hint="Give the sketch line a `name` in the recipe and reference it here.",
    )


def _named_work_point(component: Any, name: str) -> Any:  # pragma: no cover - Windows only
    points = component.WorkPoints
    for index in range(1, int(points.Count) + 1):
        if str(points.Item(index).Name) == name:
            return points.Item(index)
    raise FeatureError(
        f"No work point named {name!r}.",
        hint="Create it with a `work_point` operation before the axis that runs through it.",
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


def _document_path(document: Any) -> str:  # pragma: no cover - Windows only
    """Where this document lives on disk, or "" for one that has never been saved.

    `FullFileName` is empty rather than absent on an unsaved document, and the
    read itself can fail on a document being closed, so both come back as "".
    """
    try:
        return str(document.FullFileName) or ""
    except Exception:
        return ""


def _parameter_units(parameter: Any, fallback: str = "mm") -> str:  # pragma: no cover - Windows only
    """This project's name for the unit a live parameter is measured in.

    For `promote_parameter`, which creates a user parameter to hold what a
    feature's property held. The unit has to come from the property rather than
    from a default: an angle promoted into a millimetre parameter is refused,
    and the refusal is Inventor's usual bare "Exception occurred".

    The fallback is a length because every other promotable property is one,
    and because a parameter whose units cannot be read is better attempted than
    refused -- `set_parameter` reports what Inventor said either way.
    """
    try:
        return unit_from_inventor(str(parameter.Units)) or fallback
    except Exception:
        return fallback


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
    "kSketchDrivenPatternFeatureObject": "sketch_driven_pattern",
    # Measured: 2027.1's type library has no kDraftFeatureObject -- the face
    # draft feature's enum is this one.
    "kFaceDraftFeatureObject": "draft",
    "kMoveFaceFeatureObject": "move_face",
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


def _count_curves(sketch: Any) -> int:  # pragma: no cover - Windows only
    """How many curves a sketch holds, across every collection that has any.

    Asked immediately after `Sketches.Add(plane, True)` so the count of what
    the face's outline contributed is known before the recipe's own geometry
    goes in. `SketchEntities` would be one call, and it is not used: it also
    counts points and text, and the number wanted here is curves.
    """
    total = 0
    for name in ("SketchLines", "SketchCircles", "SketchArcs", "SketchEllipses",
                 "SketchSplines"):
        try:
            total += int(getattr(sketch, name).Count)
        except Exception:
            continue
    return total


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


def _fully_constrained(sketch: Any, constants: Constants) -> bool | None:
    """Inventor's own verdict on a sketch, or ``None`` when it will not give one.

    There is no ``FullyConstrained`` property. This looked for one -- and for
    ``IsFullyConstrained`` -- and neither exists, so it returned ``None`` for
    every sketch on every version and the flag never arrived at all.
    ``ConstraintStatus`` is what Inventor actually answers, and it is a
    four-value enum: measured on 2027.1 both from the type library
    (``scripts/com_signatures.py --search Constrain``) and from a live sketch
    through late binding, which is the only way to be sure a missing attribute
    is Inventor's answer and not the makepy wrapper's.

    Over-constrained and unknown both come back ``None``: a bool cannot say
    "constrained, but wrongly", and ``refused_dimensions`` is where that shows
    up instead.

    There is no degrees-of-freedom count to go with it -- see
    :func:`~inventor_mcp.backend.mock.backend._degrees_of_freedom`.
    """
    status = getattr(_dynamic(sketch), "ConstraintStatus", None)
    if not isinstance(status, int) or isinstance(status, bool):
        return None
    if status == constants["kFullyConstrainedConstraintStatus"]:
        return True
    if status == constants["kUnderConstrainedConstraintStatus"]:
        return False
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


def _set_option(settings: Any, name: str, value: Any) -> None:  # pragma: no cover
    """Put *name* on a `NameValueMap`, replacing it if it is already there.

    `Value` is a *parameterised* property, and the VBA spelling for setting one
    -- `map.Value("Name") = 3` -- has no equivalent through late binding in
    Python: `map.Value(name)` is a call, and a call is not an assignment
    target. `Add(Name, Value)` is the method, and it refuses a name the map
    already holds -- which it will, because `HasSaveCopyAsOptions` fills the
    map with the translator's own defaults before this runs. So a name already
    present is removed by index and added again.

    `Remove` is 1-based, as every Inventor collection is.
    """
    for index in range(1, int(settings.Count) + 1):
        if str(settings.Name(index)) == name:
            settings.Remove(index)
            break
    settings.Add(name, value)


def _created_by(entity: Any, kind: str) -> str | None:  # pragma: no cover
    """The name of the feature that made this face, or edge's face.

    `Face.CreatedByFeature` is published and a plain property. An **edge** has
    no such property -- it is where two faces meet, so it belongs to both --
    and the answer taken here is the first of its faces that will say, which
    is what "the edges of the boss I just made" means in practice and what a
    selector's `feature` filter already matches on the face side.
    """
    try:
        if kind == "face":
            feature = entity.CreatedByFeature
        else:
            feature = next(
                (made for made in
                 (getattr(face, "CreatedByFeature", None) for face in _iterate(entity.Faces))
                 if made is not None), None)
        return None if feature is None else str(feature.Name)
    except Exception:
        return None


def _still_there(entity: Any) -> bool:  # pragma: no cover - Windows only
    """Whether a stored COM entity still refers to live geometry.

    Probed by asking for something every B-Rep entity has and nothing computes
    -- `Evaluator` -- because a dead reference raises on the first access and a
    live one does not. Cheap, and it is the only way to tell: Inventor does not
    hand out a validity flag, and a released object looks like an object until
    it is touched.
    """
    try:
        return entity.Evaluator is not None
    except Exception:
        return False


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


def _convexity_index(bodies: Any) -> dict[str, set[int]] | None:  # pragma: no cover
    """Inventor's own classification of every edge on *bodies*, keyed by `TransientKey`.

    `SurfaceBody.ConvexEdges` and `SurfaceBody.ConcaveEdges` are documented
    read-only `EdgeCollection`s -- "all inside corners" in one property -- and
    `Edge.TransientKey` is documented as an id "valid only while the document
    state remains unchanged", which is exactly the lifetime of one `select`
    call. Read once per selection rather than once per edge, because comparing
    COM identities edge by edge is quadratic.

    Returns None when either collection is unreadable, which is how a release
    without them, or a fake in a test, says so. Never executed against an
    Inventor: `docs/INVENTOR_SETUP.md` says what a run must confirm.
    """
    index: dict[str, set[int]] = {"convex": set(), "concave": set()}
    try:
        for body in bodies:
            for verdict, attribute in (("convex", "ConvexEdges"), ("concave", "ConcaveEdges")):
                collection = getattr(body, attribute)
                for edge in _iterate(collection):
                    index[verdict].add(int(edge.TransientKey))
    except Exception:
        return None
    return index


def _convexity_from_body(edge: Any, index: dict[str, set[int]] | None) -> str | None:  # pragma: no cover
    """What the body's own convex/concave collections say about *edge*, or None."""
    if not index:
        return None
    try:
        key = int(edge.TransientKey)
    except Exception:
        return None
    convex, concave = key in index["convex"], key in index["concave"]
    if convex == concave:
        return None  # in neither (tangent, or unclassified), or absurdly in both
    return "convex" if convex else "concave"


def _edge_convexity(edge: Any,
                    index: dict[str, set[int]] | None = None) -> tuple[str | None, str]:  # pragma: no cover
    """Whether an edge is an outside corner or an inside one, and how we know.

    The boundary loops give an exact answer and are measured, so they decide
    wherever they can. Where they decline -- a full circle has no endpoints to
    orient it by, which is why `flanged_shaft`'s chamfer could never ask for
    ``convex`` -- the body's own ``ConvexEdges`` / ``ConcaveEdges`` collections
    are asked, when the caller has read them. That is Inventor's own
    classification and the documented one; it is placed *behind* the loops
    rather than in front of them because the loops are measured on this
    release and the collections have never run here, and defect 5 is what
    happens when an unmeasured answer is trusted over a measured one. Where
    both answer and disagree, the loops win and the disagreement is logged,
    which is what a live run reads to decide whether the order should flip.

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
    body_says = _convexity_from_body(edge, index)
    if decided is not None:
        if body_says is not None and body_says != decided:
            logger.warning(
                "Edge convexity: the boundary loops say %s and the body's own "
                "collections say %s. Trusting the loops, which are measured; "
                "this disagreement is what a live run should look at.",
                decided, body_says)
        return (decided, "loops")
    if body_says is not None:
        return (body_says, "body")
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



def _volume_change(document: Any, before: float) -> float | None:  # pragma: no cover
    """How much the part's volume moved since *before*, or None if unreadable."""
    after = _solid_volume(document)
    return None if after is None else after - before


# ---------------------------------------------------------------------------
# Drawings
# ---------------------------------------------------------------------------
#
# Everything below reads a drawing object rather than creating one, and every
# one of them is written to answer None rather than raise. That is deliberate
# and is the difference between reading a sheet and building one: a sheet read
# back is evidence, and a reader that raises on the first property a release
# spells differently produces no evidence at all. A missing answer is recorded
# as missing and the caller can say so.


def _model_parameter(annotation: Any) -> tuple[str | None, str | None]:  # pragma: no cover - Windows only
    """The name and expression of the parameter a model annotation is driven by.

    A `DimensionConstraint.Parameter` is documented; a proxy of one reaches the
    same thing through `NativeObject`. A `FeatureDimension` is documented to
    exist and its members are not published on the pages read here, so the same
    two paths are tried and (None, None) is the honest answer when neither
    exists.

    **The expression matters as much as the name, and that took a run to
    learn.** The parameter a sketch dimension is driven by is a *model*
    parameter -- `d0`, `d4`, `d7` -- and the user parameter a recipe names is
    what that model parameter's expression *references*. Measured on 2027.1,
    2026-09-08: Inventor offered eight retrievable annotations on a view and
    the names were `d0, d1, d4, d5, d6, d7, d8, d9`, so a match on the name
    alone found nothing and the whole design read as unmeasurable.
    """
    for path in (("Parameter",), ("NativeObject", "Parameter")):
        current: Any = annotation
        for step in path:
            current = getattr(current, step, None)
            if current is None:
                break
        if current is None:
            continue
        name = getattr(current, "Name", None)
        expression = getattr(current, "Expression", None)
        if isinstance(name, str) and name:
            return name, (expression if isinstance(expression, str) else None)
    return None, None


def _model_parameter_name(annotation: Any) -> str | None:  # pragma: no cover - Windows only
    """Just the name, for a caller that has no use for the expression."""
    return _model_parameter(annotation)[0]


def _states_parameter(expression: str | None, wanted: Iterable[str]) -> str | None:
    """Which asked-for parameter a model dimension actually *states*, if any.

    A dimension states a parameter's number only when its expression **is**
    that parameter. `plate_w` states 120; `plate_w - 2 * edge_margin` states 96
    and is not a statement of either name in it -- which is the finding the
    shipped drawing recipe records about `edge_margin`, and the reason this is
    a bare-reference test rather than "references it somewhere".

    The unit suffix is accepted because this project writes it: `Resolver`
    turns a bare number or reference into `<source> * 1 mm` so Inventor keeps
    the dimension, so `plate_w * 1 mm` is the same statement as `plate_w`.
    """
    if not expression:
        return None
    text = expression.strip()
    for name in wanted:
        if _is_bare_reference(text, name):
            return name
    return None


#: `name`, or `name * 1 <unit>` -- what `Resolver` writes for a reference.
_BARE_REFERENCE = re.compile(
    r"""(?xi) \A \(? \s* (?P<name>[A-Za-z_][A-Za-z_0-9]*) \s* \)?
        (?: \s* \* \s* 1 \s* [A-Za-z_]+ )? \s* \Z""")


def _is_bare_reference(expression: str, name: str) -> bool:
    """Whether *expression* is nothing but a reference to *name*."""
    match = _BARE_REFERENCE.match(expression)
    return match is not None and match.group("name").lower() == name.lower()


def _annotations_wanted(named: Sequence[tuple[Any, str | None]],
                        wanted: dict[str, bool],
                        expressions: Sequence[str | None] | None = None
                        ) -> list[tuple[Any, str]]:
    """The offered annotations a drawing asked for, one per parameter.

    Matched on the parameter's **name first and its expression second**. The
    name is a model parameter on a real part -- `d4` -- and the user parameter
    the recipe asked for is what that model parameter's expression is: see
    `_model_parameter`. A name match is still tried first, because a recipe may
    name a parameter that drives a dimension directly.

    First-come per parameter: a parameter that drives both a sketch dimension
    and a feature dimension would otherwise put two copies of one number on the
    sheet, and a draughtsman writes each dimension once. Annotations that name
    no parameter are skipped rather than kept -- keeping them would be the
    "every dimension the model happens to hold" this whole route exists to
    avoid.
    """
    chosen: list[tuple[Any, str]] = []
    taken: set[str] = set()
    held = list(expressions or [None] * len(named))
    for index, (item, name) in enumerate(named):
        if name is None:
            continue
        matched = name if name in wanted else _states_parameter(
            held[index] if index < len(held) else None, wanted)
        if matched is None or matched in taken:
            continue
        taken.add(matched)
        chosen.append((item, matched))
    return chosen


def _as_list(result: Any) -> list[Any]:  # pragma: no cover - Windows only
    """A COM collection or a single object as a plain Python list.

    Taken out of the collection immediately, because the caller deletes some of
    what it is given: removing an item from a live COM collection while
    iterating it is how a loop silently skips half its members.
    """
    if result is None:
        return []
    try:
        count = int(result.Count)
    except Exception:
        return [result]
    return [result.Item(index) for index in range(1, count + 1)]


def _view_extent(view: Any) -> tuple[float, float] | None:  # pragma: no cover
    """What the view spans on the sheet, in cm, if it will say.

    The check on defect 4's drawing-shaped cousin: a view whose direction did
    not mean what its name said has an extent that does not match the part's on
    those axes, and this is the number that shows it.
    """
    try:
        return (float(view.Width), float(view.Height))
    except Exception:
        return None


def _view_position(view: Any) -> tuple[float, float]:  # pragma: no cover
    """Where the view's centre sits on the sheet, in cm."""
    try:
        centre = view.Center
        return (float(centre.X), float(centre.Y))
    except Exception:
        return (0.0, 0.0)


#: Where a base view's camera sits, per direction, as the sign of the axis it
#: looks down from. Measured on Inventor 2027.1, 2026-09-09, by placing one
#: base view per direction of a 120 x 80 x 8 mm block and reading each camera:
#:
#:     front   eye (0, 0, +29)   up (0, +1, 0)
#:     rear    eye (0, 0, -28)   up (0, +1, 0)
#:     top     eye (0, +29, 0)   up (0, 0, -1)
#:     bottom  eye (0, -29, 0)   up (0, 0, +1)
#:     left    eye (-29, 0, 0)   up (0, +1, 0)
#:     right   eye (+29, 0, 0)   up (0, +1, 0)
#:
#: Which is Inventor being **consistently Y-up**: every view but the top and
#: bottom pair has +Y up the screen, and those two -- looking down and up the
#: Y axis, where Y cannot be up -- put -Z and +Z there instead. That is the
#: whole of "`top` renders Z inverted", the observation defect 4 recorded and
#: could not explain, and it is a convention rather than a fault.
#:
#: This exists because **`DrawingView.ViewOrientationType` is not readable on
#: 2027.1** -- measured the same run, on all seven views, base and projected
#: alike. The camera is, and it says strictly more: an orientation enum names
#: a view and a camera says where it looks from and which way is up. So a
#: view's direction is derived from it.
_VIEW_EYE: dict[str, tuple[int, int, int]] = {
    "front": (0, 0, 1), "rear": (0, 0, -1),
    "top": (0, 1, 0), "bottom": (0, -1, 0),
    "left": (-1, 0, 0), "right": (1, 0, 0),
}


def _direction_from_camera(camera: dict[str, Any]) -> str | None:
    """Which direction a view looks from, out of its camera. None if it cannot say.

    The eye against the target, reduced to the axis it lies along. A diagonal
    eye -- all three components of a size -- is an isometric and says so; a
    camera missing either point says nothing, which is not the same as saying
    `front`.

    The up vector is not needed to name the direction and is reported beside
    it, because it answers the other question: which way is up inside the
    plane the name settles.
    """
    eye, target = camera.get("eye"), camera.get("target")
    if not eye or not target or len(eye) != 3 or len(target) != 3:
        return None
    away = [round(float(e) - float(t), 6) for e, t in zip(eye, target)]
    size = max(abs(value) for value in away)
    if size <= 0:
        return None
    # A tenth of the longest component: an axis view's other two components
    # are the target's own offset from the origin, not zero, and an isometric's
    # three are all within a factor of two of each other.
    minor = [abs(value) for value in away if abs(value) < size / 10]
    if len(minor) != 2:
        return "iso" if len(minor) == 0 else None
    for name, axis in _VIEW_EYE.items():
        if all((value > size / 10) == (component > 0)
               and (value < -size / 10) == (component < 0)
               for value, component in zip(away, axis)):
            return name
    return None


def _view_camera(view: Any) -> dict[str, Any]:  # pragma: no cover - Windows only
    """A view's camera as plain numbers: where it looks from, at, and which way is up.

    The reading the orientation table needs and an extent cannot give. An
    extent settles which *plane* a view shows -- measured 2026-09-08: Inventor's
    `front` shows XY and this project's `front` means XZ -- and says nothing
    about which way is up inside it, because a view rotated or mirrored spans
    the same. Eye, target and up vector settle it completely.

    Empty where the view will not say, which is not a failure: it is one more
    reason the remap is not written from a guess.
    """
    out: dict[str, Any] = {}
    try:
        camera = view.Camera
    except Exception:
        return out
    for label, attribute in (("eye", "Eye"), ("target", "Target"),
                             ("up", "UpVector")):
        try:
            point = getattr(camera, attribute)
            out[label] = [round(float(point.X), 6), round(float(point.Y), 6),
                          round(float(point.Z), 6)]
        except Exception:
            continue
    return out


def _view_orientation_name(view: Any, orientations: dict[str, str],
                           resolve: Callable[[str], int]) -> str:  # pragma: no cover
    """Which direction this view is, asked of the view.

    Asked rather than remembered, because a sheet read back has to be able to
    disagree with what was requested or reading it back proves nothing. Falls
    back to naming the raw enum rather than to the request: a direction this
    cannot read is not evidence that the request was honoured.

    The enum values come through `resolve` -- the backend's `_k` -- and not from
    the fallback table, because these are exactly the names the table does not
    carry. So the comparison works on a machine whose type library is readable
    and reports the raw number where it is not, which is the honest answer.
    """
    try:
        value = int(view.ViewOrientationType)
    except Exception:
        return "unknown"
    for direction, enum in orientations.items():
        try:
            if value == resolve(enum):
                return direction
        except Exception:
            continue
    return f"orientation {value}"


#: How a drawing dimension's type might announce itself. Read as a string
#: because the enum names are what distinguish a diameter from a length, and a
#: number would have to be mapped through a table nobody here has measured.
_DIMENSION_KIND_HINTS = (
    ("diameter", "diameter"), ("radius", "radius"), ("angular", "angle"),
    ("angle", "angle"), ("linear", "linear"),
)


def _dimension_kind(entry: Any) -> str:  # pragma: no cover
    """Whether this is a length, a diameter, a radius or an angle.

    From the object's own type name, which pywin32 exposes for a specialised
    object and which reads `DiameterGeneralDimension` or `LinearGeneralDimension`
    -- so the answer is in the name rather than in an enum whose numbering would
    have to be measured. Defaults to linear, which is what most dimensions are
    and what a wrong answer costs least on.
    """
    described = f"{type(entry).__name__} {getattr(entry, 'Type', '')}".lower()
    for hint, kind in _DIMENSION_KIND_HINTS:
        if hint in described:
            return kind
    return "linear"


def _dimension_expression(entry: Any) -> str | None:  # pragma: no cover
    """The expression behind the dimension, where it has one to give."""
    for path in (("ModelDimension", "Parameter", "Expression"),
                 ("Parameter", "Expression"), ("Text", "Text")):
        current: Any = entry
        for step in path:
            current = getattr(current, step, None)
            if current is None:
                break
        if isinstance(current, str) and current:
            return current
    return None


def _dimension_view_name(entry: Any) -> str | None:  # pragma: no cover
    """Which view the dimension is attached to, if it will say."""
    for path in (("Parent", "Name"), ("DrawingView", "Name")):
        current: Any = entry
        for step in path:
            current = getattr(current, step, None)
            if current is None:
                break
        if isinstance(current, str) and current:
            return current
    return None


def _is_reference(entry: Any) -> bool:  # pragma: no cover
    """Whether the dimension is shown as a reference -- bracketed, driving nothing."""
    try:
        return bool(entry.IsReferenceDimension)
    except Exception:
        return False

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


def _update(document: Any) -> bool | None:  # pragma: no cover - Windows only
    """Recompute what is out of date, and say whether Inventor reported an error.

    `Document.Update2([AcceptErrorsAndContinue])` is documented to return False
    when any entity failed to compute, where `Update` returns nothing at all --
    so a feature that could not be built was, until now, only ever found by the
    volume it failed to move. The result is logged rather than raised: every
    caller of this already measures the geometry, and a warning that names the
    document is the honest addition, not a second failure path nobody has seen
    fire. A release without `Update2` gets `Update`, and `None` for an answer.
    """
    routine = getattr(document, "Update2", None)
    try:
        if routine is None:
            document.Update()
            return None
        outcome = routine(True)
    except Exception:
        return None
    if outcome is False:
        logger.warning(
            "Document.Update2 reported that something in %s failed to compute. "
            "The geometry checks below are what say what; `rebuild` names the "
            "feature and its health status.",
            getattr(document, "DisplayName", "the document"))
    return None if outcome is None else bool(outcome)


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
