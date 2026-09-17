#!/usr/bin/env bash
# Install the collector as a launchd user agent and enable OTEL export in Claude Code.
#
# Usage: ./scripts/install.sh
# Env:   CLAUDE_SPEND_PORT  port the collector listens on (default 4318)
#        FORCE=1            replace an OTEL endpoint that already points elsewhere
#
# Re-running is safe: it re-syncs dependencies, reloads the agent and rewrites the env block.
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.claude-spend.collector"
PLIST_SRC="$PROJECT/launchd/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${CLAUDE_SPEND_PORT:-4318}"
SETTINGS="$HOME/.claude/settings.json"

die() { echo "error: $*" >&2; exit 1; }

echo "==> preflight"
[[ "$(uname -s)" == "Darwin" ]] || die "this installer uses launchd and only supports macOS"
command -v uv >/dev/null || die "uv not found. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v claude >/dev/null || echo "    warning: 'claude' CLI not on PATH; install Claude Code before expecting data"
UV="$(command -v uv)"

echo "==> syncing dependencies"
(cd "$PROJECT" && uv sync --quiet)

echo "==> installing $PLIST_DST"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s|__UV__|$UV|g" -e "s|__PROJECT__|$PROJECT|g" -e "s|__HOME__|$HOME|g" -e "s|__PORT__|$PORT|g" \
    "$PLIST_SRC" > "$PLIST_DST"
plutil -lint -s "$PLIST_DST" || die "generated plist is invalid"
# Remove the pre-rename agent if it is still loaded (same port).
launchctl bootout "gui/$(id -u)/com.claude-usage.collector" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.claude-usage.collector.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "==> enabling telemetry in $SETTINGS"
# Refuses (exit 1) if settings.json already exports OTEL somewhere else; pass FORCE=1 to replace.
uv run --project "$PROJECT" claude-spend setup --port "$PORT" ${FORCE:+--force}

echo "==> health check"
ok=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
done
if [[ -z "$ok" ]]; then
    echo "collector did not answer on :$PORT after 10s. Last log lines:" >&2
    tail -n 20 "$HOME/Library/Logs/claude-spend-collector.log" >&2 || true
    exit 1
fi
echo "    collector is up on 127.0.0.1:$PORT"

cat <<MSG

Done. New Claude Code sessions will report to the collector (already-open ones will not).

  Watch:      cd $PROJECT && uv run claude-spend-tui
  Logs:       tail -f ~/Library/Logs/claude-spend-collector.log
  Restart:    launchctl kickstart -k gui/$(id -u)/$LABEL
  Uninstall:  $PROJECT/scripts/uninstall.sh [--purge-settings]
MSG
