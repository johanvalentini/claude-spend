"""Textual TUI for watching live Claude Code spend.

Run: claude-spend tui [--db PATH]
Keys: q quit · r refresh · d cycle history window (7/14/30 days) · 1-5 switch tab
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane
from textual_plotext import PlotextPlot

from . import DEFAULT_DB
from . import queries as q


def fmt_usd(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "-"


def fmt_tok(n: int | float | None) -> str:
    if n is None:
        return "-"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_bytes(n: int | float | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{int(n)}B"


def fmt_ago(ts: float | None) -> str:
    if not ts:
        return "-"
    d = int(time.time() - ts)
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h{(d % 3600) // 60:02d}m"
    return f"{d // 86400}d"


def fmt_gap(s: float | None) -> str:
    if s is None:
        return "-"
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.1f}h"


def fmt_write(kind: str) -> str:
    return {"expiry": "[b red]expiry[/]", "cold": "[yellow]cold[/]"}.get(kind, kind)


def short_model(m: str | None) -> str:
    if not m:
        return "-"
    return m.replace("claude-", "").split("[")[0]


def bar(value: float, maximum: float, width: int = 16) -> str:
    if maximum <= 0:
        return ""
    n = round(width * value / maximum)
    return "█" * n + "░" * (width - n)


class Summary(Static):
    pass


class DayChart(PlotextPlot):
    """Single-series chart over a fixed window of local days.
    One hue, no legend, left axis only, sparse ticks."""

    HUE = "blue+"

    def __init__(self, title: str, kind: str = "bar", fmt: str = "{:.0f}", **kw):
        super().__init__(**kw)
        self.chart_title = title
        self.kind = kind
        self.fmt = fmt

    def update_series(self, window: list[str], values: dict[str, float]) -> None:
        """window: every day label in the range, oldest first. values: day -> value."""
        plt = self.plt
        plt.clear_figure()
        plt.title(self.chart_title)
        plt.frame(False)
        plt.xaxes(True, True)  # upper axis carries the title
        plt.yaxes(True, False)
        plt.grid(False, False)
        n = len(window)
        xs = [i for i, d in enumerate(window) if d in values]
        ys = [values[window[i]] for i in xs]
        if not ys:
            plt.text("no data", 0.5, 0.5)
            self.refresh()
            return
        if self.kind == "bar":
            plt.bar(xs, ys, width=0.6, marker="hd", color=self.HUE)
        else:
            plt.plot(xs, ys, marker="braille", color=self.HUE)
            plt.scatter(xs, ys, marker="hd", color=self.HUE)
        plt.xlim(-0.6, n - 0.4)
        step = 1 if n <= 8 else 2 if n <= 16 else 5
        ticks = list(range(n - 1, -1, -step))[::-1]  # always include the latest day
        plt.xticks(ticks, [window[i][5:] for i in ticks])  # MM-DD
        top = max(ys) if max(ys) > 0 else 1
        plt.ylim(0, top * 1.15)
        plt.yticks([0, top / 2, top], [self.fmt.format(v) for v in (0, top / 2, top)])
        self.refresh()


class UsageApp(App):
    CSS = """
    Screen { layout: vertical; }
    #summary { height: 5; padding: 0 1; border: round $accent; }
    .charts { height: 12; }
    .charts PlotextPlot { width: 1fr; height: 1fr; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    #levers { height: 9; }
    #attr-mid { height: 12; }
    #tools { height: 1fr; }
    #session-list { height: 12; }
    #timeline { height: 1fr; }
    #top-turns { height: 1fr; }
    #turns-bottom { height: 9; }
    .col { width: 1fr; }
    .title { height: 1; padding: 0 1; color: $text-muted; text-style: bold; }
    DataTable { height: 1fr; }
    #models { height: 10; }
    #sessions { height: 14; }
    #efficiency { height: 1fr; }
    #efficiency DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("d", "cycle_days", "History window"),
        Binding("1", "tab('overview')", "Overview"),
        Binding("2", "tab('tab-models')", "By model"),
        Binding("3", "tab('attribution')", "Where it goes"),
        Binding("4", "tab('tab-timeline')", "Session timeline"),
        Binding("5", "tab('tab-turns')", "Turns & errors"),
    ]
    DAY_CHOICES = (7, 14, 30)

    def __init__(self, db_path: Path):
        super().__init__()
        self.db_path = db_path
        self.days_idx = 1
        self.conn: sqlite3.Connection | None = None
        self.selected_session: str | None = None

    @property
    def days(self) -> int:
        return self.DAY_CHOICES[self.days_idx]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Summary(id="summary")
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                with Horizontal(classes="charts"):
                    yield DayChart("Daily cost (USD)", kind="bar", fmt="${:.0f}", id="chart-cost")
                    yield DayChart("Median cost per turn (USD)", kind="line", fmt="${:.2f}", id="chart-turn")
                    yield DayChart("Cache expiry premium (USD)", kind="bar", fmt="${:.2f}", id="chart-expiry")
                with Horizontal(classes="charts"):
                    yield DayChart("Cache hit rate (% of input tokens)", kind="line", fmt="{:.0f}%", id="chart-cache")
                    yield DayChart("Median context per main-thread request (k tokens)", kind="line", fmt="{:.0f}k", id="chart-ctx")
                with Horizontal():
                    with Vertical(classes="col"):
                        yield Static("DAILY HISTORY", classes="title", id="daily-title")
                        yield DataTable(id="daily", zebra_stripes=True, cursor_type="none")
                    with Vertical(classes="col"):
                        yield Static("SESSIONS (last 24h)", classes="title")
                        yield DataTable(id="sessions", zebra_stripes=True, cursor_type="none")
                        yield Static("RECENT API REQUESTS", classes="title")
                        yield DataTable(id="requests", zebra_stripes=True, cursor_type="none")
            with TabPane("By model", id="tab-models"):
                yield Static("TODAY BY MODEL", classes="title")
                yield DataTable(id="models", zebra_stripes=True, cursor_type="none")
                with Horizontal(id="efficiency"):
                    with Vertical(classes="col"):
                        yield Static("COST PER TURN", classes="title", id="turns-title")
                        yield DataTable(id="turns", zebra_stripes=True, cursor_type="none")
                    with Vertical(classes="col"):
                        yield Static("COST PER SESSION", classes="title", id="tasks-title")
                        yield DataTable(id="tasks", zebra_stripes=True, cursor_type="none")
            with TabPane("Where it goes", id="attribution"):
                yield Static("FOCUS: ranked cost levers", classes="title", id="levers-title")
                yield DataTable(id="levers", zebra_stripes=True, cursor_type="none")
                with Horizontal(id="attr-mid"):
                    with Vertical(classes="col"):
                        yield Static("BY PROJECT", classes="title")
                        yield DataTable(id="projects", zebra_stripes=True, cursor_type="none")
                    with Vertical(classes="col"):
                        yield Static("BY REQUEST SOURCE", classes="title")
                        yield DataTable(id="sources", zebra_stripes=True, cursor_type="none")
                    with Vertical(classes="col"):
                        yield Static("BY SKILL", classes="title")
                        yield DataTable(id="skills", zebra_stripes=True, cursor_type="none")
                yield Static("TOOL OUTPUT IN CONTEXT (ESTIMATE: bytes/4 tokens, written once, re-read by every later main-thread request)", classes="title", id="tools-title")
                yield DataTable(id="tools", zebra_stripes=True, cursor_type="none")
            with TabPane("Session timeline", id="tab-timeline"):
                yield Static("SESSIONS (↑/↓ to pick one)", classes="title", id="session-list-title")
                yield DataTable(id="session-list", zebra_stripes=True, cursor_type="row")
                yield Static("TURNS", classes="title", id="timeline-title")
                yield DataTable(id="timeline", zebra_stripes=True, cursor_type="none")
            with TabPane("Turns & errors", id="tab-turns"):
                yield Static("MOST EXPENSIVE TURNS", classes="title", id="top-turns-title")
                yield DataTable(id="top-turns", zebra_stripes=True, cursor_type="none")
                with Horizontal(id="turns-bottom"):
                    with Vertical(classes="col"):
                        yield Static("CACHE WRITE ANATOMY (main thread; kind uses the 5 min TTL, tokens are exact)", classes="title")
                        yield DataTable(id="cache-anatomy", zebra_stripes=True, cursor_type="none")
                    with Vertical(classes="col"):
                        yield Static("API ERRORS AND REFUSALS", classes="title", id="errors-title")
                        yield DataTable(id="errors", zebra_stripes=True, cursor_type="none")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Claude Code usage"
        self.query_one("#models", DataTable).add_columns("model", "req", "cost", "input", "output", "cache rd", "cache wr")
        self.query_one("#daily", DataTable).add_columns("day", "cost", "", "req", "sess", "input", "output", "cache rd")
        self.query_one("#sessions", DataTable).add_columns("last", "project", "model", "req", "cost", "in+cache", "out", "id")
        self.query_one("#requests", DataTable).add_columns("time", "project", "model", "src", "eff", "in", "out", "cache rd", "cache wr", "cost", "ms")
        self.query_one("#turns", DataTable).add_columns("model", "eff", "turns", "total", "$/turn", "median", "req/turn", "med secs")
        self.query_one("#tasks", DataTable).add_columns("model", "eff", "sess", "total", "$/sess", "median", "turns/sess", "med mins")
        self.query_one("#levers", DataTable).add_columns("lever", "cost", "basis", "share", "", "detail", "what to do")
        self.query_one("#projects", DataTable).add_columns("project", "cost", "share", "sess", "req", "context", "out")
        self.query_one("#sources", DataTable).add_columns("source", "kind", "cost", "share", "req", "med ctx")
        self.query_one("#skills", DataTable).add_columns("skill", "cost", "turns", "req")
        self.query_one("#session-list", DataTable).add_columns("last", "project", "turns", "req", "cost", "peak ctx", "cache wr", "id")
        self.query_one("#timeline", DataTable).add_columns(
            "time", "gap", "write", "model", "eff", "cost", "req", "sub", "ctx", "cache wr", "cache rd", "out",
            "ttft", "wall", "tools", "tool out", "largest", "prompt", "skill", "agents")
        self.query_one("#top-turns", DataTable).add_columns(
            "time", "project", "model", "eff", "cost", "req", "sub", "ctx", "cache wr", "out", "wall",
            "tools", "tool out", "largest", "prompt", "skill", "agents", "session")
        self.query_one("#cache-anatomy", DataTable).add_columns("kind", "req", "tokens", "priced $", "share", "premium vs read")
        self.query_one("#errors", DataTable).add_columns("time", "kind", "model", "status", "attempt", "error", "session")
        self.query_one("#tools", DataTable).add_columns("tool", "burden $ (est)", "", "calls", "fail%", "total out", "median out", "re-read tok", "avg ms")
        self.refresh_data()
        self.set_interval(2.0, self.refresh_data)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.refresh_data()
        if event.pane.id == "tab-timeline":
            self.query_one("#session-list", DataTable).focus()

    def _connect(self) -> sqlite3.Connection | None:
        if self.conn is None:
            if not self.db_path.exists():
                return None
            self.conn = q.open_ro(self.db_path)
        return self.conn

    def action_refresh(self) -> None:
        self.refresh_data()

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_cycle_days(self) -> None:
        self.days_idx = (self.days_idx + 1) % len(self.DAY_CHOICES)
        self.refresh_data()

    def refresh_data(self) -> None:
        conn = self._connect()
        summary = self.query_one("#summary", Summary)
        if conn is None:
            summary.update(
                f"[b red]No database yet[/] at {self.db_path}\n"
                "Start the collector and run a Claude Code session with telemetry enabled."
            )
            return
        try:
            self._render(conn, summary)
        except sqlite3.OperationalError as e:
            summary.update(f"[red]DB error:[/] {e}")

    def _render(self, conn: sqlite3.Connection, summary: Summary) -> None:
        t = q.today_totals(conn)
        week = q.totals_since(conn, time.time() - 7 * 86400)
        month = q.totals_since(conn, q._day_start(29))
        active = q.active_session_count(conn)
        last = q.last_request_ts(conn)
        drift = ""
        if t["cost"] and t["est"]:
            pct = (t["est"] - t["cost"]) / t["cost"] * 100
            drift = f"  [dim](local estimate {fmt_usd(t['est'])}, {pct:+.1f}%)[/]"
        summary.update(
            f"[b]TODAY[/]  [b green]{fmt_usd(t['cost'])}[/]{drift}   "
            f"requests {t['n']}   active sessions [b]{active}[/]   last request {fmt_ago(last)} ago\n"
            f"tokens  input {fmt_tok(t['inp'])}  output {fmt_tok(t['out'])}  "
            f"cache read {fmt_tok(t['cr'])}  cache write {fmt_tok(t['cc'])}\n"
            f"[dim]7 days {fmt_usd(week['cost'])}   30 days {fmt_usd(month['cost'])}   db {self.db_path}[/]"
        )

        self.query_one("#daily-title", Static).update(f"DAILY HISTORY (last {self.days} days)")
        daily = self.query_one("#daily", DataTable)
        daily.clear()
        rows = q.daily(conn, self.days)
        window = [
            time.strftime("%Y-%m-%d", time.localtime(q._day_start(i))) for i in range(self.days - 1, -1, -1)
        ]
        self.query_one("#chart-cost", DayChart).update_series(
            window, {r["day"]: r["cost"] or 0 for r in rows}
        )
        cache_pct = {}
        for r in rows:
            total = (r["inp"] or 0) + (r["cr"] or 0) + (r["cc"] or 0)
            if total:
                cache_pct[r["day"]] = 100 * (r["cr"] or 0) / total
        self.query_one("#chart-cache", DayChart).update_series(window, cache_pct)
        self.query_one("#chart-turn", DayChart).update_series(
            window, {r["day"]: r["median_cost"] for r in q.daily_turn_cost(conn, self.days)}
        )
        self.query_one("#chart-ctx", DayChart).update_series(
            window, {r["day"]: r["median_ctx"] / 1000 for r in q.daily_context_size(conn, self.days)}
        )
        self.query_one("#chart-expiry", DayChart).update_series(
            window, {r["day"]: r["premium"] for r in q.daily_cache_expiry_cost(conn, self.days)}
        )
        mx = max((r["cost"] or 0 for r in rows), default=0)
        for r in rows:
            daily.add_row(
                r["day"], fmt_usd(r["cost"]), bar(r["cost"] or 0, mx), str(r["n"]), str(r["sessions"]),
                fmt_tok(r["inp"]), fmt_tok(r["out"]), fmt_tok(r["cr"]),
            )

        sessions = self.query_one("#sessions", DataTable)
        sessions.clear()
        for r in q.sessions_since(conn, time.time() - 86400):
            sessions.add_row(
                fmt_ago(r["last_seen"]), (r["project"] or "?")[:28], short_model(r["model"]),
                str(r["n"]), fmt_usd(r["cost"]), fmt_tok(r["inp"]), fmt_tok(r["out"]),
                (r["session_id"] or "")[:8],
            )

        since = q._day_start(self.days - 1)

        reqs = self.query_one("#requests", DataTable)
        reqs.clear()
        for r in q.recent_requests(conn, 40):
            reqs.add_row(
                time.strftime("%H:%M:%S", time.localtime(r["ts"])),
                (r["project"] or "?")[:20], short_model(r["model"]),
                (r["query_source"] or "")[:4], (r["effort"] or "")[:4],
                fmt_tok(r["input_tokens"]), fmt_tok(r["output_tokens"]),
                fmt_tok(r["cache_read_tokens"]), fmt_tok(r["cache_creation_tokens"]),
                f"{(r['cost_usd'] or 0):.4f}", str(r["duration_ms"] or ""),
            )

        active = self.query_one(TabbedContent).active
        if active == "tab-models":
            self._render_models(conn, since)
        elif active == "attribution":
            self._render_attribution(conn, since)
        elif active == "tab-timeline":
            self._render_timeline(conn, since)
        elif active == "tab-turns":
            self._render_turns(conn, since)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "session-list" and event.row_key is not None:
            sid = str(event.row_key.value)
            if sid != self.selected_session:
                self.selected_session = sid
                conn = self._connect()
                if conn is not None:
                    self._render_turn_rows(conn, sid)

    def _render_timeline(self, conn: sqlite3.Connection, since: float) -> None:
        self.query_one("#session-list-title", Static).update(
            f"SESSIONS (last {self.days} days; ↑/↓ to pick one)"
        )
        table = self.query_one("#session-list", DataTable)
        rows = q.session_list(conn, since)
        ids = [r["session_id"] for r in rows]
        if self.selected_session not in ids:
            self.selected_session = ids[0] if ids else None
        table.clear()
        for r in rows:
            table.add_row(
                fmt_ago(r["last_seen"]), (r["project"] or "?")[:30], str(r["turns"]), str(r["n"]),
                fmt_usd(r["cost"]), fmt_tok(r["peak_ctx"]), fmt_tok(r["cc"]), r["session_id"][:8],
                key=r["session_id"],
            )
        if self.selected_session:
            table.move_cursor(row=ids.index(self.selected_session), animate=False)
            self._render_turn_rows(conn, self.selected_session)

    def _render_turn_rows(self, conn: sqlite3.Connection, session_id: str) -> None:
        turns = q.turn_details(conn, 0, session_id)
        total = sum(t["cost"] for t in turns)
        self.query_one("#timeline-title", Static).update(
            f"TURNS in {session_id[:8]}  ({len(turns)} turns, {fmt_usd(total)}; "
            "write: cold = first request, expiry = after >5 min gap, incremental = normal)"
        )
        table = self.query_one("#timeline", DataTable)
        table.clear()
        for t in turns:
            table.add_row(
                time.strftime("%m-%d %H:%M", time.localtime(t["ts"])),
                fmt_gap(t["gap_s"]), fmt_write(t["write_kind"]), short_model(t["model"]),
                (t["effort"] or "")[:4], fmt_usd(t["cost"]), str(t["n"]), str(t["n_sub"]),
                fmt_tok(t["ctx"]), fmt_tok(t["cc"]), fmt_tok(t["cr"]), fmt_tok(t["out"]),
                f"{t['ttft_ms']}" if t["ttft_ms"] is not None else "-", f"{t['wall_s']:.0f}s",
                str(t["tools"]), fmt_bytes(t["tool_bytes"]),
                f"{t['top_tool']} {fmt_bytes(t['top_bytes'])}" if t["top_tool"] else "-",
                f"{t['prompt_len']}" if t["prompt_len"] is not None else "-",
                (t["skill"] or "")[:18], ",".join(t["agents"])[:20],
            )

    def _render_turns(self, conn: sqlite3.Connection, since: float) -> None:
        self.query_one("#top-turns-title", Static).update(
            f"MOST EXPENSIVE TURNS (last {self.days} days; reported cost, one user prompt and everything it triggered)"
        )
        table = self.query_one("#top-turns", DataTable)
        table.clear()
        for t in q.top_turns(conn, since, 30):
            table.add_row(
                time.strftime("%m-%d %H:%M", time.localtime(t["ts"])), (t["project"] or "?")[:22],
                short_model(t["model"]), (t["effort"] or "")[:4], fmt_usd(t["cost"]),
                str(t["n"]), str(t["n_sub"]), fmt_tok(t["ctx"]), fmt_tok(t["cc"]), fmt_tok(t["out"]),
                f"{t['wall_s']:.0f}s", str(t["tools"]), fmt_bytes(t["tool_bytes"]),
                f"{t['top_tool']} {fmt_bytes(t['top_bytes'])}" if t["top_tool"] else "-",
                f"{t['prompt_len']}" if t["prompt_len"] is not None else "-",
                (t["skill"] or "")[:18], ",".join(t["agents"])[:20], t["session_id"][:8],
            )
        anat = self.query_one("#cache-anatomy", DataTable)
        anat.clear()
        for r in q.cache_write_anatomy(conn, since):
            anat.add_row(
                fmt_write(r["kind"]), str(r["n"]), fmt_tok(r["tokens"]), fmt_usd(r["cost"]),
                f"{r['share'] * 100:.0f}%", fmt_usd(r["premium"]) if r["kind"] == "expiry" else "-",
            )
        es = q.error_summary(conn, since)
        self.query_one("#errors-title", Static).update(
            f"API ERRORS AND REFUSALS ({es['errors']} errors, {es['refusals']} refusals in {es['requests']} requests)"
        )
        errs = self.query_one("#errors", DataTable)
        errs.clear()
        for e in q.errors_since(conn, since, 20):
            errs.add_row(
                time.strftime("%m-%d %H:%M", time.localtime(e["ts"])), e["kind"], short_model(e["model"]),
                str(e["status"] or ""), str(e["attempt"] or ""), str(e["error"] or "")[:50],
                (e["session_id"] or "")[:8],
            )

    def _render_models(self, conn: sqlite3.Connection, since: float) -> None:
        models = self.query_one("#models", DataTable)
        models.clear()
        for r in q.by_model_since(conn, q._day_start(0)):
            models.add_row(
                short_model(r["model"]), str(r["n"]), fmt_usd(r["cost"]),
                fmt_tok(r["inp"]), fmt_tok(r["out"]), fmt_tok(r["cr"]), fmt_tok(r["cc"]),
            )

        self.query_one("#turns-title", Static).update(f"COST PER TURN by model/effort (last {self.days} days)")
        turns = self.query_one("#turns", DataTable)
        turns.clear()
        for r in q.cost_per_turn(conn, since):
            turns.add_row(
                short_model(r["model"]), r["effort"] or "-", str(r["turns"]), fmt_usd(r["cost"]),
                fmt_usd(r["mean_cost"]), fmt_usd(r["median_cost"]),
                f"{r['mean_reqs']:.1f}", f"{r['median_secs']:.0f}",
            )

        self.query_one("#tasks-title", Static).update(f"COST PER SESSION by model/effort (last {self.days} days)")
        tasks = self.query_one("#tasks", DataTable)
        tasks.clear()
        for r in q.cost_per_session(conn, since):
            tasks.add_row(
                short_model(r["model"]), r["effort"] or "-", str(r["sessions"]), fmt_usd(r["cost"]),
                fmt_usd(r["mean_cost"]), fmt_usd(r["median_cost"]),
                f"{r['mean_turns']:.1f}", f"{r['median_mins']:.0f}",
            )

    def _render_attribution(self, conn: sqlite3.Connection, since: float) -> None:
        self.query_one("#levers-title", Static).update(
            f"FOCUS: ranked cost levers (last {self.days} days; figures overlap, rank is what matters; "
            "basis: reported = Claude Code cost_usd, priced = tokens x price table, modelled = priced + assumptions)"
        )
        lev = self.query_one("#levers", DataTable)
        lev.clear()
        rows = q.levers(conn, since)
        mx = max((r["cost"] for r in rows), default=0)
        for r in rows:
            lev.add_row(
                r["lever"], fmt_usd(r["cost"]), r["basis"], f"{r['share'] * 100:.0f}%", bar(r["cost"], mx, 10),
                r["detail"][:60], r["hint"],
            )

        projects = self.query_one("#projects", DataTable)
        projects.clear()
        prow = q.cost_by_project(conn, since)
        total = sum(r["cost"] for r in prow) or 1
        for r in prow[:10]:
            projects.add_row(
                r["project"][:30], fmt_usd(r["cost"]), f"{r['cost'] / total * 100:.0f}%",
                str(r["sessions"]), str(r["n"]), fmt_tok(r["ctx"]), fmt_tok(r["out"]),
            )

        sources = self.query_one("#sources", DataTable)
        sources.clear()
        for r in q.cost_by_source(conn, since)[:10]:
            sources.add_row(
                r["source"].replace("agent:builtin:", "agent:")[:26], r["category"][:6],
                fmt_usd(r["cost"]), f"{r['share'] * 100:.0f}%", str(r["n"]), fmt_tok(r["median_ctx"]),
            )

        skills = self.query_one("#skills", DataTable)
        skills.clear()
        for r in q.cost_by_skill(conn, since)[:10]:
            skills.add_row((r["skill_name"] or "")[:28], fmt_usd(r["cost"]), str(r["turns"]), str(r["n"]))

        tools = self.query_one("#tools", DataTable)
        tools.clear()
        rows = q.context_burden(conn, since)
        mx = max((r["cost"] for r in rows), default=0)
        for r in rows[:12]:
            tools.add_row(
                r["tool"][:22], fmt_usd(r["cost"]), bar(r["cost"], mx, 12), str(r["calls"]),
                f"{r['fail_rate'] * 100:.0f}", fmt_bytes(r["bytes"]), fmt_bytes(r["median_bytes"]),
                fmt_tok(r["reread_tokens"]), f"{r['ms'] / r['calls']:.0f}" if r["calls"] else "-",
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args(argv)
    UsageApp(args.db).run()
    return 0


if __name__ == "__main__":
    main()
