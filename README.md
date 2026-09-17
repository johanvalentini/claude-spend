# claude-spend

Live token and cost capture for your Claude Code sessions, via Claude Code's built-in
OpenTelemetry export. A small local collector receives the events and stores them in
SQLite; a terminal UI shows spend, cost per turn and cache efficiency by model and effort.

Nothing reads old transcripts and nothing leaves your machine: only traffic emitted
while the collector is running is stored, and the collector listens on localhost only.

```
Claude Code ──OTLP http/json──▶ claude-spend collector (launchd, :4318) ──▶ ~/.local/share/claude-spend/usage.db
                                                                                     ▲
                                                                  claude-spend tui ──┘ (read-only)
```

## Requirements

- Claude Code CLI
- Either [Homebrew](https://brew.sh) on macOS or Linux (`brew services` runs the collector
  under launchd or systemd), or macOS with [uv](https://docs.astral.sh/uv/) for the
  checkout install (`curl -LsSf https://astral.sh/uv/install.sh | sh`; Python 3.14 is
  fetched automatically if missing).

## Install with Homebrew

```sh
brew tap johanvalentini/claude-spend
brew trust johanvalentini/claude-spend  # Homebrew asks this once for third-party taps
brew install claude-spend
brew services start claude-spend   # launchd agent on 127.0.0.1:4318, survives reboots
claude-spend setup                 # writes the telemetry env block to ~/.claude/settings.json
```

Bottles are built by the tap's CI for macOS and Linux, so `brew install` is a download;
`brew install --HEAD claude-spend` builds the current `main` instead.

`claude-spend setup` refuses to overwrite an `OTEL_EXPORTER_OTLP_ENDPOINT` that already
points somewhere else; pass `--force` to replace it. Claude Code supports a single
endpoint, so you would need an OTel Collector in front to fan out.

Already-running Claude Code sessions will not report; start a new one.

Logs go to `$(brew --prefix)/var/log/claude-spend-collector.log`, rotated at 5 MB with
three old files kept. To change the port:

```sh
claude-spend setup --port 4319       # updates settings.json and ~/.config/claude-spend/config
brew services restart claude-spend   # the collector reads the config file at start
```

Do not edit the plist `brew services` generates; it is rewritten from the formula on every
start. See [Configuration](#configuration) for the file.

Undo:

```sh
claude-spend setup --purge         # remove the OTEL_* keys from ~/.claude/settings.json
brew services stop claude-spend
brew uninstall claude-spend
```

The formula lives in [`Formula/claude-spend.rb`](Formula/claude-spend.rb); the tap repo
carries a copy plus the `bottle` block its CI adds. Releases are cut with
`scripts/release.sh` (see the header of the formula).

## Install from a checkout (uv)

```sh
git clone git@github.com:johanvalentini/claude-spend.git ~/code/claude-spend
cd ~/code/claude-spend
./scripts/install.sh
```

The installer:

1. runs `uv sync` to create a local `.venv`,
2. installs and starts a launchd user agent (`com.claude-spend.collector`, KeepAlive)
   that runs the collector from this checkout on `127.0.0.1:4318`,
3. runs `claude-spend setup` to add the telemetry env block to `~/.claude/settings.json`
   (set `FORCE=1` to replace an endpoint that already points elsewhere),
4. waits for `/health` to answer.

Keep the checkout where it is: the launchd agent points at this directory. Re-run
`./scripts/install.sh` after moving it, after `git pull`, or to change the port
(`CLAUDE_SPEND_PORT=4319 ./scripts/install.sh`, stored in `~/.config/claude-spend/config`).
The collector logs to `~/Library/Logs/claude-spend-collector.log`, rotated at 5 MB.

Already-running Claude Code sessions will not report; start a new one.

## Uninstall (checkout install)

```sh
./scripts/uninstall.sh                   # stop and remove the launchd agent
./scripts/uninstall.sh --purge-settings  # also remove the OTEL_* keys from ~/.claude/settings.json
```

The database at `~/.local/share/claude-spend/usage.db` and `~/.config/claude-spend/config`
are never deleted automatically. A database left at the pre-rename path
`~/.local/share/claude-usage/usage.db` is used as a fallback while the new path does not
exist; move it to keep one file.

## Configuration

All commands read `~/.config/claude-spend/config` (or `$CLAUDE_SPEND_CONFIG`), a file of
`KEY=VALUE` lines. Environment variables of the same name override it, command-line flags
override both. `claude-spend setup --port N` writes the port here.

```sh
CLAUDE_SPEND_PORT=4318                    # collector port; also the endpoint setup writes
CLAUDE_SPEND_HOST=127.0.0.1               # bind address
CLAUDE_SPEND_DB=~/.local/share/claude-spend/usage.db
CLAUDE_SPEND_LOG=                         # collector log file (default: stderr; the service passes --log-file)
```

Restart the collector after changing it: `brew services restart claude-spend` or
`launchctl kickstart -k gui/$(id -u)/com.claude-spend.collector`.

## Watch

```sh
claude-spend tui                                   # Homebrew install
cd ~/code/claude-spend && uv run claude-spend tui  # checkout install
```

All commands are subcommands of `claude-spend` (`setup`, `collector`, `tui`, `report`).

Keys: `q` quit, `r` refresh, `d` cycle 7/14/30-day history, `1`-`5` switch tabs:
**Overview**, **By model**, **Where it goes**, **Session timeline**, **Turns & errors**.

Five charts, up to three per row, sit under the summary and follow the `d` window:

- **Daily cost (USD)**: bars, one per day.
- **Median cost per turn (USD)**: the efficiency trend to watch when switching
  models or effort levels.
- **Cache expiry premium (USD)**: per day, what main-thread requests after a pause
  longer than the 5-minute cache TTL cost over a plain cache read. Same rule as the
  *Cache expiry* lever on tab 3, so a spike here says "I left sessions idle".
- **Cache hit rate**: cache-read tokens as a share of all input tokens per day.
  Cache reads are ~10x cheaper than fresh input, so a drop here shows up in cost.
- **Median context per main-thread request**: how much the model re-reads every
  turn. Rising means sessions are running long without `/clear` or `/compact`.

## By model (tab 2)

Today's spend per model, and **cost per task** two ways, grouped by model and effort so
configurations can be compared over time:

- **Cost per turn**: one user prompt and every request it triggered, including
  subagents (linked by `prompt.id`). Labelled by the main-thread model/effort.
  Columns: turns, total, mean and median $/turn, requests per turn, median wall seconds.
- **Cost per session**: labelled by the configuration that spent the most in the
  session. Columns: sessions, total, mean and median $/session, turns per session,
  median wall minutes. Turns per session is the retry signal: a cheaper model that
  needs more turns to finish is not cheaper per task.

Housekeeping requests (`prompt_suggestion`, `away_summary`, `generate_session_title`)
are excluded from both. Both tables follow the `d` history window.

## Where it goes (tab 3)

The second tab answers "what are my tokens actually spent on" without reading
transcripts. Everything is derived from `api_request` and `tool_result` telemetry.

- **FOCUS: ranked cost levers**: each row is one thing you could change, with the
  dollars it controls over the window, its share of total spend, a detail line and a
  hint. Figures overlap (a tool's context burden is also cache-read cost), so read
  it as a ranking, not a sum. The **basis** column says how each figure was obtained:
  *reported* is Claude Code's own `cost_usd`; *priced* is telemetry token counts
  multiplied by the local price table; *modelled* adds assumptions (bytes per token,
  cache TTL, re-read count) and is the least exact. Levers:
  - *Context re-reads*: cache-read cost and median main-thread context size.
  - *Output tokens*: the most expensive token kind; driven by effort and verbosity.
  - *Cache expiry*: a main-thread request that follows a pause longer than the 5-minute
    cache TTL rewrites the whole context at the cache-write price. The figure is the
    premium over what a cache read would have cost.
  - *Tool output in context*: the sum of the tool table below.
  - *Subagents*: `agent:*` requests plus their `agent_summary` calls.
  - *Background requests*: prompt suggestions, away summaries, compaction, titles.
- **By project**, **by request source**, **by skill**: plain groupings of spend.
- **Tool output in context**: the approximate cost of each tool's results. A result of
  B bytes is taken as B/4 tokens, written to the cache once and re-read on every later
  main-thread request in the same session. `re-read tok` is that token count; `burden $`
  prices it with the session's model. Known limits: results produced inside subagents
  are counted against the main thread, and compaction is not modelled, so long
  agent-heavy sessions are over-estimated. Use it to rank tools, not to bill them.

## Session timeline (tab 4)

Pick a session with the arrow keys and see one row per user turn, in order, from
exact telemetry figures (no estimates):

- **gap**: time since the previous main-thread request in the session, and **write**:
  how the turn's cache write is classified. *cold* is the first request of the
  session; *expiry* follows a gap longer than the 5-minute cache TTL, so the whole
  context was re-written at the write price; *incremental* is a normal turn. The
  classification is the only inferred column; the token counts next to it are exact.
- **ctx**: context size of the turn's last main-thread request; **cache wr / rd / out**:
  tokens summed over every request in the turn, subagents included.
- **req / sub**: requests in the turn and how many ran in subagents; **ttft**: time to
  first token of the first request; **wall**: first to last request.
- **tools / tool out / largest**: tool calls in the turn, total result bytes, and the
  single largest result. This is the column that shows which call made the context jump.
- **prompt**: characters you typed; **skill**, **agents**: what the turn invoked.

## Turns & errors (tab 5)

- **Most expensive turns** in the window, across sessions, same columns as the timeline.
  Answers "which of my prompts cost the most" with reported dollars.
- **Cache write anatomy**: every main-thread cache write classified as cold, expiry or
  incremental, with exact tokens, the priced cost, and for expiry the premium over
  what a cache read would have cost. This is the cache-expiry lever with the numbers
  behind it.
- **API errors and refusals**: the stored `api_error` and `api_refusal` events. Retried
  requests are paid for twice, so a non-zero count here is worth a look.

## Report (for cron, launchd or a quick look)

```sh
claude-spend report --days 7          # plain text: tab 3 plus cache anatomy, top turns, errors
claude-spend report --days 30 --json  # machine-readable
```

## What is stored

- `requests`: one row per `claude_code.api_request` event: timestamp, session, model,
  query_source (main/subagent/auxiliary), effort, input/output/cache-read/cache-write
  tokens, `cost_usd` as reported by Claude Code, plus a local price-table estimate
  for cross-checking.
- `sessions`: first/last seen, terminal, project (resolved from the transcript
  *filename* under `~/.claude/projects`, contents are never read).
- `events`: user_prompt (length only), api_error, api_refusal, tool_result,
  tool_decision, assistant_response (length only). Prompt/response text is dropped
  even if content logging is enabled.
- `metric_points`: delta `claude_code.token.usage` / `cost.usage` for reconciliation.

## Ad-hoc SQL

```sh
sqlite3 ~/.local/share/claude-spend/usage.db \
  "select date(ts,'unixepoch','localtime') d, round(sum(cost_usd),2) usd from requests group by d order by d desc limit 14"
```

## Troubleshooting

- **TUI shows nothing**: open a new Claude Code session and send a prompt. Then check
  `curl -s localhost:4318/health` and `tail ~/Library/Logs/claude-spend-collector.log`.
- **Restart the collector**: `brew services restart claude-spend`, or for a checkout
  install `launchctl kickstart -k gui/$(id -u)/com.claude-spend.collector`
- **Port in use**: `claude-spend setup --port 4319` then restart the collector (both installs
  read the port from `~/.config/claude-spend/config`). For the checkout install
  `CLAUDE_SPEND_PORT=4319 ./scripts/install.sh` does both steps.
- **Is the agent loaded?** `brew services info claude-spend`, or
  `launchctl print gui/$(id -u)/com.claude-spend.collector | head`
- **Homebrew and checkout installs both present**: they fight over the port. Run
  `./scripts/switch.sh dev` or `./scripts/switch.sh brew` so only one is loaded.

## Dev

```sh
uv run pytest
uv run claude-spend tui                                                  # TUI from the checkout, no install needed
uv run claude-spend collector -v --port 4319 --db /tmp/test.db           # throwaway collector next to the live one
launchctl kickstart -k gui/$(id -u)/com.claude-spend.collector           # restart the checkout collector after a code change
```

### Developing vs trying the real Homebrew package

Both install modes listen on the same port and share `~/.claude/settings.json`,
`~/.config/claude-spend/config` and the database, so only one collector may be loaded at
a time. `scripts/switch.sh` stops one and starts the other; nothing else needs to change
and history carries over.

```sh
./scripts/switch.sh status        # which one is loaded, and whether /health answers
./scripts/switch.sh brew          # stop the checkout agent, brew install (if needed) and start the bottle
./scripts/switch.sh brew --head   # same, but build the current origin/main from git
./scripts/switch.sh dev           # brew services stop, then ./scripts/install.sh for this checkout
```

Day to day, stay on `dev`: the launchd agent runs `uv run` from this directory, so a code
change is live after the `kickstart` above (re-run `./scripts/install.sh` after a
dependency change). Switch to `brew` to check a release the way a user gets it, or to
`brew --head` to check the formula itself. Note that `--head` builds what is pushed to
`main`, not your working tree. Switch back with `dev` when done; leaving both loaded is
the one thing to avoid.

`brew install --HEAD --build-from-source johanvalentini/claude-spend/claude-spend` is what
`switch.sh brew --head` runs, if you prefer to do it by hand.
