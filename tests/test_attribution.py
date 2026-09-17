"""Attribution queries: source categories, context burden, cache expiry, levers, report."""

import json

from claude_spend import queries as q
from claude_spend.db import Store
from claude_spend.report import collect, render

FABLE = "claude-fable-5-1"  # write $12.50/M, read $0.25/M, out $50/M


def _req(store, ts, session, source="repl_main_thread", cc=0, cr=0, out=0, cost=1.0, skill=None):
    store.insert_request(
        ts,
        {
            "session.id": session,
            "prompt.id": f"p{ts}",
            "model": FABLE,
            "query_source": source,
            "cache_creation_tokens": cc,
            "cache_read_tokens": cr,
            "output_tokens": out,
            "cost_usd": cost,
            "request_id": f"req-{ts}",
            "skill.name": skill,
        },
        None,
    )


def _tool(store, ts, session, tool, size, success="true"):
    store.insert_event(
        ts,
        "claude_code.tool_result",
        {"session.id": session, "tool_name": tool, "tool_result_size_bytes": str(size), "success": success,
         "duration_ms": "10"},
    )


def test_source_category():
    assert q.source_category("repl_main_thread") == "main"
    assert q.source_category("agent:builtin:Explore") == "subagent"
    assert q.source_category("agent_summary") == "subagent"
    assert q.source_category("prompt_suggestion") == "background"
    assert q.source_category("compact") == "background"
    assert q.source_category("sdk") == "other"


def test_context_burden_counts_later_main_requests(tmp_path):
    store = Store(tmp_path / "t.db")
    # 4000 bytes = 1000 tokens, followed by 3 main-thread requests and 1 subagent request (ignored)
    _tool(store, 100, "A", "Read", 4000)
    _tool(store, 101, "A", "Bash", 400, success="false")
    for ts in (110, 120, 130):
        _req(store, ts, "A")
    _req(store, 125, "A", source="agent:builtin:Explore")
    _req(store, 50, "A")  # before the tool call: must not count
    rows = {r["tool"]: r for r in q.context_burden(store.conn, 0)}
    read = rows["Read"]
    assert read["calls"] == 1 and read["reread_tokens"] == 3000
    # write once at $12.50/M + 3 re-reads at $0.25/M
    assert abs(read["cost"] - (1000 * 12.5e-6 + 3000 * 0.25e-6)) < 1e-9
    assert rows["Bash"]["fail_rate"] == 1.0
    assert list(rows) == ["Read", "Bash"]  # sorted by cost desc


def test_cache_expiry_waste_only_after_gap(tmp_path):
    store = Store(tmp_path / "t.db")
    _req(store, 0, "A", cc=100_000)                 # first request: cold write, not waste
    _req(store, 60, "A", cc=1_000)                   # 1 min later: normal
    _req(store, 60 + 600, "A", cc=100_000)           # 10 min later: expiry
    _req(store, 60 + 600 + 10, "A", cc=50_000, source="agent:builtin:Explore")  # subagent ignored
    r = q.cache_expiry_waste(store.conn, 0)
    assert r["events"] == 1
    assert abs(r["wasted_usd"] - 100_000 * (12.5 - 0.25) / 1e6) < 1e-9
    assert abs(r["cache_write_usd"] - 201_000 * 12.5 / 1e6) < 1e-9
    assert r["top_sessions"][0][0] == "A"


def test_daily_cache_expiry_cost_uses_pre_window_gap(tmp_path):
    import time
    store = Store(tmp_path / "t.db")
    today = q._day_start(0)
    _req(store, today - 3600, "A", cc=100_000)            # yesterday: cold write, outside a 1-day window
    _req(store, today + 60, "A", cc=100_000)              # first in-window request, >5 min gap: expiry
    _req(store, today + 120, "A", cc=1_000)               # incremental
    _req(store, today + 120 + 600, "A", cc=20_000)        # expiry
    _req(store, today + 130, "A", cc=50_000, source="agent:builtin:Explore")  # subagent ignored
    rows = q.daily_cache_expiry_cost(store.conn, 1)
    assert [r["day"] for r in rows] == [time.strftime("%Y-%m-%d", time.localtime(today))]
    assert rows[0]["n"] == 2
    assert abs(rows[0]["premium"] - 120_000 * (12.5 - 0.25) / 1e6) < 1e-9


def test_cost_by_source_and_skill(tmp_path):
    store = Store(tmp_path / "t.db")
    _req(store, 1, "A", cost=3.0, skill="prod-db-explore")
    _req(store, 2, "A", cost=1.0, source="prompt_suggestion")
    rows = q.cost_by_source(store.conn, 0)
    assert rows[0]["source"] == "repl_main_thread" and rows[0]["share"] == 0.75
    assert rows[1]["category"] == "background"
    skills = q.cost_by_skill(store.conn, 0)
    assert skills[0]["skill_name"] == "prod-db-explore" and skills[0]["cost"] == 3.0


def test_levers_ranked_and_report_renders(tmp_path):
    store = Store(tmp_path / "t.db")
    _req(store, 1, "A", cr=1_000_000, out=10_000, cost=5.0)
    _req(store, 2, "A", cost=0.5, source="away_summary")
    _tool(store, 0.5, "A", "Bash", 8000)
    since = 0
    lev = q.levers(store.conn, since)
    assert [l["lever"] for l in lev][0] in ("Output tokens", "Context re-reads")
    assert all(lev[i]["cost"] >= lev[i + 1]["cost"] for i in range(len(lev) - 1))
    assert next(l for l in lev if l["lever"] == "Background requests")["cost"] == 0.5
    d = collect(store.conn, 365 * 60)
    text = render(d)
    assert "FOCUS" in text and "Bash" in text and "away_summary" in text
    json.dumps(d, default=str)  # --json path must serialise


def test_classify_write():
    assert q.classify_write(None, 100) == "cold"
    assert q.classify_write(10, 100) == "incremental"
    assert q.classify_write(q.CACHE_TTL_S + 1, 100) == "expiry"
    assert q.classify_write(None, 0) == "none"


def test_turn_details_and_top_turns(tmp_path):
    store = Store(tmp_path / "t.db")
    # turn p100: main request + subagent request + 2 tools + user prompt
    _req(store, 100, "A", cc=5000, cr=20000, out=300, cost=1.0)
    store.insert_request(105, {"session.id": "A", "prompt.id": "p100", "model": FABLE,
                               "query_source": "agent:builtin:Explore", "agent.name": "Explore",
                               "cost_usd": 0.4, "output_tokens": 50, "request_id": "sub-1"}, None)
    store.insert_event(99, "claude_code.user_prompt", {"session.id": "A", "prompt.id": "p100", "prompt_length": "42"})
    store.insert_event(101, "claude_code.tool_result", {"session.id": "A", "prompt.id": "p100", "tool_name": "Read",
                                                        "tool_result_size_bytes": "9000", "success": "true"})
    store.insert_event(102, "claude_code.tool_result", {"session.id": "A", "prompt.id": "p100", "tool_name": "Bash",
                                                        "tool_result_size_bytes": "100", "success": "true"})
    # turn p1000: 15 minutes later -> expiry
    _req(store, 1000, "A", cc=25000, cr=0, out=10, cost=0.2)
    # background request must be ignored entirely
    _req(store, 1001, "A", source="away_summary", cost=9.0)

    turns = q.turn_details(store.conn, 0, "A")
    assert [t["prompt_id"] for t in turns] == ["p100", "p1000"]
    t0, t1 = turns
    assert t0["write_kind"] == "cold" and t0["gap_s"] is None
    assert t0["n"] == 2 and t0["n_sub"] == 1 and t0["agents"] == ["Explore"]
    assert abs(t0["cost"] - 1.4) < 1e-9 and t0["out"] == 350
    assert t0["ctx"] == 25000 and t0["cc"] == 5000
    assert t0["tools"] == 2 and t0["tool_bytes"] == 9100 and t0["top_tool"] == "Read"
    assert t0["prompt_len"] == 42 and t0["wall_s"] == 5
    assert t1["write_kind"] == "expiry" and t1["gap_s"] == 900
    top = q.top_turns(store.conn, 0, 1)
    assert top[0]["prompt_id"] == "p100"


def test_cache_write_anatomy(tmp_path):
    store = Store(tmp_path / "t.db")
    _req(store, 0, "A", cc=1000)          # cold
    _req(store, 30, "A", cc=100)          # incremental
    _req(store, 30 + 400, "A", cc=2000)   # expiry
    _req(store, 30 + 401, "A", cc=0)      # none: not counted
    rows = {r["kind"]: r for r in q.cache_write_anatomy(store.conn, 0)}
    assert rows["cold"]["n"] == 1 and rows["cold"]["tokens"] == 1000
    assert rows["incremental"]["n"] == 1
    assert rows["expiry"]["n"] == 1 and abs(rows["expiry"]["premium"] - 2000 * (12.5 - 0.25) / 1e6) < 1e-12
    assert abs(sum(r["share"] for r in rows.values()) - 1) < 1e-9


def test_errors(tmp_path):
    store = Store(tmp_path / "t.db")
    store.insert_event(5, "claude_code.api_error", {"session.id": "A", "model": FABLE, "status_code": "529",
                                                    "error": "overloaded", "attempt": "2"})
    store.insert_event(6, "claude_code.api_refusal", {"session.id": "A", "model": FABLE})
    _req(store, 7, "A")
    s = q.error_summary(store.conn, 0)
    assert s == {"errors": 1, "refusals": 1, "requests": 1}
    errs = q.errors_since(store.conn, 0)
    assert errs[0]["kind"] == "refusal" and errs[1]["error"] == "overloaded" and errs[1]["status"] == "529"
    d = collect(store.conn, 365 * 60)
    assert "overloaded" in render(d)
