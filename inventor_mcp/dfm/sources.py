"""Assembling one declaration out of everywhere it could come from.

Five places can say which parameter is the wall and what may not be touched, and
they do not agree in general. This decides who wins, and the order is an
argument rather than a convenience:

1. **given at the call** -- a person saying it now beats anything remembered.
2. **the recipe** -- when the part was built here, the recipe *is* the design
   intent, and it is current.
3. **the part itself** -- read out of the document, where Inventor lets us keep
   it. Travels with the file.
4. **a sidecar** -- ``bracket.dfm.json`` beside the part. Same content, weaker
   claim: a file next to a file can be separated from it, and can be left behind
   by a rename nobody thought about.
5. **discovery** -- inferred from the part's own features. Weakest, because it is
   this code's reading rather than anybody's statement.

Freezes do not participate in that contest. They are unioned across all five, so
every source can add protection and none can remove it.

Discovery runs last and is asked for first, which sounds backwards and is not:
it is cheap, and its findings are worth *reporting* even when a stronger source
overrides them -- a recipe mapping the wall to one parameter while the part's
only shell reads another is worth somebody's attention.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from .declaration import Declaration, from_recipe, given, merge, read_sidecar
from .discover import Discovery, discover, facts_from
from .freeze import FreezeGuard


def resolve(
    session: Any,
    context: Any,
    *,
    path: str | Path | None = None,
    roles: Mapping[str, str] | None = None,
    freeze: Iterable[str] = (),
    freeze_features: Iterable[str] = (),
    settings: Mapping[str, Any] | None = None,
    infer: bool = True,
) -> tuple[Declaration, Discovery | None]:
    """The declaration to use, and what discovery made of the part.

    *path* is where the part came from, for finding a sidecar beside it; without
    one, the document's own path is used if it has one. *infer* turns discovery
    off, for a caller who would rather have unmapped roles than inferred ones.
    """
    found = discover_for(session, context) if infer else None

    beside = Path(path) if path else _document_path(session, context)
    sidecar = read_sidecar(beside) if beside else None

    embedded = _from_document(session, context)

    declaration = merge(
        found.declaration if found else None,
        sidecar,
        embedded,
        from_recipe(context.recipe),
        given(roles=roles, frozen=freeze, frozen_features=freeze_features,
              settings=settings),
    )

    # Where a stronger source disagrees with the evidence, say so rather than
    # quietly taking the stronger one. The recipe wins either way; somebody
    # should still know the part's only shell reads a different parameter.
    if found is not None:
        for role, inferred in found.declaration.roles.items():
            used = declaration.roles.get(role)
            if used and used != inferred:
                declaration.notes.append(
                    f"The {role!r} role is set to {used!r} by {declaration.origin.get(role)}, "
                    f"but the part itself points at {inferred!r}: "
                    f"{found.declaration.evidence.get(role, 'no reason recorded')}. "
                    f"Using what was declared."
                )
    return declaration, found


def discover_for(session: Any, context: Any) -> Discovery:
    """What the part's own features imply about the role map."""
    facts = facts_from(session, context.doc_id)
    parameters = [
        info.name for info in session.backend.list_parameters(context.doc_id)
    ]
    return discover(facts, parameters)


def _document_path(session: Any, context: Any) -> Path | None:
    """Where the open document lives, if it lives anywhere yet.

    Asked of the document itself. The first version matched ``context.doc_id``
    against ``list_documents``, and on the COM backend that listing identifies
    documents by Python wrapper identity -- late binding hands back a fresh
    wrapper per call, so the match never matched, the sidecar beside the part
    was never found, and the freezes in it were silently dropped for any call
    that did not pass the path explicitly.
    """
    try:
        where = session.backend.document_path(context.doc_id)
    except Exception:
        return None
    return Path(where) if where else None


def _from_document(session: Any, context: Any) -> Declaration | None:
    """The declaration kept inside the part, where the backend can keep one.

    Optional by design. A backend that cannot store one is not broken -- the
    sidecar covers it -- so this asks and moves on. What it must not do is
    invent an empty declaration, which would read as "this part says nothing is
    frozen" rather than "nobody asked this part".
    """
    reader = getattr(session.backend, "read_declaration", None)
    if reader is None:
        return None
    # Deliberately not caught: a reader that *raises* found something and could
    # not read it, which is a different fact from finding nothing -- and the
    # difference is whatever the unreadable declaration froze. Swallowing it
    # here read a corrupted freeze list as "nothing is protected".
    stored = reader(context.doc_id)
    if not stored:
        return None
    return Declaration.from_dict(stored, source="the part itself")


def remember(session: Any, context: Any, declaration: Declaration,
             *, path: str | Path | None = None) -> dict[str, Any]:
    """Write *declaration* back where it will be found again.

    Both places, when both are available. The document is the one that matters --
    it travels with the file, through a rename and a move -- and the sidecar is
    the one that always works, including in the simulator and on a backend that
    cannot store anything. Neither failing is fatal; a declaration that could not
    be saved is a declaration that has to be given again, not a broken run.
    """
    from .declaration import write_sidecar

    out: dict[str, Any] = {}
    writer = getattr(session.backend, "write_declaration", None)
    if writer is not None:
        try:
            writer(context.doc_id, declaration.as_dict(for_storage=True))
            out["in_the_part"] = True
            # Into the open document, which is not the same as onto the disk: a
            # property written and never saved dies with the session, and the
            # tool used to say "survives being closed" either way.
            out["on_disk_when"] = "the document is next saved"
        except Exception as exc:
            out["in_the_part"] = False
            out["why_not"] = str(exc)[:200]

    beside = Path(path) if path else _document_path(session, context)
    if beside is not None:
        try:
            out["sidecar"] = str(write_sidecar(beside, declaration))
        except Exception as exc:
            out["sidecar"] = None
            out["sidecar_failed"] = str(exc)[:200]
    else:
        out["sidecar"] = None
        out["note"] = ("This part has not been saved anywhere, so there is nowhere "
                       "beside it to keep the declaration.")
    return out


# ---------------------------------------------------------------------------
# Building the guard, features included
# ---------------------------------------------------------------------------


def guard_expressions(session: Any, doc_id: str) -> dict[str, str]:
    """Every parameter's expression, model parameters included, for a guard.

    The freeze closure follows what a frozen expression reads, and a frozen
    ``seal_face = d0 * 2`` reads a model parameter -- which ``set_parameter``
    can write, because Inventor resolves the name in the whole collection. A
    closure built from the user table alone was blind to that.
    """
    try:
        listed = session.backend.list_parameters(doc_id, include_model=True)
    except TypeError:
        listed = session.backend.list_parameters(doc_id)
    return {info.name: info.expression for info in listed}


def build_guard(session: Any, context: Any,
                declaration: Declaration) -> tuple[FreezeGuard, list[str]]:
    """The guard a declaration means, with its frozen features made real.

    A frozen feature is a promise that its geometry stays put, and geometry is
    changed by changing parameters -- so each frozen feature pins every
    parameter that reaches it: its own driven properties and the dimensions of
    the sketches it consumes, traced by the backend, closed transitively by the
    guard. Without this, "freeze the Bosses" stopped the feature being deleted
    and let every dimension of it move.

    Returns the guard and the notes that must travel with it -- above all the
    honest failure: a backend that cannot trace a feature's parameters leaves
    that feature protected from deletion and NOT from being reshaped, and
    saying so is the difference between a limitation and a lie.
    """
    import fnmatch

    expressions = guard_expressions(session, context.doc_id)
    guard = FreezeGuard(
        declaration.frozen, expressions=expressions,
        features=declaration.frozen_features,
    )
    notes: list[str] = []
    if not declaration.frozen_features:
        return guard, notes

    try:
        listed = [info.name for info in session.backend.list_features(context.doc_id)]
    except Exception:
        listed = []
    for pattern in declaration.frozen_features:
        matched = [name for name in listed
                   if fnmatch.fnmatch(name.lower(), pattern.lower())]
        if not matched and "*" not in pattern and "?" not in pattern:
            matched = [pattern]
        for feature in matched:
            try:
                traced = session.backend.feature_dependencies(
                    context.doc_id, feature)
            except Exception as exc:
                traced = None
                notes.append(
                    f"Tracing what drives the frozen feature {feature!r} failed "
                    f"({str(exc)[:120]})."
                )
            if traced is None:
                notes.append(
                    f"The frozen feature {feature!r} is protected from being "
                    f"suppressed or deleted, and NOT from being reshaped: this "
                    f"backend cannot trace which parameters drive it, so none "
                    f"were pinned. Freeze them by name."
                )
                continue
            pinned = traced.get("parameters") or []
            if pinned:
                guard = guard.extend(
                    pinned,
                    reason=f"driven by the frozen feature {feature!r} -- "
                           f"changing it would reshape geometry that was "
                           f"declared to stay the same",
                )
                notes.append(
                    f"Freezing the feature {feature!r} pinned "
                    f"{', '.join(pinned)}."
                )
    return guard, notes
