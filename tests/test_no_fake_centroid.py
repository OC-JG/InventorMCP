"""The simulator's centre of mass: absent rather than wrong.

`MockBackend.mass_properties` used to report the bounding box's centre as the
part's centroid. That is not an approximation of a centroid, it is a different
quantity: a centroid moves when material moves, a box centre does not move for
a void at all. The plate below is the reproduction -- six 5 mm bores on a circle
whose centre is a parameter -- and it reported `(0, 0, 0.5)` for every value of
that parameter. A real centroid sits 0.37283 mm off that in X at `bolt_x` 30 and
moves a further 0.18640 mm at 45, both derived from the arithmetic the
acceptance script asserts against rather than read off a seat: the live run of
2026-09-07 stopped at defect 8 without reaching this check.

`scripts/live_acceptance.py`'s `check_work_geometry` is what made it expensive
rather than merely untidy. It judges the off-centre bolt-circle axis by how far
the centre of mass moved, and it skips -- deliberately, with a note -- when a
backend reports no centroid. A backend reporting a constant one instead does not
get skipped: it gets a failure, against a work axis that had done its job.
"""

from __future__ import annotations

from inventor_mcp.builder import build_part
from inventor_mcp.schema import PartRecipe


def plate(session, bolt_x: float, *, pattern: bool = True):
    """A 120 x 80 x 10 plate with a bolt circle centred at `bolt_x`."""
    operations = [
        {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
            {"type": "rectangle", "center": [0, 0], "width": 120, "height": 80}]},
        {"op": "extrude", "name": "Body", "sketch": "Outline", "distance": 10},
        {"op": "work_axis", "name": "BoltAxis", "kind": "normal_to_plane",
         "plane": "xy", "at": ["bolt_x", 0]},
        {"op": "sketch", "name": "Pilot", "plane": "xy", "entities": [
            {"type": "point", "position": ["bolt_x + pcd / 2", 0]}]},
        {"op": "hole", "name": "Bolt1", "sketch": "Pilot", "diameter": 5,
         "through_all": True, "direction": "negative"},
    ]
    if pattern:
        operations.append({"op": "circular_pattern", "name": "BoltCircle",
                           "features": ["Bolt1"], "axis": "BoltAxis", "count": 6})
    result = build_part(session, PartRecipe.model_validate({
        "name": "Plate", "units": "mm",
        "parameters": [{"name": "pcd", "value": 60},
                       {"name": "bolt_x", "value": bolt_x}],
        "operations": operations,
    }))
    assert result["ok"], result["errors"]
    return result["document"]


class TestTheCentroidIsNotInvented:
    def test_the_plate_with_off_centre_holes_reports_no_centroid(self, session):
        """The reproduction: nothing is reported, so nothing is wrong."""
        properties = session.backend.mass_properties(plate(session, 30))
        assert properties.center_of_mass is None
        assert properties.bounding_box is not None, "the box itself is still real"

    def test_it_says_why_and_where_the_box_centre_went(self, session):
        """Absent is only honest if the caller can tell absent from forgotten."""
        properties = session.backend.mass_properties(plate(session, 30))
        assert "no centroid" in (properties.center_of_mass_from or "")
        assert "bounding_box" in (properties.center_of_mass_from or "")

    def test_the_tool_result_carries_no_centroid_either(self, session):
        """`measure_part` and a build both hand callers `as_dict`, which drops
        a `None` -- so the key is simply absent rather than present and false."""
        as_dict = session.backend.mass_properties(plate(session, 30)).as_dict()
        assert "center_of_mass" not in as_dict
        assert "center_of_mass_from" in as_dict

    def test_no_centroid_is_reported_without_a_pattern_either(self, session):
        """The ledger holds this part's one bore in full, and still says nothing.

        Not an oversight. A centroid summed over the ledger's signed prisms
        would be real *here*, and wrong the moment the same part gets its
        pattern back -- a `circular_pattern` moves volume without recording
        prisms, so the ledger would count one bore of six and produce a figure
        that moves by a sixth of the truth. Reporting one for the easy case and
        a sixth of one for the case the acceptance script actually builds is
        worse than reporting neither.
        """
        properties = session.backend.mass_properties(plate(session, 30, pattern=False))
        assert properties.center_of_mass is None


class TestWhatTheAcceptanceScriptDoesWithThat:
    def test_the_bolt_circle_check_skips_rather_than_failing(self, session):
        """`_centre_shift_mm` returns None, which is the skip branch.

        The two plates are the two ends of the acceptance script's measurement:
        the bolt circle 30 mm off-axis and then 45. Zero -- what a box centre
        gave for both -- is the *failure* branch, and it blames the work axis
        for the simulator.
        """
        from scripts.live_acceptance import _centre_shift_mm

        before = session.backend.mass_properties(plate(session, 30))
        after = session.backend.mass_properties(plate(session, 45))
        assert _centre_shift_mm(before, after) is None
