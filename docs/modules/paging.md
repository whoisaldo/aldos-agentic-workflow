# Paging the owner

Choose this if agents work while you are away and sometimes stop on something only you can clear in person: a UAC prompt on a Windows worker, a macOS password or permission dialog, or a 2FA code. Skip it if you sit at the machine while agents work, or their reports are enough.

`fleet page` sends one short message through the channels in `fleet.json`'s `notify` section. It is a last resort. The agent's report stays the normal way to reach you, and a page never replaces it. The module needs fleet tools; the command is part of `scripts/fleet`.

1. Create a channel you own: a Discord or Slack webhook, an ntfy topic, an OpenClaw channel, or a command that reads the message on stdin.
2. Store its URL or topic in the [vault](vault.md), or in an environment variable on the notify host. `fleet.json` holds only the reference, such as `{"secret": "personal/FLEET_PAGE_DISCORD_WEBHOOK"}`; `fleet config check` rejects a literal URL, topic or token.
3. Preview without sending:

```sh
scripts/fleet page --dry-run 'The Windows build is waiting on a UAC prompt on mainpc. Approve it at the PC.'
```

The dry run prints each channel's request with references in place of values, reads no secret and leaves the rate limit unchanged.

A page has at most `max_sentences` sentences (default 3) and `max_chars` characters (default 500). Each agent session can page once per `interval_minutes`. Channels are tried in order until one accepts. Other machines relay the page to the notify host over SSH. Every attempt is logged under `state_dir/pages/`, and a failed channel is logged with only a status or error class, never the message or a resolved URL.

Tell your agents when a page is allowed. This repository's [AGENTS.md](../../AGENTS.md) has a rule you can copy. The [fleet CLI reference](../fleet-cli.md#paging-the-owner) lists every channel type and setting.
