# Fleet CLI reference

`scripts/fleet` runs `scripts/fleetlib.py` with Python 3.11 or newer and only the standard library. Everything specific to one owner comes from `fleet.json`: machines, account homes, hosts, channels, labels and intervals. `fleet.json` is committed, so it never holds credentials. Private state (releases, tasks, leases, pages, the secret vault and logs) lives under `state_dir`, outside Git.

`fleet.schema.json` describes the file for editors. The starter `fleet.json` references it with `"$schema": "./fleet.schema.json"`, which the loader ignores.

## Environment

| Variable | Effect |
| --- | --- |
| `FLEET_CONFIG` | Path of the `fleet.json` to use. Default: the one beside the running code, else the checkout's. |
| `FLEET_STATE_DIR` | Overrides `state_dir`. Windows defaults to `D:\AgentWork\fleet-state`. |
| `FLEET_MACHINE` | This machine's name in `fleet.json`. Default: the machine whose `hostname` (or name) matches the short hostname. |
| `FLEET_USER_HOME` | Home used to expand `~/` paths. Tests point it at a temporary folder. |
| `FLEET_REPO_ROOT` | Checkout that `release` reads skills from, when the CLI runs from a synced runtime. |

## Start a configuration

### `fleet init`

```sh
scripts/fleet init --owner 'Your name' --state-dir '~/Services/fleet' \
  --machine 'devbox1,os=macos,user=your-user,address=100.64.0.1,ssh=devbox1,roles=development+macos' \
  --machine 'mainpc,os=windows,user=your-user,address=100.64.0.2,ssh=mainpc,roles=windows+gpu+display' \
  --identity personal --identity work --notify ntfy --output ~/.config/fleet/fleet.json
export FLEET_CONFIG=~/.config/fleet/fleet.json
```

In a clone of this repository, write the file outside the checkout and point `FLEET_CONFIG` at it. The checkout's starter `fleet.json` is tracked, and `scripts/bank.py pull` refuses to export once a hashed file has changed.

Writes a starter `fleet.json`: the machines, a native Codex and Claude profile per identity (the first identity uses `~/.codex` and `~/.claude`, later ones `~/.codex_IDENTITY` and `~/.claude_IDENTITY`), and `secrets`, `notify` and `sync` sections hosted on the first machine. `--machine` is repeatable: `NAME,os=macos|windows|linux,user=USER,address=ADDRESS,ssh=ALIAS,roles=ROLE+ROLE`, optionally with `repo=` and `work_root=`. `--notify` is one of `discord-webhook`, `slack-webhook`, `ntfy`, `openclaw`, `command` or `none`; the channel it adds names a vault secret rather than holding a URL or topic.

In a terminal, `init` prompts for values you did not pass. Otherwise it fills placeholders. It refuses to replace an existing file unless you pass `--force`. Its output always passes `fleet config check`.

### `fleet config check [--config PATH]`

```sh
scripts/fleet config check
scripts/fleet config check --config /path/to/fleet.json
```

Validates the file without third-party packages and prints one line per problem, such as `fleet.json.notify.host: "nowhere" is not a machine in fleet.json machines (devbox1, mainpc)`. It checks key names and types against `fleet.schema.json`, plus rules across sections: hosts must be configured machines, managed profiles need a model, effort and gateway, vault references must use a configured scope, a service label must not contain a username, and a notification URL, topic or token must be a reference, never the value. Types are checked first, and the rules across sections skip any section with a type problem, so a wrongly shaped file gets problem lines rather than a traceback. Exits 1 when it finds a problem.

## Tasks and builds

### `fleet task REPO SLUG --profile NAME --done TEXT`

Creates branch `fleet/SLUG` and a worktree under `state_dir/worktrees` from the repository's committed HEAD, and records the task under `state_dir/tasks`. The original checkout and its uncommitted changes are untouched. It does not launch an agent.

### `fleet lease RESOURCE -- COMMAND ...`

Runs a command while holding `state_dir/leases/RESOURCE`, so fleet-launched heavy builds do not overlap. It only coordinates commands launched through it. A crashed owner may leave a lease; inspect its `owner.json` before removing it.

### `fleet run --profile NAME -- ARGS ...`

Runs the profile's CLI (`codex` or `claude`) with that profile's home.

## Releases and profiles

### `fleet release`

Packages `skills/` and the pinned vendored skills (verified against `skills/external.lock.json`) into a read-only, content-addressed `state_dir/releases/REVISION`, and records it as the latest release.

### `fleet profiles`

Lists profiles with identity, home and whether the home is managed or a preserved native home.

### `fleet diff --profile NAME` and `fleet apply --profile NAME [--release REVISION]`

`diff` previews a managed profile's settings and skill links, with the gateway key redacted. `apply` writes them, backing up replaced files. Native homes are read-only targets, an occupied skill directory is never replaced, and a profile with session history is refused.

### `fleet doctor [--profile NAME ...]`

Checks original profile hashes and T3 (when a baseline exists), installed managed profiles, skills against the latest release, the sync worker's last report, and each gateway's reachability, account counts and identity isolation. Prints `PENDING:` lines and exits 1 when something needs attention. It does not call agents.

## Skill synchronization

```json
"skill_policy": {"personal_identities": ["personal"]},
"sync": {
  "host": "devbox1",
  "machines": ["mainpc", "macbook-pro-2"],
  "interval_seconds": 120,
  "stale_after_minutes": 10,
  "service_label": "org.sharedfleet.skill-sync",
  "include_native": false,
  "shared_homes": []
}
```

### `fleet sync [--apply] [--check] [--profile NAME ...] [--release REVISION]`

Without flags, prints the plan. `--apply` links each profile's skills to the latest (or given) release, backing up replaced links under `state_dir/backups/skill-sync-*`. Old releases stay in place for running sessions. It writes only fleet-owned skill links: settings, credentials, histories, provider-owned skills and T3 entries are never touched, and an unmanaged directory with a fleet skill's name blocks the whole sync. `--check` prints the problems and exits 1 if any.

Managed profiles are synced by default. `include_native: true` adds native homes; `shared_homes` adds extra skill roots such as `~/.agents`, which receive only portable skills. A skill containing a `.claude-only` marker file goes only to Claude profiles whose identity is in `skill_policy.personal_identities` (default `["personal"]`).

### `scripts/sync-fleet.py [--local]` and `scripts/install-skill-sync.py [--apply]`

The worker runs `fleet sync --apply` on the sync host, then updates each machine in `sync.machines` over SSH (its `ssh` alias): it copies a checksummed runtime (fleet code and `fleet.json`) and the release archive, installs them under the peer's state, syncs there and keeps the peer's fleet launcher current. A peer that is offline is recorded and retried on the next pass. Results go to `state_dir/fleet-sync-status.json`, which `fleet doctor` reads.

Optional per-machine keys: `python` (default `python3`, or `python` on Windows), `state_dir` (when the peer's state differs), and `launcher` (default `~/.local/bin/fleet`, or `WORK_ROOT\bin\fleet.cmd` on Windows).

`install-skill-sync.py` previews, or with `--apply` installs, a macOS LaunchAgent on the sync host that runs the worker every `interval_seconds`. Its label is `sync.service_label`; keep it free of usernames.

## Paging the owner

```json
"notify": {
  "host": "devbox1",
  "max_chars": 500,
  "max_sentences": 3,
  "interval_minutes": 30,
  "channels": [
    {"name": "discord", "type": "discord-webhook", "url": {"secret": "personal/FLEET_PAGE_DISCORD_WEBHOOK"}},
    {"name": "phone", "type": "ntfy", "server": "https://ntfy.sh", "topic": {"env": "FLEET_PAGE_NTFY_TOPIC"}}
  ]
}
```

### `fleet page MESSAGE [--dry-run]`

```sh
fleet page "The Windows build is waiting on a UAC prompt on mainpc. Approve it at the PC; nothing else in the task can continue until then."
fleet page --dry-run 'Test.'
```

A last resort for a blocker only the owner can clear in person, such as a UAC prompt, a macOS password or a 2FA code. Reports remain the normal way to reach the owner. The text must have at most `max_sentences` sentences and `max_chars` characters. Each agent session (Claude's `CLAUDE_CODE_SESSION_ID`, Codex's `CODEX_THREAD_ID`, else the working folder) can page once per `interval_minutes`, so one agent's page never blocks another's.

The notify host tries each channel in order and stops at the first that accepts the page. The message is headed `MACHINE · PROFILE · FOLDER`. Other machines relay the page to the host with `ssh ALIAS FLEET_COMMAND page --relay` and the request on stdin; the relay counts only the host's `Page sent.` confirmation. Every attempt is logged in `state_dir/pages/log.jsonl`. A failed channel is reported and logged by name with only an HTTP status, an exit status or an error class such as `ValueError`, never the sender's message or a command's output, since those can quote a resolved URL, token or environment. A lock per agent spans the limit check, the send and the record, so simultaneous pages from one agent send once; a page that every channel refuses leaves the limit unused. `--dry-run` prints each channel and its payload, with references shown instead of values, and sends nothing, reads no secret and leaves the limit unchanged.

| Type | Settings | Sends |
| --- | --- | --- |
| `discord-webhook` | `url` | JSON `content`, with mentions disabled |
| `slack-webhook` | `url` | JSON `text` |
| `ntfy` | `topic`, optional `server` (default `https://ntfy.sh`), `token`, `priority` | Plain text with a `Title` header |
| `openclaw` | `channel`, `target`, optional `account`, `binary` | `openclaw message send --channel C --target T [--account A] --message TEXT` |
| `command` | `argv` | Runs the argv with the message on stdin |

`url`, `topic` and `token` are references: `{"secret": "SCOPE/NAME"}` reads the vault, `{"env": "NAME"}` reads an environment variable on the notify host. `config check` rejects a literal URL, topic or token. Relays use the host's `fleet_command`, defaulting to `REPO/scripts/fleet` (or `WORK_ROOT\bin\fleet.cmd` on Windows).

## Secrets

```json
"secrets": {"host": "devbox1", "scopes": ["personal", "work"]}
```

### `fleet secret list|get|set|remove`

```sh
fleet secret list [--scope personal]
printf '%s' 'THE_TOKEN' | fleet secret set DISCORD_EXAMPLE_BOT_TOKEN --scope personal --note 'What it is for and which account issued it'
DISCORD_TOKEN="$(fleet secret get DISCORD_EXAMPLE_BOT_TOKEN --scope personal)" npm run bot
fleet secret set DISCORD_EXAMPLE_BOT_TOKEN --scope personal --replace < new-token.txt
fleet secret remove DISCORD_EXAMPLE_BOT_TOKEN --scope personal
```

Each key is a mode-600 JSON file at `state_dir/secrets/SCOPE/NAME.json` on the vault host, in a mode-700 folder. Names are environment variable names. Scopes default to the profiles' distinct identities. `set` reads the value from stdin, or prompts without echo in a terminal, and needs `--note` for a new key. It refuses to overwrite a key without `--replace`, and then keeps the old record in `replaced/`. `remove` keeps a copy in `removed/`. Writes to one key take a lock under `secrets/.locks/` (`flock` on macOS and Linux, `msvcrt.locking` on Windows), and a first write publishes with a hard link that fails if the key exists, so simultaneous writers never both save and every replaced value is kept. `list` prints scope, name, date and note, never values. Other machines relay requests over SSH with the request, including any value, on stdin rather than in a command line. The `fleet-secrets` skill tells agents when to use it.

## Claude without the gateway

```json
"debug": {
  "claude": {
    "personal": {"home": "~/.claude", "email": "your-email@example.com", "login": "claudeai", "sso": false}
  }
}
```

### `fleet claude-debug status|ask|login|logout NAME`

```sh
fleet claude-debug login personal        # the owner, in a terminal
fleet claude-debug status personal
fleet claude-debug ask personal 'Say ok.' --model claude-opus-5-5 --effort xhigh --fast --reason 'Does the account allow fast mode?'
```

For debugging only, when the owner asks: one tiny request to a direct Claude login, bypassing the gateway. `NAME` is a `debug.claude` entry. Without that section the command says it is not configured and exits 1.

`ask` refuses a home that is signed out or signed in to an account other than its `email`. The account is the email that `claude auth status` reports, which is the account Claude Code recorded for that home at sign-in; the check does not confirm it with the server. If status reports no email, `ask` refuses. It removes `ANTHROPIC_*` and `CLAUDE*` variables (the caller's gateway route and session), runs in an empty temporary folder with only project settings, no tools, no MCP servers and no saved session, and caps spending with `--max-usd` (default 0.25). `--reason` is required. The model defaults to `claude-haiku-5-5`. Each request is logged with its reason and sender in `state_dir/claude-debug/requests.jsonl`. `login` uses `--claudeai` or `--console` from `login`, adds `--sso` when configured, and prints the sign-in URL instead of opening a browser. `logout` signs the home out. The default `~/.claude` home is addressed without `CLAUDE_CONFIG_DIR`, matching how a terminal and T3 address it. The `fleet-debug` skill tells agents when they may use it.

## Machines

```json
"machines": {
  "devbox1": {"os": "macos", "user": "your-user", "address": "100.64.0.1", "ssh": "devbox1", "repo": "~/fleet", "roles": ["development", "macos"]}
}
```

Required: `os` (`macos`, `windows` or `linux`), `user`, `address`, `ssh` and `roles`. Optional: `hostname`, `repo`, `fleet_command`, `work_root`, `state_dir`, `python`, `launcher`, `heavy_build_slots` and `interactive_tests_require_login`.
