"""Live Claude Code spend collector and TUI."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import config

try:
    __version__ = version("claude-spend")
except PackageNotFoundError:  # running from an unpacked source tree without install
    __version__ = "0+unknown"

_DATA_DIR = Path.home() / ".local/share/claude-spend"
_LEGACY_DB = Path.home() / ".local/share/claude-usage/usage.db"  # pre-rename location


def _default_db() -> Path:
    configured = config.get("CLAUDE_SPEND_DB")
    if configured:
        return Path(configured).expanduser()
    new = _DATA_DIR / "usage.db"
    if not new.exists() and _LEGACY_DB.exists():
        return _LEGACY_DB
    return new


DEFAULT_DB = _default_db()
DEFAULT_HOST = config.get("CLAUDE_SPEND_HOST", "127.0.0.1")
DEFAULT_PORT = int(config.get("CLAUDE_SPEND_PORT", "4318"))
DEFAULT_LOG = config.get("CLAUDE_SPEND_LOG")  # None: log to stderr
