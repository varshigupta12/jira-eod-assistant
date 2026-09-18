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
from urllib.parse import quote

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
MAX_RELEASES = 6


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
        'statusCategory = "In Progress")'
    )
    return " AND ".join(clauses) + " ORDER BY updated DESC"


def build_release_metrics_jql(team: Team, releases: Sequence[str]) -> str:
    """Scope a squad to every issue in the selected releases."""
    clauses = build_team_scope_clauses(team)
    if not clauses:
        raise EODReportError(
            f"{team.name} needs projects, filters, or a Team-field mapping"
        )
    release_clauses = [
        f'(fixVersion = "{_jql_quote(release)}" '
        f'OR labels = "{_jql_quote(release)}")'
        for release in releases
    ]
    clauses.append("(" + " OR ".join(release_clauses) + ")")
    return " AND ".join(clauses) + " ORDER BY updated DESC"


def _search_issues(
    team: Team,
    config: Config,
    settings: ReportSettings,
    session: requests.Session,
    jql: str,
) -> list[dict[str, Any]]:
    """Run one paginated Jira issue search with changelogs expanded."""
    fields = BASE_FIELDS
    if settings.delivery_metrics.sprint_field:
        fields = f"{fields},{settings.delivery_metrics.sprint_field}"

    issues: list[dict[str, Any]] = []
    next_page_token: str | None = None
    while True:
        params: dict[str, Any] = {
            "jql": jql,
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
        issues.extend(
            _complete_changelog(issue, team, config, session)
            for issue in page
            if isinstance(issue, dict)
        )
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            return issues


def _complete_changelog(
    issue: Mapping[str, Any],
    team: Team,
    config: Config,
    session: requests.Session,
) -> dict[str, Any]:
    """Fetch all changelog pages when Jira search returned a partial expansion."""
    result = dict(issue)
    changelog = issue.get("changelog")
    if not isinstance(changelog, dict):
        return result
    histories = changelog.get("histories")
    total = changelog.get("total")
    if (
        not isinstance(histories, list)
        or not isinstance(total, int)
        or total <= len(histories)
    ):
        return result

    key = str(issue.get("key") or "").strip()
    if not key:
        raise EODReportError(
            f"Jira returned a truncated changelog without an issue key for {team.name}"
        )
    complete: list[dict[str, Any]] = []
    start_at = 0
    while start_at < total:
        try:
            response = session.get(
                f"{config.jira_base_url}/rest/api/3/issue/"
                f"{quote(key, safe='-')}/changelog",
                headers={"Accept": "application/json"},
                auth=(config.jira_email, config.jira_api_token),
                params={"startAt": start_at, "maxResults": CHANGELOG_PAGE_SIZE},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise EODReportError(
                f"Failed to fetch {team.name} delivery metrics changelog: "
                f"{request_error_summary(exc)}"
            ) from exc
        values = data.get("values", [])
        if not isinstance(values, list):
            raise EODReportError(
                f"Jira returned an invalid changelog for {team.name}"
            )
        page = [value for value in values if isinstance(value, dict)]
        if not page:
            raise EODReportError(
                f"Jira returned an incomplete changelog for {team.name}"
            )
        complete.extend(page)
        start_at += len(page)
        page_total = data.get("total")
        if isinstance(page_total, int):
            total = page_total

    result["changelog"] = {**changelog, "histories": complete, "total": total}
    return result


def _raw_fix_versions(issue: Mapping[str, Any]) -> tuple[str, ...]:
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        return ()
    return _fix_versions(fields)


def fetch_team_issues(
    team: Team,
    config: Config,
    settings: ReportSettings,
    session: requests.Session,
    releases: Sequence[str] | None = None,
    recent_issues: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent work, then complete every release shown by the dashboard.

    The lookback query discovers currently relevant releases. A second query
    retrieves all issues in those releases, so release metrics are never cut
    off merely because an issue finished before the lookback boundary.
    """
    recent = list(recent_issues) if recent_issues is not None else _search_issues(
        team,
        config,
        settings,
        session,
        build_metrics_jql(team, settings.delivery_metrics.lookback_days),
    )
    release_names = {
        name.strip().casefold(): name.strip()
        for issue in recent
        for name in _raw_fix_versions(issue)
        if name.strip()
    }
    configured = settings.release_blockers.label
    if configured and configured.strip():
        release_names.setdefault(configured.strip().casefold(), configured.strip())
    selected = list(releases) if releases is not None else [
        release_names[key]
        for key in sorted(release_names, reverse=True)[:MAX_RELEASES]
    ]

    combined: dict[str, dict[str, Any]] = {}
    for issue in recent:
        marked = dict(issue)
        marked["_delivery_in_lookback"] = True
        combined[str(issue.get("key") or id(issue))] = marked
    if selected:
        complete = _search_issues(
            team,
            config,
            settings,
            session,
            build_release_metrics_jql(team, selected),
        )
        for issue in complete:
            key = str(issue.get("key") or id(issue))
            if key not in combined:
                marked = dict(issue)
                marked["_delivery_in_lookback"] = False
                combined[key] = marked
    return list(combined.values())


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
    status_category = (
        status_data.get("statusCategory")
        if isinstance(status_data, dict)
        else None
    )
    category_key = (
        str(status_category.get("key") or "").strip().casefold()
        if isinstance(status_category, dict)
        else ""
    )
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
            intervals,
            _waiting_statuses(settings),
            settings.done_statuses,
            now,
            started_statuses,
        ),
        carry_over_sprints=carry_over_count(
            fields, settings.done_statuses, status
        ),
        fix_versions=_fix_versions(fields),
        labels=_labels(fields),
        in_lookback=bool(issue.get("_delivery_in_lookback", True)),
        is_started=(
            category_key == "indeterminate"
            if category_key
            else normalized not in {"to do", "open", "backlog"}
            and not is_done
        ),
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
    releases: Sequence[str] | None = None,
    recent_issues: Sequence[Mapping[str, Any]] | None = None,
) -> TeamSnapshot:
    """Fetch a squad's issues and capture their metrics as a snapshot."""
    current_time = now or datetime.now(timezone.utc)
    config = _team_config(team, settings)
    issues = fetch_team_issues(
        team,
        config,
        settings,
        session,
        releases=releases,
        recent_issues=recent_issues,
    )
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
    return tuple(
        seen[key] for key in sorted(seen, reverse=True)[:MAX_RELEASES]
    )


def summarize(
    snapshot: TeamSnapshot,
    settings: ReportSettings,
    release: str | None = None,
) -> TeamMetrics:
    """Reduce a snapshot to the headline numbers and ranked lists.

    When a release is given, only issues tagged with that release count.
    """
    scoped = [
        issue
        for issue in snapshot.issues
        if in_release(issue, release)
        and (release is not None or issue.in_lookback)
    ]
    completed = [issue for issue in scoped if issue.is_done]
    active = [
        issue for issue in scoped if not issue.is_done and issue.is_started
    ]
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
    persist: bool = True,
) -> tuple[TeamSnapshot, ...]:
    """Collect every squad with the same complete set of release scopes."""
    if not settings.delivery_metrics.enabled:
        return ()
    client = session or requests.Session()
    recent_by_team: dict[str, list[dict[str, Any]]] = {}
    releases: dict[str, str] = {}
    configured = settings.release_blockers.label
    if configured and configured.strip():
        releases[configured.strip().casefold()] = configured.strip()

    for team in settings.teams:
        config = _team_config(team, settings)
        recent = _search_issues(
            team,
            config,
            settings,
            client,
            build_metrics_jql(team, settings.delivery_metrics.lookback_days),
        )
        recent_by_team[team.id] = recent
        for issue in recent:
            for release in _raw_fix_versions(issue):
                releases.setdefault(release.strip().casefold(), release.strip())

    selected = [
        releases[key] for key in sorted(releases, reverse=True)[:MAX_RELEASES]
    ]
    snapshots = []
    for team in settings.teams:
        snapshot = collect_team_snapshot(
            team,
            settings,
            client,
            now,
            releases=selected,
            recent_issues=recent_by_team[team.id],
        )
        if persist:
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
        now = datetime.now(timezone.utc)
        snapshots = collect_all(settings, now=now, persist=not args.dry_run)
        for snapshot in snapshots:
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
