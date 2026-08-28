"""The DFM tools, at the layer a model actually calls them through.

The one worth reading is ``read_dfm_report``: it takes a file exported from the
DFM tool in a browser and says what it implies about the model, without Node,
without a checkout of the analyser and without re-running anything. That is the
path a person actually takes -- they had the tool open, they exported the JSON --
and it must not require the whole headless apparatus to be in place.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures" / "dfm"

RECIPE = {
    "name": "Housing", "units": "mm",
    "parameters": [
        {"name": "wall_t", "value": 2.0},
        {"name": "draft_a", "value": 0.2, "unit": "deg"},
        {"name": "rib_t", "value": 1.9},
        {"name": "seal_face", "value": "wall_t * 3", "frozen": True},
    ],
    "operations": [
        {"op": "sketch", "name": "Base", "plane": "xy",
         "entities": [{"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]},
        {"op": "extrude", "name": "Body", "sketch": "Base", "distance": 20},
    ],
    "dfm": {"parameters": {"wall": "wall_t", "draft": "draft_a",
                           "rib_thickness": "rib_t"},
            "settings": {"material": "abs"}},
}


@pytest.fixture
def tools(server):
    """Every registered tool, by name, callable directly."""
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def call(server, name, **arguments):
    return asyncio.run(server.call_tool(name, arguments)).structured_content


@pytest.fixture
def built(server):
    """A part built through the server, so the tools and the model share a session.

    The server owns its own Session; building through the ``session`` fixture
    would put the part somewhere the tools cannot see.
    """
    out = call(server, "build_part_from_recipe", recipe=copy.deepcopy(RECIPE))
    assert out["ok"], out.get("errors")
    return out["document"]


class TestRegistration:
    def test_the_dfm_tools_are_there(self, tools):
        assert {"check_manufacture", "improve_for_manufacture", "read_dfm_report",
                "protect_geometry", "dfm_capabilities"} <= set(tools)

    def test_each_says_what_it_does(self, tools):
        for name in ("check_manufacture", "improve_for_manufacture",
                     "read_dfm_report", "protect_geometry", "dfm_capabilities"):
            assert tools[name].description


class TestCapabilities:
    def test_it_lists_the_roles(self, server):
        out = call(server, "dfm_capabilities")
        assert "wall" in out["roles"]
        assert out["roles"]["wall"]

    def test_and_says_what_needs_a_person(self, server):
        out = call(server, "dfm_capabilities")
        assert "undercut" in out["needs_a_person"]

    def test_and_is_honest_about_which_checks_it_can_answer(self, server):
        out = call(server, "dfm_capabilities")
        assert set(out["answerable_by_a_parameter"]) == {"wall", "draft", "ribs"}


class TestReadingAnExportedReport:
    def test_it_reads_a_file_from_the_browser_tool(self, server, built):
        out = call(server, "read_dfm_report",
                   path=str(FIXTURES / "many_findings.json"))
        assert out["ok"]
        assert out["score"] == 49

    def test_and_says_what_it_would_change(self, server, built):
        out = call(server, "read_dfm_report",
                   path=str(FIXTURES / "many_findings.json"))
        changed = {c["parameter"] for c in out["would_change"]["changes"]}
        assert {"draft_a", "rib_t"} <= changed
        assert "wall_t" not in changed, (
            "the frozen seal_face is 3x the wall, so the wall is protected too")

    def test_a_change_names_the_finding_it_answers(self, server, built):
        out = call(server, "read_dfm_report",
                   path=str(FIXTURES / "many_findings.json"))
        for change in out["would_change"]["changes"]:
            assert change["answers"], change
            assert change["why"], change

    def test_it_changes_nothing(self, server, built):
        def table():
            return {p["name"]: p["value"]
                    for p in call(server, "inspect_part")["parameters"]}
        before = table()
        call(server, "read_dfm_report", path=str(FIXTURES / "many_findings.json"))
        assert table() == before

    def test_the_frozen_parameter_is_reported_with_it(self, server, built):
        out = call(server, "read_dfm_report",
                   path=str(FIXTURES / "many_findings.json"))
        assert "seal_face" in out["key_geometry"]["declared"]

    def test_and_what_the_freeze_drags_in(self, server, built):
        """seal_face is 3x the wall, so the wall cannot move either."""
        out = call(server, "read_dfm_report",
                   path=str(FIXTURES / "many_findings.json"))
        also = {entry["parameter"] for entry in out["key_geometry"]["also_protected"]}
        assert "wall_t" in also
        held = [d for d in out["would_change"]["not_acted_on"]
                if d["not_acted_on"] == "frozen"]
        assert held

    def test_a_report_with_no_part_open_still_reads(self, server):
        out = call(server, "read_dfm_report",
                   path=str(FIXTURES / "many_findings.json"))
        assert out["ok"] and out["score"] == 49
        assert "would_change" not in out
        assert "No part is open" in out["note"]

    def test_something_that_is_not_a_report_is_refused_clearly(self, server, tmp_path):
        bad = tmp_path / "nope.json"
        bad.write_text('{"hello": "world"}', encoding="utf-8")
        out = call(server, "read_dfm_report", path=str(bad))
        assert out["ok"] is False
        assert out["error"] == "dfm_report_error"


class TestProtectingGeometry:
    def test_it_reports_what_is_already_protected(self, server, built):
        out = call(server, "protect_geometry")
        assert "seal_face" in out["key_geometry"]["declared"]

    def test_it_adds_to_the_set(self, server, built):
        out = call(server, "protect_geometry", parameters=["rib_t"])
        assert "rib_t" in out["key_geometry"]["declared"]
        assert "seal_face" in out["key_geometry"]["declared"], "additive, not a replacement"

    def test_and_the_addition_is_then_enforced(self, server, built):
        call(server, "protect_geometry", parameters=["rib_t"])
        out = call(server, "set_parameters", parameters=[{"name": "rib_t", "value": 5}])
        assert out["ok"] is False
        assert out["error"] == "frozen_geometry"

    def test_the_override_gets_through_and_says_so(self, server, built):
        call(server, "protect_geometry", parameters=["rib_t"])
        out = call(server, "set_parameters",
                   parameters=[{"name": "rib_t", "value": 5}], override_frozen=True)
        assert out["ok"]
        assert out["overrode_key_geometry"] == ["rib_t"]

    def test_a_name_that_is_not_a_parameter_is_flagged_not_refused(self, server, built):
        """Protecting a name before it exists is legitimate; a typo that silently
        protects nothing is not."""
        out = call(server, "protect_geometry", parameters=["wal_t"])
        assert out["ok"]
        assert out["not_a_parameter_yet"] == ["wal_t"]
        assert "wall_t" in out["note"], "the real names are listed to compare against"

    def test_a_glob_is_not_flagged(self, server, built):
        out = call(server, "protect_geometry", parameters=["seal_*"])
        assert "not_a_parameter_yet" not in out

    def test_it_warns_when_nothing_is_protected(self, server):
        call(server, "build_part_from_recipe", recipe={
            "name": "Bare", "units": "mm",
            "parameters": [{"name": "w", "value": 2}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy",
                 "entities": [{"type": "circle", "center": [0, 0], "diameter": 20}]},
                {"op": "extrude", "name": "E", "sketch": "S", "distance": "w"},
            ],
        })
        out = call(server, "protect_geometry")
        assert "Nothing is protected" in out["note"]


class TestCheckManufactureWithoutAnAnalyser:
    def test_it_says_what_is_missing_rather_than_failing_obscurely(self, server, built):
        out = call(server, "check_manufacture", dfm_root="/nowhere/at/all")
        assert out["ok"] is False
        assert out["error"] in ("dfm_unavailable", "unexpected_error")
        if out["error"] == "dfm_unavailable":
            assert "checkout" in (out.get("hint") or "").lower()

    def test_the_mock_cannot_export_a_mesh_and_says_which_backend_to_use(
            self, server, built, tmp_path):
        out = call(server, "check_manufacture", workspace=str(tmp_path))
        assert out["ok"] is False
        message = json.dumps(out).lower()
        assert "inventor" in message or "dfm" in message
