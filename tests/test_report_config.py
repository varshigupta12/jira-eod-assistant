import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from daily_runner import (
    generate_updates_with_fallback,
    requested_report_format,
    team_environment,
    teams_due,
)
from eod_report import Config, EODReportError, build_jql
from report_config import (
    ReportConfigError,
    load_report_config,
    nominal_schedule_time,
)


CONFIG = """
version: 1
teams:
  - id: core
    name: Core Platform
    projects: [CORE, OPS]
    filters: [Core board, Operations board]
    boards: [101, 202]
    daily:
      time: "17:00"
      timezone: Europe/London
      weekdays: [monday, friday]
  - id: docs
    name: Documentation
    projects: DOCS
    include_in_pulse: false
ai:
  enabled: true
  model: google/gemini-3.7-flash
  max_tokens: 2048
pulse:
  enabled: true
  title: Engineering Pulse
  timezone: America/New_York
  weekday: friday
  time: "20:00"
  cadence_days: 14
  anchor_date: "2026-08-14"
"""


class ConfigTests(unittest.TestCase):
    def load(self, content=CONFIG):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yml"
            path.write_text(content, encoding="utf-8")
            return load_report_config(path)

    def test_supports_arbitrary_teams_and_multiple_boards(self):
        settings = self.load()

        self.assertEqual([team.id for team in settings.teams], ["core", "docs"])
        self.assertEqual(settings.team("core").projects, ("CORE", "OPS"))
        self.assertEqual(settings.team("core").board_ids, (101, 202))
        self.assertEqual(settings.pulse.title, "Engineering Pulse")

    def test_rejects_duplicate_team_ids(self):
        duplicate = CONFIG.replace(
            "  - id: docs", "  - id: core"
        )

        with self.assertRaisesRegex(ReportConfigError, "Duplicate team ID"):
            self.load(duplicate)

    def test_selects_due_teams_in_their_timezone(self):
        settings = self.load()
        friday = datetime(2026, 8, 14, 16, 0, tzinfo=timezone.utc)

        selected = teams_due(friday, settings.teams)

        self.assertEqual([team.id for team in selected], ["core"])

    def test_manual_run_can_select_one_or_all_daily_teams(self):
        settings = self.load()
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)

        self.assertEqual(
            [team.id for team in teams_due(now, settings.teams, "core")],
            ["core"],
        )
        self.assertEqual(
            [team.id for team in teams_due(now, settings.teams, force_all=True)],
            ["core"],
        )

    def test_daily_report_format_is_always_format_c(self):
        self.assertEqual(requested_report_format(""), "c")
        self.assertEqual(requested_report_format(None), "c")
        self.assertEqual(requested_report_format("c"), "c")

    def test_rejects_unknown_report_format(self):
        with self.assertRaisesRegex(EODReportError, "REPORT_FORMAT"):
            requested_report_format("unknown")

    def test_builds_generic_team_environment(self):
        settings = self.load()

        values = team_environment(
            settings.team("core"),
            {"JIRA_DOMAIN": "example.atlassian.net"},
            settings.ai.enabled,
            settings.ai.model,
            settings.ai.max_tokens,
            settings.blocked_statuses,
            settings.deploy_statuses,
            settings.done_statuses,
            settings.review_statuses,
        )

        self.assertEqual(values["TEAM_NAME"], "Core Platform")
        self.assertEqual(values["JIRA_PROJECTS_JSON"], '["CORE", "OPS"]')
        self.assertEqual(
            values["JIRA_FILTERS_JSON"],
            '["Core board", "Operations board"]',
        )

    def test_filter_only_team_does_not_gain_a_project_clause(self):
        settings = self.load()
        team = settings.team("core")
        filter_only = team.__class__(
            id=team.id,
            name=team.name,
            projects=(),
            filters=team.filters,
            board_ids=team.board_ids,
            team_field=team.team_field,
            team_value=team.team_value,
            daily_schedule=team.daily_schedule,
            include_in_pulse=team.include_in_pulse,
        )
        values = team_environment(
            filter_only,
            {
                "JIRA_DOMAIN": "example.atlassian.net",
                "JIRA_EMAIL": "person@example.com",
                "JIRA_API_TOKEN": "token",
                "MATTERMOST_WEBHOOK_URL": "https://mattermost.example/hook",
            },
            False,
            settings.ai.model,
            settings.ai.max_tokens,
            settings.blocked_statuses,
            settings.deploy_statuses,
            settings.done_statuses,
            settings.review_statuses,
        )

        jql = build_jql(Config.from_env(values))

        self.assertNotIn("project =", jql)
        self.assertIn('filter = "Core board"', jql)

    def test_daily_ai_failure_falls_back_to_raw_updates(self):
        config = Config.from_env(
            {
                "JIRA_DOMAIN": "example.atlassian.net",
                "JIRA_EMAIL": "person@example.com",
                "JIRA_API_TOKEN": "token",
                "MATTERMOST_WEBHOOK_URL": "https://mattermost.example/hook",
                "TEAM_NAME": "Core",
                "JIRA_PROJECT": "CORE",
                "AI_SUMMARIZE": "true",
                "OPENROUTER_API_KEY": "sk-or-test",
            }
        )
        stderr = StringIO()
        with patch(
            "daily_runner.generate_ai_updates",
            side_effect=EODReportError("credits exhausted"),
        ), redirect_stderr(stderr):
            updates = generate_updates_with_fallback([], config, Mock())

        self.assertEqual(updates, {})
        self.assertIn("using raw Jira updates", stderr.getvalue())

    def test_recovers_nominal_time_from_delayed_schedule(self):
        actual = datetime(2026, 8, 20, 1, 46, tzinfo=timezone.utc)

        nominal = nominal_schedule_time(actual, "23 0 * * *")

        self.assertEqual(
            nominal, datetime(2026, 8, 20, 0, 23, tzinfo=timezone.utc)
        )


if __name__ == "__main__":
    unittest.main()
