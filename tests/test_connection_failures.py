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

    def test_every_check_reports_rather_than_raises(self):
        from inventor_mcp import preflight

        findings = preflight.run_checks()
        assert len(findings) == len(preflight.CHECKS)
        for finding in findings:
            assert finding.status in preflight.MARKERS, finding
            assert finding.detail, finding
            assert finding.line()

    def test_a_healthy_install_is_reported_as_healthy(self):
        """Run under the suite's own interpreter, which by definition has the deps.

        The analyser and Node are allowed to be missing -- they are warnings, and
        the offline CI legs have neither. A FAIL here means the interpreter
        running the tests could not start the server, which the next assertion
        would also have caught, later and less clearly.
        """
        from inventor_mcp import preflight

        broken = [f for f in preflight.run_checks() if f.status == preflight.FAIL]
        assert not broken, [(f.name, f.detail) for f in broken]

    def test_it_exits_zero_and_names_what_is_missing(self):
        from inventor_mcp import preflight

        out = io.StringIO()
        assert preflight.doctor(out=out) == 0
        printed = out.getvalue()
        assert "preflight" in printed
        for finding in preflight.run_checks():
            assert finding.name in printed, finding.name

    def test_every_problem_carries_a_repair(self):
        """A finding that is not OK and has no hint leaves somebody stuck.

        Skipped checks are the exception, and deliberately: their detail already
        names the failure that caused them, and a hint of their own would point
        at the wrong thing.
        """
        from inventor_mcp import preflight

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

    def test_the_running_interpreter_is_tried_last(self):
        """It is the one the client happened to launch, which is the thing being
        worked around. The venv the installer fills goes first."""
        launcher = load_launcher()
        found = launcher.candidates()
        assert found, "sys.executable is always a candidate"
        assert found[-1] == (sys.executable, True), found

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
