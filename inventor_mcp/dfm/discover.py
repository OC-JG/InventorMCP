"""Working out which parameter plays which DFM role, from evidence.

A recipe can say. A part somebody hands over cannot, and the loop needs to know
before it may change anything -- so something has to work it out, and there are
two very different ways to try.

The wrong way is the parameter's name. ``wall``, ``wall_t``, ``t``, ``thk``,
``WallThickness``: a table of spellings gets most parts right, and the parts it
gets wrong are indistinguishable from the parts it gets right until a loop has
already thinned the wrong dimension. This project has a standing rule about
that, and it holds here.

The right way is what the parameter actually *does*. A shell feature takes its
thickness from somewhere; whatever that expression reads is the wall, not by
resemblance but by construction. An extrude with a taper takes the angle from
somewhere; that is the draft. Those are measurements of the model, and they are
as good as a declaration.

So:

* **evidence maps a role.** One shell, one parameter in its thickness, one
  answer -- and the answer is reported with what it was read from, because it is
  a claim somebody may want to check.
* **ambiguity maps nothing.** Two shells reading two different parameters is not
  a wall; it is two walls and a question. Reported, unmapped.
* **a name is a suggestion and never a mapping.** Where no evidence exists, a
  likely-looking parameter comes back under ``suggestions`` with the exact call
  needed to accept it. Nothing acts on a suggestion.

The unmapped roles matter as much as the mapped ones. A rib ratio judged against
a parameter nobody supplied is judged against the analyser's default, which can
report a rib too thick on a part that has no ribs -- so leaving a role unmapped
and saying so is the useful answer, not a gap in this file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..expressions import referenced_parameters
from .declaration import Declaration
from .roles import ROLES


@dataclass(frozen=True)
class FeatureFacts:
    """One feature, reduced to what it is and what expressions it holds.

    Deliberately not a live COM object: the rules below are then a pure function
    of a few strings, testable without Inventor and without a mesh, and whatever
    the backend can actually read gets normalised into this on the way in.
    """

    name: str
    kind: str
    expressions: dict[str, str] = field(default_factory=dict)

    def expression(self, *names: str) -> tuple[str, str] | None:
        """The first of *names* this feature carries, as (property, expression)."""
        for wanted in names:
            for held, expression in self.expressions.items():
                if held.lower().replace("_", "") == wanted.lower().replace("_", ""):
                    if expression and str(expression).strip():
                        return held, str(expression)
        return None


#: What a feature reading a parameter proves about that parameter.
#:
#: Each entry is (feature kind, the properties to look at, the role it proves,
#: how to say it). The kinds are matched loosely -- Inventor calls a shell
#: feature's type ``kShellFeatureObject`` and this project's own info calls it
#: ``shell`` -- so a substring is enough and a release renaming its enums does
#: not silently stop the evidence working.
EVIDENCE: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("shell", ("thickness",), "wall",
     "the shell feature {feature} takes its wall thickness from it"),
    ("thicken", ("thickness",), "wall",
     "the thicken feature {feature} takes its thickness from it"),
    ("extrude", ("taper", "taperangle"), "draft",
     "the extrude {feature} takes its draft angle from it"),
    ("revolve", ("taper", "taperangle"), "draft",
     "the revolve {feature} takes its draft angle from it"),
)

#: Spellings that suggest a role, used only to offer a candidate a person can
#: accept. Never applied. Ordered longest-first inside each role so ``rib_t``
#: does not match the pattern meant for ``t``.
SUGGESTS: dict[str, tuple[str, ...]] = {
    "wall": ("wall_thickness", "wallthk", "wall_t", "wall", "shell_t", "shell",
             "nominal_wall", "thk", "thickness"),
    "draft": ("draft_angle", "draft_a", "draft", "taper_angle", "taper"),
    "rib_thickness": ("rib_thickness", "rib_thk", "rib_t", "rib_w", "rib"),
    "rib_height": ("rib_height", "rib_h"),
    "rib_fillet": ("rib_fillet", "rib_radius", "rib_r", "rib_root"),
    "boss_od": ("boss_od", "boss_diameter", "boss_d", "boss_dia", "boss"),
    "boss_wall": ("boss_wall", "boss_w", "boss_t", "boss_thickness"),
}


@dataclass
class Discovery:
    """What could be worked out, what could not, and why."""

    declaration: Declaration = field(default_factory=Declaration)
    #: role -> [(parameter, why)] where more than one answer was found
    ambiguous: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    #: role -> parameter, from spelling alone. Never applied.
    suggestions: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = self.declaration.describe()
        if self.ambiguous:
            out["ambiguous"] = {
                role: [{"parameter": name, "because": why} for name, why in found]
                for role, found in sorted(self.ambiguous.items())
            }
        if self.suggestions:
            out["suggestions"] = dict(sorted(self.suggestions.items()))
            out["to_accept_the_suggestions"] = {
                "roles": dict(sorted(self.suggestions.items())),
            }
        out["notes"] = list(self.notes) + list(self.declaration.notes)
        return out


def normalise(description: Mapping[str, Any]) -> FeatureFacts:
    """One feature's facts, out of whatever shape the backend reported.

    Written tolerantly on purpose. ``list_features`` and ``describe_feature``
    report different shapes, releases rename properties, and a feature this
    cannot read contributes no evidence -- which is a smaller problem than one it
    reads wrongly.
    """
    name = str(description.get("name") or description.get("feature") or "")
    kind = str(
        description.get("kind")
        or description.get("type")
        or description.get("feature_type")
        or ""
    )
    expressions: dict[str, str] = {}
    for key, value in description.items():
        if key in ("name", "feature", "kind", "type", "feature_type"):
            continue
        if isinstance(value, str):
            expressions[str(key)] = value
        elif isinstance(value, Mapping):
            # describe_feature nests a feature's own properties and its
            # definition's under separate keys.
            for inner, held in value.items():
                if isinstance(held, str):
                    expressions.setdefault(str(inner), held)
    return FeatureFacts(name=name, kind=kind, expressions=expressions)


def discover(
    features: Iterable[Mapping[str, Any]],
    parameters: Iterable[str],
    *,
    consumed_by: Mapping[str, Sequence[str]] | None = None,
) -> Discovery:
    """Work out the role map from the part itself.

    *features* are feature descriptions, *parameters* the user parameter names
    that exist, and *consumed_by* an optional parameter-to-features index -- a
    second, independent channel for the same evidence, used where a feature's own
    expressions could not be read but the parameter table knows what reads it.
    """
    known = {name.lower(): name for name in parameters}
    facts = [normalise(entry) for entry in features]
    found: dict[str, list[tuple[str, str]]] = {}

    for fact in facts:
        for kind, properties, role, wording in EVIDENCE:
            if kind not in fact.kind.lower():
                continue
            held = fact.expression(*properties)
            if held is None:
                continue
            for referenced in _parameters_in(held[1], known):
                _record(found, role, referenced,
                        wording.format(feature=fact.name or "(unnamed)"))

    # The parameter table's own view, where there is one. A parameter Inventor
    # says is consumed by a shell is the wall whether or not the shell's own
    # thickness expression could be read.
    by_kind = {fact.name: fact.kind.lower() for fact in facts}
    for parameter, consumers in (consumed_by or {}).items():
        canonical = known.get(parameter.lower())
        if canonical is None:
            continue
        for consumer in consumers or ():
            kind = by_kind.get(str(consumer), str(consumer)).lower()
            for wanted, _properties, role, _wording in EVIDENCE:
                if wanted in kind:
                    _record(found, role, canonical,
                            f"Inventor reports {consumer} as consuming it, and "
                            f"{consumer} is a {wanted} feature")

    declaration = Declaration()
    ambiguous: dict[str, list[tuple[str, str]]] = {}
    notes: list[str] = []
    for role, candidates in found.items():
        distinct = sorted({name for name, _ in candidates})
        if len(distinct) == 1:
            declaration.roles[role] = distinct[0]
            declaration.origin[role] = "discovered"
            declaration.evidence[role] = "; ".join(
                sorted({why for _, why in candidates})
            )
            continue
        # Two answers is not an answer. Reported rather than resolved: picking
        # one would be exactly the guess this file exists to avoid.
        ambiguous[role] = candidates
        notes.append(
            f"Could not settle the {role!r} role: {', '.join(distinct)} all have "
            f"a claim to it. Say which with roles={{{role!r}: '<parameter>'}}."
        )

    suggestions = _suggest(
        [role for role in ROLES if role not in declaration.roles and role not in ambiguous],
        known,
    )
    if suggestions:
        notes.append(
            "The rest were matched by name alone, which is not evidence, so they "
            "are offered rather than used. Nothing acts on a suggestion."
        )
    unmapped = [role for role in ROLES
                if role not in declaration.roles and role not in suggestions
                and role not in ambiguous]
    if unmapped:
        notes.append(
            "Nothing in this part points to a parameter for "
            + ", ".join(sorted(unmapped))
            + ". A check judged on a role nothing supplies is judged on the "
            "analyser's own default, which can report a rib too thick on a part "
            "with no ribs -- so declare them or switch that check off."
        )

    return Discovery(declaration=declaration, ambiguous=ambiguous,
                     suggestions=suggestions, notes=notes)


def _parameters_in(expression: str, known: Mapping[str, str]) -> list[str]:
    """Which user parameters an expression reads.

    A literal reads none, and that is the common case for a feature nobody made
    parametric -- a shell whose thickness is ``2 mm`` proves there is a wall and
    proves no parameter drives it, which is worth knowing and is not a role.
    """
    try:
        referenced = referenced_parameters(expression)
    except Exception:
        # Inventor writes expressions this project's parser does not accept --
        # unit suffixes it spells differently, functions it does not have. An
        # expression that will not parse contributes nothing rather than a guess.
        return []
    return sorted(
        known[name.lower()] for name in referenced if name.lower() in known
    )


def _record(found: dict[str, list[tuple[str, str]]], role: str,
            parameter: str, why: str) -> None:
    entries = found.setdefault(role, [])
    if (parameter, why) not in entries:
        entries.append((parameter, why))


_WORD = re.compile(r"[^a-z0-9]+")


def _suggest(roles: Sequence[str], known: Mapping[str, str]) -> dict[str, str]:
    """A likely parameter per role, from spelling alone.

    Longest pattern first, so ``rib_thickness`` is not claimed by the pattern
    meant for ``thickness``, and one parameter is never suggested for two roles.
    """
    out: dict[str, str] = {}
    claimed: set[str] = set()
    ordered = sorted(
        ((role, pattern) for role in roles for pattern in SUGGESTS.get(role, ())),
        key=lambda pair: -len(pair[1]),
    )
    for role, pattern in ordered:
        if role in out:
            continue
        flattened = _WORD.sub("", pattern)
        for lowered, canonical in sorted(known.items()):
            if canonical in claimed:
                continue
            if _WORD.sub("", lowered) == flattened:
                out[role] = canonical
                claimed.add(canonical)
                break
    return out
