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


class TestTheCostOfAsking:
    """Why the guard does not go through `list_documents`.

    The first live run reported 1033 open documents behind an assembly. On the
    COM backend `list_documents` reads six properties per document, scans the
    held handles by COM identity for each, **and registers every document it did
    not recognise** -- so one save would have minted a thousand session handles
    and left the next call comparing a million COM identities. The guard asks
    one narrow question instead, and each backend answers it as cheaply as it
    can.
    """

    def test_the_com_backend_does_not_use_the_listing_here(self):
        import ast
        import inspect
        import textwrap

        from inventor_mcp.backend.com.backend import ComBackend

        # The docstring names `list_documents` to explain why it is avoided, so
        # the check has to be on the code rather than on the text. Through
        # `ast` and not by subtracting `__doc__` from the source: 3.13 dedents
        # docstrings at compile time, so that subtraction quietly stopped
        # matching and the check read the very sentence it means to exclude --
        # green on 3.11 and 3.12, red on 3.13, for nothing either backend did.
        # `unparse` drops the comments too, which is the same intent.
        source = textwrap.dedent(inspect.getsource(ComBackend.document_at_path))
        function = ast.parse(source).body[0]
        statements = (function.body[1:] if ast.get_docstring(function)
                      else function.body)
        body = ast.unparse(statements)
        assert "list_documents" not in body, (
            "the whole reason document_at_path exists is that list_documents is "
            "unbounded in cost and registers what it walks")
        assert "FullFileName" in body

    def test_the_com_backend_overrides_it_on_purpose(self):
        """The guard itself stays shared; only the enumeration is per-backend."""
        from inventor_mcp.backend.com.backend import ComBackend

        assert "document_at_path" in vars(ComBackend)
        assert "refuse_a_path_another_document_holds" not in vars(ComBackend)

    def test_the_mock_is_happy_with_the_shared_default(self):
        """Its listing is a dict walk, so there is nothing to save."""
        assert "document_at_path" not in vars(MockBackend)

    def test_the_guard_asks_document_at_path_exactly_once(self):
        """A guard that asked twice would double whatever it costs."""
        calls: list[str] = []

        class _Counting(MockBackend):
            def document_at_path(self, path):
                calls.append(path)
                return super().document_at_path(path)

        backend = _Counting()
        backend.connect()
        first = backend.new_part("Bracket")
        second = backend.new_part("Other")
        backend.save_document(first.id, "out/bracket.ipt")
        calls.clear()  # that save asked once too; the conflicting one is the subject
        with pytest.raises(DocumentError):
            backend.save_document(second.id, "out/bracket.ipt")
        assert len(calls) == 1, calls

    def test_an_in_place_save_does_not_ask_at_all(self):
        """No path means `Save`, which cannot collide, so it must not pay for a
        walk over every open document."""
        calls: list[str] = []

        class _Counting(MockBackend):
            def document_at_path(self, path):
                calls.append(path)
                return super().document_at_path(path)

        backend = _Counting()
        backend.connect()
        document = backend.new_part("Bracket")
        backend.save_document(document.id, "out/bracket.ipt")
        calls.clear()
        backend.save_document(document.id)
        assert calls == []

    def test_saving_onto_its_own_path_does_not_ask_either(self):
        """Settled from the document's own path before anything is walked."""
        calls: list[str] = []

        class _Counting(MockBackend):
            def document_at_path(self, path):
                calls.append(path)
                return super().document_at_path(path)

        backend = _Counting()
        backend.connect()
        document = backend.new_part("Bracket")
        backend.save_document(document.id, "out/bracket.ipt")
        calls.clear()
        backend.save_document(document.id, "out/bracket.ipt")
        assert calls == []


class TestADocumentOpenedOutsideThisSession:
    """The case a session-registry check could never have seen.

    On COM the file may be open because the user opened it in Inventor's UI.
    There is no handle to close by, so the message and the remedy both have to
    read differently -- telling someone to call `close_part(document=None)`
    would be worse than saying nothing.
    """

    class _UserOpened(MockBackend):
        def document_at_path(self, path):
            return None, "bracket.ipt"

    def test_it_is_still_refused(self):
        backend = self._UserOpened()
        backend.connect()
        document = backend.new_part("Bracket")
        with pytest.raises(DocumentError) as raised:
            backend.save_document(document.id, "somewhere/else.ipt")
        assert "opened outside this session" in str(raised.value)

    def test_the_hint_does_not_offer_a_handle_it_has_not_got(self):
        backend = self._UserOpened()
        backend.connect()
        document = backend.new_part("Bracket")
        with pytest.raises(DocumentError) as raised:
            backend.save_document(document.id, "somewhere/else.ipt")
        hint = raised.value.hint or ""
        assert "close_part" not in hint
        assert "Close bracket.ipt in Inventor" in hint

    def test_a_holder_that_turns_out_to_be_this_document_is_not_a_conflict(self):
        """`document_path` could not answer -- COM has raised on it before -- and
        reporting a document as blocking itself would be worse than the bare
        exception this replaces."""

        class _ItsOwnHolder(MockBackend):
            def document_path(self, doc_id):
                return None

            def document_at_path(self, path):
                return self._active, "Bracket"

        backend = _ItsOwnHolder()
        backend.connect()
        document = backend.new_part("Bracket")
        assert backend.save_document(document.id, "out/bracket.ipt").path
