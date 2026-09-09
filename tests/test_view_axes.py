"""Two view vocabularies, and the one place they are translated.

A recipe's `direction` is **Inventor's** naming, which is Y-up: measured on
2027.1 on 2026-09-08 by placing one base view per direction of a 120 x 80 x 8
mm plate, `front` and `rear` span XY, `top` and `bottom` span XZ, `left` and
`right` span YZ with Z across. `backend.base.VIEW_AXES` is that measurement and
`docs/DECISIONS.md` is why the naming follows Inventor rather than a vocabulary
of our own.

A `DrawingReading`'s `kind` is the view **as the sheet labels it** -- the ISO
drafting convention a person reads with, where FRONT is the elevation. That is
`drawing.READING_AXES`, and it deliberately did not change: sharing one table
would make every supplier's FRONT view reconstruct as a plan.

So there are two tables holding two different facts, and one function that
crosses between them. This file is what stops either drifting into the other's
meaning, because the failure would be silent: `_overall_from` reconstructs a
part's bounding box from a kind and an extent together, so a wrong translation
does not raise -- it assigns 80 mm to the axis that is 8 mm and reports the
part as the wrong shape.
"""

from __future__ import annotations

import pytest

from inventor_mcp.backend.base import VIEW_AXES
from inventor_mcp.drafting import _VIEW_KINDS, _as_read
from inventor_mcp.drawing import READING_AXES


class TestTheMeasurementIsWhatTheTableSays:
    """The table against the numbers the run produced, direction by direction.
    A plate 120 (X) x 80 (Y) x 8 (Z) mm, so each plane is a different pair and
    no two directions could be confused for one another."""

    PLATE = (120.0, 80.0, 8.0)

    @pytest.mark.parametrize("direction, spans", [
        ("front", (120.0, 80.0)),
        ("rear", (120.0, 80.0)),
        ("top", (120.0, 8.0)),
        ("bottom", (120.0, 8.0)),
        ("left", (8.0, 80.0)),
        ("right", (8.0, 80.0)),
    ])
    def test_the_axes_give_the_measured_extent(self, direction, spans):
        across, up = VIEW_AXES[direction]
        assert (self.PLATE[across], self.PLATE[up]) == spans

    def test_an_isometric_is_absent_rather_than_guessed(self):
        """It shows all three foreshortened, which is not two numbers."""
        assert "iso" not in VIEW_AXES and "isometric" not in VIEW_AXES

    def test_opposite_views_share_a_plane(self):
        """`front`/`rear`, `top`/`bottom` and `left`/`right` are the two sides
        of one plane each. A table that had lost that would be describing a
        part nobody could hold."""
        for one, other in (("front", "rear"), ("top", "bottom"),
                           ("left", "right")):
            assert VIEW_AXES[one] == VIEW_AXES[other]

    def test_the_three_planes_are_all_different(self):
        assert len({frozenset(pair) for pair in VIEW_AXES.values()}) == 3


class TestTheTwoVocabulariesStayDifferent:
    """The point of the pair of tables. If these ever agree, one of them has
    been made to answer the other's question."""

    def test_inventor_and_a_reading_disagree_about_front(self):
        assert VIEW_AXES["front"] == (0, 1), "Inventor's front shows XY"
        assert READING_AXES["front"] == (0, 2), "a sheet's FRONT is the elevation"

    def test_they_are_a_permutation_of_each_other_and_not_a_copy(self):
        """Same three planes, different names on them -- which is exactly what
        makes a translation possible and a shared table wrong."""
        assert {frozenset(pair) for pair in VIEW_AXES.values()} == \
            {frozenset(pair) for pair in READING_AXES.values()}
        assert VIEW_AXES != READING_AXES


class TestTheTranslationIsConsistentWithBothTables:
    """`_VIEW_KINDS` is the single boundary, and this is the invariant that
    makes it checkable: the kind it hands a reading must describe *the same
    plane* the view actually shows, and the extent must be transposed exactly
    when the two tables order that plane differently."""

    @pytest.mark.parametrize("direction", sorted(VIEW_AXES))
    def test_the_kind_names_the_same_plane(self, direction):
        kind, _ = _VIEW_KINDS[direction]
        assert set(READING_AXES[kind]) == set(VIEW_AXES[direction]), (
            f"a {direction!r} view shows {VIEW_AXES[direction]} and would be "
            f"read as {kind!r}, which a reading takes to be "
            f"{READING_AXES[kind]}"
        )

    @pytest.mark.parametrize("direction", sorted(VIEW_AXES))
    def test_the_transpose_is_set_exactly_where_the_order_differs(self, direction):
        kind, transposed = _VIEW_KINDS[direction]
        differs = READING_AXES[kind] != VIEW_AXES[direction]
        assert transposed is differs, (
            f"{direction!r} shows {VIEW_AXES[direction]} and reads as {kind!r} "
            f"which is {READING_AXES[kind]}: transposed should be {differs}"
        )

    def test_only_the_side_views_need_transposing(self):
        """Both vocabularies put left and right on YZ and disagree about which
        way round it is; the other four differ by name alone."""
        assert {name for name, (_, turned) in _VIEW_KINDS.items() if turned} == \
            {"left", "right"}

    def test_an_isometric_translates_to_the_readings_word_for_it(self):
        assert _VIEW_KINDS["iso"] == ("isometric", False)

    def test_the_extent_comes_back_in_the_readings_order(self):
        """The whole point, end to end: a `left` view measured off a sheet as
        8 by 80 -- Z across, Y up, which is what Inventor drew -- reaches a
        reading as 80 by 8, because a reading's LEFT has Y across."""
        kind, extent = _as_read("left", [8.0, 80.0])
        assert (kind, extent) == ("left", [80.0, 8.0])

    def test_a_view_with_no_extent_still_gets_its_kind(self):
        assert _as_read("front", None) == ("top", None)

    def test_an_unknown_direction_does_not_transpose_silently(self):
        """A direction the schema does not have should not reach here, and if
        it does, guessing a transpose would corrupt the extent."""
        assert _as_read("section", [1.0, 2.0]) == ("front", [1.0, 2.0])
