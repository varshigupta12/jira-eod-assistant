# Jira EOD Reporter

Serverless Jira status reporting for Mattermost. GitHub Actions collects recent
work from any number of Jira teams, creates concise updates, and posts:

- daily EOD reports grouped by Epic in Format C;
- sprint-end highlights grouped by region and Epic in Format C.

No Jira administrator access or hosted infrastructure is required. For live
use, deploy from a private fork because Actions logs can reveal operational
metadata when external API requests fail.

## Features

- Any number of teams, projects, saved filters, and Scrum boards
- One or multiple boards per team
- Team-local schedules with daylight-saving support
- Jira Cloud REST API authentication using a personal API token
- Mattermost incoming-webhook delivery
- Atlassian Document Format comment support
- Optional OpenRouter summaries grounded in Jira descriptions and comments
- Configurable sprint-report title, cadence, timezone, and status names
- Manual GitHub Actions runs for safe setup testing

## How it works

```text
GitHub Actions
    |
    +-- daily-runner check
    |      +-- teams due in their local timezone
    |      +-- Jira saved filter/project query
    |      +-- optional OpenRouter summary
    |      `-- Mattermost EOD post
    |
    `-- sprint-runner check
           +-- teams and boards from report-config.yml
           +-- active/recent sprint issues
           +-- AI-selected done/blocked/carryover highlights
           `-- combined Mattermost sprint post
```

The public source workflows are manual-only by default. Configure scheduling in
a private deployment fork or an external scheduler so private Jira metadata is
not exposed through public Actions logs.

## Quickstart

### 1. Fork or clone the repository

Edit [`report-config.yml`](report-config.yml) for your teams. The committed file
contains a three-team example.

### 2. Create credentials

1. Create an [Atlassian API token](https://id.atlassian.com/manage-profile/security/api-tokens).
2. Create a Mattermost incoming webhook for the target channel.
3. For intelligent summaries and highlight selection, create an
   [OpenRouter API key](https://openrouter.ai/settings/keys).

### 3. Add GitHub Actions secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Required | Description |
| --- | --- | --- |
| `JIRA_DOMAIN` | Yes | Jira host, for example `company.atlassian.net` |
| `JIRA_EMAIL` | Yes | Atlassian account email associated with the token |
| `JIRA_API_TOKEN` | Yes | Atlassian personal API token |
| `MATTERMOST_WEBHOOK_URL` | Yes | Mattermost incoming-webhook URL |
| `OPENROUTER_API_KEY` | Optional | OpenRouter API key; raw Jira text is used when absent or unavailable |

No repository variables are required. Non-secret behavior lives in
`report-config.yml`.

### 4. Test manually

In **Actions**:

- Run **Jira EOD Daily Report**. Leave `team_id` blank to run every team with a
  daily schedule, or enter one configured team ID. Enable `dry_run` to inspect
  the   active-sprint result count without posting to Mattermost. Daily reports
  use Format C exclusively: Epic progress headings followed by compact Done,
  Blocked, In Review, and In Progress ticket lines.
- Run **Sprint Highlights Report** to bypass the cadence check and post a
  combined region- and Epic-grouped Format C report immediately.

Add scheduling only in the private deployment repository after testing.

## Configuration

`report-config.yml` is the only non-secret configuration file.

### Minimal single-team setup

```yaml
version: 1

teams:
  - id: platform
    name: Platform
    projects: [PLAT]
    boards: [123]
    daily:
      time: "17:00"
      timezone: America/New_York

ai:
  enabled: false

pulse:
  enabled: false
```

### Multiple teams and boards

```yaml
version: 1

teams:
  - id: backend
    name: Backend
    projects: [ENG]
    filters: ["Backend delivery board"]
    boards: [101, 102]
    daily:
      time: "17:00"
      timezone: Europe/London
      weekdays: [monday, tuesday, wednesday, thursday, friday]

  - id: mobile
    name: Mobile
    projects: [IOS, ANDROID]
    filters: ["Mobile sprint filter"]
    boards: [201]
    daily:
      time: "18:00"
      timezone: Asia/Kolkata

ai:
  enabled: true
  model: google/gemini-3.7-flash
  max_tokens: 2048

pulse:
  enabled: true
  title: Engineering Sprint Pulse
  timezone: America/New_York
  weekday: friday
  time: "20:00"
  cadence_days: 14
  anchor_date: "2026-08-14"

blocked_statuses: [Blocked, Impediment, On Hold]
deploy_statuses: [Ready for Deployment, To Be Deployed]
done_statuses: [Done, Closed, Resolved]
review_statuses: [In Review, Code Review]
```

### Team fields

| Field | Required | Description |
| --- | --- | --- |
| `id` | Yes | Unique stable ID used by manual workflow runs |
| `name` | Yes | Display heading in reports |
| `projects` | For project filtering | One project key or a list |
| `filters` | For saved-filter filtering | One Jira saved-filter name/ID or a list |
| `boards` | For sprint reports | One board ID or a list |
| `daily` | No | Local time, IANA timezone, and optional weekdays |
| `include_in_pulse` | No | Defaults to `true`; set `false` to omit the team |
| `team_field` / `team_value` | No | Alternative when a Jira Team field is queryable |

When projects and filters are both present, issues must match both. Multiple
values within either list are ORed. Schedule times must be on the hour.

Find a board ID in its URL: `.../jira/software/c/projects/KEY/boards/123`.

### Daily report behavior

Daily reports query tickets updated or resolved in the previous 24 hours and
exclude Jira's **To Do** status category. They include:

- done, blocked, deployment-ready, and in-progress work;
- recent progress comments;
- explicit blocker reasons;
- unassigned work only when a recent progress comment exists.

AI summaries are limited to concise, factual statements and validated as
structured JSON before posting. If OpenRouter is unavailable, out of credits,
rate-limited, or returns malformed output, the report still posts using exact
recent Jira comments. Jira and Mattermost errors remain fatal.

### Sprint report behavior

The sprint workflow:

1. Selects the active or recently completed sprint ending closest to report
   time for every configured board.
2. Deduplicates issues when a team has multiple boards.
3. Classifies issues as done, blocked, or carryover.
4. Uses OpenRouter to select only material highlights.
5. Posts one report with sections in the same order as `teams`.

If OpenRouter fails, the sprint report falls back to each ticket's latest Jira
comment, or its summary when no comment exists.

`anchor_date` must be a date when a report should run. `cadence_days: 14`
creates an alternate-Friday schedule. Manual runs ignore the cadence.

## Run locally

Python 3.11 or newer is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.txt

export JIRA_DOMAIN="company.atlassian.net"
export JIRA_EMAIL="you@company.com"
export JIRA_API_TOKEN="..."
export MATTERMOST_WEBHOOK_URL="https://mattermost.example/hooks/..."
export OPENROUTER_API_KEY="..." # only when required
```

Run all configured daily teams:

```bash
DAILY_FORCE_RUN=true python daily_runner.py
```

Run one team:

```bash
DAILY_FORCE_RUN=true TEAM_ID=services python daily_runner.py
```

Run the sprint report:

```bash
PULSE_FORCE_RUN=true python pulse_report.py
```

Use another configuration file with `REPORT_CONFIG=/path/to/config.yml`.

## Permissions

The Jira account needs permission to:

- browse configured projects and issues;
- view configured saved filters and boards;
- view comments.

The reporter does not modify Jira data.

## Security and privacy

- Never commit `.env` files, API tokens, or webhook URLs.
- Use a dedicated Atlassian account with least-privilege project access when
  possible.
- Jira summaries, descriptions, statuses, and recent comments are sent to the
  selected OpenRouter model when AI is enabled.
- Review your organization's data-handling requirements before enabling AI.
- GitHub Actions secrets are masked and are not passed to pull requests from
  forks.

## Troubleshooting

**No Jira issues appear**

- Run the configured project/filter JQL directly in Jira.
- Confirm the API user can view the saved filter.
- Daily reports intentionally exclude the To Do status category.

**Jira returns HTTP 410**

The reporter uses Jira's enhanced `/rest/api/3/search/jql` endpoint. Update your
fork if an older version still uses `/rest/api/3/search`.

**OpenRouter returns 402**

Add credits, choose a less expensive model, or lower `ai.max_tokens`. Keep it
high enough to return valid JSON for the number of issues.

**No sprint is found**

Confirm the board is a Scrum board and has an active or completed sprint ending
within seven days of the run.

## Development

```bash
python -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance.

## License

MIT
