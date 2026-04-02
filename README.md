# mcnotifier

A Telegram bot that monitors a Minecraft server and sends notifications when players join or leave. Tracks achievements, deaths, player stats, and performs automated daily backups via RCON.

## Files

| File | Description |
|------|-------------|
| `bot.py` | Main bot — log watcher, Telegram handlers, stats |
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
   - `BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
   - `MINECRAFT_DIR` — absolute path to the Minecraft server directory (e.g. `/home/user/Minecraft`)
   - `RCON_PASSWORD` — RCON password (must match `server.properties` `rcon.password`)
   - `BACKUP_HOUR` — hour of day (0–23) for daily auto-backup (default: `4`)
   - `BACKUP_DIR` — directory for backup zip files (default: `~/minecraft_backup`)
   - `BACKUP_COPY_CMD` — optional shell command to copy the backup off-server; `{file}` is replaced with the zip path (e.g. `scp {file} user@nas:/backups/` or `cp {file} /mnt/backup/`)

3. **Run**
   ```bash
   python bot.py
   ```

## First-time authorisation

1. Send any private message to the bot — the first sender becomes the **admin**.
2. Add the bot to your group chat, then send `/chat_id` in the group to get its ID.
3. In a private message to the bot, send `/authorize <chat_id>` to whitelist the group.

The bot will now send join/leave notifications to all authorised chats and respond to commands there.

## RCON setup

The backup feature and any future server commands require RCON to be enabled on the Minecraft server.

1. Edit `server.properties` and set:
   ```
   enable-rcon=true
   rcon.port=25575
   rcon.password=your_password
   ```
2. Restart the Minecraft server for the changes to take effect.
3. Set the same password in `.env` as `RCON_PASSWORD`.

## Backups

The bot performs automated daily backups of the entire Minecraft server directory.

**How it works:**

1. Sends `save-off` via RCON to disable auto-save.
2. Sends `save-all` to flush world data from memory to disk.
3. Zips the entire `MINECRAFT_DIR` (e.g. `minecraftopia_20260401_040000.zip`).
4. Sends `save-on` to re-enable auto-save (guaranteed even if the zip fails).
5. Saves the zip to `BACKUP_DIR` (default: `~/minecraft_backup`).
6. Optionally runs `BACKUP_COPY_CMD` to copy the zip off-server.

Players do not need to be kicked — the save-off/save-all/save-on sequence ensures a consistent snapshot while the server stays online.

**Configuration:**

| Variable | Description | Default |
|----------|-------------|---------|
| `RCON_PASSWORD` | Must match `server.properties` `rcon.password` | *(required for backup)* |
| `BACKUP_HOUR` | Hour of day (0–23) for the daily auto-backup | `4` |
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

**Daily auto-backup:** Runs automatically at the configured `BACKUP_HOUR`. Progress is sent to the admin's private chat.

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

## Runtime state

The bot writes the following at runtime (all excluded from git):

- `auth.json` — admin user ID and authorised chat list
- `player_names.json` — UUID → username mappings learned from server logs
- `player_achievements.json` — player achievements with timestamps, keyed by UUID
- `player_deaths.json` — player death history with timestamps, keyed by UUID
- `logs/log_<YYYYMMDD_HHMMSS>.txt` — a new log file is created each time the bot starts

Delete `auth.json`, `player_names.json`, `player_achievements.json`, and `player_deaths.json` to reset the bot to a fresh state.

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
