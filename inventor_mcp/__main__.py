"""``python -m inventor_mcp``.

Deliberately thin, and deliberately not importing the server at module level.
``from .server import main`` at the top of this file is what turned a missing
dependency into a client reporting ``CONNECTION_CLOSED``: the import raised
before any code of ours ran, so the only account of the problem was a traceback
on the stderr an MCP client discards.

So the import happens inside ``main``, where it can be caught and explained, and
``--doctor`` is answered before it is attempted at all -- the doctor's whole job
is to run on the machine where the server will not.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # Read before the server is imported, not by the server's own argparse: on
    # an install missing `mcp` the parser is unreachable, and that is precisely
    # when somebody is asking for the doctor.
    if "--doctor" in args:
        from .preflight import doctor

        return doctor()

    try:
        from .server import main as serve
    except ImportError as exc:
        from .preflight import startup_failure

        startup_failure(exc)
        return 2

    return serve(args)


if __name__ == "__main__":
    sys.exit(main())
