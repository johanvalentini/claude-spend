#!/usr/bin/env bash
# Remove the launchd agent. With --purge-settings, also remove the telemetry env
# keys from ~/.claude/settings.json. The SQLite database is never deleted.
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.claude-spend.collector"
SETTINGS="$HOME/.claude/settings.json"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed launchd agent $LABEL."

if [[ "${1:-}" == "--purge-settings" && -f "$SETTINGS" ]]; then
    uv run --project "$PROJECT" claude-spend setup --purge
else
    echo "Telemetry env vars in $SETTINGS were left in place."
    echo "Re-run with --purge-settings to remove them, or delete the CLAUDE_CODE_ENABLE_TELEMETRY / OTEL_* keys by hand."
fi
echo "Data is still at ~/.local/share/claude-spend/usage.db and settings at ~/.config/claude-spend/config;"
echo "delete them yourself if you want them gone."
