---
name: codex-computer-use
description: Hand a running web UI to GPT-6-Astra at max reasoning, through the Codex CLI, so it drives the page in a real headless Chrome, screenshots it, looks at every screenshot, and reports check results and defects with evidence. Use after building or changing UI, when asked to verify, QA, test, "check that it works", "does it look right", compare a page against a design, or get visual feedback on a page, form, flow or dashboard that is running at a URL. Use it as the verifier in a build-verify-fix loop - Claude writes the code, Astra checks it. Do NOT use to generate images (codex-image), build 3D or vector objects, write unit or e2e test files, review source without running it, or drive native macOS apps (Codex's native Computer Use exists only in the desktop app).
---

Fleet execution: use a configured macOS/POSIX host with the user's native personal Codex account. These bridges are for personal work. Employer work must use its approved environment and must not cross into a personal account. Dependencies live outside the immutable release in `~/.cache/agent-skills`, overridable with `AGENT_SKILL_CACHE_DIR`. From Windows, use an explicitly configured Mac worker and copy the artifacts back.


# Codex Computer Use (Astra verifies what Claude built)

Claude is good at designing and writing UI. It is worse at noticing what is
actually wrong once the UI is running: the card that clips at 1440 wide, the
label nobody can read, the button whose click does nothing. GPT-6-Astra at
max reasoning is stronger at exactly that - driving a page, looking hard at a
screenshot, and being stubborn about checking every step. This skill hands
the verification to Astra and gives Claude back a report with evidence, so
the loop becomes: **Claude builds, Astra verifies, Claude fixes.**

What "computer use" means here: Codex's native Browser and Computer Use
tools exist only in the ChatGPT desktop app, not in `codex exec`. So the
wrapper starts a real headless Chrome outside Codex's sandbox with
[agent-browser](https://github.com/vercel-labs/agent-browser), points it at
the page, and lets the sandboxed Astra drive that browser from the shell:
accessibility snapshots with `@refs`, clicks, typing, scrolling, screenshots
it then views with `view_image`. It is the page, not a picture of it.

Auth is the user's existing ChatGPT OAuth login in `codex` - **no API key.
Runs use that Codex sign-in and its quota.** Max-effort runs are slow.

## When to use this skill

Use it when **something is running at a URL and the question is whether it
is right**:

| Situation | Brief |
|---|---|
| Just built or restyled a page, component, or flow | "Verify the new pricing section: three tiers, CTA per tier, no clipping at 1440" |
| A form, wizard, or interaction must work end to end | "Sign-up: fill, submit, expect the success toast; check validation on empty email" |
| Compare the live page with a design | `--reference design.png` "report every deviation" |
| Responsive or dark-mode sweep | run in parallel with `--viewport 390x844`, `--media dark` |
| Regression check after a fix | narrow brief: "Re-check only the overflow in the project card" |
| Exploratory QA before handing work to the user | "Explore every nav item and control; report anything broken or odd" |

## When NOT to use this skill

- **Generating a picture** (hero, illustration, mockup) - `codex-image`.
- **Building an object** (three.js module, animatable SVG) - use `codex-3d`
  for Claude and GPT-6.1 Sol to collaborate at max reasoning.
- **Writing tests** (unit, Playwright, Cypress files) - write code; Astra's
  report is evidence for you, not a test suite.
- **Reviewing source without running it** - read it yourself or use
  `/code-review`.
- **Native macOS apps or the desktop itself** - not available headlessly.
  Electron apps are reachable via agent-browser's `electron` skill and a CDP
  port, but that is a manual setup, not this wrapper.
- **A ten-second look** - if you only need one screenshot to check a colour,
  the T3 Code preview tools or agent-browser by hand are faster. Reach for
  Astra when judgement is needed: flows, layout at real sizes, "is this
  actually good".

**Never** substitute a report for looking. Read the screenshots Astra saved
before you act on its findings, and check any finding that contradicts what
you see.

## Usage

```bash
python3 ~/.claude/skills/codex-computer-use/scripts/codex_computer_use.py \
  --url http://127.0.0.1:3000/pricing \
  --task "<brief>"
```

Run it from anywhere; everything lands in the run directory it prints
(default `/tmp/codex-computer-use/<run-id>/`): `shots/*.png`, `report.md`,
`report.json`, `prompt.txt`.

| Flag | Purpose |
|---|---|
| `--url URL` | Page to verify. **Required.** Use `127.0.0.1` or the Tailscale IP, never `0.0.0.0`. The wrapper fails fast if nothing answers. |
| `--task "..."` / `--task-file F` | The brief (below). **Required.** |
| `--viewport WxH` | Default `1440x900`. `390x844` for phone, `1024x768` for tablet. |
| `--media dark\|light` | Emulate `prefers-color-scheme`. |
| `--reference IMG` | Design reference to compare against. Repeatable. |
| `--project DIR` | Lets Astra *read* the source for intent. Never written. |
| `--effort` | `max` (default). `high` for quick regression re-checks. `ultra` exists but adds delegation, not accuracy. |
| `--out DIR` | Custom run directory (e.g. inside the project's gitignored scratch). |
| `--timeout` | Seconds, default 1500. |
| `--json` | Machine-readable result on stdout. |

**Exit code is the verdict:** `0` pass, `1` fail, `2` blocked (Astra could
not perform the checks), `3` error (server down, Chrome, Codex, no report).

**Budget 3-10 minutes at `max`.** Run it in a background Bash call and keep
working; the log prints every command Astra runs and a heartbeat every 15 s.
Independent runs (other viewports, other pages) can run in parallel - each
has its own browser session, socket and Codex thread.

## Writing the brief

Astra checks what you tell it and then looks around on its own. The brief
should read like a hand-off to a QA engineer who has never seen the feature:

```
What changed: <one or two sentences on what was built and why>
Checks:
  1. <observable expectation, with exact text/labels/values>
  2. <interaction: do X, expect Y>
  3. ...
Pass criteria: <what "done" means; anything that must be pixel-exact>
Ignore: <known issues, unfinished areas, noise to skip>
```

Rules that change the quality of the result:

- **Quote exact strings.** "status reads exactly 'Invite sent to
  ali@example.com'" is checkable; "the invite works" is not.
- **Say what the page is for and who uses it.** Astra judges polish against
  that, not against a generic standard.
- **Name the viewport reason.** "Marketing page, most traffic is mobile" makes
  a phone-width run the one that matters.
- **Attach the design** when one exists (`--reference`). Deviations from a
  real reference are far more actionable than taste.
- **Keep one run to one page or flow.** Two pages means two runs in
  parallel; a sprawling brief produces a shallow report.

See `references/briefs.md` for copy-paste briefs and failure-mode fixes.

## Workflow

1. **Build.** Do the UI work first; this skill checks running code.
2. **Serve.** Start the dev server bound to `0.0.0.0` (so the user can also open
   it at `http://<configured-preview-host>:<port>`), and pass `http://127.0.0.1:<port>/...`
   to `--url`.
3. **Run in the background** with a specific brief. Keep working, or start
   the other viewports in parallel.
4. **Read `report.md` and look at every screenshot** with the Read tool.
   Findings are evidence-backed, but the screenshots are the evidence.
5. **Fix what is real.** Change the code, keep the server running.
6. **Re-run with a narrowed brief** ("re-check only ...") at `--effort high`
   until the verdict is `pass` or the remaining findings are ones you and the user
   accept.
7. **Report to the user**: the URL on the Tailscale IP, the verdict, what was
   fixed, what remains, and the paths of the screenshots that show it. He is
   not at this machine; nothing opened in a GUI here is visible to him.

## Reading Astra's report

`checks` mirrors the brief one to one: `pass` / `fail` / `blocked` with the
evidence quoted. `findings` is what Astra noticed beyond the brief, with a
severity, the screenshot that shows it, and a `likely_cause` in CSS/markup
terms when it could tell. `console` is the page's own errors. A `blocked`
verdict means the checks could not be run at all (page did not load, login
wall, driver failure) - fix the environment, not the UI.

Treat `blocker` and `major` as must-fix, `minor` as should-fix, `nit` as
taste to weigh yourself. Astra is thorough, not infallible: when a finding
does not match the screenshot it cites, trust the screenshot.

## Failure handling

- **`nothing answered at <url>`** - the server is not up, or you passed
  `0.0.0.0`/a port that is not listening. Start it, check `lsof -nP -iTCP:<port>`.
- **`agent-browser could not start Chrome`** - run
  `~/.cache/agent-skills/codex-computer-use/agent-browser-0.38.0/node_modules/.bin/agent-browser install`
  once; it uses the installed Google Chrome when it finds one.
- **`did not create its socket in the run dir`** - a newer agent-browser
  stopped honouring `AGENT_BROWSER_SOCKET_DIR`; pin the version in the script
  or check its `--namespace` option.
- **Verdict `blocked` with "session"/"socket" in the summary** - the
  sandboxed Codex client could not reach the browser; re-run, and if it
  repeats, check that the run dir is on a local disk.
- **Timed out** - max effort on a long brief can exceed 25 min; raise
  `--timeout` or split the brief.
- **Auth errors** - tell the user to run `codex login`. Never ask for an API key.
- **Report says pass but you can see a defect** - the brief did not point at
  it. Add it as an explicit check and re-run; do not argue with the report,
  fix the code.
