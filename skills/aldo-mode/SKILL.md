---
name: aldo-mode
description: Single front door for Ali's agentic workflow. Inspect the request, pick the right skill or workflow (orchestrate-trifecta for multi-part builds, task-to-review for scoped changes, remote-handoff for Windows/GPU or remote workers, design-taste-frontend or frontend-design for UI, Codex bridges for raster art, 3D or visual QA, unslop for writing, fleet-secrets for keys, fleet-operate or fleet-debug for fleet ops), and drive it to a verified result. Use when the user invokes /aldo-mode or asks you to handle a task end to end with this stack.
---

# Aldo mode

One front door for the whole stack. Read the user's request, pick the right workflow or skill from the table below, and carry the task to a verified result without making the user memorize every skill name.

If you are already running inside a delegated child task, do the task you were given instead of starting a new orchestration loop.

## Route the task

Pick the smallest route that fits the request. Combine skills when a task clearly crosses two areas (for example, `design-taste-frontend` plus `codex-computer-use` to build and visually verify a landing page, or `orchestrate-trifecta` calling builders that follow `task-to-review` and `unslop`).

| Request shape | Route to | Done means |
| --- | --- | --- |
| Long, multi-part, or overnight build in T3 Code with multiple accounts configured | `orchestrate-trifecta` | Merged worktree changes, independent reviews (70/30 GPT/Claude), passing checks, progress file updated, final report with usage readings |
| Single scoped feature, bug fix, refactor, or PR prep | `task-to-review` | Separate branch and worktree, targeted checks green, reachable preview or screenshot if UI changed, clean reviewable diff |
| Native Windows, GPU, or display work on another configured host | `remote-handoff` | Target SSH verified, work placed in the target's work root, interactive session noted for display checks, actual output reported |
| Landing page, marketing site, portfolio, product page, or page redesign | `design-taste-frontend` | Direction chosen, built, checked in a real browser via its inspector and screenshots |
| App component, dashboard, form, or UI styling | `frontend-design` + `baseline-ui` | Clean component or view with no generic slop, verified against the running app |
| Accessibility or animation jank | `fixing-accessibility` / `fixing-motion-performance` | Root cause fixed and verified |
| Real raster artwork (hero image, illustration, cutout, device mockup) | `codex-image` | PNG generated and integrated; no hand-rolled SVG fakes |
| Real 3D object, Blender GLB, or animatable SVG with named pivots | `codex-3d` | Built with GPT-6.2 Sol at max reasoning, rendered, inspected, contract checked |
| Visual QA or design comparison on a running web page | `codex-computer-use` | Headless Chrome pass with screenshots and defect evidence, fixed and re-checked |
| Launch video or motion reel for a project | `brag` + `hyperframes-*` | Validated HyperFrames composition and rendered video |
| Writing, docs, README, commit prose, or PR body | `unslop` | Plain human prose with AI tells stripped |
| Resume writing, tailoring, or drafting a job application | `resume-craft` / `job-apply` | Verified PDF, saved draft, and HTML screenshot review gallery before any submission |
| Creating, rotating, or fetching API keys, bot tokens, or webhooks | `fleet-secrets` | Vault checked first, new keys saved via stdin with a note, values kept out of transcripts and Git |
| Fleet machines, skill sync, gateways, or direct gateway-free Claude checks | `fleet-operate` / `fleet-debug` | Validated `fleet.json`, `fleet doctor` or `fleet gateway-status`, or one spend-capped `fleet claude-debug` probe when explicitly asked |

If the user is setting up this repo for the first time on their own machine, walk them through `scripts/fleet init`, `scripts/fleet config check`, and `skills/orchestrate-trifecta/scripts/usage.py --check-config` instead of assuming anything is preconfigured.

## Standing rules

These hold on every route, whether one skill or five run:

1. **Keep identities separate.** Never route employer work through a personal gateway or the personal Codex bridges (`codex-image`, `codex-3d`, `codex-computer-use`). Never start work on a provider listed in `never_use` or `debug.claude`.
2. **Isolate code changes.** Use a separate branch and worktree for code work. Leave unrelated agents, T3, and network routes alone.
3. **Prove it before reporting done.** Run the tests, build checks, or browser inspection that would actually catch a regression. If a check cannot run here, say so plainly.
4. **Write like a human.** Apply `unslop` to user-facing docs, PR descriptions, and replies. Never put questions or asks for the owner inside a PR body or commit message.
5. **Save keys right away.** Check `fleet secret list` before creating a key or asking for one, and store new keys through `fleet secret set` over stdin.
6. **Respect the paging boundary.** Use `fleet page` only when stopped on a blocker only a human can clear in person (UAC prompt, OS password, disk unlock, 2FA) or when `orchestrate-trifecta`'s `usage.py` flags a weekly window at or below its threshold. Never page for finished work, normal errors, or questions.
7. **Stop at outward boundaries.** Do not merge to main, push to a public remote, submit an application, spend money, or message other people unless the user explicitly authorized that action.
