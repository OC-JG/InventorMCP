"""The instruments in `examples/calibration/`, checked before they are trusted.

Four entries in `PREDICTED` sit at a placeholder 0.5 -- `coil`, `draft`,
`emboss` and `split` -- because no shipped example uses those operations, so no
acceptance run has ever compared one with Inventor. At 0.5 the divergence check
would wave through a fillet applied to the wrong edge, which is the thing it
exists to catch.

These recipes are how that gets measured, so they have to be right about what
they are measuring: the operation under test has to be last, everything before
it has to be something the simulator gets exactly right, and the numbers the
directory's README quotes have to be the numbers the simulator actually
produces. A calibration fixture that has quietly stopped isolating its operation
produces a tolerance for something else entirely.

Nothing here can check what Inventor does. That needs the machine with Inventor
on it, and `scripts/live_acceptance.py --only calibration` is where it happens.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from inventor_mcp.rehearsal import PREDICTED, rehearse
from inventor_mcp.schema import PartRecipe

ROOT = pathlib.Path(__file__).resolve().parent.parent
CALIBRATION = ROOT / "examples" / "calibration"
RECIPES = sorted(CALIBRATION.glob("*.json"))

#: The entries this directory exists to measure: every operation whose tolerance
#: is the placeholder rather than something somebody wrote down after a run.
PLACEHOLDER = 0.5


def recipe_for(path: pathlib.Path) -> PartRecipe:
    return PartRecipe.model_validate(json.loads(path.read_text()))


def rehearsal_of(path: pathlib.Path) -> dict:
    report = rehearse(recipe_for(path))
    assert report["ok"], f"{path.stem} does not rehearse: {report['findings']}"
    return report


def test_there_are_recipes_at_all():
    assert RECIPES, f"nothing in {CALIBRATION}"


@pytest.mark.parametrize("path", RECIPES, ids=lambda p: p.stem)
class TestEachOneIsolatesWhatItMeasures:
    def test_it_rehearses(self, path: pathlib.Path):
        rehearsal_of(path)

    def test_the_operation_under_test_is_the_last_one(self, path: pathlib.Path):
        """So nothing after it can move the volume it is being blamed for."""
        operations = recipe_for(path).operations
        assert operations[-1].op in PREDICTED, (
            f"{path.stem} ends with {operations[-1].op}, which has no tolerance "
            "to calibrate")

    def test_everything_before_it_is_something_the_simulator_gets_right(
            self, path: pathlib.Path):
        """Otherwise the difference belongs to two operations and calibrates neither.

        A tolerance of 0.02 is the simulator claiming a fifth of a percent; a
        sketch or a work plane moves no volume at all. Anything looser in the
        run-up and the number this fixture produces is a sum.
        """
        for op in recipe_for(path).operations[:-1]:
            tolerance = PREDICTED.get(op.op)
            assert tolerance is None or tolerance <= 0.02, (
                f"{path.stem} runs {op.op} (tolerance {tolerance}) before the "
                "operation under test, so their errors add")


def test_every_placeholder_tolerance_has_an_instrument():
    """The point of the directory: no uncalibrated operation left unmeasurable.

    Not one recipe each, and not only placeholders. An operation whose tolerance
    has since been set from a live run keeps its recipe, because that is what
    re-measures it on the next Inventor release; and `split` has two, because
    telling its two possible explanations apart needs the same cut made both
    ways. What must hold is that nothing is left at the placeholder with no way
    to measure it, which is the state this directory was created to end.
    """
    placeholders = {op for op, value in PREDICTED.items() if value >= PLACEHOLDER}
    measured = {recipe_for(path).operations[-1].op for path in RECIPES}
    assert placeholders <= measured, (
        "at the placeholder tolerance and no recipe measures it: "
        f"{sorted(placeholders - measured)}. Add one to examples/calibration/, "
        "isolating it as the last operation.")


def test_no_instrument_measures_something_the_guard_ignores():
    """A recipe whose last operation has no tolerance calibrates nothing."""
    for path in RECIPES:
        last = recipe_for(path).operations[-1].op
        assert last in PREDICTED, (
            f"{path.stem} ends with {last}, which the divergence check does not "
            "compare at all, so nothing it measures reaches anything")


class TestTheTwoThatCanBeWorkedOut:
    """Predictions, not observations. The live run either confirms or finds something.

    Both are stated in `examples/calibration/README.md`, and both are the whole
    reason those two recipes are shaped the way they are.
    """

    def volume_after(self, stem: str) -> list[float]:
        steps = rehearsal_of(CALIBRATION / f"{stem}.json")["steps"]
        return [(s.get("measured") or {}).get("volume_cm3") for s in steps]

    def test_the_drafted_block_is_a_wedge_estimate_over_a_frustum(self):
        """72 - 45k + 9k^2 for k = 2 tan 3 deg is the true solid; the estimate is linear."""
        import math

        k = 2 * math.tan(math.radians(3))
        truth = 72 - 45 * k + 9 * k**2
        volumes = self.volume_after("drafted_block")
        assert volumes[1] == pytest.approx(72.0)
        estimate = volumes[-1]
        assert estimate == pytest.approx(67.2833, abs=5e-4)
        assert truth == pytest.approx(67.3821, abs=5e-4)
        # The estimate removes more than the frustum does, by about 2%.
        removed_estimate, removed_truth = 72 - estimate, 72 - truth
        assert removed_estimate > removed_truth
        assert (removed_estimate - removed_truth) / removed_truth == pytest.approx(
            0.0214, abs=5e-4)

    def test_the_stepped_split_keeps_the_part_and_not_a_share_of_the_box(self):
        """19.2 of base and 1.6 of boss is 20.8 kept, which is now what it says.

        It used to keep 1.2/2.8 of 27.2, or 11.657: the share of the *bounding
        box* below the plane, on the assumption that a part is spread evenly
        either side of a cut. A base slab with a boss on it is not, and 44% is
        not a rounding error. The trim reads the ledger of prisms now, so a
        prismatic part comes out exact.
        """
        volumes = self.volume_after("stepped_split")
        assert volumes[3] == pytest.approx(27.2), "base plus boss, before the cut"
        truth = 6.0 * 4.0 * 0.8 + 2.0 * 2.0 * 0.4
        assert truth == pytest.approx(20.8)
        assert volumes[-1] == pytest.approx(truth, abs=5e-6)
        # And the old answer is gone rather than coincidentally close.
        assert volumes[-1] != pytest.approx(27.2 * (1.2 / 2.8), abs=1e-3)

    def test_the_other_side_of_the_same_cut_is_the_complement(self):
        """The two halves have to add up, or the trim is inventing material."""
        keeps_below = self.volume_after("stepped_split")[-1]
        keeps_above = self.volume_after("stepped_split_negative")[-1]
        assert keeps_below + keeps_above == pytest.approx(27.2, abs=5e-6)

    def test_the_origin_plane_cut_keeps_what_lies_under_the_origin(self):
        """A part straddling z = 0: 19.2 below it, 8.0 above, and no work plane."""
        volumes = self.volume_after("origin_plane_split")
        assert volumes[-2] == pytest.approx(27.2)
        assert volumes[-1] == pytest.approx(19.2, abs=5e-6)


def test_the_readme_quotes_the_numbers_the_simulator_produces():
    """A duplication, so a test that the two agree -- the rule from DECISIONS.md."""
    readme = (CALIBRATION / "README.md").read_text()
    # The table is prose, so it uses a typographic minus (U+2212) rather than a
    # hyphen. Matching only the hyphen read every removal as an addition and the
    # sign error was in the test, not in the thing it was checking.
    rows = re.findall(r"^\| `([a-z_]+)` \| `([a-z]+)` \| ([\u2212+-]?[\d.]+) cm³ \|",
                      readme, re.MULTILINE)
    described = {stem for stem, _, _ in rows}
    assert described == {path.stem for path in RECIPES}, (
        f"the table describes {sorted(described)} and the directory holds "
        f"{sorted(path.stem for path in RECIPES)}")
    for stem, op, quoted in rows:
        steps = rehearsal_of(CALIBRATION / f"{stem}.json")["steps"]
        volumes = [(s.get("measured") or {}).get("volume_cm3") for s in steps]
        volumes = [v for v in volumes if v is not None]
        delta = volumes[-1] - (volumes[-2] if len(volumes) > 1 else 0.0)
        assert recipe_for(CALIBRATION / f"{stem}.json").operations[-1].op == op, (
            f"the README says {stem} measures {op}")
        assert float(quoted.replace("\u2212", "-")) == pytest.approx(delta, abs=5e-4), (
            f"the README says {stem}'s {op} moves {quoted} cm³ and the simulator "
            f"says {delta:.4f}")
