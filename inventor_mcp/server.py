"""Server assembly: tools, resources and prompts."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from . import __version__
from .compat import make_server
from .guide import MODELLING_NOTES, RECIPE_CHEATSHEET, SERVER_INSTRUCTIONS
from .schema import recipe_json_schema
from .session import Session
from .tools import register_all
from .units import describe_units

logger = logging.getLogger("inventor_mcp")


def create_server(backend: str = "auto") -> Any:
    """Build the MCP server with its tools, resources and prompts registered."""
    session = Session(backend_kind=backend)
    server = make_server(
        name="inventor-mcp",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
    )

    register_all(server, session)
    _register_resources(server)
    _register_prompts(server)
    return server


def _register_resources(server: Any) -> None:
    @server.resource("inventor://recipe/schema", mime_type="application/json")
    def recipe_schema() -> str:
        """JSON Schema for a part recipe."""
        return json.dumps(recipe_json_schema(), indent=2)

    @server.resource("inventor://recipe/guide", mime_type="text/markdown")
    def recipe_guide() -> str:
        """How to write a part recipe, with a worked example."""
        return f"# Part recipes\n\n```\n{RECIPE_CHEATSHEET}\n```\n\n## Notes\n\n{MODELLING_NOTES}"

    @server.resource("inventor://units", mime_type="application/json")
    def units() -> str:
        """Units accepted in recipe expressions, grouped by dimension."""
        return json.dumps(describe_units(), indent=2)


def _register_prompts(server: Any) -> None:
    @server.prompt()
    def model_this_part(description: str, units: str = "mm") -> str:
        """Turn a description of a part into a validated parametric model."""
        return (
            f"Model this part in Autodesk Inventor, working in {units}.\n\n"
            f"Part description:\n{description}\n\n"
            "Work in this order:\n"
            "1. Identify the driving dimensions and declare each as a named parameter. "
            "Anything the description states as a number is a candidate; anything derived "
            "from another dimension should be an expression, not a repeated number.\n"
            "2. Write a recipe and check it with `validate_recipe`.\n"
            "3. Build it with `build_part_from_recipe`.\n"
            "4. Confirm the result with `measure_part`, and `capture_view` if a picture helps.\n"
            "5. Report the parameter names and values so they can be revised afterwards.\n\n"
            "If the description leaves a dimension unstated, choose a sensible engineering "
            "value, make it a parameter, and say plainly which values you chose."
        )

    @server.prompt()
    def revise_part(change: str) -> str:
        """Change an existing model by editing its parameters rather than its geometry."""
        return (
            f"Apply this change to the part that is currently open: {change}\n\n"
            "Call `inspect_part` first. Prefer changing a parameter with `set_parameters` -- "
            "that is what the model is built for. Only add or edit geometry with "
            "`apply_operations` if no parameter expresses the change. "
            "Afterwards, report the before-and-after of anything that moved."
        )


def main(argv: list[str] | None = None) -> int:
    """Console entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="inventor-mcp",
        description="MCP server for text-to-parametric-model authoring in Autodesk Inventor.",
    )
    parser.add_argument(
        "--backend",
        default=os.environ.get("INVENTOR_MCP_BACKEND", "auto"),
        choices=["auto", "inventor", "mock"],
        help="Which backend to use. 'mock' simulates Inventor for offline work.",
    )
    parser.add_argument(
        "--transport",
        default=os.environ.get("INVENTOR_MCP_TRANSPORT", "stdio"),
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("INVENTOR_MCP_LOG_LEVEL", "INFO"),
        help="Python logging level.",
    )
    args = parser.parse_args(argv)

    # stderr only: stdout carries the MCP protocol on the stdio transport.
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    server = create_server(backend=args.backend)
    server.run(transport=args.transport)
    return 0
