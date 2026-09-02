"""Which sketch a cut is checked against, and whether it reaches the material.

``rehearse`` warns when a cut's profile lies entirely outside the part -- the
mistake DECISIONS.md records as the most expensive in this project's history,
the angle bracket's slots cutting empty air for three rounds. The check was
sound; what it was pointed at was not.

Three faults, all in the one line that chose the sketch:

* ``getattr(op, "sketch", None) or context.last_sketch`` gave *every*
  operation a sketch, including ones that have none. A shell was reported as
  "sketch 'Unrelated' does not reach the part", naming a sketch it has nothing
  to do with -- a warning that fires on a correct recipe, which teaches the
  reader to skip the field.
* An engraved emboss says it is a cut through ``style``, not ``operation``, so
  the one feature whose entire job is to cut a shape into a face was the one
  cut nothing checked.
* A sweep names ``profile_sketch`` and ``path_sketch`` and neither is
  ``sketch``, so it was checked against whatever happened to be declared last.

Underneath all three, ``plan_bounds`` skipped text primitives outright, so a
sketch holding nothing but text reported bounds of (0, 0, 0, 0) and every
caller believed the text sat on the origin.
"""

from __future__ import annotations

import pytest

from inventor_mcp.builder import rehearse
from inventor_mcp.geometry import plan_bounds, plan_sketch
from inventor_mcp.resolve import Resolver
from inventor_mcp.schema import PartRecipe, SketchOp

PLATE = [
    {"op": "sketch", "name": "Base", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 40, "height": 40}]},
    {"op": "extrude", "name": "Plate", "sketch": "Base", "distance": 5},
]


def warnings_for(operations):
    report = rehearse(PartRecipe.model_validate(
        {"name": "Part", "units": "mm", "parameters": [], "operations": PLATE + operations}))
    return [warning["warning"] for warning in report.get("warnings") or []]


def missed(operations):
    return [w for w in warnings_for(operations) if "does not reach the part" in w]


def text_sketch(name, position, text="MARK", height=5):
    return {"op": "sketch", "name": name, "plane": "xy", "entities": [
        {"type": "text", "position": position, "text": text, "height": height}]}


class TestTextHasBounds:
    def test_a_text_sketch_is_not_at_the_origin_just_because_it_is_text(self):
        """(0, 0, 0, 0) was the answer for every text sketch there has ever been."""
        plan = plan_sketch(SketchOp.model_validate(
            text_sketch("T", [20, 50])), Resolver("mm", "deg"))
        low_u, low_v, high_u, high_v = plan_bounds(plan)
        assert low_u < 2.0 < high_u, "the run should straddle its anchor's x"
        assert low_v < 5.0 <= high_v, "the anchor is the top, so the box hangs below"

    def test_the_box_hangs_below_the_anchor_as_measured(self):
        """A run anchored at 12.6 with height 8 reached down to 2.11 in Inventor."""
        plan = plan_sketch(SketchOp.model_validate(
            text_sketch("T", [0, 12.6], height=8)), Resolver("mm", "deg"))
        _, low_v, _, high_v = plan_bounds(plan)
        assert high_v == pytest.approx(1.26)          # the anchor, exactly, in cm
        assert low_v == pytest.approx(1.26 - 1.31 * 0.8, abs=1e-9)

    def test_a_longer_run_reaches_further(self):
        def width(text):
            plan = plan_sketch(SketchOp.model_validate(
                text_sketch("T", [0, 0], text=text)), Resolver("mm", "deg"))
            low_u, _, high_u, _ = plan_bounds(plan)
            return high_u - low_u

        assert width("OnlyCatEnclosure") > width("A")


class TestAnEngraveThatMissesThePart:
    def test_it_is_reported(self):
        assert missed([text_sketch("T", [0, 500]),
                       {"op": "emboss", "sketch": "T", "depth": 0.5, "style": "engrave"}])

    def test_one_on_the_part_is_not(self):
        assert not missed([text_sketch("T", [0, 0]),
                           {"op": "emboss", "sketch": "T", "depth": 0.5, "style": "engrave"}])

    def test_a_raised_emboss_is_not_a_cut_so_it_is_left_alone(self):
        """It adds material. Missing the part is a different complaint."""
        assert not missed([text_sketch("T", [0, 500]),
                           {"op": "emboss", "sketch": "T", "depth": 0.5, "style": "raise"}])


class TestOperationsWithNoSketchOfTheirOwn:
    def test_a_shell_is_not_blamed_for_the_last_sketch_declared(self):
        """The false alarm: a shell has no profile to miss with."""
        assert not missed([
            {"op": "sketch", "name": "Unrelated", "plane": "xy", "entities": [
                {"type": "circle", "center": [0, 500], "diameter": 6}]},
            {"op": "shell", "faces": {"kind": "face", "filter": "top"}, "thickness": 2}])

    def test_nor_is_a_chamfer(self):
        assert not missed([
            {"op": "sketch", "name": "Unrelated", "plane": "xy", "entities": [
                {"type": "circle", "center": [0, 500], "diameter": 6}]},
            {"op": "chamfer", "edges": {"filter": "vertical"}, "distance": 1}])


class TestASweepIsCheckedOnBothOfItsSketches:
    PATH_HOME = {"op": "sketch", "name": "Q", "plane": "xy", "entities": [
        {"type": "line", "start": [-30, 0], "end": [30, 0]}]}
    PATH_AWAY = {"op": "sketch", "name": "Q", "plane": "xy", "entities": [
        {"type": "line", "start": [0, 500], "end": [70, 500]}]}

    def profile(self, at):
        return {"op": "sketch", "name": "P", "plane": "yz", "entities": [
            {"type": "circle", "center": at, "diameter": 6}]}

    def test_a_stray_profile_is_caught(self):
        """It was not: the check only ever looked at whichever came last."""
        assert missed([self.profile([0, 500]), self.PATH_HOME,
                       {"op": "sweep", "profile_sketch": "P", "path_sketch": "Q",
                        "operation": "cut"}]) == ["sketch 'P' does not reach the part"]

    def test_a_stray_path_is_caught(self):
        assert missed([self.profile([0, 0]), self.PATH_AWAY,
                       {"op": "sweep", "profile_sketch": "P", "path_sketch": "Q",
                        "operation": "cut"}]) == ["sketch 'Q' does not reach the part"]


class TestTheControlsThatAlreadyWorked:
    @pytest.mark.parametrize("operations", [
        [{"op": "sketch", "name": "F", "plane": "xy", "entities": [
            {"type": "circle", "center": [0, 500], "diameter": 6}]},
         {"op": "extrude", "sketch": "F", "distance": 20, "operation": "cut"}],
        [{"op": "sketch", "name": "H", "plane": "xy", "entities": [
            {"type": "point", "position": [0, 500]}]},
         {"op": "hole", "sketch": "H", "diameter": 6, "through_all": True}],
    ])
    def test_an_extrude_cut_and_a_hole_still_report_a_miss(self, operations):
        assert missed(operations)

    def test_and_a_cut_that_lands_on_the_part_still_says_nothing(self):
        assert not missed([
            {"op": "sketch", "name": "On", "plane": "xy", "entities": [
                {"type": "circle", "center": [0, 0], "diameter": 6}]},
            {"op": "extrude", "sketch": "On", "distance": 20, "operation": "cut"}])
