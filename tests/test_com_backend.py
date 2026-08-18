"""Tests for the parts of the COM backend that can run without Inventor.

The COM calls themselves need Windows and a licensed Inventor, so they are not
exercised here.  What *is* checked is everything around them: that the backend
degrades to a clear message off Windows, that enum resolution prefers the type
library and falls back predictably, and that the shared helpers agree with the
mock backend's interpretation of the same inputs.
"""

from __future__ import annotations

import sys

import pytest

from inventor_mcp.backend import create_backend
from inventor_mcp.backend.com import backend as com
from inventor_mcp.backend.com.constants import BOOLEAN_OPERATIONS, FALLBACK, Constants
from inventor_mcp.errors import BackendUnavailableError


def fake_point(x: float, y: float):
    """A stand-in for a SketchPoint: just something with .Geometry.X/.Y."""
    geometry = type("Geometry", (), {"X": x, "Y": y})()
    return type("SketchPoint", (), {"Geometry": geometry})()


class TestAvailability:
    @pytest.mark.skipif(sys.platform == "win32", reason="pywin32 is importable on Windows")
    def test_it_refuses_to_start_off_windows_with_an_actionable_message(self):
        with pytest.raises(BackendUnavailableError) as info:
            com.ComBackend()
        assert "Windows" in info.value.message
        assert "--backend mock" in info.value.hint

    @pytest.mark.skipif(sys.platform == "win32", reason="COM backend is available on Windows")
    def test_auto_falls_back_to_the_simulator(self):
        assert create_backend("auto").name == "mock"

    @pytest.mark.skipif(sys.platform == "win32", reason="COM backend is available on Windows")
    def test_asking_for_inventor_explicitly_still_fails_loudly(self):
        with pytest.raises(BackendUnavailableError):
            create_backend("inventor")

    def test_an_unknown_backend_name(self):
        with pytest.raises(BackendUnavailableError, match="Unknown backend"):
            create_backend("solidworks")


class TestConstants:
    def test_the_type_library_wins_when_it_is_available(self):
        class Fake:
            kJoinOperation = 999

        constants = Constants(Fake())
        assert constants.resolve("kJoinOperation") == 999
        assert constants.describe()["resolved"]["kJoinOperation"]["source"] == "typelib"

    def test_the_fallback_table_is_used_otherwise(self):
        constants = Constants(None)
        assert constants.resolve("kJoinOperation") == FALLBACK["kJoinOperation"]
        assert constants.describe()["resolved"]["kJoinOperation"]["source"] == "fallback"

    def test_a_partially_populated_type_library_falls_back_per_name(self):
        class Fake:
            kJoinOperation = 999

        constants = Constants(Fake())
        assert constants.resolve("kCutOperation") == FALLBACK["kCutOperation"]

    def test_an_unknown_enum_says_how_to_fix_it(self):
        with pytest.raises(BackendUnavailableError, match="kNotARealEnum"):
            Constants(None).resolve("kNotARealEnum")

    def test_every_operation_name_maps_to_a_known_enum(self):
        for name in BOOLEAN_OPERATIONS.values():
            assert name in FALLBACK

    def test_values_are_cached_after_the_first_lookup(self):
        constants = Constants(None)
        constants.resolve("kJoinOperation")
        assert "kJoinOperation" in constants.describe()["resolved"]


class TestHelpers:
    def test_export_extensions_cover_the_advertised_formats(self):
        for fmt in ("step", "stl", "iges", "sat", "dwg", "dxf", "obj", "3mf"):
            assert fmt in com.EXPORT_EXTENSIONS

    def test_com_error_messages_are_unwrapped(self):
        class FakeComError(Exception):
            excepinfo = (0, "Inventor", "The profile is not closed.  ", None, 0, 0)

        assert com._com_message(FakeComError()) == "The profile is not closed."

    def test_a_plain_exception_still_produces_a_message(self):
        assert com._com_message(RuntimeError("boom")) == "boom"

    def test_polar_matches_the_arc_sampling_used_elsewhere(self):
        import math

        assert com._polar((1.0, 2.0), 2.0, 0.0) == pytest.approx((3.0, 2.0))
        assert com._polar((0.0, 0.0), 1.0, math.pi / 2) == pytest.approx((0.0, 1.0))

    @pytest.mark.parametrize(
        "filter_name,normal,expected",
        [("top", (0, 0, 1), True), ("top", (0, 0, -1), False), ("bottom", (0, 0, -1), True)],
    )
    def test_face_filters_agree_with_the_mock_backend(self, filter_name, normal, expected):
        from inventor_mcp.backend.base import TopoInfo

        info = TopoInfo(id="f1", kind="face", description="", normal=normal, geometry="planar")
        assert com._com_passes_filter(info, filter_name) is expected

    def test_edge_filters_use_the_edge_direction(self):
        from inventor_mcp.backend.base import TopoInfo

        vertical = TopoInfo(id="e1", kind="edge", description="", direction=(0, 0, 1),
                            geometry="linear")
        horizontal = TopoInfo(id="e2", kind="edge", description="", direction=(1, 0, 0),
                              geometry="linear")
        assert com._com_passes_filter(vertical, "vertical") is True
        assert com._com_passes_filter(horizontal, "vertical") is False
        assert com._com_passes_filter(horizontal, "horizontal") is True


class TestContract:
    def test_both_backends_implement_the_whole_interface(self):
        from inventor_mcp.backend.base import Backend
        from inventor_mcp.backend.mock.backend import MockBackend

        expected = {
            name for name in dir(Backend)
            if not name.startswith("_") and callable(getattr(Backend, name))
        }
        for implementation in (MockBackend, com.ComBackend):
            missing = {
                name for name in expected
                if getattr(getattr(implementation, name, None), "__isabstractmethod__", False)
            }
            assert not missing, f"{implementation.__name__} leaves {missing} abstract"


class TestDocumentSpecialisation:
    """``Documents.Add`` returns the generic ``Document`` interface.

    Under early binding that interface really is generic: ``ComponentDefinition``
    lives on ``PartDocument``, so without a cast every call after `new_part`
    fails with AttributeError. These guard the mapping and the fallbacks.
    """

    def test_every_document_type_maps_to_an_interface(self):
        assert com._DOCUMENT_INTERFACES[12290] == "PartDocument"
        assert com._DOCUMENT_INTERFACES[12291] == "AssemblyDocument"
        assert set(com._DOCUMENT_INTERFACES) == {12290, 12291, 12292, 12293}

    def test_it_is_a_no_op_without_pywin32(self):
        sentinel = object()
        assert com._specialise(sentinel) is sentinel

    def test_an_object_with_no_document_type_is_returned_unchanged(self, monkeypatch):
        class Fake:
            pass

        monkeypatch.setattr(com, "win32com", None)
        fake = Fake()
        assert com._specialise(fake) is fake


class TestAssetLookup:
    def test_a_missing_collection_is_not_an_error(self):
        class Bare:
            pass

        assert com._asset_collection(Bare(), "material") is None

    def test_it_falls_back_to_the_generic_assets_collection(self):
        class Collection:
            Count = 0

        class Doc:
            Assets = Collection()

        assert com._asset_collection(Doc(), "material") is not None

    def test_a_collection_that_cannot_be_counted_is_skipped(self):
        class Broken:
            @property
            def Count(self):
                raise RuntimeError("COM says no")

        class Doc:
            MaterialAssets = Broken()

        assert com._asset_collection(Doc(), "material") is None

    def test_finding_an_asset_records_where_it_looked(self):
        class Asset:
            DisplayName = "Aluminum 6061"

        class Collection:
            Count = 1

            def Item(self, index):
                return Asset()

        class Doc:
            MaterialAssets = Collection()

        tried: list[str] = []
        found = com._find_asset(object(), Doc(), "aluminum 6061", "material", tried)
        assert found is not None
        assert tried == ["document assets"]

    def test_a_miss_reports_everywhere_it_searched(self):
        class Empty:
            Count = 0

        class Doc:
            MaterialAssets = Empty()

        class App:
            AssetLibraries = Empty()

        tried: list[str] = []
        assert com._find_asset(App(), Doc(), "unobtainium", "material", tried) is None
        assert tried == ["document assets", "asset libraries"]


class TestConstraintApplication:
    """A constraint that cannot be applied is an error, not a warning.

    Geometry that merely sits at the right coordinates is not joined, and
    Inventor will refuse to build a profile from it -- so the only case worth
    skipping is a constraint between an entity and itself.
    """

    def test_identical_wrappers_are_recognised(self):
        thing = object()
        assert com._same_com_object(thing, thing) is True

    def test_distinct_objects_without_com_are_not_the_same(self):
        assert com._same_com_object(object(), object()) is False

    def test_every_constraint_kind_is_dispatched(self):
        calls: list[str] = []

        class Collection:
            def __getattr__(self, name):
                def record(*args):
                    calls.append(name)
                return record

        backend = object.__new__(com.ComBackend)
        targets = [object(), object(), object()]
        for kind in ("horizontal", "vertical", "horizontal_align", "vertical_align",
                     "coincident", "collinear", "parallel", "perpendicular", "tangent",
                     "concentric", "equal", "symmetric", "midpoint", "ground"):
            backend._apply_constraint(Collection(), kind, targets)
        assert calls == [
            "AddHorizontal", "AddVertical", "AddHorizontalAlign", "AddVerticalAlign",
            "AddCoincident", "AddCollinear", "AddParallel", "AddPerpendicular",
            "AddTangent", "AddConcentric", "AddEqual", "AddSymmetry", "AddMidpoint",
            "AddGround",
        ]

    def test_an_unknown_kind_is_refused(self):
        from inventor_mcp.errors import SketchError

        backend = object.__new__(com.ComBackend)
        with pytest.raises(SketchError, match="Unsupported constraint"):
            backend._apply_constraint(object(), "welded", [object()])


class TestConstructionGeometry:
    """Construction is a property of curves, not of points.

    A sketch point forms no profile, so the flag is meaningless on one and
    Inventor rejects the assignment rather than ignoring it.
    """

    def test_curves_take_the_flag(self):
        from inventor_mcp.plan import PArc, PCircle, PEllipse, PLine

        for primitive in (PLine("l1"), PCircle("c1"), PArc("a1"), PEllipse("e1")):
            assert com._supports_construction(primitive) is True

    def test_points_do_not(self):
        from inventor_mcp.plan import PPoint

        assert com._supports_construction(PPoint("p1")) is False

    def test_the_shapes_that_build_construction_points_still_mark_them(self):
        """The plan should still say 'construction'; only the COM call is skipped."""
        from inventor_mcp.geometry import plan_sketch
        from inventor_mcp.plan import PPoint
        from inventor_mcp.resolve import Resolver
        from inventor_mcp.schema import SketchOp

        spec = SketchOp.model_validate({
            "op": "sketch", "plane": "xy",
            "entities": [{"type": "rectangle", "center": [0, 0], "width": 40, "height": 20}],
        })
        plan = plan_sketch(spec, Resolver("mm", "deg"))
        points = [p for p in plan.primitives if isinstance(p, PPoint)]
        assert len(points) == 1
        assert points[0].construction is True


class TestOriginPoint:
    """The sketch origin has to be projected in before it can be constrained.

    ``PlanarSketch.OriginPoint`` marks where the sketch sits; Inventor refuses
    to constrain against it. These cover the projection and the grounded-point
    fallback without needing Inventor.
    """

    def backend(self):
        # __init__ refuses to run without pywin32, which is the point of it;
        # bypass it to exercise the pure logic underneath.
        instance = object.__new__(com.ComBackend)
        instance._app = None
        return instance

    def test_it_projects_the_origin_work_point(self):
        projected = object()
        work_point = object()

        class Sketch:
            Name = "Sketch1"
            Parent = type("Def", (), {"WorkPoints": type("WP", (), {
                "Item": staticmethod(lambda index: work_point)})()})()

            def AddByProjectingEntity(self, entity):
                assert entity is work_point
                return projected

        assert self.backend()._origin_point(Sketch()) is projected

    def test_it_falls_back_to_a_grounded_point(self):
        created = object()
        grounded: list[object] = []

        class Sketch:
            Name = "Sketch1"

            @property
            def Parent(self):
                raise RuntimeError("no parent here")

            SketchPoints = type("SP", (), {
                "Add": staticmethod(lambda point, hole_center: created)})()
            GeometricConstraints = type("GC", (), {
                "AddGround": staticmethod(lambda entity: grounded.append(entity))})()

        instance = self.backend()
        instance._app = type("App", (), {"TransientGeometry": type("TG", (), {
            "CreatePoint2d": staticmethod(lambda x, y: (x, y))})()})()

        assert instance._origin_point(Sketch()) is created
        assert grounded == [created]

    def test_the_origin_is_resolved_once_and_reused(self):
        calls: list[int] = []

        class Sketch:
            Name = "Sketch1"
            Parent = type("Def", (), {"WorkPoints": type("WP", (), {
                "Item": staticmethod(lambda index: object())})()})()

            def AddByProjectingEntity(self, entity):
                calls.append(1)
                return "origin"

        from inventor_mcp.plan import ORIGIN

        instance = self.backend()
        sketch = Sketch()
        objects: dict = {}
        first = instance._entity(sketch, objects, ORIGIN)
        second = instance._entity(sketch, objects, ORIGIN)
        assert first == second == "origin"
        assert len(calls) == 1, "the origin should be projected once per sketch"


class TestBindingMode:
    """Late binding is the default because the generated wrapper misbehaves."""

    def test_late_is_the_default(self, monkeypatch):
        monkeypatch.delenv("INVENTOR_MCP_BINDING", raising=False)
        assert com.resolve_binding() == "late"

    def test_an_explicit_argument_wins(self, monkeypatch):
        monkeypatch.setenv("INVENTOR_MCP_BINDING", "late")
        assert com.resolve_binding("early") == "early"

    def test_the_environment_is_honoured(self, monkeypatch):
        monkeypatch.setenv("INVENTOR_MCP_BINDING", "  EARLY  ")
        assert com.resolve_binding() == "early"

    def test_nonsense_falls_back_to_late(self, monkeypatch):
        monkeypatch.setenv("INVENTOR_MCP_BINDING", "telepathy")
        assert com.resolve_binding() == "late"

    def test_late_binding_is_a_no_op_without_pywin32(self):
        sentinel = object()
        assert com._as_late_bound(sentinel) is sentinel


class TestDistinct:
    def test_duplicates_are_dropped_by_identity(self):
        first, second = object(), object()
        assert com._distinct(first, second, first) == [first, second]

    def test_none_is_ignored(self):
        thing = object()
        assert com._distinct(thing, None) == [thing]

    def test_equal_but_separate_objects_are_both_kept(self):
        # COM wrappers compare equal surprisingly often; identity is what matters.
        assert len(com._distinct([1], [1])) == 2
