# Skill bank

The bank keeps 23 base fleet skills and three general assistant skills, for 26 bundled skills. Canvas LMS is an optional planning capability guide. OpenClaw visual canvas/widgets are a separate recipe.

Use `python3 scripts/bank.py list` to see selectable modules, skills and presets. Base sources remain in `skills/` and `vendor/skills/`; optional assistant sources live in `bank/skills/`. Provider-owned skills are cataloged rather than copied.

| Skill | What it does | Setup notes |
| --- | --- | --- |
| baseline-ui | Applies UI conventions | No fleet service required |
| frontend-design | Builds and styles components and app UI | Uses the project's tools |
| design-taste-frontend | Art-directs landing pages, marketing sites and app screens, then checks renders in a real browser | Vendored tasteskill v2 (MIT); Node 22 and Chrome for its inspector |
| fixing-accessibility | Finds and fixes accessibility issues | Uses the project's tools |
| fixing-motion-performance | Fixes animation performance | Uses the project's tools |
| unslop | Edits writing into plain language | No fleet service required |
| resume-craft | Writes and reviews resumes and application material | Kept for future job changes, even with a full-time role |
| [job-apply](../skills/job-apply/README.md) | Tailors a resume, fills an application draft and builds a screenshot review before approved submission | Your own private profile answers and a browser tool your agent can drive |
| task-to-review | Takes code work through checks and a reviewable result | Optional fleet task and lease helpers |
| fleet-operate | Maintains configured fleet profiles and machines | Read your own fleet.json |
| remote-handoff | Prepares work for another configured worker | Optional SSH worker; Windows display needs interactive login |
| fleet-secrets | Stores and fetches keys and tokens in the fleet vault | `secrets` section in fleet.json |
| fleet-debug | Sends one tiny direct request to a Claude login without the gateway, only when asked | Claude-only; `debug.claude` section in fleet.json |
| [orchestrate-trifecta](../skills/orchestrate-trifecta/README.md) | Coordinates builds and reviews across configured accounts, with builder failover across usage windows | T3 tools, your own `trifecta.json` and an optional CLIProxyAPI usage gateway |
| codex-image | Generates and edits raster images through Codex | Older Claude-only personal bridge, Mac/POSIX and native Codex |
| codex-3d | Creates 3D artifacts through Codex | Older Claude-only personal bridge; Blender for relevant modes |
| codex-computer-use | Runs visual browser QA through Codex | Older Claude-only personal bridge; browser dependencies |
| brag | Turns a project into a launch video | HyperFrames and its documented media dependencies |
| hyperframes-core | Defines renderable compositions | HyperFrames |
| hyperframes-creative | Plans video design, audio and beats | HyperFrames |
| hyperframes-animation | Builds seek-safe animation | HyperFrames |
| hyperframes-keyframes | Adds camera moves and keyframes | HyperFrames |
| hyperframes-cli | Runs validation, preview and render workflows | HyperFrames CLI; optional cloud services require your own accounts |

The six video skills and design-taste-frontend are vendored with their assets and original notices. `skills/external.lock.json` pins their unchanged content. The bridge skills retain their scripts, examples and explicit specialized model choices. Their old standalone installers are reference material, not required for fleet setup.

Built-in, plugin and organization-managed skills are intentionally left with their providers. The legacy coding checkouts contained the same nine imported custom skills. Optional assistant skills cover Canvas planning, shared resources and form review.

| Optional OpenClaw skill | What it does |
| --- | --- |
| canvas-planning | General planning and organization options through your own supported Canvas integration |
| shared-resource-review | Reads embedded guides and checks deliverables |
| survey-requirements-review | Reviews form requirements and prepares an offline checklist |

See [provider skills](modules/provider-skills.md) for built-ins, synced tools, plugins and the general OpenClaw capability catalog. Older bridge helpers and deprecated setup records remain available.
