"""Handing over a file, rather than building the part here.

This is the path a person actually takes: they have a part, they want it looked
at. Everything the loop needs -- which parameter is the wall, what may not be
touched -- has to come from the part itself or be asked for, because there is no
recipe to read.

Three things are being tested. That the right thing happens per format: an .ipt
opens, a STEP file imports and says it cannot be driven, a mesh is refused with
somewhere better to go. That the original file is never written. And that what
was worked out about a part is still there next time.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from inventor_mcp.builder import apply_operation, build_part
from inventor_mcp.dfm.declaration import read_sidecar, sidecar_for
from inventor_mcp.dfm.sources import discover_for, resolve
from inventor_mcp.schema import ExtrudeOp, PartRecipe, ShellOp, SketchOp
from inventor_mcp.versioning import versions_of

SHAPES = Path(__file__).parent / "dfm_shapes.mjs"


def call(server, tool, /, **arguments):
    """Positional-only, so a tool argument called `name` is not shadowed."""
    return asyncio.run(server.call_tool(tool, arguments)).structured_content


@pytest.fixture
def files(tmp_path):
    """A part, a STEP file and a mesh, all of them just bytes on disk."""
    for name in ("bracket.ipt", "bracket.stp", "bracket.stl", "bracket.igs"):
        (tmp_path / name).write_bytes(b"not really CAD, and does not need to be")
    return tmp_path


# ---------------------------------------------------------------------------
# What happens per format
# ---------------------------------------------------------------------------


class TestOpeningWhatWasHandedOver:
    def test_a_part_opens(self, server, files):
        out = call(server, "open_part", path=str(files / "bracket.ipt"))
        assert out["ok"] and out["document"]

    def test_a_step_file_is_imported(self, server, files):
        out = call(server, "open_part", path=str(files / "bracket.stp"))
        assert out["ok"] and out["detail"]["imported"] is True

    def test_and_says_it_cannot_be_driven(self, server, files):
        """Translated geometry carries no history, so there are no parameters.
        Saying so beats a loop that reports nothing to do and sounds like success."""
        out = call(server, "open_part", path=str(files / "bracket.stp"))
        assert out["parametric"] is False
        assert "no user parameters" in out["what_that_means"]

    def test_iges_too(self, server, files):
        out = call(server, "open_part", path=str(files / "bracket.igs"))
        assert out["ok"] and out["detail"]["imported"] is True

    def test_a_mesh_is_refused_with_somewhere_better(self, server, files):
        out = call(server, "open_part", path=str(files / "bracket.stl"))
        assert out["ok"] is False
        assert "check_manufacture" in out["hint"]

    def test_a_missing_file_says_so(self, server, files):
        out = call(server, "open_part", path=str(files / "nope.ipt"))
        assert out["ok"] is False
        assert "no file at" in out["message"]

    def test_a_relative_path_is_made_absolute(self, server, files, monkeypatch):
        monkeypatch.chdir(files)
        out = call(server, "open_part", path="bracket.ipt")
        assert out["opened"] == str(files / "bracket.ipt")


class TestTheWorkingCopy:
    def test_it_opens_the_next_version(self, server, files):
        out = call(server, "open_part", path=str(files / "bracket.ipt"),
                   working_copy=True)
        assert Path(out["working_copy"]).name == "bracket_v2.ipt"

    def test_which_exists(self, server, files):
        call(server, "open_part", path=str(files / "bracket.ipt"), working_copy=True)
        assert (files / "bracket_v2.ipt").is_file()

    def test_and_the_original_is_untouched(self, server, files):
        before = (files / "bracket.ipt").read_bytes()
        call(server, "open_part", path=str(files / "bracket.ipt"), working_copy=True)
        assert (files / "bracket.ipt").read_bytes() == before

    def test_the_result_says_which_file_is_which(self, server, files):
        out = call(server, "open_part", path=str(files / "bracket.ipt"),
                   working_copy=True)
        assert out["original_untouched"] == str(files / "bracket.ipt")
        assert out["working_copy"] != out["original_untouched"]

    def test_a_second_run_does_not_reuse_the_name(self, server, files):
        first = call(server, "open_part", path=str(files / "bracket.ipt"),
                     working_copy=True)["working_copy"]
        second = call(server, "open_part", path=str(files / "bracket.ipt"),
                      working_copy=True)["working_copy"]
        assert first != second

    def test_a_translated_file_needs_none_and_says_why(self, server, files):
        out = call(server, "open_part", path=str(files / "bracket.stp"),
                   working_copy=True)
        assert "working_copy" not in out
        assert "read and never written" in out["note"]


# ---------------------------------------------------------------------------
# Working out what the part means
# ---------------------------------------------------------------------------


def shelled_part(session, *, wall="2.5", draft="1.5"):
    """A part with a shell and a drafted extrude, and no recipe behind it.

    Built through the operations rather than from a recipe on purpose: this is
    the shape of a part somebody hands over, where the only thing that knows
    which parameter is the wall is the shell that reads it.
    """
    backend = session.ensure_backend()
    info = backend.new_part("Handed", units="mm")
    context = session.register(info, "mm", "deg")
    backend.set_parameter(context.doc_id, "wall_t", wall, units="mm")
    backend.set_parameter(context.doc_id, "draft_a", draft, units="deg")
    backend.set_parameter(context.doc_id, "rib_t", "1.1", units="mm")
    session.sync_parameters(context.doc_id)
    apply_operation(session, context, SketchOp(
        name="Outline", plane="xy",
        entities=[{"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]))
    apply_operation(session, context, ExtrudeOp(
        name="Block", sketch="Outline", distance=30, taper="draft_a"))
    apply_operation(session, context, ShellOp(
        name="Cavity", faces={"kind": "face", "filter": "top"},
        thickness="wall_t", direction="inside"))
    return context


class TestDiscoveryOnAPartNobodyDescribed:
    def test_the_shell_names_the_wall(self, session):
        found = discover_for(session, shelled_part(session))
        assert found.declaration.roles["wall"] == "wall_t"

    def test_the_taper_names_the_draft(self, session):
        found = discover_for(session, shelled_part(session))
        assert found.declaration.roles["draft"] == "draft_a"

    def test_with_the_evidence_recorded(self, session):
        found = discover_for(session, shelled_part(session))
        assert "Cavity" in found.declaration.evidence["wall"]
        assert "Block" in found.declaration.evidence["draft"]

    def test_and_marked_as_inferred_rather_than_stated(self, session):
        declaration, _ = resolve(session, shelled_part(session))
        assert declaration.origin["wall"] == "discovered"

    def test_a_rib_is_only_suggested(self, session):
        """Nothing reads rib_t, so its role rests on its name -- offered, not used."""
        found = discover_for(session, shelled_part(session))
        assert found.suggestions.get("rib_thickness") == "rib_t"
        assert "rib_thickness" not in found.declaration.roles

    def test_what_is_stated_beats_what_is_inferred(self, session):
        context = shelled_part(session)
        declaration, _ = resolve(session, context, roles={"wall": "rib_t"})
        assert declaration.roles["wall"] == "rib_t"

    def test_and_the_disagreement_is_reported(self, session):
        """The recipe or the caller wins either way. Somebody should still know
        the part's only shell reads a different parameter."""
        context = shelled_part(session)
        declaration, _ = resolve(session, context, roles={"wall": "rib_t"})
        assert any("wall_t" in note for note in declaration.notes)

    def test_turning_inference_off_leaves_it_unmapped(self, session):
        declaration, found = resolve(session, shelled_part(session), infer=False)
        assert "wall" not in declaration.roles
        assert found is None


class TestDiscoveryThroughTheTool:
    def test_it_reports_what_the_part_says(self, server, session):
        # Built through the server so the tool and the model share a session.
        call(server, "new_part", name="Handed")
        out = call(server, "set_parameters", parameters=[
            {"name": "wall_t", "value": 2.5}], rebuild=False)
        assert out["ok"]
        out = call(server, "apply_operations", operations=[
            {"op": "sketch", "name": "Outline", "plane": "xy",
             "entities": [{"type": "rectangle", "center": [0, 0],
                           "width": 60, "height": 40}]},
            {"op": "extrude", "name": "Block", "sketch": "Outline", "distance": 30},
            {"op": "shell", "name": "Cavity",
             "faces": {"kind": "face", "filter": "top"},
             "thickness": "wall_t", "direction": "inside"},
        ])
        assert out["ok"], out
        found = call(server, "discover_dfm_roles")
        assert found["from_the_part"]["roles"]["wall"]["parameter"] == "wall_t"
        assert "Cavity" in found["from_the_part"]["roles"]["wall"]["evidence"]

    def test_and_lists_the_roles_it_knows(self, server):
        call(server, "new_part", name="Empty")
        found = call(server, "discover_dfm_roles")
        assert "wall" in found["roles"]


# ---------------------------------------------------------------------------
# Remembering it
# ---------------------------------------------------------------------------


class TestRememberingTheDeclaration:
    def test_declaring_writes_it_into_the_part(self, server):
        call(server, "new_part", name="Housing")
        call(server, "set_parameters",
             parameters=[{"name": "wall_t", "value": 2.5},
                         {"name": "bore_d", "value": 8}], rebuild=False)
        out = call(server, "declare_dfm", roles={"wall": "wall_t"},
                   frozen=["bore_d"], material="abs")
        assert out["ok"]
        assert out["remembered"]["in_the_part"] is True

    def test_and_it_reads_back(self, server, session):
        call(server, "new_part", name="Housing")
        call(server, "set_parameters", parameters=[{"name": "wall_t", "value": 2.5}],
             rebuild=False)
        call(server, "declare_dfm", roles={"wall": "wall_t"}, frozen=["wall_t"])
        found = call(server, "discover_dfm_roles")
        assert found["what_would_be_used"]["roles"]["wall"]["from"] == "the part itself"

    def test_a_declared_freeze_is_enforced_immediately(self, server):
        """Not only inside the next loop. A guarantee that waits for a loop is
        one anything else walks through."""
        call(server, "new_part", name="Housing")
        call(server, "set_parameters", parameters=[{"name": "bore_d", "value": 8}],
             rebuild=False)
        call(server, "declare_dfm", frozen=["bore_d"])
        out = call(server, "set_parameters",
                   parameters=[{"name": "bore_d", "value": 9}], rebuild=False)
        assert out["ok"] is False and out["error"] == "frozen_geometry"

    def test_a_name_that_is_not_a_parameter_is_flagged(self, server):
        call(server, "new_part", name="Housing")
        call(server, "set_parameters", parameters=[{"name": "wall_t", "value": 2.5}],
             rebuild=False)
        out = call(server, "declare_dfm", roles={"wall": "wal_t"})
        assert out["not_a_parameter_of_this_part"] == ["wal_t"]
        assert "wall_t" in out["note"]

    def test_the_sidecar_survives_the_document(self, session, tmp_path):
        """The part is closed and the file reopened, which in the simulator is a
        different document entirely. What was worked out is still there."""
        from inventor_mcp.dfm.declaration import Declaration
        from inventor_mcp.dfm.sources import remember

        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"")
        context = shelled_part(session)
        session.backend.save_document(context.doc_id, str(part))
        remember(session, context,
                 Declaration(roles={"wall": "wall_t"}, frozen=["bore_d"]))

        assert sidecar_for(part).is_file()
        # Actually closed, not merely reopened: an open file now comes back as
        # the document it already is, so the only way to lose the in-memory
        # state -- which is what this test is about -- is to close it first.
        session.backend.close_document(context.doc_id, save=False)
        session.forget(context.doc_id)
        again = session.backend.open_document(str(part))
        second = session.register(again, "mm", "deg")
        declaration, _ = resolve(session, second, path=part)
        assert declaration.roles["wall"] == "wall_t"
        assert "bore_d" in declaration.frozen
        assert declaration.origin["wall"] == "a sidecar file"

    def test_and_travels_to_the_working_copy(self, server, session, tmp_path):
        """A versioned copy that had forgotten which parameter is the wall would
        rediscover it, and might discover something else."""
        from inventor_mcp.dfm.declaration import Declaration, write_sidecar

        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"")
        write_sidecar(part, Declaration(roles={"wall": "wall_t"}, frozen=["bore_d"]))
        out = call(server, "open_part", path=str(part), working_copy=True)
        copy_path = Path(out["working_copy"])
        assert sidecar_for(copy_path).is_file()
        carried = read_sidecar(copy_path)
        assert carried.roles == {"wall": "wall_t"}
        assert carried.frozen == ["bore_d"]


# ---------------------------------------------------------------------------
# What the loop does with a file
# ---------------------------------------------------------------------------


class TestImprovingAFile:
    def test_a_part_with_no_parameters_is_refused_helpfully(self, server, files):
        """The simulator opens an .ipt as an empty part, which is exactly the
        shape of the real problem: imported geometry with nothing to drive."""
        out = call(server, "improve_for_manufacture", path=str(files / "bracket.ipt"))
        assert out["ok"] is False
        assert out["error"] == "nothing_to_drive"
        assert "recipe" in out["hint"]

    def test_it_says_which_file_it_looked_at(self, server, files):
        out = call(server, "improve_for_manufacture", path=str(files / "bracket.ipt"))
        assert out["file"]["opened"] == str(files / "bracket.ipt")

    def test_a_step_file_is_refused_the_same_way(self, server, files):
        out = call(server, "improve_for_manufacture", path=str(files / "bracket.stp"))
        assert out["ok"] is False and out["error"] == "nothing_to_drive"

    def test_the_refusal_still_leaves_the_part_open_to_be_measured(self, server, files):
        call(server, "improve_for_manufacture", path=str(files / "bracket.stp"))
        status = call(server, "session_status")
        assert status["documents"]


class TestCapabilities:
    def test_it_says_which_formats_it_takes(self, server):
        out = call(server, "dfm_capabilities")
        assert ".ipt" in out["accepts"]
        assert ".stp" in out["accepts"]["translated"]
        assert ".stl" in out["accepts"]

    def test_and_that_only_a_parametric_part_can_be_improved(self, server):
        out = call(server, "dfm_capabilities")
        assert "measurable, not improvable" in out["accepts"]["translated_note"]

    def test_and_how_a_role_gets_settled(self, server):
        out = call(server, "dfm_capabilities")
        order = out["how_roles_are_settled"]
        assert order[0].startswith("what you say")
        assert "evidence, never a name" in order[-1]


class TestReopeningWhatIsAlreadyOpen:
    """Re-registering a document must keep what the session learned about it.

    Handing a path to a tool for a part that is already on screen used to replace
    its context outright, silently dropping the recipe, the sketch plans and --
    worst -- the freeze guard protecting its key geometry. A second open turned a
    protected part into an unprotected one and said nothing about it.
    """

    def test_the_recipe_survives(self, session, tmp_path):
        from inventor_mcp.schema import PartRecipe

        recipe = PartRecipe.model_validate({
            "name": "Housing", "units": "mm",
            "parameters": [{"name": "wall_t", "value": 2.5}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy",
                 "entities": [{"type": "rectangle", "center": [0, 0],
                               "width": 40, "height": 30}]},
                {"op": "extrude", "name": "E", "sketch": "S", "distance": 20},
            ],
            "dfm": {"parameters": {"wall": "wall_t"}, "frozen": ["wall_t"]},
        })
        build_part(session, recipe)
        context = session.context()
        info = session.backend.list_documents()[0]
        again = session.register(info, "mm", "deg")
        assert again is context
        assert again.recipe is not None
        assert again.recipe["dfm"]["parameters"] == {"wall": "wall_t"}

    def test_and_so_does_the_freeze(self, session):
        from inventor_mcp.dfm.freeze import FrozenGeometryError
        from inventor_mcp.schema import ParameterSpec, PartRecipe

        build_part(session, PartRecipe.model_validate({
            "name": "Housing", "units": "mm",
            "parameters": [{"name": "bore_d", "value": 8, "frozen": True}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy",
                 "entities": [{"type": "circle", "center": [0, 0],
                               "diameter": "bore_d"}]},
                {"op": "extrude", "name": "E", "sketch": "S", "distance": 20},
            ],
        }))
        info = session.backend.list_documents()[0]
        again = session.register(info, "mm", "deg")
        assert again.frozen is not None
        with pytest.raises(FrozenGeometryError):
            apply_operation  # keep the import honest
            from inventor_mcp.builder import apply_parameter
            apply_parameter(session, again, ParameterSpec(name="bore_d", value=9))

    def test_the_units_are_refreshed(self, session):
        from inventor_mcp.schema import PartRecipe

        build_part(session, PartRecipe.model_validate({
            "name": "Housing", "units": "mm",
            "parameters": [{"name": "w", "value": 2}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy",
                 "entities": [{"type": "circle", "center": [0, 0], "diameter": 10}]},
                {"op": "extrude", "name": "E", "sketch": "S", "distance": "w"},
            ],
        }))
        info = session.backend.list_documents()[0]
        again = session.register(info, "in", "rad")
        assert again.units == "in" and again.angle_units == "rad"
        assert again.resolver.length_unit == "in"

    def test_a_different_document_gets_its_own_context(self, session):
        first = session.backend.new_part("First")
        second = session.backend.new_part("Second")
        assert session.register(first, "mm", "deg") is not session.register(
            second, "mm", "deg")


class TestNotWritingOverSomebodysPart:
    """Saving over the file somebody named is a different act from saving the
    copy this made, and it must not happen by default."""

    def test_working_on_the_original_does_not_save_it(self, server, session, files,
                                                      monkeypatch):
        saved: list[str] = []
        monkeypatch.setattr(
            type(session.backend), "save_document",
            lambda self, doc_id, path=None: saved.append(path or "in place"),
            raising=False,
        )
        out = call(server, "improve_for_manufacture",
                   path=str(files / "bracket.ipt"), working_copy=False)
        # The mock part has no parameters, so it refuses before the loop -- and
        # that is exactly the path where a stray save would be worst.
        assert out["ok"] is False
        assert saved == []

    def test_the_refusal_points_at_the_orphaned_copy(self, server, files):
        out = call(server, "improve_for_manufacture",
                   path=str(files / "bracket.ipt"), working_copy=True)
        assert out["ok"] is False
        assert out["file"]["working_copy"] in out["hint"]

    def test_and_says_the_document_is_still_open_to_be_measured(self, server, files):
        out = call(server, "improve_for_manufacture", path=str(files / "bracket.stp"))
        assert "still open" in out["hint"]


class TestAFreezeThatArrivesWithTheFile:
    """Protection declared beside a part holds from the moment it is opened.

    The freeze is enforced where parameters change. Without installing it at
    open, a sidecar saying bore_d is key geometry would hold inside the DFM loop
    -- which resolves the declaration for itself -- and be walked straight
    through by the next `set_parameters`, with every report still saying the
    freeze was honoured.
    """

    def _part_with_sidecar(self, tmp_path):
        from inventor_mcp.dfm.declaration import Declaration, write_sidecar

        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"")
        write_sidecar(part, Declaration(frozen=["bore_d"]))
        return part

    def test_set_parameters_is_refused(self, server, tmp_path):
        part = self._part_with_sidecar(tmp_path)
        opened = call(server, "open_part", path=str(part))
        assert opened["ok"]
        assert "bore_d" in opened["key_geometry"]["declared"]
        out = call(server, "set_parameters",
                   parameters=[{"name": "bore_d", "value": 9}], rebuild=False)
        assert out["ok"] is False
        assert out["error"] == "frozen_geometry"

    def test_the_override_still_exists(self, server, tmp_path):
        part = self._part_with_sidecar(tmp_path)
        call(server, "open_part", path=str(part))
        out = call(server, "set_parameters",
                   parameters=[{"name": "bore_d", "value": 9}],
                   rebuild=False, override_frozen=True)
        assert out["ok"]

    def test_a_part_with_no_declaration_installs_nothing(self, server, tmp_path):
        part = tmp_path / "plain.ipt"
        part.write_bytes(b"")
        opened = call(server, "open_part", path=str(part))
        assert "key_geometry" not in opened

    def test_an_unreadable_sidecar_is_reported_not_dropped(self, server, tmp_path):
        """Somebody wrote it on purpose. Running as though it were absent would
        ignore whatever it protects."""
        from inventor_mcp.dfm.declaration import sidecar_for

        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"")
        sidecar_for(part).write_text("{ not json", encoding="utf-8")
        opened = call(server, "open_part", path=str(part))
        assert opened["ok"]
        assert "could not be read" in opened["declaration_problem"]


class TestReviewFindings:
    """Behaviours pinned after an adversarial review of the file-driven work.

    Each of these was a real defect: the test names the harm, not the code.
    """

    def test_declare_dfm_widens_the_guard_rather_than_replacing_it(self, server):
        """A freeze added with protect_geometry lives only on the context, and
        rebuilding the guard from the declaration alone dropped it -- the one
        operation no source is allowed to perform, done by accident."""
        call(server, "new_part", name="Housing")
        call(server, "set_parameters", parameters=[
            {"name": "bore_d", "value": 8}, {"name": "wall_t", "value": 2.5}],
            rebuild=False)
        call(server, "protect_geometry", parameters=["bore_d"])
        call(server, "declare_dfm", roles={"wall": "wall_t"}, frozen=["wall_t"])
        out = call(server, "set_parameters",
                   parameters=[{"name": "bore_d", "value": 9}], rebuild=False)
        assert out["ok"] is False and out["error"] == "frozen_geometry"

    def test_protect_geometry_remember_keeps_the_stored_roles(self, server, session):
        """`remember` replaces the stored declaration whole, so writing only the
        freeze erased the roles and material somebody declared earlier."""
        call(server, "new_part", name="Housing")
        call(server, "set_parameters", parameters=[
            {"name": "wall_t", "value": 2.5}, {"name": "bore_d", "value": 8}],
            rebuild=False)
        call(server, "declare_dfm", roles={"wall": "wall_t"}, material="abs")
        call(server, "protect_geometry", parameters=["bore_d"], remember_it=True)
        found = call(server, "discover_dfm_roles")
        used = found["what_would_be_used"]
        assert used["roles"]["wall"]["parameter"] == "wall_t", "roles survived"
        assert used["settings"].get("material") == "abs", "settings survived"
        assert "bore_d" in used["frozen"], "and the new freeze is there"

    def test_a_failed_open_removes_the_copy_it_made(self, server, session,
                                                    files, monkeypatch):
        """A copy made for an open that then fails held nothing the original
        does not, and leaving it orphans a version nobody was told about --
        which the next run then counts past."""
        def refuse(path):
            raise RuntimeError("this document is corrupt")
        monkeypatch.setattr(type(session.backend), "open_document",
                            lambda self, path: refuse(path), raising=False)
        out = call(server, "open_part", path=str(files / "bracket.ipt"),
                   working_copy=True)
        assert out["ok"] is False
        assert not (files / "bracket_v2.ipt").exists()

    def test_reverting_a_round_deletes_a_parameter_it_created(self, session):
        """A created parameter has no prior expression, and 'putting it back'
        means deleting it -- skipping it kept the new value through a revert,
        and the reverted part was then saved."""
        from inventor_mcp.dfm.loop import _undo

        backend = session.ensure_backend()
        info = backend.new_part("P")
        context = session.register(info, "mm", "deg")
        backend.set_parameter(context.doc_id, "brand_new", "9", units="mm")
        _undo(session, context, [("brand_new", None)])
        names = [p.name for p in backend.list_parameters(context.doc_id)]
        assert "brand_new" not in names

    def test_the_sidecar_is_found_from_the_documents_own_path(self, session, tmp_path):
        """Without passing the path in: the document knows where it lives, and
        matching ids against list_documents never matched on the COM backend."""
        from inventor_mcp.dfm.declaration import Declaration, write_sidecar
        from inventor_mcp.dfm.sources import resolve

        part = tmp_path / "bracket.ipt"
        backend = session.ensure_backend()
        info = backend.new_part("bracket")
        context = session.register(info, "mm", "deg")
        backend.save_document(context.doc_id, str(part))
        part.write_bytes(b"")
        write_sidecar(part, Declaration(frozen=["bore_d"]))
        declaration, _ = resolve(session, context)   # no path argument
        assert "bore_d" in declaration.frozen


class TestAnInferenceIsNotLaundered:
    """Storing the merged declaration wrote every inference into the part as
    though the part had stated it, so a wrong inference came back next time as
    'the part itself' -- and even infer_roles=false could not escape it."""

    def _shelled(self, server):
        call(server, "new_part", name="Housing")
        call(server, "set_parameters", parameters=[
            {"name": "wall_t", "value": 2.5}, {"name": "bore_d", "value": 8}],
            rebuild=False)
        call(server, "apply_operations", operations=[
            {"op": "sketch", "name": "S", "plane": "xy",
             "entities": [{"type": "rectangle", "center": [0, 0],
                           "width": 40, "height": 30}]},
            {"op": "extrude", "name": "E", "sketch": "S", "distance": 20},
            {"op": "shell", "name": "Cavity",
             "faces": {"kind": "face", "filter": "top"},
             "thickness": "wall_t", "direction": "inside"},
        ])

    def test_declare_dfm_does_not_store_what_discovery_inferred(self, server):
        self._shelled(server)
        # Declare only a freeze. Discovery has meanwhile inferred the wall.
        out = call(server, "declare_dfm", frozen=["bore_d"])
        assert out["remembered"]["in_the_part"] is True
        found = call(server, "discover_dfm_roles")
        wall = found["what_would_be_used"]["roles"]["wall"]
        assert wall["from"] == "discovered", (
            "an inference stays an inference until a person confirms it -- "
            "stored, it would read back as 'the part itself'")
        assert "bore_d" in found["what_would_be_used"]["frozen"]

    def test_but_a_declared_role_is_stored(self, server):
        self._shelled(server)
        call(server, "declare_dfm", roles={"wall": "wall_t"})
        found = call(server, "discover_dfm_roles")
        wall = found["what_would_be_used"]["roles"]["wall"]
        assert wall["from"] == "the part itself"


class TestFreezeIntegrityFindings:
    """The freeze-integrity lens of the review: ways protection could be lost."""

    def test_an_unreadable_declaration_freezes_everything_until_fixed(
            self, server, tmp_path):
        """What it protects cannot be known, and the one wrong answer is
        'nothing'. The old behaviour was a quiet note and a part whose freezes
        were gone."""
        from inventor_mcp.dfm.declaration import sidecar_for

        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"")
        sidecar_for(part).write_text('{"frozen": "bore_d"}', encoding="utf-8")
        opened = call(server, "open_part", path=str(part))
        assert opened["ok"]
        assert "every_parameter_is_frozen_because" in opened
        call(server, "set_parameters", parameters=[{"name": "w", "value": 2}],
             rebuild=False)  # creating is also refused -- prove it
        out = call(server, "set_parameters",
                   parameters=[{"name": "anything", "value": 1}], rebuild=False)
        assert out["ok"] is False and out["error"] == "frozen_geometry"
        assert "could not be read" in out["message"]

    def test_a_frozen_feature_cannot_be_suppressed(self, server):
        """The other half of the freeze: protecting a parameter while its
        feature can be deleted protects a number and loses the geometry."""
        out = call(server, "build_part_from_recipe", recipe={
            "name": "Sealed", "units": "mm",
            "parameters": [{"name": "w", "value": 2}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy",
                 "entities": [{"type": "rectangle", "center": [0, 0],
                               "width": 40, "height": 30}]},
                {"op": "extrude", "name": "Body", "sketch": "S", "distance": 20},
            ],
            "dfm": {"frozen_features": ["Body"]},
        })
        assert out["ok"], out.get("errors")
        refused = call(server, "edit_feature", action="suppress", name="Body")
        assert refused["ok"] is False and refused["error"] == "frozen_geometry"
        forced = call(server, "edit_feature", action="suppress", name="Body",
                      override_frozen=True)
        assert forced["ok"]

    def test_reopening_an_open_file_keeps_the_same_document(self, server, tmp_path):
        """A second handle to the same geometry had no recipe and no freeze
        guard -- an unprotected door into a protected part."""
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"")
        first = call(server, "open_part", path=str(part))
        call(server, "protect_geometry", parameters=["bore_d"])
        second = call(server, "open_part", path=str(part))
        assert second["document"] == first["document"]
        out = call(server, "set_parameters",
                   parameters=[{"name": "bore_d", "value": 9}], rebuild=False)
        assert out["ok"] is False and out["error"] == "frozen_geometry"

    def test_a_session_freeze_is_planning_knowledge_not_an_abort(self, session,
                                                                 monkeypatch,
                                                                 tmp_path):
        """A loop that plans without the session guard proposes the change, has
        it refused at apply, and reports that as a broken rebuild."""
        import copy
        from inventor_mcp.builder import build_part
        from inventor_mcp.dfm import loop as loop_module
        from inventor_mcp.dfm.freeze import FreezeGuard
        from inventor_mcp.dfm.loop import improve
        from inventor_mcp.schema import PartRecipe
        from test_dfm_loop import RECIPE, Scripted, report

        build_part(session, PartRecipe.model_validate(copy.deepcopy(RECIPE)))
        context = session.context()
        values = {p.name: p.expression for p in session.backend.list_parameters(context.doc_id)}
        context.frozen = FreezeGuard(["draft_a"], expressions=values)
        monkeypatch.chdir(tmp_path)
        scripted = Scripted(report(score=49), report("clean", score=100))
        monkeypatch.setattr(loop_module, "measure", scripted)
        outcome = improve(session, context)
        assert "broke the rebuild" not in outcome.stopped_because
        held = [d for d in outcome.outstanding if d.reason == "frozen"]
        assert held and any("draft_a" in d.why for d in held)


class TestTwoMoreClobbersAndAContract:
    def test_extending_a_document_with_a_second_recipe_keeps_the_session_freeze(
            self, server):
        """The same clobber declare_dfm had: build_part replaced context.frozen
        wholesale, so rebuilding into an open document dropped a freeze added a
        moment earlier with protect_geometry."""
        recipe = {
            "name": "Housing", "units": "mm",
            "parameters": [{"name": "wall_t", "value": 2.5},
                           {"name": "bore_d", "value": 8}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy",
                 "entities": [{"type": "rectangle", "center": [0, 0],
                               "width": 40, "height": 30}]},
                {"op": "extrude", "name": "E", "sketch": "S", "distance": 20},
            ],
        }
        built = call(server, "build_part_from_recipe", recipe=recipe)
        call(server, "protect_geometry", parameters=["bore_d"])
        call(server, "build_part_from_recipe", recipe=recipe,
             document=built["document"])
        out = call(server, "set_parameters",
                   parameters=[{"name": "bore_d", "value": 9}], rebuild=False)
        assert out["ok"] is False and out["error"] == "frozen_geometry"

    def test_naming_both_a_path_and_a_document_is_called_out(self, server, files):
        """A caller holding two parts in mind must not get a report that quietly
        describes one of them."""
        other = call(server, "new_part", name="Other")
        out = call(server, "improve_for_manufacture",
                   path=str(files / "bracket.ipt"), document=other["document"])
        assert "not used" in out["file"]["note_on_document"]
