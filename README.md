# mcnotifier

A chat bot that watches your Minecraft server and keeps you in the loop from
**Telegram** and/or **Slack** — player join/leave, deaths, in-game chat, stats,
and automated backups with point-in-time restore. Works with **Java** and
**Bedrock** dedicated servers.

**Highlights**

- **Notifications** to Telegram and/or Slack (both at once): join/leave, deaths,
  achievements, and a one-way in-game **chat relay**.
- **Player stats & playtime** leaderboards, and a known-player list.
- **Backups** — scheduled full backups plus space-efficient incrementals while
  players are online, with optional off-server copy.
- **Restore** — roll back the whole world to any backup point, or restore a
  **single player** without touching anyone else.
- **`/allowlist`** — manage the server's whitelist/allowlist from chat.
- One process serves all your chat platforms; commands are answered where they're
  sent, announcements broadcast everywhere.

### What works on each edition

| Feature | Java | Bedrock |
|---|:---:|:---:|
| Join / leave notifications | ✓ | ✓ |
| Player stats & `/playtime` | ✓ | ✓ |
| Full + incremental backups | ✓ | ✓ |
| Whole-world restore | ✓ | ✓ |
| Per-player restore | ✓ | ✓ |
| `/allowlist` | ✓ | ✓ |
| Death notifications | ✓ | ✓ — needs the [behavior pack](#bedrock-chat--death-events) |
| In-game chat relay | ✓ | ✓ — needs the [behavior pack](#bedrock-chat--death-events) |
| Achievements | ✓ | ✗ — Xbox-bound, not exposed to servers |

> **Host requirement:** the Minecraft **server** must run on **Linux**. Windows
> file locking breaks backup zips (e.g. `session.lock`), and buffered log writes
> make the command confirmations unreliable. The bot itself can run anywhere
> Python + `watchdog` work, but the server it watches should be on Linux.

---

## Quick start

1. **Install**
   ```bash
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure** — easiest is to just **skip to step 3 and run `python bot.py`**:
   if `.env` is missing or a required field is unset, the bot runs a short
   interactive setup, asks for what it needs, and writes `.env` for you. On every
   later start it also tops `.env` up with any new fields from `.env.example`
   (your values are kept), and stops with a clear list if anything required is
   missing in a non-interactive launch (e.g. under systemd).

   To configure by hand instead, copy the template and edit it:
   ```bash
   cp .env.example .env
   ```
   The essentials to get running:
   | Variable | What it is |
   |---|---|
   | `MINECRAFT_DIR` | Absolute path to the server directory |
   | `CHAT_PLATFORMS` | `telegram` (default), `slack`, or `telegram,slack` |
   | `BOT_TOKEN` | Telegram bot token (if Telegram is on) — see [Telegram](#telegram) |
   | `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack tokens (if Slack is on) — see [Slack](#slack) |
   | `SERVER_EDITION` | `java` (default) or `bedrock` |
   | `RCON_PASSWORD` | **Java:** match `server.properties` — see [Java](#java-rcon) |
   | `MUX_SESSION` / `CONSOLE_LOG` | **Bedrock:** see [Bedrock](#bedrock) |

   Then set up your [chat platform](#connecting-a-chat-platform) and
   [Minecraft server](#connecting-your-minecraft-server) (the next two sections).

3. **Run**
   ```bash
   python bot.py
   ```

4. **Claim & authorise** — DM the bot to become its admin, then whitelist a group
   to receive notifications. See [Authorising chats](#authorising-chats).

---

## Connecting a chat platform

`CHAT_PLATFORMS` (comma-separated) selects which platforms run; the bot serves
them **all at once** from one process. Commands are answered on the platform they
arrive on; only announcements broadcast to every platform. A `/backup` (or
`/restore_player`) started on one platform while one is already running anywhere
replies "already in progress" — backups are globally serialised.

### Telegram

Long-polling — no public URL needed. Create a bot with
[@BotFather](https://t.me/BotFather), put the token in `BOT_TOKEN`, and include
`telegram` in `CHAT_PLATFORMS`.

### Slack

Uses **Socket Mode** (an outbound websocket), so no public URL or webhook is
needed — it works behind NAT like Telegram. Set `CHAT_PLATFORMS=...,slack` and
both `SLACK_BOT_TOKEN` (`xoxb-…`) and `SLACK_APP_TOKEN` (`xapp-…`).

1. Create an app at <https://api.slack.com/apps> → **From an app manifest**, and
   paste the manifest below (it declares every slash command and the scopes).
2. **Basic Information → App-Level Tokens →** generate a token with the
   `connections:write` scope → that's `SLACK_APP_TOKEN` (`xapp-…`).
3. **Install App** to your workspace → **Bot User OAuth Token** is
   `SLACK_BOT_TOKEN` (`xoxb-…`).
4. Invite the bot to each channel you want notifications in (`/invite @yourbot`).
   `chat:write.public` lets it also post to public channels it hasn't joined.

```yaml
display_information:
  name: mcnotifier
features:
  bot_user:
    display_name: mcnotifier
    always_online: true
  slash_commands:
    - { command: /online,        description: Show online players,      should_escape: false }
    - { command: /list,          description: List known players,       should_escape: false }
    - { command: /stats,         description: Player statistics,        should_escape: false }
    - { command: /playtime,      description: Playtime leaderboard,     should_escape: false }
    - { command: /achievements,  description: Player achievements,      should_escape: false }
    - { command: /deaths,        description: Death history,            should_escape: false }
    - { command: /death_summary, description: Deaths grouped by cause,  should_escape: false }
    - { command: /scan_achievements, description: Scan logs for achievements, should_escape: false }
    - { command: /scan_deaths,   description: Scan logs for deaths,      should_escape: false }
    - { command: /backup,        description: Trigger a backup now,     should_escape: false }
    - { command: /allowlist,     description: Manage the allow/whitelist, should_escape: false }
    - { command: /restore_player, description: Restore one player,      should_escape: false }
    - { command: /chat_id,       description: Show this chat's ID,      should_escape: false }
    - { command: /authorize,     description: Whitelist a chat,         should_escape: false }
    - { command: /revoke,        description: Remove a chat,            should_escape: false }
    - { command: /listchats,     description: List authorized chats,    should_escape: false }
    - { command: /commands,      description: Show commands,            should_escape: false }
oauth_config:
  scopes:
    bot: [commands, chat:write, chat:write.public]
settings:
  socket_mode_enabled: true
  org_deploy_enabled: false
```

> **Why `/online` and `/commands` instead of `/status` and `/help`?** Slack
> reserves `/status` and `/help` for its own built-in commands and rejects an app
> manifest that tries to register them ("invalid name"). The Slack adapter maps
> `/online → status` and `/commands → help` internally, so they behave exactly
> like the Telegram `/status` and `/help`. All other commands are identical
> across platforms.

Slack uses string IDs (`U…` users, `C…`/`D…` channels), which `/authorize` and
the per-platform `auth.json` handle automatically.

---

## Connecting your Minecraft server

Set `SERVER_EDITION` to `java` or `bedrock`. The two editions differ in how the
bot sends commands to the server.

### Java (RCON)

The bot drives a Java server over **RCON** (used for backups, `/list`,
`/allowlist`, …). Enable it:

1. In `server.properties`:
   ```
   enable-rcon=true
   rcon.port=25575
   rcon.password=your_password
   ```
2. Restart the server.
3. Set the same password in `.env` as `RCON_PASSWORD`.

#### Securing the RCON port

RCON transmits passwords and commands in plaintext. If your server is
network-accessible, block the RCON port from outside so only the local bot can
use it.

```bash
# Allow RCON on localhost only, drop all other connections to the port
sudo iptables -A INPUT -i lo -p tcp --dport 25575 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 25575 -j DROP

# Persist across reboots
sudo apt install iptables-persistent
sudo netfilter-persistent save
```

To remove the block later:
```bash
sudo iptables -D INPUT -p tcp --dport 25575 -j DROP
sudo iptables -D INPUT -i lo -p tcp --dport 25575 -j ACCEPT
sudo netfilter-persistent save        # update saved rules
```
Verify with `sudo iptables -L INPUT -n --line-numbers`.

### Bedrock

Bedrock Dedicated Server (BDS) has **no RCON**, and its console is terser than
Java's. Run the server inside a terminal multiplexer (tmux or screen — byobu
wraps either) so the bot can type commands on its stdin.

**Command injection.** The bot sends commands via the tmux/screen session hosting
the server. It auto-detects the session, or you can pin it with `MUX_SESSION`. If
no session is found, the bot logs a clear message and exits. The bot must run as
the **same user** that owns the session (it can only see that user's sessions).

**Setting `MUX_SESSION`.** This is the session **name**, not the multiplexer type
— `MUX_SESSION=tmux` is wrong. Leave it blank to auto-detect the first live
session, or set one of:

- `MUX_SESSION=<session>` — e.g. `MUX_SESSION=1`
- `MUX_SESSION=<session>:<window>` — pin a specific window (recommended)
- `MUX_SESSION=<session>:<window>.<pane>` — pin a window and pane

Pinning the window is recommended because byobu uses **grouped tmux sessions**
(e.g. `1` and `1-8` sharing windows), where the active-window pointer shifts as
you navigate — so a bare session target can send a command to whatever window
you're viewing. Find the session and window with:

```bash
tmux list-sessions                 # session names, e.g. "1"
tmux list-windows -t 1             # window index + name, e.g. "0: bedrock"
```

If BDS runs in the window named `bedrock` of session `1`, set
`MUX_SESSION=1:bedrock` (the **name** is more robust than the index `1:0`, which
shifts if windows are reordered). On startup the log shows
`Detected tmux session for command injection: 1:bedrock`. For `screen`, the form
is `MUX_SESSION=<session>:<window-number>`.

**Capturing output.** BDS writes to stdout, not `logs/latest.log`. Launch it so
its output is captured to a file the bot tails (default `MINECRAFT_DIR/console.log`,
override with `CONSOLE_LOG`), e.g. inside your tmux/screen window:
```bash
./bedrock_server 2>&1 | tee -a console.log
```

Bedrock also has a few edition-specific features and limitations — see
[Bedrock specifics](#bedrock-specifics).

---

## Authorising chats

Authorisation is **per platform** (each has its own admin and whitelist, stored
namespaced in `auth.json`). On each platform you use:

1. Send the bot a private message / DM — the first sender becomes that platform's
   **admin** (on Telegram, any message; on Slack, any slash command such as
   `/status`).
2. In the group/channel you want notifications in, send `/chat_id` to get its ID
   (works even before the chat is authorised; on Slack, invite the bot first).
3. In a private message/DM to the bot, send `/authorize <chat_id>` to whitelist it.

The bot then broadcasts announcements (join/leave, deaths, …) to every authorised
chat on every platform, and answers commands in whichever chat they're sent.

---

## Commands

| Command | Description |
|---------|-------------|
| `/status` | Show currently online players |
| `/list` | List all known players |
| `/stats [player]` | Full stats for one or all players |
| `/playtime` | Playtime leaderboard |
| `/achievements [player]` | Show player achievements with timestamps |
| `/deaths [player]` | Show death history |
| `/death_summary` | Deaths grouped by cause with per-player counts |
| `/chat_id` | Show the current chat's ID |
| `/authorize <chat_id>` | *(Admin)* Whitelist a chat |
| `/revoke <chat_id>` | *(Admin)* Remove a chat from the whitelist |
| `/listchats` | *(Admin)* List authorised chats |
| `/scan_achievements` | *(Admin)* Scan all log files for achievements |
| `/scan_deaths` | *(Admin)* Scan all log files for deaths |
| `/backup` | *(Admin)* Trigger a server backup now |
| `/allowlist <on\|off\|add\|remove\|list\|reload> [player]` | *(Admin)* Manage the server allow/whitelist; the server's response is piped back |
| `/restore_player <username> [<N> [confirm]]` | *(Admin)* List, select, and restore one player's data |

**Slack note.** Slack reserves `/status` and `/help`, so on Slack they're
`/online` and `/commands` respectively (the bot maps them back internally). Every
other command name is the same on both platforms.

**Edition notes.** On **Bedrock**, `/achievements` and `/scan_achievements` are
unavailable (achievements are Xbox-bound and not exposed to servers). `/deaths`
and `/death_summary` work only with the optional [behavior
pack](#bedrock-chat--death-events) + `BEDROCK_SCRIPT_EVENTS=true`; the `/scan_*`
commands stay Java-only (they scan log history, which Bedrock doesn't keep).
`/restore_player` works on both, via different mechanisms (see
[Per-player restore](#restoring-a-single-player)).

`/allowlist` runs the server's allow/whitelist command and pipes the response
back — `whitelist` on Java (via RCON) and `allowlist` on Bedrock (via tmux/screen,
read back from `console.log`). Subcommands are identical on both: `on`, `off`,
`add <player>`, `remove <player>`, `list`, `reload`.

---

## Notifications & chat relay

The bot announces player **join/leave** on both editions, and **deaths** and
**achievements** where available (see the [matrix](#what-works-on-each-edition)).
Deaths are recorded for `/deaths` and `/death_summary`; achievements for
`/achievements`.

**Chat relay** (`CHAT_RELAY=true`, off by default) mirrors in-game chat to every
authorised chat as `💬 <player>: <message>` — one-way (chat platforms → game is
not relayed). On **Java** this works out of the box (chat is read from
`latest.log`); on **Bedrock** it needs the [behavior
pack](#bedrock-chat--death-events).

---

## Backups

The bot takes automated full backups of the entire server directory on a schedule
(daily, weekly, or monthly), and can take **incremental** backups while players
are online.

**How a full backup works (Java):**

1. `save-off` (via RCON) disables auto-save.
2. `save-all` flushes world data to disk.
3. Wait for the filesystem to settle (no changes for 5 s).
4. Zip the entire `MINECRAFT_DIR` (e.g. `myserver_20260401_040000.zip`) into
   `BACKUP_DIR`.
5. `save-on` re-enables auto-save (guaranteed even if the zip fails).
6. Optionally run `BACKUP_COPY_CMD` to copy the zip off-server.

Players don't need to be kicked — the save-off/save-all/save-on sequence ensures a
consistent snapshot while the server stays online.

On **Bedrock** the freeze sequence is `save hold` → `save query` → `save resume`,
copying each file truncated to the snapshot length `save query` reports;
everything else (scheduling, chains, off-server copy, restore) is identical. Bot
infrastructure that lives in the server directory is excluded from every zip (the
`.mcnotifier_chain` marker on both editions, and the Bedrock `console.log`), and
Unix file permissions (e.g. the executable bit on the Bedrock server binary) are
preserved through backup and restore.

**Configuration:**

| Variable | Description | Default |
|----------|-------------|---------|
| `RCON_PASSWORD` | **Java only.** Match `server.properties` `rcon.password` | *(required for Java)* |
| `BACKUP_SCHEDULE` | `daily`, `weekly` (Monday), or `monthly` (1st) | `daily` |
| `BACKUP_HOUR` | Hour of day (0–23) for the scheduled backup | `4` |
| `BACKUP_DIR` | Where backup zips are saved | `~/minecraft_backup` |
| `BACKUP_COPY_CMD` | Shell command to copy the zip off-server; `{file}` → the full zip path | *(empty — disabled)* |

**Off-server copy examples:**

```bash
BACKUP_COPY_CMD=cp {file} /mnt/nas/minecraft_backups/                  # mounted NAS
BACKUP_COPY_CMD=scp {file} user@backup-server:/backups/minecraft/      # SCP
BACKUP_COPY_CMD=rsync -az {file} user@backup-server:/backups/minecraft/  # rsync
```

**Triggering:** `/backup` runs one immediately (admin, private chat); the
scheduled backup runs at `BACKUP_HOUR` on the `BACKUP_SCHEDULE`. Progress is sent
to the admin's chat.

### Incremental backups

When `INCREMENTAL_BACKUP_ENABLED=true`, the bot backs up only files changed since
the last backup while players are active — far smaller than repeated full backups.

1. The cycle starts when the first player joins.
2. Every `INCREMENTAL_INTERVAL_MINUTES`, the bot compares file mtimes against a
   stored manifest, freezes the world (Java: save-off/save-all; Bedrock: save
   hold/query), waits for the filesystem to settle, and zips only changed/added
   files (plus `_deletions.json` for removed ones), then resumes saving.
3. When the last player leaves, one final incremental runs and the cycle stops.
4. A full backup resets the manifest, so the next incremental only captures
   changes since that full backup.

| Variable | Description | Default |
|----------|-------------|---------|
| `INCREMENTAL_BACKUP_ENABLED` | Enable incrementals (`true`/`1`/`yes`) | `false` |
| `INCREMENTAL_INTERVAL_MINUTES` | Minutes between incrementals while players are active | `15` |

**Chains.** Each full backup starts a new chain (an 8-char hex ID embedded in
incremental filenames and contents) so that after a restore, new incrementals are
never confused with old ones. A `.mcnotifier_chain` marker in the server directory
lets the bot detect if the server state was replaced while it was offline; if the
marker doesn't match the manifest on startup, incrementals pause until the next
full backup. File naming:

- Full: `servername_20260401_040000.zip`
- Incremental: `servername_incr_a1b2c3d4_20260401_041500.zip` (includes the chain ID)

---

## Restoring from backups

Use `restore.py` to restore the whole server from a backup chain (full +
incrementals). **Stop the server first** — restoring while it runs corrupts data.

```bash
python restore.py [--backup-dir PATH] [--target-dir PATH] [--dry-run]
```

| Flag | Description | Default |
|------|-------------|---------|
| `--backup-dir` | Directory containing backup zips | `BACKUP_DIR` from `.env` |
| `--target-dir` | Directory to restore into | `MINECRAFT_DIR` from `.env` |
| `--dry-run` | Preview without writing files | *(off)* |

The tool scans the backup directory, groups incrementals into chains, and shows
restore points organised by chain. After you pick one it extracts the full backup
and applies each incremental up to that point.

- **In-place** (`--target-dir` is `MINECRAFT_DIR`, the default): rebuilds the
  manifest and writes a fresh `.mcnotifier_chain`. Restoring to an incremental
  point also writes a single **merged incremental** so the new chain needs only
  the original full backup + that one file going forward.
- **Out-of-place** (any other target): extracts files only — no new chain, no
  marker, manifest untouched. The bot keeps tracking `MINECRAFT_DIR`'s chain. If
  you later promote the target by replacing `MINECRAFT_DIR` with it, the missing
  `.mcnotifier_chain` forces a fresh full backup on the next `/backup`.

If you restore by other means (manual copy, other tools), delete
`backup_manifest.json` and `.mcnotifier_chain` so the bot starts a fresh chain on
the next full backup.

### Restoring a single player

`/restore_player` rolls back **one** player without touching the rest of the
world. It runs in three enforced steps so a mistyped message can't trigger a
destructive restore, and the player must be offline:

1. `/restore_player <username>` — lists every available version of that player's
   data, latest first (live working copies, prior pre-restore safety copies, and
   every backup zip that contains them).
2. `/restore_player <username> <N>` — shows a confirmation block for selection `N`.
3. `/restore_player <username> <N> confirm` — performs the restore, keeping the
   current data as a pre-restore safety copy first.

Steps must run in order and within 5 minutes; the selection state is per-admin and
in-memory (a bot restart clears it).

The mechanics differ by edition:

- **Java** — replaces the player's `<uuid>.dat` live (server up). Sources include
  the live player-data folder and backup zips; both the pre-26.1
  (`<world>/playerdata/`) and 26.1+ (`<world>/players/data/`) layouts are
  supported. The previous file is kept as `<uuid>.dat.pre-restore-<timestamp>`.
- **Bedrock** — players live in one world **LevelDB** that BDS locks while
  running, so the bot **stops the server**, overwrites that player's record,
  writes a pre-restore undo copy to `BACKUP_DIR`, and **relaunches** via
  `MUX_START_CMD` (the server is briefly offline). See [Per-player restore on
  Bedrock](#bedrock-per-player-restore) for the extra dependencies and setup.

---

## Bedrock specifics

Beyond [connecting a Bedrock server](#bedrock), a few features work differently
on Bedrock because BDS exposes less than Java.

### Bedrock player list and online-time stats

Bedrock has no per-player stats files, so the bot derives both the player list and
playtime from console join/leave events:

- **Portable player list** — `bedrock_players.json` is the authoritative registry,
  keyed by xuid, holding each player's name, account-stable identities (used for
  per-player restore), and first/last-seen times. Because the identities are the
  same on any server for a given Xbox account, this file is **portable**: copy it
  to another instance and it immediately knows your players (and can restore them)
  without re-learning. Every write merges by xuid (union — nothing is lost). It
  replaces `player_names.json` on Bedrock.
- **Online-time stats** — `statistics.json` accumulates each player's total
  connected time and session count from sign-in/sign-off, driving `/stats` and
  `/playtime`. **Per-server, not portable.** It's written on join/leave, on clean
  shutdown, and checkpointed periodically (each incremental backup and each
  `/stats`/`/playtime`) so in-progress time is persisted. Caveats: a player
  already online when the bot starts is timed from bot-startup (slight undercount),
  and a hard crash loses only the time since the last checkpoint.

### Bedrock chat + death events

BDS's console doesn't report deaths or chat, so an optional **behavior pack**
(`bedrock_pack/`) supplies them via the Script API. It emits `console.warn` marker
lines (`MCNOTIFIER {…}`) which — with `content-log-console-output-enabled=true` —
land in the same `console.log` the bot already tails, so no HTTP endpoint or
network permission is needed. The bot feeds these into the normal notify pipeline:
deaths announce + record (`/deaths`, `/death_summary` work, with a Bedrock
damage-cause→message map that mirrors Java's wording), and chat is relayed like on
Java.

**Install — one command** (with the server stopped, from the repo root):

```bash
python install_bedrock_pack.py
```

It confirms the server is Bedrock and stopped, then reads `MINECRAFT_DIR` from
`.env` and the world's `level-name` from `server.properties`, copies the pack into
`behavior_packs/`, activates it in the world's `world_behavior_packs.json`, sets
`content-log-console-output-enabled=true` in `server.properties`, enables the
**Beta APIs** experiment in `level.dat`, and sets `BEDROCK_SCRIPT_EVENTS=true` +
`CHAT_RELAY=true` in `.env` — then you just restart the server and bot. Use
`--deaths-only` to skip the experiment (deaths only, no chat), or `--uninstall` to
reverse it (the Beta APIs experiment can't be undone — Bedrock flags a world
permanently once used). Full details and the manual steps are in
[`bedrock_pack/INSTALL.md`](bedrock_pack/INSTALL.md).

Caveats: the pack uses the Script API, so it tracks Minecraft's update cadence;
and enabling an experiment is **irreversible** for that world (and disables
achievements — moot on a dedicated server). Both flags default off, and the bot
runs fine without the pack (deaths/chat simply absent).

### Bedrock per-player restore

`/restore_player` on Bedrock needs two extra Python libraries to read/write the
world LevelDB. amulet-leveldb has no Linux wheels and its sdist needs Cython
3.0.x, so install it separately (one-time, in the bot's virtualenv):

```bash
sudo apt install -y build-essential zlib1g-dev
pip install --upgrade pip
printf 'cython>=3.0,<3.1\n' > /tmp/build-constraints.txt
PIP_BUILD_CONSTRAINT=/tmp/build-constraints.txt pip install -r requirements-bedrock-restore.txt
```
The bot lazily imports them — without them, backups and notifications still work;
only per-player restore is unavailable.

Each backup embeds a small `_players.json` sidecar (each player's data + their
account-stable identity), so a restore reads one zip with no chain reconstruction.
On restore the bot stops the server, overwrites that player's record, writes a
pre-restore undo to `BACKUP_DIR`, and relaunches via `MUX_START_CMD` (defaults to
`cd <MINECRAFT_DIR> && ./bedrock_server 2>&1 | tee -a console.log`; override only
for non-standard launches). If the relaunch fails, the bot prints the exact manual
start command.

**Name → player mapping.** A player's xuid (from the join log) and their LevelDB
identity aren't linked anywhere queryable, so the bot **learns** the binding the
first time that player is solo-online during a backup. Until a player has been
learned, `/restore_player <name>` finds no versions for them — just have them play
once.

---

## Reference

### Runtime state

The bot writes these at runtime (all git-ignored):

- `auth.json` — per-platform admin + authorised-chat list
  (`{telegram: {…}, slack: {…}}`; an old flat file is auto-migrated into the
  `telegram` namespace)
- `player_names.json` — *(Java)* UUID → username, learned from logs
- `player_achievements.json` — achievements with timestamps, keyed by UUID
- `player_deaths.json` — death history with timestamps, keyed by UUID
- `backup_manifest.json` — incremental backup state (chain ID, base full backup,
  file mtimes)
- `bedrock_player_state.json` — *(Bedrock)* per-player data hashes, so incremental
  sidecars only carry players that changed
- `bedrock_players.json` — *(Bedrock)* the portable player registry (replaces
  `player_names.json` on Bedrock)
- `statistics.json` — *(Bedrock)* accumulated online time + session counts;
  per-server, not portable
- `<MINECRAFT_DIR>/.mcnotifier_chain` — chain-validity marker in the server dir
- `logs/log_<YYYYMMDD_HHMMSS>.txt` — a new log file per bot start

To reset the bot to a fresh state, delete `auth.json`, `player_names.json`,
`player_achievements.json`, and `player_deaths.json`. To force a fresh backup
chain, delete `backup_manifest.json` and `.mcnotifier_chain`.

### Source layout

Entry points (run directly):

| File | Description |
|------|-------------|
| `bot.py` | Main bot — log watcher, command handlers, stats, orchestration |
| `restore.py` | Interactive CLI to restore from backup chains |
| `install_bedrock_pack.py` | One-command installer/uninstaller for the Bedrock behavior pack |

Packages and helpers (imported):

| Path | Description |
|------|-------------|
| `chat/` | Chat-platform adapters: `base` (interface + command router), `telegram`, `slack` |
| `backends/` | Edition backends: `base`, `java` (RCON), `bedrock` (tmux/screen), `mux` |
| `utils/` | Imported helpers: `config` (`ServerConfig` + world-layout), `backup_utils` (chain/manifest), `bedrock_player` (world-LevelDB + sidecar) |
| `bedrock_pack/` | Optional Bedrock behavior pack for chat + death events (Script API) |
| `requirements.txt` | Python dependencies |
| `requirements-bedrock-restore.txt` | Optional deps for Bedrock per-player restore |
| `.env.example` | Template for environment variables |

### Logging

Each run writes a timestamped log under `logs/`: join/leave, death, and
achievement notifications; every command (with the requester); admin actions
(claim, authorise, revoke); player-registry updates; backup progress; log-rotation
detection; and errors.
