"""The shipped Skill makes factual claims; they have to stay true.

Everything in it was paid for in live runs, and it is the one place a model
reads before touching Inventor. Documentation that has drifted is worse than
none, because it is believed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL = (Path(__file__).resolve().parent.parent / "skills"
         / "inventor-parametric-modelling" / "SKILL.md")


@pytest.fixture(scope="module")
def skill() -> str:
    return SKILL.read_text()


class TestItIsAWellFormedSkill:
    def test_it_exists_and_has_front_matter(self, skill):
        assert skill.startswith("---\n")
        assert "\nname: inventor-parametric-modelling\n" in skill
        assert re.search(r"\ndescription: .{80,}", skill), (
            "the description is what decides whether it triggers at all")

    def test_the_description_names_what_it_triggers_on(self, skill):
        description = skill.split("description:", 1)[1].split("\n", 1)[0].lower()
        for cue in ("inventor", "parametric", ".ipt"):
            assert cue in description


class TestTheToolsItNamesExist:
    def test_every_tool_it_mentions_is_a_real_tool(self, skill, server):
        named = set(re.findall(r"`([a-z_]+)`", skill))
        tools = {
            "connect", "validate_recipe", "build_part_from_recipe", "set_parameters",
            "inspect_part", "select_topology", "measure_part", "export_model",
            "capture_view", "apply_operations", "part_recipe_schema",
        }
        # Only check the names that look like tools; prose has backticks too.
        claimed = {name for name in named if name in tools or name.endswith("_part")
                   or name.startswith("build_") or name.startswith("validate_")}
        assert claimed, "the skill should name the tools"
        assert claimed <= tools, f"unknown tool(s): {sorted(claimed - tools)}"


class TestTheFieldsItTellsTheModelToReadExist:
    def test_the_measurement_fields_are_real(self, session):
        from inventor_mcp.builder import _changed

        before = {"volume_cm3": 10.0, "faces": 6, "edges": 12, "span_mm": [1, 2, 3]}
        after = {"volume_cm3": 9.0, "faces": 8, "edges": 16, "span_mm": [1, 2, 4]}
        report = _changed(before, after)
        for field in ("volume_cm3", "volume_change_cm3", "faces", "edges"):
            assert field in report, f"the skill tells the model to read {field}"

    def test_the_did_not_change_note_says_exactly_what_the_skill_quotes(self, skill):
        from inventor_mcp.builder import _changed

        report = _changed({"volume_cm3": 10.0}, {"volume_cm3": 10.0})
        assert report["note"] in skill, (
            "the skill quotes this string; it must match the code")

    def test_the_sketch_fields_are_real(self, skill):
        from inventor_mcp.backend.base import SketchInfo

        for field in ("driving_dimensions", "refused_dimensions", "driven_parameters",
                      "axes"):
            assert field in SketchInfo.__dataclass_fields__
        assert "driven by" in skill


class TestTheSelectorsItRecommendsAreAccepted:
    @pytest.mark.parametrize("selector", [
        {"filter": "concave", "min_length": 40, "limit": 1},
        {"filter": "vertical"},
        {"filter": "top"},
        {"filter": "circular", "near": [0, 0, 102], "limit": 1},
    ])
    def test_each_example_validates(self, selector):
        from inventor_mcp.schema import Selector

        Selector.model_validate(selector)

    def test_every_filter_name_it_mentions_is_a_real_filter(self, skill):
        import typing

        from inventor_mcp.schema import Selector

        allowed = set(typing.get_args(
            Selector.model_fields["filter"].annotation)) | {None}
        quoted = set(re.findall(r"`(concave|convex|vertical|horizontal|top|bottom|"
                                r"circular|linear|planar|cylindrical)`", skill))
        assert quoted, "the skill should name filters"
        assert quoted <= {a for a in allowed if isinstance(a, str)}


class TestTheClaimsAboutBehaviourHold:
    def test_unknown_convexity_really_does_match_nothing(self, skill):
        """The skill tells the model that 'matched no edges' can mean 'unsure'."""
        from inventor_mcp.backend.base import TopoInfo
        from inventor_mcp.backend.com import backend as com

        unknown = TopoInfo(id="e", kind="edge", description="")
        assert com._com_passes_filter(unknown, "concave") is False
        assert com._com_passes_filter(unknown, "convex") is False
        assert "matches nothing" in skill or "matched no edges" in skill

    def test_a_recipes_x_really_is_model_x_on_every_plane(self, skill):
        from inventor_mcp.backend.mock.backend import map3d

        assert map3d("xy", 1.0, 0.0, 0.0) == (1.0, 0.0, 0.0)
        assert map3d("xz", 1.0, 0.0, 0.0) == (1.0, 0.0, 0.0)
        assert map3d("yz", 1.0, 0.0, 0.0) == (0.0, 1.0, 0.0)
        assert "model X on every plane" in skill

    def test_the_hole_styles_it_warns_about_really_are_dropped(self, skill):
        """If this is ever implemented, the skill must stop saying otherwise."""
        import inspect

        from inventor_mcp.backend.com import backend as com

        source = inspect.getsource(com.ComBackend.hole)
        assert "AddCBore" not in source, (
            "counterbore is implemented now -- update the Skill")
        assert "plain hole" in skill

    def test_the_unproven_operations_it_lists_match_the_documentation(self, skill):
        setup = (SKILL.parent.parent.parent / "docs" / "INVENTOR_SETUP.md").read_text()
        lowered, setup = skill.lower(), setup.lower()
        for operation in ("revolve", "sweep", "loft", "patterns", "threads"):
            assert operation in lowered
            assert operation in setup, (
                f"{operation} is called unproven in the Skill; keep the docs agreeing")
