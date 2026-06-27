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

Then activate it for your world by adding the pack's **header** UUID — the
`header.uuid` in `manifest.json`, NOT the module uuid — to
`worlds/<level-name>/world_behavior_packs.json` (create the file if absent; if it
already lists other packs, append to the array):

```json
[
  { "pack_id": "dd12725f-61ca-4f6a-bca2-170cef3008ed", "version": [1, 0, 0] }
]
```

(`pack_id` is always the `header.uuid`. If the server log says
"Configured pack (id: …) was not found", the id in `world_behavior_packs.json`
doesn't match this header uuid.)

## 3. Enable Beta APIs (only needed for chat)

Chat capture uses an experimental Script API, so the world needs the **Beta
APIs** experiment on (deaths don't — skip this step if you only want deaths).

With the **server stopped**, run the bundled helper (needs `amulet-nbt` from
`requirements-bedrock-restore.txt`) — it edits the world's `level.dat` directly,
so you don't need the game client:

```bash
python bedrock_pack/enable_beta_apis.py "worlds/<level-name>"
```

It backs up `level.dat` first and is idempotent. If chat events don't appear
after installing the pack, the experiment key may differ on your version — rerun
with both candidates:

```bash
python bedrock_pack/enable_beta_apis.py --keys beta_api,gametest "worlds/<level-name>"
```

> Enabling an experiment is **irreversible** for that world and disables
> achievements (moot on a dedicated server).

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
