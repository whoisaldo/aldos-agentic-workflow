---
name: codex-3d
description: Collaboratively build and refine 3D geometry and animatable vector objects with Claude and GPT-6.2 Sol through Codex, exclusively at max reasoning. Use for three.js/WebGL objects, GLB/glTF or Blender models, procedural assets, or SVG objects with independently animated parts. Claude develops the brief, reviews the geometry and renders, and integrates the result; Sol reasons through modeling choices and implements or revises the asset. Static pictures belong to codex-image. Excludes UI layout, charts, icon sets, and app screenshots.
---

Fleet execution: use a configured macOS/POSIX host with the user's native personal Codex account. These bridges are for personal work. Employer work must use its approved environment and must not cross into a personal account. Dependencies live outside the immutable release in `~/.cache/agent-skills`, overridable with `AGENT_SKILL_CACHE_DIR`. From Windows, use an explicitly configured Mac worker and copy the artifacts back.


# Collaborative 3D modeling

Claude and GPT-6.2 Sol share the modeling work. Claude brings the project's
context, visual direction and integration constraints. Sol checks the proposed
approach, builds or revises the geometry, and explains consequential decisions.
Claude independently inspects the result and contributes edits or focused
feedback. Either can spot a better approach; evaluate it against the brief and
the renders.

Every Codex call uses `gpt-6.2-sol` with `model_reasoning_effort="max"`, including
revisions and optional self-review. The wrapper rejects other model and effort
values and disables nested agent delegation. If the account cannot use this
model, report the exact error and stop the Codex step. Do not substitute a
model, lower the effort, switch accounts, or change authentication.

Use an up-to-date Codex CLI and the account already logged into `codex`. See the [model and access
notes](README.md#model-and-access) for the verified model settings and sources.

## Object or picture? Decide this first

This skill and `codex-image` are deliberately disjoint. The question is what
the deliverable *is*, not what it looks like.

| The deliverable is... | Skill |
|---|---|
| A 3D object the page rotates, animates, lights in real time, or lets the user orbit (product spinner, drifting car intro, 3D logo, configurator, WebGL hero) | **codex-3d** |
| A 3D asset for a scene or engine (three.js / React Three Fiber component, procedural model) | **codex-3d** |
| A GLB/glTF or `.blend` model, for a page, a game engine, or someone who will keep editing it in Blender | **codex-3d** (`--format blender`) |
| A 2D vector object that must be animated part by part (wheels spin, door opens), recoloured from CSS, or scaled without blur | **codex-3d** (`--format svg`) |
| A static picture: hero art, illustration, texture, product mockup, og:image, empty-state art, even one that *looks* like a 3D render | `codex-image` |
| A flat picture of an object that nobody will rotate or animate | `codex-image` |
| UI, layout, components, CSS | write code |
| A chart of real data | `dataviz` |
| Simple shapes, arrows, dividers, an icon matching an existing set | inline SVG by hand |

> Rule of thumb: if the result will be **looked at**, generate an image. If it
> will be **turned, moved, animated, lit, or placed in a scene**, build an
> object with this skill.

**Never** build an object when the user asked for a picture, and never
generate a picture when they asked for something interactive. If a request
is ambiguous ("put a car on the landing page"), ask whether it should move or
be orbitable; if not, `codex-image` is cheaper and better.

## Usage

```bash
python3 ~/.claude/skills/codex-3d/scripts/codex_3d.py \
  --prompt "<spec>" --out path/to/build-dir
```

`--out` is a build folder (created). Codex is sandboxed to it and cannot
touch anything else in the project. You get:

| File | What it is |
|---|---|
| `object.js` | The deliverable: ES module, `createObject()` returns a `THREE.Group`; optional `updateObject(group, t, dt)`; `meta` |
| `object.svg` | The deliverable with `--format svg`: standalone SVG with `<g id="...">` parts on their pivots |
| `object.glb` + `object.blend` | The deliverable with `--format blender`: binary glTF whose named nodes are the parts, plus the Blender source it was exported from |
| `preview.png` | Three-quarter hero render. **View it.** |
| `sheet.png` | 2x2 contact sheet (three-quarter, front, side, top). **View it too** - a single angle hides broken sides |
| `viewer.html` | Standalone orbit viewer for the object (three.js from a CDN importmap) |
| `notes.md` | Spec plus Sol's build and review reports: part names, animation hooks, compromises |
| `check.mjs` | Contract checker Sol ran (`node check.mjs`); re-run it after hand edits |

Key flags:

| Flag | Purpose |
|---|---|
| `--format three` (default) / `svg` / `blender` | 3D three.js module, 2D vector object, or a GLB modelled in Blender (see "Modelling in Blender") |
| `--passes N` | `1` by default: build or revise, render, and return to Claude for review. `2+` adds Sol self-review before the handoff |
| `--revise "change"` | Change the existing object in `--out` instead of rebuilding. Attaches current renders so Sol sees what it is fixing |
| `--render-only` | Re-render after editing `object.js` by hand |
| `--input FILE` | Reference image (repeatable): a photo of the real thing, a site screenshot for palette |
| `--model`, `--effort` | Locked to `gpt-6.2-sol` and `max`. Other values are rejected |
| `--size WxH`, `--bg HEX` | Render size (default 1536x1024) and background |
| `--blender PATH` | Blender binary for `--format blender`, if it is not in the usual place |
| `--json` | Machine-readable result including measured bounds, triangle count, part names |
| `--timeout` | Seconds per Codex pass (default 1500) |

Run longer jobs in a background shell and keep the user informed. The wrapper
prints a heartbeat every 15 seconds. Previous Astra timings in the examples
are historical, not measurements of Sol at max. Independent objects may use
separate build folders; never have two writers changing the same asset.

## Modelling in Blender (`--format blender`)

Sol models in a real Blender through the Blender MCP server instead of
writing three.js geometry code, then exports `object.glb`. The spec, the
renders and the review loop stay the same. The viewer loads the exported GLB
in three.js, so what you check is what the page will show, not Blender's
viewport.

Pick it when:

- the user asks for Blender, a `.blend`, or a GLB/glTF asset;
- the object is going somewhere other than a three.js page (a game engine,
  AR, a designer who will keep editing it);
- the form needs real modelling tools (subdivision surfaces, bevels,
  booleans, curves) and a `three` build came out faceted or boxy.

Stay with `three`, the default, when the page only needs a drop-in module.
That is a ~20 KB text file with a synchronous `createObject()`, recolourable
in code, with no binary to host and no Blender needed on the machine. The
historical Astra example car came out as a 1.4 MB GLB from Blender.

The script starts its **own throwaway Blender** on a free port for the run,
makes it the only MCP server Codex can see, and kills it afterwards. It never
connects to the Blender the user already has open (the shared one on port
9876), so unsaved work there is safe and Blender builds can run in parallel.
Keep Blender edits inside the wrapper's throwaway instance. Do not use shared
`mcp__blender__*` tools to contribute edits; those may drive the user's open
session. Send Blender feedback through `--revise`.

Sol's Python runs inside Blender, which is outside the Codex sandbox. For
these runs the MCP server's safe mode is on (only `bpy`, `bmesh`, `mathutils`
and pure stdlib import; no `os`, `subprocess` or `open()`) and the
asset-library tools (Sketchfab, Poly Haven, Hyper3D) are off, so the object
is Sol's own geometry with no third-party licence attached.

Needs Blender, the Blender MCP add-on enabled in it, `mcp-for-blender` on
`PATH` (or its command in `CODEX_3D_BLENDER_MCP`), and a logged-in graphical
session, because the add-on cannot serve from `blender -b`. `--revise`
reopens `object.blend`; `--render-only` needs no Blender.

## Writing the spec

Give Sol the visual intent and the constraints it needs to reason about the
geometry. Include your proposed approach when useful and invite it to identify
tradeoffs. Include only lines that matter:

```
Object: <one thing, one sentence>
Use: <where it lives and how the page will move it - this decides which parts must be separate>
Style: <stylised realism | low-poly | technical | toy-like> + the defining shapes (long hood, short deck...)
Materials: <per surface: gloss paint #hex clearcoat, tinted glass, matte rubber, brushed metal, emissive lights>
Size: <real-world metres; wheelbase / key dimensions>
Parts to expose: <names for everything the page animates: wheel_fl, door_l, blade, lid...>
Idle animation: <none | what updateObject should do>
Constraints: no text, no logos, symmetric, under N triangles
Proposed approach: <optional geometry strategy; invite better alternatives>
Acceptance: <visible and measurable conditions both agents should check>
```

For `--format svg`, replace Size with a viewBox, use kebab-case part ids, and
name the medium (flat, outlined, isometric, technical drawing).

For `--format blender`, write the same spec but say "the ground" rather than
`y = 0` (Blender is Z-up; the export converts), keep part names to lowercase
letters, digits and underscores, and describe an idle animation as motion of
named parts with a loop length. Sol keyframes it and the GLB carries it as
glTF clips, not `updateObject`.

Rules that materially change the result:

- **Name the parts you will animate.** Sol builds separate meshes with
  pivots for named parts; unnamed detail gets merged.
- **State real dimensions.** Bounds are measured and checked; wrong scale is
  the most common miss when you leave it out.
- **Describe materials per surface** with hex values from the project palette.
- **Say what it is for.** "Will be orbited by the user" gets a finished
  underside; "seen only from the front" lets Sol spend triangles wisely.

See `references/prompting.md` for recipes and failure-mode fixes.

## Collaborative workflow

1. Read the project context and decide whether the deliverable is geometry or
   a picture. For geometry, choose the format and a dedicated build folder.
2. Write a shared brief with dimensions, materials, named parts, required
   motion and acceptance criteria. Add reference images and a proposed
   modeling approach when they help. Use `--prompt-file` for a longer brief.
3. Run a build with the default single pass. Sol implements the object and
   returns its decisions, checks, assumptions and unresolved issues in
   `notes.md`. `--passes 2` can add a self-review; Claude still reviews the
   result independently.
4. Read the handoff and view `preview.png` and the 3D `sheet.png`. Compare the
   silhouette, dimensions, joins, pivots and materials with the brief. Separate
   visible defects from taste preferences and from issues caused by lighting
   or integration. Do not accept a result solely because Sol says it passed.
5. Contribute the next improvement. Claude may edit `object.js` or `object.svg`
   between runs, then use `--render-only` and recheck it. Or send Sol a focused
   `--revise` explaining the observation, requested change and what must stay
   intact. The wrapper attaches current renders and supplies the saved spec;
   Sol reads the current source and prior notes. Blender edits use `--revise`.
6. Inspect the new result. Iterate while concrete defects remain; do not run
   speculative review rounds once acceptance criteria pass. Give Sol room to
   challenge a proposed fix and explain a better one within the same brief.
7. Integrate the asset, animation and lighting into the project. Check it in
   the actual page as well as the standalone viewer, especially part motion
   and materials. Report remaining compromises accurately.
8. To share the viewer, serve the folder with
   `python3 -m http.server 8765 --bind 0.0.0.0 -d <build-dir>` and report
   `http://<configured-preview-host>:8765/viewer.html`. Nothing opened in a GUI here is visible
   to the user. Report the asset path, exposed parts and viewer URL.

Example feedback:

```bash
python3 ~/.claude/skills/codex-3d/scripts/codex_3d.py --out public/3d/car \
  --revise "The side render shows the roof too tall. Lower it 0.10 m. Preserve the wheelbase, wheel pivots and material palette. Check the window joins and explain any compromise."
```

## Wiring the object in

`object.js` imports the bare specifier `three`, so it drops into any bundled
project that has `three` installed (`npm i three@0.186.0`), and into a plain
page through an importmap (copy the one from `viewer.html`).

```js
import * as THREE from 'three';
import { createObject } from './3d/car/object.js';

const car = createObject();          // THREE.Group, rests on y = 0, faces +Z
scene.add(car);
const { wheel_fl, wheel_fr, wheel_rl, wheel_rr, steer_fl, steer_fr } = car.userData.parts;

function tick(t, dt) {
  for (const w of [wheel_fl, wheel_fr, wheel_rl, wheel_rr]) w.rotation.x -= dt * 12;
  steer_fl.rotation.y = steer_fr.rotation.y = Math.sin(t) * 0.3;
  car.position.x = -6 + (t % 6);
}
```

React Three Fiber: build once with `useMemo(() => createObject(), [])` and
render `<primitive object={car} />`; animate parts in `useFrame`.

`object.glb` (`--format blender`) loads through `GLTFLoader`; parts are found
by name and turn about the origins Sol gave them. Copy the file somewhere
the page can fetch it, such as `public/3d/car/`:

```js
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const gltf = await new GLTFLoader().loadAsync('/3d/car/object.glb');
const car = gltf.scene;              // rests on y = 0, faces +Z
scene.add(car);
const wheel_fl = car.getObjectByName('wheel_fl');
// idle animation, if the spec asked for one:
const mixer = new THREE.AnimationMixer(car);
gltf.animations.forEach(clip => mixer.clipAction(clip).play());   // then mixer.update(dt) per frame
```

React Three Fiber: `const { scene, animations } = useGLTF('/3d/car/object.glb')`
and `<primitive object={scene} />`. Keep `object.blend` out of `public/`
unless it should ship; it is the editable source, not something the page loads.

Lighting is the consumer's job. The viewer uses an environment map
(`RoomEnvironment`), a shadowed key light and a rim light; copy that setup
if the page has no lighting yet, or the materials will look flat.

SVG: inline the file (so ids are reachable) and animate the groups:

```css
#wheel-front, #wheel-rear { animation: spin 0.6s linear infinite; transform-box: fill-box; transform-origin: center; }
@keyframes spin { to { transform: rotate(360deg); } }
```

## Failure handling

- **`render error: ... object.js failed`** - the module threw in the
  browser. The review pass usually fixes it; if it survives, `--revise` with
  the error text, or open `object.js` and fix the line yourself, then
  `--render-only`.
- **Looks like boxes glued together** - the spec was too vague. Add the
  defining shapes and "stylised realism, continuous surfaces", then
  `--revise` with the specific silhouette correction. Reasoning stays at `max`.
- **Wrong scale** - state metres in the spec; `--revise "scale to 4.6 m long"`.
- **Parts not exposed** - `--revise "expose <name> as a separate pivot group in userData.parts"`.
- **Codex timed out** - raise `--timeout`; max reasoning on complex objects can take longer.
- **Model unavailable or auth errors** - report the exact failure and check `codex --version`. An outdated Codex CLI can report `gpt-6.2-sol` unsupported even for accounts that have access after updating. Keep `gpt-6.2-sol` and `max` pinned; do not change accounts or authentication to bypass an access failure.
- **No Chrome found** - pass `--chrome PATH` (Google Chrome, Chromium or Canary). Chrome only renders; Codex does not need it.
- **`Blender exited during startup` / `did not open its MCP port`** (`--format blender`) - the Blender MCP add-on is not installed or enabled (`mcp-for-blender install-addon`, then enable "Blender MCP" in Preferences), or there is no logged-in graphical session. Pass `--blender PATH` if Blender is not in the usual place.
- **`Blender MCP server not found`** - put `mcp-for-blender` on `PATH` or set `CODEX_3D_BLENDER_MCP`. Do not use a launcher connected to an existing shared Blender. Verify that the MCP command honors the throwaway instance's requested port.
- **Sol replies `WRONG BLENDER`** - the MCP server reached a Blender other than the throwaway one and Sol stopped before changing anything. Fix `CODEX_3D_BLENDER_MCP` so the command honours `--port`.
- **Looked right in Blender, wrong in the render** - only plain Principled BSDF values survive the glTF export. `--revise "replace the procedural shader on <part> with plain Principled values"`.
