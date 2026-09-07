"""Why "the connection closed" was the only thing anybody was told.

The server stopped being reachable from Claude and from the DFM tools at the
same time, and every client said the same four words. It was not the modelling
code, and the suite was green throughout: the shipped project-scoped
``.mcp.json`` launched ``python -m inventor_mcp`` with a bare ``python``, which
an MCP client resolves without the shell's ``PATH`` and without any virtualenv
the shell had active. That Python had no ``mcp`` installed, so
``inventor_mcp/__main__.py`` -- which imported the server at module level --
raised ``ModuleNotFoundError`` before a byte of protocol was written, and the
process exited. The client had nothing to report but ``CONNECTION_CLOSED``.

The README already warned, in as many words, that a bare ``"python"`` is the
commonest reason an MCP server shows as failed. Two facts that had to agree,
and only one of them was written down anywhere a test could see. So they are
checked here, along with the three properties that keep the failure legible
next time:

* nothing may be imported at the top of ``__main__`` that a broken install
  cannot import, because that import *is* the silent failure;
* ``preflight`` must run on the install where the server does not, so it may
  not need the dependencies it exists to report on;
* a venv's interpreter is a symlink to the one it was built from, so the
  launcher may not identify interpreters by resolved path.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def no_client_probe(monkeypatch):
    """Stop ``check_registration`` launching servers.

    It spawns a subprocess per registration and speaks MCP to each, which is
    the point of it and also a second or two every time. Tests that only need
    the *shape* of a report -- that every check reports, that every name is
    printed -- should not pay for it, and did: the file went from three seconds
    to over two minutes before this existed.
    """
    from inventor_mcp import preflight

    monkeypatch.setattr(preflight, "client_configs", lambda: [])
    return preflight


def top_level_imports(path: pathlib.Path) -> set[str]:
    """Modules imported at the top of a file, not inside a function or a try."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def load_launcher():
    """``scripts/serve.py``, loaded by path.

    Nothing in ``tests/`` imports from ``scripts/``: the repo root is not on
    ``sys.path`` under a bare ``pytest``, which is what CI runs.
    """
    spec = importlib.util.spec_from_file_location(
        "_serve_under_test", ROOT / "scripts" / "serve.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheConfigThatShipped:
    """What ``.mcp.json`` launches, held against what the README says to."""

    def test_it_does_not_run_a_bare_python_against_the_package(self):
        """The regression itself.

        ``python -m inventor_mcp`` needs the deps to be importable by whichever
        ``python`` the client finds first. Running the launcher instead needs
        only a Python: it re-executes the server on one that can import it.
        """
        config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        entry = config["mcpServers"]["inventor"]
        args = entry["args"]
        assert "-m" not in args, (
            f"{entry['command']} {' '.join(args)} asks an unknown interpreter "
            "to import the package. That is the configuration that reported "
            "only CONNECTION_CLOSED."
        )
        assert args[0].endswith("serve.py"), args

    def test_the_launcher_it_names_is_there(self):
        """A path in a config file is a claim until something opens it."""
        config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        named = config["mcpServers"]["inventor"]["args"][0]
        assert (ROOT / named).is_file(), f"{named} is not in the repository"

    def test_the_readme_still_warns_about_the_bare_interpreter(self):
        """The warning is half of the pair above; without it this test is arbitrary.

        Reworded freely -- matched on `"python"` and the word `PATH`, not on a
        sentence -- but if the warning goes, the reason `.mcp.json` is shaped
        the way it is goes with it.
        """
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        assert 'bare `"python"`' in readme
        assert "PATH" in readme


class TestTheEntryPointStaysAbleToSpeak:
    def test_main_does_not_import_the_server_at_module_level(self):
        """The one line that made the failure silent.

        ``from .server import main`` at the top of ``__main__`` runs before any
        code of ours can catch it, so a missing dependency became a traceback on
        the stderr an MCP client discards. Inside ``main`` it is catchable, and
        is caught.
        """
        imported = top_level_imports(ROOT / "inventor_mcp" / "__main__.py")
        assert ".server" not in imported and "inventor_mcp.server" not in imported
        assert imported <= {"__future__", "sys"}, imported

    def test_the_console_script_target_exists(self):
        """``pyproject`` points ``inventor-mcp`` at ``__main__:main``.

        It used to resolve to the name bound by the module-level import, which
        is a different thing that happened to work. Moving that import inside a
        function would have broken the installed command silently.
        """
        spec = tomllib_loads()["project"]["scripts"]["inventor-mcp"]
        module_name, _, attribute = spec.partition(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute))


def tomllib_loads() -> dict:
    import tomllib

    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


class TestTheDoctorRunsWhereTheServerCannot:
    def test_it_imports_nothing_the_broken_install_is_missing(self):
        """The property that makes the doctor worth having.

        A missing ``mcp`` is the commonest cause of a closed connection, so a
        doctor that needs ``mcp`` to load reports nothing on the machine that
        needs it. Every such import is inside a check, inside a ``try``.
        """
        imported = top_level_imports(ROOT / "inventor_mcp" / "preflight.py")
        for forbidden in ("mcp", "pydantic"):
            assert not any(name.split(".")[0] == forbidden for name in imported), (
                f"preflight imports {forbidden} at module level, so it cannot "
                "run on the install it exists to diagnose"
            )

    def test_every_check_reports_rather_than_raises(self, no_client_probe):
        preflight = no_client_probe

        findings = preflight.run_checks()
        assert len(findings) == len(preflight.CHECKS)
        for finding in findings:
            assert finding.status in preflight.MARKERS, finding
            assert finding.detail, finding
            assert finding.line()

    #: Checks that describe the machine's *configuration* rather than this
    #: install. `clients` launches whatever a client config names, and on a
    #: machine where no client is wired up correctly it fails while the install
    #: is perfectly sound -- which is the whole point of it, and makes it the
    #: wrong thing to assert about an install.
    ABOUT_THE_MACHINE = ("clients",)

    def test_a_healthy_install_is_reported_as_healthy(self, no_client_probe):
        """Run under the suite's own interpreter, which by definition has the deps.

        The analyser and Node are allowed to be missing -- they are warnings, and
        the offline CI legs have neither. A FAIL here means the interpreter
        running the tests could not start the server.
        """
        preflight = no_client_probe

        broken = [
            f for f in preflight.run_checks()
            if f.status == preflight.FAIL and f.name not in self.ABOUT_THE_MACHINE
        ]
        assert not broken, [(f.name, f.detail) for f in broken]

    def test_the_report_names_every_check_it_ran(self, no_client_probe):
        """Separated from the exit code deliberately.

        The two used to be one assertion, and it broke the moment a check could
        fail for a reason that is not the install's fault: a `clients` failure is
        a real non-zero exit on a machine with a misconfigured client and a
        healthy package. The report's completeness is the property worth holding
        here; the exit code has its own test below.
        """
        preflight = no_client_probe

        out = io.StringIO()
        preflight.doctor(out=out)
        printed = out.getvalue()
        assert "preflight" in printed
        for finding in preflight.run_checks():
            assert finding.name in printed, finding.name

    def test_the_exit_code_follows_the_failures(self):
        from inventor_mcp import preflight

        healthy = [preflight.Finding("server", preflight.OK, "builds")]
        warned = [preflight.Finding("node", preflight.WARN, "absent", hint="get it")]
        failed = [preflight.Finding("mcp sdk", preflight.FAIL, "gone", hint="fix")]

        assert preflight.doctor_from(healthy, out=io.StringIO()) == 0
        assert preflight.doctor_from(healthy + warned, out=io.StringIO()) == 0
        assert preflight.doctor_from(healthy + failed, out=io.StringIO()) == 1

    def test_every_problem_carries_a_repair(self, no_client_probe):
        """A finding that is not OK and has no hint leaves somebody stuck.

        Skipped checks are the exception, and deliberately: their detail already
        names the failure that caused them, and a hint of their own would point
        at the wrong thing.
        """
        preflight = no_client_probe

        for check, _ in preflight.CHECKS:
            finding = check()
            if finding.status in (preflight.OK, preflight.SKIP):
                continue
            assert finding.hint, f"{finding.name}: {finding.detail}"

    def test_a_missing_dependency_is_named_once_not_four_times(self):
        """``backend``, ``server`` and ``analyser`` all fail without pydantic.

        Their own reasons are true and useless: a backend that cannot import
        pydantic is not a broken backend. So they are skipped, and the summary
        names the dependency.
        """
        from inventor_mcp import preflight

        def missing() -> preflight.Finding:
            return preflight.Finding(
                "pydantic", preflight.FAIL, "not importable", hint="install it"
            )

        original = preflight.CHECKS
        preflight.CHECKS = (
            (preflight.check_python, False),
            (missing, False),
            (preflight.check_backend, True),
            (preflight.check_server, True),
        )
        try:
            findings = {f.name: f for f in preflight.run_checks()}
        finally:
            preflight.CHECKS = original

        assert findings["backend"].status == preflight.SKIP
        assert findings["server"].status == preflight.SKIP
        assert "pydantic" in findings["backend"].detail

    def test_the_doctor_does_not_reach_inventor(self):
        """A diagnostic that connects changes the thing it is diagnosing.

        ``connect`` launches Inventor or attaches to a live session. The backend
        check reports which backend ``auto`` picks and says, in the line it
        prints, that it did not connect.
        """
        from inventor_mcp import preflight

        finding = preflight.check_backend()
        assert "not connected" in finding.detail


class TestWhatTheClientWouldOtherwiseNotSee:
    def test_the_startup_failure_names_the_interpreter(self):
        """Nearly always the answer: the package is in a venv and the client
        launched a different Python."""
        from inventor_mcp import preflight

        out = io.StringIO()
        preflight.startup_failure(ModuleNotFoundError("No module named 'mcp'"), out=out)
        printed = out.getvalue()
        assert "No module named 'mcp'" in printed
        assert sys.executable in printed
        assert "--doctor" in printed

    def test_doctor_is_answered_before_the_server_is_imported(self):
        """``--doctor`` has to work on the install where importing the server is
        what fails, so ``__main__`` reads it out of argv itself rather than
        leaving it to the server's own argparse."""
        source = (ROOT / "inventor_mcp" / "__main__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        main = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        body = ast.unparse(main)
        assert body.index("--doctor") < body.index(".server"), (
            "the server is imported before --doctor is read, so the doctor "
            "cannot run on a broken install"
        )


class TestTheLauncher:
    def test_a_venv_python_is_not_mistaken_for_the_running_one(self, tmp_path, monkeypatch):
        """The bug that made the first version of this launcher useless.

        A virtualenv's ``bin/python`` is a symlink to the interpreter it was
        built from, so ``Path(venv).resolve() == Path(sys.executable).resolve()``
        is true for a venv that is not the interpreter now running. Comparing
        them that way sent the server down the in-process path on the very
        Python that could not import it -- the exact failure the launcher was
        added to remove, reintroduced inside the fix.
        """
        launcher = load_launcher()

        venv = tmp_path / ".venv" / "bin"
        venv.mkdir(parents=True)
        interpreter = venv / "python"
        try:
            interpreter.symlink_to(pathlib.Path(sys.executable).resolve())
        except (OSError, NotImplementedError) as exc:
            # Windows refuses symlinks without Developer Mode or elevation, and
            # the machine with the Inventor seat runs this suite. The property
            # is platform-independent; only this way of setting it up is not.
            pytest.skip(f"cannot create a symlink here: {exc}")

        monkeypatch.setattr(launcher, "VENV_PYTHONS", (interpreter,))
        found = launcher.candidates()

        assert (str(interpreter), False) in found, found
        current = [python for python, is_current in found if is_current]
        assert current == [sys.executable], found

    def test_the_running_interpreter_is_always_a_candidate_and_flagged(self):
        """Whatever the client launched must be offered, and recognised as
        itself. Written as one property rather than "it is last", which was the
        earlier assertion and was wrong the moment a `.venv` existed *and* was
        the interpreter running: the venv entry then deduped `sys.executable`
        away and carried `False`, so the launcher spawned a redundant copy of
        the very interpreter it was already inside.
        """
        launcher = load_launcher()
        found = launcher.candidates()

        assert found, "sys.executable is always a candidate"
        assert sys.executable in [python for python, _ in found]
        current = [python for python, is_current in found if is_current]
        assert current == [sys.executable], found

    def test_the_venv_is_tried_before_whatever_the_client_launched(self):
        """The installer fills `.venv`, so it is the best guess -- and the
        interpreter the client happened to pick is the thing being worked
        around, so it goes last."""
        launcher = load_launcher()
        found = [python for python, _ in launcher.candidates()]
        if len(found) > 1:
            assert found[-1] == sys.executable, found

    def test_it_asks_for_the_import_that_actually_breaks(self):
        """Both ``mcp`` and the package, not just the package.

        An editable install puts ``inventor_mcp`` on the path of an interpreter
        that may still be missing ``mcp``, and ``inventor_mcp/__init__.py``
        imports nothing, so importing it alone succeeds there. Checking only
        that would hand the server to the interpreter that cannot run it.
        """
        source = (ROOT / "scripts" / "serve.py").read_text(encoding="utf-8")
        assert "import mcp, inventor_mcp" in source

    def test_this_interpreter_can_serve(self):
        """The suite's own Python has the deps, so the check must say yes.

        A ``can_serve`` that answered no for a working install would send every
        start down the explanation path.
        """
        launcher = load_launcher()
        assert launcher.can_serve(sys.executable)

    def test_it_refuses_an_interpreter_that_cannot(self, tmp_path):
        launcher = load_launcher()
        assert not launcher.can_serve(tmp_path / "not-a-python")

    def test_nothing_usable_explains_itself_on_stderr(self, capsys, monkeypatch):
        """Stderr knowing the client discards it, because stdout is the protocol
        and prose written there corrupts the stream rather than being ignored.
        The message is for whoever runs the command by hand afterwards."""
        launcher = load_launcher()
        monkeypatch.setattr(launcher, "can_serve", lambda python: False)

        assert launcher.main(["--backend", "auto"]) == 2
        printed = capsys.readouterr()
        assert printed.out == "", "the stdio transport's stream was written to"
        assert "cannot start" in printed.err
        assert "--doctor" in printed.err
        assert sys.executable in printed.err


def test_doctor_is_documented_by_help(capsys):
    """``--help`` has to mention it, or nobody finds it when they need it."""
    from inventor_mcp.server import main

    with pytest.raises(SystemExit) as exit_code:
        main(["--help"])
    assert exit_code.value.code == 0
    assert "--doctor" in capsys.readouterr().out


class TestWhenTheServerIsFineAndInventorIsNot:
    """The link the doctor deliberately does not touch until it is asked to.

    Every other check passed on the machine with the Inventor seat -- interpreter,
    SDK, pywin32, backend, server, Node, analyser, all `ok` -- which is what
    ruled the server out and left the COM connection as the only thing between
    the two. ``--doctor --connect`` is that link, and it attaches to a running
    session rather than dispatching one, so it can neither launch Inventor nor
    take a licence to answer the question.

    The three failures below are three different repairs, and ``GetActiveObject``
    reports the first two identically. The HRESULT is the only thing that tells
    them apart, so the mapping is asserted here rather than trusted -- these
    branches cannot run on the offline legs at all.
    """

    def com_error(self, hresult: int) -> Exception:
        """A stand-in for ``pythoncom.com_error``, which is Windows-only.

        pywin32 puts the HRESULT in ``args[0]`` and, on newer builds, also on an
        ``hresult`` attribute. Both are read, so both are set here.
        """
        exc = OSError(hresult, "stand-in for a com_error")
        exc.hresult = hresult  # type: ignore[attr-defined]
        return exc

    def test_access_denied_is_named_as_an_elevation_mismatch(self):
        """The answer that fits "it worked yesterday" better than any other.

        The running-object table is per Windows integrity level, so an elevated
        Inventor is invisible to an unelevated server and the other way round.
        Nothing about the install has to change for this to start happening --
        somebody launching Inventor as administrator once is enough.
        """
        from inventor_mcp import preflight

        finding = preflight._no_running_inventor(
            self.com_error(preflight._E_ACCESSDENIED)
        )
        assert finding.status == preflight.FAIL
        assert "integrity" in (finding.hint or "")
        assert "administrator" in (finding.hint or "")

    def test_an_unresolvable_progid_asks_for_a_repair_not_a_reinstall(self):
        from inventor_mcp import preflight

        finding = preflight._no_running_inventor(
            self.com_error(preflight._CO_E_CLASSSTRING)
        )
        assert finding.status == preflight.FAIL
        assert "repair" in (finding.hint or "").lower()

    def test_nothing_running_is_a_warning_not_a_failure(self):
        """Inventor being closed is not a broken install.

        A FAIL here would make the doctor exit non-zero on a perfectly healthy
        machine that simply had Inventor shut, which teaches people to ignore
        the exit code.
        """
        from inventor_mcp import preflight

        finding = preflight._no_running_inventor(
            self.com_error(preflight._MK_E_UNAVAILABLE)
        )
        assert finding.status == preflight.WARN
        assert "Start Inventor" in (finding.hint or "")

    def test_an_hresult_nobody_predicted_still_reports(self):
        from inventor_mcp import preflight

        finding = preflight._no_running_inventor(self.com_error(-2147467259))
        assert finding.status == preflight.FAIL
        assert finding.hint

    def test_an_exception_carrying_no_hresult_is_read_as_not_running(self):
        """``GetActiveObject`` can raise something that is not a ``com_error``.

        Treated as "not running" rather than as a hard failure: it is the
        commonest case by a wide margin, and the hint covers the rest.
        """
        from inventor_mcp import preflight

        finding = preflight._no_running_inventor(RuntimeError("no idea"))
        assert finding.status == preflight.WARN

    def test_the_check_is_off_unless_asked(self):
        """It needs Inventor running before its answer means anything, so it
        cannot be part of the default report."""
        from inventor_mcp import preflight

        default = {f.name for f in preflight.run_checks()}
        asked = {f.name for f in preflight.run_checks(connect=True)}
        assert "inventor" not in default
        assert "inventor" in asked

    def test_it_attaches_and_never_dispatches(self):
        """``Dispatch`` would start Inventor and take a licence to answer a
        question about whether it was running.

        Read off the syntax tree rather than the text: the docstring above says
        the word ``Dispatch`` in the course of explaining why the code does not,
        and a substring search cannot tell the promise from the breach.
        """
        tree = ast.parse(
            (ROOT / "inventor_mcp" / "preflight.py").read_text(encoding="utf-8")
        )
        check = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "check_inventor"
        )
        called = {
            node.func.attr for node in ast.walk(check)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "GetActiveObject" in called
        for launches in ("Dispatch", "DispatchEx", "EnsureDispatch"):
            assert launches not in called, f"check_inventor calls {launches}"

    def test_a_failure_here_does_not_read_as_a_server_that_will_not_start(self):
        """Two different jobs. Reporting the wrong one sends somebody to
        reinstall a package that was never the problem."""
        from inventor_mcp import preflight

        findings = [
            preflight.Finding("server", preflight.OK, "builds"),
            preflight.Finding("inventor", preflight.FAIL, "access denied",
                              hint="both elevated, or neither"),
        ]
        out = io.StringIO()
        preflight.report(findings, out=out)
        printed = out.getvalue()
        assert "cannot reach Inventor" in printed
        assert "will not start" not in printed


class FakeRegistry:
    """Just enough of ``winreg`` to drive the registration probe off Windows.

    *values* maps a key path to its default value. A path that is absent raises
    ``FileNotFoundError``, which is what ``winreg.OpenKey`` raises for a key that
    is not there, and *broken* raises ``OSError`` instead -- the registry being
    unreadable rather than the key being missing.
    """

    HKEY_CLASSES_ROOT = object()

    def __init__(self, values: dict[str, str] | None = None, broken: bool = False):
        self.values = values or {}
        self.broken = broken

    class _Key:
        def __init__(self, value: str):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def OpenKey(self, root, path):  # noqa: N802 - mirrors winreg's own name
        assert root is FakeRegistry.HKEY_CLASSES_ROOT
        if self.broken:
            raise OSError(5, "Access is denied")
        if path not in self.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return FakeRegistry._Key(self.values[path])

    def QueryValueEx(self, key, name):  # noqa: N802 - mirrors winreg's own name
        assert name == ""
        return key.value, 1


class TestWhetherInventorIsRegisteredAtAll:
    """The probe that got this wrong on the first attempt.

    It asked ``pythoncom.CLSIDFromProgID``, which does not exist. The
    ``AttributeError`` was swallowed by a broad ``except`` and reported as
    `Inventor.Application` is not registered on this machine` — on a machine
    whose Inventor was registered and working. The report was confident, wrong,
    and pointed at repairing an install that had nothing wrong with it.

    So the question is answered from the registry, which is what "registered"
    means, and the answer is three-valued: yes, no, and could-not-tell. The last
    of those is the one that matters, and every test below exists to keep it from
    collapsing into "no" again.
    """

    def test_a_registered_progid_is_found_with_its_version(self):
        from inventor_mcp import preflight

        registry = FakeRegistry({
            "Inventor.Application\\CLSID": "{B6B5DC40-96E3-11D2-B774-0060B0F159EF}",
            "Inventor.Application\\CurVer": "Inventor.Application.28",
        })
        got = preflight._progid_registration(registry=registry)
        assert got.present is True
        assert got.version == "Inventor.Application.28"
        assert got.clsid.startswith("{")

    def test_a_missing_curver_is_not_a_fault(self):
        """Absent on plenty of healthy installs. Not knowing which version is
        current is not the same as not being registered."""
        from inventor_mcp import preflight

        registry = FakeRegistry({"Inventor.Application\\CLSID": "{...}"})
        got = preflight._progid_registration(registry=registry)
        assert got.present is True
        assert got.version is None

    def test_an_absent_key_is_the_one_real_no(self):
        from inventor_mcp import preflight

        got = preflight._progid_registration(registry=FakeRegistry({}))
        assert got.present is False
        assert got.reason is None

    def test_an_unreadable_registry_is_not_reported_as_absent(self):
        """The bug, in the form it would take next time.

        ``present`` must come back ``None``, because that is the truth: the
        question was not answered. ``False`` here is the false accusation.
        """
        from inventor_mcp import preflight

        got = preflight._progid_registration(registry=FakeRegistry(broken=True))
        assert got.present is None
        assert got.reason

    def test_a_probe_that_raises_something_unexpected_still_says_it_cannot_tell(self):
        """Exactly what happened: the probe itself was broken.

        An `AttributeError` from calling a function that does not exist must
        never become a verdict about the machine.
        """
        from inventor_mcp import preflight

        class Hostile:
            HKEY_CLASSES_ROOT = FakeRegistry.HKEY_CLASSES_ROOT

            def OpenKey(self, root, path):  # noqa: N802
                raise AttributeError("module 'pythoncom' has no attribute 'nope'")

        got = preflight._progid_registration(registry=Hostile())
        assert got.present is None
        assert "AttributeError" in (got.reason or "")

    def test_only_a_definite_no_is_allowed_to_fail_the_check(self):
        """``check_inventor`` fails on `present is False` and on nothing else.

        Read off the syntax tree: an `if not registration.present` would pass
        every test above and still turn could-not-tell into a missing install,
        because `None` is falsey. That is the bug, spelled differently.
        """
        tree = ast.parse(
            (ROOT / "inventor_mcp" / "preflight.py").read_text(encoding="utf-8")
        )
        check = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "check_inventor"
        )
        tests = [
            ast.unparse(node.test) for node in ast.walk(check)
            if isinstance(node, ast.If)
        ]
        assert "registration.present is False" in tests, tests
        for sloppy in ("not registration.present", "registration.present == False"):
            assert sloppy not in tests, f"check_inventor branches on `{sloppy}`"

    def test_the_registry_is_read_rather_than_a_com_api_guessed(self):
        """Why the probe is winreg: it is standard library, it is what
        "registered" means, and it needs no API name guessed at."""
        tree = ast.parse(
            (ROOT / "inventor_mcp" / "preflight.py").read_text(encoding="utf-8")
        )
        probe = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_progid_registration"
        )
        imported = {
            alias.name for node in ast.walk(probe)
            if isinstance(node, ast.Import) for alias in node.names
        }
        assert imported == {"winreg"}, imported

    def test_a_known_registration_is_named_when_nothing_is_running(self):
        """"Registered as Inventor.Application.28 but not running" rules the
        registration out on the spot, rather than leaving it as the next
        suspect."""
        from inventor_mcp import preflight

        finding = preflight._no_running_inventor(
            OSError(preflight._MK_E_UNAVAILABLE, "unavailable"),
            preflight.Registration(
                preflight.PROGID, True, clsid="{...}",
                version="Inventor.Application.28",
            ),
        )
        assert finding.status == preflight.WARN
        assert "Inventor.Application.28" in finding.detail

    def test_it_says_nothing_about_a_registration_it_does_not_know(self):
        from inventor_mcp import preflight

        finding = preflight._no_running_inventor(
            OSError(preflight._MK_E_UNAVAILABLE, "unavailable"),
            preflight.Registration(preflight.PROGID, None, reason="could not read"),
        )
        assert "registered as" not in finding.detail


class TestTheCheckThatAnswersForTheClient:
    """Every other check passed while nothing worked, and this is why.

    `python`, `mcp sdk`, `pydantic`, `pywin32`, `backend`, `server`, `node`,
    `analyser` and a live attach to Inventor 2027.1 all read `ok` on the machine
    with the seat -- because every one of them describes **the interpreter
    running the doctor**, which is the one somebody typed an absolute path to.
    A client launches a different command, out of a config file, with no shell
    and no virtualenv. Nothing was checking that command, so a report of eight
    green lines was consistent with a client that could not start the server at
    all.

    So this check launches what the config says, appends `--doctor`, and lets
    the thing launched answer for itself. It is not a model of the client's
    launch; it is the launch.
    """

    def config(self, path: pathlib.Path, command: str, args: list[str]) -> pathlib.Path:
        path.write_text(
            json.dumps({"mcpServers": {"inventor": {
                "command": command, "args": args,
            }}}),
            encoding="utf-8",
        )
        return path

    def test_it_reads_the_launch_command_out_of_a_config(self, tmp_path):
        from inventor_mcp import preflight

        written = self.config(
            tmp_path / "claude_desktop_config.json",
            "C:\\python.exe", ["-m", "inventor_mcp", "--backend", "auto"],
        )
        assert preflight.server_entries(written) == [
            ("inventor", ["C:\\python.exe", "-m", "inventor_mcp",
                          "--backend", "auto"]),
        ]

    def test_it_matches_on_the_command_not_the_key(self, tmp_path):
        """Registered under another name is still this server; a different
        server registered as `inventor` is not, and is none of our business."""
        from inventor_mcp import preflight

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"mcpServers": {
            "cad": {"command": "py", "args": ["-m", "inventor_mcp"]},
            "inventor": {"command": "node", "args": ["some-other-server.js"]},
        }}), encoding="utf-8")
        assert [name for name, _ in preflight.server_entries(path)] == ["cad"]

    def test_a_config_that_does_not_parse_is_a_finding_not_an_absence(self, tmp_path):
        """The client cannot read it either, and the symptom is identical to the
        server never having been registered."""
        from inventor_mcp import preflight

        path = tmp_path / "config.json"
        path.write_text("{ not json", encoding="utf-8")
        with pytest.raises(preflight.ConfigUnreadable):
            preflight.server_entries(path)

    def test_a_config_with_no_mcpservers_yields_nothing(self, tmp_path):
        from inventor_mcp import preflight

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        assert preflight.server_entries(path) == []

    def test_the_repos_own_config_is_among_the_places_looked(self):
        from inventor_mcp import preflight

        assert (ROOT / ".mcp.json") in preflight.client_configs()

    def test_a_working_command_probes_clean(self):
        """The suite's interpreter with `-m inventor_mcp` is, by definition, a
        command that starts the server."""
        from inventor_mcp import preflight

        ok, detail = preflight.probe([sys.executable, "-m", "inventor_mcp"])
        assert ok, detail

    def test_a_command_that_cannot_serve_is_caught(self, tmp_path):
        from inventor_mcp import preflight

        ok, detail = preflight.probe([str(tmp_path / "nope"), "-m", "inventor_mcp"])
        assert not ok
        assert "would not launch" in detail

    def test_the_probe_does_not_probe_itself(self, monkeypatch):
        """`--doctor` appended to the config's own command means the child runs
        this very check. Without the marker it launches a doctor that launches a
        doctor, and so on."""
        from inventor_mcp import preflight

        monkeypatch.setenv(preflight.DOCTOR_CHILD, "1")
        assert preflight.check_registration().status == preflight.SKIP

    def test_the_quoted_reason_is_the_verdict_not_the_closing_advice(self):
        """A launcher that fails ends its explanation with "Then: ... --doctor",
        which says nothing quoted on its own -- and was what the first version
        of this printed."""
        from inventor_mcp import preflight

        stderr = (
            "inventor-mcp cannot start: no interpreter here can import it.\n"
            "\n"
            "  tried: /usr/bin/python\n"
            "\n"
            "  Then:  .venv/bin/python -m inventor_mcp --doctor\n"
        )
        assert preflight._why(None, stderr) == (
            "inventor-mcp cannot start: no interpreter here can import it."
        )

    def test_a_childs_own_summary_is_preferred_to_its_first_line(self):
        from inventor_mcp import preflight

        stdout = (
            "inventor-mcp 0.1.0 preflight\n"
            "[FAIL] mcp sdk      not importable\n"
            "The server will not start: mcp sdk\n"
        )
        assert preflight._why(stdout, "") == "The server will not start: mcp sdk"

    def test_silence_still_reports_something(self):
        from inventor_mcp import preflight

        assert preflight._why("", "") == ""

    def test_a_broken_client_command_is_not_called_a_broken_server(self):
        """The distinction the check exists to draw.

        "The server will not start" is false when it starts perfectly from the
        path somebody just typed, and it sends them to reinstall a package that
        was never the problem.
        """
        from inventor_mcp import preflight

        out = io.StringIO()
        preflight.report([
            preflight.Finding("server", preflight.OK, "builds"),
            preflight.Finding("clients", preflight.FAIL, "config:inventor died",
                              hint="point it at an absolute path"),
        ], out=out)
        printed = out.getvalue()
        assert "the command your client launches does not" in printed
        assert "The server will not start" not in printed

    def test_no_config_anywhere_is_a_warning(self, tmp_path, monkeypatch):
        """Nothing registered is not a broken install -- it is an install that
        has not been wired to a client yet."""
        from inventor_mcp import preflight

        monkeypatch.setattr(preflight, "client_configs", lambda: [])
        monkeypatch.delenv(preflight.DOCTOR_CHILD, raising=False)
        finding = preflight.check_registration()
        assert finding.status == preflight.WARN
        assert sys.executable in (finding.hint or "")


class TestStartingIsNotServing:
    """The gap that survived every check before it.

    On the machine with the seat: the server started from the client's own
    command, the client config named an absolute virtualenv interpreter, Inventor
    attached at 2027.1, and `clients` read `ok` — because `ok` meant "the process
    started, imported everything and exited 0". A server that does all of that
    and then fails or hangs answering `initialize` is indistinguishable from the
    outside: the client reports `CONNECTION_CLOSED`, which is what it says about
    a server it never heard from.

    So the probe speaks MCP. Raw JSON-RPC rather than the SDK's own client,
    because what is in doubt is the wire, and an SDK client talking to an SDK
    server would be blind to exactly the mismatch worth finding.
    """

    def test_a_real_server_answers_initialize(self):
        from inventor_mcp import preflight

        ok, detail = preflight.handshake(
            [sys.executable, "-m", "inventor_mcp", "--backend", "mock"]
        )
        assert ok, detail
        assert "serves" in detail
        assert "protocol" in detail

    def test_every_protocol_version_a_client_might_ask_for_is_answered(self):
        """An unpinned `mcp>=1.2` means a reinstall can move the SDK under a
        working install, and a server that had dropped the version an installed
        client speaks would fail exactly like this — start fine, never talk.

        Asserted rather than assumed, because the dependency floor permits the
        SDK to change without anything here changing.
        """
        from inventor_mcp import preflight

        for version in ("2024-11-05", "2025-03-26", "2025-06-18"):
            ok, detail = preflight.handshake(
                [sys.executable, "-m", "inventor_mcp", "--backend", "mock"],
                version=version,
            )
            assert ok, f"{version}: {detail}"

    def test_a_command_that_dies_is_reported_with_its_stderr(self):
        """The stderr a client throws away is the whole reason it said nothing."""
        from inventor_mcp import preflight

        ok, detail = preflight.handshake(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); raise SystemExit(3)"]
        )
        assert not ok
        assert "boom" in detail or "exited 3" in detail

    def test_a_server_that_never_answers_is_called_hung_not_crashed(self):
        """Two different faults with one symptom. A client cannot tell them
        apart; this can, and the repairs are not the same.

        The elapsed time is asserted, not incidental. The first version of this
        returned the right answer after **120 seconds** against a child that
        slept for 120 -- because it closed the stream before terminating the
        child, and closing waits on the buffer lock that the blocked
        ``readline()`` is holding. So the report of a hang was itself hostage to
        the hang, which on a real hung server means a doctor that never comes
        back. The wording was right and the timeout did nothing.
        """
        import time

        from inventor_mcp import preflight

        started = time.monotonic()
        ok, detail = preflight.handshake(
            [sys.executable, "-c", "import time; time.sleep(120)"], timeout=3,
        )
        elapsed = time.monotonic() - started

        assert not ok
        assert "hung" in detail
        assert elapsed < 30, (
            f"took {elapsed:.0f}s to report a 3s timeout -- the timeout is not "
            "in control, and against a real hung server this would not return"
        )

    def test_prose_on_stdout_is_caught_as_a_corrupted_stream(self):
        """stdout carries the protocol. A stray `print`, or a logging handler
        left on stdout, makes a client drop the connection — and looks like
        nothing at all from the server's side."""
        from inventor_mcp import preflight

        ok, detail = preflight.handshake(
            [sys.executable, "-c",
             "import sys; sys.stdin.readline(); print('hello from a stray print')"],
        )
        assert not ok
        assert "not clean" in detail

    def test_a_refusal_is_quoted(self):
        from inventor_mcp import preflight

        script = (
            "import sys, json;"
            "sys.stdin.readline();"
            "print(json.dumps({'jsonrpc':'2.0','id':1,"
            "'error':{'code':-32602,'message':'unsupported protocol version'}}))"
        )
        ok, detail = preflight.handshake([sys.executable, "-c", script])
        assert not ok
        assert "unsupported protocol version" in detail

    def test_the_probe_runs_doctor_before_it_tries_to_talk(self):
        """Order matters. A broken launch reports what is missing far better
        through its own doctor than through a dead pipe."""
        source = (ROOT / "inventor_mcp" / "preflight.py").read_text(encoding="utf-8")
        body = source[source.index("def probe("):]
        body = body[: body.index("\ndef ", 1)] if "\ndef " in body[1:] else body
        assert body.index("--doctor") < body.index("handshake(")

    def test_the_handshake_speaks_json_rpc_itself(self):
        """Not through the SDK's client: a mismatch between the SDK's client and
        its own server is the fault this would then be unable to see."""
        tree = ast.parse(
            (ROOT / "inventor_mcp" / "preflight.py").read_text(encoding="utf-8")
        )
        fn = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "handshake"
        )
        imported = {
            node.module for node in ast.walk(fn)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "mcp.client.stdio" not in imported
        assert "mcp.types" in imported, "the protocol version should come from the SDK"


class TestTheOneThingTheProbeCannotSee:
    """A bare command probes green from a shell and can still fail under a client.

    `.mcp.json` says `"command": "python"`. The probe launches it and it serves,
    because the probe runs from a shell whose PATH resolves `python`. A
    GUI-launched client — Claude Code running inside the desktop app, an IDE
    extension — is handed the environment Windows gives GUI processes, which is
    not that PATH. On Windows the gap is sharper: the first `python` on many
    machines is the WindowsApps execution alias, which resolves for a console
    session and not reliably for a process spawned without that context.

    So the check reports it while still saying the launch worked. Reporting only
    what it could test would be a claim it cannot support, and this is the one
    failure mode left that looks exactly like the original symptom: a server
    that never speaks, and a client that says only that the connection closed.
    """

    def test_a_bare_command_is_recognised(self):
        from inventor_mcp import preflight

        assert preflight.resolves_by_path("python")
        assert preflight.resolves_by_path("python.exe")

    def test_a_path_is_not(self):
        """Both separators, because the string comes out of a config file that
        may name a Windows path while something on another platform reads it."""
        from inventor_mcp import preflight

        assert not preflight.resolves_by_path("C:\\Users\\J\\.venv\\python.exe")
        assert not preflight.resolves_by_path("/usr/local/bin/python")
        assert not preflight.resolves_by_path(".venv/bin/python")
        assert not preflight.resolves_by_path(".venv\\Scripts\\python.exe")

    def test_the_repos_own_config_is_the_case_in_point(self):
        """Kept honest deliberately. `.mcp.json` cannot name an absolute path --
        it is shared, and the interpreter is somewhere different on every
        machine -- so the fragility is real and permanent, and the doctor says
        so rather than the README alone."""
        config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        from inventor_mcp import preflight

        command = config["mcpServers"]["inventor"]["command"]
        assert preflight.resolves_by_path(command), (
            "if .mcp.json ever names an absolute interpreter this test should "
            "go, along with the warning it justifies"
        )

    def test_a_working_bare_command_still_warns(self, tmp_path, monkeypatch):
        from inventor_mcp import preflight

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"mcpServers": {"inventor": {
            "command": "python", "args": ["-m", "inventor_mcp"],
        }}}), encoding="utf-8")
        monkeypatch.setattr(preflight, "client_configs", lambda: [path])
        monkeypatch.setattr(preflight, "probe", lambda argv: (True, "serves"))
        monkeypatch.delenv(preflight.DOCTOR_CHILD, raising=False)

        finding = preflight.check_registration()
        assert finding.status == preflight.WARN
        assert "PATH" in finding.detail
        assert "absolute path" in (finding.hint or "")

    def test_an_absolute_command_that_works_is_simply_ok(self, tmp_path, monkeypatch):
        """The warning has to be about the bare name, not about every success,
        or it is noise that gets tuned out."""
        from inventor_mcp import preflight

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"mcpServers": {"inventor": {
            "command": sys.executable, "args": ["-m", "inventor_mcp"],
        }}}), encoding="utf-8")
        monkeypatch.setattr(preflight, "client_configs", lambda: [path])
        monkeypatch.setattr(preflight, "probe", lambda argv: (True, "serves"))
        monkeypatch.delenv(preflight.DOCTOR_CHILD, raising=False)

        assert preflight.check_registration().status == preflight.OK

    def test_a_launch_that_fails_outright_still_fails(self, tmp_path, monkeypatch):
        """A bare command that does not even start is a failure, not a warning
        about a hypothetical."""
        from inventor_mcp import preflight

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"mcpServers": {"inventor": {
            "command": "python", "args": ["-m", "inventor_mcp"],
        }}}), encoding="utf-8")
        monkeypatch.setattr(preflight, "client_configs", lambda: [path])
        monkeypatch.setattr(preflight, "probe", lambda argv: (False, "died"))
        monkeypatch.delenv(preflight.DOCTOR_CHILD, raising=False)

        assert preflight.check_registration().status == preflight.FAIL


class TestTheHookThatMakesAWebSessionRun:
    """A fresh container clones the repo and installs nothing.

    So `inventor_mcp` does not import, `pytest` collects nothing, and the
    `inventor` server in `.mcp.json` exits before it can speak -- reported as
    `CONNECTION_CLOSED` with no reason attached, which is the same four words
    this whole file is about, from a different cause.
    """

    HOOK = pathlib.Path("/home/user/InventorMCP/.claude/hooks/session-start.sh")

    def hook(self) -> str:
        return (ROOT / ".claude" / "hooks" / "session-start.sh").read_text(
            encoding="utf-8"
        )

    def test_it_is_registered(self):
        settings = json.loads(
            (ROOT / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        commands = [
            hook["command"]
            for entry in settings["hooks"]["SessionStart"]
            for hook in entry["hooks"]
        ]
        assert any("session-start.sh" in c for c in commands), commands

    def test_it_is_executable(self):
        assert (ROOT / ".claude" / "hooks" / "session-start.sh").stat().st_mode & 0o111

    def test_it_leaves_a_local_machine_alone(self):
        """A hook that rebuilds an environment under somebody's feet is a hook
        that breaks their setup. The Windows machine with the CAD seat has a
        real one already."""
        assert "CLAUDE_CODE_REMOTE" in self.hook()

    def test_it_installs_into_a_venv_that_the_launcher_will_find(self):
        """Two problems, one place. `pip install -e .` into the container's own
        Python fails on a distro-managed package pip will not uninstall; and
        `scripts/serve.py` looks for `.venv` first, so the dependencies landing
        there is also what lets `.mcp.json` start a working server."""
        hook = self.hook()
        assert "venv" in hook
        assert ".venv/bin/python -m pip install --quiet -e \".[dev]\"" in hook

    def test_it_does_not_ask_for_the_windows_only_extra(self):
        """pywin32 is Windows-only, and asking for it on Linux fails the
        install for a backend that could not work there anyway."""
        assert '[inventor,dev]' not in self.hook()

    def test_a_private_submodule_that_cannot_be_fetched_is_not_fatal(self):
        """The analyser is a separate private repository. Everything except the
        DFM tools works without it, and `--doctor` names the fix."""
        hook = self.hook()
        assert "git submodule update --init --depth 1 dfm" in hook
        assert "|| echo" in hook
