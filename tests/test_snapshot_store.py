import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from snapshot_store import (
    SCHEMA_VERSION,
    IssueMetric,
    SnapshotError,
    TeamSnapshot,
    latest_by_team,
    load_snapshots,
    snapshot_path,
    utc_timestamp,
    write_snapshot,
)


def make_snapshot(team_id="emea", captured_at="2026-09-18T20:00:00+00:00", issues=()):
    return TeamSnapshot(
        team_id=team_id,
        team_name=team_id.upper(),
        captured_at=captured_at,
        sprint="Sprint 9",
        issues=tuple(issues),
    )


ISSUE = IssueMetric(
    key="ENG-1",
    summary="Summary",
    status="In Progress",
    assignee="Ada",
    is_done=False,
    is_blocked=False,
    age_in_status_days=3.5,
    cycle_time_days=None,
    flow_efficiency=0.8,
    carry_over_sprints=2,
)


class WriteTests(unittest.TestCase):
    def test_writes_one_file_per_team_per_day(self):
        with tempfile.TemporaryDirectory() as directory:
            target = write_snapshot(directory, make_snapshot(issues=[ISSUE]))

            self.assertEqual(target.name, "2026-09-18-emea.json")
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
            self.assertEqual(payload["issues"][0]["key"], "ENG-1")
            self.assertEqual(payload["issues"][0]["carry_over_sprints"], 2)

    def test_rewriting_the_same_day_replaces_the_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            write_snapshot(directory, make_snapshot(issues=[ISSUE]))
            write_snapshot(directory, make_snapshot())

            stored = load_snapshots(directory)
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].issues, ())

    def test_creates_missing_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            nested = Path(directory) / "a" / "b"

            target = write_snapshot(nested, make_snapshot())

            self.assertTrue(target.exists())

    def test_team_identifiers_cannot_escape_the_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(team_id="../../etc/passwd")

            target = snapshot_path(directory, snapshot)

            self.assertEqual(target.parent, Path(directory))
            self.assertNotIn("..", target.name)


class LoadTests(unittest.TestCase):
    def test_returns_nothing_when_directory_is_absent(self):
        self.assertEqual(load_snapshots("/nonexistent-metrics-dir"), ())

    def test_orders_snapshots_oldest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            write_snapshot(
                directory, make_snapshot(captured_at="2026-09-18T20:00:00+00:00")
            )
            write_snapshot(
                directory, make_snapshot(captured_at="2026-09-11T20:00:00+00:00")
            )

            stored = load_snapshots(directory)

            self.assertEqual(
                [item.captured_date for item in stored],
                ["2026-09-11", "2026-09-18"],
            )

    def test_filters_by_team(self):
        with tempfile.TemporaryDirectory() as directory:
            write_snapshot(directory, make_snapshot(team_id="emea"))
            write_snapshot(directory, make_snapshot(team_id="apac"))

            stored = load_snapshots(directory, team_ids=["apac"])

            self.assertEqual([item.team_id for item in stored], ["apac"])

    def test_filters_by_date(self):
        with tempfile.TemporaryDirectory() as directory:
            write_snapshot(
                directory, make_snapshot(captured_at="2026-08-01T20:00:00+00:00")
            )
            write_snapshot(
                directory, make_snapshot(captured_at="2026-09-18T20:00:00+00:00")
            )

            stored = load_snapshots(
                directory, since=datetime(2026, 9, 1, tzinfo=timezone.utc)
            )

            self.assertEqual([item.captured_date for item in stored], ["2026-09-18"])

    def test_round_trips_issue_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            write_snapshot(directory, make_snapshot(issues=[ISSUE]))

            restored = load_snapshots(directory)[0].issues[0]

            self.assertEqual(restored, ISSUE)

    def test_rejects_an_unsupported_schema_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-09-18-emea.json"
            path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")

            with self.assertRaisesRegex(SnapshotError, "unsupported snapshot schema"):
                load_snapshots(directory)

    def test_rejects_malformed_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-09-18-emea.json"
            path.write_text("{not json", encoding="utf-8")

            with self.assertRaisesRegex(SnapshotError, "Failed to read"):
                load_snapshots(directory)

    def test_latest_snapshot_wins_per_team(self):
        snapshots = [
            make_snapshot(team_id="emea", captured_at="2026-09-11T20:00:00+00:00"),
            make_snapshot(team_id="emea", captured_at="2026-09-18T20:00:00+00:00"),
            make_snapshot(team_id="apac", captured_at="2026-09-12T20:00:00+00:00"),
        ]

        newest = latest_by_team(snapshots)

        self.assertEqual(newest["emea"].captured_date, "2026-09-18")
        self.assertEqual(set(newest), {"emea", "apac"})


class TimestampTests(unittest.TestCase):
    def test_drops_microseconds_and_normalizes_to_utc(self):
        moment = datetime(2026, 9, 18, 20, 0, 0, 123456, tzinfo=timezone.utc)

        self.assertEqual(utc_timestamp(moment), "2026-09-18T20:00:00+00:00")

    def test_treats_naive_timestamps_as_utc(self):
        self.assertEqual(
            utc_timestamp(datetime(2026, 9, 18, 20, 0)), "2026-09-18T20:00:00+00:00"
        )


if __name__ == "__main__":
    unittest.main()
