"""Sketch planes must place geometry on the axes their names promise."""

from __future__ import annotations

import math

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

    def test_every_plane_keeps_the_sign_of_the_axes_it_is_named_after(self):
        """What a recipe's coordinates mean, on every plane alike.

        "x from 0 to 90" on `xz` means model +X, the same as it does on `xy`.
        Inventor's own XZ plane runs its first axis along -X, but that is an
        implementation detail of one backend, compensated for in
        `SketchPlan.mirrored_u`; it is not something a recipe author should
        have to know, and it must not show up here.
        """
        assert map3d("xy", 1.0, 0.0, 0.0) == (1.0, 0.0, 0.0)
        assert map3d("xz", 1.0, 0.0, 0.0) == (1.0, 0.0, 0.0)
        assert map3d("xz", 0.0, 1.0, 0.0) == (0.0, 0.0, 1.0)
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


class TestMirroringForInventorsXzPlane:
    """`SketchPlan.mirrored_u` is what keeps the promise the tests above make.

    Inventor's XZ sketch plane runs its first axis along model -X, measured on
    2027.1: a profile drawn from 0 to 90 in sketch X came out spanning -90 to 0.
    The COM backend mirrors the plan's first axis on the way in so the geometry
    lands where the recipe asked for it.  Mirroring is a reflection, so lengths,
    radii and the angles between lines are unchanged -- only positions move.
    """

    def plan(self):
        from inventor_mcp.plan import (
            PArc, PCircle, PEllipse, PLine, PPoint, Ref, SketchPlan,
        )

        plan = SketchPlan(plane="xz")
        plan.add(PLine(id="l1", start=(0.0, 0.0), end=(9.0, 0.0)))
        plan.add(PArc(id="a1", center=(2.0, 3.0), radius=1.0,
                      start_angle=0.0, end_angle=math.pi / 2))
        plan.add(PCircle(id="c1", center=(4.0, 1.0), radius=0.5))
        plan.add(PEllipse(id="e1", center=(-1.0, 2.0), major_radius=2.0,
                          minor_radius=1.0, rotation=math.pi / 6))
        plan.add(PPoint(id="p1", position=(6.5, 2.0)))
        plan.constrain("horizontal", Ref("l1"))
        plan.dimension("horizontal", [Ref("l1")], "width", 9.0,
                       name="width", text_offset=(1.0, 2.0))
        return plan

    def endpoints(self, arc):
        return [
            (arc.center[0] + arc.radius * math.cos(angle),
             arc.center[1] + arc.radius * math.sin(angle))
            for angle in (arc.start_angle, arc.end_angle)
        ]

    def test_a_profile_drawn_to_plus_ninety_is_handed_over_as_minus_ninety(self):
        """Which is what Inventor's own axis then flips back to +90."""
        line = self.plan().mirrored_u().by_id("l1")
        assert line.start == (0.0, 0.0)
        assert line.end == (-9.0, 0.0)

    def test_the_second_axis_is_untouched(self):
        original, mirrored = self.plan(), self.plan().mirrored_u()
        for primitive in original.primitives:
            image = mirrored.by_id(primitive.id)
            for attribute in ("start", "end", "center", "position"):
                if hasattr(primitive, attribute):
                    assert getattr(image, attribute)[1] == getattr(primitive, attribute)[1]

    def test_an_arcs_endpoints_are_the_mirror_images_of_the_originals(self):
        original = self.plan().by_id("a1")
        mirrored = self.plan().mirrored_u().by_id("a1")
        start, end = self.endpoints(original)
        # The sweep reverses under reflection, so the endpoints swap over: what
        # was the start is now where the arc finishes.
        assert self.endpoints(mirrored)[1] == pytest.approx((-start[0], start[1]))
        assert self.endpoints(mirrored)[0] == pytest.approx((-end[0], end[1]))

    def test_an_arc_still_sweeps_the_same_amount(self):
        def sweep(arc):
            return (arc.end_angle - arc.start_angle) % (2 * math.pi)

        assert sweep(self.plan().mirrored_u().by_id("a1")) == pytest.approx(
            sweep(self.plan().by_id("a1")))

    def test_an_ellipse_keeps_its_axes_and_reflects_its_rotation(self):
        original = self.plan().by_id("e1")
        mirrored = self.plan().mirrored_u().by_id("e1")
        assert mirrored.center == (1.0, 2.0)
        assert mirrored.major_radius == original.major_radius
        assert mirrored.minor_radius == original.minor_radius
        assert mirrored.rotation == pytest.approx(math.pi - original.rotation)

    def test_sizes_survive_untouched(self):
        mirrored = self.plan().mirrored_u()
        assert mirrored.by_id("c1").radius == 0.5
        assert mirrored.by_id("a1").radius == 1.0
        assert abs(mirrored.by_id("l1").length) == pytest.approx(9.0)

    def test_the_dimensions_still_drive_the_model(self):
        """Reflecting must not disturb the expressions -- that is the point."""
        [dimension] = self.plan().mirrored_u().dimensions
        assert (dimension.kind, dimension.expression, dimension.name) == (
            "horizontal", "width", "width")
        assert dimension.value == 9.0
        assert dimension.text_offset == (-1.0, 2.0)

    def test_constraints_are_carried_over_unchanged(self):
        original, mirrored = self.plan(), self.plan().mirrored_u()
        assert mirrored.constraints == original.constraints

    def test_the_original_plan_is_left_alone(self):
        plan = self.plan()
        plan.mirrored_u()
        assert plan.by_id("l1").end == (9.0, 0.0)

    def test_mirroring_twice_is_the_identity(self):
        there_and_back = self.plan().mirrored_u().mirrored_u()
        assert there_and_back.by_id("l1").end == pytest.approx((9.0, 0.0))
        assert self.endpoints(there_and_back.by_id("a1")) == pytest.approx(
            self.endpoints(self.plan().by_id("a1")))

    def test_only_inventors_xz_plane_needs_it(self):
        from inventor_mcp.backend.com import backend as com

        assert com._MIRRORED_PLANES == {"xz"}
