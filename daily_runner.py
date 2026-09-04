"""Run config-driven EOD reports for any number of teams."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Mapping

import requests

from eod_report import (
    Config,
    EODReportError,
    fetch_jira_tickets,
    filter_issues_with_recent_activity,
    generate_ai_updates,
    parse_and_format,
    parse_and_format_by_status,
    send_to_mattermost,
)
from format_c_report import format_format_c, load_format_c_groups
from pulse_report import PulseConfig
from report_config import (
    ReportConfigError,
    Team,
    load_report_config,
    nominal_schedule_time,
)


def teams_due(
    now: datetime,
    teams: tuple[Team, ...],
    force_team_id: str | None = None,
    force_all: bool = False,
) -> tuple[Team, ...]:
    if force_team_id:
        normalized = force_team_id.strip().casefold()
        selected = tuple(team for team in teams if team.id.casefold() == normalized)
        if not selected:
            raise EODReportError(f"Unknown team ID: {force_team_id}")
        return selected
    if force_all:
        return tuple(team for team in teams if team.daily_schedule)

    due = []
    for team in teams:
        schedule = team.daily_schedule
        if not schedule:
            continue
        local = now.astimezone(schedule.timezone)
        if local.weekday() in schedule.weekdays and local.hour == schedule.time.hour:
            due.append(team)
    return tuple(due)


def requested_report_format(value: str | None) -> str:
    """Normalize an optional workflow input to a supported report format."""
    report_format = (value or "").strip().casefold() or "configured"
    if report_format not in {"configured", "epic", "status", "assignee"}:
        raise EODReportError(
            "REPORT_FORMAT must be 'configured', 'epic', 'status', or 'assignee'"
        )
    return report_format


def report_format_for_team(requested: str, team: Team) -> str:
    """Apply a manual override or return the team's configured daily format."""
    if requested == "configured":
        if not team.daily_schedule:
            raise EODReportError(
                f"{team.name} has no daily format; select a report-format override"
            )
        report_format = team.daily_schedule.report_format
    else:
        report_format = requested
    if report_format == "epic" and not team.board_ids:
        raise EODReportError(f"{team.name} needs a board for epic reports")
    if report_format != "epic" and not (
        team.projects
        or team.filters
        or (team.team_field and team.team_value)
    ):
        raise EODReportError(
            f"{team.name} needs a project, filter, or Team-field mapping "
            f"for {report_format} reports"
        )
    return report_format


def team_environment(
    team: Team,
    base: Mapping[str, str],
    ai_enabled: bool,
    ai_model: str,
    ai_max_tokens: int,
    blocked_statuses: frozenset[str],
    deploy_statuses: frozenset[str],
    done_statuses: frozenset[str],
    review_statuses: frozenset[str],
) -> dict[str, str]:
    values = dict(base)
    values.update(
        {
            "TEAM_REGION": team.id,
            "TEAM_NAME": team.name,
            "JIRA_PROJECTS_JSON": json.dumps(team.projects),
            "JIRA_FILTERS_JSON": json.dumps(team.filters),
            "AI_SUMMARIZE": str(ai_enabled).lower(),
            "OPENROUTER_MODEL": ai_model,
            "OPENROUTER_MAX_TOKENS": str(ai_max_tokens),
            "BLOCKED_STATUSES": ",".join(sorted(blocked_statuses)),
            "DEPLOY_STATUSES": ",".join(sorted(deploy_statuses)),
            "DONE_STATUSES": ",".join(sorted(done_statuses)),
            "REVIEW_STATUSES": ",".join(sorted(review_statuses)),
        }
    )
    if team.team_field and team.team_value:
        values["JIRA_TEAM_FIELD"] = team.team_field
        values["JIRA_TEAM_VALUE"] = team.team_value
    else:
        values.pop("JIRA_TEAM_FIELD", None)
        values.pop("JIRA_TEAM_VALUE", None)
    return values


def generate_updates_with_fallback(
    issues,
    config: Config,
    session: requests.Session,
    include_all_started: bool = False,
):
    try:
        return generate_ai_updates(
            issues, config, session, include_all_started=include_all_started
        )
    except EODReportError as exc:
        print(
            f"Warning: AI enrichment unavailable for {config.team_region}; "
            f"using raw Jira updates: {exc}",
            file=sys.stderr,
        )
        return {}


def main() -> int:
    try:
        settings = load_report_config()
        event_name = os.getenv("GITHUB_EVENT_NAME", "")
        force = event_name == "workflow_dispatch" or os.getenv(
            "DAILY_FORCE_RUN", ""
        ).strip().casefold() in {"1", "true", "yes"}
        manual_team = os.getenv("TEAM_ID", "").strip() or None
        dry_run = os.getenv("DRY_RUN", "").strip().casefold() in {
            "1",
            "true",
            "yes",
        }
        requested_format = requested_report_format(os.getenv("REPORT_FORMAT"))
        now = datetime.now(timezone.utc)
        evaluation_time = (
            nominal_schedule_time(now, os.getenv("GITHUB_EVENT_SCHEDULE"))
            if event_name == "schedule"
            else now
        )
        selected = teams_due(
            evaluation_time,
            settings.teams,
            force_team_id=manual_team,
            force_all=force and not manual_team,
        )
        if not selected:
            print("No daily team report is due.")
            return 0

        client = requests.Session()
        pulse_config = None
        report_count = 0
        for team in selected:
            config = Config.from_env(
                team_environment(
                    team,
                    os.environ,
                    settings.ai.enabled,
                    settings.ai.model,
                    settings.ai.max_tokens,
                    settings.blocked_statuses,
                    settings.deploy_statuses,
                    settings.done_statuses,
                    settings.review_statuses,
                )
            )
            print(f"Processing EOD report for team: {team.name}...")
            current_format = report_format_for_team(requested_format, team)
            print(f"Using {current_format} report format.")
            if current_format == "epic":
                pulse_config = pulse_config or PulseConfig.from_env()
                groups, sprint_names = load_format_c_groups(
                    team, config, pulse_config, client
                )
                issues = [issue for group in groups for issue in group.issues]
                print(
                    f"{len(issues)} active-sprint issue(s) had a recent comment "
                    "or status change."
                )
                if dry_run:
                    continue
                ai_updates = generate_updates_with_fallback(
                    issues,
                    config,
                    client,
                    include_all_started=True,
                )
                report = format_format_c(
                    groups, sprint_names, config, ai_updates
                )
            else:
                issues = fetch_jira_tickets(config, client)
                if current_format == "status":
                    issues = filter_issues_with_recent_activity(
                        issues, config, client
                    )
                print(f"{len(issues)} issue(s) matched the daily report.")
                if dry_run:
                    continue
                ai_updates = generate_updates_with_fallback(
                    issues,
                    config,
                    client,
                    include_all_started=current_format == "status",
                )
                report = (
                    parse_and_format_by_status(issues, config, ai_updates)
                    if current_format == "status"
                    else parse_and_format(issues, config, ai_updates)
                )
            send_to_mattermost(report, config, client)
            report_count += 1
    except (EODReportError, ReportConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    action = "Checked" if dry_run else "Posted"
    count = len(selected) if dry_run else report_count
    print(f"{action} {count} daily EOD report(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
