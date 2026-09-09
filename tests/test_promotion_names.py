"""A promotion asked for in a recipe's words has to reach Inventor's property.

`promote_parameter` takes a property name from a caller, and a caller says what
a recipe says. The simulator matches that against its own feature detail, which
is keyed by the recipe's own field names, so it accepts `taper`. Inventor's
`ExtrudeDefinition` calls the same thing `TaperAngle`, and the COM backend used
to match names by normalised equality alone -- so the promotion the simulator
performed happily failed live with *"The feature 'Block' has no drivable
property 'taper'"*, measured on Inventor 2027.1 on 2026-09-08 by
`scripts/live_acceptance.py --only promotion`.

Two self-consistent halves disagreeing about a word is how defect 5 survived
three runs. So the words live in `_PROMOTION_ALIASES` as data and this file is
the test that they still name something the backend can drive.

Everything here is a pure function over name tables: it needs no Inventor and
no simulator, which is the point -- the divergence it guards was invisible to
both.
"""

from __future__ import annotations

import pytest

from inventor_mcp.backend.com import backend as com

DRIVING = com.ComBackend._DRIVING


class TestARecipesWordReachesInventorsProperty:
    """The words a recipe uses for a dimensioned feature property, each against
    the name Inventor answers to. A word missing from here is a promotion that
    works in the simulator and fails on the seat."""

    @pytest.mark.parametrize("word, inventor", [
        # The one that was measured failing.
        ("taper", "TaperAngle"),
        # A hole's own sizes. `hole.diameter` is Inventor's `HoleDiameter`, and
        # the seat and style sizes are spelled out rather than abbreviated.
        ("diameter", "HoleDiameter"),
        ("cbore_diameter", "CounterboreDiameter"),
        ("cbore_depth", "CounterboreDepth"),
        ("csink_diameter", "CountersinkDiameter"),
        ("csink_angle", "CountersinkAngle"),
        ("bottom_angle", "BottomTipAngle"),
        # The words that already agree, pinned so a rename of either side is a
        # failure here rather than a surprise on a CAD machine.
        ("thickness", "Thickness"),
        ("radius", "Radius"),
        ("distance", "Distance"),
        ("depth", "Depth"),
        ("angle", "Angle"),
        ("count", "Count"),
        ("x_count", "XCount"),
        ("y_spacing", "YSpacing"),
    ])
    def test_the_word_names_a_property_this_backend_drives(self, word, inventor):
        named, inferred = com._promotion_candidates(word, DRIVING)
        assert inventor in named, (
            f"{word!r} has to reach {inventor!r} outright, not by inference: "
            f"named={named}, inferred={inferred}"
        )

    def test_inventors_own_spelling_always_works_too(self):
        """Discovery reports the property by the name it read off the feature,
        so `promote_parameters` is normally handed `TaperAngle` rather than
        `taper`. Both have to work: the plan comes from discovery and the
        acceptance check is written by hand."""
        for name in DRIVING:
            named, _ = com._promotion_candidates(name, DRIVING)
            assert name in named, name

    def test_every_alias_names_something_the_backend_can_drive(self):
        """An alias pointing at a property `_DRIVING` does not list would
        resolve to a name nothing ever looks for -- a silent no-op."""
        from inventor_mcp.backend.base import PROMOTION_ALIASES

        for word, spelling in PROMOTION_ALIASES.items():
            assert spelling in DRIVING, (
                f"{word!r} aliases {spelling!r}, which is not in _DRIVING, so "
                "nothing would ever read it"
            )


class TestInferenceIsSecondAndNeverSilent:
    """A name that merely *starts with* the request is a guess. It earns its
    place -- it is how a release spelling a property slightly differently still
    resolves -- but it cannot outrank the request itself and it cannot pick
    between two answers."""

    def test_an_exact_name_outranks_a_prefix(self):
        """`count` starts `CounterboreDepth`, and a pattern's count is not a
        counterbore. So `Count` is named and the rest are inferred."""
        named, inferred = com._promotion_candidates("count", DRIVING)
        assert named == ["Count"]
        assert "CounterboreDepth" in inferred

    def test_a_prefix_reaching_two_answers_stays_a_question(self):
        """`counterbore` is a diameter and a depth. Promoting the wrong one
        names a value that drives something else, which then reads as the part
        changing by itself the next time that parameter is set."""
        named, inferred = com._promotion_candidates("counterbore", DRIVING)
        assert named == []
        assert set(inferred) == {"CounterboreDiameter", "CounterboreDepth"}

    def test_case_and_underscores_do_not_matter(self):
        """And the alias reads both ways: `taper` and `TaperAngle` are one
        request, so the same names come back whichever was asked for."""
        for spelling in ("TaperAngle", "taperangle", "taper_angle", "TAPER_ANGLE",
                         "taper", " Taper "):
            named, _ = com._promotion_candidates(spelling, DRIVING)
            assert named == ["TaperAngle", "Taper"], spelling

    def test_a_word_that_names_nothing_resolves_to_nothing(self):
        """Not to the first plausible thing. The refusal that follows lists
        what the feature does carry, which is the answer to the real
        question."""
        assert com._promotion_candidates("nonsense", DRIVING) == ([], [])
        assert com._promotion_candidates("", DRIVING) == ([], [])
        assert com._promotion_candidates("   ", DRIVING) == ([], [])


class TestBothBackendsAcceptBothWords:
    """The simulator holds the recipe's words and Inventor holds its own, so a
    promotion has to be askable either way on either backend. This is the half
    that can be run here; `scripts/live_acceptance.py --only promotion` is the
    other, and it is the run that found the divergence."""

    RECIPE = [
        {"op": "sketch", "name": "S", "plane": "xy",
         "entities": [{"type": "rectangle", "center": [0, 0],
                       "width": 60, "height": 40}]},
        {"op": "extrude", "name": "Block", "sketch": "S", "distance": 30,
         "taper": "1.5 deg"},
    ]

    def _part(self, server):
        import asyncio

        def call(tool, /, **arguments):
            return asyncio.run(server.call_tool(tool, arguments)).structured_content

        call("new_part", name="Promotion")
        call("apply_operations", operations=self.RECIPE)
        return call

    @pytest.mark.parametrize("word", ["taper", "TaperAngle", "taper_angle"])
    def test_the_taper_promotes_under_any_of_its_names(self, server, word):
        call = self._part(server)
        out = call("promote_parameters", promotions=[
            {"feature": "Block", "property": word, "name": f"draft_{word.lower()}"}])
        assert out["promoted"], out.get("failed")
        assert out["promoted"][0]["was"] == "1.5 deg"

    def test_a_word_that_names_nothing_is_refused_with_what_is_there(self, server):
        """The refusal has to name the alternatives. "No such property" sends
        the caller back to guess again, which is how this cost a live run."""
        call = self._part(server)
        out = call("promote_parameters", promotions=[
            {"feature": "Block", "property": "wall", "name": "wall_t"}])
        assert out["promoted"] == []
        assert "taper" in out["failed"][0]["error"]


class TestAPromotedValueKeepsItsUnit:
    """A promotion creates a user parameter to hold what a property held, and
    the unit has to come from the property. `set_parameter` defaults to
    millimetres, and the taper's expression is `1.5 deg` -- so the first
    promotion that got past the property name was refused by Inventor for
    handing an angle to a length parameter, with its usual bare "Exception
    occurred". Measured on 2027.1, 2026-09-08."""

    class _Parameter:
        def __init__(self, units):
            self.Units = units

    def test_inventors_own_spelling_comes_back_as_ours(self):
        assert com._parameter_units(self._Parameter("deg")) == "deg"
        assert com._parameter_units(self._Parameter("mm")) == "mm"
        assert com._parameter_units(self._Parameter("in")) == "in"

    def test_a_spelled_out_unit_resolves_too(self):
        """Inventor spells some of them out, and `unit_from_inventor` is what
        knows that -- this is a test that promotion goes through it rather than
        reading `Units` raw."""
        assert com._parameter_units(self._Parameter("degree")) == "deg"

    def test_an_unreadable_unit_falls_back_to_a_length(self):
        """Attempted rather than refused: every promotable property but the
        taper is a length, and `set_parameter` reports what Inventor says
        either way. A refusal here would turn an unreadable unit into no
        promotion at all."""
        class Angry:
            @property
            def Units(self):
                raise RuntimeError("marshalled for a different thread")

        assert com._parameter_units(Angry()) == "mm"
        assert com._parameter_units(self._Parameter("")) == "mm"
        assert com._parameter_units(self._Parameter("furlongs")) == "mm"

    def test_the_promotion_asks_for_the_units_it_read(self):
        import inspect

        source = inspect.getsource(com.ComBackend.promote_parameter)
        assert "units=_parameter_units(target)" in source, \
            "a promoted angle in a millimetre parameter is what Inventor refused"
