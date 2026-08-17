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
