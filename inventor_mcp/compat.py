"""Compatibility shim across MCP Python SDK versions.

The SDK renamed ``FastMCP`` to ``MCPServer`` in 2.0 while keeping the decorator
API identical.  Importing through here means the server runs on either.
"""

from __future__ import annotations

from typing import Any

try:  # MCP SDK >= 2.0
    from mcp.server.mcpserver import MCPServer as ServerClass
except ImportError:  # pragma: no cover - SDK 1.x
    from mcp.server.fastmcp import FastMCP as ServerClass  # type: ignore[assignment]

__all__ = ["ServerClass", "make_server"]


def make_server(name: str, instructions: str, version: str) -> Any:
    """Construct the SDK server object, tolerating older keyword sets."""
    try:
        return ServerClass(name=name, instructions=instructions, version=version)
    except TypeError:  # pragma: no cover - very old SDKs
        return ServerClass(name=name, instructions=instructions)
