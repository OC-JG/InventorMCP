"""Reading the DFM tool's JSON export.

Deliberately tolerant, and for a specific reason. The DFM tool's own revision
comparison declines to treat a field an older record does not carry as zero --
it reports it as unavailable -- because a missing wall measurement read as
0.00 mm is a critical wall failure that nobody has, and a loop acting on that
would thin a part that was already correct.

So every accessor here returns ``None`` for absent, and the callers are written
to notice. Nothing in this module invents a number.

What the record is *not* is a source of thresholds. The rules state theirs
inline -- "below ABS minimum (1.2 mm)" -- and the export carries them only
inside display strings. The headless bridge therefore sends the material table's
numbers across as numbers, in ``material_limits``, and :class:`MaterialLimits`
is where they land. When a record predates that block the limits are simply
unavailable, and remediation says so rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import InventorMCPError


class DfmReportError(InventorMCPError):
    code = "dfm_report_error"


#: Severity bands, weakest first, as the tool's ``SEVERITY_ORDER`` has them.
SEVERITY_ORDER = ("none", "minor", "major", "critical")


def worse(left: str | None, right: str | None) -> str:
    """The worse of two severities, treating anything unknown as ``none``."""
    return max((left or "none", right or "none"), key=_rank)


def _rank(severity: str) -> int:
    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return 0


def _number(value: Any) -> float | None:
    """A float, or ``None`` -- never a zero standing in for a missing field."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


@dataclass(frozen=True)
class Check:
    """One check, as the tool reported it.

    ``metrics`` are kept as the tool wrote them: ``[label, formatted value]``
    pairs meant for a report page. They are shown to a human and never parsed
    into a number -- a target derived from ``"1.2-3.5 mm"`` by regular
    expression is a target that breaks silently the day someone changes a dash.
    """

    key: str
    name: str
    status: str
    severity: str
    detail: str
    weight: float
    deduction: float
    metrics: tuple[tuple[str, str], ...] = ()

    @property
    def costs_points(self) -> bool:
        return self.deduction > 0

    @property
    def is_advisory(self) -> bool:
        """A check that reports but cannot deduct, such as the corner reminder."""
        return self.weight == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "status": self.status,
            "severity": self.severity,
            "detail": self.detail,
            "weight": self.weight,
            "deduction": self.deduction,
        }


@dataclass(frozen=True)
class MaterialLimits:
    """The moulding limits for the chosen material, from the tool's own table."""

    key: str
    name: str
    wall_lo: float | None
    wall_hi: float | None
    draft_min: float | None
    required_draft: float | None
    lt_max: float | None
    warp_risk: str | None

    @classmethod
    def read(cls, block: Any) -> "MaterialLimits | None":
        if not isinstance(block, dict):
            return None
        return cls(
            key=str(block.get("key") or ""),
            name=str(block.get("name") or block.get("key") or "the material"),
            wall_lo=_number(block.get("wall_lo_mm")),
            wall_hi=_number(block.get("wall_hi_mm")),
            draft_min=_number(block.get("draft_min_deg")),
            required_draft=_number(block.get("required_draft_deg")),
            lt_max=_number(block.get("lt_max")),
            warp_risk=block.get("warp_risk") if isinstance(block.get("warp_risk"), str) else None,
        )


@dataclass(frozen=True)
class DfmReport:
    """A whole DFM run.

    ``declared`` is the tool's ``input`` block: the numbers a person typed into
    the panel -- nominal wall, rib thickness, boss diameter. Several checks are
    judged on these rather than on the mesh, which is the single most useful
    fact about this integration: a recipe already knows those numbers exactly,
    so they can be supplied from the model instead of retyped and guessed at.

    ``mesh`` is the measured half, in raw numbers rather than formatted strings.
    """

    score: float | None
    grade: str | None
    critical_findings: int | None
    budget: float | None
    deduction: float | None
    material: str | None
    checks: tuple[Check, ...]
    declared: dict[str, Any]
    mesh: dict[str, Any]
    health: dict[str, Any]
    limits: MaterialLimits | None
    moulding: dict[str, Any]
    two_shot: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # -- lookups ----------------------------------------------------------

    def check(self, key: str) -> Check | None:
        for candidate in self.checks:
            if candidate.key == key:
                return candidate
        return None

    @property
    def findings(self) -> tuple[Check, ...]:
        """Checks that cost points, worst first."""
        return tuple(sorted(
            (c for c in self.checks if c.costs_points),
            key=lambda c: (-_rank(c.severity), -c.deduction),
        ))

    def measured(self, name: str) -> float | None:
        """One number from the mesh summary, or ``None`` if it is not there."""
        return _number(self.mesh.get(name))

    def declared_number(self, name: str) -> float | None:
        """One number from the declared input block."""
        return _number(self.declared.get(name))

    # -- confidence -------------------------------------------------------

    @property
    def confidence(self) -> str | None:
        value = self.health.get("confidence")
        return value if isinstance(value, str) else None

    @property
    def trustworthy(self) -> bool:
        """Whether the measurements are worth acting on at all.

        A score computed on an inch-scaled or open mesh is arithmetic, not a
        manufacturability judgement -- the tool says so in the export's own
        comments. Changing a part on the strength of one would be changing it
        for no reason, so the loop refuses rather than proceeding quietly.
        """
        if self.health.get("analysable") is False:
            return False
        return self.confidence in (None, "high", "medium")

    @property
    def wall_nominal(self) -> float | None:
        """The wall the checks are judged on: the sphere figure where there is one.

        The tool judges on the inscribed sphere because the ray cast overstates
        any wall whose opposite face is not parallel, and overstating is the
        optimistic direction. Reading the ray figure here would put remediation
        on a different quantity from the check it is trying to satisfy.
        """
        sphere = self.measured("wall_sphere_median_mm")
        if sphere is not None:
            return sphere
        return self.measured("wall_median_mm")

    def summary(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "grade": self.grade,
            "critical_findings": self.critical_findings,
            "budget": self.budget,
            "material": self.material,
            "mesh_confidence": self.confidence,
            "wall_nominal_mm": self.wall_nominal,
            "findings": [c.key for c in self.findings],
        }


def read_report(data: Any) -> DfmReport:
    """Read one exported record.

    Accepts the record itself, or a ``{"report": ...}`` wrapper, because that is
    what a human pasting a file tends to produce.
    """
    if isinstance(data, dict) and "report" in data and "checks" not in data:
        data = data["report"]
    if not isinstance(data, dict):
        raise DfmReportError(
            "A DFM report should be a JSON object.",
            hint="Export one from the DFM tool with 'Export JSON', or let "
                 "`analyse_for_dfm` produce it.",
        )
    if "checks" not in data:
        raise DfmReportError(
            "This JSON has no `checks`, so it is not a DFM report.",
            hint="The DFM tool's JSON export carries `score`, `checks` and "
                 "`mesh_summary`. A PDF report cannot be read here.",
        )

    checks: list[Check] = []
    for entry in data.get("checks") or ():
        if not isinstance(entry, dict):
            continue
        metrics = tuple(
            (str(pair[0]), str(pair[1]))
            for pair in (entry.get("metrics") or ())
            if isinstance(pair, (list, tuple)) and len(pair) >= 2
        )
        checks.append(Check(
            key=str(entry.get("key") or ""),
            name=str(entry.get("name") or entry.get("key") or "a check"),
            status=str(entry.get("status") or "unknown"),
            severity=str(entry.get("severity") or "none"),
            detail=str(entry.get("detail") or ""),
            weight=_number(entry.get("weight")) or 0.0,
            deduction=_number(entry.get("score_deduction")) or 0.0,
            metrics=metrics,
        ))

    scoring = data.get("scoring") if isinstance(data.get("scoring"), dict) else {}
    critical = scoring.get("critical_findings")

    return DfmReport(
        score=_number(data.get("score")),
        grade=data.get("grade") if isinstance(data.get("grade"), str) else None,
        critical_findings=int(critical) if isinstance(critical, (int, float)) else None,
        budget=_number(scoring.get("budget")),
        deduction=_number(scoring.get("deduction")),
        material=data.get("material") if isinstance(data.get("material"), str) else None,
        checks=tuple(checks),
        declared=data.get("input") if isinstance(data.get("input"), dict) else {},
        mesh=data.get("mesh_summary") if isinstance(data.get("mesh_summary"), dict) else {},
        health=data.get("mesh_health") if isinstance(data.get("mesh_health"), dict) else {},
        limits=MaterialLimits.read(data.get("material_limits")),
        moulding=data.get("moulding") if isinstance(data.get("moulding"), dict) else {},
        two_shot=data.get("two_shot") if isinstance(data.get("two_shot"), dict) else {},
        raw=data,
    )
