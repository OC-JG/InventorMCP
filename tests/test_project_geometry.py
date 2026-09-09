"""A sketch that borrows geometry from the solid it sits on.

The second-oldest bullet in `docs/FEATURE_COVERAGE.md`'s gap list: a sketch
could not reference the edges of the part it was drawn on, so "put a 2 mm wall
round the outline that is already there" meant writing the outline out again --
and writing it out again means it stops following the part when the part
changes, which is the one thing this server exists to prevent.

Two ways in, both published:

* `PlanarSketches.Add(face, UseFaceEdges=True)` takes the whole outline of the
  face at creation. There is no after-the-fact call for a whole face, which is
  why it is a flag on the sketch and not an entity.
* `PlanarSketch.AddByProjectingEntity(Entity)` takes one edge at a time.

**Both go in as real curves, not references.** What Inventor hands back is
marked `Reference = True` and bounds no material, so an extrude from a loop of
it finds no profile -- the sketch looks right and the feature does nothing.
That flag is cleared, and a release that will not clear it is a hard error.

The simulator's half was the one the roadmap called harder, and it landed
**exact** rather than declined: a prism's end face records the loop that made
it, so `use_face_edges` reproduces that loop's own lines and arcs -- not a
polygon through samples of them -- and the extrude after it is predicted to the
digit. Where the ledger cannot answer it **refuses**, which is a deliberate
departure from the declining-in-writing it does elsewhere: a declined placement
still leaves a feature with a volume, where a projection silently skipped leaves
a sketch with no profile and a recipe that looks like it worked.

One thing fell out of this that was a bug of its own. Sketching on a `face:`
handle was answered **XY at offset zero** by the simulator, silently, so a
sketch on the top of a 10 mm plate was filed at the bottom of it and every cut
from it was charged against material 10 mm away. The face's recorded plane and
depth are the real answer, and they are the same record the projection reads.
"""

from __future__ import annotations

import inspect
import math

import pytest

from inventor_mcp.backend.base import ResolvedSelector
from inventor_mcp.backend.com import backend as com
from inventor_mcp.builder import apply_operation, build_part
from inventor_mcp.errors import FeatureError, SketchError
from inventor_mcp.plan import PLine, PPoint
from inventor_mcp.schema import Operation, PartRecipe, SketchOp
from pydantic import TypeAdapter

_OPERATION = TypeAdapter(Operation)

PLATE = [
    {"op": "sketch", "name": "S", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]},
    {"op": "extrude", "name": "Plate", "sketch": "S", "distance": 10},
]

#: The same plate with rounded corners, so the outline has arcs in it and a
#: sampled polygon would be visibly wrong.
ROUNDED = [
    {"op": "sketch", "name": "S", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40,
         "corners": 8}]},
    {"op": "extrude", "name": "Plate", "sketch": "S", "distance": 10},
]


def built(session, operations: list[dict]):
    out = build_part(session, PartRecipe.model_validate(
        {"name": "P", "units": "mm", "operations": operations}))
    assert out["ok"], out["errors"]
    return out["document"], session.context(out["document"])


def top_face(session, doc: str) -> str:
    return session.backend.select(
        doc, ResolvedSelector(kind="face", filter="top", limit=1))[0].id


def step(session, context, operation: dict) -> dict:
    return apply_operation(session, context, _OPERATION.validate_python(operation))


class TestTheWholeOutline:
    def test_it_comes_in_as_the_faces_own_curves(self, session):
        doc, context = built(session, PLATE)
        result = step(session, context, {
            "op": "sketch", "name": "Lid", "plane": f"face:{top_face(session, doc)}",
            "use_face_edges": True})
        assert result["projected"] == 4
        assert result["entities"] == 4
        assert result["profiles"] == 1

    def test_an_arc_stays_an_arc(self, session):
        """The reason the loop's own primitives are copied rather than a
        polygon through points on them: a rounded rectangle's outline has four
        arcs, and a sampled version would have a hundred lines and an area a
        hair out."""
        doc, context = built(session, ROUNDED)
        result = step(session, context, {
            "op": "sketch", "name": "Lid", "plane": f"face:{top_face(session, doc)}",
            "use_face_edges": True})
        assert result["projected"] == 8  # four edges and four corner arcs
        plan = context.plans["Lid"]
        assert sum(1 for p in plan.primitives if type(p).__name__ == "PArc") == 4

    def test_an_extrude_from_it_is_predicted_to_the_digit(self, session):
        """The whole point of doing this in the simulator rather than declining
        it: the profile is the real outline, so the volume is arithmetic and
        not an estimate."""
        doc, context = built(session, PLATE)
        step(session, context, {
            "op": "sketch", "name": "Lid", "plane": f"face:{top_face(session, doc)}",
            "use_face_edges": True})
        step(session, context, {"op": "extrude", "name": "Rim", "sketch": "Lid",
                                "distance": 5})
        assert session.backend.mass_properties(doc).volume == pytest.approx(
            (60 * 40 * 10 + 60 * 40 * 5) / 1000, rel=1e-12)

    def test_it_is_fully_constrained_without_a_single_dimension(self, session):
        """Projected geometry is driven by the solid, so it has no free
        parameters of its own. Counting them would report every sketch that
        borrowed an outline as under-constrained, and a caller reading that
        would go looking for a fault that is not there."""
        doc, context = built(session, PLATE)
        result = step(session, context, {
            "op": "sketch", "name": "Lid", "plane": f"face:{top_face(session, doc)}",
            "use_face_edges": True})
        assert result["dimensions"] == 0
        assert result["fully_constrained"] is True
        assert result["degrees_of_freedom"] == 0

    def test_the_schema_refuses_it_on_anything_but_a_face(self):
        """An origin plane is not a face and a work plane has no edges, so
        there would be no outline to project -- and a flag silently ignored is
        how somebody learns the wrong lesson about their own recipe."""
        import pydantic

        for plane in ("xy", "SomeWorkPlane"):
            with pytest.raises(pydantic.ValidationError, match="needs `plane` to be"):
                SketchOp(name="X", plane=plane, use_face_edges=True)

    def test_a_face_with_no_recorded_outline_is_refused_with_the_reason(self, session):
        """A cylindrical face has no outline in one plane. Refused rather than
        skipped: a sketch whose projection was skipped has no profile, so the
        extrude after it does nothing and the recipe looks like it worked."""
        doc, context = built(session, PLATE + [
            {"op": "sketch", "name": "R", "plane": "xy", "entities": [
                {"type": "circle", "center": [0, 0], "diameter": 20}]},
            {"op": "extrude", "name": "Boss", "sketch": "R", "distance": 20}])
        curved = session.backend.select(
            doc, ResolvedSelector(kind="face", filter="cylindrical", limit=1))[0]
        with pytest.raises((SketchError, FeatureError)) as raised:
            step(session, context, {"op": "sketch", "name": "Bad",
                                    "plane": f"face:{curved.id}",
                                    "use_face_edges": True})
        assert "cylindrical" in str(raised.value) + str(raised.value.hint)


class TestSketchingOnAFaceAtAll:
    """The bug found on the way, and it was silent.

    `_plane_and_offset` answered XY at offset zero for any `face:` handle, so a
    sketch on the top of a 10 mm plate was filed at the bottom of it. Nothing
    said so, and every cut from that sketch was charged against material 10 mm
    from where it really was."""

    def test_a_sketch_on_the_top_face_is_at_the_top(self, session):
        doc, context = built(session, PLATE)
        step(session, context, {
            "op": "sketch", "name": "Up", "plane": f"face:{top_face(session, doc)}",
            "entities": [{"type": "rectangle", "center": [0, 0],
                          "width": 10, "height": 10}]})
        step(session, context, {"op": "extrude", "name": "Pip", "sketch": "Up",
                                "distance": 4})
        # A pip on the top adds all of itself. Placed at Z=0 it would have been
        # inside the plate and added nothing, which is the reading that says
        # the plane was wrong rather than the volume.
        assert session.backend.mass_properties(doc).volume == pytest.approx(
            (60 * 40 * 10 + 10 * 10 * 4) / 1000, rel=1e-12)

    def test_a_cut_from_a_face_sketch_meets_the_material_under_it(self, session):
        doc, context = built(session, PLATE)
        step(session, context, {
            "op": "sketch", "name": "Down", "plane": f"face:{top_face(session, doc)}",
            "entities": [{"type": "rectangle", "center": [0, 0],
                          "width": 10, "height": 10}]})
        step(session, context, {"op": "extrude", "name": "Pocket", "sketch": "Down",
                                "distance": 4, "operation": "cut",
                                "direction": "negative"})
        assert session.backend.mass_properties(doc).volume == pytest.approx(
            (60 * 40 * 10 - 10 * 10 * 4) / 1000, rel=1e-12)

    def test_a_face_this_ledger_cannot_place_is_refused(self, session):
        doc, context = built(session, PLATE + [
            {"op": "sketch", "name": "R", "plane": "xy", "entities": [
                {"type": "circle", "center": [0, 0], "diameter": 20}]},
            {"op": "extrude", "name": "Boss", "sketch": "R", "distance": 20}])
        curved = session.backend.select(
            doc, ResolvedSelector(kind="face", filter="cylindrical", limit=1))[0]
        with pytest.raises((SketchError, FeatureError), match="which plane"):
            step(session, context, {"op": "sketch", "name": "Bad",
                                    "plane": f"face:{curved.id}",
                                    "entities": [{"type": "point", "position": [0, 0]}]})


class TestProjectingNamedEdges:
    def test_an_edge_in_the_sketch_plane_becomes_a_line(self, session):
        """A straight edge is a midpoint, a direction and a length, which is
        enough. The plate's bottom edges lie in XY."""
        doc, context = built(session, PLATE)
        result = step(session, context, {
            "op": "sketch", "name": "Ref", "plane": "xy",
            "project": {"kind": "edge", "filter": "horizontal"}})
        plan = context.plans["Ref"]
        lines = [p for p in plan.primitives if isinstance(p, PLine)]
        assert result["projected"] == len(lines) > 0
        assert all(p.projected for p in plan.primitives)
        # The plate is 60 x 40, so every projected edge is one or the other.
        assert {round(line.length, 6) for line in lines} == {6.0, 4.0}

    def test_an_edge_perpendicular_to_the_plane_becomes_a_point(self, session):
        """Which is what Inventor gives, and the useful answer -- it is where a
        hole goes. A zero-length line would be worse than useless: the loop
        walker chains segments on their endpoints, and one with both at the
        same place would join anything to anything."""
        doc, context = built(session, PLATE)
        step(session, context, {
            "op": "sketch", "name": "Ref", "plane": "xy",
            "project": {"kind": "edge", "filter": "vertical"}})
        plan = context.plans["Ref"]
        assert len(plan.primitives) == 4
        assert all(isinstance(p, PPoint) for p in plan.primitives)
        assert {(round(p.position[0], 6), round(p.position[1], 6))
                for p in plan.primitives} == {
            (-3.0, -2.0), (3.0, -2.0), (3.0, 2.0), (-3.0, 2.0)}

    def test_a_selector_matching_nothing_is_refused(self, session):
        doc, context = built(session, PLATE)
        with pytest.raises(SketchError, match="matched no edges"):
            step(session, context, {
                "op": "sketch", "name": "Ref", "plane": "xy",
                "project": {"kind": "edge", "filter": "all",
                            "near": [500, 500, 500], "within": 1}})

    def test_a_circular_edge_is_refused_with_the_reason(self, session):
        """This ledger holds a circular edge as a centre and a circumference,
        which says nothing about which way it faces -- so whether it projects
        to a circle or to a line is not something it can answer. Refused rather
        than assumed."""
        doc, context = built(session, PLATE + [
            {"op": "sketch", "name": "R", "plane": "xy", "entities": [
                {"type": "circle", "center": [0, 0], "diameter": 20}]},
            {"op": "extrude", "name": "Boss", "sketch": "R", "distance": 20}])
        with pytest.raises(SketchError, match="cannot say where that lands"):
            step(session, context, {
                "op": "sketch", "name": "Ref", "plane": "xy",
                "project": {"kind": "edge", "filter": "circular", "limit": 1}})


class TestThePlanCarriesBothFields:
    def test_use_face_edges_comes_from_plan_sketch_and_project_from_the_builder(self):
        """They are set in two different places, because resolving a selector
        needs the recipe's units and `plan_sketch` has no way to get them. A
        plan that carried one and not the other would rehearse a sketch that
        referenced nothing and build one that did, so both are pinned here."""
        from inventor_mcp.geometry import plan_sketch
        from inventor_mcp.resolve import Resolver

        plan = plan_sketch(
            SketchOp(name="X", plane="face:face1", use_face_edges=True),
            Resolver("mm", "deg"))
        assert plan.use_face_edges is True
        assert plan.project is None, "plan_sketch cannot resolve a selector"

        source = inspect.getsource(
            __import__("inventor_mcp.builder", fromlist=["_apply_one"])._apply_one)
        assert "plan.project = resolve_selector(op.project" in source


class TestTheComCallsAreThePublishedOnes:
    def test_the_face_outline_is_asked_for_at_creation(self):
        """`PlanarSketches.Add(PlanarEntity, UseFaceEdges)` is the only place it
        can be asked for -- there is no after-the-fact call for a whole face."""
        source = inspect.getsource(com.ComBackend.build_sketch)
        assert "Sketches.Add(plane, bool(plan.use_face_edges))" in source

    def test_each_edge_goes_in_through_add_by_projecting_entity(self):
        source = inspect.getsource(com.ComBackend._project_entities)
        assert "sketch.AddByProjectingEntity(entity)" in source

    def test_the_reference_flag_is_cleared_and_a_refusal_is_fatal(self):
        """The detail the whole feature turns on. Reference geometry bounds no
        material, so a profile built from it comes back empty and the feature
        after it finds nothing to sweep -- while the sketch looks right."""
        source = inspect.getsource(com.ComBackend._project_entities)
        assert "made.Reference = False" in source
        assert "raise SketchError(" in source

    def test_the_selector_is_resolved_before_the_batch_opens(self):
        source = inspect.getsource(com.ComBackend.build_sketch)
        assert source.index("_topology_selection") < source.index("with self._batch")
