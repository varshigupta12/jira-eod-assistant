import unittest
import json
from datetime import datetime, timezone
from unittest.mock import Mock

import requests

from eod_report import (
    Config,
    EODReportError,
    AIUpdate,
    build_jql,
    fetch_jira_tickets,
    filter_issues_with_recent_activity,
    generate_ai_updates,
    get_latest_comment,
    get_recent_comment,
    comment_body_to_text,
    parse_and_format,
    parse_and_format_by_status,
    post_openrouter_with_credit_retry,
    send_to_mattermost,
)


BASE_ENV = {
    "JIRA_DOMAIN": "example.atlassian.net",
    "JIRA_EMAIL": "person@example.com",
    "JIRA_API_TOKEN": "token",
    "MATTERMOST_WEBHOOK_URL": "https://mattermost.example/hooks/test",
    "TEAM_REGION": "APAC",
}


def make_config(**overrides):
    return Config.from_env(BASE_ENV | overrides)


def make_issue(
    key, status, assignee="Ada", comments=None, category=None, issue_type="Task"
):
    status_data = {"name": status}
    if category:
        status_data["statusCategory"] = {"key": category}
    return {
        "key": key,
        "fields": {
            "summary": f"Summary for {key}",
            "description": "Ticket context",
            "assignee": {"displayName": assignee} if assignee else None,
            "status": status_data,
            "issuetype": {"name": issue_type},
            "comment": {"comments": comments or []},
        },
    }


class ConfigTests(unittest.TestCase):
    def test_reports_all_missing_environment_variables(self):
        with self.assertRaisesRegex(
            EODReportError,
            "JIRA_DOMAIN, JIRA_EMAIL, JIRA_API_TOKEN, MATTERMOST_WEBHOOK_URL",
        ):
            Config.from_env({})

    def test_uses_region_specific_project_and_custom_statuses(self):
        config = make_config(
            JIRA_PROJECT_APAC="APAC-SW",
            BLOCKED_STATUSES="Blocked, Waiting",
        )

        self.assertEqual(config.jira_base_url, "https://example.atlassian.net")
        self.assertEqual(config.jira_project, "APAC-SW")
        self.assertIsNone(config.jira_filter)
        self.assertIsNone(config.jira_team_field)
        self.assertIsNone(config.jira_team_value)
        self.assertEqual(config.blocked_statuses, {"blocked", "waiting"})

    def test_empty_actions_project_variable_falls_back_to_region(self):
        config = make_config(JIRA_PROJECT_APAC="")

        self.assertEqual(config.jira_project, "APAC")

    def test_builds_shared_project_team_query(self):
        config = make_config(
            JIRA_PROJECT_APAC="ENG",
            JIRA_TEAM_FIELD="Team",
            JIRA_TEAM_APAC="Infrastructure APAC",
        )

        self.assertIn('project = "ENG"', build_jql(config))
        self.assertIn('"Team" = "Infrastructure APAC"', build_jql(config))
        self.assertIn('statusCategory != "To Do"', build_jql(config))

    def test_saved_filter_takes_precedence_over_team_field(self):
        config = make_config(
            JIRA_PROJECT_APAC="ENG",
            JIRA_FILTER_APAC="APAC delivery board",
            JIRA_TEAM_FIELD="Team",
            JIRA_TEAM_APAC="Platform Engineering - APAC",
        )

        jql = build_jql(config)
        self.assertIn('project = "ENG"', jql)
        self.assertIn('filter = "APAC delivery board"', jql)
        self.assertNotIn('"Team" =', jql)

    def test_requires_team_field_and_value_together(self):
        with self.assertRaisesRegex(EODReportError, "must both be set"):
            make_config(JIRA_TEAM_APAC="Platform Engineering - APAC")

    def test_allows_ai_without_key_for_runtime_fallback(self):
        config = make_config(AI_SUMMARIZE="true")

        self.assertTrue(config.ai_summarize)
        self.assertIsNone(config.openrouter_api_key)

    def test_uses_structured_output_model_by_default(self):
        config = make_config()

        self.assertEqual(config.openrouter_model, "google/gemini-3.7-flash")
        self.assertEqual(config.openrouter_max_tokens, 2048)

    def test_rejects_invalid_openrouter_token_limit(self):
        with self.assertRaisesRegex(EODReportError, "between 256 and 8192"):
            make_config(OPENROUTER_MAX_TOKENS="20000")

    def test_accepts_arbitrary_team_name(self):
        config = make_config(
            TEAM_REGION="platform",
            TEAM_NAME="Platform Engineering",
            JIRA_PROJECT="ENG",
        )

        self.assertEqual(config.team_region, "Platform Engineering")


class CommentTests(unittest.TestCase):
    NOW = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)

    def test_returns_newest_recent_comment_and_flattens_adf(self):
        comments = {
            "comments": [
                {
                    "created": "2026-08-14T09:00:00.000+0000",
                    "body": "Earlier update",
                },
                {
                    "created": "2026-08-14T11:00:00.000Z",
                    "body": {
                        "type": "doc",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "Waiting on"},
                                    {
                                        "type": "mention",
                                        "attrs": {"text": "@Platform"},
                                    },
                                ],
                            }
                        ],
                    },
                },
            ]
        }

        self.assertEqual(
            get_recent_comment(comments, self.NOW), "Waiting on @Platform"
        )

    def test_ignores_comments_older_than_24_hours(self):
        comments = {
            "comments": [
                {
                    "created": "2026-08-13T11:59:59+00:00",
                    "body": "Too old",
                }
            ]
        }

        self.assertIsNone(get_recent_comment(comments, self.NOW))

    def test_returns_latest_comment_even_when_older_than_24_hours(self):
        comments = {
            "comments": [
                {
                    "created": "2026-08-10T11:00:00+00:00",
                    "body": "Rejected because the deployment path is unsupported.",
                }
            ]
        }

        self.assertEqual(
            get_latest_comment(comments),
            "Rejected because the deployment path is unsupported.",
        )

    def test_preserves_jira_smart_link_urls(self):
        body = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "This should be done with"},
                        {
                            "type": "inlineCard",
                            "attrs": {
                                "url": "https://example.atlassian.net/browse/ENG-9999"
                            },
                        },
                    ],
                }
            ],
        }

        self.assertEqual(
            comment_body_to_text(body),
            "This should be done with https://example.atlassian.net/browse/ENG-9999",
        )


class JiraTests(unittest.TestCase):
    def test_fetches_all_pages(self):
        first = Mock()
        first.raise_for_status.return_value = None
        first.json.return_value = {
            "issues": [make_issue("APAC-1", "Done")],
            "nextPageToken": "next-page",
        }
        second = Mock()
        second.raise_for_status.return_value = None
        second.json.return_value = {
            "issues": [make_issue("APAC-2", "Done")],
        }
        session = Mock()
        session.get.side_effect = [first, second]

        issues = fetch_jira_tickets(make_config(), session)

        self.assertEqual([issue["key"] for issue in issues], ["APAC-1", "APAC-2"])
        self.assertEqual(session.get.call_count, 2)
        self.assertTrue(
            session.get.call_args_list[0].args[0].endswith("/rest/api/3/search/jql")
        )
        self.assertNotIn(
            "nextPageToken", session.get.call_args_list[0].kwargs["params"]
        )
        self.assertEqual(
            session.get.call_args_list[1].kwargs["params"]["nextPageToken"],
            "next-page",
        )

    def test_wraps_request_errors(self):
        session = Mock()
        session.get.side_effect = requests.Timeout("timed out")

        with self.assertRaisesRegex(EODReportError, "Failed to fetch"):
            fetch_jira_tickets(make_config(), session)

    def test_filters_to_recent_comments_or_status_changes(self):
        recent_comment = {
            "created": datetime.now(timezone.utc).isoformat(),
            "body": "Implementation completed",
        }
        status_response = Mock()
        status_response.raise_for_status.return_value = None
        status_response.json.return_value = {
            "values": [
                {
                    "created": datetime.now(timezone.utc).isoformat(),
                    "items": [
                        {
                            "fieldId": "status",
                            "fromString": "In Progress",
                            "toString": "Done",
                        }
                    ],
                }
            ],
            "total": 1,
        }
        no_change_response = Mock()
        no_change_response.raise_for_status.return_value = None
        no_change_response.json.return_value = {"values": [], "total": 0}
        session = Mock()
        session.get.side_effect = [status_response, no_change_response]
        issues = [
            make_issue("APAC-1", "In Progress", comments=[recent_comment]),
            make_issue("APAC-2", "Done"),
            make_issue("APAC-3", "In Progress"),
        ]

        active = filter_issues_with_recent_activity(
            issues, make_config(), session
        )

        self.assertEqual([issue["key"] for issue in active], ["APAC-1", "APAC-2"])
        self.assertEqual(
            active[1]["_eod_status_change"],
            "Status changed from In Progress to Done.",
        )
        self.assertEqual(session.get.call_count, 2)

    def test_tracks_current_continuous_blocked_period(self):
        now = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
        recent_comment = {
            "created": now.isoformat(),
            "body": "Still waiting for approval",
        }
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "values": [
                {
                    "created": "2026-08-20T09:00:00+00:00",
                    "items": [
                        {
                            "fieldId": "status",
                            "fromString": "In Progress",
                            "toString": "Blocked",
                        }
                    ],
                },
                {
                    "created": "2026-08-21T09:00:00+00:00",
                    "items": [
                        {
                            "fieldId": "status",
                            "fromString": "Blocked",
                            "toString": "In Progress",
                        }
                    ],
                },
                {
                    "created": "2026-08-23T09:00:00+00:00",
                    "items": [
                        {
                            "fieldId": "status",
                            "fromString": "In Progress",
                            "toString": "Blocked",
                        }
                    ],
                },
            ],
            "total": 3,
        }
        session = Mock()
        session.get.return_value = response

        active = filter_issues_with_recent_activity(
            [make_issue("APAC-1", "Blocked", comments=[recent_comment])],
            make_config(),
            session,
            now,
        )

        self.assertEqual(active[0]["_eod_blocked_duration"], "2 days 6 hours")


class ReportTests(unittest.TestCase):
    def test_groups_and_classifies_issues(self):
        recent_comment = {
            "created": datetime.now(timezone.utc).isoformat(),
            "body": "Implementation started",
        }
        issues = [
            make_issue("APAC-3", "Done", "Grace"),
            make_issue("APAC-2", "To Be Deployed", "Ada"),
            make_issue("APAC-1", "Blocked", "Ada"),
            make_issue("APAC-4", "Code Review", None, [recent_comment]),
            make_issue("APAC-5", "Code Review", None),
            make_issue("APAC-6", "Selected for Development", "Ada", category="new"),
        ]

        report = parse_and_format(issues, make_config())

        self.assertIn("**APAC Team**", report)
        self.assertLess(report.index("### 👤 Ada"), report.index("### 👤 Grace"))
        self.assertLess(report.index("**BLOCKED:**"), report.index("**TO BE DEPLOYED:**"))
        self.assertIn("No recent comment logged explaining the blocker.", report)
        self.assertIn("IN PROGRESS (Code Review)", report)
        self.assertIn("### 👤 Unassigned", report)
        self.assertIn("APAC-4", report)
        self.assertNotIn("APAC-5", report)
        self.assertNotIn("APAC-6", report)

    def test_uses_intelligent_update_and_blocker_reason(self):
        issues = [
            make_issue("APAC-1", "Blocked", "Ada"),
            make_issue("APAC-2", "In Progress", "Ada"),
        ]
        updates = {
            "APAC-1": AIUpdate(
                "Implementation is paused.", "Waiting for firewall approval."
            ),
            "APAC-2": AIUpdate("API integration is complete.", ""),
        }

        report = parse_and_format(issues, make_config(), updates)

        self.assertIn("Waiting for firewall approval.", report)
        self.assertIn("Implementation is paused.", report)
        self.assertIn("API integration is complete.", report)

    def test_formats_empty_report(self):
        report = parse_and_format([], make_config())

        self.assertIn("No ticket activity or updates logged today.", report)

    def test_groups_recently_active_issues_by_status(self):
        recent_comment = {
            "created": datetime.now(timezone.utc).isoformat(),
            "body": "Deployment validation completed",
        }
        issues = [
            make_issue("APAC-1", "Done", "Ada", [recent_comment]),
            make_issue("APAC-2", "Blocked", "Grace", [recent_comment]),
            make_issue("APAC-3", "To Be Deployed", "Lin", [recent_comment]),
            make_issue("APAC-4", "Code Review", "Sam", [recent_comment]),
            make_issue("APAC-5", "Rejected", "Sam", [recent_comment]),
            make_issue("APAC-6", "Code Review", "Sam"),
        ]
        updates = {
            key: AIUpdate(f"Intelligent update for {key}.", "")
            for key in ("APAC-1", "APAC-2", "APAC-3", "APAC-4", "APAC-5")
        }

        report = parse_and_format_by_status(issues, make_config(), updates)

        self.assertLess(report.index("### 🟢 Done"), report.index("### 🔴 Blocked"))
        self.assertLess(
            report.index("### 🔴 Blocked"), report.index("### 🚀 In Deployment")
        )
        self.assertLess(
            report.index("### 🚀 In Deployment"), report.index("### ⛔ Rejected")
        )
        self.assertLess(
            report.index("### ⛔ Rejected"), report.index("### 🔵 In Review")
        )
        self.assertNotIn("### 🟡 In Progress", report)
        self.assertIn("APAC-4", report[report.index("### 🔵 In Review") :])
        self.assertIn("Assignee: Ada", report)
        self.assertIn("Intelligent update for APAC-1.", report)
        self.assertIn("Rejection reason:", report)
        self.assertNotIn("APAC-6", report)

    def test_status_only_activity_has_no_update_line(self):
        issue = make_issue("APAC-1", "Done", "Ada")
        issue["_eod_status_change"] = "Status changed from In Progress to Done."

        report = parse_and_format_by_status([issue], make_config())

        self.assertIn("[APAC-1](https://example.atlassian.net/browse/APAC-1)", report)
        self.assertIn("Assignee: Ada", report)
        self.assertNotIn("*Update:*", report)
        self.assertNotIn("Status changed", report)

    def test_excludes_epics_only_from_in_progress(self):
        recent_comment = {
            "created": datetime.now(timezone.utc).isoformat(),
            "body": "Epic planning updated",
        }
        issues = [
            make_issue(
                "APAC-1",
                "In Progress",
                comments=[recent_comment],
                issue_type="Epic",
            ),
            make_issue(
                "APAC-2",
                "Done",
                comments=[recent_comment],
                issue_type="Epic",
            ),
        ]

        report = parse_and_format_by_status(issues, make_config())

        self.assertNotIn("APAC-1", report)
        self.assertIn("APAC-2", report)

    def test_orders_tickets_with_updates_before_status_only_tickets(self):
        recent_comment = {
            "created": datetime.now(timezone.utc).isoformat(),
            "body": "Implementation completed",
        }
        with_update = make_issue("APAC-9", "Done", comments=[recent_comment])
        status_only = make_issue("APAC-1", "Done")
        status_only["_eod_status_change"] = "Status changed to Done."

        report = parse_and_format_by_status(
            [status_only, with_update], make_config()
        )

        self.assertLess(report.index("APAC-9"), report.index("APAC-1"))

    def test_formats_current_continuous_blocked_duration(self):
        recent_comment = {
            "created": datetime.now(timezone.utc).isoformat(),
            "body": "Waiting for approval",
        }
        issue = make_issue("APAC-1", "Blocked", "Ada", [recent_comment])
        issue["_eod_blocked_duration"] = "2 days 6 hours"

        report = parse_and_format_by_status([issue], make_config())

        self.assertIn("*Blocked for:* 2 days 6 hours", report)

    def test_rejected_ticket_without_ai_has_reason_fallback(self):
        issue = make_issue("APAC-1", "Rejected", "Ada")
        issue["_eod_status_change"] = "Status changed from Review to Rejected."

        report = parse_and_format_by_status([issue], make_config())

        self.assertIn("### ⛔ Rejected", report)
        self.assertIn("No rejection reason recorded.", report)
        self.assertNotIn("### 🟡 In Progress", report)

    def test_rejected_ticket_replaces_incomplete_reason_fragment(self):
        recent_comment = {
            "created": datetime.now(timezone.utc).isoformat(),
            "body": "This should be done with",
        }
        issue = make_issue("APAC-1", "Rejected", "Ada", [recent_comment])
        updates = {"APAC-1": AIUpdate("This should be done with", "")}

        report = parse_and_format_by_status([issue], make_config(), updates)

        self.assertIn("No rejection reason recorded.", report)
        self.assertNotIn("Rejection reason:* This should be done with", report)

    def test_rejected_ticket_uses_explicit_comment_when_ai_is_unavailable(self):
        recent_comment = {
            "created": datetime.now(timezone.utc).isoformat(),
            "body": (
                "This should be done with "
                "https://example.atlassian.net/browse/ENG-9999"
            ),
        }
        issue = make_issue("APAC-1", "Rejected", "Ada", [recent_comment])

        report = parse_and_format_by_status([issue], make_config())

        self.assertIn(
            "Rejection reason:* This should be done with "
            "https://example.atlassian.net/browse/ENG-9999",
            report,
        )


class MattermostTests(unittest.TestCase):
    def test_posts_expected_payload(self):
        response = Mock()
        response.raise_for_status.return_value = None
        session = Mock()
        session.post.return_value = response
        config = make_config()

        send_to_mattermost("report", config, session)

        session.post.assert_called_once_with(
            config.mattermost_webhook_url,
            json={
                "text": "report",
                "username": "Jira EOD Reporter",
                "icon_emoji": "clipboard",
            },
            timeout=30,
        )

    def test_does_not_expose_webhook_url_in_errors(self):
        response = Mock()
        response.status_code = 500
        error = requests.HTTPError(
            "500 Server Error for url: "
            "https://mattermost.example/hooks/test",
            response=response,
        )
        session = Mock()
        session.post.side_effect = error

        with self.assertRaises(EODReportError) as raised:
            send_to_mattermost("report", make_config(), session)

        self.assertEqual(
            str(raised.exception),
            "Failed to post report to Mattermost: HTTP 500",
        )
        self.assertNotIn("/hooks/test", str(raised.exception))


class OpenRouterTests(unittest.TestCase):
    def test_retries_with_affordable_credit_limit(self):
        credit_response = Mock()
        credit_response.status_code = 402
        credit_response.json.return_value = {
            "error": {
                "message": (
                    "You requested up to 2048 tokens, but can only afford 1876."
                )
            }
        }
        first = Mock()
        first.raise_for_status.side_effect = requests.HTTPError(
            "402 Client Error", response=credit_response
        )
        second = Mock()
        second.raise_for_status.return_value = None
        session = Mock()
        session.post.side_effect = [first, second]

        response = post_openrouter_with_credit_retry(
            session,
            {"Authorization": "Bearer sk-or-test"},
            {"model": "test", "max_tokens": 2048},
        )

        self.assertIs(response, second)
        self.assertEqual(
            session.post.call_args_list[1].kwargs["json"]["max_tokens"], 1876
        )

    def test_generates_structured_updates(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "issues": [
                                    {
                                        "key": "APAC-1",
                                        "update": "Authentication work is complete.",
                                        "blocker_reason": "",
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        }
        session = Mock()
        session.post.return_value = response
        config = make_config(
            AI_SUMMARIZE="true",
            OPENROUTER_API_KEY="sk-or-test",
            OPENROUTER_MODEL="openai/gpt-5.2",
        )

        updates = generate_ai_updates(
            [make_issue("APAC-1", "In Progress")], config, session
        )

        self.assertEqual(updates["APAC-1"].update, "Authentication work is complete.")
        request = session.post.call_args
        self.assertEqual(request.args[0], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(
            request.kwargs["headers"]["Authorization"], "Bearer sk-or-test"
        )
        self.assertEqual(request.kwargs["json"]["response_format"]["type"], "json_schema")
        self.assertEqual(request.kwargs["json"]["max_tokens"], 2048)
        self.assertEqual(
            request.kwargs["json"]["reasoning"],
            {"effort": "minimal", "exclude": True},
        )
        self.assertEqual(
            request.kwargs["json"]["plugins"], [{"id": "response-healing"}]
        )
        self.assertNotIn("provider", request.kwargs["json"])
        prompt = request.kwargs["json"]["messages"][1]["content"]
        self.assertIn("at most 20 words", prompt)
        self.assertIn("Do not repeat the ticket key or title", prompt)

    def test_does_not_enrich_status_only_done_ticket(self):
        session = Mock()
        config = make_config(
            AI_SUMMARIZE="true", OPENROUTER_API_KEY="sk-or-test"
        )
        done_issue = make_issue("APAC-1", "Done")
        done_issue["_eod_status_change"] = "Status changed from Review to Done."

        updates = generate_ai_updates(
            [done_issue], config, session, include_all_started=True
        )

        self.assertEqual(updates, {})
        session.post.assert_not_called()

    def test_rejects_missing_issue_results(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": '{"issues": []}'}}]
        }
        session = Mock()
        session.post.return_value = response
        config = make_config(
            AI_SUMMARIZE="true", OPENROUTER_API_KEY="sk-or-test"
        )

        with self.assertRaisesRegex(EODReportError, "every requested issue"):
            generate_ai_updates([make_issue("APAC-1", "Blocked")], config, session)

    def test_surfaces_openrouter_error_message(self):
        response = Mock()
        response.status_code = 404
        response.json.return_value = {
            "error": {"message": "No endpoints found for the selected model"}
        }
        session = Mock()
        session.post.side_effect = requests.HTTPError(
            "404 Client Error", response=response
        )
        config = make_config(
            AI_SUMMARIZE="true", OPENROUTER_API_KEY="sk-or-test"
        )

        with self.assertRaisesRegex(
            EODReportError, "No endpoints found for the selected model"
        ):
            generate_ai_updates([make_issue("APAC-1", "Blocked")], config, session)

    def test_reports_truncated_response(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": '{"issues": ['},
                }
            ]
        }
        session = Mock()
        session.post.return_value = response
        config = make_config(
            AI_SUMMARIZE="true", OPENROUTER_API_KEY="sk-or-test"
        )

        with self.assertRaisesRegex(EODReportError, "response was truncated"):
            generate_ai_updates([make_issue("APAC-1", "Blocked")], config, session)


if __name__ == "__main__":
    unittest.main()
