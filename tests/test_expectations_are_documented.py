"""The numbers in `examples/expected/README.md`, against the files it describes.

That README is a table of volumes beside the volumes it describes, which is a
duplication, and it had drifted the way duplications do. Two of its derivations
were arithmetic for a different part than the one they named:

* `pipe_bend` was given as Pappus over a solid circle, 22.206610, where the
  recipe sweeps an annulus and the file says 7.994380. The derivation had
  forgotten the bore -- on a *pipe*.
* `threaded_boss` was given as two bosses less one tap hole, 33.872087, where
  the part is a plate carrying two bosses, each tapped. The derivation had left
  out the 38.4 cm^3 plate the bosses stand on and half the holes.

Both files were right and both had been confirmed against a live Inventor. Only
the prose was wrong, which is the worst way round: the reader checking whether a
number is trustworthy is reading exactly the part nothing verifies.

`docs/DECISIONS.md` says documentation that has drifted is worse than none. The
rule this repository applies to a duplication is a test the day it is created,
so here is that test.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPECTED = ROOT / "examples" / "expected"
README = EXPECTED / "README.md"

#: `| `name` | 12.345 cm³ | ... |` -- the shape of a row in every table that
#: quotes a volume. The unit is what separates those from the comparison table,
#: which is read separately below.
ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|\s*([\d.]+)\s*cm³\s*\|", re.MULTILINE)


def documented() -> dict[str, float]:
    found = {name: float(value) for name, value in ROW.findall(README.read_text())}
    assert found, "no volume rows found in the README -- have the tables moved?"
    return found


def fixtures() -> dict[str, float]:
    return {
        path.stem: json.loads(path.read_text())["volume_cm3"]
        for path in EXPECTED.glob("*.json")
    }


@pytest.mark.parametrize("name", sorted(documented()))
def test_every_documented_volume_matches_its_file(name: str):
    """Six decimal places, because that is what the files carry."""
    assert documented()[name] == pytest.approx(fixtures()[name], abs=5e-7), (
        f"the README says {documented()[name]} for {name} and "
        f"examples/expected/{name}.json says {fixtures()[name]}"
    )


def test_every_expectation_file_is_described():
    """An undocumented expectation is a number nobody has to justify."""
    undescribed = sorted(set(fixtures()) - set(documented()))
    assert not undescribed, (
        f"shipped with no row in examples/expected/README.md: {undescribed}. "
        "Say where the number came from -- derived, confirmed, or recorded."
    )


def test_nothing_is_described_that_does_not_exist():
    invented = sorted(set(documented()) - set(fixtures()))
    assert not invented, f"described in the README with no file: {invented}"


def test_the_comparison_table_quotes_every_example_and_agrees_with_the_files():
    """The simulator-versus-Inventor table repeats the volumes a third time.

    Its left-hand column is what Inventor *measured*, printed to four decimals by
    the acceptance run, and the file may hold a *derived* figure instead -- those
    are different numbers on purpose, and `threaded_boss` is 0.0002 apart. So
    this compares at 5e-4, which is `TOLERANCE` in `scripts/live_acceptance.py`:
    the distance at which that run itself stops calling two numbers the same.
    Anything further apart means the table and the file disagree about the part.
    """
    table = README.read_text().split("## How close the simulator is")[1]
    rows = re.findall(r"^\|\s*`([a-z_]+)`\s*\|\s*([\d.]+)\s*\|", table, re.MULTILINE)
    assert len(rows) == len(fixtures()), (
        f"the comparison table has {len(rows)} rows for {len(fixtures())} examples")
    for name, inventor in rows:
        assert float(inventor) == pytest.approx(fixtures()[name], abs=5e-4), (
            f"the comparison table says Inventor measured {inventor} for {name}, "
            f"and examples/expected/{name}.json says {fixtures()[name]}")
