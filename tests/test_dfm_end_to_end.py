"""The loop, closed, against the real analyser -- with no Inventor involved.

Every other test here scripts one half. ``test_dfm_loop.py`` scripts the
analyser to check the loop's bookkeeping; ``test_dfm_targets.py`` runs the real
engine to check the targets. Neither shows the thing actually converging, and
convergence is the claim.

So the export step is replaced by one that writes a mesh built *from the current
parameters* -- a hollow frustum whose wall and draft are whatever the parameter
table says. Everything else is real: the parameters go through
``apply_parameter``, the mesh goes through the DFM tool's own parser, analysis
and rules, and the proposal comes back from the same remediation code. The part
the loop is improving is a mesh generated from its own edits, which is exactly
what Inventor would be doing, only cheaper.

What that catches which nothing else does: a correction with the sign the wrong
way round, a target that moves the mesh somewhere the check still refuses, and a
loop that reports a cleared finding it has not cleared. Skipped without Node or
a checkout of the analyser.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from inventor_mcp.backend.base import ExportRequest
from inventor_mcp.builder import build_part
from inventor_mcp.dfm.loop import current_parameters, improve
from inventor_mcp.dfm.runner import DfmUnavailable, find_dfm_root
from inventor_mcp.schema import PartRecipe

SHAPES = Path(__file__).parent / "dfm_shapes.mjs"

#: Ribs and bosses written as fractions of the wall from the start, so those
#: checks stay clean at any wall and the loop's only work is the wall and the
#: draft. The point here is convergence, not breadth.
RECIPE = {
    "name": "Frustum", "units": "mm",
    "parameters": [
        {"name": "wall_t", "value": 0.6, "comment": "under ABS's 1.2 mm minimum"},
        {"name": "draft_a", "value": 0.2, "unit": "deg", "comment": "under what ABS needs"},
        {"name": "rib_t", "value": "wall_t * 0.45"},
        {"name": "rib_h", "value": "rib_t * 2.5"},
        {"name": "rib_r", "value": "wall_t * 0.3"},
        {"name": "boss_w", "value": "wall_t * 0.6"},
        {"name": "boss_d", "value": "wall_t * 2.4"},
    ],
    "operations": [
        {"op": "sketch", "name": "S", "plane": "xy",
         "entities": [{"type": "rectangle", "center": [0, 0], "width": 40, "height": 40}]},
        {"op": "extrude", "name": "E", "sketch": "S", "distance": 30},
    ],
    "dfm": {
        "parameters": {
            "wall": "wall_t", "draft": "draft_a", "rib_thickness": "rib_t",
            "rib_height": "rib_h", "rib_fillet": "rib_r",
            "boss_od": "boss_d", "boss_wall": "boss_w",
        },
        "settings": {"material": "abs", "surfaceFinish": "spi-a2",
                     "checks": {"flow": False}},
    },
}


@pytest.fixture(scope="module")
def analyser() -> Path:
    if shutil.which("node") is None:
        pytest.skip("no node, so the DFM analyser cannot run")
    try:
        return find_dfm_root()
    except DfmUnavailable as exc:
        pytest.skip(f"{exc.message} {exc.hint or ''}")


@pytest.fixture
def part(session, analyser, monkeypatch, tmp_path):
    """A part whose exported mesh really is a function of its parameters."""
    result = build_part(session, PartRecipe.model_validate(RECIPE))
    assert result["ok"], result["errors"]
    context = session.context()
    exported: list[tuple[float, float]] = []

    def export(doc_id: str, request: ExportRequest) -> dict:
        values, _ = current_parameters(session, context)
        wall, draft = values["wall_t"], values["draft_a"]
        exported.append((wall, draft))
        finished = subprocess.run(
            ["node", str(SHAPES), str(analyser), request.path,
             "hollowFrustum", "20", "30", f"{draft:g}", f"{wall:g}"],
            capture_output=True, text=True, timeout=120,
        )
        assert finished.returncode == 0, finished.stderr
        return {"written": True, "path": request.path, "format": request.format}

    monkeypatch.setattr(session.backend, "export", export)
    monkeypatch.chdir(tmp_path)
    context.exported = exported          # type: ignore[attr-defined]
    return context


@pytest.fixture
def outcome(session, part, tmp_path):
    return improve(session, part, rounds=4, workspace=str(tmp_path / "dfm"))


class TestItConverges:
    def test_the_part_starts_with_a_wall_and_a_draft_finding(self, outcome):
        assert {"wall", "draft"} <= set(outcome.rounds[0].findings), (
            f"the fixture is meant to be wrong: {outcome.rounds[0].findings}")

    def test_the_score_goes_up(self, outcome):
        assert outcome.finished_at > outcome.started_at, (
            f"{outcome.started_at} -> {outcome.finished_at}: "
            f"{outcome.stopped_because}")

    def test_the_wall_finding_actually_clears(self, outcome):
        """Measured on a mesh built to the corrected wall, not asserted."""
        cleared = {key for r in outcome.rounds[1:] for key in r.cleared}
        assert "wall" in cleared, outcome.stopped_because

    def test_and_so_does_the_draft(self, outcome):
        cleared = {key for r in outcome.rounds[1:] for key in r.cleared}
        assert "draft" in cleared, outcome.stopped_because

    def test_it_ends_with_nothing_a_parameter_answers(self, outcome):
        assert "nothing is left" in outcome.stopped_because, outcome.stopped_because

    def test_in_a_couple_of_rounds_rather_than_the_limit(self, outcome):
        assert len(outcome.rounds) <= 3, (
            f"{len(outcome.rounds) - 1} rounds: {outcome.stopped_because}")

    def test_the_grade_improves(self, outcome):
        assert outcome.grade_at_end != outcome.grade_at_start


class TestTheCorrectionGoesTheRightWay:
    def test_a_wall_under_the_minimum_gets_thicker(self, session, part, outcome):
        """The sign. A correction the wrong way round would also 'converge' --
        on a part with no material in it."""
        values, _ = current_parameters(session, part)
        assert values["wall_t"] > 0.6

    def test_and_lands_inside_the_material_band(self, session, part, outcome):
        values, _ = current_parameters(session, part)
        assert 1.2 <= values["wall_t"] <= 3.5, "ABS's band"

    def test_the_mesh_really_did_change_between_rounds(self, part, outcome):
        """Otherwise this is measuring the same part twice and calling it progress."""
        seen = part.exported            # type: ignore[attr-defined]
        assert len(seen) >= 2
        assert seen[0] != seen[-1], seen

    def test_the_ratios_followed_the_wall(self, session, part, outcome):
        """rib_t was written as a fraction of the wall in the recipe, so it must
        have moved with it -- and the ribs check must have stayed clean, or the
        loop would have had more to do."""
        values, _ = current_parameters(session, part)
        assert values["rib_t"] == pytest.approx(values["wall_t"] * 0.45)
        assert "ribs" not in outcome.rounds[-1].findings


class TestKeyGeometryUnderARealRun:
    def test_a_frozen_wall_stops_the_whole_thing_and_says_why(
            self, session, part, tmp_path):
        outcome = improve(session, part, rounds=3, freeze=["wall_t"],
                          workspace=str(tmp_path / "frozen"))
        values, _ = current_parameters(session, part)
        assert values["wall_t"] == pytest.approx(0.6)
        held = [d for d in outcome.outstanding if d.reason == "frozen"]
        assert held and any("wall_t" in d.why for d in held)

    def test_and_the_draft_is_still_fixed_around_it(self, session, part, tmp_path):
        improve(session, part, rounds=3, freeze=["wall_t"],
                workspace=str(tmp_path / "frozen2"))
        values, _ = current_parameters(session, part)
        assert values["draft_a"] > 0.2, "one refusal must not stop the rest"
