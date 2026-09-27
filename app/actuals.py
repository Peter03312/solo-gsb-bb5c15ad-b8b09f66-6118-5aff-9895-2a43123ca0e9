"""Departure-actual audit for saved validate batches.

A validate batch freezes the plan (flights, wake matrix, closure windows,
rolling capacity). This module layers reported **actual departure times** on
top of that frozen batch without touching the plan itself or any slot-book
reservation:

* :func:`submit_actuals` validates one atomic batch POST (unknown/duplicate
  flight numbers, revision ordering) and upserts the accepted rows;
* :func:`build_actual_audit` recomputes the audit result purely from the
  frozen constraints plus the currently stored actuals, so rerunning it after
  a restart — or after a late report / correction — yields exactly the
  violations that hold for the current data and never a stale one.

Only **reported** flights participate in an audit. Per runway they are taken
in ``(actual_off, flight_no)`` stable order and re-checked against the same
three frozen rule families: adjacent wake gaps, closure windows and rolling
capacity. Flights not yet reported are listed separately and do not affect
any adjacency, window or capacity count.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .repository import ActualRow, upsert_actual
from .separation import (
    Capacity,
    CapacityViolation,
    ClosureConflict,
    ClosureWindow,
    Flight,
    Violation,
    find_capacity_violations,
    find_closure_conflicts,
    find_violations,
    sort_flights,
)
from .timeutils import to_epoch_seconds

#: POST outcome for one flight: new row, higher-revision replacement or an
#: equal-revision/equal-value idempotent replay.
ACTUAL_CREATED = "created"
ACTUAL_REPLACED = "replaced"
ACTUAL_UNCHANGED = "unchanged"


class ActualsSubmissionError(Exception):
    """Whole actuals batch must be rejected (API 层转为 4xx)。

    ``status_code`` is 422 for malformed domain input (unknown or duplicate
    flight numbers within the request) and 409 for revision conflicts
    (lower revision, or equal revision carrying a different actual time).
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ActualIn:
    """One normalised item of an actuals submission."""

    flight_no: str
    actual_off: datetime  # always tz-aware UTC
    revision: int


@dataclass(frozen=True, slots=True)
class AcceptedActual:
    """Per-flight result of a committed actuals submission."""

    flight_no: str
    actual_off: datetime
    revision: int
    status: str  # ACTUAL_CREATED | ACTUAL_REPLACED | ACTUAL_UNCHANGED


@dataclass(frozen=True, slots=True)
class ActualCurrentRevision:
    """Current reported revision of one reported flight in audit order."""

    position: int  # 1-based position within the runway's reported order
    flight_no: str
    actual_off: datetime
    revision: int


@dataclass(frozen=True, slots=True)
class UnreportedFlight:
    """A frozen-plan flight without an actual yet (audit order stable)."""

    planned_position: int
    flight_no: str
    wake: str
    scheduled_off: datetime


@dataclass(slots=True)
class RunwayActualAudit:
    """One runway's audit: violations over reported flights, their current
    revisions, and the still-unreported planned flights."""

    runway: str
    order: list[str]
    reported_count: int
    violations: list[Violation]
    closure_conflicts: list[ClosureConflict]
    capacity_violations: list[CapacityViolation]
    current_revisions: list[ActualCurrentRevision]
    unreported: list[UnreportedFlight]
    capacity: Capacity | None


@dataclass(slots=True)
class ActualAudit:
    """Full-batch audit result (recomputed on every read, never stored)."""

    batch_id: str
    created_at: str
    runways: list[RunwayActualAudit]


def submit_actuals(conn, batch_id: str, data: dict, items: list[ActualIn]) -> list[AcceptedActual]:
    """Validate and atomically apply one actuals submission to a batch.

    ``data`` is :func:`app.repository.load_actual_audit_inputs` output (the
    caller guarantees the batch exists and is a validate batch); ``conn``
    must already be inside a ``BEGIN IMMEDIATE`` write transaction.

    The whole request is rejected before any write when:

    * a flight number appears more than once in the request (422);
    * a flight number is not part of the frozen batch plan (422);
    * a stored revision is higher (409), or equal while the submitted
      ``actual_off`` differs from the stored one (409).

    Equal revision with an equal value is idempotent; a higher revision
    replaces. Accepted items are returned in their request order with their
    per-flight outcome.
    """
    planned: dict[str, Flight] = {
        flight.flight_no: flight
        for flights in data["flights_by_runway"].values()
        for flight in flights
    }
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in items:
        if item.flight_no in seen and item.flight_no not in duplicates:
            duplicates.append(item.flight_no)
        seen.add(item.flight_no)
    if duplicates:
        raise ActualsSubmissionError(
            422, f"请求内航班号重复: {', '.join(duplicates)}"
        )

    unknown = [item.flight_no for item in items if item.flight_no not in planned]
    if unknown:
        raise ActualsSubmissionError(
            422,
            f"航班不属于该 validate 批次的冻结计划: {', '.join(unknown)}",
        )

    stored = data["actuals"]
    conflicts: list[str] = []
    for item in items:
        current = stored.get(item.flight_no)
        if current is None:
            continue
        if item.revision < current.revision:
            conflicts.append(
                f"{item.flight_no}（提交修订号 {item.revision} 低于已保存 "
                f"{current.revision}）"
            )
        elif item.revision == current.revision and (
            item.actual_off != current.actual_off
        ):
            conflicts.append(f"{item.flight_no}（同修订号 {item.revision} 的实际时刻与已保存值不一致）")
    if conflicts:
        raise ActualsSubmissionError(
            409,
            "实际离场时刻修订冲突，已整批拒绝（高版本覆盖、等版本同值幂等）："
            + "；".join(conflicts),
        )

    accepted: list[AcceptedActual] = []
    for item in items:
        current = stored.get(item.flight_no)
        if current is None:
            status = ACTUAL_CREATED
        elif item.revision > current.revision:
            status = ACTUAL_REPLACED
        else:
            # Equal revision and equal value: replay without rewriting.
            status = ACTUAL_UNCHANGED
        if status != ACTUAL_UNCHANGED:
            upsert_actual(
                conn,
                batch_id=batch_id,
                flight_no=item.flight_no,
                actual_off=item.actual_off,
                revision=item.revision,
            )
        accepted.append(
            AcceptedActual(
                flight_no=item.flight_no,
                actual_off=item.actual_off,
                revision=item.revision,
                status=status,
            )
        )
    return accepted


def _reported_actual_flights(
    planned_by_no: dict[str, Flight], actuals: dict[str, ActualRow]
) -> list[Flight]:
    """Reported flights rebuilt at their actual times, in stable order.

    The wake category comes from the frozen plan; the frozen scheduled time
    is irrelevant to the audit except for the unreported list.
    """
    return sort_flights(
        [
            Flight(
                flight_no=row.flight_no,
                scheduled_off=row.actual_off,
                wake=planned_by_no[row.flight_no].wake,
                runway=planned_by_no[row.flight_no].runway,
            )
            for row in actuals.values()
        ]
    )


def build_actual_audit(
    data: dict,
    *,
    batch_id: str,
) -> ActualAudit:
    """Recompute the audit from the frozen batch and its stored actuals.

    Per runway only reported flights are kept, ordered by
    ``(actual_off, flight_no)``; the frozen matrix, closure windows and
    rolling capacity of that runway are then applied exactly like validate.
    Because every result is derived afresh from the stored actuals, a late
    report or correction automatically changes adjacency/capacity windows
    and drops violations that no longer hold; restarting changes nothing.
    """
    matrix = data["matrix"]
    windows_by_runway: dict[str, list[ClosureWindow]] = data["windows_by_runway"]
    capacity_by_runway: dict[str, Capacity] = data["capacity_by_runway"]
    actuals: dict[str, ActualRow] = data["actuals"]

    runway_audits: list[RunwayActualAudit] = []
    for runway, planned in sorted(data["flights_by_runway"].items()):
        planned_by_no = {flight.flight_no: flight for flight in planned}
        runway_actuals = {
            no: row for no, row in actuals.items() if no in planned_by_no
        }
        reported = [
            flight
            for flight in _reported_actual_flights(planned_by_no, runway_actuals)
        ]
        windows = windows_by_runway.get(runway, [])
        capacity = capacity_by_runway.get(runway)
        violations = find_violations(reported, matrix)
        closure_conflicts = find_closure_conflicts(reported, windows)
        capacity_violations = find_capacity_violations(reported, capacity)

        reported_no_to_position = {
            flight.flight_no: position
            for position, flight in enumerate(reported, start=1)
        }
        current_revisions = [
            ActualCurrentRevision(
                position=reported_no_to_position[row.flight_no],
                flight_no=row.flight_no,
                actual_off=row.actual_off,
                revision=row.revision,
            )
            # Same whole-second (epoch, flight_no) key the audit engine uses:
            # two actuals inside the same second tie on flight number, never
            # on their sub-second remainder.
            for row in sorted(
                runway_actuals.values(),
                key=lambda r: (to_epoch_seconds(r.actual_off), r.flight_no),
            )
        ]
        unreported = [
            UnreportedFlight(
                planned_position=position,
                flight_no=flight.flight_no,
                wake=flight.wake,
                scheduled_off=flight.scheduled_off,
            )
            for position, flight in enumerate(planned, start=1)
            if flight.flight_no not in runway_actuals
        ]
        runway_audits.append(
            RunwayActualAudit(
                runway=runway,
                order=[flight.flight_no for flight in reported],
                reported_count=len(reported),
                violations=violations,
                closure_conflicts=closure_conflicts,
                capacity_violations=capacity_violations,
                current_revisions=current_revisions,
                unreported=unreported,
                capacity=capacity,
            )
        )

    return ActualAudit(
        batch_id=batch_id,
        created_at=data["created_at"],
        runways=runway_audits,
    )
