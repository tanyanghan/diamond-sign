# mcnotifier events — Bedrock behavior pack

This optional behavior pack makes a Bedrock server emit **player chat** and
**death** events to the console so mcnotifier can relay them (Bedrock's console
otherwise reports only join/leave). It uses the Script API and emits marker lines
via `console.warn`, which the bot reads from the same `console.log` it already
tails — no HTTP endpoint or `@minecraft/server-net` permission required.

The pack ships pinned to the **`"beta"`** script module (chat uses an
experimental API), which requires the world's **Beta APIs** experiment. Deaths
work on the stable module too — see "Deaths only" below to skip the experiment.

## 1. Enable console capture (required)

In `server.properties`:

```
content-log-console-output-enabled=true
```

This mirrors the content log (including script `console` output) to the server's
stdout, which your `tee -a console.log` captures. On startup the server prints
`Content logging to console is enabled.`

## 2. Install the pack

A pack directory only makes the pack *available*; the world's
`world_behavior_packs.json` is what *activates* it. BDS scans both the
server-root `behavior_packs/` (the global pool) and
`worlds/<level-name>/behavior_packs/`, so either works — the server-root one is
conventional and usually already present:

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

## 3. Enable Beta APIs (required for chat / the default `"beta"` module)

With the **server stopped**, run the bundled helper (needs `amulet-nbt` from
`requirements-bedrock-restore.txt`) — it edits the world's `level.dat` directly,
so you don't need the game client:

```bash
python bedrock_pack/enable_beta_apis.py "worlds/<level-name>"
```

It sets both known experiment keys (`gametest` is the one current versions
honor), backs up `level.dat`, and is idempotent.

> Enabling an experiment is **irreversible** for that world and disables
> achievements (moot on a dedicated server).

## 4. Restart the server

On startup, success looks like:

```
Experiment(s) active: gtst
Pack Stack - [00] mcnotifier events (id: dd12725f-…) @ behavior_packs/mcnotifier_events
```

with **no** `[Scripting] ... chatSend unavailable` error. Then:
- A player **dies** → `[… WARN] [Scripting] MCNOTIFIER {"t":"death",…}`
- A player **chats** → `[… WARN] [Scripting] MCNOTIFIER {"t":"chat",…}`

Common startup errors:
- `requesting dependency on beta APIs … but the Beta APIs experiment is not
  enabled` → run step 3 (the experiment key didn't take; the helper now sets
  `gametest`).
- `Configured pack (id: …) was not found` → wrong UUID in step 2 (use the header
  uuid).

## 5. Turn it on in the bot

In the bot's `.env`:

```
BEDROCK_SCRIPT_EVENTS=true   # ingest death markers; enables /deaths, /death_summary
CHAT_RELAY=true              # relay in-game chat to the chat platforms
```

Restart the bot. Deaths now announce + record (like Java); chat is relayed to
every authorized chat as `💬 <player>: <message>`.

## Deaths only (no experiment)

If you only want death notifications and don't want the irreversible Beta APIs
experiment, edit `manifest.json` and change the dependency `"version": "beta"` to
a **stable** version your server provides (e.g. `"2.7.0"`). The pack then loads
without the experiment; deaths work, chat does not (the script logs
`chatSend unavailable` and carries on). Set only `BEDROCK_SCRIPT_EVENTS=true`.
