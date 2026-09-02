"""Selectors, edge/face treatments and the incremental editing path."""

from __future__ import annotations

import math

import pytest

from inventor_mcp.builder import apply_operation, build_part, resolve_selector
from inventor_mcp.errors import SelectionError
from inventor_mcp.schema import PartRecipe, Selector
from pydantic import TypeAdapter

from inventor_mcp.schema import Operation

_OPS = TypeAdapter(list[Operation])

BLOCK = {
    "name": "Block",
    "units": "mm",
    "parameters": [{"name": "size", "value": 40}, {"name": "height", "value": 20}],
    "operations": [
        {"op": "sketch", "name": "Base", "plane": "xy", "entities": [
            {"type": "rectangle", "center": [0, 0], "width": "size", "height": "size"}]},
        {"op": "extrude", "name": "Body", "sketch": "Base", "distance": "height"},
    ],
}


@pytest.fixture
def block(session):
    build_part(session, PartRecipe.model_validate(BLOCK))
    return session.context()


def apply(session, context, operations):
    return [apply_operation(session, context, op) for op in _OPS.validate_python(operations)]


def select(session, context, **selector):
    resolved = resolve_selector(Selector.model_validate(selector), context.resolver)
    return session.backend.select(context.doc_id, resolved)


class TestSelectors:
    def test_a_box_has_four_vertical_edges(self, session, block):
        assert len(select(session, block, kind="edge", filter="vertical")) == 4

    def test_and_eight_horizontal_ones(self, session, block):
        assert len(select(session, block, kind="edge", filter="horizontal")) == 8

    def test_top_and_bottom_faces(self, session, block):
        assert len(select(session, block, kind="face", filter="top")) == 1
        assert len(select(session, block, kind="face", filter="bottom")) == 1

    def test_the_top_face_is_where_it_should_be(self, session, block):
        top = select(session, block, kind="face", filter="top")[0]
        assert top.midpoint[2] == pytest.approx(2.0)  # 20 mm in cm

    def test_limit_trims_the_result(self, session, block):
        assert len(select(session, block, kind="edge", filter="vertical", limit=2)) == 2

    def test_near_sorts_by_distance(self, session, block):
        matches = select(session, block, kind="edge", filter="vertical", near=[20, 20, 10])
        assert matches[0].midpoint[0] == pytest.approx(2.0)
        assert matches[0].midpoint[1] == pytest.approx(2.0)

    def test_within_filters_out_the_far_ones(self, session, block):
        matches = select(session, block, kind="edge", filter="vertical",
                         near=[20, 20, 10], within=5)
        assert len(matches) == 1

    def test_feature_scoping(self, session, block):
        assert select(session, block, kind="face", feature="Body")
        assert select(session, block, kind="face", feature="Nothing") == []

    def test_explicit_ids_round_trip(self, session, block):
        first = select(session, block, kind="edge", filter="vertical")[0]
        again = select(session, block, kind="edge", ids=[first.id])
        assert [m.id for m in again] == [first.id]

    def test_a_stale_handle_is_reported_clearly(self, session, block):
        with pytest.raises(SelectionError, match="already-consumed"):
            select(session, block, kind="edge", ids=["edge999"])

    def test_length_filters(self, session, block):
        long_edges = select(session, block, kind="edge", min_length=30)
        assert all(edge.length >= 3.0 for edge in long_edges)

    def test_selector_units_are_converted(self, session, block):
        resolved = resolve_selector(
            Selector.model_validate({"near": [10, 0, 0], "within": 5}), block.resolver
        )
        assert resolved.near == pytest.approx((1.0, 0.0, 0.0))
        assert resolved.within == pytest.approx(0.5)


class TestEdgeTreatments:
    def test_filleting_the_vertical_edges(self, session, block):
        [result] = apply(session, block, [
            {"op": "fillet", "edges": {"filter": "vertical"}, "radius": 5}])
        assert result["detail"]["edges"] == 4

    def test_filleted_edges_cannot_be_filleted_again(self, session, block):
        apply(session, block, [{"op": "fillet", "edges": {"filter": "vertical"}, "radius": 5}])
        assert select(session, block, kind="edge", filter="vertical") == []

    def test_a_selector_that_matches_nothing_explains_itself(self, session, block):
        with pytest.raises(SelectionError, match="matched no edges"):
            apply(session, block, [
                {"op": "fillet", "edges": {"feature": "Ghost"}, "radius": 5}])

    def test_chamfer_on_the_top_edges(self, session, block):
        [result] = apply(session, block, [
            {"op": "chamfer", "edges": {"filter": "horizontal", "near": [0, 0, 20], "within": 30},
             "distance": 1}])
        assert result["detail"]["edges"] >= 4

    def test_shell_removes_a_face(self, session, block):
        [result] = apply(session, block, [
            {"op": "shell", "faces": {"kind": "face", "filter": "top"}, "thickness": 2}])
        assert result["detail"]["removed_faces"] == 1


class TestPatternsAndFeatureEditing:
    def test_a_pattern_defaults_to_the_previous_feature(self, session, block):
        [result] = apply(session, block, [
            {"op": "rectangular_pattern", "axis1": "x", "count1": 3, "spacing1": 50}])
        assert result["detail"]["features"] == ["Body"]
        assert result["detail"]["occurrences"] == 3

    def test_a_circular_pattern_records_its_angle(self, session, block):
        [result] = apply(session, block, [
            {"op": "circular_pattern", "features": ["Body"], "axis": "z", "count": 6}])
        assert result["detail"]["angle_deg"] == pytest.approx(360.0)

    def test_patterning_a_feature_that_does_not_exist(self, session, block):
        with pytest.raises(Exception, match="No feature named"):
            apply(session, block, [{"op": "mirror", "features": ["Rib"], "plane": "yz"}])

    def test_rename_then_reference(self, session, block):
        session.backend.rename_feature(block.doc_id, "Body", "MainBody")
        assert select(session, block, kind="face", feature="MainBody")

    def test_suppress_and_delete(self, session, block):
        assert session.backend.suppress_feature(block.doc_id, "Body", True).suppressed
        session.backend.delete_feature(block.doc_id, "Body")
        assert [f.name for f in session.backend.list_features(block.doc_id)] == []


class TestHoles:
    def test_a_bolt_circle_drills_every_point(self, session, block):
        results = apply(session, block, [
            {"op": "sketch", "name": "Bolts", "plane": "xy", "entities": [
                {"type": "bolt_circle", "diameter": 30, "count": 6}]},
            {"op": "hole", "sketch": "Bolts", "diameter": 5, "through_all": True},
        ])
        assert results[1]["detail"]["count"] == 6

    def test_named_points_select_a_subset(self, session, block):
        results = apply(session, block, [
            {"op": "sketch", "name": "Pts", "plane": "xy", "entities": [
                {"type": "point", "position": [10, 10], "name": "a"},
                {"type": "point", "position": [-10, -10], "name": "b"},
            ]},
            {"op": "hole", "sketch": "Pts", "points": ["a"], "diameter": 5},
        ])
        assert results[1]["detail"]["count"] == 1

    def test_a_point_name_that_is_not_a_hole_centre(self, session, block):
        with pytest.raises(Exception, match="not a hole-centre point"):
            apply(session, block, [
                {"op": "sketch", "name": "Mixed", "plane": "xy", "entities": [
                    {"type": "circle", "diameter": 10, "name": "bore"}]},
                {"op": "hole", "sketch": "Mixed", "points": ["bore"], "diameter": 5},
            ])

    def test_a_blind_hole_is_not_through_all(self, session, block):
        results = apply(session, block, [
            {"op": "sketch", "name": "Blind", "plane": "xy", "entities": [
                {"type": "point", "position": [0, 0]}]},
            {"op": "hole", "sketch": "Blind", "diameter": 6, "depth": 10},
        ])
        assert results[1]["detail"]["through_all"] is False


class TestCutsAndPlanes:
    def test_a_cut_removes_material(self, session, block):
        before = session.backend.mass_properties(block.doc_id).volume
        apply(session, block, [
            {"op": "sketch", "name": "Pocket", "plane": "xy", "entities": [
                {"type": "circle", "center": [0, 0], "diameter": 20}]},
            {"op": "extrude", "sketch": "Pocket", "distance": 5, "operation": "cut"},
        ])
        after = session.backend.mass_properties(block.doc_id).volume
        assert after == pytest.approx(before - math.pi * 1.0**2 * 0.5)

    def test_a_sketch_on_an_offset_work_plane(self, session, block):
        results = apply(session, block, [
            {"op": "work_plane", "name": "Upper", "kind": "offset", "base": "xy",
             "offset": "height"},
            {"op": "sketch", "name": "OnTop", "plane": "Upper", "entities": [
                {"type": "circle", "diameter": 10}]},
            {"op": "extrude", "sketch": "OnTop", "distance": 5},
        ])
        assert results[-1]["kind"] == "extrude"

    def test_sketching_on_an_unknown_plane_is_reported(self, session, block):
        with pytest.raises(Exception, match="Unknown sketch plane"):
            apply(session, block, [
                {"op": "sketch", "name": "Nowhere", "plane": "abc", "entities": [
                    {"type": "circle", "diameter": 5}]}])


class TestConvexity:
    """"Round the inside corner" should be sayable without guessing a point.

    A `near` point depends on which way the sketch plane faces, which is not
    something a recipe author should have to know; `concave` does not.
    """

    def test_the_selector_accepts_it(self):
        assert Selector.model_validate({"filter": "concave"}).filter == "concave"
        assert Selector.model_validate({"filter": "convex"}).filter == "convex"

    def test_it_survives_resolution(self, session, block):
        resolved = resolve_selector(
            Selector.model_validate({"kind": "edge", "filter": "concave"}), block.resolver
        )
        assert resolved.filter == "concave"

    def test_the_simulator_knows_a_profile_corner(self, session, block):
        """The one case it can answer: an edge along an extrusion at a corner.

        It used to decline for every edge and match all of them, which is worse
        than declining -- a recipe asking for the single concave edge on an
        angle bracket got whichever edge came first.
        """
        every = select(session, block, kind="edge", filter="all")
        convex = select(session, block, kind="edge", filter="convex")
        assert 0 < len(convex) < len(every)
        assert all(match.convexity == "convex" for match in convex)
        assert all(match.convexity_from == "profile corner" for match in convex)

    def test_a_box_has_no_concave_edge(self, session, block):
        assert select(session, block, kind="edge", filter="concave") == []

    def test_it_still_declines_for_the_edges_it_cannot_see(self, session, block):
        """A cap edge's convexity depends on what the boss sits on."""
        every = select(session, block, kind="edge", filter="all")
        assert any(match.convexity is None for match in every)

    def test_an_l_section_has_exactly_one_concave_edge(self, session):
        """The inside corner, which is what a bracket's fillet is for."""
        from inventor_mcp.builder import build_part
        from inventor_mcp.schema import PartRecipe

        build_part(session, PartRecipe.model_validate({
            "name": "L", "units": "mm", "operations": [
                {"op": "sketch", "name": "S", "plane": "xz", "entities": [
                    {"type": "polyline", "closed": True, "points": [
                        [0, 0], [60, 0], [60, 6], [6, 6], [6, 40], [0, 40]]}]},
                {"op": "extrude", "sketch": "S", "distance": 30},
            ]}))
        context = session.context(session.active)
        [inside] = select(session, context, kind="edge", filter="concave")
        assert inside.length == pytest.approx(3.0), "it runs the length of the extrusion"
        assert len(select(session, context, kind="edge", filter="convex")) == 5

    def test_the_com_filter_requires_a_known_convexity(self):
        from inventor_mcp.backend.base import TopoInfo
        from inventor_mcp.backend.com import backend as com

        inside = TopoInfo(id="e1", kind="edge", description="", convexity="concave")
        outside = TopoInfo(id="e2", kind="edge", description="", convexity="convex")
        unknown = TopoInfo(id="e3", kind="edge", description="")
        assert com._com_passes_filter(inside, "concave") is True
        assert com._com_passes_filter(outside, "concave") is False
        assert com._com_passes_filter(unknown, "concave") is False
        assert com._com_passes_filter(unknown, "convex") is False


class TestEveryOperationReportsWhatItDid:
    """An operation that only says it ran has told the model nothing.

    A cut that met no material, a hole drilled past the part and a fillet on
    the wrong edge all reported success. The volume was the witness every time,
    and it was computed and then thrown away -- printed in a script for a human
    to read rather than returned to the model that could act on it.
    """

    def test_a_cut_reports_the_material_it_removed(self, session, block):
        [result] = apply(session, block, [
            {"op": "sketch", "name": "P", "plane": "xy", "entities": [
                {"type": "circle", "center": [0, 0], "diameter": 20}]},
        ])
        [cut] = apply(session, block, [
            {"op": "extrude", "sketch": "P", "distance": 5, "operation": "cut"}])
        assert cut["measured"]["volume_change_cm3"] < 0
        assert "note" not in cut["measured"]

    def test_an_operation_that_changed_nothing_says_so(self, session, block):
        """The signal that was missing for three rounds of live debugging.

        A work plane is the example because it genuinely adds no material. A
        pattern used to serve here, back when the simulator did not model an
        occurrence's volume -- which meant a pattern that worked and a cut that
        met no material looked identical, the very thing this reports.
        """
        [result] = apply(session, block, [
            {"op": "work_plane", "base": "xy", "offset": 10}])
        assert result["measured"]["note"] == "the volume did not change"

    def test_a_pattern_of_a_cut_removes_more_material(self, session, block):
        """An occurrence does what its seed did, so three slots remove three."""
        apply(session, block, [
            {"op": "sketch", "name": "Slot", "plane": "xy", "entities": [
                {"type": "circle", "center": [-15, 0], "diameter": 6}]},
        ])
        [cut] = apply(session, block, [
            {"op": "extrude", "sketch": "Slot", "distance": 20, "operation": "cut"}])
        one = cut["measured"]["volume_change_cm3"]
        assert one < 0
        [pattern] = apply(session, block, [
            {"op": "rectangular_pattern", "features": [cut["name"]],
             "axis1": "x", "count1": 3, "spacing1": 15}])
        assert pattern["measured"]["volume_change_cm3"] == pytest.approx(2 * one)

    def test_the_topology_counts_come_back_too(self, session, block):
        [result] = apply(session, block, [
            {"op": "fillet", "edges": {"filter": "vertical"}, "radius": 5}])
        assert result["measured"]["faces"] > 0
        assert result["measured"]["edges"] > 0

    def test_a_fillet_reports_the_edges_it_consumed(self, session, block):
        [result] = apply(session, block, [
            {"op": "fillet", "edges": {"filter": "vertical"}, "radius": 5}])
        assert result["measured"]["edges_change"] != 0

    def test_a_sketch_is_not_weighed(self, session, block):
        """It adds no material, so a volume report would be noise."""
        [result] = apply(session, block, [
            {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                {"type": "circle", "diameter": 10}]}])
        assert "measured" not in result

    def test_the_first_measurement_admits_it_has_no_baseline(self, session):
        from inventor_mcp.builder import build_part
        from inventor_mcp.schema import PartRecipe

        recipe = PartRecipe.model_validate(BLOCK)
        operations = recipe.operations
        recipe.operations = []
        build_part(session, recipe)
        context = session.context()
        results = [apply_operation(session, context, op) for op in operations]
        first = next(r for r in results if "measured" in r)
        assert first["measured"]["note"] == "first measurement, nothing to compare"
        assert "volume_change_cm3" not in first["measured"]

    def test_a_growing_part_reports_a_bigger_span(self, session, block):
        [result] = apply(session, block, [
            {"op": "sketch", "name": "Tower", "plane": "xy", "entities": [
                {"type": "circle", "center": [0, 0], "diameter": 10}]},
        ])
        [grown] = apply(session, block, [
            {"op": "extrude", "sketch": "Tower", "distance": 30}])
        assert grown["measured"]["span_mm"] != grown["measured"]["span_mm_was"]

    def test_a_backend_that_cannot_measure_is_not_guessed_at(self, session, block):
        """No measurement is reported rather than a fabricated one."""
        from inventor_mcp.builder import measure

        session.backend.mass_properties = lambda doc_id: (_ for _ in ()).throw(
            RuntimeError("no solid"))
        assert measure(session, block) is None


class TestWhatAnEdgeTreatmentCostsInVolume:
    """A fillet and a chamfer take away different shapes.

    They shared one formula until this file was written: ``_edge_treatment`` was
    handed a "size" and squared it against r^2 (1 - pi/4), which is the corner
    outside a quarter round. A chamfer takes a triangle. So every rehearsed
    chamfer was charged 0.2146 d^2 where the answer is d^2 / 2 -- 57% light, on a
    number nothing pinned. Worse than the error was where it pointed: the
    divergence check allows a chamfer 30%, so a live run would have accused a
    correct recipe of catching more edges than it meant to.

    The block is 40 x 40 x 20, so its four vertical edges are 2 cm each: 8 cm of
    edge, and every figure below is 8 cm times one cross-section.
    """

    EDGE_CM = 8.0
    #: `measured` rounds to six places, so that is the resolution to compare at.
    ROUNDING = 1e-6

    def moved(self, session, block, op):
        [result] = apply(session, block, [op])
        return result["measured"]["volume_change_cm3"]

    def test_an_equal_chamfer_takes_a_right_isoceles_triangle(self, session, block):
        got = self.moved(session, block, {
            "op": "chamfer", "edges": {"filter": "vertical"}, "distance": 2})
        assert got == pytest.approx(-self.EDGE_CM * 0.5 * 0.2 * 0.2, abs=self.ROUNDING)

    def test_two_distances_take_the_triangle_they_describe(self, session, block):
        got = self.moved(session, block, {
            "op": "chamfer", "edges": {"filter": "vertical"},
            "distance": 2, "distance2": 4})
        assert got == pytest.approx(-self.EDGE_CM * 0.5 * 0.2 * 0.4, abs=self.ROUNDING)

    def test_an_angled_chamfer_uses_the_angle_it_was_given(self, session, block):
        """Both were ignored before: the same number came back either way."""
        got = self.moved(session, block, {
            "op": "chamfer", "edges": {"filter": "vertical"},
            "distance": 2, "angle": 30})
        assert got == pytest.approx(
            -self.EDGE_CM * 0.5 * 0.2 * 0.2 * math.tan(math.radians(30)),
            abs=self.ROUNDING)

    def test_a_chamfer_is_not_a_fillet_of_the_same_size(self):
        """The regression itself: these must not come back equal.

        A fresh part each time -- an edge treatment consumes the edges it
        matched, so the second selector on one block finds nothing.
        """
        from inventor_mcp.session import Session

        def treat(op):
            fresh = Session(backend_kind="mock")
            fresh.ensure_backend().connect()
            build_part(fresh, PartRecipe.model_validate(BLOCK))
            return abs(self.moved(fresh, fresh.context(), op))

        cut = treat({"op": "chamfer", "edges": {"filter": "vertical"}, "distance": 3})
        rounded = treat({"op": "fillet", "edges": {"filter": "vertical"}, "radius": 3})
        assert cut > rounded
        assert cut / rounded == pytest.approx(0.5 / (1 - math.pi / 4), rel=1e-4)

    def test_a_constant_fillet_still_takes_the_corner_outside_the_round(self, session, block):
        got = self.moved(session, block, {
            "op": "fillet", "edges": {"filter": "vertical"}, "radius": 3})
        assert got == pytest.approx(-self.EDGE_CM * 0.09 * (1 - math.pi / 4),
                                    abs=self.ROUNDING)

    def test_a_variable_fillet_still_matches_what_inventor_removed(self, session, block):
        """3 mm to 8 mm over a 10 mm edge measured 0.071420 cm^3 in Inventor.

        Pinned here because the refactor above moved the mean-of-r-squared out
        of ``_edge_treatment`` and into ``fillet``, and this is the one number in
        the file that came off a real part.
        """
        [result] = apply(session, block, [{
            "op": "fillet", "edges": {"filter": "vertical", "limit": 1},
            "radius": 3, "radius_end": 8}])
        per_cm = result["measured"]["volume_change_cm3"] / 2.0  # the edge is 2 cm
        assert per_cm * 1.0 == pytest.approx(-0.071420, abs=5e-5)
