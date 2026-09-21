# Jira EOD Reporter

Serverless Jira status reporting for Mattermost. GitHub Actions collects recent
work from any number of Jira teams, creates concise updates, and posts:

- daily EOD reports grouped by Epic, status, or assignee;
- sprint-end highlights grouped by region and Epic in Format C;
- current-release blockers grouped by team and ordered by blocked duration;
- a private, static cross-squad delivery dashboard with release filters.

No Jira administrator access or hosted infrastructure is required. For live
use, deploy from a private fork because Actions logs can reveal operational
metadata when external API requests fail.

## Features

- Any number of teams, projects, saved filters, and Scrum boards
- One or multiple boards per team
- Team-local schedules with daylight-saving support
- Per-team Epic, status, or assignee report formats with manual overrides
- Jira Cloud REST API authentication using a personal API token
- Mattermost incoming-webhook delivery
- Atlassian Document Format comment support
- Optional activity from GitHub pull requests formally linked to Jira issues
- Optional OpenRouter summaries grounded in Jira descriptions and comments
- Configurable sprint-report title, cadence, timezone, and status names
- Release matching through either Jira labels or Fix Version/s
- Changelog-derived delivery metrics Jira cannot calculate natively
- JavaScript-free HTML dashboard with Jira links and release tabs
- Manual GitHub Actions runs for safe setup testing

## How it works

```text
GitHub Actions
    |
    +-- daily-runner check
    |      +-- teams due in their local timezone
    |      +-- Jira board, saved-filter, or project query
    |      +-- optional Jira-linked GitHub PR and commit activity
    |      +-- optional OpenRouter summary
    |      `-- Mattermost EOD post
    |
    +-- sprint-runner check
           +-- teams and boards from report-config.yml
           +-- active/recent sprint issues
           +-- AI-selected done/blocked/carryover highlights
           +-- combined Mattermost sprint post
           `-- optional current-release blocker post
    |
    `-- delivery-dashboard run
           +-- changelog-derived squad metrics
           +-- retained trend snapshots
           `-- private HTML artifact
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
4. To include linked GitHub activity, enable `source_control` and provide a
   GitHub token. The built-in Actions token is sufficient for public
   repositories; a separate read-only token is optional for private
   repositories.

### 3. Add GitHub Actions secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Required | Description |
| --- | --- | --- |
| `JIRA_DOMAIN` | Yes | Jira host, for example `company.atlassian.net` |
| `JIRA_EMAIL` | Yes | Atlassian account email associated with the token |
| `JIRA_API_TOKEN` | Yes | Atlassian personal API token |
| `MATTERMOST_WEBHOOK_URL` | Yes | Mattermost incoming-webhook URL |
| `OPENROUTER_API_KEY` | Optional | OpenRouter API key; raw Jira text is used when absent or unavailable |
| `SCM_GITHUB_TOKEN` | When source control is enabled | GitHub token used only for Jira-linked PRs; the workflow falls back to `github.token` |

No repository variables are required. Non-secret behavior lives in
`report-config.yml`.

### 4. Test manually

In **Actions**:

- Run **Jira EOD Daily Report**. Leave `team_id` blank to run every team with a
  daily schedule, or enter one configured team ID. Enable `dry_run` to inspect
  the aggregate result count without posting to Mattermost. Leave
  `report_format` set to `configured` to use each team's YAML setting, or select
  `epic`, `status`, or `assignee` as a one-run override.
- Run **Sprint Highlights Report** with `full` to post the sprint report followed
  by current-release blockers, or use `blocked-only` to post only the blocker
  report.
- In a private fork, run **Delivery Metrics Dashboard**, download the
  `delivery-dashboard` artifact, and open `delivery-dashboard.html`.

Add scheduling only in the private deployment repository after testing.

## Configuration

`report-config.yml` is the only non-secret configuration file.

### Minimal single-team setup

```yaml
version: 1

source_control:
  enabled: false

teams:
  - id: platform
    name: Platform
    projects: [PLAT]
    boards: [123]
    daily:
      format: epic
      time: "17:00"
      timezone: America/New_York

ai:
  enabled: false

pulse:
  enabled: false

release_blockers:
  enabled: false

delivery_metrics:
  enabled: false
```

### Multiple teams and boards

```yaml
version: 1

source_control:
  enabled: true
  github_organization: acme

teams:
  - id: backend
    name: Backend
    projects: [ENG]
    filters: ["Backend delivery board"]
    boards: [101, 102]
    daily:
      format: status
      time: "17:00"
      timezone: Europe/London
      weekdays: [monday, tuesday, wednesday, thursday, friday]

  - id: mobile
    name: Mobile
    projects: [IOS, ANDROID]
    filters: ["Mobile sprint filter"]
    boards: [201]
    daily:
      format: assignee
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

release_blockers:
  enabled: true
  label: "2026.1"

delivery_metrics:
  enabled: true
  lookback_days: 30
  aging_wip_days: 5
  snapshot_dir: metrics
  dashboard_path: delivery-dashboard.html
  sprint_field: customfield_10020

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
| `release_labels` | No | Stable squad labels used to scope dashboard releases |
| `release_scope_jql` | No | Custom ownership predicate for every release; overrides `release_labels` |
| `boards` | For sprint reports | One board ID or a list |
| `daily` | No | Format, local time, IANA timezone, and optional weekdays |
| `include_in_pulse` | No | Defaults to `true`; set `false` to omit the team |
| `team_field` / `team_value` | No | Alternative when a Jira Team field is queryable |

When projects and filters are both present, issues must match both. Multiple
values within either list are ORed. Schedule times must be on the hour.

### Daily report formats

Set `daily.format` independently for each team:

| Value | Output | Required team configuration |
| --- | --- | --- |
| `epic` | Epic progress followed by status-ordered ticket updates | `boards` |
| `status` | Tickets grouped by Blocked, In Progress, In Review, In Deployment, and Done | `projects`, `filters`, or a Team-field mapping |
| `assignee` | Tickets grouped by Jira assignee | `projects`, `filters`, or a Team-field mapping |

`epic` is the default when `daily.format` is omitted. Invalid combinations fail
during configuration loading with a team-specific error. The workflow's
`configured` option honors these defaults; the other options override the
format for only that manual run.

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
recent Jira comments or linked source activity. Jira and Mattermost errors
remain fatal, except linked-activity lookups: those warn and continue with the
Jira-only report.

### Jira-linked GitHub activity

Source-control enrichment is opt-in and organization-scoped:

```yaml
source_control:
  enabled: true
  github_organization: acme
```

For each active-sprint Jira issue, the reporter first checks Jira development
data at `/rest/dev-status/1.0/issue/detail`. If Jira returns no pull-request
URLs there, it checks the issue's standard remote links. It continues to GitHub
only for an exact link shaped like
`https://github.com/acme/repository/pull/123`; it never searches GitHub by Jira
key or across the organization. Issues without a formally linked, in-scope PR
make no GitHub API requests and retain Jira-only behavior.

For each accepted PR, the reporter reads PR details and all paginated commits,
reuses a PR shared by multiple Jira issues, deduplicates source URLs, and keeps
only activity from the previous 24 hours. Recent linked activity can make a
ticket appear even when no Jira comment was added. Format C shows the latest
raw PR or commit text when Jira has no recent comment and appends up to three
compact source links.

When AI summaries are enabled, linked PR and commit facts are included in the
structured OpenRouter context. The prompt requires concrete implementation
detail, comparison with Jira facts, and prohibits treating code or PR existence
alone as proof of completion.

### Sprint report behavior

The sprint workflow:

1. Selects the active or recently completed sprint ending closest to report
   time for every configured board.
2. Deduplicates issues when a team has multiple boards.
3. Classifies issues as done, blocked, or carryover.
4. Uses OpenRouter to select only material highlights.
5. Posts one report with sections in the same order as `teams`.
6. When enabled, posts a separate current-release blocker report afterward.

If OpenRouter fails, the sprint report falls back to each ticket's latest Jira
comment, or its summary when no comment exists.

`anchor_date` must be a date when a report should run. `cadence_days: 14`
creates an alternate-Friday schedule. Manual runs ignore the cadence.

### Current-release blocker report

Enable `release_blockers` and set `label` to the current release identifier:

```yaml
release_blockers:
  enabled: true
  label: "2026.1"
```

A blocked ticket is included when its current status matches
`blocked_statuses`, it matches a configured team's project/filter criteria, and
either its Jira label or **Fix Version/s** equals the configured release value.
The report:

- includes matching blockers even when they are outside the sprint;
- groups tickets by team and keeps the first team when filters overlap;
- orders each team from longest blocked to shortest blocked;
- shows ticket, summary, assignee, continuous blocked duration, and an explicit
  AI-derived blocker reason when one is available;
- uses compact bullets and splits safely when Mattermost's post limit is reached;
- is skipped when no current-release blockers exist.

Update `release_blockers.label` at each release rollover. The public workflow is
manual-only; a private deployment can schedule `full` mode to post this report
immediately after each sprint pulse.

### Delivery metrics dashboard

The dashboard complements Jira's native burndown, velocity, and control charts
with metrics Jira Cloud does not provide:

| Metric | Meaning |
| --- | --- |
| Blocked duration | Continuous time each currently blocked ticket has been blocked, longest first |
| Median delivery time | Half of completed tickets moved from first active status to done within this time |
| 85th percentile delivery time | 85% of completed tickets moved from first active status to done within this time |
| Throughput | Completed tickets in the selected release, or in the lookback window for Recent work |
| Aging work in progress | In-flight tickets sitting in one non-blocked status beyond the configured threshold |
| Chronic carry-over | In-flight tickets present in at least two sprints |
| Flow efficiency | Percentage of elapsed delivery time spent in active work rather than blocked, review, or deployment queues |

Each squad is shown side by side and then expanded into aging, blocked, and
carry-over ticket lists. Ticket keys link to Jira. Up to six release tabs are discovered from recent **Fix Version/s**, plus the
configured `release_blockers.label`; issues match a tab through either Fix
Version/s or label. Once discovered, each release is queried separately with
no date boundary, so every matching issue is counted. A **Recent work** tab
uses `lookback_days` for a release-independent operational view.

If a saved board filter represents only current work, set `release_labels` on
each team to a durable ownership label. Release queries then use the team's
projects plus those labels instead of the current board filter:

```yaml
teams:
  - id: platform
    name: Platform
    projects: [PLAT]
    filters: ["Current Platform board"]
    release_labels: [platform-team]
```

For boards whose ownership also depends on Jira Team fields or assignees, use a
trusted configuration-only JQL predicate:

```yaml
release_scope_jql: >-
  "Team[Team]" = your-team-id
  OR labels = "platform-team"
  OR assignee in (account-id-1, account-id-2)
```

This predicate replaces the saved filter for every release query, including the
current release. The reporter wraps it in parentheses and still applies the
team's project and selected release constraints.

```yaml
delivery_metrics:
  enabled: true
  lookback_days: 30
  aging_wip_days: 5
  snapshot_dir: metrics
  dashboard_path: delivery-dashboard.html
  sprint_field: customfield_10020
```

Set `sprint_field` to the Sprint custom-field ID for your Jira site. Omit it if
you do not need carry-over; that metric will remain zero. Find the ID through
Jira's fields API or your browser's issue API response.

The collector requests Jira search results with `expand=changelog` to avoid one
API call per issue. Jira may truncate very large issue histories, so metrics for
issues with exceptionally long changelogs can be incomplete.

The generated file contains no JavaScript, external assets, or CDN calls.
Ticket text is HTML-escaped. Release switching uses pre-rendered HTML and CSS,
so the file works offline.

The included workflow runs only when the repository is private. It restores the
previous successful artifact's snapshots, retains 90 days of trend history, and
uploads the HTML and snapshots as a private Actions artifact. Do not publish
this dashboard with ordinary GitHub Pages: a Pages site backed by a private
repository is still public unless the organization uses GitHub Enterprise
Cloud Pages access control.

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
export SCM_GITHUB_TOKEN="..." # only when source_control.enabled is true
```

Run all configured daily teams:

```bash
DAILY_FORCE_RUN=true python daily_runner.py
```

Run one team:

```bash
DAILY_FORCE_RUN=true TEAM_ID=services python daily_runner.py
```

Override that team's configured format for one run:

```bash
DAILY_FORCE_RUN=true TEAM_ID=services REPORT_FORMAT=assignee python daily_runner.py
```

Run the sprint report:

```bash
PULSE_FORCE_RUN=true python pulse_report.py
```

Run only the current-release blocker report:

```bash
PULSE_FORCE_RUN=true PULSE_REPORT_MODE=blocked-only python pulse_report.py
```

Collect Jira metrics and write snapshots:

```bash
python delivery_metrics.py
```

Generate the dashboard with release tabs:

```bash
python dashboard.py --all-releases
```

Render from existing snapshots without querying Jira:

```bash
python dashboard.py --offline --all-releases
```

Render only one release:

```bash
python dashboard.py --release 2026.1
```

Use another configuration file with `REPORT_CONFIG=/path/to/config.yml`.

## Permissions

The Jira account needs permission to:

- browse configured projects and issues;
- view configured saved filters and boards;
- view comments and issue history;
- view labels and Fix Version/s values.

The reporter does not modify Jira data.

## Security and privacy

- Never commit `.env` files, API tokens, or webhook URLs.
- Use a dedicated Atlassian account with least-privilege project access when
  possible.
- Jira summaries, descriptions, statuses, recent comments, and comments from a
  ticket's current blocked period are sent to the selected OpenRouter model when
  AI is enabled.
- Matched GitHub PR titles and recent commit metadata are also sent to the
  selected OpenRouter model when both source control and AI are enabled.
- Review your organization's data-handling requirements before enabling AI.
- GitHub Actions secrets are masked and are not passed to pull requests from
  forks.
- Dashboard HTML and snapshot JSON contain Jira keys, summaries, assignees, and
  derived delivery data. Keep the repository and downloaded artifact private.

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
