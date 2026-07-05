# Diamond Sign

A chat bot that watches your Minecraft server(s) and keeps you in the loop from
**Telegram** and/or **Slack** — player join/leave, deaths, in-game chat, stats,
and automated backups with point-in-time restore. Works with **Java** and
**Bedrock** dedicated servers, and one process can front **multiple servers** and
run **multiple bots** at once.

**Highlights**

- **Notifications** to Telegram and/or Slack (both at once): join/leave, deaths,
  achievements, and a one-way in-game **chat relay**.
- **Player stats & playtime** leaderboards, and a known-player list.
- **Backups** — scheduled full backups plus space-efficient incrementals while
  players are online, with optional off-server copy.
- **Restore** — roll back the whole world to any backup point (the bot stops and
  restarts the server for you), or restore a **single player** without touching
  anyone else.
- **`/allowlist`** — manage the server's whitelist/allowlist from chat.
- **Multi-tenant** — one process serves any mix of bots × servers; commands are
  answered where they're sent, announcements go to the chats bound to each server.

### What works on each edition

| Feature | Java | Bedrock |
|---|:---:|:---:|
| Join / leave notifications | ✓ | ✓ |
| Player stats & `/playtime` | ✓ | ✓ |
| Full + incremental backups | ✓ | ✓ |
| Whole-world restore | ✓ — needs `mux` set (below) | ✓ |
| Per-player restore | ✓ | ✓ |
| `/allowlist` | ✓ | ✓ |
| Death notifications | ✓ | ✓ — needs the [behavior pack](#bedrock-chat--death-events) |
| In-game chat relay | ✓ | ✓ — needs the [behavior pack](#bedrock-chat--death-events) |
| Achievements | ✓ | ✗ — Xbox-bound, not exposed to servers |

> **Host requirement:** the Minecraft **server** must run on **Linux** (Windows
> file locking breaks backup zips, e.g. `session.lock`, and buffered log writes
> make command confirmations unreliable). The bot is developed and run on Linux
> too; it drives servers via tmux/screen, RCON, and POSIX signals.

---

## Quick start

1. **Install**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure** — the bot reads a single JSON file, **`diamondsign.json`**, next
   to `bot.py`. The fastest way to get a template:
   ```bash
   cp diamondsign.example.json diamondsign.json    # then edit it
   ```
   A minimal single-bot / single-server config:
   ```json
   {
     "version": 1,
     "bots": [
       {
         "name": "default",
         "platforms": ["telegram"],
         "telegram": { "bot_token": "123456:your-telegram-token" },
         "servers": [
           {
             "name": "survival",
             "edition": { "type": "java", "rcon": { "password": "your_rcon_password" } },
             "minecraft_dir": "/srv/survival",
             "backup": { "dir": "~/backup/survival",
                         "incremental": { "enabled": true } }
           }
         ]
       }
     ]
   }
   ```
   See [Configuration](#configuration) for every field, and
   [Multiple servers & bots](#multiple-servers--bots) for multi-server setups.

3. **Run**
   ```bash
   python bot.py                # run every configured bot
   python bot.py --only default # run just one bot (per-process isolation)
   ```

4. **Claim & authorise** — DM the bot to become its admin, then whitelist a group
   to receive notifications. See [Authorising chats](#authorising-chats).

Then set up your [chat platform](#connecting-a-chat-platform) and
[Minecraft server](#connecting-your-minecraft-server) (next two sections).

---

## Configuration

Everything lives in **`diamondsign.json`**: a list of **bots**, each a chat
identity (one Telegram bot and/or one Slack app) fronting a list of **servers**.

```jsonc
{
  "version": 1,
  "bots": [
    {
      "name": "default",                 // unique; namespaces this bot's auth
      "platforms": ["telegram", "slack"],// which adapters to run
      "telegram": { "bot_token": "123:ABC" },
      "slack":    { "bot_token": "xoxb-…", "app_token": "xapp-…" },
      "servers": [
        {
          "name": "survival",            // UNIQUE across the whole file, and used
                                         // verbatim as the data-dir key and the
                                         // /use & /authorize argument — so it must
                                         // be filesystem-safe: letters, digits,
                                         // '.', '_', '-' (NO spaces). Defaults to
                                         // the world's level-name (slugified).
          "edition": {                   // edition-specific settings, nested here:
            "type": "java",              //   "java" (default) or "bedrock"
            "rcon": { "password": "…", "host": "localhost", "port": 25575 }
                                         //   Java only — RCON command transport
            // Bedrock uses these two instead of rcon:
            //   "type": "bedrock",
            //   "bedrock_script_events": false,// deaths/chat via the behavior pack
            //   "console_log": null            // captured-stdout path (null → <dir>/console.log)
          },
          "minecraft_dir": "/srv/survival",
          "mux": { "session": "", "start_cmd": "" },  // shared: Bedrock's console
                                         // transport, and Java's optional /restore restart
          "chat_relay": false,           // shared: relay in-game chat to the chats
                                         // (on Bedrock also needs bedrock_script_events)
          "backup": {
            "dir": "~/backup/survival",
            "schedule": "daily",         // daily | weekly (Mon) | monthly (1st)
            "hour": 4,                   // 0–23, scheduled-backup hour
            "copy_cmd": "",              // off-server copy; "{file}" → zip path
            "pre_restore_backup": false, // /restore: back up current world first
            "restore_warning_seconds": 15,
            "incremental": { "enabled": true, "interval_minutes": 15 }
          }
        }
      ]
    }
  ]
}
```

The config is validated on start; it stops with a clear list of problems if
anything required is missing or a server `name` isn't filesystem-safe. Secrets
(`diamondsign.json`, `auth.json`) are git-ignored.

---

## Connecting a chat platform

A bot's `platforms` selects which adapters run; the bot serves them **all at
once** from one process. Commands are answered on the platform they arrive on;
announcements go to the chats bound to the relevant server. A `/backup` (or
restore) started while one is already running on that server replies "already in
progress" — per-server backups are serialised.

### Telegram

Long-polling — no public URL needed. Create a bot with
[@BotFather](https://t.me/BotFather) and put the token in the bot's
`telegram.bot_token`, with `"telegram"` in `platforms`.

### Slack

Uses **Socket Mode** (an outbound websocket), so no public URL or webhook is
needed — it works behind NAT like Telegram. Add `"slack"` to `platforms` and set
both `slack.bot_token` (`xoxb-…`) and `slack.app_token` (`xapp-…`).

1. Create an app at <https://api.slack.com/apps> → **From an app manifest**,
   select your workspace, choose the **JSON** tab, and paste the manifest below
   (it declares every slash command and the scopes).
2. **Basic Information → App-Level Tokens →** generate a token with the
   `connections:write` scope → that's `slack.app_token` (`xapp-…`).
3. **Install App** to your workspace → **Bot User OAuth Token** is
   `slack.bot_token` (`xoxb-…`).
4. Invite the bot to each channel you want notifications in (`/invite @yourbot`).
   `chat:write.public` lets it also post to public channels it hasn't joined.

```json
{
  "display_information": {
    "name": "Diamond Sign"
  },
  "features": {
    "bot_user": {
      "display_name": "Diamond Sign",
      "always_online": true
    },
    "slash_commands": [
      { "command": "/online", "description": "Show online players", "should_escape": false },
      { "command": "/list", "description": "List known players", "should_escape": false },
      { "command": "/stats", "description": "Player statistics", "should_escape": false },
      { "command": "/playtime", "description": "Playtime leaderboard", "should_escape": false },
      { "command": "/achievements", "description": "Player achievements", "should_escape": false },
      { "command": "/deaths", "description": "Death history", "should_escape": false },
      { "command": "/death_summary", "description": "Deaths grouped by cause", "should_escape": false },
      { "command": "/scan_achievements", "description": "Scan logs for achievements", "should_escape": false },
      { "command": "/scan_deaths", "description": "Scan logs for deaths", "should_escape": false },
      { "command": "/backup", "description": "Trigger a backup now", "should_escape": false },
      { "command": "/allowlist", "description": "Manage the allow/whitelist", "should_escape": false },
      { "command": "/restore_player", "description": "Restore one player", "should_escape": false },
      { "command": "/restore", "description": "Restore the whole world (stops + restarts the server)", "should_escape": false },
      { "command": "/start", "description": "Start the server if it's offline", "should_escape": false },
      { "command": "/chat_id", "description": "Show this chat's ID", "should_escape": false },
      { "command": "/use", "description": "Pick the server your commands act on", "should_escape": false },
      { "command": "/authorize", "description": "Whitelist a chat", "should_escape": false },
      { "command": "/revoke", "description": "Remove a chat", "should_escape": false },
      { "command": "/listchats", "description": "List authorized chats", "should_escape": false },
      { "command": "/commands", "description": "Show commands", "should_escape": false }
    ]
  },
  "oauth_config": {
    "scopes": {
      "bot": ["commands", "chat:write", "chat:write.public"]
    }
  },
  "settings": {
    "socket_mode_enabled": true,
    "org_deploy_enabled": false
  }
}
```

> **Why `/online` and `/commands` instead of `/status` and `/help`?** Slack
> reserves `/status` and `/help` for its own built-in commands and rejects an app
> manifest that tries to register them ("invalid name"). The Slack adapter maps
> `/online → status` and `/commands → help` internally, so they behave exactly
> like the Telegram `/status` and `/help`. All other commands are identical
> across platforms.

Slack uses string IDs (`U…` users, `C…`/`D…` channels), which `/authorize` and
`auth.json` handle automatically.

---

## Connecting your Minecraft server

Each server's `edition` is a nested object whose `type` is `java` or `bedrock`,
holding that edition's settings (Java: `rcon`; Bedrock: `bedrock_script_events`,
`console_log`). The two editions differ in how the bot sends commands to the
server. Settings that apply to both — `mux` and `chat_relay` — sit at the server
top level, alongside `edition`.

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
3. Set the same password in the server's `edition.rcon.password` (and
   `edition.rcon.port`/`edition.rcon.host` if not the defaults).

To also use **`/restore` and `/start`** (whole-world restore / bring-up), Java
needs a way to *restart* the JVM — RCON can stop it but can't relaunch it. Run the
server under tmux/screen and set the server's `mux`:

```jsonc
"mux": { "session": "0:survival",
         "start_cmd": "cd /srv/survival && java -Xmx4G -jar server.jar nogui" }
```

The bot stops it via RCON `stop` and restarts it by typing `start_cmd` into that
session. Without `mux`, everything else works and `/restore`/`/start` are refused
with a clear message.

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
the server. It auto-detects the session, or you can pin it with the server's
`mux.session`. If no session is found, the bot logs a clear message and skips that
server. The bot must run as the **same user** that owns the session (it can only
see that user's sessions).

**Setting `mux.session`.** This is the session **name**, not the multiplexer type
— `"tmux"` is wrong. Leave it blank to auto-detect the first live session, or set:

- `"session": "<session>"` — e.g. `"1"`
- `"session": "<session>:<window>"` — pin a specific window (recommended)
- `"session": "<session>:<window>.<pane>"` — pin a window and pane

Pinning the window is recommended because byobu uses **grouped tmux sessions**
(e.g. `1` and `1-8` sharing windows), where the active-window pointer shifts as
you navigate — so a bare session target can send a command to whatever window
you're viewing. Find the session and window with:

```bash
tmux list-sessions                 # session names, e.g. "1"
tmux list-windows -t 1             # window index + name, e.g. "0: bedrock"
```

If BDS runs in the window named `bedrock` of session `1`, set
`"session": "1:bedrock"` (the **name** is more robust than the index `1:0`, which
shifts if windows are reordered). On startup the log shows
`Detected tmux session for command injection: 1:bedrock`. For `screen`, the form
is `"<session>:<window-number>"`.

**Capturing output.** BDS writes to stdout, not `logs/latest.log`. Launch it so
its output is captured to a file the bot tails (default
`minecraft_dir/console.log`, override with `edition.console_log`), e.g. inside
your tmux/screen window:
```bash
./bedrock_server 2>&1 | tee -a console.log
```
The server's `mux.start_cmd` defaults to exactly this launch (used to relaunch BDS
after a restore); override only for a non-standard launch.

Bedrock also has a few edition-specific features and limitations — see
[Bedrock specifics](#bedrock-specifics).

---

## Multiple servers & bots

One process runs **any mix of bots × servers** from `diamondsign.json`:

- **One bot, many servers** — add more entries to a bot's `servers`. The bot
  routes each server's events to the chats **bound** to that server, and
  server-scoped commands (`/backup`, `/restore`, …) target a server you pick with
  **`/use <server>`** (or the channel's binding). `/authorize <chat_id> <server>`
  binds a chat to a server. (`/status` is the exception: in an admin DM it lists
  every server the bot fronts, so it never needs `/use`.)
- **Many bots** — add more entries to `bots` (e.g. a separate Telegram bot per
  community). Each bot has its own tokens, its own admin, and its own slice of
  `auth.json`. All run in one process with independent pollers (N tokens = N
  pollers, no conflict).
- **Per-process isolation** — `python bot.py --only <bot>` runs a single bot, for
  a systemd-unit-per-bot deployment.

On a **single-server bot** none of this is visible: commands act on the only
server implicitly and announcements fan out to every authorised chat.

Per-server state never collides: each server's files live under
`data/<name>/` (see [Runtime state](#runtime-state)).

---

## Authorising chats

Authorisation is **per bot, per platform** (each has its own admin and whitelist,
stored namespaced in `auth.json`). On each platform you use:

1. Send the bot a private message / DM — the first sender becomes that bot's
   **admin** on that platform (on Telegram, any message; on Slack, any slash
   command such as `/status`).
2. In the group/channel you want notifications in, send `/chat_id` to get its ID
   (works even before the chat is authorised; on Slack, invite the bot first).
3. In a DM to the bot, send `/authorize <chat_id>` to whitelist it. On a
   **multi-server** bot, add the target server: `/authorize <chat_id> <server>` —
   that binds the chat so it receives *that* server's announcements.

The bot then broadcasts each server's announcements (join/leave, deaths, …) to the
chats bound to it, and answers commands in whichever chat they're sent.
`/listchats` shows the authorised chats (with names and their bound server);
`/revoke <chat_id>` removes one.

---

## Commands

| Command | Description |
|---------|-------------|
| `/status` | Show whether the server is online, and who's playing. In an admin DM it lists every server the bot fronts; in an authorized group/channel it reports just that chat's bound server |
| `/list` | List all known players |
| `/stats [player]` | Full stats for one or all players |
| `/playtime` | Playtime leaderboard |
| `/achievements [player]` | Show player achievements with timestamps |
| `/deaths [player]` | Show death history |
| `/death_summary` | Deaths grouped by cause with per-player counts |
| `/chat_id` | Show the current chat's ID |
| `/use [<server>]` | *(Admin)* Pick which server your commands act on; bare `/use` lists servers (multi-server bots only) |
| `/authorize <chat_id> [<server>]` | *(Admin)* Whitelist a chat; on a multi-server bot bind it to `<server>` |
| `/revoke <chat_id>` | *(Admin)* Remove a chat from the whitelist |
| `/listchats` | *(Admin)* List authorised chats (name + bound server) |
| `/scan_achievements` | *(Admin)* Scan all log files for achievements |
| `/scan_deaths` | *(Admin)* Scan all log files for deaths |
| `/backup` | *(Admin)* Trigger a server backup now |
| `/allowlist <on\|off\|add\|remove\|list\|reload> [player]` | *(Admin)* Manage the server allow/whitelist; the server's response is piped back |
| `/restore_player <username> [<N> [confirm]]` | *(Admin)* List, select, and restore one player's data |
| `/restore [<N> [confirm]]` | *(Admin)* Restore the whole world to a backup point — warns players in-game, then stops, replaces, and restarts the server |
| `/start` | *(Admin)* Start the server if it's offline (via `mux.start_cmd`) |

**Online-only commands.** `/backup`, `/allowlist`, and `/restore_player` need the
server **running** and are refused with a clear message when it's offline (bring
it up with `/start`). Read commands (`/list`, `/stats`, `/deaths`, …) read stored
data/world files and work whether the server is up or down. `/restore` works
offline (it's the recovery path).

**Slack note.** Slack reserves `/status` and `/help`, so on Slack they're
`/online` and `/commands` respectively (the bot maps them back internally). Every
other command name is the same on both platforms.

**Edition notes.** On **Bedrock**, `/achievements` and `/scan_achievements` are
unavailable (achievements are Xbox-bound and not exposed to servers). `/deaths`
and `/death_summary` work only with the optional [behavior
pack](#bedrock-chat--death-events) + `edition.bedrock_script_events: true`; the `/scan_*`
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

**Chat relay** (a server's `chat_relay: true`, off by default) mirrors in-game
chat to that server's authorised chats as `💬 <player>: <message>` — one-way (chat
platforms → game is not relayed). On **Java** this works out of the box (chat is
read from `latest.log`); on **Bedrock** it needs the [behavior
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
4. Zip the entire `minecraft_dir` (e.g. `survival_20260401_040000.zip`) into the
   server's `backup.dir`.
5. `save-on` re-enables auto-save (guaranteed even if the zip fails).
6. Optionally run `backup.copy_cmd` to copy the zip off-server.

Players don't need to be kicked — the save-off/save-all/save-on sequence ensures a
consistent snapshot while the server stays online.

On **Bedrock** the freeze sequence is `save hold` → `save query` → `save resume`,
copying each file truncated to the snapshot length `save query` reports;
everything else (scheduling, chains, off-server copy, restore) is identical. Bot
infrastructure that lives in the server directory is excluded from every zip (the
`.diamondsign_chain` marker on both editions, and the Bedrock `console.log`), and
Unix file permissions (e.g. the executable bit on the Bedrock server binary) are
preserved through backup and restore.

**Configuration** (per server, under `backup`):

| `backup.…` key | Description | Default |
|----------------|-------------|---------|
| `dir` | Where backup zips are saved | `~/minecraft_backup` |
| `schedule` | `daily`, `weekly` (Monday), or `monthly` (1st) | `daily` |
| `hour` | Hour of day (0–23) for the scheduled backup | `4` |
| `copy_cmd` | Shell command to copy the zip off-server; `{file}` → the full zip path | *(empty — disabled)* |
| `pre_restore_backup` | `/restore`: take a full backup of the current world before wiping it | `false` |
| `restore_warning_seconds` | `/restore`: in-game warning lead time before the stop | `15` |
| `incremental.enabled` | Enable incrementals while players are online | `false` |
| `incremental.interval_minutes` | Minutes between incrementals while players are active | `15` |

**Off-server copy examples** (`backup.copy_cmd`):

```jsonc
"copy_cmd": "cp {file} /mnt/nas/minecraft_backups/"                 // mounted NAS
"copy_cmd": "scp {file} user@backup-server:/backups/minecraft/"     // SCP
"copy_cmd": "rsync -az {file} user@backup-server:/backups/minecraft/" // rsync
```

**Triggering:** `/backup` runs one immediately (admin, private chat); the
scheduled backup runs at `hour` on the `schedule`. Progress is sent to the admin's
chat.

### Incremental backups

When `incremental.enabled` is set, the bot backs up only files changed since the
last backup while players are active — far smaller than repeated full backups.

1. The cycle starts when the first player joins.
2. Every `interval_minutes`, the bot compares file mtimes against a stored
   manifest, freezes the world (Java: save-off/save-all; Bedrock: save
   hold/query), waits for the filesystem to settle, and zips only changed/added
   files (plus `_deletions.json` for removed ones), then resumes saving.
3. When the last player leaves, one final incremental runs and the cycle stops.
4. A full backup resets the manifest, so the next incremental only captures
   changes since that full backup.

**Chains.** Each full backup starts a new chain (an 8-char hex ID embedded in
incremental filenames and contents) so that after a restore, new incrementals are
never confused with old ones. A `.diamondsign_chain` marker in the server
directory lets the bot detect if the server state was replaced while it was
offline; if the marker doesn't match the manifest on startup, incrementals pause
until the next full backup. File naming:

- Full: `servername_20260401_040000.zip`
- Incremental: `servername_incr_a1b2c3d4_20260401_041500.zip` (includes the chain ID)

---

## Restoring from backups

### Whole-world restore from chat — `/restore`

`/restore` (admin, in a DM) restores the entire world to a chosen backup point
**and handles the server for you**: it warns players in-game, stops the server,
replaces the world with the restored chain, and restarts it — no manual
stop/start. It runs in three enforced steps so a mistyped message can't wipe the
world:

1. `/restore` — lists restore points (full + incrementals, latest first);
   `/restore more` pages.
2. `/restore <N>` — shows a confirmation block (the point, whether a pre-restore
   backup will run, and the in-game warning window).
3. `/restore <N> confirm` — warns players, stops, restores, and restarts.

Tune it with `backup.pre_restore_backup` (take a recoverable snapshot of the
current world before wiping — default off) and `backup.restore_warning_seconds`.

**Restart transport.** `/restore` must stop and restart the server. **Bedrock**
already runs under tmux/screen with a start command, so it works out of the box.
**Java** additionally needs `mux.session` + `mux.start_cmd` set (see
[Java](#java-rcon)). Without them, `/restore` is refused with a clear message;
everything else keeps working.

### Offline restore — `restore.py`

For disaster recovery when the bot isn't running, `restore.py` does the same
restore from a shell (it does **not** stop/start the server — do that yourself):

```bash
python restore.py                            # pick a server from diamondsign.json
python restore.py --server <name>            # skip the picker (name/key)
python restore.py --dry-run                  # preview only
python restore.py --backup-dir P --target-dir Q   # files-only, no chain reset
```

Run with no arguments and it reads `diamondsign.json` and offers a **numbered
list of servers** to choose from (a single-server config is auto-selected);
`--server <name>` skips the picker. It then scans that server's backup directory,
groups incrementals into chains, shows restore points, and extracts the full
backup and applies each incremental up to your pick. In server mode (in-place) it
rebuilds the manifest and writes a fresh `.diamondsign_chain`; restoring to an
incremental point also writes a single **merged incremental** so the new chain
needs only the original full + that one file. The `--backup-dir P --target-dir Q`
files-only mode (no server context) extracts files only — no chain reset.

If you restore by other means (manual copy, other tools), delete that server's
`data/<name>/backup_manifest.json` and its `.diamondsign_chain` so the bot starts
a fresh chain on the next full backup.

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
  writes a pre-restore undo copy to `backup.dir`, and **relaunches** via
  `mux.start_cmd` (the server is briefly offline). See [Per-player restore on
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
lines (`DIAMONDSIGN {…}`) which — with `content-log-console-output-enabled=true` —
land in the same `console.log` the bot already tails, so no HTTP endpoint or
network permission is needed. The bot feeds these into the normal notify pipeline:
deaths announce + record (`/deaths`, `/death_summary` work, with a Bedrock
damage-cause→message map that mirrors Java's wording), and chat is relayed like on
Java.

**Install — one command** (with the server stopped, from the repo root):

```bash
python install_bedrock_pack.py
```

It reads the Bedrock server(s) from `diamondsign.json` (if you have more than
one, it lists them and asks which — or pass `--server <name>`), confirms it's
Bedrock and stopped, takes the world's `level-name` from `server.properties`,
copies the pack into `behavior_packs/diamondsign_events/`, activates it in the
world's `world_behavior_packs.json`, sets
`content-log-console-output-enabled=true` in `server.properties`, enables the
**Beta APIs** experiment in `level.dat`, and sets that server's
`edition.bedrock_script_events: true` (and top-level `chat_relay: true` for chat)
in `diamondsign.json` — then you just restart the server and bot. Use `--deaths-only`
to skip the experiment (deaths only, no chat), or `--uninstall` to reverse it (the
Beta APIs experiment can't be undone — Bedrock flags a world permanently once
used). Full details and the manual steps are in
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
pre-restore undo to `backup.dir`, and relaunches via `mux.start_cmd` (defaults to
`cd <minecraft_dir> && ./bedrock_server 2>&1 | tee -a console.log`; override only
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

Per-server state lives under **`data/<name>/`** (keyed by the server's `name`), so
servers in one process never collide. All of it is git-ignored:

- `data/<name>/player_names.json` — *(Java)* UUID → username, learned from logs
- `data/<name>/player_achievements.json` — achievements with timestamps, by UUID
- `data/<name>/player_deaths.json` — death history with timestamps, by UUID
- `data/<name>/backup_manifest.json` — incremental backup state (chain ID, base
  full backup, file mtimes)
- `data/<name>/bedrock_players.json` — *(Bedrock)* the portable player registry
- `data/<name>/statistics.json` — *(Bedrock)* accumulated online time + session
  counts (per-server, not portable)
- `data/<name>/bedrock_player_state.json` — *(Bedrock)* per-player data hashes, so
  incremental sidecars only carry players that changed
- `<minecraft_dir>/.diamondsign_chain` — chain-validity marker, in the server dir

Process-wide state (repo root, git-ignored):

- `auth.json` — bot-namespaced admin + authorised chats:
  `{ "<bot>": { "<platform>": { "admin_user_id", "authorized_chat_ids",
  "chat_servers", "chat_names" } } }`. An older flat / platform-level file is
  auto-migrated on load.
- `diamondsign.json` — your config. `.env` (if present) is legacy, read only to
  migrate once.
- `logs/log_<YYYYMMDD_HHMMSS>.txt` — a new log file per bot start.

To reset a server to fresh state, delete its `data/<name>/` directory. To reset
authorisation, delete `auth.json`. To force a fresh backup chain for a server,
delete its `data/<name>/backup_manifest.json` and `<minecraft_dir>/.diamondsign_chain`.

### Source layout

Entry points (run directly):

| File | Description |
|------|-------------|
| `bot.py` | Main bot — multi-bot/server loop, log watcher, command handlers, backups, orchestration |
| `restore.py` | Interactive CLI to restore from backup chains (offline; over `utils/restore_core`) |
| `install_bedrock_pack.py` | One-command installer/uninstaller for the Bedrock behavior pack |

Packages and helpers (imported):

| Path | Description |
|------|-------------|
| `chat/` | Chat-platform adapters: `base` (interface + command router), `telegram`, `slack` |
| `backends/` | Edition backends: `base`, `java` (RCON), `bedrock` (tmux/screen), `mux` |
| `utils/` | Imported helpers: `config` (`AppConfig`/`BotConfig`/`ServerConfig` + JSON loader), `backup_utils` (chain/manifest), `restore_core` (headless restore, shared by the bot + `restore.py`), `bedrock_player` (world-LevelDB + sidecar) |
| `bedrock_pack/` | Optional Bedrock behavior pack for chat + death events (Script API) |
| `requirements.txt` | Python dependencies |
| `requirements-bedrock-restore.txt` | Optional deps for Bedrock per-player restore |
| `diamondsign.example.json` | Annotated config template |

### Logging

Each run writes a timestamped log under `logs/`. Per-server lines are prefixed
`[<server>]` and per-bot lines `[<bot>]`, so multi-server output stays
attributable: join/leave, death, and achievement notifications; every command
(`[<bot>] <Action>: requested by [<user>] on [<chat>]`); admin actions (claim,
authorise, revoke); player-registry updates; backup progress; log-rotation and
server-(re)start detection; and errors.
