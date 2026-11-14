# Changelog

## Unreleased

- Time split everywhere: every session, built-in sub-agent and root total has `time: {wall_s, active_s, idle_s}`
  (`wall = active + idle`). Active is the sum of gaps between transcript records, minus the wait before each real user
  prompt and any gap over the idle threshold (300 s, `idle_gap_s` config or `$AGENT_COST_IDLE_GAP_S`). Shown in `show`,
  `list` (ACTIVE/IDLE columns), the live page (agent panel, row tooltip, Time tile) and the status line (`active …`).
- `agent-cost export --session <sid> [--since] [--until] --json` (contract 1): read-only JSON with the session's own
  cost, tokens and time and every descendant's, for agent-verify-loop. `version --json` now prints
  `{"name", "version", "contract"}`.
- Built-in Agent-tool sub-agents get `purpose` and `run_id` from their prompt (vloop run path, vault `Operation:`) and
  agent type, so a vault-keeper retro is `verify:retro` with its run id; sessions carry `run_id` too.
- Fourth time category `waiting`: non-active time during which at least one descendant agent (Herdr, headless,
  built-in, any depth) was running, merged so overlapping agents count once. `wall = active + waiting + idle` in
  `show`, `list` (WAITING column), `roots/<sid>.json`, `export` (`waiting_ms`, contract 1 amendment), the live page and
  the status line (`act … wait … idle …`). An orchestrator waiting on long Herdr agents no longer shows that time as idle.
- Sessions without a transcript or cached split have `time: null` instead of counting as fully idle; `list` prints `-`.
- `list` DURATION is now the main session's wall time, the same source as ACTIVE/WAITING/IDLE.
- `export` is fully read-only: it no longer creates the state directory or the session index.
- The transcript cache format is bumped (v4, stores the non-active gaps) and is rebuilt once.

- `agent-cost serve`: live report page on a local server (127.0.0.1, default port 47821) that refreshes data every
  ~10 s and re-renders the page when it changes, keeping selection, filters, open folders and scroll. Reuses a healthy
  running server, restarts it after a plugin update, stops after 2 h without requests; `--stop`, `--status`,
  `--foreground`.
- `/agent-cost` runs `serve` directly and relays only the URL; `report` stays as the static export.
- Scan links Herdr children from launch records and background task output files, and moves a session first recorded
  as its own root under its parent once the link is known.
- Headless runs (`claude -p`, SDK) are flagged; those started from another session's scratchpad are linked to it as
  kind `headless`, the rest are hidden in the sidebar behind "Show headless (N)" but still counted in daily spend.
- Auxiliary sessions belong to their main session: verify-loop reviewer/QA/retro are linked from vloop state (run
  context, ledger, events, vloop session records), their task-file prompt, or the main transcript that mentions the
  run; triage cost (`claude -p`, no transcript) becomes a synthetic `vloop-triage-<run>` session; named Herdr agents and
  headless runs also link through scratchpad paths in their prompt. Live vloop headless children link from
  `VLOOP_PARENT_SESSION_ID`.
- Sessions carry a `purpose` (`verify:triage|review|qa|retro`, `vault`, `harness`, `task`), shown as a badge in the
  timeline and agent panel.
- `visible_roots` config (default `["~/Desktop"]`): roots outside are hidden from `list` (`--all`) and the sidebar
  ("Show hidden (N)" at the bottom, replacing "Show headless") and excluded from period totals unless shown.
- Incremental live refresh: `/api/data?since=<token>` returns only changed/removed roots; the page loads the full
  payload once and merges deltas. `/api/version` tracks root files only (`?roots=1` adds per-root versions), and
  unchanged root rebuilds no longer touch the file.
- Default project roots include `~/Desktop/projects`, `~/code`, `~/src`, `~/dev`, `~/work` and `~/.claude`.

- Document the CLI: `SKILL.md` (report via a separate haiku agent, status line format, data layout) and a full README.

## 0.1.0

- First release: repo scaffold, plugin manifests, session hooks, the `agent-cost` CLI and shared state helpers.
