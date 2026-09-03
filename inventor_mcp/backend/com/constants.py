"""Inventor API enum values.

Inventor's COM enums are exposed through its type library.  When ``pywin32``
can generate the early-bound wrapper (``gencache.EnsureDispatch``) we read the
values straight from there, which is always correct for the installed version.

Some machines cannot generate the cache -- a read-only ``gen_py`` directory, a
locked-down profile, or a version mismatch after an Inventor upgrade.  For
those we fall back to the table below.  The table is a convenience, not the
source of truth: :func:`resolve` prefers the type library every time, and
:func:`describe` reports which source a value came from so a mismatch is
diagnosable rather than mysterious.
"""

from __future__ import annotations

import logging
from typing import Any

from ...errors import BackendUnavailableError

logger = logging.getLogger(__name__)

#: Best-effort fallback values, used only when the type library is unreadable.
FALLBACK: dict[str, int] = {
    # DocumentTypeEnum
    "kPartDocumentObject": 12290,
    "kAssemblyDocumentObject": 12291,
    "kDrawingDocumentObject": 12292,
    "kPresentationDocumentObject": 12293,
    # PartFeatureOperationEnum
    "kJoinOperation": 20481,
    "kCutOperation": 20482,
    "kIntersectOperation": 20483,
    "kSurfaceOperation": 20484,
    "kNewBodyOperation": 20485,
    # PartFeatureExtentDirectionEnum
    "kPositiveExtentDirection": 20993,
    "kNegativeExtentDirection": 20994,
    "kSymmetricExtentDirection": 20995,
    # DimensionOrientationEnum
    "kAlignedDim": 19203,
    "kHorizontalDim": 19201,
    "kVerticalDim": 19202,
    # PartFeatureExtentEnum
    "kDistanceExtent": 20737,
    "kThroughAllExtent": 20743,
    "kToNextExtent": 20740,
    # ShellDirectionEnum
    "kInsideShellDirection": 41217,
    "kOutsideShellDirection": 41218,
    "kBothShellDirection": 41987,
    # PatternComputeTypeEnum
    "kIdenticalCompute": 47361,
    "kAdjustToModelCompute": 47362,
    "kOptimizedCompute": 47363,
    # ViewOrientationTypeEnum
    "kIsoTopRightViewOrientation": 10759,
    "kFrontViewOrientation": 10764,
    "kTopViewOrientation": 10754,
    "kRightViewOrientation": 10755,
    "kBackViewOrientation": 10756,
    # DisplayModeEnum / RenderStyle
    "kShadedRendering": 8708,
    "kHiddenLineRendering": 9986,
    "kWireframeRendering": 8706,
    # SelectionFilterEnum (used for view fitting)
    "kPartFaceFilter": 15877,
    "kPartEdgeFilter": 15873,
    # CurveTypeEnum
    "kLineSegmentCurve": 5123,
    "kCircularArcCurve": 5125,
    "kCircleCurve": 5124,
    "kEllipseFullCurve": 5126,
    "kEllipticalArcCurve": 5127,
    "kBSplineCurve": 5128,
    # SurfaceTypeEnum
    "kPlaneSurface": 5890,
    "kCylinderSurface": 5891,
    "kConeSurface": 5893,
    "kSphereSurface": 5896,
    "kTorusSurface": 5895,
    # HoleTypeEnum / HoleBottomTypeEnum
    "kDrilledHole": 21505,
    "kCounterBoreHole": 21507,
    "kSpotFaceHole": 21508,
    "kCounterSinkHole": 21506,
    "kFlatHoleBottom": 39425,
    "kAngleHoleBottom": 39426,
    # ConstraintStatusEnum -- read back from a sketch, not passed to Inventor,
    # but a wrong number here would silently call every sketch under-constrained.
    "kFullyConstrainedConstraintStatus": 51713,
    "kUnderConstrainedConstraintStatus": 51714,
    # WeldBeadReliefShapeEnum placeholder kept out; add values here as needed.
}

#: Fallback values that were once disputed, and are now measured.  Every entry
#: in :data:`FALLBACK` was checked against Inventor 2027.1's own type library on
#: 2026-08-21 by ``scripts/dump_constants.py``, and thirty-two of the fifty-one
#: were wrong -- most of them not slightly wrong but from a different numbering
#: family altogether: ``kDrilledHole`` is 21505, not 39169.
#:
#: One of those was the quiet kind of dangerous. The table's
#: ``kThroughAllExtent`` (20740) is Inventor's real ``kToNextExtent``, so a
#: through-all extrude would have become a to-next one -- building a part that
#: is wrong in a way no error reports. It never fired only because the type
#: library has been readable on every machine this has run on.
#:
#: The disputes recorded here before are settled: NeonGlay's field notes were
#: right about the dimension-orientation values (19201/19202/19203) and about
#: ``kTorusSurface``, and the sphere is 5896 rather than either guess.
#:
#: Re-run ``scripts/dump_constants.py`` on any release that is not 2027.1: these
#: numbers are a measurement of one version, not a fact about the API.
#:
#: Which is what Inventor 2026.1 then said. Forty-seven of the fifty-one agree
#: with it exactly; the four below are not in its type library under these names
#: at all, so on 2026 there is nothing to read and the table is what would be
#: used -- and the table has never been checked for them anywhere.
#:
#: Three of the four give themselves away by their numbering. Shell directions
#: run 41217 and 41218, so a third member of that enum is not 41987. Rendering
#: styles run 8706 and 8708, so hidden line is not 9986. A value out of its own
#: family is the signature of the mistake that put ``kThroughAllExtent`` on
#: Inventor's ``kToNextExtent`` -- an extrude that stopped at the next face
#: while every report said "through all", wrong in a way nothing raises.
#:
#: So they refuse. ``kFlatHoleBottom`` and ``kAngleHoleBottom`` are not reached
#: by any code path today -- the hole calls take a ``FlatBottom`` boolean and a
#: ``BottomTipAngle`` instead -- and refusing costs nothing. The other two are
#: reachable: a shell with ``direction: "both"``, and ``capture_view`` in
#: hidden-line mode. On a release whose type library has the names, none of this
#: fires; ``resolve`` prefers the type library and never consults the table.
SUSPECT: dict[str, str] = {
    "kBothShellDirection": (
        "Inventor 2026.1's type library has no such name, and 41987 is outside "
        "the 41217/41218 family the other two shell directions belong to"
    ),
    "kHiddenLineRendering": (
        "Inventor 2026.1's type library has no such name, and 9986 is outside "
        "the 8706/8708 family the other two render styles belong to"
    ),
    "kFlatHoleBottom": (
        "Inventor 2026.1's type library has no such name; nothing reads this "
        "entry today, so it has never been exercised anywhere"
    ),
    "kAngleHoleBottom": (
        "Inventor 2026.1's type library has no such name; nothing reads this "
        "entry today, so it has never been exercised anywhere"
    ),
}


class Constants:
    """Looks enum values up in the type library, falling back to :data:`FALLBACK`."""

    def __init__(self, module: Any | None = None) -> None:
        self._module = module
        self._sources: dict[str, str] = {}
        self._cache: dict[str, int] = {}
        self._warned: set[str] = set()

    def resolve(self, name: str) -> int:
        if name in self._cache:
            return self._cache[name]

        value: int | None = None
        source = "fallback"
        if self._module is not None:
            value = getattr(self._module, name, None)
            if isinstance(value, int):
                source = "typelib"
            else:
                value = None
        if value is None:
            value = FALLBACK.get(name)
        if value is None:
            raise BackendUnavailableError(
                f"Inventor enum {name!r} is unknown.",
                hint="Add it to inventor_mcp/backend/com/constants.py, or repair the "
                "pywin32 type-library cache so values can be read from Inventor itself.",
            )
        if source == "fallback":
            if name in SUSPECT:
                raise BackendUnavailableError(
                    f"The fallback value for {name!r} is disputed: this table says "
                    f"{value}, and {SUSPECT[name]}. It has never been verified here, "
                    "because the type library has always been readable.",
                    hint="Repair the pywin32 type-library cache so the value comes "
                    "from Inventor itself: delete %LOCALAPPDATA%\\Temp\\gen_py and "
                    "re-run. Or run scripts/dump_constants.py on a machine where the "
                    "cache works and correct the table from what it prints.",
                )
            if name not in self._warned:
                self._warned.add(name)
                logger.warning(
                    "Using the unverified fallback value %d for %s: the type library "
                    "could not be read, so this is a table entry rather than a "
                    "measurement. Run scripts/dump_constants.py to check it.",
                    value, name)
        self._cache[name] = value
        self._sources[name] = source
        return value

    __getitem__ = resolve

    def describe(self) -> dict[str, Any]:
        return {
            "typelib_available": self._module is not None,
            "resolved": {name: {"value": value, "source": self._sources.get(name, "fallback")}
                         for name, value in sorted(self._cache.items())},
        }


def load(app: Any | None = None) -> Constants:
    """Build a :class:`Constants` bound to the generated type-library module."""
    module = None
    try:  # pragma: no cover - Windows only
        import win32com.client  # type: ignore[import-not-found]

        candidate = win32com.client.constants
        # ``constants`` is populated lazily; touching a known name proves it works.
        if getattr(candidate, "kPartDocumentObject", None) is not None:
            module = candidate
    except Exception:  # pragma: no cover - any failure means "use the fallback"
        module = None
    return Constants(module)


#: Recipe operation name -> Inventor enum name.
BOOLEAN_OPERATIONS = {
    "join": "kJoinOperation",
    "cut": "kCutOperation",
    "intersect": "kIntersectOperation",
    "new_body": "kNewBodyOperation",
    "surface": "kSurfaceOperation",
}

EXTENT_DIRECTIONS = {
    "positive": "kPositiveExtentDirection",
    "negative": "kNegativeExtentDirection",
    "symmetric": "kSymmetricExtentDirection",
}

SHELL_DIRECTIONS = {
    "inside": "kInsideShellDirection",
    "outside": "kOutsideShellDirection",
    "both": "kBothShellDirection",
}

TEXT_ALIGNMENT = {
    "left": "kAlignTextLeft",
    "center": "kAlignTextCenter",
    "right": "kAlignTextRight",
}

VIEW_ORIENTATIONS = {
    "iso": "kIsoTopRightViewOrientation",
    "front": "kFrontViewOrientation",
    "top": "kTopViewOrientation",
    "right": "kRightViewOrientation",
    "back": "kBackViewOrientation",
}

DISPLAY_MODES = {
    "shaded": "kShadedRendering",
    "hidden_line": "kHiddenLineRendering",
    "wireframe": "kWireframeRendering",
}
