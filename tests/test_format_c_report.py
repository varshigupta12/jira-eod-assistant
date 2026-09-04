import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from eod_report import AIUpdate, Config
from format_c_report import (
    EpicGroup,
    EpicProgress,
    _progress,
    _select_active_sprints,
    format_format_c,
    group_format_c_issues,
)
from pulse_report import PulseConfig


ENV = {
    "JIRA_DOMAIN": "example.atlassian.net",
    "JIRA_EMAIL": "person@example.com",
    "JIRA_API_TOKEN": "token",
    "MATTERMOST_WEBHOOK_URL": "https://mattermost.example/hook",
    "TEAM_NAME": "APAC",
    "JIRA_PROJECT": "ENG",
    "REVIEW_STATUSES": "In Review,Code Review",
}


def issue(
    key,
    status,
    *,
    category="indeterminate",
    assignee="Ada",
    comment=None,
    points=None,
    logged=None,
    estimated=None,
):
    fields = {
        "summary": f"Summary for {key}",
        "assignee": {"displayName": assignee} if assignee else None,
        "status": {
            "name": status,
            "statusCategory": {"key": category},
        },
        "comment": {"comments": []},
        "timespent": logged,
        "timeoriginalestimate": estimated,
        "customfield_points": points,
    }
    if comment:
        fields["comment"]["comments"].append(
            {
                "created": datetime.now(timezone.utc).isoformat(),
                "body": comment,
            }
        )
    return {"key": key, "fields": fields}


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.config = Config.from_env(ENV)

    def test_prefers_logged_time_over_story_points(self):
        issues = [
            issue(
                "ENG-1",
                "In Progress",
                points=5,
                logged=5 * 3600,
                estimated=10 * 3600,
            )
        ]

        progress = _progress(issues, self.config, ("customfield_points",))

        self.assertEqual(progress, EpicProgress(50, "5h logged / 10h estimated"))

    def test_uses_story_points_then_ticket_count(self):
        with_points = [
            issue("ENG-1", "Done", category="done", points=3),
            issue("ENG-2", "In Progress", points=2),
        ]
        without_points = [
            issue("ENG-1", "Done", category="done"),
            issue("ENG-2", "In Progress"),
        ]

        self.assertEqual(
            _progress(with_points, self.config, ("customfield_points",)),
            EpicProgress(60, "3 / 5 story points done"),
        )
        self.assertEqual(
            _progress(without_points, self.config, ("customfield_points",)),
            EpicProgress(50, "1 / 2 tickets done"),
        )

    def test_selects_only_active_sprint_matching_team_name(self):
        team = SimpleNamespace(id="emea", name="EMEA")
        sprints = [
            {"id": 1, "name": "Platform APAC - Sprint 17", "state": "active"},
            {"id": 2, "name": "Platform EMEA - Sprint 17", "state": "active"},
        ]

        selected = _select_active_sprints(team, 202, sprints)

        self.assertEqual([sprint["id"] for sprint in selected], [2])


class FormatTests(unittest.TestCase):
    def test_pulse_visible_keys_keep_selected_to_do_tickets(self):
        eod_config = Config.from_env(ENV)
        pulse_config = PulseConfig.from_env(ENV)
        selected = issue("ENG-1", "To Do", category="new")

        groups = group_format_c_issues(
            [selected],
            eod_config,
            pulse_config,
            (),
            (),
            visible_keys={"ENG-1"},
        )

        self.assertEqual([ticket["key"] for ticket in groups[0].issues], ["ENG-1"])

    def test_groups_by_epic_and_omits_missing_update_text(self):
        config = Config.from_env(ENV)
        no_comment = issue("ENG-1", "In Progress")
        blocked = issue("ENG-2", "Blocked", comment="Waiting for access")
        blocked["_eod_blocked_duration"] = "2 days 3 hours"
        deployment = issue(
            "ENG-3", "To Be Deployed", comment="Release is queued"
        )
        done = issue("ENG-4", "Done", category="done", comment="Released")
        group = EpicGroup(
            "ENG-100",
            "Improve runner reliability",
            EpicProgress(40, "2 / 5 story points done"),
            (done, no_comment, deployment, blocked),
        )

        report = format_format_c(
            (group,),
            ("Sprint 42",),
            config,
            {"ENG-2": AIUpdate("Access approval is pending.", "")},
        )

        self.assertTrue(report.startswith("**EOD Progress Report - APAC Team**"))
        self.assertNotIn("Format C", report)
        self.assertNotIn("active sprint:", report)
        self.assertIn(
            "[ENG-100](https://example.atlassian.net/browse/ENG-100)",
            report,
        )
        self.assertIn("40% complete", report)
        self.assertNotIn("Progress basis", report)
        self.assertNotIn("story points done", report)
        self.assertIn("**Blocked** —", report)
        self.assertIn("**In Deployment** —", report)
        self.assertNotIn("###", report)
        self.assertNotIn("🔴", report)
        self.assertIn("Blocked for: 2 days 3 hours", report)
        self.assertIn("> *Access approval is pending.*", report)
        self.assertIn("**Done** —", report)
        self.assertLess(report.index("**Blocked** —"), report.index("**In Progress** —"))
        self.assertLess(
            report.index("**In Progress** —"), report.index("**In Deployment** —")
        )
        self.assertLess(report.index("**In Deployment** —"), report.index("**Done** —"))
        self.assertIn("ENG-1", report)
        self.assertNotIn("No update", report)

    def test_escapes_jira_markup_in_ticket_text(self):
        config = Config.from_env(ENV)
        ticket = issue(
            "ENG-1",
            "In Progress",
            comment="{{staging-service*-dev}} enabled",
        )
        group = EpicGroup(
            "ENG-100",
            "Deployment *stability*",
            EpicProgress(20, "1 / 5 tickets done"),
            (ticket,),
        )

        report = format_format_c((group,), ("Sprint 42",), config)

        self.assertIn("Deployment \\*stability\\*", report)
        self.assertIn(
            '> *"staging-service\\*-dev" enabled*',
            report,
        )


if __name__ == "__main__":
    unittest.main()
