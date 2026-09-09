r"""Ask Inventor which translator add-ins it has, and what their ClassIds are.

`EXPORT_TRANSLATORS` in `inventor_mcp/backend/base.py` was the one table in
this project that was **neither measured nor quoted from a page in the tree**.
This is the run that measured it, and **three of its seven entries were
wrong**: `dwg` and `dxf` held each other's GUIDs, and `iges` held one no add-in
on 2027.1 has at all.

The swap is why this file is kept rather than deleted after one good run. A
wrong GUID that matches nothing is caught by the arrangement around the
table -- `ItemById` raises, and `export` falls back to `Document.SaveAs` with
a note. **Two valid GUIDs in each other's slots are not**: they resolve
happily and write a DWG where a DXF was asked for. Only a listing catches
that, and only a listing catches it again after a release changes something.

It prints, for every add-in Inventor has loaded:

* its own `ClassIdString`, which is the value the table should hold;
* its `DisplayName`, which is what the table deliberately does *not* match on,
  because the name is localised and the GUID is not -- the reference says so,
  and it is the opposite of what `ARCHITECTURE.md` assumed when it chose
  `SaveAs`;
* whether it is `Activated`, since a present-but-dormant translator is a
  different problem from an absent one and only Inventor can tell them apart;
* and, for the ones the table names, whether `ItemById` on our GUID finds that
  same add-in.

Then, for each translator the table claims, it asks
`HasSaveCopyAsOptions(document, context, map)` against a scratch part and
**prints the option names the translator itself puts in the map**. That is the
half `EXPORT_OPTIONS` cannot get from a page: the reference extraction recorded
three names, every translator publishes more, and a name is added to that
whitelist when it has been read rather than when it seems likely. Whatever this
prints is a reading, and it can go straight in.

Nothing is written and nothing is saved. Run it, paste the output back.

    python scripts/probe_translators.py
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apartment import on_thread, raw  # noqa: E402
from inventor_mcp.backend.base import (  # noqa: E402
    EXPORT_EXTENSIONS,
    EXPORT_OPTIONS,
    EXPORT_TRANSLATORS,
)
from inventor_mcp.builder import apply_operation  # noqa: E402
from inventor_mcp.schema import Operation  # noqa: E402
from inventor_mcp.session import Session  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402

_OPERATION = TypeAdapter(Operation)

#: Something with a solid in it, because `HasSaveCopyAsOptions` is asked about a
#: document and a translator may answer differently for an empty one.
PLATE = [
    _OPERATION.validate_python(
        {"op": "sketch", "name": "S", "plane": "xy", "entities": [
            {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]}),
    _OPERATION.validate_python(
        {"op": "extrude", "name": "Plate", "sketch": "S", "distance": 10}),
]


def _text(value: Any) -> str:
    try:
        return str(value)
    except Exception as exc:
        return f"<unreadable: {type(exc).__name__}: {exc}>"


def list_add_ins(inner: Any) -> dict[str, str]:
    """Every add-in Inventor has, and a ClassId-to-name index of them."""
    app = inner._require_app()
    add_ins = app.ApplicationAddIns
    count = int(add_ins.Count)
    print(f"\n--- {count} add-ins loaded")
    index: dict[str, str] = {}
    for position in range(1, count + 1):
        add_in = add_ins.Item(position)
        class_id = _text(getattr(add_in, "ClassIdString", "<no ClassIdString>"))
        name = _text(getattr(add_in, "DisplayName", "<no DisplayName>"))
        try:
            active = bool(add_in.Activated)
        except Exception:
            active = False
        index[class_id.upper()] = name
        # Only the translators are worth the line; there are dozens of others.
        if "translat" in name.lower() or "export" in name.lower() or class_id.upper() in {
                guid.upper() for guid in EXPORT_TRANSLATORS.values()}:
            print(f"    {class_id}  {'active ' if active else 'dormant'}  {name}")
    return index


def check_the_table(inner: Any, index: dict[str, str]) -> None:
    """Whether each GUID in `EXPORT_TRANSLATORS` finds the add-in it should."""
    app = inner._require_app()
    print(f"\n--- the {len(EXPORT_TRANSLATORS)} GUIDs this server carries")
    for fmt, guid in sorted(EXPORT_TRANSLATORS.items()):
        known = index.get(guid.upper())
        try:
            found = app.ApplicationAddIns.ItemById(guid)
            name = _text(getattr(found, "DisplayName", "<no DisplayName>"))
            print(f"    {fmt:5} {guid}  ItemById -> {name}")
        except Exception as exc:
            print(f"    {fmt:5} {guid}  ItemById raises: "
                  f"{type(exc).__name__}: {exc}")
            if known:
                print(f"          ...but the listing above has that ClassId as "
                      f"{known!r}, so the GUID is right and something else is wrong")
            else:
                print("          ...and nothing in the listing has that ClassId, "
                      "so the table's value is wrong for this release. Take the "
                      "right one from the listing above.")


def probe_options(inner: Any, doc_id: str, dynamic: Any) -> None:
    """What each translator puts in a `NameValueMap` when asked for its options.

    The reading `EXPORT_OPTIONS` needs: it holds three names off a page, and
    every one of these translators publishes more. `HasSaveCopyAsOptions`
    populates the map with the translator's own defaults, so the names *and*
    the current values come back together -- which also says what a file
    exported without options would have come out as.
    """
    app = inner._require_app()
    document = inner._doc(doc_id)
    transient = app.TransientObjects
    print("\n--- what each translator says its SaveCopyAs options are")
    for fmt, guid in sorted(EXPORT_TRANSLATORS.items()):
        offered_here = sorted(EXPORT_OPTIONS.get(fmt, {}))
        print(f"\n    {fmt} ({EXPORT_EXTENSIONS.get(fmt, '?')}), "
              f"this server offers {offered_here or 'nothing'}")
        try:
            translator = app.ApplicationAddIns.ItemById(guid)
        except Exception as exc:
            print(f"        not reachable: {type(exc).__name__}: {exc}")
            continue
        # Through dynamic dispatch: `ItemById` is declared as returning an
        # `ApplicationAddIn`, so the makepy wrapper has none of
        # `TranslatorAddIn`'s members and the 2026-09-09 run answered
        # `AttributeError: ... has no attribute 'HasSaveCopyAsOptions'` for
        # all seven. The object is a translator; the declared type is not.
        translator = dynamic(translator)
        try:
            if not bool(translator.Activated):
                translator.Activate()
                print("        (it was dormant; activated)")
        except Exception as exc:
            print(f"        would not activate: {type(exc).__name__}: {exc}")
            continue
        context = transient.CreateTranslationContext()
        context.Type = inner._k("kFileBrowseIOMechanism")
        settings = transient.CreateNameValueMap()
        try:
            has = bool(translator.HasSaveCopyAsOptions(document, context, settings))
        except Exception as exc:
            print(f"        HasSaveCopyAsOptions raises: {type(exc).__name__}: {exc}")
            continue
        count = int(settings.Count)
        print(f"        HasSaveCopyAsOptions -> {has}, and it filled in {count}")
        for position in range(1, count + 1):
            name = _text(settings.Name(position))
            try:
                value = _text(settings.Value(name))
            except Exception as exc:
                value = f"<unreadable: {type(exc).__name__}>"
            mark = "  <- offered" if name in offered_here else ""
            print(f"          {name} = {value}{mark}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args(argv)

    session = Session(backend_kind="inventor")
    backend = session.ensure_backend()
    info = backend.connect(visible=True, create=True)
    print("=" * 70)
    print(f"Inventor {info.version} -- probing the translator add-ins")
    print("=" * 70)

    document = backend.new_part("TranslatorProbe", units="mm")
    context = session.register(document, "mm", "deg")
    for op in PLATE:
        apply_operation(session, context, op)

    def everything() -> None:
        """All of it on the apartment. Nothing live leaves this function."""
        from inventor_mcp.backend.com.backend import _dynamic

        inner = raw(backend)
        # Each section separately, so one exception does not throw away what
        # the others already learned -- the lesson `probe_definitions.py`
        # records, and repeated once since with a bad attribute in its own
        # formatting.
        index: dict[str, str] = {}
        for label, work in (
                ("the add-in listing", lambda: index.update(list_add_ins(inner))),
                ("the GUID table", lambda: check_the_table(inner, index)),
                ("the option names",
                 lambda: probe_options(inner, context.doc_id, _dynamic))):
            try:
                work()
            except Exception:
                print(f"\n!!! probing {label} failed; the rest still follows")
                traceback.print_exc(limit=6, file=sys.stdout)

    on_thread(backend, everything)

    print("\n" + "=" * 70)
    if not args.keep_open:
        backend.close_document(context.doc_id, save=False)
        session.forget(context.doc_id)
        print("The scratch part is closed and nothing was saved.")
    else:
        print("Left open, as asked.")
    print("Paste all of the above back. The two things it settles are whether "
          "the seven GUIDs are right on this release, and which option names "
          "each translator really takes -- the second replaces a three-name "
          "whitelist read off a page with a measurement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
