from __future__ import annotations

import pytest

from inventor_mcp.resolve import Resolver
from inventor_mcp.session import Session


@pytest.fixture
def resolver() -> Resolver:
    return Resolver("mm", "deg")


@pytest.fixture
def session() -> Session:
    session = Session(backend_kind="mock")
    session.ensure_backend().connect()
    return session


@pytest.fixture
def server():
    from inventor_mcp.server import create_server

    return create_server("mock")


PLATE_RECIPE = {
    "name": "MountingPlate",
    "units": "mm",
    "parameters": [
        {"name": "plate_w", "value": 120},
        {"name": "plate_d", "value": 80},
        {"name": "thk", "value": 8},
        {"name": "hole_d", "value": 6.6},
        {"name": "edge_margin", "value": 12},
        {"name": "corner_r", "value": 10},
    ],
    "operations": [
        {
            "op": "sketch",
            "name": "Body",
            "plane": "xy",
            "entities": [
                {"type": "rectangle", "center": [0, 0], "width": "plate_w", "height": "plate_d"}
            ],
        },
        {"op": "extrude", "name": "Plate", "sketch": "Body", "distance": "thk"},
        {"op": "fillet", "edges": {"filter": "vertical"}, "radius": "corner_r"},
        {
            "op": "sketch",
            "name": "Holes",
            "plane": "xy",
            "entities": [
                {
                    "type": "point_grid",
                    "center": [0, 0],
                    "columns": 2,
                    "rows": 2,
                    "x_spacing": "plate_w - 2 * edge_margin",
                    "y_spacing": "plate_d - 2 * edge_margin",
                }
            ],
        },
        {"op": "hole", "sketch": "Holes", "diameter": "hole_d", "through_all": True},
    ],
}


@pytest.fixture
def plate_recipe() -> dict:
    import copy

    return copy.deepcopy(PLATE_RECIPE)


#: Prefix on every skip that means "the DFM analyser could not be reached".
#:
#: CI's manufacturability job exists to catch the thresholds in
#: `inventor_mcp/dfm/remedy.py` drifting out of agreement with the analyser's
#: own rules. Every test that can do that skips when the analyser is absent, and
#: a fully skipped `pytest` run exits 0 -- so with a token present and the
#: analyser somehow unreachable, the job reported green while checking nothing.
#: A sentinel rather than a prose match, because the job has to key on something
#: that will not drift when the message is reworded.
DFM_UNAVAILABLE = "dfm-unavailable:"


def skip_without_analyser():
    """Find the DFM analyser, or skip with a reason CI can recognise."""
    import shutil

    import pytest

    from inventor_mcp.dfm.runner import DfmUnavailable, find_dfm_root

    if shutil.which("node") is None:
        pytest.skip(f"{DFM_UNAVAILABLE} no node, so the DFM analyser cannot run")
    try:
        return find_dfm_root()
    except DfmUnavailable as exc:
        pytest.skip(f"{DFM_UNAVAILABLE} {exc.message} {exc.hint or ''}")
