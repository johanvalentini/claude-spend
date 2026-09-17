import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

from claude_spend.collector import Handler
from claude_spend.db import Store, connect
from claude_spend.otlp import event_name, iter_log_records, iter_metric_points
from claude_spend.pricing import estimate_cost_usd, normalize_model
from claude_spend import queries as q


def kv(k, v):
    if isinstance(v, bool):
        return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int):
        return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, float):
        return {"key": k, "value": {"doubleValue": v}}
    return {"key": k, "value": {"stringValue": v}}


def api_request_payload(ts_ns, request_id="req_1", session="sess-abc", cost=0.1234):
    return {
        "resourceLogs": [{
            "resource": {"attributes": [kv("service.name", "claude-code"), kv("service.version", "2.1.263")]},
            "scopeLogs": [{
                "scope": {"name": "com.anthropic.claude_code.events"},
                "logRecords": [{
                    "timeUnixNano": str(ts_ns),
                    "body": {"stringValue": "claude_code.api_request"},
                    "attributes": [
                        kv("event.name", "claude_code.api_request"),
                        kv("session.id", session),
                        kv("model", "claude-fable-5-1"),
                        kv("input_tokens", 2),
                        kv("output_tokens", 645),
                        kv("cache_read_tokens", 13726),
                        kv("cache_creation_tokens", 8710),
                        kv("cost_usd", cost),
                        kv("duration_ms", 4200),
                        kv("request_id", request_id),
                        kv("query_source", "main"),
                        kv("effort", "medium"),
                        kv("user.email", "x@example.com"),
                        kv("terminal.type", "iTerm.app"),
                    ],
                }],
            }],
        }]
    }


def test_parse_log_record():
    recs = list(iter_log_records(api_request_payload(1_700_000_000_000_000_000)))
    assert len(recs) == 1
    r = recs[0]
    assert event_name(r) == "claude_code.api_request"
    assert r["attrs"]["input_tokens"] == 2
    assert r["attrs"]["cost_usd"] == 0.1234
    assert r["ts"] == 1_700_000_000.0
    assert r["resource"]["service.name"] == "claude-code"


def test_parse_metric_points():
    payload = {
        "resourceMetrics": [{
            "resource": {"attributes": []},
            "scopeMetrics": [{"metrics": [{
                "name": "claude_code.token.usage", "unit": "tokens",
                "sum": {"aggregationTemporality": 1, "isMonotonic": True, "dataPoints": [
                    {"asInt": "500", "timeUnixNano": "1700000000000000000",
                     "attributes": [kv("type", "cacheRead"), kv("model", "claude-opus-5")]},
                    {"asDouble": 1.5, "timeUnixNano": "1700000000000000000",
                     "attributes": [kv("type", "output")]},
                ]}}]}],
        }]
    }
    pts = list(iter_metric_points(payload))
    assert [p["value"] for p in pts] == [500.0, 1.5]
    assert pts[0]["attrs"]["type"] == "cacheRead"
    assert pts[0]["temporality"] == 1


def test_pricing():
    assert normalize_model("claude-fable-5-1[1m]") == "claude-fable-5-1"
    assert normalize_model("claude-fable-5") == "claude-fable-5"
    assert normalize_model("claude-opus-4-8-20260101") == "claude-opus-4-8"
    assert normalize_model("gpt-9") is None
    # 1M output on fable 5.1 = $50; 1M cache reads = $0.25
    assert estimate_cost_usd("claude-fable-5-1", 0, 1_000_000, 0, 0) == 50.0
    assert abs(estimate_cost_usd("claude-fable-5-1", 0, 0, 1_000_000, 0) - 0.25) < 1e-9


def test_end_to_end_http(tmp_path):
    db = tmp_path / "u.db"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.store = Store(db)
    srv.db_path = db
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        now_ns = int(time.time() * 1e9)

        def post(path, payload):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req) as resp:
                return resp.status

        assert post("/v1/logs", api_request_payload(now_ns, "r1")) == 200
        assert post("/v1/logs", api_request_payload(now_ns, "r1")) == 200  # duplicate ignored
        assert post("/v1/logs", api_request_payload(now_ns, "r2", cost=1.0)) == 200
        assert post("/v1/metrics", {"resourceMetrics": []}) == 200
        assert post("/v1/traces", {"resourceSpans": []}) == 200

        conn = connect(db)
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2
        t = q.today_totals(conn)
        assert t["n"] == 2
        assert abs(t["cost"] - 1.1234) < 1e-9
        assert t["cr"] == 2 * 13726
        s = conn.execute("SELECT * FROM sessions").fetchone()
        assert s["session_id"] == "sess-abc"
        assert s["terminal_type"] == "iTerm.app"
        assert q.active_session_count(conn) == 1
        assert q.recent_requests(conn)[0]["model"] == "claude-fable-5-1"
    finally:
        srv.shutdown()


def test_event_name_accepts_bare_attribute():
    rec = {"body": "claude_code.api_request", "attrs": {"event.name": "api_request"}}
    assert event_name(rec) == "claude_code.api_request"
    rec = {"body": None, "attrs": {"event.name": "api_request"}}
    assert event_name(rec) == "claude_code.api_request"
    assert event_name({"body": "hello", "attrs": {}}) is None
