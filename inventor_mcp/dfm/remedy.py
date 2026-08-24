"""Turning DFM findings into parameter changes.

What this module will and will not do is the whole design, so it is worth
stating plainly.

**It changes parameters, never geometry.** Every fix comes out as a new value
for a named parameter, which means it is revisable, diffable and reversible, and
the model stays the parametric thing it was built as. A fix that reached in and
moved a face would leave a part nobody could revise.

**Ratio fixes come out as expressions, not numbers.** A rib that should be 45%
of the wall becomes ``wall_t * 0.45``, not ``0.9 mm``. The relationship is then
structurally true rather than true until someone edits the wall, and a later
wall change carries the ribs with it instead of quietly re-breaking the check.
This makes the model *more* parametric than it was, which is the opposite of
what an optimiser usually does to one.

**Every number it aims at comes from the report.** The material's wall band, the
required draft, the measured wall -- all of it arrives from the tool. The one
thing this file does hold is the *targets*: aim for 45% of the wall, not 80%;
aim a tenth above the material floor, not exactly on it. Those are choices about
where inside a band to sit, and the bands themselves stay where they are stated.

**Which is a duplication, and it is handled by measurement rather than by care.**
The tool's thresholds live as literals inside its rules and are not exported, so
a target here could drift out of agreement with the check it is trying to
satisfy. Two things answer that. The loop re-runs the analyser after applying a
change and reports whether the finding actually cleared -- so a stale target
shows up as a proposal that did not work, not as a part quietly changed for
nothing. And ``tests/test_dfm_targets.py`` puts every target through the real
engine and asserts the check comes back clean, which is the alarm that rings if
a threshold moves.

**Anything that is not a parameter change is reported, not attempted.** An
undercut needs a decision about tooling; a sink needs coring; a corner radius
cannot be measured from a mesh at all, so a change to one could never be
verified. Those come back in the report with the tool's own wording, marked with
why the loop is not touching them, rather than being silently dropped -- a
finding nobody mentioned is a finding everybody assumes was handled.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from .freeze import FreezeGuard, FrozenParameter
from .roles import ROLES
from .report import Check, DfmReport

#: Where inside each band to aim.
#:
#: Every one of these is a choice, not a threshold, and each is a fraction of a
#: band the tool states rather than a limit of its own:
#:
#: * ribs are wanted in 0.4--0.5x the wall, so aim at the middle;
#: * rib roots in 0.25--0.4x, so aim at 0.3;
#: * boss walls in 0.5--0.7x, so aim at 0.6, and a boss diameter of 2.4x the
#:   wall is then the largest that keeps screw retention (OD/4) inside it;
#: * rib height is capped at 2.5x its thickness, so aim at the cap -- going
#:   under it would shorten a rib further than anything asked for;
#: * a wall a tenth above the material floor, because sitting exactly on a
#:   minimum is what the check itself calls "no margin for variation";
#: * a draft a degree above what is required, which is the figure the draft
#:   check prints as its own advice.
RIB_THICKNESS_OF_WALL = 0.45
RIB_FILLET_OF_WALL = 0.30
RIB_HEIGHT_OF_THICKNESS = 2.5
BOSS_WALL_OF_WALL = 0.60
BOSS_OD_OF_WALL = 2.4
WALL_MARGIN_OVER_FLOOR = 1.10
WALL_MARGIN_UNDER_CEILING = 0.90
DRAFT_MARGIN_DEG = 1.0


@dataclass(frozen=True)
class Change:
    """One parameter edit, and everything needed to judge it."""

    parameter: str
    role: str
    expression: str
    check: str
    why: str
    was: str | None = None
    #: The value aimed at, in millimetres or degrees, where there is one. An
    #: expression-valued change has a target too -- what it works out to now.
    target: float | None = None
    #: ``declared`` means the check reads this number directly, so assigning the
    #: target satisfies it arithmetically. ``measured`` means the check reads the
    #: mesh, so the parameter is an actuator and the change is a correction of
    #: the measured shortfall -- which only converges because the loop measures
    #: again afterwards.
    kind: str = "declared"
    #: Whether this alters what the part *does*, as against how well it moulds.
    #: A thinner wall, a shorter rib and a smaller boss all change function, and
    #: are the changes most worth freezing.
    functional: bool = False
    #: For a ratio change, the parameter it is a fraction of, and the fraction.
    #: Kept so that ``target`` can be restated against the driver's *new* value
    #: when the driver is changing in the same pass -- otherwise the report says
    #: a rib is going to 0.90 mm while the expression it just wrote makes it
    #: 1.27, and the number a reader checks is the wrong one.
    driver: str | None = None
    fraction: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "parameter": self.parameter,
            "role": self.role,
            "to": self.expression,
            "from": self.was,
            "target": round(self.target, 4) if self.target is not None else None,
            "answers": self.check,
            "why": self.why,
            "basis": self.kind,
            "changes_function": self.functional,
        }


@dataclass(frozen=True)
class Deferred:
    """A finding the loop is not acting on, and why not.

    Reported rather than dropped. Four reasons, and they mean different things
    to whoever reads the result:

    ``frozen``        a fix exists and was refused. Unfreeze to allow it.
    ``unmapped``      a fix exists but no parameter is declared for the role.
    ``decision``      no single parameter fixes it; someone has to choose.
    ``unverifiable``  a change could be made but the tool cannot measure the
                      result, so the loop would be working blind.
    """

    check: str
    reason: str
    why: str
    finding: str = ""
    role: str | None = None
    frozen: FrozenParameter | None = None
    severity: str = "none"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "check": self.check,
            "not_acted_on": self.reason,
            "why": self.why,
            "severity": self.severity,
        }
        if self.role:
            out["role"] = self.role
        if self.finding:
            out["finding"] = self.finding
        if self.frozen is not None:
            out["frozen"] = self.frozen.as_dict()
        return out


@dataclass
class Proposal:
    """Everything the report implies: what to change, and what to say instead."""

    changes: tuple[Change, ...] = ()
    deferred: tuple[Deferred, ...] = ()
    notes: tuple[str, ...] = ()
    #: An input the analyser was missing rather than a change to the part --
    #: presently only the gate position, which the tool searches for itself.
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return bool(self.changes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "changes": [c.as_dict() for c in self.changes],
            "not_acted_on": [d.as_dict() for d in self.deferred],
            "notes": list(self.notes),
            "inputs": dict(self.inputs),
        }


# ---------------------------------------------------------------------------
# The working context one rule sees
# ---------------------------------------------------------------------------


@dataclass
class _Context:
    report: DfmReport
    roles: dict[str, str]
    guard: FreezeGuard
    values: dict[str, float]
    expressions: dict[str, str]

    def parameter(self, role: str) -> str | None:
        return self.roles.get(role)

    def value(self, role: str) -> float | None:
        name = self.roles.get(role)
        if name is None:
            return None
        return self.values.get(name)

    def was(self, role: str) -> str | None:
        name = self.roles.get(role)
        return self.expressions.get(name) if name else None

    def wall_expression(self) -> str | None:
        """The wall parameter's name, for writing a ratio against it."""
        return self.roles.get("wall")


Rule = Callable[[_Context], list[Change | Deferred]]
_RULES: list[Rule] = []


def _rule(fn: Rule) -> Rule:
    _RULES.append(fn)
    return fn


def _length(value: float) -> str:
    """A length as an expression, with its unit, so the recipe's units cannot
    silently reinterpret it. The DFM tool measures in millimetres and validates
    that a mesh plausibly is; a bare number in an inch recipe would be 25.4x
    wrong and would still build."""
    return f"{round(value, 4):g} mm"


def _angle(value: float) -> str:
    return f"{round(value, 4):g} deg"


def _needs(context: _Context, role: str, check: Check, what: str,
           reason_when_missing: str | None = None) -> Deferred | None:
    """The reason *role* cannot be used, or ``None`` if it can.

    *what* is one short clause saying what the fix would have been -- it is
    prefixed to the refusal. An earlier version took the first sentence of the
    full explanation instead, and every explanation of a ratio starts with a
    number, so "A rib 4.7x its thickness..." was cut at the decimal point and
    the refusal read "A rib 4. rib_h is key geometry."
    """
    name = context.parameter(role)
    if name is None:
        return Deferred(
            check=check.key, reason="unmapped", role=role, severity=check.severity,
            finding=check.detail,
            why=reason_when_missing or (
                f"{what} No parameter is declared for the {role!r} role, so there is "
                f"nothing to change. Declare one in the recipe's `dfm.parameters` "
                f"block -- {ROLES[role][1]}."
            ),
        )
    frozen = context.guard.check(name)
    if frozen is not None:
        return Deferred(
            check=check.key, reason="frozen", role=role, frozen=frozen,
            severity=check.severity, finding=check.detail,
            why=f"{what} {frozen.explain()}.",
        )
    return None


# ---------------------------------------------------------------------------
# Wall
# ---------------------------------------------------------------------------


def _wall_floor(report: DfmReport) -> tuple[float | None, str]:
    """The lowest wall this part may have, and what sets it.

    An FPC insert raises the floor above the material's own minimum: the
    overmould has to contain the flex plus cover on both faces. The wall check
    applies that floor, so remediation has to aim at the same one or it will
    propose a wall the check still fails.
    """
    limits = report.limits
    material_floor = limits.wall_lo if limits else None
    fpc = report.declared.get("fpc")
    if isinstance(fpc, dict) and fpc.get("enabled"):
        thickness = fpc.get("thickness")
        cover = fpc.get("cover")
        if isinstance(thickness, (int, float)) and isinstance(cover, (int, float)):
            fpc_floor = float(thickness) + 2 * float(cover)
            if material_floor is None or fpc_floor > material_floor:
                return fpc_floor, (
                    f"the FPC-overmould floor of {fpc_floor:.2f} mm "
                    f"({thickness} mm of flex plus 2x{cover} mm of cover)"
                )
    if material_floor is None:
        return None, "the material's minimum wall"
    return material_floor, f"{limits.name}'s {material_floor} mm minimum"


@_rule
def _wall(context: _Context) -> list[Change | Deferred]:
    """A wall outside the material's band, corrected by what it measured short."""
    report = context.report
    check = report.check("wall")
    if check is None or not check.costs_points:
        return []

    measured = report.wall_nominal
    floor, floor_says = _wall_floor(report)
    ceiling = report.limits.wall_hi if report.limits else None

    if measured is None:
        return [Deferred(
            check="wall", reason="decision", severity=check.severity,
            finding=check.detail,
            why="The record carries no measured wall thickness, so there is no "
                "shortfall to correct. Re-export the report from a run that had a "
                "mesh loaded.",
        )]
    if floor is None and ceiling is None:
        return [Deferred(
            check="wall", reason="decision", severity=check.severity,
            finding=check.detail,
            why="This record does not carry the material's wall limits, so the "
                "target cannot be worked out. Re-export it from a current build "
                "of the DFM tool, which reports them as numbers.",
        )]

    thin = floor is not None and measured < floor
    thick = ceiling is not None and measured > ceiling
    if not (thin or thick):
        # The check is costing points for something else it folded in -- the
        # bulk variation, or the two measures disagreeing. Neither is one
        # parameter, and both are already in the detail text.
        return [_wall_variation(context, check)]

    if thin:
        target = floor * WALL_MARGIN_OVER_FLOOR
        if ceiling is not None and target > ceiling:
            target = (floor + ceiling) / 2
        why = (f"The wall measures {measured:.2f} mm, under {floor_says}. Aiming at "
               f"{target:.2f} mm leaves margin for variation rather than sitting on "
               f"the limit.")
        functional = False
    else:
        target = ceiling * WALL_MARGIN_UNDER_CEILING
        if floor is not None and target < floor:
            target = (floor + ceiling) / 2
        why = (f"The wall measures {measured:.2f} mm, over {report.limits.name}'s "
               f"{ceiling} mm maximum. Aiming at {target:.2f} mm brings it inside "
               f"the band; coring out the thick section is the other way to do it, "
               f"and the better one where the thickness is structural.")
        functional = True

    blocked = _needs(context, "wall", check, "The wall needs to change.")
    if blocked is not None:
        return [blocked]

    name = context.parameter("wall")
    current = context.value("wall")
    if current is None:
        return [Deferred(
            check="wall", reason="decision", role="wall", severity=check.severity,
            finding=check.detail,
            why=f"The current value of {name!r} is not known here, so the correction "
                f"cannot be worked out.",
        )]

    # A correction, not an assignment. The parameter and the measured wall are
    # not necessarily the same number -- a shell thickness, a boss wall and the
    # thinnest section of a part can all differ -- so what is known is how far
    # the measurement is out, and that much is what the parameter moves. The
    # loop measuring again is what makes this converge rather than assume.
    moved = current + (target - measured)
    if moved <= 0:
        return [Deferred(
            check="wall", reason="decision", role="wall", severity=check.severity,
            finding=check.detail,
            why=f"Correcting the measured wall by {target - measured:+.2f} mm would "
                f"take {name} to {moved:.2f} mm, which is not a thickness. The wall "
                f"the checks measure is not this parameter, so it needs a look.",
        )]

    return [Change(
        parameter=name, role="wall", expression=_length(moved), was=context.was("wall"),
        target=moved, check="wall", kind="measured", functional=functional, why=why,
    )]


def _wall_variation(context: _Context, check: Check) -> Deferred:
    ratio = context.report.measured("wall_iqr_ratio")
    seen = f" The bulk wall varies {ratio:.2f}x across the part." if ratio else ""
    return Deferred(
        check="wall", reason="decision", severity=check.severity, finding=check.detail,
        why=("The wall is inside the material's band, so what this check is charging "
             "for is how unevenly it is distributed, and no single parameter "
             f"evens that out.{seen} Read the WALL heatmap in the tool and even up "
             "the section that stands out."),
    )


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------


@_rule
def _draft(context: _Context) -> list[Change | Deferred]:
    """Draft, which is declared and measured, and can disagree with itself."""
    report = context.report
    check = report.check("draft")
    if check is None or not check.costs_points:
        return []

    required = report.limits.required_draft if report.limits else None
    if required is None:
        required = report.measured("effective_min_draft_deg")
    if required is None:
        return [Deferred(
            check="draft", reason="decision", severity=check.severity,
            finding=check.detail,
            why="The record does not say what draft this part requires -- it depends "
                "on the material and the surface finish together -- so there is no "
                "target to aim at.",
        )]

    declared = report.declared_number("draftAngle")
    under = report.measured("sidewall_area_under_min_draft_pct")
    target = required + DRAFT_MARGIN_DEG

    stated_short = declared is not None and declared < target
    mesh_short = under is not None and under > 8

    if not stated_short and mesh_short:
        # The declared angle is already generous and the walls still are not
        # drafted, which means the parameter is not what drives those faces.
        # Raising it again would change nothing and report progress.
        name = context.parameter("draft")
        return [Deferred(
            check="draft", reason="decision", role="draft", severity=check.severity,
            finding=check.detail,
            why=(f"{under:.0f}% of the side-wall area is under the {required:.2f} deg "
                 f"this part needs, but the declared draft is already "
                 f"{declared:.2f} deg" + (f" and {name} is set accordingly" if name else "")
                 + ". So the faces that are short of draft are not driven by that "
                 "parameter -- they need drafting in the model, which is a change to "
                 "the geometry rather than to a number."),
        )]
    if not stated_short:
        return []

    blocked = _needs(context, "draft", check, "The draft angle needs to increase.")
    if blocked is not None:
        return [blocked]

    seen = (f" The mesh has {under:.0f}% of its side-wall area under that."
            if mesh_short else "")
    return [Change(
        parameter=context.parameter("draft"), role="draft", expression=_angle(target),
        was=context.was("draft"), target=target, check="draft", kind="declared",
        why=(f"This part needs {required:.2f} deg of draft for its material and finish, "
             f"and {declared:.2f} deg is declared. {target:.2f} deg is the required "
             f"minimum plus the degree of margin the check itself asks for.{seen}"),
    )]


# ---------------------------------------------------------------------------
# Ribs and bosses
# ---------------------------------------------------------------------------


def _ratio_change(context: _Context, check: Check, role: str, against: str,
                  fraction: float, *, functional: bool, why: str) -> Change | Deferred:
    """A change written as a fraction of another parameter where possible.

    ``rib_t = wall_t * 0.45`` rather than ``0.9 mm``: the ratio the check tests
    is then a property of the model rather than of one moment in its history,
    and the next wall change keeps it rather than breaking it.
    """
    blocked = _needs(
        context, role, check,
        f"{ROLES[role][1].capitalize()} would be set to {fraction:g}x "
        f"{ROLES[against][1]}.",
    )
    if blocked is not None:
        return blocked

    driver = context.parameter(against)
    base = context.value(against)
    if driver is None or base is None:
        return Deferred(
            check=check.key, reason="unmapped", role=role, severity=check.severity,
            finding=check.detail,
            why=(f"{why} The target is a fraction of {ROLES[against][1]}, and no "
                 f"parameter is declared for the {against!r} role, so there is "
                 f"nothing to write it against."),
        )

    # Writing the ratio against a frozen driver is fine -- reading a value is
    # not changing it -- so only the parameter being edited is checked.
    return Change(
        parameter=context.parameter(role), role=role,
        expression=f"{driver} * {fraction:g}", was=context.was(role),
        target=base * fraction, check=check.key, kind="declared",
        functional=functional, why=why, driver=driver, fraction=fraction,
    )


@_rule
def _ribs_and_bosses(context: _Context) -> list[Change | Deferred]:
    """The five ratios the ribs check tests, each fixed independently.

    One check carries all of them, and it escalates rather than replacing, so a
    single ``ribs`` finding can be a rib too thick *and* a boss wall too thin at
    once. Reading only the worst of them would leave the others to reappear on
    the next iteration, one per pass.
    """
    report = context.report
    check = report.check("ribs")
    if check is None or not check.costs_points:
        return []

    wall = report.declared_number("wallThk")
    if not wall:
        return [Deferred(
            check="ribs", reason="decision", severity=check.severity,
            finding=check.detail,
            why="Every ratio this check tests is against the nominal wall, and the "
                "record does not carry one.",
        )]

    out: list[Change | Deferred] = []

    rib_t = report.declared_number("ribThk")
    if rib_t is not None:
        ratio = rib_t / wall
        if ratio > 0.5 or ratio < 0.4:
            out.append(_ratio_change(
                context, check, "rib_thickness", "wall", RIB_THICKNESS_OF_WALL,
                functional=False,
                why=(f"A rib {ratio:.2f}x the wall is "
                     + ("thick enough to sink" if ratio > 0.5
                        else "too thin to fill reliably")
                     + f". {RIB_THICKNESS_OF_WALL:g}x sits in the middle of the "
                     f"0.4-0.5x band the check wants."),
            ))

    rib_h = report.declared_number("ribH")
    if rib_h is not None and rib_t:
        h_ratio = rib_h / rib_t
        if h_ratio > RIB_HEIGHT_OF_THICKNESS:
            out.append(_ratio_change(
                context, check, "rib_height", "rib_thickness", RIB_HEIGHT_OF_THICKNESS,
                functional=True,
                why=(f"A rib {h_ratio:.1f}x its own thickness tall is hard to fill and "
                     f"hard to eject. {RIB_HEIGHT_OF_THICKNESS:g}x is the cap. This "
                     f"shortens a rib, so it changes what the part does: freeze the "
                     f"height if it is carrying a load, and use more ribs instead."),
            ))

    rib_r = report.declared_number("ribRadius")
    if rib_r is not None:
        r_ratio = rib_r / wall
        if r_ratio < 0.25 or r_ratio > 0.40:
            out.append(_ratio_change(
                context, check, "rib_fillet", "wall", RIB_FILLET_OF_WALL,
                functional=False,
                why=(f"A rib root at {r_ratio:.2f}x the wall is "
                     + ("a stress riser" if r_ratio < 0.25
                        else "piling up mass that will sink")
                     + f". {RIB_FILLET_OF_WALL:g}x is inside the 0.25-0.4x band."),
            ))

    out.extend(_bosses(context, check, wall))
    return out


def _bosses(context: _Context, check: Check, wall: float) -> list[Change | Deferred]:
    """Boss wall, and the bind it can be caught in.

    Two guidelines pull against each other: retention around the screw wants a
    boss wall of at least a quarter of the outside diameter, and the sink limit
    caps it at 0.7 of the nominal wall. Above an outside diameter of 2.8x the
    wall there is no value that satisfies both, and no amount of adjusting the
    boss wall will find one -- which is exactly the loop that a naive optimiser
    runs forever, nudging one number back and forth.

    So the diameter is what moves. At 2.4x the wall, a quarter of it is 0.6x --
    which is the boss wall being aimed at anyway, so both hold at once with the
    sink cap still 0.1x clear above.
    """
    report = context.report
    od = report.declared_number("bossOD")
    boss_wall = report.declared_number("bossWall")
    if od is None or boss_wall is None:
        return []

    hole = od - 2 * boss_wall
    if hole <= 0:
        # A solid post. The screw guideline does not apply and the tool says so.
        return []

    screw_minimum = od / 4
    sink_maximum = 0.7 * wall
    ratio = boss_wall / wall
    conflict = screw_minimum > sink_maximum + 1e-9
    out_of_band = ratio > 0.7 or ratio < 0.5 or boss_wall < screw_minimum - 1e-9
    if not (conflict or out_of_band):
        return []

    out: list[Change | Deferred] = []
    if conflict:
        out.append(_ratio_change(
            context, check, "boss_od", "wall", BOSS_OD_OF_WALL, functional=True,
            why=(f"A boss {od:g} mm across cannot satisfy both guidelines on a "
                 f"{wall:g} mm wall: retention wants at least {screw_minimum:.2f} mm "
                 f"of boss wall and the sink limit caps it at {sink_maximum:.2f} mm. "
                 f"Bringing the boss to {BOSS_OD_OF_WALL:g}x the wall makes the two "
                 f"meet. This narrows a screw boss, so check what goes into it -- "
                 f"coring the base or gusseting into the side wall is the other "
                 f"answer, and it keeps the diameter."),
        ))
    out.append(_ratio_change(
        context, check, "boss_wall", "wall", BOSS_WALL_OF_WALL, functional=False,
        why=(f"A boss wall {ratio:.2f}x the nominal wall is "
             + ("thick enough to show as sink at the base" if ratio > 0.7
                else "thin enough to crack around an insert" if ratio < 0.5
                else f"under the {screw_minimum:.2f} mm its diameter wants")
             + f". {BOSS_WALL_OF_WALL:g}x sits inside the 0.5-0.7x window."),
    ))
    return out


# ---------------------------------------------------------------------------
# Everything that is not a parameter change
# ---------------------------------------------------------------------------

#: Findings no parameter answers, and what each actually needs. The wording is
#: about what to do next; the tool's own explanation of the finding travels
#: alongside it, unedited.
_NOT_PARAMETRIC: dict[str, tuple[str, str]] = {
    "undercut": ("decision",
                 "An undercut is a tooling decision, not a dimension: it is answered "
                 "by a slide or a lifter, by stepping the parting line, or by "
                 "redesigning the feature to release. The tool reports the regions "
                 "and what each would need."),
    "sink": ("decision",
             "Sink is mass sitting behind a surface, and it is fixed by coring that "
             "mass out or moving it, which is new geometry rather than a new number."),
    "warp": ("decision",
             "Warpage comes from the material's shrinkage and the part's shape "
             "together. It is answered by evening up the section, by gate position, "
             "or by choosing a material that shrinks less -- none of them one "
             "parameter."),
    "transitions": ("decision",
                    "A wall transition is fixed by blending between the two "
                    "thicknesses over a distance, which is geometry. The check is "
                    "also advisory on a mesh, and says so."),
    "finish_compat": ("decision",
                      "The finish and the material disagree. That is a specification "
                      "choice -- a different finish, or a different grade -- and not "
                      "something to change about the part."),
    "corners": ("unverifiable",
                "Corner radii cannot be measured from a mesh -- it takes B-rep face "
                "topology, which an STL does not carry -- so this check advises "
                "rather than measures. A fillet change here could not be confirmed "
                "by re-running the analysis, and a loop that cannot see the result "
                "of its own change is guessing."),
    "flow": ("decision",
             "Flow length is answered by where the gate goes and by evening up the "
             "section, not by one dimension. The gate position is an input the tool "
             "will search for itself."),
    "fpc": ("decision",
            "An FPC finding is about how the flex is held and covered, which is the "
            "overmould's geometry and the anchoring scheme."),
}


@_rule
def _not_parametric(context: _Context) -> list[Change | Deferred]:
    out: list[Change | Deferred] = []
    for check in context.report.findings:
        if check.key in ("wall", "draft", "ribs"):
            continue
        reason, why = _NOT_PARAMETRIC.get(
            check.key,
            ("decision", "This project has no rule for turning this finding into a "
                         "parameter change, so it is passed through rather than "
                         "guessed at."),
        )
        out.append(Deferred(check=check.key, reason=reason, why=why,
                            finding=check.detail, severity=check.severity))
    return out


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def propose(
    report: DfmReport,
    roles: Mapping[str, str] | None = None,
    guard: FreezeGuard | None = None,
    values: Mapping[str, float] | None = None,
    expressions: Mapping[str, str] | None = None,
) -> Proposal:
    """What this report implies about the model.

    *roles* maps a DFM role to a parameter name; *values* gives each parameter's
    current value in millimetres or degrees; *expressions* what it is currently
    set to, for reporting what changed. A role with no parameter is not an error
    -- it comes back as a finding nobody can act on, named -- because guessing
    which parameter is "the wall" from its spelling is how a loop ends up
    thinning the wrong thing.
    """
    unknown = set(roles or {}) - set(ROLES)
    if unknown:
        raise ValueError(
            f"Unknown DFM role(s) {sorted(unknown)}. Known roles: {sorted(ROLES)}."
        )

    context = _Context(
        report=report,
        roles=dict(roles or {}),
        guard=guard or FreezeGuard(),
        values=dict(values or {}),
        expressions=dict(expressions or {}),
    )

    notes: list[str] = []
    if not report.trustworthy:
        return Proposal(notes=(
            "The mesh this report was made from is "
            f"{report.confidence or 'not analysable'}, so its numbers are arithmetic "
            "rather than a judgement about the part, and nothing should be changed on "
            "the strength of them. Fix the mesh first -- the tool's health panel says "
            "what is wrong and offers the rescale or normal flip where that is the "
            "answer.",
        ))

    changes: list[Change] = []
    deferred: list[Deferred] = []
    for rule in _RULES:
        for outcome in rule(context):
            (changes if isinstance(outcome, Change) else deferred).append(outcome)

    # One parameter, one change. Two rules wanting the same parameter is not
    # expected, but the second would silently overwrite the first at apply time
    # and the report would show both as made.
    kept: list[Change] = []
    claimed: dict[str, Change] = {}
    for change in changes:
        first = claimed.get(change.parameter.lower())
        if first is None:
            claimed[change.parameter.lower()] = change
            kept.append(change)
            continue
        notes.append(
            f"Both the {first.role!r} and {change.role!r} fixes wanted to set "
            f"{change.parameter!r}; kept the first ({first.expression}) and left the "
            f"other for the next pass."
        )

    # A change that does nothing is not a change, and reporting it as one makes
    # a loop look like it is making progress while it stands still.
    settled = [
        change for change in kept
        if (change.was or "").strip().lower() != change.expression.strip().lower()
    ]
    for change in kept:
        if change not in settled:
            notes.append(
                f"{change.parameter} is already {change.expression}, so the "
                f"{change.check!r} finding is not answered by changing it."
            )

    settled = _restate_targets(settled)

    if not report.wall_nominal and report.check("ribs") is not None:
        notes.append(
            "No wall thickness was measured, so every ratio the ribs check tests was "
            "judged against the declared figure alone."
        )
    if "wall" not in context.roles:
        notes.append(
            "No parameter is declared for the 'wall' role. The nominal wall is what "
            "the rib, boss and corner guidelines are all fractions of, so declaring "
            "it is worth more than any other single entry in the map."
        )

    return Proposal(
        changes=tuple(settled),
        deferred=tuple(deferred),
        notes=tuple(notes),
        inputs=_missing_inputs(report),
    )


def _restate_targets(changes: Sequence[Change]) -> list[Change]:
    """Recompute each ratio target against its driver's new value.

    A rib written as ``wall_t * 0.45`` works out to one thing against today's
    wall and another against the wall this same pass is about to set. The
    expression is right either way -- that is why it is written as a ratio -- but
    the number reported next to it should be the one that will actually hold,
    otherwise anyone checking the report checks the wrong figure. Resolved
    iteratively so a chain (rib height off rib thickness off the wall) settles.
    """
    becoming = {c.parameter.lower(): c.target for c in changes if c.target is not None}
    out = list(changes)
    for _ in range(len(out) + 1):
        changed = False
        for index, change in enumerate(out):
            if change.driver is None or change.fraction is None:
                continue
            base = becoming.get(change.driver.lower())
            if base is None:
                continue
            restated = base * change.fraction
            if change.target is not None and abs(restated - change.target) < 1e-9:
                continue
            out[index] = replace(change, target=restated)
            becoming[change.parameter.lower()] = restated
            changed = True
        if not changed:
            break
    return out


def _missing_inputs(report: DfmReport) -> dict[str, Any]:
    """Inputs the analyser was short of, as against changes to the part.

    The flow check deducts nothing when no gate is set -- not having chosen one
    is not a defect in the part -- but it also cannot run, so twelve points of
    the budget sit unexamined. The tool searches for the best position anyway
    and reports it, so the loop can supply it and get the check to run. That is
    filling in an input, not fixing anything, and it is reported separately so
    it cannot be mistaken for a change.
    """
    search = report.mesh.get("gate_search")
    if not isinstance(search, dict):
        return {}
    best = search.get("best")
    if not isinstance(best, dict) or not isinstance(best.get("point"), (list, tuple)):
        return {}
    point = [float(v) for v in best["point"] if isinstance(v, (int, float))]
    if len(point) != 3:
        return {}
    return {
        "gate": point,
        "why": (
            "No gate was set, so the flow check did not run. This is the best of the "
            f"{search.get('positions_tried', 'searched')} positions the tool tried, at "
            f"a worst-case L/T of {best.get('max_lt')}. Supplying it lets the check "
            "run; it is an input to the analysis, not a change to the part."
        ),
    }
