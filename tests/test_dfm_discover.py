"""Working out the role map from the part, and refusing to when it cannot.

Two things are being tested and only one of them is "does it find the wall".
The other is the discipline: that a parameter is mapped because a feature
demonstrably reads it, that two candidates map nothing, and that a name that
merely looks right comes back as an offer rather than an answer.
"""

from __future__ import annotations

import pytest

from inventor_mcp.dfm.discover import EVIDENCE, discover, normalise

PARAMETERS = ["box_h", "wall", "draft_a", "rib_h", "rib_t", "rib_r",
              "boss_d", "boss_w", "corner_r"]

SHELLED = [
    {"name": "Block", "type": "kExtrudeFeatureObject",
     "taper": "draft_a", "distance": "box_h"},
    {"name": "Cavity", "type": "kShellFeatureObject", "thickness": "wall"},
]


class TestEvidence:
    def test_a_shell_names_the_wall(self):
        """Not by resemblance -- by construction. The shell reads it."""
        found = discover(SHELLED, PARAMETERS)
        assert found.declaration.roles["wall"] == "wall"

    def test_and_says_what_it_read(self):
        found = discover(SHELLED, PARAMETERS)
        assert "Cavity" in found.declaration.evidence["wall"]

    def test_an_extrude_taper_names_the_draft(self):
        found = discover(SHELLED, PARAMETERS)
        assert found.declaration.roles["draft"] == "draft_a"
        assert "Block" in found.declaration.evidence["draft"]

    def test_a_discovered_role_is_marked_as_discovered(self):
        """A map mixing a person's statement with this code's inference is only
        trustworthy if the reader can tell which is which."""
        found = discover(SHELLED, PARAMETERS)
        assert found.declaration.origin["wall"] == "discovered"

    def test_the_feature_kind_is_matched_loosely(self):
        """Inventor says kShellFeatureObject; this project's own info says shell.
        A release renaming its enums must not silently stop the evidence."""
        for kind in ("shell", "Shell", "kShellFeatureObject", "ShellFeature"):
            found = discover([{"name": "C", "type": kind, "thickness": "wall"}],
                             PARAMETERS)
            assert found.declaration.roles.get("wall") == "wall", kind

    def test_an_expression_rather_than_a_bare_name_still_resolves(self):
        found = discover([{"name": "C", "type": "shell", "thickness": "wall / 2"}],
                         PARAMETERS)
        assert found.declaration.roles["wall"] == "wall"

    def test_a_property_spelt_differently_is_still_found(self):
        found = discover([{"name": "B", "type": "extrude", "TaperAngle": "draft_a"}],
                         PARAMETERS)
        assert found.declaration.roles["draft"] == "draft_a"

    def test_nested_definition_properties_are_read(self):
        """describe_feature reports the feature and its definition separately."""
        found = discover(
            [{"name": "C", "type": "shell", "definition": {"thickness": "wall"}}],
            PARAMETERS)
        assert found.declaration.roles["wall"] == "wall"


class TestWhatProvesNothing:
    def test_a_literal_thickness_maps_nothing(self):
        """It proves there is a wall and proves no parameter drives it."""
        found = discover([{"name": "C", "type": "shell", "thickness": "2 mm"}],
                         PARAMETERS)
        assert "wall" not in found.declaration.roles

    def test_an_expression_reading_no_known_parameter_maps_nothing(self):
        found = discover([{"name": "C", "type": "shell", "thickness": "d0 / 2"}],
                         PARAMETERS)
        assert "wall" not in found.declaration.roles

    def test_an_unparseable_expression_maps_nothing_rather_than_guessing(self):
        """Inventor writes expressions this parser does not accept."""
        found = discover([{"name": "C", "type": "shell", "thickness": "wall ][ 2"}],
                         PARAMETERS)
        assert "wall" not in found.declaration.roles

    def test_a_feature_of_no_interest_contributes_nothing(self):
        found = discover([{"name": "H", "type": "kHoleFeatureObject",
                           "diameter": "wall"}], PARAMETERS)
        assert found.declaration.roles == {}

    def test_an_empty_part_discovers_nothing_and_does_not_fail(self):
        found = discover([], [])
        assert found.declaration.empty
        assert found.notes


class TestAmbiguity:
    """Two answers is not an answer. Picking one is the guess this avoids."""

    def test_two_shells_reading_different_parameters_map_nothing(self):
        found = discover([
            {"name": "Inner", "type": "shell", "thickness": "wall"},
            {"name": "Outer", "type": "shell", "thickness": "boss_w"},
        ], PARAMETERS)
        assert "wall" not in found.declaration.roles

    def test_and_both_candidates_are_reported(self):
        found = discover([
            {"name": "Inner", "type": "shell", "thickness": "wall"},
            {"name": "Outer", "type": "shell", "thickness": "boss_w"},
        ], PARAMETERS)
        assert {name for name, _ in found.ambiguous["wall"]} == {"wall", "boss_w"}

    def test_with_the_call_that_settles_it(self):
        found = discover([
            {"name": "Inner", "type": "shell", "thickness": "wall"},
            {"name": "Outer", "type": "shell", "thickness": "boss_w"},
        ], PARAMETERS)
        assert any("roles=" in note for note in found.notes)

    def test_two_shells_agreeing_is_not_ambiguous(self):
        found = discover([
            {"name": "A", "type": "shell", "thickness": "wall"},
            {"name": "B", "type": "shell", "thickness": "wall"},
        ], PARAMETERS)
        assert found.declaration.roles["wall"] == "wall"
        assert not found.ambiguous

    def test_and_cites_both(self):
        found = discover([
            {"name": "A", "type": "shell", "thickness": "wall"},
            {"name": "B", "type": "shell", "thickness": "wall"},
        ], PARAMETERS)
        assert "A" in found.declaration.evidence["wall"]
        assert "B" in found.declaration.evidence["wall"]


class TestSuggestions:
    def test_a_likely_name_is_offered(self):
        found = discover(SHELLED, PARAMETERS)
        assert found.suggestions["rib_thickness"] == "rib_t"

    def test_but_never_applied(self):
        """This is the rule the whole module exists for."""
        found = discover(SHELLED, PARAMETERS)
        assert "rib_thickness" not in found.declaration.roles

    def test_and_the_offer_is_a_call_that_can_be_pasted(self):
        found = discover(SHELLED, PARAMETERS)
        assert found.as_dict()["to_accept_the_suggestions"]["roles"]["rib_height"] == "rib_h"

    def test_a_suggestion_says_it_is_not_evidence(self):
        found = discover(SHELLED, PARAMETERS)
        assert any("not evidence" in note for note in found.notes)

    def test_evidence_beats_a_name_for_the_same_role(self):
        """`wall` is mapped from the shell, so it is not also suggested."""
        found = discover(SHELLED, PARAMETERS)
        assert "wall" not in found.suggestions

    def test_one_parameter_is_not_offered_for_two_roles(self):
        found = discover([], ["rib_t"])
        assert list(found.suggestions.values()).count("rib_t") <= 1

    def test_a_longer_pattern_wins(self):
        """`rib_thickness` must not be claimed by the pattern meant for
        `thickness`, which would map it to the wall."""
        found = discover([], ["rib_thickness", "wall_thickness"])
        assert found.suggestions.get("rib_thickness") == "rib_thickness"
        assert found.suggestions.get("wall") == "wall_thickness"


class TestTheConsumedByChannel:
    def test_a_parameter_a_shell_consumes_is_the_wall(self):
        """A second, independent route to the same evidence, for when a
        feature's own expressions cannot be read but the parameter table knows
        what reads it."""
        found = discover(
            [{"name": "Cavity", "type": "kShellFeatureObject"}],
            PARAMETERS,
            consumed_by={"wall": ["Cavity"]},
        )
        assert found.declaration.roles["wall"] == "wall"
        assert "Cavity" in found.declaration.evidence["wall"]

    def test_a_parameter_that_is_not_a_parameter_is_ignored(self):
        found = discover([{"name": "Cavity", "type": "shell"}], PARAMETERS,
                         consumed_by={"not_a_parameter": ["Cavity"]})
        assert "wall" not in found.declaration.roles

    def test_the_two_channels_agreeing_is_not_ambiguous(self):
        found = discover(SHELLED, PARAMETERS, consumed_by={"wall": ["Cavity"]})
        assert found.declaration.roles["wall"] == "wall"
        assert not found.ambiguous


class TestNormalising:
    def test_it_reads_a_name_and_a_kind(self):
        facts = normalise({"name": "Cavity", "type": "shell", "thickness": "wall"})
        assert facts.name == "Cavity" and facts.kind == "shell"

    def test_non_string_values_are_left_out(self):
        facts = normalise({"name": "E", "type": "extrude", "volume": 12.5})
        assert facts.expressions == {}

    def test_a_missing_name_does_not_fail(self):
        assert normalise({"type": "shell", "thickness": "wall"}).name == ""

    def test_every_evidence_rule_names_a_real_role(self):
        from inventor_mcp.dfm.roles import ROLES
        for _kind, _properties, role, wording in EVIDENCE:
            assert role in ROLES
            assert "{feature}" in wording, "the evidence has to say what it read"


class TestAnUnreadableKindIsNotEvidence:
    """The kind is half the evidence: a rib has a thickness too.

    The first version used a property from a kind-less feature anyway, counting
    on two candidates coming out ambiguous. One candidate sailed straight
    through -- and one candidate that happens to be a rib maps the wall to the
    rib, which is the wrong-parameter mapping this module exists to prevent. So
    an unreadable kind demotes the match to an offer, the same standing as a
    likely name.
    """

    def test_a_kindless_thickness_is_offered_not_mapped(self):
        found = discover([{"name": "C", "type": "unknown",
                           "thickness": {"expression": "rib_t"}}], ["rib_t"])
        assert "wall" not in found.declaration.roles
        assert found.suggestions.get("wall") == "rib_t"

    def test_with_a_note_naming_the_probe(self):
        found = discover([{"name": "C", "type": "unknown",
                           "thickness": {"expression": "wall_t"}}], ["wall_t"])
        assert any("could not be read" in note for note in found.notes)
        assert any("probe" in note for note in found.notes)

    def test_a_readable_shell_is_still_mapped(self):
        found = discover([{"name": "C", "type": "shell",
                           "thickness": {"expression": "wall_t"}}], ["wall_t"])
        assert found.declaration.roles.get("wall") == "wall_t"

    def test_a_real_mapping_beats_a_kindless_offer(self):
        found = discover([
            {"name": "Cavity", "type": "shell", "thickness": "wall_t"},
            {"name": "Mystery", "type": "unknown", "thickness": "rib_t"},
        ], ["wall_t", "rib_t"])
        assert found.declaration.roles["wall"] == "wall_t"
        assert "wall" not in found.suggestions, "the offer must not shadow real evidence"
