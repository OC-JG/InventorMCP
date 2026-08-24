"""Shared plumbing for the tool layer."""

from __future__ import annotations

import functools
import logging
import os
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import ValidationError

from ..errors import DocumentError, InventorMCPError
from ..units import Dim, Quantity, from_internal
from ..versioning import working_copy as make_working_copy

#: Extensions Inventor reads through a translator rather than opening as its own
#: file. A translated file carries geometry and not the history that made it, so
#: what arrives has a solid body and no parameters.
IMPORT_EXTENSIONS = frozenset({
    ".stp", ".step", ".stpz", ".igs", ".iges", ".sat", ".sab",
    ".x_t", ".x_b", ".jt",
})

#: Meshes. Inventor will read one, and for manufacturability there is no reason
#: to: the analysis wants a mesh and that is already what this is.
MESH_EXTENSIONS = frozenset({".stl", ".obj", ".3mf", ".ply"})

logger = logging.getLogger("inventor_mcp")

F = TypeVar("F", bound=Callable[..., dict])


def guard(func: F) -> F:
    """Turn exceptions into structured results the caller can act on.

    A model that gets ``{"ok": false, "error": "...", "hint": "..."}`` can fix
    its own input on the next call; an opaque stack trace cannot be fixed at
    all.  Unexpected exceptions are still logged in full server-side.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result = func(*args, **kwargs)
        except InventorMCPError as exc:
            logger.info("%s: %s", func.__name__, exc.message)
            return exc.to_dict()
        except ValidationError as exc:
            return {
                "ok": False,
                "error": "invalid_input",
                "message": f"{func.__name__} received input that does not match the schema.",
                "issues": _format_validation(exc),
                "hint": "Read `part_recipe_schema` for the exact field names and types.",
            }
        except Exception as exc:  # pragma: no cover - genuine bugs and COM surprises
            logger.exception("%s failed", func.__name__)
            return {
                "ok": False,
                "error": "unexpected_error",
                "message": f"{type(exc).__name__}: {exc}",
            }
        if isinstance(result, dict):
            result.setdefault("ok", True)
        return result

    return wrapper  # type: ignore[return-value]


def _format_validation(exc: ValidationError) -> list[dict[str, Any]]:
    issues = []
    for error in exc.errors()[:20]:
        location = ".".join(str(part) for part in error["loc"])
        issues.append({"field": location or "(root)", "problem": error["msg"], "type": error["type"]})
    return issues


def display_length(value_cm: float, unit: str, decimals: int = 4) -> float:
    return round(from_internal(value_cm, unit), decimals)


def display_point(point: tuple[float, float, float] | None, unit: str) -> list[float] | None:
    if point is None:
        return None
    return [display_length(component, unit) for component in point]


def display_box(box: tuple[float, ...] | None, unit: str) -> dict[str, Any] | None:
    if not box:
        return None
    low = [display_length(component, unit) for component in box[:3]]
    high = [display_length(component, unit) for component in box[3:]]
    return {
        "min": low,
        "max": high,
        "size": [round(h - l, 4) for l, h in zip(low, high)],
        "units": unit,
    }


def quantity_dict(value: float, dim: Dim, unit: str) -> dict[str, Any]:
    return {"value": round(Quantity(value, dim).as_display(unit), 6), "units": unit}


# ---------------------------------------------------------------------------
# Opening whatever was handed over
# ---------------------------------------------------------------------------


def open_source(session: Any, path: str, *, working_copy: bool = False,
                ) -> dict[str, Any]:
    """Open a part file the right way for what it is, and say what arrived.

    One implementation, because three tools need it and the decisions are not
    obvious: an .ipt opens, a translated file imports, a mesh is refused with
    somewhere better to go, and a working copy is made *before* anything opens
    the original so that nothing can write to it.

    What is reported is measured rather than assumed. Whether the part can be
    driven is a count of its user parameters, not an inference from its
    extension -- an .ipt somebody built by importing a STEP file and never
    parameterised has exactly the same problem as the STEP file.
    """
    backend = session.ensure_backend()
    source = os.path.abspath(path)
    suffix = os.path.splitext(source)[1].lower()
    out: dict[str, Any] = {"opened": source}

    if suffix in MESH_EXTENSIONS:
        raise DocumentError(
            f"A {suffix} file is a mesh, not a part: there is nothing in it to "
            f"drive, and Inventor is not needed to read it.",
            hint="For manufacturability, pass it straight to "
                 "`check_manufacture(path=...)`, which analyses a mesh without "
                 "opening Inventor at all.",
        )

    if not os.path.isfile(source):
        raise DocumentError(
            f"There is no file at {source}.",
            hint="Give an absolute path. A relative one is resolved against the "
                 "server's working directory, which is rarely where you think.",
        )

    if suffix in IMPORT_EXTENSIONS:
        if working_copy:
            out["note"] = ("A translated file is read and never written, so it "
                           "needs no working copy. Imported directly.")
        info = backend.import_geometry(source)
    else:
        if working_copy:
            copy = make_working_copy(source)
            out["working_copy"] = str(copy)
            out["original_untouched"] = source
            source = str(copy)
            try:
                info = backend.open_document(source)
            except Exception:
                # The copy was made for this open and holds nothing that the
                # original does not, so a failed open removes it rather than
                # leaving an orphan version on the drive that nobody was told
                # about -- which the next run would then count past.
                import contextlib
                from ..dfm.declaration import sidecar_for as _sidecar
                for leftover in (Path(source), _sidecar(source)):
                    with contextlib.suppress(OSError):
                        leftover.unlink(missing_ok=True)
                raise
        else:
            info = backend.open_document(source)

    context = session.register(info, info.units, info.angle_units)
    session.sync_parameters(context.doc_id)

    # The freeze is enforced where parameters change, and that has to include a
    # part whose protection arrived with the file. Without this, a sidecar or an
    # embedded declaration saying bore_d is key geometry would hold inside the
    # DFM loop -- which resolves it for itself -- and be walked straight through
    # by the next `set_parameters`, with every report still saying the freeze
    # was honoured.
    try:
        from ..dfm.sources import resolve as resolve_declaration

        declaration, _ = resolve_declaration(session, context, path=source,
                                             infer=False)
        # Getting here means the stored declaration READS -- which has two
        # consequences the first version missed, both found by reproducing them:
        #
        # * a session freeze added with `protect_geometry` lives only on the
        #   context, and REPLACING the guard from the declaration dropped it --
        #   the operation no source may perform. Widen instead.
        # * the '*' guard installed when a declaration could NOT be read is
        #   protection standing in for knowledge. Now that the declaration
        #   reads, the knowledge is here, and the sentinel must clear -- carried
        #   forward, it froze the part for the whole session even after the
        #   sidecar was fixed, with the refusal relabelled to hide the cause.
        from ..dfm.freeze import UNPROTECTABLE_PREFIX
        from ..dfm.sources import build_guard

        if context.frozen is not None:
            for held, reason in context.frozen.declared_reasons():
                if reason.startswith(UNPROTECTABLE_PREFIX):
                    continue
                if held not in declaration.frozen:
                    declaration.frozen.append(held)
            for held in context.frozen.features:
                if held not in declaration.frozen_features:
                    declaration.frozen_features.append(held)

        if declaration.frozen or declaration.frozen_features:
            context.frozen, pin_notes = build_guard(session, context, declaration)
            out["key_geometry"] = context.frozen.as_dict()
            if pin_notes:
                out["key_geometry"]["notes"] = (
                    out["key_geometry"].get("notes") or []) + pin_notes
        elif context.frozen is not None and context.frozen.unprotectable:
            context.frozen = None
            out["note_on_protection"] = (
                "The declaration that could not be read before reads cleanly now "
                "and freezes nothing, so the everything-frozen guard is lifted."
            )
    except Exception as exc:
        # A declaration exists and cannot be read, which means what it protects
        # cannot be known -- and the one wrong answer is "nothing". So until it
        # is fixed, everything is protected: the next parameter change is
        # refused with this reason, which is a loud failure pointing at the
        # actual problem, where the old behaviour was a quiet note and a part
        # whose freezes were gone.
        from ..dfm.freeze import FreezeGuard

        out["declaration_problem"] = str(exc)[:300]
        from ..dfm.freeze import UNPROTECTABLE_PREFIX

        context.frozen = FreezeGuard(
            ["*"],
            reason=(UNPROTECTABLE_PREFIX + " the declaration stored for this "
                    "part could not be read, so what it froze is unknown -- fix "
                    "or delete it and open the part again (see "
                    "declaration_problem in the open_part result)"),
        )
        out["key_geometry"] = context.frozen.as_dict()
        out["every_parameter_is_frozen_because"] = (
            "the stored declaration could not be read, so what it protects is "
            "unknown. Fix or delete it; override_frozen=True forces a change "
            "through in the meantime."
        )

    parameters = sorted(context.resolver.known())
    out.update({
        "document": info.id,
        **info.as_dict(),
        "parameters": parameters,
        "features": [f.name for f in backend.list_features(context.doc_id)],
        "path_on_disk": source,
    })
    if not parameters:
        out["parametric"] = False
        out["what_that_means"] = (
            "This part has no user parameters, so there is nothing for a revision "
            "or a DFM loop to change. `check_manufacture` still measures it; "
            "`improve_for_manufacture` will have nothing to act on. Add the "
            "parameters you want to be able to drive, or rebuild it from a recipe."
        )
    else:
        out["parametric"] = True
    return out


def guard_expressions(session: Any, doc_id: str) -> dict[str, str]:
    """Kept as a re-export; the one implementation lives with the guard logic."""
    from ..dfm.sources import guard_expressions as _shared

    return _shared(session, doc_id)
