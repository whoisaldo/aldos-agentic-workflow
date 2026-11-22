# Fleet walkthrough and captured output

Everything below is real command output captured in an empty home directory against the Quick start config and the example `trifecta.json`, with temporary paths shown as `~/` and `...` where fields were cut. `fleet` means `scripts/fleet` or the launcher `fleet sync` installs on each machine.

## Quick start from a fresh clone

You need Python 3.11 or newer. Nothing below signs in, installs a service, or opens a port.

```sh
git clone https://github.com/whoisaldo/aldos-agentic-workflow.git
cd aldos-agentic-workflow

# 1. Write fleet.json for your own machines, identities and paging channel. It goes outside
#    the checkout, so the tracked starter file stays as shipped and the export in step 3 works.
#    In a terminal, init prompts for anything you leave out.
scripts/fleet init --owner 'Your name' --state-dir '~/Services/fleet' \
  --machine 'devbox1,os=macos,user=your-user,address=100.64.0.1,ssh=devbox1,roles=development+macos' \
  --machine 'mainpc,os=windows,user=your-user,address=100.64.0.2,ssh=mainpc,roles=windows+gpu+display' \
  --identity personal --identity work --notify ntfy --output ~/.config/fleet/fleet.json
export FLEET_CONFIG=~/.config/fleet/fleet.json   # every fleet command reads this file

# 2. Validate it. Prints one line per problem, or "<path>: valid".
scripts/fleet config check

# 3. Pick a preset and export only that selection to a new folder.
python3 scripts/bank.py plan --preset personal-code
python3 scripts/bank.py pull --preset personal-code --output ../my-fleet-kit

# 4. Install the trifecta skill from the kit for Claude Code, then configure and validate it.
mkdir -p ~/.claude/skills
test ! -e ~/.claude/skills/orchestrate-trifecta && test ! -L ~/.claude/skills/orchestrate-trifecta && \
  cp -R ../my-fleet-kit/skills/orchestrate-trifecta ~/.claude/skills/orchestrate-trifecta
cp -n ~/.claude/skills/orchestrate-trifecta/trifecta.example.json ~/.claude/skills/orchestrate-trifecta/trifecta.json
python3 ~/.claude/skills/orchestrate-trifecta/scripts/usage.py --check-config

# 5. Check your own commits before every push.
git config core.hooksPath .githooks
```

Add `export FLEET_CONFIG=...` to your shell profile so later sessions read the same file. You can also paste [docs/handoff.md](handoff.md) into a coding agent to have it walk you through choosing modules, running `fleet init`, and setting up `trifecta.json`.

## Gateways and trifecta usage checks

Each builder has a `switch_at_percent`. Before each wave of builds the coordinator reads every usage window the gateways (and, for a native Codex reviewer, Codex's session logs) report for each role. A builder at its threshold gets nothing large, and a builder that fails with a usage-limit error hands its work to the next builder in order. `usage.py --sleep-until-reset builder1` then runs in the background (marked with `t3_background_wait`) and wakes the thread when the window reopens. When a configured weekly window drops to `page_at_left_percent` left (default 3%), `usage.py` prints a `PAGE:` line so the coordinator sends one `fleet page` for that reset. Validating the example config makes no network request:

```console
$ TRIFECTA_CONFIG=skills/orchestrate-trifecta/trifecta.example.json \
    python3 skills/orchestrate-trifecta/scripts/usage.py --check-config
config: skills/orchestrate-trifecta/trifecta.example.json

ROLE         LABEL                            PROVIDER INSTANCE          MODEL              OPTIONS                    USAGE                RULE
coordinator  Coordinator account              claudeAgent_coordinator    -                  {}                         coordinator-claude   coordinates; the thread you are in; page on weekly, fable_weekly
builder1     Builder seat                     claudeAgent_builder        claude-opus-5-5    {"effort": "high"}         builder-claude       switch at 85%; page on fable_weekly
builder2     Coordinator account (fallback)   claudeAgent_coordinator    claude-opus-5-5    {"effort": "high"}         coordinator-claude   switch at 85%
reviewer1    GPT reviewer                     codex                      gpt-6.2-sol        {"reasoningEffort": "high"} reviewer-codex       weight 7 (70% of reviews); page on weekly
reviewer2    Claude reviewer                  claudeAgent_builder        claude-opus-5-5    {"effort": "high"}         builder-claude       weight 3 (30% of reviews)

max_parallel_builders: 2
stale_minutes: 20
page_at_left_percent: 3
never_use: claudeAgent_direct, codex_old_seat

usage sources:
  coordinator-claude: cliproxyapi http://127.0.0.1:8317 provider=claude unit=fraction key_file=~/.config/trifecta/main-gateway.json  (key file not found yet)
  builder-claude: cliproxyapi http://127.0.0.1:8318 provider=claude unit=fraction key_file=~/.config/trifecta/builder-gateway.json  (key file not found yet)
  reviewer-codex: cliproxyapi http://127.0.0.1:8317 provider=codex unit=percent key_file=~/.config/trifecta/main-gateway.json codex_sessions=~/.codex/sessions  (key file not found yet)
```

`fleet gateway-status --json` reports each configured gateway's health, routing and session-affinity checks, account statuses, and last-hour request counts without exposing keys, tokens, or emails. See the [proxy module](modules/proxy.md) for gateway setup.

## `fleet page`: a last resort that reaches your phone

An agent stuck on a UAC prompt, a macOS password, or a 2FA code sends one to three sentences through the channels in `fleet.json`. Each agent session can page once per 30 minutes, and the report in the thread stays the main way an agent reaches you. A dry run shows each channel's request with vault references in place of the URL and topic:

```console
$ fleet page --dry-run 'The Windows build is waiting on a UAC prompt on mainpc. Approve it at the PC; nothing else in the task can continue until then.'
{
  "channel": "ntfy",
  "type": "ntfy",
  "url": "https://ntfy.sh/<vault secret personal/FLEET_PAGE_NTFY_TOPIC>",
  "headers": {
    "Title": "devbox1 · personal-codex · my-app",
    "Content-Type": "text/plain; charset=utf-8"
  },
  "body": "The Windows build is waiting on a UAC prompt on mainpc. Approve it at the PC; nothing else in the task can continue until then."
}
Dry run: nothing was sent and the page limit is unchanged.

$ fleet page --dry-run 'One. Two. Three. Four sentences is too many.'
fleet: A page is one to 3 sentences and at most 500 characters; this has 4 sentences and 44 characters
```

Supported channels: `discord-webhook`, `slack-webhook`, `ntfy`, `openclaw`, and `command`, tried in order until one accepts. See the [paging module](modules/paging.md).

## `fleet secret`: keys go in the vault as soon as they exist

When an agent creates a bot token or API key, it saves it right away, and it checks the vault before asking you for one. Values go in on stdin and come out inside `$(...)`, so they never land in a command line or a transcript. `fleet secret fingerprints` exports only names, lengths, and SHA-256 hashes (including rotated entries and gateway keys) so an output redactor can scrub secrets without holding them.

```console
$ printf '%s' "$TOKEN" | fleet secret set DISCORD_EXAMPLE_BOT_TOKEN --scope personal \
    --note 'Bot token for the example Discord bot, from the personal developer account'
Saved personal DISCORD_EXAMPLE_BOT_TOKEN.
$ fleet secret list
personal      DISCORD_EXAMPLE_BOT_TOKEN                2026-10-31  Bot token for the example Discord bot, from the personal developer account
$ printf '%s' x | fleet secret set DISCORD_EXAMPLE_BOT_TOKEN --scope personal --note again
fleet: personal DISCORD_EXAMPLE_BOT_TOKEN already exists. Pass --replace only to rotate it; the vault keeps the old value.
```

See the [vault module](modules/vault.md).

## Skill releases and `fleet sync`

`fleet release` copies the skills into a read-only folder named by their content hash. `fleet sync --apply` points every profile on every machine at it, so a Mac and a Windows PC run the same skill versions. Old releases stay in place for sessions still using them. Claude-only skills reach only personal Claude profiles.

`fleet init` lists your existing Codex and Claude homes as native profiles, and sync leaves native homes alone by default. Set `sync.include_native` to `true` in your `fleet.json` to link skills into those homes; sync adds links next to your own skills and stops with a problem instead of replacing one.

```console
$ fleet release
~/Services/fleet/releases/44204301cc7a721cc676
$ fleet sync --apply > /dev/null
$ fleet sync --check
{
  "revision": "44204301cc7a721cc676",
  "profiles": 4,
  "problems": []
}
```

A LaunchAgent on the sync host repeats this every two minutes and updates the other machines over SSH. See [skill synchronization](fleet-cli.md#skill-synchronization).

## `fleet task` worktrees and build leases

A task gets its own branch and worktree from the repository's committed HEAD, so uncommitted work in your checkout stays untouched. A lease keeps two heavy builds from running at once on one machine.

```console
$ fleet task ~/code/my-app fix-export --profile personal-codex --done 'Export works and the unit tests pass'
Preparing worktree (new branch 'fleet/fix-export')
HEAD is now at 468edf6 init
{
  "task": "fix-export",
  "machine": "devbox1",
  "profile": "personal-codex",
  ...
  "worktree": "~/Services/fleet/worktrees/my-app-32a36590/fix-export",
  "done": "Export works and the unit tests pass",
  ...
}
$ fleet lease heavy-build -- sh -c 'echo building'
building
```

## `fleet claude-debug`: one direct request, only when asked

Most traffic goes through the gateways. When you need to know whether a problem is the gateway's or the account's, `claude-debug ask` sends one tiny request to a direct Claude login, with no tools, no MCP servers, a spending cap, and a required `--reason`. It refuses a home signed in to an account other than the configured email, and does nothing until configured:

```sh
fleet claude-debug ask personal 'Say ok.' --model claude-opus-5-5 --fast --reason 'Does the account allow fast mode?'
```

```console
$ fleet claude-debug status personal     # a config without a debug section
fleet: claude-debug is not configured. Add debug.claude entries with each direct home and its account email to fleet.json (see docs/fleet-cli.md).
```

## Public-safety tooling

Every push runs a checker for home paths, tailnet details, real-looking email addresses, private state files, and Gitleaks' secret rules. Names that only you know are private (a surname, an employer, a bot name) go in a deny-list kept outside Git. A finding names the file, the line, and the deny-list line, never the matched text:

```console
$ python3 scripts/check-public.py --denylist ~/.config/public-check/denylist.txt
working-tree: denylisted-term (deny-list line 1): notes.md:4
working-tree: personal-home: notes.md:2
working-tree: personal-email: notes.md:3
Public check failed: 3 location(s). Values withheld.
```

Configure your local hook and optional private deny-list once per clone:

```sh
git config core.hooksPath .githooks
mkdir -p ~/.config/public-check
test -e ~/.config/public-check/denylist.txt || cat > ~/.config/public-check/denylist.txt <<'LIST'
# One case-insensitive regex per line. Replace these fictional examples with your own.
# Jane Q\. Example
# Acme Widgets
LIST
git config publiccheck.denylist ~/.config/public-check/denylist.txt
```

See [public release checks](public-release.md).

## Configuration map

Two files hold everything specific to one owner. Neither holds a credential: secrets are references to vault entries or environment variables.

| Section | What it sets |
| --- | --- |
| `owner`, `state_dir` | Your name, and the private folder outside Git for releases, tasks, leases, pages, the vault, and logs |
| [`machines`](fleet-cli.md#machines) | Each machine's OS, user, Tailscale address, SSH alias, roles, and optional paths |
| `profiles` | Codex and Claude homes per identity: native homes stay read-only, managed homes get a model, effort, and gateway |
| [`gateway`, `additional_gateways`](modules/proxy.md) | One CLIProxyAPI gateway per identity: URL, pinned version and checksum, runtime folder, expected sign-in email |
| `protected_services` | Services agents must leave alone, such as the T3 desktop app's port |
| [`skill_policy`](fleet-cli.md#skill-synchronization) | Which identities count as personal and receive Claude-only skills |
| [`notify`](fleet-cli.md#paging-the-owner) | `fleet page`: host, limits, and ordered channels |
| [`secrets`](fleet-cli.md#secrets) | `fleet secret`: vault host and scopes |
| [`sync`](fleet-cli.md#skill-synchronization) | `fleet sync`: host, peer machines, interval, and service label |
| [`debug`](fleet-cli.md#claude-without-the-gateway) | `fleet claude-debug`: direct Claude homes and their expected emails |
| [`trifecta.json`](../skills/orchestrate-trifecta/README.md#config-reference) | Coordinator, ordered builders, weighted reviewers, `never_use`, weekly paging, and usage sources |

## The bank selector

`scripts/bank.py` lists modules, skills, and presets, plans a selection with its dependencies, and copies only that selection into a new folder with a hash manifest. It installs nothing.

```sh
python3 scripts/bank.py list
python3 scripts/bank.py plan --preset personal-code --module tailscale --module paging
python3 scripts/bank.py pull --skill unslop --skill resume-craft --output ../writing-kit
python3 scripts/bank.py plan --preset personal-code --skip-skill baseline-ui
```

| Preset | A useful start for | Leaves out |
| --- | --- | --- |
| `minimal` | Portable UI, writing, and `/aldo-mode` front door | All services and account setup |
| `personal-code` | Local coding with T3 and the trifecta build/review workflow | School, OpenClaw, proxy, and tunnels |
| `work-code` | Coding in your employer-approved environment | Personal bridges, proxy, and school workflows |
| `student` | OpenClaw, general Canvas planning, and document review | Coding proxy, video, and tunnels |
| `remote` | T3, private networking, paging, friend previews, and proxy profiles | School, assistant, and video |
| `creative` | Design, video, resume, and the Claude-to-Codex bridges | School and remote networking |
| `everything` | The whole bank for reference | Nothing, but nothing is enabled |

Modules: [coding agents](modules/coding-agents.md), [T3 Code](modules/t3-code.md), [trifecta](modules/trifecta.md), [fleet tools](modules/fleet-tools.md), [paging](modules/paging.md), [vault](modules/vault.md), [inference proxy](modules/proxy.md), [Tailscale](modules/tailscale.md), [Cloudflare Tunnel](modules/cloudflare.md), [personal bridges](modules/personal-bridges.md), [video](modules/video.md), [OpenClaw](modules/openclaw.md), [OpenClaw visual canvas](modules/openclaw-canvas.md), [Canvas LMS](modules/canvas-lms.md), [document reviews](modules/document-reviews.md), [provider skills](modules/provider-skills.md), [assistant extras](modules/assistant-extras.md), and [legacy](modules/legacy.md).
