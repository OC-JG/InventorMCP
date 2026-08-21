"""Choosing and calling the right ``HoleFeatures.Add*`` method.

Inventor does not have one hole method with options; it has sixteen, named for
the combination of style and extent, and their argument orders do not read the
way the dialog does.  ``AddCBoreByDistanceExtent`` puts the *extent direction*
in the middle and the counterbore's own dimensions after it, which means a
plausible-looking positional call lands a diameter where an enum belongs.

That is why the dispatch lives here rather than inline in the backend: it is a
pure function of a request and a ``features`` object, so the argument order can
be tested offline against a recorder instead of discovered on a live machine.

Two things follow from getting this wrong quietly:

* a wrong argument order can still *build* -- Inventor coerces what it can --
  and the result is a plain hole reported as a counterbore;
* a hole consumes its sketch, so the feature cannot be deleted and rebuilt
  another way.

So the caller reads the style back off the finished feature and refuses to
report a counterbore it cannot see.  :func:`verify` is that check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

#: Which ``HoleTypeEnum`` value each style should read back as.  The names are
#: resolved through the constants table rather than hard-coded, because the
#: numbers differ between releases and a wrong enum here would turn the
#: verification into a false alarm.
STYLE_ENUM = {
    "drilled": "kDrilledHole",
    "counterbore": "kCounterBoreHole",
    "spotface": "kSpotFaceHole",
    "countersink": "kCounterSinkHole",
}

#: Method name per (style, through_all).
METHOD = {
    ("drilled", False): "AddDrilledByDistanceExtent",
    ("drilled", True): "AddDrilledByThroughAllExtent",
    ("counterbore", False): "AddCBoreByDistanceExtent",
    ("counterbore", True): "AddCBoreByThroughAllExtent",
    ("spotface", False): "AddSpotFaceByDistanceExtent",
    ("spotface", True): "AddSpotFaceByThroughAllExtent",
    ("countersink", False): "AddCSinkByDistanceExtent",
    ("countersink", True): "AddCSinkByThroughAllExtent",
}

#: The style-specific trailing arguments, in the order Inventor takes them and
#: with the names it gives them.  Every one of these families is
#: ``(placement, diameter-or-tap, [depth,] extent_direction, *these)`` -- the
#: extent direction comes *before* the style's own dimensions, which is the
#: part that reads wrong.
EXTRAS: dict[str, tuple[tuple[str, str], ...]] = {
    "drilled": (),
    "counterbore": (("CBoreDiameter", "cbore_diameter"), ("CBoreDepth", "cbore_depth")),
    "spotface": (("SpotFaceDiameter", "cbore_diameter"), ("SpotFaceDepth", "cbore_depth")),
    "countersink": (("CSinkDiameter", "csink_diameter"), ("CSinkAngle", "csink_angle")),
}


def thread_type_for(designation: str) -> str:
    """Which thread table a designation like 'M6x1' or '1/4-20' comes from.

    The names are the sheet names in Inventor's own ``Thread.xls``. Measured on
    2027.1 by ``scripts/probe_hole_styles.py``:

    * ``("ANSI Metric M Profile", "M8x1.25", "6H")`` -- accepted
    * ``("ISO Metric profile", "M8x1.25", "6H")`` -- also accepted
    * ``("ANSI Unified Screw Threads", "1/4-20 UNC", "2B")`` -- accepted
    * ``("ANSI Metric M Profile", "M8", "6H")`` -- **refused**: the designation
      must carry its pitch, so "M8" alone is not a thread Inventor knows
    * ``NPT``/``BSP`` with ``"1/8"`` and ``"G1/4"`` -- **refused**, so the
      designation format for those tables is still unknown here

    Getting one wrong produces an error from Inventor rather than a silently
    untapped hole, which is why a guess is acceptable and the recipe can
    override it with ``tap_type`` and ``tap_class``.
    """
    text = designation.strip().upper()
    if text.startswith("NPT") or text.endswith("NPT"):
        return "NPT"
    if text.startswith("BSP") or (text.startswith("G") and text[1:2].isdigit()):
        return "BSP"
    if text.startswith("M") and text[1:2].isdigit():
        return "ANSI Metric M Profile"
    return "ANSI Unified Screw Threads"


def thread_class_for(designation: str) -> str:
    """A sensible internal-thread class, since a tap has to have one."""
    return "6H" if thread_type_for(designation) == "ANSI Metric M Profile" else "2B"


@dataclass
class HoleCall:
    """What was called, so a failure can name it and a success can report it."""

    method: str
    keywords: dict[str, Any]
    positional: tuple[Any, ...]

    def describe(self) -> str:
        shown = ", ".join(
            f"{name}={_short(value)}" for name, value in self.keywords.items()
        )
        return f"{self.method}({shown})"


def _short(value: Any) -> str:
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, (int, float)):
        return f"{value}"
    return type(value).__name__


def plan_call(request: Any, placement: Any, extent: int, diameter_or_tap: Any,
              bottom_angle: str | None) -> HoleCall:
    """Work out the method and arguments for *request*.

    *diameter_or_tap* is either the diameter expression or a ``HoleTapInfo``:
    Inventor takes both through the same argument, and a tapped hole gets its
    drill size from the thread table rather than from the recipe.
    """
    style = request.style
    if style not in EXTRAS:
        raise ValueError(f"Unknown hole style {style!r}.")
    through = bool(request.through_all)
    method = METHOD[(style, through)]

    keywords: dict[str, Any] = {
        "PlacementDefinition": placement,
        "DiameterOrTapInfo": diameter_or_tap,
    }
    positional: list[Any] = [placement, diameter_or_tap]
    if not through:
        if request.depth is None:
            raise ValueError("A blind hole needs a depth.")
        keywords["Depth"] = request.depth.expression
        positional.append(request.depth.expression)
    keywords["ExtentDirection"] = extent
    positional.append(extent)

    for name, field in EXTRAS[style]:
        driven = getattr(request, field, None)
        if driven is None:
            raise ValueError(
                f"Style {style!r} needs {field}; the schema should have caught this."
            )
        keywords[name] = driven.expression
        positional.append(driven.expression)

    # A pointed bottom takes *two* arguments, and the first is a boolean.
    # `AddDrilledByDistanceExtent(..., FlatBottom, BottomTipAngle)`: passing the
    # angle alone put the string "118 deg" into FlatBottom, where it coerced to
    # True and produced the flat bottom it was meant to replace. The hole built,
    # reported success and was the wrong shape -- caught by the volume, which is
    # 0.0226 cm^3 more than a 118 degree tip leaves.
    #
    # Both are omitted when no angle is asked for, so a recipe that says nothing
    # gets Inventor's own default, which is flat.
    if not through and bottom_angle is not None:
        keywords["FlatBottom"] = False
        positional.append(False)
        keywords["BottomTipAngle"] = bottom_angle
        positional.append(bottom_angle)

    return HoleCall(method=method, keywords=keywords, positional=tuple(positional))


def invoke(features: Any, call: HoleCall) -> Any:
    """Make the call, by name where the binding allows it.

    Named arguments are tried first because they make the order irrelevant.
    Late-bound dispatch does not accept them, so the positional form -- built in
    the same order in :func:`plan_call` -- stands behind it.
    """
    method = getattr(features, call.method, None)
    if method is None:
        raise AttributeError(
            f"This Inventor does not expose HoleFeatures.{call.method}."
        )
    try:
        return method(**call.keywords)
    except TypeError:
        return method(*call.positional)


def tap_info(features: Any, request: Any) -> Any:
    """A ``HoleTapInfo`` for the request's thread, or raise saying why not.

    The measured signature is
    ``CreateTapInfo(RightHanded, ThreadType, ThreadDesignation, Class,
    FullTapDepth, [ThreadDepth])`` -- handedness first, and the depth flag
    called ``FullTapDepth`` rather than ``FullThreadDepth``. The first version
    of this had the two booleans at opposite ends, which worked only because
    both happened to be True.
    """
    designation = request.tap
    thread_type = request.tap_type or thread_type_for(designation)
    thread_class = request.tap_class or thread_class_for(designation)
    keywords = {
        "RightHanded": bool(request.tap_right_handed),
        "ThreadType": thread_type,
        "ThreadDesignation": designation,
        "Class": thread_class,
        "FullTapDepth": bool(request.tap_full_depth),
    }
    try:
        return features.CreateTapInfo(**keywords)
    except TypeError:
        return features.CreateTapInfo(*keywords.values())


def verify(feature: Any, request: Any, resolve: Callable[[str], int]) -> tuple[bool | None, str]:
    """Read the built feature back and say whether it is what was asked for.

    Returns ``(True, note)`` when Inventor agrees, ``(False, note)`` when it
    reports a different kind of hole, and ``(None, note)`` when the properties
    cannot be read at all -- which is not evidence either way and must not be
    reported as success.
    """
    notes: list[str] = []

    # The tap first, because it decides how much weight the type carries.  A
    # thread is a plain yes or no; what ``HoleTypeEnum`` value a *tapped* hole
    # reports has never been measured here.
    tapped = None
    if request.tap:
        tapped = _property(feature, "Tapped")
        if tapped is None:
            notes.append("Inventor did not report whether the hole is tapped")
            return None, "; ".join(notes)
        if not bool(tapped):
            return False, "asked for a tapped hole and Inventor built an untapped one"

    wanted = None
    try:
        wanted = resolve(STYLE_ENUM[request.style])
    except Exception:
        notes.append("the HoleTypeEnum value for this style is unknown here")

    actual = _property(feature, "HoleType")
    if actual is None or wanted is None:
        notes.append("Inventor did not report the hole's type")
        return None, "; ".join(notes)

    if int(actual) != int(wanted):
        if request.style == "drilled" and not request.tap:
            # Nothing extra was claimed, and a plain hole that removed material
            # has already proved itself. What value Inventor reports for one has
            # never been measured here, so refusing on it would break the holes
            # that work today to guard a claim nobody made.
            names = {value: name for name, value in
                     ((name, _safe(resolve, name)) for name in STYLE_ENUM.values())}
            got = names.get(int(actual), str(int(actual)))
            notes.append(
                f"Inventor reports hole type {got} for a plain drilled hole; the "
                "hole removed material, so this is a note about the enum and not "
                "about the part"
            )
            return True, "; ".join(notes)
        if tapped and request.style == "drilled":
            # The thread is confirmed and no seat was asked for, so there is
            # nothing left to be wrong -- a tapped hole simply reports a type
            # this project has not seen before.  Saying so beats refusing a
            # hole that is right.
            notes.append(
                f"Inventor reports hole type {int(actual)} for a tapped hole "
                f"rather than {STYLE_ENUM['drilled']} = {int(wanted)}; the tap "
                "itself is confirmed"
            )
            return True, "; ".join(notes)
        names = {value: name for name, value in
                 ((name, _safe(resolve, name)) for name in STYLE_ENUM.values())}
        got = names.get(int(actual), str(int(actual)))
        return False, (f"asked for a {request.style} hole and Inventor built "
                       f"{got} instead")
    return True, "; ".join(notes)


def _property(feature: Any, name: str) -> Any:
    """One property, from the feature or from its definition.

    A hole's parameters live on ``HoleFeature.Definition``, not on the feature:
    ``feature.HoleType`` simply does not exist. So the first version of this read
    nothing, returned "cannot tell" for every hole, and the run that reported
    eight verified styles had verified none of them -- the failure text said
    "the style read back correctly" because that string was written next to a
    check that never ran.
    """
    for holder in (feature, getattr(feature, "Definition", None)):
        if holder is None:
            continue
        try:
            value = getattr(holder, name)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _safe(resolve: Callable[[str], int], name: str) -> int | None:
    try:
        return resolve(name)
    except Exception:
        return None
