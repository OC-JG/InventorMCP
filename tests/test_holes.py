"""The hole-method dispatch, checked without Inventor.

Inventor's hole methods take their arguments in an order that does not read the
way the dialog does, and a wrong order can still build -- producing a plain hole
that gets reported as a counterbore. These tests pin the order and the names
against a recorder, so the only thing left to discover on a live machine is
whether Inventor agrees, which :func:`holes.verify` then asks it directly.
"""

from __future__ import annotations

import math

import pytest

from inventor_mcp.backend.base import Driven, HoleRequest
from inventor_mcp.backend.com import holes
from inventor_mcp.schema import HoleOp

PLACEMENT = object()
EXTENT = 20989  # a stand-in for kPositiveExtentDirection


class Recorder:
    """Stands in for ``HoleFeatures``, remembering how it was called."""

    def __init__(self, accepts_keywords: bool = True, feature: object | None = None):
        self.accepts_keywords = accepts_keywords
        self.calls: list[tuple[str, tuple, dict]] = []
        self.feature = feature if feature is not None else Feature()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            if kwargs and not self.accepts_keywords:
                raise TypeError(f"{name}() takes no keyword arguments")
            self.calls.append((name, args, kwargs))
            return self.feature

        return method


class Feature:
    """A built hole, as much of one as ``verify`` looks at."""

    def __init__(self, hole_type: int | None = None, tapped: bool = False):
        if hole_type is not None:
            self.HoleType = hole_type
        self.Tapped = tapped


#: The numbers the constants table gives, so verify() can be exercised with the
#: same resolver the backend passes it.
ENUMS = {
    "kDrilledHole": 39169,
    "kCounterBoreHole": 39170,
    "kSpotFaceHole": 39171,
    "kCounterSinkHole": 39172,
}


def resolve(name: str) -> int:
    return ENUMS[name]


def request(**overrides) -> HoleRequest:
    fields = dict(
        sketch="centres",
        diameter=Driven("bolt_d", 0.55),
        through_all=True,
    )
    fields.update(overrides)
    return HoleRequest(**fields)


class TestTheMethodChosen:
    def test_every_style_and_extent_has_a_method(self):
        """A style added to the schema must not silently fall back to drilled."""
        styles = HoleOp.model_fields["style"].annotation.__args__
        for style in styles:
            for through in (True, False):
                assert (style, through) in holes.METHOD, f"{style}, through={through}"
            assert style in holes.EXTRAS
            assert style in holes.STYLE_ENUM

    @pytest.mark.parametrize(
        "style,through,method",
        [
            ("drilled", True, "AddDrilledByThroughAllExtent"),
            ("drilled", False, "AddDrilledByDistanceExtent"),
            ("counterbore", True, "AddCBoreByThroughAllExtent"),
            ("counterbore", False, "AddCBoreByDistanceExtent"),
            ("spotface", True, "AddSpotFaceByThroughAllExtent"),
            ("countersink", False, "AddCSinkByDistanceExtent"),
        ],
    )
    def test_the_name_matches_the_style(self, style, through, method):
        extras = {}
        if style in ("counterbore", "spotface"):
            extras = {"cbore_diameter": Driven("cbore_d", 1.0),
                      "cbore_depth": Driven("cbore_deep", 0.55)}
        if style == "countersink":
            extras = {"csink_diameter": Driven("csink_d", 1.2),
                      "csink_angle": Driven("90 deg", math.pi / 2)}
        call = holes.plan_call(
            request(style=style, through_all=through,
                    depth=None if through else Driven("plate_t", 0.8), **extras),
            PLACEMENT, EXTENT, "bolt_d", None,
        )
        assert call.method == method


class TestTheArgumentOrder:
    def test_a_through_hole_takes_no_depth(self):
        call = holes.plan_call(request(), PLACEMENT, EXTENT, "bolt_d", None)
        assert call.positional == (PLACEMENT, "bolt_d", EXTENT)
        assert "Depth" not in call.keywords

    def test_a_blind_hole_takes_a_depth_before_the_direction(self):
        call = holes.plan_call(
            request(through_all=False, depth=Driven("deep", 0.5)),
            PLACEMENT, EXTENT, "bolt_d", None,
        )
        assert call.positional == (PLACEMENT, "bolt_d", "deep", EXTENT)

    def test_the_counterbore_dimensions_come_after_the_direction(self):
        """The part that reads wrong: the enum is in the middle, not at the end."""
        call = holes.plan_call(
            request(
                through_all=False,
                depth=Driven("plate_t", 0.8),
                style="counterbore",
                cbore_diameter=Driven("cbore_d", 1.0),
                cbore_depth=Driven("cbore_deep", 0.55),
            ),
            PLACEMENT, EXTENT, "bolt_d", None,
        )
        assert call.method == "AddCBoreByDistanceExtent"
        assert call.positional == (
            PLACEMENT, "bolt_d", "plate_t", EXTENT, "cbore_d", "cbore_deep",
        )
        assert list(call.keywords) == [
            "PlacementDefinition", "DiameterOrTapInfo", "Depth", "ExtentDirection",
            "CBoreDiameter", "CBoreDepth",
        ]

    def test_the_keywords_and_the_positional_form_say_the_same_thing(self):
        """Named and positional are two spellings of one call, not two calls."""
        call = holes.plan_call(
            request(style="countersink", csink_diameter=Driven("csink_d", 1.2),
                    csink_angle=Driven("90 deg", math.pi / 2)),
            PLACEMENT, EXTENT, "bolt_d", None,
        )
        assert tuple(call.keywords.values()) == call.positional

    def test_a_spotface_uses_the_counterbore_fields(self):
        call = holes.plan_call(
            request(style="spotface", cbore_diameter=Driven("pad_d", 1.6),
                    cbore_depth=Driven("pad_deep", 0.1)),
            PLACEMENT, EXTENT, "bolt_d", None,
        )
        assert call.keywords["SpotFaceDiameter"] == "pad_d"
        assert call.keywords["SpotFaceDepth"] == "pad_deep"

    def test_the_tip_angle_is_last_and_only_when_asked_for(self):
        blind = dict(through_all=False, depth=Driven("deep", 0.5))
        flat = holes.plan_call(request(**blind), PLACEMENT, EXTENT, "bolt_d", None)
        assert "BottomTipAngle" not in flat.keywords
        pointed = holes.plan_call(request(**blind), PLACEMENT, EXTENT, "bolt_d", "118 deg")
        assert pointed.positional[-1] == "118 deg"

    def test_a_through_hole_never_takes_a_tip_angle(self):
        """There is no bottom to point: the argument does not exist there."""
        call = holes.plan_call(request(), PLACEMENT, EXTENT, "bolt_d", "118 deg")
        assert "BottomTipAngle" not in call.keywords

    def test_a_blind_hole_without_a_depth_is_refused(self):
        with pytest.raises(ValueError, match="depth"):
            holes.plan_call(request(through_all=False), PLACEMENT, EXTENT, "bolt_d", None)

    def test_a_counterbore_without_its_dimensions_is_refused(self):
        with pytest.raises(ValueError, match="cbore_diameter"):
            holes.plan_call(request(style="counterbore"), PLACEMENT, EXTENT, "bolt_d", None)


class TestCalling:
    def test_names_are_used_when_the_binding_allows_it(self):
        features = Recorder(accepts_keywords=True)
        call = holes.plan_call(request(), PLACEMENT, EXTENT, "bolt_d", None)
        holes.invoke(features, call)
        name, args, kwargs = features.calls[0]
        assert name == "AddDrilledByThroughAllExtent"
        assert not args and kwargs == call.keywords

    def test_late_binding_falls_back_to_position(self):
        features = Recorder(accepts_keywords=False)
        call = holes.plan_call(request(), PLACEMENT, EXTENT, "bolt_d", None)
        holes.invoke(features, call)
        _, args, kwargs = features.calls[0]
        assert args == call.positional and not kwargs

    def test_a_missing_method_says_so_rather_than_guessing(self):
        class Old:
            def AddDrilledByThroughAllExtent(self, *args, **kwargs):
                return Feature()

        call = holes.plan_call(
            request(style="counterbore", cbore_diameter=Driven("d", 1.0),
                    cbore_depth=Driven("z", 0.5)),
            PLACEMENT, EXTENT, "bolt_d", None,
        )
        with pytest.raises(AttributeError, match="AddCBoreByThroughAllExtent"):
            holes.invoke(Old(), call)

    def test_the_call_describes_itself_for_an_error_message(self):
        call = holes.plan_call(request(), PLACEMENT, EXTENT, "bolt_d", None)
        described = call.describe()
        assert described.startswith("AddDrilledByThroughAllExtent(")
        assert "DiameterOrTapInfo='bolt_d'" in described


class TestTheTap:
    def test_the_thread_table_is_derived_from_the_designation(self):
        assert holes.thread_type_for("M6x1") == "ANSI Metric M Profile"
        assert holes.thread_type_for("1/4-20 UNC") == "ANSI Unified Screw Threads"
        assert holes.thread_type_for("NPT 1/8") == "NPT"
        assert holes.thread_type_for("G1/4") == "BSP"
        assert holes.thread_class_for("M6x1") == "6H"
        assert holes.thread_class_for("1/4-20 UNC") == "2B"

    def test_a_metric_tap_is_set_up_from_the_metric_table(self):
        features = Recorder()
        holes.tap_info(features, request(tap="M6x1"))
        name, _, kwargs = features.calls[0]
        assert name == "CreateTapInfo"
        assert kwargs["ThreadType"] == "ANSI Metric M Profile"
        assert kwargs["ThreadDesignation"] == "M6x1"
        assert kwargs["Class"] == "6H"
        assert kwargs["RightHanded"] is True
        assert kwargs["FullThreadDepth"] is True

    def test_the_recipe_can_override_the_guess(self):
        features = Recorder()
        holes.tap_info(features, request(
            tap="M6x1", tap_type="ISO Metric profile", tap_class="6G",
            tap_right_handed=False, tap_full_depth=False))
        _, _, kwargs = features.calls[0]
        assert kwargs["ThreadType"] == "ISO Metric profile"
        assert kwargs["Class"] == "6G"
        assert kwargs["RightHanded"] is False
        assert kwargs["FullThreadDepth"] is False

    def test_late_binding_gets_the_same_arguments_by_position(self):
        features = Recorder(accepts_keywords=False)
        holes.tap_info(features, request(tap="M6x1"))
        _, args, _ = features.calls[0]
        assert args == (True, "ANSI Metric M Profile", "M6x1", "6H", True)


class TestVerifying:
    def test_the_right_kind_of_hole_is_accepted(self):
        agreed, why = holes.verify(
            Feature(hole_type=ENUMS["kCounterBoreHole"]),
            request(style="counterbore", cbore_diameter=Driven("d", 1.0),
                    cbore_depth=Driven("z", 0.5)),
            resolve,
        )
        assert agreed is True and why == ""

    def test_a_plain_hole_reported_as_a_counterbore_is_caught(self):
        """The failure this exists for: the call built something, not the thing."""
        agreed, why = holes.verify(
            Feature(hole_type=ENUMS["kDrilledHole"]),
            request(style="counterbore", cbore_diameter=Driven("d", 1.0),
                    cbore_depth=Driven("z", 0.5)),
            resolve,
        )
        assert agreed is False
        assert "counterbore" in why and "kDrilledHole" in why

    def test_a_plain_hole_is_not_refused_over_an_unfamiliar_enum(self):
        """The holes that work today must not break to guard a claim nobody made.

        A plain drilled hole asserts nothing beyond removing material, which is
        checked separately. What value Inventor reports for one has never been
        measured here, so a mismatch is a note.
        """
        agreed, why = holes.verify(Feature(hole_type=39177), request(), resolve)
        assert agreed is True
        assert "note about the enum and not about the part" in why

    def test_a_counterbore_is_still_refused_over_the_same_enum(self):
        """Because there the readback is the only evidence the seat exists."""
        agreed, _ = holes.verify(
            Feature(hole_type=39177),
            request(style="counterbore", cbore_diameter=Driven("d", 1.0),
                    cbore_depth=Driven("z", 0.5)),
            resolve,
        )
        assert agreed is False

    def test_an_unreadable_feature_is_not_evidence_either_way(self):
        agreed, why = holes.verify(Feature(), request(), resolve)
        assert agreed is None
        assert "did not report" in why

    def test_an_untapped_tapped_hole_is_caught(self):
        agreed, why = holes.verify(
            Feature(hole_type=ENUMS["kDrilledHole"], tapped=False),
            request(tap="M6x1"), resolve,
        )
        assert agreed is False and "tapped" in why

    def test_a_tapped_hole_that_is_tapped_passes(self):
        agreed, _ = holes.verify(
            Feature(hole_type=ENUMS["kDrilledHole"], tapped=True),
            request(tap="M6x1"), resolve,
        )
        assert agreed is True

    def test_a_confirmed_tap_survives_an_unfamiliar_hole_type(self):
        """Nothing here has measured what type a tapped hole reports.

        The thread is confirmed and no seat was asked for, so refusing on the
        enum alone would reject a hole that is right.
        """
        agreed, why = holes.verify(
            Feature(hole_type=39177, tapped=True), request(tap="M6x1"), resolve)
        assert agreed is True
        assert "39177" in why and "tap itself is confirmed" in why

    def test_a_confirmed_tap_does_not_excuse_a_missing_counterbore(self):
        """The seat is a separate claim, and the tap says nothing about it."""
        agreed, why = holes.verify(
            Feature(hole_type=ENUMS["kDrilledHole"], tapped=True),
            request(tap="M6x1", style="counterbore",
                    cbore_diameter=Driven("d", 1.0), cbore_depth=Driven("z", 0.5)),
            resolve,
        )
        assert agreed is False and "kDrilledHole" in why

    def test_an_unreadable_tap_is_not_read_as_a_success(self):
        class NoTapProperty:
            HoleType = ENUMS["kDrilledHole"]

            def __getattr__(self, name):
                raise AttributeError(name)

        agreed, why = holes.verify(NoTapProperty(), request(tap="M6x1"), resolve)
        assert agreed is None and "whether the hole is tapped" in why

    def test_an_unknown_enum_name_does_not_become_a_false_alarm(self):
        def unresolvable(name: str) -> int:
            raise KeyError(name)

        agreed, why = holes.verify(
            Feature(hole_type=ENUMS["kDrilledHole"]), request(), unresolvable)
        assert agreed is None and "unknown" in why


class TestTheSimulatorsVolume:
    """The mock's arithmetic, which the drawing check and rehearsals rely on."""

    def test_a_counterbore_removes_an_annulus_not_a_cylinder(self):
        from inventor_mcp.backend.mock.backend import _style_volume

        extra = _style_volume(
            request(style="counterbore", cbore_diameter=Driven("d", 1.0),
                    cbore_depth=Driven("z", 0.55)),
            0.3,
        )
        assert extra == pytest.approx(math.pi * (0.5**2 - 0.3**2) * 0.55)

    def test_a_countersink_removes_a_cone(self):
        from inventor_mcp.backend.mock.backend import _style_volume

        extra = _style_volume(
            request(style="countersink", csink_diameter=Driven("d", 1.2),
                    csink_angle=Driven("90 deg", math.pi / 2)),
            0.3,
        )
        # 90 degrees included, so the cone's depth equals (R - r) = 0.3 cm, and
        # the frustum runs from r = 0.3 up to R = 0.6.
        frustum = math.pi * 0.3 / 3 * (0.36 + 0.18 + 0.09)
        assert extra == pytest.approx(frustum - math.pi * 0.09 * 0.3)

    def test_a_countersink_narrower_than_its_bore_removes_nothing_extra(self):
        from inventor_mcp.backend.mock.backend import _style_volume

        assert _style_volume(
            request(style="countersink", csink_diameter=Driven("d", 0.4),
                    csink_angle=Driven("90 deg", math.pi / 2)),
            0.3,
        ) == 0.0

    def test_a_plain_hole_has_no_extra(self):
        from inventor_mcp.backend.mock.backend import _style_volume

        assert _style_volume(request(), 0.3) == 0.0
