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

from pathlib import Path

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


class TestPappus:
    """A revolved profile's volume, which is where the pulley's 2.4 cm^3 went."""

    def test_the_centroid_is_the_area_centroid_not_the_box_centre(self):
        """A triangle's centroid is a third of the way in, not half."""
        from inventor_mcp.geometry import polygon_centroid

        triangle = [(4.1, 0.25), (3.5, 0.8), (4.1, 1.35)]
        centre, _ = polygon_centroid(triangle)
        assert centre == pytest.approx(3.9)          # (4.1 + 3.5 + 4.1) / 3
        box_centre = (3.5 + 4.1) / 2
        assert centre != pytest.approx(box_centre)   # 3.8: what it used to use

    def test_a_rectangle_is_the_case_where_both_agree(self):
        from inventor_mcp.geometry import polygon_centroid

        assert polygon_centroid(
            [(0.6, 0.0), (4.0, 0.0), (4.0, 1.6), (0.6, 1.6)]) == pytest.approx((2.3, 0.8))

    def test_a_degenerate_polygon_has_no_centroid(self):
        from inventor_mcp.geometry import polygon_centroid

        assert polygon_centroid([(0, 0), (1, 1)]) is None
        assert polygon_centroid([(0, 0), (1, 0), (2, 0)]) is None

    def test_clipping_trims_the_overshoot_and_nothing_else(self):
        from inventor_mcp.geometry import clip_to_box, polygon_centroid

        triangle = [(4.1, 0.25), (3.5, 0.8), (4.1, 1.35)]
        clipped = clip_to_box(triangle, -9.9, 4.0, -9.9, 9.9)
        assert len(clipped) == 3
        assert max(u for u, _ in clipped) == pytest.approx(4.0)
        assert polygon_centroid(clipped)[0] == pytest.approx(4.0 - 0.5 / 3)

    def test_a_polygon_wholly_inside_is_untouched(self):
        from inventor_mcp.geometry import clip_to_box

        square = [(1.0, 1.0), (2.0, 1.0), (2.0, 2.0), (1.0, 2.0)]
        assert clip_to_box(square, 0, 3, 0, 3) == square

    def test_a_polygon_wholly_outside_clips_to_nothing(self):
        from inventor_mcp.geometry import clip_to_box

        assert clip_to_box([(5.0, 5.0), (6.0, 5.0), (6.0, 6.0)], 0, 1, 0, 1) == []

    def test_a_revolved_groove_removes_only_what_is_there(self, session):
        """The pulley: a groove drawn past the rim removes the rim, not the air."""
        import math

        blank = math.pi * (4.0**2 - 0.6**2) * 1.6
        volume = build(session, [
            {"op": "sketch", "name": "Section", "plane": "xz", "entities": [
                {"type": "polyline", "closed": True, "points": [
                    [6, 0], [40, 0], [40, 16], [6, 16]]}]},
            {"op": "revolve", "name": "Blank", "sketch": "Section", "axis": "z"},
            {"op": "sketch", "name": "Groove", "plane": "xz", "entities": [
                {"type": "polyline", "closed": True, "points": [
                    [41, 2.5], [35, 8], [41, 13.5]]}]},
            {"op": "revolve", "name": "VGroove", "sketch": "Groove", "axis": "z",
             "operation": "cut"},
        ])
        # The clipped triangle: base 9.1667 mm at r = 40, apex 5 mm deep.
        clipped_area = 0.91667 * 0.5 / 2
        groove = clipped_area * (4.0 - 0.5 / 3) * 2 * math.pi
        assert volume == pytest.approx(blank - groove, rel=1e-4)

    def test_the_shipped_pulley_matches_the_hand_calculation(self, session):
        """Which is what `examples/expected/belt_pulley.json` records."""
        import json

        root = Path(__file__).resolve().parent.parent
        recipe = PartRecipe.model_validate(
            json.loads((root / "examples" / "belt_pulley.json").read_text()))
        result = build_part(session, recipe)
        assert result["ok"], result["errors"]
        expected = json.loads(
            (root / "examples" / "expected" / "belt_pulley.json").read_text())
        measured = session.backend.mass_properties(result["document"]).volume
        assert measured == pytest.approx(expected["volume_cm3"], abs=5e-6)


class TestSeparateProfilesInOneSketch:
    """Four bosses are four bosses, not one boss with three holes in it."""

    def test_disjoint_circles_all_add_material(self, session):
        area = 4 * math.pi * 0.35**2
        volume = build(session, [
            {"op": "sketch", "name": "Pads", "plane": "xy", "entities": [
                {"type": "circle", "center": [x, y], "diameter": 7}
                for x in (-20, 20) for y in (-10, 10)]},
            {"op": "extrude", "sketch": "Pads", "distance": 10},
        ])
        assert volume == pytest.approx(area * 1.0, rel=1e-6)

    def test_a_hole_inside_a_profile_still_takes_area_away(self, session):
        """The case the old rule got right, which the new one must not break."""
        volume = build(session, [
            {"op": "sketch", "name": "Washer", "plane": "xy", "entities": [
                {"type": "circle", "center": [0, 0], "diameter": 40},
                {"type": "circle", "center": [0, 0], "diameter": 20}]},
            {"op": "extrude", "sketch": "Washer", "distance": 5},
        ])
        assert volume == pytest.approx(math.pi * (2.0**2 - 1.0**2) * 0.5, rel=1e-9)

    def test_a_boss_inside_a_pocket_inside_a_plate_counts_once(self, session):
        """Even-odd: depth 0 adds, depth 1 subtracts, depth 2 adds again."""
        volume = build(session, [
            {"op": "sketch", "name": "Nested", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 40, "height": 40},
                {"type": "circle", "center": [0, 0], "diameter": 20},
                {"type": "circle", "center": [0, 0], "diameter": 6}]},
            {"op": "extrude", "sketch": "Nested", "distance": 10},
        ])
        area = 4.0 * 4.0 - math.pi * 1.0**2 + math.pi * 0.3**2
        assert volume == pytest.approx(area * 1.0, rel=1e-9)

    def test_the_enclosure_builds_the_bosses_it_names(self, session):
        import json

        root = Path(__file__).resolve().parent.parent
        recipe = PartRecipe.model_validate(
            json.loads((root / "examples" / "enclosure_base.json").read_text()))
        result = build_part(session, recipe)
        assert result["ok"], result["errors"]
        bosses = [op for op in result["operations"] if op.get("name") == "Bosses"]
        assert bosses, "the recipe should build them, not just name their holes"
        assert bosses[0]["measured"]["volume_change_cm3"] == pytest.approx(
            4 * math.pi * 0.35**2 * 2.75, rel=1e-5)


HOLLOW_BOX = [
    {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]},
    {"op": "extrude", "name": "Body", "sketch": "Outline", "distance": 20},
    {"op": "shell", "name": "Hollow", "thickness": 2.5,
     "faces": {"kind": "face", "filter": "top"}},
    {"op": "sketch", "name": "Route", "plane": "yz", "entities": [
        {"type": "point", "position": [0, 10]}]},
]

SOLID_PLATE = [
    {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]},
    {"op": "extrude", "name": "Body", "sketch": "Outline", "distance": 10},
    {"op": "sketch", "name": "Pilot", "plane": "xy", "entities": [
        {"type": "point", "position": [0, 0]}]},
]


def warnings_for(operations: list[dict]) -> list[str]:
    from inventor_mcp.rehearsal import rehearse

    report = rehearse(PartRecipe.model_validate(
        {"name": "T", "units": "mm", "operations": operations}))
    return [entry["warning"] for entry in report["warnings"]]


class TestAThroughHoleThatDrillsTheNearWallOnly:
    """Defect 1: Inventor's through-all extent stops where it first leaves
    material, so a hole across a hollow box drills one wall and the part looks
    built. Nothing in the volumes says so. The count of material pieces the
    drill axis crosses does, and the simulator has that count already.
    """

    def test_a_hole_across_a_hollow_box_is_warned_about(self):
        told = warnings_for(HOLLOW_BOX + [
            {"op": "hole", "name": "Cable", "sketch": "Route",
             "diameter": 6, "through_all": True}])
        assert any("crosses 2 walls" in entry for entry in told), told

    def test_the_warning_names_the_substitute_that_works(self):
        from inventor_mcp.rehearsal import rehearse

        report = rehearse(PartRecipe.model_validate({
            "name": "T", "units": "mm", "operations": HOLLOW_BOX + [
                {"op": "hole", "name": "Cable", "sketch": "Route",
                 "diameter": 6, "through_all": True}]}))
        why = next(entry["why"] for entry in report["warnings"]
                   if "walls" in entry["warning"])
        assert "symmetric" in why

    def test_an_ordinary_through_hole_says_nothing(self):
        """A warning that fires on a correct recipe teaches the reader to skip
        the field, so the quiet case matters as much as the loud one."""
        assert warnings_for(SOLID_PLATE + [
            {"op": "hole", "name": "Bolt", "sketch": "Pilot",
             "diameter": 6, "through_all": True}]) == []

    def test_a_blind_hole_says_nothing_even_across_a_hollow(self):
        """A blind hole was never going to reach the far wall, and does not
        claim to. Only `through_all` makes the promise this breaks."""
        assert warnings_for(HOLLOW_BOX + [
            {"op": "hole", "name": "Cable", "sketch": "Route",
             "diameter": 6, "depth": 2}]) == []

    def test_the_wall_count_is_recorded_on_the_feature(self, session):
        out = build_part(session, PartRecipe.model_validate({
            "name": "T", "units": "mm", "operations": HOLLOW_BOX + [
                {"op": "hole", "name": "Cable", "sketch": "Route",
                 "diameter": 6, "through_all": True}]}))
        hole = next(s for s in out["operations"] if s["op"] == "hole")
        assert hole["detail"]["walls_on_the_axis"] == 2

    def test_a_solid_part_records_one_wall(self, session):
        out = build_part(session, PartRecipe.model_validate({
            "name": "T", "units": "mm", "operations": SOLID_PLATE + [
                {"op": "hole", "name": "Bolt", "sketch": "Pilot",
                 "diameter": 6, "through_all": True}]}))
        hole = next(s for s in out["operations"] if s["op"] == "hole")
        assert hole["detail"]["walls_on_the_axis"] == 1

    def test_no_shipped_example_triggers_it(self):
        """Eleven real parts, and the enclosure among them -- the part defect 1
        was found on, which has since been built with the substitute. A false
        positive here would be the warning's own undoing.
        """
        import json

        for path in sorted(Path(__file__).resolve().parent.parent.glob("examples/*.json")):
            from inventor_mcp.rehearsal import rehearse

            report = rehearse(PartRecipe.model_validate(json.loads(path.read_text())))
            offending = [entry for entry in report["warnings"] if "walls" in entry["warning"]]
            assert offending == [], f"{path.name}: {offending}"
