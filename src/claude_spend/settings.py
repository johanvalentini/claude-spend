"""Enable or disable Claude Code telemetry export to the local collector.

Run: claude-spend setup [--port 4318] [--settings ~/.claude/settings.json]
     claude-spend setup --purge

Writes the env block Claude Code needs into ~/.claude/settings.json. Only the
telemetry keys are touched; everything else in the file is preserved.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import DEFAULT_PORT

DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"
ENDPOINT_KEY = "OTEL_EXPORTER_OTLP_ENDPOINT"


def telemetry_env(port: int) -> dict[str, str]:
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        ENDPOINT_KEY: f"http://127.0.0.1:{port}",
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "delta",
        "OTEL_METRIC_EXPORT_INTERVAL": "10000",
        "OTEL_LOGS_EXPORT_INTERVAL": "2000",
    }


def _is_telemetry_key(key: str) -> bool:
    return key == "CLAUDE_CODE_ENABLE_TELEMETRY" or key.startswith("OTEL_")


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise SystemExit(f"error: {path} is not a JSON object")
    return data


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def current_endpoint(path: Path = DEFAULT_SETTINGS) -> str | None:
    """The OTLP endpoint Claude Code currently exports to, or None."""
    return _load(path).get("env", {}).get(ENDPOINT_KEY) or None


def enable(port: int = DEFAULT_PORT, path: Path = DEFAULT_SETTINGS) -> dict[str, str]:
    """Write the telemetry env block. Returns the env block written."""
    data = _load(path)
    env = data.setdefault("env", {})
    block = telemetry_env(port)
    env.update(block)
    _save(path, data)
    return block


def disable(path: Path = DEFAULT_SETTINGS) -> list[str]:
    """Remove the telemetry keys. Returns the keys removed."""
    if not path.exists():
        return []
    data = _load(path)
    env = data.get("env", {})
    removed = [k for k in env if _is_telemetry_key(k)]
    for k in removed:
        del env[k]
    if "env" in data and not env:
        del data["env"]
    _save(path, data)
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="claude-spend setup",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="collector port (default %(default)s)")
    ap.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS, help="path to Claude Code settings.json")
    ap.add_argument("--purge", action="store_true", help="remove the telemetry keys instead of writing them")
    ap.add_argument("--force", action="store_true", help="replace an endpoint that points somewhere else without asking")
    args = ap.parse_args(argv)

    if args.purge:
        removed = disable(args.settings)
        if removed:
            print(f"removed {len(removed)} telemetry keys from {args.settings}")
        else:
            print(f"no telemetry keys in {args.settings}")
        return 0

    target = f"http://127.0.0.1:{args.port}"
    existing = current_endpoint(args.settings)
    if existing and existing != target and not args.force:
        print(f"warning: {args.settings} already exports OTEL to {existing}", file=sys.stderr)
        print("         Claude Code supports a single endpoint. Re-run with --force to replace it.", file=sys.stderr)
        return 1

    enable(args.port, args.settings)
    print(f"wrote telemetry env block to {args.settings} (endpoint {target})")
    print("Already-running Claude Code sessions will not report; start a new one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
