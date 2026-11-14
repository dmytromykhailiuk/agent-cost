# Changelog

## Unreleased

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
