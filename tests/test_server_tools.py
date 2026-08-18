"""The MCP surface: tool registration, structured results and error shapes."""

from __future__ import annotations

import asyncio
import json

import pytest


def call(server, name: str, arguments: dict | None = None) -> dict:
    result = asyncio.run(server.call_tool(name, arguments or {}))
    return result.structured_content


@pytest.fixture
def connected(server):
    call(server, "connect", {"backend": "mock"})
    return server


class TestRegistration:
    def test_the_expected_tools_are_present(self, server):
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert {
            "connect", "new_part", "validate_recipe", "build_part_from_recipe",
            "apply_operations", "set_parameters", "inspect_part", "select_topology",
            "measure_part", "export_model", "capture_view", "part_recipe_schema",
        } <= names

    def test_every_tool_documents_itself(self, server):
        for tool in asyncio.run(server.list_tools()):
            assert tool.description, f"{tool.name} has no description"

    def test_resources_are_published(self, server):
        uris = {str(resource.uri) for resource in asyncio.run(server.list_resources())}
        assert "inventor://recipe/schema" in uris
        assert "inventor://recipe/guide" in uris

    def test_the_published_schema_is_valid_json(self, server):
        contents = asyncio.run(server.read_resource("inventor://recipe/schema"))
        payload = json.loads(list(contents)[0].content)
        assert payload["title"] == "PartRecipe"

    def test_prompts_are_published(self, server):
        names = {prompt.name for prompt in asyncio.run(server.list_prompts())}
        assert {"model_this_part", "revise_part"} <= names


class TestConnection:
    def test_connecting_to_the_simulator(self, server):
        result = call(server, "connect", {"backend": "mock"})
        assert result["ok"] and result["simulated"] is True

    def test_status_before_connecting(self, server):
        result = call(server, "session_status")
        assert result["connected"] is False
        assert any(entry["backend"] == "mock" for entry in result["available_backends"])

    def test_status_after_building(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        status = call(connected, "session_status")
        assert status["active"]["name"] == "MountingPlate"
        assert "plate_w" in status["active"]["parameters"]
        assert status["active"]["last_feature"] == "Hole1"

    def test_working_without_a_part_open(self, connected):
        result = call(connected, "measure_part")
        assert result["ok"] is False
        assert result["error"] == "document_error"
        assert "new_part" in result["hint"]


class TestRecipeTools:
    def test_the_schema_tool_returns_the_cheatsheet(self, server):
        result = call(server, "part_recipe_schema", {"include_json_schema": False})
        assert "WORKED EXAMPLE" in result["cheatsheet"]
        assert "json_schema" not in result

    def test_validation_needs_no_connection(self, server, plate_recipe):
        result = call(server, "validate_recipe", {"recipe": plate_recipe})
        assert result["ok"] and result["findings"] == []

    def test_validation_reports_schema_problems_field_by_field(self, server):
        result = call(server, "validate_recipe", {"recipe": {"name": "X", "operations": [
            {"op": "extrude", "extent": "distance"}]}})
        assert result["ok"] is False
        assert result["error"] == "invalid_input"
        assert result["issues"]

    def test_validation_reports_modelling_problems(self, server, plate_recipe):
        plate_recipe["operations"][1]["distance"] = "thicknes"
        result = call(server, "validate_recipe", {"recipe": plate_recipe})
        assert result["ok"] is False
        assert "Unknown parameter" in result["findings"][0]["error"]

    def test_building_refuses_an_invalid_recipe_by_default(self, connected, plate_recipe):
        plate_recipe["operations"][1]["distance"] = "thicknes"
        result = call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        assert result["ok"] is False
        assert result["error"] == "recipe_invalid"
        assert "validate_first=false" in result["hint"]

    def test_building_the_plate(self, connected, plate_recipe):
        result = call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        assert result["ok"], result.get("errors")
        assert result["bounding_box"]["size"] == [120.0, 80.0, 8.0]
        assert result["simulated"] is True

    def test_operations_can_be_appended_afterwards(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "apply_operations", {"operations": [
            {"op": "chamfer", "edges": {"filter": "circular"}, "distance": 0.5}]})
        assert result["ok"], result["errors"]
        assert result["applied"][0]["kind"] == "chamfer"

    def test_appending_a_bad_operation_reports_the_index(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "apply_operations", {"operations": [
            {"op": "fillet", "edges": {"feature": "Ghost"}, "radius": 1}]})
        assert result["ok"] is False
        assert result["errors"][0]["index"] == 0


class TestParameterEditing:
    def test_changing_a_parameter(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "set_parameters", {
            "parameters": [{"name": "plate_w", "value": 160}]})
        assert result["ok"]
        assert result["parameters"][0]["value"] == 160.0
        assert result["rebuild"]["rebuilt"] is True

    def test_a_new_parameter_may_reference_existing_ones(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "set_parameters", {"parameters": [
            {"name": "aspect", "value": "plate_w / plate_d", "unit": "ul"}]})
        assert result["parameters"][0]["value"] == pytest.approx(1.5)

    def test_a_bad_expression_is_explained(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "set_parameters", {"parameters": [
            {"name": "plate_w", "value": "widht * 2"}]})
        assert result["ok"] is False
        assert "widht" in result["message"]

    def test_the_simulator_says_it_did_not_resolve_geometry(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "set_parameters", {
            "parameters": [{"name": "plate_w", "value": 160}]})
        assert "does not re-solve" in result["rebuild"]["note"]


class TestInspection:
    def test_inspect_lists_the_whole_model(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "inspect_part")
        assert [f["name"] for f in result["features"]] == ["Plate", "Fillet1", "Hole1"]
        assert [s["name"] for s in result["sketches"]] == ["Body", "Holes"]
        assert result["size"]["size"] == [120.0, 80.0, 8.0]

    def test_select_topology_reports_handles_and_positions(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "select_topology", {
            "selector": {"kind": "face", "filter": "top"}})
        assert result["count"] == 1
        assert result["matches"][0]["midpoint"][2] == pytest.approx(8.0)
        assert "expire" in result["note"]

    def test_select_topology_rejects_an_unknown_filter(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "select_topology", {"selector": {"filter": "sideways"}})
        assert result["ok"] is False
        assert result["error"] == "invalid_input"

    def test_measurements_come_back_in_document_units(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "measure_part")
        assert result["units"] == "mm"
        assert result["bounding_box"]["size"] == [120.0, 80.0, 8.0]
        assert result["volume_cm3"] == pytest.approx(75.0, rel=0.01)

    def test_mass_needs_a_material(self, connected, plate_recipe):
        plate_recipe["material"] = "Aluminum"
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "measure_part")
        assert result["mass_kg"] == pytest.approx(0.2026, rel=0.02)


class TestOutput:
    def test_export_is_refused_by_the_simulator_but_says_so(self, connected, plate_recipe, tmp_path):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "export_model", {
            "path": str(tmp_path / "plate.stp"), "format": "step"})
        assert result["written"] is False and result["simulated"] is True
        assert "Inventor" in result["note"]

    def test_an_unsupported_export_format_never_reaches_the_backend(
        self, connected, plate_recipe, tmp_path
    ):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        # The format is a Literal in the tool schema, so the SDK rejects it before
        # the body runs and the caller is told the permitted values.
        with pytest.raises(Exception, match="Input should be 'step'"):
            call(connected, "export_model",
                 {"path": str(tmp_path / "plate.xyz"), "format": "xyz"})

    def test_capture_view_reports_that_it_cannot_render(self, connected, plate_recipe, tmp_path):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "capture_view", {"path": str(tmp_path / "v.png")})
        assert result["written"] is False


class TestFeatureEditing:
    def test_rename_updates_the_session_memory(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "edit_feature", {
            "action": "rename", "name": "Plate", "new_name": "Slab"})
        assert result["feature"]["name"] == "Slab"
        assert "Slab" in call(connected, "session_status")["active"]["features"]

    def test_rename_without_a_new_name(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "edit_feature", {"action": "rename", "name": "Plate"})
        assert result["ok"] is False

    def test_an_unknown_action(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        result = call(connected, "edit_feature", {"action": "polish", "name": "Plate"})
        assert result["ok"] is False and "suppress" in result["hint"]

    def test_deleting_a_feature(self, connected, plate_recipe):
        call(connected, "build_part_from_recipe", {"recipe": plate_recipe})
        call(connected, "edit_feature", {"action": "delete", "name": "Hole1"})
        assert [f["name"] for f in call(connected, "inspect_part")["features"]] == [
            "Plate", "Fillet1"]


class TestDocumentLifecycle:
    def test_new_save_close(self, connected, tmp_path):
        created = call(connected, "new_part", {"name": "Widget", "units": "in"})
        assert created["units"] == "in"
        saved = call(connected, "save_part", {"path": str(tmp_path / "widget.ipt")})
        assert saved["path"].endswith("widget.ipt")
        closed = call(connected, "close_part")
        assert closed["closed"] == created["document"]

    def test_two_parts_can_be_open_at_once(self, connected):
        first = call(connected, "new_part", {"name": "A"})
        second = call(connected, "new_part", {"name": "B"})
        assert first["document"] != second["document"]
        status = call(connected, "session_status")
        assert len(status["documents"]) == 2
        assert call(connected, "activate_part", {"document": first["document"]})["ok"]
