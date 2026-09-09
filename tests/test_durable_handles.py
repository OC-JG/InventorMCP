"""Topology handles, and what happens to one after a rebuild.

A handle from `select_topology` is a name for a live COM object, and a rebuild
throws that object away. The docstrings said handles expire; **nothing enforced
it**, so what a caller got back was not a refusal but a pointer at geometry
that no longer existed. That is the shape of bug this repository keeps finding:
a rule written down in one place and enforced in none.

Three things landed together, and they are different in kind.

`_live` is the enforcement: every use of a handle goes through it, it probes
the stored object, rebinds it from a `ReferenceKeyManager` key if it has one,
and **refuses** if neither works. That refusal is an improvement even where the
rebinding never works, which matters because the rebinding is unmeasured: the
call is published and the *marshalling* is not -- `GetReferenceKey` takes the
key as a byte-array `[out]` parameter, which pywin32 usually turns into the
return value. Usually. `scripts/probe_reference_keys.py` is what settles it,
and a release that gives no key leaves handles exactly as they were.

`Face.CreatedByFeature` closes a live divergence rather than adding anything:
the simulator has answered "which feature made this face" since it was written
and the COM backend never did, so `TopoInfo.feature` was set on one side and
`None` on the other. It is the field that makes a DFM finding sayable as "the
faces of the boss" instead of as four indices.

`durable` is the honest reporting of the first two. The tool's note used to say
handles expire, flatly, which overstates it where a key exists and gets a
caller re-selecting geometry that would have been fine.
"""

from __future__ import annotations

import inspect

import pytest

from inventor_mcp.backend.base import ResolvedSelector
from inventor_mcp.backend.com import backend as com
from inventor_mcp.builder import build_part
from inventor_mcp.schema import PartRecipe

PLATE = {
    "name": "P", "units": "mm",
    "parameters": [{"name": "plate_w", "value": 60}],
    "operations": [
        {"op": "sketch", "name": "S", "plane": "xy", "entities": [
            {"type": "rectangle", "center": [0, 0], "width": "plate_w",
             "height": 40}]},
        {"op": "extrude", "name": "Plate", "sketch": "S", "distance": 10},
        {"op": "sketch", "name": "R", "plane": "xy", "entities": [
            {"type": "circle", "center": [0, 0], "diameter": 20}]},
        {"op": "extrude", "name": "Boss", "sketch": "R", "distance": 20},
    ],
}


@pytest.fixture
def part(session):
    out = build_part(session, PartRecipe.model_validate(PLATE))
    assert out["ok"], out["errors"]
    return out["document"]


class TestWhichFeatureMadeIt:
    def test_every_face_names_its_feature(self, session, part):
        matches = session.backend.select(
            part, ResolvedSelector(kind="face", filter="all"))
        assert matches
        assert all(match.feature for match in matches), \
            [m.description for m in matches if not m.feature]
        assert {"Plate", "Boss"} <= {match.feature for match in matches}

    def test_the_feature_filter_and_the_reported_feature_agree(self, session, part):
        """Two answers to the same question -- a selector filtering by feature,
        and a match reporting one -- and a part where they disagreed would send
        a DFM finding at the wrong geometry."""
        for name in ("Plate", "Boss"):
            matches = session.backend.select(
                part, ResolvedSelector(kind="face", feature=name))
            assert matches, name
            assert {match.feature for match in matches} == {name}

    def test_the_live_backend_reads_it_off_the_published_property(self):
        """`Face.CreatedByFeature`. An *edge* has no such property -- it is
        where two faces meet, so it belongs to both -- and the answer taken is
        the first of its faces that will say."""
        source = inspect.getsource(com._created_by)
        assert "entity.CreatedByFeature" in source
        assert "entity.Faces" in source


class TestAHandleSaysWhetherItSurvives:
    def test_the_simulator_calls_its_handles_durable(self, session, part):
        """And honestly rather than flatteringly: its topology is a ledger
        nothing invalidates, and its `rebuild` does not re-solve geometry at
        all -- it says so in its own note."""
        matches = session.backend.select(
            part, ResolvedSelector(kind="face", filter="all"))
        assert all(match.durable for match in matches)

    def test_a_handle_still_works_after_a_parameter_change(self, session, part):
        """The behaviour the field is claiming. On the simulator this is cheap;
        it is here so that the claim and the behaviour cannot drift apart."""
        face = session.backend.select(
            part, ResolvedSelector(kind="face", filter="top", limit=1))[0]
        session.backend.set_parameter(part, "plate_w", "90 mm")
        session.backend.rebuild(part)
        again = session.backend.select(
            part, ResolvedSelector(kind="face", ids=[face.id]))
        assert [match.id for match in again] == [face.id]

    def test_the_tool_reports_it_per_handle_rather_than_in_a_blanket_note(self):
        """The note used to say handles expire, flatly. That overstates it
        where a key exists, and a caller reading it re-selects geometry that
        would have been fine."""
        from inventor_mcp.tools import inspection

        source = inspect.getsource(inspection)
        assert '"durable": match.durable' in source


class TestEveryUseOfAHandleGoesThroughOnePlace:
    """The enforcement, checked structurally because the alternative needs a
    seat. A handle used directly out of `_topology` is a handle that can be
    dead, which is the bug."""

    def test_nothing_reaches_into_the_stored_object_directly(self):
        source = inspect.getsource(com)
        offenders = [line.strip() for line in source.splitlines()
                     if '_topology[' in line and '"object"' in line]
        # One assignment, in `_describe`, and no reads.
        assert offenders == [
            'self._topology[handle] = {"object": entity, "info": info,'
        ], offenders

    def test_the_accessor_probes_rebinds_and_then_refuses(self):
        source = inspect.getsource(com.ComBackend._live)
        probe = source.index("_still_there(entity)")
        rebind = source.index("BindKeyToObject")
        refuse = source.index("no longer points at anything")
        assert probe < rebind < refuse, (
            "the order is the behaviour: use what works, rebind what does not, "
            "refuse what cannot be rebound")

    def test_liveness_is_probed_by_touching_the_object(self):
        """Inventor hands out no validity flag, and a released object looks
        like an object until it is touched."""
        source = inspect.getsource(com._still_there)
        assert "entity.Evaluator" in source

    def test_the_key_context_is_kept_per_document(self):
        """The published rule: a B-Rep key needs the context it was made with
        to be bound back, so a context created per call is a key that can never
        be used."""
        source = inspect.getsource(com.ComBackend._reference_key)
        assert "self._key_contexts" in source
        assert "CreateKeyContext()" in source

    def test_a_release_with_no_key_costs_nothing(self):
        """`_reference_key` returns None on any failure, and a handle with no
        key then behaves exactly as every handle behaved before this existed.
        That is what makes shipping an unmeasured call defensible here."""
        source = inspect.getsource(com.ComBackend._reference_key)
        assert source.count("return None") >= 2

    def test_the_probe_script_covers_the_step_that_matters(self):
        """A key that only works while nothing has moved is a key with no use,
        so the run has to get as far as rebinding after a rebuild."""
        from pathlib import Path

        script = (Path(__file__).resolve().parent.parent / "scripts"
                  / "probe_reference_keys.py").read_text(encoding="utf-8")
        assert "BindKeyToObject(key, context)" in script
        assert "AFTER A REBUILD" in script
        assert "CreatedByFeature" in script
