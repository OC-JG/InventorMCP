"""The loop: when it changes things, and when it stops.

The analyser is scripted here rather than run. What is under test is not whether
the DFM tool measures correctly -- its own tests cover that, and
``test_dfm_targets.py`` checks the targets against the live rules -- but whether
this loop does the right thing with an answer: applies the change, notices what
it did to the score, and puts it back when the answer is "made it worse".

The parameter changes are real. They go through ``apply_parameter`` into the mock
document, so a revert that fails to revert shows up as the wrong number in the
parameter table rather than as a passing assertion about intent.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from inventor_mcp.builder import build_part
from inventor_mcp.dfm import loop as loop_module
from inventor_mcp.dfm.loop import current_parameters, improve, plan_from_recipe
from inventor_mcp.dfm.report import read_report
from inventor_mcp.schema import PartRecipe

FIXTURES = Path(__file__).parent / "fixtures" / "dfm"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def report(name: str = "many_findings", *, score=None, keep=None,
           required_draft=None, **mesh) -> object:
    """A report, optionally with a different score, fewer findings or another
    required draft angle -- which is what makes the draft target move."""
    data = copy.deepcopy(fixture(name))
    if score is not None:
        data["score"] = score
    if required_draft is not None:
        data["material_limits"]["required_draft_deg"] = required_draft
        data["mesh_summary"]["effective_min_draft_deg"] = required_draft
    if keep is not None:
        for check in data["checks"]:
            if check["key"] not in keep:
                check["score_deduction"] = 0
                check["severity"] = "none"
                check["status"] = "ok"
    data["mesh_summary"].update(mesh)
    return read_report(data)


RECIPE = {
    "name": "Housing", "units": "mm",
    "parameters": [
        {"name": "wall_t", "value": 2.0},
        {"name": "draft_a", "value": 0.2, "unit": "deg"},
        {"name": "rib_t", "value": 1.9},
        {"name": "rib_h", "value": 9.0},
        {"name": "rib_r", "value": 0.05},
        {"name": "boss_d", "value": 6.0},
        {"name": "boss_w", "value": 0.6},
        {"name": "plate_l", "value": 60},
    ],
    "operations": [
        {"op": "sketch", "name": "Base", "plane": "xy",
         "entities": [{"type": "rectangle", "center": [0, 0],
                       "width": "plate_l", "height": 40}]},
        {"op": "extrude", "name": "Body", "sketch": "Base", "distance": 20},
    ],
    "dfm": {
        "parameters": {
            "wall": "wall_t", "draft": "draft_a", "rib_thickness": "rib_t",
            "rib_height": "rib_h", "rib_fillet": "rib_r",
            "boss_od": "boss_d", "boss_wall": "boss_w",
        },
        "settings": {"material": "abs"},
    },
}


class Scripted:
    """Stands in for ``measure``: hands back the next scripted report.

    The parameter values it returns are read from the live document, so the
    proposal each round is computed against whatever the previous round actually
    managed to apply.
    """

    def __init__(self, *reports):
        self.reports = list(reports)
        self.calls = 0
        self.seen: list[dict[str, float]] = []

    def __call__(self, session, context, **kwargs):
        values, expressions = current_parameters(session, context)
        self.seen.append(dict(values))
        index = min(self.calls, len(self.reports) - 1)
        self.calls += 1
        told = dict(kwargs.get("settings") or {})
        return (self.reports[index], values, expressions,
                Path(f"round-{index}.stl"), Path(f"round-{index}.json"), told)


@pytest.fixture
def part(session, monkeypatch, tmp_path):
    recipe = PartRecipe.model_validate(copy.deepcopy(RECIPE))
    result = build_part(session, recipe)
    assert result["ok"], result["errors"]
    monkeypatch.chdir(tmp_path)
    return session.context()


def run(session, part, *reports, **kw):
    scripted = Scripted(*reports)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(loop_module, "measure", scripted)
        outcome = improve(session, part, **kw)
    return outcome, scripted


def value(session, part, name: str) -> float:
    values, _ = current_parameters(session, part)
    return values[name]


class TestItActuallyChangesThings:
    def test_the_first_round_applies_the_proposal(self, session, part):
        run(session, part, report(score=49), report("clean", score=100))
        assert value(session, part, "draft_a") == pytest.approx(2.1)

    def test_a_ratio_lands_as_an_expression(self, session, part):
        run(session, part, report(score=49), report("clean", score=100))
        _, expressions = current_parameters(session, part)
        assert "wall_t" in expressions["rib_t"], expressions["rib_t"]

    def test_and_the_expression_keeps_holding_when_the_wall_moves(self, session, part):
        """The reason a ratio is written as one. Change the wall afterwards and
        the rib follows, instead of the check breaking again."""
        run(session, part, report(score=49), report("clean", score=100))
        from inventor_mcp.builder import apply_parameter
        from inventor_mcp.schema import ParameterSpec
        apply_parameter(session, part, ParameterSpec(name="wall_t", value=4))
        assert value(session, part, "rib_t") == pytest.approx(4 * 0.45)

    def test_it_stops_when_nothing_is_left_to_change(self, session, part):
        outcome, _ = run(session, part, report(score=49), report("clean", score=100))
        assert "nothing is left" in outcome.stopped_because

    def test_the_rounds_record_what_cleared(self, session, part):
        outcome, _ = run(session, part, report(score=49), report("clean", score=100))
        assert set(outcome.rounds[1].cleared) >= {"wall", "draft", "ribs"}

    def test_the_score_movement_is_reported(self, session, part):
        outcome, _ = run(session, part, report(score=49), report("clean", score=100))
        assert outcome.improvement == pytest.approx(51)


class TestItPutsThingsBack:
    def test_a_change_that_lowers_the_score_is_undone(self, session, part):
        outcome, _ = run(session, part, report(score=60), report(score=40))
        assert value(session, part, "draft_a") == pytest.approx(0.2), (
            "the document is the deliverable; a described regression is still a "
            "worse part")
        assert "made the part worse" in outcome.stopped_because

    def test_every_parameter_in_the_round_goes_back_not_just_one(self, session, part):
        run(session, part, report(score=60), report(score=40))
        values, _ = current_parameters(session, part)
        assert values["rib_t"] == pytest.approx(1.9)
        assert values["boss_d"] == pytest.approx(6.0)
        assert values["wall_t"] == pytest.approx(2.0)

    def test_the_round_says_it_was_reverted(self, session, part):
        outcome, _ = run(session, part, report(score=60), report(score=40))
        assert outcome.rounds[-1].reverted
        assert "put back" in outcome.rounds[-1].reverted

    def test_a_change_that_breaks_the_rebuild_is_undone(self, session, part, monkeypatch):
        monkeypatch.setattr(
            session.backend, "rebuild",
            lambda doc_id: {"ok": False, "errors": ["Body failed to compute"]},
        )
        outcome, _ = run(session, part, report(score=49), report("clean", score=100))
        assert value(session, part, "draft_a") == pytest.approx(0.2)
        assert "broke the rebuild" in outcome.stopped_because

    def test_an_untranslated_health_status_is_not_a_broken_rebuild(self, session, part, monkeypatch):
        """Inventor 2027.1 has no HealthStatusEnum to ask, so the backend reports
        statuses it cannot name. Treating one as a failure once made a correct
        rebuild look like three broken features."""
        monkeypatch.setattr(
            session.backend, "rebuild",
            lambda doc_id: {"ok": True, "uninterpreted_health": [11778]},
        )
        outcome, _ = run(session, part, report(score=49), report("clean", score=100))
        assert "broke the rebuild" not in outcome.stopped_because


class TestItKnowsWhenToStop:
    def test_a_change_that_comes_back_round_again_is_a_cycle(self, session, part):
        """Draft to 2.1, then to 4.0, then back to 2.1 -- which is oscillation,
        not convergence, and the second 2.1 is the tell.

        This is a backstop rather than the main guard. It only fires on an exact
        repeat, and floating-point corrections rarely land on one; what actually
        bounds the loop is the score check, the did-not-clear check and the round
        limit. Worth having, not worth relying on.
        """
        outcome, _ = run(
            session, part,
            # 1.1 deg required, so the draft target is 2.1.
            report(score=49, required_draft=1.1),
            # The wall and the ribs came clean, and the requirement moved: 4.0.
            report(score=70, keep=["draft"], required_draft=3.0),
            # Back to 1.1, so the target returns to 2.1 -- which has been set
            # before, and setting it again is going round rather than forward.
            report(score=80, keep=["draft"], required_draft=1.1),
            rounds=6,
        )
        assert "cycle" in outcome.stopped_because, outcome.stopped_because
        assert "draft_a" in outcome.stopped_because

    def test_the_round_limit_is_honoured(self, session, part):
        outcome, scripted = run(
            session, part,
            report(score=40), report(score=50, wall_sphere_median_mm=1.0),
            rounds=2,
        )
        assert len(outcome.rounds) <= 3

    def test_an_untrustworthy_mesh_stops_before_anything_is_changed(self, session, part):
        outcome, _ = run(session, part, report("inch_scaled"))
        assert value(session, part, "draft_a") == pytest.approx(0.2)
        assert "unusable" in outcome.stopped_because
        assert len(outcome.rounds) == 1

    def test_a_clean_part_is_left_alone_and_said_to_be(self, session, part):
        outcome, _ = run(session, part, report("clean", score=100))
        assert outcome.rounds == outcome.rounds[:1]
        assert "no findings" in outcome.stopped_because

    def test_a_change_that_does_not_clear_its_finding_is_called_out(self, session, part):
        """What a drifted target looks like from inside the loop."""
        outcome, _ = run(session, part, report(score=49), report(score=49.2), rounds=4)
        assert ("cycle" in outcome.stopped_because
                or "disagree" in outcome.stopped_because)


class TestKeyGeometryHolds:
    def test_a_frozen_parameter_is_not_touched(self, session, part):
        run(session, part, report(score=49), report("clean", score=100),
            freeze=["draft_a"])
        assert value(session, part, "draft_a") == pytest.approx(0.2)

    def test_and_the_result_says_why(self, session, part):
        outcome, _ = run(session, part, report(score=49), report("clean", score=100),
                         freeze=["draft_a"])
        held = [d for d in outcome.outstanding if d.reason == "frozen"]
        assert held, "a refusal nobody mentions reads as a fix"

    def test_what_is_protected_is_reported_with_the_result(self, session, part):
        outcome, _ = run(session, part, report(score=49), report("clean", score=100),
                         freeze=["draft_a"])
        assert "draft_a" in outcome.frozen["declared"]

    def test_the_others_still_change(self, session, part):
        run(session, part, report(score=49), report("clean", score=100),
            freeze=["draft_a"])
        assert value(session, part, "rib_r") != pytest.approx(0.05)

    def test_freezing_something_a_change_depends_on_blocks_it_too(self, session):
        """A frozen sealing face computed from the wall protects the wall.

        Without this the freeze is honoured on paper and broken in fact: nothing
        would have touched `seal_face`, and the wall it is measured from would
        have moved underneath it.
        """
        recipe = copy.deepcopy(RECIPE)
        recipe["parameters"].append(
            {"name": "seal_face", "value": "wall_t * 2", "frozen": True})
        build_part(session, PartRecipe.model_validate(recipe))
        context = session.context()
        run(session, context, report(score=49), report("clean", score=100))
        assert value(session, context, "wall_t") == pytest.approx(2.0)
        assert value(session, context, "seal_face") == pytest.approx(4.0)

    def test_and_a_literal_valued_freeze_protects_only_itself(self, session, part):
        """The counterpart. rib_t is 1.9 with no dependencies, so freezing it
        says nothing about the wall -- claiming otherwise would refuse changes
        for no reason."""
        run(session, part, report(score=49), report("clean", score=100),
            freeze=["rib_t"])
        assert value(session, part, "wall_t") != pytest.approx(2.0)
        assert value(session, part, "rib_t") == pytest.approx(1.9)

    def test_a_freeze_written_by_an_earlier_round_is_honoured_afterwards(self, session):
        """Round 1 rewrites rib_t as `wall_t * 0.45`. From then on rib_t really
        does depend on the wall, so a guard resolved against the recipe -- a
        snapshot from build time -- would be working from a table that has
        moved."""
        recipe = copy.deepcopy(RECIPE)
        build_part(session, PartRecipe.model_validate(recipe))
        context = session.context()
        run(session, context, report(score=49), report("clean", score=100))
        _, guard, _ = plan_from_recipe(context.recipe, freeze=["rib_t"])
        _, expressions = current_parameters(session, context)
        assert guard.check("wall_t") is None, "the snapshot still has rib_t as 1.9"
        assert guard.with_expressions(expressions).check("wall_t") is not None

    def test_a_run_cannot_take_a_freeze_off(self, session, part):
        """The recipe froze it; nothing passed at the call may release it."""
        recipe = copy.deepcopy(RECIPE)
        recipe["parameters"][0]["frozen"] = True
        build_part(session, PartRecipe.model_validate(recipe))
        context = session.context()
        _, guard, _ = plan_from_recipe(context.recipe, freeze=[])
        assert guard.check("wall_t") is not None


class TestFunctionalChanges:
    def test_they_are_applied_by_default(self, session, part):
        run(session, part, report(score=49), report("clean", score=100))
        assert value(session, part, "rib_h") != pytest.approx(9.0)

    def test_and_held_back_when_asked(self, session, part):
        run(session, part, report(score=49), report("clean", score=100),
            include_functional=False)
        assert value(session, part, "rib_h") == pytest.approx(9.0)

    def test_with_a_note_saying_what_was_not_done(self, session, part):
        outcome, _ = run(session, part, report(score=49), report("clean", score=100),
                         include_functional=False)
        assert any("changes are switched off" in note for note in outcome.notes)

    def test_the_non_functional_ones_still_go_in(self, session, part):
        run(session, part, report(score=49), report("clean", score=100),
            include_functional=False)
        assert value(session, part, "rib_r") != pytest.approx(0.05)


class TestTheGate:
    def test_it_is_supplied_from_the_tool_s_own_search(self, session, part):
        outcome, _ = run(session, part, report(score=49), report("clean", score=100))
        assert any("gate" in note for note in outcome.notes)


class TestUnits:
    def test_parameters_are_handed_over_in_millimetres(self, session):
        """The analyser is millimetres throughout, and refuses a mesh whose size
        makes millimetres implausible. An inch recipe passed through as-is would
        be judged 25.4x small, which reads as a critical wall failure."""
        recipe = copy.deepcopy(RECIPE)
        recipe["units"] = "in"
        recipe["parameters"] = [{"name": "wall_t", "value": 0.1}]
        recipe["operations"] = [
            {"op": "sketch", "name": "Base", "plane": "xy",
             "entities": [{"type": "rectangle", "center": [0, 0], "width": 2, "height": 1}]},
            {"op": "extrude", "name": "Body", "sketch": "Base", "distance": 0.5},
        ]
        recipe["dfm"] = {"parameters": {"wall": "wall_t"}}
        build_part(session, PartRecipe.model_validate(recipe))
        values, _ = current_parameters(session, session.context())
        assert values["wall_t"] == pytest.approx(2.54), "0.1 in is 2.54 mm"


class TestTheResultShape:
    def test_it_serialises(self, session, part):
        outcome, _ = run(session, part, report(score=49), report("clean", score=100))
        as_dict = outcome.as_dict()
        json.dumps(as_dict)
        assert as_dict["score"]["start"] == 49
        assert as_dict["stopped_because"]
        assert "key_geometry" in as_dict

    def test_it_always_says_why_it_stopped(self, session, part):
        for reports in (
            (report(score=49), report("clean", score=100)),
            (report(score=60), report(score=40)),
            (report(score=49), report(score=49)),
            (report("inch_scaled"),),
            (report("clean", score=100),),
        ):
            outcome, _ = run(session, part, *reports, rounds=3)
            assert outcome.stopped_because, reports
