# Optional Windows deployment

Start with a configured Mac gateway and reviewed skill release. This workflow expects `D:\AgentWork`, Python 3.11+, Node and npm on your Windows worker. The included bootstrap installs isolated CLIs and preserves global installations. Windows execution has not been revalidated for this edition.

On the Mac, export the current verified release to a new archive:

```sh
python3 scripts/portable.py export --output ~/Services/fleet-share/distributions/skills.zip
```

The command writes a `.sha256` companion and refuses to overwrite an archive. Transfer that archive, its checksum and this shared source snapshot to the worker. Do not transfer the private fleet's history, profile directories or sessions.

Stage only your own gateway client key in a private `gateway-keys/personal.json` file containing an `api_key` field. Never transfer gateway management keys, OAuth files or native credentials. These are personal client profiles; employer work belongs in its approved environment.

If the checkout is `D:\AgentWork\fleet`, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\AgentWork\fleet\scripts\bootstrap-windows.ps1 `
  -ReleaseArchive D:\AgentWork\fleet-staging\skills.zip `
  -Sha256 (Get-Content -Raw D:\AgentWork\fleet-staging\skills.zip.sha256).Trim() `
  -GatewayKeyDirectory D:\AgentWork\fleet-staging\gateway-keys `
  -Profiles fleet-codex,fleet-claude
```

Use the actual checkout location if different. The bootstrap stores profiles under `D:\AgentWork\fleet-state` and launchers under `D:\AgentWork\bin`. It verifies the archive and preserves installed profiles, differing keys, launchers and toolchains. It leaves PATH and native profiles alone.

Run `D:\AgentWork\bin\fleet.cmd doctor --profile fleet-codex --profile fleet-claude`. If using T3, add providers through its authenticated supported settings API after inspecting the current entries. `scripts/t3-api.mjs` supports the local server and a private session file; it does not supply your session automatically. Do not launch another backend against a live T3 home.

Native GPU/display checks require the owner's logged-in interactive session. This bootstrap does not create or restart interactive scheduled tasks. The [legacy Windows record](legacy-windows-deployment.md) keeps the original deployment details for reference.
