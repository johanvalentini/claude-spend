#!/usr/bin/env bash
# Cut a release: bump version, tag, push, update the formula, publish to the tap, verify with brew.
#
# Usage: ./scripts/release.sh X.Y.Z
# Env:   TAP_DIR   existing clone of johanvalentini/homebrew-claude-spend (default: fresh clone in a temp dir)
#        SKIP_BREW=1  skip the final brew install/test/audit (e.g. no Homebrew on this machine)
#
# Steps (each idempotent enough to re-run after fixing a failure):
#   1. preflight: on main, clean tree, up to date with origin, tests + brew style pass
#   2. bump version in pyproject.toml + src/claude_spend/__init__.py, uv lock, commit "Release vX.Y.Z"
#   3. tag vX.Y.Z, push main + tag
#   4. scripts/update_formula.py (url, sha256, resources), commit "Formula: vX.Y.Z", push
#   5. copy formula into the tap repo, commit, push
#   6. brew update && brew reinstall claude-spend && brew test && brew audit --strict
set -euo pipefail

VERSION="${1:-}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: $0 X.Y.Z" >&2; exit 2; }
TAG="v$VERSION"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAP_REPO="johanvalentini/homebrew-claude-spend"
cd "$ROOT"

die() { echo "error: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

step "preflight"
[[ "$(git branch --show-current)" == "main" ]] || die "not on main"
[[ -z "$(git status --porcelain)" ]] || die "working tree not clean"
git fetch -q origin
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || die "main is not in sync with origin/main"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "tag $TAG already exists"
uv run pytest -q
if command -v brew >/dev/null; then brew style Formula/claude-spend.rb; fi

step "bump version to $VERSION"
CURRENT="$(uv run python -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
echo "    $CURRENT -> $VERSION"
sed -i '' -E "s/^version = \"[0-9.]+\"/version = \"$VERSION\"/" pyproject.toml
sed -i '' -E "s/^__version__ = \"[0-9.]+\"/__version__ = \"$VERSION\"/" src/claude_spend/__init__.py
uv lock -q
[[ "$(uv run claude-spend --version)" == "claude-spend $VERSION" ]] || die "version bump did not take"
git add pyproject.toml src/claude_spend/__init__.py uv.lock
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

step "publish formula to $TAP_REPO"
if [[ -z "${TAP_DIR:-}" ]]; then
    TAP_DIR="$(mktemp -d)/homebrew-claude-spend"
    gh repo clone "$TAP_REPO" "$TAP_DIR" -- -q
fi
cp Formula/claude-spend.rb "$TAP_DIR/Formula/claude-spend.rb"
git -C "$TAP_DIR" add Formula/claude-spend.rb
if git -C "$TAP_DIR" diff --cached --quiet; then
    echo "    tap already up to date"
else
    git -C "$TAP_DIR" commit -q -m "claude-spend $VERSION"
    git -C "$TAP_DIR" push -q
fi

if [[ "${SKIP_BREW:-}" == "1" ]] || ! command -v brew >/dev/null; then
    echo; echo "Skipping brew verification. Run on a Mac with Homebrew:"
    echo "  brew update && brew reinstall claude-spend && brew test claude-spend && brew audit --strict claude-spend"
    exit 0
fi

step "verify with Homebrew"
export HOMEBREW_NO_AUTO_UPDATE=1
brew tap johanvalentini/claude-spend >/dev/null 2>&1 || true
brew trust johanvalentini/claude-spend >/dev/null 2>&1 || true
git -C "$(brew --repository johanvalentini/claude-spend)" pull -q
brew reinstall claude-spend
[[ "$(brew --prefix)/bin/claude-spend --version" ]]
"$(brew --prefix)/bin/claude-spend" --version
brew test claude-spend
brew audit --strict claude-spend
echo
echo "Released claude-spend $TAG."
echo "  https://github.com/johanvalentini/claude-spend/releases/tag/$TAG"
echo "Note: the brew build is left installed. If the checkout launchd agent also runs on :4318,"
echo "      keep only one: brew uninstall claude-spend, or ./scripts/uninstall.sh."
