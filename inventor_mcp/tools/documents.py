"""Session and document tools: connecting, creating parts, saving, closing."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from ..backend import create_backend
from ..session import Session
from ..units import ANGLE_UNIT_NAMES, LENGTH_UNIT_NAMES
from ._common import guard, open_source


def register(server: Any, session: Session) -> None:
    @server.tool()
    @guard
    def connect(
        backend: Annotated[
            Literal["auto", "inventor", "mock"],
            Field(description="'inventor' drives a live Inventor session over COM; 'mock' simulates "
                              "one so recipes can be written and checked without Inventor; "
                              "'auto' uses Inventor when available."),
        ] = "auto",
        visible: Annotated[bool, Field(description="Show the Inventor window.")] = True,
        start_if_needed: Annotated[
            bool, Field(description="Launch Inventor if it is not already running.")
        ] = True,
    ) -> dict[str, Any]:
        """Connect to Autodesk Inventor (or the offline simulator) and report what you got.

        Call this first. If Inventor is not installed the mock backend still lets you
        author and validate recipes; every result from it is flagged as simulated.
        """
        instance = session.reset_backend(backend)
        info = instance.connect(visible=visible, create=start_if_needed)
        return {
            "backend": instance.name,
            "simulated": instance.name == "mock",
            **info.as_dict(),
        }

    @server.tool()
    @guard
    def session_status() -> dict[str, Any]:
        """What this session currently has open: backend, documents, and the active part."""
        if not session.connected:
            return {
                "connected": False,
                "hint": "Call `connect` to start.",
                "available_backends": _available_backends(),
            }
        backend = session.backend
        active: dict[str, Any] | None = None
        if session.active:
            context = session.context()
            active = {
                "document": context.doc_id,
                "name": context.name,
                "units": context.units,
                "parameters": sorted(context.resolver.known()),
                "sketches": sorted(context.plans),
                "features": list(context.feature_names),
                "last_sketch": context.last_sketch,
                "last_feature": context.last_feature,
            }
        return {
            "connected": True,
            "backend": backend.name,
            "simulated": backend.name == "mock",
            "documents": [info.as_dict() for info in backend.list_documents()],
            "active": active,
        }

    @server.tool()
    @guard
    def new_part(
        name: Annotated[str, Field(description="Part name, used as the file name when saved.")] = "Part",
        units: Annotated[
            Literal[LENGTH_UNIT_NAMES], Field(description="Default length unit for this part.")  # type: ignore[valid-type]
        ] = "mm",
        angle_units: Annotated[Literal[ANGLE_UNIT_NAMES], Field(description="Default angle unit.")] = "deg",  # type: ignore[valid-type]
        material: Annotated[str | None, Field(description="Material name as it appears in Inventor.")] = None,
        template: Annotated[str | None, Field(description="Path to an .ipt template to start from.")] = None,
    ) -> dict[str, Any]:
        """Create an empty parametric part and make it the active document."""
        backend = session.ensure_backend()
        info = backend.new_part(name, template=template, units=units, angle_units=angle_units)
        session.register(info, units, angle_units)
        if material:
            backend.set_material(info.id, material)
        return {"document": info.id, **info.as_dict()}

    @server.tool()
    @guard
    def open_part(
        path: Annotated[str, Field(
            description="Absolute path to a part. An .ipt opens directly; a STEP, "
                        "IGES, SAT or Parasolid file is imported as a solid body.")],
        working_copy: Annotated[bool, Field(
            description="Open the next version of the file instead of the file "
                        "itself -- bracket.ipt becomes bracket_v2.ipt -- so the "
                        "original cannot be changed. Do this before anything that "
                        "will edit the part.")] = False,
    ) -> dict[str, Any]:
        """Open an existing part so its parameters can be inspected and driven.

        Takes an Inventor part or a translated file. A translated file carries
        geometry and not the history that made it, so what arrives has a solid
        body and no parameters: it can be measured and it cannot be driven. That
        is reported rather than left to be discovered.
        """
        return open_source(session, path, working_copy=working_copy)

    @server.tool()
    @guard
    def save_part(
        path: Annotated[str | None, Field(description="Where to save. Omit to save in place.")] = None,
        document: Annotated[str | None, Field(description="Document handle; defaults to the active part.")] = None,
    ) -> dict[str, Any]:
        """Save the part to disk."""
        context = session.context(document)
        info = session.backend.save_document(context.doc_id, path)
        return info.as_dict()

    @server.tool()
    @guard
    def close_part(
        document: Annotated[str | None, Field(description="Document handle; defaults to the active part.")] = None,
        save: Annotated[bool, Field(description="Save before closing.")] = False,
    ) -> dict[str, Any]:
        """Close a part, optionally saving it first."""
        context = session.context(document)
        session.backend.close_document(context.doc_id, save=save)
        session.forget(context.doc_id)
        return {"closed": context.doc_id, "saved": save, "active": session.active}

    @server.tool()
    @guard
    def activate_part(
        document: Annotated[str, Field(description="Document handle to bring to the front.")],
    ) -> dict[str, Any]:
        """Make a different open part the active one."""
        context = session.context(document)
        info = session.backend.activate_document(context.doc_id)
        session.active = context.doc_id
        return info.as_dict()


def _available_backends() -> list[dict[str, Any]]:
    results = []
    for kind in ("inventor", "mock"):
        try:
            create_backend(kind)
            results.append({"backend": kind, "available": True})
        except Exception as exc:
            results.append({"backend": kind, "available": False, "reason": str(exc)})
    return results
