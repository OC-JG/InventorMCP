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
# `encoding`, because the gap titles below carry em-dashes and this file is
# matched against them. Without it Windows decodes the markdown as cp1252,
# the em-dash arrives as three characters, and every lookup keyed by a title
# raises `KeyError` -- four failures on any Windows checkout, none on CI.
COVERAGE = (ROOT / "docs/FEATURE_COVERAGE.md").read_text(encoding="utf-8")

# Which recipe operation, if it exists, closes each gap the list names. A gap
# with no operation that could close it -- sketch fillet, project geometry --
# maps to nothing and is skipped: those are sketch-level abilities rather than
# operations, so the schema cannot answer for them.
GAP_OPERATIONS = {
    "No work axis or work point": {"work_axis", "work_point"},
    "`hole` still only drills the primary body — on Inventor": {"hole:bodies"},
    # Struck through on 2026-09-09, so what this checks is the reverse: the two
    # fields the closure rests on had better exist. A tick with nothing under
    # it is the same mistake as an open gap that has been closed.
    "`work_plane` builds only `offset` and `midplane` on Inventor":
        {"work_plane:axis", "work_plane:face"},
}

#: A gap the *schema* closed and Inventor did not. `hole` + `bodies` is the
#: first: the simulator honours it, and Inventor 2027.1's `HoleFeature` has no
#: `AffectedBodies` at all, measured 2026-09-07. So the bullet stays open while
#: the field exists, which is neither of the two states this file first knew
#: about -- and the third test below is what stops that reading as staleness.
#: The claim is checked against `_KNOWN_BROKEN_FIELDS`, so a gap can only sit
#: here while the code agrees Inventor cannot do it.
LIVE_ONLY_GAPS = {
    "`hole` still only drills the primary body — on Inventor": ("hole", "bodies"),
    # `work_plane` was the second, from 2026-09-08 to 2026-09-09: `kind`
    # offered `angle` and `tangent`, the simulator accepted both and the COM
    # backend refused both -- it built an offset plane before that, defect 12.
    # It is gone from here because it is *closed* rather than because it was
    # reworded: both kinds build, so the bullet is struck through and
    # `GAP_OPERATIONS` below carries the fields the tick has to have behind it.
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
    if gap in LIVE_ONLY_GAPS:
        pytest.skip("the schema closed this one and Inventor did not; "
                    "test_a_gap_open_only_live_says_so_in_the_code covers it")
    landed = sorted(c for c in GAP_OPERATIONS[gap] if schema_can(c))
    assert not landed, (
        f"{gap!r} is listed as an open gap, and the schema has {landed}. The "
        "list tells a reader to use a workaround that is no longer needed.")


@pytest.mark.parametrize("gap", sorted(LIVE_ONLY_GAPS))
def test_a_gap_open_only_live_says_so_in_the_code(gap):
    """A gap the schema closed and Inventor did not needs three things to agree.

    The bullet has to be open, because the workaround is still the only thing
    that works; the schema has to still carry the field, because the simulator
    honours it and a recipe written for a later Inventor should still rehearse;
    and `_KNOWN_BROKEN_FIELDS` has to say Inventor cannot do it, because that is
    what warns a caller at rehearsal instead of on a CAD seat.

    Drop any one of the three and a reader is misled: an unwarned field looks
    supported, a struck-through bullet hides the workaround they need, and a
    warning with no field behind it is noise.
    """
    from inventor_mcp.rehearsal import _KNOWN_BROKEN_FIELDS

    assert gap_bullets()[gap], (
        f"{gap!r} is struck through, but Inventor still cannot do it -- the "
        "workaround the bullet names is the only thing that works.")
    field = LIVE_ONLY_GAPS[gap]
    assert schema_can(f"{field[0]}:{field[1]}"), (
        f"{gap!r} is recorded as schema-closed and the schema has no "
        f"{field[1]!r} on {field[0]!r}. Move it back to an ordinary gap.")
    assert field in _KNOWN_BROKEN_FIELDS, (
        f"{gap!r} says Inventor cannot do this and `_KNOWN_BROKEN_FIELDS` does "
        f"not carry {field}, so nothing warns a caller before they spend a seat.")


def test_nothing_is_warned_about_that_the_schema_does_not_offer():
    """The reverse: a broken-field entry for a field nobody can set is noise."""
    from inventor_mcp.rehearsal import _KNOWN_BROKEN_FIELDS

    missing = sorted(f"{op}.{field}" for op, field in _KNOWN_BROKEN_FIELDS
                     if not schema_can(f"{op}:{field}"))
    assert not missing, f"warned about, and not in the schema: {missing}"


@pytest.mark.parametrize("gap", sorted(GAP_OPERATIONS))
def test_a_gap_recorded_as_closed_has_the_operation_behind_it(gap):
    """The same mistake in reverse: a tick with nothing under it."""
    if gap_bullets()[gap]:
        pytest.skip("the document still records this gap as open")
    absent = sorted(c for c in GAP_OPERATIONS[gap] if not schema_can(c))
    assert not absent, (
        f"{gap!r} is struck through, and the schema is missing {absent}.")
