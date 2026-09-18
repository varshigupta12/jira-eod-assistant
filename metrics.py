"""Changelog-derived delivery metrics shared by reports and the dashboard.

Jira exposes only *average* time-in-status natively, and JQL cannot order by a
computed duration. Every metric here is therefore derived locally from an
issue's status changelog, which is the same source the blocker report uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence


def parse_jira_datetime(value: str) -> datetime:
    """Parse a Jira timestamp into an aware UTC datetime."""
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def status_transitions(
    histories: Sequence[Mapping[str, Any]],
) -> list[tuple[datetime, str, str]]:
    """Extract sorted (moment, from, to) status changes from a Jira changelog."""
    transitions: list[tuple[datetime, str, str]] = []
    for history in histories:
        if not history.get("created"):
            continue
        try:
            created = parse_jira_datetime(str(history["created"]))
        except ValueError:
            continue
        items = history.get("items", [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            field = str(item.get("fieldId") or item.get("field") or "").casefold()
            if field != "status":
                continue
            transitions.append(
                (
                    created,
                    str(item.get("fromString") or "Unknown"),
                    str(item.get("toString") or "Unknown"),
                )
            )
    return sorted(transitions)


@dataclass(frozen=True)
class StatusInterval:
    """One continuous period an issue spent in a single status."""

    status: str
    start: datetime
    end: datetime | None

    @property
    def is_open(self) -> bool:
        return self.end is None

    def duration(self, now: datetime | None = None) -> timedelta:
        finish = self.end or _utc(now or datetime.now(timezone.utc))
        return max(timedelta(0), finish - self.start)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize(status: str) -> str:
    return status.strip().casefold()


def _matches(status: str, statuses: Iterable[str]) -> bool:
    return _normalize(status) in {_normalize(item) for item in statuses}


def issue_created(fields: Mapping[str, Any]) -> datetime | None:
    """Return an issue's creation timestamp when Jira supplied it."""
    created = fields.get("created")
    if not isinstance(created, str):
        return None
    try:
        return _utc(parse_jira_datetime(created))
    except ValueError:
        return None


def status_intervals(
    histories: Sequence[Mapping[str, Any]],
    created: datetime | None,
    current_status: str,
    now: datetime | None = None,
) -> tuple[StatusInterval, ...]:
    """Rebuild the full status timeline for one issue.

    The first interval starts at issue creation, so work created directly into
    a status (with no transition into it) still has a measurable duration.
    """
    current_time = _utc(now or datetime.now(timezone.utc))
    transitions = [
        (_utc(moment), previous, current)
        for moment, previous, current in status_transitions(histories)
    ]

    if not transitions:
        start = _utc(created) if created else current_time
        return (StatusInterval(current_status, min(start, current_time), None),)

    start = _utc(created) if created else transitions[0][0]
    intervals: list[StatusInterval] = []
    # Before the first transition the issue sat in that transition's "from" status.
    open_status = transitions[0][1]
    open_start = min(start, transitions[0][0])

    for moment, _previous, following in transitions:
        if moment > open_start:
            intervals.append(StatusInterval(open_status, open_start, moment))
        open_status = following
        open_start = max(moment, open_start)

    # The issue's own status field wins over the changelog, which Jira truncates
    # once an issue accumulates more than 10,000 changes.
    intervals.append(StatusInterval(current_status or open_status, open_start, None))
    return tuple(intervals)


def time_in_statuses(
    intervals: Sequence[StatusInterval],
    statuses: Iterable[str],
    now: datetime | None = None,
) -> timedelta:
    """Total time an issue spent in any of the given statuses."""
    wanted = {_normalize(status) for status in statuses}
    return sum(
        (
            interval.duration(now)
            for interval in intervals
            if _normalize(interval.status) in wanted
        ),
        timedelta(0),
    )


def continuous_entry(
    intervals: Sequence[StatusInterval],
    statuses: Iterable[str],
) -> datetime | None:
    """When the issue *most recently and continuously* entered the status set.

    Transitions inside the set (for example Blocked to Impediment) do not reset
    the clock; leaving the set does.
    """
    wanted = {_normalize(status) for status in statuses}
    if not intervals or _normalize(intervals[-1].status) not in wanted:
        return None
    entry = intervals[-1].start
    for interval in reversed(intervals[:-1]):
        if _normalize(interval.status) not in wanted:
            break
        entry = interval.start
    return entry


def current_status_age(
    intervals: Sequence[StatusInterval],
    now: datetime | None = None,
) -> timedelta | None:
    """How long the issue has sat in its current status."""
    if not intervals:
        return None
    return intervals[-1].duration(now)


def cycle_time(
    intervals: Sequence[StatusInterval],
    started_statuses: Iterable[str],
    done_statuses: Iterable[str],
) -> timedelta | None:
    """Elapsed time from first entering active work to reaching a done status.

    Returns None while the issue is unfinished, so in-flight work never
    contaminates completed-work percentiles.
    """
    started_at = next(
        (
            interval.start
            for interval in intervals
            if _matches(interval.status, started_statuses)
        ),
        None,
    )
    if started_at is None:
        return None
    finished_at = next(
        (
            interval.start
            for interval in reversed(intervals)
            if _matches(interval.status, done_statuses)
        ),
        None,
    )
    if finished_at is None or finished_at < started_at:
        return None
    return finished_at - started_at


def lead_time(
    created: datetime | None,
    intervals: Sequence[StatusInterval],
    done_statuses: Iterable[str],
) -> timedelta | None:
    """Elapsed time from issue creation to reaching a done status."""
    if created is None:
        return None
    finished_at = next(
        (
            interval.start
            for interval in reversed(intervals)
            if _matches(interval.status, done_statuses)
        ),
        None,
    )
    if finished_at is None:
        return None
    start = _utc(created)
    if finished_at < start:
        return None
    return finished_at - start


def flow_efficiency(
    intervals: Sequence[StatusInterval],
    waiting_statuses: Iterable[str],
    done_statuses: Iterable[str],
    now: datetime | None = None,
) -> float | None:
    """Share of elapsed working time that was active rather than waiting.

    Time after the issue reached a done status is excluded, so finished work is
    not credited with idle time it spent sitting in Done.
    """
    considered = []
    for interval in intervals:
        if _matches(interval.status, done_statuses):
            break
        considered.append(interval)
    if not considered:
        return None
    total = sum(
        (interval.duration(now) for interval in considered), timedelta(0)
    )
    if total <= timedelta(0):
        return None
    waiting = time_in_statuses(considered, waiting_statuses, now)
    return max(0.0, min(1.0, (total - waiting) / total))


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Linear-interpolated percentile, matching the common 'linear' method."""
    ordered = sorted(value for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return float(ordered[0])
    bounded = max(0.0, min(1.0, fraction))
    position = bounded * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def median(values: Sequence[float]) -> float | None:
    """Middle value of the sample, interpolating across an even count."""
    return percentile(values, 0.5)


def duration_percentile(
    durations: Sequence[timedelta], fraction: float
) -> timedelta | None:
    """Percentile over durations, returned as a duration."""
    seconds = percentile(
        [duration.total_seconds() for duration in durations], fraction
    )
    return None if seconds is None else timedelta(seconds=seconds)


def format_duration(delta: timedelta | None) -> str | None:
    """Render a duration the same way the blocker report does."""
    if delta is None:
        return None
    elapsed_seconds = max(0, int(delta.total_seconds()))
    days, remainder = divmod(elapsed_seconds, 24 * 60 * 60)
    hours = remainder // (60 * 60)
    if days == 0 and hours == 0:
        return "<1 hour"
    parts = []
    if days:
        parts.append(f"{days} {'day' if days == 1 else 'days'}")
    if hours:
        parts.append(f"{hours} {'hour' if hours == 1 else 'hours'}")
    return " ".join(parts)


def format_days(delta: timedelta | None, digits: int = 1) -> float | None:
    """Render a duration in days for charting and snapshots."""
    if delta is None:
        return None
    return round(delta.total_seconds() / 86_400, digits)


def sprint_names(fields: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract sprint names from Jira's sprint field in either shape."""
    for key, value in fields.items():
        if not key.startswith("customfield_") and key != "sprint":
            continue
        if not isinstance(value, list):
            continue
        names = []
        for entry in value:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.append(entry["name"])
            elif isinstance(entry, str) and "name=" in entry:
                # Older Jira returns sprints as a serialized string blob.
                remainder = entry.split("name=", 1)[1]
                names.append(remainder.split(",", 1)[0])
        if names:
            return tuple(names)
    return ()


def carry_over_count(
    fields: Mapping[str, Any],
    done_statuses: Iterable[str],
    current_status: str,
) -> int:
    """How many sprints an unfinished issue has already been carried through.

    Completed work reports zero, so the metric only surfaces work still in
    flight after multiple sprints.
    """
    if _matches(current_status, done_statuses):
        return 0
    return max(0, len(sprint_names(fields)) - 1)
