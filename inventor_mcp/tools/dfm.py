"""Manufacturability: measuring it, acting on it, and refusing to.

Seven tools, and the split between them is the point.

``check_manufacture``       measure and report. Changes nothing.
``improve_for_manufacture`` close the loop: change, rebuild, measure again.
``read_dfm_report``         act on a report exported from the tool in a browser.
``discover_dfm_roles``      work out which parameter means what, from the part.
``declare_dfm``             say which parameter means what, and remember it.
``protect_geometry``        say what may not be changed.
``dfm_capabilities``        what any of this can and cannot do.

All of them take a file. Hand over an ``.ipt`` and the loop works on the next
version of it -- ``bracket_v2.ipt`` -- so the original cannot be changed by
anything that goes wrong. Hand over a STEP file and it is imported and measured;
it carries geometry and not the history that made it, so there are no parameters
to drive and that is said rather than discovered. Hand over an ``.stl`` and
Inventor is not involved at all.

``protect_geometry`` is not an afterthought. An improvement loop is a machine for
changing dimensions until a number stops rising, and left alone it will thin the
wall that seals against a gasket, shorten the boss that sets a stack height, or
open out the bore a bearing presses into -- every one of those a real way to
raise a DFM score and a broken part.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from ..dfm.declaration import Declaration, given
from ..dfm.loop import current_parameters, guard_for, improve, measure
from ..dfm.remedy import ROLES, propose
from ..dfm.report import read_report
from ..dfm.runner import (
    analyse_stl, compare_reports, find_dfm_root, settings_from_roles,
)
from ..dfm.sources import build_guard, discover_for, remember, resolve
from ..session import Session
from ..versioning import versions_of
from ._common import MESH_EXTENSIONS, guard, open_source


def _roles_table() -> dict[str, str]:
    return {role: what for role, (_, what) in sorted(ROLES.items())}


def _fold_session_freeze(context: Any, declaration: Any) -> None:
    """Widen *declaration* by what the session already protects.

    The loop does this for itself; the previews have to do it too, or
    `check_manufacture` promises a change that `improve_for_manufacture` then
    refuses -- a preview that contradicts the act it previews.
    """
    if context.frozen is None:
        return
    for name in context.frozen.as_dict()["declared"]:
        if name not in declaration.frozen:
            declaration.frozen.append(name)
    for name in context.frozen.features:
        if name not in declaration.frozen_features:
            declaration.frozen_features.append(name)


def _workspace(where: str | None) -> Path:
    room = Path(where) if where else Path.cwd() / ".dfm"
    room.mkdir(parents=True, exist_ok=True)
    return room


def _stamped(stem: str, suffix: str) -> str:
    """A file name no other run will use.

    Every run used to write the same names into the shared ./.dfm workspace, so
    the "before" report of a comparison was routinely overwritten by the run
    producing the "after" -- and compare_manufacture then compared a part with
    itself and reported nothing moved, which reads as "the fix did nothing".
    """
    from datetime import datetime, timezone

    moment = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return f"{stem}-{moment}.{suffix}"


def register(server: Any, session: Session) -> None:

    def _subject(path: str | None, document: str | None, *,
                 working_copy: bool) -> tuple[Any, dict[str, Any]]:
        """The document to work on, opening a file first if one was given.

        A path wins over a document handle, and saying so beats resolving it
        silently: a caller who names both is holding two different parts in
        mind, and quietly acting on one of them leaves the report reading as
        though it were about the other.
        """
        if path is None:
            return session.context(document), {}
        opened = open_source(session, path, working_copy=working_copy)
        if document is not None and document != opened["document"]:
            opened["note_on_document"] = (
                f"Both a path and document={document!r} were given; the file "
                f"was opened and acted on ({opened['document']}), and the "
                f"document handle was not used. Call again without a path to "
                f"act on {document!r}."
            )
        return session.context(opened["document"]), opened

    @server.tool(
        description="Measure how manufacturable a part is by injection moulding, and say "
        "which findings are parameter changes. Takes an .ipt, a STEP file or an .stl -- or "
        "nothing, for the part already open. Exports a mesh, runs the DFM analyser on it, "
        "and reports the score, every finding, and what would be changed to answer each. "
        "Changes nothing.",
    )
    @guard
    def check_manufacture(
        path: Annotated[str | None, Field(
            description="A part or mesh to measure: .ipt, .stp/.step, .igs, .sat, or "
                        ".stl. An .stl is analysed without opening Inventor at all. "
                        "Omit to measure the part already open.")] = None,
        document: Annotated[str | None, Field(
            description="Target part; defaults to the active one. With an .stl path, the "
                        "part that mesh came from, so changes can be proposed against "
                        "its parameters.")] = None,
        material: Annotated[str | None, Field(
            description="Moulding material, e.g. 'abs', 'pp', 'pc', 'pa66gf'. Defaults "
                        "to what the part or its recipe declares, then the analyser's "
                        "own default.")] = None,
        roles: Annotated[dict[str, str] | None, Field(
            description="Which parameter plays which role, as {role: parameter}. Added "
                        "to whatever the part declares. Roles: " + ", ".join(sorted(ROLES)))] = None,
        freeze: Annotated[list[str] | None, Field(
            description="Extra parameters to protect from automated change. Globs allowed.")] = None,
        workspace: Annotated[str | None, Field(
            description="Where to write the mesh and the report. Defaults to ./.dfm")] = None,
        dfm_root: Annotated[str | None, Field(
            description="A checkout of the DFM tool. Defaults to $INVENTOR_MCP_DFM_ROOT, "
                        "then a sibling 'dfm' directory.")] = None,
        pull_axis: Annotated[Literal["+x", "-x", "+y", "-y", "+z", "-z"], Field(
            description="Mould pull direction.")] = "+z",
        infer_roles: Annotated[bool, Field(
            description="Work out unmapped roles from the part's own features -- a "
                        "shell's thickness is the wall. Evidence only; a parameter is "
                        "never mapped from its name.")] = True,
    ) -> dict[str, Any]:
        settings = {"material": material} if material else None

        # A mesh needs no CAD at all. Handed one, the shortest honest path is to
        # analyse it and say so, rather than opening Inventor to re-export what
        # is already a mesh.
        if path and os.path.splitext(path)[1].lower() in MESH_EXTENSIONS:
            return _check_a_mesh(path, document, settings, roles, freeze,
                                 workspace, dfm_root, pull_axis, infer_roles)

        context, opened = _subject(path, document, working_copy=False)
        declaration, discovered = resolve(
            session, context, path=opened.get("path_on_disk"), roles=roles,
            freeze=freeze or (), settings=settings, infer=infer_roles,
        )
        _fold_session_freeze(context, declaration)
        values, expressions = current_parameters(session, context)
        guard_, pin_notes = build_guard(session, context, declaration)

        report, values, expressions, mesh, written = measure(
            session, context, roles=declaration.roles,
            settings=declaration.settings, workspace=_workspace(workspace),
            label=_stamped(f"{context.name}-check", "").rstrip("."),
            dfm_root=dfm_root, pull_axis=pull_axis,
        )
        proposal = propose(report, declaration.roles, guard_, values, expressions)
        out: dict[str, Any] = {
            "document": context.doc_id,
            **report.summary(),
            "checks": [c.as_dict() for c in report.checks],
            "would_change": proposal.as_dict(),
            "read_the_part_as": declaration.describe(),
            "key_geometry": {**guard_.as_dict(),
                             **({"pinned_by_features": pin_notes} if pin_notes else {})},
            "stl": str(mesh),
            "report": str(written),
        }
        if opened:
            out["file"] = {k: v for k, v in opened.items()
                           if k in ("opened", "working_copy", "original_untouched",
                                    "parametric", "what_that_means", "detail",
                                    "note_on_document")}
        if discovered is not None and discovered.suggestions:
            out["unmapped_roles_look_like"] = discovered.as_dict().get("suggestions")
        return out

    def _check_a_mesh(path, document, settings, roles, freeze, workspace,
                      dfm_root, pull_axis, infer_roles) -> dict[str, Any]:
        """Analyse a mesh, and propose against a part only if one was named.

        A mesh has no parameters, so there is nothing to change *in it*. Naming a
        document says "this mesh came from that part", and only then can a
        finding be turned into a parameter change -- guessing that an open part
        and a handed-over mesh are the same thing is exactly the assumption that
        would edit the wrong model.
        """
        room = _workspace(workspace)
        source = Path(path).resolve()
        written = room / _stamped(f"{source.stem}", "json")

        # The declaration first, when a part is named, because it is an INPUT to
        # the analysis and not only to the proposal: the rib and boss checks are
        # judged on declared numbers, and analysing with the tool's defaults
        # while the result claims the part's declaration was used would judge a
        # different part from the one reported.
        declared = dict(settings or {})
        context = None
        declaration = None
        if document is not None:
            context = session.context(document)
            declaration, _ = resolve(session, context, roles=roles,
                                     freeze=freeze or (), settings=settings,
                                     infer=infer_roles)
            _fold_session_freeze(context, declaration)
            values, expressions = current_parameters(session, context)
            declared = settings_from_roles(declaration.roles, values,
                                           {**declaration.settings,
                                            **(settings or {})})

        report = analyse_stl(source, declared, dfm_root=dfm_root,
                            pull_axis=pull_axis, save_report_to=written)
        out: dict[str, Any] = {
            "mesh": str(source), **report.summary(),
            "checks": [c.as_dict() for c in report.checks],
            "report": str(written),
        }
        if context is None:
            out["note"] = (
                "A mesh has no parameters, so nothing is proposed. To get the "
                "parameter changes these findings imply, name the part this mesh "
                "came from with document=..., or open it and call without a path."
            )
            return out
        guard_, _pins = build_guard(session, context, declaration)
        out["document"] = context.doc_id
        out["would_change"] = propose(
            report, declaration.roles, guard_, values, expressions).as_dict()
        out["read_the_part_as"] = declaration.describe()
        out["key_geometry"] = guard_.as_dict()
        out["note"] = (f"Taken as a mesh of {context.name}, because that is what "
                       f"document={document!r} names; the analysis was run with "
                       f"that part's declared numbers.")
        return out

    @server.tool(
        description="Improve a part's manufacturability: apply the parameter changes the DFM "
        "findings imply, rebuild, run the analyser again, and repeat. Give it an .ipt and it "
        "works on the next version of the file, so the original cannot be changed. Each round "
        "reports which findings actually cleared, so a fix is closed by measurement rather "
        "than by assertion. Never keeps a part that scores worse than it started, and never "
        "touches key geometry.",
    )
    @guard
    def improve_for_manufacture(
        path: Annotated[str | None, Field(
            description="An .ipt to improve. Worked on as the next version -- "
                        "bracket.ipt becomes bracket_v2.ipt -- leaving the original "
                        "alone. Omit to work on the part already open.")] = None,
        document: Annotated[str | None, Field(description="Target part.")] = None,
        rounds: Annotated[int, Field(ge=1, le=12, description="Maximum rounds.")] = 4,
        material: Annotated[str | None, Field(description="Moulding material.")] = None,
        roles: Annotated[dict[str, str] | None, Field(
            description="Which parameter plays which role. Added to whatever the part "
                        "declares. Declaring 'wall' matters most: the rib, boss and "
                        "corner guidelines are all fractions of the nominal wall.")] = None,
        freeze: Annotated[list[str] | None, Field(
            description="Parameters this run may not change, over and above those the "
                        "part declares. Globs allowed. Additive only -- nothing here "
                        "can remove a freeze the part or its recipe declares.")] = None,
        freeze_features: Annotated[list[str] | None, Field(
            description="Features this run may not alter.")] = None,
        include_functional: Annotated[bool, Field(
            description="Whether to apply changes that alter what the part does -- a "
                        "thinner wall, a shorter rib, a narrower boss. Off means they "
                        "are reported and not made.")] = True,
        working_copy: Annotated[bool, Field(
            description="Work on the next version of the file rather than the file "
                        "itself. On by default and worth leaving on: this changes the "
                        "model.")] = True,
        save: Annotated[bool | None, Field(
            description="Save when the loop finishes. Left unset it saves a working "
                        "copy and does NOT save over the file you named -- writing "
                        "over somebody's part is something to ask for, not something "
                        "to default to. Pass true to save over it anyway.")] = None,
        infer_roles: Annotated[bool, Field(
            description="Work out unmapped roles from the part's own features. "
                        "Evidence only; never from a parameter's name.")] = True,
        workspace: Annotated[str | None, Field(
            description="Where to write each round's mesh and report. Defaults to ./.dfm")] = None,
        dfm_root: Annotated[str | None, Field(description="A checkout of the DFM tool.")] = None,
        pull_axis: Annotated[Literal["+x", "-x", "+y", "-y", "+z", "-z"], Field(
            description="Mould pull direction.")] = "+z",
    ) -> dict[str, Any]:
        context, opened = _subject(path, document, working_copy=working_copy)
        if opened.get("parametric") is False:
            return {
                "ok": False,
                "error": "nothing_to_drive",
                "message": (f"{opened['opened']} has no user parameters, so there is "
                            f"nothing for the loop to change."),
                "hint": ("Measure it with `check_manufacture` -- every finding still "
                         "applies, and the document is still open. To be able to "
                         "improve it, the part needs parameters: build it from a "
                         "recipe, or add the dimensions you want to be able to drive."
                         + (f" A working copy was made at {opened['working_copy']} "
                            f"before this was known; delete it if you do not want it."
                            if opened.get("working_copy") else "")),
                "file": opened,
            }

        result = improve(
            session, context,
            roles=roles, freeze=freeze or (), freeze_features=freeze_features or (),
            settings={"material": material} if material else None,
            rounds=rounds, workspace=workspace, dfm_root=dfm_root,
            include_functional=include_functional, pull_axis=pull_axis,
            path=opened.get("path_on_disk"), infer=infer_roles,
        )
        out: dict[str, Any] = {"document": context.doc_id, **result.as_dict()}
        if opened:
            out["file"] = {k: v for k, v in opened.items()
                           if k in ("opened", "working_copy", "original_untouched",
                                    "note_on_document")}

        # One rule governs both writes below: this run owns what it created --
        # the working copy it made -- and nothing else. Not the file named in
        # `path` without a copy, and not the file an already-open document came
        # from: `open_part` then `improve_for_manufacture()` with no arguments
        # used to save straight over the original, because the guard looked
        # only at the path given to THIS call.
        mine = opened.get("working_copy")
        lives_at = opened.get("path_on_disk") or session.backend.document_path(
            context.doc_id)

        # What was used is written back beside the copy, so the next run on
        # this version starts from the same reading -- and only beside the
        # copy. Rewriting the sidecar of a file this run declined to write is
        # still rewriting a reviewed file. And only what somebody *stated*: a
        # discovered role stays an inference until a person confirms it, and
        # storing it would bring it back next run as "the part itself".
        if mine:
            stated_only, _ = resolve(
                session, context, path=mine, roles=roles, freeze=freeze or (),
                freeze_features=freeze_features or (),
                settings={"material": material} if material else None,
                infer=False,
            )
            out["remembered"] = remember(session, context, stated_only, path=mine)

        # The question anybody asks on the second pass is "is it better than it
        # was", and the loop has both records already. Asked of the DFM tool's own
        # comparison rather than diffed here: it knows which direction is better
        # for each measurement, and it raises a caveat where a score moved for a
        # reason other than the part.
        first, last = result.rounds[0], result.rounds[-1]
        if len(result.rounds) > 1 and first.report and last.report:
            try:
                out["what_moved"] = compare_reports(
                    first.report, last.report, dfm_root=dfm_root)
            except Exception as exc:
                out["what_moved"] = None
                out["comparison_failed"] = str(exc)[:200]

        should_save = save if save is not None else bool(mine)
        if should_save:
            try:
                out["saved"] = session.backend.save_document(context.doc_id).as_dict()
                if lives_at:
                    try:
                        out["versions"] = [str(p) for p in versions_of(lives_at)]
                    except OSError:
                        pass  # a listing is a nicety; the save already happened
                if not mine and lives_at:
                    out["overwrote"] = (
                        f"{lives_at} was saved over, because save=true was asked "
                        f"for on a document that is not a working copy this call "
                        f"made."
                    )
            except Exception as exc:
                out["saved"] = None
                out["save_failed"] = str(exc)[:200]
        elif save is None and not mine:
            out["not_saved"] = (
                "The model in Inventor has been changed and nothing has been "
                "written to disk: this call made no working copy, and saving over "
                + (f"{lives_at}" if lives_at else "the open document")
                + " is not something it does unless told to. Pass save=true to "
                "write it, or `save_part(path=...)` to put it somewhere else -- "
                "or hand the file to this tool with path=... next time and get a "
                "versioned copy for free."
            )
        return out

    @server.tool(
        description="Work out which parameter plays which role in the manufacturability "
        "assessment, from the part itself: a shell feature's thickness is the wall, an "
        "extrude's taper is the draft. Evidence only -- a parameter is never mapped from its "
        "name, though likely-looking ones are offered for you to confirm. Changes nothing.",
    )
    @guard
    def discover_dfm_roles(
        path: Annotated[str | None, Field(
            description="A part to read. Omit for the one already open.")] = None,
        document: Annotated[str | None, Field(description="Target part.")] = None,
    ) -> dict[str, Any]:
        context, opened = _subject(path, document, working_copy=False)
        found = discover_for(session, context)
        declaration, _ = resolve(session, context,
                                 path=opened.get("path_on_disk"), infer=True)
        out: dict[str, Any] = {
            "document": context.doc_id,
            "from_the_part": found.as_dict(),
            "what_would_be_used": declaration.describe(),
            "roles": _roles_table(),
        }
        if opened:
            out["file"] = {k: v for k, v in opened.items()
                           if k in ("opened", "parametric", "what_that_means")}
        return out

    @server.tool(
        description="Say which parameter plays which role in the manufacturability "
        "assessment, and which dimensions are key geometry -- then remember it, in the part "
        "and beside it, so the next run and every later version starts from the same reading. "
        "What goes into the part is in the open document until it is saved; the sidecar is on "
        "disk immediately.",
    )
    @guard
    def declare_dfm(
        roles: Annotated[dict[str, str] | None, Field(
            description="{role: parameter}. Roles: " + ", ".join(sorted(ROLES)))] = None,
        frozen: Annotated[list[str] | None, Field(
            description="Parameters automated changes may not touch. Globs allowed.")] = None,
        frozen_features: Annotated[list[str] | None, Field(
            description="Features automated changes may not alter.")] = None,
        material: Annotated[str | None, Field(description="Moulding material.")] = None,
        settings: Annotated[dict[str, Any] | None, Field(
            description="Other settings for the analyser, using its own names: "
                        "'surfaceFinish', 'moldType', 'checks'.")] = None,
        document: Annotated[str | None, Field(description="Target part.")] = None,
        remember_it: Annotated[bool, Field(
            description="Write it into the part and beside it, so it survives being "
                        "closed and travels with a versioned copy.")] = True,
    ) -> dict[str, Any]:
        context = session.context(document)
        combined = dict(settings or {})
        if material:
            combined["material"] = material
        stated = given(roles=roles, frozen=frozen or (),
                       frozen_features=frozen_features or (), settings=combined,
                       source="declared for this part")

        values, expressions = current_parameters(session, context)
        unknown = [name for name in (roles or {}).values()
                   if name.lower() not in {k.lower() for k in expressions}]

        declaration, _ = resolve(session, context, roles=roles,
                                 freeze=frozen or (),
                                 freeze_features=frozen_features or (),
                                 settings=combined, infer=True)
        # The guard has to hold from now on, not only inside the next loop --
        # and it widens what is already held rather than replacing it. A freeze
        # added a moment ago with `protect_geometry` lives only on the context,
        # and building the new guard from the declaration alone dropped it: the
        # one operation no source is allowed to perform, done here by accident.
        held = (list(context.frozen.as_dict()["declared"])
                if context.frozen is not None else [])
        held_features = (list(context.frozen.features)
                         if context.frozen is not None else [])
        for name in held:
            if name not in declaration.frozen:
                declaration.frozen.append(name)
        for name in held_features:
            if name not in declaration.frozen_features:
                declaration.frozen_features.append(name)
        context.frozen, pin_notes = build_guard(session, context, declaration)

        out: dict[str, Any] = {
            "document": context.doc_id,
            "declared": stated.describe(),
            "what_would_be_used": declaration.describe(),
            "key_geometry": {**context.frozen.as_dict(),
                             **({"pinned_by_features": pin_notes} if pin_notes else {})},
        }
        if unknown:
            out["not_a_parameter_of_this_part"] = unknown
            out["note"] = (
                "These names are not parameters here, so check the spelling: "
                + ", ".join(unknown) + ". Parameters: " + ", ".join(sorted(expressions))
                + "."
            )
        if remember_it:
            # What gets written is what somebody said -- this call and the
            # stored sources -- never what discovery inferred. Writing the
            # merged declaration stored every inference as though the part had
            # stated it, so a wrong inference came back next time as "the part
            # itself" and even infer_roles=false could not escape it. An
            # inference stays an inference until a person confirms it, and
            # confirming it is exactly what passing it to this tool does.
            stated_only, _ = resolve(session, context, roles=roles,
                                     freeze=frozen or (),
                                     freeze_features=frozen_features or (),
                                     settings=combined, infer=False)
            out["remembered"] = remember(session, context, stated_only)
        return out

    @server.tool(
        description="Make a part drivable by giving its dimensions named parameters, in "
        "place. An .ipt built without parameters is not parameterless -- every dimension "
        "is a model parameter with a value; what is missing is names. Each promotion "
        "creates a user parameter at the property's current value and rewires the property "
        "to reference it: no geometry is re-authored, the part is identical afterwards, "
        "and the DFM loop can then drive it. Run `discover_dfm_roles` first -- its "
        "`to_promote` block is exactly this tool's input.",
    )
    @guard
    def promote_parameters(
        promotions: Annotated[list[dict[str, str]] | None, Field(
            description="Each is {feature, property, name}: which feature's driven "
                        "property gets which new parameter. Omit to promote everything "
                        "discovery found promotable, under its suggested names.")] = None,
        path: Annotated[str | None, Field(
            description="An .ipt to promote. Worked on as the next version of the file, "
                        "so the original is untouched. Omit for the part already open.")] = None,
        document: Annotated[str | None, Field(description="Target part.")] = None,
        working_copy: Annotated[bool, Field(
            description="Work on the next version of the file rather than the file "
                        "itself. On by default: this changes the document.")] = True,
        save: Annotated[bool | None, Field(
            description="Save when done. Unset saves a working copy this call made and "
                        "never the file you named; true saves regardless.")] = None,
        declare: Annotated[bool, Field(
            description="Also declare the roles the promotions serve, and remember the "
                        "declaration in the part -- promoting for a role is stating it.")] = True,
    ) -> dict[str, Any]:
        context, opened = _subject(path, document, working_copy=working_copy)

        wanted = promotions
        found = None
        if wanted is None:
            found = discover_for(session, context)
            wanted = [
                {"feature": entry["feature"], "property": entry["property"],
                 "name": entry["suggested_name"], "role": entry["role"]}
                for entry in found.promotable
            ]
            if not wanted:
                return {
                    "document": context.doc_id,
                    "promoted": [],
                    "note": ("Discovery found nothing promotable: every role it can "
                             "see is either already driven by a named parameter, or "
                             "not evidenced in this part at all. "
                             "`discover_dfm_roles` shows which is which."),
                    **({"file": opened} if opened else {}),
                }

        promoted: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for entry in wanted:
            try:
                outcome = session.backend.promote_parameter(
                    context.doc_id, entry["feature"], entry["property"],
                    entry["name"],
                )
                if entry.get("role"):
                    outcome["role"] = entry["role"]
                promoted.append(outcome)
            except Exception as exc:
                failed.append({**entry, "error": str(exc)[:200]})
        session.sync_parameters(context.doc_id)

        out: dict[str, Any] = {
            "document": context.doc_id,
            "promoted": promoted,
            "identical_geometry": ("each promotion holds the property's current "
                                   "value, so the part is the same shape it was"),
        }
        if failed:
            out["failed"] = failed
        if opened:
            out["file"] = {k: v for k, v in opened.items()
                           if k in ("opened", "working_copy", "original_untouched",
                                    "note_on_document")}

        if declare and promoted:
            roles = {entry["role"]: entry["parameter"]
                     for entry in promoted if entry.get("role")}
            if roles:
                declaration, _ = resolve(session, context,
                                         path=opened.get("path_on_disk"),
                                         roles=roles, infer=False)
                out["declared"] = remember(session, context, declaration,
                                           path=opened.get("path_on_disk"))
                out["roles_declared"] = roles

        mine = opened.get("working_copy")
        should_save = save if save is not None else bool(mine)
        if should_save:
            try:
                out["saved"] = session.backend.save_document(context.doc_id).as_dict()
            except Exception as exc:
                out["saved"] = None
                out["save_failed"] = str(exc)[:200]
        elif save is None and (path or document):
            out["not_saved"] = (
                "The promotions are in the open document and nothing has been "
                "written to disk. Pass save=true, or `save_part`."
            )
        return out

    @server.tool(
        description="Read a DFM report exported from the tool in a browser and say what it "
        "implies about the model: which findings are parameter changes, which are frozen, and "
        "which need a person. Needs no Inventor connection and re-runs nothing.",
    )
    @guard
    def read_dfm_report(
        path: Annotated[str, Field(description="A JSON file exported from the DFM tool.")],
        document: Annotated[str | None, Field(
            description="Target part, for reading the current parameter values. Omit to "
                        "report the findings without proposing changes against a model.")] = None,
        roles: Annotated[dict[str, str] | None, Field(
            description="Which parameter plays which role. Added to what the part declares.")] = None,
        freeze: Annotated[list[str] | None, Field(
            description="Extra parameters to protect.")] = None,
    ) -> dict[str, Any]:
        with open(os.path.abspath(path), encoding="utf-8") as handle:
            report = read_report(json.load(handle))

        out: dict[str, Any] = {"report": os.path.abspath(path), **report.summary(),
                              "checks": [c.as_dict() for c in report.checks]}
        if document is not None:
            # Named and not found is a mistake to correct, not a state to note:
            # "No part is open" against an explicit handle hides the typo.
            context = session.context(document)
        else:
            try:
                context = session.context(None)
            except Exception:
                # No model open. The findings are still worth reading; what
                # cannot be said is which parameter answers which, so it is not
                # guessed at.
                out["note"] = ("No part is open, so this is the findings alone. "
                               "Open the part and call again to see which "
                               "parameters would change.")
                return out

        declaration, _ = resolve(session, context, roles=roles, freeze=freeze or ())
        _fold_session_freeze(context, declaration)
        values, expressions = current_parameters(session, context)
        guard_, _pins = build_guard(session, context, declaration)
        out["document"] = context.doc_id
        out["would_change"] = propose(
            report, declaration.roles, guard_, values, expressions).as_dict()
        out["read_the_part_as"] = declaration.describe()
        out["key_geometry"] = guard_.as_dict()
        return out

    @server.tool(
        description="Declare key geometry: parameters and features that automated changes must "
        "not touch. Enforced wherever a parameter changes, not just in the improvement loop. "
        "Anything a protected value is computed from is protected too, since changing that "
        "would move it. Call with nothing to see what is currently protected.",
    )
    @guard
    def protect_geometry(
        parameters: Annotated[list[str] | None, Field(
            description="Parameter names to protect. A '*' glob is allowed, so 'seal_*' "
                        "covers a family.")] = None,
        features: Annotated[list[str] | None, Field(
            description="Feature names to protect from suppression, deletion or editing.")] = None,
        document: Annotated[str | None, Field(description="Target part.")] = None,
        remember_it: Annotated[bool, Field(
            description="Write the protection into the part and beside it, so it "
                        "survives being closed.")] = False,
    ) -> dict[str, Any]:
        context = session.context(document)
        values, expressions = current_parameters(session, context)

        if context.frozen is None:
            from ..dfm.freeze import FreezeGuard
            context.frozen = FreezeGuard(expressions=expressions)
        guard_ = context.frozen.with_expressions(expressions)
        if parameters or features:
            guard_ = guard_.extend(
                parameters or (), features=features or (),
                reason="declared as key geometry",
            )
        pin_notes: list[str] = []
        if features:
            # A frozen feature means its parameters too, here as everywhere.
            from ..dfm.declaration import Declaration as _Declaration

            pinned, pin_notes = build_guard(
                session, context,
                _Declaration(frozen=list(guard_.as_dict()["declared"]),
                             frozen_features=list(guard_.features)))
            guard_ = pinned
        context.frozen = guard_

        known = {name.lower() for name in expressions}
        unknown = [
            name for name in (parameters or ())
            if "*" not in name and "?" not in name and name.lower() not in known
        ]
        out: dict[str, Any] = {"document": context.doc_id, "key_geometry": guard_.as_dict()}
        if pin_notes:
            out["key_geometry"]["pinned_by_features"] = pin_notes
        if unknown:
            # Not refused: protecting a name before it exists is legitimate, and
            # a typo would otherwise protect nothing while reporting success.
            out["not_a_parameter_yet"] = unknown
            out["note"] = (
                "These are protected but are not parameters of this part, so check the "
                "spelling: " + ", ".join(unknown) + ". Parameters here: "
                + ", ".join(sorted(expressions)) + "."
            )
        if guard_.empty:
            out["note"] = (
                "Nothing is protected. Any DFM improvement round may change every "
                "parameter of this part. Protect the dimensions that carry function -- "
                "sealing faces, bearing bores, mating pitches, stack heights."
            )
        if remember_it:
            # Merged onto what the part already declares, because `remember`
            # replaces the stored declaration whole: writing only the freeze
            # would erase the roles and the material somebody declared earlier,
            # and the next run would be back to inferring them.
            stored, _ = resolve(session, context, infer=False)
            for name in guard_.as_dict()["declared"]:
                if name not in stored.frozen:
                    stored.frozen.append(name)
            for name in guard_.features:
                if name not in stored.frozen_features:
                    stored.frozen_features.append(name)
            out["remembered"] = remember(session, context, stored)
        return out

    @server.tool(
        description="Compare two manufacturability runs and say what moved: the score, the "
        "grade, which checks changed band, and which measurements shifted and in which "
        "direction. This is the question worth asking of a versioned part -- bracket.ipt "
        "against bracket_v3.ipt. It declines to mislead: a score that moved because the "
        "material or the set of checks changed comes back with that said above the diff.",
    )
    @guard
    def compare_manufacture(
        before: Annotated[str, Field(
            description="The earlier report: a JSON file from `check_manufacture`, from a "
                        "round of `improve_for_manufacture`, or exported from the DFM "
                        "tool in a browser.")],
        after: Annotated[str, Field(description="The later report.")],
        dfm_root: Annotated[str | None, Field(description="A checkout of the DFM tool.")] = None,
        save_to: Annotated[str | None, Field(
            description="Where to write the comparison. Omit to return it only.")] = None,
    ) -> dict[str, Any]:
        return {
            "before": os.path.abspath(before),
            "after": os.path.abspath(after),
            **compare_reports(before, after, dfm_root=dfm_root, save_to=save_to),
        }

    @server.tool(
        description="What the DFM integration can and cannot do: the parameter roles, where the "
        "analyser is, which file formats are accepted, and which findings are answerable by a "
        "parameter change at all.",
    )
    @guard
    def dfm_capabilities() -> dict[str, Any]:
        from ..dfm.remedy import _NOT_PARAMETRIC
        from ._common import IMPORT_EXTENSIONS

        where: dict[str, Any]
        try:
            where = {"analyser": str(find_dfm_root())}
        except Exception as exc:
            where = {"analyser": None, "problem": getattr(exc, "message", str(exc)),
                     "hint": getattr(exc, "hint", None)}
        return {
            "roles": _roles_table(),
            "answerable_by_a_parameter": ["wall", "draft", "ribs"],
            "needs_a_person": {
                key: reason for key, (_, reason) in sorted(_NOT_PARAMETRIC.items())
            },
            **where,
            "accepts": {
                ".ipt": "opened directly; worked on as the next version so the "
                        "original is untouched. The only format the loop can improve, "
                        "and only where the part has parameters.",
                "translated": sorted(IMPORT_EXTENSIONS),
                "translated_note": "imported as a solid body. Carries geometry and not "
                                   "the history that made it, so there are no "
                                   "parameters to drive: measurable, not improvable.",
                ".stl": "analysed directly, with no Inventor involved at all.",
            },
            "how_roles_are_settled": [
                "what you say at the call",
                "what the recipe says, for a part built here",
                "what the part itself says, from an earlier declaration",
                "a .dfm.json sidecar beside the part",
                "discovery from the part's own features -- evidence, never a name",
            ],
        }
