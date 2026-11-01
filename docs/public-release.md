# Keep public source clean

The bank contains recipes, generic configuration and selected skills. Store real account settings, Canvas/Notion configuration, credentials, session state, device information, logs, backups and personal data outside the checkout. Personal spreadsheets and health exports are excluded.

Before committing, review the full diff, including added binary assets. Stage only intended source, refresh the inventory, then run:

```sh
python3 scripts/index-bank.py
git add bank/files.json
python3 scripts/check-public.py --history --secrets
python3 -m unittest discover -s tests -v
```

To catch names and terms that only you know are private, add `--denylist FILE` (see below).

Install [Gitleaks](https://github.com/gitleaks/gitleaks) first; on macOS use `brew install gitleaks`. The `--secrets` flag requires it and fails if it is unavailable. CI uses version 8.30.1 with the published release checksum. The custom check scans tracked bytes, validates inventory hashes, and flags personal home paths, private tailnet details, real-looking email addresses, example session IDs and private state files. Findings print locations, not matched values. Secret scanning supplements that check using Gitleaks' default rules; the checker reads Gitleaks' JSON report and withholds its own output, which names authors and emails. Errors name files through the same redaction, and git's and Gitleaks' error text is withheld.

## Private deny-list

Generic rules cannot know your surname, school, employer, relatives, machine names or bot names. Keep those in a private deny-list file and pass it with `--denylist FILE`, or set `PUBLIC_CHECK_DENYLIST=FILE`. Each line is one Python regular expression, matched case-insensitively. Lines starting with `#` and blank lines are ignored. For example, `\bacme\b` would catch a whole-word employer name. The real file holds your own terms.

Keep the deny-list outside Git and outside this checkout, for example in `~/.config/public-check/denylist.txt`. Committing it, or quoting its terms in an issue, PR or test, would publish exactly the words it exists to keep out. For the same reason a finding names only the file, the line and the deny-list line number of the pattern (`denylisted-term (deny-list line 4): docs/x.md:12`); it never prints the matched text or the pattern. When a path itself holds a denied term or a real-looking email address, every finding prints it as its clean leading directories plus `<redacted name HASH>`, where HASH is the first ten hex digits of the path's SHA-256. The rule applies to file paths and text in the current files and, with `--history` or `--range`, to the scanned revisions and to their commit messages and author names. It also applies, with the email rule, to ref names: each `--ref-name NAME` (repeatable) and, with `--history`, every local branch and tag name. A failing ref name prints only as `<redacted ref HASH>`. Binary files (any NUL byte) are skipped for this rule, because decoded media bytes produce random false matches.

## Email addresses

Tracked text outside `vendor/` and license files may contain only placeholder addresses: `example.com`, `example.org`, `example.net`, `example.edu`, the reserved `.example`, `.invalid`, `.test` and `.localhost` domains, `users.noreply.github.com`, and the short allowlist in `scripts/check-public.py`. Anything else that looks like a real address is a `personal-email` finding, in a file's text or in its path.

With `--history` or `--range`, the author and committer email of every scanned commit must be a GitHub private relay (`*@users.noreply.github.com`) or `noreply@github.com`. Add more with `--allow-email PATTERN` (repeatable, glob). A finding reports the commit's short SHA and whether the author or committer address failed, not the address. Set your repository's email to your GitHub noreply address before the first commit (`git config user.email ID+USER@users.noreply.github.com`); fixing an address later means rewriting history.

## Pre-push hook

`.githooks/pre-push` checks what a push would publish, judged by what the destination itself lists. It hands the checker the push URL git gives it (the `pushurl` when one is set) as `--destination`, and the checker runs `git ls-remote` on it and counts as published only the listed commits that also exist in this clone. Local tracking refs are not trusted: they can belong to a nested remote name (`foo/bar` under `foo`), come from a fetch URL that differs from the push URL, or be stale. A listed commit this clone lacks proves nothing, so the commits it would have excluded are checked.

A destination that is neither a URL (`scheme://...`) nor scp-like (`host:path`, with no slash before the colon) is a local path. The checker makes it absolute against the hook's working directory, the top of the checkout, which is where `git push` resolves it too; a relative `destination` can therefore never be read as a remote of that name. If `git ls-remote --get-url` shows that git would list a different repository for the value, because a remote has that very name or a URL rewrite matches it, the listing is not used.

The listing never prompts and is bounded. It runs with `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS` and `SSH_ASKPASS` set to `false`, `GCM_INTERACTIVE=never`, no terminal and no input, and its ssh command (`GIT_SSH_COMMAND`, else `core.sshCommand`, else `GIT_SSH` or `ssh`) gets `-o BatchMode=yes -o ConnectTimeout=10`. It is stopped after 30 seconds (`--destination-timeout SECONDS`). Configured credential helpers still answer, but nothing asks you for a password. The listing's commits are checked in one `git cat-file` batch and excluded through `git rev-list --stdin`, so a destination with thousands of refs costs seconds, not minutes.

When the destination cannot be listed (offline, refused or missing credentials, a missing path, a timeout, or a name git would send elsewhere), the checker says so briefly and fails closed: nothing counts as published and everything the pushed tips reach is checked, which can block a push on old history that is in fact public until the destination is reachable again. The same holds in a shallow clone or one with a grafts file, whose local history cannot prove what the destination holds.

One assumption remains: a listed commit is taken to come with its whole history, as `git push` itself assumes. A shallow destination may lack the older commits, but `git push` does not send them either. Local replace refs (`git replace`) are ignored by every git call the hook and checker make, as `git push` ignores them; grafts and the shallow file are honoured, as `git push` honours them.

The hook skips deletes. For the other pushed refs, whether updates, force pushes, new branches or unfetched remote tips, it passes `--destination`, one `--range` of the pushed tips and the local and remote name of each ref as `--ref-name`. It runs `python3 scripts/check-public.py --secrets` with those arguments, adds `--denylist PATH` when it knows a deny-list, and blocks the push if the check fails. Gitleaks scans the pushed commits' diffs plus, in one batch, their messages, the pushed ref names and any annotated tags. Neither the hook nor the checker prints the destination or git's messages, since either can hold credentials; `git push` itself still prints the destination URL or path, with any user name and password removed, in its own `To ...` and error lines. Already-published history and local refs that are not pushed do not block it. Enable it once per clone:

```sh
git config core.hooksPath .githooks
git config publiccheck.denylist ~/.config/public-check/denylist.txt   # optional
```

The hook takes the deny-list path from `PUBLIC_CHECK_DENYLIST` when that variable is set, and otherwise from `git config --get publiccheck.denylist`. Repository-local Git config lives in `.git/config`, which is never committed, so the path stays on your machine. Set it with `--global` instead to use one deny-list for every clone. The environment variable still works and wins when both are set:

```sh
export PUBLIC_CHECK_DENYLIST=~/.config/public-check/denylist.txt   # in your shell profile
```

The hook needs Gitleaks like `--secrets` does. It catches mistakes; it cannot enforce anything. It sees only the refs and objects git hands it, runs only in clones that enabled it, and `git push --no-verify` skips it. The destination is listed when the hook starts, so a ref someone else pushes before the push lands is not seen. CI runs the same check without your deny-list and is the backstop for secrets and the generic rules. Only the hook knows your private terms, and a deny-list catches only the terms it contains.

## Exceptions and history

The only Gitleaks exception is a SHA256 value in `bank/files.json` under the generic key rule. The checker verifies every inventory value against actual source bytes; other rules and other files remain enabled. Ignored local files never enter the scan snapshot or an exported kit.

`--history` checks every revision reachable from `HEAD`, including data deleted from the latest files. It never scans every ref, so local checkpoints (such as T3's `refs/t3/...`) and unrelated branches stay out. To audit other refs, name them: `--range REV_RANGE` (repeatable, git rev-list arguments such as `origin/main..HEAD` or `my-branch`) checks only those revisions and their commits, without the working tree. A file a range keeps unchanged from its base is reported again unless `--published OID` (repeatable, an object name or ref the destination already has) reaches that base; a range base alone proves nothing. With `--secrets`, Gitleaks reads the range's diffs and, in one stdin batch, every scanned commit's message, the ref names and the annotated tags. A diff does not show a file kept unchanged or renamed from a base, so when no `--published` value reaches a base, Gitleaks also scans each file of the range whose content that base holds, at its own path. Gitleaks' git scan shows no diff for a merge either, so for every merge it scans, under `--range` and `--history`, Gitleaks also scans each file whose content none of the merge's parents holds at that path: a conflict resolution or a file only the merge adds, even when the next commit deletes it. The custom checks read every file of every scanned revision, merges included.

Annotated tags are checked as objects. A tag a range names, and with `--history` each tag under `refs/tags` that leads into `HEAD`'s history, has its tagger email held to the commit email rule, its name and tagger name to the deny-list, and its message and signature block to the deny-list and the content rules; Gitleaks scans its text. A tag of a tree or blob has that object checked too. Lightweight tags name commits that are already checked. Each unique file version is read and scanned once, however many revisions contain it. Removing a file in a new commit does not remove its old contents. History cleanup requires coordinated replacement of affected refs and fresh clones for collaborators. Copies, forks and hosting caches can outlive that replacement. If an actual credential was exposed, revoke or rotate it with its owner; rewriting Git is insufficient.

Automated checks do not prove that every document or media file is appropriate to publish. Review screenshots, metadata and third-party licensing manually. Keep upstream credits and license notices. Private repository provenance and deployment records are omitted; hashes in the historical source manifest describe imports before public adaptation.
