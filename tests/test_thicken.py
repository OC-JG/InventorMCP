"""`thicken`, whose uncertainty is about a side rather than a number.

The arithmetic is the easy half and is exact: a planar face of area A grown by a
layer `t` along its own normal adds a prism of `A*t`, and thickening a set of
them is the sum. What is not arithmetic is `THICKEN_SHARE` -- which side of a
face a `negative` layer lies on, and therefore which pairs of direction and
operation do anything at all. That table is derived from set algebra (the layer
is a slab, the operation is a boolean, a face's normal points out of the solid)
and is silent on whether Inventor agrees. `--only thicken` is what settles it;
these tests hold the half that can be held here.

The two figures below are the calibration fixtures' own: 1.4400 cm^3 for four
walls grown 1 mm and -0.2400 for one wall thinned 1 mm. They have to be the same
numbers on both sides of that comparison.
"""

from __future__ import annotations

import pytest

from inventor_mcp.backend.base import THICKEN_SHARE
from inventor_mcp.builder import build_part, rehearse
from inventor_mcp.schema import PartRecipe

#: 80 x 40 x 6 mm: 19.2 cm^3. Its top face is 32 cm^2 and its four walls are
#: 240 mm of perimeter 6 mm tall, so 14.4 cm^2.
PLATE = [
    {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 80, "height": 40}]},
    {"op": "extrude", "name": "Plate", "sketch": "Outline", "distance": 6},
]

TOP = {"kind": "face", "filter": "top"}
WALLS = {"kind": "face", "filter": "vertical"}
RIGHT_WALL = {"kind": "face", "filter": "vertical", "near": [40, 0, 3], "limit": 1}


def build(session, ops, *, parameters=None, **kwargs):
    recipe = PartRecipe.model_validate({
        "name": "Thicken", "units": "mm",
        "parameters": parameters or [],
        "operations": PLATE + ops,
    })
    return build_part(session, recipe, **kwargs)


def layer(faces=None, **fields):
    return {"op": "thicken", "faces": faces or TOP, "thickness": 1, **fields}


def volume(out):
    return out["operations"][-1]["measured"]["volume_cm3"]


def detail(out):
    return out["operations"][-1]["detail"]


class TestEachFaceGrowsAlongItsOwnNormal:
    """The property that makes this a different operation from `move_face`."""

    def test_four_walls_grow_outward_in_one_operation(self, session):
        """14.4 cm^2 of wall by a 1 mm layer, plus the four corners it leaves.

        A single named direction cannot express this: the four walls point four
        ways, so moving them all along X would push two out and two in.

        The corner term is measured rather than assumed. Inventor 2027.1 came
        back at **1.4640 cm^3** on 2026-09-07 where the sum of the four layers
        is 1.4400 -- it closes the 1 x 1 x 6 mm notch where two layers meet, and
        4 x 6 mm^3 is the 0.024 difference exactly.
        """
        out = build(session, [layer(WALLS)])
        assert volume(out) == pytest.approx(19.2 + 1.44 + 0.024, abs=5e-6)
        assert detail(out)["area_cm2"] == pytest.approx(14.4, abs=5e-6)
        assert detail(out)["corner_edges"] == 4
        assert detail(out)["corner_cm3"] == pytest.approx(0.024, abs=5e-9)

    def test_one_planar_face_agrees_with_move_face_to_the_digit(self, session):
        """Two independently written operations, one answer. A cross-check.

        On a single planar face the two coincide by construction -- thicken's
        layer along the face's normal is exactly the prism move_face's dot
        product measures -- so a disagreement here means one of them broke.
        """
        thickened = build(session, [layer(TOP, thickness=2)])
        moved = build(session, [{"op": "move_face", "faces": TOP,
                                 "direction": "z", "distance": 2}])
        assert volume(thickened) == pytest.approx(volume(moved), abs=5e-9)
        assert volume(thickened) == pytest.approx(19.2 + 6.4, abs=5e-6)


class TestTheDirectionAndOperationTable:
    """Every pair in `THICKEN_SHARE`, against the geometry it claims."""

    #: (direction, operation, the volume change on the plate's 32 cm^2 top face
    #: with a 1 mm layer). Written out rather than computed from the table, so
    #: that a change to the table has to be agreed with here. One face, so no
    #: corner term: these figures are unaffected by it, which is also why
    #: `thinned_wall` measured exactly against Inventor before the corners were
    #: understood at all.
    CASES = [
        ("positive", "join", +3.2),
        ("positive", "cut", 0.0),
        ("negative", "join", 0.0),
        ("negative", "cut", -3.2),
        ("symmetric", "join", +1.6),
        ("symmetric", "cut", -1.6),
    ]

    @pytest.mark.parametrize("direction,operation,expected", CASES)
    def test_the_pair_moves_what_the_table_says(self, session, direction,
                                                operation, expected):
        out = build(session, [layer(TOP, direction=direction, operation=operation)])
        assert volume(out) == pytest.approx(19.2 + expected, abs=5e-6)

    def test_the_cases_here_cover_the_table_exactly(self):
        """Otherwise a pair could be added and never checked."""
        assert {(d, o) for d, o, _ in self.CASES} == set(THICKEN_SHARE)

    def test_a_wall_thinned_from_behind_is_the_fixtures_figure(self, session):
        """40 x 6 x 1 mm off one wall is -0.24 cm^3 -- `thinned_wall`'s."""
        out = build(session, [layer(RIGHT_WALL, direction="negative", operation="cut")])
        assert volume(out) == pytest.approx(19.2 - 0.24, abs=5e-6)


class TestTheTwoPairsThatCancel:
    """A feature that builds and changes nothing is the commonest wrong recipe.

    Warned about rather than refused, and the reason is which half is certain:
    the set algebra says these do nothing, and whether Inventor agrees about
    which side `negative` is has never been measured. A refusal would prevent
    the run that settles it.
    """

    def recipe(self, direction, operation):
        return PartRecipe.model_validate({
            "name": "Cancels", "units": "mm",
            "operations": PLATE + [layer(TOP, direction=direction,
                                         operation=operation)]})

    @pytest.mark.parametrize("direction,operation", [("positive", "cut"),
                                                     ("negative", "join")])
    def test_the_step_says_so_in_its_own_detail(self, session, direction, operation):
        out = build(session, [layer(TOP, direction=direction, operation=operation)])
        assert out["ok"] is True, "it builds: Inventor would build it too"
        assert "changes_nothing" in detail(out)

    def test_the_rehearsal_turns_that_into_a_warning(self):
        report = rehearse(self.recipe("negative", "join"))
        assert report["ok"] is True
        warnings = [w["warning"] for w in report["warnings"]]
        assert any("changes nothing" in w for w in warnings), warnings

    def test_the_warning_names_the_pairs_that_do_work(self):
        why = " ".join(w["why"] for w in rehearse(self.recipe("negative", "join"))["warnings"])
        assert "positive" in why and "join" in why and "negative" in why

    @pytest.mark.parametrize("direction,operation", [("positive", "join"),
                                                     ("negative", "cut"),
                                                     ("symmetric", "join")])
    def test_a_pair_that_does_something_is_not_warned_about(self, direction, operation):
        """A warning that fires on a correct recipe teaches the reader to ignore it."""
        report = rehearse(self.recipe(direction, operation))
        assert not [w for w in report["warnings"] if "changes nothing" in w["warning"]]


class TestWhatItWillNotAnswerForExactly:
    """A curved face is charged to first order and the step says so.

    Charged rather than declined, which is the opposite of what `move_face`
    does with the same face, and the difference is real: there the move's
    direction is arbitrary relative to the face, so the dot product has no
    answer at all, while here the direction *is* the face's own normal and
    `area * t` is the correct first term. What is missing is second order in
    the thickness.
    """

    BORE = [
        {"op": "sketch", "name": "Holes", "plane": "xy", "entities": [
            {"type": "point", "name": "p1", "position": [0, 0]}]},
        {"op": "hole", "name": "Bore", "sketch": "Holes", "points": ["p1"],
         "diameter": 10, "through_all": True},
    ]
    LINER = {"op": "thicken", "name": "Liner",
             "faces": {"kind": "face", "filter": "cylindrical"},
             "thickness": 1, "direction": "positive", "operation": "join"}

    def recipe(self):
        return PartRecipe.model_validate({
            "name": "Bored", "units": "mm",
            "operations": PLATE + self.BORE + [self.LINER]})

    def test_the_first_order_term_is_what_is_charged(self, session):
        """pi x 10 x 6 mm of bore wall is 1.885 cm^2, so a 1 mm liner is 0.1885."""
        import math

        out = build_part(session, self.recipe())
        expected = math.pi * 1.0 * 0.6 * 0.1
        assert detail(out)["area_cm2"] == pytest.approx(math.pi * 1.0 * 0.6, abs=5e-6)
        assert out["operations"][-1]["measured"]["volume_change_cm3"] == pytest.approx(
            expected, abs=5e-6)

    def test_it_declares_itself_an_estimate(self, session):
        out = build_part(session, self.recipe())
        assert detail(out)["estimated"] is True
        assert detail(out)["curved_faces"]
        assert "square of the thickness" in detail(out)["volume_from"]

    def test_the_rehearsal_declines_to_compare_it(self):
        assert rehearse(self.recipe())["steps"][-1]["predictable"] is False

    def test_a_planar_layer_is_not_marked_estimated(self, session):
        """Otherwise the flag is on everything and means nothing."""
        out = build(session, [layer(TOP)])
        assert "estimated" not in detail(out)


class TestItRefusesRatherThanDoingNothingQuietly:
    def test_a_selector_matching_no_face_is_refused(self, session):
        out = build(session, [layer({"kind": "face", "feature": "Nope"})],
                    stop_on_error=True)
        assert out["ok"] is False
        assert "nothing to thicken" in out["errors"][0]["error"]

    def test_a_part_with_no_solid_yet_is_refused(self, session):
        recipe = PartRecipe.model_validate({
            "name": "Empty", "units": "mm", "operations": [
                {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": 20, "height": 20}]},
                layer(TOP),
            ]})
        out = build_part(session, recipe, stop_on_error=True)
        assert out["ok"] is False
        assert "no solid body yet" in out["errors"][0]["error"]

    def test_a_negative_thickness_is_refused_rather_than_flipping_the_layer(self, session):
        """`direction` is the one way to say which side."""
        out = build(session, [layer(TOP, thickness=-1)], stop_on_error=True)
        assert out["ok"] is False
        assert "greater than zero" in out["errors"][0]["error"]

    def test_the_schema_refuses_the_operations_that_have_no_meaning_here(self):
        """`intersect` on a layer outside the solid leaves nothing at all."""
        import pydantic

        for operation in ("intersect", "new_body", "surface"):
            with pytest.raises(pydantic.ValidationError):
                PartRecipe.model_validate({
                    "name": "X", "units": "mm",
                    "operations": PLATE + [layer(TOP, operation=operation)]})


class TestTheThicknessIsAnExpression:
    def each(self, session, value):
        return build(session, [layer(WALLS, thickness="wall")],
                     parameters=[{"name": "wall", "value": value}])

    def test_the_volume_follows_the_parameter(self, session):
        """And the corner term follows it too, as the square that it is: the
        layer doubles and the four corners quadruple, 0.024 to 0.096."""
        assert volume(self.each(session, 1)) == pytest.approx(20.664, abs=5e-6)
        assert volume(self.each(session, 2)) == pytest.approx(22.176, abs=5e-6)
        assert detail(self.each(session, 2))["corner_cm3"] == pytest.approx(
            0.096, abs=5e-9)

    def test_the_expression_reaches_the_report_and_not_just_its_value(self, session):
        assert detail(self.each(session, 1))["thickness"]["expression"] == "wall"


class TestTheLimitsItsPonytailNames:
    def test_a_later_cut_is_charged_the_thickness_before_the_layer(self, session):
        """The ledger does not follow a thicken, and the comment says so.

        Pinned so that fixing it fails here and sends whoever did it to the
        `ponytail:` that claims it is broken.
        """
        out = build(session, [
            layer(TOP, thickness=20),
            {"op": "sketch", "name": "Slot", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 10, "height": 10}]},
            {"op": "extrude", "name": "Cut", "sketch": "Slot", "distance": 40,
             "operation": "cut", "extent": "through_all"},
        ])
        cut = out["operations"][-1]["measured"]["volume_change_cm3"]
        # 1 cm^2 of profile through the 0.6 cm of prism the ledger knows about,
        # not the 2.6 cm the part is thick after a 20 mm layer.
        assert cut == pytest.approx(-0.6, abs=5e-6)
