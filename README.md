# mcnotifier

A Telegram bot that monitors a Minecraft server and sends notifications when players join or leave. Tracks achievements, deaths, player stats, and performs automated backups via RCON. Full backups run on a configurable daily, weekly, or monthly schedule, while incremental backups capture changes every few minutes (configurable) as players explore the world. Restoration to any backup point is done through an interactive CLI tool and requires the server to be offline.

## Files

| File | Description |
|------|-------------|
| `bot.py` | Main bot — log watcher, Telegram handlers, stats |
| `backup_utils.py` | Shared backup utilities (chain IDs, manifest, constants) |
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

## Backups

The bot performs automated full backups of the entire Minecraft server directory on a configurable schedule (daily, weekly, or monthly).

**How it works:**

1. Sends `save-off` via RCON to disable auto-save.
2. Sends `save-all` to flush world data from memory to disk.
3. Waits for the filesystem to settle (no file changes for 5 seconds) to ensure all data is fully written.
4. Zips the entire `MINECRAFT_DIR` (e.g. `minecraftopia_20260401_040000.zip`).
5. Sends `save-on` to re-enable auto-save (guaranteed even if the zip fails).
6. Saves the zip to `BACKUP_DIR` (default: `~/minecraft_backup`).
7. Optionally runs `BACKUP_COPY_CMD` to copy the zip off-server.

Players do not need to be kicked — the save-off/save-all/save-on sequence ensures a consistent snapshot while the server stays online.

**Configuration:**

| Variable | Description | Default |
|----------|-------------|---------|
| `RCON_PASSWORD` | Must match `server.properties` `rcon.password` | *(required for backup)* |
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
   - If changes are detected, runs save-off / save-all via RCON, then waits for the filesystem to settle (no file changes for 5 seconds).
   - Creates a zip containing only changed/added files, plus `_deletions.json` for removed files.
   - Runs save-on to re-enable auto-save.
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
6. Rebuilds `backup_manifest.json` and `.mcnotifier_chain` with a new chain ID. When restoring to an incremental point, creates a **merged incremental** zip that combines all applied incrementals into a single file — so the new chain only needs the original full backup + one merged incremental for future restores.

**Important:** Always stop the Minecraft server before restoring. Restoring while the server is running will cause data corruption.

**If you restore by other means** (manual copy, other tools): delete `backup_manifest.json` and `.mcnotifier_chain` from the server directory to force the bot to start a fresh chain with the next full backup.

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
- `backup_manifest.json` — incremental backup state: chain ID, base full backup, and file modification timestamps
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
