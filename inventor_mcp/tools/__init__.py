"""Tool registration."""

from __future__ import annotations

from typing import Any

from ..session import Session
from . import documents, inspection, modeling

__all__ = ["register_all"]


def register_all(server: Any, session: Session) -> None:
    documents.register(server, session)
    modeling.register(server, session)
    inspection.register(server, session)
