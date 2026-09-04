"""Build regional Jira end-of-day reports and publish them to Mattermost."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import requests

DEFAULT_DONE_STATUSES = ("done", "closed", "resolved")
DEFAULT_BLOCKED_STATUSES = ("blocked", "impediment")
DEFAULT_DEPLOY_STATUSES = ("to be deployed", "ready for deployment", "ready to deploy")
DEFAULT_REVIEW_STATUSES = ("in review", "code review")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class EODReportError(RuntimeError):
    """Raised when the report cannot be generated or delivered."""


def request_error_summary(exc: Exception) -> str:
    """Describe a request failure without exposing URLs or query parameters."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return f"HTTP {status_code}" if status_code else type(exc).__name__


def _csv_values(value: str | None, defaults: Sequence[str]) -> frozenset[str]:
    values = value.split(",") if value else defaults
    return frozenset(item.strip().casefold() for item in values if item.strip())


def _env_flag(value: str | None) -> bool:
    normalized = (value or "").strip().casefold()
    if normalized in {"", "0", "false", "no"}:
        return False
    if normalized in {"1", "true", "yes"}:
        return True
    raise EODReportError(f"Expected a boolean value, got {value!r}")


def _env_list(
    values: Mapping[str, str], json_name: str, csv_name: str
) -> tuple[str, ...]:
    raw_json = values.get(json_name, "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except ValueError as exc:
            raise EODReportError(f"{json_name} must be valid JSON") from exc
        if not isinstance(parsed, list) or any(
            not isinstance(item, str) or not item.strip() for item in parsed
        ):
            raise EODReportError(f"{json_name} must be a JSON array of strings")
        return tuple(item.strip() for item in parsed)
    return tuple(
        item.strip()
        for item in values.get(csv_name, "").split(",")
        if item.strip()
    )


@dataclass(frozen=True)
class Config:
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    mattermost_webhook_url: str
    team_region: str
    jira_projects: tuple[str, ...]
    jira_filters: tuple[str, ...]
    jira_team_field: str | None
    jira_team_value: str | None
    ai_summarize: bool
    openrouter_api_key: str | None
    openrouter_model: str
    openrouter_max_tokens: int
    done_statuses: frozenset[str]
    blocked_statuses: frozenset[str]
    deploy_statuses: frozenset[str]
    review_statuses: frozenset[str]

    @property
    def jira_project(self) -> str:
        return self.jira_projects[0] if self.jira_projects else ""

    @property
    def jira_filter(self) -> str | None:
        return self.jira_filters[0] if self.jira_filters else None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        values = os.environ if env is None else env
        required = (
            "JIRA_DOMAIN",
            "JIRA_EMAIL",
            "JIRA_API_TOKEN",
            "MATTERMOST_WEBHOOK_URL",
        )
        missing = [name for name in required if not values.get(name, "").strip()]
        if missing:
            raise EODReportError(
                f"Missing required environment variables: {', '.join(missing)}"
            )

        team_id = values.get("TEAM_REGION", "team").strip()
        team_name = values.get("TEAM_NAME", team_id).strip()
        if not team_id or not team_name:
            raise EODReportError("TEAM_REGION and TEAM_NAME cannot be empty")
        suffix = team_id.upper().replace("-", "_")
        projects_are_explicit = bool(
            values.get("JIRA_PROJECTS_JSON", "").strip()
        )
        projects = _env_list(values, "JIRA_PROJECTS_JSON", "JIRA_PROJECTS")
        if not projects and not projects_are_explicit:
            legacy_project = (
                values.get("JIRA_PROJECT")
                or values.get(f"JIRA_PROJECT_{suffix}")
                or team_id
            ).strip()
            projects = (legacy_project,) if legacy_project else ()
        jira_filters = _env_list(values, "JIRA_FILTERS_JSON", "JIRA_FILTERS")
        if not jira_filters:
            legacy_filter = (
                values.get("JIRA_FILTER")
                or values.get(f"JIRA_FILTER_{suffix}")
                or ""
            ).strip()
            jira_filters = (legacy_filter,) if legacy_filter else ()
        team_field = values.get("JIRA_TEAM_FIELD", "").strip() or None
        team_value = (
            values.get("JIRA_TEAM_VALUE")
            or values.get(f"JIRA_TEAM_{suffix}")
            or ""
        ).strip() or None
        if bool(team_field) != bool(team_value):
            raise EODReportError(
                "JIRA_TEAM_FIELD and JIRA_TEAM_VALUE must both be set"
            )
        ai_summarize = _env_flag(values.get("AI_SUMMARIZE"))
        openrouter_api_key = values.get("OPENROUTER_API_KEY", "").strip() or None
        try:
            openrouter_max_tokens = int(
                values.get("OPENROUTER_MAX_TOKENS", "2048").strip()
            )
        except ValueError as exc:
            raise EODReportError("OPENROUTER_MAX_TOKENS must be an integer") from exc
        if not 256 <= openrouter_max_tokens <= 8192:
            raise EODReportError(
                "OPENROUTER_MAX_TOKENS must be between 256 and 8192"
            )

        domain = values["JIRA_DOMAIN"].strip().rstrip("/")
        base_url = domain if domain.startswith(("https://", "http://")) else f"https://{domain}"

        return cls(
            jira_base_url=base_url,
            jira_email=values["JIRA_EMAIL"].strip(),
            jira_api_token=values["JIRA_API_TOKEN"].strip(),
            mattermost_webhook_url=values["MATTERMOST_WEBHOOK_URL"].strip(),
            team_region=team_name,
            jira_projects=projects,
            jira_filters=jira_filters,
            jira_team_field=team_field,
            jira_team_value=team_value,
            ai_summarize=ai_summarize,
            openrouter_api_key=openrouter_api_key,
            openrouter_model=(
                values.get("OPENROUTER_MODEL", "").strip()
                or "google/gemini-3.7-flash"
            ),
            openrouter_max_tokens=openrouter_max_tokens,
            done_statuses=_csv_values(
                values.get("DONE_STATUSES"), DEFAULT_DONE_STATUSES
            ),
            blocked_statuses=_csv_values(
                values.get("BLOCKED_STATUSES"), DEFAULT_BLOCKED_STATUSES
            ),
            deploy_statuses=_csv_values(
                values.get("DEPLOY_STATUSES"), DEFAULT_DEPLOY_STATUSES
            ),
            review_statuses=_csv_values(
                values.get("REVIEW_STATUSES"), DEFAULT_REVIEW_STATUSES
            ),
        )


def _jql_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_jql(config: Config) -> str:
    clauses = []
    if config.jira_projects:
        projects = " OR ".join(
            f'project = "{_jql_quote(project)}"' for project in config.jira_projects
        )
        clauses.append(f"({projects})")
    if config.jira_filters:
        filters = " OR ".join(
            f'filter = "{_jql_quote(saved_filter)}"'
            for saved_filter in config.jira_filters
        )
        clauses.append(f"({filters})")
    elif config.jira_team_field and config.jira_team_value:
        clauses.append(
            f'"{_jql_quote(config.jira_team_field)}" = '
            f'"{_jql_quote(config.jira_team_value)}"'
        )
    if not clauses:
        raise EODReportError(
            "At least one Jira project, saved filter, or Team-field mapping is required"
        )
    clauses.append('statusCategory != "To Do"')
    clauses.append("(updated >= -1d OR resolved >= -1d)")
    return " AND ".join(clauses) + " ORDER BY assignee ASC, updated DESC"


def fetch_jira_tickets(
    config: Config, session: requests.Session | None = None
) -> list[dict[str, Any]]:
    """Fetch all Jira issues matching the regional EOD query."""
    client = session or requests.Session()
    url = f"{config.jira_base_url}/rest/api/3/search/jql"
    issues: list[dict[str, Any]] = []
    next_page_token: str | None = None

    while True:
        params = {
            "jql": build_jql(config),
            "fields": "summary,description,assignee,status,issuetype,comment",
            "maxResults": 100,
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token

        try:
            response = client.get(
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
                "Failed to fetch tickets from Jira: "
                f"{request_error_summary(exc)}"
            ) from exc

        page = data.get("issues", [])
        if not isinstance(page, list):
            raise EODReportError("Jira returned an invalid issues payload")
        issues.extend(page)

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            return issues


def _is_started(issue: Mapping[str, Any]) -> bool:
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        raise EODReportError("Jira issue is missing its fields object")
    status = fields.get("status")
    if not isinstance(status, dict):
        return True
    category = status.get("statusCategory")
    return not (
        isinstance(category, dict)
        and str(category.get("key", "")).strip().casefold() == "new"
    )


def _adf_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(_adf_text(item) for item in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "hardBreak":
        return "\n"
    if isinstance(node.get("text"), str):
        return node["text"]
    attrs = node.get("attrs")
    if node.get("type") == "mention" and isinstance(attrs, dict):
        return str(attrs.get("text", ""))
    if node.get("type") in {"blockCard", "embedCard", "inlineCard"} and isinstance(
        attrs, dict
    ):
        return str(attrs.get("url", ""))
    return _adf_text(node.get("content", []))


def comment_body_to_text(body: Any) -> str:
    """Convert Jira plain-text or Atlassian Document Format comments to one line."""
    return re.sub(r"\s+", " ", _adf_text(body)).strip()


def _parse_jira_datetime(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_recent_comment(
    comments_data: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> str | None:
    """Return the newest non-empty comment created during the last 24 hours."""
    if not comments_data:
        return None
    comments = comments_data.get("comments", [])
    if not isinstance(comments, list):
        return None

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    cutoff = current_time.astimezone(timezone.utc) - timedelta(hours=24)

    candidates: list[tuple[datetime, str]] = []
    for comment in comments:
        if not isinstance(comment, dict) or not comment.get("created"):
            continue
        try:
            created = _parse_jira_datetime(str(comment["created"]))
        except ValueError:
            continue
        text = comment_body_to_text(comment.get("body", ""))
        if created >= cutoff and text:
            candidates.append((created, text))

    return max(candidates, default=(cutoff, ""))[1] or None


def get_latest_comment(comments_data: Mapping[str, Any] | None) -> str | None:
    """Return the newest non-empty Jira comment regardless of its age."""
    if not comments_data:
        return None
    comments = comments_data.get("comments", [])
    if not isinstance(comments, list):
        return None
    candidates: list[tuple[datetime, str]] = []
    for comment in comments:
        if not isinstance(comment, dict) or not comment.get("created"):
            continue
        try:
            created = _parse_jira_datetime(str(comment["created"]))
        except ValueError:
            continue
        text = comment_body_to_text(comment.get("body", ""))
        if text:
            candidates.append((created, text))
    return max(candidates, default=(datetime.min.replace(tzinfo=timezone.utc), ""))[1] or None


def get_recent_comments(
    comments_data: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> list[str]:
    """Return non-empty comments from the last 24 hours in chronological order."""
    if not comments_data:
        return []
    comments = comments_data.get("comments", [])
    if not isinstance(comments, list):
        return []

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    cutoff = current_time.astimezone(timezone.utc) - timedelta(hours=24)
    recent: list[tuple[datetime, str]] = []
    for comment in comments:
        if not isinstance(comment, dict) or not comment.get("created"):
            continue
        try:
            created = _parse_jira_datetime(str(comment["created"]))
        except ValueError:
            continue
        text = comment_body_to_text(comment.get("body", ""))
        if created >= cutoff and text:
            recent.append((created, text))
    return [text for _, text in sorted(recent)]


def _fetch_issue_changelog(
    issue_key: str,
    config: Config,
    session: requests.Session | None = None,
) -> list[Mapping[str, Any]]:
    """Fetch the complete Jira changelog for one issue."""
    client = session or requests.Session()
    url = (
        f"{config.jira_base_url}/rest/api/3/issue/"
        f"{quote(issue_key, safe='-')}/changelog"
    )
    start_at = 0
    all_histories: list[Mapping[str, Any]] = []

    while True:
        try:
            response = client.get(
                url,
                headers={"Accept": "application/json"},
                auth=(config.jira_email, config.jira_api_token),
                params={"startAt": start_at, "maxResults": 100},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise EODReportError(
                "Failed to fetch Jira changelog: "
                f"{request_error_summary(exc)}"
            ) from exc

        histories = data.get("values", [])
        if not isinstance(histories, list):
            raise EODReportError(f"Jira returned an invalid changelog for {issue_key}")
        all_histories.extend(
            history for history in histories if isinstance(history, dict)
        )

        start_at += len(histories)
        total = data.get("total")
        if not histories or not isinstance(total, int) or start_at >= total:
            break

    return all_histories


def _status_transitions(
    histories: Sequence[Mapping[str, Any]],
) -> list[tuple[datetime, str, str]]:
    transitions: list[tuple[datetime, str, str]] = []
    for history in histories:
        if not history.get("created"):
            continue
        try:
            created = _parse_jira_datetime(str(history["created"]))
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


def _recent_status_change(
    histories: Sequence[Mapping[str, Any]],
    now: datetime | None = None,
) -> str | None:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    cutoff = current_time.astimezone(timezone.utc) - timedelta(hours=24)
    candidates = [
        (created, f"Status changed from {previous} to {current}.")
        for created, previous, current in _status_transitions(histories)
        if created >= cutoff
    ]
    return max(candidates, default=(cutoff, ""))[1] or None


def get_recent_status_change(
    issue_key: str,
    config: Config,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return the newest Jira status transition recorded during the last 24 hours."""
    histories = _fetch_issue_changelog(issue_key, config, session)
    return _recent_status_change(histories, now)


def _current_blocked_duration(
    histories: Sequence[Mapping[str, Any]],
    blocked_statuses: frozenset[str],
    now: datetime | None = None,
) -> str | None:
    blocked_since: datetime | None = None
    for created, previous, current in _status_transitions(histories):
        previous_is_blocked = previous.strip().casefold() in blocked_statuses
        current_is_blocked = current.strip().casefold() in blocked_statuses
        if current_is_blocked and not previous_is_blocked:
            blocked_since = created
        elif not current_is_blocked:
            blocked_since = None
    if blocked_since is None:
        return None

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    elapsed_seconds = max(
        0,
        int(
            (
                current_time.astimezone(timezone.utc)
                - blocked_since.astimezone(timezone.utc)
            ).total_seconds()
        ),
    )
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


def filter_issues_with_recent_activity(
    issues: Sequence[Mapping[str, Any]],
    config: Config,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Keep issues with a recent comment or status transition."""
    client = session or requests.Session()
    active: list[dict[str, Any]] = []
    for issue in issues:
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            raise EODReportError("Jira issue is missing its fields object")
        comments = fields.get("comment")
        recent_comment = get_recent_comment(
            comments if isinstance(comments, dict) else None, now
        )
        status_data = fields.get("status")
        normalized_status = (
            str(status_data.get("name") or "").strip().casefold()
            if isinstance(status_data, dict)
            else ""
        )
        is_blocked = normalized_status in config.blocked_statuses
        histories = None
        status_change = None
        if not recent_comment or is_blocked:
            histories = _fetch_issue_changelog(
                str(issue.get("key", "Unknown")), config, client
            )
        if not recent_comment and histories is not None:
            status_change = _recent_status_change(histories, now)
        if recent_comment or status_change:
            active_issue = dict(issue)
            active_issue["_eod_status_change"] = status_change
            if is_blocked and histories is not None:
                active_issue["_eod_blocked_duration"] = _current_blocked_duration(
                    histories, config.blocked_statuses, now
                )
            active.append(active_issue)
    return active


@dataclass(frozen=True)
class AIUpdate:
    update: str
    blocker_reason: str


def _ai_issue_context(issue: Mapping[str, Any]) -> dict[str, Any]:
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        raise EODReportError("Jira issue is missing its fields object")
    status = fields.get("status")
    comment_data = fields.get("comment")
    comments = comment_data if isinstance(comment_data, dict) else None
    return {
        "key": str(issue.get("key", "Unknown")),
        "summary": str(fields.get("summary") or "No summary")[:500],
        "description": comment_body_to_text(fields.get("description", ""))[:4000],
        "status": (
            str(status.get("name") or "Unknown")
            if isinstance(status, dict)
            else "Unknown"
        ),
        "recent_comments": get_recent_comments(comments)[-10:],
        "latest_comment": get_latest_comment(comments),
        "recent_status_change": issue.get("_eod_status_change"),
    }


def _ai_response_schema() -> dict[str, Any]:
    item = {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "update": {
                "type": "string",
                "maxLength": 240,
                "description": (
                    "At most 20 words describing the concrete outcome, change, "
                    "or current state without repeating the ticket title."
                ),
            },
            "blocker_reason": {
                "type": "string",
                "maxLength": 240,
                "description": (
                    "At most 20 words stating the explicit cause, dependency, and "
                    "needed action when available, or an empty string."
                ),
            },
        },
        "required": ["key", "update", "blocker_reason"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"issues": {"type": "array", "items": item}},
        "required": ["issues"],
        "additionalProperties": False,
    }


def _openrouter_error_message(exc: requests.RequestException) -> str:
    response = exc.response
    if response is None:
        return request_error_summary(exc)
    try:
        payload = response.json()
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return f"HTTP {response.status_code}: {error['message'][:500]}"
    except ValueError:
        pass
    return request_error_summary(exc)


def post_openrouter_with_credit_retry(
    client: requests.Session,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
) -> requests.Response:
    """Retry a credit-limited request using OpenRouter's affordable token cap."""

    def send(body: Mapping[str, Any]) -> requests.Response:
        response = client.post(
            OPENROUTER_URL,
            headers=headers,
            json=body,
            timeout=60,
        )
        response.raise_for_status()
        return response

    try:
        return send(payload)
    except requests.HTTPError as exc:
        response = exc.response
        if response is None or response.status_code != 402:
            raise
        try:
            error = response.json().get("error", {})
        except ValueError:
            raise
        message = error.get("message") if isinstance(error, dict) else None
        match = (
            re.search(r"can only afford\s+(\d+)", message, re.IGNORECASE)
            if isinstance(message, str)
            else None
        )
        requested = payload.get("max_tokens")
        if not match or not isinstance(requested, int):
            raise
        affordable = int(match.group(1))
        retry_tokens = min(requested - 1, affordable)
        if retry_tokens < 256:
            raise
        retry_payload = dict(payload)
        retry_payload["max_tokens"] = retry_tokens
        print(
            f"Warning: OpenRouter credit limited output to {affordable} tokens; "
            f"retrying with {retry_tokens}.",
            file=sys.stderr,
        )
        return send(retry_payload)


def generate_ai_updates(
    issues: Sequence[Mapping[str, Any]],
    config: Config,
    session: requests.Session | None = None,
    include_all_started: bool = False,
) -> dict[str, AIUpdate]:
    """Use OpenRouter to turn Jira context into concise, grounded updates."""
    if not config.ai_summarize:
        return {}
    if not config.openrouter_api_key or not config.openrouter_api_key.startswith(
        "sk-or-"
    ):
        raise EODReportError(
            "OpenRouter API key is missing or is not a raw key starting with 'sk-or-'"
        )

    client = session or requests.Session()
    contexts = []
    for issue in issues:
        if not _is_started(issue):
            continue
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            raise EODReportError("Jira issue is missing its fields object")
        status_data = fields.get("status")
        status = (
            str(status_data.get("name") or "Unknown").strip().casefold()
            if isinstance(status_data, dict)
            else "unknown"
        )
        assignee = fields.get("assignee")
        comment_data = fields.get("comment")
        recent_comment = get_recent_comment(
            comment_data if isinstance(comment_data, dict) else None
        )
        if include_all_started:
            latest_comment = get_latest_comment(
                comment_data if isinstance(comment_data, dict) else None
            )
            if not recent_comment and not (
                status == "rejected" and latest_comment
            ):
                continue
        elif status in config.done_statuses or (
            not isinstance(assignee, dict) and not recent_comment
        ):
            continue
        contexts.append(_ai_issue_context(issue))
    updates: dict[str, AIUpdate] = {}
    for offset in range(0, len(contexts), 8):
        batch = contexts[offset : offset + 8]
        expected_keys = {item["key"] for item in batch}
        prompt = (
            "Create executive-readable EOD updates from the Jira data below. Each "
            "update must be one direct sentence of at most 20 words, focused on the "
            "latest concrete outcome, change, or current state. Do not repeat the "
            "ticket key or title. Remove greetings, chronology, names, hedging, and "
            "phrases such as 'the team reported'. Use only facts in the summary, "
            "description, status, and recent comments. Do not infer work, causes, "
            "owners, dates, or dependencies that are not explicit. For blocked "
            "issues, blocker_reason must be at most 20 words and state the explicit "
            "cause, dependency, and needed action when available. If no blocker is "
            "explicit, set blocker_reason to an empty string. For issues whose current "
            "status is Rejected, use update only for the explicit rejection reason in "
            "the latest comment; otherwise say 'No rejection reason recorded.' Ignore "
            "recent_status_change when writing updates. If no concrete progress is "
            "reported, say 'No concrete progress reported.' Return one result for every "
            "issue key.\n\n"
            f"{json.dumps(batch, ensure_ascii=True)}"
        )
        try:
            response = post_openrouter_with_credit_retry(
                client,
                {
                    "Authorization": "Bearer " + config.openrouter_api_key,
                    "Content-Type": "application/json",
                    "X-OpenRouter-Title": "Jira EOD Assistant",
                },
                {
                    "model": config.openrouter_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You summarize engineering status accurately and "
                                "never add unsupported details."
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
                            "name": "jira_eod_updates",
                            "strict": True,
                            "schema": _ai_response_schema(),
                        },
                    },
                },
            )
            response_data = response.json()
            choice = response_data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise EODReportError(
                    "OpenRouter response was truncated; increase "
                    "OPENROUTER_MAX_TOKENS"
                )
            content = choice["message"]["content"]
            result = json.loads(content)
        except EODReportError:
            raise
        except requests.RequestException as exc:
            raise EODReportError(
                "Failed to generate intelligent updates with OpenRouter: "
                f"{_openrouter_error_message(exc)}"
            ) from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise EODReportError(
                f"OpenRouter returned an invalid response: {exc}"
            ) from exc

        rows = result.get("issues")
        if not isinstance(rows, list):
            raise EODReportError("OpenRouter returned an invalid issues payload")
        returned_keys: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise EODReportError("OpenRouter returned an invalid issue update")
            key = row.get("key")
            update = row.get("update")
            blocker_reason = row.get("blocker_reason")
            if (
                not isinstance(key, str)
                or key not in expected_keys
                or key in returned_keys
                or not isinstance(update, str)
                or not update.strip()
                or not isinstance(blocker_reason, str)
            ):
                raise EODReportError("OpenRouter returned an invalid issue update")
            returned_keys.add(key)
            updates[key] = AIUpdate(update.strip(), blocker_reason.strip())
        if returned_keys != expected_keys:
            raise EODReportError("OpenRouter did not return every requested issue")
    return updates


@dataclass(frozen=True)
class ReportItem:
    rank: int
    text: str


def _issue_item(
    issue: Mapping[str, Any],
    config: Config,
    ai_updates: Mapping[str, AIUpdate],
) -> tuple[str, ReportItem] | None:
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        raise EODReportError("Jira issue is missing its fields object")

    key = str(issue.get("key", "Unknown"))
    summary = str(fields.get("summary") or "No summary")
    assignee_data = fields.get("assignee")
    assignee = (
        str(assignee_data.get("displayName") or "Unassigned")
        if isinstance(assignee_data, dict)
        else "Unassigned"
    )
    status_data = fields.get("status")
    status = (
        str(status_data.get("name") or "Unknown")
        if isinstance(status_data, dict)
        else "Unknown"
    )
    normalized_status = status.strip().casefold()
    ticket_url = f"{config.jira_base_url}/browse/{quote(key, safe='-')}"
    link = f"[{key}]({ticket_url}) {summary}"
    comment_data = fields.get("comment")
    recent_comment = get_recent_comment(
        comment_data if isinstance(comment_data, dict) else None
    )
    if assignee == "Unassigned" and not recent_comment:
        return None
    ai_update = ai_updates.get(key)

    if normalized_status in config.blocked_statuses:
        reason = (
            ai_update.blocker_reason or "No explicit blocker reason was found."
            if ai_update
            else recent_comment or "No recent comment logged explaining the blocker."
        )
        context = f"\n  > *Context:* {ai_update.update}" if ai_update else ""
        return assignee, ReportItem(
            0,
            f"🔴 **BLOCKED:** {link}{context}\n"
            f"  > ⚠️ *Blocker reason:* {reason}",
        )
    if normalized_status in config.deploy_statuses:
        deployment_update = ai_update.update if ai_update else recent_comment
        note = (
            f"\n  > 🚀 *Deployment note:* {deployment_update}"
            if deployment_update
            else ""
        )
        return assignee, ReportItem(1, f"🚀 **TO BE DEPLOYED:** {link}{note}")
    if normalized_status in config.done_statuses:
        return assignee, ReportItem(2, f"🟢 **DONE:** {link}")

    update = (
        ai_update.update
        if ai_update
        else recent_comment or "No update comment logged in the last 24 hours."
    )
    return assignee, ReportItem(
        3,
        f"🟡 **IN PROGRESS ({status}):** {link}\n"
        f"  > *Update:* {update}",
    )


def parse_and_format(
    issues: Sequence[Mapping[str, Any]],
    config: Config,
    ai_updates: Mapping[str, AIUpdate] | None = None,
) -> str:
    """Group Jira issues by assignee and format Mattermost Markdown."""
    updates: dict[str, list[ReportItem]] = {}
    enriched_updates = ai_updates or {}
    for issue in issues:
        if not _is_started(issue):
            continue
        result = _issue_item(issue, config, enriched_updates)
        if result is None:
            continue
        assignee, item = result
        updates.setdefault(assignee, []).append(item)

    lines = [
        f"## 🌇 EOD Progress Report — **{config.team_region} Team**",
        "_Format A · grouped by assignee_",
        "---",
    ]
    if not updates:
        lines.append("_No ticket activity or updates logged today._")
        return "\n".join(lines)

    for assignee in sorted(updates, key=str.casefold):
        lines.append(f"### 👤 {assignee}")
        for item in sorted(updates[assignee], key=lambda entry: (entry.rank, entry.text)):
            lines.append(f"* {item.text}")
        lines.append("")
    return "\n".join(lines).rstrip()


def parse_and_format_by_status(
    issues: Sequence[Mapping[str, Any]],
    config: Config,
    ai_updates: Mapping[str, AIUpdate] | None = None,
) -> str:
    """Group recently active Jira issues by workflow state."""
    enriched_updates = ai_updates or {}
    groups: dict[str, list[tuple[bool, str]]] = {
        "Done": [],
        "Blocked": [],
        "In Deployment": [],
        "Rejected": [],
        "In Review": [],
        "In Progress": [],
    }
    icons = {
        "Done": "🟢",
        "Blocked": "🔴",
        "In Deployment": "🚀",
        "Rejected": "⛔",
        "In Review": "🔵",
        "In Progress": "🟡",
    }

    for issue in issues:
        if not _is_started(issue):
            continue
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            raise EODReportError("Jira issue is missing its fields object")
        comments = fields.get("comment")
        recent_comment = get_recent_comment(
            comments if isinstance(comments, dict) else None
        )
        status_change = issue.get("_eod_status_change")
        if not recent_comment and not isinstance(status_change, str):
            continue

        key = str(issue.get("key", "Unknown"))
        summary = str(fields.get("summary") or "No summary")
        assignee_data = fields.get("assignee")
        assignee = (
            str(assignee_data.get("displayName") or "Unassigned")
            if isinstance(assignee_data, dict)
            else "Unassigned"
        )
        status_data = fields.get("status")
        status = (
            str(status_data.get("name") or "Unknown")
            if isinstance(status_data, dict)
            else "Unknown"
        )
        normalized_status = status.strip().casefold()
        if normalized_status in config.done_statuses:
            group = "Done"
        elif normalized_status in config.blocked_statuses:
            group = "Blocked"
        elif normalized_status in config.deploy_statuses:
            group = "In Deployment"
        elif normalized_status == "rejected":
            group = "Rejected"
        elif normalized_status in config.review_statuses:
            group = "In Review"
        else:
            group = "In Progress"
        issue_type_data = fields.get("issuetype")
        issue_type = (
            str(issue_type_data.get("name") or "").strip().casefold()
            if isinstance(issue_type_data, dict)
            else ""
        )
        if group == "In Progress" and issue_type == "epic":
            continue

        ticket_url = f"{config.jira_base_url}/browse/{quote(key, safe='-')}"
        link = f"[{key}]({ticket_url}) {summary}"
        ai_update = enriched_updates.get(key)
        details: list[str] = []
        if group == "Rejected":
            latest_comment = get_latest_comment(
                comments if isinstance(comments, dict) else None
            )
            explicit_comment_reason = (
                latest_comment
                if latest_comment
                and re.search(
                    r"\b(?:duplicate|out of scope|reject(?:ed|ion)?|"
                    r"should be done|supersed(?:e|ed|ing))\b",
                    latest_comment,
                    re.IGNORECASE,
                )
                else None
            )
            rejection_reason = (
                ai_update.update
                if ai_update
                else explicit_comment_reason or "No rejection reason recorded."
            )
            if re.search(
                r"\b(?:because|by|due|for|from|to|using|with)\W*$",
                rejection_reason,
                re.IGNORECASE,
            ):
                rejection_reason = "No rejection reason recorded."
            details.append(f"  > *Rejection reason:* {rejection_reason}")
        elif recent_comment:
            activity = ai_update.update if ai_update else recent_comment
            details.append(f"  > *Update:* {activity}")
        if group == "Blocked":
            blocked_duration = issue.get("_eod_blocked_duration")
            duration = (
                blocked_duration
                if isinstance(blocked_duration, str)
                else "Unavailable"
            )
            details.append(f"  > ⏱️ *Blocked for:* {duration}")
        if group == "Blocked" and recent_comment:
            blocker = (
                ai_update.blocker_reason
                if ai_update and ai_update.blocker_reason
                else recent_comment or "No explicit blocker reason was found."
            )
            details.append(f"  > ⚠️ *Blocker:* {blocker}")
        elif group == "In Progress" and recent_comment:
            details.append(f"  > *Current status:* {status}")
        row = f"* {link} — *Assignee: {assignee}*"
        if details:
            row += "\n" + "\n".join(details)
        groups[group].append((bool(recent_comment), row))

    lines = [
        f"## 🌇 EOD Progress Report — **{config.team_region} Team**",
        "_Format B · grouped by status · tickets with a comment or status change "
        "in the last 24 hours_",
        "---",
    ]
    if not any(groups.values()):
        lines.append("_No ticket comments or status changes logged today._")
        return "\n".join(lines)

    for group in (
        "Done",
        "Blocked",
        "In Deployment",
        "Rejected",
        "In Review",
        "In Progress",
    ):
        if not groups[group]:
            continue
        lines.append(f"### {icons[group]} {group}")
        ordered = sorted(
            groups[group],
            key=lambda entry: (not entry[0], entry[1].casefold()),
        )
        lines.extend(row for _, row in ordered)
        lines.append("")
    return "\n".join(lines).rstrip()


def send_to_mattermost(
    markdown_text: str,
    config: Config,
    session: requests.Session | None = None,
) -> None:
    """Post a formatted report to a Mattermost incoming webhook."""
    client = session or requests.Session()
    try:
        response = client.post(
            config.mattermost_webhook_url,
            json={
                "text": markdown_text,
                "username": "Jira EOD Reporter",
                "icon_emoji": "clipboard",
            },
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise EODReportError(
            "Failed to post report to Mattermost: "
            f"{request_error_summary(exc)}"
        ) from exc


def main() -> int:
    try:
        config = Config.from_env()
        print(f"Processing EOD report for region: {config.team_region}...")
        issues = fetch_jira_tickets(config)
        try:
            ai_updates = generate_ai_updates(issues, config)
        except EODReportError as exc:
            print(
                f"Warning: AI enrichment unavailable; using raw Jira updates: {exc}",
                file=sys.stderr,
            )
            ai_updates = {}
        report = parse_and_format(issues, config, ai_updates)
        send_to_mattermost(report, config)
    except EODReportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("EOD report posted successfully to Mattermost.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
