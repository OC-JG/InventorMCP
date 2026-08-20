"""Error types shared by every layer of the server.

Every error carries a short machine-readable ``code`` and, where possible, a
``hint`` describing the repair.  Tool wrappers turn these into structured
responses so a language model can correct itself without guessing.
"""

from __future__ import annotations

import re as _re
from typing import Any


#: A Windows path, a UNC share, or a POSIX home directory in running text.
#:
#: Deliberately stops at whitespace. Windows paths do contain spaces, so
#: "C:\\Users\\Jo\\My Parts\\x.ipt" is redacted only as far as "My" -- but the
#: alternative, letting a component span spaces, bridges across ordinary
#: sentences: "C:\\a\\b.ipt and \\\\server\\share\\c.step" then matches as one
#: path and the words between them are deleted. Leaking a directory name is a
#: smaller harm than silently eating the explanation of what went wrong.
_PATHS = _re.compile(
    r"""(?xi)
    (?: [A-Z]:[\\/] | \\\\[^\\/\s]+[\\/] | /(?:home|Users)/ )
    [^\s'"()<>]*
    """
)


def sanitise(text: str) -> str:
    """Replace filesystem paths in *text* with just their last component.

    An error message travels to whatever is driving the server, and a COM
    failure or a traceback readily carries an absolute path -- a user name, a
    project directory, a network share. The filename is the part that helps;
    the route to it is nobody else's business.
    """
    def keep_the_leaf(match: "_re.Match[str]") -> str:
        leaf = _re.split(r"[\\/]", match.group(0).rstrip("\\/"))[-1]
        return f"...{'/' if leaf else ''}{leaf}" if leaf else "..."

    return _PATHS.sub(keep_the_leaf, text)


class InventorMCPError(Exception):
    """Base class for all errors raised by the server."""

    code = "error"

    def __init__(self, message: str, *, hint: str | None = None, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        # Sanitised on the way out rather than at the raise site, so no future
        # error can forget to do it.
        payload: dict[str, Any] = {
            "ok": False, "error": self.code, "message": sanitise(self.message),
        }
        if self.hint:
            payload["hint"] = sanitise(self.hint)
        if self.details:
            payload["details"] = self.details
        return payload


class NotConnectedError(InventorMCPError):
    code = "not_connected"

    def __init__(self, message: str = "Not connected to Autodesk Inventor.") -> None:
        super().__init__(message, hint="Call `connect` first.")


class ConnectionFailedError(InventorMCPError):
    code = "connection_failed"


class BackendUnavailableError(InventorMCPError):
    code = "backend_unavailable"


class DocumentError(InventorMCPError):
    code = "document_error"


class UnknownHandleError(InventorMCPError):
    code = "unknown_handle"


class ParameterError(InventorMCPError):
    code = "parameter_error"


class ExpressionError(InventorMCPError):
    code = "expression_error"


class UnitError(InventorMCPError):
    code = "unit_error"


class SketchError(InventorMCPError):
    code = "sketch_error"


class FeatureError(InventorMCPError):
    code = "feature_error"


class SelectionError(InventorMCPError):
    code = "selection_error"


class ExportError(InventorMCPError):
    code = "export_error"


class RecipeError(InventorMCPError):
    code = "recipe_error"
