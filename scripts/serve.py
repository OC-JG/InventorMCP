#!/usr/bin/env python3
"""Launch the server on an interpreter that can actually import it.

The project-scoped ``.mcp.json`` used to run ``python -m inventor_mcp``. On the
machine this was installed on, that is the wrong Python. ``install.ps1`` builds
a ``.venv`` and registers the server by the absolute path to the interpreter
inside it, and the README says in as many words that a bare ``"python"`` is the
commonest reason an MCP server shows as failed -- because a client launches it
without the shell's ``PATH`` and without any virtualenv the shell had active.
The shipped ``.mcp.json`` then did the thing the README warns against, so
opening this repository with Claude Code spawned a Python with no ``mcp``
installed, which exited before speaking and was reported as
``CONNECTION_CLOSED``.

This script closes that gap. ``.mcp.json`` runs it with whatever ``python`` is
on PATH -- it needs only the standard library -- and it re-executes the server
on the first interpreter that can import the package. The repository's own
``.venv`` is preferred, because that is the one the installer fills.

Nothing here is Windows-specific and nothing here is required: an MCP client
configured with an absolute interpreter path, as ``install.ps1`` does, is still
the better arrangement and this simply agrees with it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Where a virtualenv keeps its interpreter, on each platform. Both are checked
#: rather than branching on ``sys.platform``: a repository shared over a network
#: share or a container mount can hold the other one.
VENV_PYTHONS = (
    ROOT / ".venv" / "Scripts" / "python.exe",
    ROOT / ".venv" / "bin" / "python",
)


def can_serve(python: Path | str) -> bool:
    """Whether *python* can import both the package and the SDK it needs.

    Both, not just the package: an editable install puts ``inventor_mcp`` on the
    path of an interpreter that may still be missing ``mcp``, and importing the
    package alone succeeds there -- ``inventor_mcp/__init__.py`` is four lines
    and imports nothing. That is the failure this script exists to avoid, so the
    check has to be the import that actually breaks.
    """
    try:
        done = subprocess.run(
            [str(python), "-c", "import mcp, inventor_mcp"],
            capture_output=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def candidates() -> list[tuple[str, bool]]:
    """Interpreters to try, best first, each flagged as the running one or not.

    The flag is carried rather than worked out later, and that is not
    fastidiousness. A virtualenv's ``python`` is a symlink to the interpreter it
    was made from, so ``Path(candidate).resolve()`` equals
    ``Path(sys.executable).resolve()`` for a venv that is emphatically *not* the
    interpreter now running -- and comparing the two that way sent the server
    down the in-process path on the very Python that could not import it, which
    is the failure this script was written to remove.
    """
    found: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for python, is_current in ((p, False) for p in VENV_PYTHONS):
        # A venv interpreter that is not there is not a candidate.
        if not python.is_file():
            continue
        if str(python) not in seen:
            seen.add(str(python))
            found.append((str(python), is_current))
    # Last, because it is the one already known to be whatever the client
    # happened to launch -- which is the thing being worked around.
    if sys.executable and sys.executable not in seen:
        found.append((sys.executable, True))
    return found


def main(argv: list[str]) -> int:
    for python, is_current in candidates():
        if not can_serve(python):
            continue
        if is_current:
            # Already the right interpreter: import and run in this process
            # rather than spawning a copy of ourselves to do it.
            from inventor_mcp.__main__ import main as run

            return run(argv)
        # subprocess rather than os.execv: exec on Windows is emulated, and the
        # emulation has the parent exit while the child inherits the console --
        # which is exactly the handle the stdio transport is speaking over.
        # A pass-through child costs one process and behaves the same on both.
        return subprocess.run([python, "-m", "inventor_mcp", *argv]).returncode

    _explain()
    return 2


def _explain() -> None:
    """Say which interpreters were tried, on the stream a person will read.

    Stderr, knowing an MCP client discards it, because the alternative is worse:
    stdout is the protocol, and writing prose there corrupts the stream instead
    of merely being ignored. The message is for whoever runs the command by hand
    after the client says the connection closed, and its last line is what turns
    that into one command.
    """
    print("inventor-mcp cannot start: no interpreter here can import it.",
          file=sys.stderr)
    print("", file=sys.stderr)
    for python, _ in candidates():
        print(f"  tried: {python}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"  repository: {ROOT}", file=sys.stderr)
    print("", file=sys.stderr)
    venv_python = (
        ".venv\\Scripts\\python.exe" if os.name == "nt" else ".venv/bin/python"
    )
    print("  Install it, from the top of the repository:", file=sys.stderr)
    print("", file=sys.stderr)
    print("      python -m venv .venv", file=sys.stderr)
    print(f'      {venv_python} -m pip install -e ".[inventor,dev]"', file=sys.stderr)
    print("", file=sys.stderr)
    if os.name == "nt":
        print("  scripts\\install.ps1 does that and registers the server by the",
              file=sys.stderr)
        print("  absolute path to the interpreter it just filled.", file=sys.stderr)
        print("", file=sys.stderr)
    print(f"  Then:  {venv_python} -m inventor_mcp --doctor", file=sys.stderr)


if __name__ == "__main__":
    # The repository has to be importable for the in-process path above: this
    # script is run by absolute path from a client's own working directory, so
    # nothing has put the project root on sys.path.
    sys.path.insert(0, str(ROOT))
    sys.exit(main(sys.argv[1:]))
