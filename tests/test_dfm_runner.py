"""Finding the analyser, and saying clearly when it is not there.

Everything about the DFM integration is optional: it needs Node and a checkout
of a separate repository. So the failure when either is missing has to name the
missing thing and what to do, because it is the message most people will meet
first. And in CI, where neither is present, the tests that need them have to
*skip* -- a test that passes because it did not run is worse than no test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inventor_mcp.dfm import runner
from inventor_mcp.dfm.runner import (
    BRIDGE, DfmFailed, DfmUnavailable, ROOT_VARIABLES, analyse_stl,
    find_dfm_root, settings_from_roles,
)


class TestFindingIt:
    def test_an_explicit_path_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "_looks_like_dfm", lambda path: path == tmp_path)
        assert find_dfm_root(str(tmp_path)) == tmp_path

    def test_then_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "_looks_like_dfm", lambda path: path == tmp_path)
        for name in ROOT_VARIABLES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("INVENTOR_MCP_DFM_ROOT", str(tmp_path))
        assert find_dfm_root() == tmp_path

    def test_and_a_sibling_checkout_needs_no_telling(self, monkeypatch):
        here = Path(runner.__file__).resolve().parents[2]
        wanted = here.parent / "dfm"
        monkeypatch.setattr(runner, "_looks_like_dfm", lambda path: path == wanted)
        for name in ROOT_VARIABLES:
            monkeypatch.delenv(name, raising=False)
        assert find_dfm_root() == wanted

    def test_with_nothing_anywhere_it_says_what_to_do(self, monkeypatch):
        monkeypatch.setattr(runner, "_looks_like_dfm", lambda path: False)
        for name in ROOT_VARIABLES:
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(DfmUnavailable) as caught:
            find_dfm_root()
        assert "INVENTOR_MCP_DFM_ROOT" in caught.value.hint
        assert "Looked in" in caught.value.hint, "and where it looked"

    def test_a_directory_that_is_not_a_checkout_is_not_accepted(self, tmp_path):
        """And is not quietly swapped for one that is.

        An explicitly named path is not a hint. Falling through to a sibling
        checkout would analyse the part against rules the caller did not ask for
        and report success -- which is the kind of wrong answer that looks right.
        """
        with pytest.raises(DfmUnavailable) as caught:
            find_dfm_root(str(tmp_path))
        assert str(tmp_path) in caught.value.message
        assert "engine.js" in caught.value.message

    def test_the_check_is_the_rule_engine_itself(self, tmp_path):
        (tmp_path / "src" / "rules").mkdir(parents=True)
        (tmp_path / "src" / "rules" / "engine.js").write_text("", encoding="utf-8")
        assert find_dfm_root(str(tmp_path)) == tmp_path


class TestWithoutNode:
    def test_it_says_the_analyser_is_javascript(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda name: None)
        monkeypatch.setattr(runner, "_looks_like_dfm", lambda path: True)
        with pytest.raises(DfmUnavailable) as caught:
            analyse_stl(tmp_path / "nothing.stl", {}, dfm_root=str(tmp_path))
        assert "Node" in caught.value.message
        assert "npm install" in (caught.value.hint or ""), "because none is needed"


class TestWithoutAnSTL:
    def test_it_says_how_to_make_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "_looks_like_dfm", lambda path: True)
        monkeypatch.setattr(runner, "_node", lambda: "node")
        with pytest.raises(DfmFailed) as caught:
            analyse_stl(tmp_path / "missing.stl", {}, dfm_root=str(tmp_path))
        assert "export_model" in (caught.value.hint or "")


class TestSettingsFromTheModel:
    def test_a_role_supplies_its_analyser_setting(self):
        assert settings_from_roles({"wall": "wall_t"}, {"wall_t": 2.5}) == {
            "wallThk": 2.5}

    def test_the_names_are_the_analyser_s_own(self):
        out = settings_from_roles(
            {"rib_thickness": "rib_t", "boss_od": "boss_d"},
            {"rib_t": 0.9, "boss_d": 4.0},
        )
        assert out == {"ribThk": 0.9, "bossOD": 4.0}

    def test_a_base_is_carried_through(self):
        out = settings_from_roles({"wall": "w"}, {"w": 2.0}, {"material": "pp"})
        assert out == {"material": "pp", "wallThk": 2.0}

    def test_a_role_whose_parameter_has_no_value_is_left_to_the_default(self):
        """Rather than sent as zero, which would be a critical wall failure."""
        assert settings_from_roles({"wall": "w"}, {}) == {}

    def test_an_unknown_role_is_refused(self):
        with pytest.raises(ValueError, match="Unknown DFM role"):
            settings_from_roles({"wal": "w"}, {"w": 2.0})


class TestThePackaging:
    def test_the_bridge_ships_with_the_module(self):
        assert BRIDGE.is_file()

    def test_and_is_declared_as_package_data(self):
        """Otherwise a wheel installs without it and the loop cannot run at all."""
        text = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        assert "mjs" in text, "headless.mjs has to be included in the distribution"
