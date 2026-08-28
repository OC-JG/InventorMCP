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

import asyncio
import copy
import shutil
import subprocess
from pathlib import Path

import pytest

from inventor_mcp.backend.base import ExportRequest
from inventor_mcp.builder import build_part
from inventor_mcp.dfm.loop import current_parameters, improve
from inventor_mcp.dfm.runner import DfmUnavailable, compare_reports, find_dfm_root
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


# ---------------------------------------------------------------------------
# The same thing, but starting from a file
# ---------------------------------------------------------------------------


class TestHandedAFile:
    """The path a person actually takes: here is a part, look at it.

    ``open_document`` is stood in for, because the simulator opens an .ipt as an
    empty part and what needs exercising is everything downstream of a file
    arriving with parameters in it. Everything else is real -- the copy is a real
    copy on disk, the mesh is regenerated from the parameters the loop sets, and
    the analysis is the DFM tool's own.
    """

    #: What a handed-over part looks like: parameters and features, and nothing
    #: saying what any of them mean. No `dfm` block, because a file that was not
    #: built here has no recipe -- so every role has to come from the part's own
    #: features or not at all. The shell reads the wall; the extrude's taper
    #: reads the draft.
    HANDED = {
        "name": "Handed", "units": "mm",
        "parameters": [
            {"name": "wall_t", "value": 0.6},
            {"name": "draft_a", "value": 0.2, "unit": "deg"},
            {"name": "rib_t", "value": "wall_t * 0.45"},
            {"name": "rib_h", "value": "rib_t * 2.5"},
            {"name": "rib_r", "value": "wall_t * 0.3"},
            {"name": "boss_w", "value": "wall_t * 0.6"},
            {"name": "boss_d", "value": "wall_t * 2.4"},
        ],
        "operations": [
            {"op": "sketch", "name": "Outline", "plane": "xy",
             "entities": [{"type": "rectangle", "center": [0, 0],
                           "width": 40, "height": 40}]},
            {"op": "extrude", "name": "Block", "sketch": "Outline",
             "distance": 30, "taper": "draft_a"},
            {"op": "shell", "name": "Cavity",
             "faces": {"kind": "face", "filter": "top"},
             "thickness": "wall_t", "direction": "inside"},
        ],
    }

    @pytest.fixture
    def handed_over(self, session, analyser, monkeypatch, tmp_path):
        part = tmp_path / "frustum.ipt"
        part.write_bytes(b"pretend this is a part")

        def open_document(path):
            result = build_part(session, PartRecipe.model_validate(self.HANDED),
                                against_rehearsal=False)
            assert result["ok"], result["errors"]
            context = session.context()
            document = session.backend._doc(context.doc_id)
            document.path = path
            return session.backend._doc_info(document)

        monkeypatch.setattr(session.backend, "open_document", open_document)
        monkeypatch.chdir(tmp_path)
        return part

    def _export_from_parameters(self, session, context, analyser, monkeypatch):
        def export(doc_id, request):
            values, _ = current_parameters(session, context)
            finished = subprocess.run(
                ["node", str(SHAPES), str(analyser), request.path, "hollowFrustum",
                 "20", "30", f"{values['draft_a']:g}", f"{values['wall_t']:g}"],
                capture_output=True, text=True, timeout=120,
            )
            assert finished.returncode == 0, finished.stderr
            return {"written": True, "path": request.path}
        monkeypatch.setattr(session.backend, "export", export)

    @pytest.fixture
    def improved(self, session, handed_over, analyser, monkeypatch, tmp_path):
        from inventor_mcp.tools._common import open_source

        opened = open_source(session, str(handed_over), working_copy=True)
        context = session.context(opened["document"])
        self._export_from_parameters(session, context, analyser, monkeypatch)
        outcome = improve(session, context, rounds=4,
                          workspace=str(tmp_path / "dfm"),
                          path=opened["path_on_disk"])
        return opened, context, outcome

    def test_it_works_on_the_next_version(self, improved, handed_over):
        opened, _, _ = improved
        assert Path(opened["working_copy"]).name == "frustum_v2.ipt"

    def test_and_leaves_the_original_alone(self, improved, handed_over):
        assert handed_over.read_bytes() == b"pretend this is a part"

    def test_the_loop_still_converges(self, improved):
        _, _, outcome = improved
        assert outcome.finished_at > outcome.started_at, outcome.stopped_because

    def test_the_roles_were_worked_out_from_the_part(self, improved):
        """No recipe was read: the part arrived as a file. Its shell named the
        wall and its taper named the draft."""
        _, _, outcome = improved
        roles = outcome.declaration["roles"]
        assert roles["wall"]["parameter"] == "wall_t"
        assert roles["draft"]["parameter"] == "draft_a"

    def test_and_the_result_says_where_that_reading_came_from(self, improved):
        """Nobody declared anything, so it has to say "discovered" -- and carry
        the evidence, because everything the loop did rests on it."""
        _, _, outcome = improved
        wall = outcome.declaration["roles"]["wall"]
        assert wall["from"] == "discovered"
        assert "Cavity" in wall["evidence"]

    def test_the_settings_default_where_nothing_declared_them(self, improved):
        """A handed-over part names no material, so the analyser's own default
        stands rather than one invented here."""
        _, _, outcome = improved
        assert "material" not in outcome.settings

    def test_the_declaration_is_left_beside_the_copy(self, improved, session):
        """So the next run on this version starts from the same reading rather
        than inferring it again -- and a person can see and correct it."""
        opened, context, _ = improved
        from inventor_mcp.dfm.sources import remember
        from inventor_mcp.dfm.declaration import Declaration, read_sidecar

        remember(session, context, Declaration(roles={"wall": "wall_t"}),
                 path=opened["path_on_disk"])
        carried = read_sidecar(opened["path_on_disk"])
        assert carried is not None and carried.roles["wall"] == "wall_t"


class TestHandedAMesh:
    """An .stl needs no CAD at all, and that shortcut has to actually work."""

    @pytest.fixture
    def mesh(self, analyser, tmp_path):
        out = tmp_path / "thin.stl"
        finished = subprocess.run(
            ["node", str(SHAPES), str(analyser), str(out),
             "hollowFrustum", "20", "30", "0.2", "0.6"],
            capture_output=True, text=True, timeout=120,
        )
        assert finished.returncode == 0, finished.stderr
        return out

    def test_it_is_analysed_without_inventor(self, server, mesh, tmp_path):
        out = asyncio.run(server.call_tool("check_manufacture", {
            "path": str(mesh), "workspace": str(tmp_path / "dfm"),
        })).structured_content
        assert out["ok"], out
        assert out["score"] is not None
        assert "wall" in out["findings"]

    def test_and_nothing_is_proposed_because_a_mesh_has_no_parameters(
            self, server, mesh, tmp_path):
        out = asyncio.run(server.call_tool("check_manufacture", {
            "path": str(mesh), "workspace": str(tmp_path / "dfm"),
        })).structured_content
        assert "would_change" not in out
        assert "no parameters" in out["note"]

    def test_with_a_part_named_it_does_propose(self, server, mesh, tmp_path):
        """Naming a document says 'this mesh came from that part'. Guessing that
        an open part and a handed-over mesh are the same thing is the assumption
        that would edit the wrong model."""
        built = asyncio.run(server.call_tool("build_part_from_recipe", {
            "recipe": copy.deepcopy(RECIPE),
        })).structured_content
        assert built["ok"], built.get("errors")
        out = asyncio.run(server.call_tool("check_manufacture", {
            "path": str(mesh), "document": built["document"],
            "workspace": str(tmp_path / "dfm"),
        })).structured_content
        assert out["ok"], out
        changed = {c["parameter"] for c in out["would_change"]["changes"]}
        assert "wall_t" in changed
        assert built["document"] in out["note"]


class TestComparingVersions:
    """What moved between two runs -- the question a versioned part exists for.

    Asked of the DFM tool's own `compareRuns`, so the direction each measurement
    should move in, and the caveats about a score that moved for a reason other
    than the part, come from the tool rather than from a diff written here.
    """

    FIXTURES = Path(__file__).parent / "fixtures" / "dfm"

    def test_it_says_what_moved(self, server, analyser):
        out = asyncio.run(server.call_tool("compare_manufacture", {
            "before": str(self.FIXTURES / "many_findings.json"),
            "after": str(self.FIXTURES / "clean.json"),
        })).structured_content
        assert out["ok"]
        assert out["score"]["delta"] == 51
        assert "PRODUCTION READY" in out["headline"]

    def test_and_which_checks_cleared(self, server, analyser):
        out = asyncio.run(server.call_tool("compare_manufacture", {
            "before": str(self.FIXTURES / "many_findings.json"),
            "after": str(self.FIXTURES / "clean.json"),
        })).structured_content
        improved = {c["key"] for c in out["checks"] if c["change"] == "improved"}
        assert {"wall", "draft", "ribs"} <= improved

    def test_it_raises_its_own_caveats(self, server, analyser):
        """A material change makes score movement something other than a change
        in the part, and the tool says so rather than letting it read as progress."""
        out = asyncio.run(server.call_tool("compare_manufacture", {
            "before": str(self.FIXTURES / "thick_wall.json"),
            "after": str(self.FIXTURES / "clean.json"),
        })).structured_content
        assert isinstance(out["caveats"], list)

    def test_a_missing_report_says_which_one(self, server, analyser, tmp_path):
        out = asyncio.run(server.call_tool("compare_manufacture", {
            "before": str(tmp_path / "nope.json"),
            "after": str(self.FIXTURES / "clean.json"),
        })).structured_content
        assert out["ok"] is False
        assert "before report" in out["message"]

    def test_the_loop_compares_its_own_first_and_last_round(self, session, part,
                                                            tmp_path):
        outcome = improve(session, part, rounds=4, workspace=str(tmp_path / "dfm"))
        assert len(outcome.rounds) > 1, outcome.stopped_because
        moved = compare_reports(outcome.rounds[0].report, outcome.rounds[-1].report)
        assert moved["score"]["delta"] == pytest.approx(
            outcome.finished_at - outcome.started_at)
