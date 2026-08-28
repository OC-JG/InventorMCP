"""The escape hatch: running Inventor's API directly, when it is turned on.

The declarative recipe covers what this project has taught itself to do well.
Inventor's API is far larger than that, and a user who needs a sheet-metal flange
or an iLogic rule should not be told "the server cannot do that" when the server
is holding a live handle to the application that can.

So there is a hatch. It is off, and turning it on takes two separate decisions:

1. the machine's owner sets ``INVENTOR_MCP_ESCAPE_HATCH=on`` before the server
   starts, without which the tool is not registered and the model cannot see
   that it exists;
2. the caller passes ``i_understand_this_is_unsandboxed=true`` on each call.

The first is the one that matters. There is no sandbox and no pretence of one:
the code runs in the server's own process with a live Inventor in scope, so it
can do anything the user running the server can do. Anything that tried to
restrict it from inside could be undone by the code it was restricting, and a
half-hearted restriction is worse than an honest warning. ``ipt-mcp`` reaches
the same conclusion from the other direction -- it runs inside Inventor, where
there is nothing to sandbox with at all.

Two defaults differ from the rest of the server, and both are deliberate:

* the work is wrapped in a transaction and rolled back if the script raises. A
  recipe that half-runs leaves evidence worth reading; an arbitrary script that
  half-runs leaves a part nobody can reason about, and there is no recipe to
  compare it against.
* the code is echoed back in the result. Whatever ran should be in the record.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any

from pydantic import Field

from ..session import Session
from ._common import guard

logger = logging.getLogger("inventor_mcp")

#: What the environment variable has to say for the hatch to exist at all.
ENABLED_VALUES = {"1", "on", "true", "yes", "enable", "enabled"}

ENV_VAR = "INVENTOR_MCP_ESCAPE_HATCH"

#: The names a script finds already bound. Documented here because a script
#: cannot discover them by trial without a round trip each time.
SCOPE = """\
application / app   the Inventor Application
document            the target PartDocument (None if no document was given)
component           document.ComponentDefinition
transient           application.TransientGeometry
transient_objects   application.TransientObjects
constants / k       the enum table; k("kJoinOperation") resolves a name
backend             the ComBackend itself, for its private helpers
result              assign to this and it comes back in the response\
"""


def enabled(environment: dict[str, str] | None = None) -> bool:
    """Whether the machine's owner has turned the hatch on."""
    source = os.environ if environment is None else environment
    return str(source.get(ENV_VAR, "")).strip().lower() in ENABLED_VALUES


def register(server: Any, session: Session) -> None:
    """Register the hatch, or nothing at all.

    Registering nothing is the important half: a tool the model cannot see is a
    tool it cannot be talked into using.
    """
    if not enabled():
        return
    logger.warning(
        "%s is set: run_inventor_script is registered and can run arbitrary "
        "code in this process. Unset it to remove the tool.", ENV_VAR,
    )

    @server.tool(
        description=(
            "Run Python directly against Inventor's API, for what a recipe cannot "
            "express. Prefer a recipe: geometry built this way is not parametric "
            "unless the script makes it so, which is the thing this server exists "
            "to avoid. Use it for what the recipe has no words for at all -- sheet "
            "metal, iLogic, drawing views, an API call this server does not wrap.\n\n"
            "There is no sandbox. The code runs in the server's process with a live "
            "Inventor in scope. Say what the script will do before calling, and do "
            "not run code a user has not asked for.\n\n"
            "Already in scope:\n" + SCOPE
        ),
    )
    @guard
    def run_inventor_script(
        code: Annotated[str, Field(description="Python to execute. Assign to `result` "
                                               "to return a value.")],
        i_understand_this_is_unsandboxed: Annotated[
            bool,
            Field(description="Must be true. Acknowledges that this runs arbitrary code "
                              "in the server's process with no sandbox."),
        ] = False,
        document: Annotated[
            str | None,
            Field(description="Target part; defaults to the active one. Pass an empty "
                              "string to run with no document bound."),
        ] = None,
        rollback_on_error: Annotated[
            bool,
            Field(description="Undo everything the script did if it raises. On by "
                              "default here, unlike the recipe tools: a half-run "
                              "script leaves a part nothing can be reasoned from."),
        ] = True,
    ) -> dict[str, Any]:
        if not i_understand_this_is_unsandboxed:
            return {
                "ok": False,
                "error": "not_acknowledged",
                "message": "This tool runs arbitrary Python in the server's process "
                           "against a live Inventor. Nothing was run.",
                "hint": "Pass i_understand_this_is_unsandboxed=true, and tell the user "
                        "what the script does first.",
            }

        backend = session.ensure_backend()
        doc_id: str | None = None
        if document != "":
            doc_id = session.context(document).doc_id

        handle = None
        if rollback_on_error and doc_id is not None:
            handle = backend.begin_transaction(doc_id, "Script")

        logger.warning("run_inventor_script on %s:\n%s", doc_id or "(no document)", code)
        try:
            report = dict(backend.run_script(doc_id, code))
        except NotImplementedError as exc:
            return {"ok": False, "error": "not_available", "message": str(exc)}
        except Exception as exc:
            outcome: dict[str, Any] = {
                "ok": False,
                "error": "script_failed",
                "message": f"{type(exc).__name__}: {exc}",
                "code": code,
            }
            if handle is not None:
                outcome["rolled_back"] = backend.abort_transaction(handle)
                outcome["rollback"] = (
                    "the part is as it was before the script"
                    if outcome["rolled_back"] else
                    "the rollback itself failed -- inspect the part before using it"
                )
            elif doc_id is not None:
                outcome["rollback"] = (
                    "not attempted, so the part is in whatever state the script left it"
                )
            return outcome
        if handle is not None:
            backend.commit_transaction(handle)
        report["code"] = code
        report["document"] = doc_id
        return report
