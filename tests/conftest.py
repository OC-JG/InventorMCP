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
