"""Sketch planes must place geometry on the axes their names promise."""

from __future__ import annotations

import pytest

from inventor_mcp.backend.mock.backend import map3d, plane_normal
from inventor_mcp.builder import build_part
from inventor_mcp.schema import PartRecipe


def disc_on(plane: str) -> PartRecipe:
    return PartRecipe.model_validate({
        "name": f"Disc_{plane}",
        "units": "mm",
        "operations": [
            {"op": "sketch", "name": "S", "plane": plane, "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 40, "height": 20}]},
            {"op": "extrude", "sketch": "S", "distance": 5},
        ],
    })


class TestPlaneMapping:
    @pytest.mark.parametrize("plane,normal", [
        ("xy", (0.0, 0.0, 1.0)),
        ("xz", (0.0, 1.0, 0.0)),
        ("yz", (1.0, 0.0, 0.0)),
    ])
    def test_normals(self, plane, normal):
        assert plane_normal(plane) == normal

    def test_the_offset_axis_is_always_the_normal(self):
        """A regression guard: the yz mapping once put the offset on Y."""
        for plane in ("xy", "xz", "yz"):
            normal = plane_normal(plane)
            offset_point = map3d(plane, 0.0, 0.0, 3.0)
            for axis in range(3):
                if normal[axis]:
                    assert offset_point[axis] == pytest.approx(3.0)
                else:
                    assert offset_point[axis] == pytest.approx(0.0)

    def test_sketch_axes_are_distinct_from_the_normal(self):
        def axis_of(point):
            return next(i for i, value in enumerate(point) if value)

        for plane in ("xy", "xz", "yz"):
            u = map3d(plane, 1.0, 0.0, 0.0)
            v = map3d(plane, 0.0, 1.0, 0.0)
            w = map3d(plane, 0.0, 0.0, 1.0)
            assert sorted([axis_of(u), axis_of(v), axis_of(w)]) == [0, 1, 2]

    def test_the_xz_plane_runs_its_horizontal_axis_along_minus_x(self):
        """Measured against Inventor 2027.1, not assumed.

        An L-profile drawn from 0 to 90 in sketch X comes out spanning -90 to 0
        in model X, which is why a `near` point picked on the +X side of an XZ
        sketch selects the wrong edge.
        """
        assert map3d("xz", 1.0, 0.0, 0.0) == (-1.0, 0.0, 0.0)
        assert map3d("xz", 0.0, 1.0, 0.0) == (0.0, 0.0, 1.0)

    def test_the_other_planes_keep_their_sign(self):
        assert map3d("xy", 1.0, 0.0, 0.0) == (1.0, 0.0, 0.0)
        assert map3d("yz", 1.0, 0.0, 0.0) == (0.0, 1.0, 0.0)
        assert map3d("yz", 0.0, 1.0, 0.0) == (0.0, 0.0, 1.0)


class TestExtrusionDirection:
    @pytest.mark.parametrize("plane,thickness_axis", [("xy", 2), ("xz", 1), ("yz", 0)])
    def test_the_part_is_thin_along_the_plane_normal(self, session, plane, thickness_axis):
        build_part(session, disc_on(plane))
        box = session.backend.mass_properties(session.active).bounding_box
        spans = [box[axis + 3] - box[axis] for axis in range(3)]
        assert spans[thickness_axis] == pytest.approx(0.5)
        assert sorted(spans) == pytest.approx([0.5, 2.0, 4.0])

    def test_a_through_hole_only_needs_to_cross_the_thickness(self, session):
        """Through-all depth follows the plane normal, not the longest span."""
        from inventor_mcp.builder import apply_operation
        from inventor_mcp.schema import Operation
        from pydantic import TypeAdapter

        build_part(session, disc_on("xy"))
        context = session.context()
        before = session.backend.mass_properties(context.doc_id).volume
        ops = TypeAdapter(list[Operation]).validate_python([
            {"op": "sketch", "name": "P", "plane": "xy", "entities": [
                {"type": "point", "position": [0, 0]}]},
            {"op": "hole", "sketch": "P", "diameter": 10, "through_all": True},
        ])
        for op in ops:
            apply_operation(session, context, op)
        after = session.backend.mass_properties(context.doc_id).volume
        import math
        assert before - after == pytest.approx(math.pi * 0.5**2 * 0.5)
