#!/bin/bash
#
# Make a Claude Code on the web session able to run this project.
#
# A fresh container clones the repository and nothing more: no dependencies, no
# virtualenv. `pyproject` declares `mcp` and `pydantic`, and without them
# `inventor_mcp` does not import -- so `pytest` collects nothing and the
# `inventor` MCP server declared in `.mcp.json` exits before it can speak,
# which a client reports as `CONNECTION_CLOSED` with no reason attached. That is
# what this hook is for.
#
# What it cannot do is give the session Autodesk Inventor. Inventor is Windows
# software driven over COM; a Linux container has no seat and cannot reach one.
# Everything here runs against the simulator: recipes validate, the static
# checks run, the rehearsal runs, and no CAD file is ever written. Driving a real
# part needs the server running on the Windows machine that has Inventor --
# see `scripts/install.ps1`.
set -euo pipefail

# Local sessions have a real environment already, and a hook that rebuilds one
# under somebody's feet is a hook that breaks their setup.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$(dirname "$(dirname "$(readlink -f "$0")")")")}"
root="$(pwd)"

# A virtualenv rather than the container's own interpreter, for two reasons and
# both of them bite. `pip install -e .` into the system Python fails outright in
# this image: a distro-managed PyJWT is installed without a RECORD file, pip
# will not uninstall what it cannot inventory, and the install aborts. And
# `scripts/serve.py` looks for `.venv` first, so putting the dependencies there
# is also what lets `.mcp.json` start a working server -- one place to fix, not
# two.
if [ ! -x .venv/bin/python ]; then
  echo "-- creating .venv"
  python3 -m venv .venv
fi

echo "-- installing inventor_mcp (editable, dev extra)"
.venv/bin/python -m pip install --quiet --upgrade pip
# The `inventor` extra is deliberately absent: pywin32 is Windows-only, and
# `create_backend("auto")` falls back to the simulator without it.
.venv/bin/python -m pip install --quiet -e ".[dev]"

# So `python`, `pip` and `pytest` mean the virtualenv's for the rest of the
# session, rather than every command needing the .venv/bin prefix spelled out.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$root/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

# The DFM analyser is a separate private repository. A session without
# credentials for it cannot fetch it, and that is not a reason to fail setup:
# everything except the manufacturability tools works without it, and
# `--doctor` reports it as a warning naming the fix.
if [ ! -f dfm/src/rules/engine.js ]; then
  echo "-- fetching the dfm/ submodule (private; may not be reachable)"
  git submodule update --init --depth 1 dfm 2>/dev/null \
    || echo "   not fetched: the DFM tools will report the analyser missing"
fi

# The setup answering for itself, in the hook's own log, rather than the first
# failure being discovered by a tool call much later. Never fatal: on this
# platform the analyser and pywin32 are legitimately absent.
echo "-- checking the pieces line up"
.venv/bin/python -m inventor_mcp --doctor || true
