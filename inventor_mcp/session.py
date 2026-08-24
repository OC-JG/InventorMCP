"""Per-session state: the backend, the open documents and what they contain.

The MCP tool layer is stateless between calls, so everything a later call needs
to make sense of an earlier one lives here -- which sketch was built last, what
its named entities were, which parameters exist and in what units.  That is
what lets a caller say "extrude it 6 mm" without repeating the sketch name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .backend import create_backend
from .backend.base import Backend, DocInfo
from .dfm.freeze import FreezeGuard
from .errors import DocumentError, NotConnectedError
from .plan import SketchPlan
from .resolve import Resolver
from .units import Dim, Quantity, lookup_unit


@dataclass
class DocumentContext:
    """Everything the server remembers about one open part."""

    doc_id: str
    name: str
    units: str = "mm"
    angle_units: str = "deg"
    resolver: Resolver = field(default_factory=Resolver)
    plans: dict[str, SketchPlan] = field(default_factory=dict)
    feature_names: list[str] = field(default_factory=list)
    last_sketch: str | None = None
    last_feature: str | None = None
    recipe: dict[str, Any] | None = None
    #: Which parameters are key geometry and may not be changed automatically.
    #: Built from the recipe once its parameters are declared; ``None`` until
    #: then, which reads as "nothing is protected here".
    frozen: FreezeGuard | None = None
    #: What the part measured after the last operation, so the next one can
    #: report what it changed rather than only that it ran.
    last_measurement: dict[str, Any] | None = None

    def remember_sketch(self, name: str, plan: SketchPlan) -> None:
        self.plans[name] = plan
        self.last_sketch = name

    def remember_feature(self, name: str) -> None:
        self.feature_names.append(name)
        self.last_feature = name

    def sketch_plan(self, name: str | None) -> tuple[str, SketchPlan]:
        key = name or self.last_sketch
        if key is None:
            raise DocumentError(
                "No sketch has been created yet.",
                hint="Add a `sketch` operation, via `apply_operations` or a recipe.",
            )
        plan = self.plans.get(key)
        if plan is None:
            known = ", ".join(sorted(self.plans)) or "(none)"
            raise DocumentError(
                f"This session did not create a sketch named {key!r}.",
                hint=f"Sketches created here: {known}.",
            )
        return key, plan


class Session:
    """Owns the backend connection and the document contexts."""

    def __init__(self, backend_kind: str = "auto") -> None:
        self.backend_kind = backend_kind
        self._backend: Backend | None = None
        self.contexts: dict[str, DocumentContext] = {}
        self.active: str | None = None

    # -- backend -----------------------------------------------------------
    @property
    def backend(self) -> Backend:
        if self._backend is None:
            raise NotConnectedError()
        return self._backend

    def ensure_backend(self) -> Backend:
        if self._backend is None:
            self._backend = create_backend(self.backend_kind)
        return self._backend

    def reset_backend(self, kind: str) -> Backend:
        if self._backend is not None:
            try:
                self._backend.disconnect()
            except Exception:  # pragma: no cover - best effort
                pass
        self.backend_kind = kind
        self._backend = create_backend(kind)
        self.contexts.clear()
        self.active = None
        return self._backend

    @property
    def connected(self) -> bool:
        return self._backend is not None

    # -- documents ---------------------------------------------------------
    def register(self, info: DocInfo, units: str, angle_units: str) -> DocumentContext:
        context = DocumentContext(
            doc_id=info.id,
            name=info.name,
            units=units,
            angle_units=angle_units,
            resolver=Resolver(units, angle_units),
        )
        self.contexts[info.id] = context
        self.active = info.id
        return context

    def context(self, doc_id: str | None = None) -> DocumentContext:
        key = doc_id or self.active
        if key is None:
            raise DocumentError(
                "No part is open in this session.",
                hint="Call `new_part` (or `build_part_from_recipe`) first.",
            )
        context = self.contexts.get(key)
        if context is None:
            for candidate in self.contexts.values():
                if candidate.name == key:
                    return candidate
            known = ", ".join(f"{c.doc_id} ({c.name})" for c in self.contexts.values()) or "(none)"
            raise DocumentError(
                f"Unknown document {key!r}.", hint=f"Open documents: {known}."
            )
        return context

    def forget(self, doc_id: str) -> None:
        self.contexts.pop(doc_id, None)
        if self.active == doc_id:
            self.active = next(iter(self.contexts), None)

    # -- parameters --------------------------------------------------------
    def sync_parameters(self, doc_id: str) -> None:
        """Refresh a context's expression scope from the backend's parameter table."""
        context = self.context(doc_id)
        table: dict[str, Quantity] = {}
        for parameter in self.backend.list_parameters(context.doc_id):
            try:
                info = lookup_unit(parameter.units)
                table[parameter.name] = Quantity(parameter.value * info.factor, info.dim)
            except Exception:  # pragma: no cover - unusual Inventor unit strings
                table[parameter.name] = Quantity(parameter.value, Dim.UNITLESS)
        context.resolver.parameters = table
