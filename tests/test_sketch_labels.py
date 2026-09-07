"""The recipe's sketch labels, and the Inventor entities they name.

The first live acceptance run of the work-geometry check failed on its first
assertion -- `The carrier sketch did not keep a point named '__work_point__'` --
and the cause was not the unmeasured `WorkPoints.AddByPoint` it was written to
test. Three lookups in the COM backend searched Inventor's own `SketchPoints`
and `SketchLines` for an entity whose `Name` equalled the recipe's label, and
**nothing ever set those names**: `_add_primitive` sets `Construction`,
`HoleCenter` and `Centerline`, and has never read `primitive.label`. The labels
lived only in the `SketchPlan`, on the Python side.

So all three were searching for a name that could not be there:

* `_carrier_point`, which is how every `work_point` and every
  `normal_to_plane` work axis is placed;
* `work_axis` with `kind: "sketch_line"`;
* `_resolve_axis`, which is how a **revolve** finds a named sketch line -- a
  path that predates the work-geometry work entirely and that `docs/ROADMAP.md`
  claimed had been measured.

The simulator reads the plan's labels directly, which is why every one of these
passed offline and the live run failed immediately. The fix keeps the entity
Inventor handed back at creation, so nothing depends on whether a sketch
entity's `Name` can be assigned -- which nothing here has measured.

What these tests can and cannot reach: the bookkeeping is ordinary Python and is
checked here. The COM calls that consume the entity still need Windows and a
licensed Inventor, so `scripts/live_acceptance.py --only work-geometry` remains
the thing that says whether this actually fixed the run.
"""

from __future__ import annotations

import pytest

from inventor_mcp.backend.com.backend import _entities_by_label
from inventor_mcp.geometry import plan_sketch
from inventor_mcp.plan import PLine, PPoint, SketchPlan
from inventor_mcp.resolve import Resolver
from inventor_mcp.schema import SketchOp


class _Entity:
    """A stand-in for whatever Inventor hands back, distinguishable by name."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - only for a failure message
        return f"<{self.name}>"


def planned(*entities: dict) -> SketchPlan:
    """A plan built the way a recipe builds one, so labels arrive as they really do."""
    op = SketchOp.model_validate(
        {"op": "sketch", "name": "S", "plane": "xy", "entities": list(entities)})
    return plan_sketch(op, Resolver("mm", "deg"))


def created(plan: SketchPlan) -> dict:
    """What `build_sketch` collects: one entity per primitive, keyed by its id."""
    return {primitive.id: _Entity(primitive.id) for primitive in plan.primitives}


class TestTheMapThatWasMissing:
    def test_a_named_line_is_findable_by_its_label(self):
        plan = planned({"type": "line", "name": "Spoke", "start": [0, 0], "end": [30, 0]})
        found = _entities_by_label(plan, created(plan))
        assert set(found) == {"Spoke"}
        assert isinstance(found["Spoke"], _Entity)

    def test_a_named_point_is_too(self):
        """The carrier point is exactly this case, and the one that failed live."""
        plan = SketchPlan(name="wpt_carrier", plane="xy")
        plan.add(PPoint("point1", construction=True), "__work_point__")
        found = _entities_by_label(plan, created(plan))
        assert list(found) == ["__work_point__"]

    def test_a_label_covering_several_primitives_keeps_the_first(self):
        """A rectangle named "Outline" is several lines under one label, and
        `resolve_axis` already takes the first `PLine` it finds under one.

        It is five lines, not four: the four edges carry the label and a
        construction diagonal carries none, which is why the count below is of
        the labelled ones rather than of `PLine`s.
        """
        plan = planned(
            {"type": "rectangle", "name": "Outline", "center": [0, 0],
             "width": 100, "height": 60})
        labelled = [p for p in plan.primitives
                    if isinstance(p, PLine) and p.label == "Outline"]
        assert len(labelled) == 4, "the fixture is meant to be one label over four edges"
        found = _entities_by_label(plan, created(plan))
        assert found["Outline"].name == labelled[0].id

    def test_the_unlabelled_diagonal_of_that_rectangle_is_not_stored(self):
        """The construction line the planner adds carries no label, so it cannot
        be reached by one -- and must not displace the edges that can."""
        plan = planned(
            {"type": "rectangle", "name": "Outline", "center": [0, 0],
             "width": 100, "height": 60})
        assert list(_entities_by_label(plan, created(plan))) == ["Outline"]

    def test_unlabelled_primitives_contribute_nothing(self):
        plan = planned({"type": "line", "start": [0, 0], "end": [10, 0]},
                       {"type": "line", "name": "Named", "start": [0, 5], "end": [10, 5]})
        assert list(_entities_by_label(plan, created(plan))) == ["Named"]

    def test_a_primitive_inventor_declined_stores_no_none(self):
        """Otherwise a caller gets `None` back and hands it to a COM call, which
        fails somewhere else entirely."""
        plan = planned({"type": "line", "name": "Spoke", "start": [0, 0], "end": [30, 0]})
        assert _entities_by_label(plan, {}) == {}

    def test_an_empty_plan_is_an_empty_map(self):
        assert _entities_by_label(SketchPlan(name="S", plane="xy"), {}) == {}


class TestTheLookup:
    """`_labelled_entity` is a dict traversal, so it runs off Windows unbound."""

    def look(self, store: dict, doc: str, sketch: str, label: str):
        from inventor_mcp.backend.com.backend import ComBackend

        class _Stub:
            _sketch_entities = store

        return ComBackend._labelled_entity(_Stub(), doc, sketch, label)

    def test_it_finds_what_was_stored(self):
        entity = _Entity("line1")
        store = {"doc1": {"Aim": {"Spoke": entity}}}
        assert self.look(store, "doc1", "Aim", "Spoke") is entity

    @pytest.mark.parametrize("doc,sketch,label", [
        ("doc2", "Aim", "Spoke"),      # another document
        ("doc1", "Other", "Spoke"),    # another sketch
        ("doc1", "Aim", "Missing"),    # a label nothing was stored under
    ])
    def test_anything_else_is_none_so_the_caller_can_fall_back(self, doc, sketch, label):
        """None means "not kept here", and every caller then tries the old name
        search -- so a stale handle is no worse than the behaviour it replaced."""
        store = {"doc1": {"Aim": {"Spoke": _Entity("line1")}}}
        assert self.look(store, doc, sketch, label) is None

    def test_an_empty_store_is_none_rather_than_a_key_error(self):
        assert self.look({}, "doc1", "Aim", "Spoke") is None


class TestTheCallersStillHaveTheirFallback:
    """The name search stays, so this change cannot be worse than what it fixes."""

    @pytest.mark.parametrize("function", [
        "_named_sketch_point", "_named_sketch_line", "_named_work_point",
    ])
    def test_the_name_searches_are_still_there(self, function):
        from inventor_mcp.backend.com import backend as com

        assert callable(getattr(com, function))

    def test_every_label_lookup_tries_the_kept_entity_first(self):
        """A call site that skipped `_labelled_entity` would be the live failure
        all over again, and no offline test could catch it any other way."""
        import inspect

        from inventor_mcp.backend.com import backend as com

        for name in ("_carrier_point", "work_axis", "_resolve_axis"):
            source = inspect.getsource(getattr(com.ComBackend, name))
            assert "_labelled_entity" in source, (
                f"{name} resolves a recipe label without asking what "
                "build_sketch kept, which is the bug this file is about")
