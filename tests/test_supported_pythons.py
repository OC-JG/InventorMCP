"""The Python versions this project claims, and the ones it actually proves.

Written after a single line -- an f-string with the XML attribute quotes escaped
inside it, which is a syntax error before 3.12 -- stopped
``inventor_mcp/backend/com/backend.py`` *parsing* on Python 3.11. The damage was
not confined to the COM backend: ``create_backend("auto")`` catches
``BackendUnavailableError`` and nothing else, so the fall-back to the simulator
raised ``SyntaxError`` instead of falling back, on the platform the simulator
exists to serve. Twenty-three tests went red and CI stayed red on ``main`` for
eight consecutive runs.

The signal was there and nobody read it, which is a process problem. But two
*facts* were wrong at the same time and neither had anything watching it:

* ``pyproject`` claimed ``>=3.10`` while the lowest leg CI runs is 3.11, so the
  claim had never been tested at all;
* the code needed 3.12, which neither of the other two numbers said.

Three numbers that have to agree, none of them checked against another. So they
are checked here.
"""

from __future__ import annotations

import ast
import pathlib
import re
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def declared_floor() -> tuple[int, int]:
    """The oldest Python ``pyproject`` says it supports."""
    spec = tomllib.loads((ROOT / "pyproject.toml").read_text())
    text = spec["project"]["requires-python"]
    match = re.fullmatch(r">=\s*(\d+)\.(\d+)", text.strip())
    assert match, f"requires-python is {text!r}; this test only reads a '>=X.Y' floor"
    return int(match.group(1)), int(match.group(2))


def matrix_versions() -> list[tuple[int, int]]:
    """The Pythons the offline CI matrix actually runs."""
    workflow = (ROOT / ".github/workflows/tests.yml").read_text()
    match = re.search(r"python-version:\s*\[([^\]]+)\]", workflow)
    assert match, "no python-version matrix found in tests.yml -- has the workflow moved?"
    found = re.findall(r"(\d+)\.(\d+)", match.group(1))
    assert found, f"could not read versions out of {match.group(1)!r}"
    return sorted((int(major), int(minor)) for major, minor in found)


def test_the_declared_floor_is_a_version_ci_actually_runs():
    """Otherwise the floor is a claim, not a fact.

    ``>=3.10`` with a matrix starting at 3.11 means the oldest supported
    interpreter has never once run the suite -- so "supported" means nobody has
    looked. Raise the floor or add the leg; either is fine, and this test does
    not care which, only that the two agree.
    """
    floor, matrix = declared_floor(), matrix_versions()
    assert floor == matrix[0], (
        f"pyproject claims Python {floor[0]}.{floor[1]} but the lowest leg CI runs "
        f"is {matrix[0][0]}.{matrix[0][1]}. Either add that leg to "
        ".github/workflows/tests.yml or raise requires-python to match it."
    )


@pytest.mark.parametrize(
    "path",
    sorted((ROOT / "inventor_mcp").rglob("*.py")),
    ids=lambda path: str(path.relative_to(ROOT)),
)
def test_every_module_parses_on_the_oldest_supported_python(path: pathlib.Path):
    """A grammar-level regression, caught on whatever interpreter is running.

    ``ast.parse(..., feature_version=...)`` makes a newer interpreter refuse
    syntax an older one could not read. It is not a complete backport check --
    it gates grammar (``match``, ``except*``) and not every tokenizer
    relaxation, and the f-string that started all this is a tokenizer change --
    so this is a second line, not the first. The first is the CI leg that the
    test above keeps honest.
    """
    major, minor = declared_floor()
    try:
        ast.parse(path.read_text(), filename=str(path), feature_version=(major, minor))
    except SyntaxError as exc:  # pragma: no cover - the point of the test
        pytest.fail(
            f"{path.relative_to(ROOT)}:{exc.lineno} needs newer than Python "
            f"{major}.{minor}, which pyproject says is supported: {exc.msg}"
        )
