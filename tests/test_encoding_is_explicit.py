"""Text is decoded the same way on every machine it is read on.

`pathlib.read_text()` with no `encoding` uses the *locale* codec -- cp1252 on
a stock Windows install, UTF-8 on the Linux runners CI uses. Every document in
this repository is UTF-8, and several are matched on prose containing em
dashes, so the same assertion passes on CI and fails on the machine the work
is done on. It has cost three failures:

* `test_coverage_gaps_still_true.py` read `FEATURE_COVERAGE.md` for gap names
  that all contain an em dash, so on Windows every name became mojibake and
  four tests could not pass whatever the document said;
* `test_roadmap_still_true.py` and `test_saving.py` were the same shape in a
  different codec -- a check that could only ever report drift.

Fixing the three that were caught leaves the other forty-odd waiting their
turn, so this is the rule rather than three repairs: every text read in this
repository names its encoding. Which is a fact about forty-seven call sites
and therefore exactly the kind that needs a test rather than a habit --
`docs/DECISIONS.md`, the section on a fact stated twice.

Deliberately not covered: `open()` in binary mode and anything reading a
`.pdf` or `.ipt`, which have no encoding to name; and `errors=` without
`encoding=`, which the scan below counts as bare, because it is.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Where a read's encoding matters. `examples/` and `docs/` hold the documents;
#: these hold the code that reads them.
SOURCES = ("inventor_mcp", "scripts", "tests")

#: Calls that decode bytes into text and take an `encoding`.
DECODES = {"read_text", "write_text", "open"}


def files() -> list[pathlib.Path]:
    return sorted(path for folder in SOURCES
                  for path in (ROOT / folder).rglob("*.py")
                  if "__pycache__" not in path.parts)


def unencoded() -> list[str]:
    """Every text read or write in the tree that does not name its encoding."""
    found = []
    for path in files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else node.func.id if isinstance(node.func, ast.Name) else None)
            if name not in DECODES:
                continue
            if any(word.arg == "encoding" for word in node.keywords):
                continue
            # A binary handle has no encoding to name. `pymupdf.open` is not
            # this `open` at all -- it takes a path and reads a PDF -- and is
            # named rather than skipping every `x.open()`, since `Path.open`
            # is one of the reads this is here to catch.
            mode = next((arg for arg in node.args[1:2]
                         if isinstance(arg, ast.Constant)), None)
            if name == "open":
                if mode is not None and "b" in str(mode.value):
                    continue
                if (isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "pymupdf"):
                    continue
            found.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    return found


def test_every_text_read_names_its_encoding():
    """Otherwise it reads one thing on CI and another on the machine it was
    written on, and the difference is invisible until an em dash turns up."""
    bare = unencoded()
    assert not bare, (
        f"these decode text in the locale codec: {bare}. Pass "
        'encoding="utf-8" -- every document in this repository is UTF-8, and '
        "the default is cp1252 on Windows.")


def test_the_scan_is_actually_looking_at_the_tree():
    """A scan that matched nothing would pass this file trivially."""
    assert len(files()) > 40
    total = sum(
        1 for path in files()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in DECODES)
    assert total > 40, f"only {total} text reads found; the scan has gone blind"
