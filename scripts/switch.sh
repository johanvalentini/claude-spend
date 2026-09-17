#!/usr/bin/env bash
# Switch between the two ways of running the collector: from this checkout (dev) or from
# the installed Homebrew package (brew). Both listen on the same port and share
# ~/.claude/settings.json, ~/.config/claude-spend/config and the database, so exactly one
# may be loaded at a time. This script stops one and starts the other.
#
# Usage: ./scripts/switch.sh dev            # run the checkout via launchd (./scripts/install.sh)
#        ./scripts/switch.sh brew           # run the released bottle via brew services
#        ./scripts/switch.sh brew --head    # build and run current origin/main instead
#        ./scripts/switch.sh status         # which one is loaded, and is /health answering
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.claude-spend.collector"
FORMULA="johanvalentini/claude-spend/claude-spend"

die() { echo "error: $*" >&2; exit 1; }

port() {
    local p="${CLAUDE_SPEND_PORT:-}"
    [[ -n "$p" ]] || p="$(sed -n 's/^CLAUDE_SPEND_PORT=//p' "$HOME/.config/claude-spend/config" 2>/dev/null | tail -1)"
    echo "${p:-4318}"
}

dev_loaded()  { launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; }
brew_loaded() { command -v brew >/dev/null && brew services list 2>/dev/null | grep -qE '^claude-spend\s+(started|scheduled)'; }
brew_installed() { command -v brew >/dev/null && brew list --versions claude-spend >/dev/null 2>&1; }

wait_health() {
    local url="http://127.0.0.1:$(port)/health"
    for _ in $(seq 1 20); do
        if curl -sf "$url" >/dev/null 2>&1; then echo "    $url ok"; return 0; fi
        sleep 0.5
    done
    echo "    warning: $url did not answer within 10s" >&2
    return 1
}

status() {
    if dev_loaded; then echo "dev:  loaded  (launchd $LABEL from $PROJECT)"; else echo "dev:  not loaded"; fi
    if brew_loaded; then echo "brew: started (brew services claude-spend, $(brew list --versions claude-spend))"
    elif brew_installed; then echo "brew: installed but stopped ($(brew list --versions claude-spend))"
    else echo "brew: not installed"; fi
    if dev_loaded && brew_loaded; then echo "warning: both loaded; they fight over port $(port)" >&2; fi
    echo "health: $(curl -s "http://127.0.0.1:$(port)/health" || echo "no answer on port $(port)")"
}

case "${1:-}" in
    dev)
        if brew_loaded; then
            echo "==> stopping brew services claude-spend"
            brew services stop claude-spend
        fi
        echo "==> installing checkout agent"
        "$PROJECT/scripts/install.sh"
        ;;
    brew)
        command -v brew >/dev/null || die "brew not found"
        if dev_loaded; then
            echo "==> removing checkout agent"
            launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
            rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
        fi
        if [[ "${2:-}" == "--head" ]]; then
            echo "==> brew reinstall --HEAD (builds origin/main, not your working tree)"
            if brew_installed; then brew reinstall --HEAD "$FORMULA"; else brew install --HEAD "$FORMULA"; fi
        elif ! brew_installed; then
            echo "==> brew install"
            brew install "$FORMULA"
        fi
        echo "==> brew services restart claude-spend"
        brew services restart claude-spend
        wait_health || true
        echo "Running $(brew list --versions claude-spend). Back to the checkout: ./scripts/switch.sh dev"
        ;;
    status) status ;;
    *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
