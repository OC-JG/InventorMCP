"""`sketch_driven_pattern`, the one pattern that places its occurrences.

The other three count them: `_repeat` charges the seed's volume once per extra
occurrence and records no prism, which is exact while the copies neither
overlap nor run off the part and is what the three shipped examples that pattern
or mirror rely on -- they agree with Inventor to 0.003% on that rule.

This one places them, and the reason is what its input is. A rectangular
pattern's count and spacing already say it did something; this one is handed
positions and nothing else, so a version that counted them could not tell a
correct recipe from one whose points all miss the part. Two consequences are
worth a test each, and they are the two that justify the code:

* the ledger knows where the copies are, so a later cut through one is measured
  against what the pattern left rather than what the seed started with;
* an occurrence of a cutting seed that stands over air is reported.

Placement is exact because a translation is: a prism is an outline in its
plane's coordinates plus a sweep along the normal, so moving one is moving
those. Rotation and reflection are not representable that way except in special
cases, which is why `circular_pattern` and `mirror` still count.
"""

from __future__ import annotations

import math

import pytest

from inventor_mcp.builder import build_part, rehearse
from inventor_mcp.schema import PartRecipe

#: 100 x 60 x 10 mm: 60 cm^3.
PLATE = [
    {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 100, "height": 60}]},
    {"op": "extrude", "name": "Plate", "sketch": "Outline", "distance": 10},
]

#: A 10 x 10 mm pocket 4 mm deep at (-35, -20): 0.4 cm^3 removed.
POCKET = [
    {"op": "sketch", "name": "Pocket", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [-35, -20], "width": 10, "height": 10}]},
    {"op": "extrude", "name": "Slot", "sketch": "Pocket", "distance": 4,
     "operation": "cut"},
]


def spots(*positions, names=None):
    names = names or [f"p{index}" for index in range(len(positions))]
    return {"op": "sketch", "name": "Spots", "plane": "xy", "entities": [
        {"type": "point", "name": name, "position": list(position)}
        for name, position in zip(names, positions)]}


def recipe(*extra):
    return PartRecipe.model_validate({
        "name": "Pattern", "units": "mm", "operations": list(extra)})


def build(session, *extra, **kwargs):
    return build_part(session, recipe(*extra), **kwargs)


def last(out):
    return out["operations"][-1]


class TestTheVolumeFollowsTheSameRuleAsTheOtherPatterns:
    """An occurrence does whatever its seed did -- `_repeat`'s rule, shared."""

    def test_three_more_pockets_remove_three_more_pocketfuls(self, session):
        out = build(session, *PLATE, *POCKET,
                    spots((-35, -20), (0, 0), (35, 20), (-35, 20),
                          names=["home", "a", "b", "c"]),
                    {"op": "sketch_driven_pattern", "name": "Spread",
                     "features": ["Slot"], "sketch": "Spots", "reference": "home"})
        # 60 less four 0.4 cm^3 pockets: the seed's and three occurrences'.
        assert last(out)["measured"]["volume_cm3"] == pytest.approx(58.4, abs=5e-6)
        assert last(out)["detail"]["occurrences"] == 3

    def test_the_seeds_own_point_carries_no_occurrence(self, session):
        """N points describe N of the feature, not N+1."""
        out = build(session, *PLATE, *POCKET,
                    spots((-35, -20), (0, 0), names=["home", "a"]),
                    {"op": "sketch_driven_pattern", "features": ["Slot"],
                     "sketch": "Spots", "reference": "home"})
        assert last(out)["detail"]["occurrences"] == 1
        assert last(out)["measured"]["volume_cm3"] == pytest.approx(59.2, abs=5e-6)

    def test_the_reference_defaults_to_the_first_point(self, session):
        out = build(session, *PLATE, *POCKET,
                    spots((-35, -20), (0, 0), (35, 20)),
                    {"op": "sketch_driven_pattern", "features": ["Slot"],
                     "sketch": "Spots"})
        assert last(out)["detail"]["occurrences"] == 2
        assert last(out)["detail"]["reference_at"] == [-3.5, -2.0]

    def test_a_reference_outside_the_named_subset_still_seeds_nothing(self, session):
        """Compared on coordinates, not on index, so the seed is never copied
        onto itself even when `points` names a subset the reference is not in."""
        out = build(session, *PLATE, *POCKET,
                    spots((-35, -20), (0, 0), (35, 20), names=["home", "a", "b"]),
                    {"op": "sketch_driven_pattern", "features": ["Slot"],
                     "sketch": "Spots", "points": ["a", "b"], "reference": "home"})
        assert last(out)["detail"]["occurrences"] == 2

    def test_patterning_a_join_adds_instead(self, session):
        boss = [
            {"op": "sketch", "name": "Pad", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [-35, -20], "width": 10, "height": 10}]},
            {"op": "extrude", "name": "Pad", "sketch": "Pad", "distance": 5},
        ]
        out = build(session, *PLATE, *boss,
                    spots((-35, -20), (0, 0), names=["home", "a"]),
                    {"op": "sketch_driven_pattern", "features": ["Pad"],
                     "sketch": "Spots", "reference": "home"})
        # 60 + 0.5 for the pad + 0.5 for its one occurrence.
        assert last(out)["measured"]["volume_cm3"] == pytest.approx(61.0, abs=5e-6)


class TestThePlacementIsWhatItIsFor:
    """The two readings a pattern that counted its points could not give."""

    def test_a_later_cut_measures_what_the_pattern_left(self, session):
        """The payoff, as a number.

        A 6 mm hole drilled where an occurrence's 4 mm pocket went meets 6 mm of
        remaining material, not the plate's full 10. Had the ledger not been told
        about the copy it would charge pi*0.3^2*1.0 = 0.2827 cm^3 instead.
        """
        out = build(session, *PLATE, *POCKET,
                    spots((-35, -20), (0, 0), names=["home", "a"]),
                    {"op": "sketch_driven_pattern", "features": ["Slot"],
                     "sketch": "Spots", "reference": "home"},
                    {"op": "sketch", "name": "Drill", "plane": "xy", "entities": [
                        {"type": "point", "name": "p", "position": [0, 0]}]},
                    {"op": "hole", "name": "Bore", "sketch": "Drill",
                     "points": ["p"], "diameter": 6, "through_all": True})
        drilled = last(out)["measured"]["volume_change_cm3"]
        assert drilled == pytest.approx(-math.pi * 0.09 * 0.6, abs=5e-6)
        assert drilled != pytest.approx(-math.pi * 0.09 * 1.0, abs=1e-3)

    def test_the_part_grows_to_cover_the_occurrences_and_no_further(self, session):
        """A pad copied off the end of the plate reaches further than the plate.

        And by the right amount, which is the part of this that was wrong first
        time: the box grows to where the copied *prism* is, not to where the
        whole box would be if it were moved. A 10 mm pad copied 200 mm along a
        100 mm plate puts material at 195..205, so the part spans -50..205 --
        255 mm. Translating the box would have claimed 300.
        """
        pad = [
            {"op": "sketch", "name": "Pad", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 10, "height": 10}]},
            {"op": "extrude", "name": "Pad", "sketch": "Pad", "distance": 5},
        ]
        out = build(session, *PLATE, *pad,
                    spots((0, 0), (200, 0), names=["home", "far"]),
                    {"op": "sketch_driven_pattern", "features": ["Pad"],
                     "sketch": "Spots", "reference": "home"})
        # The bounding box, in cm, which is where the part's reach is reported;
        # the step's own `measured` block carries volume and topology only.
        box = out["mass_properties"]["bounding_box"]
        assert (box[0], box[3]) == pytest.approx((-5.0, 20.5), abs=1e-9)

    def test_an_occurrence_over_air_is_reported(self, session):
        out = build(session, *PLATE, *POCKET,
                    spots((-35, -20), (0, 0), (200, 0), (0, 200),
                          names=["home", "on", "off1", "off2"]),
                    {"op": "sketch_driven_pattern", "features": ["Slot"],
                     "sketch": "Spots", "reference": "home"})
        assert last(out)["detail"]["occurrences_over_nothing"] == [1, 2]

    def test_the_rehearsal_turns_that_into_a_warning(self):
        report = rehearse(recipe(
            *PLATE, *POCKET,
            spots((-35, -20), (200, 0), names=["home", "off"]),
            {"op": "sketch_driven_pattern", "features": ["Slot"],
             "sketch": "Spots", "reference": "home"}))
        assert report["ok"] is True
        assert any("cut nothing" in w["warning"] for w in report["warnings"])

    def test_a_pattern_that_lands_on_the_part_is_not_warned_about(self):
        """A warning that fires on a correct recipe teaches the reader to ignore it."""
        report = rehearse(recipe(
            *PLATE, *POCKET,
            spots((-35, -20), (0, 0), (35, 20), names=["home", "a", "b"]),
            {"op": "sketch_driven_pattern", "features": ["Slot"],
             "sketch": "Spots", "reference": "home"}))
        assert not [w for w in report["warnings"] if "cut nothing" in w["warning"]]

    def test_a_join_standing_clear_is_not_warned_about(self):
        """An island is legitimate; only a cut over air is a mistake."""
        pad = [
            {"op": "sketch", "name": "Pad", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 10, "height": 10}]},
            {"op": "extrude", "name": "Pad", "sketch": "Pad", "distance": 5},
        ]
        report = rehearse(recipe(
            *PLATE, *pad, spots((0, 0), (200, 0), names=["home", "far"]),
            {"op": "sketch_driven_pattern", "features": ["Pad"],
             "sketch": "Spots", "reference": "home"}))
        assert not [w for w in report["warnings"] if "cut nothing" in w["warning"]]


class TestWhatItWillNotClaim:
    """The ledger answers where it can and declines where it cannot."""

    REVOLVED = [
        {"op": "sketch", "name": "Section", "plane": "xz", "entities": [
            {"type": "rectangle", "center": [20, 5], "width": 10, "height": 10}]},
        {"op": "revolve", "name": "Ring", "sketch": "Section", "axis": "z"},
    ]

    def test_a_seed_with_no_prisms_says_placement_was_not_recorded(self, session):
        out = build(session, *self.REVOLVED,
                    spots((0, 0), (60, 0), names=["home", "a"]),
                    {"op": "sketch_driven_pattern", "features": ["Ring"],
                     "sketch": "Spots", "reference": "home"})
        assert last(out)["detail"]["prisms_placed"] == 0
        assert "not recorded" in last(out)["detail"]["placement"]

    def test_it_makes_no_claim_about_misses_on_a_part_it_cannot_model(self, session):
        """A revolve puts no prisms in the ledger, so "no material here" is not
        an answer it has -- and reporting it as one would fire on a correct
        recipe, which is worse than saying nothing."""
        pocket = [
            {"op": "sketch", "name": "Pocket", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [20, 0], "width": 4, "height": 4}]},
            {"op": "extrude", "name": "Slot", "sketch": "Pocket", "distance": 2,
             "operation": "cut"},
        ]
        out = build(session, *self.REVOLVED, *pocket,
                    spots((20, 0), (-20, 0), names=["home", "a"]),
                    {"op": "sketch_driven_pattern", "features": ["Slot"],
                     "sketch": "Spots", "reference": "home"})
        assert "occurrences_over_nothing" not in last(out)["detail"]


class TestItRefusesRatherThanDoingNothingQuietly:
    def test_a_sketch_with_no_points_is_refused(self, session):
        out = build(session, *PLATE, *POCKET,
                    {"op": "sketch", "name": "Spots", "plane": "xy", "entities": [
                        {"type": "rectangle", "center": [0, 0], "width": 5, "height": 5}]},
                    {"op": "sketch_driven_pattern", "features": ["Slot"],
                     "sketch": "Spots"},
                    stop_on_error=True)
        assert out["ok"] is False
        assert "no hole-centre points" in out["errors"][0]["error"]

    def test_an_unknown_reference_point_is_refused(self, session):
        out = build(session, *PLATE, *POCKET, spots((0, 0), (10, 0)),
                    {"op": "sketch_driven_pattern", "features": ["Slot"],
                     "sketch": "Spots", "reference": "nonesuch"},
                    stop_on_error=True)
        assert out["ok"] is False
        assert "no entity named" in out["errors"][0]["error"]

    def test_an_unknown_feature_is_refused(self, session):
        out = build(session, *PLATE, *POCKET, spots((0, 0), (10, 0)),
                    {"op": "sketch_driven_pattern", "features": ["Nope"],
                     "sketch": "Spots"},
                    stop_on_error=True)
        assert out["ok"] is False
        assert "No feature named" in out["errors"][0]["error"]

    def test_a_single_point_patterns_nothing_and_says_so(self, session):
        """The seed's own point and no others: legitimate, and worth reporting."""
        out = build(session, *PLATE, *POCKET, spots((-35, -20), names=["home"]),
                    {"op": "sketch_driven_pattern", "features": ["Slot"],
                     "sketch": "Spots", "reference": "home"})
        assert last(out)["detail"]["occurrences"] == 0
        assert last(out)["measured"]["volume_cm3"] == pytest.approx(59.6, abs=5e-6)


class TestTheOccurrencePrismsBelongToThePattern:
    def test_patterning_the_pattern_copies_the_occurrences_too(self, session):
        """Each copy is attributed to the pattern, not to the seed.

        Otherwise a second pattern of the same seed would find the first one's
        occurrences and copy those as well -- and a pattern *of the pattern*
        would find nothing.
        """
        out = build(session, *PLATE, *POCKET,
                    spots((-35, -20), (0, 0), names=["home", "a"]),
                    {"op": "sketch_driven_pattern", "name": "First",
                     "features": ["Slot"], "sketch": "Spots", "reference": "home"},
                    {"op": "sketch", "name": "More", "plane": "xy", "entities": [
                        {"type": "point", "name": "here", "position": [0, 0]},
                        {"type": "point", "name": "up", "position": [0, 20]}]},
                    {"op": "sketch_driven_pattern", "name": "Second",
                     "features": ["First"], "sketch": "More", "reference": "here"})
        # One occurrence prism in First, copied once by Second.
        assert last(out)["detail"]["prisms_placed"] == 1
