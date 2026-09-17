---
name: release
description: Cut and publish a claude-spend release (version bump, git tag, Homebrew formula and tap update, brew verification). Use when the user asks to release, cut a version, bump the version, publish to Homebrew, or update the tap.
---

# Release claude-spend

Everything is driven by `scripts/release.sh X.Y.Z`. Do not hand-edit versions, tags or the
formula for a release; the script keeps the main repo, the tag and the tap consistent.

## Decide the version

Read the current version from `pyproject.toml` and the commits since the last tag:

```sh
git describe --tags --abbrev=0
git log --oneline "$(git describe --tags --abbrev=0)..HEAD"
```

Semver: patch for fixes, minor for new views/commands/columns, major for breaking DB or
CLI changes. If the user did not name a version, propose one with a one-line reason and
use it unless they object.

## Run

```sh
./scripts/release.sh X.Y.Z
```

Requirements: on `main`, clean tree, in sync with `origin/main`, `gh` authenticated, and
Homebrew present for the final verification (set `SKIP_BREW=1` to skip it). The script:

1. runs the tests and `brew style`,
2. bumps `pyproject.toml`, `src/claude_spend/__init__.py`, `uv.lock` and commits `Release vX.Y.Z`,
3. tags and pushes,
4. rewrites `Formula/claude-spend.rb` (tarball url + sha256, resource blocks regenerated from
   `uv.lock`) via `scripts/update_formula.py`, commits `Formula: vX.Y.Z`, pushes,
5. copies the formula into a fresh clone of `johanvalentini/homebrew-claude-spend` and pushes,
6. `brew reinstall claude-spend`, `brew test`, `brew audit --strict`.

## If a step fails

The steps are ordered so that re-running after a fix is safe, except:

- **Tag already pushed but a later step failed**: fix the cause, then continue by hand from
  that step (the commands are in the script header). Do not delete a pushed tag unless the
  tarball was never used by the tap.
- **Tests or `brew style` fail in preflight**: nothing has been changed yet; fix and re-run.
- **`update_formula.py` says the layout is not recognised**: the formula was edited in a way
  the regex does not expect. Restore the `url` / `sha256` / resource-block structure, then
  re-run only `uv run python scripts/update_formula.py --version X.Y.Z`.
- **`brew audit` complains about a resource**: a dependency without an sdist on PyPI or a
  new transitive dependency. Check `uv.lock`, then `brew update-python-resources claude-spend`
  as a second opinion.

## After

- The brew build is left installed. The checkout launchd agent and `brew services` both
  want port 4318; keep only one (`brew uninstall claude-spend` or `./scripts/uninstall.sh`).
- Report: version, both commit hashes, tag URL, and the brew test/audit result. If audit or
  test failed, say so with the output; do not report the release as done.
