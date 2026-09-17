"""User configuration: ~/.config/claude-spend/config (KEY=VALUE lines).

Precedence for every setting: command-line flag, then environment variable, then this
file, then the built-in default. The keys are the environment variable names:

    CLAUDE_SPEND_PORT=4318
    CLAUDE_SPEND_HOST=127.0.0.1
    CLAUDE_SPEND_DB=/path/to/usage.db
    CLAUDE_SPEND_LOG=/path/to/collector.log

The file lives in the user's home so the collector started by launchd or systemd, the TUI
and `claude-spend setup` all read the same values regardless of how the package was
installed. `claude-spend setup --port N` writes CLAUDE_SPEND_PORT here.
"""

from __future__ import annotations

import os
from pathlib import Path

KEYS = ("CLAUDE_SPEND_PORT", "CLAUDE_SPEND_HOST", "CLAUDE_SPEND_DB", "CLAUDE_SPEND_LOG")


def path() -> Path:
    if "CLAUDE_SPEND_CONFIG" in os.environ:
        return Path(os.environ["CLAUDE_SPEND_CONFIG"])
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "claude-spend" / "config"


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def load(p: Path | None = None) -> dict[str, str]:
    p = p or path()
    try:
        return parse(p.read_text())
    except (FileNotFoundError, OSError):
        return {}


def get(key: str, default: str | None = None) -> str | None:
    """Environment first, then the config file, then `default`."""
    if key in os.environ:
        return os.environ[key]
    return load().get(key, default)


def set_values(values: dict[str, str], p: Path | None = None) -> Path:
    """Write or replace KEY=VALUE lines, keeping comments and unrelated lines."""
    p = p or path()
    lines = p.read_text().splitlines() if p.exists() else []
    pending = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip().removeprefix("export ").strip() if "=" in line else None
        if key in pending and not line.lstrip().startswith("#"):
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in pending.items()]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out) + "\n")
    return p
