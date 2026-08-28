"""Opt-in rollback: undoing a failed build, and not doing it by default.

The default is deliberately the opposite of what a transaction-minded reader
expects. A half-built part is the best evidence there is about why a build
failed -- three of this project's geometry bugs were found by looking at one --
so it is left alone unless the caller says otherwise.

The simulator models a rollback exactly (it copies the document aside), which is
what makes any of this testable without Inventor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inventor_mcp.builder import build_part
from inventor_mcp.schema import PartRecipe
from inventor_mcp.session import Session

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def session() -> Session:
    session = Session(backend_kind="mock")
    session.ensure_backend().connect()
    return session


def recipe(**changes) -> PartRecipe:
    """The mounting plate, optionally broken in a stated way."""
    data = json.loads((EXAMPLES / "mounting_plate.json").read_text())
    data.update(changes)
    return PartRecipe.model_validate(data)


def breaks_at_the_last_operation() -> PartRecipe:
    """A recipe that builds a plate and then fails to drill it.

    The hole names a sketch that does not exist, so everything before it
    succeeds -- which is the case worth testing, since a build that fails on
    step one has nothing to roll back.
    """
    data = json.loads((EXAMPLES / "mounting_plate.json").read_text())
    data["operations"][-1]["sketch"] = "NoSuchSketch"
    return PartRecipe.model_validate(data)


def appends_then_breaks() -> PartRecipe:
    """Operations for an existing part: a pocket that cuts, then a bad hole.

    Literal sizes rather than expressions, so this says nothing about whichever
    parameters the part it is appended to happens to carry.
    """
    return PartRecipe.model_validate({
        "name": "MountingPlate",
        "units": "mm",
        "operations": [
            {"op": "sketch", "name": "Pocket", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 40, "height": 20}]},
            {"op": "extrude", "name": "Cut", "sketch": "Pocket", "distance": 3,
             "operation": "cut"},
            {"op": "hole", "sketch": "NoSuchSketch", "diameter": 5},
        ],
    })


class TestTheDefaultLeavesThePart:
    def test_a_failed_build_leaves_what_it_built(self, session):
        result = build_part(session, breaks_at_the_last_operation())
        assert result["ok"] is False
        assert "rolled_back" not in result
        # The plate and its fillet are still there to look at.
        document = session.backend._doc(result["document"])
        assert [f.kind for f in document.features] == ["extrude", "fillet"]
        assert document.volume > 0

    def test_nothing_is_asked_of_the_backend(self, session):
        build_part(session, breaks_at_the_last_operation())
        actions = [action for action, _ in session.backend.calls]
        assert "begin_transaction" not in actions


class TestRollingBack:
    def test_a_failed_build_is_undone(self, session):
        result = build_part(session, breaks_at_the_last_operation(),
                            rollback_on_error=True)
        assert result["ok"] is False
        assert result["rolled_back"] is True
        document = session.backend._doc(result["document"])
        assert document.features == []
        assert document.volume == 0.0

    def test_the_record_of_how_far_it_got_survives(self, session):
        """The geometry goes; the account of what was built does not."""
        result = build_part(session, breaks_at_the_last_operation(),
                            rollback_on_error=True)
        assert result["applied_then_undone"] is True
        assert [op["kind"] for op in result["operations"] if "kind" in op] == [
            "extrude", "fillet"]
        assert "nothing under `operations` exists any more" in result["rollback"]

    def test_a_successful_build_is_committed_and_kept(self, session):
        result = build_part(session, recipe(), rollback_on_error=True)
        assert result["ok"] is True
        assert "rolled_back" not in result
        assert session.backend.mass_properties(result["document"]).volume > 0
        actions = [action for action, _ in session.backend.calls]
        assert "commit_transaction" in actions and "abort_transaction" not in actions

    def test_appending_to_a_good_part_leaves_it_good(self, session):
        """The case rollback is actually for: the part already worked.

        The appended operations have to *succeed* before one of them fails, or
        the rollback is a no-op and the test passes without testing anything.
        """
        first = build_part(session, recipe())
        assert first["ok"]
        was = session.backend.mass_properties(first["document"]).volume
        features = len(session.backend._doc(first["document"]).features)

        second = build_part(session, appends_then_breaks(),
                            document=first["document"], rollback_on_error=True)
        assert second["ok"] is False and second["rolled_back"] is True
        # The cut ran and took material out; the rollback has to put it back.
        assert [op.get("kind") for op in second["operations"]] == [None, "extrude"]
        assert session.backend.mass_properties(first["document"]).volume == was
        assert len(session.backend._doc(first["document"]).features) == features
        assert "as it was before the call" in second["rollback"]

    def test_an_empty_document_is_reported_as_such(self, session):
        result = build_part(session, breaks_at_the_last_operation(),
                            rollback_on_error=True)
        assert "the part document is empty" in result["rollback"]


class TestABackendThatCannotUndo:
    def test_the_build_goes_ahead_without_a_net(self, session):
        """Refusing to build would be worse than building without rollback."""
        session.backend.begin_transaction = lambda doc_id, name: None
        result = build_part(session, recipe(), rollback_on_error=True)
        assert result["ok"] is True
        assert "not available from this backend" in result["rollback"]

    def test_a_failed_rollback_says_the_part_is_in_an_unknown_state(self, session):
        session.backend.abort_transaction = lambda handle: False
        result = build_part(session, breaks_at_the_last_operation(),
                            rollback_on_error=True)
        assert result["rolled_back"] is False
        assert "inspect it before building on it" in result["rollback"]
        assert "applied_then_undone" not in result


class TestTheContractItself:
    def test_the_base_backend_admits_it_cannot(self):
        """A backend that does nothing must not look like one that succeeded."""
        from inventor_mcp.backend.base import Backend

        assert Backend.begin_transaction(None, "doc", "name") is None  # type: ignore[arg-type]
        assert Backend.abort_transaction(None, "txn") is False  # type: ignore[arg-type]

    def test_an_unknown_handle_is_not_a_rollback(self, session):
        assert session.backend.abort_transaction("txn:nonexistent") is False

    def test_committing_twice_is_harmless(self, session):
        handle = session.backend.begin_transaction(
            session.ensure_backend().new_part("P", units="mm").id, "x")
        session.backend.commit_transaction(handle)
        session.backend.commit_transaction(handle)
        assert session.backend.abort_transaction(handle) is False
