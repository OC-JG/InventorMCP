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

from typing import Any

from ...errors import BackendUnavailableError

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
    "kAlignedDim": 34561,
    "kHorizontalDim": 34562,
    "kVerticalDim": 34563,
    # PartFeatureExtentEnum
    "kDistanceExtent": 20737,
    "kThroughAllExtent": 20740,
    "kToNextExtent": 20739,
    # ShellDirectionEnum
    "kInsideShellDirection": 41985,
    "kOutsideShellDirection": 41986,
    "kBothShellDirection": 41987,
    # PatternComputeTypeEnum
    "kIdenticalCompute": 107265,
    "kAdjustToModelCompute": 107266,
    "kOptimizedCompute": 107267,
    # ViewOrientationTypeEnum
    "kIsoTopRightViewOrientation": 10758,
    "kFrontViewOrientation": 10753,
    "kTopViewOrientation": 10755,
    "kRightViewOrientation": 10757,
    "kBackViewOrientation": 10754,
    # DisplayModeEnum / RenderStyle
    "kShadedRendering": 9985,
    "kHiddenLineRendering": 9986,
    "kWireframeRendering": 9987,
    # SelectionFilterEnum (used for view fitting)
    "kPartFaceFilter": 8449,
    "kPartEdgeFilter": 8450,
    # CurveTypeEnum
    "kLineSegmentCurve": 5378,
    "kCircularArcCurve": 5379,
    "kCircleCurve": 5380,
    "kEllipseFullCurve": 5381,
    "kEllipticalArcCurve": 5382,
    "kBSplineCurve": 5383,
    # SurfaceTypeEnum
    "kPlaneSurface": 5890,
    "kCylinderSurface": 5891,
    "kConeSurface": 5892,
    "kSphereSurface": 5893,
    "kTorusSurface": 5894,
    # HoleTypeEnum / HoleBottomTypeEnum
    "kDrilledHole": 39169,
    "kCounterBoreHole": 39170,
    "kSpotFaceHole": 39171,
    "kCounterSinkHole": 39172,
    "kFlatHoleBottom": 39425,
    "kAngleHoleBottom": 39426,
    # WeldBeadReliefShapeEnum placeholder kept out; add values here as needed.
}


class Constants:
    """Looks enum values up in the type library, falling back to :data:`FALLBACK`."""

    def __init__(self, module: Any | None = None) -> None:
        self._module = module
        self._sources: dict[str, str] = {}
        self._cache: dict[str, int] = {}

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
