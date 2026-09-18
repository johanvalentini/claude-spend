"""SQLite storage. WAL mode so the TUI can read while the collector writes."""

from __future__ import annotations

import glob
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import DEFAULT_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    key                   TEXT PRIMARY KEY,   -- request_id or synthesized dedupe key
    ts                    REAL NOT NULL,      -- unix seconds
    session_id            TEXT,
    model                 TEXT,
    query_source          TEXT,               -- main | subagent | auxiliary
    speed                 TEXT,
    effort                TEXT,
    agent_name            TEXT,
    skill_name            TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL,               -- reported by Claude Code (authoritative)
    cost_estimate_usd     REAL,               -- from local price table (cross-check)
    duration_ms           INTEGER,
    request_id            TEXT,
    prompt_id             TEXT,
    message_uuid          TEXT,
    attrs                 TEXT                -- full attribute JSON
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id, ts);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    name        TEXT NOT NULL,
    session_id  TEXT,
    attrs       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_name ON events(name, ts);

CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    project       TEXT,
    terminal_type TEXT,
    user_email    TEXT,
    app_version   TEXT
);

CREATE TABLE IF NOT EXISTS metric_points (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    name        TEXT NOT NULL,
    session_id  TEXT,
    model       TEXT,
    type        TEXT,
    value       REAL NOT NULL,
    temporality INTEGER,
    attrs       TEXT
);
CREATE INDEX IF NOT EXISTS idx_metric_points_ts ON metric_points(name, ts);
"""

_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def resolve_project(session_id: str) -> str | None:
    """Find which project dir owns this session by transcript *filename* only.

    Never reads transcript contents. Claude Code names transcripts
    ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
    """
    if not session_id:
        return None
    hits = glob.glob(str(_PROJECTS_DIR / "*" / f"{session_id}.jsonl"))
    if not hits:
        return None
    encoded = Path(hits[0]).parent.name  # e.g. -Users-<name>-code-claude-spend
    parts = [p for p in encoded.split("-") if p]
    # Drop the leading /Users/<name> for a shorter label
    if len(parts) >= 2 and parts[0] == "Users":
        parts = parts[2:]
    return "/".join(parts) or encoded


def connect(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


class Store:
    """Thread-safe writer used by the collector."""

    def __init__(self, path: Path = DEFAULT_DB):
        self.conn = connect(path)
        self.lock = threading.Lock()
        self._project_cache: dict[str, str | None] = {}

    def _project(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        if session_id not in self._project_cache or self._project_cache[session_id] is None:
            self._project_cache[session_id] = resolve_project(session_id)
        return self._project_cache[session_id]

    def touch_session(self, session_id: str | None, ts: float, attrs: dict[str, Any]) -> None:
        if not session_id:
            return
        project = self._project(session_id)
        self.conn.execute(
            """
            INSERT INTO sessions(session_id, first_seen, last_seen, project, terminal_type, user_email, app_version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                last_seen = MAX(last_seen, excluded.last_seen),
                project = COALESCE(sessions.project, excluded.project),
                terminal_type = COALESCE(sessions.terminal_type, excluded.terminal_type),
                user_email = COALESCE(sessions.user_email, excluded.user_email),
                app_version = COALESCE(sessions.app_version, excluded.app_version)
            """,
            (
                session_id,
                ts,
                ts,
                project,
                attrs.get("terminal.type"),
                attrs.get("user.email"),
                attrs.get("app.version"),
            ),
        )

    def insert_request(self, ts: float, attrs: dict[str, Any], cost_estimate: float | None) -> bool:
        session_id = attrs.get("session.id")
        request_id = attrs.get("request_id") or attrs.get("client_request_id")
        key = request_id or f"{session_id}:{attrs.get('message.uuid')}:{ts}"
        cost = attrs.get("cost_usd")
        if cost is None and attrs.get("cost_usd_micros") is not None:
            cost = float(attrs["cost_usd_micros"]) / 1e6
        with self.lock:
            cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO requests(
                    key, ts, session_id, model, query_source, speed, effort, agent_name, skill_name,
                    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                    cost_usd, cost_estimate_usd, duration_ms, request_id, prompt_id, message_uuid, attrs)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key,
                    ts,
                    session_id,
                    attrs.get("model"),
                    attrs.get("query_source"),
                    attrs.get("speed"),
                    attrs.get("effort"),
                    attrs.get("agent.name"),
                    attrs.get("skill.name"),
                    int(attrs.get("input_tokens") or 0),
                    int(attrs.get("output_tokens") or 0),
                    int(attrs.get("cache_read_tokens") or 0),
                    int(attrs.get("cache_creation_tokens") or 0),
                    float(cost) if cost is not None else None,
                    cost_estimate,
                    int(attrs["duration_ms"]) if attrs.get("duration_ms") is not None else None,
                    attrs.get("request_id"),
                    attrs.get("prompt.id"),
                    attrs.get("message.uuid"),
                    json.dumps(attrs, default=str),
                ),
            )
            self.touch_session(session_id, ts, attrs)
            self.conn.commit()
            return cur.rowcount > 0

    def insert_event(self, ts: float, name: str, attrs: dict[str, Any]) -> None:
        session_id = attrs.get("session.id")
        with self.lock:
            self.conn.execute(
                "INSERT INTO events(ts, name, session_id, attrs) VALUES (?,?,?,?)",
                (ts, name, session_id, json.dumps(attrs, default=str)),
            )
            self.touch_session(session_id, ts, attrs)
            self.conn.commit()

    def insert_metric_point(self, p: dict[str, Any]) -> None:
        attrs = p["attrs"]
        with self.lock:
            self.conn.execute(
                """INSERT INTO metric_points(ts, name, session_id, model, type, value, temporality, attrs)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    p["ts"] or time.time(),
                    p["name"],
                    attrs.get("session.id"),
                    attrs.get("model"),
                    attrs.get("type"),
                    p["value"],
                    p["temporality"],
                    json.dumps(attrs, default=str),
                ),
            )
            self.conn.commit()
