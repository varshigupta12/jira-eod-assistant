"""Build the current-release blocked-item report."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import quote

import requests

from eod_report import (
    Config,
    EODReportError,
    _fetch_issue_changelog,
    _issue_comments,
    _jql_quote,
    build_team_scope_clauses,
    generate_ai_updates,
    request_error_summary,
)
from format_c_report import _display_text
from metrics import (
    continuous_entry,
    format_duration,
    issue_created,
    status_intervals,
)
from report_config import ReportSettings, Team


SQUAD_ICONS = {"apac": "🌏", "emea": "🌍", "amer": "🌎"}
MAX_MATTERMOST_POST_LENGTH = 14_000


@dataclass(frozen=True)
class ReleaseBlocker:
    key: str
    summary: str
    assignee: str
    duration: str | None
    blocked_since: datetime | None
    reason: str


def _team_config(team: Team, settings: ReportSettings) -> Config:
    values = dict(os.environ)
    values.update(
        {
            "TEAM_REGION": team.id,
            "TEAM_NAME": team.name,
            "JIRA_PROJECTS_JSON": json.dumps(team.projects),
            "JIRA_FILTERS_JSON": json.dumps(team.filters),
            "AI_SUMMARIZE": str(settings.ai.enabled).lower(),
            "OPENROUTER_MODEL": settings.ai.model,
            "OPENROUTER_MAX_TOKENS": str(settings.ai.max_tokens),
            "BLOCKED_STATUSES": ",".join(sorted(settings.blocked_statuses)),
            "DEPLOY_STATUSES": ",".join(sorted(settings.deploy_statuses)),
            "DONE_STATUSES": ",".join(sorted(settings.done_statuses)),
            "REVIEW_STATUSES": ",".join(sorted(settings.review_statuses)),
        }
    )
    if team.team_field and team.team_value:
        values["JIRA_TEAM_FIELD"] = team.team_field
        values["JIRA_TEAM_VALUE"] = team.team_value
    else:
        values.pop("JIRA_TEAM_FIELD", None)
        values.pop("JIRA_TEAM_VALUE", None)
    return Config.from_env(values)


def build_release_blocker_jql(
    team: Team,
    release: str,
    blocked_statuses: frozenset[str],
) -> str:
    """Build a squad-scoped query for all current-release blockers."""
    clauses = build_team_scope_clauses(team)
    if not clauses:
        raise EODReportError(
            f"{team.name} needs projects, filters, or a Team-field mapping"
        )
    statuses = ", ".join(
        f'"{_jql_quote(status)}"' for status in sorted(blocked_statuses)
    )
    clauses.extend(
        (
            (
                f'(labels = "{_jql_quote(release)}" '
                f'OR fixVersion = "{_jql_quote(release)}")'
            ),
            f"status in ({statuses})",
        )
    )
    return " AND ".join(clauses) + " ORDER BY updated DESC"


def _fetch_team_blockers(
    team: Team,
    release: str,
    config: Config,
    session: requests.Session,
) -> list[dict[str, Any]]:
    issues = []
    next_page_token = None
    while True:
        params: dict[str, Any] = {
            "jql": build_release_blocker_jql(
                team, release, config.blocked_statuses
            ),
            "fields": "summary,description,assignee,status,comment,created",
            "maxResults": 100,
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
                f"Failed to fetch {team.name} release blockers: "
                f"{request_error_summary(exc)}"
            ) from exc
        page = data.get("issues", [])
        if not isinstance(page, list):
            raise EODReportError(
                f"Jira returned invalid release blockers for {team.name}"
            )
        issues.extend(issue for issue in page if isinstance(issue, dict))
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            return issues


def _blocked_since(
    histories: list[Mapping[str, Any]],
    blocked_statuses: frozenset[str],
    created: datetime | None = None,
    current_status: str = "",
) -> datetime | None:
    intervals = status_intervals(histories, created, current_status)
    return continuous_entry(intervals, blocked_statuses)


def _blocked_duration(
    blocked_since: datetime | None, now: datetime
) -> str | None:
    if blocked_since is None:
        return None
    return format_duration(now - blocked_since.astimezone(timezone.utc))


def collect_release_blockers(
    settings: ReportSettings,
    session: requests.Session,
    now: datetime | None = None,
) -> dict[str, tuple[ReleaseBlocker, ...]]:
    """Collect, enrich, deduplicate, and sort release blockers by squad."""
    release = settings.release_blockers
    if not release.enabled or not release.label:
        return {}
    current_time = now or datetime.now(timezone.utc)
    seen: set[str] = set()
    result = {}
    for team in settings.teams:
        if not team.include_in_pulse:
            continue
        config = _team_config(team, settings)
        issues = []
        duration_data = {}
        for issue in _fetch_team_blockers(
            team, release.label, config, session
        ):
            key = str(issue.get("key") or "Unknown")
            if key in seen:
                print(
                    f"Warning: release blocker {key} also matched {team.name}; "
                    "keeping its first squad assignment.",
                    file=sys.stderr,
                )
                continue
            seen.add(key)
            fields = issue.get("fields")
            if not isinstance(fields, dict):
                continue
            comments = _issue_comments(issue, config, session)
            enriched_issue = dict(issue)
            enriched_fields = dict(fields)
            if comments is not None:
                enriched_fields["comment"] = comments
            enriched_issue["fields"] = enriched_fields
            histories = _fetch_issue_changelog(key, config, session)
            status_data = fields.get("status")
            blocked_since = _blocked_since(
                histories,
                config.blocked_statuses,
                created=issue_created(fields),
                current_status=(
                    str(status_data.get("name") or "")
                    if isinstance(status_data, dict)
                    else ""
                ),
            )
            duration_data[key] = (
                blocked_since,
                _blocked_duration(blocked_since, current_time),
            )
            if duration_data[key][0]:
                enriched_issue["_eod_blocked_since"] = (
                    duration_data[key][0].isoformat()
                )
            issues.append(enriched_issue)
        if not issues:
            continue
        try:
            updates = generate_ai_updates(
                issues, config, session, include_all_blocked=True
            )
        except EODReportError as exc:
            print(
                f"Warning: blocker summaries unavailable for {team.name}: {exc}",
                file=sys.stderr,
            )
            updates = {}
        blockers = []
        for issue in issues:
            key = str(issue.get("key") or "Unknown")
            fields = issue["fields"]
            assignee = fields.get("assignee")
            update = updates.get(key)
            blocked_since, duration = duration_data[key]
            blockers.append(
                ReleaseBlocker(
                    key=key,
                    summary=str(fields.get("summary") or "No summary"),
                    assignee=(
                        str(assignee.get("displayName") or "Unassigned")
                        if isinstance(assignee, dict)
                        else "Unassigned"
                    ),
                    duration=duration,
                    blocked_since=blocked_since,
                    reason=(
                        update.blocker_reason.strip()
                        if update and update.blocker_reason.strip()
                        else ""
                    ),
                )
            )
        blockers.sort(
            key=lambda item: (
                item.blocked_since is None,
                item.blocked_since
                or datetime.max.replace(tzinfo=timezone.utc),
            )
        )
        result[team.id] = tuple(blockers)
    return result


def format_release_blocker_report(
    blockers: Mapping[str, tuple[ReleaseBlocker, ...]],
    settings: ReportSettings,
    jira_base_url: str,
) -> str | None:
    """Format a concise release report, omitting empty squads and reports."""
    if not blockers or not settings.release_blockers.label:
        return None
    lines = [
        f"**🚧 Release {_display_text(settings.release_blockers.label, 80)} "
        "Blocked Items**"
    ]
    rendered_squads = 0
    for team in settings.teams:
        items = blockers.get(team.id, ())
        if not items:
            continue
        if rendered_squads:
            lines.extend(("", "---"))
        lines.extend(
            (
                "",
                f"**{SQUAD_ICONS.get(team.id, '👥')} "
                f"{_display_text(team.name, 80)} Squad**",
                "",
            )
        )
        for item in items:
            url = f"{jira_base_url}/browse/{quote(item.key, safe='-')}"
            duration = item.duration or "Duration unavailable"
            lines.append(
                f"• [{item.key}]({url}) {_display_text(item.summary, 180)} "
                f"— {_display_text(item.assignee, 80)} — "
                f"**Blocked for: {_display_text(duration, 80)}**"
            )
            if item.reason:
                lines.append(f"> *{_display_text(item.reason, 300)}*")
            lines.append("")
        rendered_squads += 1
    return "\n".join(lines).rstrip() if rendered_squads else None


def split_release_blocker_report(report: str) -> tuple[str, ...]:
    """Split a large report between item paragraphs for Mattermost."""
    if len(report) <= MAX_MATTERMOST_POST_LENGTH:
        return (report,)
    title = report.splitlines()[0]
    continuation = (
        title[:-2] + " (continued)**" if title.endswith("**") else title
    )
    posts = []
    paragraphs = report.split("\n\n")
    current = []
    for paragraph in paragraphs:
        candidate = "\n\n".join((*current, paragraph))
        if len(candidate) <= MAX_MATTERMOST_POST_LENGTH:
            current.append(paragraph)
            continue
        if not current:
            raise EODReportError(
                "One release blocker entry exceeds the Mattermost post limit"
            )
        posts.append("\n\n".join(current))
        current = [continuation, paragraph]
        if len("\n\n".join(current)) > MAX_MATTERMOST_POST_LENGTH:
            raise EODReportError(
                "One release blocker entry exceeds the Mattermost post limit"
            )
    if current:
        posts.append("\n\n".join(current))
    return tuple(posts)


def post_release_blocker_report(
    settings: ReportSettings,
    jira_base_url: str,
    mattermost_webhook_url: str,
    session: requests.Session,
) -> bool:
    """Post configured release blockers, returning false when none exist."""
    report = format_release_blocker_report(
        collect_release_blockers(settings, session),
        settings,
        jira_base_url,
    )
    if not report:
        print("No current-release blocked items found; skipping blocked report.")
        return False
    for post in split_release_blocker_report(report):
        try:
            response = session.post(
                mattermost_webhook_url,
                json={
                    "text": post,
                    "username": "Jira Sprint Reporter",
                    "icon_emoji": "construction",
                },
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise EODReportError(
                "Failed to post release blocker report to Mattermost: "
                f"{request_error_summary(exc)}"
            ) from exc
    print("Release blocker report posted successfully.")
    return True
