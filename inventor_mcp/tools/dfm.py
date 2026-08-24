"""Manufacturability: measuring it, acting on it, and refusing to.

Four tools, and the split between them is the point.

``check_manufacture`` measures and reports. It changes nothing.
``read_dfm_report`` reads a report exported from the DFM tool in a browser and
says what it implies -- no Inventor, no Node, no re-analysis.
``improve_for_manufacture`` closes the loop: change, rebuild, measure again.
``protect_geometry`` says what may not be changed, and shows what that covers.

The last one is not an afterthought. An improvement loop is a machine for
changing dimensions until a number stops rising, and left alone it will thin the
wall that seals, shorten the boss that sets a stack height, or open the bore a
bearing presses into -- every one a real way to raise a DFM score and a broken
part. So the protection is declared, enforced where parameters change rather
than only inside the loop, and reported alongside every result.
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Any, Literal

from pydantic import Field

from ..backend.base import ExportRequest
from ..dfm.loop import current_parameters, improve, measure, plan_from_recipe
from ..dfm.remedy import ROLES, propose
from ..dfm.report import read_report
from ..dfm.runner import find_dfm_root
from ..session import Session
from ._common import guard


def _roles_table() -> dict[str, str]:
    return {role: what for role, (_, what) in sorted(ROLES.items())}


def register(server: Any, session: Session) -> None:

    @server.tool(
        description="Measure how manufacturable the part is by injection moulding, and say "
        "which findings are parameter changes. Exports an STL, runs the DFM analyser on it "
        "and reports the score, every finding, and what would be changed to answer each -- "
        "without changing anything.",
    )
    @guard
    def check_manufacture(
        document: Annotated[str | None, Field(description="Target part; defaults to the active one.")] = None,
        material: Annotated[str | None, Field(
            description="Moulding material, e.g. 'abs', 'pp', 'pc', 'pa66gf'. Defaults to "
                        "the recipe's dfm.settings, then the analyser's own default.")] = None,
        roles: Annotated[dict[str, str] | None, Field(
            description="Which parameter plays which role, as {role: parameter}. Added to "
                        "whatever the recipe declares. Roles: " + ", ".join(sorted(ROLES)))] = None,
        freeze: Annotated[list[str] | None, Field(
            description="Extra parameters to protect from automated change. Globs allowed.")] = None,
        workspace: Annotated[str | None, Field(
            description="Where to write the STL and the report. Defaults to ./.dfm")] = None,
        dfm_root: Annotated[str | None, Field(
            description="A checkout of the DFM tool. Defaults to $INVENTOR_MCP_DFM_ROOT, "
                        "then a sibling 'dfm' directory.")] = None,
        pull_axis: Annotated[Literal["+x", "-x", "+y", "-y", "+z", "-z"], Field(
            description="Mould pull direction.")] = "+z",
    ) -> dict[str, Any]:
        from pathlib import Path

        context = session.context(document)
        mapped, guard_, settings = plan_from_recipe(
            context.recipe, roles=roles, freeze=freeze or (),
            settings={"material": material} if material else None,
        )
        room = Path(workspace) if workspace else Path.cwd() / ".dfm"
        room.mkdir(parents=True, exist_ok=True)

        report, values, expressions, stl, path = measure(
            session, context, roles=mapped, settings=settings, workspace=room,
            label="check", dfm_root=dfm_root, pull_axis=pull_axis,
        )
        proposal = propose(report, mapped, guard_, values, expressions)
        return {
            "document": context.doc_id,
            **report.summary(),
            "checks": [c.as_dict() for c in report.checks],
            "would_change": proposal.as_dict(),
            "key_geometry": guard_.as_dict(),
            "roles_used": mapped,
            "stl": str(stl),
            "report": str(path),
        }

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
            description="Which parameter plays which role. Added to the recipe's own.")] = None,
        freeze: Annotated[list[str] | None, Field(
            description="Extra parameters to protect.")] = None,
    ) -> dict[str, Any]:
        with open(os.path.abspath(path), encoding="utf-8") as handle:
            report = read_report(json.load(handle))

        out: dict[str, Any] = {"report": os.path.abspath(path), **report.summary(),
                              "checks": [c.as_dict() for c in report.checks]}
        try:
            context = session.context(document)
        except Exception:
            # No model open. The findings are still worth reading; what cannot be
            # said is which parameter answers which, so it is not guessed at.
            out["note"] = ("No part is open, so this is the findings alone. Open the "
                           "part and call again to see which parameters would change.")
            return out

        mapped, guard_, _ = plan_from_recipe(
            context.recipe, roles=roles, freeze=freeze or (),
        )
        values, expressions = current_parameters(session, context)
        out["document"] = context.doc_id
        out["would_change"] = propose(report, mapped, guard_, values, expressions).as_dict()
        out["key_geometry"] = guard_.as_dict()
        return out

    @server.tool(
        description="Improve the part's manufacturability: apply the parameter changes the DFM "
        "findings imply, rebuild, run the analyser again, and repeat. Each round reports which "
        "findings actually cleared, so a fix is closed by measurement rather than by assertion. "
        "Never keeps a part that scores worse than it started, and never touches key geometry.",
    )
    @guard
    def improve_for_manufacture(
        document: Annotated[str | None, Field(description="Target part.")] = None,
        rounds: Annotated[int, Field(ge=1, le=12, description="Maximum rounds.")] = 4,
        material: Annotated[str | None, Field(description="Moulding material.")] = None,
        roles: Annotated[dict[str, str] | None, Field(
            description="Which parameter plays which role. Added to the recipe's own. "
                        "Declaring 'wall' matters most: the rib, boss and corner "
                        "guidelines are all fractions of the nominal wall.")] = None,
        freeze: Annotated[list[str] | None, Field(
            description="Parameters this run may not change, over and above those the "
                        "recipe marks frozen. Globs allowed. Additive only -- nothing "
                        "here can remove a freeze the recipe declares.")] = None,
        freeze_features: Annotated[list[str] | None, Field(
            description="Features this run may not alter.")] = None,
        include_functional: Annotated[bool, Field(
            description="Whether to apply changes that alter what the part does -- a "
                        "thinner wall, a shorter rib, a narrower boss. Off means they "
                        "are reported and not made.")] = True,
        workspace: Annotated[str | None, Field(
            description="Where to write each round's STL and report. Defaults to ./.dfm")] = None,
        dfm_root: Annotated[str | None, Field(description="A checkout of the DFM tool.")] = None,
        pull_axis: Annotated[Literal["+x", "-x", "+y", "-y", "+z", "-z"], Field(
            description="Mould pull direction.")] = "+z",
    ) -> dict[str, Any]:
        context = session.context(document)
        result = improve(
            session, context,
            roles=roles, freeze=freeze or (), freeze_features=freeze_features or (),
            settings={"material": material} if material else None,
            rounds=rounds, workspace=workspace, dfm_root=dfm_root,
            include_functional=include_functional, pull_axis=pull_axis,
        )
        return {"document": context.doc_id, **result.as_dict()}

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
        context.frozen = guard_

        known = {name.lower() for name in expressions}
        unknown = [
            name for name in (parameters or ())
            if "*" not in name and "?" not in name and name.lower() not in known
        ]
        out: dict[str, Any] = {"document": context.doc_id, "key_geometry": guard_.as_dict()}
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
        return out

    @server.tool(
        description="What the DFM integration can and cannot do: the parameter roles, where the "
        "analyser is, and which findings are answerable by a parameter change at all.",
    )
    @guard
    def dfm_capabilities() -> dict[str, Any]:
        from ..dfm.remedy import _NOT_PARAMETRIC

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
            "input_format": "STL. The analyser reads STEP too, but only through a "
                            "WASM module fetched from a CDN; Inventor writes STL well.",
        }
