# Herdr Focus: interaction contract

This is the behavior the plugin implements and the tests pin down. It extends Herdr's
agent rows; it never replaces Herdr's lifecycle state.

## Four separate records

| Record | Source | Persisted |
| --- | --- | --- |
| Runtime | Herdr's effective status: `idle`, `working`, `blocked`, `done`, `unknown` | no, re-read on every event |
| Background tasks | provider transcript evidence (Claude Code today) | no, rebuilt from the transcript |
| Human triage | unread, review acknowledgement, settled, snoozed-until, pinned, keep-active, manual watch, title ownership | yes, `state.json` per Herdr server |
| Projection | title slot, status label, eye badge, sort rank, hidden flag, workspace rollup | no, derived and diffed |

Identity is `session:<agent>:<native session id>` when Herdr has an official session
reference, otherwise `terminal:<terminal id>`. Pane IDs are never keys: they change on
moves and get reused. A terminal-keyed record is merged into the session key once the
session is reported. State is kept per Herdr socket, so two servers never share records.

## What a row says

Priority, first match wins:

| Category | When | Status label | Title |
| --- | --- | --- | --- |
| Needs you | Herdr status `blocked` | `Needs you 2m` (red, bold) | bold |
| Working | Herdr status `working` | `Working 3m` (amber) | dim |
| Parked | settled or snoozed | `Settled`, `Snoozed 1h` | dim, row hidden from the Agents view |
| Done | Herdr `done`, or a turn finished while you were in another pane | `Done` (green, bold) | bold |
| Unread | marked unread by you, or woken from a snooze | `Unread`, `Woke` (green, bold) | bold |
| Watching | live classified watch tasks, or manual watch | `<eye> Watching 2`, `<eye> Manual watch` (cyan) | dim |
| Unknown / Idle | otherwise | `Unknown`, `Idle 5m` (dim) | plain |

- The status readout itself is never dimmed; only the title recedes (CodeMux rule).
- The focused pane never recedes.
- Watches are shown next to any other state: `Done · <eye> 2` means the result is ready and
  two watches are still running. The eye clears only when the last watch ends.
- Elapsed time appears only after the daemon has seen the transition. No fabricated times.
- Workspace rollup: `Needs you` / `2 need you`, else `N done`, else `Working`; plus `<eye> N`.

## Transitions

| Event | Effect |
| --- | --- |
| `working` → idle while another pane is focused | Done (finished, unread) |
| focus that pane (`ack_on_focus`) | finished result acknowledged; manual unread stays |
| `working` or `blocked` starts | settled/snoozed rows come back; manual unread clears on new work |
| mark read on a `done` row | stores the acknowledged `state_change_seq`; Herdr focus is not touched |
| settle | refused for Needs you and Working; acknowledges the result; never stops processes |
| unsettle | back in the list, protected from auto-settle until new activity |
| snooze | refused for Needs you and Working; wakes at the deadline as `Woke` |
| pin | sorts first; pinned rows are never auto-settled |
| watch | manual eye for providers without task evidence; labelled "Manual watch" |
| undo | restores the triage fields of the last change (bulk settles undo as one step) |
| agent relaunched in a new terminal (e.g. resumed after restart) | older task evidence is dropped |
| agent leaves the pane | its task evidence is dropped |

Automatic settling (`auto_settle_days`, off by default) only touches idle, read, unpinned,
unwatched, not-kept-active rows.

## Background watches

Each adapter reads the agent's own records, so watches are detected, not guessed. Commands
matching `monitoring.service_patterns` (dev servers and similar) are services, never watches.

| Agent | Starts | Ends |
| --- | --- | --- |
| Claude Code | `Monitor`, or `Bash` with `run_in_background`, once the result returns the task id | `<task-notification>` with a terminal status, monitor "stream ended", `TaskStop`, Monitor timeout |
| Codex | tool output "Process running with session ID N" (code mode: `"session_id": N`) | `item_completed` CommandExecution with `process_id` N (legacy: `write_stdin` output with an exit code) |
| OpenCode | a child session (background `task` subagent) whose last assistant message is unfinished | that message completes |
| Hermes | an entry in `<home>/processes.json` owned by the session | the entry goes, or its pid is dead, a zombie, or reused (start time differs) |

Evidence also goes stale after `max_task_hours`, when the agent leaves the pane, and when
the same conversation reappears in a new terminal (a relaunch or resume). Pi has no
background shell; Cursor, Copilot and others record none, so they get the manual watch.

## Where conversations are found

From Herdr's official session reference (`agent_session`): Claude `~/.claude/projects`,
Codex `~/.codex/sessions` (+ `state_*.sqlite` for names), OpenCode
`~/.local/share/opencode/opencode.db` (read-only; only the session, message and part
tables), Pi `~/.pi/agent/sessions`, Cursor `~/.cursor/projects/*/agent-transcripts`, Copilot
`~/.copilot/session-state`. Hermes is found even without Herdr's Hermes plugin: the live
entry in `<home>/runtime/active_sessions.json` whose process runs in the pane, then the CLI's
per-terminal breadcrumb, across `~/.hermes` and every `~/.hermes/profiles/*`.

## Titles

Order: your manual title (plugin rename or a Herdr pane name) > a model title you asked for
("generate a new title") > the agent's own title (`titles.agent_titles`) > model title >
fallback. Agents that name their own sessions get no automatic model call.
The fallback is the agent's own terminal title when it is meaningful (not a shell prompt or
the bare agent name; Codex's ` | project` suffix is dropped), else the first prompt trimmed
to a few words. Model titles run on one background thread with a per-minute budget, are
cached by input hash, and are dropped if the record's title generation changed while the
request was in flight (rename, reset, regenerate).

## Targets

Keyboard actions act on the pane Herdr reports in the invocation context. Menu actions send
the selected row's pane ID *and* record key; if that pane now hosts a different
conversation the daemon refuses instead of acting on the wrong agent.

## Known limits

- Herdr 0.9.1 has no Agent-row context menu and no plugin hook for one. The popup menu
  (`prefix+a`) and key bindings are the fallback; see `native-hooks.md` for the core hook
  that would give native right-click parity.
- `pane.focused` carries no client identity, so with two attached clients either one's
  focus acknowledges a finished result. Manual unread is unaffected.
- The Agents view supports one owner. Set `sidebar.agent_view = false` if another plugin
  owns it; tokens and rollups still work.
- Sidebar row styles are fixed hex colors (CodeMux's dark-theme status tokens). Herdr
  token styles accept hex only.
