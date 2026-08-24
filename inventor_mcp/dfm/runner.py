"""Running the DFM analyser on an STL, without a browser.

The DFM tool is a web page, and the obvious way to drive one is a headless
browser. That is not what happens here. Its analysis modules are pure -- they
take every input as an argument and touch no DOM, which is what lets the tool
run them in a worker -- and its own unit tests import them straight into Node.
So the bridge is a Node script that calls the same functions the page calls, and
the answer it produces is the tool's answer rather than something resembling it.

Checked, not assumed: on the ``hollowFrustum(20, 30, 3, 2)`` fixture with the
tool's own clean inputs, this bridge returns a score of 100 out of a budget of
100, which is what ``test/unit.mjs`` asserts for that part.

What it does not do is read STEP. That path needs a 6 MB OpenCascade WASM module
fetched from a CDN, and Inventor writes STL perfectly well.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import InventorMCPError
from .remedy import ROLES
from .report import DfmReport, read_report

#: Where the script that does the work lives, next to this file.
BRIDGE = Path(__file__).with_name("headless.mjs")

#: Environment variables consulted, in order, for the DFM checkout.
ROOT_VARIABLES = ("INVENTOR_MCP_DFM_ROOT", "DFM_ROOT")

#: How long to let one analysis run. Ray-casting a large mesh is genuinely slow
#: -- seconds, sometimes tens of them -- so this is generous, and a part big
#: enough to exceed it is a part worth knowing about rather than waiting on.
TIMEOUT_SECONDS = 600


class DfmUnavailable(InventorMCPError):
    code = "dfm_unavailable"


class DfmFailed(InventorMCPError):
    code = "dfm_failed"


def find_dfm_root(explicit: str | None = None) -> Path:
    """Locate a checkout of the DFM tool, or say precisely what is missing.

    Looked for in the order somebody would expect: what was asked for, then the
    environment, then next to this repository -- a sibling ``dfm`` directory is
    where the two end up when both are cloned into one workspace.
    """
    # An explicitly named path is not a hint. Falling through to a different
    # checkout would analyse the part against rules the caller did not ask for
    # and report success, so a path that is wrong is said to be wrong.
    if explicit:
        path = Path(explicit).expanduser()
        if _looks_like_dfm(path):
            return path
        raise DfmUnavailable(
            f"{path} is not a checkout of the DFM tool: "
            f"src/rules/engine.js is not there.",
            hint="Point dfm_root at the top of a clone of the OnlyCat DFM "
                 "repository.",
        )

    tried: list[str] = []
    for variable in ROOT_VARIABLES:
        candidate = os.environ.get(variable)
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if _looks_like_dfm(path):
            return path
        # Somebody set this on purpose, so a wrong value is an error to fix and
        # not a hint to fall past: silently using the pinned submodule instead
        # would analyse against rules the variable was set to override.
        raise DfmUnavailable(
            f"{variable} points at {path}, which is not a checkout of the DFM "
            f"tool: src/rules/engine.js is not there.",
            hint=f"Fix or unset {variable}. Without it, the dfm/ submodule "
                 f"pinned in this repository is used.",
        )

    here = Path(__file__).resolve().parents[2]
    # The repository's own submodule first: it is pinned to the version the
    # drift tests ran against, where a sibling checkout is whatever somebody
    # last pulled. An explicit path or environment variable still overrides.
    for candidate in (here / "dfm", here.parent / "dfm", here.parent / "DFM"):
        tried.append(str(candidate))
        if _looks_like_dfm(candidate):
            return candidate

    raise DfmUnavailable(
        "The DFM tool is not where this could find it, so the analysis cannot run.",
        hint=("Clone the OnlyCat DFM repository and point INVENTOR_MCP_DFM_ROOT at it, "
              "or pass dfm_root. Looked in: " + ", ".join(tried or ["nowhere"]) + "."),
    )


def _looks_like_dfm(path: Path) -> bool:
    return (path / "src" / "rules" / "engine.js").is_file()


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        raise DfmUnavailable(
            "Node is not installed, and the DFM analyser is JavaScript.",
            hint="Install Node 18 or newer; the analysis needs no npm install.",
        )
    return node


def settings_from_roles(
    roles: Mapping[str, str],
    values: Mapping[str, float],
    base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The analyser's declared inputs, taken from the model's own parameters.

    This is the part of the integration worth the most. Several of the tool's
    checks are judged on numbers a person types into a panel -- nominal wall, rib
    thickness, boss diameter -- and every one of those is a parameter the model
    already knows exactly. Typed in, they are a recollection; read from the
    parameter table they are the part. The rib ratios in particular are computed
    against the *declared* wall rather than the measured one, so a wall figure
    that is merely close makes every one of them slightly wrong.

    Anything not covered by a role keeps whatever *base* says, and anything not
    in *base* keeps the tool's own default.
    """
    settings: dict[str, Any] = dict(base or {})
    for role, parameter in roles.items():
        if role not in ROLES:
            raise ValueError(f"Unknown DFM role {role!r}. Known: {sorted(ROLES)}.")
        value = values.get(parameter)
        if value is None:
            continue
        settings[ROLES[role][0]] = round(float(value), 6)
    return settings


def compare_reports(
    before: str | os.PathLike[str],
    after: str | os.PathLike[str],
    *,
    dfm_root: str | None = None,
    save_to: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """What moved between two runs, as the DFM tool itself reads it.

    Its own ``compareRuns`` rather than a diff written here, for the reason its
    comments give: it knows which direction is better for each measurement, and
    it declines to mislead. A score that rose because the material changed, or
    because a different set of checks ran, comes back with that stated above the
    diff. A comparison written here would report the five points and not the
    reason for them.

    This is what makes a versioned copy worth having: ``bracket.ipt`` against
    ``bracket_v3.ipt`` is the question somebody actually asks on the second pass.
    """
    root = find_dfm_root(dfm_root)
    node = _node()
    for label, where in (("before", before), ("after", after)):
        if not Path(where).is_file():
            raise DfmFailed(
                f"There is no {label} report at {where}.",
                hint="Both are JSON records: either exported from the DFM tool, or "
                     "written by `check_manufacture` and each round of "
                     "`improve_for_manufacture`.",
            )

    with tempfile.TemporaryDirectory(prefix="inventor-mcp-dfm-") as scratch:
        output = Path(save_to) if save_to else Path(scratch) / "comparison.json"
        command = [
            node, str(BRIDGE),
            "--before", str(Path(before).resolve()),
            "--after", str(Path(after).resolve()),
            "--out", str(output),
            "--dfm-root", str(root),
        ]
        try:
            finished = subprocess.run(command, capture_output=True, text=True,
                                      timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise DfmFailed("Comparing the two reports did not finish.") from None
        if finished.returncode != 0:
            detail = (finished.stderr or finished.stdout or "").strip().splitlines()
            raise DfmFailed(
                "The comparison failed: " + (detail[-1] if detail else "no output."),
                hint=f"Ran: {' '.join(command[1:])}",
            )
        try:
            return json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DfmFailed(f"The comparison wrote nothing readable: {exc}") from exc


def analyse_stl(
    stl: str | os.PathLike[str],
    settings: Mapping[str, Any] | None = None,
    *,
    dfm_root: str | None = None,
    gate: Sequence[float] | None = None,
    pull_axis: str = "+z",
    save_report_to: str | os.PathLike[str] | None = None,
) -> DfmReport:
    """Analyse one STL and return the report.

    *settings* are the DFM tool's own setting names -- ``material``, ``wallThk``,
    ``surfaceFinish`` and so on. An unknown name is refused by the bridge rather
    than ignored: a misspelled ``wallThk`` that silently scores as the 2.0 mm
    default is exactly the sort of quietly wrong answer this must not produce.
    """
    root = find_dfm_root(dfm_root)
    node = _node()
    source = Path(stl)
    if not source.is_file():
        raise DfmFailed(
            f"There is no STL at {source}.",
            hint="Export one first: `export_model(path=..., format='stl')`.",
        )

    with tempfile.TemporaryDirectory(prefix="inventor-mcp-dfm-") as scratch:
        settings_file = Path(scratch) / "settings.json"
        settings_file.write_text(json.dumps(dict(settings or {})), encoding="utf-8")
        output = Path(save_report_to) if save_report_to else Path(scratch) / "report.json"

        command = [
            node, str(BRIDGE),
            "--stl", str(source),
            "--settings", str(settings_file),
            "--out", str(output),
            "--dfm-root", str(root),
            "--pull-axis", pull_axis,
        ]
        if gate is not None:
            command += ["--gate", ",".join(f"{float(v):.6f}" for v in gate)]

        try:
            finished = subprocess.run(
                command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise DfmFailed(
                f"The DFM analysis did not finish within {TIMEOUT_SECONDS} seconds.",
                hint="A very dense mesh is the usual cause. Export with a coarser "
                     "tolerance, or analyse a simplified body.",
            ) from None

        if finished.returncode != 0:
            detail = (finished.stderr or finished.stdout or "").strip().splitlines()
            raise DfmFailed(
                "The DFM analysis failed: " + (detail[-1] if detail else "no output."),
                hint=f"Ran: {' '.join(command[1:])}",
            )
        try:
            data = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DfmFailed(f"The DFM analysis wrote nothing readable: {exc}") from exc

    return read_report(data)
