"""Rewrite Formula/claude-spend.rb from pyproject/uv.lock and a release tarball.

Usage: uv run python scripts/update_formula.py --version 0.2.0 [--sha256 HEX]

Updates: the `url` (tag tarball), the `sha256` (fetched from GitHub if not given)
and the `resource` blocks (runtime dependency closure from uv.lock, sdist URLs).
No Homebrew needed; the resulting file still needs `brew style` and a test install,
which scripts/release.sh does.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORMULA = ROOT / "Formula" / "claude-spend.rb"
REPO = "johanvalentini/claude-spend"


def resource_blocks() -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    pk = {p["name"]: p for p in lock["package"]}
    root = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["name"]
    seen: set[str] = set()
    todo = [root]
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        todo += [d["name"] for d in pk[n].get("dependencies", [])]
    out = []
    for n in sorted(seen - {root}):
        s = pk[n].get("sdist")
        if not s:
            sys.exit(f"error: {n} has no sdist in uv.lock; Homebrew needs one")
        out.append(
            f'  resource "{n}" do\n    url "{s["url"]}"\n    sha256 "{s["hash"].removeprefix("sha256:")}"\n  end\n'
        )
    return "\n".join(out)


def tarball_sha256(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as r:
        return hashlib.sha256(r.read()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True, help="release version without the v, e.g. 0.2.0")
    ap.add_argument("--sha256", help="tarball sha256; fetched from GitHub when omitted")
    args = ap.parse_args()

    url = f"https://github.com/{REPO}/archive/refs/tags/v{args.version}.tar.gz"
    sha = args.sha256 or tarball_sha256(url)
    text = FORMULA.read_text()

    text, n_url = re.subn(r'^  url ".*"$', f'  url "{url}"', text, count=1, flags=re.M)
    text, n_sha = re.subn(r'^  sha256 ".*"$', f'  sha256 "{sha}"', text, count=1, flags=re.M)
    m = re.search(r'(depends_on "python@[\d.]+"\n\n)(.*?)(\n  def install)', text, flags=re.S)
    if not (n_url and n_sha and m):
        sys.exit("error: formula layout not recognised; edit Formula/claude-spend.rb by hand")
    text = text[: m.start(2)] + resource_blocks() + text[m.end(2) :]
    FORMULA.write_text(text)
    print(f"updated {FORMULA.relative_to(ROOT)}: v{args.version} sha256={sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
