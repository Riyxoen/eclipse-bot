# Eclipse — Discord Moderation Bot

A production-quality Discord moderation bot built with [discord.py](https://pypi.org/project/discord.py/).

Eclipse provides a full moderation toolkit: slash commands, text commands, an opt-in automated moderation engine, per-guild configuration, and a complete case history — all backed by local SQLite with no external services.

## Features

- **Moderation commands** — `/warn`, `/timeout`, `/kick`, `/ban`, `/unban`, `/purge`, `/slowmode`, `/lock`, `/unlock` with confirmation prompts for dangerous actions
- **Case history** — every moderation action creates a permanent, guild-isolated case record (`/case`, `/cases`, `/moderation-history`)
- **Automated moderation** — opt-in engine with spam, duplicate, mention, link, invite, word-filter, and raid-protection detectors (each configurable per guild)
- **Per-guild configuration** — `/config` and `/automod` commands let administrators tune thresholds, exemptions, and actions without restarting
- **Custom roles** — `·cr enable`, `·cr rename`, `·cr color` for a bot-managed role
- **AFK system** — `·afk [message]` to set AFK status with automatic nickname change and mention notifications
- **Jail system** — `·jail setup`, `·jail @user`, `·unjail @user` with full role preservation
- **Text commands** — `·warn`, `·ban`, `·kick`, `·mute`, `·unmute`, `·untimeout`, `·purge`, `·slowmode`, `·lockdown`, `·unlockdown`, `·afk`, `·jail`, `·unjail`, `·help` with a configurable prefix (default `·`)

## Requirements

- **Python 3.11+** (developed and tested on 3.14)
- A Discord bot account — [create one here](https://discord.com/developers/applications)
- The **Members** privileged intent enabled in the developer portal (*Bot → Privileged Gateway Intents → Server Members Intent*) — required for member caching, role-hierarchy checks, and target validation
- The **Message Content** intent is required for prefix commands and automated moderation. Enable it in the developer portal if you plan to use those features. The bot will refuse to start with mismatched intent settings

## Setup

### 1. Create a virtual environment

With [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv sync
source .venv/bin/activate
```

Or with the standard library:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest pytest-asyncio ruff
```

### 2. Configure the environment

```bash
cp .env.example .env
```

Edit `.env` and set your bot token:

```
DISCORD_TOKEN=your_token_here
```

> **Never commit `.env` to version control.** It is already in `.gitignore`.

### 3. Run

```bash
python main.py --check     # validate configuration and initialize the database (offline)
python main.py             # start the bot (also: python -m bot)
```

Stop with `Ctrl+C` — shutdown is graceful with a bounded timeout.

## Configuration

All settings come from environment variables (or `.env`). These seed every guild's defaults; servers override them at runtime via `/config`.

### Required

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Bot token from the Discord developer portal. **Never commit this value.** |

### Logging & Runtime

| Variable | Default | Purpose |
| --- | --- | --- |
| `RIYXOEN_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `RIYXOEN_LOG_FILE` | *(console only)* | Optional log file path |
| `RIYXOEN_SHUTDOWN_TIMEOUT_SECONDS` | `10` | Graceful-shutdown timeout |

### Intents & Prefix

| Variable | Default | Purpose |
| --- | --- | --- |
| `RIYXOEN_ENABLE_MESSAGE_CONTENT_INTENT` | `1` | Enable the privileged message-content intent (required for prefix commands and automod; must also be enabled in the developer portal) |
| `RIYXOEN_COMMAND_PREFIX` | `·` | Prefix for text commands (e.g. `·ban`, `·kick`). 1–3 non-space characters |

### Moderation

| Variable | Default | Purpose |
| --- | --- | --- |
| `RIYXOEN_MODERATOR_ROLE_IDS` | *(empty)* | Comma-separated Discord role IDs allowed to run all moderation commands |
| `RIYXOEN_LOG_CHANNEL_ID` | *(disabled)* | Discord channel ID that receives a case summary per action |
| `RIYXOEN_NOTIFY_USERS` | `1` | Set `0` to disable DM notifications to punished users |
| `RIYXOEN_MAX_PURGE_AMOUNT` | `100` | Max messages per `/purge` (1–1000) |
| `RIYXOEN_DATABASE_PATH` | `data/cases.db` | Local SQLite case database path (git-ignored) |

### Automated Moderation

Automated moderation is **off by default**. Set `RIYXOEN_AUTOMOD_ENABLED=1` to enable it. Enabling this also requires the message-content intent.

| Variable | Default | Purpose |
| --- | --- | --- |
| `RIYXOEN_AUTOMOD_ENABLED` | `0` | Master switch for automated moderation |
| `RIYXOEN_AUTOMOD_SPAM_THRESHOLD` | `5` | Messages within the spam window before enforcement |
| `RIYXOEN_AUTOMOD_SPAM_WINDOW_SECONDS` | `5` | Spam detection window (seconds) |
| `RIYXOEN_AUTOMOD_SPAM_ACTION` | `delete` | Action for spam: `delete`, `warn`, or `timeout` |
| `RIYXOEN_AUTOMOD_DUPLICATE_THRESHOLD` | `4` | Identical messages before enforcement |
| `RIYXOEN_AUTOMOD_DUPLICATE_WINDOW_SECONDS` | `30` | Duplicate detection window |
| `RIYXOEN_AUTOMOD_DUPLICATE_ACTION` | `delete` | Action for duplicates |
| `RIYXOEN_AUTOMOD_MENTION_USER_THRESHOLD` | `10` | Max unique user mentions (`0` disables) |
| `RIYXOEN_AUTOMOD_MENTION_ROLE_THRESHOLD` | `6` | Max role mentions |
| `RIYXOEN_AUTOMOD_MENTION_TOTAL_THRESHOLD` | `15` | Max total mentions |
| `RIYXOEN_AUTOMOD_MENTION_EVERYONE_THRESHOLD` | `0` | Max @everyone/@here mentions (`0` = disabled) |
| `RIYXOEN_AUTOMOD_MENTION_ACTION` | `delete` | Action for mention spam |
| `RIYXOEN_AUTOMOD_TIMEOUT_DURATION_SECONDS` | `3600` | Default timeout duration when action is `timeout` |
| `RIYXOEN_AUTOMOD_LINK_ACTION` | `allow` | URL enforcement: `allow`, `delete`, `warn`, or `timeout` |
| `RIYXOEN_AUTOMOD_ALLOWED_DOMAINS` | *(empty)* | Comma-separated domain allowlist (exact-or-subdomain match) |
| `RIYXOEN_AUTOMOD_BLOCKED_TERMS` | *(empty)* | Comma-separated blocked terms (whole-word by default) |
| `RIYXOEN_AUTOMOD_BLOCKED_TERMS_SUBSTRING` | `0` | Set `1` for substring matching instead of whole-word |
| `RIYXOEN_AUTOMOD_WORD_FILTER_ACTION` | `delete` | Action for blocked terms |
| `RIYXOEN_AUTOMOD_EXEMPT_USER_IDS` | *(empty)* | Comma-separated user IDs exempt from detection |
| `RIYXOEN_AUTOMOD_EXEMPT_ROLE_IDS` | *(empty)* | Comma-separated role IDs exempt from detection |
| `RIYXOEN_AUTOMOD_EXEMPT_CHANNEL_IDS` | *(empty)* | Comma-separated channel IDs exempt from detection |
| `RIYXOEN_AUTOMOD_ENFORCEMENT_COOLDOWN_SECONDS` | `60` | Cooldown between enforcements for the same user |
| `RIYXOEN_AUTOMOD_WARNING_WINDOW_SECONDS` | `604800` | Warning expiration window (`0` = never expire) |
| `RIYXOEN_AUTOMOD_ESCALATION` | *(empty)* | Warn-to-timeout escalation, e.g. `3:3600,5:43200` |

### Invite & Raid Protection

| Variable | Default | Purpose |
| --- | --- | --- |
| `RIYXOEN_AUTOMOD_INVITE_ACTION` | `allow` | Invite link enforcement |
| `RIYXOEN_AUTOMOD_INVITE_ALLOWED_CODES` | *(empty)* | Comma-separated allowed invite codes |
| `RIYXOEN_AUTOMOD_RAID_JOIN_THRESHOLD` | `10` | Join count for raid detection |
| `RIYXOEN_AUTOMOD_RAID_WINDOW_SECONDS` | `10` | Raid detection window |
| `RIYXOEN_AUTOMOD_RAID_ACTION` | `alert` | Raid response: `alert` (default, safe) or `timeout` |

## Commands

### Slash Commands

| Command | Description |
| --- | --- |
| `/ping` | Reply with gateway latency |
| `/help` | Show available commands |
| `/health ping` | Health check |
| `/warn <member> [reason]` | Warn a member (records a case, optionally DMs them) |
| `/timeout <member> <duration> [reason]` | Time out a member (e.g. `10s`, `30m`, `2h`, `3d`; max 28 days) |
| `/kick <member> [reason]` | Kick a member (confirmation required) |
| `/ban <member> [reason]` | Ban a member (confirmation required) |
| `/unban <user> [reason]` | Unban a user — pick from autocompleted banned users |
| `/purge <amount> [reason]` | Bulk-delete messages (large purges require confirmation) |
| `/slowmode <duration> [channel] [reason]` | Set channel slowmode (`0` clears; max 6 hours) |
| `/lock [channel] [reason]` | Lock a channel (confirmation required) |
| `/unlock [channel] [reason]` | Unlock a channel (restores exact pre-lock permissions) |
| `/case <case_id>` | Show a full case record |
| `/cases <member> [page]` | List a member's cases (10 per page) |
| `/moderation-history <member> [page]` | Alias of `/cases` |
| `/config view [page]` | Show server configuration (admin only) |
| `/config prefix <prefix>` | Set the text-command prefix (admin only) |
| `/config moderation …` | Tune spam/duplicate/mention/link/word settings (admin only) |
| `/config logs [channel] [enabled]` | Set the log channel (admin only) |
| `/config roles …` | Manage moderator/administrator roles (admin only) |
| `/config exemptions …` | Manage exempt users/roles/channels (admin only) |
| `/config reset [confirm]` | Restore documented defaults (admin only) |
| `/automod enable` / `/automod disable` | Toggle automated moderation per guild (admin only) |
| `/automod status` | Show every detector's state (admin only) |
| `/automod invites action <…>` | Set invite filtering action (admin only) |
| `/automod invites allowed add\|remove\|list` | Manage allowed invite codes (admin only) |
| `/automod raid configure <threshold> <window> <action>` | Configure raid protection (admin only) |

Reasons are **optional** in slash commands and default to "No reason provided".

### Text Commands

Text commands use a configurable prefix (default `·`). Require the message-content intent.

| Command | Description |
| --- | --- |
| `·cr enable` | Enable the custom-role system for this server |
| `·cr rename <name>` | Rename the managed role |
| `·cr color <hex>` | Set the managed role's color (e.g. `·cr color #5865f2`) |
| `·warn @user <reason>` | Warn a member (reason required) |
| `·warnings @user` | Show a member's active warning count |
| `·clearwarnings @user` | Clear a member's active warnings |
| `·modhistory @user [page]` | Show a member's moderation cases |
| `·purge <amount>` | Bulk-delete recent messages |
| `·slowmode <seconds>` | Set channel slowmode (`0` clears; max 6 hours) |
| `·lockdown` / `·unlockdown` | Lock / unlock the current channel |
| `·ban @user <reason>` | Ban a member (reason required) |
| `·kick @user <reason>` | Kick a member (reason required) |
| `·mute @user <duration> <reason>` | Mute via Discord timeout (e.g. `10m`, `1h`, `2h`, `1d`) |
| `·unmute @user <reason>` | Remove a timeout |
| `·untimeout @user [reason]` | Remove a Discord timeout |
| `·afk [message]` | Set AFK status (auto-removed on next message) |
| `·jail setup` | Configure the jail system (admin only) |
| `·jail @user [reason]` | Jail a member (admin only) |
| `·unjail @user` | Release a member from jail (admin only) |
| `·help` | Show organized command help by category |

Reasons are **required** for `·warn`, `·ban`, `·kick`, `·mute`, `·unmute`. All reasons are capped at 300 characters. If a DM to a punished user cannot be delivered, the action still succeeds and the moderator sees a note.

### AFK System

When a user sends `·afk [message]`, their nickname changes to `AFK | <name>` and the original name is preserved. When they send another message, AFK is automatically removed. If another user mentions an AFK user, a notification is sent. AFK state persists through bot restarts.

### Jail System

Administrators configure the jail with `·jail setup`, which creates a jail role and channel. When `·jail @user` is used, the user's previous roles are saved, roles are stripped, and the jail role is applied. `·unjail @user` restores previous roles (deleted roles are skipped safely). Every jail/unjail action creates a moderation case for full audit trail.

## Automated Moderation

The automated moderation engine is **opt-in** (`RIYXOEN_AUTOMOD_ENABLED=1`). When enabled, guild messages flow through a detection pipeline:

```
Message received → normalization → exemption check → detectors → action → case record
```

### Detectors

| Detector | What it catches | Default |
| --- | --- | --- |
| **Spam** | N messages from one user within N seconds | 5 msgs / 5 s → delete |
| **Duplicate** | N identical messages within N seconds | 4 msgs / 30 s → delete |
| **Mentions** | Excessive user/role/total/@everyone mentions | 10/6/15/0 → delete |
| **Links** | URLs not on the allowlist | allow (no enforcement) |
| **Word filter** | Configured blocked terms | empty → off |
| **Invites** | Discord invite links | allow (no enforcement) |
| **Raid** | Join bursts (N joins in N seconds) | alert to log channel |

**Safety:** Only `delete`, `warn`, and `timeout` are automated — ban and kick are never automatic. An enforcement cooldown prevents repeated punishment for one burst. Exempt users, roles, channels, moderators, bots, and the server owner are always skipped.

### Warning Escalation

Configure `RIYXOEN_AUTOMOD_ESCALATION=3:3600,5:43200` to convert warnings into timeouts at thresholds (3rd warning → 1h timeout, 5th → 12h). Active warnings expire after `RIYXOEN_AUTOMOD_WARNING_WINDOW_SECONDS` (default 7 days).

### Per-Guild Overrides

After the initial environment seed, administrators tune automod per guild with `/config` and `/automod` commands — no restart needed.

## Permission Model

Every moderation action is validated **server-side**. The bot verifies:

1. The command was used inside a guild
2. The bot has the required Discord permission
3. The moderator has the required permission **or** a configured moderator role
4. The target is not the bot itself or the server owner
5. Role hierarchy is respected (moderator's highest role > target's highest role)
6. The action is valid for the target's current state

Required permissions: warn/timeout → `Moderate Members`; kick → `Kick Members`; ban/unban → `Ban Members`; purge → `Manage Messages`; slowmode/lock/unlock → `Manage Channels`.

`/config` commands require the server owner, `Administrator` permission, or a configured administrator role.

## Case Records

Every moderation action produces a case record:

```
Case #42
Action: Timeout
Target: @user
Moderator: @moderator
Reason: spam
Status: Success
```

Cases are stored in a local SQLite database (`data/cases.db`), isolated per guild. A case from guild A is invisible in guild B. Failed actions also produce records with a `failed` status for audit purposes.

## Security

- **Never commit your `.env` file.** It is already in `.gitignore`. `.env.example` contains only placeholders.
- **Keep `DISCORD_TOKEN` in environment variables only.** Do not hardcode it in source files.
- **Automated moderation is off by default.** Enable it only when you need it.
- **All moderation data is local.** Eclipse uses SQLite — no external databases, cloud services, or telemetry.
- **Log redaction** prevents tokens and sensitive values from appearing in log output.
- **Guild isolation** ensures each server's cases, configuration, and moderation data are completely independent.

## Development & Testing

### Running the Test Suite

```bash
python -m pytest           # run the full test suite
python -m compileall .     # byte-compile all sources
ruff check .               # lint
ruff format --check .      # verify formatting
```

The unit tests use lightweight fakes for Discord objects and never contact a real server. Tests cover:

- Database initialization, migrations, and schema management
- Case creation, retrieval, listing, updates, and guild isolation
- Permission checks and role hierarchy validation
- Confirmation flows (expiry, cancellation, double-click protection)
- Automated moderation detectors, exemptions, and enforcement
- Per-guild configuration (model, repository, service, and command layer)
- Custom role management and text command parsing

### Functional Testing

To test with a real Discord server (private test recommended):

1. Start the bot: `uv sync`, `cp .env.example .env`, add your token to `.env`, then `python main.py`
2. Run `/warn @user test` — verify a case is created and visible with `/case`
3. Test automod: enable with `/automod enable`, trigger a spam burst, verify the delete/warn/timeout action
4. Test guild isolation: run `/case 1` from a different server — should return "Case not found."
5. Verify persistence: restart the bot and confirm `/case` still shows the record
6. Test permissions: have a non-moderator try `/warn` — should be denied

## Legal

- [Terms of Service](https://riyxoen.github.io/eclipse-bot/terms.html)
- [Privacy Policy](https://riyxoen.github.io/eclipse-bot/privacy.html)

## Architecture

Eclipse follows a layered architecture:

```
Commands (cogs) → Permission checks → Moderation service → Case service → SQLite repository
Message events → Automod engine (normalize → exempt → detect → decide → enforce)
/config commands → Admin permission → Config service → SQLite → Engine
```

Moderation logic lives in the service layer. Commands validate arguments, delegate, and format responses. The automated moderation engine sits on top of the same services and never touches SQLite or case records directly. Per-guild configuration flows through a dedicated configuration service with caching and audit logging.

### Key Modules

| Module | Purpose |
| --- | --- |
| `bot/core/` | Client, intents, error handling, startup, shutdown |
| `bot/configuration/` | Environment loading, settings, per-guild config |
| `bot/services/` | Moderation, cases, notifications, config, custom roles, confirmations |
| `bot/automod/` | Detection engine, detectors, exemptions, normalization, cooldowns |
| `bot/database/` | SQLite repository, config repository, schema migrations |
| `bot/permissions/` | Centralized permission and hierarchy checking |
| `bot/cogs/` | Slash command groups (general, moderation, cases, config, automod) |
| `bot/prefix/` | Text command dispatcher and handlers |
| `bot/tests/` | Full test suite (no real Discord) |

## Manual Testing

### Basic Moderation

1. Start the bot and run `/warn @user spamming` — verify the case summary
2. Run `/case <id>` to inspect the record; run `/cases @user` for a list
3. Restart the bot — confirm cases persist from `data/cases.db`
4. Run `/case 1` from a different server — verify guild isolation

### Automated Moderation

With `RIYXOEN_AUTOMOD_ENABLED=1`:

1. Send normal messages — no detection (baseline)
2. Send 5+ rapid messages — verify spam detection and case creation
3. Send the same message 4× within 30s — verify duplicate detection
4. Mention 11+ users — verify mention detection
5. Configure a blocked term and trigger it — verify word filter
6. Verify the cooldown prevents repeated enforcement for the same user

### Server Administration

1. Run `/config view` to see current settings (admin only)
2. Change a setting (e.g. `/config moderation spam threshold:7`) — verify it applies immediately
3. Add a moderator role with `/config roles moderator-add` — verify the role can now moderate
4. Test `/config` as a non-admin — verify denial

### Custom Roles & Prefix Commands

1. Run `.el enable` as an admin — verify the managed role is created
2. Run `.el rename` and `.el color` — verify the role updates
3. Test `.ban`, `.kick`, `.mute`, `.unmute` with required reasons
4. Test `.help` with different permission levels (admin, moderator, regular member)
5. Change prefix with `/config prefix !` — verify `!warn` works and `.warn` does not

## Cost

Development costs $0. There are no paid APIs, hosted databases, cloud services, telemetry, or external AI services. The only network endpoint is Discord itself; all persistence is local SQLite.

## Future Roadmap

- Stronger-evidence automation (ban/kick with multiple corroborating signals)
- Case notes and appeals/resolutions
- Optional web dashboard and API server

*(All require explicit approval; nothing here is built yet.)*

For support please contact me via discord:riyxo
Or create an issue!
