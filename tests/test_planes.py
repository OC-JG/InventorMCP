"""Sketch planes must place geometry on the axes their names promise."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from inventor_mcp.backend.mock.backend import map3d, plane_normal
from inventor_mcp.builder import build_part
from inventor_mcp.schema import PartRecipe

_ROOT = Path(__file__).resolve().parent.parent


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


class TestMirroringKeepsCoincidencesCoincident:
    """The invariant the arc-endpoint bug broke, stated once for all sketches.

    A coincident constraint asserts two points are one point. If mirroring
    moves them apart, the sketch is wrong -- and wrong *silently*: the COM
    backend sees both references in one shared-point group and skips the
    constraint, so Inventor is never given the chance to refuse it.

    Reflecting reverses an arc's sweep, so its start and end swap places and
    a reference to the old start has to be remapped to the new end. Lines need
    no remapping: the endpoint simply moves to its own mirror image.
    """

    def point(self, plan, ref):
        from inventor_mcp.plan import PArc, PCircle, PLine, PointRef, PPoint

        primitive = plan.by_id(ref.entity)
        if isinstance(primitive, PLine):
            if ref.point is PointRef.START:
                return primitive.start
            if ref.point is PointRef.END:
                return primitive.end
            if ref.point is PointRef.MID:
                return tuple((a + b) / 2 for a, b in zip(primitive.start, primitive.end))
            return None
        if isinstance(primitive, PArc):
            if ref.point is PointRef.CENTER:
                return primitive.center
            angle = {PointRef.START: primitive.start_angle,
                     PointRef.END: primitive.end_angle}.get(ref.point)
            if angle is None:
                return None
            return (primitive.center[0] + primitive.radius * math.cos(angle),
                    primitive.center[1] + primitive.radius * math.sin(angle))
        if isinstance(primitive, PPoint):
            return primitive.position
        if isinstance(primitive, PCircle):
            return primitive.center
        return None

    def widest_gap(self, plan):
        """How far apart the two ends of any coincidence are, in cm."""
        from inventor_mcp.plan import ORIGIN

        worst = 0.0
        checked = 0
        for constraint in plan.constraints:
            if constraint.kind != "coincident" or len(constraint.refs) != 2:
                continue
            if any(ref.entity == ORIGIN.entity for ref in constraint.refs):
                continue
            first, second = (self.point(plan, ref) for ref in constraint.refs)
            if first is None or second is None:
                continue
            checked += 1
            worst = max(worst, math.dist(first, second))
        assert checked, "the fixture has no coincidences to check"
        return worst

    def plan_for(self, entity, plane="xy"):
        from inventor_mcp.builder import build_part
        from inventor_mcp.schema import PartRecipe
        from inventor_mcp.session import Session

        session = Session(backend_kind="mock")
        session.ensure_backend().connect()
        build_part(session, PartRecipe.model_validate({
            "name": "Fixture", "units": "mm",
            "operations": [{"op": "sketch", "name": "S", "plane": plane,
                            "entities": [entity]}],
        }))
        return session.backend._doc(session.active).sketches[0].plan

    ENTITIES = [
        {"type": "slot", "center": [30, 0], "length": 20, "width": 8, "angle": 0},
        {"type": "slot", "center": [30, 10], "length": 24, "width": 6, "angle": 30},
        {"type": "rectangle", "center": [20, 0], "width": 40, "height": 25},
        {"type": "polyline", "points": [[0, 0], [40, 0], [40, 6], [0, 6]], "closed": True},
    ]

    @pytest.mark.parametrize("entity", ENTITIES, ids=lambda e: e["type"])
    def test_coincidences_start_out_coincident(self, entity):
        assert self.widest_gap(self.plan_for(entity)) == pytest.approx(0.0, abs=1e-9)

    @pytest.mark.parametrize("entity", ENTITIES, ids=lambda e: e["type"])
    def test_and_stay_coincident_through_the_mirror(self, entity):
        """8 mm apart before the fix, on any entity with an arc in it."""
        mirrored = self.plan_for(entity).mirrored_u()
        assert self.widest_gap(mirrored) == pytest.approx(0.0, abs=1e-9)

    def test_an_arcs_endpoint_references_are_swapped_over(self):
        from inventor_mcp.plan import PArc, PointRef

        plan = self.plan_for(self.ENTITIES[0])
        arcs = {p.id for p in plan.primitives if isinstance(p, PArc)}
        before = [(str(r), r.point) for c in plan.constraints for r in c.refs
                  if r.entity in arcs and r.point in (PointRef.START, PointRef.END)]
        after = [(str(r), r.point) for c in plan.mirrored_u().constraints for r in c.refs
                 if r.entity in arcs and r.point in (PointRef.START, PointRef.END)]
        assert before, "the fixture should reference arc endpoints"
        assert [p for _, p in after] == [
            PointRef.END if p is PointRef.START else PointRef.START for _, p in before]

    def test_a_lines_endpoint_references_are_left_alone(self):
        """The point moves to its own mirror image; start is still start."""
        from inventor_mcp.plan import PLine, PointRef

        plan = self.plan_for(self.ENTITIES[2])  # a plain rectangle: lines only
        lines = {p.id for p in plan.primitives if isinstance(p, PLine)}
        pick = lambda pl: [(r.entity, r.point) for c in pl.constraints for r in c.refs
                           if r.entity in lines and r.point is not PointRef.SELF]
        assert pick(plan.mirrored_u()) == pick(plan)


class TestMeasuredOrientation:
    """The backend measures where a sketch's axes point; it does not guess.

    Guessing cost two rounds. The XZ plane runs its first axis along model -X,
    which put an L-profile at x -90..0; the YZ plane orders its axes some other
    way again, which put the angle bracket's upright holes off the part entirely
    and drilled air in both directions. Neither is derivable from the plane's
    name, and both are silent. So the sketch is created, asked where its axes
    are, and the geometry transformed to suit before anything is drawn.
    """

    def matrix(self, along_u, along_v):
        from inventor_mcp.backend.com import backend as com

        return com._orientation_matrix((along_u, along_v))

    X, Y, Z = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)

    def negated(self, axis):
        return tuple(-c for c in axis)

    def test_the_convention_matches_the_simulators(self):
        """Both backends have to agree on what a recipe's coordinates mean."""
        from inventor_mcp.backend.com.backend import _RECIPE_AXES

        for plane, facing in (("xy", 2), ("xz", 1), ("yz", 0)):
            intended_u, intended_v = _RECIPE_AXES[facing]
            assert map3d(plane, 1.0, 0.0, 0.0) == pytest.approx(intended_u)
            assert map3d(plane, 0.0, 1.0, 0.0) == pytest.approx(intended_v)
            assert plane_normal(plane)[facing] == pytest.approx(1.0)

    def test_a_plane_already_pointing_the_right_way_needs_no_transform(self):
        assert self.matrix(self.X, self.Y) == (1.0, 0.0, 0.0, 1.0)
        assert self.matrix(self.X, self.Z) == (1.0, 0.0, 0.0, 1.0)
        assert self.matrix(self.Y, self.Z) == (1.0, 0.0, 0.0, 1.0)

    def test_inventors_xz_plane_comes_out_as_the_mirror_we_already_shipped(self):
        """u along -X, v along +Z: measured on 2027.1."""
        assert self.matrix(self.negated(self.X), self.Z) == (-1.0, 0.0, 0.0, 1.0)

    def test_a_plane_with_its_axes_swapped_is_handled_too(self):
        """Which is the shape the YZ failure has, if the order is the problem."""
        matrix = self.matrix(self.Z, self.Y)
        assert matrix == (0.0, 1.0, 1.0, 0.0)
        assert matrix[0] * matrix[3] - matrix[1] * matrix[2] < 0, "a swap reflects"

    @pytest.mark.parametrize("along_u,along_v,plane", [
        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), "xy"),
        ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), "xy"),
        ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), "xy"),
        ((-1.0, 0.0, 0.0), (0.0, -1.0, 0.0), "xy"),
        ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0), "xy"),
        ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), "xy"),
        ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0), "xz"),
        ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), "xz"),
        ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0), "yz"),
        ((0.0, 0.0, 1.0), (0.0, -1.0, 0.0), "yz"),
        ((0.0, -1.0, 0.0), (0.0, 0.0, -1.0), "yz"),
    ])
    def test_the_recipes_point_lands_where_the_recipe_asked(self, along_u, along_v, plane):
        """The property that matters, for every orientation a plane could have.

        Put the recipe's point through the transform, then place it using the
        sketch's *own* axes -- which is what Inventor will do with it. It has to
        arrive at the model position the recipe named, which is what `map3d`
        says. That is the whole contract, and it held for none of these before.
        """
        matrix = self.matrix(along_u, along_v)
        assert matrix is not None, f"{along_u} / {along_v} should be reconcilable"
        a, b, c, d = matrix

        recipe_u, recipe_v = 9.0, 4.0
        sketch_u = a * recipe_u + b * recipe_v
        sketch_v = c * recipe_u + d * recipe_v
        landed = tuple(sketch_u * along_u[axis] + sketch_v * along_v[axis]
                       for axis in range(3))

        assert landed == pytest.approx(map3d(plane, recipe_u, recipe_v, 0.0))

    def test_without_the_transform_the_point_lands_somewhere_else(self):
        """A guard on the test above: it would pass trivially on an identity."""
        along_u, along_v = (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)  # Inventor's XZ
        untransformed = tuple(9.0 * along_u[axis] + 4.0 * along_v[axis] for axis in range(3))
        assert untransformed != pytest.approx(map3d("xz", 9.0, 4.0, 0.0))

    def test_a_plane_at_an_angle_is_left_alone(self):
        """No agreed meaning for its axes, so the coordinates pass through."""
        tilted = (0.7071, 0.7071, 0.0)
        assert self.matrix(tilted, self.Z) is None

    def test_unmeasurable_axes_are_left_alone(self):
        from inventor_mcp.backend.com import backend as com

        assert com._orientation_matrix(None) is None


class TestReorientingCarriesDimensions:
    """A transform has to move what a dimension *means*, not just where it is.

    Under a swap the two sketch axes trade places, so a dimension still called
    "horizontal" would measure the other one: the right number on the wrong
    axis, which is the quietest kind of wrong there is. The words have to swap
    with the axes they name. No plane has needed a swap yet, but
    `_orientation_matrix` can return one, and a polyline now emits horizontal
    and vertical dimensions by the handful.
    """

    def plan(self):
        from inventor_mcp.plan import PLine, Ref, SketchPlan

        plan = SketchPlan(plane="xz")
        plan.add(PLine(id="l1", start=(0.0, 0.0), end=(9.0, 0.0)))
        plan.add(PLine(id="l2", start=(9.0, 0.0), end=(9.0, 4.0)))
        plan.constrain("horizontal", Ref("l1"))
        plan.constrain("vertical", Ref("l2"))
        plan.constrain("horizontal_align", Ref("l1"), Ref("l2"))
        plan.dimension("horizontal", [Ref("l1")], "base_len", 9.0, optional=True)
        plan.dimension("vertical", [Ref("l2")], "height", 4.0)
        return plan

    SWAP = (0.0, 1.0, 1.0, 0.0)
    MIRROR = (-1.0, 0.0, 0.0, 1.0)

    def test_a_swap_renames_the_axes_a_dimension_measures(self):
        swapped = self.plan().reoriented(self.SWAP)
        assert [d.kind for d in swapped.dimensions] == ["vertical", "horizontal"]

    def test_a_swap_renames_the_constraints_too(self):
        swapped = self.plan().reoriented(self.SWAP)
        assert [c.kind for c in swapped.constraints] == [
            "vertical", "horizontal", "vertical_align"]

    def test_a_mirror_leaves_the_names_alone(self):
        """Reflection reverses an axis; it does not exchange the two."""
        mirrored = self.plan().reoriented(self.MIRROR)
        assert [d.kind for d in mirrored.dimensions] == ["horizontal", "vertical"]
        assert [c.kind for c in mirrored.constraints] == [
            "horizontal", "vertical", "horizontal_align"]

    def test_the_expressions_and_values_are_untouched(self):
        for matrix in (self.SWAP, self.MIRROR):
            moved = self.plan().reoriented(matrix)
            assert [(d.expression, d.value) for d in moved.dimensions] == [
                ("base_len", 9.0), ("height", 4.0)]

    def test_the_optional_flag_survives(self):
        """It is constructed positionally, so it is easy to drop by accident."""
        for matrix in (self.SWAP, self.MIRROR):
            moved = self.plan().reoriented(matrix)
            assert [d.optional for d in moved.dimensions] == [True, False]

    def test_the_bracket_keeps_measuring_the_axes_it_meant_to(self):
        """Its section really is on xz, which really is mirrored on 2027.1."""
        import json

        from inventor_mcp.builder import build_part
        from inventor_mcp.schema import PartRecipe
        from inventor_mcp.session import Session

        session = Session(backend_kind="mock")
        session.ensure_backend().connect()
        recipe = PartRecipe.model_validate(
            json.loads((_ROOT / "examples" / "angle_bracket.json").read_text()))
        build_part(session, recipe)
        plan = session.backend._doc(session.active).sketches[0].plan
        assert plan.plane == "xz"
        before = [(d.kind, d.expression) for d in plan.dimensions]
        assert [(d.kind, d.expression) for d in plan.mirrored_u().dimensions] == before


class TestSimulatorFidelity:
    """Where the simulator was not merely approximate but wrong.

    It is what 546 tests run against and what `validate_recipe` rehearses in,
    so an answer that is wrong rather than rough is worth more than it looks.
    Each of these was found by building a part that exercises it and checking
    the number by hand.
    """

    def build(self, recipe):
        from inventor_mcp.builder import build_part
        from inventor_mcp.schema import PartRecipe
        from inventor_mcp.session import Session

        session = Session(backend_kind="mock")
        session.ensure_backend().connect()
        build_part(session, PartRecipe.model_validate(recipe))
        return session.backend.mass_properties(session.active)

    def test_a_sketch_on_a_work_plane_sits_at_the_planes_offset(self):
        """The flanged shaft was built from z=0 and came out 12 mm short."""
        properties = self.build({
            "name": "OnTop", "units": "mm",
            "operations": [
                {"op": "sketch", "name": "Base", "plane": "xy", "entities": [
                    {"type": "circle", "diameter": 40}]},
                {"op": "extrude", "sketch": "Base", "distance": 12},
                {"op": "work_plane", "name": "Upper", "kind": "offset",
                 "base": "xy", "offset": 12},
                {"op": "sketch", "name": "Boss", "plane": "Upper", "entities": [
                    {"type": "circle", "diameter": 20}]},
                {"op": "extrude", "sketch": "Boss", "distance": 30},
            ]})
        box = properties.bounding_box
        assert (box[5] - box[2]) * 10 == pytest.approx(42.0), "12 + 30, not 30"

    def test_a_swept_arc_has_a_length(self):
        """Only straight segments counted, so an elbow had no volume at all."""
        properties = self.build({
            "name": "Elbow", "units": "mm",
            "operations": [
                {"op": "sketch", "name": "Path", "plane": "xy", "entities": [
                    {"type": "arc", "center": [0, 0], "radius": 45,
                     "start_angle": 0, "end_angle": 90}]},
                {"op": "sketch", "name": "Profile", "plane": "yz", "entities": [
                    {"type": "circle", "center": [45, 0], "diameter": 20}]},
                {"op": "sweep", "profile_sketch": "Profile", "path_sketch": "Path"},
            ]})
        # Pappus: the section's area times the distance its centroid travels.
        wanted = math.pi * 10 ** 2 * (45 * math.pi / 2) / 1000
        assert properties.volume == pytest.approx(wanted, rel=1e-9)

    def test_a_loft_has_a_volume_and_not_an_area(self):
        """The mean area was being added as if it were a volume."""
        properties = self.build({
            "name": "Duct", "units": "mm",
            "operations": [
                {"op": "sketch", "name": "Lower", "plane": "xy", "entities": [
                    {"type": "circle", "diameter": 60}]},
                {"op": "work_plane", "name": "Top", "kind": "offset",
                 "base": "xy", "offset": 70},
                {"op": "sketch", "name": "Upper", "plane": "Top", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": 50, "height": 50}]},
                {"op": "loft", "sketches": ["Lower", "Upper"]},
            ]})
        mean_area = (math.pi * 30 ** 2 + 50 * 50) / 2
        assert properties.volume == pytest.approx(mean_area * 70 / 1000, rel=1e-9)

    def test_a_revolved_ring_is_not_a_ball(self):
        """The bounds were expanded to a cube of the profile's reach."""
        properties = self.build({
            "name": "Ring", "units": "mm",
            "operations": [
                {"op": "sketch", "name": "Section", "plane": "xz", "entities": [
                    {"type": "polyline", "closed": True, "points": [
                        [6, 0], [40, 0], [40, 16], [6, 16]]}]},
                {"op": "revolve", "sketch": "Section", "axis": "z"},
            ]})
        box = properties.bounding_box
        spans = [round((box[axis + 3] - box[axis]) * 10, 3) for axis in range(3)]
        assert spans == [80.0, 80.0, 16.0]
        assert properties.volume == pytest.approx(
            math.pi * (40 ** 2 - 6 ** 2) * 16 / 1000, rel=1e-9)
