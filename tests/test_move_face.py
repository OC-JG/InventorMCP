"""`move_face`, whose whole point is that its answer is derivable.

Every other estimate in the simulator is an estimate: a wedge for a draft,
Pappus for a revolve, glyph coverage for an emboss. This one is not. A planar
face of area A translated by a vector v changes the solid by exactly `A*(v.n)`,
its own normal doing the projecting -- so the expected volumes below are worked
out from the geometry rather than recorded from a run, and a change that breaks
them breaks arithmetic rather than a tolerance.

The two calibration fixtures use the same numbers, deliberately: 6.4 cm^3 for
the lifted cap and 0.24 for the widened wall are what
`scripts/live_acceptance.py --only move-face` will hold Inventor to, and they
have to be the same figures on both sides of that comparison.

What is *not* tested here is the COM half, because it has never executed. See
the `move_face` section of `docs/INVENTOR_SETUP.md`.
"""

from __future__ import annotations

import pytest

from inventor_mcp.builder import build_part, rehearse
from inventor_mcp.schema import PartRecipe

#: 80 x 40 x 6 mm: 19.2 cm^3, and every figure below is derived from it.
PLATE = [
    {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 80, "height": 40}]},
    {"op": "extrude", "name": "Plate", "sketch": "Outline", "distance": 6},
]

TOP = {"kind": "face", "filter": "top"}
#: The wall at x = +40, picked out of the four by position. This is the form the
#: `widened_wall` fixture uses and the one a live selector has to agree with.
RIGHT_WALL = {"kind": "face", "filter": "vertical", "near": [40, 0, 3], "limit": 1}


def build(session, ops, *, parameters=None, **kwargs):
    recipe = PartRecipe.model_validate({
        "name": "MoveFace", "units": "mm",
        "parameters": parameters or [],
        "operations": PLATE + ops,
    })
    return build_part(session, recipe, **kwargs)


def volume(out):
    return out["operations"][-1]["measured"]["volume_cm3"]


def detail(out):
    return out["operations"][-1]["detail"]


class TestTheVolumeIsAreaTimesTheMoveAlongTheNormal:
    """The dot product, checked on each of the three cases it distinguishes."""

    def test_the_top_face_lifted_adds_exactly_its_area_times_the_distance(self, session):
        """32 cm^2 moved 0.2 cm is 6.4 cm^3. This is `lifted_face`'s figure."""
        out = build(session, [{"op": "move_face", "name": "Lift", "faces": TOP,
                               "direction": "z", "distance": 2}])
        assert volume(out) == pytest.approx(19.2 + 6.4, abs=5e-6)

    def test_flipping_it_removes_the_same_amount(self, session):
        out = build(session, [{"op": "move_face", "name": "Drop", "faces": TOP,
                                 "direction": "z", "distance": 2, "flip": True}])
        assert volume(out) == pytest.approx(19.2 - 6.4, abs=5e-6)

    def test_a_face_slid_along_its_own_plane_changes_nothing(self, session):
        """The reading a magnitude cannot give, and the reason this is a dot
        product rather than a rule about which faces count: the top face moved
        2 mm along X is still the same solid, because the move is perpendicular
        to the face's normal."""
        out = build(session, [{"op": "move_face", "name": "Slide", "faces": TOP,
                                 "direction": "x", "distance": 2}])
        assert volume(out) == pytest.approx(19.2, abs=5e-6)
        assert detail(out)["shift_cm"] == [0.2, 0.0, 0.0]

    def test_one_side_wall_pushed_out_adds_its_own_area_times_the_distance(self, session):
        """40 x 6 mm of wall moved 1 mm is 0.24 cm^3. This is `widened_wall`'s."""
        out = build(session, [{"op": "move_face", "name": "Widen",
                                 "faces": RIGHT_WALL, "direction": "x", "distance": 1}])
        assert volume(out) == pytest.approx(19.2 + 0.24, abs=5e-6)

    def test_flipping_a_wall_inward_removes_it_instead(self, session):
        out = build(session, [{"op": "move_face", "name": "Narrow",
                                 "faces": RIGHT_WALL, "direction": "x",
                                 "distance": 1, "flip": True}])
        assert volume(out) == pytest.approx(19.2 - 0.24, abs=5e-6)

    def test_a_flipped_move_reads_as_a_negative_shift_and_not_a_negative_zero(self, session):
        """-0.0 in a report looks like a sign that means something."""
        out = build(session, [{"op": "move_face", "faces": TOP, "direction": "z",
                                 "distance": 2, "flip": True}])
        assert detail(out)["shift_cm"] == [0.0, 0.0, -0.2]


class TestTheDistanceIsAnExpression:
    """The lesson of defect 11: a feature can build and be parametric in name only.

    Nothing here can prove the expression reaches an Inventor dimension -- that
    is what `--only move-face` is for. What it can prove is that the recipe
    layer resolves it per parameter value rather than freezing a number, which
    is the half that would otherwise fail silently.
    """

    def each(self, session, value):
        return build(session,
                     [{"op": "move_face", "name": "Lift", "faces": TOP,
                       "direction": "z", "distance": "lift"}],
                     parameters=[{"name": "lift", "value": value}])

    def test_the_volume_follows_the_parameter(self, session):
        assert volume(self.each(session, 2)) == pytest.approx(25.6, abs=5e-6)
        assert volume(self.each(session, 4)) == pytest.approx(32.0, abs=5e-6)

    def test_the_expression_reaches_the_report_and_not_just_its_value(self, session):
        assert detail(self.each(session, 2))["distance"]["expression"] == "lift"

    def test_a_negative_distance_is_refused_rather_than_reversing_the_move(self, session):
        """`flip` is the one way to say it, so there is one spelling per move."""
        out = build(session, [{"op": "move_face", "faces": TOP,
                               "direction": "z", "distance": -2}],
                    stop_on_error=True)
        assert out["ok"] is False
        assert "greater than zero" in out["errors"][0]["error"]


class TestTheDirectionCanBeAnythingResolveAxisAccepts:
    def test_a_named_work_axis_moves_along_the_axis_it_was_built_with(self, session):
        out = build(session, [
            {"op": "work_axis", "name": "Up", "kind": "normal_to_plane",
             "plane": "xy", "at": [0, 0]},
            {"op": "move_face", "name": "Lift", "faces": TOP,
             "direction": "Up", "distance": 2},
        ])
        assert volume(out) == pytest.approx(25.6, abs=5e-6)

    def test_a_sketch_line_gives_its_own_direction(self, session):
        """A line along X on XY moves a face along model X, so the top face
        sliding along it changes nothing -- which is the same reading as the
        origin-axis case and confirms the line resolved to a direction at all."""
        out = build(session, [
            {"op": "sketch", "name": "Aim", "plane": "xy", "entities": [
                {"type": "line", "name": "Along", "start": [0, 0], "end": [10, 0]}]},
            {"op": "move_face", "name": "Slide", "faces": TOP,
             "direction": "Along", "distance": 2},
        ])
        assert volume(out) == pytest.approx(19.2, abs=5e-6)
        assert detail(out)["shift_cm"] == [0.2, 0.0, 0.0]

    def test_an_unknown_direction_is_refused_rather_than_defaulting(self, session):
        """A move along an axis nobody asked for is the quiet wrong answer."""
        out = build(session, [{"op": "move_face", "faces": TOP,
                               "direction": "Nonesuch", "distance": 2}],
                    stop_on_error=True)
        assert out["ok"] is False
        assert "axis" in out["errors"][0]["error"].lower()


class TestItRefusesRatherThanDoingNothingQuietly:
    def test_a_selector_matching_no_face_is_refused(self, session):
        out = build(session, [{"op": "move_face",
                               "faces": {"kind": "face", "feature": "Nope"},
                               "direction": "z", "distance": 2}],
                    stop_on_error=True)
        assert out["ok"] is False
        assert "nothing to move" in out["errors"][0]["error"]

    def test_a_part_with_no_solid_yet_is_refused(self, session):
        recipe = PartRecipe.model_validate({
            "name": "Empty", "units": "mm", "operations": [
                {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": 20, "height": 20}]},
                {"op": "move_face", "faces": TOP, "direction": "z", "distance": 2},
            ]})
        out = build_part(session, recipe, stop_on_error=True)
        assert out["ok"] is False
        assert "no solid body yet" in out["errors"][0]["error"]


class TestWhatTheSimulatorWillNotAnswerFor:
    """A face with no normal here, and the rehearsal declining to compare it.

    A tolerance loose enough to cover a number nobody has is loose enough to
    cover a fault, so the step says outright that it is an estimate and
    `rehearse` leaves it out -- the same seam a trimmed revolve uses.
    """

    BORE = [
        {"op": "sketch", "name": "Holes", "plane": "xy", "entities": [
            {"type": "point", "name": "p1", "position": [0, 0]}]},
        {"op": "hole", "name": "Bore", "sketch": "Holes", "points": ["p1"],
         "diameter": 10, "through_all": True},
    ]
    SLIDE = {"op": "move_face", "name": "Slide",
             "faces": {"kind": "face", "filter": "cylindrical"},
             "direction": "x", "distance": 1}

    def recipe(self):
        return PartRecipe.model_validate({
            "name": "Bored", "units": "mm",
            "operations": PLATE + self.BORE + [self.SLIDE]})

    def test_a_cylindrical_face_is_declared_an_estimate(self, session):
        out = build_part(session, self.recipe())
        assert out["ok"] is True
        assert detail(out)["estimated"] is True
        assert detail(out)["faces_not_answered_for"], "it should say which faces"
        assert "has no normal" in detail(out)["volume_from"]

    def test_the_rehearsal_declines_to_compare_that_step(self, session):
        report = rehearse(self.recipe())
        assert report["ok"] is True
        assert report["steps"][-1]["predictable"] is False

    def test_a_planar_move_is_not_marked_estimated(self, session):
        """Otherwise the flag is on everything and means nothing."""
        out = build(session, [{"op": "move_face", "faces": TOP,
                                 "direction": "z", "distance": 2}])
        assert "estimated" not in detail(out)
        assert rehearse(PartRecipe.model_validate({
            "name": "P", "units": "mm", "operations": PLATE + [
                {"op": "move_face", "faces": TOP, "direction": "z", "distance": 2}],
        }))["steps"][-1].get("predictable") is not False


class TestTheLimitsTheirPonytailNames:
    """The approximation is recorded in the code; this is what it costs.

    Pinned rather than left to prose, so that fixing it -- the ledger following
    a moved face -- fails here and sends whoever did it to the `ponytail:`
    comment that says it does not.
    """

    def test_a_later_cut_is_charged_the_thickness_before_the_move(self, session):
        """The plate is 8 mm thick after the move and the ledger still says 6.

        So a through-cut takes 6 mm of material rather than 8. Wrong, known, and
        the reason `PREDICTED["move_face"]` is not treated as licence to build a
        part whose later operations pass through a moved face.
        """
        out = build(session, [
            {"op": "move_face", "name": "Lift", "faces": TOP,
             "direction": "z", "distance": 20},
            {"op": "sketch", "name": "Slot", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 10, "height": 10}]},
            {"op": "extrude", "name": "Cut", "sketch": "Slot", "distance": 40,
             "operation": "cut", "extent": "through_all"},
        ])
        cut = out["operations"][-1]["measured"]["volume_change_cm3"]
        # 1 cm^2 of profile through the 0.6 cm the ledger knows about.
        assert cut == pytest.approx(-0.6, abs=5e-6)
        # And not the 2.6 cm the part is actually thick after a 20 mm lift.
        assert cut != pytest.approx(-2.6, abs=1e-3)

    def test_the_moved_faces_own_position_does_follow(self, session):
        """The half that is kept current, because a later selector says `near`."""
        out = build(session, [
            {"op": "move_face", "name": "Lift", "faces": TOP,
             "direction": "z", "distance": 20},
            {"op": "move_face", "name": "Again",
             "faces": {"kind": "face", "near": [0, 0, 26], "within": 1, "limit": 1},
             "direction": "z", "distance": 2},
        ])
        # The cap started at z = 6, so after a 20 mm lift it is at 26 and a
        # selector aimed there finds it. Its area is unchanged, so this second
        # move is another 6.4 cm^3.
        assert volume(out) == pytest.approx(19.2 + 64.0 + 6.4, abs=5e-6)
