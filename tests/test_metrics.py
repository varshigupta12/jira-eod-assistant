import unittest
from datetime import datetime, timedelta, timezone

from metrics import (
    StatusInterval,
    carry_over_count,
    continuous_entry,
    current_status_age,
    cycle_time,
    duration_percentile,
    flow_efficiency,
    format_days,
    format_duration,
    issue_created,
    lead_time,
    median,
    percentile,
    sprint_names,
    status_intervals,
    time_in_statuses,
)


NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
BLOCKED = ("blocked", "impediment")
DONE = ("done", "closed", "resolved")
STARTED = ("in progress",)


def at(day, hour=0):
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


def transition(moment, previous, following):
    return {
        "created": moment.isoformat(),
        "items": [
            {"field": "status", "fromString": previous, "toString": following}
        ],
    }


class StatusIntervalTests(unittest.TestCase):
    def test_builds_timeline_from_creation_through_transitions(self):
        intervals = status_intervals(
            [
                transition(at(3), "To Do", "In Progress"),
                transition(at(5), "In Progress", "Done"),
            ],
            created=at(1),
            current_status="Done",
            now=NOW,
        )

        self.assertEqual(
            [interval.status for interval in intervals],
            ["To Do", "In Progress", "Done"],
        )
        self.assertEqual(intervals[0].start, at(1))
        self.assertEqual(intervals[0].end, at(3))
        self.assertEqual(intervals[1].duration(NOW), timedelta(days=2))
        self.assertTrue(intervals[-1].is_open)

    def test_issue_without_transitions_spans_creation_to_now(self):
        intervals = status_intervals(
            [], created=at(10), current_status="Blocked", now=NOW
        )

        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0].status, "Blocked")
        self.assertEqual(intervals[0].start, at(10))
        self.assertEqual(intervals[0].duration(NOW), timedelta(days=8, hours=12))

    def test_issue_created_directly_into_blocked_still_measures_duration(self):
        """Replaces the old created-timestamp special case in the blocker report."""
        intervals = status_intervals(
            [], created=at(4), current_status="Blocked", now=NOW
        )

        self.assertEqual(continuous_entry(intervals, BLOCKED), at(4))

    def test_missing_creation_falls_back_to_first_transition(self):
        intervals = status_intervals(
            [transition(at(6), "To Do", "In Progress")],
            created=None,
            current_status="In Progress",
            now=NOW,
        )

        self.assertEqual(intervals[0].start, at(6))

    def test_naive_timestamps_are_treated_as_utc(self):
        intervals = status_intervals(
            [], created=datetime(2026, 9, 10), current_status="Open", now=NOW
        )

        self.assertEqual(intervals[0].start, at(10))

    def test_issue_status_field_wins_over_a_truncated_changelog(self):
        intervals = status_intervals(
            [transition(at(3), "To Do", "In Progress")],
            created=at(1),
            current_status="Blocked",
            now=NOW,
        )

        self.assertEqual(intervals[-1].status, "Blocked")
        self.assertEqual(continuous_entry(intervals, BLOCKED), at(3))


class ContinuousEntryTests(unittest.TestCase):
    def test_transitions_inside_the_status_set_do_not_reset_the_clock(self):
        intervals = status_intervals(
            [
                transition(at(4), "In Progress", "Blocked"),
                transition(at(6), "Blocked", "Impediment"),
            ],
            created=at(1),
            current_status="Impediment",
            now=NOW,
        )

        self.assertEqual(continuous_entry(intervals, BLOCKED), at(4))

    def test_leaving_and_re_entering_resets_to_the_latest_entry(self):
        intervals = status_intervals(
            [
                transition(at(2), "In Progress", "Blocked"),
                transition(at(5), "Blocked", "In Progress"),
                transition(at(9), "In Progress", "Blocked"),
            ],
            created=at(1),
            current_status="Blocked",
            now=NOW,
        )

        self.assertEqual(continuous_entry(intervals, BLOCKED), at(9))

    def test_returns_none_when_not_currently_in_the_status_set(self):
        intervals = status_intervals(
            [
                transition(at(2), "In Progress", "Blocked"),
                transition(at(5), "Blocked", "In Progress"),
            ],
            created=at(1),
            current_status="In Progress",
            now=NOW,
        )

        self.assertIsNone(continuous_entry(intervals, BLOCKED))


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.intervals = status_intervals(
            [
                transition(at(2), "To Do", "In Progress"),
                transition(at(4), "In Progress", "Blocked"),
                transition(at(8), "Blocked", "In Progress"),
                transition(at(10), "In Progress", "Done"),
            ],
            created=at(1),
            current_status="Done",
            now=NOW,
        )

    def test_sums_repeated_visits_to_a_status(self):
        self.assertEqual(
            time_in_statuses(self.intervals, ("in progress",), NOW),
            timedelta(days=4),
        )

    def test_totals_blocked_time_across_the_whole_life(self):
        self.assertEqual(
            time_in_statuses(self.intervals, BLOCKED, NOW), timedelta(days=4)
        )

    def test_cycle_time_runs_from_first_active_status_to_done(self):
        self.assertEqual(
            cycle_time(self.intervals, STARTED, DONE), timedelta(days=8)
        )

    def test_lead_time_runs_from_creation_to_done(self):
        self.assertEqual(
            lead_time(at(1), self.intervals, DONE), timedelta(days=9)
        )

    def test_cycle_time_is_none_while_work_is_unfinished(self):
        unfinished = status_intervals(
            [transition(at(2), "To Do", "In Progress")],
            created=at(1),
            current_status="In Progress",
            now=NOW,
        )

        self.assertIsNone(cycle_time(unfinished, STARTED, DONE))

    def test_cycle_time_is_none_when_work_never_became_active(self):
        self.assertIsNone(
            cycle_time(
                status_intervals(
                    [transition(at(3), "To Do", "Done")],
                    created=at(1),
                    current_status="Done",
                    now=NOW,
                ),
                STARTED,
                DONE,
            )
        )

    def test_current_status_age_measures_the_open_interval(self):
        aging = status_intervals(
            [transition(at(14), "To Do", "In Progress")],
            created=at(1),
            current_status="In Progress",
            now=NOW,
        )

        self.assertEqual(
            current_status_age(aging, NOW), timedelta(days=4, hours=12)
        )


class FlowEfficiencyTests(unittest.TestCase):
    def test_splits_active_time_from_waiting_time(self):
        intervals = status_intervals(
            [
                transition(at(1), "To Do", "In Progress"),
                transition(at(4), "In Progress", "Blocked"),
                transition(at(5), "Blocked", "In Progress"),
                transition(at(7), "In Progress", "Done"),
            ],
            created=at(1),
            current_status="Done",
            now=NOW,
        )

        # 6 days total before Done, 1 of them blocked.
        self.assertAlmostEqual(
            flow_efficiency(intervals, BLOCKED, DONE, NOW), 5 / 6
        )

    def test_excludes_time_spent_sitting_in_done(self):
        intervals = status_intervals(
            [transition(at(3), "In Progress", "Done")],
            created=at(1),
            current_status="Done",
            now=NOW,
        )

        self.assertEqual(flow_efficiency(intervals, BLOCKED, DONE, NOW), 1.0)

    def test_returns_none_when_work_starts_already_done(self):
        intervals = status_intervals(
            [], created=at(1), current_status="Done", now=NOW
        )

        self.assertIsNone(flow_efficiency(intervals, BLOCKED, DONE, NOW))

    def test_excludes_backlog_before_the_first_active_status(self):
        intervals = (
            StatusInterval("To Do", at(1), at(5)),
            StatusInterval("In Progress", at(5), at(7)),
            StatusInterval("Blocked", at(7), at(8)),
            StatusInterval("Done", at(8), None),
        )

        self.assertAlmostEqual(
            flow_efficiency(
                intervals,
                BLOCKED,
                DONE,
                NOW,
                started_statuses=("In Progress",),
            ),
            2 / 3,
        )


class StatisticsTests(unittest.TestCase):
    def test_median_interpolates_across_an_even_sample(self):
        self.assertEqual(median([1, 2, 3, 4]), 2.5)

    def test_median_of_odd_sample_is_the_middle_value(self):
        self.assertEqual(median([5, 1, 3]), 3)

    def test_percentile_interpolates_linearly(self):
        self.assertAlmostEqual(percentile([1, 2, 3, 4, 5], 0.85), 4.4)

    def test_single_value_reports_itself(self):
        self.assertEqual(percentile([7], 0.85), 7.0)

    def test_empty_sample_has_no_percentile(self):
        self.assertIsNone(percentile([], 0.5))
        self.assertIsNone(median([]))

    def test_duration_percentile_returns_a_duration(self):
        self.assertEqual(
            duration_percentile(
                [timedelta(days=1), timedelta(days=3)], 0.5
            ),
            timedelta(days=2),
        )


class FormattingTests(unittest.TestCase):
    def test_formats_days_and_hours(self):
        self.assertEqual(
            format_duration(timedelta(days=3, hours=3)), "3 days 3 hours"
        )

    def test_uses_singular_units(self):
        self.assertEqual(
            format_duration(timedelta(days=1, hours=1)), "1 day 1 hour"
        )

    def test_reports_short_durations(self):
        self.assertEqual(format_duration(timedelta(minutes=5)), "<1 hour")

    def test_formats_days_for_charting(self):
        self.assertEqual(format_days(timedelta(days=2, hours=12)), 2.5)
        self.assertIsNone(format_days(None))


class SprintTests(unittest.TestCase):
    def test_reads_structured_sprint_objects(self):
        fields = {"customfield_10020": [{"name": "Sprint 4"}, {"name": "Sprint 5"}]}

        self.assertEqual(sprint_names(fields), ("Sprint 4", "Sprint 5"))

    def test_reads_serialized_sprint_strings(self):
        fields = {
            "customfield_10020": [
                "com.atlassian.greenhopper.service.sprint.Sprint@1[id=1,name=Sprint 9,state=ACTIVE]"
            ]
        }

        self.assertEqual(sprint_names(fields), ("Sprint 9",))

    def test_counts_carry_over_for_unfinished_work(self):
        fields = {"customfield_10020": [{"name": "S1"}, {"name": "S2"}, {"name": "S3"}]}

        self.assertEqual(carry_over_count(fields, DONE, "In Progress"), 2)

    def test_completed_work_never_reports_carry_over(self):
        fields = {"customfield_10020": [{"name": "S1"}, {"name": "S2"}]}

        self.assertEqual(carry_over_count(fields, DONE, "Done"), 0)

    def test_first_sprint_is_not_carry_over(self):
        fields = {"customfield_10020": [{"name": "S1"}]}

        self.assertEqual(carry_over_count(fields, DONE, "In Progress"), 0)


class CreatedTests(unittest.TestCase):
    def test_parses_jira_creation_timestamp(self):
        self.assertEqual(
            issue_created({"created": "2026-09-01T00:00:00.000+0000"}), at(1)
        )

    def test_missing_or_invalid_creation_is_none(self):
        self.assertIsNone(issue_created({}))
        self.assertIsNone(issue_created({"created": "not-a-date"}))


if __name__ == "__main__":
    unittest.main()
