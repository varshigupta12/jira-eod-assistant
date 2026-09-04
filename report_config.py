"""Load and validate the public YAML configuration for Jira reports."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ReportConfigError(ValueError):
    """Raised when report-config.yml is missing or invalid."""


@dataclass(frozen=True)
class DailySchedule:
    time: time
    timezone: ZoneInfo
    weekdays: frozenset[int]
    report_format: str


@dataclass(frozen=True)
class Team:
    id: str
    name: str
    projects: tuple[str, ...]
    filters: tuple[str, ...]
    board_ids: tuple[int, ...]
    team_field: str | None
    team_value: str | None
    daily_schedule: DailySchedule | None
    include_in_pulse: bool


@dataclass(frozen=True)
class AISettings:
    enabled: bool
    model: str
    max_tokens: int


@dataclass(frozen=True)
class PulseSettings:
    enabled: bool
    title: str
    timezone: ZoneInfo
    time: time
    weekday: int
    cadence_days: int
    anchor_date: date


@dataclass(frozen=True)
class ReportSettings:
    teams: tuple[Team, ...]
    ai: AISettings
    pulse: PulseSettings
    blocked_statuses: frozenset[str]
    deploy_statuses: frozenset[str]
    done_statuses: frozenset[str]
    review_statuses: frozenset[str]

    def team(self, team_id: str) -> Team:
        normalized = team_id.strip().casefold()
        for team in self.teams:
            if team.id.casefold() == normalized:
                return team
        raise ReportConfigError(f"Unknown team ID: {team_id}")


WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
DAILY_REPORT_FORMATS = frozenset({"epic", "status", "assignee"})


def nominal_schedule_time(now: datetime, schedule: str | None) -> datetime:
    """Recover the intended UTC run time from a single-hour cron expression."""
    if not schedule:
        return now
    parts = schedule.split()
    if len(parts) != 5:
        raise ReportConfigError(f"Invalid workflow schedule: {schedule}")
    try:
        minute = int(parts[0])
        hour = int(parts[1])
    except ValueError as exc:
        raise ReportConfigError(
            "Scheduled workflows must use one numeric UTC hour per cron entry"
        ) from exc
    if not 0 <= minute <= 59 or not 0 <= hour <= 23:
        raise ReportConfigError(f"Invalid workflow schedule: {schedule}")

    utc_now = now.astimezone(timezone.utc)
    nominal = utc_now.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if nominal > utc_now:
        nominal -= timedelta(days=1)
    return nominal


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ReportConfigError(f"{path} must be a mapping")
    return value


def _string(value: Any, path: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReportConfigError(f"{path} must be a non-empty string")
    return value.strip()


def _strings(value: Any, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (_string(value, path),)
    if not isinstance(value, list):
        raise ReportConfigError(f"{path} must be a string or list of strings")
    return tuple(_string(item, f"{path}[]") for item in value)


def _zone(value: Any, path: str) -> ZoneInfo:
    name = _string(value, path)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ReportConfigError(f"{path} is not a valid IANA timezone: {name}") from exc


def _time(value: Any, path: str) -> time:
    text = _string(value, path)
    try:
        parsed = time.fromisoformat(text)
    except ValueError as exc:
        raise ReportConfigError(f"{path} must use HH:MM format") from exc
    if parsed.second or parsed.microsecond:
        raise ReportConfigError(f"{path} must not include seconds")
    return parsed


def _weekday(value: Any, path: str) -> int:
    name = _string(value, path).casefold()
    if name not in WEEKDAYS:
        raise ReportConfigError(
            f"{path} must be one of: {', '.join(WEEKDAYS)}"
        )
    return WEEKDAYS[name]


def _statuses(
    root: Mapping[str, Any], name: str, defaults: tuple[str, ...]
) -> frozenset[str]:
    statuses = _strings(root.get(name), name) or defaults
    return frozenset(value.casefold() for value in statuses)


def load_report_config(path: str | os.PathLike[str] | None = None) -> ReportSettings:
    config_path = Path(path or os.getenv("REPORT_CONFIG", "report-config.yml"))
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReportConfigError(f"Configuration file not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ReportConfigError(f"Invalid YAML in {config_path}: {exc}") from exc
    root = _mapping(raw, "configuration")
    if root.get("version") != 1:
        raise ReportConfigError("version must be 1")
    early_pulse = _mapping(root.get("pulse", {}), "pulse")
    pulse_is_enabled = early_pulse.get("enabled", False)
    if not isinstance(pulse_is_enabled, bool):
        raise ReportConfigError("pulse.enabled must be true or false")

    raw_teams = root.get("teams")
    if not isinstance(raw_teams, list) or not raw_teams:
        raise ReportConfigError("teams must be a non-empty list")
    teams = []
    seen_ids: set[str] = set()
    for index, value in enumerate(raw_teams):
        path_prefix = f"teams[{index}]"
        item = _mapping(value, path_prefix)
        team_id = _string(item.get("id"), f"{path_prefix}.id")
        normalized_id = team_id.casefold()
        if normalized_id in seen_ids:
            raise ReportConfigError(f"Duplicate team ID: {team_id}")
        seen_ids.add(normalized_id)

        board_values = item.get("boards", [])
        if isinstance(board_values, int):
            board_values = [board_values]
        if not isinstance(board_values, list) or any(
            not isinstance(board_id, int) or board_id <= 0
            for board_id in board_values
        ):
            raise ReportConfigError(
                f"{path_prefix}.boards must contain positive integer board IDs"
            )

        daily_value = item.get("daily")
        daily_schedule = None
        if daily_value is not None:
            daily = _mapping(daily_value, f"{path_prefix}.daily")
            weekday_values = daily.get(
                "weekdays",
                ["monday", "tuesday", "wednesday", "thursday", "friday"],
            )
            if not isinstance(weekday_values, list) or not weekday_values:
                raise ReportConfigError(
                    f"{path_prefix}.daily.weekdays must be a non-empty list"
                )
            daily_time = _time(daily.get("time"), f"{path_prefix}.daily.time")
            if daily_time.minute:
                raise ReportConfigError(
                    f"{path_prefix}.daily.time must be on the hour"
                )
            report_format = _string(
                daily.get("format", "epic"), f"{path_prefix}.daily.format"
            ).casefold()
            if report_format not in DAILY_REPORT_FORMATS:
                raise ReportConfigError(
                    f"{path_prefix}.daily.format must be one of: "
                    f"{', '.join(sorted(DAILY_REPORT_FORMATS))}"
                )
            daily_schedule = DailySchedule(
                time=daily_time,
                timezone=_zone(
                    daily.get("timezone"), f"{path_prefix}.daily.timezone"
                ),
                weekdays=frozenset(
                    _weekday(day, f"{path_prefix}.daily.weekdays[]")
                    for day in weekday_values
                ),
                report_format=report_format,
            )

        team_field = _string(
            item.get("team_field"), f"{path_prefix}.team_field", required=False
        )
        team_value = _string(
            item.get("team_value"), f"{path_prefix}.team_value", required=False
        )
        if bool(team_field) != bool(team_value):
            raise ReportConfigError(
                f"{path_prefix}.team_field and team_value must both be set"
            )

        include_in_pulse = item.get("include_in_pulse", True)
        if not isinstance(include_in_pulse, bool):
            raise ReportConfigError(
                f"{path_prefix}.include_in_pulse must be true or false"
            )
        if pulse_is_enabled and include_in_pulse and not board_values:
            raise ReportConfigError(
                f"{path_prefix}.boards is required when include_in_pulse is true"
            )
        projects = _strings(item.get("projects"), f"{path_prefix}.projects")
        filters = _strings(item.get("filters"), f"{path_prefix}.filters")
        if daily_schedule:
            if daily_schedule.report_format == "epic" and not board_values:
                raise ReportConfigError(
                    f"{path_prefix}.boards is required for daily.format epic"
                )
            if daily_schedule.report_format != "epic" and not (
                projects or filters or (team_field and team_value)
            ):
                raise ReportConfigError(
                    f"{path_prefix} needs projects, filters, or a Team-field "
                    f"mapping for daily.format {daily_schedule.report_format}"
                )
        teams.append(
            Team(
                id=team_id,
                name=_string(item.get("name", team_id), f"{path_prefix}.name"),
                projects=projects,
                filters=filters,
                board_ids=tuple(board_values),
                team_field=team_field,
                team_value=team_value,
                daily_schedule=daily_schedule,
                include_in_pulse=include_in_pulse,
            )
        )

    ai_raw = _mapping(root.get("ai", {}), "ai")
    ai_enabled = ai_raw.get("enabled", False)
    if not isinstance(ai_enabled, bool):
        raise ReportConfigError("ai.enabled must be true or false")
    max_tokens = ai_raw.get("max_tokens", 2048)
    if not isinstance(max_tokens, int) or not 256 <= max_tokens <= 8192:
        raise ReportConfigError("ai.max_tokens must be an integer from 256 to 8192")
    ai = AISettings(
        enabled=ai_enabled,
        model=_string(
            ai_raw.get("model", "google/gemini-3.7-flash"), "ai.model"
        ),
        max_tokens=max_tokens,
    )
    if pulse_is_enabled and not ai.enabled:
        raise ReportConfigError(
            "ai.enabled must be true when pulse.enabled is true"
        )

    pulse_raw = early_pulse
    pulse_enabled = pulse_raw.get("enabled", False)
    if not isinstance(pulse_enabled, bool):
        raise ReportConfigError("pulse.enabled must be true or false")
    cadence_days = pulse_raw.get("cadence_days", 14)
    if not isinstance(cadence_days, int) or cadence_days < 1:
        raise ReportConfigError("pulse.cadence_days must be a positive integer")
    try:
        anchor_date = date.fromisoformat(
            _string(
                pulse_raw.get("anchor_date", "2026-01-02"),
                "pulse.anchor_date",
            )
        )
    except ValueError as exc:
        raise ReportConfigError("pulse.anchor_date must use YYYY-MM-DD") from exc
    pulse_time = _time(pulse_raw.get("time", "20:00"), "pulse.time")
    if pulse_time.minute:
        raise ReportConfigError("pulse.time must be on the hour")
    pulse = PulseSettings(
        enabled=pulse_enabled,
        title=_string(
            pulse_raw.get("title", "Sprint progress report"), "pulse.title"
        ),
        timezone=_zone(pulse_raw.get("timezone", "UTC"), "pulse.timezone"),
        time=pulse_time,
        weekday=_weekday(pulse_raw.get("weekday", "friday"), "pulse.weekday"),
        cadence_days=cadence_days,
        anchor_date=anchor_date,
    )

    return ReportSettings(
        teams=tuple(teams),
        ai=ai,
        pulse=pulse,
        blocked_statuses=_statuses(
            root, "blocked_statuses", ("blocked", "impediment")
        ),
        deploy_statuses=_statuses(
            root,
            "deploy_statuses",
            ("to be deployed", "ready for deployment", "ready to deploy"),
        ),
        done_statuses=_statuses(root, "done_statuses", ("done", "closed", "resolved")),
        review_statuses=_statuses(
            root, "review_statuses", ("in review", "code review")
        ),
    )
