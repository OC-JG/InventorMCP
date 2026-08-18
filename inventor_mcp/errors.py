"""Error types shared by every layer of the server.

Every error carries a short machine-readable ``code`` and, where possible, a
``hint`` describing the repair.  Tool wrappers turn these into structured
responses so a language model can correct itself without guessing.
"""

from __future__ import annotations

from typing import Any


class InventorMCPError(Exception):
    """Base class for all errors raised by the server."""

    code = "error"

    def __init__(self, message: str, *, hint: str | None = None, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
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
