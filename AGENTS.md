# Shared fleet

This is a selectable recipe and skill bank. Read `bank/catalog.json` and `README.md` before setup. Identify the user's OS, goal, selected personal/work identity, existing tools and whether they use either Canvas feature. Reuse known preferences; ask only for missing choices that affect setup. Suggest a small selection and explain why unrelated modules can be skipped. Use `scripts/bank.py plan` and export the selection with `pull` to a new folder. Selection/export is preparation, not authority to sign in, install services, expose endpoints, enable schedules or replace an existing setup. When the user requests setup of a chosen module, carry the authorized steps through its documented checks. Do not make them approve routine preparation twice.

Read `fleet.json` for the owner's configured machines, profiles and protected services. Read `README.md` for setup and commands. Addresses and usernames in the starter configuration are examples, not reachable hosts.

A new owner starts with `scripts/fleet init`, which writes a `fleet.json` for their own machines, identities and paging channel, then `scripts/fleet config check`, which prints one line per problem. Offer both before any setup that reads `fleet.json`. For the trifecta workflow, offer to copy `skills/orchestrate-trifecta/trifecta.example.json` to `trifecta.json`, fill in the owner's provider instance ids from `orchestrator_capabilities`, and validate it with `usage.py --check-config`. Never commit either file's private values.

## Accounts and machines

Choose the machine and account at thread creation. Keep personal and employer identities, repositories, memory and artifacts separate. For employer work, use the user's approved work environment and provider. Do not route work through a personal gateway or the personal Claude-to-Codex bridges. Do not infer permission to access internal systems from the user's employer. Report account exhaustion without changing identity.

Use a configured Mac for macOS/iOS/browser work and a configured Windows worker for native Windows/GPU/display checks. Display checks require the owner's interactive login. A single Mac is enough to start; extra workers are optional.

## Existing work

Work in a separate task worktree. Touch only the task's files and processes. Never use process-name kill commands, restart T3, change Tailscale Serve, replace provider settings, or launch a second backend against a live T3 home. Additive provider changes use the supported settings API and preserve existing entries.

Define completion from the request, run the relevant checks, and provide a reviewable result. Remote previews bind to `0.0.0.0` only on a configured trusted network; return the actual reachable address. Save screenshots and give their paths. Do not assume the user sees an opened GUI.

Requests to prepare work do not authorize merging, publishing or messaging others. Keep questions for the user out of PR bodies and commit messages. Author commits as the repository's configured human owner, without agent attribution or co-author trailers.

## Reaching the owner

Reach the owner through your report in the thread. `fleet page` is a last resort and replaces neither the report nor delivering files. Use it only when a task is stopped on something only the owner can clear in person and waiting costs something: a UAC prompt or Windows sign-in on a worker, a macOS password, permission dialog or disk unlock, or a 2FA code. Page once, with one to three sentences saying what is blocked, why it needs the owner, and the exact action to take, then continue any work that does not depend on it. Leave out code, credentials and employer or school task details. Finished work, failing checks, questions, quota limits and blockers you can work around go in your report, never a page; so does a page that fails or is refused. Run commands that may need elevation with a timeout, so a hidden prompt cannot hang the task. Use no messaging channel other than the configured `fleet page` channels.

## Keys and tokens

API keys, bot tokens and webhook URLs that agents create or need go in the vault through `fleet secret`; the `fleet-secrets` skill explains how. Run `fleet secret list` before creating a key or asking the owner for one. Save a key right after creating it, with a note saying what it is for and which account issued it, under the scope of the account that owns it. Pass values on stdin and read them inside `$(...)` or a pipe, so they never appear in a command line, transcript, report, commit or log.

## Configuration and validation

Fleet-owned source lives in `skills/`; vendored skills and hashes are in `vendor/skills` and `skills/external.lock.json`. Preserve built-in, plugin-owned and organization-managed skills under their providers' control. Credentials, sessions, OAuth state, private memory, backups and logs stay outside Git. Use fresh gateway sign-ins, never copied refresh credentials.

Run `scripts/fleet diff --profile NAME` before applying to a new managed home. Never replace native homes or update a profile with history through ordinary apply. Use the documented idle migration for an existing installation only when authorized. Preserve explicit model and reasoning choices in the image, 3D and computer-use skills.

Run `python3 -m unittest discover -s tests -v` after deployment changes. Tests use temporary homes and repositories. Run `scripts/fleet doctor` after actual local setup changes. Historical files under `imports/`, `examples/legacy-fleet.json` and `docs/legacy-reference.md` are reference material, not installation instructions.

For changes in the full bank source checkout, review all added files and media, stage the selected changes, refresh `bank/files.json`, and run `python3 scripts/check-public.py --history --secrets`. Selected kits use `bundle-manifest.json` and their module checks. Enable the pre-push hook once per clone with `git config core.hooksPath .githooks`; it runs the public check on the commits a push would publish and reads a private deny-list from `PUBLIC_CHECK_DENYLIST` or `git config publiccheck.denylist`. Never commit the deny-list or quote its terms. Keep findings and private audit reports outside Git. Never treat a clean scan as permission to publish credentials or personal state. See `docs/public-release.md`.
