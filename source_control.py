"""Collect recent activity from GitHub pull requests formally linked in Jira."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlparse

import requests

from eod_report import request_error_summary

GITHUB_API_URL = "https://api.github.com"
MAX_COMMIT_PAGES = 10


class SourceControlError(RuntimeError):
    """Raised when linked source-control activity cannot be collected."""


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _request_json(
    session: requests.Session,
    url: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, Any] | None = None,
    auth: tuple[str, str] | None = None,
) -> Any:
    try:
        response = session.get(
            url,
            headers=dict(headers),
            params=params,
            auth=auth,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise SourceControlError(
            f"Linked activity request failed: {request_error_summary(exc)}"
        ) from exc


def _jira_json(
    session: requests.Session,
    url: str,
    jira_email: str,
    jira_api_token: str,
    params: Mapping[str, Any] | None = None,
) -> Any:
    return _request_json(
        session,
        url,
        headers={"Accept": "application/json"},
        params=params,
        auth=(jira_email, jira_api_token),
    )


def _github_json(
    session: requests.Session,
    path: str,
    github_token: str,
    params: Mapping[str, Any] | None = None,
) -> Any:
    return _request_json(
        session,
        f"{GITHUB_API_URL}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        params=params,
    )


def _development_urls(data: Any) -> set[str]:
    if not isinstance(data, dict) or not isinstance(data.get("detail"), list):
        raise SourceControlError("Jira returned invalid development data")
    urls = set()
    for detail in data["detail"]:
        if not isinstance(detail, dict):
            continue
        pull_requests = detail.get("pullRequests", [])
        if not isinstance(pull_requests, list):
            continue
        for pull_request in pull_requests:
            if (
                isinstance(pull_request, dict)
                and isinstance(pull_request.get("url"), str)
                and pull_request["url"].strip()
            ):
                urls.add(pull_request["url"].strip())
    return urls


def _remote_link_urls(data: Any) -> set[str]:
    if not isinstance(data, list):
        raise SourceControlError("Jira returned invalid remote links")
    urls = set()
    for link in data:
        obj = link.get("object") if isinstance(link, dict) else None
        url = obj.get("url") if isinstance(obj, dict) else None
        if isinstance(url, str) and url.strip():
            urls.add(url.strip())
    return urls


def _pull_request_coordinates(
    url: str, organization: str
) -> tuple[str, str, int] | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.casefold() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 4 or parts[2] != "pull":
        return None
    owner, repository, _, number = parts
    if owner.casefold() != organization.casefold() or not repository:
        return None
    try:
        pull_number = int(number)
    except ValueError:
        return None
    return (owner, repository, pull_number) if pull_number > 0 else None


def linked_github_pull_requests(
    issue: Mapping[str, Any],
    jira_base_url: str,
    jira_email: str,
    jira_api_token: str,
    organization: str,
    session: requests.Session | None = None,
) -> set[tuple[str, str, int]]:
    """Return configured-organization PRs linked through Jira development data."""
    client = session or requests.Session()
    urls: set[str] = set()
    issue_id = issue.get("id")
    if issue_id is not None and str(issue_id).strip():
        data = _jira_json(
            client,
            f"{jira_base_url}/rest/dev-status/1.0/issue/detail",
            jira_email,
            jira_api_token,
            {
                "issueId": str(issue_id),
                "applicationType": "GitHub",
                "dataType": "pullrequest",
            },
        )
        urls = _development_urls(data)

    issue_key = str(issue.get("key") or "").strip()
    if not urls and issue_key:
        data = _jira_json(
            client,
            f"{jira_base_url}/rest/api/3/issue/"
            f"{quote(issue_key, safe='-')}/remotelink",
            jira_email,
            jira_api_token,
        )
        urls = _remote_link_urls(data)

    return {
        coordinates
        for url in urls
        if (coordinates := _pull_request_coordinates(url, organization))
    }


def _pull_request_activity(
    owner: str,
    repository: str,
    number: int,
    github_token: str,
    session: requests.Session,
    now: datetime,
) -> list[dict[str, str]]:
    base_path = f"/repos/{quote(owner)}/{quote(repository)}/pulls/{number}"
    pull_request = _github_json(session, base_path, github_token)
    if not isinstance(pull_request, dict):
        raise SourceControlError(
            f"GitHub returned invalid pull request data for "
            f"{owner}/{repository}#{number}"
        )

    commits: list[Mapping[str, Any]] = []
    for page_number in range(1, MAX_COMMIT_PAGES + 1):
        page = _github_json(
            session,
            f"{base_path}/commits",
            github_token,
            {"per_page": 100, "page": page_number},
        )
        if not isinstance(page, list):
            raise SourceControlError(
                f"GitHub returned invalid commits for "
                f"{owner}/{repository}#{number}"
            )
        valid_page = [item for item in page if isinstance(item, dict)]
        commits.extend(valid_page)
        if len(valid_page) < 100:
            break
    else:
        raise SourceControlError(
            f"Pull request {owner}/{repository}#{number} has more than 1,000 commits"
        )

    cutoff = now - timedelta(hours=24)
    repository_name = f"{owner}/{repository}"
    activity: list[dict[str, str]] = []
    updated = _timestamp(pull_request.get("updated_at"))
    merged = _timestamp(pull_request.get("merged_at"))
    pull_timestamp = max(
        (value for value in (updated, merged) if value is not None),
        default=None,
    )
    if pull_timestamp is not None and cutoff <= pull_timestamp <= now:
        state = "merged" if merged is not None else str(
            pull_request.get("state") or "open"
        )
        activity.append(
            {
                "source": "pull_request",
                "timestamp": pull_timestamp.isoformat(),
                "text": f"{str(pull_request.get('title') or f'Pull request {number}').strip()} ({state})",
                "url": str(
                    pull_request.get("html_url")
                    or f"https://github.com/{owner}/{repository}/pull/{number}"
                ),
                "repository": repository_name,
                "reference": f"#{number}",
            }
        )

    for commit in commits:
        details = commit.get("commit")
        if not isinstance(details, dict):
            continue
        committer = details.get("committer")
        author = details.get("author")
        committed = _timestamp(
            committer.get("date") if isinstance(committer, dict) else None
        ) or _timestamp(author.get("date") if isinstance(author, dict) else None)
        if committed is None or committed < cutoff or committed > now:
            continue
        sha = str(commit.get("sha") or "").strip()
        short_sha = sha[:7] or "unknown"
        message = str(details.get("message") or "").splitlines()
        activity.append(
            {
                "source": "commit",
                "timestamp": committed.isoformat(),
                "text": (
                    message[0].strip()
                    if message and message[0].strip()
                    else f"Commit {short_sha}"
                ),
                "url": str(
                    commit.get("html_url")
                    or f"https://github.com/{owner}/{repository}/commit/{sha}"
                ),
                "repository": repository_name,
                "reference": f"@{short_sha}",
            }
        )
    return activity


def collect_linked_github_activity(
    issues: Sequence[Mapping[str, Any]],
    jira_base_url: str,
    jira_email: str,
    jira_api_token: str,
    organization: str,
    github_token: str,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Collect recent activity only from GitHub PRs formally linked in Jira."""
    client = session or requests.Session()
    current = _utc(now)
    fetched_prs: dict[tuple[str, str, int], list[dict[str, str]]] = {}
    result: dict[str, list[dict[str, str]]] = {}
    for issue in issues:
        issue_key = str(issue.get("key") or "").strip().upper()
        if not issue_key:
            continue
        entries = []
        coordinates = linked_github_pull_requests(
            issue,
            jira_base_url,
            jira_email,
            jira_api_token,
            organization,
            client,
        )
        for owner, repository, number in sorted(
            coordinates, key=lambda item: (item[0].casefold(), item[1].casefold(), item[2])
        ):
            cache_key = (owner.casefold(), repository.casefold(), number)
            if cache_key not in fetched_prs:
                fetched_prs[cache_key] = _pull_request_activity(
                    owner,
                    repository,
                    number,
                    github_token,
                    client,
                    current,
                )
            entries.extend(fetched_prs[cache_key])
        deduplicated = {
            entry["url"]: entry
            for entry in entries
            if isinstance(entry.get("url"), str) and entry["url"]
        }
        result[issue_key] = sorted(
            deduplicated.values(), key=lambda entry: entry["timestamp"]
        )
    return result


def annotate_github_activity(
    issues: Sequence[Mapping[str, Any]],
    jira_base_url: str,
    jira_email: str,
    jira_api_token: str,
    organization: str,
    github_token: str,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return shallow issue copies annotated with recent linked GitHub activity."""
    activity = collect_linked_github_activity(
        issues,
        jira_base_url,
        jira_email,
        jira_api_token,
        organization,
        github_token,
        session,
        now,
    )
    annotated = []
    for issue in issues:
        copy = dict(issue)
        copy["_eod_scm_activity"] = activity.get(
            str(issue.get("key") or "").strip().upper(), []
        )
        annotated.append(copy)
    return annotated
