# mcnotifier

A chat bot that monitors a Minecraft server and sends notifications when players join or leave. Tracks achievements, deaths, player stats, and performs automated backups. Full backups run on a configurable daily, weekly, or monthly schedule, while incremental backups capture changes every few minutes (configurable) as players explore the world. Restoration to any backup point is done through an interactive CLI tool and requires the server to be offline.

Works with **Telegram** and **Slack** — one or both at once (set `CHAT_PLATFORMS`). Commands are answered on the platform they arrive on; announcements broadcast to every platform's authorized chats. See [Chat platforms](#chat-platforms).

Both **Java** and **Bedrock** dedicated servers are supported (set `SERVER_EDITION`). Java uses RCON; Bedrock — which has no RCON — injects commands through the tmux/screen session hosting the server. See [Bedrock servers](#bedrock-servers) for the differences and feature limitations.

> **Platform support:** mcnotifier is designed for and tested only on **Linux Minecraft servers**. Running against a Windows-hosted Minecraft server is not supported — Windows file locking causes backup zips to fail with `PermissionError` on files the server holds open (e.g. `session.lock`), and buffered log writes make confirmation waiters unreliable on Windows. The bot itself can run on any platform where Python and `watchdog` work, but the Minecraft server it monitors should be on Linux.

## Files

| File | Description |
|------|-------------|
| `bot.py` | Main bot — log watcher, command handlers, stats, orchestration |
| `config.py` | Central config (`ServerConfig`) and world-layout helpers |
| `chat/` | Chat-platform adapters: `base` (interface + command router), `telegram`, `slack` |
| `backends/` | Edition backends: `base` (interface), `java` (RCON), `bedrock` (tmux/screen), `mux` (multiplexer detection) |
| `bedrock_player.py` | Bedrock world-LevelDB access + backup sidecar for per-player restore |
| `bedrock_pack/` | Optional Bedrock behavior pack for chat + death events (Script API) + `enable_beta_apis.py` |
| `backup_utils.py` | Shared backup utilities (chain IDs, manifest, constants) |
| `requirements-bedrock-restore.txt` | Optional deps for Bedrock per-player restore (amulet-leveldb/amulet-nbt) |
| `restore.py` | Interactive CLI tool for restoring from backup chains |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for required environment variables |
| `.gitignore` | Excludes secrets, runtime state, and virtualenv |

## Setup

1. **Install dependencies**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and fill in:
   - `MINECRAFT_DIR` — absolute path to the Minecraft server directory (e.g. `/home/user/Minecraft`)
   - `CHAT_PLATFORMS` — `telegram` (default), `slack`, or `telegram,slack`. See [Chat platforms](#chat-platforms)
   - `BOT_TOKEN` — Telegram bot token from [@BotFather](https://t.me/BotFather) (needed if Telegram is enabled)
   - `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` — Slack tokens (needed if Slack is enabled; see [Chat platforms](#chat-platforms))
   - `SERVER_EDITION` — `java` (default) or `bedrock`
   - `RCON_PASSWORD` — **Java only:** RCON password (must match `server.properties` `rcon.password`)
   - `MUX_SESSION` / `CONSOLE_LOG` — **Bedrock only:** see [Bedrock servers](#bedrock-servers)
   - `BACKUP_HOUR` — hour of day (0–23) for daily auto-backup (default: `4`)
   - `BACKUP_DIR` — directory for backup zip files (default: `~/minecraft_backup`)
   - `BACKUP_COPY_CMD` — optional shell command to copy the backup off-server; `{file}` is replaced with the zip path (e.g. `scp {file} user@nas:/backups/` or `cp {file} /mnt/backup/`)

3. **Run**
   ```bash
   python bot.py
   ```

## First-time authorisation

Authorization is **per platform** (each has its own admin and whitelist, stored namespaced in `auth.json`). On each platform you use:

1. Send the bot a private message / DM — the first sender becomes that platform's **admin** (on Telegram, any message; on Slack, any slash command such as `/status`).
2. In the group/channel you want notifications in, send `/chat_id` to get its ID (works even before the chat is authorized; on Slack, invite the bot to the channel first).
3. In a private message/DM to the bot, send `/authorize <chat_id>` to whitelist it.

The bot then sends join/leave (and death/achievement) announcements to every authorized chat on every platform, and answers commands in whichever chat they're sent.

## Chat platforms

`CHAT_PLATFORMS` (comma-separated) selects which platforms run; the bot serves them **all at once** from one process. Commands are answered on the platform they arrive on; only announcements broadcast to every platform. A `/backup` (or `/restore_player`) started on one platform while one is already running anywhere replies "already in progress" — backups are globally serialized.

### Telegram

Long-polling — no public URL needed. Create a bot with [@BotFather](https://t.me/BotFather), set `BOT_TOKEN`, and include `telegram` in `CHAT_PLATFORMS`.

### Slack

Uses **Socket Mode** (an outbound websocket), so no public URL or webhook is needed — it works behind NAT like Telegram. Set `CHAT_PLATFORMS=...,slack` and both `SLACK_BOT_TOKEN` (`xoxb-…`) and `SLACK_APP_TOKEN` (`xapp-…`).

1. Create an app at <https://api.slack.com/apps> → **From an app manifest**, and paste the manifest below (it declares every slash command and the needed scopes).
2. **Basic Information → App-Level Tokens →** generate a token with the `connections:write` scope → that's `SLACK_APP_TOKEN` (`xapp-…`).
3. **Install App** to your workspace → **Bot User OAuth Token** is `SLACK_BOT_TOKEN` (`xoxb-…`).
4. Invite the bot to each channel you want notifications in (`/invite @yourbot`). `chat:write.public` lets it also post to public channels it hasn't joined.

```yaml
display_information:
  name: mcnotifier
features:
  bot_user:
    display_name: mcnotifier
    always_online: true
  slash_commands:
    - { command: /status,        description: Show online players,      should_escape: false }
    - { command: /list,          description: List known players,       should_escape: false }
    - { command: /stats,         description: Player statistics,        should_escape: false }
    - { command: /playtime,      description: Playtime leaderboard,     should_escape: false }
    - { command: /achievements,  description: Player achievements,      should_escape: false }
    - { command: /deaths,        description: Death history,            should_escape: false }
    - { command: /death_summary, description: Deaths grouped by cause,  should_escape: false }
    - { command: /scan_achievements, description: Scan logs for achievements, should_escape: false }
    - { command: /scan_deaths,   description: Scan logs for deaths,      should_escape: false }
    - { command: /backup,        description: Trigger a backup now,     should_escape: false }
    - { command: /restore_player, description: Restore one player,      should_escape: false }
    - { command: /chat_id,       description: Show this chat's ID,      should_escape: false }
    - { command: /authorize,     description: Whitelist a chat,         should_escape: false }
    - { command: /revoke,        description: Remove a chat,            should_escape: false }
    - { command: /listchats,     description: List authorized chats,    should_escape: false }
    - { command: /help,          description: Show commands,            should_escape: false }
oauth_config:
  scopes:
    bot: [commands, chat:write, chat:write.public]
settings:
  socket_mode_enabled: true
  org_deploy_enabled: false
```

Slack uses string IDs (`U…` users, `C…`/`D…` channels), which `/authorize` and the per-platform `auth.json` handle automatically.

## RCON setup

> **Java servers only.** Bedrock has no RCON — it uses tmux/screen command injection instead (see [Bedrock servers](#bedrock-servers)). Skip this section for Bedrock.

On Java, the backup feature and `/list` require RCON to be enabled on the Minecraft server.

1. Edit `server.properties` and set:
   ```
   enable-rcon=true
   rcon.port=25575
   rcon.password=your_password
   ```
2. Restart the Minecraft server for the changes to take effect.
3. Set the same password in `.env` as `RCON_PASSWORD`.

### Securing the RCON port

RCON transmits passwords and commands in plaintext. If your server is network-accessible, block the RCON port from external access so only the local bot can use it.

**Block external access (iptables):**

```bash
# Allow RCON on localhost only, drop all other connections to the port
sudo iptables -A INPUT -i lo -p tcp --dport 25575 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 25575 -j DROP

# Make the rules persist across reboots
sudo apt install iptables-persistent
sudo netfilter-persistent save
```

**Remove the block:**

```bash
# Remove the two rules (run in this order)
sudo iptables -D INPUT -p tcp --dport 25575 -j DROP
sudo iptables -D INPUT -i lo -p tcp --dport 25575 -j ACCEPT

# Update the saved rules (or uninstall iptables-persistent entirely)
sudo netfilter-persistent save

# Optional: remove persistence package
sudo apt remove iptables-persistent
```

You can verify the current rules with `sudo iptables -L INPUT -n --line-numbers`.

## Bedrock servers

Bedrock Dedicated Server (BDS) has **no RCON**, and its console is much terser than Java's. Set `SERVER_EDITION=bedrock` and run the server inside a terminal multiplexer so the bot can drive it.

**Command injection (tmux / screen / byobu).** The bot types commands on the server's stdin via the tmux or screen session hosting it (byobu wraps either). It auto-detects the session, or you can pin it with `MUX_SESSION`. If no tmux/screen session is found, the bot logs a clear message and exits — Bedrock cannot run without one. The bot must run as the **same user** that owns the session (it can only see that user's tmux/screen sessions).

**Setting `MUX_SESSION`.** This is the session **name** to target, not the multiplexer type — `MUX_SESSION=tmux` is wrong. Leave it blank to auto-detect the first live session, or set one of:

- `MUX_SESSION=<session>` — e.g. `MUX_SESSION=1`
- `MUX_SESSION=<session>:<window>` — pin a specific window (recommended)
- `MUX_SESSION=<session>:<window>.<pane>` — pin a window and pane

Pinning the window is recommended because byobu uses **grouped tmux sessions** (e.g. `1` and `1-8` sharing the same windows), where the active-window pointer is shared and shifts as you navigate byobu — so a bare session target can send a command to whatever window you happen to be viewing. Find the session and window with:

```bash
tmux list-sessions                 # session names, e.g. "1"
tmux list-windows -t 1             # window index + name, e.g. "0: bedrock"
```

If BDS runs in the window named `bedrock` of session `1`, set `MUX_SESSION=1:bedrock` (the window **name** is more robust than its index `1:0`, which shifts if windows are reordered). On startup the log shows `Detected tmux session for command injection: 1:bedrock`. For `screen`, the form is `MUX_SESSION=<session>:<window-number>`.

**Capturing output.** BDS writes to stdout, not `logs/latest.log`. Launch the server so its output is captured to a file the bot tails (default `MINECRAFT_DIR/console.log`, override with `CONSOLE_LOG`). For example, inside your byobu/tmux/screen window:
```bash
./bedrock_server 2>&1 | tee -a console.log
```

**Backups.** Instead of Java's `save-off` / `save-all` / `save-on`, the bot uses `save hold` → poll `save query` → `save resume`. `save query` reports each file with the exact number of bytes that belong to the snapshot, and the bot copies each file truncated to that length (BDS keeps appending past the snapshot point). Full and incremental backups and the [restore tool](#restoring-from-backups) work the same as Java otherwise.

**Feature limitations (current).** BDS's console reports only join/leave — no death, chat, or achievement events. So out of the box on Bedrock:
- Join/leave notifications, full + incremental backups, whole-world restore, per-player restore (see below), player list, and playtime stats (see below) all work.
- `/deaths`, `/death_summary`, `/achievements`, and the `/scan_*` commands reply that they are not available on this edition.
- **Deaths and in-game chat can be added** with an optional behavior pack (see [Bedrock chat + death events](#bedrock-chat--death-events)); achievements remain unavailable (Xbox-bound, not script-exposed).

### Bedrock player list and online-time stats

Bedrock has no per-player stats files (unlike Java), so the bot derives both the player list and playtime from the console join/leave events:

- **Portable player list** — `bedrock_players.json` is the authoritative player registry, keyed by xuid, holding each player's name, account-stable identities (used for per-player restore), and first/last-seen times. Because the identities are the same on any server for a given Xbox account, this file is **portable**: copy it to another mcnotifier instance and that instance immediately knows your players (and can restore them) without re-learning. The bot merges by xuid on every write (union — copied-in entries and locally-learned ones are never lost). It replaces `player_names.json` on Bedrock.
- **Online-time stats** — `statistics.json` accumulates each player's total connected time and session count from sign-in/sign-off. It drives `/stats` and `/playtime` on Bedrock. This file is **per-server, not portable** (playtime is server-specific). It is written on join/leave, on a clean shutdown, and checkpointed periodically (on each incremental backup and whenever `/stats` or `/playtime` is run) so in-progress time is persisted as you go. Caveats: a player already online when the bot starts is timed from bot-startup (slight undercount), and a hard crash loses only the in-progress time since the last checkpoint.

### Bedrock chat + death events

BDS's console doesn't report deaths or chat, so an optional **behavior pack**
(`bedrock_pack/`) supplies them via the Script API. It emits `console.warn` marker
lines (`MCNOTIFIER {…}`) which — with `content-log-console-output-enabled=true` —
land in the same `console.log` the bot already tails, so no HTTP endpoint or extra
network permission is needed. The bot parses these into the normal notify
pipeline: deaths announce + record (so `/deaths` and `/death_summary` work, with a
Bedrock-cause→message map that mirrors Java's wording), and chat is relayed to
every authorized chat as `💬 <player>: <message>`.

Setup is in [`bedrock_pack/INSTALL.md`](bedrock_pack/INSTALL.md). In brief:

1. Set `content-log-console-output-enabled=true` in `server.properties`.
2. Copy `bedrock_pack/` into `behavior_packs/` and activate it in the world's
   `world_behavior_packs.json` (by the pack's **header** UUID).
3. **Chat** needs the world's **Beta APIs** experiment (deaths don't if you pin a
   stable module version). Enable it with the server stopped via the bundled
   helper, which edits `level.dat` directly (no client needed):
   `python bedrock_pack/enable_beta_apis.py "worlds/<level-name>"`.
4. In the bot's `.env`, set `BEDROCK_SCRIPT_EVENTS=true` (deaths) and/or
   `CHAT_RELAY=true` (chat).

Caveats: the pack uses the Script API, so it tracks Minecraft's update cadence;
and enabling an experiment is **irreversible** for that world (and disables
achievements — moot on a dedicated server). Both flags default off, and the bot
runs fine without the pack (deaths/chat simply absent).

### Bedrock per-player restore

`/restore_player` works on Bedrock too, but the mechanics differ from Java because all players live in one world **LevelDB** that BDS keeps locked while running. It needs two extra dependencies and runs a stop→edit→restart cycle.

**Install the LevelDB libraries** (one-time, in the bot's virtualenv). amulet-leveldb has no Linux wheels and its sdist needs Cython 3.0.x:
```bash
sudo apt install -y build-essential zlib1g-dev
pip install --upgrade pip
printf 'cython>=3.0,<3.1\n' > /tmp/build-constraints.txt
PIP_BUILD_CONSTRAINT=/tmp/build-constraints.txt pip install -r requirements-bedrock-restore.txt
```
The bot lazily imports them — without them, backups and notifications still work; only per-player restore is unavailable.

**How it works.** Each backup embeds a small `_players.json` sidecar (each player's data + their account-stable identity), so a restore reads one zip with no chain reconstruction. To restore, the bot **stops the server** (the only way to write the locked db), overwrites that one player's data, and **relaunches** it via `MUX_START_CMD` — so the server is briefly offline. A pre-restore copy of the player's current data is written to `BACKUP_DIR` as an undo. If the relaunch fails, the bot prints the exact manual start command.

- **`MUX_START_CMD`** — the command to relaunch BDS in the mux window. Defaults to `cd <MINECRAFT_DIR> && ./bedrock_server 2>&1 | tee -a console.log`; override only for non-standard launches.
- **Name → player mapping.** A player's xuid (from the join log) and their LevelDB identity aren't linked anywhere queryable, so the bot **learns** the binding the first time that player is solo-online during a backup (stored in `bedrock_players.json`). Until a player has been learned, `/restore_player <name>` finds no versions for them — just have them play once.

## Backups

The bot performs automated full backups of the entire Minecraft server directory on a configurable schedule (daily, weekly, or monthly).

**How it works (Java):**

1. Sends `save-off` via RCON to disable auto-save.
2. Sends `save-all` to flush world data from memory to disk.
3. Waits for the filesystem to settle (no file changes for 5 seconds) to ensure all data is fully written.
4. Zips the entire `MINECRAFT_DIR` (e.g. `minecraftopia_20260401_040000.zip`).
5. Sends `save-on` to re-enable auto-save (guaranteed even if the zip fails).
6. Saves the zip to `BACKUP_DIR` (default: `~/minecraft_backup`).
7. Optionally runs `BACKUP_COPY_CMD` to copy the zip off-server.

Players do not need to be kicked — the save-off/save-all/save-on sequence ensures a consistent snapshot while the server stays online.

On **Bedrock** the freeze sequence is `save hold` → `save query` → `save resume` with snapshot-truncated copies (see [Bedrock servers](#bedrock-servers)); everything else (scheduling, chains, off-server copy, restore) is identical. Bot infrastructure that lives in the server directory is excluded from every zip: the `.mcnotifier_chain` marker (both editions) and the Bedrock `console.log` the bot tails. Unix file permissions (e.g. the executable bit on the Bedrock server binary) are preserved through backup and restore.

**Configuration:**

| Variable | Description | Default |
|----------|-------------|---------|
| `RCON_PASSWORD` | **Java only.** Must match `server.properties` `rcon.password` | *(required for Java backup)* |
| `BACKUP_SCHEDULE` | How often to run full backups: `daily`, `weekly` (Monday), or `monthly` (1st) | `daily` |
| `BACKUP_HOUR` | Hour of day (0–23) for the scheduled backup | `4` |
| `BACKUP_DIR` | Directory where backup zips are saved | `~/minecraft_backup` |
| `BACKUP_COPY_CMD` | Shell command to copy the zip off-server; `{file}` is replaced with the full zip path | *(empty — disabled)* |

**Copy command examples:**

```bash
# Copy to a mounted NAS
BACKUP_COPY_CMD=cp {file} /mnt/nas/minecraft_backups/

# SCP to a remote server
BACKUP_COPY_CMD=scp {file} user@backup-server:/backups/minecraft/

# Rsync to a remote server
BACKUP_COPY_CMD=rsync -az {file} user@backup-server:/backups/minecraft/
```

**Manual backup:** The admin can trigger a backup at any time via `/backup` in a private message. Progress updates are sent as the backup runs.

**Scheduled auto-backup:** Runs automatically at the configured `BACKUP_HOUR` on the schedule set by `BACKUP_SCHEDULE`. Progress is sent to the admin's private chat.

## Incremental Backups

When enabled, the bot performs incremental backups while players are active. Only files that have changed since the last backup are included, saving disk space compared to repeated full backups.

**How it works:**

1. When the first player joins, the incremental backup cycle starts.
2. Every `INCREMENTAL_INTERVAL_MINUTES` minutes, the bot:
   - Compares file modification timestamps against a stored manifest.
   - If changes are detected, freezes the world (Java: save-off / save-all; Bedrock: save hold / query), then waits for the filesystem to settle (no file changes for 5 seconds).
   - Creates a zip containing only changed/added files, plus `_deletions.json` for removed files.
   - Resumes saving (Java: save-on; Bedrock: save resume).
3. When the last player leaves, one final incremental backup runs and the cycle stops.
4. After a full backup, the manifest resets so the next incremental only captures changes since the full backup.

**Chain tracking:** Each full backup starts a new chain identified by a unique 8-character hex ID. Incremental backups embed this chain ID in their filenames and contents. This ensures that after a restore, new incrementals are correctly distinguished from old ones — even when multiple chains share the same base full backup.

A `.mcnotifier_chain` marker file is written in the Minecraft server directory to detect if the server state was replaced while the bot was offline. If the marker doesn't match the manifest's chain ID on startup, incremental backups are skipped until a new full backup establishes a fresh chain.

**Configuration:**

| Variable | Description | Default |
|----------|-------------|---------|
| `INCREMENTAL_BACKUP_ENABLED` | Enable incremental backups (`true`/`1`/`yes`) | `false` |
| `INCREMENTAL_INTERVAL_MINUTES` | Minutes between incremental backups while players are active | `15` |

**File naming:**

- Full backups: `servername_20260401_040000.zip`
- Incremental backups: `servername_incr_a1b2c3d4_20260401_041500.zip` (includes chain ID)

## Restoring from Backups

Use `restore.py` to restore the server from a backup chain (full + incrementals).

```bash
python restore.py [--backup-dir PATH] [--target-dir PATH] [--dry-run]
```

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--backup-dir` | Directory containing backup zip files | `BACKUP_DIR` from `.env` |
| `--target-dir` | Directory to restore into | `MINECRAFT_DIR` from `.env` |
| `--dry-run` | Preview what would be restored without writing files | *(off)* |

**How it works:**

1. Scans the backup directory for full and incremental zips.
2. Groups incrementals by chain ID (reads `_meta.json` from each incremental zip).
3. Displays restore points organised by chain, each linked to its base full backup.
4. After selecting a point, warns that the Minecraft server **must be stopped** before restoring.
5. Extracts the full backup, then applies each incremental in the chain up to the selected point.
6. **In-place restore** (`--target-dir` is `MINECRAFT_DIR`, the default): rebuilds `backup_manifest.json` and writes `.mcnotifier_chain` with a new chain ID. When restoring to an incremental point, creates a **merged incremental** zip that combines all applied incrementals into a single file — so the new chain only needs the original full backup + one merged incremental for future restores.
7. **Out-of-place restore** (`--target-dir` points elsewhere): only extracts files. No new chain is established, no merged incremental is created, no marker file is written, and the bot's `backup_manifest.json` is left untouched. The bot continues tracking `MINECRAFT_DIR` with its existing chain. If you later promote the target directory by replacing `MINECRAFT_DIR`'s contents with it, the absent `.mcnotifier_chain` will force the bot to take a fresh full backup on next `/backup`, cleanly starting a new chain.

**Important:** Always stop the Minecraft server before restoring in-place. Restoring while the server is running will cause data corruption.

**If you restore by other means** (manual copy, other tools): delete `backup_manifest.json` and `.mcnotifier_chain` from the server directory to force the bot to start a fresh chain with the next full backup.

### Restoring a single player's data

> This section describes the **Java** flow (replace one `<uuid>.dat` live). Bedrock supports `/restore_player` too, but via a stop→edit→restart of the world LevelDB — see [Bedrock per-player restore](#bedrock-per-player-restore).

The admin can roll back one player's `<uuid>.dat` without restoring the whole world via the `/restore_player` Telegram command. The command runs in three enforced steps so a single mistyped message can never trigger a destructive restore:

1. `/restore_player <username>` — bot replies with a numbered, latest-first list of every available `.dat` version it can find for that player. Sources include the live player-data folder (`<uuid>.dat`, `<uuid>.dat_old`, `<uuid>.dat_old.gz`, plus any prior `pre-restore` safety copies) and every backup zip in the active chain that contains the player's `.dat`. Both the pre-26.1 layout (`<world>/playerdata/`) and the 26.1+ layout (`<world>/players/data/`) are supported, so old backups created before the upgrade are still usable.
2. `/restore_player <username> <N>` — bot replies with a confirmation block showing the player name, UUID, timestamp, and source for selection `N`, plus the exact `confirm` command to send.
3. `/restore_player <username> <N> confirm` — bot performs the restore: verifies the player is offline (via RCON `/list`), runs `save-off` / `save-all`, waits for the filesystem to settle, then atomically replaces `<uuid>.dat` with the chosen version. The previous `.dat` is preserved alongside as `<uuid>.dat.pre-restore-<timestamp>` so the admin can manually undo. `save-on` is always re-enabled, even if the restore step fails.

Steps must be executed in order (and within 5 minutes of each other); jumping straight to step 3 is rejected. The selection state is per-admin and lives only in memory — restarting the bot clears it.

The player must be offline before the restore proceeds; the command refuses with a message asking the admin to log them out first.

## Commands

| Command | Description |
|---------|-------------|
| `/status` | Show currently online players |
| `/list` | List all players found in the stats directory |
| `/stats [player]` | Full stats for one or all players |
| `/playtime` | Playtime leaderboard |
| `/achievements [player]` | Show player achievements with timestamps |
| `/deaths [player]` | Show death history |
| `/death_summary` | Show deaths grouped by cause with per-player counts |
| `/chat_id` | Show the current chat's ID |
| `/authorize <chat_id>` | *(Admin)* Whitelist a group chat |
| `/revoke <chat_id>` | *(Admin)* Remove a group from the whitelist |
| `/listchats` | *(Admin)* List all authorised chat IDs |
| `/scan_achievements` | *(Admin)* Scan all log files for achievements |
| `/scan_deaths` | *(Admin)* Scan all log files for deaths |
| `/backup` | *(Admin)* Trigger a server backup now |
| `/allowlist <on\|off\|add\|remove\|list\|reload> [player]` | *(Admin)* Manage the server allow/whitelist; the server's response is piped back to the chat |
| `/restore_player <username> [<N> [confirm]]` | *(Admin)* List, select, and restore a single player's `.dat` file from any backup or live working copy |

On **Bedrock**, the achievement commands (`/achievements`, `/scan_achievements`) are unavailable. `/deaths` and `/death_summary` work only with the optional behavior pack + `BEDROCK_SCRIPT_EVENTS=true` (see [Bedrock chat + death events](#bedrock-chat--death-events)); the `/scan_*` commands stay Java-only (they scan log history, which Bedrock doesn't keep). `/restore_player` works via a different mechanism — see [Bedrock per-player restore](#bedrock-per-player-restore).

`/allowlist` runs the server's allow/whitelist command and pipes the response back: it calls `whitelist` on Java (via RCON) and `allowlist` on Bedrock (injected via tmux/screen, with the response read back from `console.log`). Subcommands are identical on both: `on`, `off`, `add <player>`, `remove <player>`, `list`, `reload`.

## Runtime state

The bot writes the following at runtime (all excluded from git):

- `auth.json` — per-platform admin user ID and authorised chat list (`{telegram: {...}, slack: {...}}`; an old flat file is auto-migrated into the `telegram` namespace)
- `player_names.json` — *(Java)* UUID → username mappings learned from server logs
- `player_achievements.json` — player achievements with timestamps, keyed by UUID
- `player_deaths.json` — player death history with timestamps, keyed by UUID
- `backup_manifest.json` — incremental backup state: chain ID, base full backup, and file modification timestamps
- `bedrock_player_state.json` — *(Bedrock)* per-player data hashes, so incremental sidecars only carry players that changed
- `bedrock_players.json` — *(Bedrock)* the player registry: xuid → name, identities, first/last-seen. Portable (see [Bedrock player list](#bedrock-player-list-and-online-time-stats)); replaces `player_names.json` on Bedrock
- `statistics.json` — *(Bedrock)* accumulated online time + session counts per player; drives `/stats` and `/playtime`. Per-server (not portable)
- `<MINECRAFT_DIR>/.mcnotifier_chain` — chain validity marker written in the server directory
- `logs/log_<YYYYMMDD_HHMMSS>.txt` — a new log file is created each time the bot starts

Delete `auth.json`, `player_names.json`, `player_achievements.json`, and `player_deaths.json` to reset the bot to a fresh state. Delete `backup_manifest.json` and `.mcnotifier_chain` to force a fresh backup chain.

## Logging

Each run produces a timestamped log file under `logs/`. Logged events include:

- Player join/leave notifications (player name, online/offline, number of chats notified)
- Achievement and death notifications
- All command requests (command name and Telegram username of requester)
- Admin actions (claim, authorize, revoke)
- Player registry updates (new UUID→name mappings and renames)
- Backup progress and results
- Minecraft log file rotation detection
- Errors and exceptions
