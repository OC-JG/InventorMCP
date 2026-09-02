"""The datasheet-reading skill claims a technique works; prove it still does.

The corpus it was measured against is somebody's Drive folder, so nothing here
depends on it. The row-clustering claim is tested against a PDF written in the
test itself, with the words placed where a borderless datasheet table puts them.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "datasheet-reading" / "SKILL.md"
SCRIPT = ROOT / "skills" / "datasheet-reading" / "scripts" / "datasheet.py"


@pytest.fixture(scope="module")
def skill() -> str:
    return SKILL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def skill_flat(skill) -> str:
    """The skill with its line wrapping removed, for asserting on phrases that
    happen to straddle a newline."""
    return " ".join(skill.split())


@pytest.fixture(scope="module")
def datasheet_module():
    """The shipped script, imported. Skipped where PyMuPDF is not installed --
    the skill tells you to install it into a scratch directory, not the venv."""
    pytest.importorskip("pymupdf")
    spec = importlib.util.spec_from_file_location("datasheet", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestItIsAWellFormedSkill:
    def test_it_exists_with_front_matter(self, skill):
        assert skill.startswith("---\n")
        assert "\nname: datasheet-reading\n" in skill
        assert re.search(r"\ndescription: .{80,}", skill), (
            "the description is what decides whether it triggers at all")

    def test_the_description_names_what_it_triggers_on(self, skill):
        description = skill.split("description:", 1)[1].split("\n", 1)[0].lower()
        for cue in ("datasheet", "pdf", "dimension"):
            assert cue in description, cue

    def test_it_points_at_a_script_that_is_there(self, skill):
        assert "scripts/datasheet.py" in skill
        assert SCRIPT.exists()


class TestTheScriptOffersWhatTheSkillDescribes:
    def test_every_rung_of_the_ladder_has_a_subcommand(self, datasheet_module):
        for name in ("survey", "sections", "dims", "find", "render", "crop"):
            assert hasattr(datasheet_module, name), name

    def test_a_bare_call_explains_itself_rather_than_crashing(self, datasheet_module):
        assert datasheet_module.main(["datasheet.py"]) == 2


class TestRowClustering:
    """The skill's central claim: clustering words by y rebuilds the visual rows
    of a table that has no ruling lines, which is what `find_tables()` cannot do."""

    @pytest.fixture
    def borderless_table(self, tmp_path, datasheet_module):
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open()
        page = doc.new_page()
        rows = [
            ("Type", "L", "W", "H"),
            ("0402", "1.00±0.10", "0.50±0.05", "0.35±0.05"),
            ("0603", "1.60±0.10", "0.80±0.10", "0.45±0.10"),
            ("0805", "2.00±0.20", "1.25±0.20", "0.50±0.10"),
        ]
        for r, cells in enumerate(rows):
            for c, cell in enumerate(cells):
                # deliberately no lines drawn: a borderless table
                page.insert_text((60 + c * 110, 100 + r * 24), cell, fontsize=9)
        out = tmp_path / "table.pdf"
        doc.save(out)
        doc.close()
        return out

    def test_it_rebuilds_one_row_per_visual_line(self, datasheet_module, borderless_table):
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open(borderless_table)
        rows = datasheet_module.flat_rows(doc[0])
        doc.close()
        assert any(r.startswith("Type") for r in rows)
        assert sum(1 for r in rows if re.match(r"0(402|603|805)\b", r)) == 3

    def test_a_key_lookup_returns_the_whole_row(self, datasheet_module, borderless_table,
                                                capsys):
        datasheet_module.find(str(borderless_table), "0603")
        out = capsys.readouterr().out
        assert "1.60" in out and "0.80" in out and "0.45" in out
        assert "0402" not in out, "the lookup should return one row, not the table"

    def test_dimension_rows_need_several_decimals(self, datasheet_module, borderless_table,
                                                  capsys):
        datasheet_module.dims(str(borderless_table), everywhere=True)
        out = capsys.readouterr().out
        assert "0603" in out
        assert "Type L W H" not in out, "a header carries no decimals and is not a dim row"

    def test_the_threshold_is_the_documented_one(self, datasheet_module, skill_flat):
        assert datasheet_module.MIN_DECIMALS == 3
        assert "three or more decimal numbers" in skill_flat


class TestTheFactsItQuotes:
    """Numbers in the skill were measured. If one is edited, say why in the diff."""

    def test_it_says_where_the_corpus_numbers_came_from(self, skill):
        assert "232" in skill and "153" in skill, "the dedupe measurement"
        assert "0 of 14" in skill, "the filename-is-not-a-key measurement"

    def test_it_keeps_the_standard_size_cross_check(self, skill_flat):
        assert "1.60 x 0.80" in skill_flat, (
            "the 0603 check is the cheapest guard against reading the wrong row")

    def test_it_warns_about_the_expensive_calls(self, skill):
        assert "get_drawings()" in skill and "get_images()" in skill
