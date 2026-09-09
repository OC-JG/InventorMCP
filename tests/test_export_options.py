"""Export through the translator add-ins, and the options `SaveAs` cannot reach.

`Document.SaveAs` hands the path to whichever translator claims the extension
and takes whatever that translator's last-used dialog settings were. So a STEP
file came out in whichever application protocol somebody last picked, a PDF at
whatever resolution, and nothing in the result said which.
`TranslatorAddIn.SaveCopyAs(document, context, options, data)` is the route that
takes settings, and the add-in is fetched by ClassId GUID -- **not** by display
name, which is localised, and which is the opposite of what `ARCHITECTURE.md`
assumed when it chose `SaveAs`.

Three things are checked here and they are different in kind.

The **whitelist** is a rule about the recipe: a `NameValueMap` silently ignores
a name the translator does not know, so a misspelled option is a file written
with the wrong settings and a result saying it worked. That is refused, above
both backends, so the simulator refuses what a live export would.

The **fallback** is a rule about not making things worse. The GUIDs in
`EXPORT_TRANSLATORS` are the one table in this project that is neither measured
nor quoted from a page in the tree, so a translator that cannot be found drops
back to `SaveAs` -- the measured route -- with a note. **Unless options were
asked for**, which is a hard error instead: a file quietly written with the
wrong settings is worse than no file, because the caller asked for AP 214 and
would have no reason to doubt what they got.

The **drift tests** are the third: three tables name formats, and a format in
one and not the others is either an option nobody can pass or a GUID for a
format the tool will not accept.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from inventor_mcp.backend.base import (
    EXPORT_EXTENSIONS,
    EXPORT_OPTIONS,
    EXPORT_TRANSLATORS,
    Backend,
    ExportRequest,
)
from inventor_mcp.backend.com import backend as com
from inventor_mcp.backend.mock.backend import MockBackend
from inventor_mcp.errors import ExportError


@pytest.fixture
def part(session):
    from inventor_mcp.builder import build_part
    from inventor_mcp.schema import PartRecipe

    out = build_part(session, PartRecipe.model_validate(
        {"name": "E", "units": "mm", "operations": [
            {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]},
            {"op": "extrude", "name": "Plate", "sketch": "S", "distance": 10}]}))
    assert out["ok"], out["errors"]
    return out["document"]


class TestTheOptionNamesAreAWhitelist:
    def test_a_known_name_is_taken(self, session, part):
        result = session.backend.export(part, ExportRequest(
            path="out.stp", format="step",
            options={"ApplicationProtocolType": 3}))
        assert result["options_applied"]["ApplicationProtocolType"] == 3

    def test_a_name_the_format_does_not_take_is_refused_with_the_list(
            self, session, part):
        """Not ignored, which is what Inventor would do. The message names what
        STEP does take, because a caller who guessed one name will guess
        another."""
        with pytest.raises(ExportError) as raised:
            session.backend.export(part, ExportRequest(
                path="out.stp", format="step", options={"Vector_Resolution": 600}))
        assert "does not take Vector_Resolution" in str(raised.value)
        assert "ApplicationProtocolType" in str(raised.value.hint)

    def test_a_format_with_no_options_at_all_says_so(self, session, part):
        """STL is deliberately not in the options table: the reference
        publishes no option names for it, so there is nothing to pass and
        pretending otherwise would be inventing them."""
        with pytest.raises(ExportError) as raised:
            session.backend.export(part, ExportRequest(
                path="out.stl", format="stl", options={"Resolution": "high"}))
        assert "No export options are known for 'stl'" in str(raised.value)
        assert "step" in str(raised.value.hint)

    def test_no_options_is_never_refused(self, session, part):
        """Every format still exports with nothing said about settings, which
        is what every caller before this did."""
        for fmt in sorted(EXPORT_EXTENSIONS):
            result = session.backend.export(part, ExportRequest(
                path=f"out.{fmt}", format=fmt))
            assert result["format"] == fmt
            assert "options_applied" not in result

    def test_an_unsupported_format_is_refused_by_both_backends(self, session, part):
        with pytest.raises(ExportError, match="Unsupported export format"):
            session.backend.export(part, ExportRequest(path="out.x", format="parasolid"))

    def test_the_whitelist_lives_above_both_backends(self):
        """So a rehearsal refuses the names a live export would. Two copies of
        a rule are one rule until the day one copy is edited."""
        assert MockBackend._checked_export_options is Backend._checked_export_options
        assert com.ComBackend._checked_export_options is Backend._checked_export_options


class TestTheTablesAgree:
    def test_every_translator_is_a_format_that_can_be_exported(self):
        unknown = sorted(set(EXPORT_TRANSLATORS) - set(EXPORT_EXTENSIONS))
        assert not unknown, (
            f"a translator GUID for {unknown}, which `export` will refuse "
            "before it ever looks for the add-in.")

    def test_every_option_belongs_to_a_format_that_has_a_translator(self):
        """An option name is only reachable through `SaveCopyAs`, so an entry
        for a format with no translator is a name nobody can pass."""
        stranded = sorted(set(EXPORT_OPTIONS) - set(EXPORT_TRANSLATORS))
        assert not stranded, (
            f"options are offered for {stranded} and no translator is recorded "
            "for them, so `export` would refuse every one of those options as "
            "unpassable.")

    def test_every_format_the_tool_offers_has_an_extension(self):
        """The tool's `Literal` and the extension table are two lists of
        formats, and the one a caller reads is the tool's."""
        from inventor_mcp.tools import inspection

        source = inspect.getsource(inspection)
        listed = source[source.index('Literal["step", "stl"'):]
        listed = listed[:listed.index("]")]
        offered = {name.strip().strip('"') for name in listed.split("[")[1].split(",")}
        assert offered <= set(EXPORT_EXTENSIONS), sorted(offered - set(EXPORT_EXTENSIONS))

    def test_the_guids_are_shaped_like_guids(self):
        """Cheap, and it is the one thing about them that can be checked from
        here: they have never been read off an installed Inventor, and
        `scripts/probe_translators.py` is the run that would."""
        import re

        for fmt, guid in EXPORT_TRANSLATORS.items():
            assert re.fullmatch(
                r"\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}",
                guid), (fmt, guid)
        assert len(set(EXPORT_TRANSLATORS.values())) == len(EXPORT_TRANSLATORS), \
            "two formats share a ClassId, so one of them fetches the wrong add-in"

    def test_the_probe_script_reads_all_three_tables(self):
        """It is what turns the GUID table from published into measured, and a
        probe that had stopped covering a table would leave that table
        unmeasurable without anybody noticing."""
        root = Path(__file__).resolve().parent.parent
        script = (root / "scripts" / "probe_translators.py").read_text(encoding="utf-8")
        for table in ("EXPORT_TRANSLATORS", "EXPORT_OPTIONS", "EXPORT_EXTENSIONS"):
            assert table in script, table


class TestTheComRouteIsTheDocumentedOne:
    def test_it_fetches_the_add_in_by_class_id(self):
        source = inspect.getsource(com.ComBackend._translator)
        assert "ApplicationAddIns.ItemById(guid)" in source

    def test_it_calls_save_copy_as_with_four_arguments(self):
        source = inspect.getsource(com.ComBackend._save_copy_as)
        assert "translator.SaveCopyAs(document, context, settings, medium)" in source
        assert "CreateTranslationContext()" in source
        assert "CreateNameValueMap()" in source
        assert "CreateDataMedium()" in source

    def test_an_option_goes_on_the_map_by_add_and_not_by_assignment(self):
        """`Value` is a parameterised property, and the VBA spelling for
        setting one has no Python equivalent through late binding: a call is
        not an assignment target. `Add` refuses a name the map already holds,
        and `HasSaveCopyAsOptions` fills the map with defaults first, so a
        name already present is removed by index and added again."""
        source = inspect.getsource(com._set_option)
        assert "settings.Add(name, value)" in source
        assert "settings.Remove(index)" in source
        assert "range(1, int(settings.Count) + 1)" in source, \
            "Inventor's collections are 1-based"

    def test_options_that_cannot_be_passed_are_an_error_and_not_a_note(self):
        """The whole point. A file written by `SaveAs` when AP 214 was asked
        for is a STEP file the caller has no reason to doubt."""
        source = inspect.getsource(com.ComBackend.export)
        raise_at = source.index("cannot be passed")
        fallback_at = source.index("document.SaveAs(path, True)")
        assert raise_at < fallback_at, (
            "the refusal has to come before the fallback, or the file is "
            "written and then complained about")

    def test_the_fallback_route_is_named_in_the_result(self, session, part):
        """`route` is what a caller reads to know whether their settings were
        Inventor's or theirs. The simulator answers it too, from the same
        table, so a rehearsal says which way a live export would go."""
        assert session.backend.export(
            part, ExportRequest(path="out.stp", format="step"))["route"] == "translator"
        # 3MF is the one format left with no translator: nothing in the
        # 2027.1 add-in listing exports it, where `stl` and `obj` both do and
        # joined the table off that listing on 2026-09-09.
        assert session.backend.export(
            part, ExportRequest(path="out.3mf", format="3mf"))["route"] == "SaveAs"
