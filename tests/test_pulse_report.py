import json
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime, timezone
from io import StringIO
from unittest.mock import Mock
from unittest.mock import patch

from eod_report import AIUpdate, Config, EODReportError
from format_c_report import EpicGroup, EpicProgress
from pulse_report import (
    PulseConfig,
    PulseRegionReport,
    build_pulse_context,
    format_pulse_report,
    generate_highlights_with_fallback,
    generate_region_highlights,
    is_pulse_due,
    raw_region_highlights,
    send_pulse_to_mattermost,
    select_sprint,
    select_team_sprint,
)


ENV = {
    "JIRA_DOMAIN": "example.atlassian.net",
    "JIRA_EMAIL": "person@example.com",
    "JIRA_API_TOKEN": "token",
    "MATTERMOST_WEBHOOK_URL": "https://mattermost.example/hooks/test",
    "OPENROUTER_API_KEY": "sk-or-test",
}


def config(**overrides):
    return PulseConfig.from_env(ENV | overrides)


def issue(key, status, category, description="Context", comments=None):
    return {
        "key": key,
        "fields": {
            "summary": f"Summary {key}",
            "description": description,
            "assignee": {"displayName": "Ada"},
            "status": {
                "name": status,
                "statusCategory": {"key": category},
            },
            "comment": {"comments": comments or []},
        },
    }


class ScheduleTests(unittest.TestCase):
    def test_runs_at_8pm_during_daylight_saving(self):
        now = datetime(2026, 8, 15, 0, 40, tzinfo=timezone.utc)

        self.assertTrue(
            is_pulse_due(now, "23 0 * * 5,6", date(2026, 8, 14))
        )
        self.assertFalse(
            is_pulse_due(now, "23 1 * * 5,6", date(2026, 8, 14))
        )

    def test_runs_at_8pm_during_standard_time_on_alternate_friday(self):
        now = datetime(2026, 11, 7, 1, 40, tzinfo=timezone.utc)

        self.assertTrue(
            is_pulse_due(now, "23 1 * * 5,6", date(2026, 8, 14))
        )
        self.assertFalse(
            is_pulse_due(now, "23 0 * * 5,6", date(2026, 8, 14))
        )

    def test_skips_non_alternate_friday(self):
        now = datetime(2026, 8, 22, 0, 40, tzinfo=timezone.utc)

        self.assertFalse(
            is_pulse_due(now, "23 0 * * 5,6", date(2026, 8, 14))
        )

    def test_manual_run_is_always_due(self):
        self.assertTrue(
            is_pulse_due(
                datetime(2026, 8, 20, tzinfo=timezone.utc),
                None,
                date(2026, 8, 14),
                force=True,
            )
        )


class SprintTests(unittest.TestCase):
    def test_selects_sprint_ending_closest_to_report(self):
        sprints = [
            {"id": 1, "endDate": "2026-08-14T23:00:00Z"},
            {"id": 2, "endDate": "2026-08-28T23:00:00Z"},
        ]

        selected = select_sprint(
            sprints, datetime(2026, 8, 15, 0, tzinfo=timezone.utc)
        )

        self.assertEqual(selected["id"], 1)

    def test_classifies_done_blocked_and_carryover(self):
        context = build_pulse_context(
            [
                issue("ENG-1", "Done", "done"),
                issue("ENG-2", "Blocked", "indeterminate"),
                issue("ENG-3", "In Progress", "indeterminate"),
                issue("ENG-4", "To Do", "new"),
            ],
            config(),
        )

        self.assertEqual(
            [item["category"] for item in context],
            ["done", "blocked", "carryover", "carryover"],
        )

    def test_selects_pulse_sprint_for_the_current_team(self):
        pulse_config = config()
        team = pulse_config.teams[0]
        sprints = [
            {
                "id": 1,
                "name": "Mobile - Sprint 17",
                "endDate": "2026-08-28T23:00:00Z",
            },
            {
                "id": 2,
                "name": "Platform - Sprint 17",
                "endDate": "2026-08-28T23:00:00Z",
            },
        ]

        selected = select_team_sprint(
            team,
            101,
            sprints,
            datetime(2026, 8, 29, tzinfo=timezone.utc),
            pulse_config.teams,
        )

        self.assertEqual(selected["id"], 2)

    def test_rejects_single_sprint_named_for_another_team(self):
        pulse_config = config()

        with self.assertRaisesRegex(EODReportError, "another team"):
            select_team_sprint(
                pulse_config.teams[0],
                101,
                [
                    {
                        "id": 1,
                        "name": "Mobile - Sprint 17",
                        "endDate": "2026-08-28T23:00:00Z",
                    }
                ],
                datetime(2026, 8, 29, tzinfo=timezone.utc),
                pulse_config.teams,
            )


class HighlightTests(unittest.TestCase):
    def test_raw_fallback_uses_latest_comment_then_summary(self):
        context = [
            {
                "key": "ENG-1",
                "category": "blocked",
                "summary": "Firewall access",
                "latest_comments": ["Waiting for security approval."],
            },
            {
                "key": "ENG-2",
                "category": "carryover",
                "summary": "Complete rollout",
                "latest_comments": [],
            },
        ]

        highlights = raw_region_highlights(context)

        self.assertEqual(
            highlights["blocked"][0]["text"],
            "Waiting for security approval.",
        )
        self.assertEqual(
            highlights["carryover"][0]["text"], "Complete rollout"
        )

    def test_raw_fallback_limits_entries_and_text_length(self):
        context = [
            {
                "key": f"ENG-{number}",
                "category": "carryover",
                "summary": "Deploy",
                "latest_comments": [
                    "{{staging-service*-dev}} enabled"
                ],
            }
            for number in range(1, 6)
        ]

        highlights = raw_region_highlights(context)

        self.assertEqual(len(highlights["carryover"]), 3)
        self.assertLessEqual(len(highlights["carryover"][0]["text"]), 300)

    def test_pulse_ai_failure_uses_raw_highlights(self):
        context = [
            {
                "key": "ENG-1",
                "category": "done",
                "summary": "Completed migration",
                "latest_comments": [],
            }
        ]
        stderr = StringIO()
        with patch(
            "pulse_report.generate_region_highlights",
            side_effect=EODReportError("rate limited"),
        ), redirect_stderr(stderr):
            highlights = generate_highlights_with_fallback(
                "Core", context, config(), Mock()
            )

        self.assertEqual(
            highlights["done"][0]["text"], "Completed migration"
        )
        self.assertIn("using raw Jira updates", stderr.getvalue())

    def test_empty_region_skips_openrouter(self):
        session = Mock()

        highlights = generate_region_highlights(
            "EMEA", [], config(), session
        )

        self.assertEqual(
            highlights, {"done": [], "blocked": [], "carryover": []}
        )
        session.post.assert_not_called()

    def test_validates_and_formats_highlights(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "done": [
                                    {
                                        "key": "ENG-1",
                                        "text": "Completed regional failover automation.",
                                    }
                                ],
                                "blocked": [],
                                "carryover": [],
                            }
                        )
                    }
                }
            ]
        }
        session = Mock()
        session.post.return_value = response
        context = build_pulse_context(
            [issue("ENG-1", "Done", "done")], config()
        )

        highlights = generate_region_highlights(
            "APAC", context, config(), session
        )

        self.assertEqual(highlights["done"][0]["key"], "ENG-1")
        self.assertEqual(
            session.post.call_args.kwargs["json"]["reasoning"],
            {"effort": "minimal", "exclude": True},
        )
        self.assertEqual(
            session.post.call_args.kwargs["headers"]["Authorization"],
            "Bearer sk-or-test",
        )
        self.assertIn(
            "only meaningful outcomes",
            session.post.call_args.kwargs["json"]["messages"][1]["content"],
        )
        self.assertNotIn(
            "provider", session.post.call_args.kwargs["json"]
        )

    def test_report_uses_required_title_and_region_order(self):
        eod_config = Config.from_env(ENV | {"TEAM_NAME": "Platform"})
        group = EpicGroup(
            "ENG-100",
            "Improve reliability",
            EpicProgress(40, "2 / 5 story points done"),
            (
                issue(
                    "ENG-1",
                    "Done",
                    "done",
                    comments=[
                        {
                            "created": datetime.now(timezone.utc).isoformat(),
                            "body": "Completed migration.",
                        }
                    ],
                ),
                issue("ENG-2", "In Progress", "indeterminate"),
            ),
        )
        reports = {
            "platform": PulseRegionReport(
                (group,),
                ("Platform - Sprint 17",),
                eod_config,
                {
                    "ENG-1": AIUpdate("Completed migration.", ""),
                    "ENG-2": AIUpdate("Unsupported synthetic update.", ""),
                },
            )
        }

        report = format_pulse_report(reports, config())

        self.assertTrue(report.startswith("Engineering Sprint Pulse"))
        self.assertNotIn("###", report)
        self.assertLess(report.index("**Platform"), report.index("**Services"))
        self.assertLess(report.index("**Services"), report.index("**Mobile"))
        self.assertIn("**[ENG-100]", report)
        self.assertIn("40% complete", report)
        self.assertIn("**Done** — [ENG-1]", report)
        self.assertIn("> *Completed migration.*", report)
        self.assertIn("**In Progress** — [ENG-2]", report)
        self.assertEqual(report.count("> *"), 1)

    def test_posts_report_as_one_message(self):
        response = Mock()
        response.raise_for_status.return_value = None
        session = Mock()
        session.post.return_value = response

        send_pulse_to_mattermost("report", config(), session)

        session.post.assert_called_once()
        self.assertEqual(
            session.post.call_args.kwargs["json"]["text"], "report"
        )

    def test_rejects_report_too_large_for_one_message(self):
        with self.assertRaisesRegex(EODReportError, "single-post"):
            send_pulse_to_mattermost("x" * 14_001, config(), Mock())


if __name__ == "__main__":
    unittest.main()
