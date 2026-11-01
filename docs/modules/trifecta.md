# Orchestrate trifecta

Choose this if you run long builds in T3 Code and have more than one AI account. One thread coordinates, builder accounts write the code, and reviewer accounts check it, all through T3's `delegate_task`. When a builder's usage window closes, new work moves to the next builder and comes back when the window reopens. Skip it if you have one account or do not use T3's delegated tasks.

The module selects the `orchestrate-trifecta` skill, its [README](../../skills/orchestrate-trifecta/README.md) with the sequence diagram of one run, `trifecta.example.json` and the helper's tests. It needs the T3 Code module. A [CLIProxyAPI gateway](proxy.md) is optional: with one, `usage.py` reads each account's usage windows from the rate-limit headers the gateway records; without one, failover goes by limit errors alone.

1. Add each account to T3 as a provider instance. Note the instance ids, model ids and option names from `orchestrator_capabilities`.
2. Install the skill folder for the coordinator's Claude home, as in [installing skills](../install-skills.md).
3. Copy `trifecta.example.json` to `trifecta.json` beside `SKILL.md`, or point `TRIFECTA_CONFIG` at your copy. Replace every id. Put the account whose limit costs least to exhaust first in `builders`.
4. Validate it. This makes no network request:

```sh
python3 ~/.claude/skills/orchestrate-trifecta/scripts/usage.py --check-config
```

It prints the role table and exits 1 for a missing or invalid config, naming the problem. Unknown keys are errors, so a typo does not fall back to a default.

`trifecta.json` holds your instance ids and gateway URLs. It is ignored by Git in this repository; keep it out of any public repository. Gateway management keys go in a separate local file the config names, readable only by you. The helper only reads usage. It never signs in, switches accounts or starts tasks.

Check: `python3 -m unittest tests.test_trifecta -v` from the repository root runs the helper against a local fake gateway with an injected clock.
