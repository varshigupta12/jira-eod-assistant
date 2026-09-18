"""Render a self-contained cross-squad delivery dashboard.

Jira gadgets cannot combine several saved filters into one chart, so this page
exists to put the squads side by side. Output is a single static HTML file with
inline CSS and SVG: no JavaScript, no CDN, and no network access, so it can be
opened straight from a private repository checkout.
"""

from __future__ import annotations

import argparse
import html
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import quote

import requests

from delivery_metrics import (
    MAX_RELEASES,
    TeamMetrics,
    collect_team_snapshot,
    release_options,
    summarize,
)
from eod_report import EODReportError
from report_config import ReportSettings, load_report_config
from snapshot_store import (
    SnapshotError,
    TeamSnapshot,
    load_snapshots,
    write_snapshot,
)

SQUAD_ICONS = {"apac": "🌏", "emea": "🌍", "amer": "🌎"}
MAX_ROWS = 10

CYCLE_P50_HELP = (
    "Half of finished tickets took less than this from first active status "
    "to done."
)
CYCLE_P85_HELP = (
    "85% of finished tickets landed within this time, so it is the number to "
    "quote as a realistic worst case."
)
FLOW_HELP = (
    "Of the total elapsed time on a ticket, the share spent actively worked "
    "rather than waiting in blocked, review or deploy queues."
)
@dataclass(frozen=True)
class TrendPoint:
    captured_date: str
    throughput: int
    blocked: int


@dataclass(frozen=True)
class ReleaseView:
    """One pre-rendered filter tab: a release and the squads scoped to it."""

    release: str | None
    summaries: tuple[TeamMetrics, ...]


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _icon(team_id: str) -> str:
    return SQUAD_ICONS.get(team_id.strip().casefold(), "🧭")


def _number(value: float | None, suffix: str = "") -> str:
    if value is None:
        return '<span class="muted">—</span>'
    return f"{value:g}{suffix}"


def _percent(value: float | None) -> str:
    if value is None:
        return '<span class="muted">—</span>'
    return f"{round(value * 100)}%"


def build_trends(
    snapshots: Sequence[TeamSnapshot], settings: ReportSettings
) -> dict[str, tuple[TrendPoint, ...]]:
    """Reduce stored snapshots to one throughput/blocked point per capture."""
    trends: dict[str, list[TrendPoint]] = {}
    for snapshot in sorted(snapshots, key=lambda item: item.captured_at):
        summary = summarize(snapshot, settings)
        trends.setdefault(snapshot.team_id, []).append(
            TrendPoint(
                captured_date=snapshot.captured_date,
                throughput=summary.throughput,
                blocked=summary.blocked,
            )
        )
    return {team: tuple(points) for team, points in trends.items()}


def _sparkline(points: Sequence[TrendPoint]) -> str:
    """Render a throughput trend as inline SVG."""
    if len(points) < 2:
        return '<span class="muted">Not enough history yet</span>'
    values = [point.throughput for point in points]
    width, height, pad = 220, 44, 4
    highest = max(values)
    lowest = min(values)
    span = (highest - lowest) or 1
    step = (width - 2 * pad) / (len(values) - 1)
    coordinates = [
        (
            pad + index * step,
            height - pad - ((value - lowest) / span) * (height - 2 * pad),
        )
        for index, value in enumerate(values)
    ]
    path = " ".join(
        f"{'M' if index == 0 else 'L'}{x:.1f},{y:.1f}"
        for index, (x, y) in enumerate(coordinates)
    )
    last_x, last_y = coordinates[-1]
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Throughput trend: {_escape(", ".join(str(v) for v in values))}">'
        f'<path d="{path}" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3" fill="currentColor"/>'
        "</svg>"
    )


def _bar(value: float | None, highest: float, tone: str) -> str:
    """Render one horizontal comparison bar."""
    if value is None or highest <= 0:
        return '<span class="muted">—</span>'
    width = max(2.0, (value / highest) * 100)
    return (
        f'<span class="bar {tone}" style="width:{width:.1f}%" '
        f'title="{_escape(value)}"></span>'
    )


def _comparison_table(summaries: Sequence[TeamMetrics]) -> str:
    """The cross-squad view that Jira gadgets cannot produce."""
    rows = []
    slowest = max(
        (item.cycle_time_p85_days or 0 for item in summaries), default=0
    )
    busiest = max((item.throughput for item in summaries), default=0)
    for summary in summaries:
        rows.append(
            "<tr>"
            f'<th scope="row">{_icon(summary.team_id)} {_escape(summary.team_name)}</th>'
            f"<td>{summary.throughput}"
            f'<div class="track">{_bar(summary.throughput, busiest, "good")}</div></td>'
            f"<td>{summary.wip}</td>"
            f"<td>{summary.blocked}</td>"
            f"<td>{_number(summary.cycle_time_median_days, 'd')}</td>"
            f"<td>{_number(summary.cycle_time_p85_days, 'd')}"
            f'<div class="track">'
            f'{_bar(summary.cycle_time_p85_days, slowest, "warn")}</div></td>'
            f"<td>{_percent(summary.flow_efficiency)}</td>"
            "</tr>"
        )
    return (
        '<table class="compare"><thead><tr>'
        "<th>Squad</th>"
        "<th>Completed</th>"
        "<th>In flight</th>"
        "<th>Blocked</th>"
        f'<th><abbr title="{_escape(CYCLE_P50_HELP)}">Median delivery time'
        "</abbr><span class=\"sub\">half finish faster</span></th>"
        f'<th><abbr title="{_escape(CYCLE_P85_HELP)}">85th percentile delivery '
        "time</abbr><span class=\"sub\">realistic worst case</span></th>"
        f'<th><abbr title="{_escape(FLOW_HELP)}">% of time actively worked'
        "</abbr><span class=\"sub\">rest spent waiting</span></th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _ticket_cell(key: str, jira_base_url: str | None) -> str:
    """Ticket key, linked to Jira when a base URL is known."""
    label = _escape(key)
    if not jira_base_url:
        return f'<td class="key">{label}</td>'
    url = f"{jira_base_url.rstrip('/')}/browse/{quote(key, safe='-')}"
    return (
        f'<td class="key"><a href="{_escape(url)}" '
        f'rel="noopener noreferrer" target="_blank">{label}</a></td>'
    )


def _issue_rows(
    issues: Iterable,
    column: str,
    jira_base_url: str | None = None,
    limit: int | None = MAX_ROWS,
) -> str:
    rows = []
    listed = list(issues)
    if limit is not None:
        listed = listed[:limit]
    for issue in listed:
        if column == "age":
            measure = f"{issue.age_in_status_days:g}d"
        elif column == "blocked":
            measure = f"{issue.blocked_days:g}d" if issue.blocked_days else "—"
        else:
            measure = f"{issue.carry_over_sprints} sprints"
        rows.append(
            "<tr>"
            f"{_ticket_cell(issue.key, jira_base_url)}"
            f"<td>{_escape(issue.summary)}</td>"
            f'<td class="who">{_escape(issue.assignee)}</td>'
            f'<td class="num">{_escape(measure)}</td>'
            "</tr>"
        )
    if not rows:
        return '<p class="empty">Nothing to flag.</p>'
    return (
        '<table class="issues"><thead><tr><th>Ticket</th><th>Summary</th>'
        "<th>Assignee</th><th></th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _panel_note(total: int, shown: int) -> str:
    if total <= shown:
        return ""
    return f'<p class="note">Showing top {shown} of {total}.</p>'


def _squad_section(
    summary: TeamMetrics,
    trend: Sequence[TrendPoint],
    jira_base_url: str | None = None,
) -> str:
    blocked = tuple(summary.longest_blocked)
    blocked_heading = "Blocked, longest first"
    if summary.release:
        blocked_heading = (
            f"Blocked in {_escape(summary.release)}, longest first"
        )
    return (
        f'<section class="squad"><h2>{_icon(summary.team_id)} '
        f"{_escape(summary.team_name)}</h2>"
        f'<div class="trend"><span class="label">Completed per capture</span>'
        f"{_sparkline(trend)}</div>"
        '<div class="panels">'
        f'<div class="panel"><h3>Aging work in progress</h3>'
        f"{_issue_rows(summary.aging_wip, 'age', jira_base_url)}"
        f"{_panel_note(len(summary.aging_wip), MAX_ROWS)}</div>"
        f'<div class="panel"><h3>{blocked_heading}'
        f' <span class="count">{len(blocked)}</span></h3>'
        f"{_issue_rows(blocked, 'blocked', jira_base_url, limit=None)}</div>"
        f'<div class="panel"><h3>Chronic carry-over</h3>'
        f"{_issue_rows(summary.chronic_carry_over, 'carry', jira_base_url)}"
        f"{_panel_note(len(summary.chronic_carry_over), MAX_ROWS)}</div>"
        "</div></section>"
    )


def _view_body(
    view: ReleaseView,
    trends: dict[str, tuple[TrendPoint, ...]],
    jira_base_url: str | None,
) -> str:
    """Squad summary plus per-squad detail for one release scope."""
    sections = "".join(
        _squad_section(summary, trends.get(summary.team_id, ()), jira_base_url)
        for summary in view.summaries
    )
    if not sections:
        return '<section><p class="empty">No squads configured.</p></section>'
    return (
        "<section><h2>Squad summary</h2>"
        f"{_comparison_table(view.summaries)}"
        '<p class="note">Delivery time runs from the first active status to a '
        "done status. Percentage actively worked excludes blocked, review and "
        "deploy queues. Hover a column heading for detail.</p></section>"
        f"{sections}"
    )


def _tab_rules(count: int) -> str:
    """Per-tab CSS so a checked radio reveals its own view.

    Indexes are generated here, never taken from Jira data, so they cannot
    carry anything injectable into the stylesheet.
    """
    rules = []
    for index in range(count):
        rules.append(
            f"#rel-{index}:checked ~ #view-{index} {{ display: block; }}\n"
            f'#rel-{index}:checked ~ .tabbar label[for="rel-{index}"] '
            "{ background: var(--tab-on-bg); color: var(--tab-on-fg); "
            "border-color: var(--tab-on-bg); }\n"
            f'#rel-{index}:focus-visible ~ .tabbar label[for="rel-{index}"] '
            "{ outline: 2px solid #3b82f6; outline-offset: 2px; }\n"
        )
    return "".join(rules)


def _tabs(
    views: Sequence[ReleaseView],
    trends: dict[str, tuple[TrendPoint, ...]],
    jira_base_url: str | None,
) -> str:
    """Render every release as a pre-built tab, switched with CSS alone."""
    inputs = []
    labels = []
    bodies = []
    for index, view in enumerate(views):
        checked = " checked" if index == 0 else ""
        name = view.release or "Recent work"
        inputs.append(
            f'<input type="radio" name="release" id="rel-{index}"{checked}>'
        )
        labels.append(f'<label for="rel-{index}">{_escape(name)}</label>')
        bodies.append(
            f'<div class="view" id="view-{index}">'
            f"{_view_body(view, trends, jira_base_url)}</div>"
        )
    return (
        '<div class="tabs">'
        + "".join(inputs)
        + '<div class="tabbar"><span class="tabhint">Release</span>'
        + "".join(labels)
        + "</div>"
        + "".join(bodies)
        + "</div>"
    )


STYLE = """
:root { color-scheme: light dark; --tab-on-bg: #14161a; --tab-on-fg: #ffffff; }
* { box-sizing: border-box; }
body { margin: 0; padding: 2rem 1.5rem 4rem; font: 15px/1.5 -apple-system,
  BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: #f6f7f9; color: #14161a; }
h1 { margin: 0 0 .25rem; font-size: 1.6rem; }
h2 { margin: 0 0 1rem; font-size: 1.15rem; }
h3 { margin: 0 0 .5rem; font-size: .8rem; text-transform: uppercase;
  letter-spacing: .06em; color: #5b6472; }
.meta { margin: 0 0 2rem; color: #5b6472; font-size: .85rem; }
.wrap { max-width: 1180px; margin: 0 auto; }
section { background: #fff; border: 1px solid #e3e6eb; border-radius: 10px;
  padding: 1.25rem; margin-bottom: 1.25rem; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid #eef0f3;
  vertical-align: top; }
thead th { font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
  color: #5b6472; border-bottom: 1px solid #e3e6eb; }
tbody tr:last-child td { border-bottom: none; }
.compare td, .compare th[scope="row"] { font-variant-numeric: tabular-nums; }
.compare th[scope="row"] { white-space: nowrap; }
.track { height: 5px; margin-top: .35rem; background: #eef0f3; border-radius: 3px; }
.bar { display: block; height: 5px; border-radius: 3px; }
.bar.good { background: #2f855a; }
.bar.warn { background: #c05621; }
.panels { display: grid; gap: 1.25rem; grid-template-columns: 1fr; }
@media (min-width: 900px) { .panels { grid-template-columns: repeat(3, 1fr); } }
.panel { min-width: 0; }
.issues td { font-size: .86rem; }
.key { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  white-space: nowrap; }
.who { color: #5b6472; white-space: nowrap; }
.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.muted, .empty { color: #8b95a3; }
.empty { font-size: .86rem; margin: .25rem 0 0; }
.sub { display: block; font-weight: 400; text-transform: none; letter-spacing: 0;
  font-size: .68rem; color: #8b95a3; margin-top: .1rem; }
thead th abbr { text-decoration: underline dotted; cursor: help; }
.count { display: inline-block; min-width: 1.3rem; padding: 0 .35rem;
  border-radius: 999px; background: #eef0f3; color: #5b6472; font-size: .7rem;
  text-align: center; }
.key a { color: inherit; text-decoration: none; border-bottom: 1px solid #c3cad4; }
.key a:hover { border-bottom-color: currentColor; }
.tabs > input { position: absolute; opacity: 0; width: 0; height: 0; }
.tabs > .view { display: none; }
.tabbar { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem;
  margin: 0 0 1.25rem; }
.tabhint { font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
  color: #5b6472; margin-right: .2rem; }
.tabbar label { padding: .3rem .7rem; border: 1px solid #d7dce3; border-radius: 999px;
  background: #fff; font-size: .8rem; cursor: pointer; user-select: none; }
.tabbar label:hover { border-color: #9aa4b2; }
.trend { display: flex; align-items: center; gap: .75rem; margin-bottom: 1.25rem;
  color: #2f855a; }
.trend .label { font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
  color: #5b6472; }
.spark { width: 220px; height: 44px; }
.note { font-size: .8rem; color: #5b6472; }
@media (prefers-color-scheme: dark) {
  :root { --tab-on-bg: #e8eaed; --tab-on-fg: #14161a; }
  body { background: #14161a; color: #e8eaed; }
  section { background: #1c1f24; border-color: #2c313a; }
  th, td { border-color: #252932; }
  thead th, h3, .meta, .who, .trend .label, .note { color: #99a2b0; }
  .track { background: #252932; }
  .count { background: #252932; color: #99a2b0; }
  .key a { border-bottom-color: #3a414d; }
  .tabbar label { background: #1c1f24; border-color: #2c313a; }
  .tabhint { color: #99a2b0; }
}
"""


def render_dashboard(
    views: Sequence[ReleaseView],
    trends: dict[str, tuple[TrendPoint, ...]],
    settings: ReportSettings,
    generated_at: datetime | None = None,
    jira_base_url: str | None = None,
) -> str:
    """Render the full dashboard as one static HTML document.

    Every release tab is pre-rendered so switching needs no JavaScript.
    """
    moment = generated_at or datetime.now(timezone.utc)
    lookback = settings.delivery_metrics.lookback_days
    if not views:
        views = (ReleaseView(None, ()),)
    tabbed = len(views) > 1
    if tabbed:
        body = _tabs(views, trends, jira_base_url)
        rules = _tab_rules(len(views))
    else:
        body = _view_body(views[0], trends, jira_base_url)
        rules = ""
    release = views[0].release if not tabbed else None
    if tabbed:
        scope = (
            f" · release tabs include all matching issues"
            f" · Recent work uses {_escape(lookback)} days"
        )
    elif release:
        scope = f" · all issues in release {_escape(release)}"
    else:
        scope = f" · {_escape(lookback)}-day recent-work window"
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Delivery metrics</title>"
        f"<style>{STYLE}{rules}</style></head><body><div class=\"wrap\">"
        "<h1>Delivery metrics</h1>"
        f'<p class="meta">Generated {_escape(moment.strftime("%Y-%m-%d %H:%M UTC"))} '
        f"{scope} · "
        "metrics Jira Cloud cannot produce natively</p>"
        f"{body}"
        "</div></body></html>\n"
    )


def write_dashboard(path: str, markup: str) -> Path:
    """Write the dashboard, creating parent directories as needed."""
    target = Path(path)
    try:
        if target.parent != Path(""):
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markup, encoding="utf-8")
    except OSError as exc:
        raise SnapshotError(f"Failed to write dashboard: {exc.strerror}") from exc
    return target


def _jira_base_url() -> str | None:
    """Base URL for ticket links, or None when Jira is not configured.

    Offline renders from stored snapshots should still work, so a missing
    domain degrades to unlinked ticket keys rather than failing.
    """
    domain = os.environ.get("JIRA_DOMAIN", "").strip().rstrip("/")
    if not domain:
        return None
    if domain.startswith(("https://", "http://")):
        return domain
    return f"https://{domain}"


def build_dashboard(
    settings: ReportSettings,
    session: requests.Session | None = None,
    now: datetime | None = None,
    refresh: bool = True,
    release: str | None = None,
    all_releases: bool = False,
) -> str:
    """Collect fresh metrics (optionally), then render the dashboard.

    With ``all_releases`` every release becomes a tab; otherwise a single
    release scope is rendered.
    """
    moment = now or datetime.now(timezone.utc)
    directory = settings.delivery_metrics.snapshot_dir
    if refresh:
        client = session or requests.Session()
        for team in settings.teams:
            snapshot = collect_team_snapshot(team, settings, client, moment)
            write_snapshot(directory, snapshot)

    history = load_snapshots(directory)
    if not history:
        raise EODReportError(
            "No metric snapshots found; run delivery_metrics.py first"
        )
    trends = build_trends(history, settings)
    newest: dict[str, TeamSnapshot] = {}
    for snapshot in history:
        newest[snapshot.team_id] = snapshot
    ordered = [
        newest[team.id] for team in settings.teams if team.id in newest
    ]

    if all_releases:
        scopes: list[str | None] = list(
            release_options(ordered, release)[:MAX_RELEASES]
        )
        scopes.append(None)
    else:
        scopes = [release]

    views = tuple(
        ReleaseView(
            scope,
            tuple(summarize(snapshot, settings, scope) for snapshot in ordered),
        )
        for scope in scopes
    )
    return render_dashboard(
        views, trends, settings, moment, _jira_base_url()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None, help="Path to report-config.yml")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Render from stored snapshots without querying Jira",
    )
    parser.add_argument("--output", default=None, help="Dashboard output path")
    parser.add_argument(
        "--release",
        default=None,
        help="Scope to one release (fixVersion or label); "
        "defaults to the configured release blocker label",
    )
    parser.add_argument(
        "--all-releases",
        action="store_true",
        help="Render every release as a switchable tab",
    )
    args = parser.parse_args()

    try:
        settings = load_report_config(args.config)
        if not settings.delivery_metrics.enabled:
            print("Delivery metrics are disabled in the configuration.")
            return 0
        release = args.release
        if release is None and settings.release_blockers.enabled:
            release = settings.release_blockers.label
        markup = build_dashboard(
            settings,
            refresh=not args.offline,
            release=release,
            all_releases=args.all_releases,
        )
        target = write_dashboard(
            args.output or settings.delivery_metrics.dashboard_path, markup
        )
        print(f"Wrote {target}")
    except (EODReportError, SnapshotError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
