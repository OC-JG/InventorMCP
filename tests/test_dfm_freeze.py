"""Key geometry, and the ways a freeze can be honoured on paper and broken in fact.

The interesting case is not the one in the list. It is ``plate_t``, which is not
frozen, drives a frozen ``seal_face``, and can therefore be moved to move the
thing that was protected -- with every report still saying the freeze held.
"""

from __future__ import annotations

import pytest

from inventor_mcp.builder import apply_parameter, build_part
from inventor_mcp.dfm.freeze import FreezeGuard, FrozenGeometryError, guard_for_recipe
from inventor_mcp.schema import ParameterSpec, PartRecipe

TABLE = {
    "plate_t": "8",
    "gasket_crush": "0.4",
    "seal_face": "plate_t - gasket_crush",
    "wall_t": "2",
    "rib_t": "wall_t * 0.45",
    "clearance": "seal_face + 0.2",
}


class TestWhatIsProtected:
    def test_a_named_parameter_is(self):
        guard = FreezeGuard(["seal_face"], expressions=TABLE)
        assert guard.check("seal_face") is not None

    def test_and_so_is_whatever_it_is_computed_from(self):
        """The whole point. Changing plate_t moves the frozen face."""
        guard = FreezeGuard(["seal_face"], expressions=TABLE)
        for name in ("plate_t", "gasket_crush"):
            frozen = guard.check(name)
            assert frozen is not None, f"{name} drives a frozen value and is not protected"
            assert frozen.via == ("seal_face",)
            assert "seal_face" in frozen.explain()

    def test_but_not_what_merely_reads_it(self):
        """`clearance` reads the frozen face. Reading is not changing."""
        guard = FreezeGuard(["seal_face"], expressions=TABLE)
        assert guard.check("clearance") is None

    def test_nor_anything_unrelated(self):
        guard = FreezeGuard(["seal_face"], expressions=TABLE)
        assert guard.check("wall_t") is None
        assert guard.check("rib_t") is None

    def test_the_chain_is_followed_all_the_way(self):
        guard = FreezeGuard(["clearance"], expressions=TABLE)
        assert guard.check("seal_face") is not None
        assert guard.check("plate_t") is not None, "two hops from the frozen name"
        assert guard.check("plate_t").via == ("clearance", "seal_face")

    def test_a_glob_protects_a_family(self):
        guard = FreezeGuard(["seal_*"], expressions=TABLE)
        assert guard.check("seal_face") is not None
        assert guard.check("wall_t") is None

    def test_matching_ignores_case(self):
        guard = FreezeGuard(["SEAL_FACE"], expressions=TABLE)
        assert guard.check("seal_face") is not None

    def test_a_cycle_does_not_hang(self):
        guard = FreezeGuard(["a"], expressions={"a": "b + 1", "b": "a + 1"})
        assert guard.check("b") is not None

    def test_an_unreadable_expression_is_reported_not_swallowed(self):
        """It must not claim protection it could not work out."""
        guard = FreezeGuard(["a"], expressions={"a": "b +* 1", "b": "2"})
        assert guard.check("b") is None
        assert guard.notes and "b" not in guard.check("a").via


class TestWidening:
    def test_extend_adds(self):
        guard = FreezeGuard(["seal_face"], expressions=TABLE).extend(["wall_t"])
        assert guard.check("seal_face") is not None
        assert guard.check("wall_t") is not None

    def test_extend_recomputes_the_closure(self):
        """Freezing rib_t must protect wall_t, which it is a fraction of."""
        guard = FreezeGuard([], expressions=TABLE).extend(["rib_t"])
        assert guard.check("wall_t") is not None

    def test_there_is_no_way_to_narrow_one(self):
        assert not hasattr(FreezeGuard([]), "remove")
        assert not hasattr(FreezeGuard([]), "unfreeze")


class TestFromARecipe:
    def _recipe(self, **dfm):
        return PartRecipe.model_validate({
            "name": "Guarded", "units": "mm",
            "parameters": [
                {"name": "wall_t", "value": 2},
                {"name": "plate_t", "value": 8},
                {"name": "seal_face", "value": "plate_t - 0.4", "frozen": True},
            ],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy",
                 "entities": [{"type": "rectangle", "center": [0, 0],
                               "width": 40, "height": 30}]},
                {"op": "extrude", "name": "E", "sketch": "S", "distance": "plate_t"},
            ],
            **({"dfm": dfm} if dfm else {}),
        })

    def test_a_frozen_flag_is_read(self):
        guard = guard_for_recipe(self._recipe().model_dump(mode="json"))
        assert guard.check("seal_face") is not None
        assert guard.check("plate_t") is not None

    def test_so_is_the_dfm_block(self):
        recipe = self._recipe(frozen=["wall_t"], parameters={"wall": "wall_t"})
        guard = guard_for_recipe(recipe.model_dump(mode="json"))
        assert guard.check("wall_t") is not None
        assert guard.check("seal_face") is not None, "the flag still counts"

    def test_building_installs_the_guard(self, session):
        result = build_part(session, self._recipe())
        assert result["ok"], result["errors"]
        assert session.context().frozen is not None

    def test_declaring_a_frozen_parameter_is_not_refused(self, session):
        """The guard must not block the statement that creates the freeze."""
        result = build_part(session, self._recipe())
        assert result["ok"], result["errors"]
        assert [p["name"] for p in result["parameters"]] == [
            "wall_t", "plate_t", "seal_face"]


class TestEnforcement:
    @pytest.fixture
    def built(self, session):
        build_part(session, PartRecipe.model_validate({
            "name": "Guarded", "units": "mm",
            "parameters": [
                {"name": "wall_t", "value": 2},
                {"name": "plate_t", "value": 8},
                {"name": "seal_face", "value": "plate_t - 0.4", "frozen": True},
            ],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy",
                 "entities": [{"type": "rectangle", "center": [0, 0],
                               "width": 40, "height": 30}]},
                {"op": "extrude", "name": "E", "sketch": "S", "distance": "plate_t"},
            ],
        }))
        return session.context()

    def test_a_frozen_parameter_is_refused(self, session, built):
        with pytest.raises(FrozenGeometryError):
            apply_parameter(session, built, ParameterSpec(name="seal_face", value=9))

    def test_and_so_is_one_it_depends_on(self, session, built):
        """Enforced, not merely reported. This is the hole the loop would fall in."""
        with pytest.raises(FrozenGeometryError) as caught:
            apply_parameter(session, built, ParameterSpec(name="plate_t", value=12))
        assert "seal_face" in str(caught.value)

    def test_an_unprotected_one_goes_through(self, session, built):
        apply_parameter(session, built, ParameterSpec(name="wall_t", value=2.5))

    def test_the_override_has_to_be_asked_for(self, session, built):
        applied = apply_parameter(
            session, built, ParameterSpec(name="seal_face", value=9),
            override_frozen=True,
        )
        assert applied["name"] == "seal_face"

    def test_the_refusal_says_what_to_do(self, session, built):
        with pytest.raises(FrozenGeometryError) as caught:
            apply_parameter(session, built, ParameterSpec(name="seal_face", value=9))
        assert caught.value.hint and "override_frozen" in caught.value.hint
