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

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


#: Set in a probe's environment so the probe does not probe itself. Without it
#: ``check_registration`` launches a doctor that launches a doctor.
DOCTOR_CHILD = "INVENTOR_MCP_DOCTOR_CHILD"


class ConfigUnreadable(Exception):
    """A client config that exists and cannot be parsed.

    Its own type because it is a finding rather than an absence: a config the
    client cannot read is a server the client will not start, and the symptom is
    identical to the server never having been registered.
    """


def client_configs() -> list[Path]:
    """The config files an MCP client reads to learn how to launch this server.

    Every one that exists, not the first: a machine can carry both the desktop
    app's config and the repository's project-scoped ``.mcp.json``, and having
    one of them right is no help if the client is reading the other.
    """
    found = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        found.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    home = Path.home()
    found.append(
        home / "Library" / "Application Support" / "Claude"
        / "claude_desktop_config.json"
    )
    found.append(home / ".config" / "Claude" / "claude_desktop_config.json")
    found.append(Path(__file__).resolve().parents[1] / ".mcp.json")
    return [path for path in found if path.is_file()]


def server_entries(config: Path) -> list[tuple[str, list[str]]]:
    """The launch commands in *config* that are this server, as (name, argv).

    Matched on what the command actually runs rather than on the key being
    called ``inventor``: it may have been registered under another name, and a
    config naming a *different* MCP server is none of this check's business.
    """
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigUnreadable(f"{config.name}: {exc}") from exc
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []

    found: list[tuple[str, list[str]]] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        args = entry.get("args") or []
        if not isinstance(command, str) or not isinstance(args, list):
            continue
        argv = [command, *(str(arg) for arg in args)]
        if any("inventor_mcp" in arg or "serve.py" in arg for arg in argv[1:]):
            found.append((str(name), argv))
    return found


#: How long to wait for a reply to ``initialize``. Generous: the first import of
#: pydantic and the SDK on a cold filesystem is seconds, and on Windows an
#: anti-virus scanner reading a new process's DLLs can add more. A server that
#: has not answered by here is hung, which is a different fault from a crash and
#: is reported as one.
HANDSHAKE_TIMEOUT = 60.0


def _read_line(stream: Any, timeout: float) -> str | None:
    """One line from *stream*, or ``None`` if it does not arrive in time.

    A reader thread and a queue, because there is no portable way to put a
    timeout on a blocking pipe read -- ``select`` does not take pipes on
    Windows, which is the platform this has to work on.
    """
    import queue
    import threading

    answers: queue.Queue = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            answers.put(stream.readline())
        except Exception:  # pragma: no cover - the pipe closed under us
            answers.put("")

    threading.Thread(target=read, daemon=True).start()
    try:
        return answers.get(timeout=timeout)
    except queue.Empty:
        return None


def handshake(
    argv: list[str],
    timeout: float = HANDSHAKE_TIMEOUT,
    version: str | None = None,
) -> tuple[bool, str]:
    """Speak MCP to *argv* over stdio, as a client would, and see if it answers.

    This is the check that `--doctor` could not make about itself. Appending
    ``--doctor`` to a config's command proves the process starts, imports
    everything and exits 0 -- and a server that does all that and then fails or
    hangs answering ``initialize`` looks identical from the outside:
    ``CONNECTION_CLOSED``, which is what a client reports for a server it never
    heard from. Starting is not serving, and the gap between them was where the
    fault lived.

    Raw JSON-RPC rather than the SDK's client, deliberately. What is in doubt is
    the wire, and a mismatch between the SDK's own client and its own server is
    exactly the class of fault this would then be blind to. One request, one
    line, one answer.
    """
    environment = dict(os.environ)
    environment[DOCTOR_CHILD] = "1"
    if version is None:
        # The SDK's own idea of current, so the probe asks for what a
        # contemporary client would. Overridable, because "does it still answer
        # the version an older installed client speaks" is a question worth
        # being able to ask directly.
        try:
            from mcp.types import LATEST_PROTOCOL_VERSION

            version = LATEST_PROTOCOL_VERSION
        except Exception:  # pragma: no cover - checked before this runs
            version = "2025-06-18"

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "inventor-mcp-doctor", "version": "1"},
        },
    })

    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=environment,
            # A relative `scripts/serve.py` in a project-scoped config is
            # resolved against the client's working directory, which for that
            # config is the project. Reproduce that rather than this shell's.
            cwd=str(Path(__file__).resolve().parents[1]),
        )
    except (OSError, ValueError) as exc:
        return False, f"would not launch: {exc}"

    try:
        try:
            child.stdin.write(request + "\n")
            child.stdin.flush()
        except OSError:
            # It died before it could be spoken to. Its stderr is the reason,
            # and is what a client throws away.
            return False, _died(child)

        line = _read_line(child.stdout, timeout)
        if line is None:
            if child.poll() is not None:
                return False, _died(child)
            return False, (
                f"started, but did not answer `initialize` within {timeout:.0f}s "
                "-- it is hung, not crashed"
            )
        if line == "":
            return False, _died(child)

        try:
            answer = json.loads(line)
        except json.JSONDecodeError:
            # Anything on stdout that is not the protocol corrupts the stream,
            # and a client will drop the connection over it. A print() or a
            # logging handler left on stdout is the usual culprit.
            return False, (
                "answered `initialize` with something that is not JSON-RPC, so "
                f"stdout is not clean: {line.strip()[:120]!r}"
            )
        if "error" in answer:
            return False, f"refused `initialize`: {answer['error']}"
        got = (answer.get("result") or {}).get("serverInfo") or {}
        spoke = (answer.get("result") or {}).get("protocolVersion", "?")
        name = got.get("name", "?")
        return True, f"serves ({name}, protocol {spoke})"
    finally:
        # Kill first, close second, and that order is load-bearing. A hung
        # server leaves the reader thread blocked inside `readline()`, which
        # holds the stream's internal buffer lock; closing that stream from this
        # thread then waits for the lock, which means waiting for the server to
        # produce a line -- the very thing that is not happening. Closing before
        # terminating turned a 3-second timeout into a 120-second block against
        # a child that slept for 120 seconds, so the report of a hang was itself
        # hostage to the hang.
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - refused SIGTERM
            child.kill()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        for end in (child.stdin, child.stdout, child.stderr):
            try:
                if end is not None:
                    end.close()
            except Exception:  # pragma: no cover
                pass


def _died(child: subprocess.Popen) -> str:
    """Why a child that will not talk is not talking."""
    try:
        _, stderr = child.communicate(timeout=10)
    except Exception:  # pragma: no cover - already gone
        stderr = ""
    code = child.poll()
    reason = _why(None, stderr)
    ending = f"exited {code}" if code is not None else "closed its pipes"
    return f"{ending}: {reason}" if reason else f"{ending} silently"


def resolves_by_path(command: str) -> bool:
    """Whether *command* is a bare name that has to be found on ``PATH``.

    The one thing a probe from here cannot test, and the reason it is reported
    anyway. This check launches the config's command from a shell, where
    ``python`` resolves to whatever that shell's ``PATH`` says. A GUI-launched
    client -- Claude Code running inside the desktop app, an IDE extension, the
    desktop app itself -- is handed the environment Windows gives GUI processes,
    which is not the one an interactive ``cmd`` gets. On Windows the gap is
    sharper still: the first ``python`` on many machines is the WindowsApps
    execution alias, which resolves for the logged-in user in a console and not
    reliably for a process spawned without that context.

    So a bare command probes green here and can still fail there, silently,
    which a client reports as ``CONNECTION_CLOSED``. An absolute path removes
    the variable rather than reasoning about it.

    Both separators are checked rather than using ``os.path``: this is asked
    about a string out of a config file, which may name a Windows path while
    something reads it on another platform.
    """
    return not any(separator in command for separator in ("/", "\\"))


def probe(argv: list[str]) -> tuple[bool, str]:
    """Whether the client's own command produces a server that answers.

    Two stages, because they fail for different reasons and the difference is
    the whole point. ``--doctor`` first: it is cheap, it exits by itself, and
    when the launch is broken its report names what is missing far better than a
    dead pipe does. Then the handshake, which is the claim that matters -- a
    process that starts is not yet a server that serves.
    """
    environment = dict(os.environ)
    environment[DOCTOR_CHILD] = "1"
    try:
        done = subprocess.run(
            [*argv, "--doctor"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, env=environment,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"would not launch: {exc}"
    if done.returncode != 0:
        return False, _why(done.stdout, done.stderr) or (
            f"exited {done.returncode} silently"
        )
    return handshake(argv)


#: How a doctor states its verdict. Recognised so a probe can quote the child's
#: own conclusion rather than guessing at which line of its output mattered.
_VERDICTS = ("The server will not start", "cannot start", "cannot reach Inventor")


def _why(stdout: str | None, stderr: str | None) -> str:
    """The one line of a failed probe's output worth repeating.

    Neither the first line nor the last will do. A child that got as far as
    running its own checks ends with a verdict naming what failed, and that is
    the line; a child that died launching writes an explanation whose *last*
    line is the closing advice -- "Then: ... --doctor" -- which is useless
    quoted out of context and was what the first version of this printed.
    """
    lines = [
        line.strip()
        for line in ((stderr or "") + "\n" + (stdout or "")).splitlines()
        if line.strip()
    ]
    for line in lines:
        if any(verdict in line for verdict in _VERDICTS):
            return line
    return lines[0] if lines else ""


def check_registration() -> Finding:
    """Whether the command a *client* launches can start the server.

    The check every other one here was standing in for, and the reason they
    could all pass while nothing worked. ``python``, ``mcp sdk``, ``server`` and
    the rest describe the interpreter running this doctor -- the one somebody
    typed a path to. A client launches a different command, out of a config
    file, with no shell and no virtualenv, and *that* command is the one that has
    to work. On the machine this was written for, every other line read ``ok``
    and the connection still closed.
    """
    if os.environ.get(DOCTOR_CHILD):
        return Finding("clients", SKIP, "not checked from inside a client probe")

    configs = client_configs()
    if not configs:
        return Finding(
            "clients", WARN, "no client config found",
            hint="Nothing on this machine is configured to launch the server. "
                 "Register it with the absolute path to this interpreter: "
                 f"`{sys.executable} -m inventor_mcp`",
        )

    working: list[str] = []
    broken: list[str] = []
    #: Registrations that probe green but name a command only PATH can find.
    fragile: list[str] = []
    for config in configs:
        try:
            entries = server_entries(config)
        except ConfigUnreadable as exc:
            broken.append(f"{exc} -- the client cannot read it either")
            continue
        for name, argv in entries:
            ok, detail = probe(argv)
            # The detail is kept either way. On success it names the protocol
            # version the two ends settled on, which is the first thing worth
            # knowing when a client that can start the server still will not
            # talk to it.
            where = f"{config.name}:{name}"
            (working if ok else broken).append(f"{where} {detail}")
            if ok and resolves_by_path(argv[0]) and argv[0] not in fragile:
                fragile.append(argv[0])

    if broken:
        return Finding(
            "clients", FAIL, "; ".join(broken),
            hint="This is the command your client runs, and it does not start "
                 "the server -- whatever this report says about the interpreter "
                 "you typed. Point that config at an absolute interpreter path: "
                 f"`{sys.executable}`. Then restart the client; the desktop app "
                 "has to be quit from its tray icon, not just closed.",
        )
    if not working:
        return Finding(
            "clients", WARN,
            f"{len(configs)} config(s) found, none registering this server",
            hint="Register it with the absolute path to this interpreter: "
                 f"`{sys.executable} -m inventor_mcp`",
        )
    if fragile:
        # Green, and reported anyway. This is the one failure mode the probe
        # above is structurally unable to see, so silence here would be a claim
        # the check cannot support.
        return Finding(
            "clients", WARN,
            "; ".join(working) + " -- but `"
            + "`, `".join(fragile)
            + "` is resolved through PATH, which this cannot test",
            hint="It starts when launched from a shell, which is how this was "
                 "just tested, and that is not how your client launches it. A "
                 "GUI-launched client gets the environment Windows gives GUI "
                 "processes, not your shell's PATH -- and on Windows the first "
                 "`python` is often the WindowsApps alias, which does not "
                 "resolve reliably outside a console. This check cannot "
                 "reproduce that, so it cannot clear it. Use an absolute path: "
                 f"`{sys.executable}`.",
        )
    return Finding("clients", OK, "; ".join(working))


#: Inventor's version-independent ProgID. The versioned ones -- ``.27``, ``.28``
#: -- are what an install actually writes; this one is the alias pointing at
#: whichever is current, which is why ``CurVer`` below is worth reporting.
PROGID = "Inventor.Application"


@dataclass
class Registration:
    """Whether a ProgID is registered for COM, and what it points at.

    ``present`` is deliberately three-valued. ``False`` means the registry was
    read and the key is not there; ``None`` means the question could not be
    answered, which is **not** the same answer and must not be reported as one.
    The first version of this check conflated them: it probed with a pythoncom
    function that does not exist, caught the ``AttributeError`` in a broad
    ``except``, and told somebody with a working Inventor that their COM
    registration was missing. A diagnostic that invents a fault is worse than
    one that admits it cannot tell.
    """

    progid: str
    present: bool | None
    clsid: str | None = None
    #: What ``CurVer`` points at -- the versioned ProgID of the current install.
    version: str | None = None
    #: Why ``present`` is ``None``.
    reason: str | None = None


def _registry_default(registry: Any, path: str) -> str:
    """The default value of ``HKEY_CLASSES_ROOT\\<path>``."""
    with registry.OpenKey(registry.HKEY_CLASSES_ROOT, path) as key:
        value, _ = registry.QueryValueEx(key, "")
        return str(value)


def _progid_registration(progid: str = PROGID, registry: Any = None) -> Registration:
    """Read the ProgID's COM registration out of the registry.

    ``winreg`` rather than a COM call, because the registry *is* what
    "registered" means here, it is standard library, and it needs no guessing at
    an API name -- which is exactly what went wrong the first time. *registry* is
    injectable so the branches can be tested off Windows, where neither
    ``winreg`` nor Inventor exists.
    """
    if registry is None:
        try:
            import winreg
        except ImportError as exc:  # not Windows
            return Registration(progid, None, reason=f"no winreg: {exc}")
        registry = winreg
    try:
        clsid = _registry_default(registry, f"{progid}\\CLSID")
    except FileNotFoundError:
        # The one answer that is a real "no": the key was looked for and is not
        # there. FileNotFoundError before OSError -- it is a subclass.
        return Registration(progid, False)
    except OSError as exc:
        return Registration(progid, None, reason=f"could not read the registry: {exc}")
    except Exception as exc:  # pragma: no cover - an injected registry misbehaving
        return Registration(progid, None, reason=f"{type(exc).__name__}: {exc}")

    version = None
    try:
        version = _registry_default(registry, f"{progid}\\CurVer")
    except Exception:
        # Absent on plenty of healthy installs. Not knowing which version is
        # current is not a fault, so it is not reported as one.
        pass
    return Registration(progid, True, clsid=clsid, version=version)


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

    # Asked first and separately: "Inventor is not running" and "Inventor is not
    # registered" are the same exception from GetActiveObject, and they are not
    # the same problem.
    registration = _progid_registration(PROGID)
    if registration.present is False:
        return Finding(
            "inventor", FAIL,
            f"`{PROGID}` has no COM registration on this machine",
            hint="Nothing can automate Inventor without it -- the server "
                 "included. Repair the Inventor install from Autodesk Access, "
                 "or run Inventor once as administrator to let it re-register "
                 "itself.",
        )

    pythoncom.CoInitialize()
    try:
        try:
            app = win32com.client.GetActiveObject(PROGID)
        except Exception as exc:
            return _no_running_inventor(exc, registration)

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


def _no_running_inventor(
    exc: BaseException, registration: Registration | None = None
) -> Finding:
    """``GetActiveObject`` refused. Which of the three reasons it was."""
    code = getattr(exc, "hresult", None)
    if code is None:
        args = getattr(exc, "args", ())
        code = args[0] if args and isinstance(args[0], int) else None

    # Worth saying when it is known: "registered as Inventor.Application.28 but
    # not running" is a different sentence from "not running", and it rules out
    # the registration on the spot rather than leaving it as the next suspect.
    registered = ""
    if registration is not None and registration.present:
        registered = f" (registered as {registration.version or registration.clsid})"

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
            "inventor", WARN,
            f"no running Inventor session to attach to{registered}",
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
    # Last of the default checks, and the only one that answers for the client
    # rather than for this shell. It launches a subprocess per registration, so
    # everything cheap has already reported by the time it runs.
    (check_registration, True),
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

    # Said before the verdict, because it changes what the verdict means. A
    # report of eight green lines and "everything the server needs is here" is
    # true of the package and wildly misleading about the machine: the whole
    # point of this server is driving a real CAD seat, and on a host with no
    # Windows there is no seat to drive and never will be. That is not a fault
    # to repair -- it is the wrong machine -- so it is stated rather than
    # failed, and stated where it cannot be read past.
    if sys.platform != "win32":
        print("", file=stream)
        print(
            f"Note: this is {sys.platform}, so there is no Inventor here and "
            "cannot be.\n"
            "  Everything below concerns the simulator. Recipes validate and "
            "the checks run,\n"
            "  but nothing reaches a CAD seat: no part is built and no file is "
            "written. To\n"
            "  drive Inventor, the server has to run on the Windows machine "
            "that has it --\n"
            "  a client in a container or a browser session cannot reach one "
            "from here.",
            file=stream,
        )

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
        failed = [f.name for f in broken]
        if failed == ["inventor"]:
            print(f"The server starts, but it cannot reach Inventor: {names}",
                  file=stream)
        elif failed == ["clients"]:
            # The distinction the whole check exists to draw. Every other line
            # can read `ok` -- and did -- while the command a client launches is
            # a different command that does not work. Saying "the server will
            # not start" here is false, and sends somebody to reinstall a
            # package that starts perfectly well from the path they just typed.
            print(
                "The server starts from here, but the command your client "
                "launches does not.", file=stream,
            )
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
    """Run every check, print the report, and exit non-zero if something failed."""
    return doctor_from(run_checks(connect=connect), out=out)


def doctor_from(findings: list[Finding], out=None) -> int:
    """Print *findings* and return the exit code they imply.

    Split from ``doctor`` so the verdict can be tested against findings chosen
    for the purpose. Warnings do not fail: Node and the analyser being absent
    leaves the modelling half working, and a non-zero exit on a machine that is
    fine for what it is used for teaches people to ignore the exit code.
    """
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
