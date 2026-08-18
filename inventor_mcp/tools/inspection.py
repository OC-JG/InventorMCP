"""Tools that read the model back: structure, selectors, measurements, output."""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from pydantic import Field

from ..backend.base import ExportRequest, ScreenshotRequest
from ..builder import resolve_selector
from ..schema import Selector
from ..session import Session
from ._common import display_box, display_length, display_point, guard


def register(server: Any, session: Session) -> None:
    @server.tool(
        description="The whole picture of a part: parameters, sketches, the feature tree and "
        "size. Start here when you need to decide what to change.",
    )
    @guard
    def inspect_part(
        document: Annotated[str | None, Field(description="Target part; defaults to the active one.")] = None,
        include_model_parameters: Annotated[
            bool, Field(description="Also list Inventor's own d0/d1 model parameters.")
        ] = False,
    ) -> dict[str, Any]:
        context = session.context(document)
        backend = session.backend
        parameters = backend.list_parameters(context.doc_id, include_model=include_model_parameters)
        report: dict[str, Any] = {
            "document": context.doc_id,
            "name": context.name,
            "units": context.units,
            "simulated": backend.name == "mock",
            "parameters": [p.as_dict() for p in parameters],
            "sketches": [s.as_dict() for s in backend.list_sketches(context.doc_id)],
            "features": [f.as_dict() for f in backend.list_features(context.doc_id)],
        }
        try:
            properties = backend.mass_properties(context.doc_id)
            report["size"] = display_box(properties.bounding_box, context.units)
            report["volume_cm3"] = round(properties.volume, 4)
            if properties.mass is not None:
                report["mass_kg"] = round(properties.mass, 6)
        except Exception:
            report["size"] = None
        under = [s for s in report["sketches"] if s.get("fully_constrained") is False]
        if under:
            report["warnings"] = [
                f"Sketch {s['name']!r} is not fully constrained; a parameter change may move "
                "geometry in unexpected ways." for s in under
            ]
        return report

    @server.tool(
        description="Preview what a selector matches before using it in a fillet, chamfer, shell "
        "or thread. Returns handles, sizes and positions so an ambiguous selection can be "
        "narrowed with `near`, `limit` or an explicit `ids` list. Handles are only valid "
        "until the model next rebuilds.",
    )
    @guard
    def select_topology(
        selector: Annotated[
            dict[str, Any],
            Field(description="{kind:'edge'|'face', feature?, filter?, near?, within?, "
                              "min_length?, max_length?, limit?, ids?}"),
        ],
        document: Annotated[str | None, Field(description="Target part.")] = None,
    ) -> dict[str, Any]:
        parsed = Selector.model_validate(selector)
        context = session.context(document)
        resolved = resolve_selector(parsed, context.resolver)
        matches = session.backend.select(context.doc_id, resolved)
        unit = context.units
        return {
            "document": context.doc_id,
            "selector": parsed.model_dump(exclude_none=True),
            "count": len(matches),
            "matches": [
                {
                    "id": match.id,
                    "kind": match.kind,
                    "description": match.description,
                    "feature": match.feature,
                    "geometry": match.geometry,
                    "midpoint": display_point(match.midpoint, unit),
                    "normal": list(match.normal) if match.normal else None,
                    "length": display_length(match.length, unit) if match.length is not None else None,
                    "area": round(match.area, 4) if match.area is not None else None,
                }
                for match in matches
            ],
            "units": unit,
            "note": "Handles expire on the next rebuild.",
        }

    @server.tool(
        description="Measure the part: bounding box, volume, surface area, mass and centre of "
        "mass. Mass needs a material to have been applied.",
    )
    @guard
    def measure_part(
        document: Annotated[str | None, Field(description="Target part.")] = None,
    ) -> dict[str, Any]:
        context = session.context(document)
        properties = session.backend.mass_properties(context.doc_id)
        return {
            "document": context.doc_id,
            "units": context.units,
            "bounding_box": display_box(properties.bounding_box, context.units),
            "volume_cm3": round(properties.volume, 6),
            "surface_area_cm2": round(properties.area, 6),
            "mass_kg": round(properties.mass, 6) if properties.mass is not None else None,
            "material": properties.material,
            "center_of_mass": display_point(properties.center_of_mass, context.units),
            "simulated": session.backend.name == "mock",
        }

    @server.tool(
        description="Export the part to a neutral CAD or mesh format: STEP for downstream CAD, "
        "STL or 3MF for printing, IGES or SAT for older tool-chains, DWG/DXF for 2D.",
    )
    @guard
    def export_model(
        path: Annotated[str, Field(description="Output file path. The extension is corrected to match the format.")],
        format: Annotated[
            Literal["step", "stl", "iges", "sat", "dwg", "dxf", "obj", "3mf", "ipt"],
            Field(description="Output format."),
        ] = "step",
        document: Annotated[str | None, Field(description="Target part.")] = None,
    ) -> dict[str, Any]:
        context = session.context(document)
        result = session.backend.export(
            context.doc_id, ExportRequest(path=os.path.abspath(path), format=format)
        )
        return {"document": context.doc_id, **result}

    @server.tool(
        description="Render the part to a PNG so it can actually be looked at. Use after building "
        "to confirm the shape matches the description.",
    )
    @guard
    def capture_view(
        path: Annotated[str, Field(description="Output image path (.png).")],
        orientation: Annotated[
            Literal["iso", "front", "top", "right", "back"],
            Field(description="Camera orientation."),
        ] = "iso",
        width: Annotated[int, Field(ge=64, le=4096, description="Image width in pixels.")] = 1200,
        height: Annotated[int, Field(ge=64, le=4096, description="Image height in pixels.")] = 900,
        display_mode: Annotated[
            Literal["shaded", "hidden_line", "wireframe"], Field(description="Render style.")
        ] = "shaded",
        document: Annotated[str | None, Field(description="Target part.")] = None,
    ) -> dict[str, Any]:
        context = session.context(document)
        result = session.backend.screenshot(
            context.doc_id,
            ScreenshotRequest(
                path=os.path.abspath(path),
                orientation=orientation,
                width=width,
                height=height,
                display_mode=display_mode,
            ),
        )
        return {"document": context.doc_id, **result}
