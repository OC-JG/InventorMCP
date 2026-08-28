"""Every shipped example must validate and build.

The examples are the first thing anyone reads, and they double as the
regression suite for the recipe schema: a change that breaks them breaks the
documentation at the same time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inventor_mcp.builder import build_part, check_recipe
from inventor_mcp.schema import PartRecipe

EXAMPLES = sorted((Path(__file__).parent.parent / "examples").glob("*.json"))


def load(path: Path) -> PartRecipe:
    return PartRecipe.model_validate(json.loads(path.read_text()))


def test_there_are_examples():
    assert EXAMPLES, "no example recipes found"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
class TestExample:
    def test_it_matches_the_schema(self, path):
        recipe = load(path)
        assert recipe.operations

    def test_it_passes_the_static_checks(self, path):
        report = check_recipe(load(path))
        assert report["ok"], report["findings"]

    def test_it_builds(self, session, path):
        result = build_part(session, load(path))
        assert result["ok"], result["errors"]

    def test_it_has_a_solid_body(self, session, path):
        build_part(session, load(path))
        assert session.backend.mass_properties(session.active).volume > 0

    def test_every_parameter_is_used_or_commented(self, path):
        """A parameter nobody references is either dead or a naming mistake."""
        recipe = load(path)
        body = json.dumps(recipe.model_dump(mode="json")["operations"])
        derived = " ".join(str(p.value) for p in recipe.parameters)
        for parameter in recipe.parameters:
            referenced = parameter.name in body or parameter.name in derived
            assert referenced or parameter.comment, (
                f"{path.stem}: parameter {parameter.name!r} is never referenced"
            )


def test_the_mounting_plate_has_the_advertised_size(session):
    build_part(session, load(Path(__file__).parent.parent / "examples" / "mounting_plate.json"))
    box = session.backend.mass_properties(session.active).bounding_box
    assert (box[3] - box[0], box[4] - box[1], box[5] - box[2]) == pytest.approx((12.0, 8.0, 0.8))


def test_the_cover_plates_hole_styles_remove_what_the_geometry_says(session):
    """The one expectation derived by hand *before* any live run.

    A counterbore removes an annulus over the bore that is already counted, and
    a countersink removes a cone -- the simulator got both wrong until these
    numbers were worked out. Pinning it here keeps the expectation file and the
    derivation in step, so a live disagreement means Inventor, not arithmetic.
    """
    import math

    root = Path(__file__).parent.parent
    build_part(session, load(root / "examples" / "cover_plate.json"))
    measured = session.backend.mass_properties(session.active).volume

    plate = 10.0 * 6.0 * 1.0
    bore = math.pi * 0.33**2 * 1.0
    counterbore = math.pi * (0.55**2 - 0.33**2) * 0.66
    spigot = math.pi * 0.4**2 * 1.0
    # 90 degrees included, so the cone is (R - r) = 0.4 cm deep.
    cone = (math.pi * 0.4 / 3 * (0.8**2 + 0.8 * 0.4 + 0.4**2)
            - math.pi * 0.4**2 * 0.4)
    by_hand = plate - 4 * (bore + counterbore) - (spigot + cone)
    assert measured == pytest.approx(by_hand, rel=1e-9)

    recorded = json.loads(
        (root / "examples" / "expected" / "cover_plate.json").read_text())
    assert recorded["volume_cm3"] == pytest.approx(by_hand, abs=5e-7), (
        "the recorded expectation no longer matches the derivation it came from")


class TestEveryParameterActuallyDrivesSomething:
    """The test that would have caught the polyline gap.

    `_plan_polyline` emitted constraints and no dimensions at all, so the angle
    bracket's L-section was built from base_len, upright_h and thk and then
    driven by none of them: the numbers were evaluated when the recipe was
    written and thrown away. Every step reported ok, the part looked right, and
    editing a parameter moved the slots along an outline that stayed put.

    A recipe that writes a parameter into an entity's geometry is asking for
    that parameter to drive it. This asserts that it does.
    """

    def sketch_ops(self, recipe):
        from inventor_mcp.schema import SketchOp

        return [op for op in recipe.operations if isinstance(op, SketchOp)]

    def authored(self, op, declared):
        """Parameters the recipe wrote into this sketch's geometry.

        Intersected with the recipe's own parameter list, because an entity's
        `type` and `locate` fields are identifiers too and mean nothing here.
        """
        from inventor_mcp.expressions import referenced_parameters

        names = set()
        for entity in op.entities:
            if not getattr(entity, "dimension", True):
                continue  # the author asked for no dimensions on this entity
            for value in entity.model_dump().values():
                for text in self.strings(value):
                    names |= referenced_parameters(text) & declared
        return names

    def strings(self, value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from self.strings(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from self.strings(item)

    def driven(self, plan):
        from inventor_mcp.expressions import referenced_parameters

        names = set()
        for dimension in plan.dimensions:
            names |= referenced_parameters(dimension.expression)
        return names

    @pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
    def test_every_parameter_written_into_a_sketch_drives_it(self, session, path):
        from inventor_mcp.builder import build_part
        from inventor_mcp.schema import PartRecipe

        recipe = PartRecipe.model_validate(json.loads(path.read_text()))
        build_part(session, recipe)
        declared = {spec.name for spec in recipe.parameters}
        plans = {name: sketch.plan for name, sketch in
                 ((s.name, s) for s in session.backend._doc(session.active).sketches)}

        for op in self.sketch_ops(recipe):
            plan = plans.get(op.name)
            if plan is None or not plan.dimensions and not plan.primitives:
                continue
            missing = self.authored(op, declared) - self.driven(plan)
            assert not missing, (
                f"{path.stem}, sketch {op.name!r}: "
                f"{', '.join(sorted(missing))} written into the geometry but "
                f"driving no dimension"
            )
