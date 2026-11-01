---
name: orchestrate-trifecta
description: Run a build as a multi-account workflow on T3 Code's orchestrator. This thread coordinates, builder accounts build, and reviewer accounts review (by default 70% GPT, 30% Claude), all through delegate_task, with builders moving to a fallback account while a usage window is closed and back when it reopens. Roles come from trifecta.json. Use when the user says "/orchestrate-trifecta", "trifecta", "use my workflow", "orchestrate this", gives a long or multi-part build goal, or asks for builders and reviewers. This is the default for any work big enough to delegate. Do NOT use for a quick edit or a question you can answer yourself, for work that belongs to an account the config does not name, or from inside a delegated child task.
---

# Orchestrate trifecta

A standing workflow for work big enough to hand out. Several accounts, one
role each, so that no single limit stops a run. The roles live in a config
file, not in this skill.

You are the coordinator. If this thread is not on the coordinator account the
config names, say so and ask before going on. If you are a delegated child, do
the task you were given and do not start this workflow.

`USAGE` below means `python3 ~/.claude/skills/orchestrate-trifecta/scripts/usage.py`.
Use the skill's real location if it is installed elsewhere.

## Load the config first

Run `USAGE --check-config` at the start of every run. It finds the config at
`TRIFECTA_CONFIG`, or `trifecta.json` next to this file, validates it and
prints the role table. If it reports a missing or invalid config, tell the
user what it printed, point them at `trifecta.example.json` and the README,
and stop. Do not guess provider ids.

From the table, note for the whole run:

| Config | Meaning | Example |
| --- | --- | --- |
| `coordinator` | The account this thread runs on | the owner's main Claude account |
| `builders` | Builder accounts in order: primary first, then fallbacks. Each has a provider instance, model, options and `switch_at_percent` (default 85) | `builder1` a builder seat, `builder2` the coordinator's own account |
| `reviewers` | Reviewer accounts with a `weight`, defaulting to 7 for a Codex/GPT reviewer and 3 for a Claude reviewer | `reviewer1` GPT at weight 7, `reviewer2` Claude at weight 3 |
| `max_parallel_builders` | Builders at once (default 2) | 2 |
| `never_use` | Provider instance ids never to start anything on | direct logins kept for manual checks |
| `stale_minutes` | Age after which a reading is STALE (default 20) | 20 |

Role ids are `coordinator`, `builder1`, `builder2`, ..., `reviewer1`, ....
Everywhere below, "the primary builder" is `builder1`, "the fallback" is the
next builder in order, and each role's provider instance, model and options
are the ones in the table.

Why the order matters, with the example config: a second Claude seat with a
short usage window goes first, so the coordinator's budget is not spent on
building. The coordinator's own account is the
fallback; it is there to be used while the primary's window is closed, and it
is not first only so that it is not spent needlessly. Order your own builders
by the same logic: the account whose limit costs least to exhaust goes first.

## Before the first delegation

1. Call `orchestrator_capabilities` (its output is large: it lands in a file,
   query it with `jq`). Confirm every provider instance in the config exists
   with `canRunCrossProviderChildTask: true`, and take model and option ids
   from it. If an id in the config no longer matches, trust the live catalog
   and tell the user. Never use an instance listed in `never_use`, even when
   the catalog shows it as available.
2. Run `USAGE` and note each role's reading.
3. Write down what done means for this run, as checks you can run, and where
   the run's state lives: one progress file a successor could resume from.

## Your job as coordinator

The coordinator account pays for every token you read, and its longest
usage window, if it has one, is the one that can end a run for days. Spend it on decisions,
not on work:

- Plan, split, brief, merge, run checks in the shell, decide. Do not write
  the feature code yourself and do not start native Claude subagents or
  workflows; those bill the coordinator account. Everything goes out through
  `delegate_task`.
- Do not open images or read large files a child can summarize. Ask for the
  answer in words with file paths and line numbers.
- Keep the progress file current after every merge and every review, with
  each `taskId`, what it was for, the role and account it ran on, and its
  outcome.

## Delegating

- A child gets no history. Each task prompt carries the goal, what done
  means, the absolute worktree path, the files that matter, the checks to
  run, the repository's commit rules, and what to report back.
- `mode: "async"` for real work. A finished child wakes this thread on its
  own, so end the turn instead of polling. `mode: "wait"` only for a result
  you need within the same turn. `task_status` reads one mid-turn;
  `task_cancel` stops one that has gone wrong.
- A new `clientRequestId` for every task and every review round, kept stable
  across retries of that same call.
- At most `max_parallel_builders` builders at once unless the user sets
  another number. Builders that run in parallel each get their own git
  worktree, created by you (`git worktree add`), with its path in the
  prompt. Two builders never share a checkout.
- Set `role`: `implementation` for builders, `review` for reviewers. Pass the
  role's provider instance, model and options from the config as the target.

## Builders and their usage windows

New builders go to the first builder in order that is open. Routing builders
through a gateway that records usage is what makes their windows visible:
the gateway keeps the rate-limit headers of every response, and `USAGE`
reads them.

- Run `USAGE` before each wave of builders and whenever a builder fails.
- Below its `switch_at_percent`: build on that builder.
- At or above it (`USAGE` prints AT THRESHOLD): start nothing large there.
  Let running builders finish.
- Exhausted (a child fails with a rate or usage limit error, or the status
  reads rejected): do these three things at once, so no work waits.
  1. Record in the progress file the time it ran out and the reset time
     `USAGE` gives.
  2. Carry on building on the fallback at the same pace and the same number
     of builders. Re-send any builder task the limit cut off.
  3. Set the clock: run `USAGE --sleep-until-reset builder1` (the role that
     ran out) as a background command with the longest timeout allowed. It
     sleeps until a minute after the latest reset among the windows that
     block the role, whichever ran out, and its exit wakes this thread.
     Timers such as ScheduleWakeup do not fire in a T3 thread; a background
     command does. Exit code 3 means the reset is still ahead: run it again.
     Each time you start it, call `t3_background_wait` with its task ID
     before ending the turn. Unmarked, T3 takes the sleep for a leftover
     command and shows the thread as finished instead of Working.
- When the clock goes off: send a one-line task to that builder's provider
  instance to confirm it answers, read `USAGE`, and from then on every new
  builder goes to it again. Builders already running on the fallback finish
  where they are. Record the switch back.
- Only one clock at a time, on the earliest builder in order that is closed.
  If this thread is resumed after a break, check the progress file for a
  recorded reset time and compare it with `date` before choosing an account.
- If every builder is closed, nothing can build: keep reviews and checks
  going, leave the clock running, and tell the user.
- If a builder's gateway itself is failing (errors that are not limits),
  carry on building on the fallback as in step 2 and tell the user. Never
  fall back to an instance in `never_use`.

A reading older than `stale_minutes` is marked STALE. A builder's reading
refreshes whenever a builder runs on it. To refresh another role, send a
one-line `delegate_task` to its provider instance; do this only when the
number changes a decision.

## Reviews

Nothing a builder produces is accepted on its own word. Every unit of work
gets an independent review before it merges.

- Split reviews by the reviewers' weights. With the default 7 and 3, seven
  of every ten reviews go to the GPT reviewer and three to the Claude
  reviewer. Keep the tally in the progress file. With one reviewer per round,
  default to the heaviest reviewer and give the others the rounds where a
  second kind of reader matters most: the final pass, a finding the builder
  disputes, anything the main reviewer passed that still looks wrong to you.
  With several reviewers on one round, split that round by weight.
- If a reviewer's account is exhausted, its share goes to the remaining
  reviewers, not to the coordinator's account.
- A reviewer is never the builder of the thing it reviews, and it gets the
  work, not your opinion of it: the brief, the diff or the artifact paths,
  and what done means.
- Ask for defects, not a score: up to five, ranked, each with the file and
  location, what is wrong, and why it matters. Scores do not converge.
- One review, the fixes, one confirming review, then close the item. Each
  round is a new `delegate_task` whose prompt carries the original brief, the
  earlier findings, what was done about each, and any objection still open.
  Do not continue a review by messaging the child's thread.
- A reviewer that can read images can check screenshots and renders. Give it
  file paths; do not look yourself.

## Running to the end

The user starts a run and expects not to hear about it until it is done. A
run that stops early and waits for them is the failure this workflow exists
to prevent.

- No time caps. Do not write "stop after N hours" into a goal, a plan or a
  child's brief. Usage limits are handled by the rules above, not by a clock.
- Never end a turn with nothing in flight before done is met: a child task
  or a background clock must be running, or the next step must be started
  in the same turn. A run whose last turn ends idle has stopped.
- Do not hand the user a choice the brief already answers or that a builder
  and a review can settle. Decide, record why in the progress file, go on.
  Stop only for what only the user can decide: merging to main, spending
  money, anything outward-facing, or a change to what the work is.
- If an item will not pass, keep working it (diagnose, fix, review, measure
  again) until it passes or the evidence shows it cannot without breaking a
  rule. Report the second case at the end with that evidence; do not stop
  the rest of the run for it.
- If the coordinator's longest-window reading reaches 95% before the run is
  done, do not stop: hand coordination to a new thread on the primary
  builder's provider instance through `t3_thread_launch`, briefed to resume
  from the progress file and to follow this skill, and record the handoff
  there.

## Finishing

- Run the checks you wrote down at the start, yourself, on the merged result.
- PRs: follow the repository's rules, call `link_pull_request` for every PR
  the run creates, and keep questions and asks for the user out of the PR.
- Report to the user: what is done and verified, what is left, the review
  tally, and every role's usage reading at the end.

## Rules that do not bend

- Use only the accounts the config names. Keep employer and other accounts,
  and their work, separate. Never start anything on an instance in
  `never_use`, even when the catalog lists it.
- Never restart T3, change its providers, or alter remote-access services
  from a run. Follow the repository's and machine's AGENTS.md for the rest.
- Moving builders to the fallback while an earlier builder's window is
  closed, and back when it reopens, is the one account switch this workflow
  makes. Any other limit is reported, not routed around.
