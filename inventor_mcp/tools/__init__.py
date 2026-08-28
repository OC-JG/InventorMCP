"""Tool registration."""

from __future__ import annotations

from typing import Any

from ..session import Session
from . import dfm, documents, escape, inspection, modeling

__all__ = ["register_all"]


def register_all(server: Any, session: Session) -> None:
    documents.register(server, session)
    modeling.register(server, session)
    inspection.register(server, session)
    dfm.register(server, session)
    # Registers nothing unless the machine's owner has turned it on, which is
    # the point: a tool the model cannot see cannot be talked into being used.
    escape.register(server, session)
