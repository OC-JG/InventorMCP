"""Reading a DFM report, and acting on it parametrically.

The OnlyCat DFM tool measures a mesh and says what is wrong with the part for
injection moulding. This package takes that verdict and turns the parts of it
that *are* parameter changes into parameter changes, rebuilds, and asks the tool
again -- so a finding is closed by a measurement rather than by an assertion
that it has been addressed.

Eight things live here, in dependency order:

``report``   reading the tool's JSON, tolerantly.
``declaration`` which parameter means what, and what may not change.
``discover``    working that out from the part itself, where nobody said.
``sources``     deciding which source of it wins.
``freeze``   which parameters are key geometry and may not be touched.
``remedy``   which findings are parameter changes, and what to change them to.
``runner``   running the analyser on an STL, headlessly.
``loop``     apply, rebuild, re-analyse, and know when to stop.

The first three are pure and are tested without Inventor, without a browser and
without Node. Only ``runner`` and ``loop`` do any I/O.
"""

from .declaration import Declaration, merge, read_sidecar, write_sidecar
from .discover import Discovery, discover, facts_from
from .sources import remember, resolve
from .report import Check, DfmReport, MaterialLimits, read_report
from .freeze import FreezeGuard, FrozenParameter
from .remedy import Change, Proposal, propose
from .runner import (
    DfmUnavailable, analyse_stl, compare_reports, find_dfm_root,
    settings_from_roles,
)

__all__ = [
    "Change",
    "Declaration",
    "Discovery",
    "Check",
    "DfmReport",
    "DfmUnavailable",
    "FreezeGuard",
    "FrozenParameter",
    "MaterialLimits",
    "Proposal",
    "analyse_stl",
    "compare_reports",
    "discover",
    "facts_from",
    "merge",
    "remember",
    "resolve",
    "read_sidecar",
    "write_sidecar",
    "find_dfm_root",
    "propose",
    "read_report",
    "settings_from_roles",
]
