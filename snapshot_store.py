"""Persist per-run delivery metric snapshots so trends survive across runs.

Jira changelog scraping is expensive and Jira itself keeps no history of these
derived metrics, so each run writes a small JSON snapshot. Trend charts read
the accumulated snapshots instead of re-reading Jira.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 4
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3, SCHEMA_VERSION})
DEFAULT_SNAPSHOT_DIR = "metrics"
_SAFE_SEGMENT = re.compile(r"[^a-z0-9_-]+")


class SnapshotError(RuntimeError):
    """Raised when a snapshot cannot be written or read."""


@dataclass(frozen=True)
class IssueMetric:
    """Per-issue derived metrics captured at snapshot time."""

    key: str
    summary: str
    status: str
    assignee: str
    is_done: bool
    is_blocked: bool
    age_in_status_days: float | None = None
    blocked_days: float | None = None
    cycle_time_days: float | None = None
    lead_time_days: float | None = None
    flow_efficiency: float | None = None
    carry_over_sprints: int = 0
    fix_versions: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    in_lookback: bool = True
    is_started: bool = True


@dataclass(frozen=True)
class TeamSnapshot:
    """One team's metrics for one point in time."""

    team_id: str
    team_name: str
    captured_at: str
    sprint: str | None = None
    issues: tuple[IssueMetric, ...] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSION

    @property
    def captured_date(self) -> str:
        return self.captured_at[:10]


def _safe_segment(value: str) -> str:
    """Reduce a value to a filename-safe slug so it cannot escape the directory."""
    slug = _SAFE_SEGMENT.sub("-", value.strip().casefold()).strip("-")
    return slug or "unknown"


def snapshot_path(
    directory: str | os.PathLike[str], snapshot: TeamSnapshot
) -> Path:
    """Where a snapshot is stored: one file per team per capture date."""
    name = f"{_safe_segment(snapshot.captured_date)}-{_safe_segment(snapshot.team_id)}.json"
    return Path(directory) / name


def write_snapshot(
    directory: str | os.PathLike[str], snapshot: TeamSnapshot
) -> Path:
    """Write one snapshot, replacing any earlier capture for the same day."""
    target = snapshot_path(directory, snapshot)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(snapshot)
        payload["issues"] = [asdict(issue) for issue in snapshot.issues]
        target.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise SnapshotError(f"Failed to write snapshot: {exc.strerror}") from exc
    return target


def _names(value: Any) -> tuple[str, ...]:
    """Clean a stored list of names, dropping anything unusable."""
    if not isinstance(value, list):
        return ()
    return tuple(
        name.strip()
        for name in value
        if isinstance(name, str) and name.strip()
    )


def _issue_from_payload(payload: Mapping[str, Any]) -> IssueMetric:
    known = {
        key: payload.get(key)
        for key in IssueMetric.__dataclass_fields__
        if key in payload
    }
    return IssueMetric(
        key=str(known.get("key") or "Unknown"),
        summary=str(known.get("summary") or ""),
        status=str(known.get("status") or "Unknown"),
        assignee=str(known.get("assignee") or "Unassigned"),
        is_done=bool(known.get("is_done")),
        is_blocked=bool(known.get("is_blocked")),
        age_in_status_days=known.get("age_in_status_days"),
        blocked_days=known.get("blocked_days"),
        cycle_time_days=known.get("cycle_time_days"),
        lead_time_days=known.get("lead_time_days"),
        flow_efficiency=known.get("flow_efficiency"),
        carry_over_sprints=int(known.get("carry_over_sprints") or 0),
        fix_versions=_names(
            known.get("fix_versions") or payload.get("releases")
        ),
        labels=_names(known.get("labels")),
        in_lookback=bool(known.get("in_lookback", True)),
        is_started=bool(
            known.get(
                "is_started",
                str(known.get("status") or "").strip().casefold()
                not in {"to do", "open", "backlog"},
            )
        ),
    )


def _snapshot_from_payload(payload: Mapping[str, Any], source: Path) -> TeamSnapshot:
    version = payload.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SnapshotError(
            f"{source.name} uses unsupported snapshot schema {version!r}"
        )
    issues = payload.get("issues", [])
    if not isinstance(issues, list):
        raise SnapshotError(f"{source.name} has an invalid issues list")
    return TeamSnapshot(
        team_id=str(payload.get("team_id") or "unknown"),
        team_name=str(payload.get("team_name") or "Unknown"),
        captured_at=str(payload.get("captured_at") or ""),
        sprint=payload.get("sprint"),
        issues=tuple(
            _issue_from_payload(issue)
            for issue in issues
            if isinstance(issue, dict)
        ),
    )


def load_snapshots(
    directory: str | os.PathLike[str],
    team_ids: Iterable[str] | None = None,
    since: datetime | None = None,
) -> tuple[TeamSnapshot, ...]:
    """Load stored snapshots, oldest first, optionally filtered by team and date."""
    root = Path(directory)
    if not root.is_dir():
        return ()
    wanted = {_safe_segment(team) for team in team_ids} if team_ids else None
    cutoff = since.astimezone(timezone.utc).date().isoformat() if since else None

    snapshots: list[TeamSnapshot] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SnapshotError(f"Failed to read {path.name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise SnapshotError(f"{path.name} is not a snapshot object")
        snapshot = _snapshot_from_payload(payload, path)
        if wanted and _safe_segment(snapshot.team_id) not in wanted:
            continue
        if cutoff and snapshot.captured_date < cutoff:
            continue
        snapshots.append(snapshot)
    return tuple(sorted(snapshots, key=lambda item: (item.captured_at, item.team_id)))


def latest_by_team(
    snapshots: Sequence[TeamSnapshot],
) -> dict[str, TeamSnapshot]:
    """Most recent snapshot for each team."""
    newest: dict[str, TeamSnapshot] = {}
    for snapshot in sorted(snapshots, key=lambda item: item.captured_at):
        newest[snapshot.team_id] = snapshot
    return newest


def utc_timestamp(now: datetime | None = None) -> str:
    """Capture timestamp in a stable, sortable UTC form."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()
