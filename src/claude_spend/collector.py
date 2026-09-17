"""OTLP/HTTP JSON receiver for Claude Code telemetry -> SQLite.

Run: claude-spend-collector [--host 127.0.0.1] [--port 4318] [--db PATH]

Claude Code must be configured with:
  CLAUDE_CODE_ENABLE_TELEMETRY=1
  OTEL_METRICS_EXPORTER=otlp  OTEL_LOGS_EXPORTER=otlp
  OTEL_EXPORTER_OTLP_PROTOCOL=http/json
  OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import signal
import sys
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import DEFAULT_DB, DEFAULT_HOST, DEFAULT_PORT
from .db import Store
from .otlp import event_name, iter_log_records, iter_metric_points
from .pricing import estimate_cost_usd

log = logging.getLogger("claude-spend")

# Events kept in the `events` table (everything else is dropped).
KEEP_EVENTS = {
    "claude_code.user_prompt",
    "claude_code.api_error",
    "claude_code.api_refusal",
    "claude_code.tool_result",
    "claude_code.tool_decision",
    "claude_code.assistant_response",
}
# Metrics kept in metric_points for cross-checking against api_request events.
KEEP_METRICS = {
    "claude_code.token.usage",
    "claude_code.cost.usage",
    "claude_code.session.count",
    "claude_code.active_time.total",
}
# Attributes never persisted even if the user turned content logging on.
DROP_ATTRS = {"prompt", "response", "body", "tool_input", "tool_parameters"}


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-spend/0.1"
    store: Store  # set on the server class

    def log_message(self, fmt, *args):  # quiet default access log
        log.debug(fmt, *args)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        enc = (self.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            raw = zlib.decompress(raw)
        return raw

    def _reply(self, code: int, body: dict | None = None) -> None:
        data = json.dumps(body or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/health"):
            self._reply(200, {"ok": True, "db": str(self.server.db_path)})  # type: ignore[attr-defined]
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        try:
            raw = self._read_body()
        except Exception as e:  # noqa: BLE001
            log.warning("bad body: %s", e)
            self._reply(400, {"error": str(e)})
            return

        if ctype == "application/x-protobuf":
            log.error(
                "received protobuf on %s; set OTEL_EXPORTER_OTLP_PROTOCOL=http/json", self.path
            )
            self._reply(415, {"error": "protobuf not supported; use http/json"})
            return

        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            self._reply(400, {"error": f"invalid json: {e}"})
            return

        store: Store = self.server.store  # type: ignore[attr-defined]
        try:
            if self.path == "/v1/logs":
                self._handle_logs(store, payload)
            elif self.path == "/v1/metrics":
                self._handle_metrics(store, payload)
            elif self.path == "/v1/traces":
                pass  # accepted and discarded
            else:
                self._reply(404, {"error": "not found"})
                return
        except Exception:  # noqa: BLE001
            log.exception("failed to process %s", self.path)
            self._reply(500, {"error": "internal"})
            return
        # OTLP/HTTP success is an empty ExportServiceResponse
        self._reply(200, {})

    def _handle_logs(self, store: Store, payload: dict) -> None:
        n_req = 0
        for rec in iter_log_records(payload):
            name = event_name(rec)
            if not name:
                continue
            attrs = {**rec["resource"], **rec["attrs"]}
            for k in DROP_ATTRS:
                attrs.pop(k, None)
            ts = rec["ts"] or time.time()
            if name == "claude_code.api_request":
                est = estimate_cost_usd(
                    attrs.get("model"),
                    int(attrs.get("input_tokens") or 0),
                    int(attrs.get("output_tokens") or 0),
                    int(attrs.get("cache_read_tokens") or 0),
                    int(attrs.get("cache_creation_tokens") or 0),
                    attrs.get("speed"),
                )
                if store.insert_request(ts, attrs, est):
                    n_req += 1
                    log.info(
                        "api_request %s %s in=%s out=%s cr=%s cc=%s $%.4f",
                        (attrs.get("session.id") or "")[:8],
                        attrs.get("model"),
                        attrs.get("input_tokens"),
                        attrs.get("output_tokens"),
                        attrs.get("cache_read_tokens"),
                        attrs.get("cache_creation_tokens"),
                        float(attrs.get("cost_usd") or 0),
                    )
            elif name in KEEP_EVENTS:
                store.insert_event(ts, name, attrs)

    def _handle_metrics(self, store: Store, payload: dict) -> None:
        for p in iter_metric_points(payload):
            if p["name"] in KEEP_METRICS:
                p["attrs"] = {**p["resource"], **p["attrs"]}
                store.insert_metric_point(p)


def serve(host: str, port: int, db_path: Path) -> None:
    store = Store(db_path)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.store = store  # type: ignore[attr-defined]
    httpd.db_path = db_path  # type: ignore[attr-defined]

    def stop(*_):
        log.info("shutting down")
        httpd.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("listening on http://%s:%d  db=%s", host, port, db_path)
    import threading

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    t.join()
    httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    serve(args.host, args.port, args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
