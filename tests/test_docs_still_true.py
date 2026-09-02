"""The countable claims the docs make about this repository.

`docs/DECISIONS.md` puts it plainly: documentation that has drifted is worse
than none, because it is believed. `tests/test_skill.py` already holds the
Skill's factual claims to that standard. This file does the same for the ones in
the README and the architecture note that are simply counts and lists, which are
the ones that go stale silently -- nothing breaks, and the number is just wrong
from then on.

Every check here found something when it was written:

* the architecture note said nineteen tools; there are thirty;
* the README's tool table was missing `check_against_drawing` and
  `drawing_reading_schema`, both registered and neither mentioned anywhere;
* the README said "all five examples" and listed five, next to a directory
  holding eleven.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
ARCHITECTURE = (ROOT / "docs/ARCHITECTURE.md").read_text()

WORDS = {"nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
         "twenty-nine": 29, "thirty-one": 31, "thirty-two": 32}


@pytest.fixture(scope="module")
def registered_tools() -> set[str]:
    from inventor_mcp.server import create_server

    server = create_server("mock")
    return {tool.name for tool in asyncio.run(server.list_tools())}


def test_the_architecture_note_counts_the_tools_correctly(registered_tools):
    """It said nineteen for a long time, through eleven more being added."""
    match = re.search(r"^([A-Za-z-]+) tools rather than one per feature type",
                      ARCHITECTURE, re.MULTILINE)
    assert match, "the tool-count sentence has moved; update this test with it"
    claimed = WORDS.get(match.group(1).lower())
    assert claimed is not None, f"unrecognised number word {match.group(1)!r}"
    assert claimed == len(registered_tools)


def test_the_readme_table_lists_every_tool(registered_tools):
    """A tool nobody documents is a tool nobody uses on purpose."""
    table = re.search(r"^\| Tool \| What it is for \|$.*?^$", README,
                      re.MULTILINE | re.DOTALL)
    assert table, "the tool table has moved; update this test with it"
    documented = set(re.findall(r"`([a-z_]+)`", table.group(0)))
    missing = registered_tools - documented
    assert not missing, f"registered but absent from the README table: {sorted(missing)}"
    invented = documented - registered_tools
    assert not invented, f"in the README table and not registered: {sorted(invented)}"


def test_the_readme_names_every_shipped_example():
    """Each is exercised by the suite, so an unnamed one is a silent one."""
    shipped = {path.stem for path in (ROOT / "examples").glob("*.json")}
    section = README[README.index("more in [`examples/`]"):][:600]
    words = set(re.findall(r"[a-z]+", section.lower()))
    unnamed = sorted(stem for stem in shipped
                     if not all(part in words for part in stem.split("_")))
    assert not unnamed, f"shipped and not named in the README: {unnamed}"


def test_the_examples_directory_and_the_claim_about_it_agree():
    match = re.search(r"([A-Za-z]+) more in \[`examples/`\]", README)
    assert match, "the examples sentence has moved; update this test with it"
    claimed = {"eleven": 11, "ten": 10, "twelve": 12, "thirteen": 13,
               "nine": 9, "eight": 8}.get(match.group(1).lower())
    assert claimed is not None, f"unrecognised number word {match.group(1)!r}"
    assert claimed == len(list((ROOT / "examples").glob("*.json")))
