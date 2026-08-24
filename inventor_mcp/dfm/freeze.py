"""Key geometry: the parameters an automated change may not touch.

A DFM loop is a machine that changes dimensions until a score stops improving.
Left alone it will happily thin the wall that seals against a gasket, shorten
the boss that sets a stack height, or open out the bore that a bearing presses
into -- every one of those a legitimate way to raise a DFM score and a broken
part.

So the loop is given a list of what it may not move, and refuses rather than
choosing. Three things follow from taking that seriously:

**It is enforced where parameters change, not where the loop runs.** A guarantee
implemented in the loop is a guarantee that ends the moment anything else edits
a parameter, so :func:`inventor_mcp.builder.apply_parameter` consults the guard
too. Overriding it is possible and has to be said out loud.

**A freeze is additive.** The loop may be handed more to protect, never less.
Taking something out of the protected set means editing the recipe, which is a
reviewable act, rather than passing a flag in the moment.

**Depending on a frozen value is the same as changing it.** If ``seal_face`` is
frozen at ``plate_t - gasket_crush``, then editing ``plate_t`` moves the frozen
face just as surely as editing it directly. The name would still not appear in
the frozen list, and the report would still say the freeze was honoured. So the
protection follows the expressions: everything a frozen parameter is computed
from is protected too, transitively, and the refusal names the chain.

The reverse is not true, and deliberately so. A parameter that *uses* a frozen
value -- ``clearance = bore_d + 0.2`` where ``bore_d`` is frozen -- is free to
move; changing it does not disturb what it read.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..errors import InventorMCPError
from ..expressions import referenced_parameters


class FrozenGeometryError(InventorMCPError):
    code = "frozen_geometry"


#: The reason-prefix marking a freeze that exists because a declaration could
#: not be READ -- protection standing in for knowledge, not knowledge. It has
#: different lifecycle rules from a real freeze: it clears the moment the
#: declaration reads cleanly, and no widening operation may copy it forward as
#: though somebody had declared it.
UNPROTECTABLE_PREFIX = "unprotectable:"


@dataclass(frozen=True)
class FrozenParameter:
    """Why one parameter is protected."""

    name: str
    reason: str
    #: For the transitive case, the frozen parameter this one is read by, and
    #: the chain between them. ``("seal_face",)`` reads as "because seal_face is
    #: frozen and its expression depends on this".
    via: tuple[str, ...] = ()

    def explain(self) -> str:
        if not self.via:
            return f"{self.name} is {self.reason}"
        chain = " <- ".join(self.via)
        return (f"{self.name} is protected because {chain} is {self.reason}, and is "
                f"what it is computed from -- changing {self.name} moves {self.via[-1]}")

    def as_dict(self) -> dict[str, Any]:
        return {"parameter": self.name, "reason": self.reason,
                "via": list(self.via), "explanation": self.explain()}


class FreezeGuard:
    """Which names are protected, and why.

    Built from the recipe's own declarations plus anything added at the call. A
    name may be an exact parameter name or a glob -- ``seal_*`` protects a
    family without listing it -- and matching ignores case, because Inventor's
    parameter names do.
    """

    def __init__(
        self,
        names: Iterable[str] = (),
        *,
        expressions: Mapping[str, str] | None = None,
        features: Iterable[str] = (),
        reason: str = "declared as key geometry",
    ) -> None:
        self._patterns: list[tuple[str, str]] = [(str(n), reason) for n in names if str(n).strip()]
        self._features = tuple(sorted({str(f) for f in features if str(f).strip()}))
        self._expressions = {str(k): str(v) for k, v in (expressions or {}).items()}
        self._derived: dict[str, FrozenParameter] = {}
        self._recompute()

    # -- construction -----------------------------------------------------

    def extend(self, names: Iterable[str] = (), *, features: Iterable[str] = (),
               reason: str = "protected for this run") -> "FreezeGuard":
        """A wider guard: this one plus *names*. Never a narrower one."""
        wider = FreezeGuard.__new__(FreezeGuard)
        wider._patterns = list(self._patterns) + [
            (str(n), reason) for n in names if str(n).strip()
        ]
        wider._features = tuple(sorted(set(self._features) | {
            str(f) for f in features if str(f).strip()
        }))
        wider._expressions = dict(self._expressions)
        wider._derived = {}
        wider._recompute()
        return wider

    def with_expressions(self, expressions: Mapping[str, str]) -> "FreezeGuard":
        """The same protection, resolved against a parameter table.

        Called once the real expressions are known: the transitive closure
        cannot be computed without them, and a guard built before the model
        exists would report a freeze it had not actually worked out.
        """
        updated = FreezeGuard.__new__(FreezeGuard)
        updated._patterns = list(self._patterns)
        updated._features = self._features
        updated._expressions = {str(k): str(v) for k, v in expressions.items()}
        updated._derived = {}
        updated._recompute()
        return updated

    # -- the closure ------------------------------------------------------

    def _recompute(self) -> None:
        """Work out which names the expressions drag in.

        Breadth-first from every directly frozen parameter through what its
        expression reads. An expression that will not parse is skipped rather
        than raising: this is a guard, and it must still protect the names it
        *can* work out when one entry in the table is malformed. What it must
        never do is claim protection it has not computed, so the skip is
        recorded as a note.
        """
        self._derived = {}
        self.notes: list[str] = []
        direct = [name for name in self._expressions if self._directly_frozen(name)]

        queue: list[tuple[str, tuple[str, ...], str]] = []
        for name in direct:
            reason = self._directly_frozen(name) or "declared as key geometry"
            queue.append((name, (name,), reason))

        seen: set[str] = set()
        while queue:
            name, chain, reason = queue.pop(0)
            expression = self._expressions.get(name)
            if expression is None:
                continue
            try:
                reads = referenced_parameters(expression)
            except Exception as exc:
                self.notes.append(
                    f"Could not read {name}'s expression {expression!r}, so anything "
                    f"it depends on is not protected through it: {exc}"
                )
                continue
            for read in sorted(reads):
                key = read.lower()
                if key in seen or self._directly_frozen(read):
                    continue
                # Only names that are actually parameters here; an expression
                # may reference a model parameter this table does not carry.
                canonical = self._canonical(read)
                if canonical is None:
                    continue
                seen.add(key)
                self._derived[key] = FrozenParameter(
                    name=canonical, reason=reason, via=chain,
                )
                queue.append((canonical, chain + (canonical,), reason))

    def _canonical(self, name: str) -> str | None:
        for known in self._expressions:
            if known.lower() == name.lower():
                return known
        return None

    def _directly_frozen(self, name: str) -> str | None:
        """The reason *name* is frozen outright, or ``None``."""
        lowered = name.lower()
        for pattern, reason in self._patterns:
            if fnmatch.fnmatch(lowered, pattern.lower()):
                return reason
        return None

    # -- asking -----------------------------------------------------------

    def check(self, name: str) -> FrozenParameter | None:
        """Why *name* may not be changed, or ``None`` if it may."""
        reason = self._directly_frozen(name)
        if reason is not None:
            return FrozenParameter(name=name, reason=reason)
        return self._derived.get(name.lower())

    def refuse(self, name: str) -> None:
        """Raise if *name* is protected. The enforcement point."""
        frozen = self.check(name)
        if frozen is None:
            return
        raise FrozenGeometryError(
            f"{frozen.explain()}, so this change was not made.",
            hint="If the change is intended, either remove the freeze from the "
                 "recipe or pass override_frozen=True to say so explicitly.",
        )

    def feature_frozen(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name.lower(), f.lower()) for f in self._features)

    def declared_reasons(self) -> list[tuple[str, str]]:
        """Every declared pattern with why it is there, for callers that widen.

        Widening from ``as_dict()["declared"]`` alone lost the reasons, and one
        reason changes everything: the unprotectable sentinel must never be
        copied forward as though somebody had declared it.
        """
        return list(self._patterns)

    @property
    def unprotectable(self) -> bool:
        """Whether this guard is standing in for a declaration nobody could read."""
        return any(reason.startswith(UNPROTECTABLE_PREFIX)
                   for _, reason in self._patterns)

    @property
    def empty(self) -> bool:
        return not self._patterns and not self._features

    @property
    def features(self) -> tuple[str, ...]:
        return self._features

    def as_dict(self) -> dict[str, Any]:
        """What is protected, including what the expressions dragged in."""
        return {
            "declared": [pattern for pattern, _ in self._patterns],
            "features": list(self._features),
            "also_protected": [
                frozen.as_dict() for frozen in sorted(
                    self._derived.values(), key=lambda f: f.name.lower()
                )
            ],
            "notes": list(self.notes),
        }


def guard_for_recipe(
    recipe: Mapping[str, Any] | None,
    *,
    extra: Iterable[str] = (),
    extra_features: Iterable[str] = (),
) -> FreezeGuard:
    """The guard a recipe asks for, widened by *extra*.

    Two declarations are read, and they mean the same thing: ``frozen: true`` on
    a parameter, and the recipe-level ``dfm.frozen`` list, which also accepts
    globs and so can protect a family that does not exist yet.
    """
    parameters = list((recipe or {}).get("parameters") or ())
    expressions = {
        str(spec.get("name")): _expression_of(spec)
        for spec in parameters
        if isinstance(spec, dict) and spec.get("name")
    }

    names = [
        str(spec["name"]) for spec in parameters
        if isinstance(spec, dict) and spec.get("name") and spec.get("frozen")
    ]
    features: list[str] = []
    block = (recipe or {}).get("dfm")
    if isinstance(block, dict):
        names += [str(n) for n in (block.get("frozen") or ())]
        features += [str(n) for n in (block.get("frozen_features") or ())]

    guard = FreezeGuard(names, expressions=expressions, features=features)
    if extra or extra_features:
        guard = guard.extend(extra, features=extra_features)
    return guard


def _expression_of(spec: Mapping[str, Any]) -> str:
    value = spec.get("value")
    return str(value) if value is not None else ""
