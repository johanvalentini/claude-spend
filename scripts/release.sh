#!/usr/bin/env bash
# Cut a release: bump version, tag, push, update the formula, publish to the tap, verify with brew.
#
# Usage: ./scripts/release.sh X.Y.Z
# Env:   TAP_DIR      existing clone of johanvalentini/homebrew-claude-spend (default: fresh clone in a temp dir)
#        SKIP_BREW=1  skip the final brew install/test/audit (e.g. no Homebrew on this machine)
#
# Steps (each idempotent enough to re-run after fixing a failure):
#   1. preflight: on main, clean tree, up to date with origin, tests + brew style pass
#   2. bump version in pyproject.toml, uv lock, commit "Release vX.Y.Z"
#   3. tag vX.Y.Z, push main + tag
#   4. scripts/update_formula.py (url, sha256, resources), commit "Formula: vX.Y.Z", push
#   5. open a PR on the tap with the new formula; its CI (brew test-bot) builds bottles for
#      macOS and Linux; when green, dispatch the "brew pr-pull" workflow, which merges the PR
#      with the bottle block and uploads the bottles to the tap's GitHub Releases
#   6. brew update && brew reinstall claude-spend (from bottle) && brew test && brew audit --strict
set -euo pipefail

VERSION="${1:-}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: $0 X.Y.Z" >&2; exit 2; }
TAG="v$VERSION"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAP_REPO="johanvalentini/homebrew-claude-spend"
TAP_NAME="johanvalentini/claude-spend"
cd "$ROOT"

die() { echo "error: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

step "preflight"
[[ "$(git branch --show-current)" == "main" ]] || die "not on main"
[[ -z "$(git status --porcelain)" ]] || die "working tree not clean"
git fetch -q origin
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || die "main is not in sync with origin/main"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "tag $TAG already exists"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated"
uv run pytest -q
if command -v brew >/dev/null; then brew style Formula/claude-spend.rb; fi

step "bump version to $VERSION"
CURRENT="$(uv run python -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
echo "    $CURRENT -> $VERSION"
sed -i '' -E "s/^version = \"[0-9.]+\"/version = \"$VERSION\"/" pyproject.toml
uv lock -q
uv sync -q   # refresh installed metadata; __version__ comes from importlib.metadata
[[ "$(uv run claude-spend --version)" == "claude-spend $VERSION" ]] || die "version bump did not take"
git add pyproject.toml uv.lock
uv run git commit -q -m "Release $TAG"

step "tag and push"
git tag -a "$TAG" -m "claude-spend $TAG"
git push -q origin main "$TAG"

step "update formula from the $TAG tarball"
# GitHub serves the tag tarball almost immediately, but give it a few tries.
for i in 1 2 3 4 5 6; do
    if uv run python scripts/update_formula.py --version "$VERSION"; then break; fi
    [[ $i -lt 6 ]] || die "could not fetch the tarball for $TAG"
    sleep 5
done
if command -v brew >/dev/null; then brew style Formula/claude-spend.rb; fi
git add Formula/claude-spend.rb
uv run git commit -q -m "Formula: $TAG"
git push -q origin main

step "open pull request on $TAP_REPO"
if [[ -z "${TAP_DIR:-}" ]]; then
    TAP_DIR="$(mktemp -d)/homebrew-claude-spend"
    gh repo clone "$TAP_REPO" "$TAP_DIR" -- -q
fi
git -C "$TAP_DIR" checkout -q main && git -C "$TAP_DIR" pull -q
BRANCH="claude-spend-$VERSION"
git -C "$TAP_DIR" checkout -q -B "$BRANCH"
cp Formula/claude-spend.rb "$TAP_DIR/Formula/claude-spend.rb"
git -C "$TAP_DIR" add Formula/claude-spend.rb
git -C "$TAP_DIR" diff --cached --quiet && die "tap formula already matches; nothing to release"
git -C "$TAP_DIR" commit -q -m "claude-spend $VERSION"
git -C "$TAP_DIR" push -q -u origin "$BRANCH" --force
PR_URL="$(gh pr create -R "$TAP_REPO" --head "$BRANCH" --base main \
    --title "claude-spend $VERSION" \
    --body "Formula for https://github.com/johanvalentini/claude-spend/releases/tag/$TAG. Bottles are built by CI; merged by the brew pr-pull workflow.")"
PR_NUM="${PR_URL##*/}"
HEAD_SHA="$(git -C "$TAP_DIR" rev-parse HEAD)"
echo "    $PR_URL"

step "wait for brew test-bot (builds the bottles; several minutes)"
gh pr checks "$PR_NUM" -R "$TAP_REPO" --watch --fail-fast || die "tap CI failed: $PR_URL"

step "dispatch brew pr-pull (merges the PR, uploads bottles)"
gh workflow run publish.yml -R "$TAP_REPO" -f "pull_request=$PR_NUM" -f "head_sha=$HEAD_SHA"
sleep 10
RUN_ID="$(gh run list -R "$TAP_REPO" --workflow publish.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch "$RUN_ID" -R "$TAP_REPO" --exit-status || die "pr-pull failed: https://github.com/$TAP_REPO/actions/runs/$RUN_ID"
[[ "$(gh pr view "$PR_NUM" -R "$TAP_REPO" --json state --jq .state)" == "MERGED" ]] \
    || echo "    warning: PR $PR_NUM is not marked merged; check $PR_URL"
git -C "$TAP_DIR" checkout -q main && git -C "$TAP_DIR" pull -q
grep -q "bottle do" "$TAP_DIR/Formula/claude-spend.rb" || echo "    warning: no bottle block in the tap formula"

if [[ "${SKIP_BREW:-}" == "1" ]] || ! command -v brew >/dev/null; then
    echo; echo "Skipping brew verification. Run on a machine with Homebrew:"
    echo "  brew update && brew reinstall claude-spend && brew test claude-spend && brew audit --strict claude-spend"
    exit 0
fi

step "verify with Homebrew"
export HOMEBREW_NO_AUTO_UPDATE=1
brew tap "$TAP_NAME" >/dev/null 2>&1 || true
brew trust "$TAP_NAME" >/dev/null 2>&1 || true
git -C "$(brew --repository "$TAP_NAME")" pull -q
brew reinstall claude-spend
"$(brew --prefix)/bin/claude-spend" --version
brew test claude-spend
brew audit --strict claude-spend
echo
echo "Released claude-spend $TAG."
echo "  https://github.com/johanvalentini/claude-spend/releases/tag/$TAG"
echo "  $PR_URL"
echo "Note: the brew build is left installed. If the checkout launchd agent also runs on the same port,"
echo "      keep only one: brew uninstall claude-spend, or ./scripts/uninstall.sh."
