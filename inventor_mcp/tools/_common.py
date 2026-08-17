"""Shared plumbing for the tool layer."""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, TypeVar

from pydantic import ValidationError

from ..errors import InventorMCPError
from ..units import Dim, Quantity, from_internal

logger = logging.getLogger("inventor_mcp")

F = TypeVar("F", bound=Callable[..., dict])


def guard(func: F) -> F:
    """Turn exceptions into structured results the caller can act on.

    A model that gets ``{"ok": false, "error": "...", "hint": "..."}`` can fix
    its own input on the next call; an opaque stack trace cannot be fixed at
    all.  Unexpected exceptions are still logged in full server-side.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result = func(*args, **kwargs)
        except InventorMCPError as exc:
            logger.info("%s: %s", func.__name__, exc.message)
            return exc.to_dict()
        except ValidationError as exc:
            return {
                "ok": False,
                "error": "invalid_input",
                "message": f"{func.__name__} received input that does not match the schema.",
                "issues": _format_validation(exc),
                "hint": "Read `part_recipe_schema` for the exact field names and types.",
            }
        except Exception as exc:  # pragma: no cover - genuine bugs and COM surprises
            logger.exception("%s failed", func.__name__)
            return {
                "ok": False,
                "error": "unexpected_error",
                "message": f"{type(exc).__name__}: {exc}",
            }
        if isinstance(result, dict):
            result.setdefault("ok", True)
        return result

    return wrapper  # type: ignore[return-value]


def _format_validation(exc: ValidationError) -> list[dict[str, Any]]:
    issues = []
    for error in exc.errors()[:20]:
        location = ".".join(str(part) for part in error["loc"])
        issues.append({"field": location or "(root)", "problem": error["msg"], "type": error["type"]})
    return issues


def display_length(value_cm: float, unit: str, decimals: int = 4) -> float:
    return round(from_internal(value_cm, unit), decimals)


def display_point(point: tuple[float, float, float] | None, unit: str) -> list[float] | None:
    if point is None:
        return None
    return [display_length(component, unit) for component in point]


def display_box(box: tuple[float, ...] | None, unit: str) -> dict[str, Any] | None:
    if not box:
        return None
    low = [display_length(component, unit) for component in box[:3]]
    high = [display_length(component, unit) for component in box[3:]]
    return {
        "min": low,
        "max": high,
        "size": [round(h - l, 4) for l, h in zip(low, high)],
        "units": unit,
    }


def quantity_dict(value: float, dim: Dim, unit: str) -> dict[str, Any]:
    return {"value": round(Quantity(value, dim).as_display(unit), 6), "units": unit}
