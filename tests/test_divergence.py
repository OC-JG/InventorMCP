"""Using the simulator as an oracle for the live build.

The simulator now predicts an extruded part's volume to within a rounding error
-- the angle bracket rehearses at 43.2012 cm^3 against Inventor's measured
43.1999. That makes it worth asking, after a live build, whether Inventor did
what the recipe implied: a fillet on the wrong edge, a cut on the wrong side and
a hole that met no material all shipped here at least once, and each of them
changes the volume by an amount the simulator would have predicted differently.

Deltas are compared, not totals, so one wrong operation does not flag every
operation after it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inventor_mcp.builder import NOTICEABLE, PREDICTED, build_part, compare_to_rehearsal
from inventor_mcp.schema import PartRecipe
from inventor_mcp.session import Session

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def step(index: int, op: str, change: float | None, name: str | None = None) -> dict:
    measured = {"volume_cm3": 10.0}
    if change is not None:
        measured["volume_change_cm3"] = change
    return {"index": index, "op": op, "name": name, "measured": measured}


class TestWhatItReports:
    def test_agreement_is_silent(self):
        assert compare_to_rehearsal(
            [step(0, "extrude", 46.2), step(1, "hole", -0.7634)],
            [step(0, "extrude", 46.2), step(1, "hole", -0.7634)],
        ) == []

    def test_the_arc_sampling_difference_is_not_a_finding(self):
        """The bracket's slots differ by 0.04%, which is the simulator's arcs."""
        assert compare_to_rehearsal([step(0, "extrude", -1.4617)],
                                    [step(0, "extrude", -1.4611)]) == []

    def test_a_hole_that_met_no_material_is_caught(self):
        [finding] = compare_to_rehearsal([step(0, "hole", 0.0, "Fixings")],
                                         [step(0, "hole", -0.7634, "Fixings")])
        assert finding["op"] == "hole" and finding["name"] == "Fixings"
        assert finding["measured_cm3"] == 0.0
        assert finding["rehearsed_cm3"] == -0.7634
        assert "met no material" in finding["why"]

    def test_a_fillet_on_the_wrong_edge_is_caught(self):
        """It removes where an inside corner would have added: the sign flips."""
        [finding] = compare_to_rehearsal([step(0, "fillet", -0.6867)],
                                         [step(0, "fillet", +0.6867)])
        assert "the other way" in finding["why"]
        assert "convex edge rather than the inside corner" in finding["why"]

    def test_a_cut_on_the_wrong_side_is_caught(self):
        [finding] = compare_to_rehearsal([step(0, "extrude", +1.5)],
                                         [step(0, "extrude", -1.5)])
        assert "the other way" in finding["why"]

    def test_removing_too_much_reads_differently_from_too_little(self):
        [more] = compare_to_rehearsal([step(0, "extrude", -3.0)], [step(0, "extrude", -1.5)])
        [less] = compare_to_rehearsal([step(0, "extrude", -0.5)], [step(0, "extrude", -1.5)])
        assert "more than the geometry implies" in more["why"]
        assert "less than the geometry implies" in less["why"]

    def test_every_divergence_carries_both_numbers(self):
        [finding] = compare_to_rehearsal([step(3, "hole", -0.1)], [step(3, "hole", -0.9)])
        assert finding["off_by_cm3"] == pytest.approx(0.8)
        assert finding["index"] == 3


class TestWhatItRefusesToJudge:
    def test_an_operation_the_simulator_does_not_model(self):
        assert compare_to_rehearsal([step(0, "thread", 0.0)],
                                    [step(0, "thread", -5.0)]) == []

    def test_a_missing_measurement_on_either_side(self):
        assert compare_to_rehearsal([step(0, "hole", None)], [step(0, "hole", -1.0)]) == []
        assert compare_to_rehearsal([step(0, "hole", -1.0)], [step(0, "hole", None)]) == []

    def test_an_operation_with_no_rehearsed_counterpart(self):
        assert compare_to_rehearsal([step(7, "hole", -1.0)], [step(0, "hole", -1.0)]) == []

    def test_a_difference_too_small_to_mean_anything(self):
        """A 2 mm chamfer moves about a thousandth of a cm^3."""
        assert compare_to_rehearsal([step(0, "chamfer", -0.001)],
                                    [step(0, "chamfer", -0.003)]) == []
        assert NOTICEABLE > 0.003

    def test_the_loose_estimates_are_loose(self):
        """Pappus and a mean section are estimates; only gross errors count."""
        assert PREDICTED["loft"] > PREDICTED["extrude"]
        assert compare_to_rehearsal([step(0, "loft", 100.0)],
                                    [step(0, "loft", 80.0)]) == []
        assert compare_to_rehearsal([step(0, "loft", 200.0)], [step(0, "loft", 80.0)])


class TestThroughTheBuild:
    @pytest.fixture
    def session(self) -> Session:
        session = Session(backend_kind="mock")
        session.ensure_backend().connect()
        return session

    def recipe(self) -> PartRecipe:
        return PartRecipe.model_validate(
            json.loads((EXAMPLES / "mounting_plate.json").read_text()))

    def test_the_simulator_is_not_compared_with_itself(self, session):
        result = build_part(session, self.recipe())
        assert "divergence" not in result
        assert "comparing it with itself" in result["divergence_note"]

    def test_appending_is_not_compared_either(self, session):
        """The rehearsal starts from an empty part, so the deltas would not line up."""
        first = build_part(session, self.recipe())
        second = build_part(session, self.recipe(), document=first["document"],
                            stop_on_error=False)
        assert "appended to" in second["divergence_note"]

    def test_it_can_be_turned_off(self, session):
        result = build_part(session, self.recipe(), against_rehearsal=False)
        assert "divergence_note" not in result

    def test_a_backend_that_disagrees_is_reported(self, session, monkeypatch):
        """Pretend the simulator is Inventor, and make one operation misbehave.

        Faked because the real case needs a CAD seat -- but the mechanism under
        test is the comparison, and this exercises the whole path through
        build_part rather than the pure function alone.
        """
        backend = session.ensure_backend()
        # Patched on the instance, not the class: the rehearsal builds its own
        # backend, and it has to stay honest or there is nothing to disagree with.
        real_hole = backend.hole

        def lazy_hole(doc_id, request):
            """Drill nothing, exactly as a hole over empty air does."""
            document = backend._doc(doc_id)
            before = list(document.bodies)
            info = real_hole(doc_id, request)
            document.bodies = before
            return info

        monkeypatch.setattr(backend, "name", "inventor")
        monkeypatch.setattr(backend, "hole", lazy_hole)
        result = build_part(session, self.recipe())
        [finding] = result["divergence"]
        assert finding["op"] == "hole"
        assert finding["measured_cm3"] == 0.0
        assert "met no material" in finding["why"]
        assert "read these before trusting the result" in result["divergence_note"]

    def test_a_rehearsal_that_breaks_does_not_break_the_build(self, session, monkeypatch):
        # Patched where `rehearse` is defined, not where it is called from:
        # `build_part` imports it from `rehearsal` at the moment it needs it, so
        # patching the name on `builder` would replace something nothing reads.
        import inventor_mcp.rehearsal as rehearsal

        monkeypatch.setattr(session.ensure_backend(), "name", "inventor")
        monkeypatch.setattr(rehearsal, "rehearse",
                            lambda recipe: (_ for _ in ()).throw(RuntimeError("boom")))
        result = build_part(session, self.recipe())
        assert result["ok"] is True
        assert "divergence" not in result


class TestAHollowPartIsNotJudged:
    """After a shell, the simulator over-removes and must not report it.

    It has no booleans: a cut into a hollow box takes a whole prism out here
    where Inventor takes only the walls it meets. The enclosure's cable entry
    removes 5.04 cm^3 in the simulator and 0.36 in the model, both correctly for
    what they are, so comparing them would fault a correct recipe.
    """

    @pytest.fixture
    def session(self) -> Session:
        session = Session(backend_kind="mock")
        session.ensure_backend().connect()
        return session

    def rehearsal(self) -> dict:
        from inventor_mcp.builder import rehearse

        recipe = PartRecipe.model_validate(
            json.loads((EXAMPLES / "enclosure_base.json").read_text()))
        report = rehearse(recipe)
        assert report["ok"], report["findings"]
        return report

    def test_the_steps_after_a_shell_are_marked(self):
        steps = self.rehearsal()["steps"]
        kinds = {step["op"]: step for step in steps}
        assert kinds["shell"].get("predictable") is not False, "the shell itself is"
        assert kinds["hole"]["predictable"] is False
        assert "no booleans" in kinds["hole"]["why_not"]

    def test_the_steps_before_it_are_not(self):
        steps = self.rehearsal()["steps"]
        first = [s for s in steps if s["op"] == "extrude"][0]
        assert "predictable" not in first

    def test_a_marked_step_is_skipped_by_the_comparison(self):
        rehearsed = [dict(step(0, "hole", -0.6), predictable=False)]
        assert compare_to_rehearsal([step(0, "hole", -0.05)], rehearsed) == []
        # And without the mark it would have been reported, so the mark is doing
        # the work rather than the numbers happening to agree.
        assert compare_to_rehearsal([step(0, "hole", -0.05)], [step(0, "hole", -0.6)])


class TestTheShellItself:
    """Which *is* predictable, because a shelled prism is an inset sweep."""

    @pytest.fixture
    def session(self) -> Session:
        session = Session(backend_kind="mock")
        session.ensure_backend().connect()
        return session

    def test_a_box_with_its_top_off_is_walls_and_a_floor(self, session):
        from inventor_mcp.builder import build_part

        result = build_part(session, PartRecipe.model_validate({
            "name": "Box", "units": "mm", "operations": [
                {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": 100, "height": 70}]},
                {"op": "extrude", "name": "Block", "sketch": "Outline", "distance": 35},
                {"op": "shell", "name": "Cavity", "thickness": 2.5,
                 "faces": {"kind": "face", "filter": "top"}},
            ]}))
        assert result["ok"], result["errors"]
        volume = session.backend.mass_properties(result["document"]).volume
        # 10 x 7 x 3.5 block, less a 9.5 x 6.5 cavity 3.25 deep.
        assert volume == pytest.approx(10 * 7 * 3.5 - 9.5 * 6.5 * 3.25, rel=1e-9)

    def test_it_says_where_the_number_came_from(self, session):
        from inventor_mcp.builder import build_part

        result = build_part(session, PartRecipe.model_validate({
            "name": "Box", "units": "mm", "operations": [
                {"op": "sketch", "name": "O", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": 40, "height": 40}]},
                {"op": "extrude", "sketch": "O", "distance": 20},
                {"op": "shell", "thickness": 2, "faces": {"kind": "face", "filter": "top"}},
            ]}))
        document = session.backend._doc(result["document"])
        shell = [f for f in document.features if f.kind == "shell"][0]
        assert shell.detail["volume_from"] == "the outline inset by the wall thickness, swept"

    def test_a_revolved_body_falls_back_and_admits_it(self, session):
        from inventor_mcp.builder import build_part

        result = build_part(session, PartRecipe.model_validate({
            "name": "Cup", "units": "mm", "operations": [
                {"op": "sketch", "name": "P", "plane": "xz", "entities": [
                    {"type": "rectangle", "corner": [0, 0], "width": 20, "height": 30}]},
                {"op": "revolve", "name": "Blank", "sketch": "P", "axis": "z"},
                {"op": "shell", "thickness": 2, "faces": {"kind": "face", "filter": "top"}},
            ]}))
        assert result["ok"], result["errors"]
        document = session.backend._doc(result["document"])
        shell = [f for f in document.features if f.kind == "shell"][0]
        assert "not a single prism" in shell.detail["volume_from"]


class TestTheGuardCoversWhatIsGuessedAt:
    """A ponytail with no tolerance beside it is an estimate nothing watches.

    The simulator marks its roughest approximations with a `ponytail:` comment.
    All three of them -- the draft wedge, the split fraction, the emboss ink
    heuristic -- were missing from PREDICTED when this was written, so the
    divergence check, whose whole job is to notice when Inventor did something
    the estimate did not expect, was silent in exactly the places the author had
    flagged as least trustworthy. The coil's helix arc length was missing too.
    """

    def marked_operations(self):
        """Which mock features carry a ponytail, read out of the source."""
        import pathlib
        import re

        source = (pathlib.Path(__file__).resolve().parent.parent
                  / "inventor_mcp/backend/mock/backend.py").read_text()
        lines = source.splitlines()
        found = set()
        for index, line in enumerate(lines):
            if "ponytail:" not in line:
                continue
            # The enclosing `def`, which for a helper is the helper's own name.
            for above in range(index, -1, -1):
                match = re.match(r"\s*def (\w+)\(", lines[above])
                if match:
                    found.add(match.group(1))
                    break
        return found

    def test_the_source_still_marks_its_approximations(self):
        """If this goes empty the convention has been dropped, not satisfied."""
        assert self.marked_operations()

    @pytest.mark.parametrize("op", ["draft", "split", "emboss", "coil"])
    def test_the_estimated_features_are_compared_against_the_live_build(self, op):
        assert op in PREDICTED, (
            f"{op!r} moves the volume by an estimate and nothing compares it "
            "with what Inventor did. Give it a tolerance in PREDICTED.")

    def test_a_loose_tolerance_still_catches_a_sign_flip(self):
        """Which is why half is enough for something never measured live."""
        assert compare_to_rehearsal([step(0, "draft", +1.0)], [step(0, "draft", -1.0)])

    def test_and_a_change_that_did_not_happen(self):
        assert compare_to_rehearsal([step(0, "emboss", 0.0)], [step(0, "emboss", -1.0)])

    def test_but_not_a_coarse_estimate_of_the_right_thing(self):
        assert compare_to_rehearsal([step(0, "emboss", -1.3)], [step(0, "emboss", -1.0)]) == []


class TestACutOnTheWrongSide:
    """The failure a comparison of volumes cannot see, and now does.

    A `trim` split kept the wrong half of every part for as long as the feature
    existed. The run that eventually exposed it reported the simulator at
    19.4286 cm^3 removed and Inventor at 19.2 -- 1.2% apart, inside every
    tolerance in `PREDICTED` -- while the two were keeping *opposite halves of
    the part*. Volume says how much an operation moved and cannot say which side
    it moved it from, so when the two sides are near enough in size there is
    nothing in the number to notice.

    The centre of the bounding box has a direction, and the two halves send it
    opposite ways. Defect 6 in `docs/FEATURE_COVERAGE.md`.
    """

    def split(self, change: float, shift: list[float]) -> list[dict]:
        return [{"index": 0, "op": "split", "name": "Trim", "measured": {
            "volume_cm3": 10.0, "volume_change_cm3": change,
            "centre_shift_mm": shift}}]

    def test_the_historical_case_is_caught_with_the_volumes_agreeing(self):
        """1.2% apart, which passes, and opposite halves, which no longer does."""
        [finding] = compare_to_rehearsal(
            self.split(-19.2, [0, 0, 4.0]),          # Inventor kept the top
            self.split(-19.4286, [0, 0, -10.0]),     # the simulator kept the bottom
        )
        assert "off_by_cm3" not in finding, "the volumes agreed; only the side did not"
        assert finding["axis"] == "z"
        assert finding["rehearsed_shift_mm"] == -10.0
        assert finding["measured_shift_mm"] == 4.0
        assert "opposite way along Z" in finding["why"]

    def test_moving_the_same_way_is_silent(self):
        assert compare_to_rehearsal(self.split(-19.2, [0, 0, -9.5]),
                                    self.split(-19.2, [0, 0, -10.0])) == []

    def test_a_shift_too_small_to_mean_anything_is_ignored(self):
        """A fillet barely moves a centre, and the sign of that is noise.

        Both sides have to clear `SHIFTED` before the direction counts, because
        the simulator's bounding box is synthesised from sketch extents and is
        only approximate for a revolve, a sweep or a loft.
        """
        from inventor_mcp.rehearsal import SHIFTED

        tiny = SHIFTED / 2
        assert compare_to_rehearsal(self.split(-19.2, [0, 0, tiny]),
                                    self.split(-19.2, [0, 0, -tiny])) == []

    def test_one_side_moving_alone_is_not_enough(self):
        """It detects a mirror, not every positional disagreement.

        A wider rule would need calibrating the way the volume tolerances were,
        and nothing has measured one yet -- so this stays narrow and says so.
        """
        assert compare_to_rehearsal(self.split(-19.2, [0, 0, 8.0]),
                                    self.split(-19.2, [0, 0, 0.0])) == []

    def test_both_kinds_of_evidence_land_in_one_finding(self):
        """An operation that is wrong twice should not be reported twice."""
        [finding] = compare_to_rehearsal(self.split(+19.2, [0, 0, 4.0]),
                                         self.split(-19.4286, [0, 0, -10.0]))
        assert "off_by_cm3" in finding and "axis" in finding
        assert "the other way" in finding["why"]
        assert "opposite way along Z" in finding["why"]

    def test_a_step_with_no_box_is_not_guessed_at(self):
        without = [{"index": 0, "op": "split", "name": "Trim",
                    "measured": {"volume_cm3": 10.0, "volume_change_cm3": -19.2}}]
        assert compare_to_rehearsal(without, self.split(-19.2, [0, 0, -10.0])) == []


class TestTheTrimmedPartIsSmallerAfterwards:
    """Which is what gives the check above anything to read.

    The simulator's bounds were left alone by a trim, so a part measured the
    size it had been before the cut -- and with the box unchanged its centre
    could not move, which is the only signal that distinguishes keeping this
    half from keeping the other.
    """

    @pytest.fixture
    def session(self) -> Session:
        session = Session(backend_kind="mock")
        session.ensure_backend().connect()
        return session

    def trim(self, session: Session, remove_positive: bool) -> dict:
        from inventor_mcp.builder import build_part

        result = build_part(session, PartRecipe.model_validate({
            "name": "T", "units": "mm", "operations": [
                {"op": "sketch", "name": "B", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]},
                {"op": "extrude", "name": "Slab", "sketch": "B", "distance": 20},
                {"op": "work_plane", "name": "Cut", "kind": "offset",
                 "base": "xy", "offset": 12},
                {"op": "split", "name": "Trim", "tool": "Cut", "style": "trim",
                 "remove_positive": remove_positive},
            ]}))
        assert result["ok"], result["errors"]
        return session.backend.mass_properties(result["document"]).as_dict()

    def test_keeping_the_lower_half_leaves_a_part_12_mm_tall(self, session):
        box = self.trim(session, remove_positive=True)["bounding_box"]
        assert (box[5] - box[2]) * 10 == pytest.approx(12.0)

    def test_keeping_the_upper_half_leaves_one_8_mm_tall(self, session):
        box = self.trim(session, remove_positive=False)["bounding_box"]
        assert (box[5] - box[2]) * 10 == pytest.approx(8.0)
        assert box[2] * 10 == pytest.approx(12.0), "it starts at the cut"
