"""Build a biweekly cross-region Jira sprint highlights report."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import requests

from eod_report import (
    AIUpdate,
    Config,
    EODReportError,
    _openrouter_error_message,
    comment_body_to_text,
    post_openrouter_with_credit_retry,
    request_error_summary,
)
from format_c_report import (
    EpicGroup,
    _field_ids,
    format_format_c_group_lines,
    format_sprint_label,
    group_format_c_issues,
)
from report_config import (
    ReportConfigError,
    Team,
    load_report_config,
    nominal_schedule_time,
)

PULSE_TIMEZONE = ZoneInfo("America/New_York")
MAX_HIGHLIGHTS_PER_CATEGORY = 3
MAX_MATTERMOST_POST_LENGTH = 14_000


def _flag(value: str | None) -> bool:
    normalized = (value or "").strip().casefold()
    if normalized in {"", "0", "false", "no"}:
        return False
    if normalized in {"1", "true", "yes"}:
        return True
    raise EODReportError(f"Expected a boolean value, got {value!r}")


@dataclass(frozen=True)
class PulseConfig:
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    mattermost_webhook_url: str
    openrouter_api_key: str
    openrouter_model: str
    openrouter_max_tokens: int
    teams: tuple[Team, ...]
    blocked_statuses: frozenset[str]
    deploy_statuses: frozenset[str]
    done_statuses: frozenset[str]
    review_statuses: frozenset[str]
    anchor_date: date
    title: str
    timezone: ZoneInfo
    report_time: time
    weekday: int
    cadence_days: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "PulseConfig":
        values = os.environ if env is None else env
        settings = load_report_config(values.get("REPORT_CONFIG"))
        required = (
            ("JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_API_TOKEN", "MATTERMOST_WEBHOOK_URL")
            if settings.pulse.enabled
            else ()
        )
        missing = [name for name in required if not values.get(name, "").strip()]
        if missing:
            raise EODReportError(
                f"Missing required environment variables: {', '.join(missing)}"
            )
        openrouter_api_key = values.get("OPENROUTER_API_KEY", "").strip()
        domain = values.get("JIRA_DOMAIN", "jira.invalid").strip().rstrip("/")
        base_url = (
            domain
            if domain.startswith(("https://", "http://"))
            else f"https://{domain}"
        )
        teams = (
            tuple(
                team
                for team in settings.teams
                if team.include_in_pulse and team.board_ids
            )
            if settings.pulse.enabled
            else ()
        )
        if settings.pulse.enabled and not teams:
            raise EODReportError("Pulse reporting needs at least one configured board")
        try:
            max_tokens = int(
                values.get(
                    "OPENROUTER_MAX_TOKENS", str(settings.ai.max_tokens)
                ).strip()
            )
        except ValueError as exc:
            raise EODReportError("OPENROUTER_MAX_TOKENS must be an integer") from exc
        if not 256 <= max_tokens <= 8192:
            raise EODReportError(
                "OPENROUTER_MAX_TOKENS must be between 256 and 8192"
            )
        return cls(
            jira_base_url=base_url,
            jira_email=values.get("JIRA_EMAIL", "").strip(),
            jira_api_token=values.get("JIRA_API_TOKEN", "").strip(),
            mattermost_webhook_url=values.get(
                "MATTERMOST_WEBHOOK_URL", ""
            ).strip(),
            openrouter_api_key=openrouter_api_key,
            openrouter_model=(
                values.get("OPENROUTER_MODEL", "").strip()
                or settings.ai.model
            ),
            openrouter_max_tokens=max_tokens,
            teams=teams,
            blocked_statuses=settings.blocked_statuses,
            deploy_statuses=settings.deploy_statuses,
            done_statuses=settings.done_statuses,
            review_statuses=settings.review_statuses,
            anchor_date=settings.pulse.anchor_date,
            title=settings.pulse.title,
            timezone=settings.pulse.timezone,
            report_time=settings.pulse.time,
            weekday=settings.pulse.weekday,
            cadence_days=settings.pulse.cadence_days,
        )


def is_pulse_due(
    now: datetime,
    schedule: str | None,
    anchor_date: date,
    force: bool = False,
    report_timezone: ZoneInfo = PULSE_TIMEZONE,
    report_time: time = time(20, 0),
    weekday: int = 4,
    cadence_days: int = 14,
) -> bool:
    """Return whether a nominal GitHub cron run is the biweekly 8 PM ET slot."""
    if force:
        return True
    if not schedule:
        return False
    local = nominal_schedule_time(now, schedule).astimezone(report_timezone)
    days_since_anchor = (local.date() - anchor_date).days
    return (
        local.weekday() == weekday
        and local.hour == report_time.hour
        and days_since_anchor >= 0
        and days_since_anchor % cadence_days == 0
    )


def _jira_get(
    url: str,
    config: PulseConfig,
    params: Mapping[str, Any],
    session: requests.Session,
) -> dict[str, Any]:
    try:
        response = session.get(
            url,
            headers={"Accept": "application/json"},
            auth=(config.jira_email, config.jira_api_token),
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise EODReportError(
            "Failed to fetch Jira sprint data: "
            f"{request_error_summary(exc)}"
        ) from exc
    if not isinstance(data, dict):
        raise EODReportError("Jira returned an invalid sprint payload")
    return data


def fetch_board_sprints(
    board_id: int,
    config: PulseConfig,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    client = session or requests.Session()
    values: list[dict[str, Any]] = []
    start_at = 0
    while True:
        data = _jira_get(
            f"{config.jira_base_url}/rest/agile/1.0/board/{board_id}/sprint",
            config,
            {"state": "active,closed", "startAt": start_at, "maxResults": 50},
            client,
        )
        page = data.get("values", [])
        if not isinstance(page, list):
            raise EODReportError("Jira returned an invalid sprint list")
        values.extend(item for item in page if isinstance(item, dict))
        if data.get("isLast", True) or not page:
            return values
        start_at += len(page)


def _jira_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def select_sprint(
    sprints: Sequence[Mapping[str, Any]], now: datetime
) -> Mapping[str, Any]:
    """Select the sprint ending closest to the report time."""
    utc_now = now.astimezone(timezone.utc)
    candidates: list[tuple[float, Mapping[str, Any]]] = []
    for sprint in sprints:
        boundary = _jira_date(sprint.get("endDate")) or _jira_date(
            sprint.get("completeDate")
        )
        if boundary:
            candidates.append((abs((boundary - utc_now).total_seconds()), sprint))
    if not candidates:
        raise EODReportError("No dated active or closed sprint was found")
    distance, selected = min(candidates, key=lambda item: item[0])
    if distance > 7 * 24 * 60 * 60:
        raise EODReportError("No sprint ending within seven days was found")
    return selected


def select_team_sprint(
    team: Team,
    board_id: int,
    sprints: Sequence[Mapping[str, Any]],
    now: datetime,
    all_teams: Sequence[Team] = (),
) -> Mapping[str, Any]:
    """Select the nearest sprint without mixing another region's sprint."""
    team_names = {team.id.casefold(), team.name.casefold()}
    matching = [
        sprint
        for sprint in sprints
        if any(
            name in str(sprint.get("name") or "").casefold()
            for name in team_names
        )
    ]
    if matching:
        return select_sprint(matching, now)
    if len(sprints) == 1:
        sprint_name = str(sprints[0].get("name") or "").casefold()
        other_team_names = {
            name
            for other in all_teams
            if other.id != team.id
            for name in (other.id.casefold(), other.name.casefold())
        }
        if any(name in sprint_name for name in other_team_names):
            raise EODReportError(
                f"Sprint for {team.name} board {board_id} matches another team"
            )
        return select_sprint(sprints, now)
    raise EODReportError(
        f"Multiple sprints found for {team.name} board {board_id}, "
        "but none match the team name"
    )


def fetch_sprint_issues(
    sprint_id: int,
    config: PulseConfig,
    session: requests.Session | None = None,
    extra_fields: Sequence[str] = (),
) -> list[dict[str, Any]]:
    client = session or requests.Session()
    issues: list[dict[str, Any]] = []
    start_at = 0
    fields = ",".join(
        dict.fromkeys(
            (
                "summary",
                "description",
                "assignee",
                "status",
                "comment",
                *extra_fields,
            )
        )
    )
    while True:
        data = _jira_get(
            f"{config.jira_base_url}/rest/agile/1.0/sprint/{sprint_id}/issue",
            config,
            {
                "fields": fields,
                "startAt": start_at,
                "maxResults": 100,
            },
            client,
        )
        page = data.get("issues", [])
        if not isinstance(page, list):
            raise EODReportError("Jira returned an invalid sprint issue list")
        issues.extend(item for item in page if isinstance(item, dict))
        total = data.get("total", start_at + len(page))
        start_at += len(page)
        if not page or start_at >= total:
            return issues


def _latest_comments(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    comments = value.get("comments", [])
    if not isinstance(comments, list):
        return []
    result = []
    for comment in comments[-5:]:
        if isinstance(comment, dict):
            text = comment_body_to_text(comment.get("body", ""))
            if text:
                result.append(text[:1000])
    return result


def build_pulse_context(
    issues: Sequence[Mapping[str, Any]], config: PulseConfig
) -> list[dict[str, Any]]:
    context = []
    for issue in issues:
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            continue
        status = fields.get("status")
        status_name = (
            str(status.get("name") or "Unknown")
            if isinstance(status, dict)
            else "Unknown"
        )
        category = status.get("statusCategory") if isinstance(status, dict) else None
        category_key = (
            str(category.get("key") or "").casefold()
            if isinstance(category, dict)
            else ""
        )
        if category_key == "done":
            pulse_category = "done"
        elif status_name.strip().casefold() in config.blocked_statuses:
            pulse_category = "blocked"
        else:
            pulse_category = "carryover"
        assignee = fields.get("assignee")
        context.append(
            {
                "key": str(issue.get("key", "Unknown")),
                "category": pulse_category,
                "summary": str(fields.get("summary") or "No summary")[:500],
                "status": status_name,
                "assignee": (
                    str(assignee.get("displayName") or "Unassigned")
                    if isinstance(assignee, dict)
                    else "Unassigned"
                ),
                "description": comment_body_to_text(
                    fields.get("description", "")
                )[:2500],
                "latest_comments": _latest_comments(fields.get("comment")),
            }
        )
    return context


def _pulse_schema() -> dict[str, Any]:
    highlight = {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "text": {"type": "string", "maxLength": 300},
        },
        "required": ["key", "text"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            name: {
                "type": "array",
                "items": highlight,
                "maxItems": MAX_HIGHLIGHTS_PER_CATEGORY,
            }
            for name in ("done", "blocked", "carryover")
        },
        "required": ["done", "blocked", "carryover"],
        "additionalProperties": False,
    }


def generate_region_highlights(
    region: str,
    context: Sequence[Mapping[str, Any]],
    config: PulseConfig,
    session: requests.Session | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Use OpenRouter to select only material sprint highlights."""
    if not context:
        return {"done": [], "blocked": [], "carryover": []}
    if not config.openrouter_api_key or not config.openrouter_api_key.startswith(
        "sk-or-"
    ):
        raise EODReportError(
            "OpenRouter API key is missing or is not a raw key starting with 'sk-or-'"
        )
    client = session or requests.Session()
    prompt = (
        f"Select material sprint highlights for {region}. Return only meaningful "
        "outcomes, blockers, and incomplete work that "
        "must move to the next sprint. Omit routine tasks, duplicates, administrative "
        "updates, and issues without enough evidence. Merge closely related work by "
        "selecting the strongest representative issue. Keep each highlight under 25 "
        "words. For blocked items, state the explicit cause or dependency. For "
        "carryover, state what remains or the next action. Never invent details. Keep "
        "each issue in its provided category and use only provided issue keys.\n\n"
        f"{json.dumps(context, ensure_ascii=True)}"
    )
    try:
        response = post_openrouter_with_credit_retry(
            client,
            {
                "Authorization": "Bearer " + config.openrouter_api_key,
                "Content-Type": "application/json",
                "X-OpenRouter-Title": config.title,
            },
            {
                "model": config.openrouter_model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You create concise, executive-readable engineering "
                            "sprint highlights grounded only in supplied Jira data."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": config.openrouter_max_tokens,
                "reasoning": {"effort": "minimal", "exclude": True},
                "plugins": [{"id": "response-healing"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "sprint_pulse_highlights",
                        "strict": True,
                        "schema": _pulse_schema(),
                    },
                },
            },
        )
        data = response.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise EODReportError(
                "OpenRouter pulse response was truncated; increase "
                "OPENROUTER_MAX_TOKENS"
            )
        result = json.loads(choice["message"]["content"])
    except EODReportError:
        raise
    except requests.RequestException as exc:
        raise EODReportError(
            "Failed to generate sprint highlights with OpenRouter: "
            f"{_openrouter_error_message(exc)}"
        ) from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise EODReportError(
            f"OpenRouter returned an invalid pulse response: {exc}"
        ) from exc

    category_by_key = {
        str(item["key"]): str(item["category"]) for item in context
    }
    validated: dict[str, list[dict[str, str]]] = {}
    seen: set[str] = set()
    for category in ("done", "blocked", "carryover"):
        rows = result.get(category)
        if not isinstance(rows, list):
            raise EODReportError("OpenRouter omitted a pulse category")
        if len(rows) > MAX_HIGHLIGHTS_PER_CATEGORY:
            raise EODReportError("OpenRouter returned too many pulse highlights")
        validated[category] = []
        for row in rows:
            if not isinstance(row, dict):
                raise EODReportError("OpenRouter returned an invalid highlight")
            key, text = row.get("key"), row.get("text")
            if (
                not isinstance(key, str)
                or category_by_key.get(key) != category
                or key in seen
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise EODReportError("OpenRouter returned an invalid highlight")
            seen.add(key)
            validated[category].append({"key": key, "text": text.strip()})
    return validated


def raw_region_highlights(
    context: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    """Build deterministic sprint entries from exact Jira text."""
    highlights = {"done": [], "blocked": [], "carryover": []}
    for item in context:
        category = str(item.get("category", ""))
        if category not in highlights:
            continue
        comments = item.get("latest_comments")
        latest_comment = (
            str(comments[-1]).strip()
            if isinstance(comments, list) and comments
            else ""
        )
        text = latest_comment or str(item.get("summary") or "No update provided.")
        if len(highlights[category]) >= MAX_HIGHLIGHTS_PER_CATEGORY:
            continue
        highlights[category].append(
            {"key": str(item.get("key", "Unknown")), "text": text[:300]}
        )
    return highlights


def generate_highlights_with_fallback(
    team_name: str,
    context: Sequence[Mapping[str, Any]],
    config: PulseConfig,
    session: requests.Session,
) -> dict[str, list[dict[str, str]]]:
    try:
        return generate_region_highlights(team_name, context, config, session)
    except EODReportError as exc:
        print(
            f"Warning: AI highlights unavailable for {team_name}; "
            f"using raw Jira updates: {exc}",
            file=sys.stderr,
        )
        return raw_region_highlights(context)


@dataclass(frozen=True)
class PulseRegionReport:
    groups: tuple[EpicGroup, ...]
    sprint_names: tuple[str, ...]
    eod_config: Config
    updates: Mapping[str, AIUpdate]


def format_pulse_report(
    reports: Mapping[str, PulseRegionReport], config: PulseConfig
) -> str:
    """Format the cross-region pulse with the compact Format C layout."""
    lines = [config.title, "_Format C · grouped by region and Epic_", "---"]
    for team in config.teams:
        regional = reports.get(team.id)
        if not regional:
            lines.extend((f"**{team.name}**", "_No material highlights._", ""))
            continue
        sprint_label = format_sprint_label(regional.sprint_names)
        lines.append(f"**{team.name} · sprint: {sprint_label}**")
        lines.extend(
            format_format_c_group_lines(
                regional.groups,
                regional.eod_config,
                regional.updates,
                allow_older_comments=True,
            )
        )
        if not regional.groups:
            lines.append("_No material highlights._")
        lines.append("")
    return "\n".join(lines).rstrip()


def _eod_config_for_team(team: Team, config: PulseConfig) -> Config:
    values = dict(os.environ)
    values.update(
        {
            "TEAM_REGION": team.id,
            "TEAM_NAME": team.name,
            "JIRA_PROJECTS_JSON": json.dumps(team.projects),
            "JIRA_FILTERS_JSON": json.dumps(team.filters),
            "AI_SUMMARIZE": "true",
            "OPENROUTER_MODEL": config.openrouter_model,
            "OPENROUTER_MAX_TOKENS": str(config.openrouter_max_tokens),
            "BLOCKED_STATUSES": ",".join(sorted(config.blocked_statuses)),
            "DEPLOY_STATUSES": ",".join(sorted(config.deploy_statuses)),
            "DONE_STATUSES": ",".join(sorted(config.done_statuses)),
            "REVIEW_STATUSES": ",".join(sorted(config.review_statuses)),
        }
    )
    return Config.from_env(values)


def send_pulse_to_mattermost(
    report: str,
    config: PulseConfig,
    session: requests.Session | None = None,
) -> None:
    if len(report) > MAX_MATTERMOST_POST_LENGTH:
        raise EODReportError(
            "Sprint pulse report exceeds the single-post Mattermost limit"
        )
    client = session or requests.Session()
    try:
        response = client.post(
            config.mattermost_webhook_url,
            json={
                "text": report,
                "username": "Jira Sprint Reporter",
                "icon_emoji": "bar_chart",
            },
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise EODReportError(
            "Failed to post sprint pulse report to Mattermost: "
            f"{request_error_summary(exc)}"
        ) from exc


def main() -> int:
    try:
        config = PulseConfig.from_env()
        if not config.teams:
            print("Pulse reporting is disabled or has no configured teams.")
            return 0
        force = _flag(os.getenv("PULSE_FORCE_RUN"))
        schedule = os.getenv("GITHUB_EVENT_SCHEDULE")
        if not is_pulse_due(
            datetime.now(timezone.utc),
            schedule,
            config.anchor_date,
            force,
            config.timezone,
            config.report_time,
            config.weekday,
            config.cadence_days,
        ):
            print("Sprint report is not due for this schedule.")
            return 0

        client = requests.Session()
        reports = {}
        story_point_fields, epic_link_fields = _field_ids(config, client)
        format_c_fields = (
            "issuetype",
            "parent",
            "timespent",
            "timeoriginalestimate",
            *story_point_fields,
            *epic_link_fields,
        )
        for team in config.teams:
            issues_by_key = {}
            sprint_names = []
            for board_id in team.board_ids:
                sprints = fetch_board_sprints(board_id, config, client)
                sprint = select_team_sprint(
                    team,
                    board_id,
                    sprints,
                    datetime.now(timezone.utc),
                    config.teams,
                )
                sprint_id = sprint.get("id")
                if not isinstance(sprint_id, int):
                    raise EODReportError(
                        f"{team.name} sprint is missing its numeric ID"
                    )
                sprint_names.append(
                    str(sprint.get("name") or f"Sprint {sprint_id}")
                )
                for issue in fetch_sprint_issues(
                    sprint_id, config, client, format_c_fields
                ):
                    issues_by_key[str(issue.get("key", ""))] = issue
            issues = list(issues_by_key.values())
            context = build_pulse_context(issues, config)
            highlights = generate_highlights_with_fallback(
                team.name, context, config, client
            )
            selected_rows = [
                row
                for category in ("done", "blocked", "carryover")
                for row in highlights[category]
            ]
            visible_keys = {row["key"] for row in selected_rows}
            updates = {
                row["key"]: AIUpdate(row["text"], "") for row in selected_rows
            }
            eod_config = _eod_config_for_team(team, config)
            groups = group_format_c_issues(
                issues,
                eod_config,
                config,
                story_point_fields,
                epic_link_fields,
                client,
                visible_keys=visible_keys,
            )
            reports[team.id] = PulseRegionReport(
                groups, tuple(sprint_names), eod_config, updates
            )
        report = format_pulse_report(reports, config)
        send_pulse_to_mattermost(report, config, client)
    except (EODReportError, ReportConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("Sprint progress report posted successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
