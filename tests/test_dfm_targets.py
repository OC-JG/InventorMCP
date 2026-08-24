"""Do the targets this project aims at actually satisfy the tool's own checks?

This is the alarm for the one duplication in the integration.

The DFM tool states its thresholds as literals inside its rules -- "0.8x
ceiling", "below ABS minimum (1.2 mm)" -- and does not export them. So
:mod:`inventor_mcp.dfm.remedy` holds its own view of where inside each band to
aim, and that view can drift out of agreement with the check it is trying to
satisfy. When it does, the loop applies a change, the finding does not clear, and
the loop says so -- but nobody wants to find out that way.

So each target is put through the real engine, on a mesh with a known answer,
and the check is required to come back clean. If a threshold moves in the DFM
tool, this fails and names the check.

Skipped without Node or a checkout of the analyser. That is a real gap in
coverage rather than a tidy one, so it is reported as skipped with the reason
rather than quietly passing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from inventor_mcp.dfm.freeze import FreezeGuard
from inventor_mcp.dfm.remedy import propose
from inventor_mcp.dfm.report import read_report
from inventor_mcp.dfm.runner import BRIDGE, DfmUnavailable, analyse_stl, find_dfm_root

FIXTURES = Path(__file__).parent / "fixtures" / "dfm"
SHAPES = Path(__file__).parent / "dfm_shapes.mjs"

ROLE_MAP = {
    "wall": "wall_t", "draft": "draft_a", "rib_thickness": "rib_t",
    "rib_height": "rib_h", "rib_fillet": "rib_r", "boss_od": "boss_d",
    "boss_wall": "boss_w",
}
VALUES = {"wall_t": 2.0, "draft_a": 0.2, "rib_t": 1.9, "rib_h": 9.0,
          "rib_r": 0.05, "boss_d": 6.0, "boss_w": 0.6}


@pytest.fixture(scope="module")
def analyser() -> Path:
    import shutil
    if shutil.which("node") is None:
        pytest.skip("no node, so the DFM analyser cannot run")
    try:
        return find_dfm_root()
    except DfmUnavailable as exc:
        pytest.skip(f"{exc.message} {exc.hint or ''}")


@pytest.fixture(scope="module")
def proposal():
    report = read_report(json.loads(
        (FIXTURES / "many_findings.json").read_text(encoding="utf-8")))
    expressions = {k: f"{v:g}" for k, v in VALUES.items()}
    return propose(report, ROLE_MAP, FreezeGuard(expressions=expressions),
                   VALUES, expressions)


def target_for(proposal, parameter: str) -> float:
    for change in proposal.changes:
        if change.parameter == parameter:
            assert change.target is not None, f"{parameter} has no numeric target"
            return change.target
    raise AssertionError(f"nothing proposed for {parameter}")


#: Which declared DFM setting each parameter in the fixture supplies.
SUPPLIES = {"wall_t": "wallThk", "rib_t": "ribThk", "rib_h": "ribH",
            "rib_r": "ribRadius", "boss_d": "bossOD", "boss_w": "bossWall"}


def declared_after(proposal) -> dict:
    """The declared inputs the model would supply once the proposal is applied.

    Built from what was actually proposed rather than from a fixed list, because
    a rule declining to change something is a legitimate answer -- the boss
    diameter stays put whenever thickening the wall resolves the bind on its own,
    and a test that insisted on a boss change would be asserting the old bug.
    """
    after = {setting: VALUES[name] for name, setting in SUPPLIES.items()}
    for change in proposal.changes:
        setting = SUPPLIES.get(change.parameter)
        if setting and change.target is not None:
            after[setting] = change.target
    return after


def shape(analyser: Path, out: Path, name: str, *args: float) -> Path:
    finished = subprocess.run(
        ["node", str(SHAPES), str(analyser), str(out), name,
         *(f"{a:g}" for a in args)],
        capture_output=True, text=True, timeout=120,
    )
    assert finished.returncode == 0, finished.stderr
    return out


def only(*keys: str) -> dict:
    """Run just these checks, so one target is judged on its own."""
    every = ("wall", "draft", "ribs", "undercut", "sink", "warp",
             "transitions", "flow", "fpc")
    return {"checks": {key: key in keys for key in every}}


def deduction(report, key: str) -> float:
    check = report.check(key)
    assert check is not None, f"the {key} check did not run"
    return check.deduction


class TestTheBridgeIsFaithful:
    """Before trusting any target, check the bridge gets the tool's own answer."""

    def test_it_reproduces_a_number_the_tool_asserts_itself(self, analyser, tmp_path):
        """``test/unit.mjs`` asserts this fixture scores 100 out of a budget of
        100 with clean inputs. Anything else means the bridge is not running the
        analysis the tool runs."""
        stl = shape(analyser, tmp_path / "frustum.stl", "hollowFrustum", 20, 30, 3, 2)
        report = analyse_stl(stl, {
            "material": "abs", "wallThk": 2.0, "wallMin": 1.6, "wallMax": 2.4,
            "draftAngle": 3.0, "ribThk": 0.9, "ribH": 2.0, "ribRadius": 0.5,
            "bossOD": 4.0, "bossWall": 1.0, "surfaceFinish": "spi-a2",
        })
        assert report.score == 100
        assert report.budget == 100

    def test_the_bridge_ships_next_to_the_runner(self):
        assert BRIDGE.is_file(), "headless.mjs must be packaged with the module"


class TestTheWallTarget:
    def test_the_proposed_wall_clears_the_wall_check(self, analyser, tmp_path, proposal):
        """The measured wall this would produce, on a mesh built to it."""
        aimed = 0.5 + (target_for(proposal, "wall_t") - VALUES["wall_t"])
        stl = shape(analyser, tmp_path / "walled.stl",
                    "hollowFrustum", 20, 30, 3, aimed)
        report = analyse_stl(stl, {"material": "abs", "wallThk": aimed, **only("wall")})
        assert deduction(report, "wall") == 0, report.check("wall").detail

    def test_and_sitting_exactly_on_the_floor_would_not(self, analyser, tmp_path):
        """Why the target carries a margin rather than being the floor itself:
        the check calls a wall on its minimum 'no margin for variation'."""
        stl = shape(analyser, tmp_path / "onlimit.stl",
                    "hollowFrustum", 20, 30, 3, 1.2)
        report = analyse_stl(stl, {"material": "abs", "wallThk": 1.2, **only("wall")})
        assert deduction(report, "wall") > 0, (
            "the floor itself passes, so the 10% margin is unnecessary -- simplify")


class TestTheDraftTarget:
    def test_the_proposed_draft_clears_the_draft_check(self, analyser, tmp_path, proposal):
        aimed = target_for(proposal, "draft_a")
        stl = shape(analyser, tmp_path / "drafted.stl",
                    "hollowFrustum", 20, 30, aimed, 2)
        report = analyse_stl(stl, {
            "material": "abs", "draftAngle": aimed, "surfaceFinish": "spi-c1",
            **only("draft"),
        })
        assert deduction(report, "draft") == 0, report.check("draft").detail

    def test_the_required_angle_alone_would_not(self, analyser, tmp_path):
        """1.10 deg is required and 1.10 deg is 'no margin' -- which is why the
        target is required + 1, the figure the check prints as its own advice."""
        stl = shape(analyser, tmp_path / "bare.stl", "hollowFrustum", 20, 30, 1.1, 2)
        report = analyse_stl(stl, {
            "material": "abs", "draftAngle": 1.1, "surfaceFinish": "spi-c1",
            **only("draft"),
        })
        assert deduction(report, "draft") > 0


class TestTheRibAndBossTargets:
    """These the check reads directly, so the same mesh can carry all of them."""

    def test_every_proposed_ratio_clears_the_ribs_check(self, analyser, tmp_path, proposal):
        stl = shape(analyser, tmp_path / "any.stl", "hollowFrustum", 20, 30, 3, 2)
        report = analyse_stl(stl, {
            "material": "abs", **declared_after(proposal), **only("ribs"),
        })
        assert deduction(report, "ribs") == 0, report.check("ribs").detail

    def test_the_boss_keeps_its_size_when_the_wall_alone_resolves_it(
            self, analyser, tmp_path, proposal):
        """A Ø6 boss is too wide for a 2 mm wall and comfortable on the 2.82 mm
        one this pass is already setting, so narrowing it would change function
        for nothing. Asserted against the live rules, not just the intent."""
        assert "boss_d" not in {c.parameter for c in proposal.changes}
        stl = shape(analyser, tmp_path / "kept.stl", "hollowFrustum", 20, 30, 3, 2)
        report = analyse_stl(stl, {
            "material": "abs", **declared_after(proposal), **only("ribs"),
        })
        assert report.declared_number("bossOD") == 6.0
        assert deduction(report, "ribs") == 0

    def test_and_the_values_it_started_from_did_not(self, analyser, tmp_path):
        stl = shape(analyser, tmp_path / "any2.stl", "hollowFrustum", 20, 30, 3, 2)
        report = analyse_stl(stl, {
            "material": "abs", "wallThk": VALUES["wall_t"],
            "ribThk": VALUES["rib_t"], "ribH": VALUES["rib_h"],
            "ribRadius": VALUES["rib_r"], "bossOD": VALUES["boss_d"],
            "bossWall": VALUES["boss_w"], **only("ribs"),
        })
        assert deduction(report, "ribs") > 0, "the fixture is supposed to be bad"

    def test_the_boss_pair_resolves_a_bind_the_boss_wall_alone_cannot(
            self, analyser, tmp_path, proposal):
        """A 6 mm boss on a 2 mm wall satisfies neither guideline at any boss
        wall. Adjusting only the boss wall must therefore still fail -- which is
        why the diameter is what moves."""
        stl = shape(analyser, tmp_path / "any3.stl", "hollowFrustum", 20, 30, 3, 2)
        report = analyse_stl(stl, {
            "material": "abs", "wallThk": 2.0,
            "ribThk": 0.9, "ribH": 2.0, "ribRadius": 0.6,
            "bossOD": 6.0, "bossWall": 1.2,     # 0.6x the wall, and still short
            **only("ribs"),
        })
        assert deduction(report, "ribs") > 0
        assert "cannot satisfy both" in report.check("ribs").detail


class TestSettingsAreCheckedNotIgnored:
    def test_a_misspelled_setting_is_refused(self, analyser, tmp_path):
        """It would otherwise score silently at the tool's default, and a wall
        judged as 2.0 mm when the model says 0.8 is a wrong answer that looks
        right."""
        stl = shape(analyser, tmp_path / "any4.stl", "hollowFrustum", 20, 30, 3, 2)
        with pytest.raises(Exception, match="[Uu]nknown"):
            analyse_stl(stl, {"material": "abs", "wallThikness": 2.0})

    def test_an_unknown_material_is_refused(self, analyser, tmp_path):
        stl = shape(analyser, tmp_path / "any5.stl", "hollowFrustum", 20, 30, 3, 2)
        with pytest.raises(Exception, match="[Uu]nknown material"):
            analyse_stl(stl, {"material": "unobtainium"})
