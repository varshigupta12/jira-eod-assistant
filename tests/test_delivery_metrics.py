import os
import unittest
import unittest.mock
from datetime import datetime, timezone
from unittest.mock import Mock

import requests

JIRA_ENV = {
    "JIRA_DOMAIN": "example.atlassian.net",
    "JIRA_EMAIL": "person@example.com",
    "JIRA_API_TOKEN": "token",
    "MATTERMOST_WEBHOOK_URL": "https://mattermost.example/hooks/test",
}

from delivery_metrics import (
    build_metrics_jql,
    collect_team_snapshot,
    fetch_team_issues,
    issue_metric,
    summarize,
)
from eod_report import EODReportError
from report_config import (
    AISettings,
    DeliveryMetricsSettings,
    PulseSettings,
    ReleaseBlockerSettings,
    ReportSettings,
    Team,
)
from snapshot_store import TeamSnapshot
from zoneinfo import ZoneInfo
from datetime import date, time


NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def team(**overrides):
    values = {
        "id": "emea",
        "name": "EMEA",
        "projects": ("ENG",),
        "filters": (),
        "board_ids": (),
        "team_field": None,
        "team_value": None,
        "daily_schedule": None,
        "include_in_pulse": True,
    }
    values.update(overrides)
    return Team(**values)


def settings(**overrides):
    metrics = DeliveryMetricsSettings(
        enabled=True, lookback_days=30, aging_wip_days=5, **overrides
    )
    return ReportSettings(
        teams=(team(),),
        ai=AISettings(enabled=False, model="m", max_tokens=1024),
        pulse=PulseSettings(
            enabled=False,
            title="Pulse",
            timezone=ZoneInfo("UTC"),
            time=time(20, 0),
            weekday=4,
            cadence_days=14,
            anchor_date=date(2026, 8, 14),
        ),
        release_blockers=ReleaseBlockerSettings(enabled=False, label=None),
        blocked_statuses=frozenset({"blocked"}),
        deploy_statuses=frozenset({"to be deployed"}),
        done_statuses=frozenset({"done"}),
        review_statuses=frozenset({"in review"}),
        delivery_metrics=metrics,
    )


def at(day, hour=0):
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


def transition(moment, previous, following):
    return {
        "created": moment.isoformat(),
        "items": [
            {"field": "status", "fromString": previous, "toString": following}
        ],
    }


def issue(
    key,
    status,
    created=None,
    histories=(),
    assignee="Ada",
    sprints=None,
    versions=None,
    labels=None,
    changelog_total=None,
    status_category=None,
):
    fields = {
        "summary": f"Summary for {key}",
        "status": {
            "name": status,
            **(
                {"statusCategory": {"key": status_category}}
                if status_category
                else {}
            ),
        },
        "assignee": {"displayName": assignee} if assignee else None,
        "created": (created or at(1)).isoformat(),
    }
    if sprints is not None:
        fields["customfield_10020"] = [{"name": name} for name in sprints]
    if versions is not None:
        fields["fixVersions"] = [{"name": name} for name in versions]
    if labels is not None:
        fields["labels"] = list(labels)
    changelog = {"histories": list(histories)}
    if changelog_total is not None:
        changelog["total"] = changelog_total
    return {
        "key": key,
        "fields": fields,
        "changelog": changelog,
    }


class JqlTests(unittest.TestCase):
    def test_scopes_to_project_and_covers_recent_and_active_work(self):
        jql = build_metrics_jql(team(), 30)

        self.assertIn('project = "ENG"', jql)
        self.assertIn("resolved >= -30d", jql)
        self.assertIn('statusCategory = "In Progress"', jql)

    def test_saved_filter_takes_precedence_over_team_field(self):
        jql = build_metrics_jql(
            team(filters=("EMEA board",), team_field="Team", team_value="EMEA"),
            14,
        )

        self.assertIn('filter = "EMEA board"', jql)
        self.assertNotIn('"Team" =', jql)

    def test_requires_a_scope(self):
        with self.assertRaisesRegex(EODReportError, "needs projects"):
            build_metrics_jql(team(projects=()), 30)

    def test_escapes_quotes_in_scope_values(self):
        jql = build_metrics_jql(team(projects=('EN"G',)), 30)

        self.assertIn('EN\\"G', jql)

    def test_release_query_has_no_date_boundary(self):
        from delivery_metrics import build_release_metrics_jql

        jql = build_release_metrics_jql(team(), ("2026.1",))

        self.assertIn('fixVersion = "2026.1"', jql)
        self.assertIn('labels = "2026.1"', jql)
        self.assertNotIn("resolved", jql)


class FetchTests(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch.dict(os.environ, JIRA_ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config(self):
        from delivery_metrics import _team_config

        return _team_config(
            team(),
            settings(),
        )

    def test_requests_the_changelog_inline_and_follows_pages(self):
        first = Mock()
        first.raise_for_status.return_value = None
        first.json.return_value = {
            "issues": [issue("ENG-1", "Done")],
            "nextPageToken": "page-2",
        }
        second = Mock()
        second.raise_for_status.return_value = None
        second.json.return_value = {"issues": [issue("ENG-2", "Done")]}
        session = Mock()
        session.get.side_effect = [first, second]

        issues = fetch_team_issues(team(), self._config(), settings(), session)

        self.assertEqual([item["key"] for item in issues], ["ENG-1", "ENG-2"])
        self.assertEqual(session.get.call_args_list[0].kwargs["params"]["expand"], "changelog")

    def test_includes_the_configured_sprint_field(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"issues": []}
        session = Mock()
        session.get.return_value = response

        fetch_team_issues(
            team(),
            self._config(),
            settings(sprint_field="customfield_10020"),
            session,
        )

        self.assertIn(
            "customfield_10020", session.get.call_args.kwargs["params"]["fields"]
        )

    def test_sanitizes_network_errors(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError(
            "failed to reach https://team.atlassian.net/rest/api/3/search/jql"
        )

        with self.assertRaises(EODReportError) as error:
            fetch_team_issues(team(), self._config(), settings(), session)

        self.assertNotIn("atlassian.net", str(error.exception))

    def test_fetches_all_issues_for_each_discovered_release(self):
        recent = Mock()
        recent.raise_for_status.return_value = None
        recent.json.return_value = {
            "issues": [issue("ENG-2", "Done", versions=("2026.1",))]
        }
        release = Mock()
        release.raise_for_status.return_value = None
        release.json.return_value = {
            "issues": [
                issue("ENG-1", "Done", versions=("2026.1",)),
                issue("ENG-2", "Done", versions=("2026.1",)),
            ]
        }
        session = Mock()
        session.get.side_effect = [recent, release]

        issues = fetch_team_issues(team(), self._config(), settings(), session)

        self.assertEqual({item["key"] for item in issues}, {"ENG-1", "ENG-2"})
        by_key = {item["key"]: item for item in issues}
        self.assertFalse(by_key["ENG-1"]["_delivery_in_lookback"])
        self.assertTrue(by_key["ENG-2"]["_delivery_in_lookback"])
        release_jql = session.get.call_args_list[1].kwargs["params"]["jql"]
        self.assertNotIn("resolved", release_jql)

    def test_fetches_every_page_of_a_truncated_changelog(self):
        expanded = Mock()
        expanded.raise_for_status.return_value = None
        expanded.json.return_value = {
            "issues": [
                issue(
                    "ENG-1",
                    "Done",
                    histories=(transition(at(2), "To Do", "In Progress"),),
                    changelog_total=2,
                )
            ]
        }
        changelog = Mock()
        changelog.raise_for_status.return_value = None
        changelog.json.return_value = {
            "values": [
                transition(at(2), "To Do", "In Progress"),
                transition(at(6), "In Progress", "Done"),
            ],
            "total": 2,
        }
        session = Mock()
        session.get.side_effect = [expanded, changelog]

        issues = fetch_team_issues(team(), self._config(), settings(), session)

        self.assertEqual(len(issues[0]["changelog"]["histories"]), 2)
        self.assertIn(
            "/issue/ENG-1/changelog",
            session.get.call_args_list[1].args[0],
        )

    def test_explicit_release_is_fetched_even_when_this_team_did_not_discover_it(self):
        recent = Mock()
        recent.raise_for_status.return_value = None
        recent.json.return_value = {"issues": []}
        release = Mock()
        release.raise_for_status.return_value = None
        release.json.return_value = {
            "issues": [issue("ENG-1", "Done", versions=("2026.1",))]
        }
        session = Mock()
        session.get.side_effect = [recent, release]

        issues = fetch_team_issues(
            team(),
            self._config(),
            settings(),
            session,
            releases=("2026.1",),
        )

        self.assertEqual([item["key"] for item in issues], ["ENG-1"])
        self.assertFalse(issues[0]["_delivery_in_lookback"])


class IssueMetricTests(unittest.TestCase):
    def test_measures_a_completed_issue(self):
        metric = issue_metric(
            issue(
                "ENG-1",
                "Done",
                histories=[
                    transition(at(2), "To Do", "In Progress"),
                    transition(at(6), "In Progress", "Done"),
                ],
            ),
            settings(),
            NOW,
        )

        self.assertTrue(metric.is_done)
        self.assertEqual(metric.cycle_time_days, 4.0)
        self.assertEqual(metric.lead_time_days, 5.0)

    def test_measures_a_blocked_issue(self):
        metric = issue_metric(
            issue(
                "ENG-2",
                "Blocked",
                histories=[
                    transition(at(2), "To Do", "In Progress"),
                    transition(at(10), "In Progress", "Blocked"),
                ],
            ),
            settings(),
            NOW,
        )

        self.assertTrue(metric.is_blocked)
        self.assertEqual(metric.blocked_days, 8.5)
        self.assertIsNone(metric.cycle_time_days)

    def test_counts_carry_over_sprints(self):
        metric = issue_metric(
            issue("ENG-3", "In Progress", sprints=["S1", "S2", "S3"]),
            settings(),
            NOW,
        )

        self.assertEqual(metric.carry_over_sprints, 2)

    def test_skips_issues_without_fields(self):
        self.assertIsNone(issue_metric({"key": "ENG-9"}, settings(), NOW))


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch.dict(os.environ, JIRA_ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _snapshot(self):
        session = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "issues": [
                issue(
                    "ENG-1",
                    "Done",
                    histories=[
                        transition(at(2), "To Do", "In Progress"),
                        transition(at(4), "In Progress", "Done"),
                    ],
                ),
                issue(
                    "ENG-2",
                    "Done",
                    histories=[
                        transition(at(2), "To Do", "In Progress"),
                        transition(at(8), "In Progress", "Done"),
                    ],
                ),
                issue(
                    "ENG-3",
                    "Blocked",
                    histories=[transition(at(9), "In Progress", "Blocked")],
                ),
                issue(
                    "ENG-4",
                    "In Progress",
                    histories=[transition(at(5), "To Do", "In Progress")],
                    sprints=["S1", "S2", "S3"],
                ),
            ]
        }
        session.get.return_value = response
        return collect_team_snapshot(team(), settings(), session, NOW)

    def test_counts_throughput_and_work_in_progress(self):
        summary = summarize(self._snapshot(), settings())

        self.assertEqual(summary.throughput, 2)
        self.assertEqual(summary.wip, 2)
        self.assertEqual(summary.blocked, 1)

    def test_reports_cycle_time_percentiles(self):
        summary = summarize(self._snapshot(), settings())

        self.assertEqual(summary.cycle_time_median_days, 4.0)
        self.assertEqual(summary.cycle_time_p85_days, 5.4)

    def test_ranks_aging_work_excluding_blocked_items(self):
        summary = summarize(self._snapshot(), settings())

        self.assertEqual([item.key for item in summary.aging_wip], ["ENG-4"])

    def test_ranks_blocked_work_longest_first(self):
        summary = summarize(self._snapshot(), settings())

        self.assertEqual([item.key for item in summary.longest_blocked], ["ENG-3"])

    def test_flags_chronic_carry_over(self):
        summary = summarize(self._snapshot(), settings())

        self.assertEqual(
            [item.key for item in summary.chronic_carry_over], ["ENG-4"]
        )

    def test_empty_squad_reports_no_percentiles(self):
        session = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"issues": []}
        session.get.return_value = response

        summary = summarize(
            collect_team_snapshot(team(), settings(), session, NOW), settings()
        )

        self.assertEqual(summary.throughput, 0)
        self.assertIsNone(summary.cycle_time_median_days)
        self.assertIsNone(summary.flow_efficiency)

    def test_release_backlog_is_not_counted_as_in_flight(self):
        backlog = issue_metric(
            issue(
                "ENG-1",
                "To Do",
                versions=("2026.1",),
                status_category="new",
            ),
            settings(),
            NOW,
        )
        snapshot = TeamSnapshot(
            team_id="emea",
            team_name="EMEA",
            captured_at=NOW.isoformat(),
            issues=(backlog,),
        )

        summary = summarize(snapshot, settings(), release="2026.1")

        self.assertEqual(summary.wip, 0)
        self.assertEqual(summary.aging_wip, ())


if __name__ == "__main__":
    unittest.main()
