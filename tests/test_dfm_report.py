"""Reading the DFM tool's export.

The fixtures under ``tests/fixtures/dfm`` are real output from the analyser, run
through the headless bridge on meshes with known answers, rather than JSON
written by hand to match this reader. A hand-written fixture tests that the
reader agrees with whoever wrote the fixture.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from inventor_mcp.dfm.report import DfmReportError, read_report, worse

FIXTURES = Path(__file__).parent / "fixtures" / "dfm"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def clean():
    return read_report(load("clean"))


@pytest.fixture
def many():
    return read_report(load("many_findings"))


class TestWhatItReads:
    def test_the_score_and_grade(self, clean):
        assert clean.score == 100
        assert clean.grade == "PRODUCTION READY"

    def test_a_clean_part_has_no_findings(self, clean):
        assert clean.findings == ()

    def test_findings_come_worst_first(self, many):
        keys = [c.key for c in many.findings]
        assert keys[0] == "wall", "the 22-point critical should lead"
        severities = [c.severity for c in many.findings]
        assert severities == sorted(severities, key=lambda s: -["none", "minor", "major", "critical"].index(s))

    def test_a_check_that_deducted_nothing_is_not_a_finding(self, many):
        """The flow advisory reports without charging, and is not a defect."""
        flow = many.check("flow")
        assert flow is not None and flow.deduction == 0
        assert "flow" not in {c.key for c in many.findings}

    def test_the_declared_inputs_come_through(self, many):
        assert many.declared_number("ribThk") == 1.9
        assert many.declared_number("bossOD") == 6.0

    def test_and_the_measured_ones(self, many):
        assert many.measured("wall_median_mm") == pytest.approx(0.5, abs=0.01)

    def test_the_material_limits_arrive_as_numbers(self, many):
        assert many.limits is not None
        assert many.limits.wall_lo == 1.2
        assert many.limits.wall_hi == 3.5
        assert many.limits.required_draft == pytest.approx(1.1)

    def test_the_metrics_stay_as_the_tool_wrote_them(self, many):
        wall = many.check("wall")
        assert ("Material band", "1.2–3.5 mm") in wall.metrics


class TestTheWallItJudgesOn:
    def test_the_sphere_figure_wins(self):
        """The tool judges on the inscribed sphere; so must anything acting on it."""
        data = load("many_findings")
        data["mesh_summary"]["wall_sphere_median_mm"] = 1.4
        data["mesh_summary"]["wall_median_mm"] = 3.0
        assert read_report(data).wall_nominal == 1.4

    def test_falling_back_to_the_ray_when_there_is_no_sphere(self):
        data = load("many_findings")
        data["mesh_summary"]["wall_sphere_median_mm"] = None
        assert read_report(data).wall_nominal == pytest.approx(0.5, abs=0.01)


class TestMissingIsNotZero:
    """A field an older record does not carry must not read as 0.

    A missing wall measurement read as 0.00 mm is a critical wall failure nobody
    has, and a loop acting on it would thin a part that was already right.
    """

    def test_an_absent_measurement_is_none(self, many):
        assert many.measured("a_field_that_does_not_exist") is None

    def test_an_absent_limits_block_is_none(self):
        data = load("many_findings")
        del data["material_limits"]
        assert read_report(data).limits is None

    def test_a_null_is_none_not_zero(self):
        data = load("many_findings")
        data["mesh_summary"]["wall_median_mm"] = None
        assert read_report(data).measured("wall_median_mm") is None

    def test_a_boolean_is_not_a_number(self):
        data = load("many_findings")
        data["mesh_summary"]["wall_median_mm"] = True
        assert read_report(data).measured("wall_median_mm") is None


class TestTrust:
    def test_a_good_mesh_is_trustworthy(self, clean):
        assert clean.trustworthy

    def test_an_inch_scaled_one_is_not(self):
        """Its score is arithmetic, not a judgement about the part."""
        report = read_report(load("inch_scaled"))
        assert report.confidence == "unusable"
        assert not report.trustworthy
        assert report.score is not None, "still read, just not to be acted on"

    def test_an_unanalysable_mesh_is_not(self, many):
        data = copy.deepcopy(many.raw)
        data["mesh_health"]["analysable"] = False
        assert not read_report(data).trustworthy


class TestRefusals:
    def test_something_that_is_not_a_report(self):
        with pytest.raises(DfmReportError):
            read_report({"hello": "world"})

    def test_a_list(self):
        with pytest.raises(DfmReportError):
            read_report([1, 2, 3])

    def test_the_hint_names_the_export(self):
        with pytest.raises(DfmReportError) as caught:
            read_report({"score": 90})
        assert "export" in (caught.value.hint or "").lower()

    def test_a_wrapper_is_unwrapped(self, many):
        assert read_report({"report": many.raw}).score == many.score


class TestSeverityOrder:
    def test_worse_picks_the_worse(self):
        assert worse("minor", "critical") == "critical"
        assert worse("major", "minor") == "major"

    def test_an_unknown_band_is_treated_as_none(self):
        assert worse("nonsense", "minor") == "minor"
