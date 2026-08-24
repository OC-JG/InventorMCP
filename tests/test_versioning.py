"""Naming and making the next version of a part file.

The rule that matters most is the one about never overwriting. Two loop runs an
hour apart would otherwise land on the same name and the second would destroy
the first -- including a copy somebody had already reviewed. Everything else here
is about a version sequence reading as one: matching separator, case and padding,
because `bracket_v002` next to `bracket_v3` sorts wrong in every file browser.
"""

from __future__ import annotations

import pytest

from inventor_mcp.versioning import (
    next_version, sidecar_for, split_version, versions_of, working_copy,
)


class TestReadingAVersion:
    @pytest.mark.parametrize("stem, base, number", [
        ("bracket", "bracket", 1),
        ("bracket_v2", "bracket", 2),
        ("bracket-v7", "bracket", 7),
        ("bracket.v10", "bracket", 10),
        ("bracket V4", "bracket", 4),
        ("bracket_v002", "bracket", 2),
    ])
    def test_it_finds_the_version(self, stem, base, number):
        found_base, _, found_number, _ = split_version(stem)
        assert (found_base, found_number) == (base, number)

    def test_an_unversioned_name_is_version_one(self):
        """So the copy of it is version two, which is what a person would call it."""
        assert split_version("bracket")[2] == 1

    @pytest.mark.parametrize("stem", ["bracket_v", "v2", "bracket_2", "bracketv2"])
    def test_what_is_not_a_version_is_left_alone(self, stem):
        base, _, number, _ = split_version(stem)
        assert base == stem and number == 1


class TestNamingTheNextOne:
    def test_it_counts_up(self, tmp_path):
        part = tmp_path / "bracket_v2.ipt"
        part.write_bytes(b"")
        assert next_version(part).name == "bracket_v3.ipt"

    def test_it_keeps_the_separator(self, tmp_path):
        part = tmp_path / "bracket-v2.ipt"
        part.write_bytes(b"")
        assert next_version(part).name == "bracket-v3.ipt"

    def test_and_the_case(self, tmp_path):
        part = tmp_path / "bracket_V2.ipt"
        part.write_bytes(b"")
        assert next_version(part).name == "bracket_V3.ipt"

    def test_and_the_padding(self, tmp_path):
        """v003 beside v4 sorts wrong, and a version list that sorts wrong is
        one somebody reads in the wrong order."""
        part = tmp_path / "bracket_v002.ipt"
        part.write_bytes(b"")
        assert next_version(part).name == "bracket_v003.ipt"

    def test_and_the_extension(self, tmp_path):
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"")
        assert next_version(part).suffix == ".ipt"

    def test_it_skips_a_name_that_is_taken(self, tmp_path):
        (tmp_path / "bracket.ipt").write_bytes(b"")
        (tmp_path / "bracket_v2.ipt").write_bytes(b"")
        (tmp_path / "bracket_v3.ipt").write_bytes(b"")
        assert next_version(tmp_path / "bracket.ipt").name == "bracket_v4.ipt"

    def test_and_one_that_is_only_reserved(self, tmp_path):
        """Planning two copies in one breath must not plan them onto one name."""
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"")
        first = next_version(part)
        second = next_version(part, taken={first})
        assert first != second

    def test_it_touches_nothing(self, tmp_path):
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"original")
        next_version(part)
        assert part.read_bytes() == b"original"
        assert list(tmp_path.iterdir()) == [part]


class TestMakingTheCopy:
    def test_the_copy_is_the_next_version(self, tmp_path):
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"geometry")
        assert working_copy(part).name == "bracket_v2.ipt"

    def test_with_the_same_content(self, tmp_path):
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"geometry")
        assert working_copy(part).read_bytes() == b"geometry"

    def test_and_the_original_is_untouched(self, tmp_path):
        """The whole reason for a copy. A filesystem copy cannot modify what it
        copies, where opening the file and saving it elsewhere leaves a window
        in which it could."""
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"geometry")
        working_copy(part)
        assert part.read_bytes() == b"geometry"

    def test_the_declaration_travels_with_it(self, tmp_path):
        """Or the copy arrives having forgotten which parameter is the wall and
        which dimensions are not to be touched."""
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"geometry")
        sidecar_for(part).write_text('{"roles": {"wall": "wall_t"}}', encoding="utf-8")
        copy = working_copy(part)
        assert sidecar_for(copy).is_file()
        assert "wall_t" in sidecar_for(copy).read_text(encoding="utf-8")

    def test_no_declaration_is_not_an_error(self, tmp_path):
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"geometry")
        copy = working_copy(part)
        assert copy.is_file() and not sidecar_for(copy).exists()

    def test_a_named_destination_is_honoured(self, tmp_path):
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"geometry")
        wanted = tmp_path / "elsewhere" / "named.ipt"
        assert working_copy(part, destination=wanted) == wanted
        assert wanted.is_file()

    def test_it_refuses_to_overwrite(self, tmp_path):
        part = tmp_path / "bracket.ipt"
        part.write_bytes(b"new")
        target = tmp_path / "taken.ipt"
        target.write_bytes(b"somebody reviewed this")
        with pytest.raises(FileExistsError):
            working_copy(part, destination=target)
        assert target.read_bytes() == b"somebody reviewed this"

    def test_a_missing_original_says_so(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            working_copy(tmp_path / "nothing.ipt")


class TestListingVersions:
    def test_they_come_back_in_order(self, tmp_path):
        for name in ("bracket.ipt", "bracket_v2.ipt", "bracket_v3.ipt"):
            (tmp_path / name).write_bytes(b"")
        assert [p.name for p in versions_of(tmp_path / "bracket.ipt")] == [
            "bracket.ipt", "bracket_v2.ipt", "bracket_v3.ipt"]

    def test_numerically_rather_than_alphabetically(self, tmp_path):
        """v10 after v9, which sorting the strings gets backwards."""
        for name in ("bracket_v9.ipt", "bracket_v10.ipt"):
            (tmp_path / name).write_bytes(b"")
        assert [p.name for p in versions_of(tmp_path / "bracket_v9.ipt")] == [
            "bracket_v9.ipt", "bracket_v10.ipt"]

    def test_another_part_is_not_a_version_of_this_one(self, tmp_path):
        (tmp_path / "bracket.ipt").write_bytes(b"")
        (tmp_path / "bracket_housing.ipt").write_bytes(b"")
        assert [p.name for p in versions_of(tmp_path / "bracket.ipt")] == ["bracket.ipt"]

    def test_nor_is_the_same_name_with_another_extension(self, tmp_path):
        (tmp_path / "bracket.ipt").write_bytes(b"")
        (tmp_path / "bracket_v2.stp").write_bytes(b"")
        assert [p.name for p in versions_of(tmp_path / "bracket.ipt")] == ["bracket.ipt"]
