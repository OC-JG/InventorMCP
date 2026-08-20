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
            before = document.volume
            info = real_hole(doc_id, request)
            document.volume = before
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
        import inventor_mcp.builder as builder

        monkeypatch.setattr(session.ensure_backend(), "name", "inventor")
        monkeypatch.setattr(builder, "rehearse",
                            lambda recipe: (_ for _ in ()).throw(RuntimeError("boom")))
        result = build_part(session, self.recipe())
        assert result["ok"] is True
        assert "divergence" not in result
