---
name: fleet-debug
description: Send one tiny, direct test request to one of the owner's Claude accounts without the gateway, through `fleet claude-debug`. Use only when the owner asks in the current thread to check something without the gateway, such as whether an account allows fast mode, a model or an effort level, or whether a problem is the gateway's, Claude Code's or T3's. Never use it to do work, as a fallback when a fleet provider fails or runs out, or on your own initiative.
---

The direct Claude logins are listed under `debug.claude` in the fleet checkout's `fleet.json`: each entry names a home that signs in to Anthropic directly, without the gateway, and the account email it must be signed in to. The owner keeps them for live verification. Never start a thread, delegated task or fallback on them, even if T3 lists them as available, and never run `claude` against those homes yourself. `fleet claude-debug` is the only way to reach them, and only when the owner asks. If `fleet.json` has no `debug.claude` section, the command says so; report that instead of improvising.

Run it on the machine that holds those homes. From another machine, use that machine's `ssh` alias from `fleet.json` and run the same command there.

Check the login first. If it reports signed out or the wrong account, tell the owner; only they sign these homes in or out.

```sh
fleet claude-debug status personal
```

Send the smallest request that answers the owner's question. The prompt must be short and generic, such as `Say ok.`: never code, files, project names or task content. A request to one identity carries nothing from another identity's task. `--reason` is required and is logged; give the owner's question in a few words.

```sh
fleet claude-debug ask personal 'Say ok.' --model claude-opus-5-5 --effort xhigh --fast --reason 'Does the account allow fast mode?'
fleet claude-debug ask personal 'Say ok.' --model claude-fable-5-1 --reason 'Is Fable 5.1 available directly?'
```

The model defaults to `claude-haiku-5-5`, the cheapest; pass `--model` only when the model is what you are testing. `--max-usd` caps the request at $0.25 unless you lower it. Send one request, read it, and send another only if the first leaves the question open. Fast mode can bill as extra usage, so use `--fast` only when fast mode is the question.

The command prints Claude Code's JSON result. The fields that usually answer the question:

- `fast_mode_state` and `fast_mode_disabled_reason`: whether fast mode was applied, and why not.
- `usage.speed`: `fast` or `standard`, the speed the request was actually served at.
- `modelUsage`: the model that answered, which shows a substitution.
- `is_error`, `api_error_status`, `result`: the failure, if any.
- `total_cost_usd`: what the request cost at list price.

The request runs in an empty folder with no tools, MCP servers, CLAUDE.md, skills, plugins or saved session, and without the caller's gateway and session variables. So a difference from a fleet thread's result points at the gateway, T3 or the profile's own settings. Each request is logged with its reason and sender in `state_dir/claude-debug/requests.jsonl`.

Report the result to the owner with the fields that answer the question and the cost.
