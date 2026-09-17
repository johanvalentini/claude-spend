"""Plain-text cost attribution report for cron/launchd or a quick terminal check.

Run: claude-spend-report [--days N] [--db PATH] [--json]

Prints the same figures as the TUI's "Where it goes" tab: ranked cost levers,
spend by project, request source, skill, and the approximate context burden of
each tool's output. Nothing reads transcripts.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import DEFAULT_DB
from . import queries as q


def _usd(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "-"


def _tok(n: float | None) -> str:
    n = n or 0
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(int(n))


def _table(headers: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> str:
    if not rows:
        return "  (no data)\n"
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    def line(cells):
        return "  " + "  ".join(
            c.rjust(widths[i]) if i in right else c.ljust(widths[i]) for i, c in enumerate(cells)
        )
    return "\n".join([line(headers), line(["-" * w for w in widths]), *(line(r) for r in rows)]) + "\n"


def collect(conn, days: int) -> dict:
    since = q._day_start(days - 1)
    t = q.totals_since(conn, since)
    return {
        "days": days,
        "since": time.strftime("%Y-%m-%d", time.localtime(since)),
        "total_cost": t["cost"],
        "requests": t["n"],
        "tokens": {"input": t["inp"], "output": t["out"], "cache_read": t["cr"], "cache_write": t["cc"]},
        "components": q.cost_components(conn, since),
        "levers": q.levers(conn, since),
        "by_project": [dict(r) for r in q.cost_by_project(conn, since)],
        "by_source": q.cost_by_source(conn, since),
        "by_skill": [dict(r) for r in q.cost_by_skill(conn, since)],
        "tools": [{k: v for k, v in b.items()} for b in q.context_burden(conn, since)],
        "cache_expiry": q.cache_expiry_waste(conn, since),
        "cache_anatomy": q.cache_write_anatomy(conn, since),
        "top_turns": q.top_turns(conn, since, 10),
        "errors": q.error_summary(conn, since),
        "recent_errors": q.errors_since(conn, since, 10),
    }


def render(d: dict) -> str:
    total = d["total_cost"] or 1
    comp = d["components"]
    out = [
        f"Claude Code spend, last {d['days']} days (since {d['since']})",
        f"  total {_usd(d['total_cost'])}   requests {d['requests']}   "
        f"tokens in {_tok(d['tokens']['input'])}  out {_tok(d['tokens']['output'])}  "
        f"cache rd {_tok(d['tokens']['cache_read'])}  cache wr {_tok(d['tokens']['cache_write'])}",
        "  by token kind (priced from local table): " + "  ".join(
            f"{k.replace('_', ' ')} {_usd(v)} ({v / total * 100:.0f}%)" for k, v in comp.items()
        ),
        "",
        "FOCUS (ranked levers; figures overlap, rank is what matters)",
        "  basis: reported = Claude Code cost_usd; priced = telemetry tokens x local price table; "
        "modelled = priced + assumptions (bytes/token, cache TTL, re-read count)",
        _table(
            ["lever", "cost", "basis", "share", "detail"],
            [[l["lever"], _usd(l["cost"]), l["basis"], f"{l['share'] * 100:.0f}%", l["detail"]] for l in d["levers"]],
            right={1, 3},
        ),
    ]
    for l in d["levers"][:3]:
        out.append(f"  * {l['lever']}: {l['hint']}")
    out += [
        "",
        "BY PROJECT",
        _table(
            ["project", "cost", "share", "sessions", "requests", "context tok", "out tok"],
            [[r["project"], _usd(r["cost"]), f"{r['cost'] / total * 100:.0f}%", str(r["sessions"]),
              str(r["n"]), _tok(r["ctx"]), _tok(r["out"])] for r in d["by_project"][:12]],
            right={1, 2, 3, 4, 5, 6},
        ),
        "BY REQUEST SOURCE",
        _table(
            ["source", "kind", "cost", "share", "requests", "median ctx"],
            [[r["source"], r["category"], _usd(r["cost"]), f"{r['share'] * 100:.0f}%", str(r["n"]),
              _tok(r["median_ctx"])] for r in d["by_source"][:12]],
            right={2, 3, 4, 5},
        ),
        "BY SKILL",
        _table(
            ["skill", "cost", "turns", "requests"],
            [[r["skill_name"], _usd(r["cost"]), str(r["turns"]), str(r["n"])] for r in d["by_skill"][:12]],
            right={1, 2, 3},
        ),
        "TOOL OUTPUT IN CONTEXT (ESTIMATE: bytes/4 tokens, written once, re-read by every later main-thread request)",
        _table(
            ["tool", "burden (est)", "calls", "fail%", "total out", "median out", "re-read tok"],
            [[r["tool"], _usd(r["cost"]), str(r["calls"]), f"{r['fail_rate'] * 100:.0f}",
              f"{r['bytes'] / 1024:.0f}KB", f"{r['median_bytes'] / 1024:.1f}KB", _tok(r["reread_tokens"])]
             for r in d["tools"][:12]],
            right={1, 2, 3, 4, 5, 6},
        ),
    ]
    out += [
        "CACHE WRITE ANATOMY (main thread; kind uses the 5 min TTL, tokens are exact, $ priced)",
        _table(
            ["kind", "requests", "tokens", "priced", "share", "premium vs read"],
            [[r["kind"], str(r["n"]), _tok(r["tokens"]), _usd(r["cost"]), f"{r['share'] * 100:.0f}%",
              _usd(r["premium"]) if r["kind"] == "expiry" else "-"] for r in d["cache_anatomy"]],
            right={1, 2, 3, 4, 5},
        ),
        "MOST EXPENSIVE TURNS (reported cost; one user prompt and everything it triggered)",
        _table(
            ["time", "project", "model", "eff", "cost", "req", "sub", "ctx", "cache wr", "out", "wall",
             "tools", "largest result", "prompt", "skill", "agents"],
            [[time.strftime("%m-%d %H:%M", time.localtime(t["ts"])), (t["project"] or "?")[:24],
              (t["model"] or "?").replace("claude-", ""), t["effort"] or "-", _usd(t["cost"]), str(t["n"]),
              str(t["n_sub"]), _tok(t["ctx"]), _tok(t["cc"]), _tok(t["out"]), f"{t['wall_s']:.0f}s",
              str(t["tools"]), f"{t['top_tool']} {t['top_bytes'] / 1024:.0f}KB" if t["top_tool"] else "-",
              str(t["prompt_len"] if t["prompt_len"] is not None else "-"), t["skill"] or "-",
              ",".join(t["agents"]) or "-"] for t in d["top_turns"]],
            right={4, 5, 6, 7, 8, 9, 10, 11, 13},
        ),
    ]
    es = d["errors"]
    out.append(f"API ERRORS  {es['errors']} errors, {es['refusals']} refusals in {es['requests']} requests")
    for e in d["recent_errors"]:
        out.append(
            f"  {time.strftime('%m-%d %H:%M', time.localtime(e['ts']))}  {e['kind']}  {e['model'] or ''}  "
            f"{e['status'] or ''}  {str(e['error'] or '')[:60]}"
        )
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    if not args.db.exists():
        print(f"no database at {args.db}", file=sys.stderr)
        return 1
    d = collect(q.open_ro(args.db), args.days)
    if args.json:
        json.dump(d, sys.stdout, indent=1, default=str)
        print()
    else:
        sys.stdout.write(render(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
