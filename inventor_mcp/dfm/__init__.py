"""Reading a DFM report, and acting on it parametrically.

The OnlyCat DFM tool measures a mesh and says what is wrong with the part for
injection moulding. This package takes that verdict and turns the parts of it
that *are* parameter changes into parameter changes, rebuilds, and asks the tool
again -- so a finding is closed by a measurement rather than by an assertion
that it has been addressed.

Four things live here, in dependency order:

``report``   reading the tool's JSON, tolerantly.
``freeze``   which parameters are key geometry and may not be touched.
``remedy``   which findings are parameter changes, and what to change them to.
``runner``   running the analyser on an STL, headlessly.
``loop``     apply, rebuild, re-analyse, and know when to stop.

The first three are pure and are tested without Inventor, without a browser and
without Node. Only ``runner`` and ``loop`` do any I/O.
"""

from .report import Check, DfmReport, MaterialLimits, read_report
from .freeze import FreezeGuard, FrozenParameter
from .remedy import Change, Proposal, propose
from .runner import DfmUnavailable, analyse_stl, find_dfm_root, settings_from_roles

__all__ = [
    "Change",
    "Check",
    "DfmReport",
    "DfmUnavailable",
    "FreezeGuard",
    "FrozenParameter",
    "MaterialLimits",
    "Proposal",
    "analyse_stl",
    "find_dfm_root",
    "propose",
    "read_report",
    "settings_from_roles",
]
