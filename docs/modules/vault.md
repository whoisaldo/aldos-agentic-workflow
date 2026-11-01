# Secret vault

Choose this if agents create or need API keys, bot tokens and webhook URLs, and you want them kept in one place on your own machine instead of scattered `.env` files. Skip it if you already use a password manager or secret store your agents can read.

`fleet secret` keeps each key as a mode-600 JSON file under `state_dir/secrets/SCOPE/NAME.json` on the host named by `secrets.host`, outside Git. Other machines reach it over SSH, sending the request and any value on stdin rather than in a command line. The module selects the `fleet-secrets` skill, which tells agents to check the vault before creating a key or asking you for one, and to save a key as soon as they create it. It needs fleet tools.

```sh
scripts/fleet secret list
printf '%s' 'THE_TOKEN' | scripts/fleet secret set DISCORD_EXAMPLE_BOT_TOKEN --scope personal --note 'What it is for and which account issued it'
DISCORD_TOKEN="$(scripts/fleet secret get DISCORD_EXAMPLE_BOT_TOKEN --scope personal)" npm run bot
```

Scopes keep identities apart: a key belongs to the account or project that owns it, typically `personal` or `work`. `set` requires a note and refuses to overwrite a key unless you pass `--replace`; the replaced value is kept in `replaced/`. `list` prints scope, name, date and note, never values.

The vault is files with Unix permissions on one machine you control. It is not encrypted at rest beyond what your disk provides, and anyone who can read your account's files can read it. Back it up the way you back up the rest of `state_dir`.

Check: `scripts/fleet secret list` on another machine returns the same list as on the vault host. The [fleet CLI reference](../fleet-cli.md#secrets) documents locking and the relay.
