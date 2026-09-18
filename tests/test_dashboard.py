import tempfile
import unittest
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dashboard import (
    ReleaseView,
    build_dashboard,
    build_trends,
    render_dashboard,
    write_dashboard,
)
from delivery_metrics import summarize
from eod_report import EODReportError
from report_config import (
    AISettings,
    DeliveryMetricsSettings,
    PulseSettings,
    ReleaseBlockerSettings,
    ReportSettings,
    Team,
)
from snapshot_store import IssueMetric, TeamSnapshot, write_snapshot


NOW = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc)


def team(team_id="emea", name="EMEA"):
    return Team(
        id=team_id,
        name=name,
        projects=("ENG",),
        filters=(),
        board_ids=(),
        team_field=None,
        team_value=None,
        daily_schedule=None,
        include_in_pulse=True,
    )


def settings(teams=None, **overrides):
    return ReportSettings(
        teams=teams or (team(),),
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
        delivery_metrics=DeliveryMetricsSettings(
            enabled=True, lookback_days=30, aging_wip_days=5, **overrides
        ),
    )


def metric(key, **overrides):
    values = {
        "key": key,
        "summary": f"Summary for {key}",
        "status": "In Progress",
        "assignee": "Ada",
        "is_done": False,
        "is_blocked": False,
        "age_in_status_days": 9.0,
        "blocked_days": None,
        "cycle_time_days": None,
        "lead_time_days": None,
        "flow_efficiency": 0.75,
        "carry_over_sprints": 0,
    }
    values.update(overrides)
    return IssueMetric(**values)


def snapshot(team_id="emea", captured_at="2026-09-18T20:00:00+00:00", issues=()):
    return TeamSnapshot(
        team_id=team_id,
        team_name=team_id.upper(),
        captured_at=captured_at,
        issues=tuple(issues),
    )


class RenderTests(unittest.TestCase):
    def _markup(self, issues=None, teams=None, snapshots=None):
        config = settings(teams=teams)
        stored = snapshots or [snapshot(issues=issues or ())]
        summaries = [summarize(item, config) for item in stored]
        return render_dashboard(
            [ReleaseView(None, tuple(summaries))],
            build_trends(stored, config),
            config,
            NOW,
        )

    def test_renders_a_standalone_document_without_scripts_or_network(self):
        markup = self._markup()

        self.assertTrue(markup.startswith("<!doctype html>"))
        self.assertNotIn("<script", markup.casefold())
        self.assertNotIn("http://", markup)
        self.assertNotIn("https://", markup)

    def test_shows_every_squad_side_by_side(self):
        stored = [snapshot(team_id="emea"), snapshot(team_id="apac")]

        markup = self._markup(
            teams=(team("emea", "EMEA"), team("apac", "APAC")), snapshots=stored
        )

        self.assertIn("EMEA", markup)
        self.assertIn("APAC", markup)

    def test_lists_aging_work_in_progress(self):
        markup = self._markup(issues=[metric("ENG-7", age_in_status_days=12.0)])

        self.assertIn("ENG-7", markup)
        self.assertIn("12d", markup)

    def test_lists_longest_blocked_work(self):
        markup = self._markup(
            issues=[metric("ENG-8", is_blocked=True, blocked_days=4.0)]
        )

        self.assertIn("ENG-8", markup)
        self.assertIn("4d", markup)

    def test_lists_chronic_carry_over(self):
        markup = self._markup(issues=[metric("ENG-9", carry_over_sprints=3)])

        self.assertIn("3 sprints", markup)

    def test_reports_empty_panels_without_failing(self):
        markup = self._markup(issues=[])

        self.assertIn("Nothing to flag.", markup)

    def test_escapes_ticket_text(self):
        markup = self._markup(
            issues=[metric("ENG-10", summary='<img src=x onerror="alert(1)">')]
        )

        self.assertNotIn("<img", markup)
        self.assertIn("&lt;img", markup)

    def test_renders_a_trend_once_history_exists(self):
        stored = [
            snapshot(captured_at="2026-09-04T20:00:00+00:00"),
            snapshot(captured_at="2026-09-11T20:00:00+00:00"),
            snapshot(captured_at="2026-09-18T20:00:00+00:00"),
        ]

        markup = self._markup(snapshots=stored)

        self.assertIn("<svg", markup)

    def test_explains_when_history_is_too_short_for_a_trend(self):
        markup = self._markup()

        self.assertIn("Not enough history yet", markup)


class TrendTests(unittest.TestCase):
    def test_orders_points_oldest_first_per_team(self):
        stored = [
            snapshot(captured_at="2026-09-18T20:00:00+00:00"),
            snapshot(captured_at="2026-09-04T20:00:00+00:00"),
        ]

        trends = build_trends(stored, settings())

        self.assertEqual(
            [point.captured_date for point in trends["emea"]],
            ["2026-09-04", "2026-09-18"],
        )

    def test_counts_completed_and_blocked_per_capture(self):
        stored = [
            snapshot(
                issues=[
                    metric("ENG-1", is_done=True),
                    metric("ENG-2", is_blocked=True, blocked_days=1.0),
                ]
            )
        ]

        point = build_trends(stored, settings())["emea"][0]

        self.assertEqual(point.throughput, 1)
        self.assertEqual(point.blocked, 1)


class OfflineBuildTests(unittest.TestCase):
    def test_renders_from_stored_snapshots_without_touching_jira(self):
        with tempfile.TemporaryDirectory() as directory:
            config = settings(snapshot_dir=directory)
            write_snapshot(directory, snapshot(issues=[metric("ENG-1")]))

            markup = build_dashboard(config, refresh=False, now=NOW)

            self.assertIn("ENG-1", markup)

    def test_explains_when_no_snapshots_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            config = settings(snapshot_dir=directory)

            with self.assertRaisesRegex(EODReportError, "No metric snapshots"):
                build_dashboard(config, refresh=False, now=NOW)


class WriteTests(unittest.TestCase):
    def test_writes_the_dashboard_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out" / "dashboard.html"

            written = write_dashboard(str(target), "<!doctype html>")

            self.assertTrue(written.exists())
            self.assertEqual(written.read_text(encoding="utf-8"), "<!doctype html>")

    def test_writes_into_the_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "dashboard.html"

            written = write_dashboard(str(target), "<!doctype html>")

            self.assertTrue(written.exists())


if __name__ == "__main__":
    unittest.main()


class ReleaseFilterTests(unittest.TestCase):
    def _views(self, scopes, issues):
        config = settings()
        stored = [snapshot(issues=issues)]
        return (
            [
                ReleaseView(
                    scope,
                    tuple(summarize(item, config, scope) for item in stored),
                )
                for scope in scopes
            ],
            build_trends(stored, config),
            config,
        )

    def test_renders_one_tab_per_release_without_scripts(self):
        issues = (
            metric("EM-1", is_blocked=True, blocked_days=4.0,
                   fix_versions=("2026.09",)),
            metric("EM-2", is_blocked=True, blocked_days=9.0,
                   fix_versions=("2026.10",)),
        )
        views, trends, config = self._views(["2026.09", "2026.10"], issues)

        markup = render_dashboard(views, trends, config, NOW)

        self.assertNotIn("<script", markup.casefold())
        self.assertIn('id="rel-0"', markup)
        self.assertIn('id="rel-1"', markup)
        self.assertIn('id="view-1"', markup)
        self.assertIn("2026.10", markup)
        self.assertIn("#rel-1:checked ~ #view-1", markup)

    def test_a_single_release_renders_without_tab_chrome(self):
        issues = (metric("EM-1", fix_versions=("2026.09",)),)
        views, trends, config = self._views(["2026.09"], issues)

        markup = render_dashboard(views, trends, config, NOW)

        self.assertNotIn('id="rel-0"', markup)
        self.assertIn("release 2026.09", markup)

    def test_release_scoping_matches_labels_as_well_as_fix_versions(self):
        config = settings()
        issues = (
            metric("EM-1", is_blocked=True, blocked_days=4.0,
                   labels=("2026.09",)),
            metric("EM-2", is_blocked=True, blocked_days=9.0,
                   fix_versions=("2026.09",)),
            metric("EM-3", is_blocked=True, blocked_days=2.0,
                   fix_versions=("2026.10",)),
        )

        summary = summarize(snapshot(issues=issues), config, "2026.09")

        self.assertEqual(
            [issue.key for issue in summary.longest_blocked], ["EM-2", "EM-1"]
        )

    def test_blocked_panel_is_not_truncated(self):
        config = settings()
        issues = tuple(
            metric(
                f"EM-{index}",
                is_blocked=True,
                blocked_days=float(index),
                fix_versions=("2026.09",),
            )
            for index in range(1, 16)
        )
        views = [
            ReleaseView(
                "2026.09",
                (summarize(snapshot(issues=issues), config, "2026.09"),),
            )
        ]

        markup = render_dashboard(views, {}, config, NOW)

        for index in range(1, 16):
            self.assertIn(f"EM-{index}<", markup)

    def test_long_aging_list_expands_without_javascript(self):
        config = settings()
        issues = tuple(
            metric(
                f"EM-{index}",
                age_in_status_days=float(index + 10),
            )
            for index in range(1, 23)
        )
        views = [
            ReleaseView(
                None,
                (summarize(snapshot(issues=issues), config),),
            )
        ]

        markup = render_dashboard(views, {}, config, NOW)

        self.assertIn("<details", markup)
        self.assertIn("<summary>Show 12 more tickets</summary>", markup)
        self.assertIn("EM-22<", markup)
        self.assertNotIn("<script", markup.casefold())

    def test_ticket_keys_link_to_jira_when_a_base_url_is_known(self):
        config = settings()
        issues = (metric("EM-1", is_blocked=True, blocked_days=3.0),)
        views = [
            ReleaseView("", (summarize(snapshot(issues=issues), config),))
        ]

        markup = render_dashboard(
            views, {}, config, NOW, "https://example.atlassian.net"
        )

        self.assertIn(
            'href="https://example.atlassian.net/browse/EM-1"', markup
        )
        self.assertIn('rel="noopener noreferrer"', markup)

    def test_total_ticket_count_links_to_exact_release_jql(self):
        config = settings()
        issues = (
            metric(
                "EM-1",
                is_done=True,
                fix_versions=("2026.09",),
            ),
        )
        views = [
            ReleaseView(
                "2026.09",
                (summarize(snapshot(issues=issues), config, "2026.09"),),
            )
        ]

        markup = render_dashboard(
            views, {}, config, NOW, "https://example.atlassian.net"
        )

        self.assertIn(
            'href="https://example.atlassian.net/issues/?jql=', markup
        )
        self.assertIn("fixVersion%20%3D%20%222026.09%22", markup)
        self.assertIn("Open all 1 tickets in Jira", markup)
