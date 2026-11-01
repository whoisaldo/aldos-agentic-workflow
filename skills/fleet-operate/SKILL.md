---
name: fleet-operate
description: Inspect or configure the user's fleet machines, profiles, shared skills or gateway. Use for fleet maintenance and machine routing, not ordinary application edits.
---

Find the fleet checkout from the current project and read its `fleet.json`, `AGENTS.md` and `README.md`. Resolve actual hosts and account homes there; never assume the starter addresses are live.

Preserve unrelated agents, native profiles, existing T3 sessions and provider entries. Do not restart T3 or change Tailscale Serve as a side effect. Route by required OS and selected account. Employer work stays in the approved employer environment; quota exhaustion is reported without switching identities.

Route Windows/GPU/display work to a configured Windows worker and run display tests through its interactive launcher. The direct (non-gateway) Claude logins under `debug.claude` never take work; a check without the gateway that the owner asks for goes through `fleet claude-debug`. Run `fleet config check` after editing `fleet.json`.

Change source in fleet and run deployment tests. Preview a profile with `scripts/fleet diff --profile NAME`. Publish a content-addressed local release with `fleet release`. `fleet sync` previews skill-only changes; `fleet sync --apply` updates fleet-owned links with backups while keeping old immutable releases. Where `fleet.json` configures `sync`, the synchronization worker deploys published releases to reachable machines without restarting agents or changing settings, logins, histories or T3 providers. `fleet sync --check` and `fleet doctor` report missing and outdated skills. Use ordinary profile apply only for a new or unused managed home. For an authorized idle migration, follow `docs/maintenance.md`: `scripts/migrate-skills.py --apply --idle` checks live T3 activity first, and only the maintenance thread itself may be exempted with `--allow-thread ID`. Preserve histories and settings, and back up changes. Credentials stay outside Git; gateway sign-ins are fresh, never imported from active native accounts. See `docs/fleet-cli.md` for every command.

Return the affected host, exact files or services changed, validation and any remaining setup. Do not claim an untested remote deployment succeeded. Follow the owner's existing authorization; do not ask a second time for a change already approved.
