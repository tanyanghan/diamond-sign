# Diamond Sign events — Bedrock behavior pack

This optional behavior pack makes a Bedrock server emit **player chat** and
**death** events to the console so Diamond Sign can relay them (Bedrock's console
otherwise reports only join/leave). It uses the Script API and emits marker lines
via `console.warn`, which the bot reads from the same `console.log` it already
tails — no HTTP endpoint or `@minecraft/server-net` permission required.

The pack ships pinned to the **`"beta"`** script module (chat uses an
experimental API), which requires the world's **Beta APIs** experiment. Deaths
work on the stable module too — see "Deaths only" below to skip the experiment.

## Quick install (one command)

With the **server stopped**, from the repo root:

```bash
python install_bedrock_pack.py
```

It reads the Bedrock server(s) from `diamondsign.json` (if more than one, it
lists them and asks which — or pass `--server <name>`), takes the world's
`level-name` from that server's `server.properties`, then: copies the pack into
`behavior_packs/diamondsign_events/`, activates it in the world's
`world_behavior_packs.json`, sets `content-log-console-output-enabled=true` in
`server.properties`, verifies the server isn't running and enables the **Beta
APIs** experiment in `level.dat`, and sets that server's
`bedrock_script_events: true` + `chat_relay: true` in `diamondsign.json`.

It first confirms the server is a Bedrock server and prompts you to confirm it's
stopped (the install edits the world and irreversibly enables an experiment).
Flags:

- `--server <name>` — which Bedrock server (name/key from `diamondsign.json`) to
  act on; if omitted and several exist, you're prompted.
- `--uninstall` — reverse it: remove the pack, deactivate it in
  `world_behavior_packs.json` (preserving other packs), turn
  `content-log-console-output-enabled` and the two config flags back off. The Beta
  APIs experiment is **not** undone — Bedrock can't disable an experiment once a
  world has used it, so `level.dat` is left as-is (harmless with the pack gone).
- `--deaths-only` — skip the experiment (deaths only; no chat, no amulet libs, sets
  only `bedrock_script_events`).
- `--yes` / `-y` — skip the "is the server stopped?" prompt (non-interactive use).
- `--force` — skip the automated "server not running" lock check (implies `--yes`).
- `--no-config` — don't modify `diamondsign.json`.

The experiment step needs `amulet-nbt`/`amulet-leveldb`
(`requirements-bedrock-restore.txt`). After it finishes, just restart the server
and the bot. The manual steps below document what the installer does.

---

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
behavior_packs/diamondsign_events/        # copy this bedrock_pack/ folder here
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

The quick installer above does this for you (needs `amulet-nbt` from
`requirements-bedrock-restore.txt`): with the server stopped it edits the world's
`level.dat` directly — no game client — setting both known experiment keys
(`gametest` is the one current versions honor), backing up `level.dat` first, and
is idempotent. To do *only* this step, run the installer with everything else
already in place; it skips work that's done and enables the experiment.

> Enabling an experiment is **irreversible** for that world and disables
> achievements (moot on a dedicated server).

## 4. Restart the server

On startup, success looks like:

```
Experiment(s) active: gtst
Pack Stack - [00] Diamond Sign events (id: dd12725f-…) @ behavior_packs/diamondsign_events
```

with **no** `[Scripting] ... chatSend unavailable` error. Then:
- A player **dies** → `[… WARN] [Scripting] DIAMONDSIGN {"t":"death",…}`
- A player **chats** → `[… WARN] [Scripting] DIAMONDSIGN {"t":"chat",…}`

Common startup errors:
- `requesting dependency on beta APIs … but the Beta APIs experiment is not
  enabled` → run step 3 (the experiment key didn't take; the helper now sets
  `gametest`).
- `Configured pack (id: …) was not found` → wrong UUID in step 2 (use the header
  uuid).

## 5. Turn it on in the bot

On this server's entry in `diamondsign.json`:

```jsonc
"bedrock_script_events": true,   // ingest death markers; enables /deaths, /death_summary
"chat_relay": true               // relay in-game chat to the chat platforms
```

Restart the bot. Deaths now announce + record (like Java); chat is relayed to
every authorized chat bound to this server as `💬 <player>: <message>`.

## Deaths only (no experiment)

If you only want death notifications and don't want the irreversible Beta APIs
experiment, edit `manifest.json` and change the dependency `"version": "beta"` to
a **stable** version your server provides (e.g. `"2.7.0"`). The pack then loads
without the experiment; deaths work, chat does not (the script logs
`chatSend unavailable` and carries on). Set only `bedrock_script_events: true`.
