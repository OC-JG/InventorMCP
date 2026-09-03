"""The countable claims `docs/ROADMAP.md` makes about this repository.

`DECISIONS.md`'s standing rule is that a fact stated in two places needs a test
that the two agree, and restructure 2 of the roadmap is that rule being written
down. The roadmap exempted itself from it, and drifted: it said thirteen of
fourteen `ponytail:` markers lived in `backend/mock/` when there were sixteen,
fifteen of them there. Nothing broke; the number was simply wrong from the day
the fifteenth was added.

A roadmap is the document most likely to rot, because it is a list of things
that are not yet so, and it says so itself. So the claims it makes that are
*checkable* are checked here.

What is deliberately not checked, and why, because the omissions are the
interesting part of a file like this:

* **Dated measurements.** "1,199 lines became 763 + 183 + 409", "tool-list bytes
  35,206 -> 17,040", "63 of 64 checks passing". These are readings taken on a
  day, and the file says at the top that its dates are when a thing landed. A
  test that made them stay true would forbid the code from growing.
* **The landscape section.** 598 catalogue entries, what MecAgent and DraftAid
  claim. The file already marks those as their claims rather than measurements,
  and nothing here can reach them.
* **"five phases"** against six `### Phase` headings. Phase 0 is triage rather
  than work -- "stop the bleeding" -- so the count is a framing choice, not a
  fact, and a test encoding either reading would be asserting an opinion.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROADMAP = (ROOT / "docs/ROADMAP.md").read_text()

WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20,
}


def word(number: str) -> int:
    value = WORDS.get(number.lower())
    assert value is not None, f"unrecognised number word {number!r}"
    return value


def ponytail_markers() -> dict[str, int]:
    """Every `ponytail:` marker in the package, by the file holding it.

    The package only -- the two in `tests/` are the drift test that reads them,
    and counting a test that counts the markers among the markers would make
    the number self-referential.
    """
    counts: dict[str, int] = {}
    for path in sorted((ROOT / "inventor_mcp").rglob("*.py")):
        found = sum(1 for line in path.read_text().splitlines() if "ponytail:" in line)
        if found:
            counts[str(path.relative_to(ROOT))] = found
    return counts


class TestTheApproximationCount:
    """The claim that started this file, and the one that had gone stale."""

    def test_the_ponytail_count_and_where_they_live_are_both_right(self):
        match = re.search(
            r"(\w+) of the (\w+) `ponytail:` markers .*? live in `backend/mock/`",
            ROADMAP, re.DOTALL)
        assert match, "the ponytail sentence has moved; update this test with it"
        in_mock, total = word(match.group(1)), word(match.group(2))

        counts = ponytail_markers()
        assert sum(counts.values()) == total, (
            f"the roadmap says {total} ponytail markers; the package has "
            f"{sum(counts.values())}: {counts}")
        assert counts.get("inventor_mcp/backend/mock/backend.py") == in_mock, (
            f"the roadmap says {in_mock} of them are in the mock backend; "
            f"the counts are {counts}")

    def test_the_convention_has_not_simply_been_dropped(self):
        """An empty count would satisfy the arithmetic and mean the opposite."""
        assert sum(ponytail_markers().values()) > 0


class TestTheExampleCount:
    def test_eleven_shipped_examples_means_eleven(self):
        claims = re.findall(r"(\w+) shipped examples", ROADMAP)
        assert claims, "the shipped-example count has moved"
        shipped = len(list((ROOT / "examples").glob("*.json")))
        for claim in claims:
            assert word(claim) == shipped, (
                f"the roadmap says {claim} shipped examples; there are {shipped}")

    def test_every_example_has_a_recorded_expectation(self):
        """The roadmap leans on `examples/expected/` throughout -- the divergence
        check and the acceptance run both read it -- so an example without one
        is a part nothing compares."""
        examples = {path.stem for path in (ROOT / "examples").glob("*.json")}
        expected = {path.stem for path in (ROOT / "examples/expected").glob("*.json")}
        assert examples - expected == set(), f"no expectation for: {examples - expected}"


class TestTheRestructureCount:
    def test_four_restructures_means_four_numbered_paragraphs(self):
        match = re.search(r"^(\w+) restructures\.", ROADMAP, re.MULTILINE)
        assert match, "the restructure count sentence has moved"
        numbered = re.findall(r"^\*\*(\d+)\. ", ROADMAP, re.MULTILINE)
        assert [int(index) for index in numbered] == list(range(1, word(match.group(1)) + 1))


class TestTheCalibratedTolerances:
    """The roadmap quotes the numbers `PREDICTED` holds. Retuning one and not
    the other is exactly the drift restructure 2 is about, and the tolerances
    are the numbers most likely to be retuned."""

    #: What the Phase 1 calibration entry says each operation was set to.
    QUOTED = {"coil": 0.15, "draft": 0.20, "emboss": 0.40, "split": 0.05}

    @pytest.mark.parametrize("operation,tolerance", sorted(QUOTED.items()))
    def test_the_quoted_tolerance_is_the_live_one(self, operation, tolerance):
        from inventor_mcp.rehearsal import PREDICTED

        assert PREDICTED[operation] == pytest.approx(tolerance), (
            f"the roadmap says {operation} was set to {tolerance}; PREDICTED "
            f"says {PREDICTED[operation]}")

    def test_the_roadmap_still_quotes_those_numbers(self):
        """So that renaming or rewording the entry cannot quietly orphan the
        parametrised test above into asserting nothing.

        Read off the whitespace-collapsed text, because the sentence is wrapped
        and a test that broke when a paragraph was re-flowed would be measuring
        the line width rather than the claim.
        """
        flat = " ".join(ROADMAP.split())
        entry = re.search(r"Tolerances set to (.*?) each looser", flat)
        assert entry, "the 'Tolerances set to' sentence has moved"
        quoted = {float(value) for value in re.findall(r"0\.\d+", entry.group(1))}
        assert quoted == {0.15, 0.20, 0.40}

    def test_each_calibrated_operation_has_its_instrument(self):
        """`examples/calibration/` holds one recipe per calibrated operation --
        that is the entry's claim, and a missing one means the number cannot be
        re-measured when someone doubts it."""
        recipes = " ".join(
            path.read_text() for path in (ROOT / "examples/calibration").glob("*.json"))
        for operation in self.QUOTED:
            assert f'"{operation}"' in recipes, (
                f"no calibration recipe exercises {operation}")


class TestTheFilesTheRoadmapSaysExist:
    def test_the_split_produced_the_modules_it_names(self):
        """Restructure 4 names the three files `builder.py` became."""
        for module in ("builder.py", "checks.py", "rehearsal.py"):
            assert (ROOT / "inventor_mcp" / module).exists(), module

    #: What the roadmap calls a thing, and where that thing actually is. It
    #: refers to its neighbours in `docs/` by bare filename, being one of them.
    POINTS_AT = {
        "examples/calibration": "examples/calibration",
        "examples/expected": "examples/expected",
        "FEATURE_COVERAGE.md": "docs/FEATURE_COVERAGE.md",
        "DECISIONS.md": "docs/DECISIONS.md",
        "ARCHITECTURE.md": "docs/ARCHITECTURE.md",
        "INVENTOR_SETUP.md": "docs/INVENTOR_SETUP.md",
        "scripts/dump_constants.py": "scripts/dump_constants.py",
    }

    @pytest.mark.parametrize("named,path", sorted(POINTS_AT.items()))
    def test_the_paths_it_points_the_reader_at_are_there(self, named, path):
        assert named in ROADMAP, f"{named} is no longer referenced; drop it from this test"
        assert (ROOT / path).exists(), f"the roadmap points at {named}, which does not exist"


class TestPhaseItemsAreHonest:
    def test_no_phase_item_is_both_ticked_and_struck_through_without_a_reason(self):
        """The file's own rule: an abandoned item is struck through with a
        sentence saying why, rather than deleted. A bare `~~...~~` with nothing
        after it is the deletion that rule forbids, wearing a costume.
        """
        for line in ROADMAP.splitlines():
            if "~~" not in line:
                continue
            after = line.split("~~")[-1].strip(" *—-")
            assert after, f"struck through with no reason given: {line.strip()}"

    def test_every_ticked_item_carries_a_date(self):
        """`Keeping this file true` says an item is ticked in the commit that
        makes it true. A tick with no date is a claim with no receipt.

        Read per *item* rather than per line: an item runs from its `- [x]` to
        the next checkbox or the next heading, and most of them put the date a
        line or two into the prose. A line-by-line version of this test called
        five sound entries undated, which is the false-positive habit the
        warnings in this repo are written to avoid.
        """
        undated = []
        for block in self.items():
            head = " ".join(block.split())
            if not head.startswith("- [x]"):
                continue
            if not re.search(r"\d{4}-\d{2}-\d{2}", head):
                undated.append(head[:70])
        # Phase 0's two carry their date on the phase heading instead: the
        # branch merge and the required status check landed together, and the
        # second is a repository setting that no file can evidence anyway.
        assert len(undated) == 2, f"ticked without a date: {undated}"

    @staticmethod
    def items() -> list[str]:
        """The roadmap's checklist entries, each with its continuation lines."""
        blocks: list[str] = []
        for line in ROADMAP.splitlines():
            if re.match(r"\s*- \[[ x]\]", line):
                blocks.append(line)
            elif blocks and line.startswith((" ", "\t")) and line.strip():
                blocks[-1] += "\n" + line
            elif line.startswith("#"):
                blocks.append("")  # a heading ends the item above it
        return [block for block in blocks if block]
