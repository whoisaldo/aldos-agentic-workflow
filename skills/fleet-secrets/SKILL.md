---
name: fleet-secrets
description: Store and fetch the owner's API keys, bot tokens, webhook URLs and other secret env values in the fleet vault. Use as soon as a task creates a key or token (a bot, an API account, a webhook) so it is saved for later, and before creating a new key or asking the owner for one, to check whether the vault already has it.
---

The vault lives on the machine named by `secrets.host` in the fleet checkout's `fleet.json`, under its private `state_dir/secrets`, outside Git. Every machine uses the same command; other machines reach the vault host over SSH using its `ssh` alias. On a Windows worker, run its fleet launcher (for example `D:\AgentWork\bin\fleet.cmd`) in place of `fleet`.

Check the vault before you create a key or ask the owner for one:

```sh
fleet secret list
fleet secret list --scope personal
```

`list` shows scope, name, date and note. It never shows values.

Every key belongs to one scope. The scopes are `secrets.scopes` in `fleet.json`, or the profiles' identities when that is not set; `personal` and `work` are typical. Choose the scope of the account or project that owns the key, which may differ from the account you are running on. Never copy a key into another scope, and keep employer keys out of personal projects.

Save a new key right after it is created, before you use it. Pass the value on stdin so it never appears in a command line:

```sh
printf '%s' 'THE_TOKEN' | fleet secret set DISCORD_EXAMPLE_BOT_TOKEN --scope personal \
  --note 'Discord bot token for the example bot, issued by the owner''s Discord developer account'
grep '^STRIPE_SECRET_KEY=' .env | cut -d= -f2- | fleet secret set STRIPE_SHOP_TEST_KEY --scope personal --note '...'
```

Name keys like environment variables, in the order service, project, kind: `OPENAI_EXAMPLE_API_KEY`. Notes are required. A note says what the key is for, which account or app issued it, and anything needed to rotate it. Never put a value in a note.

`set` will not overwrite an existing key. Add `--replace` only when you mean to rotate a key; the vault keeps the old value in `replaced/`. `remove` keeps a copy in `removed/`; use it only when the owner asks.

Read a key inside `$(...)` or a pipe, never as a bare command. A bare `get` prints the value into your transcript.

```sh
DISCORD_TOKEN="$(fleet secret get DISCORD_EXAMPLE_BOT_TOKEN --scope personal)" npm run bot
printf 'DISCORD_TOKEN=%s\n' "$(fleet secret get DISCORD_EXAMPLE_BOT_TOKEN --scope personal)" >> .env
```

In PowerShell: `$env:DISCORD_TOKEN = D:\AgentWork\bin\fleet.cmd secret get DISCORD_EXAMPLE_BOT_TOKEN --scope personal`.

Notification channels in `fleet.json` (`notify.channels`) refer to vault entries as `{"secret": "SCOPE/NAME"}`. Store a webhook URL or ntfy topic there, never in `fleet.json` itself, which is committed.

Confirm a project's `.env` is gitignored before you write a key into it. Values stay out of replies, reports, PRs, commit messages, logs and committed files. If the vault lacks a key and you cannot create one, say so in your report.
