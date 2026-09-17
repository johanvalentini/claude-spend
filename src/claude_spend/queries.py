"""Read-only queries used by the TUI. All day grouping is in local time."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from . import DEFAULT_DB


def open_ro(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _day_start(days_ago: int = 0) -> float:
    lt = time.localtime(time.time() - days_ago * 86400)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def totals_since(conn: sqlite3.Connection, since: float) -> sqlite3.Row:
    return conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(cost_estimate_usd),0) est,
                  COALESCE(SUM(input_tokens),0) inp, COALESCE(SUM(output_tokens),0) out,
                  COALESCE(SUM(cache_read_tokens),0) cr, COALESCE(SUM(cache_creation_tokens),0) cc
           FROM requests WHERE ts >= ?""",
        (since,),
    ).fetchone()


def today_totals(conn: sqlite3.Connection) -> sqlite3.Row:
    return totals_since(conn, _day_start(0))


def by_model_since(conn: sqlite3.Connection, since: float) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT model, COUNT(*) n, SUM(cost_usd) cost,
                  SUM(input_tokens) inp, SUM(output_tokens) out,
                  SUM(cache_read_tokens) cr, SUM(cache_creation_tokens) cc
           FROM requests WHERE ts >= ? GROUP BY model ORDER BY cost DESC""",
        (since,),
    ).fetchall()


def daily(conn: sqlite3.Connection, days: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT date(ts, 'unixepoch', 'localtime') day, COUNT(*) n, SUM(cost_usd) cost,
                  SUM(input_tokens) inp, SUM(output_tokens) out,
                  SUM(cache_read_tokens) cr, SUM(cache_creation_tokens) cc,
                  COUNT(DISTINCT session_id) sessions
           FROM requests WHERE ts >= ? GROUP BY day ORDER BY day DESC""",
        (_day_start(days - 1),),
    ).fetchall()


def sessions_since(conn: sqlite3.Connection, since: float, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT s.session_id, s.project, s.last_seen, s.first_seen,
                  COUNT(r.key) n, COALESCE(SUM(r.cost_usd),0) cost,
                  COALESCE(SUM(r.input_tokens + r.cache_read_tokens + r.cache_creation_tokens),0) inp,
                  COALESCE(SUM(r.output_tokens),0) out,
                  (SELECT model FROM requests r2 WHERE r2.session_id = s.session_id
                     ORDER BY r2.ts DESC LIMIT 1) model
           FROM sessions s LEFT JOIN requests r ON r.session_id = s.session_id AND r.ts >= ?
           WHERE s.last_seen >= ?
           GROUP BY s.session_id ORDER BY s.last_seen DESC LIMIT ?""",
        (since, since, limit),
    ).fetchall()


def recent_requests(conn: sqlite3.Connection, limit: int = 40) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT r.ts, r.session_id, s.project, r.model, r.query_source, r.effort,
                  r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_creation_tokens,
                  r.cost_usd, r.duration_ms
           FROM requests r LEFT JOIN sessions s ON s.session_id = r.session_id
           ORDER BY r.ts DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def active_session_count(conn: sqlite3.Connection, window_s: int = 300) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE last_seen >= ?", (time.time() - window_s,)
    ).fetchone()[0]


def last_request_ts(conn: sqlite3.Connection) -> float | None:
    return conn.execute("SELECT MAX(ts) FROM requests").fetchone()[0]


# ---------------------------------------------------------------------------
# Cost per task. Two proxies for "task": a *turn* (one user prompt and every
# request it triggers, including subagents, linked by prompt.id) and a
# *session*. Both are labelled by the model/effort of the main-thread request
# so different configurations can be compared side by side.
# ---------------------------------------------------------------------------

# Background helpers Claude Code runs on its own; never part of a user's task.
HOUSEKEEPING_SOURCES = ("prompt_suggestion", "away_summary", "generate_session_title")

_TURN_SQL = f"""
WITH r AS (
    SELECT prompt_id, session_id, ts, model, effort, query_source, cost_usd
    FROM requests
    WHERE ts >= ? AND prompt_id IS NOT NULL
      AND query_source NOT IN ({",".join("?" * len(HOUSEKEEPING_SOURCES))})
),
label AS (
    -- model/effort of the first main-thread request in the turn (fallback: first request)
    SELECT prompt_id, model, effort FROM (
        SELECT prompt_id, model, effort,
               ROW_NUMBER() OVER (
                   PARTITION BY prompt_id
                   ORDER BY CASE WHEN query_source = 'repl_main_thread' THEN 0 ELSE 1 END, ts
               ) rn
        FROM r
    ) WHERE rn = 1
)
SELECT r.prompt_id, r.session_id, l.model, COALESCE(l.effort, '') effort,
       COUNT(*) n, COALESCE(SUM(r.cost_usd), 0) cost,
       MIN(r.ts) first_ts, MAX(r.ts) last_ts
FROM r JOIN label l ON l.prompt_id = r.prompt_id
GROUP BY r.prompt_id
"""


def turns_since(conn: sqlite3.Connection, since: float) -> list[sqlite3.Row]:
    """One row per user turn: prompt_id, session_id, model, effort, n, cost, first_ts, last_ts."""
    return conn.execute(_TURN_SQL, (since, *HOUSEKEEPING_SOURCES)).fetchall()


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0.0
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def cost_per_turn(conn: sqlite3.Connection, since: float) -> list[dict]:
    """Per (model, effort): turns, total cost, mean/median cost per turn,
    mean requests per turn, median wall seconds per turn."""
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for t in turns_since(conn, since):
        groups.setdefault((t["model"] or "?", t["effort"]), []).append(t)
    out = []
    for (model, effort), ts in groups.items():
        costs = [t["cost"] for t in ts]
        out.append(
            {
                "model": model,
                "effort": effort,
                "turns": len(ts),
                "cost": sum(costs),
                "mean_cost": sum(costs) / len(ts),
                "median_cost": _median(costs),
                "mean_reqs": sum(t["n"] for t in ts) / len(ts),
                "median_secs": _median([t["last_ts"] - t["first_ts"] for t in ts]),
            }
        )
    out.sort(key=lambda d: d["cost"], reverse=True)
    return out


def cost_per_session(conn: sqlite3.Connection, since: float) -> list[dict]:
    """Per (model, effort) of the session's dominant-cost configuration:
    sessions, total cost, mean/median cost per session, mean turns per session,
    median wall minutes (first to last request)."""
    per_session: dict[str, dict] = {}
    for t in turns_since(conn, since):
        s = per_session.setdefault(
            t["session_id"],
            {"turns": 0, "cost": 0.0, "first": t["first_ts"], "last": t["last_ts"], "cfg": {}},
        )
        s["turns"] += 1
        s["cost"] += t["cost"]
        s["first"] = min(s["first"], t["first_ts"])
        s["last"] = max(s["last"], t["last_ts"])
        key = (t["model"] or "?", t["effort"])
        s["cfg"][key] = s["cfg"].get(key, 0.0) + t["cost"]
    groups: dict[tuple[str, str], list[dict]] = {}
    for s in per_session.values():
        label = max(s["cfg"], key=s["cfg"].get)
        groups.setdefault(label, []).append(s)
    out = []
    for (model, effort), ss in groups.items():
        costs = [s["cost"] for s in ss]
        out.append(
            {
                "model": model,
                "effort": effort,
                "sessions": len(ss),
                "cost": sum(costs),
                "mean_cost": sum(costs) / len(ss),
                "median_cost": _median(costs),
                "mean_turns": sum(s["turns"] for s in ss) / len(ss),
                "median_mins": _median([(s["last"] - s["first"]) / 60 for s in ss]),
            }
        )
    out.sort(key=lambda d: d["cost"], reverse=True)
    return out


def daily_turn_cost(conn: sqlite3.Connection, days: int) -> list[dict]:
    """Per local day: number of turns and median cost per turn (housekeeping excluded)."""
    by_day: dict[str, list[float]] = {}
    for t in turns_since(conn, _day_start(days - 1)):
        day = time.strftime("%Y-%m-%d", time.localtime(t["first_ts"]))
        by_day.setdefault(day, []).append(t["cost"])
    return [
        {"day": day, "turns": len(costs), "median_cost": _median(costs)}
        for day, costs in sorted(by_day.items())
    ]


# ---------------------------------------------------------------------------
# Attribution: where the money goes, by project / request source / skill / tool,
# plus a ranked list of optimisation levers. Nothing here reads transcripts;
# every figure comes from api_request and tool_result telemetry.
# ---------------------------------------------------------------------------

from .pricing import PRICES, normalize_model  # noqa: E402

# Requests Claude Code makes on its own that are not part of any user task.
BACKGROUND_SOURCES = HOUSEKEEPING_SOURCES + ("compact",)
# Cache TTL Claude Code uses; a gap longer than this rewrites the whole context.
CACHE_TTL_S = 300
# Rough bytes-per-token for tool output (code and prose mix).
BYTES_PER_TOKEN = 4.0


def _prices(model: str | None) -> tuple[float, float, float]:
    """(input, cache_write_5m, cache_read) USD per token for the model, or zeros."""
    key = normalize_model(model)
    if key is None:
        return 0.0, 0.0, 0.0
    inp, _out, cw5, _cw1h, cr = PRICES[key]
    return inp / 1e6, cw5 / 1e6, cr / 1e6


def _output_price(model: str | None) -> float:
    key = normalize_model(model)
    return PRICES[key][1] / 1e6 if key else 0.0


def source_category(query_source: str | None) -> str:
    """main | subagent | background | other. agent_summary is subagent overhead."""
    s = query_source or ""
    if s == "repl_main_thread":
        return "main"
    if s.startswith("agent:") or s == "agent_summary":
        return "subagent"
    if s in BACKGROUND_SOURCES:
        return "background"
    return "other"


def cost_by_project(conn: sqlite3.Connection, since: float) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT COALESCE(s.project, '?') project, COUNT(*) n,
                  COUNT(DISTINCT r.session_id) sessions,
                  COALESCE(SUM(r.cost_usd),0) cost,
                  SUM(r.input_tokens + r.cache_read_tokens + r.cache_creation_tokens) ctx,
                  SUM(r.output_tokens) out
           FROM requests r LEFT JOIN sessions s ON s.session_id = r.session_id
           WHERE r.ts >= ? GROUP BY project ORDER BY cost DESC""",
        (since,),
    ).fetchall()


def cost_by_source(conn: sqlite3.Connection, since: float) -> list[dict]:
    """Per query_source: category, requests, cost, share of window total, median context tokens."""
    rows = conn.execute(
        """SELECT query_source, COUNT(*) n, COALESCE(SUM(cost_usd),0) cost,
                  SUM(output_tokens) out
           FROM requests WHERE ts >= ? GROUP BY query_source ORDER BY cost DESC""",
        (since,),
    ).fetchall()
    total = sum(r["cost"] for r in rows) or 1.0
    ctx = {}
    for r in conn.execute(
        "SELECT query_source, input_tokens + cache_read_tokens + cache_creation_tokens c "
        "FROM requests WHERE ts >= ?",
        (since,),
    ):
        ctx.setdefault(r["query_source"], []).append(r["c"])
    return [
        {
            "source": r["query_source"] or "?",
            "category": source_category(r["query_source"]),
            "n": r["n"],
            "cost": r["cost"],
            "share": r["cost"] / total,
            "median_ctx": _median(ctx.get(r["query_source"], [])),
            "out": r["out"] or 0,
        }
        for r in rows
    ]


def cost_by_skill(conn: sqlite3.Connection, since: float) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT skill_name, COUNT(*) n, COUNT(DISTINCT prompt_id) turns,
                  COALESCE(SUM(cost_usd),0) cost
           FROM requests WHERE ts >= ? AND skill_name IS NOT NULL
           GROUP BY skill_name ORDER BY cost DESC""",
        (since,),
    ).fetchall()


def tool_calls_since(conn: sqlite3.Connection, since: float) -> list[sqlite3.Row]:
    """tool_result events with the attributes we need, oldest first."""
    return conn.execute(
        """SELECT ts, session_id,
                  json_extract(attrs, '$.tool_name') tool,
                  json_extract(attrs, '$.success') success,
                  CAST(COALESCE(json_extract(attrs, '$.tool_result_size_bytes'), 0) AS INTEGER) bytes,
                  CAST(COALESCE(json_extract(attrs, '$.duration_ms'), 0) AS INTEGER) ms
           FROM events WHERE name = 'claude_code.tool_result' AND ts >= ? ORDER BY ts""",
        (since,),
    ).fetchall()


def context_burden(conn: sqlite3.Connection, since: float) -> list[dict]:
    """Approximate what each tool's output costs by living in the context.

    A tool result of B bytes adds ~B/4 tokens to the conversation. Those tokens are
    written to the cache once (cache-write price) and then re-read on every later
    main-thread request in the same session (cache-read price) until the session
    ends or is compacted. Summed per tool, this is the "context burden".

    Limits: tool_result events do not say which thread ran them, so output produced
    inside subagents is counted against the main thread too (over-estimate for
    Agent-heavy sessions). Compaction is not modelled (over-estimate for very long
    sessions). Treat the numbers as ranking signal, not invoice.
    """
    import bisect

    # main-thread requests per session: sorted timestamps + model per request
    main: dict[str, tuple[list[float], list[str | None]]] = {}
    for r in conn.execute(
        "SELECT session_id, ts, model FROM requests "
        "WHERE ts >= ? AND query_source = 'repl_main_thread' ORDER BY ts",
        (since,),
    ):
        ts_list, models = main.setdefault(r["session_id"], ([], []))
        ts_list.append(r["ts"])
        models.append(r["model"])

    # cache-read price to use per session: that of the session's latest main-thread model
    acc: dict[str, dict] = {}
    for ev in tool_calls_since(conn, since):
        tool = ev["tool"] or "?"
        a = acc.setdefault(
            tool,
            {"tool": tool, "calls": 0, "failures": 0, "bytes": 0, "sizes": [], "ms": 0,
             "reread_tokens": 0.0, "cost": 0.0},
        )
        a["calls"] += 1
        a["failures"] += 0 if str(ev["success"]).lower() in ("true", "1") else 1
        a["bytes"] += ev["bytes"]
        a["sizes"].append(ev["bytes"])
        a["ms"] += ev["ms"]
        tokens = ev["bytes"] / BYTES_PER_TOKEN
        chain = main.get(ev["session_id"])
        if not chain or tokens == 0:
            continue
        ts_list, models = chain
        i = bisect.bisect_right(ts_list, ev["ts"])
        later = len(ts_list) - i
        _inp, cw, cr = _prices(models[-1])
        a["reread_tokens"] += tokens * later
        a["cost"] += tokens * (cw + later * cr)

    out = []
    for a in acc.values():
        a["median_bytes"] = _median(a.pop("sizes"))
        a["fail_rate"] = a["failures"] / a["calls"] if a["calls"] else 0.0
        out.append(a)
    out.sort(key=lambda d: d["cost"], reverse=True)
    return out


def cache_expiry_waste(conn: sqlite3.Connection, since: float) -> dict:
    """Premium paid for re-writing context after the prompt cache expired.

    Main-thread requests that follow a gap longer than CACHE_TTL_S in the same
    session must re-write the whole context at the cache-write price instead of
    reading it at the cache-read price. The waste is cache_creation_tokens times
    (write price - read price) for those requests.

    Returns events, wasted_usd, total_cache_write_usd and the sessions most affected.
    """
    prev: dict[str, float] = {}
    events = 0
    waste = 0.0
    write_total = 0.0
    per_session: dict[str, float] = {}
    for r in conn.execute(
        "SELECT session_id, ts, model, cache_creation_tokens cc FROM requests "
        "WHERE ts >= ? AND query_source = 'repl_main_thread' ORDER BY ts",
        (since,),
    ):
        _inp, cw, cr = _prices(r["model"])
        write_total += r["cc"] * cw
        last = prev.get(r["session_id"])
        prev[r["session_id"]] = r["ts"]
        if last is not None and r["ts"] - last > CACHE_TTL_S and r["cc"] > 0:
            events += 1
            w = r["cc"] * (cw - cr)
            waste += w
            per_session[r["session_id"]] = per_session.get(r["session_id"], 0.0) + w
    top = sorted(per_session.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return {"events": events, "wasted_usd": waste, "cache_write_usd": write_total, "top_sessions": top}


def daily_cache_expiry_cost(conn: sqlite3.Connection, days: int) -> list[dict]:
    """Per local day: premium paid for main-thread cache writes that followed a gap
    longer than CACHE_TTL_S (same rule as cache_expiry_waste), and how many there were.
    The previous request per session is tracked from before the window so the first
    in-window request of a running session is classified correctly."""
    since = _day_start(days - 1)
    prev: dict[str, float] = {}
    by_day: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT session_id, ts, model, cache_creation_tokens cc FROM requests "
        "WHERE query_source = 'repl_main_thread' ORDER BY ts"
    ):
        last = prev.get(r["session_id"])
        prev[r["session_id"]] = r["ts"]
        if r["ts"] < since or last is None or r["ts"] - last <= CACHE_TTL_S or r["cc"] <= 0:
            continue
        _inp, cw, cr = _prices(r["model"])
        day = time.strftime("%Y-%m-%d", time.localtime(r["ts"]))
        d = by_day.setdefault(day, {"day": day, "premium": 0.0, "n": 0})
        d["premium"] += r["cc"] * (cw - cr)
        d["n"] += 1
    return [by_day[k] for k in sorted(by_day)]


def daily_context_size(conn: sqlite3.Connection, days: int) -> list[dict]:
    """Per local day: median context tokens (input + cache read + cache write) of
    main-thread requests. The single best signal for 'my sessions are too long'."""
    by_day: dict[str, list[int]] = {}
    for r in conn.execute(
        "SELECT ts, input_tokens + cache_read_tokens + cache_creation_tokens c FROM requests "
        "WHERE ts >= ? AND query_source = 'repl_main_thread'",
        (_day_start(days - 1),),
    ):
        by_day.setdefault(time.strftime("%Y-%m-%d", time.localtime(r["ts"])), []).append(r["c"])
    return [{"day": d, "median_ctx": _median(v), "n": len(v)} for d, v in sorted(by_day.items())]


def cost_components(conn: sqlite3.Connection, since: float) -> dict[str, float]:
    """Cost split by token kind using the price table: input, output, cache_write, cache_read."""
    comp = {"input": 0.0, "output": 0.0, "cache_write": 0.0, "cache_read": 0.0}
    for r in conn.execute(
        "SELECT model, SUM(input_tokens) i, SUM(output_tokens) o, SUM(cache_read_tokens) cr, "
        "SUM(cache_creation_tokens) cc FROM requests WHERE ts >= ? GROUP BY model",
        (since,),
    ):
        inp, cw, cr = _prices(r["model"])
        comp["input"] += (r["i"] or 0) * inp
        comp["output"] += (r["o"] or 0) * _output_price(r["model"])
        comp["cache_write"] += (r["cc"] or 0) * cw
        comp["cache_read"] += (r["cr"] or 0) * cr
    return comp


def levers(conn: sqlite3.Connection, since: float) -> list[dict]:
    """Ranked list of where cost concentrates, each with the dollar figure it
    controls over the window, its share of total spend, a detail line and a hint.
    Figures overlap (a tool's context burden is also cache-read cost); the point
    is to rank what to look at first, not to sum the column."""
    total = totals_since(conn, since)["cost"] or 0.0
    comp = cost_components(conn, since)
    out: list[dict] = []

    # basis: how the dollar figure was obtained.
    #   reported  - sum of cost_usd as sent by Claude Code (authoritative)
    #   priced    - token counts from telemetry x local price table
    #   modelled  - priced, plus assumptions (bytes/token, cache TTL, re-read count)
    def add(name: str, cost: float, basis: str, detail: str, hint: str) -> None:
        out.append({"lever": name, "cost": cost, "basis": basis, "share": cost / total if total else 0.0,
                    "detail": detail, "hint": hint})

    med_ctx = _median([
        r[0] for r in conn.execute(
            "SELECT input_tokens + cache_read_tokens + cache_creation_tokens FROM requests "
            "WHERE ts >= ? AND query_source = 'repl_main_thread'", (since,))
    ])
    add(
        "Context re-reads", comp["cache_read"], "priced",
        f"median main-thread context {med_ctx / 1000:.0f}k tokens",
        "Long sessions re-read everything every turn. /clear between tasks, /compact earlier, "
        "check /context for a fat baseline (MCP schemas, CLAUDE.md, skills).",
    )
    add(
        "Output tokens", comp["output"], "priced",
        f"{totals_since(conn, since)['out'] / 1000:.0f}k output tokens",
        "Output is the priciest token. Lower effort for routine work; ask for shorter answers.",
    )
    exp = cache_expiry_waste(conn, since)
    add(
        "Cache expiry", exp["wasted_usd"], "modelled",
        f"{exp['events']} main-thread requests after a >{CACHE_TTL_S // 60} min pause "
        f"(of {fmt_money(exp['cache_write_usd'])} total cache writes)",
        "A pause longer than the cache TTL rewrites the whole context at the cache-write price instead of the read price. "
        "Resume promptly or start a fresh session after a break.",
    )
    burden = context_burden(conn, since)
    if burden:
        top = burden[0]
        add(
            "Tool output in context", sum(b["cost"] for b in burden), "modelled",
            f"top: {top['tool']} {fmt_money(top['cost'])} over {top['calls']} calls, "
            f"median {top['median_bytes'] / 1024:.1f} KB",
            "Every tool result stays in context for the rest of the session. Pipe through head, "
            "grep or limit; read line ranges instead of whole files; delegate big sweeps to Explore.",
        )
    src = cost_by_source(conn, since)
    sub = sum(s["cost"] for s in src if s["category"] == "subagent")
    sub_detail = ", ".join(
        f"{s['source'].replace('agent:builtin:', '')} {fmt_money(s['cost'])}"
        for s in src if s["category"] == "subagent"
    )[:90]
    add(
        "Subagents", sub, "reported", sub_detail or "none",
        "Each subagent starts a fresh context and its result is summarised (agent_summary). "
        "Use Explore for search; avoid spawning agents for single lookups.",
    )
    bg = sum(s["cost"] for s in src if s["category"] == "background")
    bg_detail = ", ".join(
        f"{s['source']} {fmt_money(s['cost'])}" for s in src if s["category"] == "background"
    )[:90]
    add(
        "Background requests", bg, "reported", bg_detail or "none",
        "Prompt suggestions and away summaries are optional: turn them off in /config if unused.",
    )
    out.sort(key=lambda d: d["cost"], reverse=True)
    return out


def fmt_money(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "-"


# ---------------------------------------------------------------------------
# High-fidelity views: per-turn detail (session timeline and turn explorer),
# cache-write anatomy, and API errors. Costs here are Claude Code's reported
# cost_usd; token counts are exact. Only the cache-write *classification*
# (cold / expiry / incremental) relies on the CACHE_TTL_S assumption.
# ---------------------------------------------------------------------------


def classify_write(gap_s: float | None, cache_creation: int) -> str:
    """cold: first main-thread request of the session.
    expiry: follows a gap longer than the cache TTL, so the context was re-written.
    incremental: normal turn, only new material written. none: nothing written."""
    if cache_creation <= 0:
        return "none"
    if gap_s is None:
        return "cold"
    if gap_s > CACHE_TTL_S:
        return "expiry"
    return "incremental"


def session_list(conn: sqlite3.Connection, since: float, limit: int = 40) -> list[sqlite3.Row]:
    """Sessions with activity in the window, most recent first, with turn counts and peak context."""
    return conn.execute(
        """SELECT s.session_id, s.project, s.first_seen, s.last_seen,
                  COUNT(DISTINCT CASE WHEN r.query_source = 'repl_main_thread' THEN r.prompt_id END) turns,
                  COUNT(r.key) n, COALESCE(SUM(r.cost_usd), 0) cost,
                  MAX(CASE WHEN r.query_source = 'repl_main_thread'
                           THEN r.input_tokens + r.cache_read_tokens + r.cache_creation_tokens END) peak_ctx,
                  SUM(CASE WHEN r.query_source = 'repl_main_thread' THEN r.cache_creation_tokens END) cc
           FROM sessions s JOIN requests r ON r.session_id = s.session_id
           WHERE r.ts >= ?
           GROUP BY s.session_id ORDER BY s.last_seen DESC LIMIT ?""",
        (since, limit),
    ).fetchall()


def turn_details(
    conn: sqlite3.Connection, since: float, session_id: str | None = None
) -> list[dict]:
    """One dict per user turn (prompt_id), oldest first, with exact per-turn figures:

    ts, gap_s (since previous main-thread request in the session), write_kind,
    model, effort, skill, cost (reported), n (requests), n_sub (subagent requests),
    agents (names), ctx (context tokens of the turn's last main-thread request),
    cc/cr/out tokens summed over the turn, ttft_ms of the first main request,
    wall_s, tools (count), tool_bytes, top_tool (name and bytes of the largest
    result), prompt_len (characters typed by the user).
    """
    where = "r.ts >= ? AND r.prompt_id IS NOT NULL"
    params: list = [since]
    if session_id:
        where += " AND r.session_id = ?"
        params.append(session_id)
    reqs = conn.execute(
        f"""SELECT r.prompt_id, r.session_id, s.project, r.ts, r.model, r.effort, r.query_source,
                   r.agent_name, r.skill_name, r.cost_usd, r.input_tokens, r.output_tokens,
                   r.cache_read_tokens, r.cache_creation_tokens, r.duration_ms,
                   json_extract(r.attrs, '$.ttft_ms') ttft
            FROM requests r LEFT JOIN sessions s ON s.session_id = r.session_id
            WHERE {where} AND r.query_source NOT IN ({",".join("?" * len(BACKGROUND_SOURCES))})
            ORDER BY r.ts""",
        (*params, *BACKGROUND_SOURCES),
    ).fetchall()

    turns: dict[str, dict] = {}
    order: list[str] = []
    prev_main_ts: dict[str, float] = {}
    for r in reqs:
        t = turns.get(r["prompt_id"])
        if t is None:
            t = turns[r["prompt_id"]] = {
                "prompt_id": r["prompt_id"], "session_id": r["session_id"], "project": r["project"],
                "ts": r["ts"], "last_ts": r["ts"], "gap_s": None, "write_kind": "none",
                "model": None, "effort": None, "skill": None, "cost": 0.0, "n": 0, "n_sub": 0,
                "agents": set(), "ctx": 0, "cc": 0, "cr": 0, "out": 0, "ttft_ms": None,
                "tools": 0, "tool_bytes": 0, "top_tool": None, "top_bytes": 0, "prompt_len": None,
            }
            order.append(r["prompt_id"])
        t["last_ts"] = max(t["last_ts"], r["ts"])
        t["cost"] += r["cost_usd"] or 0.0
        t["n"] += 1
        t["out"] += r["output_tokens"]
        t["skill"] = t["skill"] or r["skill_name"]
        if r["query_source"] == "repl_main_thread":
            if t["model"] is None:
                t["model"], t["effort"] = r["model"], r["effort"]
                t["ttft_ms"] = r["ttft"]
                last = prev_main_ts.get(r["session_id"])
                t["gap_s"] = None if last is None else r["ts"] - last
            prev_main_ts[r["session_id"]] = r["ts"]
            t["ctx"] = r["input_tokens"] + r["cache_read_tokens"] + r["cache_creation_tokens"]
            t["cc"] += r["cache_creation_tokens"]
            t["cr"] += r["cache_read_tokens"]
        else:
            t["n_sub"] += 1
            if r["agent_name"]:
                t["agents"].add(r["agent_name"])
    for t in turns.values():
        t["write_kind"] = classify_write(t["gap_s"], t["cc"])
        t["wall_s"] = t["last_ts"] - t["ts"]
        if t["model"] is None:  # turn with no main-thread request (rare): fall back
            t["model"] = "?"

    if turns:
        ev_where = "e.ts >= ?"
        ev_params: list = [since]
        if session_id:
            ev_where += " AND e.session_id = ?"
            ev_params.append(session_id)
        for e in conn.execute(
            f"""SELECT e.name, json_extract(e.attrs, '$."prompt.id"') pid,
                       json_extract(e.attrs, '$.tool_name') tool,
                       CAST(COALESCE(json_extract(e.attrs, '$.tool_result_size_bytes'), 0) AS INTEGER) bytes,
                       CAST(COALESCE(json_extract(e.attrs, '$.prompt_length'), 0) AS INTEGER) plen
                FROM events e
                WHERE {ev_where} AND e.name IN ('claude_code.tool_result', 'claude_code.user_prompt')""",
            ev_params,
        ):
            t = turns.get(e["pid"])
            if t is None:
                continue
            if e["name"] == "claude_code.user_prompt":
                t["prompt_len"] = e["plen"]
            else:
                t["tools"] += 1
                t["tool_bytes"] += e["bytes"]
                if e["bytes"] > t["top_bytes"]:
                    t["top_bytes"], t["top_tool"] = e["bytes"], e["tool"]
    out = [turns[p] for p in order]
    for t in out:
        t["agents"] = sorted(t["agents"])
    return out


def top_turns(conn: sqlite3.Connection, since: float, limit: int = 25) -> list[dict]:
    """Most expensive turns in the window (reported cost), across all sessions."""
    return sorted(turn_details(conn, since), key=lambda t: t["cost"], reverse=True)[:limit]


def cache_write_anatomy(conn: sqlite3.Connection, since: float) -> list[dict]:
    """Main-thread cache writes grouped by kind (cold / expiry / incremental):
    requests, tokens written, priced cost, and for expiry the premium over a read."""
    prev: dict[str, float] = {}
    acc: dict[str, dict] = {
        k: {"kind": k, "n": 0, "tokens": 0, "cost": 0.0, "premium": 0.0}
        for k in ("cold", "expiry", "incremental")
    }
    for r in conn.execute(
        "SELECT session_id, ts, model, cache_creation_tokens cc FROM requests "
        "WHERE ts >= ? AND query_source = 'repl_main_thread' ORDER BY ts",
        (since,),
    ):
        last = prev.get(r["session_id"])
        prev[r["session_id"]] = r["ts"]
        kind = classify_write(None if last is None else r["ts"] - last, r["cc"])
        if kind == "none":
            continue
        _inp, cw, cr = _prices(r["model"])
        a = acc[kind]
        a["n"] += 1
        a["tokens"] += r["cc"]
        a["cost"] += r["cc"] * cw
        if kind == "expiry":
            a["premium"] += r["cc"] * (cw - cr)
    total = sum(a["cost"] for a in acc.values()) or 1.0
    for a in acc.values():
        a["share"] = a["cost"] / total
    return list(acc.values())


def errors_since(conn: sqlite3.Connection, since: float, limit: int = 40) -> list[dict]:
    """api_error and api_refusal events, newest first. Attribute names follow Claude
    Code's telemetry docs; missing ones show as None."""
    rows = conn.execute(
        """SELECT ts, name, session_id, attrs FROM events
           WHERE ts >= ? AND name IN ('claude_code.api_error', 'claude_code.api_refusal')
           ORDER BY ts DESC LIMIT ?""",
        (since, limit),
    ).fetchall()
    import json as _json

    out = []
    for r in rows:
        a = _json.loads(r["attrs"] or "{}")
        out.append(
            {
                "ts": r["ts"],
                "kind": r["name"].removeprefix("claude_code.api_"),
                "session_id": r["session_id"],
                "model": a.get("model"),
                "status": a.get("status_code"),
                "error": a.get("error") or a.get("refusal_reason") or a.get("message"),
                "attempt": a.get("attempt"),
                "duration_ms": a.get("duration_ms"),
                "input_tokens": a.get("input_tokens"),
                "output_tokens": a.get("output_tokens"),
            }
        )
    return out


def error_summary(conn: sqlite3.Connection, since: float) -> dict:
    """Counts of errors and refusals in the window, and the number of API requests."""
    row = conn.execute(
        """SELECT SUM(name = 'claude_code.api_error') errors,
                  SUM(name = 'claude_code.api_refusal') refusals
           FROM events WHERE ts >= ? AND name IN ('claude_code.api_error', 'claude_code.api_refusal')""",
        (since,),
    ).fetchone()
    n = conn.execute("SELECT COUNT(*) FROM requests WHERE ts >= ?", (since,)).fetchone()[0]
    return {"errors": row["errors"] or 0, "refusals": row["refusals"] or 0, "requests": n}
