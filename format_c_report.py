"""Build sprint-only EOD reports grouped by Epic."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from urllib.parse import quote

import requests

from eod_report import (
    AIUpdate,
    Config,
    EODReportError,
    filter_issues_with_recent_activity,
    get_latest_comment,
    get_recent_comment,
    request_error_summary,
)
from report_config import Team

if TYPE_CHECKING:
    from pulse_report import PulseConfig

FORMAT_C_CATEGORY_ORDER = (
    "Blocked",
    "In Progress",
    "In Review",
    "In Deployment",
    "Done",
)


@dataclass(frozen=True)
class EpicProgress:
    percent: int
    basis: str


@dataclass(frozen=True)
class EpicGroup:
    key: str | None
    summary: str
    progress: EpicProgress
    issues: tuple[Mapping[str, Any], ...]


def _jira_json(
    url: str,
    config: PulseConfig,
    session: requests.Session,
    params: Mapping[str, Any] | None = None,
) -> Any:
    try:
        response = session.get(
            url,
            headers={"Accept": "application/json"},
            auth=(config.jira_email, config.jira_api_token),
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise EODReportError(
            "Failed to fetch Jira report data: "
            f"{request_error_summary(exc)}"
        ) from exc


def _field_ids(
    config: PulseConfig, session: requests.Session
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    fields = _jira_json(
        f"{config.jira_base_url}/rest/api/3/field", config, session
    )
    if not isinstance(fields, list):
        raise EODReportError("Jira returned an invalid field list")
    story_points = []
    epic_links = []
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("id"), str):
            continue
        name = str(field.get("name") or "").strip().casefold()
        if name in {"story point estimate", "story points"}:
            story_points.append(field["id"])
        elif name == "epic link":
            epic_links.append(field["id"])
    return tuple(story_points), tuple(epic_links)


def _select_active_sprints(
    team: Team,
    board_id: int,
    sprints: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    active = [
        sprint
        for sprint in sprints
        if str(sprint.get("state") or "").casefold() == "active"
    ]
    if not active:
        raise EODReportError(
            f"No active sprint found for {team.name} board {board_id}"
        )
    team_names = {team.id.casefold(), team.name.casefold()}
    matching = [
        sprint
        for sprint in active
        if any(
            name in str(sprint.get("name") or "").casefold()
            for name in team_names
        )
    ]
    if matching:
        return matching
    if len(active) == 1:
        return active
    raise EODReportError(
        f"Multiple active sprints found for {team.name} board {board_id}, "
        "but none match the team name"
    )


def _fetch_active_sprint_issues(
    team: Team,
    config: PulseConfig,
    session: requests.Session,
    field_ids: Sequence[str],
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    from pulse_report import fetch_board_sprints

    issues_by_key: dict[str, dict[str, Any]] = {}
    sprint_names = []
    fields = ",".join(
        (
            "summary",
            "description",
            "assignee",
            "status",
            "issuetype",
            "parent",
            "comment",
            "timespent",
            "timeoriginalestimate",
            *field_ids,
        )
    )
    for board_id in team.board_ids:
        selected = _select_active_sprints(
            team,
            board_id,
            fetch_board_sprints(board_id, config, session),
        )
        for sprint in selected:
            sprint_id = sprint.get("id")
            if not isinstance(sprint_id, int):
                raise EODReportError("Active Jira sprint is missing its numeric ID")
            sprint_names.append(str(sprint.get("name") or f"Sprint {sprint_id}"))
            start_at = 0
            while True:
                data = _jira_json(
                    f"{config.jira_base_url}/rest/agile/1.0/sprint/"
                    f"{sprint_id}/issue",
                    config,
                    session,
                    {
                        "fields": fields,
                        "startAt": start_at,
                        "maxResults": 100,
                    },
                )
                if not isinstance(data, dict):
                    raise EODReportError("Jira returned invalid sprint issues")
                page = data.get("issues", [])
                if not isinstance(page, list):
                    raise EODReportError("Jira returned invalid sprint issues")
                for issue in page:
                    if isinstance(issue, dict):
                        issues_by_key[str(issue.get("key") or "")] = issue
                start_at += len(page)
                total = data.get("total", start_at)
                if not page or not isinstance(total, int) or start_at >= total:
                    break
    return list(issues_by_key.values()), tuple(sprint_names)


def _is_done(issue: Mapping[str, Any], config: Config) -> bool:
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        return False
    status = fields.get("status")
    if not isinstance(status, dict):
        return False
    name = str(status.get("name") or "").strip().casefold()
    category = status.get("statusCategory")
    category_key = (
        str(category.get("key") or "").strip().casefold()
        if isinstance(category, dict)
        else ""
    )
    return name in config.done_statuses or category_key == "done"


def _number(fields: Mapping[str, Any], names: Sequence[str]) -> float:
    for name in names:
        value = fields.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def _progress(
    issues: Sequence[Mapping[str, Any]],
    config: Config,
    story_point_fields: Sequence[str],
) -> EpicProgress:
    estimated_seconds = 0.0
    logged_seconds = 0.0
    total_points = 0.0
    done_points = 0.0
    done_count = 0
    for issue in issues:
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            continue
        done = _is_done(issue, config)
        estimated_seconds += _number(fields, ("timeoriginalestimate",))
        logged_seconds += _number(fields, ("timespent",))
        points = _number(fields, story_point_fields)
        total_points += points
        if done:
            done_count += 1
            done_points += points

    if logged_seconds > 0 and estimated_seconds > 0:
        percent = round(min(logged_seconds / estimated_seconds, 1.0) * 100)
        logged_hours = round(logged_seconds / 3600, 1)
        estimated_hours = round(estimated_seconds / 3600, 1)
        return EpicProgress(
            percent,
            f"{logged_hours:g}h logged / {estimated_hours:g}h estimated",
        )
    if total_points > 0:
        percent = round(done_points / total_points * 100)
        return EpicProgress(
            percent, f"{done_points:g} / {total_points:g} story points done"
        )
    total = len(issues)
    percent = round(done_count / total * 100) if total else 0
    return EpicProgress(percent, f"{done_count} / {total} tickets done")


def _epic_reference(
    issue: Mapping[str, Any], epic_link_fields: Sequence[str]
) -> tuple[str | None, str | None]:
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        return None, None
    parent = fields.get("parent")
    if isinstance(parent, dict):
        parent_fields = parent.get("fields")
        parent_type = (
            parent_fields.get("issuetype")
            if isinstance(parent_fields, dict)
            else None
        )
        if (
            isinstance(parent_type, dict)
            and str(parent_type.get("name") or "").casefold() == "epic"
        ):
            return (
                str(parent.get("key") or "") or None,
                str(parent_fields.get("summary") or "") or None,
            )
    for field_id in epic_link_fields:
        value = fields.get(field_id)
        if isinstance(value, str) and value.strip():
            return value.strip(), None
    return None, None


def _fetch_epic_summary(
    epic_key: str, config: PulseConfig, session: requests.Session
) -> str:
    data = _jira_json(
        f"{config.jira_base_url}/rest/api/3/issue/"
        f"{quote(epic_key, safe='-')}",
        config,
        session,
        {"fields": "summary"},
    )
    fields = data.get("fields") if isinstance(data, dict) else None
    return (
        str(fields.get("summary") or "No summary")
        if isinstance(fields, dict)
        else "No summary"
    )


def group_format_c_issues(
    all_issues: Sequence[dict[str, Any]],
    eod_config: Config,
    pulse_config: PulseConfig,
    story_point_fields: Sequence[str],
    epic_link_fields: Sequence[str],
    session: requests.Session | None = None,
    now: datetime | None = None,
    visible_keys: set[str] | None = None,
) -> tuple[EpicGroup, ...]:
    """Group sprint issues by Epic while calculating progress from all children."""
    client = session or requests.Session()
    all_by_epic: dict[str | None, list[dict[str, Any]]] = {}
    epic_summaries: dict[str, str] = {}
    for issue in all_issues:
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            continue
        issue_type = fields.get("issuetype")
        if (
            isinstance(issue_type, dict)
            and str(issue_type.get("name") or "").casefold() == "epic"
        ):
            epic_summaries[str(issue.get("key") or "")] = str(
                fields.get("summary") or "No summary"
            )
            continue
        epic_key, epic_summary = _epic_reference(issue, epic_link_fields)
        annotated = dict(issue)
        annotated["_format_c_epic_key"] = epic_key
        all_by_epic.setdefault(epic_key, []).append(annotated)
        if epic_key and epic_summary:
            epic_summaries[epic_key] = epic_summary

    candidates = [issue for values in all_by_epic.values() for issue in values]
    active_issues = (
        [issue for issue in candidates if str(issue.get("key") or "") in visible_keys]
        if visible_keys is not None
        else filter_issues_with_recent_activity(
            candidates,
            eod_config,
            client,
            now,
        )
    )
    active_by_epic: dict[str | None, list[Mapping[str, Any]]] = {}
    for issue in active_issues:
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            continue
        status = fields.get("status")
        status_name = (
            str(status.get("name") or "").strip().casefold()
            if isinstance(status, dict)
            else ""
        )
        status_category = (
            status.get("statusCategory") if isinstance(status, dict) else None
        )
        category_key = (
            str(status_category.get("key") or "").strip().casefold()
            if isinstance(status_category, dict)
            else ""
        )
        supported = (
            status_name in eod_config.done_statuses
            or status_name in eod_config.blocked_statuses
            or status_name in eod_config.review_statuses
            or status_name in eod_config.deploy_statuses
            or (
                category_key == "indeterminate"
                and status_name != "rejected"
            )
        )
        if supported or visible_keys is not None:
            active_by_epic.setdefault(issue.get("_format_c_epic_key"), []).append(
                issue
            )

    groups = []
    for epic_key, active in active_by_epic.items():
        all_children = all_by_epic.get(epic_key, [])
        if epic_key and epic_key not in epic_summaries:
            epic_summaries[epic_key] = _fetch_epic_summary(
                epic_key, pulse_config, client
            )
        groups.append(
            EpicGroup(
                key=epic_key if isinstance(epic_key, str) else None,
                summary=epic_summaries.get(
                    epic_key, "Sprint tickets without an Epic"
                ),
                progress=_progress(
                    all_children, eod_config, story_point_fields
                ),
                issues=tuple(active),
            )
        )
    return tuple(sorted(groups, key=lambda group: group.key or "~"))


def load_format_c_groups(
    team: Team,
    eod_config: Config,
    pulse_config: PulseConfig,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> tuple[tuple[EpicGroup, ...], tuple[str, ...]]:
    """Load active-sprint issues and group recent activity by Epic."""
    client = session or requests.Session()
    story_point_fields, epic_link_fields = _field_ids(pulse_config, client)
    all_issues, sprint_names = _fetch_active_sprint_issues(
        team,
        pulse_config,
        client,
        (*story_point_fields, *epic_link_fields),
    )
    groups = group_format_c_issues(
        all_issues,
        eod_config,
        pulse_config,
        story_point_fields,
        epic_link_fields,
        client,
        now,
    )
    return groups, sprint_names


def _issue_category(issue: Mapping[str, Any], config: Config) -> str:
    fields = issue.get("fields")
    status = fields.get("status") if isinstance(fields, dict) else None
    name = (
        str(status.get("name") or "").strip().casefold()
        if isinstance(status, dict)
        else ""
    )
    if name in config.blocked_statuses:
        return "Blocked"
    if name in config.done_statuses:
        return "Done"
    if name in config.review_statuses:
        return "In Review"
    if name in config.deploy_statuses:
        return "In Deployment"
    return "In Progress"


def _display_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"\{\{(.*?)\}\}", lambda match: f'"{match.group(1)}"', text)
    text = re.sub(r"([\\`*_~\[\]<>])", r"\\\1", text)
    if len(text) > limit:
        text = text[: limit - 3].rstrip(" \\") + "..."
    return text


def format_sprint_label(sprint_names: Sequence[str]) -> str:
    """Render Jira sprint names safely in a compact Mattermost label."""
    return ", ".join(
        dict.fromkeys(_display_text(name, 120) for name in sprint_names)
    )


def format_format_c_group_lines(
    groups: Sequence[EpicGroup],
    config: Config,
    ai_updates: Mapping[str, AIUpdate] | None = None,
    allow_older_comments: bool = False,
) -> list[str]:
    """Format compact Epic headings and flat ticket status lines."""
    enriched = ai_updates or {}
    lines = []
    for group in groups:
        if group.key:
            epic_url = (
                f"{config.jira_base_url}/browse/{quote(group.key, safe='-')}"
            )
            heading = (
                f"[{group.key}]({epic_url}) {_display_text(group.summary, 180)}"
            )
        else:
            heading = "No Epic"
        lines.append(f"**{heading} — {group.progress.percent}% complete**")
        categorized: dict[str, list[tuple[bool, str, str | None]]] = {
            category: [] for category in FORMAT_C_CATEGORY_ORDER
        }
        for issue in group.issues:
            fields = issue.get("fields")
            if not isinstance(fields, dict):
                continue
            key = str(issue.get("key") or "Unknown")
            summary = _display_text(fields.get("summary") or "No summary", 180)
            assignee_data = fields.get("assignee")
            assignee = (
                _display_text(
                    assignee_data.get("displayName") or "Unassigned", 80
                )
                if isinstance(assignee_data, dict)
                else "Unassigned"
            )
            comments = fields.get("comment")
            recent_comment = get_recent_comment(
                comments if isinstance(comments, dict) else None
            )
            latest_comment = (
                get_latest_comment(comments)
                if allow_older_comments and isinstance(comments, dict)
                else None
            )
            ticket_url = (
                f"{config.jira_base_url}/browse/{quote(key, safe='-')}"
            )
            category = _issue_category(issue, config)
            row = (
                f"**{category}** — [{key}]({ticket_url}) {summary} "
                f"— {assignee}"
            )
            ai_update = enriched.get(key)
            update_line = None
            source_comment = recent_comment or latest_comment
            if source_comment:
                update = ai_update.update if ai_update else source_comment
                update_line = f"> *{_display_text(update, 300)}*"
            if category == "Blocked":
                duration = issue.get("_eod_blocked_duration")
                if isinstance(duration, str):
                    row += f" — Blocked for: {_display_text(duration, 80)}"
            categorized[category].append((update_line is not None, row, update_line))
        for category in FORMAT_C_CATEGORY_ORDER:
            entries = categorized[category]
            if not entries:
                continue
            ordered = sorted(
                entries, key=lambda entry: (not entry[0], entry[1].casefold())
            )
            for _, row, update_line in ordered:
                lines.append(row)
                if update_line:
                    lines.append(update_line)
        lines.append("")
    return lines


def format_format_c(
    groups: Sequence[EpicGroup],
    sprint_names: Sequence[str],
    config: Config,
    ai_updates: Mapping[str, AIUpdate] | None = None,
) -> str:
    """Format Epic-grouped active-sprint activity for Mattermost."""
    lines = [
        f"**EOD Progress Report - {config.team_region} Team**",
        "",
        *format_format_c_group_lines(groups, config, ai_updates),
    ]
    if not groups:
        lines.append("_No active-sprint ticket activity in the last 24 hours._")
    return "\n".join(lines).rstrip()
