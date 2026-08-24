"""Findings in, parameter changes out.

Two things are being checked here and they are different. That the right
parameter changes -- and that when the right thing to do is *nothing*, nothing
is what comes out, with a reason attached. A remediation engine that quietly
drops the findings it has no rule for is worse than one that has no rules at
all, because the silence reads as "handled".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inventor_mcp.dfm.freeze import FreezeGuard
from inventor_mcp.dfm.remedy import ROLES, propose
from inventor_mcp.dfm.report import read_report

FIXTURES = Path(__file__).parent / "fixtures" / "dfm"

#: A full map, so a test that is not about an unmapped role does not trip over one.
ROLE_MAP = {
    "wall": "wall_t", "draft": "draft_a", "rib_thickness": "rib_t",
    "rib_height": "rib_h", "rib_fillet": "rib_r", "boss_od": "boss_d",
    "boss_wall": "boss_w",
}
VALUES = {"wall_t": 2.0, "draft_a": 0.2, "rib_t": 1.9, "rib_h": 9.0,
          "rib_r": 0.05, "boss_d": 6.0, "boss_w": 0.6}


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def plan(name: str = "many_findings", *, roles=None, frozen=(), values=None, **kw):
    report = read_report(load(name))
    values = dict(VALUES if values is None else values)
    expressions = {k: f"{v:g}" for k, v in values.items()}
    guard = FreezeGuard(frozen, expressions=expressions)
    roles = ROLE_MAP if roles is None else roles
    return propose(report, roles, guard, values, expressions, **kw)


def change_for(proposal, parameter):
    for change in proposal.changes:
        if change.parameter == parameter:
            return change
    return None


def deferral_for(proposal, check, reason=None):
    for entry in proposal.deferred:
        if entry.check == check and (reason is None or entry.reason == reason):
            return entry
    return None


class TestNothingToDo:
    def test_a_clean_part_gets_no_changes(self):
        assert plan("clean").changes == ()

    def test_and_nothing_deferred_either(self):
        assert plan("clean").deferred == ()


class TestTheWall:
    def test_a_thin_wall_is_thickened(self):
        change = change_for(plan(), "wall_t")
        assert change is not None
        assert change.kind == "measured"

    def test_by_what_it_measured_short_rather_than_to_a_flat_value(self):
        """The parameter and the measured wall need not be the same number.

        The fixture measures 0.5 mm with the parameter at 2.0. What is known is
        that the wall is 0.82 mm short of where it should be; that much is what
        the parameter moves. Assigning the target outright would set a 2 mm
        parameter to 1.32 and thin the part that was already too thin.
        """
        change = change_for(plan(), "wall_t")
        assert change.target == pytest.approx(2.0 + (1.2 * 1.1 - 0.5), abs=0.01)
        assert change.target > 2.0, "it must go up, not down"

    def test_the_target_clears_the_material_floor(self):
        report = read_report(load("many_findings"))
        change = change_for(plan(), "wall_t")
        aimed = 0.5 + (change.target - 2.0)   # what the wall would measure
        assert aimed > report.limits.wall_lo, "on the limit is 'no margin' to the check"

    def test_a_thick_wall_is_thinned(self):
        values = dict(VALUES, wall_t=5.0, rib_t=2.2, rib_r=1.5, boss_d=12.0,
                      boss_w=3.0, draft_a=3.0)
        change = change_for(plan("thick_wall", values=values), "wall_t")
        assert change is not None and change.target < 5.0

    def test_thinning_a_wall_is_marked_as_changing_function(self):
        values = dict(VALUES, wall_t=5.0, draft_a=3.0)
        change = change_for(plan("thick_wall", values=values), "wall_t")
        assert change.functional, "coring out is the alternative and it matters"

    def test_a_frozen_wall_is_refused_with_the_reason(self):
        proposal = plan(frozen=["wall_t"])
        assert change_for(proposal, "wall_t") is None
        held = deferral_for(proposal, "wall", "frozen")
        assert held is not None and held.frozen.name == "wall_t"

    def test_an_unmapped_wall_says_which_role_is_missing(self):
        roles = {k: v for k, v in ROLE_MAP.items() if k != "wall"}
        held = deferral_for(plan(roles=roles), "wall", "unmapped")
        assert held is not None and "'wall'" in held.why

    def test_without_the_limits_it_declines_rather_than_guessing(self):
        data = load("many_findings")
        del data["material_limits"]
        report = read_report(data)
        proposal = propose(report, ROLE_MAP, FreezeGuard(), VALUES,
                           {k: str(v) for k, v in VALUES.items()})
        assert change_for(proposal, "wall_t") is None
        held = deferral_for(proposal, "wall", "decision")
        assert "limits" in held.why


class TestDraft:
    def test_a_shallow_draft_is_opened_up(self):
        change = change_for(plan(), "draft_a")
        assert change is not None and change.kind == "declared"

    def test_to_the_required_angle_plus_the_margin_the_check_asks_for(self):
        change = change_for(plan(), "draft_a")
        assert change.expression == "2.1 deg", "1.10 required + 1.0 margin"

    def test_the_target_carries_its_unit(self):
        """A bare number would be read in the recipe's units, which may be inches."""
        assert change_for(plan(), "draft_a").expression.endswith("deg")

    def test_a_generous_declared_draft_with_undrafted_walls_is_not_a_number(self):
        """The parameter is not what drives those faces, and saying so beats
        raising it again and reporting progress that did not happen."""
        data = load("many_findings")
        data["input"]["draftAngle"] = 5.0
        report = read_report(data)
        proposal = propose(report, ROLE_MAP, FreezeGuard(), VALUES,
                           {k: str(v) for k, v in VALUES.items()})
        assert change_for(proposal, "draft_a") is None
        held = deferral_for(proposal, "draft", "decision")
        assert held is not None and "geometry" in held.why


class TestRibsAndBosses:
    def test_every_ratio_in_one_finding_is_answered_at_once(self):
        """The ribs check escalates rather than replacing, so one finding can be
        four problems. Fixing only the worst leaves the rest to reappear."""
        proposal = plan()
        assert {c.parameter for c in proposal.changes} >= {
            "rib_t", "rib_r", "boss_w", "boss_d"}

    def test_a_ratio_is_written_as_an_expression_not_a_number(self):
        """So the relationship survives the next wall change."""
        assert change_for(plan(), "rib_t").expression == "wall_t * 0.45"
        assert change_for(plan(), "rib_r").expression == "wall_t * 0.3"

    def test_the_reported_target_is_against_the_new_wall_not_the_old_one(self):
        """The wall is moving in the same pass. Reporting 0.90 mm next to an
        expression that will make it 1.27 sends a reader to the wrong number."""
        proposal = plan()
        wall = change_for(proposal, "wall_t")
        assert change_for(proposal, "rib_t").target == pytest.approx(
            wall.target * 0.45)

    def test_a_tall_rib_is_shortened_and_marked_functional(self):
        change = change_for(plan(), "rib_h")
        assert change is not None
        assert change.expression == "rib_t * 2.5"
        assert change.functional, "a shorter rib carries less load"

    def test_freezing_the_height_leaves_the_others_alone(self):
        proposal = plan(frozen=["rib_h"])
        assert change_for(proposal, "rib_h") is None
        assert change_for(proposal, "rib_t") is not None
        held = deferral_for(proposal, "ribs", "frozen")
        assert held is not None

    def test_the_refusal_reads_as_a_sentence(self):
        """It once read 'A rib 4. rib_h is key geometry' -- cut at a decimal point."""
        held = deferral_for(plan(frozen=["rib_h"]), "ribs", "frozen")
        assert "A rib 4." not in held.why
        assert "rib_h" in held.why

    def test_a_ratio_needs_the_wall_to_be_written_against(self):
        roles = {k: v for k, v in ROLE_MAP.items() if k != "wall"}
        held = deferral_for(plan(roles=roles), "ribs", "unmapped")
        assert held is not None

    def test_a_ratio_may_be_written_against_a_frozen_driver(self):
        """Reading a frozen value is not changing it."""
        change = change_for(plan(frozen=["wall_t"]), "rib_t")
        assert change is not None and change.expression == "wall_t * 0.45"


class TestTheBossBind:
    """Two guidelines that cannot both hold, which is where a naive loop spins.

    Retention wants a boss wall of at least a quarter of the outside diameter;
    the sink limit caps it at 0.7 of the nominal wall. Above OD = 2.8x wall no
    value satisfies both, and nudging the boss wall back and forth will never
    find one.
    """

    def test_the_diameter_is_what_moves(self):
        change = change_for(plan(), "boss_d")
        assert change is not None and change.expression == "wall_t * 2.4"

    def test_and_the_pair_it_proposes_satisfies_every_guideline(self):
        proposal = plan()
        wall = change_for(proposal, "wall_t").target
        od = change_for(proposal, "boss_d").target
        boss_wall = change_for(proposal, "boss_w").target
        assert boss_wall >= od / 4 - 1e-9, "screw retention"
        assert boss_wall <= 0.7 * wall + 1e-9, "the sink limit"
        assert boss_wall >= 0.5 * wall - 1e-9, "cracking around an insert"

    def test_freezing_the_diameter_still_adjusts_the_wall(self):
        proposal = plan(frozen=["boss_d"])
        assert change_for(proposal, "boss_d") is None
        assert change_for(proposal, "boss_w") is not None


class TestWhatItWillNotTouch:
    def test_an_undercut_is_a_tooling_decision(self):
        data = load("many_findings")
        data["checks"].append({
            "key": "undercut", "name": "Undercuts / parting line", "status": "fail",
            "severity": "critical", "detail": "Two regions need a slide.",
            "weight": 10, "score_deduction": 10, "metrics": [],
        })
        held = deferral_for(propose(read_report(data), ROLE_MAP, FreezeGuard(),
                                   VALUES, {}), "undercut")
        assert held is not None and held.reason == "decision"
        assert "slide" in held.why or "lifter" in held.why

    def test_a_corner_radius_cannot_be_verified_so_it_is_not_attempted(self):
        """The tool cannot measure a radius from a mesh, so a change to one could
        never be confirmed by re-running it, and a loop that cannot see the
        result of its own change is guessing."""
        data = load("many_findings")
        data["checks"].append({
            "key": "corners", "name": "Corner radii", "status": "warn",
            "severity": "minor", "detail": "Advisory.", "weight": 4,
            "score_deduction": 1, "metrics": [],
        })
        held = deferral_for(propose(read_report(data), ROLE_MAP, FreezeGuard(),
                                   VALUES, {}), "corners")
        assert held is not None and held.reason == "unverifiable"

    def test_a_finding_with_no_rule_is_passed_through_not_dropped(self):
        data = load("many_findings")
        data["checks"].append({
            "key": "something_new", "name": "A check added later", "status": "fail",
            "severity": "critical", "detail": "Whatever it says.", "weight": 9,
            "score_deduction": 9, "metrics": [],
        })
        held = deferral_for(propose(read_report(data), ROLE_MAP, FreezeGuard(),
                                   VALUES, {}), "something_new")
        assert held is not None, "silence would read as 'handled'"
        assert held.finding == "Whatever it says."

    def test_every_finding_is_either_changed_or_accounted_for(self):
        proposal = plan()
        answered = {c.check for c in proposal.changes} | {d.check for d in proposal.deferred}
        report = read_report(load("many_findings"))
        assert {c.key for c in report.findings} <= answered


class TestAnUntrustworthyMesh:
    def test_nothing_is_proposed_at_all(self):
        """A score computed on an inch-scaled mesh is arithmetic. Acting on it
        would change a part for no reason."""
        proposal = plan("inch_scaled")
        assert proposal.changes == ()
        assert proposal.notes and "unusable" in proposal.notes[0]


class TestTheGate:
    def test_the_best_searched_position_is_offered_as_an_input(self):
        proposal = plan()
        assert len(proposal.inputs["gate"]) == 3

    def test_and_is_not_presented_as_a_change_to_the_part(self):
        proposal = plan()
        assert "gate" not in {c.parameter for c in proposal.changes}
        assert "not a change to the part" in proposal.inputs["why"]


class TestGuards:
    def test_an_unknown_role_is_refused(self):
        with pytest.raises(ValueError, match="Unknown DFM role"):
            plan(roles={"wal": "wall_t"})

    def test_every_role_names_a_dfm_setting(self):
        for role, (setting, what) in ROLES.items():
            assert setting and what, role

    def test_a_change_that_changes_nothing_is_not_reported_as_one(self):
        """Otherwise a loop looks like it is progressing while standing still."""
        values = dict(VALUES)
        expressions = {k: f"{v:g}" for k, v in values.items()}
        expressions["rib_t"] = "wall_t * 0.45"
        report = read_report(load("many_findings"))
        proposal = propose(report, ROLE_MAP, FreezeGuard(expressions=expressions),
                           values, expressions)
        assert change_for(proposal, "rib_t") is None
        assert any("already" in note for note in proposal.notes)
