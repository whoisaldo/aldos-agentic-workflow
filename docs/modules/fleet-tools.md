# Fleet tools

Choose this for task worktrees, build leases, pinned skill releases or isolated managed profiles. No gateway is needed to create a task or acquire a build lease. Applying the included managed profiles does require the proxy recipe and your own keys.

Edit fleet.json's owner, hosts, paths and protected services before actual setup. The default config describes example machines and personal accounts. Remove hosts you do not have. A single Mac can do useful work. `scripts/fleet init` writes a starter file for your own machines and identities, and `scripts/fleet config check` validates it. The [fleet CLI reference](../fleet-cli.md) documents every command and config section, including paging (`fleet page`), the secret vault (`fleet secret`), direct Claude checks (`fleet claude-debug`) and skill synchronization (`fleet sync`).

```sh
scripts/fleet task /path/to/repo fix-export --profile personal-codex --done 'Export works and relevant checks pass'
scripts/fleet lease heavy-build -- your-build-command
```

Task creation branches from committed HEAD and preserves the original checkout's uncommitted changes. It does not launch an agent. Records and worktrees go to configured private state. Leases only coordinate fleet-launched commands; inspect a stale owner's recorded process before clearing it.

For profile installation, read the proxy guide, then inspect `scripts/fleet diff --profile NAME` before applying. Native homes are read-only targets; apply refuses occupied skill directories and profiles with history. Later updates use the authorized idle migration in [maintenance](../maintenance.md).

A bank export preserves selected skill paths and writes a lock containing only the selected vendored skills. `fleet release` then packages the exported top-level custom skills and selected vendor skills. OpenClaw-only optional skills remain under `bank/skills/` and are installed separately; they are not injected into Codex/Claude fleet profiles. Running release on the complete original bank checkout still creates the full base fleet release.

The optional pinned CLI installers do not replace global CLIs. Built-in and organization-managed skills remain provider-owned. [Windows deployment](../windows-deployment.md) is available if your worker uses the expected D: layout. Store credentials and mutable memory outside the checkout.
