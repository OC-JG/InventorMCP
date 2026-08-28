"""What a part declares about its own manufacture, and which source wins.

The asymmetry is the point, and it is the only thing here that is a safety
property rather than a convenience: roles from a stronger source replace weaker
ones, and frozen names are unioned across every source. If any source could
take a freeze off, every freeze would be advisory.
"""

from __future__ import annotations

import json

import pytest

from inventor_mcp.dfm.declaration import (
    Declaration, DeclarationError, from_recipe, given, merge, read_sidecar,
    sidecar_for, write_sidecar,
)


class TestReadingOne:
    def test_the_recipe_form_is_read(self):
        d = Declaration.from_dict({"parameters": {"wall": "wall_t"},
                                   "frozen": ["bore"], "settings": {"material": "pp"}})
        assert d.roles == {"wall": "wall_t"}
        assert d.frozen == ["bore"]
        assert d.settings == {"material": "pp"}

    def test_and_so_is_a_roles_key(self):
        """Written by hand, or by an earlier version of this."""
        assert Declaration.from_dict({"roles": {"wall": "w"}}).roles == {"wall": "w"}

    def test_nothing_is_an_empty_declaration(self):
        assert Declaration.from_dict(None).empty

    def test_an_unknown_role_is_refused(self):
        with pytest.raises(DeclarationError, match="Unknown DFM role"):
            Declaration(roles={"wal": "w"})

    def test_the_refusal_lists_the_real_roles(self):
        with pytest.raises(DeclarationError) as caught:
            Declaration(roles={"wal": "w"})
        assert "wall" in caught.value.hint

    def test_a_frozen_list_that_is_not_a_list_is_refused(self):
        """Read as 'nothing is protected' it would take the protection off
        exactly when it was most wanted."""
        with pytest.raises(DeclarationError, match="nothing is protected"):
            Declaration.from_dict({"frozen": "bore"})

    def test_every_role_records_where_it_came_from(self):
        d = Declaration.from_dict({"parameters": {"wall": "w"}}, source="the recipe")
        assert d.origin == {"wall": "the recipe"}


class TestWhichSourceWins:
    def test_a_stronger_source_replaces_a_role(self):
        weak = Declaration(roles={"wall": "guessed"}, origin={"wall": "discovered"})
        strong = given(roles={"wall": "stated"})
        assert merge(weak, strong).roles == {"wall": "stated"}

    def test_and_the_report_says_which_won(self):
        weak = Declaration(roles={"wall": "guessed"}, origin={"wall": "discovered"})
        merged = merge(weak, given(roles={"wall": "stated"}))
        assert merged.origin["wall"] == "given at the call"

    def test_evidence_for_a_replaced_role_is_dropped(self):
        """It was evidence for a different answer, and leaving it would read as
        evidence for this one."""
        weak = Declaration(roles={"wall": "guessed"}, origin={"wall": "discovered"},
                           evidence={"wall": "a shell takes its thickness from it"})
        merged = merge(weak, given(roles={"wall": "stated"}))
        assert "wall" not in merged.evidence

    def test_a_role_nobody_stronger_mentioned_survives(self):
        weak = Declaration(roles={"wall": "w"}, origin={"wall": "discovered"})
        merged = merge(weak, given(roles={"draft": "d"}))
        assert merged.roles == {"wall": "w", "draft": "d"}


class TestFreezesOnlyEverAdd:
    def test_they_are_unioned(self):
        merged = merge(Declaration(frozen=["a"]), Declaration(frozen=["b"]),
                       given(frozen=["c"]))
        assert set(merged.frozen) == {"a", "b", "c"}

    def test_a_later_source_cannot_take_one_off(self):
        """The safety property. If it could, every freeze would be advisory."""
        merged = merge(Declaration(frozen=["seal_face"]), given(frozen=[]))
        assert merged.frozen == ["seal_face"]

    def test_duplicates_do_not_pile_up(self):
        merged = merge(Declaration(frozen=["a"]), given(frozen=["a"]))
        assert merged.frozen == ["a"]

    def test_features_too(self):
        merged = merge(Declaration(frozen_features=["Gasket"]),
                       given(frozen_features=[]))
        assert merged.frozen_features == ["Gasket"]


class TestSettings:
    def test_a_later_source_wins(self):
        merged = merge(Declaration(settings={"material": "abs"}),
                       given(settings={"material": "pp"}))
        assert merged.settings["material"] == "pp"

    def test_but_checks_merge_key_by_key(self):
        """Turning one check off must not turn the others back on."""
        merged = merge(Declaration(settings={"checks": {"ribs": False, "flow": True}}),
                       given(settings={"checks": {"transitions": True}}))
        assert merged.settings["checks"] == {
            "ribs": False, "flow": True, "transitions": True}


class TestFromARecipe:
    def test_the_dfm_block_is_read(self):
        d = from_recipe({"dfm": {"parameters": {"wall": "w"}, "frozen": ["bore"]}})
        assert d.roles == {"wall": "w"} and d.frozen == ["bore"]

    def test_and_a_parameter_marked_frozen(self):
        d = from_recipe({"parameters": [{"name": "seal", "value": 1, "frozen": True},
                                        {"name": "w", "value": 2}]})
        assert d.frozen == ["seal"]

    def test_both_at_once_without_duplicating(self):
        d = from_recipe({"parameters": [{"name": "seal", "value": 1, "frozen": True}],
                         "dfm": {"frozen": ["seal", "bore"]}})
        assert sorted(d.frozen) == ["bore", "seal"]

    def test_a_recipe_with_no_dfm_block_declares_nothing(self):
        assert from_recipe({"name": "X", "parameters": []}).empty

    def test_none_is_not_an_error(self):
        assert from_recipe(None).empty


class TestTheSidecar:
    def test_it_round_trips(self, tmp_path):
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"")
        written = Declaration(roles={"wall": "wall_t"}, frozen=["bore"],
                              settings={"material": "abs"})
        write_sidecar(part, written)
        read = read_sidecar(part)
        assert read.roles == written.roles
        assert read.frozen == written.frozen
        assert read.settings == written.settings

    def test_it_lands_next_to_the_part(self, tmp_path):
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"")
        assert write_sidecar(part, Declaration()) == tmp_path / "bracket.dfm.json"

    def test_no_sidecar_is_none_not_an_error(self, tmp_path):
        assert read_sidecar(tmp_path / "bracket.ipt") is None

    def test_an_unreadable_one_is_an_error(self, tmp_path):
        """Somebody wrote it on purpose. Running as though it were absent would
        ignore whatever it protects."""
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"")
        sidecar_for(part).write_text("{ not json", encoding="utf-8")
        with pytest.raises(DeclarationError, match="could not be read"):
            read_sidecar(part)

    def test_it_says_what_it_is(self, tmp_path):
        """So a future reader can tell what they are looking at."""
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"")
        write_sidecar(part, Declaration(roles={"wall": "w"}))
        data = json.loads(sidecar_for(part).read_text(encoding="utf-8"))
        assert data["format"].startswith("inventor-mcp/dfm-declaration/")

    def test_the_stored_form_is_the_recipe_form(self, tmp_path):
        """So it can be pasted into a recipe's dfm block, and read back by one."""
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"")
        write_sidecar(part, Declaration(roles={"wall": "w"}, frozen=["b"]))
        data = json.loads(sidecar_for(part).read_text(encoding="utf-8"))
        assert data["parameters"] == {"wall": "w"}
        assert from_recipe({"dfm": data}).roles == {"wall": "w"}


class TestDescribing:
    def test_it_names_the_unmapped_roles(self):
        described = Declaration(roles={"wall": "w"}).describe()
        assert "draft" in described["unmapped"]
        assert "wall" not in described["unmapped"]

    def test_and_shows_the_evidence_for_an_inferred_one(self):
        d = Declaration(roles={"wall": "w"}, origin={"wall": "discovered"},
                        evidence={"wall": "the Shell feature Cavity uses it"})
        described = d.describe()
        assert described["roles"]["wall"]["from"] == "discovered"
        assert "Shell" in described["roles"]["wall"]["evidence"]

    def test_a_stated_role_needs_no_evidence(self):
        described = given(roles={"wall": "w"}).describe()
        assert "evidence" not in described["roles"]["wall"]
