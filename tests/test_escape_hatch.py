"""The escape hatch, and the two decisions it takes to open it.

The gate is the feature. A tool that runs arbitrary code in the server's process
is defensible when the machine's owner asked for it and indefensible when it
appears by default, so what these tests are really about is that it is absent
unless somebody said otherwise -- and absent in the strong sense of never being
registered, rather than registered and refusing.
"""

from __future__ import annotations

from typing import Any

import pytest

from inventor_mcp.session import Session
from inventor_mcp.tools import escape


class Recorder:
    """Stands in for the MCP server, remembering what got registered."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **_kwargs):
        def decorate(function):
            self.tools[function.__name__] = function
            return function

        return decorate


@pytest.fixture
def session() -> Session:
    session = Session(backend_kind="mock")
    backend = session.ensure_backend()
    backend.connect()
    session.register(backend.new_part("Part", units="mm"), "mm", "deg")
    return session


@pytest.fixture
def hatch(session, monkeypatch):
    """The tool, with the environment switch on."""
    monkeypatch.setenv(escape.ENV_VAR, "on")
    server = Recorder()
    escape.register(server, session)
    return server.tools["run_inventor_script"]


class TestTheSwitch:
    def test_off_by_default(self, session, monkeypatch):
        monkeypatch.delenv(escape.ENV_VAR, raising=False)
        server = Recorder()
        escape.register(server, session)
        assert server.tools == {}, "the hatch must not be registered by default"

    def test_registered_only_by_the_environment(self, session, monkeypatch):
        monkeypatch.setenv(escape.ENV_VAR, "on")
        server = Recorder()
        escape.register(server, session)
        assert "run_inventor_script" in server.tools

    @pytest.mark.parametrize("value", ["on", "1", "true", "YES", "Enabled"])
    def test_the_spellings_that_count_as_on(self, value):
        assert escape.enabled({escape.ENV_VAR: value})

    @pytest.mark.parametrize("value", ["", "off", "0", "no", "false", "maybe", "ON!"])
    def test_everything_else_is_off(self, value):
        assert not escape.enabled({escape.ENV_VAR: value})

    def test_the_whole_server_leaves_it_out(self, monkeypatch):
        """Through the real registration path, not just escape.register."""
        from inventor_mcp.tools import register_all

        monkeypatch.delenv(escape.ENV_VAR, raising=False)
        server = Recorder()
        register_all(server, Session(backend_kind="mock"))
        assert "run_inventor_script" not in server.tools
        assert server.tools, "the other tools should still be there"

    def test_the_whole_server_includes_it_when_asked(self, monkeypatch):
        from inventor_mcp.tools import register_all

        monkeypatch.setenv(escape.ENV_VAR, "on")
        server = Recorder()
        register_all(server, Session(backend_kind="mock"))
        assert "run_inventor_script" in server.tools


class TestTheAcknowledgement:
    def test_nothing_runs_without_it(self, hatch, session):
        result = hatch(code="document.volume = 999")
        assert result["ok"] is False
        assert result["error"] == "not_acknowledged"
        assert session.backend.mass_properties(session.active).volume != 999

    def test_it_has_to_be_true_not_merely_present(self, hatch):
        result = hatch(code="result = 1", i_understand_this_is_unsandboxed=False)
        assert result["ok"] is False


class TestRunning:
    def test_a_script_can_read_the_model(self, hatch):
        result = hatch(code="result = document.name",
                       i_understand_this_is_unsandboxed=True)
        assert result["ok"] is True
        assert result["result"] == "Part"

    def test_what_it_printed_comes_back(self, hatch):
        result = hatch(code="print('hello from the hatch')",
                       i_understand_this_is_unsandboxed=True)
        assert "hello from the hatch" in result["printed"]

    def test_the_code_is_echoed_for_the_record(self, hatch):
        result = hatch(code="result = 2 + 2", i_understand_this_is_unsandboxed=True)
        assert result["code"] == "result = 2 + 2"
        assert result["result"] == 4

    def test_a_failing_script_reports_the_exception(self, hatch):
        result = hatch(code="raise ValueError('deliberate')",
                       i_understand_this_is_unsandboxed=True)
        assert result["ok"] is False
        assert result["error"] == "script_failed"
        assert "deliberate" in result["message"]

    def test_it_can_run_with_no_document_bound(self, hatch):
        result = hatch(code="result = document is None", document="",
                       i_understand_this_is_unsandboxed=True)
        assert result["result"] is True
        assert result["document"] is None

    def test_an_object_comes_back_described_rather_than_repr_ed(self, hatch):
        result = hatch(code="result = document.features",
                       i_understand_this_is_unsandboxed=True)
        assert isinstance(result["result"], list)


class TestRollingBackAScript:
    def test_a_failed_script_is_undone_by_default(self, hatch, session):
        was = session.backend.mass_properties(session.active).volume
        result = hatch(
            code="document.bodies = [500.0]\nraise RuntimeError('too late')",
            i_understand_this_is_unsandboxed=True,
        )
        assert result["ok"] is False
        assert result["rolled_back"] is True
        assert session.backend.mass_properties(session.active).volume == was

    def test_the_default_is_the_opposite_of_the_recipe_tools(self, hatch, session):
        """Stated as a test because the asymmetry is a decision, not an accident."""
        result = hatch(code="document.bodies = [500.0]\nraise RuntimeError('x')",
                       i_understand_this_is_unsandboxed=True, rollback_on_error=False)
        assert "rolled_back" not in result
        assert "not attempted" in result["rollback"]
        assert session.backend.mass_properties(session.active).volume == 500.0

    def test_a_script_that_works_is_kept(self, hatch, session):
        result = hatch(code="document.bodies = [42.0]",
                       i_understand_this_is_unsandboxed=True)
        assert result["ok"] is True
        assert session.backend.mass_properties(session.active).volume == 42.0


class TestABackendWithNoApi:
    def test_it_says_so_rather_than_pretending(self, hatch, session):
        def refuse(doc_id, code):
            raise NotImplementedError("no live Inventor API here")

        session.backend.run_script = refuse
        result = hatch(code="result = 1", i_understand_this_is_unsandboxed=True)
        assert result["ok"] is False and result["error"] == "not_available"

    def test_the_base_contract_refuses(self):
        from inventor_mcp.backend.base import Backend

        with pytest.raises(NotImplementedError):
            Backend.run_script(Backend, None, "result = 1")  # type: ignore[arg-type]


class TestWhatTheDocumentationPromises:
    def test_the_guide_tells_a_model_the_hatch_may_be_absent(self):
        from inventor_mcp.guide import MODELLING_NOTES

        assert escape.ENV_VAR in MODELLING_NOTES
        assert "run_inventor_script" in MODELLING_NOTES

    def test_the_scope_listing_matches_what_the_com_backend_binds(self):
        """A name promised in the tool description but not bound costs a round trip.

        Read off the COM backend's source rather than by running a script,
        because that is the scope the description is describing -- the simulator
        binds a deliberately smaller set.
        """
        import ast
        import inspect

        from inventor_mcp.backend.com.backend import ComBackend

        import textwrap

        source = textwrap.dedent(inspect.getsource(ComBackend.run_script))
        tree = ast.parse(source)
        bound: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                bound |= {key.value for key in node.keys
                          if isinstance(key, ast.Constant) and isinstance(key.value, str)}

        promised: set[str] = set()
        for line in escape.SCOPE.splitlines():
            if not line.strip():
                continue
            for name in line.split()[0].split("/"):
                promised.add(name.strip())
        # The listing writes "application / app" as one row, so the alternative
        # spelling is the second token; pick those up too.
        for line in escape.SCOPE.splitlines():
            parts = line.split()
            if len(parts) > 2 and parts[1] == "/":
                promised.add(parts[2])

        assert promised, "the scope listing did not parse"
        assert promised <= bound, (
            f"promised but not bound: {sorted(promised - bound)}")

    def test_the_simulator_binds_what_it_says_it_binds(self, hatch):
        """It offers less than Inventor, and a script must fail loudly on the rest."""
        for name in ("document", "component", "backend", "application"):
            probe = hatch(code=f"result = repr({name})",
                          i_understand_this_is_unsandboxed=True)
            assert probe["ok"] is True, name
        missing = hatch(code="result = transient", i_understand_this_is_unsandboxed=True)
        assert missing["ok"] is False and "transient" in missing["message"]
