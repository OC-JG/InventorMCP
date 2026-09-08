"""Counting a pattern's occurrences, which is not counting features.

`examples/calibration/spread_pockets.json` was built to ask one question:
does Inventor put an occurrence of a sketch-driven pattern on the reference
point as well? Two of its three possible answers are the same volume, so the
2026-09-08 run measured -1.2000 cm^3 exactly and settled nothing -- the
finished part reads `Plate`, `Slot`, `Spread`, because a sketch-driven pattern
is **one** feature holding its occurrences.

`describe_feature` asks the pattern itself now. The property name is unmeasured,
so two are tried and the one that answered is reported beside the number, and
`scripts/live_acceptance.py --only sketch-driven-pattern` reports rather than
asserts when neither does. These fakes pin that shape without a CAD seat.
"""

from __future__ import annotations

from inventor_mcp.backend.com import backend as com


class TestAPatternsOccurrenceCount:
    """`_occurrence_count` is the read that would settle the one open question
    about `sketch_driven_pattern`: whether Inventor places an occurrence on the
    reference point as well. `spread_pockets` measured exactly and could not
    say, because a sketch-driven pattern is *one* feature holding its
    occurrences and counting features cannot count them.

    The property name is unmeasured, which is why two are tried and the one
    that answered is reported. These fakes pin that shape without a seat."""

    class _Collection:
        def __init__(self, count):
            self.Count = count

    def test_the_first_collection_that_answers_is_used(self):
        class Feature:
            Occurrences = TestAPatternsOccurrenceCount._Collection(3)
            PatternElements = TestAPatternsOccurrenceCount._Collection(99)

        assert com._occurrence_count(Feature()) == (3, "Occurrences")

    def test_the_second_is_the_fallback_and_says_so(self):
        """A release keeping them under the other name is a different reading,
        not the same one -- so the answer carries where it came from."""
        class Feature:
            PatternElements = TestAPatternsOccurrenceCount._Collection(4)

        assert com._occurrence_count(Feature()) == (4, "PatternElements")

    def test_a_feature_that_holds_neither_answers_nothing(self):
        """Not zero. A feature with no occurrences and a feature whose
        occurrences could not be read are different facts, and reporting the
        second as the first is how a check passes by measuring nothing."""
        class Feature:
            Name = "Block"

        assert com._occurrence_count(Feature()) == (None, None)

    def test_a_collection_that_raises_is_skipped_rather_than_fatal(self):
        class Angry:
            @property
            def Count(self):
                raise RuntimeError("marshalled for a different thread")

        class Feature:
            Occurrences = Angry()
            PatternElements = TestAPatternsOccurrenceCount._Collection(3)

        assert com._occurrence_count(Feature()) == (3, "PatternElements")
