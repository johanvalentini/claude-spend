"""Per-turn and per-session cost grouping (queries.cost_per_turn / cost_per_session)."""

from claude_spend.db import Store
from claude_spend import queries as q


def _req(store, ts, prompt, session, model, effort, cost, source="repl_main_thread", rid=None):
    store.insert_request(
        ts,
        {
            "session.id": session,
            "prompt.id": prompt,
            "model": model,
            "effort": effort,
            "query_source": source,
            "cost_usd": cost,
            "request_id": rid or f"req-{ts}",
        },
        None,
    )


def _seed(tmp_path):
    store = Store(tmp_path / "t.db")
    # Session A: fable/medium, two turns. Turn 1 fans out to an opus subagent.
    _req(store, 100, "p1", "A", "claude-fable-5-1", "medium", 1.0)
    _req(store, 110, "p1", "A", "claude-opus-5", "low", 0.5, source="agent:builtin:Explore")
    _req(store, 160, "p1", "A", "claude-fable-5-1", "medium", 0.5)
    _req(store, 300, "p2", "A", "claude-fable-5-1", "medium", 2.0)
    # Session B: sonnet/high, one turn, plus a housekeeping request that must be ignored.
    _req(store, 400, "p3", "B", "claude-sonnet-5", "high", 0.3)
    _req(store, 401, "p3", "B", "claude-haiku-4-5", None, 9.0, source="prompt_suggestion")
    return store.conn


def test_cost_per_turn_groups_by_main_thread_config(tmp_path):
    conn = _seed(tmp_path)
    rows = {(r["model"], r["effort"]): r for r in q.cost_per_turn(conn, 0)}
    assert set(rows) == {("claude-fable-5-1", "medium"), ("claude-sonnet-5", "high")}
    fable = rows[("claude-fable-5-1", "medium")]
    assert fable["turns"] == 2
    assert fable["cost"] == 4.0                # subagent cost attributed to parent turn
    assert fable["mean_cost"] == 2.0
    assert fable["median_cost"] == 2.0
    assert fable["mean_reqs"] == 2.0           # (3 + 1) / 2
    assert fable["median_secs"] == 30          # median of (60, 0)
    sonnet = rows[("claude-sonnet-5", "high")]
    assert sonnet["turns"] == 1 and sonnet["cost"] == 0.3  # housekeeping excluded


def test_cost_per_session_labels_by_dominant_config(tmp_path):
    conn = _seed(tmp_path)
    rows = {(r["model"], r["effort"]): r for r in q.cost_per_session(conn, 0)}
    fable = rows[("claude-fable-5-1", "medium")]
    assert fable["sessions"] == 1
    assert fable["cost"] == 4.0
    assert fable["mean_turns"] == 2.0
    assert abs(fable["median_mins"] - 200 / 60) < 1e-9
    assert rows[("claude-sonnet-5", "high")]["sessions"] == 1


def test_since_filter_applies(tmp_path):
    conn = _seed(tmp_path)
    rows = q.cost_per_turn(conn, 350)
    assert [r["model"] for r in rows] == ["claude-sonnet-5"]


def test_daily_turn_cost_groups_by_local_day(tmp_path):
    import time
    conn = _seed(tmp_path)
    days = 365 * 60  # window wide enough to include the 1970 timestamps in the seed
    rows = q.daily_turn_cost(conn, days)
    assert len(rows) == 1
    assert rows[0]["day"] == time.strftime("%Y-%m-%d", time.localtime(100))
    assert rows[0]["turns"] == 3
    assert rows[0]["median_cost"] == 2.0   # turns cost 2.0, 2.0, 0.3
