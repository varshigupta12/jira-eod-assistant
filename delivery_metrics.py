"""Collect changelog-derived delivery metrics per squad and store snapshots.

These are the metrics Jira Cloud cannot produce natively: per-ticket time in
status, cycle-time percentiles, throughput counts, aging WIP rankings, chronic
carry-over and flow efficiency.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import requests

from eod_report import (
    Config,
    EODReportError,
    _jql_quote,
    build_team_scope_clauses,
    request_error_summary,
)
from metrics import (
    carry_over_count,
    continuous_entry,
    current_status_age,
    cycle_time,
    duration_percentile,
    flow_efficiency,
    format_days,
    issue_created,
    lead_time,
    status_intervals,
    time_in_statuses,
)
from report_config import ReportSettings, Team, load_report_config
from release_blocker_report import _team_config
from snapshot_store import (
    IssueMetric,
    TeamSnapshot,
    load_snapshots,
    utc_timestamp,
    write_snapshot,
)

BASE_FIELDS = "summary,assignee,status,created,resolutiondate,fixVersions,labels"
CHANGELOG_PAGE_SIZE = 100


@dataclass(frozen=True)
class TeamMetrics:
    """Aggregated, chart-ready metrics for one squad."""

    team_id: str
    team_name: str
    captured_at: str
    throughput: int
    cycle_time_median_days: float | None
    cycle_time_p85_days: float | None
    lead_time_median_days: float | None
    flow_efficiency: float | None
    wip: int
    blocked: int
    aging_wip: tuple[IssueMetric, ...]
    longest_blocked: tuple[IssueMetric, ...]
    chronic_carry_over: tuple[IssueMetric, ...]
    release: str | None = None


def build_metrics_jql(team: Team, lookback_days: int) -> str:
    """Scope a squad to recently completed work plus everything still in flight."""
    clauses = build_team_scope_clauses(team)
    if not clauses:
        raise EODReportError(
            f"{team.name} needs projects, filters, or a Team-field mapping"
        )
    clauses.append(
        f"(resolved >= -{lookback_days}d OR "
        '(resolved IS EMPTY AND statusCategory != "To Do"))'
    )
    return " AND ".join(clauses) + " ORDER BY updated DESC"


def fetch_team_issues(
    team: Team,
    config: Config,
    settings: ReportSettings,
    session: requests.Session,
) -> list[dict[str, Any]]:
    """Fetch squad issues with their status changelog expanded inline.

    Expanding the changelog during search avoids one extra request per issue.
    """
    fields = BASE_FIELDS
    if settings.delivery_metrics.sprint_field:
        fields = f"{fields},{settings.delivery_metrics.sprint_field}"

    issues: list[dict[str, Any]] = []
    next_page_token: str | None = None
    while True:
        params: dict[str, Any] = {
            "jql": build_metrics_jql(team, settings.delivery_metrics.lookback_days),
            "fields": fields,
            "expand": "changelog",
            "maxResults": CHANGELOG_PAGE_SIZE,
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token
        try:
            response = session.get(
                f"{config.jira_base_url}/rest/api/3/search/jql",
                headers={"Accept": "application/json"},
                auth=(config.jira_email, config.jira_api_token),
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise EODReportError(
                f"Failed to fetch {team.name} delivery metrics: "
                f"{request_error_summary(exc)}"
            ) from exc
        page = data.get("issues", [])
        if not isinstance(page, list):
            raise EODReportError(
                f"Jira returned invalid delivery metrics for {team.name}"
            )
        issues.extend(issue for issue in page if isinstance(issue, dict))
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            return issues


def _histories(issue: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    changelog = issue.get("changelog")
    if not isinstance(changelog, dict):
        return []
    histories = changelog.get("histories", [])
    if not isinstance(histories, list):
        return []
    return [history for history in histories if isinstance(history, dict)]


def _fix_versions(fields: Mapping[str, Any]) -> tuple[str, ...]:
    """Release names from the fixVersion field."""
    versions = fields.get("fixVersions")
    if not isinstance(versions, list):
        return ()
    names = []
    for version in versions:
        if isinstance(version, dict):
            name = str(version.get("name") or "").strip()
            if name:
                names.append(name)
    return tuple(names)


def _labels(fields: Mapping[str, Any]) -> tuple[str, ...]:
    """Issue labels, which some squads use to tag a release."""
    labels = fields.get("labels")
    if not isinstance(labels, list):
        return ()
    return tuple(
        label.strip()
        for label in labels
        if isinstance(label, str) and label.strip()
    )


def _waiting_statuses(settings: ReportSettings) -> frozenset[str]:
    """Statuses that count as waiting rather than active work."""
    return frozenset(
        settings.blocked_statuses
        | settings.review_statuses
        | settings.deploy_statuses
    )


def issue_metric(
    issue: Mapping[str, Any],
    settings: ReportSettings,
    now: datetime,
) -> IssueMetric | None:
    """Derive one issue's metrics from its fields and status changelog."""
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        return None
    status_data = fields.get("status")
    status = (
        str(status_data.get("name") or "Unknown")
        if isinstance(status_data, dict)
        else "Unknown"
    )
    normalized = status.strip().casefold()
    created = issue_created(fields)
    intervals = status_intervals(_histories(issue), created, status, now)

    is_done = normalized in settings.done_statuses
    is_blocked = normalized in settings.blocked_statuses
    blocked_since = continuous_entry(intervals, settings.blocked_statuses)
    assignee = fields.get("assignee")

    started_statuses = _started_statuses(intervals, settings)
    return IssueMetric(
        key=str(issue.get("key") or "Unknown"),
        summary=str(fields.get("summary") or "No summary"),
        status=status,
        assignee=(
            str(assignee.get("displayName") or "Unassigned")
            if isinstance(assignee, dict)
            else "Unassigned"
        ),
        is_done=is_done,
        is_blocked=is_blocked,
        age_in_status_days=format_days(current_status_age(intervals, now)),
        blocked_days=(
            format_days(now - blocked_since) if blocked_since else None
        ),
        cycle_time_days=format_days(
            cycle_time(intervals, started_statuses, settings.done_statuses)
        ),
        lead_time_days=format_days(
            lead_time(created, intervals, settings.done_statuses)
        ),
        flow_efficiency=flow_efficiency(
            intervals, _waiting_statuses(settings), settings.done_statuses, now
        ),
        carry_over_sprints=carry_over_count(
            fields, settings.done_statuses, status
        ),
        fix_versions=_fix_versions(fields),
        labels=_labels(fields),
    )

def _started_statuses(
    intervals: Sequence[Any], settings: ReportSettings
) -> tuple[str, ...]:
    """Every status that represents work already underway.

    Jira has no universal "in progress" name, so anything that is not a To Do,
    done, or waiting status is treated as active work.
    """
    excluded = settings.done_statuses | _waiting_statuses(settings)
    return tuple(
        interval.status
        for interval in intervals
        if interval.status.strip().casefold() not in excluded
        and interval.status.strip().casefold() not in {"to do", "open", "backlog"}
    )


def collect_team_snapshot(
    team: Team,
    settings: ReportSettings,
    session: requests.Session,
    now: datetime | None = None,
) -> TeamSnapshot:
    """Fetch a squad's issues and capture their metrics as a snapshot."""
    current_time = now or datetime.now(timezone.utc)
    config = _team_config(team, settings)
    issues = fetch_team_issues(team, config, settings, session)
    measured = [
        metric
        for metric in (
            issue_metric(issue, settings, current_time) for issue in issues
        )
        if metric is not None
    ]
    return TeamSnapshot(
        team_id=team.id,
        team_name=team.name,
        captured_at=utc_timestamp(current_time),
        issues=tuple(measured),
    )


def in_release(issue: IssueMetric, release: str | None) -> bool:
    """Whether an issue belongs to a release, by fixVersion or label.

    Squads tag releases either way, so both conventions are accepted.
    """
    if not release:
        return True
    wanted = release.strip().casefold()
    return any(
        name.strip().casefold() == wanted
        for name in (*issue.fix_versions, *issue.labels)
    )


def release_options(
    snapshots: Sequence[TeamSnapshot], configured: str | None = None
) -> tuple[str, ...]:
    """Releases worth offering as a filter.

    Only fixVersions are treated as releases; arbitrary labels would flood the
    list. The configured release label is added because squads that tag by
    label would otherwise have no entry at all.
    """
    seen: dict[str, str] = {}
    if configured and configured.strip():
        seen[configured.strip().casefold()] = configured.strip()
    for snapshot in snapshots:
        for issue in snapshot.issues:
            for name in issue.fix_versions:
                seen.setdefault(name.strip().casefold(), name.strip())
    return tuple(seen[key] for key in sorted(seen, reverse=True))


def summarize(
    snapshot: TeamSnapshot,
    settings: ReportSettings,
    release: str | None = None,
) -> TeamMetrics:
    """Reduce a snapshot to the headline numbers and ranked lists.

    When a release is given, only issues tagged with that release count.
    """
    scoped = [issue for issue in snapshot.issues if in_release(issue, release)]
    completed = [issue for issue in scoped if issue.is_done]
    active = [issue for issue in scoped if not issue.is_done]
    cycle_times = [
        timedelta(days=issue.cycle_time_days)
        for issue in completed
        if issue.cycle_time_days is not None
    ]
    lead_times = [
        timedelta(days=issue.lead_time_days)
        for issue in completed
        if issue.lead_time_days is not None
    ]
    efficiencies = [
        issue.flow_efficiency
        for issue in scoped
        if issue.flow_efficiency is not None
    ]
    threshold = settings.delivery_metrics.aging_wip_days
    aging = sorted(
        (
            issue
            for issue in active
            if not issue.is_blocked
            and (issue.age_in_status_days or 0) >= threshold
        ),
        key=lambda issue: issue.age_in_status_days or 0,
        reverse=True,
    )
    blocked = sorted(
        (issue for issue in active if issue.is_blocked),
        key=lambda issue: issue.blocked_days or 0,
        reverse=True,
    )
    carry_over = sorted(
        (issue for issue in active if issue.carry_over_sprints >= 2),
        key=lambda issue: issue.carry_over_sprints,
        reverse=True,
    )
    return TeamMetrics(
        team_id=snapshot.team_id,
        team_name=snapshot.team_name,
        captured_at=snapshot.captured_at,
        throughput=len(completed),
        cycle_time_median_days=format_days(duration_percentile(cycle_times, 0.5)),
        cycle_time_p85_days=format_days(duration_percentile(cycle_times, 0.85)),
        lead_time_median_days=format_days(duration_percentile(lead_times, 0.5)),
        flow_efficiency=(
            round(sum(efficiencies) / len(efficiencies), 3)
            if efficiencies
            else None
        ),
        wip=len(active),
        blocked=len(blocked),
        aging_wip=tuple(aging),
        longest_blocked=tuple(blocked),
        chronic_carry_over=tuple(carry_over),
        release=release,
    )


def collect_all(
    settings: ReportSettings,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> tuple[TeamSnapshot, ...]:
    """Collect and persist a snapshot for every configured squad."""
    if not settings.delivery_metrics.enabled:
        return ()
    client = session or requests.Session()
    snapshots = []
    for team in settings.teams:
        snapshot = collect_team_snapshot(team, settings, client, now)
        write_snapshot(settings.delivery_metrics.snapshot_dir, snapshot)
        snapshots.append(snapshot)
    return tuple(snapshots)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", help="Path to report-config.yml", default=None
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the summary without writing snapshots",
    )
    args = parser.parse_args()

    try:
        settings = load_report_config(args.config)
        if not settings.delivery_metrics.enabled:
            print("Delivery metrics are disabled in the configuration.")
            return 0
        session = requests.Session()
        now = datetime.now(timezone.utc)
        for team in settings.teams:
            snapshot = collect_team_snapshot(team, settings, session, now)
            if not args.dry_run:
                write_snapshot(settings.delivery_metrics.snapshot_dir, snapshot)
            summary = summarize(snapshot, settings)
            print(
                f"{summary.team_name}: throughput={summary.throughput} "
                f"wip={summary.wip} blocked={summary.blocked} "
                f"cycle_p50={summary.cycle_time_median_days} "
                f"cycle_p85={summary.cycle_time_p85_days} "
                f"aging={len(summary.aging_wip)} "
                f"carry_over={len(summary.chronic_carry_over)}"
            )
    except EODReportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
