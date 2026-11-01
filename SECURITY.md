# Security

## Reporting a problem

Report security problems privately. On GitHub, open the repository's **Security** tab and choose **Report a vulnerability**, which opens a private security advisory visible only to the maintainer. Do not put vulnerability details in a public issue or pull request.

If the Security tab has no **Report a vulnerability** button, private reporting is not turned on yet. Open an issue titled "Private security report" that says only that you have something to report privately. Leave out the file, the problem and any steps. The maintainer will reply on the issue with a private channel, and you send the details there.

Include what you found, where (file and line, or the command and its output), and how to reproduce it. Leave real credentials out of the report, even ones you think are already dead.

## If you think the repository leaked a secret

The repository ships no credentials. `fleet.json` and `trifecta.example.json` hold placeholders and references to vault entries or environment variables, and the public check with Gitleaks runs on every push. Checks can still miss something.

If you see what looks like a live key, token, webhook URL or private address in a file or in Git history, do not copy it, test it or post it anywhere. Report it through the private advisory, or the fallback above, with the file path and commit SHA only. The maintainer will revoke or rotate it with its issuer; removing it from Git alone does not make it safe, because clones and caches keep old history.

If you adopted this setup and committed one of your own secrets, rotate it with its issuer first, then clean your history. [Public release checks](docs/public-release.md) explains the checks and their limits.
