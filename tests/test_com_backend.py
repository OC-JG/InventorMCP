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


class _RecordingRadius:
    """A stand-in for a Parameter: remembers the expression assigned to it."""

    def __init__(self, feature):
        object.__setattr__(self, "_feature", feature)

    def __setattr__(self, name, value):
        if name == "Expression":
            self._feature.applied = value
        else:
            object.__setattr__(self, name, value)


class _EdgeSet:
    """One fillet edge set, whose Radius is a Parameter."""

    def __init__(self, feature):
        self.Radius = _RecordingRadius(feature)


class _EdgeSets:
    def __init__(self, feature):
        self._feature = feature

    def Item(self, index):
        return _EdgeSet(self._feature)


class FakeFilletFeature:
    """A fillet whose radius parameter hangs off FilletEdgeSets."""

    def __init__(self):
        self.applied = None

    @property
    def FilletEdgeSets(self):
        return _EdgeSets(self)


class UnhelpfulFeature:
    """A fillet exposing neither route to its radius parameter."""

    @property
    def FilletEdgeSets(self):
        raise RuntimeError("not on this version")

    @property
    def Radius(self):
        raise RuntimeError("nor this")


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
                     "concentric", "equal_length", "equal_radius", "symmetric",
                     "midpoint", "ground"):
            backend._apply_constraint(Collection(), kind, targets)
        assert calls == [
            "AddHorizontal", "AddVertical", "AddHorizontalAlign", "AddVerticalAlign",
            "AddCoincident", "AddCollinear", "AddParallel", "AddPerpendicular",
            "AddTangent", "AddConcentric", "AddEqualLength", "AddEqualRadius",
            "AddSymmetry", "AddMidpoint", "AddGround",
        ]

    def test_every_plan_constraint_kind_is_handled(self):
        """The IR and the backend must not drift apart."""
        import typing

        from inventor_mcp.plan import ConstraintKind

        calls: list[str] = []

        class Collection:
            def __getattr__(self, name):
                def record(*args):
                    calls.append(name)
                return record

        backend = object.__new__(com.ComBackend)
        for kind in typing.get_args(ConstraintKind):
            backend._apply_constraint(Collection(), kind, [object(), object(), object()])
        assert len(calls) == len(typing.get_args(ConstraintKind))

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


class TestFilletOptions:
    """Optional-with-default COM arguments must be passed explicitly.

    Leaving them out makes pywin32 send a missing-variant that Inventor
    rejects. AddForSolid failed the same way and started working the moment
    its Combine flag was passed.
    """

    def test_all_six_trailing_options_are_supplied(self):
        assert len(com.ComBackend._FILLET_OPTIONS) == 6
        assert all(isinstance(option, bool) for option in com.ComBackend._FILLET_OPTIONS)

    def test_it_does_not_fillet_every_edge_in_the_part(self):
        all_fillets, all_rounds = com.ComBackend._FILLET_OPTIONS[:2]
        assert all_fillets is False and all_rounds is False

    def test_the_expression_can_be_restored_onto_an_edge_set(self):
        feature = FakeFilletFeature()
        assert com._set_radius_expression(feature, "corner_r") is True
        assert feature.applied == "corner_r"

    def test_it_reports_failure_rather_than_pretending(self):
        assert com._set_radius_expression(UnhelpfulFeature(), "corner_r") is False


class TestTypedCollections:
    """Inventor's feature methods take typed collections, not ObjectCollection.

    A generic ObjectCollection holds the same objects but is refused as a type
    mismatch, which is indistinguishable from a bad radius until you look at
    which argument the error names.
    """

    class Transient:
        def __init__(self, available=("CreateEdgeCollection", "CreateFaceCollection")):
            self.available = available
            self.made: list[str] = []

        def __getattr__(self, name):
            if name not in self.available and name != "CreateObjectCollection":
                raise AttributeError(name)

            def make():
                self.made.append(name)
                return f"<{name}>"

            return make

    def backend(self, transient):
        instance = object.__new__(com.ComBackend)
        instance._app = type("App", (), {"TransientObjects": transient})()
        return instance

    def test_edges_get_an_edge_collection(self):
        transient = self.Transient()
        assert self.backend(transient)._new_collection("edge") == "<CreateEdgeCollection>"

    def test_faces_get_a_face_collection(self):
        transient = self.Transient()
        assert self.backend(transient)._new_collection("face") == "<CreateFaceCollection>"

    def test_anything_else_gets_an_object_collection(self):
        transient = self.Transient()
        assert self.backend(transient)._new_collection("feature") == "<CreateObjectCollection>"

    def test_it_falls_back_when_the_typed_factory_is_missing(self):
        transient = self.Transient(available=())
        assert self.backend(transient)._new_collection("edge") == "<CreateObjectCollection>"


class TestExtentDirections:
    """Extent direction is an enum, not a flag.

    The COM signature types it as VT_I4, so a Python bool arrives as 1 or 0 --
    neither of which is a value of PartFeatureExtentDirectionEnum.
    """

    def test_both_directions_resolve_to_enum_values(self):
        constants = Constants(None)
        positive = constants.resolve("kPositiveExtentDirection")
        negative = constants.resolve("kNegativeExtentDirection")
        assert positive != negative
        assert {positive, negative}.isdisjoint({0, 1}), "must not collide with bool coercion"

    def test_the_recipe_directions_all_map(self):
        from inventor_mcp.backend.com.constants import EXTENT_DIRECTIONS

        constants = Constants(None)
        for name in EXTENT_DIRECTIONS.values():
            assert isinstance(constants.resolve(name), int)

    def test_symmetric_is_available_for_extrudes(self):
        from inventor_mcp.backend.com.constants import EXTENT_DIRECTIONS

        assert set(EXTENT_DIRECTIONS) == {"positive", "negative", "symmetric"}


class TestRefusedConstraints:
    """Whether a refusal mattered is asked of the sketch, not of the kind.

    Coincidence used to be treated as structural, so any refused coincident
    failed the whole sketch. But Inventor infers coincidences for itself and
    then refuses ours as duplicates, and a sketch of hole centres never had a
    profile to lose -- Inventor's own hole tool populates from the bolt circle
    it rejected. So the test is now the outcome: closed loops in the recipe and
    no profile out of Inventor.
    """

    def test_no_kind_is_fatal_on_its_own_any_more(self):
        assert not hasattr(com, "_STRUCTURAL_KINDS")

    def test_the_outcome_is_what_is_checked(self):
        import inspect

        source = inspect.getsource(com.ComBackend.build_sketch)
        assert "profiles == 0 and profile_loops(plan)" in source

    def test_inferred_kinds_are_all_real_too(self):
        import typing

        from inventor_mcp.plan import ConstraintKind

        assert com._INFERRED_KINDS <= set(typing.get_args(ConstraintKind))


class TestFilterFallthrough:
    """A filter that cannot be evaluated must not match everything.

    Falling through to True turns "the top face" into "every face", which is
    how a shell came to be handed all ten faces of a box to open.
    """

    def unknown(self, kind="face"):
        from inventor_mcp.backend.base import TopoInfo

        return TopoInfo(id="x", kind=kind, description="")

    def test_an_axis_filter_needs_a_normal(self):
        for name in ("top", "bottom", "left", "right", "front", "back"):
            assert com._com_passes_filter(self.unknown(), name) is False

    def test_a_convexity_filter_needs_a_convexity(self):
        assert com._com_passes_filter(self.unknown("edge"), "concave") is False

    def test_a_geometry_filter_needs_a_geometry(self):
        assert com._com_passes_filter(self.unknown(), "planar") is False

    def test_all_still_matches_anything(self):
        assert com._com_passes_filter(self.unknown(), "all") is True

    def test_a_known_normal_is_honoured(self):
        from inventor_mcp.backend.base import TopoInfo

        top = TopoInfo(id="f", kind="face", description="", normal=(0, 0, 1))
        assert com._com_passes_filter(top, "top") is True
        assert com._com_passes_filter(top, "bottom") is False


class TestEdgeConvexity:
    """Convexity is decided locally, at the edge.

    An earlier version compared the edge against the body's bounding-box
    centre, which put an L-section's outside corner in the concave set: the
    centre of a re-entrant part's bounding box is not inside the material.
    """

    def edge(self, midpoint, faces):
        box = type("Box", (), {
            "MinPoint": type("P", (), dict(zip("XYZ", midpoint)))(),
            "MaxPoint": type("P", (), dict(zip("XYZ", midpoint)))(),
        })()
        collection = type("Faces", (), {
            "Count": len(faces),
            "Item": staticmethod(lambda index: faces[index - 1]),
        })()
        return type("Edge", (), {
            "Faces": collection,
            "Evaluator": type("Ev", (), {"RangeBox": box})(),
        })()

    def face(self, normal, point):
        return type("Face", (), {
            "Geometry": type("G", (), {"Normal": type("N", (), dict(zip("XYZ", normal)))()})(),
            "IsParamReversed": False,
            "PointOnFace": type("P", (), dict(zip("XYZ", point)))(),
        })()

    def test_a_box_corner_is_convex(self):
        # Top face and a side face of a block; both point away from the material.
        top = self.face((0, 0, 1), (-45, 0, 6))
        side = self.face((0, -1, 0), (-45, -25, 3))
        assert com._edge_convexity(self.edge((-45, -25, 6), [top, side])) == ("convex", "sampled")

    def test_an_l_section_inside_corner_is_concave(self):
        # The exact case the body-centre heuristic got wrong: the base's top
        # face meeting the upright's inner face.
        base_top = self.face((0, 0, 1), (-45, 0, 6))
        upright = self.face((-1, 0, 0), (-6, 0, 38))
        assert com._edge_convexity(self.edge((-6, 0, 6), [base_top, upright])) == ("concave", "sampled")

    def test_an_edge_with_one_face_is_unknown(self):
        top = self.face((0, 0, 1), (0, 0, 6))
        assert com._edge_convexity(self.edge((0, 0, 6), [top])) == (None, "sampled")

    def test_a_curved_face_leaves_it_unknown(self):
        class Curved:
            Geometry = type("G", (), {})()  # a cylinder has no single Normal
            PointOnFace = type("P", (), {"X": 0, "Y": 0, "Z": 0})()

        top = self.face((0, 0, 1), (0, 0, 6))
        assert com._edge_convexity(self.edge((0, 0, 6), [top, Curved()])) == (None, "sampled")


class TestNoOpFeatures:
    """Inventor reports success for a cut that meets no material.

    The angle bracket spent two runs like that: its slots cut empty air and
    its upright holes drilled past the part, and every step still printed
    `ok`. The volume was the only witness -- 43.96331886 cm^3, exactly the
    body less its slots plus its fillet, with the holes contributing nothing.
    So a feature that removes material now has to prove it.
    """

    class Document:
        """A part whose volume is whatever the test says it is."""

        def __init__(self, volume):
            self._volume = volume
            outer = self

            class MassProperties:
                @property
                def Volume(self):
                    if outer._volume is None:
                        raise RuntimeError("no solid body")
                    return outer._volume

            self.ComponentDefinition = type(
                "CD", (), {"MassProperties": MassProperties()}
            )()

    def test_a_smaller_part_counts_as_removed(self):
        assert com._removed_material(self.Document(43.2), 46.2) is True

    def test_an_unchanged_part_does_not(self):
        assert com._removed_material(self.Document(46.2), 46.2) is False

    def test_nor_does_a_part_that_grew(self):
        """A join reported as a cut, or a fillet that filled a corner."""
        assert com._removed_material(self.Document(46.9), 46.2) is False

    def test_rounding_noise_is_not_a_cut(self):
        assert com._removed_material(self.Document(46.2 - 1e-12), 46.2) is False

    def test_a_real_cut_survives_the_tolerance(self):
        """The smallest cut worth making is still millions of times the floor."""
        assert com._removed_material(self.Document(46.2 - 1e-4), 46.2) is True

    def test_an_unmeasurable_part_is_given_the_benefit_of_the_doubt(self):
        """Better a missed no-op than a working feature reported as failed."""
        assert com._removed_material(self.Document(None), 46.2) is True
        assert com._removed_material(self.Document(43.2), None) is True

    def test_the_volume_reader_reports_no_solid_as_unknown(self):
        assert com._solid_volume(self.Document(None)) is None
        assert com._solid_volume(self.Document(46.2)) == 46.2


class TestHoleDirection:
    """A hole's direction is chosen before it is drilled, because there is no
    second chance.

    A hole consumes its sketch, so the feature cannot be deleted and rebuilt --
    the retry has no centres left to place itself on. Neither
    HoleFeature.ExtentDirection nor its Definition is writable on 2027.1, so it
    cannot be reversed in place either. Both routes were measured with
    scripts/probe_hole.py; both are closed. So the side is decided up front.
    """

    def test_a_holes_enum_runs_opposite_to_an_extrudes(self):
        """Measured, not assumed: see _HOLE_ALONG_NORMAL for the evidence.

        On a plane whose normal is +X with material at x > 0,
        kNegativeExtentDirection removed exactly a 9 mm x 6 mm hole and
        kPositiveExtentDirection removed nothing.
        """
        from inventor_mcp.backend.com.constants import EXTENT_DIRECTIONS

        assert com._HOLE_ALONG_NORMAL == "kNegativeExtentDirection"
        assert com._HOLE_AGAINST_NORMAL == "kPositiveExtentDirection"
        assert EXTENT_DIRECTIONS["positive"] == "kPositiveExtentDirection", (
            "an extrude's positive is along the normal; a hole's is not")
        assert com._HOLE_ALONG_NORMAL != EXTENT_DIRECTIONS["positive"]

    def test_the_two_enums_are_actually_different_values(self):
        constants = Constants(None)
        assert (constants.resolve(com._HOLE_ALONG_NORMAL)
                != constants.resolve(com._HOLE_AGAINST_NORMAL))

    class Part:
        """A part whose centre of mass sits where the test puts it."""

        def __init__(self, centroid=(3.0, 0.0, 0.0), origin=(0.0, 0.0, 0.0)):
            point = lambda values: type("P", (), dict(zip("XYZ", values)))()
            self.ComponentDefinition = type("CD", (), {
                "MassProperties": type("MP", (), {"CenterOfMass": point(centroid)})(),
            })()
            self._origin = point(origin)

        def sketch(self):
            outer = self
            return type("Sketch", (), {
                "Application": type("App", (), {
                    "TransientGeometry": type("TG", (), {
                        "CreatePoint2d": staticmethod(lambda u, v: (u, v))})()})(),
                "SketchToModelSpace": staticmethod(lambda point: outer._origin),
            })()

    def test_an_explicit_direction_is_obeyed(self):
        part = self.Part(centroid=(-3.0, 0.0, 0.0))  # material the other way
        for requested, expected in (("positive", True), ("negative", False)):
            along, why = com._drilling_side(
                requested, part, part.sketch(), (1.0, 0.0, 0.0))
            assert along is expected
            assert "the recipe asked" in why

    def test_auto_drills_towards_the_material(self):
        """The bracket: sketch on YZ at x=0, the part at x 0..90."""
        part = self.Part(centroid=(3.0, 0.0, 0.0))
        along, why = com._drilling_side("auto", part, part.sketch(), (1.0, 0.0, 0.0))
        assert along is True
        assert "the part lies along" in why

    def test_auto_drills_the_other_way_when_the_material_is_there(self):
        part = self.Part(centroid=(-3.0, 0.0, 0.0))
        along, why = com._drilling_side("auto", part, part.sketch(), (1.0, 0.0, 0.0))
        assert along is False

    def test_a_plane_through_the_middle_goes_either_way(self):
        """Both directions cut, so the choice cannot be wrong."""
        part = self.Part(centroid=(0.0, 0.0, 0.0))
        along, why = com._drilling_side("auto", part, part.sketch(), (1.0, 0.0, 0.0))
        assert along is True
        assert "through the middle" in why

    def test_an_unmeasurable_normal_still_gives_an_answer(self):
        part = self.Part()
        along, why = com._drilling_side("auto", part, part.sketch(), None)
        assert along is True
        assert "could not be measured" in why

    def test_an_unreadable_part_still_gives_an_answer(self):
        class Broken:
            @property
            def ComponentDefinition(self):
                raise RuntimeError("no mass properties")

        along, why = com._drilling_side(
            "auto", Broken(), self.Part().sketch(), (1.0, 0.0, 0.0))
        assert along is True
        assert "could not be located" in why

    def test_nothing_deletes_or_flips_a_hole_any_more(self):
        import inspect

        source = inspect.getsource(com.ComBackend.hole)
        assert "_delete_quietly" not in source, "deleting a hole loses its sketch"
        assert not hasattr(com, "_flip_extent"), "neither route is writable"
        assert "_drilling_side" in source

    def test_auto_is_the_default_a_recipe_gets(self):
        from inventor_mcp.schema import HoleOp

        assert HoleOp.model_fields["direction"].default == "auto"


class TestConvexityFromLoops:
    """Convexity decided from the boundary loops, which is exact.

    A face's boundary runs anticlockwise about its outward normal, so the
    material lies to the left of the loop -- `normal x tangent`. Whether that
    direction points into the neighbouring face's normal is the whole answer.

    Every case here is a real edge off a 60x40x10 plate with a 20x10x4 pocket
    in its underside, worked out from the loop orderings that give each face
    its outward normal. The pocket is what matters: its opening ring makes the
    underside a face with an inner loop, which is where sampling a point on the
    face stops being trustworthy.
    """

    _serial = iter(range(1, 999))

    def face(self, normal, point=(0.0, 0.0, 0.0)):
        """A planar face with an outward normal and its own identity."""
        tag = float(next(self._serial))
        box = type("Box", (), {
            "MinPoint": type("P", (), {"X": 0.0, "Y": 0.0, "Z": 0.0})(),
            "MaxPoint": type("P", (), {"X": tag, "Y": tag, "Z": tag})(),
        })()
        return type("Face", (), {
            "Geometry": type("G", (), {"Normal": type("N", (), dict(zip("XYZ", normal)))()})(),
            "IsParamReversed": False,
            "PointOnFace": type("P", (), dict(zip("XYZ", point)))(),
            "Evaluator": type("Ev", (), {"RangeBox": box, "Area": tag})(),
        })()

    def collection(self, items):
        return type("Collection", (), {
            "Count": len(items),
            "Item": staticmethod(lambda index: items[index - 1]),
        })()

    def segment(self, start, end):
        point = lambda values: type("P", (), dict(zip("XYZ", values)))()
        return type("G", (), {"StartPoint": point(start), "EndPoint": point(end)})()

    def edge(self, tangent, uses, midpoint=(0.0, 0.0, 0.0)):
        """An edge with endpoints, and one use per adjacent face.

        `uses` is (face, runs_against_the_parametric_direction) per face. Each
        use gets a `Next` whose edge lies on that face alone and meets ours at
        one end -- which is how the backend finds both the face and the
        direction the loop runs, neither of which the API gives directly.
        """
        begin = (0.0, 0.0, 0.0)
        finish = tuple(float(component) for component in tangent)
        faces = [face for face, _ in uses]

        objects = []
        for face, against in uses:
            # A loop runs towards the vertex it shares with the edge that
            # follows, so which end the neighbour meets sets the direction.
            shared = begin if against else finish
            elsewhere = self.face((0.0, 0.0, 1.0))  # a face our edge never touches
            neighbour = type("Edge", (), {
                "Faces": self.collection([face, elsewhere]),
                "Geometry": self.segment(shared, tuple(c + 7.0 for c in shared)),
            })()
            use = type("EdgeUse", (), {"Edge": type("Edge", (), {})()})()
            type(use).Next = type("EdgeUse", (), {"Edge": neighbour, "Next": use})()
            objects.append(use)

        box = type("Box", (), {
            "MinPoint": type("P", (), dict(zip("XYZ", midpoint)))(),
            "MaxPoint": type("P", (), dict(zip("XYZ", midpoint)))(),
        })()
        return type("Edge", (), {
            "EdgeUses": self.collection(objects),
            "Faces": self.collection(faces),
            "Geometry": self.segment(begin, finish),
            "Evaluator": type("Ev", (), {"RangeBox": box})(),
        })()

    def test_the_plates_top_front_edge_is_convex(self):
        top, front = self.face((0, 0, 1)), self.face((0, -1, 0))
        edge = self.edge((1, 0, 0), [(top, False), (front, True)])
        assert com._convexity_from_loops(edge) == "convex"

    def test_where_the_pocket_opens_onto_the_underside_is_convex(self):
        """A hole's opening is a 90-degree wedge of material like any corner."""
        underside, wall = self.face((0, 0, -1)), self.face((0, 1, 0))
        edge = self.edge((1, 0, 0), [(underside, False), (wall, True)])
        assert com._convexity_from_loops(edge) == "convex"

    def test_the_pockets_ceiling_meeting_a_wall_is_concave(self):
        ceiling, wall = self.face((0, 0, -1)), self.face((0, 1, 0))
        edge = self.edge((1, 0, 0), [(ceiling, True), (wall, False)])
        assert com._convexity_from_loops(edge) == "concave"

    def test_the_pockets_own_corners_are_concave(self):
        end, side = self.face((1, 0, 0)), self.face((0, 1, 0))
        edge = self.edge((0, 0, 1), [(end, True), (side, False)])
        assert com._convexity_from_loops(edge) == "concave"

    def test_an_l_sections_inside_corner_is_concave(self):
        """The bracket: the base's top face meeting the upright's inner face."""
        base_top, upright = self.face((0, 0, 1)), self.face((1, 0, 0))
        edge = self.edge((0, 1, 0), [(base_top, True), (upright, False)])
        assert com._convexity_from_loops(edge) == "concave"

    def test_both_faces_have_to_agree(self):
        """Either use answers on its own, so a disagreement means don't know."""
        top, front = self.face((0, 0, 1)), self.face((0, -1, 0))
        wrong = self.edge((1, 0, 0), [(top, False), (front, False)])
        assert com._convexity_from_loops(wrong) is None

    def test_faces_that_meet_smoothly_are_neither(self):
        """A filleted corner's tangent edges: no corner left to round."""
        flat, tangent = self.face((0, 0, 1)), self.face((0, 0, 1))
        edge = self.edge((1, 0, 0), [(flat, False), (tangent, True)])
        assert com._convexity_from_loops(edge) is None


class TestFindingTheFaceAndTheLoopDirection:
    """Neither is given by the API, so both come from the loop.

    Measured on 2027.1: EdgeUse has Next, Previous, Edge, IsParamReversed and a
    Parent that is the whole SurfaceBody -- no Face, no EdgeUseLoop. And
    IsParamReversed does not mean "runs against the loop": both uses of an edge
    report False, which made the two faces contradict each other on all 24
    edges of the probe's test part, so the exact method answered nothing.

    Next gives both facts. The following edge lies on the same face and shares
    exactly one face with ours, which names the face; it also meets ours at one
    vertex, and a loop runs towards the vertex it shares with the edge that
    follows, which gives the direction.
    """

    def helper(self):
        return TestConvexityFromLoops()

    def resolve(self, tangent, uses):
        probe = self.helper()
        edge = probe.edge(tangent, uses)
        ends = com._edge_ends(edge)
        faces = [face for face, _ in uses]
        use = edge.EdgeUses.Item(1)
        return com._use_face_and_tangent(use, ends, faces)

    def test_the_neighbour_names_the_face_and_the_direction(self):
        probe = self.helper()
        top, side = probe.face((0, 0, 1)), probe.face((0, -1, 0))
        face, tangent = self.resolve((1, 0, 0), [(top, False), (side, True)])
        assert face is top
        assert tangent == pytest.approx((1.0, 0.0, 0.0))

    def test_a_use_running_the_other_way_reports_the_other_direction(self):
        probe = self.helper()
        top, side = probe.face((0, 0, 1)), probe.face((0, -1, 0))
        face, tangent = self.resolve((1, 0, 0), [(top, True), (side, False)])
        assert face is top
        assert tangent == pytest.approx((-1.0, 0.0, 0.0))

    def test_two_faces_that_cannot_be_told_apart_give_up(self):
        probe = self.helper()
        first = probe.face((0, 0, 1))
        second = type(first)  # a second handle on faces with the same identity
        del second
        same = probe.face((0, 0, 1))
        edge = probe.edge((1, 0, 0), [(first, False), (same, True)])
        ends = com._edge_ends(edge)
        # Force the two candidates to share an identity, as coincident faces do.
        assert com._use_face_and_tangent(
            edge.EdgeUses.Item(1), ends, [first, first]) is None

    def test_an_unreadable_face_gives_up(self):
        class Opaque:
            @property
            def Evaluator(self):
                raise RuntimeError("no evaluator")

        probe = self.helper()
        good = probe.face((0, 0, 1))
        edge = probe.edge((1, 0, 0), [(good, False), (probe.face((0, -1, 0)), True)])
        ends = com._edge_ends(edge)
        assert com._use_face_and_tangent(edge.EdgeUses.Item(1), ends, [good, Opaque()]) is None

    def test_the_endpoints_are_read_from_the_geometry(self):
        probe = self.helper()
        edge = probe.edge((1, 0, 0), [(probe.face((0, 0, 1)), False),
                                      (probe.face((0, -1, 0)), True)])
        assert com._edge_ends(edge) == ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))

    def test_an_edge_with_no_readable_endpoints_gives_up(self):
        assert com._edge_ends(type("Edge", (), {})()) is None

    def test_a_use_with_no_isparamreversed_at_all_still_works(self):
        """The flag is not consulted, so its absence cannot matter."""
        probe = self.helper()
        top, side = probe.face((0, 0, 1)), probe.face((0, -1, 0))
        edge = probe.edge((1, 0, 0), [(top, False), (side, True)])
        for index in (1, 2):
            assert not hasattr(edge.EdgeUses.Item(index), "IsParamReversed")
        assert com._convexity_from_loops(edge) == "convex"


class TestConvexityFallsBackToSampling:
    """Loop orientation is preferred; sampling still answers when it cannot."""

    def sampled_only_edge(self, midpoint, faces):
        """No EdgeUses at all, as an older release or a surface body might be."""
        box = type("Box", (), {
            "MinPoint": type("P", (), dict(zip("XYZ", midpoint)))(),
            "MaxPoint": type("P", (), dict(zip("XYZ", midpoint)))(),
        })()
        return type("Edge", (), {
            "Faces": type("Faces", (), {
                "Count": len(faces),
                "Item": staticmethod(lambda index: faces[index - 1]),
            })(),
            "Evaluator": type("Ev", (), {"RangeBox": box})(),
        })()

    def face(self, normal, point):
        return type("Face", (), {
            "Geometry": type("G", (), {"Normal": type("N", (), dict(zip("XYZ", normal)))()})(),
            "IsParamReversed": False,
            "PointOnFace": type("P", (), dict(zip("XYZ", point)))(),
        })()

    def test_an_edge_with_no_uses_is_still_answered(self):
        top = self.face((0, 0, 1), (-45, 0, 6))
        side = self.face((0, -1, 0), (-45, -25, 3))
        edge = self.sampled_only_edge((-45, -25, 6), [top, side])
        assert com._convexity_from_loops(edge) is None
        assert com._edge_convexity(edge) == ("convex", "sampled")

    def test_the_loop_answer_wins_where_sampling_gets_it_wrong(self):
        """The bracket's own edge87, in centimetres, with the samples that beat it.

        The slot's outer wall meeting the top of the base at (65, 19.5, 6) mm.
        It is convex -- the material there is the quarter wedge y > 19.5, z < 6,
        the same as at any of the seven edges symmetric to it. The sampler said
        concave, because the top face is 84 mm long with two slots through it
        and its interior point landed across the part, while the wall's own
        point sat off to one end rather than square under the edge.
        """
        probe = TestConvexityFromLoops()
        base_top = probe.face((0, 0, 1), point=(2.0, -2.0, 0.6))
        slot_wall = probe.face((0, -1, 0), point=(7.4, 1.95, 0.25))
        edge = probe.edge((1, 0, 0), [(base_top, False), (slot_wall, True)],
                          midpoint=(6.5, 1.95, 0.6))
        assert com._convexity_from_samples(edge) == "concave", "the sampler is fooled"
        assert com._edge_convexity(edge) == ("convex", "loops"), "the loops are not"


class TestConvexityNeverSecondGuessesTheLoops:
    """An unknown answer is better than a sampled one, where loops exist.

    Sampling was the fallback for any edge the loops could not settle, which
    meant a loop that looked and declined got overruled by a heuristic that a
    face with a hole in it can fool. That is how the bracket's "inside corner"
    fillet moved onto a 56 mm convex edge after the upright was drilled --
    silently, with a plausible-looking volume change.

    Unknown matches no filter, so it surfaces as "the selector matched no
    edges". Wrong, but visibly wrong, which the wrong fillet was not.
    """

    def probe(self):
        return TestConvexityFromLoops()

    def test_where_the_loops_decide_they_decide(self):
        probe = self.probe()
        top, front = probe.face((0, 0, 1)), probe.face((0, -1, 0))
        edge = probe.edge((1, 0, 0), [(top, False), (front, True)])
        assert com._edge_convexity(edge) == ("convex", "loops")

    def test_where_the_loops_decline_the_sampler_is_not_asked(self):
        """Tangent faces: the loops look, find no corner, and that stands."""
        probe = self.probe()
        flat = probe.face((0, 0, 1), point=(30.0, 0.0, 0.0))
        tangent = probe.face((0, 0, 1), point=(0.0, -0.5, 0.2))
        edge = probe.edge((1, 0, 0), [(flat, False), (tangent, True)])
        assert com._convexity_from_loops(edge) is None
        assert com._edge_convexity(edge) == (None, "loops declined")

    def test_only_a_missing_loop_layer_falls_back(self):
        """An older release, or a surface body with no edge uses at all."""
        fallback = TestConvexityFallsBackToSampling()
        top = fallback.face((0, 0, 1), (-45, 0, 6))
        side = fallback.face((0, -1, 0), (-45, -25, 3))
        edge = fallback.sampled_only_edge((-45, -25, 6), [top, side])
        assert com._edge_convexity(edge) == ("convex", "sampled")

    def test_the_answer_says_which_method_gave_it(self):
        """So a topology dump shows whether an edge can be trusted."""
        from inventor_mcp.backend.base import TopoInfo

        assert "convexity_from" in TopoInfo.__dataclass_fields__


class TestRefusedDimensions:
    """A refused dimension leaves a degree of freedom; it does not kill a sketch.

    It used to. `_add_dimension` ran inside `_translate_errors(..., SketchError)`
    so one dimension Inventor called redundant took the whole sketch with it --
    which is why polyline profiles shipped carrying no dimensions at all and
    could not be revised. Constraints had the soft path all along; dimensions
    now have the same one.
    """

    class Parameter:
        def __init__(self, storable=True):
            for name, value in (("storable", storable), ("Expression", None),
                                ("Name", None)):
                object.__setattr__(self, name, value)

        def __setattr__(self, name, value):
            if name == "Expression" and not self.storable:
                raise RuntimeError("Inventor will not store that")
            object.__setattr__(self, name, value)

    class Created:
        def __init__(self, storable=True, deletable=True):
            self.Parameter = TestRefusedDimensions.Parameter(storable)
            self.deletable = deletable
            self.deleted = False

        def Delete(self):
            if not self.deletable:
                raise RuntimeError("will not delete")
            self.deleted = True

    def backend_with(self, outcome):
        """A backend whose dimension creation does whatever the test wants."""
        backend = com.ComBackend.__new__(com.ComBackend)
        backend._create_dimension = lambda *args: outcome()
        backend._entity = lambda sketch, objects, ref: ref
        backend._explain = lambda exc: str(exc)
        return backend

    def dimension(self, **extra):
        from inventor_mcp.plan import Ref

        fields = {"kind": "horizontal", "refs": (Ref("l1"),), "expression": "base_len",
                  "value": 9.0, "name": None, "text_offset": (0.0, 0.0),
                  "optional": True}
        fields.update(extra)
        return type("D", (), fields)()

    def sketch(self):
        return type("Sketch", (), {"DimensionConstraints": object()})()

    def transient(self):
        return type("TG", (), {"CreatePoint2d": staticmethod(lambda *a: a)})()

    def test_a_dimension_inventor_refuses_is_reported_not_raised(self):
        def refuse():
            raise RuntimeError("the dimension is redundant")

        backend = self.backend_with(refuse)
        outcome, note = backend._add_dimension(
            self.sketch(), self.transient(), {}, self.dimension())
        assert outcome == "refused"
        assert "redundant" in note

    def test_an_accepted_dimension_stores_its_expression(self):
        created = self.Created()
        backend = self.backend_with(lambda: created)
        outcome, _ = backend._add_dimension(
            self.sketch(), self.transient(), {}, self.dimension())
        assert outcome == "applied"
        assert created.Parameter.Expression == "base_len"

    def test_a_dimension_that_will_not_hold_its_expression_is_taken_back_out(self):
        """A frozen number has spent the degree of freedom and drives nothing."""
        created = self.Created(storable=False)
        backend = self.backend_with(lambda: created)
        outcome, note = backend._add_dimension(
            self.sketch(), self.transient(), {}, self.dimension())
        assert outcome == "refused"
        assert created.deleted is True
        assert "would not store" in note

    def test_one_that_will_neither_hold_nor_be_removed_is_fatal(self):
        """A frozen number silently standing in the model is the worst case."""
        from inventor_mcp.errors import SketchError

        created = self.Created(storable=False, deletable=False)
        backend = self.backend_with(lambda: created)
        with pytest.raises(SketchError, match="frozen number"):
            backend._add_dimension(self.sketch(), self.transient(), {}, self.dimension())

    def test_a_planner_bug_still_raises(self):
        """An unsupported kind is our fault, not Inventor's judgement."""
        from inventor_mcp.errors import SketchError

        def unsupported():
            raise SketchError("Unsupported dimension 'spiral'.")

        backend = self.backend_with(unsupported)
        with pytest.raises(SketchError, match="Unsupported"):
            backend._add_dimension(self.sketch(), self.transient(), {}, self.dimension())

    def test_the_recipes_own_dimensions_go_first(self):
        """So an author's dimension claims its degree of freedom before a rail."""
        import inspect

        source = inspect.getsource(com.ComBackend.build_sketch)
        assert 'if not getattr(d, "optional", False)' in source
        assert "for dimension in required:" in source
        assert source.index("for dimension in required:") < source.index(
            "for dimension in optional:")

    def test_a_required_refusal_is_still_fatal(self):
        import inspect

        source = inspect.getsource(com.ComBackend.build_sketch)
        required = source[source.index("for dimension in required:"):
                          source.index("for dimension in optional:")]
        assert "raise SketchError" in required

    def test_the_profile_check_no_longer_needs_something_to_have_been_refused(self):
        """A dimension the solver acted on can break a loop with nothing refused."""
        import inspect

        source = inspect.getsource(com.ComBackend.build_sketch)
        assert "if profiles == 0 and profile_loops(plan):" in source


class TestDisputedEnumValues:
    """An unverified fallback must not be used where it is known to be disputed.

    The table has never been exercised: on every machine this has run on the
    type library was readable and won, so it was never consulted and never
    checked. Two of its regions are contradicted by another project's published
    2026 field notes -- the dimension orientations and the surface types. A
    wrong value there is the quiet kind of wrong: an aligned dimension where a
    horizontal one was meant, with nothing to see but a part that is subtly not
    the part that was asked for.
    """

    def test_the_disputed_names_are_recorded_with_what_disputes_them(self):
        from inventor_mcp.backend.com.constants import FALLBACK, SUSPECT

        assert SUSPECT, "the point is to name them, not to carry a silent risk"
        for name, why in SUSPECT.items():
            assert name in FALLBACK, f"{name} is disputed but not in the table"
            assert any(char.isdigit() for char in why), (
                f"{name}'s note should say what value is claimed instead")

    def test_a_disputed_value_refuses_rather_than_guessing(self):
        from inventor_mcp.backend.com.constants import SUSPECT, Constants
        from inventor_mcp.errors import BackendUnavailableError

        constants = Constants(None)  # no type library, so the table is all there is
        for name in SUSPECT:
            with pytest.raises(BackendUnavailableError, match="disputed"):
                constants.resolve(name)

    def test_the_refusal_says_how_to_settle_it(self):
        from inventor_mcp.backend.com.constants import Constants
        from inventor_mcp.errors import BackendUnavailableError

        with pytest.raises(BackendUnavailableError) as raised:
            Constants(None).resolve("kHorizontalDim")
        assert "dump_constants" in (raised.value.hint or "")
        assert "gen_py" in (raised.value.hint or "")

    def test_an_undisputed_value_still_works_from_the_table(self):
        """Refusing everything would make a broken cache fatal for no reason."""
        from inventor_mcp.backend.com.constants import Constants

        assert Constants(None).resolve("kJoinOperation") == 20481

    def test_the_type_library_is_still_preferred_over_the_table(self):
        from inventor_mcp.backend.com.constants import Constants

        class Library:
            kHorizontalDim = 19201

        constants = Constants(Library())
        assert constants.resolve("kHorizontalDim") == 19201, (
            "a measured value beats the table, disputed or not")

    def test_using_the_table_at_all_warns_once(self, caplog):
        import logging

        from inventor_mcp.backend.com.constants import Constants

        constants = Constants(None)
        with caplog.at_level(logging.WARNING):
            constants.resolve("kJoinOperation")
            constants.resolve("kJoinOperation")
        assert sum("kJoinOperation" in r.message for r in caplog.records) == 1
