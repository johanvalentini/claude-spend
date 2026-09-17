"""claude-spend: one command wrapping the collector, TUI, report and setup.

Usage:
  claude-spend setup [--purge]     enable/disable telemetry export in ~/.claude/settings.json
  claude-spend collector [...]     run the OTLP receiver (normally started by launchd / brew services)
  claude-spend tui [...]           open the terminal UI
  claude-spend report [...]        print a text/JSON summary

Any further arguments are passed to the subcommand; use `claude-spend <cmd> --help`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from . import __version__


def _commands() -> dict[str, Callable[[list[str]], int]]:
    # Imported lazily so `claude-spend setup` does not import textual.
    def setup(argv):
        from .settings import main

        return main(argv)

    def collector(argv):
        from .collector import main

        return main(argv)

    def tui(argv):
        from .tui import main

        return main(argv)

    def report(argv):
        from .report import main

        return main(argv)

    return {"setup": setup, "collector": collector, "tui": tui, "report": report}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    commands = _commands()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv[0] in ("-V", "--version"):
        print(f"claude-spend {__version__}")
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd not in commands:
        print(f"claude-spend: unknown command {cmd!r}", file=sys.stderr)
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return commands[cmd](rest)


if __name__ == "__main__":
    sys.exit(main())
