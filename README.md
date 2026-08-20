# Riyxoen Moderation Bot

A production-quality Discord moderation bot built with [discord.py](https://pypi.org/project/discord.py/).

**Current status: Phase 10 (moderation polish).** The bot supports
`/ping`, `/help`, a consistent top-level moderation command set — `/warn`,
`/timeout`, `/kick`, `/ban`, `/unban`, `/purge`, `/slowmode`, `/lock`,
`/unlock` — case-management commands (`/case`, `/cases`,
`/moderation-history`), an opt-in **automated moderation engine** with seven
configurable detectors (spam, duplicates, mentions, links, invites, word
filter, raid protection), a **per-guild administration system** (`/config`
and `/automod`), and a **custom-role system** plus **quick-moderation text
commands** (`.el enable/rename/color`, `.warn`, `.warnings`,
`.clearwarnings`, `.modhistory`, `.ban`, `.kick`, `.mute`, `.unmute`,
`.purge`, `.slowmode`, `.lockdown`/`.unlockdown`, and `.help`). The prefix
is **per-guild configurable** (`/config prefix`).
Every moderation attempt — manual *or* automated — gets a permanent,
guild-isolated case ID stored in a local SQLite database that initializes
automatically at startup. Warnings are first-class: `.warn` records them,
`.warnings` lists them, and `.clearwarnings` marks them as cleared while
keeping history (cleared warnings stop counting toward escalation).
Dangerous slash actions (kick, ban, large purge, lock) require an ephemeral
button confirmation; raid protection is conservative by default (log-channel
**alert**, never an automatic punishment). Dashboards, web servers, AI
moderation, and hosted databases remain out of scope.

## Requirements

- Python 3.11+ (developed and tested on 3.14)
- A Discord application with a bot account: https://discord.com/developers/applications
- The **Members** privileged intent enabled in the developer portal
  (*Bot → Privileged Gateway Intents → Server Members Intent*). It is
  required for member caching, role-hierarchy checks, and target validation.
- The **Message Content** intent is **off by default** (manual moderation does
  not read message contents). Enable it in the developer portal if you set
  `RIYXOEN_ENABLE_MESSAGE_CONTENT_INTENT=1` **or** `RIYXOEN_AUTOMOD_ENABLED=1`
  — the automated moderation detectors require message text, so enabling
  automod implies the intent. If the intent is enabled in config but not in
  the portal, the gateway rejects the connection at startup.

## Setup

### 1. Create a virtual environment

With [uv](https://docs.astral.sh/uv/) (fast, recommended):

```bash
uv sync          # creates .venv and installs all dependencies (incl. dev)
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
# then edit .env and set DISCORD_TOKEN=<your bot token>
```

The bot token **must** come from an environment variable (`DISCORD_TOKEN`),
read from your shell or a local `.env` file. `.env` is git-ignored;
`.env.example` ships placeholders only. **Never commit your token** and never
paste it into chat — put it directly into `.env` yourself.

### 3. Run

```bash
python main.py --check     # validate configuration + initialize the database, offline
python main.py             # start the bot (also: python -m bot)
```

Stop with `Ctrl+C` (SIGINT) or `SIGTERM` — shutdown is graceful with a bounded
timeout.

## Commands

Slash commands (the primary interface):

| Command | Description |
| --- | --- |
| `/ping` | Reply `Pong` with gateway latency |
| `/help` | Show available commands |
| `/health ping` | Health check |
| `/warn <member> [reason]` | Warn a member (records a case, optionally DMs them) |
| `/timeout <member> <duration> [reason]` | Time a member out (`10s`, `30m`, `2h`, `3d`; max 28 days) |
| `/kick <member> [reason]` | Kick a member (confirmation) |
| `/ban <member> [reason]` | Ban a member (confirmation) |
| `/unban <user> [reason]` | Unban a user — pick from autocompleted banned users |
| `/purge <amount> [reason]` | Bulk-delete messages in the current channel (large purges require confirmation) |
| `/slowmode <duration> [channel] [reason]` | Set a channel's slowmode delay (`0` clears; max 6 hours) |
| `/lock [channel] [reason]` | Lock a channel by denying `@everyone` send (confirmation, reversible) |
| `/unlock [channel] [reason]` | Unlock a channel, restoring its exact pre-lock permission state |
| `/case <case_id>` | Show the full record for one case |
| `/cases <member> [page]` | Show a member's recent cases (10 per page) |
| `/moderation-history <member> [page]` | Alias of `/cases` |
| `/config view [page]` | Show this server's configuration (admin only) |
| `/config prefix <prefix>` | Set the per-guild text-command prefix, e.g. `!` (1–3 non-space characters; admin only) |
| `/config moderation …` | Configure spam/duplicate/mention/link/word settings, escalation, cooldown, timeout, purge max, DM notifications, per-guild automod (admin only) |
| `/config logs [channel] [enabled]` | Set the log channel and toggle moderation logs (admin only) |
| `/config roles …` | Add/remove moderator and administrator roles (admin only) |
| `/config exemptions …` | Add/remove exempt users, roles, and channels (admin only) |
| `/config reset [confirm]` | Restore documented defaults (cases untouched; requires confirmation) |
| `/automod enable` / `/automod disable` | Turn per-guild automated moderation on/off (admin only) |
| `/automod status` | Summarize every detector's state (admin only) |
| `/automod invites action <allow\|delete\|warn\|timeout>` | Set invite filtering (admin only) |
| `/automod invites allowed add\|remove\|list <code>` | Manage allowlisted invite codes (admin only) |
| `/automod raid configure <threshold> <window> <alert\|timeout>` | Configure raid protection (admin only) |

Reasons are **optional** in the slash commands and default to a documented
`No reason provided` (the stored case always carries the final reason); the
prefix `.warn`/`.ban`/`.kick`/`.mute`/`.unmute` commands **require** a
reason, while `.purge`/`.slowmode`/`.lockdown`/`.unlockdown` accept an
optional one (defaulting to `No reason provided`). All reasons are capped at
300 characters. Purge amounts are capped by the per-guild maximum
(`RIYXOEN_MAX_PURGE_AMOUNT` default 100, adjustable via `/config moderation`);
`TextChannel.purge` respects Discord's 14-day bulk-delete limit internally,
and the result is reported accurately when some messages could not be
deleted. Confirmation prompts are ephemeral, expire after 30 seconds, are
bound to the moderator who started them, and are consumed atomically — a
double click (or an expired/cancelled confirmation) can never execute the
action twice.

Every moderation action produces a **case record**:

```
Case #42
Action: Timeout
Target: @user
Moderator: @moderator
Reason: spam
Status: Success
```

Case-management commands are **moderator-only**: they reuse the same
centralized permission system as the moderation commands (required Discord
permission or a configured moderator role). A regular member who runs
`/case` gets the standard safe denial message.

The prefix moderation commands (`.ban`, `.kick`, `.mute`, `.unmute`) reuse the
exact same server-side pipeline as the slash commands — same permission,
bot-permission, hierarchy, target, and state checks; same case system; same
DM notifications; same log-channel embeds. A `timeout` case created via
`.mute` carries `muted: true` in its metadata and its `expires_at` is stored
in UTC, so mute state is auditable and survives restarts even though Discord
itself lifts the timeout automatically.

`/config` commands are **administrator-only**: the server owner, members with
the Discord **Administrator** permission, or members holding a configured
administrator role (Phase 5). A regular moderator never receives sensitive
configuration automatically.

### Prefix (text) commands — custom roles & quick moderation

Text commands use a configurable prefix (`RIYXOEN_COMMAND_PREFIX`, default
`.`), which administrators can **override per guild** with `/config prefix`
(validated: 1–3 non-space characters, no mention/channel/slash syntax). They
require the message-content intent (on by default; enable it in the Discord
developer portal). Handlers stay thin — all permission, hierarchy, and state
checks live in the services, and every action reuses the existing
case/audit/log pipeline. A **local, in-memory rate limiter** (10 commands
per 5 seconds per user, bounded to 10 000 tracked users) stops command spam
before it reaches Discord — no external rate-limiting service. Unknown
commands get a safe `Command unavailable.` reply instead of being silently
ignored.

| Command | Description |
| --- | --- |
| `.el enable` | Enable the custom-role system for this server (requires Manage Roles / Administrator). Creates the bot-managed role; idempotent — a second call reports it is already enabled. Recreates the role if it was deleted |
| `.el rename <name>` | Rename the managed role (disabled system → clear message to run `.el enable` first) |
| `.el color <hex>` | Set the managed role's color, e.g. `.el color #5865f2` (normalizes case; rejects malformed hex) |
| `.warn @user <reason>` | Warn a member — moderator permission + role hierarchy + required reason; records a case and DMs the member when possible |
| `.warnings @user` | Show a member's active warning count and recent warning cases (moderator-only) |
| `.clearwarnings @user` | Mark a member's active warnings as cleared (history kept; cleared warnings no longer count toward escalation) |
| `.modhistory @user [page]` | Show a member's recent moderation cases (10 per page; moderator-only) |
| `.purge <amount>` | Bulk-delete recent messages in this channel (capped by the per-guild maximum; accurate result reporting) |
| `.slowmode <seconds>` | Set this channel's slowmode (`0` clears; max 6 hours) |
| `.lockdown` / `.unlockdown` | Lock / unlock the **current channel** (reversible, preserves existing permission configuration, idempotent) |
| `.ban @user <reason>` | Ban a member — moderator permission + bot permission + role hierarchy + required reason, DM attempt before the action |
| `.kick @user <reason>` | Kick a member — same safety model as `.ban` |
| `.mute @user <duration> <reason>` | Mute via Discord timeout (`10m`, `1h`, `2h`, `1d`; max 28 days). Expiry is tracked in the case and Discord lifts it automatically |
| `.unmute @user <reason>` | Remove a timeout; refuses cleanly when the user isn't muted (restart-safe: live timeout state is the source of truth) |
| `.help` | Organized command help by category (Moderation / Custom Roles / Configuration / Utility); restricted sections are hidden for users without the required permissions |

If the DM to the punished user cannot be delivered, the action still
succeeds and the moderator sees a private note (`_Note: the user could not
be DM'd..._`) — a failed DM never fails the action.

## Automated moderation (Phase 8 — invites & raid)

Two detectors were added to the Phase 4 engine:

- **Invites** — detects Discord invite links (`discord.gg/code`,
  `discord.com/invite/code`, `discordapp.com/invite/code`) as a *dedicated*
  detector (a server may allow arbitrary URLs while blocking invites).
  Configurable action (`allow`/`delete`/`warn`/`timeout`, default `allow`)
  plus a per-guild **allowed-codes** allowlist so owners whitelist their own
  invite links. Domain matching is boundary-safe (`notdiscord.com` and
  `discord.gg.evil.com` never match).
- **Raid protection** — detects unusual join bursts (`N` joins within `N`
  seconds) using join timestamps only. The default action is `alert` (a
  notice to the configured log channel) — **never an automatic punishment**
  on weak evidence. `timeout` is opt-in and only touches members who joined
  within the window; ban/kick are never raid actions. Join history is bounded
  (max 500 guilds × 200 timestamps) and pruned periodically.

Both are per-guild configurable via `/automod` and `/config view`; raid uses
an enforcement cooldown per guild so a sustained burst alerts once, not
repeatedly.

### Privileged-data report (required by Phase 8)

- **Message Content intent** — the spam/duplicate/mentions/link/invite/word
  detectors analyze message text, so they require the privileged
  message-content intent. It is **on by default** (prefix commands need it
  too) and must be enabled in the Discord developer portal. Code that uses
  it: `bot/automod/*` detectors and `bot/prefix/*` text commands. Operators
  who cannot enable it should keep `RIYXOEN_AUTOMOD_ENABLED=0` (the master
  switch — when off, no message content is read).
- **Raid protection does NOT require message content** — it consumes
  member-join events (`guilds` + `members` intents, both already enabled for
  moderation) and records only join timestamps.
- No other privileged intents (presences, message reactions, voice states)
  are used; nothing is sent to any external service.

## Case model and database

Each case stores:

| Field | Type | Notes |
| --- | --- | --- |
| `case_id` | integer | Unique, auto-incremented, assigned by the database |
| `guild_id` | integer | Owning guild (every query is guild-scoped) |
| `target_user_id` | integer | Discord user ID — never a username |
| `moderator_user_id` | integer | Discord user ID (the bot itself for automated actions) |
| `action` | text | `warn`/`timeout`/`unmute`/`kick`/`ban`/`unban`/`purge`/`delete`/`slowmode`/`lock`/`unlock` |
| `reason` | text | Required for punishments |
| `created_at` | text (ISO-8601 UTC) | Always UTC |
| `duration_seconds` | integer | Timeouts only |
| `expires_at` | text (ISO-8601 UTC) | `created_at + duration` for timeouts |
| `status` | text | `success`, `failed`, or `cleared` (warnings cleared via `.clearwarnings` keep their history but stop counting as active) |
| `error` | text | Safe user-facing message for failed cases |
| `automated` | boolean | `1` when created by the automated moderation engine |
| `detector` | text | Detector name for automated cases (`spam`, `duplicate`, ...), `NULL` for manual |
| `metadata` | text (JSON) | Optional structured extras — e.g. the pre-lock permission state for `lock` cases, used by `/unlock` |

Automated cases carry `automated = 1` and the triggering `detector`, and are
shown with a `🤖` marker in `/cases` and a `Source: Automated (spam)` line in
`/case`.

Storage details:

- **SQLite** (standard library `sqlite3`), file at `RIYXOEN_DATABASE_PATH`
  (default `data/cases.db`), git-ignored.
- **Migrations**: schema is versioned with `PRAGMA user_version`; pending
  migrations run automatically when the repository opens, which happens at
  startup (`--check` also verifies the database opens and initializes).
- **Repository abstraction**: `CaseRepository` (SQLite + in-memory fallback)
  lives in `bot/database/`; the case service and commands never see SQL.
  Every query is parameterized — no user input is ever interpolated into SQL.
- **Guild isolation**: every read/update filters by `guild_id`. A case from
  guild A requested from guild B is indistinguishable from a missing case
  (`Case not found.`), so the bot never leaks that a case exists elsewhere.
- **UTC**: timestamps are created as UTC-aware datetimes and stored as
  ISO-8601 UTC text; `tzinfo` survives the round trip.

## Persistence-failure contract (documented decision)

Order of operations for a moderation action:

```
1. moderation action occurs (Discord mutation)
2. case persistence occurs (SQLite write)
3. response is sent
```

- **Persistence fails after the action succeeded** → the action is **not**
  rolled back (Discord has no rollback for kick/ban/timeout). The bot logs
  full (secret-free) diagnostics and tells the user the truth: the action was
  completed but the case record could not be saved. It never claims the
  action failed, and it never pretends the case was stored.
- **Persistence fails for a failed action** → the original safe error still
  reaches the user; the persistence failure is logged.
- **Exactly one case per attempt** — a retry is a separate attempt with its
  own case, never a duplicate of the original.
- **Database unavailable at startup** → the bot logs the error and falls back
  to an in-memory repository so it stays functional (cases then do not
  survive a restart; `--check` will report the database problem up front).
- SQL errors are never shown to Discord users; they appear only in local
  logs.

## Automated moderation (Phase 4)

The automated moderation engine is **opt-in** (`RIYXOEN_AUTOMOD_ENABLED=1`)
and **off by default**. When enabled, every guild message flows through:

```
Discord event -> normalization -> exemption checks -> detectors -> decision -> enforcement -> case service -> audit log
```

### Detectors

| Detector | What it detects | Default |
| --- | --- | --- |
| `spam` | `N` messages from one user within `N` seconds | 5 msgs / 5 s → delete |
| `duplicate` | `N` identical (normalized) messages within `N` seconds | 4 msgs / 30 s → delete |
| `mentions` | Excessive user/role/total/@everyone mentions | 10/6/15, everyone off → delete |
| `links` | URLs not on the allowlist | `allow` (no enforcement) |
| `word_filter` | Configured blocked terms in normalized text | empty terms → off |

Detectors are pure: they **never** touch Discord — they only report a
finding. The engine maps the finding to the configured action
(`delete`/`warn`/`timeout`) and executes it through the existing moderation
service, which creates one automated case, audit-logs it, and posts to the
log channel. **Automated actions are limited to delete, warn, and timeout** —
ban and kick are never automated in this phase.

### Text normalization (predictable, first iteration)

- Unicode **NFKC** normalization (full-width/homoglyph folding) + case folding.
- Punctuation/symbols/emoji are dropped; **whitespace is preserved**, so
  `f.u.c.k` normalizes to `fuck` while `you are an ass` keeps its word
  boundaries — whole-word matching never matches inside unrelated words
  (`ass` does not match `assessment`) unless substring mode is enabled.
- Domain allowlisting is exact-or-proper-subdomain: `github.com` allows
  `api.github.com` but **never** `evilgithub.com` or `github.com.evil.com`.
- Blocked terms are never hardcoded in source — they come from
  `RIYXOEN_AUTOMOD_BLOCKED_TERMS`.

### Exemptions (checked before any detector runs)

Bots, the server owner, moderators (Discord permission **or** configured
moderator role), and configured exempt users/roles/channels are never
analyzed. Exemption logic lives in one place (`bot/automod/exemptions.py`)
and reuses the shared permission checker — detectors contain no exemption
duplication.

### Escalation and warning expiration

Configure `RIYXOEN_AUTOMOD_ESCALATION=3:3600,5:43200` to convert warnings
into timeouts: once a member's **active warning count** (including the new
warning) reaches a threshold, the action becomes a timeout of the configured
duration instead of a warn. Active warnings are successful `warn` cases in
the guild created within `RIYXOEN_AUTOMOD_WARNING_WINDOW_SECONDS` (default 7
days); a warning stops counting once older than the window. `0` means
warnings never expire. Escalation uses the existing case system — no second
warning store.

### Cooldowns, rate-limit safety, and memory

- **Enforcement cooldown** — after any automated enforcement for a
  `(guild, user)`, further detections for that user are skipped until
  `RIYXOEN_AUTOMOD_ENFORCEMENT_COOLDOWN_SECONDS` (default 60) elapse, so a
  spam burst produces **one case, not dozens**. `0` disables the cooldown.
- **No retry loops** — Discord rejections (rate limits, permission races,
  already-deleted messages) are handled gracefully: logged, recorded as a
  failed case (or skipped for already-deleted messages), never retried
  aggressively. The engine never raises into the event loop.
- **Bounded state** — the spam/duplicate detectors track at most 5,000
  `(guild, user)` keys each, with a fixed per-user history cap and stale
  entries pruned on access plus a periodic sweep; the cooldown tracker holds
  at most 10,000 keys. Total memory is bounded regardless of message volume
  or user count.

## Server administration (Phase 5)

Per-guild configuration is managed at runtime through the `/config` command
group instead of manual source-code or `.env` changes:

```
Discord command -> admin permission layer -> configuration service -> local SQLite -> moderation engine
```

- **Per-guild, isolated** — every guild has an independent configuration
  row in the same local SQLite database (schema v3, migrated automatically).
  Guild A can never read or modify guild B's configuration.
- **Seeded defaults** — a guild's configuration is seeded from the
  environment settings below, so existing deployments keep their behavior;
  administrators then override per guild. The environment stays the
  operator-level bootstrap; `/config` is the per-server override.
- **Live consumption** — the moderation engine, the moderation service, and
  the permission checker all read per-guild configuration through the
  configuration service (never storage directly). A `/config` change
  invalidates the engine's cached per-guild detector sets, so new thresholds
  apply to the next message — no restart needed.
- **Bounded cache** — guild snapshots are cached up to 256 guilds and the
  engine caches at most 32 per-guild detector sets (LRU eviction); stale
  configuration is invalidated on every change.
- **Audit trail** — every change emits a structured audit log line
  (`config change: guild=… actor=… setting=… old=… new=…`) and, when mod
  logs are enabled, a `Configuration change` embed to the guild's log
  channel. Values are configuration, never secrets or message contents.
- **Failure handling** — a config read failure degrades to the seeded
  defaults with a loud log (the bot keeps moderating); a config write
  failure raises a safe message (`…could not be saved to the local
database…`) and never exposes SQL details. Deleted roles/channels render as
`deleted role (id)` in `/config view` and are handled safely everywhere.

`/config reset` requires an explicit `confirm: true` on a second invocation
and restores the documented defaults — **moderation cases are never
deleted**.

## Configuration

All settings come from environment variables (or `.env`). Secrets and local
runtime configuration only — no hardcoded guild/user IDs. These values seed
every guild's per-guild configuration; servers override them at runtime via
`/config`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | *(required)* | Bot token; must never be committed |
| `RIYXOEN_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` |
| `RIYXOEN_LOG_FILE` | *(console only)* | Optional log file path |
| `RIYXOEN_SHUTDOWN_TIMEOUT_SECONDS` | `10` | Graceful-shutdown timeout |
| `RIYXOEN_ENABLE_MESSAGE_CONTENT_INTENT` | `1` | Privileged message-content intent — **required** by the prefix commands and automod (enable in the developer portal too) |
| `RIYXOEN_COMMAND_PREFIX` | `.` | Prefix for the text commands (`.el`, `.ban`, `.kick`, `.mute`, `.unmute`) |
| `RIYXOEN_MODERATOR_ROLE_IDS` | *(empty)* | Comma-separated role IDs allowed to run all moderation commands |
| `RIYXOEN_LOG_CHANNEL_ID` | *(disabled)* | Discord channel receiving a case summary per action |
| `RIYXOEN_NOTIFY_USERS` | `1` | Set `0` to disable DM notifications to punished users |
| `RIYXOEN_MAX_PURGE_AMOUNT` | `100` | Max messages per purge (1–1000) |
| `RIYXOEN_DATABASE_PATH` | `data/cases.db` | Local SQLite case database (git-ignored) |
| `RIYXOEN_AUTOMOD_ENABLED` | `0` | Master switch for the automated moderation engine |
| `RIYXOEN_AUTOMOD_SPAM_THRESHOLD` / `..._SPAM_WINDOW_SECONDS` / `..._SPAM_ACTION` | `5` / `5` / `delete` | Spam: messages within the window → action |
| `RIYXOEN_AUTOMOD_DUPLICATE_THRESHOLD` / `..._DUPLICATE_WINDOW_SECONDS` / `..._DUPLICATE_ACTION` | `4` / `30` / `delete` | Duplicate: identical messages within the window → action |
| `RIYXOEN_AUTOMOD_MENTION_USER_THRESHOLD` / `..._MENTION_ROLE_THRESHOLD` / `..._MENTION_TOTAL_THRESHOLD` / `..._MENTION_EVERYONE_THRESHOLD` / `..._MENTION_ACTION` | `10` / `6` / `15` / `0` / `delete` | Mention dimensions (`0` disables a dimension; @everyone is off by default) |
| `RIYXOEN_AUTOMOD_TIMEOUT_DURATION_SECONDS` | `3600` | Default timeout length when a detector's action is `timeout` |
| `RIYXOEN_AUTOMOD_LINK_ACTION` / `..._ALLOWED_DOMAINS` | `allow` / *(empty)* | URL behavior; comma-separated allowlist (exact-or-subdomain match) |
| `RIYXOEN_AUTOMOD_BLOCKED_TERMS` / `..._BLOCKED_TERMS_SUBSTRING` / `..._WORD_FILTER_ACTION` | *(empty)* / `0` / `delete` | Blocked terms (whole-word by default; substring is opt-in) |
| `RIYXOEN_AUTOMOD_EXEMPT_USER_IDS` / `..._EXEMPT_ROLE_IDS` / `..._EXEMPT_CHANNEL_IDS` | *(empty)* | Comma-separated exempt users/roles/channels |
| `RIYXOEN_AUTOMOD_ENFORCEMENT_COOLDOWN_SECONDS` | `60` | Seconds between automated enforcements for the same user |
| `RIYXOEN_AUTOMOD_WARNING_WINDOW_SECONDS` | `604800` | Warning expiration for escalation (`0` = never expire) |
| `RIYXOEN_AUTOMOD_ESCALATION` | *(empty)* | `count:seconds,count:seconds` warn→timeout escalation, e.g. `3:3600,5:43200` |
| `RIYXOEN_AUTOMOD_INVITE_ACTION` / `..._INVITE_ALLOWED_CODES` | `allow` / *(empty)* | Invite filtering; comma-separated allowed invite codes |
| `RIYXOEN_AUTOMOD_RAID_JOIN_THRESHOLD` / `..._RAID_WINDOW_SECONDS` / `..._RAID_ACTION` | `10` / `10` / `alert` | Join-burst detection; `alert` posts a log notice (never auto-punishes) |

## Permission model

Every moderation action is validated **server-side** inside the application —
the bot never relies only on a command's visible Discord permission gate.
For each action the bot verifies:

1. The command was used inside a guild.
2. The bot has the required Discord permission.
3. The moderator has the required permission **or** a configured moderator role.
4. The target exists.
5. The target is not the bot itself or the server owner.
6. The moderator's highest role is higher than the target's highest role.
7. The bot's highest role is higher than the target's highest role.
8. The action is valid for the target's current state.

Required permissions: warn/timeout/view-cases → `Moderate Members`; kick →
`Kick Members`; ban/unban → `Ban Members`; purge → `Manage Messages` (+
`Read Message History` on the channel for the bot); slowmode/lock/unlock →
`Manage Channels`. Viewing moderation history uses the same centralized
checker — no duplicated permission logic. Every action is re-validated
server-side inside the application, including the bot's own permissions and
role hierarchy — never only the command's visible Discord permission gate.

## Architecture

```
main.py                        thin entrypoint (python main.py)
bot/
  cli.py                       argument parsing, --check, --log-level
  core/
    bot.py                     RiyxoenBot (discord.Client) + command tree + run loop
    intents.py                 Gateway intent definitions with rationale
    errors.py                  domain exception hierarchy (BotError, ConfigError)
    error_handler.py           centralized, sanitized app-command error handling
    shutdown.py                ShutdownManager: signals, cancellation, bounded close
    startup.py                 pre-flight diagnostics (--check, incl. DB open)
  configuration/
    env.py                     environment access + .env loading
    settings.py                Settings dataclass (stdlib only)
    loader.py                  env -> Settings, fail-fast validation
    automod.py                 AutomodSettings + env parsing (Phase 4 defaults)
    guild.py                   GuildConfig per-guild model + validation (Phase 5)
    errors.py                  GuildConfigError (safe user messages)
    display.py                 /config view formatting + pagination
  logging/
    configure.py               structured logging (console + optional file)
    redaction.py               secret redaction (filter + formatter)
  database/
    migrations.py              versioned schema (PRAGMA user_version, v1..v3)
    repository.py              CaseRepository (SQLite + in-memory fallback)
    config_repository.py       GuildConfigRepository (SQLite + in-memory fallback)
  permissions/
    checks.py                  PermissionChecker: guild/moderator/admin/bot/target/hierarchy/state
  automod/
    engine.py                  AutomodEngine: pipeline + enforcement decision
    detectors.py               spam/duplicate/mentions/links/invites/raid/word_filter detectors
    exemptions.py              centralized exemption checks (pre-detector)
    normalize.py               text + domain normalization (NFKC, allowlist)
    state.py                   bounded per-user history (memory safety)
    cooldowns.py               bounded enforcement cooldown tracker
  services/
    moderation.py              ModerationService: warn/timeout/kick/ban/unban/purge/delete/slowmode/lock/unlock
    cases.py                   CaseService: create/get/list/update + guild isolation
    notifications.py           best-effort DM notifications (never fail the action)
    guild_config.py            GuildConfigService: load/update/reset + cache + audit
    confirmation.py            ConfirmationController: expiry, ownership, atomic double-click protection
    custom_roles.py            CustomRoleService: .el enable/rename/color, role creation, hierarchy safety
  moderation/
    errors.py                  moderation exceptions with safe user messages
    actions.py                 action names + required permissions
    validation.py              reasons, durations, purge amounts (pure functions)
    cases.py                   CaseRecord domain model (status, expires_at, UTC)
    response.py                user-facing case summaries/detail/lists
  cogs/
    general.py                 /ping, /help
    health.py                  /health ping
    moderation.py              top-level /warn /timeout /kick /ban /unban /purge /slowmode /lock /unlock (thin command layer)
    cases.py                   /case, /cases, /moderation-history
    config.py                  /config group (view/moderation/logs/roles/exemptions/reset)
    automod.py                 /automod group (enable/disable/status/invites/raid)
  prefix/
    __init__.py                prefix dispatcher + .el/.warn/.warnings/.clearwarnings/.modhistory/.ban/.kick/.mute/.unmute/.purge/.slowmode/.lockdown/.unlockdown/.help handlers (thin)
  tests/                       pytest suite (fakes, no real Discord)
```

Command flow:

```
commands (cogs) -> permission checks -> moderation service -> case service -> SQLite repository
Discord message event -> automod engine (normalize -> exempt -> detect -> decide -> enforce)
/config commands -> admin permission layer -> config service -> SQLite -> moderation engine
```

Moderation logic lives in the service layer; cogs only validate arguments,
delegate, and format responses. The moderation service depends on the case
service, which depends on the repository abstraction — a future database
backend can be swapped in without touching commands. The automated moderation
engine sits on top of the same services: it never touches SQLite or case
records directly, and detection is fully separated from enforcement. Per-guild
configuration flows the same way: commands and the engine depend on the
configuration service, which depends on the config repository — no SQL and no
config logic live in cogs or detectors.

## Testing and static checks

```bash
python -m pytest           # run the test suite (626 tests; no real Discord)
python -m compileall .     # byte-compile all sources
ruff check .               # lint
ruff format --check .      # verify formatting
```

The unit tests use lightweight fakes for Discord objects — they never contact
a real server. Coverage includes database initialization and schema
creation, migrations (including upgrading a Phase 3 v1 database in place),
case creation/retrieval/listing/updates, duplicate-case prevention, strict
guild isolation, missing and invalid case IDs, multiple
guilds/moderators/targets, pagination, UTC timestamps, failed moderation
actions, persistence failures after success, and the command layer
(`/case`, `/cases`, `/moderation-history`) with its permission gate. Phase 4
adds detector tests (spam/duplicate/mentions/links/word-filter, normalization,
domain-allowlist security cases, bounded memory), exemption tests
(users/roles/channels/moderator/owner/bot), engine pipeline tests
(detection → enforcement → automated case, cooldowns, escalation, deleted-
message handling, missing permissions, failed enforcement), and configuration
parsing/validation tests. Phase 5 adds per-guild configuration tests: the
model (default seeding, JSON round-trip, value bounds/upper limits), the
config repository (SQLite persistence, migration v3, guild isolation), the
config service (creation, updates, reset, bounded cache + invalidation,
database-failure handling, audit logging), the `/config` command layer
(owner/administrator-permission/administrator-role gates, moderator denial,
channel verification, role/exemption management, reset confirmation), and
engine integration (per-guild automod toggle, config changes applying without
restart, per-guild exemptions, detector-cache bounds). Phase 6 adds the
professional-UX suite: the confirmation controller (expiry, ownership,
cancellation, atomic double-click protection, bounded pending set), the
confirmation-gated flows (kick/ban/large purge/lock), immediate actions
(warn/timeout/slowmode/unlock/small purge), unban autocomplete, optional-
reason defaults, purge accuracy reporting, reversible lock/unlock with exact
state restoration, duration validation (timeout 28-day cap, slowmode 6-hour
cap), and the permission/hierarchy denial paths. Detectors are testedwithout Discord, enforcement separately, and the repository/service/commands
each in isolation. Phase 6 (custom roles + prefix commands) adds: prefix
parsing (command/subcommand/arguments, mention extraction, custom prefixes),
the custom-role service (enable/idempotency, role creation without
duplicates, deleted-role recovery, rename/color with validation, Manage
Roles + bot-permission + hierarchy failures, Discord error mapping), hex
color and role-name validation, the `.el` command behaviors (disabled-state
messages, malformed input), the `.ban`/`.kick` flows (required reason,
permission and hierarchy denials, safe Discord failures, DM-failure
reporting without failing the action), the `.mute`/`.unmute` flows (duration
parsing, expiry tracking, already-unmuted handling, restart-safe state via
live timeout), and repository/service `update_metadata` round-trips.
Phase 8 adds the invite and raid detectors (boundary-safe invite matching,
allowlist codes, join-burst windows, per-guild isolation, bounded join
history), the raid engine flow (alert to the log channel without punishing,
cooldown deduplication, opt-in timeout with default duration, master-switch
gating, bot joins ignored), and the `/automod` command group (owner /
administrator gate, enable/disable/status, invites action + allowed-codes
management, raid configuration with invalid-value rejection). Detectors are
tested without Discord, enforcement separately, and the
repository/service/commands each in isolation.
Phase 10 (polish) adds: the `.warn`/`.warnings`/`.clearwarnings` flows
(required reason, moderator gates, `cleared` status round-trip, escalation
count drop, no-phantom-cases), `.modhistory` (list + empty + denied),
`.purge` (accurate counts, partial-deletion reporting, invalid amounts),
`.slowmode` (set/clear/invalid), `.lockdown`/`.unlockdown` (lock state,
idempotent repeats without duplicate cases, restore, denied), the
categorized `.help` (permission-filtered sections), the per-guild command
prefix (validation at the model level, `/config prefix` admin gate,
dispatcher honoring the override), the `cleared` case status
(model/response), and the bot presence refresh.

## Security model

- **Token handling** — the token comes only from `DISCORD_TOKEN`; it is never
  hardcoded, printed, or logged. `.env` is git-ignored; `.env.example`
  contains placeholders only. The case database stores case metadata only —
  never tokens, credentials, environment variables, or authorization headers.
- **Log redaction** — every log record passes through a redaction filter *and*
  a redacting formatter (defense in depth): token-shaped strings, secret
  assignments (`Authorization: ...`, `token=...`), and configured secret
  values are scrubbed, including from tracebacks. Audit logs never contain
  message contents or reasons.
- **Fail-fast configuration** — invalid configuration aborts startup with
  every problem reported at once.
- **Centralized error handling** — users only ever see sanitized messages;
  full (redacted) detail goes to local logs. SQL errors never reach Discord.
- **Least-privilege intents** — `guilds` + `members` always; `message_content`
  is **on by default since Phase 6** because the prefix commands and the
  automated-moderation detectors genuinely require message text (documented
  in `bot/core/intents.py`); it can be disabled with
  `RIYXOEN_ENABLE_MESSAGE_CONTENT_INTENT=0`.
- **Automated moderation safety** — the engine is off by default; exempt
  authors/channels are skipped before any detector runs; only delete/warn/
  timeout are automated (never ban/kick); an enforcement cooldown prevents
  repeated punishment for one burst; detector state and cooldowns are
  bounded; message contents are never logged (audit lines carry IDs only).
- **Guild-isolated moderation data** — every case query is scoped to the
  requesting guild; moderator-only commands protect history from regular
  members; parameterized SQL prevents injection. Per-guild configuration is
  isolated the same way (each guild owns exactly one row; no cross-guild
  path).
- **Administrator-only configuration** — `/config` is gated server-side by
  owner / Discord `administrator` permission / configured administrator
  role; a regular moderator never inherits it. The gate is the same
  centralized checker the moderation commands use — no duplicated logic.
- **Config audit + safe errors** — every configuration change produces a
  structured audit line and an optional log-channel embed; invalid values
  are rejected with safe messages and sensible upper bounds (no unbounded
  lists, no resource abuse); SQL/database errors never reach Discord users.
- **No secrets in git** — verified via `.gitignore` (`data/`, `*.db`,
  `logs/`, `.env`) and a pre-commit habit of scanning for token-shaped
  strings.

## Manual testing (Phase 3)

1. **Start the bot**: `uv sync`, `cp .env.example .env`, put your token in
   `.env` yourself, then `python main.py`. You should see `bot connected`,
   `Bot connected as <name>`, `Guild count: N`.
2. **Run a moderation command**: in your server, `/warn @user spamming`.
   The bot replies with a case summary — note the **Case #** number (e.g. `#1`).
3. **Find the case ID**: it's in the response to step 2 (also in the local
   log's audit line: `case=1 action=warn ...`).
4. **Run `/case`**: `/case 1` shows the full record (action, target,
   moderator, reason, created time, status). A timeout case also shows
   duration and expiration.
5. **Run `/cases`**: `/cases @user` lists that member's cases (10 per page;
   append `page 2` for more). `/moderation-history @user` is the same list.
6. **Restart the bot**: `Ctrl+C`, then `python main.py` again.
7. **Verify persistence**: `/case 1` still shows the case — it was read from
   `data/cases.db`, not memory.
8. **Verify guild isolation**: from a *different* server, `/case 1` replies
   `Case not found.` even though the case exists in the first server. Also
   confirm a regular (non-moderator) member gets a permission denial when
   running `/case` or `/cases`.

## Manual testing (Phase 4 — automated moderation)

With automod enabled (`RIYXOEN_AUTOMOD_ENABLED=1` in `.env`, and the Message
Content intent enabled in the developer portal):

1. **Normal message**: send `hello everyone` in a normal channel → nothing
   happens, no case is created (`/cases @you` stays empty).
2. **Spam burst**: paste 5+ distinct short messages within 5 seconds → the
   bot deletes them (default action) and `/case <id>` shows a `delete` case
   with `Source: Automated (spam)`.
3. **Duplicate messages**: send the same message 4 times within 30 seconds →
   `Automated (duplicate)` case.
4. **Excessive mentions**: mention 11+ users in one message →
   `Automated (mentions)` case (defaults: 10 users, 6 roles, 15 total).
5. **Blocked term**: with `RIYXOEN_AUTOMOD_BLOCKED_TERMS=spammy` set, send
   `that is spammy!!!` → `Automated (word_filter)` case.
6. **Allowed URL**: with `RIYXOEN_AUTOMOD_ALLOWED_DOMAINS=youtube.com` and
   `RIYXOEN_AUTOMOD_LINK_ACTION=delete`, send a `youtube.com` link → nothing.
7. **Blocked URL**: send a link to an unlisted domain → `Automated (links)`
   case.
8. **Moderator exemption**: a moderator (permission or configured role) can
   spam freely — no detection.
9. **Exempt channel**: messages in an exempt channel are never analyzed.
10. **Automated warning**: set `RIYXOEN_AUTOMOD_SPAM_ACTION=warn` and trigger
    a burst → the target gets a DM and a `warn` case.
11. **Automated timeout**: set `..._SPAM_ACTION=timeout` (or configure
    `RIYXOEN_AUTOMOD_ESCALATION`) and trigger → a `timeout` case with a
    duration; the member is timed out on Discord.
12. **Case creation**: every automated action created exactly one case with
    `automated` set; verify with `/case` (shows `Source: Automated (...)`)
    and `/cases` (shows a `🤖` marker).
13. **Cooldown**: after one enforcement, keep sending → no new cases until
    the cooldown expires (default 60 s).
14. **Bot restart**: restart the bot — automated cases persist in
    `data/cases.db` and remain visible via `/case`.

## Manual testing (Phase 5 — server administration)

`/config` commands require the **server owner**, the **Administrator**
permission, or a configured **administrator role**.

1. **Start the bot** (`python main.py`) and confirm `/config view` works.
2. **Run `/config view`** — shows the current per-guild configuration
   (paginated; defaults seeded from your `.env`).
3. **Change the log channel**: `/config logs channel:#mod-logs` — the bot
   verifies it can post there; if it lacks Send Messages / Embed Links it
   saves the channel anyway and tells you clearly. With automod enabled,
   moderation actions now post embeds there.
4. **Change a moderation setting**: `/config moderation spam threshold:7
   window_seconds:10 action:delete` — `/config view` reflects the new value
   immediately; no restart.
5. **Add a moderator role**: `/config roles moderator-add role:@staff` —
   members with that role can now run all moderation commands.
6. **Add an exempt role**: `/config exemptions role-add role:@trusted` —
   members with that role are never analyzed by automod.
7. **Add an exempt channel**: `/config exemptions channel-add
   channel:#bot-spam` — messages there are never analyzed.
8. **Verify automod uses the new settings**: with
   `RIYXOEN_AUTOMOD_ENABLED=1`, lower `/config moderation spam threshold` to
   `3`, send 3 quick messages, and confirm the bot enforces (delete/warn/
   timeout per the configured action) — the change took effect without a
   restart.
9. **Reset configuration**: `/config reset` (shows the warning), then
   `/config reset confirm:true` — settings return to defaults.
10. **Verify cases remain intact**: `/cases @user` still shows all prior
    moderation history after the reset.
11. **Test with an administrator** — full access to `/config`.
12. **Test with a regular moderator** (e.g. `moderate_members` permission
    only) — `/config` is denied with the safe administrator message.
13. **Test with a regular member** — denied everywhere.
14. **Guild isolation** (if you have two test servers): configure something
    in guild A, then run `/config view` in guild B — B shows its own
    independent settings, never A's.

## Manual testing (Phase 6 — custom roles & prefix commands)

All prefix commands run in any text channel with the `.` prefix. You need the
message-content intent enabled in the developer portal.

1. **`.el enable`** — an admin (Manage Roles / Administrator) runs it: the
   bot replies that the system is enabled and a managed role
   (`Riyxoen Custom`) is created. Run it again → “already enabled”, and no
   duplicate role appears.
2. **`.el rename Cool Role`** — the managed role is renamed.
3. **`.el color #ff0000`** — the role turns red; `.el color #FF0000` also
   works (case-normalized). `.el color banana` → safe “That color isn't
   valid” reply.
4. **Disabled system** — in a fresh server, `.el rename X` / `.el color #fff`
   reply that `.el enable` must be run first; nothing changes.
5. **Missing role** — delete the managed role, then `.el color #00ff00` →
   “managed custom role is missing”; `.el enable` recreates it.
6. **No permission** — a regular member runs `.el enable` → safe denial; the
   system stays off.
7. **`.ban @user spam`** — the member is banned, DMed (if possible), a case
   is created, and the log channel (if configured) gets an embed. Missing
   reason → “A reason is required”; mention of a non-member → “User not
   found.”
8. **`.kick @user reason`** — same flow, kick.
9. **`.mute @user 10m spam`** — the member is timed out for 10 minutes; the
   case shows Duration + Expires. `.mute @user -5m x` → rejected.
10. **`.unmute @user ok`** — the timeout is lifted and an `unmute` case is
    recorded; running it again → “isn't currently muted”.
11. **DM failure** — if the target has DMs closed, the ban/kick/mute still
    succeeds and your reply includes the private “could not be DM'd” note.
12. **Permission/hierarchy** — a regular member gets “You do not have
    permission”; moderating someone with a higher role is refused with the
    hierarchy message; a `failed` case is recorded (auditable).
13. **Restart** — restart the bot: `.el` state persists (per-guild config),
    mute expiry stays correct (Discord-side), and all cases remain in
    `data/cases.db`.
14. **Failed actions never crash the bot** — trigger a Discord rejection
    (e.g. remove the bot's Ban Members permission, then `.ban`) — the bot
    replies safely and keeps running.

## Manual testing (Phase 6 — professional command UX)

All moderation commands are top-level and guild-only. Reasons are optional
everywhere (defaults to `No reason provided`). Kick, ban, large purges, and
lock require a **Confirm** button click within 30 seconds.

1. **`/warn @user`** — responds immediately with the case summary; no
   confirmation (warn is not dangerous).
2. **`/timeout @user 5m spam`** — times the member out for 5 minutes; the
   case shows Duration + Expires.
3. **`/kick @user`** — shows the confirmation prompt first; the member is
   only kicked after you click **Confirm**.
4. **`/ban @user`** — same confirmation flow; a `ban` case is recorded.
5. **`/unban`** — type in the box: banned users appear as autocomplete
   suggestions; pick one (no manual IDs) and confirm.
6. **`/purge 10`** — deletes immediately. **`/purge 50`** — confirmation
   first (large-purge threshold).
7. **`/slowmode 10s`** — sets the channel slowmode; `/slowmode 0` clears it.
   Try `/slowmode 7h` → rejected (max 6 hours), safe message, no case.
8. **`/lock`** — confirm, then `@everyone` can't send in the channel.
9. **`/unlock`** — restores the exact pre-lock permission state (the lock's
   metadata drives the restore — existing overwrites are never destroyed).
10. **Invalid permissions** — a regular member runs `/warn` → `You do not
    have permission to use this command.`
11. **Role hierarchy protection** — warn a member whose highest role is
    higher than or equal to the moderator's → the action is refused with
    `I cannot moderate this user because their role is higher than or equal
    to mine.` (and a `failed` case is recorded).
12. **Confirmation cancellation** — click **Cancel** → `Action cancelled.`;
    the action never runs, and the confirmation is consumed.
13. **Confirmation expiration** — start `/kick`, wait 30+ seconds, click
    **Confirm** → `This confirmation has expired or was already used.`;
    nobody is kicked.
14. **Double-click protection** — click **Confirm** twice fast → the action
    executes exactly once (one case); the second click reports the
    confirmation was already used.
15. **Case creation** — every action produced exactly one case; verify with
    `/case <id>` and `/cases @user`.
16. **Moderation logging** — with a log channel configured
    (`/config logs channel:#mod-logs`), each action posts a consistent
    `Case #N — Action` embed to that channel.

## Manual testing (Phase 8 — advanced moderation & automod)

Use a private test server. Prefix commands need the message-content intent;
`/automod` commands need the server owner / Administrator permission / a
configured administrator role.

1. **`/warn @user spam`** — warning case created; optional DM; `/case` shows it.
2. **`/ban @user raid`** — confirmation prompt; Confirm → ban, case, DM attempt.
3. **`/kick @user bye`** — confirmation → kick.
4. **`/timeout @user 10m spam`** — timeout applied; case shows Duration + Expires.
5. **`/unban`** — pick the banned user from autocomplete; reason optional.
6. **`/purge 10`** — deletes; `/purge 0` and `/purge -5` rejected; large purge asks for confirmation; the response reports the exact count deleted.
7. **`/slowmode 10s`** — channel slowmode set; `/slowmode 7h` rejected (max 6 hours).
8. **`/lock`** — confirm; `@everyone` can't send. **`/unlock`** — restores the exact pre-lock state (lock case metadata drives the restore).
9. **`/automod enable`** — turns per-guild automod on; **`/automod status`** shows every detector.
10. **`/automod disable`** — master switch off; nothing is analyzed.
11. **Spam detection** — with `/config moderation spam threshold:5 window_seconds:5 action:delete`, send 5+ quick messages → delete case (`Source: Automated (spam)`).
12. **Mention spam** — mention 11+ users in one message → `Automated (mentions)` case.
13. **Duplicate messages** — send the same message 4× within 30 s → `Automated (duplicate)`.
14. **Invite filtering** — set `/automod invites action delete`, post `discord.gg/xyz` → deleted; add your own code via `/automod invites allowed add` → that invite passes.
15. **Link filtering** — set `/config moderation links action:delete` and `domains add youtube.com` → unlisted URLs deleted, youtube.com passes.
16. **Custom word filter** — `/config moderation words add <term>` → matching messages deleted (whole-word by default).
17. **Escalation** — with `/config moderation escalation:3:3600`, the 3rd active warning becomes a 1-hour timeout.
18. **Cooldown** — after one enforcement, keep spamming → no new cases until the cooldown (default 60 s) expires.
19. **Permission failures** — a regular member gets the safe denial for `/warn`, `/config`, and `/automod`.
20. **Role hierarchy** — warn someone with a higher/equal role → refused with the hierarchy message and a `failed` case.
21. **Bot restart** — all cases, per-guild config, custom-role state, and raid settings persist in `data/cases.db`; mute expiry stays correct (Discord-side).
22. **Failed Discord API action** — remove the bot's Ban Members permission then `/ban` → safe error, failed case, bot keeps running.

## Manual testing (Phase 10 — moderation polish)

Use a private test server; prefix commands need the message-content intent.

1. **`.warn @user spam`** — warning case created (Action: Warn in the reply);
   the target is DM'd when possible; `/case <id>` shows the record.
2. **`.warn @user`** (no reason) — `A reason is required for this action.`
3. **`.warnings @user`** — shows the member's active warning count and recent
   warning cases. A regular member gets `You do not have permission...`.
4. **`.clearwarnings @user`** — clears the active warnings; the reply reports
   the count. Run `.warnings @user` again — the count is now 0 and the cases
   show `Cleared`, but `/modhistory @user` still lists them (history kept).
   Clearing again → `has no warnings to clear.`
5. **`.modhistory @user`** — paginated list of the member's cases (Warn,
   Timeout, Ban, ... newest first); `.modhistory @user 2` pages further.
6. **`.purge 5`** — deletes 5 recent messages and reports `Purged 5
   messages`. `.purge 0` / `.purge abc` / `.purge 99999` → safe rejections.
   Try purging messages older than 14 days — the reply reports the exact
   count deleted, never claiming more.
7. **`.slowmode 10`** — this channel's slowmode becomes 10 s; `.slowmode 0`
   clears it; `.slowmode 999999` → rejected (max 6 hours).
8. **`.lockdown`** — `@everyone` can no longer send in the current channel
   (other permission overwrites are untouched). Run `.lockdown` again —
   `This channel is already locked.` and no duplicate case.
9. **`.unlockdown`** — restores the exact pre-lock permission state. On an
   unlocked channel it replies `This channel isn't locked.`.
10. **`.help`** — shows the organized categories. Run it as a moderator (full
    Moderation + Custom Roles + Utility), as an administrator (+
    Configuration), and as a regular member (Utility only, with a note that
    other categories are restricted).
11. **Per-guild prefix** — `/config prefix !` (admin), then `.warn` stops
    parsing and `!warn @user spam` works; `/config view` shows the prefix.
12. **Logging** — with `/config logs channel:#mod-logs`, `.clearwarnings`
    posts a `Warnings cleared` embed and every punishment posts its
    `Case #N — Action` embed.
13. **Presence** — after startup the bot shows `Watching N servers`; the
    count updates when the bot joins/leaves a server.
14. **Restart** — warnings, cleared statuses, per-guild prefix, and all cases
    persist in `data/cases.db`.

## Future roadmap (NOT implemented — pending approval)

- Stronger-evidence automation (ban/kick only with multiple corroborating
  signals)
- Case notes and appeals/resolutions (the case `status` field now also
  supports `cleared`; `update_status` is wired and used by
  `.clearwarnings`)
- Optional web dashboard and API server
- (All require explicit approval; nothing here is built yet.)

## Cost and service policy

Development costs $0. There are no paid APIs, hosted databases, cloud
services, telemetry, or external AI services. The only network endpoint the
bot ever contacts is Discord itself; all persistence is local SQLite.
