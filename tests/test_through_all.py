"""How much material a through-all feature passes through.

The simulator used to charge a through cut for the body's whole span along the
cut axis. For a plate that is right; for anything else it is not, and the angle
bracket -- 90 mm tall with a 6 mm base -- had its base slots charged 90 mm. The
part came out 16 cm^3 light, and because a mirror repeats what its seed did, the
mirror of that cut doubled the error.

The fix measures the material over the profile from the prisms that built the
part. These tests are about the cases where a bounding box and the geometry
disagree, because those are the only cases where any of it matters.
"""

from __future__ import annotations

import math

import pytest

from inventor_mcp.backend.mock.backend import _Slab, _through_all_distance
from inventor_mcp.builder import build_part
from inventor_mcp.schema import PartRecipe
from inventor_mcp.session import Session


@pytest.fixture
def session() -> Session:
    session = Session(backend_kind="mock")
    session.ensure_backend().connect()
    return session


def build(session: Session, operations: list[dict]) -> float:
    result = build_part(session, PartRecipe.model_validate(
        {"name": "T", "units": "mm", "operations": operations}))
    assert result["ok"], result["errors"]
    return session.backend.mass_properties(result["document"]).volume


#: A 60 x 40 x 8 plate: a prism, where the span is the right answer anyway.
PLATE = [
    {"op": "sketch", "name": "Body", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]},
    {"op": "extrude", "name": "Plate", "sketch": "Body", "distance": 8},
]

#: An L: a 60 x 6 base with a 40 mm upright at x = 0, extruded 30 mm along Y.
#: Its bounding box is 40 mm deep in Z where the base is 6, which is the whole
#: point of the exercise.
L_SECTION = [
    {"op": "sketch", "name": "Section", "plane": "xz", "entities": [
        {"type": "polyline", "closed": True, "points": [
            [0, 0], [60, 0], [60, 6], [6, 6], [6, 40], [0, 40]]}]},
    {"op": "extrude", "name": "Body", "sketch": "Section", "distance": 30},
]


class TestAPlateIsUnaffected:
    def test_a_through_hole_removes_the_plate_thickness(self, session):
        volume = build(session, PLATE + [
            {"op": "sketch", "name": "C", "plane": "xy", "entities": [
                {"type": "point", "position": [20, 0]}]},
            {"op": "hole", "sketch": "C", "diameter": 10, "through_all": True},
        ])
        assert volume == pytest.approx(6 * 4 * 0.8 - math.pi * 0.5**2 * 0.8, rel=1e-9)

    def test_a_through_cut_removes_the_plate_thickness(self, session):
        volume = build(session, PLATE + [
            {"op": "sketch", "name": "Slot", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 10, "height": 10}]},
            {"op": "extrude", "sketch": "Slot", "extent": "through_all",
             "operation": "cut"},
        ])
        assert volume == pytest.approx(6 * 4 * 0.8 - 1.0 * 1.0 * 0.8, rel=1e-9)


class TestAnLSectionIsWhereItMatters:
    def test_a_cut_through_the_base_is_charged_the_base(self, session):
        """Not the 40 mm the bounding box is deep."""
        section = 6.0 * 0.6 + 0.6 * 3.4  # cm^2: the base plus the upright above it
        body = section * 3.0
        cut = 1.0 * 1.0 * 0.6  # a 10 x 10 mm hole through 6 mm of base
        volume = build(session, L_SECTION + [
            {"op": "sketch", "name": "Slot", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [40, 0], "width": 10, "height": 10}]},
            {"op": "extrude", "sketch": "Slot", "extent": "through_all",
             "operation": "cut"},
        ])
        assert volume == pytest.approx(body - cut, rel=1e-6)

    def test_a_hole_through_the_base_is_too(self, session):
        section = 6.0 * 0.6 + 0.6 * 3.4
        volume = build(session, L_SECTION + [
            {"op": "sketch", "name": "C", "plane": "xy", "entities": [
                {"type": "point", "position": [40, 0]}]},
            {"op": "hole", "sketch": "C", "diameter": 8, "through_all": True},
        ])
        assert volume == pytest.approx(section * 3.0 - math.pi * 0.4**2 * 0.6, rel=1e-6)

    def test_a_cut_through_the_upright_gets_the_upright(self, session):
        """Same part, different place, different thickness: 6 mm along X."""
        section = 6.0 * 0.6 + 0.6 * 3.4
        volume = build(session, L_SECTION + [
            {"op": "sketch", "name": "C", "plane": "yz", "entities": [
                {"type": "point", "position": [0, 25]}]},
            {"op": "hole", "sketch": "C", "diameter": 8, "through_all": True},
        ])
        assert volume == pytest.approx(section * 3.0 - math.pi * 0.4**2 * 0.6, rel=1e-6)

    def test_the_mirrored_cut_removes_the_same_again(self, session):
        section = 6.0 * 0.6 + 0.6 * 3.4
        cut = 1.0 * 1.0 * 0.6
        volume = build(session, L_SECTION + [
            {"op": "sketch", "name": "Slot", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [40, 10], "width": 10, "height": 10}]},
            {"op": "extrude", "name": "SlotCut", "sketch": "Slot",
             "extent": "through_all", "operation": "cut"},
            {"op": "mirror", "features": ["SlotCut"], "plane": "xz"},
        ])
        assert volume == pytest.approx(section * 3.0 - 2 * cut, rel=1e-6)


class TestWhatItRefusesToGuess:
    def test_a_revolved_part_falls_back_to_the_span(self, session):
        """No prism was recorded, so the old answer stands rather than a worse one."""
        operations = [
            {"op": "sketch", "name": "P", "plane": "xz", "entities": [
                {"type": "rectangle", "corner": [0, 0], "width": 20, "height": 10}]},
            {"op": "revolve", "name": "Disc", "sketch": "P", "axis": "z"},
        ]
        result = build_part(session, PartRecipe.model_validate(
            {"name": "R", "units": "mm", "operations": operations}))
        document = session.backend._doc(result["document"])
        assert document.slabs == []
        span = document.bounds[5] - document.bounds[2]
        assert _through_all_distance(document, "xy", over=(0.0, 0.0, 0.0)) == pytest.approx(span)

    def test_a_point_no_prism_covers_falls_back_too(self, session):
        build(session, L_SECTION)
        document = session.backend._doc(session.active)
        # Well outside the L in Y: the extrusion runs 0..30 mm.
        far = _through_all_distance(document, "xy", over=(4.0, 20.0, 0.0))
        assert far == pytest.approx(document.bounds[5] - document.bounds[2])

    def test_without_a_point_it_is_the_span(self, session):
        build(session, L_SECTION)
        document = session.backend._doc(session.active)
        assert _through_all_distance(document, "xy") == pytest.approx(
            document.bounds[5] - document.bounds[2])


class TestTheSlabItself:
    """The two cases in `interval_along`, isolated from any recipe."""

    def slab(self) -> _Slab:
        # A 4 x 1 cm bar on xy, swept 0..2 cm along +Z.
        return _Slab(plane="xy", outline=[(0.0, 0.0), (4.0, 0.0), (4.0, 1.0), (0.0, 1.0)],
                     near=0.0, far=2.0)

    def test_along_the_sweep_it_is_the_sweep(self):
        assert self.slab().interval_along(2, (2.0, 0.5, 0.0)) == (0.0, 2.0)

    def test_outside_the_profile_it_covers_nothing(self):
        assert self.slab().interval_along(2, (9.0, 0.5, 0.0)) is None

    def test_across_the_profile_it_is_the_profile(self):
        assert self.slab().interval_along(0, (2.0, 0.5, 1.0)) == (0.0, 4.0)
        assert self.slab().interval_along(1, (2.0, 0.5, 1.0)) == (0.0, 1.0)

    def test_beyond_the_sweep_it_covers_nothing(self):
        assert self.slab().interval_along(0, (2.0, 0.5, 5.0)) is None
