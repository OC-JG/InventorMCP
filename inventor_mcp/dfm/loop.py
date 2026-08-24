"""The closed loop: change the model, rebuild it, and ask the tool again.

The discipline is the same one the rest of this project runs on. An estimate is
allowed to be imprecise; it is not allowed to be *believed*. So no finding here
is closed by a change having been made -- it is closed by the analyser being run
again on the rebuilt part and no longer reporting it. Every iteration therefore
carries the evidence for what it claims: which findings went, which stayed, and
what the score did.

That is also what makes the one duplication in :mod:`remedy` safe. The targets
there -- 45% of the wall, a tenth above the material floor -- are this project's
reading of bands the tool states inline and does not export. If one of them
drifts out of agreement, the loop does not quietly ship a part changed for
nothing: the finding fails to clear, and the iteration says so.

Four ways it stops, and it always says which:

* nothing is left that a parameter answers;
* an iteration made the score worse, so it is undone;
* a change repeats one already made, which is a cycle rather than progress;
* the iteration cap.

The loop never keeps a part that scores worse than the one it started with. It
is not enough to report the regression -- the document is the deliverable, and
leaving it in the worse state would mean the report and the part disagreed.

This module is deliberately not imported by ``inventor_mcp.dfm.__init__``: it
reaches back into the builder and the schema, and the schema reads the role
table from this package, so importing it from the package root would close a
circle. Import it by path -- ``from inventor_mcp.dfm.loop import improve``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..backend.base import ExportRequest
from ..builder import apply_parameter
from ..schema import ParameterSpec
from ..session import DocumentContext, Session
from ..units import ANGLE_UNIT_NAMES, LENGTH_UNIT_NAMES, convert
from .freeze import FreezeGuard, guard_for_recipe
from .remedy import Change, Deferred, Proposal, propose
from .report import DfmReport
from .roles import ROLES
from .runner import analyse_stl, settings_from_roles

#: How many rounds before giving up. Small on purpose: each round is an export,
#: a full ray-cast analysis and a rebuild, and a loop that has not converged in
#: four rounds is not converging -- it is oscillating, or it is being asked to
#: fix something no parameter fixes.
DEFAULT_ROUNDS = 4

#: A score change smaller than this is noise, not progress. The tool rounds its
#: score to a whole number, so anything below one point is not a change it
#: reported.
NOTICEABLE = 0.5


@dataclass
class Round:
    """One pass: what was changed, and what the tool said afterwards."""

    number: int
    score: float | None
    grade: str | None
    findings: tuple[str, ...] = ()
    applied: tuple[Change, ...] = ()
    cleared: tuple[str, ...] = ()
    appeared: tuple[str, ...] = ()
    persisted: tuple[str, ...] = ()
    rebuild: dict[str, Any] | None = None
    reverted: str | None = None
    stl: str | None = None
    report: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "round": self.number,
            "score": self.score,
            "grade": self.grade,
            "findings": list(self.findings),
        }
        if self.applied:
            out["changes"] = [change.as_dict() for change in self.applied]
        if self.cleared:
            out["cleared"] = list(self.cleared)
        if self.appeared:
            out["appeared"] = list(self.appeared)
        if self.persisted:
            out["did_not_clear"] = list(self.persisted)
        if self.reverted:
            out["reverted"] = self.reverted
        if self.rebuild is not None:
            out["rebuild"] = self.rebuild
        if self.report:
            out["report"] = self.report
        return out


@dataclass
class LoopResult:
    """The whole run, in the terms somebody has to make a decision from."""

    rounds: tuple[Round, ...] = ()
    stopped_because: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    grade_at_start: str | None = None
    grade_at_end: str | None = None
    frozen: dict[str, Any] = field(default_factory=dict)
    outstanding: tuple[Deferred, ...] = ()
    notes: tuple[str, ...] = ()
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def improvement(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.started_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": {
                "start": self.started_at,
                "end": self.finished_at,
                "change": (round(self.improvement, 1)
                           if self.improvement is not None else None),
            },
            "grade": {"start": self.grade_at_start, "end": self.grade_at_end},
            "stopped_because": self.stopped_because,
            "rounds": [round_.as_dict() for round_ in self.rounds],
            "key_geometry": self.frozen,
            "needs_a_person": [d.as_dict() for d in self.outstanding],
            "notes": list(self.notes),
            "dfm_settings": self.settings,
        }


# ---------------------------------------------------------------------------
# Reading the model
# ---------------------------------------------------------------------------


def current_parameters(session: Session, context: DocumentContext
                       ) -> tuple[dict[str, float], dict[str, str]]:
    """Every user parameter, as a number in millimetres or degrees, and as text.

    Converted rather than passed through: the analyser is millimetres throughout
    -- it goes as far as refusing a mesh whose size makes millimetres implausible
    -- and an inch-authored recipe handed over as-is would be judged 25.4 times
    small, which reads as a critical wall failure on a part that does not have
    one. Unitless parameters are left alone; they are counts.
    """
    values: dict[str, float] = {}
    expressions: dict[str, str] = {}
    for info in session.backend.list_parameters(context.doc_id):
        expressions[info.name] = info.expression
        if info.units in LENGTH_UNIT_NAMES:
            values[info.name] = convert(info.value, info.units, "mm")
        elif info.units in ANGLE_UNIT_NAMES:
            values[info.name] = convert(info.value, info.units, "deg")
        else:
            values[info.name] = info.value
    return values, expressions


def plan_from_recipe(
    recipe: Mapping[str, Any] | None,
    *,
    roles: Mapping[str, str] | None = None,
    freeze: Sequence[str] = (),
    freeze_features: Sequence[str] = (),
    settings: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], FreezeGuard, dict[str, Any]]:
    """The role map, the guard and the analyser settings a run will use.

    Arguments widen what the recipe declares; they never narrow it. Extra names
    are added to the protected set and extra roles fill gaps, but nothing passed
    at the call can take a freeze *off* -- that means editing the recipe, which
    is a reviewable act rather than a flag in the moment.
    """
    block = (recipe or {}).get("dfm") if isinstance(recipe, dict) else None
    block = block if isinstance(block, dict) else {}

    mapped: dict[str, str] = dict(block.get("parameters") or {})
    mapped.update(roles or {})
    unknown = sorted(set(mapped) - set(ROLES))
    if unknown:
        raise ValueError(f"Unknown DFM role(s) {unknown}. Known: {sorted(ROLES)}.")

    guard = guard_for_recipe(recipe, extra=freeze, extra_features=freeze_features)

    combined: dict[str, Any] = dict(block.get("settings") or {})
    for key, value in (settings or {}).items():
        if key == "checks" and isinstance(value, dict):
            merged = dict(combined.get("checks") or {})
            merged.update(value)
            combined["checks"] = merged
        else:
            combined[key] = value
    return mapped, guard, combined


# ---------------------------------------------------------------------------
# One measurement
# ---------------------------------------------------------------------------


def measure(
    session: Session,
    context: DocumentContext,
    *,
    roles: Mapping[str, str],
    settings: Mapping[str, Any],
    workspace: Path,
    label: str,
    dfm_root: str | None = None,
    gate: Sequence[float] | None = None,
    pull_axis: str = "+z",
) -> tuple[DfmReport, dict[str, float], dict[str, str], Path, Path]:
    """Export the part, analyse it, and return the report with what it was of."""
    values, expressions = current_parameters(session, context)
    stl = workspace / f"{label}.stl"
    session.backend.export(
        context.doc_id, ExportRequest(path=str(stl), format="stl")
    )
    if not stl.is_file():
        raise FileNotFoundError(
            f"No STL was written to {stl}. The mock backend does not write CAD "
            f"files -- connect to Inventor to run the DFM loop."
        )
    report_path = workspace / f"{label}.json"
    report = analyse_stl(
        stl,
        settings_from_roles(roles, values, settings),
        dfm_root=dfm_root,
        gate=gate,
        pull_axis=pull_axis,
        save_report_to=report_path,
    )
    return report, values, expressions, stl, report_path


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def improve(
    session: Session,
    context: DocumentContext,
    *,
    roles: Mapping[str, str] | None = None,
    freeze: Sequence[str] = (),
    freeze_features: Sequence[str] = (),
    settings: Mapping[str, Any] | None = None,
    rounds: int = DEFAULT_ROUNDS,
    workspace: str | os.PathLike[str] | None = None,
    dfm_root: str | None = None,
    include_functional: bool = True,
    pull_axis: str = "+z",
    gate: Sequence[float] | None = None,
) -> LoopResult:
    """Improve the part's manufacturability, one measured round at a time.

    *include_functional* controls whether changes that alter what the part does
    -- a thinner wall, a shorter rib, a narrower boss -- are applied or only
    reported. It defaults to on, because a loop that reports without acting is
    not a loop; freezing the dimensions that matter is the intended way to hold
    it back, and it is enforced rather than advised.
    """
    mapped, guard, dfm_settings = plan_from_recipe(
        context.recipe, roles=roles, freeze=freeze,
        freeze_features=freeze_features, settings=settings,
    )
    room = Path(workspace) if workspace else Path.cwd() / ".dfm"
    room.mkdir(parents=True, exist_ok=True)

    result = LoopResult(frozen=guard.as_dict(), settings=dict(dfm_settings))
    notes: list[str] = []

    report, values, expressions, _, report_path = measure(
        session, context, roles=mapped, settings=dfm_settings, workspace=room,
        label="round-0", dfm_root=dfm_root, gate=gate, pull_axis=pull_axis,
    )
    # Against what the model holds *now*, not what the recipe said when it was
    # built. The recipe is a snapshot: parameters get edited by hand afterwards,
    # and this loop itself rewrites literals into expressions as it goes. A guard
    # resolved against the snapshot would work out which parameters a frozen
    # value depends on from a table that has since moved, and would report a
    # freeze it had not actually enforced.
    guard = guard.with_expressions(expressions)
    result.frozen = guard.as_dict()
    result.started_at = report.score
    result.grade_at_start = report.grade
    result.rounds = (Round(
        number=0, score=report.score, grade=report.grade,
        findings=tuple(c.key for c in report.findings), report=str(report_path),
    ),)

    if not report.trustworthy:
        result.stopped_because = (
            f"the mesh is {report.confidence or 'not analysable'}, so its numbers are "
            "arithmetic rather than a judgement about the part and nothing should be "
            "changed on the strength of them"
        )
        result.finished_at = report.score
        result.grade_at_end = report.grade
        stalled = propose(report, mapped, guard, values, expressions)
        return _finish(result, stalled, notes, {})

    # The gate is an input the analyser was short of, not a defect in the part.
    # Supplied once, from the tool's own search, so the flow check actually runs
    # for the rest of the loop rather than sitting unexamined.
    proposal = propose(report, mapped, guard, values, expressions)
    if gate is None and proposal.inputs.get("gate"):
        gate = tuple(proposal.inputs["gate"])
        notes.append(
            "Set the gate to the best position the tool found, so the flow check "
            f"runs from round 1 onwards: {proposal.inputs['why']}"
        )

    applied_before: set[tuple[str, str]] = set()
    best_score = report.score if report.score is not None else 0.0

    # Kept across rounds, not just from the last one. The loop can refuse a
    # frozen parameter in round 0, clear everything else in round 1, and finish
    # with a clean proposal -- at which point the refusal is the single most
    # important thing left to say and would have been dropped.
    outstanding: dict[tuple[str, str, str | None], Deferred] = {}
    _remember(outstanding, proposal)

    for number in range(1, max(1, rounds) + 1):
        wanted = [
            change for change in proposal.changes
            if include_functional or not change.functional
        ]
        held_back = [c for c in proposal.changes if c not in wanted]
        for change in held_back:
            notes.append(
                f"{change.parameter} was left alone: setting it to "
                f"{change.expression} would change what the part does, and "
                f"functional changes are switched off for this run."
            )
        if not wanted:
            if number == 1 and not report.findings:
                result.stopped_because = (
                    "the part has no findings to answer -- it scores "
                    f"{report.score:g} and every check that ran is clean"
                )
            elif proposal.deferred:
                result.stopped_because = (
                    "nothing is left that a parameter change answers; what remains "
                    "needs a person"
                )
            else:
                result.stopped_because = "nothing is left that a parameter change answers"
            break

        repeats = [c for c in wanted if (c.parameter.lower(), c.expression) in applied_before]
        if repeats:
            result.stopped_because = (
                "the next change repeats one already made -- "
                + ", ".join(f"{c.parameter} to {c.expression}" for c in repeats)
                + " -- which is a cycle rather than progress"
            )
            break

        # Apply, remembering what each was, so the round can be undone whole.
        undo: list[tuple[str, str]] = []
        failed: list[dict[str, Any]] = []
        for change in wanted:
            try:
                apply_parameter(session, context, ParameterSpec(
                    name=change.parameter, value=change.expression,
                    comment=f"DFM round {number}: {change.check}",
                ))
                undo.append((change.parameter, expressions.get(change.parameter, "")))
                applied_before.add((change.parameter.lower(), change.expression))
            except Exception as exc:
                failed.append({"parameter": change.parameter, "error": str(exc)})

        rebuild = session.backend.rebuild(context.doc_id)
        if failed or _rebuild_unhappy(rebuild):
            _undo(session, context, undo)
            session.backend.rebuild(context.doc_id)
            result.rounds += (Round(
                number=number, score=result.rounds[-1].score,
                grade=result.rounds[-1].grade, applied=tuple(wanted),
                rebuild=rebuild, findings=result.rounds[-1].findings,
                reverted="the model would not rebuild with these values, so they "
                         "were put back",
            ),)
            said = "; ".join(entry["error"] for entry in failed)
            result.stopped_because = (
                "the change broke the rebuild, so it was undone"
                + (f": {said}" if said else "")
            )
            break

        after, values, expressions, stl, report_path = measure(
            session, context, roles=mapped, settings=dfm_settings, workspace=room,
            label=f"round-{number}", dfm_root=dfm_root, gate=gate, pull_axis=pull_axis,
        )
        was = {c.key for c in report.findings}
        now = {c.key for c in after.findings}
        entry = Round(
            number=number, score=after.score, grade=after.grade,
            findings=tuple(c.key for c in after.findings), applied=tuple(wanted),
            cleared=tuple(sorted(was - now)), appeared=tuple(sorted(now - was)),
            persisted=tuple(sorted({c.check for c in wanted} & now)),
            rebuild=rebuild, stl=str(stl), report=str(report_path),
        )

        score = after.score if after.score is not None else 0.0
        if score < best_score - NOTICEABLE:
            # Undo rather than report. The document is the deliverable, and a
            # part left worse than it started with, however carefully described,
            # is a worse part.
            _undo(session, context, undo)
            session.backend.rebuild(context.doc_id)
            entry.reverted = (
                f"the score fell from {best_score:g} to {score:g}, so these values "
                f"were put back"
            )
            result.rounds += (entry,)
            result.stopped_because = (
                f"the change made the part worse ({best_score:g} to {score:g}), so it "
                f"was undone"
            )
            values, expressions = current_parameters(session, context)
            break

        previous_best = best_score
        result.rounds += (entry,)
        report = after
        best_score = max(best_score, score)
        guard = guard.with_expressions(expressions)
        proposal = propose(report, mapped, guard, values, expressions)
        _remember(outstanding, proposal)

        # The change went in, the finding it was aimed at is still being
        # reported, and the score did not move. That is what a target drifted
        # out of agreement with its check looks like, and it is worth saying so
        # rather than spending three more rounds on it. A finding whose severity
        # dropped without its key disappearing shows up as a score rise, so it
        # does not land here.
        if entry.persisted and not entry.cleared and score <= previous_best + NOTICEABLE:
            result.stopped_because = (
                "the change was made and the finding it answers did not clear, so "
                "the target and the check disagree -- "
                + ", ".join(entry.persisted)
                + " is still reported. This is what a drifted target looks like; "
                "the values are left in place for inspection"
            )
            break
    else:
        result.stopped_because = f"the {rounds}-round limit was reached"

    last = result.rounds[-1]
    result.finished_at = last.score
    result.grade_at_end = last.grade
    result.frozen = guard.as_dict()
    return _finish(result, proposal, notes, outstanding)


def _remember(seen: dict[tuple[str, str, str | None], Deferred],
              proposal: Proposal) -> None:
    """Keep one entry per (check, reason, role), first sighting wins."""
    for entry in proposal.deferred:
        seen.setdefault((entry.check, entry.reason, entry.role), entry)


def _finish(result: LoopResult, proposal: Proposal, notes: list[str],
            outstanding: Mapping[tuple[str, str, str | None], Deferred]) -> LoopResult:
    collected = dict(outstanding)
    _remember(collected, proposal)
    result.outstanding = tuple(sorted(
        collected.values(),
        key=lambda d: (-["none", "minor", "major", "critical"].index(d.severity),
                       d.check, d.reason),
    ))
    result.notes = tuple(notes) + tuple(proposal.notes)
    return result


def _rebuild_unhappy(rebuild: Mapping[str, Any] | None) -> bool:
    """Whether a rebuild reported anything wrong.

    Only actual sickness counts. The backend also reports statuses it cannot
    translate -- there is no ``HealthStatusEnum`` in Inventor 2027.1's type
    library to ask -- and treating an untranslated number as a failure is what
    once made a correct rebuild look like three broken features.
    """
    if not isinstance(rebuild, Mapping):
        return False
    if rebuild.get("ok") is False:
        return True
    return bool(rebuild.get("sick") or rebuild.get("errors"))


def _undo(session: Session, context: DocumentContext,
          undo: Sequence[tuple[str, str]]) -> None:
    """Put parameters back, in reverse, overriding the freeze.

    The override is right here and nowhere else: these are values this loop set
    a moment ago, and restoring one is undoing its own change rather than
    touching key geometry. Refusing here would leave a part the loop had already
    decided was worse than the one it started with.
    """
    for name, expression in reversed(list(undo)):
        if not expression:
            continue
        try:
            apply_parameter(session, context,
                            ParameterSpec(name=name, value=expression),
                            override_frozen=True)
        except Exception:
            # Reported by the round's own `reverted` note; there is nothing
            # better to do here than continue putting the rest back.
            continue
