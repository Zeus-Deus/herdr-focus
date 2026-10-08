# Native hooks Herdr would need for full parity

Herdr Focus runs entirely on Herdr 0.9.1's public plugin surface. Three behaviors from the
plan cannot be built there without core support. Each hook below is additive and optional:
nothing changes for clients or plugins that do not use it.

## 1. Plugin actions in the Agent-row context menu

**Gap.** Right-clicking an Agent row does nothing in 0.9.1. `src/client/shell/mouse.rs`
routes right-clicks to workspace, tab and pane menus only, and
`src/client/shell/context_menu.rs` builds fixed item lists. Manifest `contexts` are stored
but no client UI reads them.

**Proposal.**

- New manifest context `agent`:
  ```toml
  [[actions]]
  id = "settle"
  title = "Settle"
  contexts = ["agent"]
  ```
- Add `ClientContextMenuTarget::Agent { pane_id }`, opened by a right-click on an Agent
  row. Items: Herdr's own (Focus, Rename agent) followed by enabled plugin actions with the
  `agent` context, grouped under the plugin name.
- The snapshot already advertises capabilities; add the action list (id, title, plugin id)
  to it so clients can build the menu without a round trip.
- Invocation sends `plugin.action.invoke` with `invocation_source = "agent_context_menu"`
  and `target_pane_id`, so the plugin acts on the clicked row, not the focused pane.
- Optional per-row labels: let an action declare `title_token = "focus_settle_label"` to
  read its label ("Settle" / "Unsettle") from the row's pane tokens.

## 2. Client identity on focus

**Gap.** `pane.focused` reports selection changes from any client without saying which, so
a plugin cannot keep per-client "seen" state. The TUI already tracks its own viewed
completions, which is why the server's `done` and a client's ✓ can disagree.

**Proposal.** Add an optional `client_id` (stable for the attachment) to `pane.focused`,
and expose the client's own seen-set through `agent.list` when the caller passes
`client_id`. Herdr Focus would then acknowledge results per client instead of globally.

## 3. Client-local Agent view sections

**Gap.** `agent.view.set` is one filter and sort per server. It cannot draw section
headers ("Needs you", "Working" shelf, "Settled · 3"), collapse a shelf, or differ per
client. Reordering workspaces globally is not a substitute.

**Proposal.** Extend the view with optional `sections`:

```json
{"sections": [
  {"label": "Needs you", "filter": {"op": "eq", "field": "status", "value": "blocked"}},
  {"label": "Working", "filter": {"op": "in", "field": "status", "values": ["working"]},
   "collapsed": true},
  {"label": "Settled", "filter": {"op": "exists", "field": {"token": "fhide"}}, "collapsed": true}
]}
```

Collapse state stays client-local. Rows that match no section fall through to an
unlabelled default section, so existing views keep working unchanged.

## What the plugin does today instead

| Want | Today |
| --- | --- |
| Right-click actions on an Agent row | `prefix+a` popup menu listing every agent; per-key bindings for the focused pane |
| Per-client acknowledgement | focus by any client acknowledges finished results (`ack_on_focus`); manual unread is sticky |
| Working shelf / settled section | `layout = "shelf"` sorts working rows last; settled rows are filtered out and shown with "show settled" |
