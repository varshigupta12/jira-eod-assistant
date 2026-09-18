import unittest
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import requests
from unittest.mock import Mock

from eod_report import EODReportError
from release_blocker_report import (
    ReleaseBlocker,
    _blocked_duration,
    build_release_blocker_jql,
    format_release_blocker_report,
    post_release_blocker_report,
    split_release_blocker_report,
)
from report_config import (
    AISettings,
    PulseSettings,
    ReleaseBlockerSettings,
    ReportSettings,
    Team,
)


def team(team_id, name, jira_filter):
    return Team(
        id=team_id,
        name=name,
        projects=("ENG",),
        filters=(jira_filter,),
        board_ids=(1,),
        team_field=None,
        team_value=None,
        daily_schedule=None,
        include_in_pulse=True,
    )


def settings():
    return ReportSettings(
        teams=(
            team("platform", "Platform", "Platform board"),
            team("services", "Services", "Services board"),
            team("mobile", "Mobile", "Mobile board"),
        ),
        ai=AISettings(True, "test-model", 2048),
        pulse=PulseSettings(
            True,
            "Engineering Pulse",
            ZoneInfo("UTC"),
            time(20),
            4,
            14,
            date(2026, 1, 9),
        ),
        release_blockers=ReleaseBlockerSettings(True, "2026.1"),
        blocked_statuses=frozenset({"blocked", "impediment"}),
        deploy_statuses=frozenset(),
        done_statuses=frozenset({"done"}),
        review_statuses=frozenset(),
    )


class ReleaseBlockerReportTests(unittest.TestCase):
    def test_jql_matches_label_or_fix_version(self):
        query = build_release_blocker_jql(
            settings().teams[0],
            "2026.1",
            frozenset({"blocked", "impediment"}),
        )

        self.assertIn('project = "ENG"', query)
        self.assertIn('filter = "Platform board"', query)
        self.assertIn('labels = "2026.1"', query)
        self.assertIn('fixVersion = "2026.1"', query)
        self.assertIn('status in ("blocked", "impediment")', query)
        self.assertNotIn("updated >=", query)

    def test_calculates_duration_from_issue_creation_fallback(self):
        now = datetime(2026, 4, 5, 12, tzinfo=timezone.utc)
        created = datetime(2026, 4, 2, 9, tzinfo=timezone.utc)

        self.assertEqual(_blocked_duration(created, now), "3 days 3 hours")

    def test_formats_bullets_and_longest_duration_first(self):
        blockers = {
            "platform": (
                ReleaseBlocker(
                    "ENG-1",
                    "Old blocker",
                    "Ada",
                    "84 days",
                    datetime(2026, 1, 1, tzinfo=timezone.utc),
                    "Waiting for approval.",
                ),
                ReleaseBlocker(
                    "ENG-2",
                    "New blocker",
                    "Grace",
                    "5 days",
                    datetime(2026, 3, 20, tzinfo=timezone.utc),
                    "",
                ),
            ),
            "services": (
                ReleaseBlocker(
                    "ENG-3",
                    "Credential blocker",
                    "Linus",
                    "3 days",
                    datetime(2026, 3, 22, tzinfo=timezone.utc),
                    "Waiting for credentials.",
                ),
            ),
        }

        report = format_release_blocker_report(
            blockers, settings(), "https://jira.example.com"
        )

        self.assertTrue(report.startswith("**🚧 Release 2026.1 Blocked Items**"))
        self.assertIn("**👥 Platform Squad**", report)
        self.assertIn("**👥 Services Squad**", report)
        self.assertNotIn("Mobile Squad", report)
        self.assertIn("• [ENG-1]", report)
        self.assertNotIn("🔴", report)
        self.assertLess(report.index("ENG-1"), report.index("ENG-2"))
        self.assertEqual(report.count("\n---\n"), 1)
        self.assertIn("> *Waiting for approval.*", report)

    def test_skips_empty_report(self):
        self.assertIsNone(
            format_release_blocker_report(
                {}, settings(), "https://jira.example.com"
            )
        )

    def test_splits_large_report_between_items(self):
        report = (
            "**🚧 Release 2026.1 Blocked Items**\n\n"
            + "\n\n".join(f"• ENG-{index} " + "x" * 400 for index in range(40))
        )

        posts = split_release_blocker_report(report)

        self.assertGreater(len(posts), 1)
        self.assertTrue(all(len(post) <= 14_000 for post in posts))
        self.assertTrue(posts[1].startswith("**🚧 Release 2026.1 Blocked Items"))

    def test_does_not_expose_webhook_url_in_errors(self):
        response = Mock()
        response.status_code = 500
        error = requests.HTTPError(
            "500 Server Error for url: https://mattermost.example/hooks/private",
            response=response,
        )
        session = Mock()
        session.post.side_effect = error

        with unittest.mock.patch(
            "release_blocker_report.collect_release_blockers",
            return_value={
                "platform": (
                    ReleaseBlocker(
                        "ENG-1",
                        "Blocked",
                        "Ada",
                        "1 day",
                        datetime.now(timezone.utc),
                        "",
                    ),
                )
            },
        ):
            with self.assertRaises(EODReportError) as raised:
                post_release_blocker_report(
                    settings(),
                    "https://jira.example.com",
                    "https://mattermost.example/hooks/private",
                    session,
                )

        self.assertEqual(
            str(raised.exception),
            "Failed to post release blocker report to Mattermost: HTTP 500",
        )
        self.assertNotIn("/hooks/private", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
