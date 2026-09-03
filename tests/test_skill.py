"""The shipped Skill makes factual claims; they have to stay true.

Everything in it was paid for in live runs, and it is the one place a model
reads before touching Inventor. Documentation that has drifted is worse than
none, because it is believed.
"""

from __future__ import annotations

import json
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

    def test_the_hole_styles_it_offers_really_are_built(self, skill):
        """The Skill used to warn these were dropped; it must not any more."""
        from inventor_mcp.backend.com import holes
        from inventor_mcp.schema import HoleOp

        for style in HoleOp.model_fields["style"].annotation.__args__:
            assert (style, True) in holes.METHOD and (style, False) in holes.METHOD
        assert "AddCBore" in holes.METHOD[("counterbore", True)]
        assert "drills a plain hole" not in skill, (
            "counterbore, countersink and tapped holes are built now")
        # And the Skill has to say what it does check, since a hole that builds
        # as the wrong style is the failure the verification exists for.
        assert "reads the style back" in skill

    def test_the_unproven_operations_it_lists_match_the_documentation(self, skill):
        setup = (SKILL.parent.parent.parent / "docs" / "INVENTOR_SETUP.md").read_text()
        lowered, setup = skill.lower(), setup.lower()
        for operation in ("revolve", "sweep", "loft", "patterns", "threads"):
            assert operation in lowered
            assert operation in setup, (
                f"{operation} is called unproven in the Skill; keep the docs agreeing")


class TestTheWorkedExamplesWork:
    """Every recipe in the Skill has to build, or it teaches a broken pattern.

    These are the few-shot material a model copies from. A recipe that no longer
    validates is worse than no example at all, because it will be imitated.
    """

    def recipes(self, skill: str) -> list[tuple[int, dict]]:
        import json

        found = []
        for match in re.finditer(r"```json\n(.*?)\n```", skill, re.DOTALL):
            body = match.group(1)
            if '"operations"' not in body:
                continue  # a fragment illustrating one field, not a whole recipe
            found.append((skill[: match.start()].count("\n") + 1, json.loads(body)))
        return found

    def test_the_skill_contains_worked_examples(self, skill):
        assert len(self.recipes(skill)) >= 2, (
            "the reasoning from a sentence to a recipe is the thing worth teaching")

    def test_every_one_of_them_rehearses_clean(self, skill):
        from inventor_mcp.builder import rehearse
        from inventor_mcp.schema import PartRecipe

        for line, recipe in self.recipes(skill):
            report = rehearse(PartRecipe.model_validate(recipe))
            assert report["ok"], (
                f"SKILL.md:{line} ({recipe.get('name')}) does not build: "
                f"{report['findings']}")
            assert report["warnings"] == [], (
                f"SKILL.md:{line} ({recipe.get('name')}) rehearses with warnings: "
                f"{[w['warning'] for w in report['warnings']]}")

    def test_and_every_declared_parameter_drives_something(self, skill):
        """The examples teach that; they had better do it."""
        from inventor_mcp.builder import rehearse
        from inventor_mcp.schema import PartRecipe

        for line, recipe in self.recipes(skill):
            report = rehearse(PartRecipe.model_validate(recipe))
            idle = [w for w in report["warnings"] if "drive nothing" in w["warning"]]
            assert not idle, f"SKILL.md:{line}: {idle}"

    def test_they_use_expressions_rather_than_literals_for_derived_sizes(self, skill):
        """The point of the examples is this habit, so check they show it."""
        for line, recipe in self.recipes(skill):
            declared = {spec["name"] for spec in recipe.get("parameters", [])}
            body = json.dumps(recipe)
            used = {name for name in declared if f'"{name}' in body or f' {name}' in body}
            assert used == declared, (
                f"SKILL.md:{line}: {sorted(declared - used)} never appears in an "
                "expression, so the example teaches a frozen number")


class TestTheStandardParts:
    """The fastener templates have to build, and be the right size.

    A hexagon given across its corners rather than its flats is 15% too big:
    an M16 nut a 24 mm spanner will not fit. It is the commonest way a hex part
    is wrong, and the only way to know is to measure one.
    """

    REFERENCE = SKILL.parent / "references" / "standard-parts.md"

    @pytest.fixture(scope="class")
    def reference(self) -> str:
        return self.REFERENCE.read_text()

    def recipes(self, text: str) -> dict:
        out = {}
        for match in re.finditer(r"```json\n(.*?)\n```", text, re.DOTALL):
            if '"operations"' not in match.group(1):
                continue
            recipe = json.loads(match.group(1))
            out[recipe["name"]] = recipe
        return out

    def test_the_reference_exists_and_holds_parts(self, reference):
        assert self.recipes(reference), "no buildable recipes in the reference"

    def test_every_part_rehearses_clean(self, reference):
        from inventor_mcp.builder import rehearse
        from inventor_mcp.schema import PartRecipe

        for name, recipe in self.recipes(reference).items():
            report = rehearse(PartRecipe.model_validate(recipe))
            assert report["ok"], f"{name}: {report['findings']}"
            assert report["warnings"] == [], (
                f"{name}: {[w['warning'] for w in report['warnings']]}")

    def build(self, recipe):
        from inventor_mcp.builder import build_part
        from inventor_mcp.schema import PartRecipe
        from inventor_mcp.session import Session

        session = Session(backend_kind="mock")
        session.ensure_backend().connect()
        build_part(session, PartRecipe.model_validate(recipe))
        return session.backend.mass_properties(session.active)

    def test_the_hex_nut_measures_its_across_flats_size(self, reference):
        """24 mm across the flats, 27.713 across the corners: 24 / cos 30."""
        import math

        nut = self.recipes(reference)["HexNut_M16"]
        box = self.build(nut).bounding_box
        across_corners = (box[3] - box[0]) * 10
        across_flats = (box[4] - box[1]) * 10
        assert across_flats == pytest.approx(24.0, abs=1e-3)
        assert across_corners == pytest.approx(24.0 / math.cos(math.pi / 6), abs=1e-3)

    def test_the_hex_nut_has_the_volume_the_standard_implies(self, reference):
        """The bore is the tapping drill, 14 mm for M16x2, not the nominal 16."""
        import math

        nut = self.recipes(reference)["HexNut_M16"]
        wanted = ((3 ** 0.5 / 2) * 24 ** 2 * 14.8 - math.pi * 7 ** 2 * 14.8) / 1000
        assert self.build(nut).volume == pytest.approx(wanted, rel=1e-6)

    def test_the_washer_does_too(self, reference):
        import math

        washer = self.recipes(reference)["Washer_M8"]
        wanted = math.pi * (8 ** 2 - 4.2 ** 2) * 1.6 / 1000
        assert self.build(washer).volume == pytest.approx(wanted, rel=1e-6)

    def test_every_fastener_asks_for_across_flats(self, reference):
        """`inscribed` on a fastener is the bug this reference exists to prevent."""
        for name, recipe in self.recipes(reference).items():
            for op in recipe["operations"]:
                for entity in op.get("entities", []):
                    if entity.get("type") == "polygon":
                        assert entity.get("fit") == "circumscribed", (
                            f"{name} gives its hexagon across the corners")

    def test_the_standards_disagreement_is_recorded(self, reference):
        """DIN 934 M10 is 17 mm across flats; ISO 4032 M10 is 16."""
        assert "ISO 4032" in reference and "DIN 934" in reference
        assert "17" in reference and "16" in reference

    def test_it_says_what_the_templates_do_not_do(self, reference):
        """The missing conical chamfer, and where a tapped hole's size comes from."""
        assert "conical" in reference
        assert "thread table" in reference, (
            "a tapped hole is drilled from Inventor's table, so the template has "
            "to say the recipe's diameter is only a claim about it")

    def test_the_tapped_template_gives_the_tapping_drill(self, reference):
        """Nominal 16 with an M16x2 tap would disagree with Inventor by 2 mm."""
        nut = self.recipes(reference)["HexNut_M16"]
        hole = [op for op in nut["operations"] if op["op"] == "hole"][0]
        assert hole["tap"] == "M16x2"
        assert hole["diameter"] != "thread_d", (
            "that is the nominal diameter, not the hole you drill to tap it")


class TestTheSnapshotPolicy:
    """That a part gets looked at, and that the caveat matches the defect.

    Adopted from the closest open-source peer, which states it as "deterministic
    checks passing is not a reason to skip": their modelling notes list six
    traps that pass every automated check and only a render finds. This server
    had `capture_view` and no rule that anyone runs it.

    The awkward half is that `capture_view`'s own orientation names do not
    describe what they return -- `docs/FEATURE_COVERAGE.md` defect 4 -- so the
    policy has to prescribe `iso` and say why. Which makes the policy and the
    defect two statements of one fact, in two files, with nothing holding them
    together. Hence the last test here.
    """

    COVERAGE = SKILL.parent.parent.parent / "docs" / "FEATURE_COVERAGE.md"

    @pytest.fixture(scope="class")
    def coverage(self) -> str:
        return self.COVERAGE.read_text()

    def test_the_skill_says_to_run_it_every_time(self, skill):
        section = skill[skill.index("## Look at it"):]
        section = section[:section.index("\n## ", 3)]
        assert "capture_view" in section
        assert "every time" in section
        assert "not a reason to skip" in section, (
            "the policy's whole point is that passing checks do not excuse it")

    def test_the_guide_the_tools_serve_says_it_too(self):
        """A caller that never loads the Skill still has to be told."""
        from inventor_mcp.guide import MODELLING_NOTES

        assert "capture_view" in MODELLING_NOTES
        assert "iso" in MODELLING_NOTES

    def test_the_tool_itself_says_it(self, server):
        import asyncio

        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        description = tools["capture_view"].description
        assert "before reporting the part finished" in description
        assert "iso" in description

    def test_the_orientation_it_prescribes_is_one_the_tool_accepts(self, server):
        import asyncio

        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        schema = tools["capture_view"].input_schema
        allowed = schema["properties"]["orientation"]
        assert "iso" in (allowed.get("enum") or [allowed.get("default")]), allowed

    def test_the_caveat_and_the_defect_are_still_the_same_fact(self, skill, coverage):
        """When defect 4 is fixed, this policy has to be revisited, not left.

        `FEATURE_COVERAGE.md` strikes a defect's title through when it is fixed.
        Until then the Skill is right to say the names mislead; afterwards it
        would be telling people to work around something that works.
        """
        defects = coverage[coverage.index("## Defects worth fixing"):]
        assert "capture_view" in defects, "defect 4 has moved out of that section"
        # It is the last entry in the file, so there may be no blank line after it.
        entry = defects[defects.index("\n4. ") + 4:].split("\n\n")[0]
        assert "capture_view" in entry, f"the fourth defect is not the one meant: {entry[:60]}"
        fixed = entry.startswith("~~")
        claims_broken = "has not been fixed" in skill
        assert fixed != claims_broken, (
            "docs/FEATURE_COVERAGE.md defect 4 and the Skill's snapshot section "
            "disagree about whether capture_view's orientation names work. "
            "Whichever changed, change the other.")
