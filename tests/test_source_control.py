import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

import requests

from source_control import (
    SourceControlError,
    annotate_github_activity,
    collect_linked_github_activity,
    linked_github_pull_requests,
)


NOW = datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc)


def response(data):
    result = Mock()
    result.raise_for_status.return_value = None
    result.json.return_value = data
    return result


def issue(key="ENG-1", issue_id="10001"):
    return {"id": issue_id, "key": key, "fields": {}}


class LinkedPullRequestTests(unittest.TestCase):
    def test_reads_jira_development_data_before_remote_links(self):
        session = Mock()
        session.get.return_value = response(
            {
                "detail": [
                    {
                        "pullRequests": [
                            {"url": "https://github.com/acme/widget/pull/42"}
                        ]
                    }
                ]
            }
        )

        links = linked_github_pull_requests(
            issue(),
            "https://jira.example",
            "person@example.com",
            "jira-token",
            "acme",
            session,
        )

        self.assertEqual(links, {("acme", "widget", 42)})
        session.get.assert_called_once()
        self.assertIn("/rest/dev-status/1.0/issue/detail", session.get.call_args.args[0])
        self.assertEqual(
            session.get.call_args.kwargs["params"]["applicationType"], "GitHub"
        )

    def test_falls_back_to_remote_links(self):
        session = Mock()
        session.get.side_effect = [
            response({"detail": []}),
            response(
                [
                    {
                        "object": {
                            "url": "https://github.com/acme/widget/pull/7?source=jira"
                        }
                    }
                ]
            ),
        ]

        links = linked_github_pull_requests(
            issue(),
            "https://jira.example",
            "person@example.com",
            "jira-token",
            "acme",
            session,
        )

        self.assertEqual(links, {("acme", "widget", 7)})
        self.assertIn("/remotelink", session.get.call_args_list[1].args[0])

    def test_rejects_links_outside_configured_organization(self):
        session = Mock()
        session.get.return_value = response(
            {
                "detail": [
                    {
                        "pullRequests": [
                            {"url": "https://github.com/other/widget/pull/42"},
                            {"url": "https://git.example/acme/widget/pull/42"},
                        ]
                    }
                ]
            }
        )

        links = linked_github_pull_requests(
            issue(),
            "https://jira.example",
            "person@example.com",
            "jira-token",
            "acme",
            session,
        )

        self.assertEqual(links, set())


class ActivityTests(unittest.TestCase):
    def github_side_effect(self, request_url, **kwargs):
        if "dev-status" in request_url:
            return response(
                {
                    "detail": [
                        {
                            "pullRequests": [
                                {"url": "https://github.com/acme/widget/pull/42"}
                            ]
                        }
                    ]
                }
            )
        if request_url.endswith("/pulls/42"):
            return response(
                {
                    "title": "Improve linked reporting",
                    "state": "open",
                    "updated_at": "2026-09-21T12:00:00Z",
                    "merged_at": None,
                    "html_url": "https://github.com/acme/widget/pull/42",
                }
            )
        if request_url.endswith("/pulls/42/commits"):
            return response(
                [
                    {
                        "sha": "abcdef012345",
                        "html_url": "https://github.com/acme/widget/commit/abcdef0",
                        "commit": {
                            "message": "Add GitHub evidence\n\nDetails",
                            "committer": {"date": "2026-09-21T11:00:00Z"},
                        },
                    },
                    {
                        "sha": "stale1234567",
                        "html_url": "https://github.com/acme/widget/commit/stale12",
                        "commit": {
                            "message": "Old work",
                            "committer": {"date": "2026-09-20T12:59:59Z"},
                        },
                    },
                ]
            )
        raise AssertionError(request_url)

    def test_keeps_only_pr_and_commit_activity_from_previous_24_hours(self):
        session = Mock()
        session.get.side_effect = self.github_side_effect

        activity = collect_linked_github_activity(
            [issue()],
            "https://jira.example",
            "person@example.com",
            "jira-token",
            "acme",
            "github-token",
            session,
            NOW,
        )["ENG-1"]

        self.assertEqual(
            [entry["source"] for entry in activity],
            ["commit", "pull_request"],
        )
        self.assertNotIn("Old work", str(activity))
        github_calls = [
            call for call in session.get.call_args_list if "api.github.com" in call.args[0]
        ]
        self.assertTrue(
            all(
                call.kwargs["headers"]["Authorization"] == "Bearer github-token"
                for call in github_calls
            )
        )

    def test_makes_no_github_calls_when_jira_has_no_link(self):
        session = Mock()
        session.get.side_effect = [
            response({"detail": []}),
            response([]),
        ]

        activity = collect_linked_github_activity(
            [issue()],
            "https://jira.example",
            "person@example.com",
            "jira-token",
            "acme",
            "github-token",
            session,
            NOW,
        )

        self.assertEqual(activity, {"ENG-1": []})
        self.assertFalse(
            any("api.github.com" in call.args[0] for call in session.get.call_args_list)
        )

    def test_reuses_shared_pr_and_deduplicates_activity_urls(self):
        session = Mock()
        session.get.side_effect = self.github_side_effect

        activity = collect_linked_github_activity(
            [issue("ENG-1", "10001"), issue("ENG-2", "10002")],
            "https://jira.example",
            "person@example.com",
            "jira-token",
            "acme",
            "github-token",
            session,
            NOW,
        )

        self.assertEqual(activity["ENG-1"], activity["ENG-2"])
        github_calls = [
            call for call in session.get.call_args_list if "api.github.com" in call.args[0]
        ]
        self.assertEqual(len(github_calls), 2)

    def test_annotation_does_not_mutate_input(self):
        original = issue()
        session = Mock()
        session.get.side_effect = [response({"detail": []}), response([])]

        annotated = annotate_github_activity(
            [original],
            "https://jira.example",
            "person@example.com",
            "jira-token",
            "acme",
            "github-token",
            session,
            NOW,
        )

        self.assertNotIn("_eod_scm_activity", original)
        self.assertEqual(annotated[0]["_eod_scm_activity"], [])

    def test_sanitizes_request_errors(self):
        session = Mock()
        session.get.side_effect = requests.Timeout(
            "token leaked in https://api.github.com/private"
        )

        with self.assertRaisesRegex(
            SourceControlError, "Linked activity request failed: Timeout"
        ) as raised:
            collect_linked_github_activity(
                [issue()],
                "https://jira.example",
                "person@example.com",
                "jira-token",
                "acme",
                "github-token",
                session,
                NOW,
            )

        self.assertNotIn("private", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
