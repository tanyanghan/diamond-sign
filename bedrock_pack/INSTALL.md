# mcnotifier events — Bedrock behavior pack

This optional behavior pack makes a Bedrock server emit **player chat** and
**death** events to the console so mcnotifier can relay them (Bedrock's console
otherwise reports only join/leave). It uses the Script API and emits marker lines
via `console.warn`, which the bot reads from the same `console.log` it already
tails — no HTTP endpoint or `@minecraft/server-net` permission required.

- **Deaths** use a **stable** API — no experiments needed.
- **Chat** uses an **experimental** API — needs the world's **Beta APIs**
  experiment enabled.

## 1. Enable console capture (required)

In `server.properties`:

```
content-log-console-output-enabled=true
```

This mirrors the content log (including script `console` output) to the server's
stdout, which your `tee -a console.log` captures.

## 2. Install the pack

A pack directory only makes the pack *available*; the world's
`world_behavior_packs.json` is what *activates* it. BDS scans both the
server-root `behavior_packs/` (the global pool) and
`worlds/<level-name>/behavior_packs/`, so either works — the server-root one
is conventional and is probably already present:

```
behavior_packs/mcnotifier_events/        # copy this bedrock_pack/ folder here
```

Then activate it for your world by adding the **module** UUID (from
`manifest.json`) to `worlds/<level-name>/world_behavior_packs.json` (create the
file if absent; if it already lists other packs, append to the array):

```json
[
  { "pack_id": "2b062566-9ef9-4de2-a5c3-c91875c79815", "version": [1, 0, 0] }
]
```

## 3. Enable Beta APIs (only needed for chat)

Edit the world so the **Beta APIs** experiment is on. On a dedicated server, in
`worlds/<level-name>/level.dat` this is the `experiments` toggle; the easiest way
is to enable "Beta APIs" when creating/editing the world in the client, or via a
world-editing tool. Deaths work without this — chat does not.

## 4. Restart the server

On startup you should see the script module load (no errors). Then:
- A player **dies** → a line like `MCNOTIFIER {"t":"death",...}` appears in the
  console / `console.log`.
- A player **chats** (with Beta APIs on) → `MCNOTIFIER {"t":"chat",...}`.

## Spike note (first install)

The `@minecraft/server` dependency version in `manifest.json` is pinned to
`2.7.0`. If the server log shows a **script module version** error on startup
(e.g. "version not found, available: …"), paste that line — it names the version
your server expects, and the pin will be updated to match.
