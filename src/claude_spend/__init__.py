"""Live Claude Code spend collector and TUI."""

from pathlib import Path
import os

__version__ = "0.1.0"

_DATA_DIR = Path.home() / ".local/share/claude-spend"
_LEGACY_DB = Path.home() / ".local/share/claude-usage/usage.db"  # pre-rename location


def _default_db() -> Path:
    if "CLAUDE_SPEND_DB" in os.environ:
        return Path(os.environ["CLAUDE_SPEND_DB"])
    new = _DATA_DIR / "usage.db"
    if not new.exists() and _LEGACY_DB.exists():
        return _LEGACY_DB
    return new


DEFAULT_DB = _default_db()
DEFAULT_HOST = os.environ.get("CLAUDE_SPEND_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("CLAUDE_SPEND_PORT", "4318"))
