"""The gap list in `docs/FEATURE_COVERAGE.md`, held against the schema.

`DECISIONS.md`'s standing rule is that a fact stated in two places needs a test
that the two agree, and `test_roadmap_still_true.py` applied it to the roadmap.
This file applies it to the one section of `FEATURE_COVERAGE.md` that is a claim
about the code rather than about Inventor: *Gaps that are not feature
collections*, the list of things the server cannot do.

It found two when it was written. `work_axis` and `work_point` landed as recipe
operations, and `hole` gained `bodies`, in the same change -- and both bullets
still said the gap was open. Nothing broke; the list simply told a reader to
reach for a workaround that had stopped being necessary, which is the failure
mode a gap list has.

The check is deliberately one-directional: a gap declared **open** while the
schema has an operation for it is a lie the reader acts on, so that fails. A gap
struck through while the operation is absent is caught by the second test, which
is the same mistake in reverse -- a tick with nothing behind it. What is *not*
checked is the prose: whether a closed gap's paragraph describes the closure
accurately is not a countable fact, and a test pretending otherwise would be
matching wording rather than truth.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
COVERAGE = (ROOT / "docs/FEATURE_COVERAGE.md").read_text()

# Which recipe operation, if it exists, closes each gap the list names. A gap
# with no operation that could close it -- sketch fillet, project geometry --
# maps to nothing and is skipped: those are sketch-level abilities rather than
# operations, so the schema cannot answer for them.
GAP_OPERATIONS = {
    "No work axis or work point": {"work_axis", "work_point"},
    "`hole` still only drills the primary body": {"hole:bodies"},
}


def gap_bullets() -> dict[str, bool]:
    """Each bullet in the gap section, mapped to whether it reads as still open.

    A bullet struck through with `~~` is a closed gap; the section keeps them
    rather than deleting them, because the roadmap's rule is that an abandoned
    or completed item is struck through with a sentence saying why.
    """
    section = re.search(
        r"^## Gaps that are not feature collections$(.*?)^## ",
        COVERAGE, re.MULTILINE | re.DOTALL)
    assert section, "the gap section has moved; update this test with it"

    bullets: dict[str, bool] = {}
    for body in re.findall(r"^\* (.+?)(?=^\* |\Z)", section.group(1),
                           re.MULTILINE | re.DOTALL):
        headline = body.split(".**")[0].lstrip("~").lstrip("*")
        bullets[headline.strip()] = not body.startswith("~~")
    assert bullets, "no bullets parsed out of the gap section"
    return bullets


def schema_can(capability: str) -> bool:
    """Whether the schema offers an operation, or a field on one."""
    from inventor_mcp import schema

    operations = {
        cls.model_fields["op"].default: cls
        for _, cls in inspect.getmembers(schema, inspect.isclass)
        if cls.__name__.endswith("Op") and "op" in getattr(cls, "model_fields", {})
    }
    if ":" not in capability:
        return capability in operations
    op, field = capability.split(":", 1)
    return op in operations and field in operations[op].model_fields


def test_the_gaps_the_list_names_are_all_ones_we_can_answer_for():
    """Otherwise a renamed bullet would silently stop being checked."""
    unknown = set(GAP_OPERATIONS) - set(gap_bullets())
    assert not unknown, (
        f"this test names gaps the document does not: {sorted(unknown)}. A "
        "bullet was reworded or removed; follow it here rather than leaving a "
        "check pointed at nothing.")


@pytest.mark.parametrize("gap", sorted(GAP_OPERATIONS))
def test_a_gap_still_declared_open_is_still_open(gap):
    """The failure this file was written for."""
    if not gap_bullets()[gap]:
        pytest.skip("the document already records this gap as closed")
    landed = sorted(c for c in GAP_OPERATIONS[gap] if schema_can(c))
    assert not landed, (
        f"{gap!r} is listed as an open gap, and the schema has {landed}. The "
        "list tells a reader to use a workaround that is no longer needed.")


@pytest.mark.parametrize("gap", sorted(GAP_OPERATIONS))
def test_a_gap_recorded_as_closed_has_the_operation_behind_it(gap):
    """The same mistake in reverse: a tick with nothing under it."""
    if gap_bullets()[gap]:
        pytest.skip("the document still records this gap as open")
    absent = sorted(c for c in GAP_OPERATIONS[gap] if not schema_can(c))
    assert not absent, (
        f"{gap!r} is struck through, and the schema is missing {absent}.")
