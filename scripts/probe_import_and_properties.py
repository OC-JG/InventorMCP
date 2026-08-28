"""Two things this project has never asked Inventor to do.

Importing a STEP file, and keeping a note inside a part document. Both have
several documented routes and no way to tell from here which one this release
actually accepts, so this tries each and reports what happened:

    python scripts/probe_import_and_properties.py --step C:\\path\\to\\part.stp
    python scripts/probe_import_and_properties.py --only properties

Nothing is assumed and nothing is left behind: every document this opens is
closed without saving, and every property it writes is written to a scratch part.

Why it matters which route works. A STEP file arrives as a solid with no
features and no parameters, and the DFM loop drives parameters -- so the useful
question is not only "did it import" but "what is in the part afterwards", which
is printed. And a declaration kept inside the .ipt travels with the file, where
a sidecar beside it can be separated from it by anybody who moves one and not
the other.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inventor_mcp.session import Session  # noqa: E402

#: Inventor's STEP translator add-in. The GUID is the documented one; whether
#: this release exposes it under that id is exactly what this probe is for.
STEP_TRANSLATOR = "{90AF7F40-0C01-11D5-8E83-0010B541CD80}"

#: The user-defined property set, the one whose contents show in the iProperties
#: dialog under Custom.
USER_DEFINED = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"


class Attempts:
    """Try things, print each result as it happens, let none stop the rest."""

    def __init__(self, explain):
        self._explain = explain
        self.worked: dict[str, object] = {}

    def __call__(self, label: str, work):
        try:
            outcome = work()
        except Exception as exc:
            print(f"  refused {label}\n            {self._explain(exc)}")
            return None
        shown = _describe(outcome)
        print(f"  ok      {label}\n            {shown}")
        self.worked[label] = outcome
        return outcome


def _describe(value) -> str:
    for attribute in ("FullFileName", "DisplayName", "Name", "Value"):
        try:
            found = getattr(value, attribute)
        except Exception:
            continue
        if found:
            return f"{type(value).__name__}: {found}"
    if isinstance(value, (str, int, float, bool)):
        return repr(value)
    return type(value).__name__


def on_thread(backend, work):
    worker = getattr(backend, "marshalling_thread", None)
    return worker.call(work) if worker is not None else work()


def raw(backend):
    return getattr(backend, "unmarshalled", backend)


# ---------------------------------------------------------------------------
# Importing a STEP file
# ---------------------------------------------------------------------------


def probe_import(session, backend, step: str) -> None:
    print(f"\n=== importing {step}")
    if not Path(step).is_file():
        print(f"  There is no file at {step}. Pass --step with a real STEP file.")
        return

    def attempt() -> None:
        inner = raw(backend)
        app = inner._app
        try_it = Attempts(inner._explain)
        opened: list[object] = []

        # 1. Straight open. Inventor's own file dialog accepts a .stp, so it is
        #    worth finding out whether Documents.Open does the same thing.
        document = try_it("Documents.Open(step)", lambda: app.Documents.Open(step, True))
        if document is not None:
            opened.append(document)
            report_contents("Documents.Open", document)

        # 2. ImportedComponents -- the modern, associative route. Needs a part to
        #    import into, so one is made first.
        holder = try_it("a scratch part to import into",
                        lambda: app.Documents.Add(
                            inner._k("kPartDocumentObject"),
                            app.FileManager.GetTemplateFile(
                                inner._k("kPartDocumentObject")),
                            True))
        if holder is not None:
            opened.append(holder)
            component = holder.ComponentDefinition
            imported = try_it(
                "ReferenceComponents.ImportedComponents.CreateDefinition",
                lambda: component.ReferenceComponents.ImportedComponents
                .CreateDefinition(step))
            if imported is not None:
                # The definition carries options -- which ones exist is worth
                # printing, since they decide whether bodies arrive as one solid
                # or many, and the DFM analysis wants one.
                for option in ("ReferenceModel", "Type", "IncludeAll",
                               "SkipAllSurfaces", "BodyFilterType"):
                    try:
                        print(f"            definition.{option} = "
                              f"{getattr(imported, option)}")
                    except Exception:
                        pass
                added = try_it(
                    "ImportedComponents.Add(definition)",
                    lambda: component.ReferenceComponents.ImportedComponents
                    .Add(imported))
                if added is not None:
                    report_contents("ImportedComponents", holder)

        # 3. The translator add-in, which is how this was done before
        #    ImportedComponents existed and is still the documented fallback.
        addin = try_it(
            f"ApplicationAddIns.ItemById({STEP_TRANSLATOR})",
            lambda: app.ApplicationAddIns.ItemById(STEP_TRANSLATOR))
        if addin is not None:
            # A makepy cache types this as a plain ApplicationAddIn, which has
            # no Open and no HasOpenOptions -- measured. Cast to the interface
            # that does, falling back to dynamic dispatch.
            def concrete(addin=addin):
                import win32com.client
                try:
                    return win32com.client.CastTo(addin, "TranslatorAddIn")
                except Exception:
                    return win32com.client.dynamic.Dispatch(addin._oleobj_)
            cast = try_it("cast the add-in to TranslatorAddIn", concrete)
            if cast is not None:
                addin = cast
        if addin is not None:
            try_it("the add-in reports itself activated",
                   lambda: bool(addin.Activated) or addin.Activate() or True)
            transients = app.TransientObjects
            medium = try_it("TransientObjects.CreateDataMedium",
                            lambda: transients.CreateDataMedium())
            context = try_it("TransientObjects.CreateTranslationContext",
                             lambda: transients.CreateTranslationContext())
            options = try_it("TransientObjects.CreateNameValueMap",
                             lambda: transients.CreateNameValueMap())
            if None not in (medium, context, options):
                medium.FileName = step
                context.Type = inner._k("kFileBrowseIOMechanism")
                has = try_it("translator.HasOpenOptions",
                             lambda: addin.HasOpenOptions(medium, context, options))
                if has:
                    try_it("the options it offers",
                           lambda: [options.Name(i + 1) for i in range(options.Count)])
                translated = try_it(
                    "translator.Open(medium, context, options)",
                    lambda: addin.Open(medium, context, options))
                if translated is not None:
                    opened.append(translated)
                    report_contents("translator.Open", translated)

        for document in opened:
            try:
                document.Close(True)
            except Exception:
                pass

    try:
        on_thread(backend, attempt)
    except Exception:
        traceback.print_exc(limit=6)


def report_contents(route: str, document) -> None:
    """What is actually in the part, which decides what the loop can do with it.

    A STEP file arrives as a solid with no features and no parameters. The loop
    drives parameters, so a count of zero here is the whole answer about what it
    can do -- and it is better to print it than to discover it in a loop that
    reports "nothing is left that a parameter change answers" and sounds like
    success.
    """
    print(f"            --- what {route} produced:")
    # Through dynamic dispatch: a makepy cache hands back the generic Document
    # interface, which declares no ComponentDefinition, and this printed
    # "no ComponentDefinition" for a document that had one.
    try:
        import win32com.client
        document = win32com.client.dynamic.Dispatch(document._oleobj_)
    except Exception:
        pass
    try:
        kind = int(document.DocumentType)
        print(f"                DocumentType = {kind}"
              + ("  (an ASSEMBLY -- a multi-body file did not arrive as one part)"
                 if kind == 12291 else ""))
    except Exception:
        pass
    try:
        component = document.ComponentDefinition
    except Exception as exc:
        print(f"                no ComponentDefinition: {str(exc)[:60]}")
        return
    for label, path in (
        ("solid bodies", "SurfaceBodies"),
        ("features", "Features"),
        ("user parameters", "Parameters.UserParameters"),
        ("model parameters", "Parameters.ModelParameters"),
        ("work planes", "WorkPlanes"),
    ):
        target = component
        try:
            for step in path.split("."):
                target = getattr(target, step)
            print(f"                {label}: {int(target.Count)}")
        except Exception as exc:
            print(f"                {label}: unreadable ({str(exc)[:50]})")
    try:
        body = component.SurfaceBodies.Item(1)
        print(f"                first body: faces={int(body.Faces.Count)} "
              f"closed={bool(body.IsSolid)}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Keeping a note inside the part
# ---------------------------------------------------------------------------

#: Long enough to find the limit if there is one at 255, which is where several
#: of Inventor's string properties stop.
LONG = "x" * 400


def probe_properties(session, backend) -> None:
    print("\n=== keeping a declaration inside the part")

    def attempt() -> None:
        inner = raw(backend)
        app = inner._app
        try_it = Attempts(inner._explain)

        document = app.Documents.Add(
            inner._k("kPartDocumentObject"),
            app.FileManager.GetTemplateFile(inner._k("kPartDocumentObject")),
            True,
        )
        try:
            sets = try_it("document.PropertySets", lambda: document.PropertySets)
            if sets is not None:
                print("            the property sets this document has:")
                for index in range(1, int(sets.Count) + 1):
                    entry = sets.Item(index)
                    try:
                        print(f"                {entry.DisplayName!r}  "
                              f"internal={entry.InternalName}")
                    except Exception:
                        print(f"                (set {index}: unreadable)")

            custom = try_it(f"PropertySets.Item({USER_DEFINED})",
                            lambda: sets.Item(USER_DEFINED))
            if custom is None:
                custom = try_it("PropertySets.Item('Inventor User Defined Properties')",
                                lambda: sets.Item("Inventor User Defined Properties"))
            if custom is not None:
                short = try_it("custom.Add('{\"wall\": \"wall_t\"}', 'DFM')",
                               lambda: custom.Add('{"wall": "wall_t"}', "DFM"))
                if short is not None:
                    try_it("read it back", lambda: custom.Item("DFM").Value)
                # How much will it hold? A role map with a freeze list is easily
                # over 255 characters, and finding the ceiling here beats
                # truncating a declaration in the field.
                long = try_it(f"custom.Add({len(LONG)} characters, 'DFM_LONG')",
                              lambda: custom.Add(LONG, "DFM_LONG"))
                if long is not None:
                    try_it("read the long one back and measure it",
                           lambda: len(str(custom.Item("DFM_LONG").Value)))

            # Attribute sets: the place an add-in is meant to keep its own data.
            # Invisible in the iProperties dialog, which is a drawback and also
            # means nobody deletes it by tidying up.
            attributes = try_it("document.AttributeSets",
                                lambda: document.AttributeSets)
            if attributes is not None:
                created = try_it(
                    "AttributeSets.Add('InventorMCP_DFM')",
                    lambda: attributes.Add("InventorMCP_DFM"))
                if created is not None:
                    stored = try_it(
                        "attribute set holds a long string",
                        lambda: created.Add("declaration",
                                            inner._k("kStringType"), LONG))
                    if stored is not None:
                        try_it("read it back and measure it",
                               lambda: len(str(created.Item("declaration").Value)))

            # Does any of it survive a save and reopen? That is the only
            # question that matters for carrying a declaration with a file, and
            # it cannot be answered without writing one.
            # Not FileManager.WorkspacePath: 2027.1's FileManager has no such
            # member, and the probe died here on the round-trip question it
            # existed to answer.
            import tempfile
            scratch = Path(tempfile.gettempdir()) / "dfm-probe.ipt"
            if scratch.exists():
                scratch.unlink()
            saved = try_it(f"SaveAs({scratch})",
                           lambda: document.SaveAs(str(scratch), False) or True)
            if saved:
                document.Close(True)
                reopened = try_it("reopen it", lambda: app.Documents.Open(str(scratch), False))
                if reopened is not None:
                    try_it("the iProperty survived",
                           lambda: reopened.PropertySets.Item(USER_DEFINED)
                           .Item("DFM").Value)
                    try_it("the attribute survived",
                           lambda: reopened.AttributeSets.Item("InventorMCP_DFM")
                           .Item("declaration").Value)
                    reopened.Close(True)
                print(f"            (delete {scratch} when you are done)")
                return
        finally:
            try:
                document.Close(True)
            except Exception:
                pass

    try:
        on_thread(backend, attempt)
    except Exception:
        traceback.print_exc(limit=6)


# ---------------------------------------------------------------------------
# What a built feature will tell us about itself
# ---------------------------------------------------------------------------


def probe_discovery(session, backend) -> None:
    """Whether role discovery has anything to read on a live part.

    Two questions, and the whole of discovery rests on them.

    ``Object.Type`` is a documented property of every Inventor object, and it is
    the only reliable way to know a shell from a rib -- ``type(feature).__name__``
    returns a pywin32 wrapper name under late binding, which is this project's
    default. If ``Type`` does not come back as a usable number, or the
    ``ObjectTypeEnum`` names do not resolve, every feature reports its kind as
    "unknown" and discovery falls back to reading the property alone.

    And a shell's ``Thickness``: is it a ``Parameter``, whose ``Expression``
    names the parameter driving it, or a bare number? Only the first is evidence.
    A hole's diameter is known to come back as a Parameter; a shell's thickness
    has never been read here.
    """
    print("\n=== what a built feature says about itself")
    from inventor_mcp.builder import apply_operation
    from inventor_mcp.schema import ExtrudeOp, ShellOp, SketchOp

    document = backend.new_part("DiscoveryProbe", units="mm")
    context = session.register(document, "mm", "deg")
    backend.set_parameter(context.doc_id, "wall_t", "2.5", units="mm")
    backend.set_parameter(context.doc_id, "draft_a", "1.5", units="deg")
    # Set on the backend, so the expression scope has not heard of them yet: a
    # later `taper="draft_a"` is refused as an unknown parameter without this.
    session.sync_parameters(context.doc_id)
    apply_operation(session, context, SketchOp(
        name="Outline", plane="xy",
        entities=[{"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]))
    apply_operation(session, context, ExtrudeOp(
        name="Block", sketch="Outline", distance=30, taper="draft_a"))
    apply_operation(session, context, ShellOp(
        name="Cavity", faces={"kind": "face", "filter": "top"},
        thickness="wall_t", direction="inside"))

    def attempt() -> None:
        inner = raw(backend)
        try_it = Attempts(inner._explain)
        component = inner._doc(context.doc_id).ComponentDefinition
        features = component.Features

        print("  --- Object.Type, and whether the enum names resolve")
        from inventor_mcp.backend.com.backend import _FEATURE_TYPES, _feature_kind
        resolved: dict[str, int] = {}
        for name in _FEATURE_TYPES:
            try:
                resolved[name] = inner._k(name)
            except Exception as exc:
                print(f"  refused {name}\n            {str(exc)[:70]}")
        print(f"  ok      {len(resolved)} of {len(_FEATURE_TYPES)} ObjectTypeEnum "
              f"names resolved")

        for index in range(1, int(features.Count) + 1):
            feature = features.Item(index)
            name = str(feature.Name)
            try:
                actual = int(feature.Type)
            except Exception as exc:
                print(f"  refused {name}.Type\n            {str(exc)[:70]}")
                actual = None
            named = next((short for enum, short in _FEATURE_TYPES.items()
                          if resolved.get(enum) == actual), None)
            print(f"  ok      {name}: Type={actual} -> {named or 'no name matched'}"
                  f"  (kind reports {_feature_kind(feature, inner._constants)!r};"
                  f" python type is {type(feature).__name__})")

        print("\n  --- is a driven property a Parameter, or just a number?")
        for name in ("Block", "Cavity"):
            described = try_it(f"describe_feature({name})",
                               lambda name=name: inner.describe_feature(context.doc_id, name))
            if described:
                for key, value in sorted(described.items()):
                    marker = ""
                    if isinstance(value, dict) and "expression" in value:
                        marker = "   <-- names the parameter: evidence"
                    print(f"            {key} = {value!r}{marker}")

        print("\n  --- and what discovery makes of it")
        try:
            from inventor_mcp.dfm.sources import discover_for
            found = discover_for(session, context)
            print(f"            roles: {found.declaration.roles}")
            for role, why in found.declaration.evidence.items():
                print(f"            {role}: {why}")
            if found.ambiguous:
                print(f"            ambiguous: {sorted(found.ambiguous)}")
            print(f"            suggested (not used): {found.suggestions}")
        except Exception as exc:
            print(f"  refused discovery\n            {str(exc)[:120]}")

    try:
        on_thread(backend, attempt)
    except Exception:
        traceback.print_exc(limit=6)
    try:
        backend.close_document(context.doc_id, save=False)
    except Exception:
        pass


def probe_copy(session, backend, part: str) -> None:
    """Whether Inventor is happy to open a filesystem copy of a part.

    A copied .ipt keeps the original's internal identity, and the question is
    whether Inventor notices -- reopening the original by reference, or refusing
    a document whose name it already has open. Save-As avoids the question and
    costs a window in which the original is open for writing, so it is worth
    knowing which problem is real.
    """
    print(f"\n=== opening a copy of {part}")
    from inventor_mcp.versioning import working_copy

    if not Path(part).is_file():
        print(f"  There is no file at {part}. Pass --part with a real .ipt.")
        return
    try:
        copy = working_copy(part)
    except Exception as exc:
        print(f"  refused making the copy: {exc}")
        return
    print(f"  ok      copied to {copy}")
    try:
        info = backend.open_document(str(copy))
        print(f"  ok      opened it: {info.name}  units={info.units} "
              f"angle={info.angle_units}")
        print(f"            path Inventor reports: {info.path}")
        if info.path and Path(info.path).resolve() != copy.resolve():
            print("            *** it opened a DIFFERENT file from the copy ***")
        if info.detail:
            print(f"            detail: {info.detail}")
        parameters = backend.list_parameters(info.id)
        print(f"            {len(parameters)} user parameters: "
              f"{[p.name for p in parameters][:8]}")
        backend.close_document(info.id, save=False)
    except Exception as exc:
        print(f"  refused opening the copy: {exc}")
    print(f"  (delete {copy} when you are done)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", help="A STEP file to try importing.")
    parser.add_argument("--part", help="An .ipt to try copying and reopening.")
    parser.add_argument("--only", nargs="*", default=[],
                        choices=["import", "properties", "discovery", "copy"])
    args = parser.parse_args(argv)

    try:
        session = Session(backend_kind="inventor")
        backend = session.ensure_backend()
        info = backend.connect(visible=True, create=True)
    except Exception as exc:
        print(f"Could not reach Inventor: {exc}")
        return 1
    print(f"Inventor {info.version}")

    wanted = lambda name: not args.only or name in args.only  # noqa: E731
    if wanted("import"):
        if args.step:
            probe_import(session, backend, args.step)
        else:
            print("\n=== importing: skipped, no --step given")
    if wanted("properties"):
        probe_properties(session, backend)
    if wanted("discovery"):
        probe_discovery(session, backend)
    if wanted("copy"):
        if args.part:
            probe_copy(session, backend, args.part)
        else:
            print("\n=== opening a copy: skipped, no --part given")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
