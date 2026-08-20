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

    def test_the_simulator_does_not_pretend_to_know(self, session, block):
        """It synthesises topology from sketch loops and has no material side."""
        every = select(session, block, kind="edge", filter="all")
        concave = select(session, block, kind="edge", filter="concave")
        assert len(concave) == len(every)
        assert all(match.convexity is None for match in concave)

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
        """The signal that was missing for three rounds of live debugging."""
        [result] = apply(session, block, [
            {"op": "rectangular_pattern", "axis1": "x", "count1": 3, "spacing1": 50}])
        assert result["measured"]["note"] == "the volume did not change"

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
