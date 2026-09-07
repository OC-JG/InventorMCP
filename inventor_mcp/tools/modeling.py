"""The modelling tools: recipes, incremental operations and parameter edits."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, TypeAdapter

from ..builder import apply_operation, apply_parameter, build_part
from ..checks import check_recipe
from ..rehearsal import rehearse
from ..guide import MODELLING_NOTES, RECIPE_CHEATSHEET, RECIPE_POINTER
from ..schema import Operation, ParameterSpec, PartRecipe, recipe_json_schema
from ..session import Session
from ._common import display_box, guard

_OPERATIONS = TypeAdapter(list[Operation])
_PARAMETERS = TypeAdapter(list[ParameterSpec])


def register(server: Any, session: Session) -> None:
    @server.tool(
        description="Return the full JSON Schema for a part recipe, plus the quick "
        "reference and modelling notes. Use when a recipe field is not obvious.",
    )
    @guard
    def part_recipe_schema(
        include_json_schema: Annotated[
            bool, Field(description="Include the machine-readable JSON Schema (large).")
        ] = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"cheatsheet": RECIPE_CHEATSHEET, "notes": MODELLING_NOTES}
        if include_json_schema:
            payload["json_schema"] = recipe_json_schema()
        return payload

    @server.tool(
        description="Check a part recipe without touching Inventor, then rehearse it: "
        "expressions, units, sketch closure, references between operations and hole "
        "centres, and then a full build in the simulator reporting what each "
        "operation would do to the part.\n\n"
        "Read the `warnings`. They are the things a valid recipe gets wrong: a cut "
        "whose profile misses the part, a parameter that drives no geometry. Read "
        "`steps` for the volume each operation moves, and check those numbers "
        "against what you intended -- a 9 mm hole 6 mm deep removes 0.382 cm3.\n\n"
        "Free and instant. Always run it before `build_part_from_recipe`.\n\n"
        + RECIPE_POINTER,
    )
    @guard
    def validate_recipe(
        recipe: Annotated[dict[str, Any], Field(description="The recipe object to check.")],
        rehearsal: Annotated[bool, Field(
            description="Also build it in the simulator and report what each operation "
                        "would do. Free, needs no Inventor, and catches a valid recipe "
                        "that builds the wrong part.")] = True,
    ) -> dict[str, Any]:
        parsed = PartRecipe.model_validate(recipe)
        result = rehearse(parsed) if rehearsal else check_recipe(parsed)
        result["name"] = parsed.name
        result["units"] = parsed.units
        result["operation_count"] = len(parsed.operations)
        return result

    @server.tool(
        description="Check a recipe against a 2D drawing you have read.\n\n"
        "A drawing is a specification, not a picture: tracing its outlines gives "
        "geometry with no parameters, which is the one thing this server exists "
        "not to produce. So read the drawing into a `reading` -- its views, its "
        "dimensions, its projection angle, its notes -- write a recipe whose "
        "PARAMETERS are those dimensions, and then call this to check one against "
        "the other.\n\n"
        "It reports: every drawing dimension that reached the model (`matched`), "
        "every one that did not (`missing` -- misread or left out), every number "
        "the model asserts as a bare literal that the drawing never gives "
        "(`invented`), and values correctly computed from drawing dimensions "
        "(`derived`, which is what a parametric model should do). It also checks "
        "the part's overall size against what the views show.\n\n"
        "Record anything you could not make out in `unreadable` rather than "
        "guessing. A missed dimension is recoverable; an invented one is not.",
    )
    @guard
    def check_against_drawing(
        recipe: Annotated[dict[str, Any], Field(description="The recipe to check.")],
        reading: Annotated[dict[str, Any], Field(
            description="What the drawing says. See `drawing_reading_schema`.")],
    ) -> dict[str, Any]:
        from ..drawing import DrawingReading, compare

        parsed = PartRecipe.model_validate(recipe)
        read = DrawingReading.model_validate(reading)
        rehearsal = rehearse(parsed)
        if not rehearsal["ok"]:
            return {
                "ok": False,
                "error": "the recipe does not build, so it cannot be compared",
                "findings": rehearsal["findings"],
            }
        result = compare(read, rehearsal)
        result["rehearsal"] = {
            "warnings": rehearsal["warnings"],
            "result": rehearsal.get("result"),
        }
        return result

    @server.tool(
        description="Build a drawing OF a part: place the views, retrieve the "
        "dimensions the recipe asks for, then read the sheet back and check it.\n\n"
        "Dimensions are RETRIEVED from the model, not placed by geometry. Every "
        "sketch dimension this server creates carries a parameter's expression, "
        "so Inventor's own retrieve-model-dimensions produces dimensions that "
        "ARE the parameters -- and a dimension on the sheet cannot then disagree "
        "with the part.\n\n"
        "The report is read off the finished sheet rather than from the request, "
        "which is the whole point: a parameter that drives no sketch dimension "
        "and no feature value has no model dimension to retrieve, so nothing is "
        "placed for it and `warnings` says so. A view whose direction did not "
        "mean what its name said has an extent that says so too.\n\n"
        "**The Inventor half of this has never run.** The simulator implements "
        "it and is worth trusting about the recipe; a live success is not yet "
        "evidence of anything. See the drawing section of docs/INVENTOR_SETUP.md.",
    )
    @guard
    def build_drawing_from_recipe(
        drawing: Annotated[dict[str, Any], Field(
            description="The drawing recipe. See `drawing_recipe_schema`.")],
        recipe: Annotated[dict[str, Any], Field(
            description="The part recipe the drawing is of.")],
        part_document: Annotated[str | None, Field(
            description="Id of an already-open part to draw instead of building "
            "one. Use it when the part took a while to build.")] = None,
    ) -> dict[str, Any]:
        from ..drafting import build_drawing
        from ..schema import DrawingRecipe

        return build_drawing(
            session, DrawingRecipe.model_validate(drawing),
            PartRecipe.model_validate(recipe), part_doc_id=part_document)

    @server.tool(
        description="Rehearse a DRAWING of a part, against the part it draws.\n\n"
        "The other direction from `check_against_drawing`, which reads a sheet "
        "somebody else produced. This takes a `drawing` recipe -- sheet, "
        "projection, views by direction, and for each view WHICH OF THE PART'S "
        "PARAMETERS it dimensions -- and works out what that sheet would say, "
        "then holds it against the part.\n\n"
        "Naming parameters rather than geometry is the point: the part's "
        "parameters are its design intent, so a drawing recipe knows which "
        "dimensions matter instead of inferring them.\n\n"
        "It reports the `ledger` -- every view and every dimension it states, "
        "resolved to a number -- and a `round_trip`: dimensions that reconcile "
        "with the part (`matched`), dimensions stating a value the part does not "
        "have (`states_what_the_part_does_not_have`), and the useful one, "
        "`undimensioned` -- numbers the part states that the sheet never gives. "
        "That last is the drawing fault you cannot see by looking, because every "
        "dimension that is on the sheet is correct.\n\n"
        "Nothing is drawn: no sheet is created and no Inventor is needed. See "
        "`drawing_recipe_schema` for the shape.",
    )
    @guard
    def rehearse_drawing_recipe(
        drawing: Annotated[dict[str, Any], Field(
            description="The drawing recipe. See `drawing_recipe_schema`.")],
        recipe: Annotated[dict[str, Any], Field(
            description="The part recipe the drawing is of.")],
    ) -> dict[str, Any]:
        from ..drafting import rehearse_drawing
        from ..schema import DrawingRecipe

        return rehearse_drawing(
            DrawingRecipe.model_validate(drawing), PartRecipe.model_validate(recipe))

    @server.tool(
        description="The JSON Schema for a drawing recipe -- what to write when you "
        "want a drawing OF a part, as opposed to reading one somebody sent you.",
    )
    @guard
    def drawing_recipe_schema() -> dict[str, Any]:
        from ..schema import DrawingRecipe

        return {"json_schema": DrawingRecipe.model_json_schema()}

    @server.tool(
        description="The JSON Schema for a drawing reading -- what to write down "
        "when you look at a 2D drawing, before writing any recipe.",
    )
    @guard
    def drawing_reading_schema() -> dict[str, Any]:
        from ..drawing import DrawingReading

        return {"json_schema": DrawingReading.model_json_schema()}

    @server.tool(
        description="Build a parametric part from a recipe. Creates the part, declares every "
        "parameter, then runs the operations in order. This is the main text-to-model "
        "entry point.\n\n" + RECIPE_CHEATSHEET,
    )
    @guard
    def build_part_from_recipe(
        recipe: Annotated[dict[str, Any], Field(description="The recipe to build.")],
        document: Annotated[
            str | None,
            Field(description="Add to an existing part instead of creating a new one."),
        ] = None,
        stop_on_error: Annotated[
            bool, Field(description="Stop at the first failing operation rather than continuing.")
        ] = True,
        validate_first: Annotated[
            bool, Field(description="Run the static checks first and refuse to build if they fail.")
        ] = True,
        rollback_on_error: Annotated[
            bool,
            Field(description="Undo the whole build if any step fails, using Inventor's own "
                              "transactions. Off by default: a half-built part is usually the "
                              "best evidence about what went wrong. Turn it on when the part "
                              "matters more than the diagnosis."),
        ] = False,
        against_rehearsal: Annotated[
            bool,
            Field(description="Rehearse in the simulator first and report any operation whose "
                              "live volume change disagrees with the prediction. On by "
                              "default: it costs milliseconds and it is how a fillet on the "
                              "wrong edge announces itself. Read `divergence` if it appears."),
        ] = True,
    ) -> dict[str, Any]:
        parsed = PartRecipe.model_validate(recipe)
        session.ensure_backend()
        if validate_first:
            check = check_recipe(parsed)
            if not check["ok"]:
                return {
                    "ok": False,
                    "error": "recipe_invalid",
                    "message": "The recipe did not pass validation, so nothing was built.",
                    "findings": check["findings"],
                    "hint": "Fix the findings, or call again with validate_first=false to "
                            "build anyway and see how far it gets.",
                }
        result = build_part(session, parsed, document=document, stop_on_error=stop_on_error,
                            rollback_on_error=rollback_on_error,
                            against_rehearsal=against_rehearsal)
        context = session.context(result["document"])
        if "mass_properties" in result:
            result["bounding_box"] = display_box(
                result["mass_properties"].get("bounding_box"), context.units
            )
        result["simulated"] = session.backend.name == "mock"
        return result

    @server.tool(
        description="Append operations to the part that is already open -- add a fillet, cut a "
        "pocket, pattern a feature. Uses exactly the same operation objects as a recipe.\n\n"
        + RECIPE_POINTER,
    )
    @guard
    def apply_operations(
        operations: Annotated[
            list[dict[str, Any]], Field(description="Operations to run, in order.")
        ],
        document: Annotated[str | None, Field(description="Target part; defaults to the active one.")] = None,
        stop_on_error: Annotated[bool, Field(description="Stop at the first failure.")] = True,
        rollback_on_error: Annotated[
            bool,
            Field(description="Undo these operations if any of them fails, using Inventor's "
                              "own transactions, leaving the part as it was. Off by default, "
                              "because the half-applied state is usually what explains the "
                              "failure -- but this is the way to retry a hole, which consumes "
                              "its sketch and so cannot be retried otherwise."),
        ] = False,
    ) -> dict[str, Any]:
        parsed = _OPERATIONS.validate_python(operations)
        context = session.context(document)
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        backend = session.backend
        handle = backend.begin_transaction(context.doc_id, "Apply operations") \
            if rollback_on_error else None
        for index, op in enumerate(parsed):
            try:
                results.append({"index": index, **apply_operation(session, context, op)})
            except Exception as exc:
                errors.append({"index": index, "op": op.op, "error": str(exc),
                               "hint": getattr(exc, "hint", None)})
                if stop_on_error:
                    break
        report = {
            "ok": not errors,
            "document": context.doc_id,
            "applied": results,
            "errors": errors,
        }
        if rollback_on_error and handle is None:
            report["rollback"] = "not available from this backend, so nothing was undone"
        elif handle is not None and errors:
            report["rolled_back"] = backend.abort_transaction(handle)
            if report["rolled_back"]:
                # The entries stay: they say what was built and are the record of
                # how far it got. They just no longer describe anything that is
                # in the part, and that has to be said rather than inferred.
                report["applied_then_undone"] = True
            report["rollback"] = ("the part is as it was before the call, so nothing "
                                  "under `applied` exists any more"
                                  if report["rolled_back"] else
                                  "the rollback itself failed -- inspect the part")
        elif handle is not None:
            backend.commit_transaction(handle)
        return report

    @server.tool(
        description="Declare or change driving parameters, then rebuild. This is how you revise a "
        "model: 'make it 20 mm wider' is a parameter change, not new geometry. "
        "Values may be numbers or expressions of other parameters.",
    )
    @guard
    def set_parameters(
        parameters: Annotated[
            list[dict[str, Any]],
            Field(description="Each is {name, value, unit?, comment?}. "
                              "value may be a number or an expression such as 'width / 2'."),
        ],
        document: Annotated[str | None, Field(description="Target part; defaults to the active one.")] = None,
        rebuild: Annotated[bool, Field(description="Rebuild the model after applying the changes.")] = True,
        override_frozen: Annotated[bool, Field(
            description="Change a parameter marked as key geometry anyway. Refused "
                        "without this, including for anything a protected value is "
                        "computed from. Say it deliberately.")] = False,
    ) -> dict[str, Any]:
        parsed = _PARAMETERS.validate_python(parameters)
        context = session.context(document)
        applied = [
            apply_parameter(session, context, spec, override_frozen=override_frozen)
            for spec in parsed
        ]
        result: dict[str, Any] = {"document": context.doc_id, "parameters": applied}
        if override_frozen and context.frozen is not None:
            touched = [spec.name for spec in parsed if context.frozen.check(spec.name)]
            if touched:
                result["overrode_key_geometry"] = touched
        if rebuild:
            result["rebuild"] = session.backend.rebuild(context.doc_id)
            try:
                properties = session.backend.mass_properties(context.doc_id)
                result["mass_properties"] = properties.as_dict()
                result["bounding_box"] = display_box(properties.bounding_box, context.units)
            except Exception:
                pass
        return result

    @server.tool(
        description="Suppress, unsuppress, rename or delete a feature. Suppressing is the safe way "
        "to test whether a feature is the cause of a rebuild failure.",
    )
    @guard
    def edit_feature(
        action: Annotated[str, Field(description="'suppress' | 'unsuppress' | 'rename' | 'delete'.")],
        name: Annotated[str, Field(description="Feature name, as shown by `inspect_part`.")],
        new_name: Annotated[str | None, Field(description="Required for 'rename'.")] = None,
        document: Annotated[str | None, Field(description="Target part.")] = None,
        override_frozen: Annotated[bool, Field(
            description="Alter a feature declared as key geometry anyway. Refused "
                        "without this. Say it deliberately.")] = False,
    ) -> dict[str, Any]:
        context = session.context(document)
        backend = session.backend
        # The other half of the freeze. Protecting a parameter while its feature
        # can be suppressed or deleted protects a number and loses the geometry
        # it describes -- `frozen_features` existed, was stored and merged, and
        # was enforced nowhere until here.
        if (not override_frozen and action in ("suppress", "delete", "rename")
                and context.frozen is not None
                and context.frozen.feature_frozen(name)):
            return {
                "ok": False,
                "error": "frozen_geometry",
                "message": f"The feature {name!r} is declared as key geometry, so "
                           f"it was not {action}d.",
                "hint": "If the change is intended, pass override_frozen=True, or "
                        "remove the feature from the declaration.",
            }
        if action == "suppress":
            return {"feature": backend.suppress_feature(context.doc_id, name, True).as_dict()}
        if action == "unsuppress":
            return {"feature": backend.suppress_feature(context.doc_id, name, False).as_dict()}
        if action == "rename":
            if not new_name:
                return {"ok": False, "error": "invalid_input",
                        "message": "rename needs `new_name`."}
            info = backend.rename_feature(context.doc_id, name, new_name)
            if name in context.feature_names:
                context.feature_names[context.feature_names.index(name)] = new_name
            if context.last_feature == name:
                context.last_feature = new_name
            return {"feature": info.as_dict()}
        if action == "delete":
            backend.delete_feature(context.doc_id, name)
            context.feature_names = [f for f in context.feature_names if f != name]
            if context.last_feature == name:
                context.last_feature = context.feature_names[-1] if context.feature_names else None
            return {"deleted": name}
        return {"ok": False, "error": "invalid_input",
                "message": f"Unknown action {action!r}.",
                "hint": "Use suppress, unsuppress, rename or delete."}

    @server.tool(description="Force a rebuild and report any features that failed.")
    @guard
    def rebuild_part(
        document: Annotated[str | None, Field(description="Target part.")] = None,
    ) -> dict[str, Any]:
        context = session.context(document)
        return {"document": context.doc_id, **session.backend.rebuild(context.doc_id)}
