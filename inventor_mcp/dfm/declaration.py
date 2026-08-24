"""What a part declares about its own manufacture, and where that is kept.

A recipe can say which parameter is the wall and which dimensions are not to be
touched, because a recipe is written. A part somebody hands over says nothing --
and the loop needs the same two answers before it may change anything.

So the declaration becomes a thing in its own right, with four places it can
come from, in this order of authority:

1. **given at the call** -- what the person asking said, this time;
2. **the part itself** -- read back out of the document, where Inventor lets us
   put it, so it survives being closed and reopened;
3. **a sidecar** -- ``bracket.dfm.json`` beside ``bracket.ipt``, which works
   everywhere including the simulator and is readable by a person;
4. **the recipe** -- when the part was built here, in this session;
5. **discovered** -- inferred from what the part's features actually consume,
   which is evidence, and never from what a parameter is called, which is not.

Roles from a higher source replace lower ones. Freezes never do: they are
unioned, always, so no source can release protection another one asked for.
That asymmetry is the whole safety property -- a run may be told to protect more,
never less, and taking something out means editing the declaration deliberately.

Every role carries where it came from. A map that mixes a person's statement with
this code's inference is only trustworthy if the reader can tell which is which,
and a discovered role is a claim about evidence that somebody may want to check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..errors import InventorMCPError
from .roles import ROLES

#: The file a part's declaration lives in when it cannot live in the part.
SIDECAR_SUFFIX = ".dfm.json"

#: What this file writes, so a future reader can tell what it is looking at.
FORMAT = "inventor-mcp/dfm-declaration/1"

#: Where a declaration came from, weakest first. Authority is the order here.
SOURCES = ("discovered", "the recipe", "a sidecar file", "the part itself",
           "given at the call")


class DeclarationError(InventorMCPError):
    code = "dfm_declaration_error"


@dataclass
class Declaration:
    """Which parameter means what, and what may not be changed.

    ``evidence`` says why each role is believed, keyed by role. A role a person
    stated needs no evidence and gets none; a role this code inferred carries
    what it inferred it from, because that is a claim somebody may want to check
    before a loop acts on it.
    """

    roles: dict[str, str] = field(default_factory=dict)
    frozen: list[str] = field(default_factory=list)
    frozen_features: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    #: role -> where that role came from
    origin: dict[str, str] = field(default_factory=dict)
    #: role -> what it was inferred from, for the inferred ones only
    evidence: dict[str, str] = field(default_factory=dict)
    #: Anything worth saying about how this was arrived at.
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        unknown = sorted(set(self.roles) - set(ROLES))
        if unknown:
            raise DeclarationError(
                f"Unknown DFM role(s) {unknown}.",
                hint=f"Known roles: {sorted(ROLES)}.",
            )

    # -- reading and writing ---------------------------------------------

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, *,
                  source: str = "a sidecar file") -> "Declaration":
        """Read a declaration, tolerating anything an older writer produced.

        A field that is not there is absent, never empty-by-assumption: an
        unreadable frozen list read as "nothing is frozen" would take the
        protection off exactly when it was most wanted.
        """
        if not data:
            return cls()
        if not isinstance(data, Mapping):
            raise DeclarationError("A DFM declaration should be a JSON object.")

        roles = data.get("parameters")
        if not isinstance(roles, Mapping):
            roles = data.get("roles") if isinstance(data.get("roles"), Mapping) else {}
        roles = {str(k): str(v) for k, v in roles.items()}

        frozen = data.get("frozen")
        features = data.get("frozen_features")
        settings = data.get("settings")
        if frozen is not None and not isinstance(frozen, (list, tuple)):
            raise DeclarationError(
                f"`frozen` should be a list of parameter names, not "
                f"{type(frozen).__name__}. Refusing to read it as 'nothing is "
                f"protected'."
            )
        if features is not None and not isinstance(features, (list, tuple)):
            raise DeclarationError(
                "`frozen_features` should be a list of feature names."
            )
        return cls(
            roles=roles,
            frozen=[str(name) for name in (frozen or ())],
            frozen_features=[str(name) for name in (features or ())],
            settings=dict(settings) if isinstance(settings, Mapping) else {},
            origin={role: source for role in roles},
            evidence={str(k): str(v) for k, v in
                      (data.get("evidence") or {}).items()
                      if isinstance(data.get("evidence"), Mapping)},
            notes=[str(n) for n in (data.get("notes") or ())],
        )

    def as_dict(self, *, for_storage: bool = False) -> dict[str, Any]:
        """As JSON. ``for_storage`` writes the form a recipe's ``dfm`` block uses."""
        out: dict[str, Any] = {
            "parameters": dict(self.roles),
            "frozen": list(self.frozen),
            "frozen_features": list(self.frozen_features),
            "settings": dict(self.settings),
        }
        if for_storage:
            out["format"] = FORMAT
        if self.evidence:
            out["evidence"] = dict(self.evidence)
        if not for_storage:
            out["role_came_from"] = dict(self.origin)
            if self.notes:
                out["notes"] = list(self.notes)
        return out

    @property
    def empty(self) -> bool:
        return not (self.roles or self.frozen or self.frozen_features or self.settings)

    def describe(self) -> dict[str, Any]:
        """The form a tool result shows: what is mapped, from where, and why."""
        return {
            "roles": {
                role: {
                    "parameter": parameter,
                    "from": self.origin.get(role, "unknown"),
                    **({"evidence": self.evidence[role]} if role in self.evidence else {}),
                }
                for role, parameter in sorted(self.roles.items())
            },
            "unmapped": [role for role in sorted(ROLES) if role not in self.roles],
            "frozen": list(self.frozen),
            "frozen_features": list(self.frozen_features),
            "settings": dict(self.settings),
            "notes": list(self.notes),
        }


def merge(*declarations: Declaration | None) -> Declaration:
    """Combine declarations, weakest first.

    Roles and settings from later arguments win. Frozen names never lose: they
    are unioned across every source, because a run may be told to protect more
    and must never be able to protect less. A declaration that could take a
    freeze off would make every freeze advisory.
    """
    out = Declaration()
    for declaration in declarations:
        if declaration is None:
            continue
        for role, parameter in declaration.roles.items():
            out.roles[role] = parameter
            out.origin[role] = declaration.origin.get(role, "unknown")
            if role in declaration.evidence:
                out.evidence[role] = declaration.evidence[role]
            else:
                out.evidence.pop(role, None)
        for name in declaration.frozen:
            if name not in out.frozen:
                out.frozen.append(name)
        for name in declaration.frozen_features:
            if name not in out.frozen_features:
                out.frozen_features.append(name)
        for key, value in declaration.settings.items():
            if key == "checks" and isinstance(value, Mapping):
                merged = dict(out.settings.get("checks") or {})
                merged.update(value)
                out.settings["checks"] = merged
            else:
                out.settings[key] = value
        for note in declaration.notes:
            if note not in out.notes:
                out.notes.append(note)
    return out


def given(roles: Mapping[str, str] | None = None,
          frozen: Iterable[str] = (),
          frozen_features: Iterable[str] = (),
          settings: Mapping[str, Any] | None = None,
          *, source: str = "given at the call") -> Declaration:
    """A declaration from arguments, for merging with the stored ones."""
    mapped = {str(k): str(v) for k, v in (roles or {}).items()}
    return Declaration(
        roles=mapped,
        frozen=[str(n) for n in frozen],
        frozen_features=[str(n) for n in frozen_features],
        settings=dict(settings or {}),
        origin={role: source for role in mapped},
    )


def from_recipe(recipe: Mapping[str, Any] | None) -> Declaration:
    """What a recipe's ``dfm`` block declares, plus its ``frozen`` parameters."""
    if not isinstance(recipe, Mapping):
        return Declaration()
    block = recipe.get("dfm")
    declaration = Declaration.from_dict(
        block if isinstance(block, Mapping) else None, source="the recipe",
    )
    for spec in recipe.get("parameters") or ():
        if isinstance(spec, Mapping) and spec.get("frozen") and spec.get("name"):
            name = str(spec["name"])
            if name not in declaration.frozen:
                declaration.frozen.append(name)
    return declaration


# ---------------------------------------------------------------------------
# The sidecar
# ---------------------------------------------------------------------------


def sidecar_for(path: str | Path) -> Path:
    source = Path(path)
    return source.with_name(source.stem + SIDECAR_SUFFIX)


def read_sidecar(path: str | Path) -> Declaration | None:
    """The declaration beside *path*, or ``None`` if there is not one.

    A sidecar that exists and will not parse is an error rather than a silent
    ``None``: somebody wrote it on purpose, and running as though it were absent
    would ignore a freeze they asked for.
    """
    beside = sidecar_for(path)
    if not beside.is_file():
        return None
    try:
        data = json.loads(beside.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeclarationError(
            f"{beside} exists but could not be read: {exc}",
            hint="Fix or delete it. Carrying on as though it were absent would "
                 "ignore whatever it protects.",
        ) from exc
    return Declaration.from_dict(data, source="a sidecar file")


def write_sidecar(path: str | Path, declaration: Declaration) -> Path:
    """Write *declaration* beside *path* and return where it went."""
    beside = sidecar_for(path)
    beside.write_text(
        json.dumps(declaration.as_dict(for_storage=True), indent=2) + "\n",
        encoding="utf-8",
    )
    return beside
