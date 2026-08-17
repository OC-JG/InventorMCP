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
