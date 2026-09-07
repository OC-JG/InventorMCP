"""Defect 3: a Save As onto a path Inventor already has open.

Inventor will not write a file it has open, and says so with a bare "Exception
occurred" and nothing in the ErrorManager -- so the second save of a rebuild,
onto the path the first one wrote, failed and named neither the file nor the
document holding it. Rebuilding leaves the earlier document open, which makes
this the normal case rather than an unusual one.

The conflict is knowable before the write, so that is where it is answered
rather than by translating a message Inventor does not give. Two consequences
worth stating, because they are what makes the check worth having rather than a
guess dressed up:

* **`list_documents` is the right source on both backends.** On COM it reads
  Inventor's own `Documents` collection, so it sees a file the *user* opened in
  the UI as well as one this session opened -- which the session's own registry
  cannot. That is the case the original defect report could not have been fixed
  from the tool layer.
* **The guard lives on `Backend`, not in either implementation.** One rule, both
  backends held to it, and no caller able to route around it -- the reasoning
  `apply_parameter` records for the freeze guard. The tests below therefore
  exercise it through the tool surface *and* directly, because a rule that only
  holds on the path a test happens to take is not the rule that shipped.
"""

from __future__ import annotations

import asyncio

import pytest

from inventor_mcp.backend.base import Backend, DocInfo, _same_file_key
from inventor_mcp.backend.mock.backend import MockBackend
from inventor_mcp.errors import DocumentError


def call(server, name: str, arguments: dict | None = None) -> dict:
    result = asyncio.run(server.call_tool(name, arguments or {}))
    return result.structured_content


@pytest.fixture
def backend() -> MockBackend:
    instance = MockBackend()
    instance.connect()
    return instance


@pytest.fixture
def connected(server):
    call(server, "connect", {"backend": "mock"})
    return server


class TestTheConflictIsNamed:
    def test_saving_onto_a_path_another_document_holds_is_refused(self, backend):
        first = backend.new_part("Bracket")
        second = backend.new_part("BracketRevised")
        backend.save_document(first.id, "out/bracket.ipt")

        with pytest.raises(DocumentError) as raised:
            backend.save_document(second.id, "out/bracket.ipt")
        assert "bracket.ipt" in str(raised.value)

    def test_it_names_the_document_holding_the_file(self, backend):
        """The message the defect was raised for: which document has it open.
        Without that the caller cannot act, and there may be several."""
        first = backend.new_part("Bracket")
        second = backend.new_part("BracketRevised")
        backend.save_document(first.id, "out/bracket.ipt")

        with pytest.raises(DocumentError) as raised:
            backend.save_document(second.id, "out/bracket.ipt")
        assert first.id in str(raised.value)
        assert "Bracket" in str(raised.value)

    def test_the_hint_offers_both_ways_out(self, backend):
        """Close the holder, or write somewhere else. The defect asked for both,
        and picking one for the caller would be guessing which copy they want."""
        first = backend.new_part("Bracket")
        second = backend.new_part("BracketRevised")
        backend.save_document(first.id, "out/bracket.ipt")

        with pytest.raises(DocumentError) as raised:
            backend.save_document(second.id, "out/bracket.ipt")
        hint = raised.value.hint or ""
        assert f"close_part(document={first.id!r})" in hint
        assert "another name" in hint

    def test_the_file_is_not_written_over(self, backend):
        """A refusal that had already changed the document would be worse than
        the bare exception it replaces."""
        first = backend.new_part("Bracket")
        second = backend.new_part("BracketRevised")
        backend.save_document(first.id, "out/bracket.ipt")

        with pytest.raises(DocumentError):
            backend.save_document(second.id, "out/bracket.ipt")
        assert backend.document_path(second.id) is None
        assert _same_file_key(backend.document_path(first.id)) == \
            _same_file_key("out/bracket.ipt")

    def test_two_names_for_one_file_still_collide(self, backend):
        """A relative name and the absolute path it resolves to are one file, and
        comparing them raw would miss the collision."""
        first = backend.new_part("Bracket")
        second = backend.new_part("BracketRevised")
        backend.save_document(first.id, "out/bracket.ipt")

        with pytest.raises(DocumentError):
            backend.save_document(second.id, "./out/../out/bracket.ipt")

    def test_closing_the_holder_releases_the_path(self, backend):
        """The remedy the hint names has to actually work."""
        first = backend.new_part("Bracket")
        second = backend.new_part("BracketRevised")
        backend.save_document(first.id, "out/bracket.ipt")
        backend.close_document(first.id)

        saved = backend.save_document(second.id, "out/bracket.ipt")
        assert saved.path.endswith("bracket.ipt")


class TestWhatMustStillBeAllowed:
    """A save that cannot collide, refused, would be worse than the defect."""

    def test_saving_in_place_is_not_checked(self, backend):
        first = backend.new_part("Bracket")
        backend.save_document(first.id, "out/bracket.ipt")
        assert backend.save_document(first.id).path.endswith("bracket.ipt")

    def test_saving_onto_its_own_path_is_an_in_place_save_longhand(self, backend):
        first = backend.new_part("Bracket")
        backend.save_document(first.id, "out/bracket.ipt")
        assert backend.save_document(first.id, "out/bracket.ipt").path.endswith(
            "bracket.ipt")

    def test_a_fresh_path_is_fine_with_others_open(self, backend):
        first = backend.new_part("Bracket")
        second = backend.new_part("BracketRevised")
        backend.save_document(first.id, "out/bracket.ipt")
        assert backend.save_document(second.id, "out/bracket_r2.ipt").path.endswith(
            "bracket_r2.ipt")

    def test_a_document_that_has_never_been_saved_holds_no_path(self, backend):
        """An unsaved document has a name and no file, so it cannot be the
        holder of anything -- and `None` must not compare equal to a path."""
        backend.new_part("Bracket")
        second = backend.new_part("BracketRevised")
        assert backend.save_document(second.id, "out/bracket.ipt").path.endswith(
            "bracket.ipt")


class TestItDoesNotDependOnMatchingIds:
    """The COM backend's ids are the reason the own-path case is checked first.

    `document_path`'s own docstring records why: on COM, `list_documents`
    identifies documents through Python wrapper identity, late binding mints a
    fresh wrapper per call, and an id-to-id match over that listing once matched
    nothing at all. So the in-place-longhand save must survive a listing whose
    ids never match, which is what this backend simulates.
    """

    class _MismatchedIds(MockBackend):
        def list_documents(self) -> list[DocInfo]:
            return [DocInfo(id=f"stale-{info.id}", name=info.name, path=info.path)
                    for info in super().list_documents()]

    def test_saving_onto_its_own_path_survives_ids_that_never_match(self):
        backend = self._MismatchedIds()
        backend.connect()
        first = backend.new_part("Bracket")
        backend.save_document(first.id, "out/bracket.ipt")
        # Without the own-path check this reads as a collision with itself.
        assert backend.save_document(first.id, "out/bracket.ipt").path.endswith(
            "bracket.ipt")

    def test_a_real_collision_is_still_caught_there(self):
        """Otherwise the test above would pass by disabling the check."""
        backend = self._MismatchedIds()
        backend.connect()
        first = backend.new_part("Bracket")
        second = backend.new_part("BracketRevised")
        backend.save_document(first.id, "out/bracket.ipt")
        with pytest.raises(DocumentError):
            backend.save_document(second.id, "out/bracket.ipt")


class TestThroughTheToolSurface:
    def test_save_part_reports_the_conflict_rather_than_failing_bare(
            self, connected, tmp_path):
        """The route a caller actually takes. `guard` turns the error into a
        structured result, so what matters is that it carries the explanation."""
        call(connected, "new_part", {"name": "Bracket"})
        call(connected, "save_part", {"path": str(tmp_path / "bracket.ipt")})
        call(connected, "new_part", {"name": "BracketRevised"})

        result = call(connected, "save_part", {"path": str(tmp_path / "bracket.ipt")})
        assert result["ok"] is False
        assert result["error"] == "document_error"
        assert "already open" in result["message"]
        assert "close_part" in result["hint"]

    def test_the_conflict_message_does_not_carry_the_directory(
            self, connected, tmp_path):
        """`to_dict` sanitises paths out of every error on the way to the
        caller, and this one is about a file, so it is worth confirming the
        filename survives and the route to it does not."""
        call(connected, "new_part", {"name": "Bracket"})
        call(connected, "save_part", {"path": str(tmp_path / "bracket.ipt")})
        call(connected, "new_part", {"name": "BracketRevised"})

        result = call(connected, "save_part", {"path": str(tmp_path / "bracket.ipt")})
        assert "bracket.ipt" in result["message"]
        assert str(tmp_path) not in result["message"]
        assert str(tmp_path) not in result["hint"]

    def test_the_ordinary_two_part_save_still_works(self, connected, tmp_path):
        call(connected, "new_part", {"name": "Bracket"})
        call(connected, "save_part", {"path": str(tmp_path / "bracket.ipt")})
        call(connected, "new_part", {"name": "BracketRevised"})
        second = call(connected, "save_part", {"path": str(tmp_path / "r2.ipt")})
        assert second["path"].endswith("r2.ipt")


class TestTheGuardIsOnTheContract:
    def test_both_backends_inherit_it_rather_than_carrying_a_copy(self):
        """The ABC's point: one rule, and no chance of the two drifting apart."""
        from inventor_mcp.backend.com.backend import ComBackend

        for implementation in (MockBackend, ComBackend):
            assert "refuse_a_path_another_document_holds" not in vars(implementation), (
                f"{implementation.__name__} overrides the shared guard; the "
                "whole reason it lives on Backend is that neither should")
        assert hasattr(Backend, "refuse_a_path_another_document_holds")

    def test_every_backend_that_saves_asks_it(self):
        """A backend whose `save_document` skipped the call would pass every test
        above that goes through the other one.
        """
        import inspect

        from inventor_mcp.backend.com.backend import ComBackend

        for implementation in (MockBackend, ComBackend):
            source = inspect.getsource(implementation.save_document)
            assert "refuse_a_path_another_document_holds" in source, (
                f"{implementation.__name__}.save_document does not ask the guard")
