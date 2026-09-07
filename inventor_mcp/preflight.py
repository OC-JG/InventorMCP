"""Why the server would not start, said out loud.

A stdio MCP server that raises before it speaks is invisible. The client spawns
it, the process writes a traceback to stderr and exits, and the client -- having
never seen a byte of protocol -- reports ``CONNECTION_CLOSED``. That is the same
message for a missing dependency, the wrong interpreter, a half-finished
install and a genuine crash, and stderr is discarded, so the one place the
reason was written is the one place nobody reads.

This module is the reason, printed. ``python -m inventor_mcp --doctor`` walks the
chain from the interpreter to the analyser and says which link is broken and how
to repair it.

Two rules follow from what it is for, and both matter more than they look:

* **It must run when the server cannot.** A missing ``mcp`` is the commonest
  cause of a closed connection, so nothing here may import ``mcp``, ``pydantic``
  or anything needing them at module level. Every such import is inside a
  check, inside a ``try``.
* **It must not reach Inventor.** Connecting launches Inventor or attaches to a
  live session, and a diagnostic that changes the thing it is diagnosing is not
  one. This reports whether the COM backend *could* connect; ``connect`` is
  still what connects.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: The oldest Python this package claims, as a pair so the check needs nothing
#: read from disk -- ``pyproject.toml`` is not installed beside the code in a
#: wheel. ``tests/test_supported_pythons.py`` holds this against the real floor.
PYTHON_FLOOR = (3, 11)

OK = "ok"
WARN = "warn"
FAIL = "fail"
#: Not run, because something it needs is already known to be missing. Its own
#: verdict would be noise: a backend that cannot import pydantic is not a broken
#: backend, and saying so sends somebody to fix the wrong thing.
SKIP = "skip"

#: Whether a status stops the server coming up. A missing Node is a warning
#: because everything except the DFM tools still works without it; a missing
#: ``mcp`` is a failure because nothing does.
MARKERS = {OK: " ok ", WARN: "warn", FAIL: "FAIL", SKIP: " -- "}

#: The checks whose subject is a dependency, and the names the rest of the
#: chain is skipped under when one of them fails.
DEPENDENCIES = ("mcp sdk", "pydantic")


@dataclass
class Finding:
    """One link in the chain, and what is true of it."""

    name: str
    status: str
    detail: str
    hint: str | None = None

    def line(self) -> str:
        return f"[{MARKERS[self.status]}] {self.name:<12} {self.detail}"


def _version_of(distribution: str) -> str:
    """The installed version of *distribution*, or a readable stand-in."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(distribution)
    except PackageNotFoundError:  # importable but not installed: a source tree
        return "unknown version"


# -- the checks -------------------------------------------------------------
#
# Each returns one Finding and raises nothing. A check that can fail in a way
# nobody predicted still has to report, because a doctor that crashes leaves
# the caller exactly where they started.


def check_python() -> Finding:
    got = sys.version_info[:2]
    where = sys.executable or "(no interpreter path)"
    detail = f"{got[0]}.{got[1]} at {where}"
    if got < PYTHON_FLOOR:
        return Finding(
            "python", FAIL, detail,
            hint=f"This package needs Python {PYTHON_FLOOR[0]}.{PYTHON_FLOOR[1]} "
                 f"or newer. Point the MCP client at a newer interpreter.",
        )
    return Finding("python", OK, detail)


def check_sdk() -> Finding:
    """The MCP SDK, and which server class the shim found in it.

    Naming the class matters: the SDK renamed ``FastMCP`` to ``MCPServer`` in
    2.0, and ``compat.py`` picks whichever is there. When a future release moves
    it again the symptom is a closed connection, and this line is what says so
    rather than leaving somebody to guess at a version number.
    """
    try:
        import mcp  # noqa: F401
    except Exception as exc:
        return Finding(
            "mcp sdk", FAIL, f"not importable: {exc}",
            hint="This interpreter cannot import the MCP SDK, so the server "
                 "exits before it can speak and the client says only that the "
                 "connection closed. Install the package into the interpreter "
                 "the client launches: `python -m pip install -e .`",
        )
    try:
        from .compat import ServerClass
    except Exception as exc:
        return Finding(
            "mcp sdk", FAIL, f"{_version_of('mcp')}, but no server class: {exc}",
            hint="Neither mcp.server.mcpserver.MCPServer nor "
                 "mcp.server.fastmcp.FastMCP could be imported. Pin a known "
                 "release: `python -m pip install 'mcp>=1.2'`",
        )
    return Finding(
        "mcp sdk", OK, f"{_version_of('mcp')}, using {ServerClass.__name__}"
    )


def check_pydantic() -> Finding:
    try:
        import pydantic  # noqa: F401
    except Exception as exc:
        return Finding(
            "pydantic", FAIL, f"not importable: {exc}",
            hint="Every tool signature is validated by pydantic, so the server "
                 "cannot register a single tool without it. "
                 "`python -m pip install -e .`",
        )
    return Finding("pydantic", OK, _version_of("pydantic"))


def check_com() -> Finding:
    """Whether Inventor's automation API is reachable from here.

    Not on Windows this is neither a failure nor a warning: the simulator is
    what this platform is for, and reporting a missing pywin32 as a problem on
    Linux teaches people to ignore the report.
    """
    if sys.platform != "win32":
        return Finding(
            "pywin32", OK,
            f"not applicable on {sys.platform}; the simulator is the backend here",
        )
    try:
        import win32com.client  # noqa: F401
    except Exception as exc:
        return Finding(
            "pywin32", WARN, f"not importable: {exc}",
            hint="Without pywin32 the server starts but offers only the "
                 "simulator, so `connect` cannot reach Inventor. "
                 "`python -m pip install -e '.[inventor]'`",
        )
    return Finding("pywin32", OK, f"{_version_of('pywin32')}; Inventor is reachable")


def check_backend() -> Finding:
    """Which backend ``--backend auto`` settles on, without connecting.

    Worth its own line because ``auto`` is meant never to fail: it takes
    Inventor when Inventor is importable and the simulator everywhere else. It
    has failed anyway -- a syntax error in the COM backend once escaped the
    fall-back, because only ``BackendUnavailableError`` was caught -- and the
    symptom was a server that would not start on the platform the simulator
    exists to serve.
    """
    try:
        from .backend import create_backend

        backend = create_backend("auto")
    except Exception as exc:
        return Finding(
            "backend", FAIL, f"`auto` raised {type(exc).__name__}: {exc}",
            hint="`auto` is supposed to fall back to the simulator rather than "
                 "raise. Run with --backend mock to confirm, and report this: "
                 "the fall-back is broken, not your machine.",
        )
    name = getattr(backend, "name", "unknown")
    try:
        backend.disconnect()
    except Exception:  # pragma: no cover - nothing was connected to begin with
        pass
    return Finding("backend", OK, f"`auto` selects {name} (not connected)")


def check_server() -> Finding:
    """That the whole server actually assembles: tools, resources and prompts.

    The strongest check here, and the one closest to what the client does. Every
    tool is registered through the SDK's decorators, which inspect each
    signature as they run, so a bad annotation or a renamed SDK keyword fails
    here -- at import and registration time -- rather than at the first call.
    """
    try:
        from .server import create_server

        server = create_server("mock")
    except Exception as exc:
        return Finding(
            "server", FAIL, f"does not build: {type(exc).__name__}: {exc}",
            hint="The tools cannot be registered, so the process would exit "
                 "before answering `initialize`. The exception above is the "
                 "reason the client only said the connection closed.",
        )
    return Finding("server", OK, f"builds ({type(server).__name__})")


def check_node() -> Finding:
    node = shutil.which("node")
    if node is None:
        return Finding(
            "node", WARN, "not on PATH",
            hint="The DFM analyser is JavaScript, so the manufacturability "
                 "tools need Node 18+ (no npm install). Everything else works "
                 "without it: https://nodejs.org",
        )
    try:
        got = subprocess.run(
            [node, "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        reported = (got.stdout or got.stderr or "").strip() or "version unknown"
    except (OSError, subprocess.SubprocessError) as exc:
        return Finding(
            "node", WARN, f"{node} would not run: {exc}",
            hint="Node is on PATH but did not answer --version, so the DFM "
                 "bridge cannot be launched either.",
        )
    return Finding("node", OK, f"{reported} at {node}")


def check_analyser() -> Finding:
    """Whether a checkout of the DFM tool can be found.

    A warning rather than a failure, and separately reported from Node, because
    the two go missing for different reasons and are fixed by different
    commands. The commonest cause by a distance is a clone made without
    ``--recurse-submodules``: ``dfm/`` then exists as an empty directory, which
    looks present to everything except the check that opens a file inside it.
    """
    try:
        from .dfm.runner import find_dfm_root

        return Finding("analyser", OK, str(find_dfm_root()))
    except Exception as exc:
        hint = getattr(exc, "hint", None) or (
            "Fetch it with `git submodule update --init dfm`, or point "
            "INVENTOR_MCP_DFM_ROOT at a clone of the OnlyCat DFM repository."
        )
        return Finding(
            "analyser", WARN,
            f"not found: {getattr(exc, 'message', None) or exc}", hint=hint,
        )


#: HRESULTs worth telling apart, because they mean different repairs. Everything
#: else is reported with whatever text Inventor supplied.
_MK_E_UNAVAILABLE = -2147221021      # 0x800401E3: nothing in the running-object table
_CO_E_CLASSSTRING = -2147221005      # 0x800401F3: the ProgID resolves to nothing
_E_ACCESSDENIED = -2147024891        # 0x80070005: refused, usually across integrity levels


def check_inventor() -> Finding:
    """Whether a *running* Inventor can actually be reached from this process.

    Off by default -- ``--doctor --connect`` -- and the last check, because it is
    the only one that touches Inventor at all. Even then it only attaches to a
    session that is already open: ``GetActiveObject``, never ``Dispatch``, so it
    cannot launch Inventor or take a licence. What it costs is that Inventor has
    to be running for the answer to mean anything, which is the trade the
    default makes the other way.

    The three failures it separates are the three different repairs:

    * the ProgID resolves to nothing -- Inventor's COM registration is absent or
      broken, which is what a repair, an uninstall of one version, or a
      half-finished update leaves behind;
    * the ProgID is registered but nothing is in the running-object table --
      Inventor is not running, or is running somewhere this process cannot see
      it;
    * access denied -- the two processes are at different Windows integrity
      levels. One of them started elevated. This is the answer that fits "it
      worked yesterday" better than any other, because nothing about the install
      has to have changed for it to start happening.
    """
    if sys.platform != "win32":
        return Finding(
            "inventor", SKIP, f"not applicable on {sys.platform}",
        )
    try:
        import pythoncom  # type: ignore[import-not-found]
        import win32com.client  # type: ignore[import-not-found]
    except Exception as exc:
        return Finding(
            "inventor", WARN, f"pywin32 not importable: {exc}",
            hint="Without pywin32 there is nothing to connect with. "
                 "`python -m pip install -e '.[inventor]'`",
        )

    pythoncom.CoInitialize()
    try:
        # Asked first and separately: "Inventor is not running" and "Inventor is
        # not registered" are the same exception from GetActiveObject, and they
        # are not the same problem.
        try:
            pythoncom.CLSIDFromProgID("Inventor.Application")
        except Exception as exc:
            return Finding(
                "inventor", FAIL,
                f"`Inventor.Application` is not registered on this machine: {exc}",
                hint="Inventor's COM registration is missing, so nothing can "
                     "automate it -- the server included. Repair the Inventor "
                     "install from Autodesk Access, or run Inventor once as "
                     "administrator to let it re-register itself.",
            )
        try:
            app = win32com.client.GetActiveObject("Inventor.Application")
        except Exception as exc:
            return _no_running_inventor(exc)

        try:
            version = app.SoftwareVersion.DisplayVersion
            build = app.SoftwareVersion.BuildIdentifier
        except Exception as exc:
            return Finding(
                "inventor", WARN,
                f"attached, but it would not say which version: {exc}",
                hint="The connection is there. Something about this session is "
                     "refusing property reads -- a modal dialog waiting for an "
                     "answer will do it. Clear anything Inventor is asking and "
                     "try again.",
            )
        return Finding("inventor", OK, f"attached to {version} (build {build})")
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # pragma: no cover - Windows only
            pass


def _no_running_inventor(exc: BaseException) -> Finding:
    """``GetActiveObject`` refused. Which of the three reasons it was."""
    code = getattr(exc, "hresult", None)
    if code is None:
        args = getattr(exc, "args", ())
        code = args[0] if args and isinstance(args[0], int) else None

    if code == _E_ACCESSDENIED:
        return Finding(
            "inventor", FAIL, "access denied reaching the running Inventor",
            hint="The two processes are at different Windows integrity levels: "
                 "one of them is elevated and the other is not. Run Inventor "
                 "and whatever launches the server the same way -- both as "
                 "administrator, or neither. Neither is the better answer.",
        )
    if code == _CO_E_CLASSSTRING:
        return Finding(
            "inventor", FAIL, f"`Inventor.Application` did not resolve: {exc}",
            hint="Repair the Inventor install from Autodesk Access.",
        )
    if code == _MK_E_UNAVAILABLE or code is None:
        return Finding(
            "inventor", WARN, "no running Inventor session to attach to",
            hint="Start Inventor and run this again. If Inventor *is* open, it "
                 "is open somewhere this process cannot see it: a different "
                 "Windows user, a different remote-desktop session, or one of "
                 "the two elevated and the other not.",
        )
    return Finding(
        "inventor", FAIL, f"could not attach: {exc}",
        hint="Inventor is registered but refused the attach. Start Inventor by "
             "hand, confirm it opens normally, and try again.",
    )


#: In the order the chain actually runs, so the first FAIL is the one to fix,
#: each paired with whether it needs the package's dependencies to mean anything.
CHECKS = (
    (check_python, False),
    (check_sdk, False),
    (check_pydantic, False),
    (check_com, False),
    (check_backend, True),
    (check_server, True),
    (check_node, False),
    (check_analyser, True),
)

#: Run only when asked, because it is the one check that touches Inventor.
OPTIONAL_CHECKS = ((check_inventor, False),)


def run_checks(connect: bool = False) -> list[Finding]:
    """Every check, in order, with a crash in one reported rather than raised.

    Order is not cosmetic. ``backend`` and ``server`` both import the package,
    so on an install with no ``mcp`` they fail too -- with their own reasons,
    which are true and useless. Once a dependency has failed they are skipped
    instead, leaving one problem named in the summary rather than four.

    *connect* adds the one check that reaches Inventor. Off by default: a
    diagnostic that changes the thing it is diagnosing is not one, and this one
    needs Inventor to be running before its answer means anything.
    """
    findings: list[Finding] = []
    missing: list[str] = []
    for check, needs_dependencies in CHECKS + (OPTIONAL_CHECKS if connect else ()):
        if needs_dependencies and missing:
            findings.append(
                Finding(
                    _label(check), SKIP,
                    "not checked: " + ", ".join(missing) + " missing",
                )
            )
            continue
        try:
            finding = check()
        except Exception as exc:  # pragma: no cover - a check should not raise
            finding = Finding(
                _label(check), FAIL,
                f"the check itself raised {type(exc).__name__}: {exc}",
            )
        if finding.status == FAIL and finding.name in DEPENDENCIES:
            missing.append(finding.name)
        findings.append(finding)
    return findings


def _label(check) -> str:
    """A name for a check that never got as far as returning a Finding."""
    return getattr(check, "__name__", "check").removeprefix("check_")


def report(findings: list[Finding], out=None) -> None:
    """Print the findings, then the repair for each one that needs it."""
    stream = out if out is not None else sys.stdout
    print(f"inventor-mcp {_version_of('inventor-mcp')} preflight", file=stream)
    print("", file=stream)
    for finding in findings:
        print(finding.line(), file=stream)

    needs_work = [f for f in findings if f.status != OK and f.hint]
    if needs_work:
        print("", file=stream)
        for finding in needs_work:
            print(f"{finding.name}: {finding.hint}", file=stream)

    print("", file=stream)
    broken = [f for f in findings if f.status == FAIL]
    if broken:
        # Named rather than counted. "1 problem" sends somebody back up the
        # page; the name is the thing they act on.
        #
        # And said as what it is: `inventor` failing is a server that starts
        # perfectly and cannot reach the CAD seat, which is a different job from
        # a server that will not start. Reporting the second for the first sends
        # somebody to reinstall a package that was never the problem.
        names = ", ".join(f.name for f in broken)
        if [f.name for f in broken] == ["inventor"]:
            print(f"The server starts, but it cannot reach Inventor: {names}",
                  file=stream)
        else:
            print(f"The server will not start: {names}", file=stream)
    elif any(f.status == WARN for f in findings):
        print(
            "The server starts. Some tools are unavailable -- see above.",
            file=stream,
        )
    else:
        print("Everything the server needs is here.", file=stream)


def doctor(out=None, connect: bool = False) -> int:
    """Run every check, print the report, and exit non-zero if the server is dead."""
    findings = run_checks(connect=connect)
    report(findings, out=out)
    return 1 if any(f.status == FAIL for f in findings) else 0


def startup_failure(exc: BaseException, out=None) -> None:
    """What to print when importing the server itself failed.

    This is the message that would otherwise have been a bare ``ImportError``
    traceback on a stream the client throws away. It names the interpreter,
    because the interpreter is nearly always the answer: the package was
    installed into a virtualenv and the client is launching a different Python
    that knows nothing about it.
    """
    stream = out if out is not None else sys.stderr
    print("inventor-mcp cannot start.", file=stream)
    print(f"  {type(exc).__name__}: {exc}", file=stream)
    print("", file=stream)
    print(f"  interpreter: {sys.executable or '(unknown)'}", file=stream)
    print(f"  package:     {Path(__file__).resolve().parent}", file=stream)
    print("", file=stream)
    print(
        "  This interpreter cannot import what the server needs. Install the\n"
        "  package into it, or point the MCP client at the interpreter it was\n"
        "  installed into -- an absolute path to the virtualenv's python, not a\n"
        "  bare `python`, which an MCP client resolves without your shell PATH.\n"
        "\n"
        "  For the full picture:  python -m inventor_mcp --doctor",
        file=stream,
    )
