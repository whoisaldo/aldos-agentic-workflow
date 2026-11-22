#!/usr/bin/env python3
"""
codex_3d.py - build real 3D objects (three.js modules) and 2D vector objects
(SVG) by handing the modelling job to GPT-6.2 Sol through the Codex CLI, then
rendering the result headlessly so it can be inspected.

Auth: whatever `codex` is already logged in with (ChatGPT OAuth). No API key.

Pipeline (default `--passes 1`, then Claude reviews and sends --revise):
  1. build   codex exec (workspace-write, sandboxed to the build dir) writes
             ./object.js (or ./object.svg) to the contract in assets/.
  2. render  headless Chrome screenshots ./viewer.html -> preview.png and,
             for 3D, sheet.png (2x2 contact sheet). The viewer POSTs a status
             JSON (bounds, triangle count, parts, or the JS error) back to a
             throwaway local server, so failures are caught, not guessed.
  3. review  Claude inspects the renders and report, then edits the source or
             sends a targeted --revise. --passes 2+ adds Sol self-review.
Everything Codex may touch is confined to the build dir. Nothing else in the
project is read or written by this script.

`--format blender` swaps step 1: Sol models in a real Blender through the
Blender MCP server and exports ./object.glb (+ ./object.blend). The script
starts its own throwaway Blender on a free port for the run and makes it the
only MCP server Codex can see, so a Blender the user already has open (and
whatever is unsaved in it) is never touched. Blender itself runs outside the
Codex sandbox; the MCP server's safe mode keeps Sol's Python to bpy.
"""

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

THREE_VERSION = "0.186.0"
DEFAULT_MODEL = "gpt-6.2-sol"
REASONING_EFFORT = "max"

SKILL_DIR = Path(__file__).resolve().parent.parent
ASSETS = SKILL_DIR / "assets"
CACHE_ROOT = Path(os.environ.get("AGENT_SKILL_CACHE_DIR") or (
    r"D:\AgentWork\cache\agent-skills" if os.name == "nt" and Path(r"D:\AgentWork").exists()
    else Path.home() / ".cache" / "agent-skills"))
VENDOR = CACHE_ROOT / "codex-3d" / ("three-" + THREE_VERSION)

FILES = {
    "three":   {"object": "object.js", "viewer": "viewer-three.html", "views": ["hero", "sheet"]},
    "svg":     {"object": "object.svg", "viewer": "viewer-svg.html", "views": ["hero"]},
    "blender": {"object": "object.glb", "viewer": "viewer-three.html", "views": ["hero", "sheet"]},
}
VIEW_PNG = {"hero": "preview.png", "sheet": "sheet.png"}

# --format blender: the per-run MCP server entry, and the only tools Sol gets.
# The asset libraries (Sketchfab, Poly Haven, Hyper3D...) stay off: the object
# must be Sol's own geometry, with no third-party licences or fetched text.
MCP_SERVER = "codex3d_blender"
MCP_TOOLS = ["get_addon_status", "get_scene_info", "get_object_info", "execute_blender_code",
             "get_viewport_screenshot", "export_scene", "bpy_api_lookup", "describe_node_type"]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------
# prompts
# ----------------------------------------------------------------------------

COMMON_RULES = """\
- Do NOT create, edit or delete any other file. Do NOT run git, package
  managers, network commands, or anything outside this folder.
- Work with Claude as a modeling collaborator. The specification may include
  Claude's proposed approach; check its geometry, pivots and tradeoffs, improve
  it where needed, and explain consequential changes in your final report.
- Choose reasonable defaults for unspecified details and finish the requested
  artifact. Report conflicting requirements or missing essentials explicitly.
- Do not launch subagents, another Codex process, or another model.
- ./viewer.html is exactly how the file will be consumed. Read it if anything
  about the contract is unclear.
- Finish with a concise handoff for Claude: what changed, design decisions and
  assumptions, named parts and animation hooks, checks actually run, and
  unresolved issues for Claude to inspect. Distinguish observed defects from
  suggestions; do not claim to have reviewed renders you have not seen."""

THREE_CONTRACT = f"""\
You are acting as a headless 3D asset builder. Your only job is to write one
file, ./object.js, in the current working directory (a scratch build folder).

Contract for ./object.js - an ES module for three.js r{THREE_VERSION}:

    import * as THREE from 'three';
    // addons are fine, e.g.
    // import {{ RoundedBoxGeometry }} from 'three/addons/geometries/RoundedBoxGeometry.js';
    export function createObject() {{ ... return group; }}   // REQUIRED, returns a THREE.Group
    export function updateObject(group, t, dt) {{ ... }}     // OPTIONAL idle animation, t in seconds
    export const meta = {{ name: '...', size: [w, h, d], parts: ['...'] }};   // REQUIRED

Rules:
- Fully procedural: geometry + materials only. No textures, no fetch(), no
  URLs, no DOM, no lights, no cameras (the viewer supplies an environment
  map, key/rim lights and shadows).
- Y is up. The object rests on the ground plane y = 0, is centred on x = 0
  and z = 0, and its front faces +Z. Real-world metres: a car is ~4.5 m
  long, a mug ~0.1 m tall.
- Name every meaningful mesh and group (`mesh.name = 'wheel_fl'`) and put the
  animatable ones in `group.userData.parts = {{ wheel_fl: mesh, ... }}`.
- Materials: MeshStandardMaterial / MeshPhysicalMaterial with deliberate
  metalness, roughness, clearcoat, transmission where they sell the surface.
- Budget: under 200,000 triangles. Prefer fewer, better-shaped pieces.
- Quality bar: it must read as the real thing from every angle. Correct
  proportions, continuous silhouette, no gaps, no floating or intersecting
  parts, no z-fighting, symmetric where the object is symmetric. Build
  curved forms with LatheGeometry, ExtrudeGeometry with bevels, Shape-based
  hulls, TubeGeometry, CapsuleGeometry, RoundedBoxGeometry and edited
  BufferGeometry. A pile of plain boxes and cylinders is a failure unless the
  object really is boxes and cylinders.
- Before finishing run `node check.mjs` (it imports object.js and checks the
  contract, bounds, and triangle budget) and fix everything it reports until
  it prints OK.
{COMMON_RULES}"""

SVG_CONTRACT = f"""\
You are acting as a headless 2D vector asset builder. Your only job is to
write one file, ./object.svg, in the current working directory (a scratch
build folder).

Contract for ./object.svg - a standalone, hand-authored SVG:
- `viewBox` set, no `width`/`height` attributes, artwork fills the viewBox
  with a small margin.
- Pure vector: paths, shapes, gradients and filters. No <image>, no external
  hrefs, no web fonts, no <script>.
- Every meaningful part is a `<g id="...">` with a stable kebab-case id
  (`wheel-front`, `door-left`). Each animatable part is positioned with
  `transform="translate(x y)"` on its group so the group's local origin is
  its natural pivot (a wheel's centre, a hinge) and a caller can rotate or
  move it from CSS or JS.
- Literal hex colours. Share colours through <defs> gradients or repeated
  fills so recolouring is easy. No editor metadata, no redundant nesting or
  transforms, no default-valued attributes. Keep it under 200 KB.
- Quality bar: it must read as the real thing, not clip art. Accurate
  proportions and silhouette, considered line weights, shading through
  gradients, highlights and shadows where they sell the form. Match the
  medium the specification asks for (flat, outlined, isometric, technical).
- Before finishing validate it with `xmllint --noout object.svg` (if
  available) and fix any error.
{COMMON_RULES}"""


def blender_contract(build_dir, scene, existing=False):
    """Contract for --format blender. `scene` is the marker name given to the
    throwaway Blender's scene, so Sol can tell it is not in someone's session."""
    start = ("It has ./object.blend open, which holds the finished object. Do not rebuild it "
             "and do not delete anything the request does not mention."
             if existing else
             "It holds Blender's default startup scene: delete the default cube, camera and "
             "light, then build.")
    return f"""\
You are acting as a headless 3D asset builder. You model one object in a real
Blender through the MCP server `{MCP_SERVER}`, and you leave two files in the
current working directory (a scratch build folder, {build_dir}):
./object.blend (the editable source) and ./object.glb (what the page loads).

The Blender instance:
- It is a throwaway Blender started for this job. Call get_scene_info first:
  the scene must be named `{scene}`. If it has any other name you are
  connected to someone else's Blender. Change nothing, reply exactly
  `WRONG BLENDER` and stop.
- {start}
- execute_blender_code runs in safe mode: only bpy, bmesh, mathutils and pure
  standard-library modules (math, random, itertools...) import. No os, sys,
  numpy, open(), file or network access; files are written only through bpy
  operators and the export_scene tool.
- Work in small steps: one part or one fix per execute_blender_code call (a
  call must return within 180 s), and look at get_viewport_screenshot after
  every meaningful step. Read blender_version from get_addon_status and use
  bpy_api_lookup instead of guessing an API that changes between versions.
- Wherever a tool takes `user_prompt`, pass the literal string "codex-3d".
  Never paste this prompt into it.

Contract for the object:
- Blender units are metres and Z is up. The object rests on the ground plane
  z = 0, is centred on x = 0 and y = 0, and its FRONT FACES -Y (Blender's
  front view). The glTF export turns that into three.js space: Y up, resting
  on y = 0, front facing +Z. Real-world size: a car is ~4.5 m long, a mug
  ~0.1 m tall.
- One root Empty at the world origin, named after the object, with every
  other object parented under it.
- Every part the specification names is its own object (or an Empty pivot
  with children) carrying exactly that name, with its origin at the natural
  pivot: a wheel's axle centre, a door's hinge line. The page finds parts with
  `gltf.scene.getObjectByName('wheel_fl')` and turns them about their origins.
  Names use lowercase letters, digits and underscores only: three.js rewrites
  dots and spaces, so Blender's automatic `.001` suffix breaks the lookup.
- Apply scale (object scale 1, 1, 1) on every mesh.
- Materials: Principled BSDF only, driven by plain values (base colour,
  metallic, roughness, coat, transmission, alpha, emission colour and
  strength). Those export to glTF PBR. No image textures and no procedural
  texture or shader-math nodes: they do not survive the export. Look nodes up
  by type, never by name. One named material per distinct surface.
- Idle animation only if the specification asks for one: keyframe object
  transforms on the parts as a loop that ends in its starting pose. It exports
  as glTF animation clips. Otherwise leave no animation data.
- No lights, cameras or helper geometry in the scene (the viewer supplies an
  environment map, key/rim lights and shadows).
- Budget: under 200,000 triangles after modifiers. Prefer fewer, better-shaped
  pieces.
- Quality bar: it must read as the real thing from every angle. Correct
  proportions, continuous silhouette, no gaps, no floating or intersecting
  parts, no z-fighting, symmetric where the object is symmetric. Use what
  Blender is good at: bmesh and from_pydata hulls, curves, and mirror, bevel,
  solidify, subdivision and boolean modifiers (applied on export), smooth
  shading with sharp edges where the form has them. A pile of plain cubes and
  cylinders is a failure unless the object really is cubes and cylinders.
- Finish in this order, and again after every later fix:
  1. save the source with execute_blender_code:
     bpy.ops.wm.save_as_mainfile(filepath="{build_dir}/object.blend")
  2. export with the export_scene tool: filepath "{build_dir}/object.glb",
     object_names [your root Empty], apply_modifiers true
  3. in your shell run `node check.mjs object.glb`. It loads the exported file
     the way three.js will and checks the contract, bounds and triangle
     budget. Fix what it reports in Blender until it prints OK.
{COMMON_RULES}"""


def build_prompt(fmt, spec, inputs, blender=None):
    head = {"three": THREE_CONTRACT, "svg": SVG_CONTRACT}.get(fmt) or blender_contract(*blender)
    parts = [head, ""]
    if inputs:
        parts += ["Attached image(s) are references. Their role is described in the "
                  "specification. Use `view_image` to inspect them if needed.", ""]
    parts += ["=== SPECIFICATION ===", spec.strip(), "=== END SPECIFICATION ===", ""]
    return "\n".join(parts)


def status_summary(status):
    if not status:
        return "The render produced no status (the page may not have finished loading)."
    if not status.get("ok"):
        return "THE RENDER FAILED with this error:\n" + str(status.get("error"))[:1500]
    if "size" in status:
        sz = " x ".join(f"{v:.3f}" for v in status["size"])
        mn = status.get("min", [0, 0, 0])
        return (f"Measured: size {sz} m (x/width, y/height, z/depth), lowest point y = "
                f"{mn[1]:.3f}, {status.get('triangles', 0):,} triangles, "
                f"{len(status.get('parts', []))} named parts.")
    return (f"Measured: viewBox {status.get('viewBox')}, {status.get('elements')} elements, "
            f"{len(status.get('ids', []))} ids, {status.get('images')} <image>, "
            f"{status.get('externalRefs')} external refs.")


BLENDER_FINISH = ("Re-save ./object.blend, re-export ./object.glb and run `node check.mjs "
                  "object.glb` until it prints OK before finishing.")


def review_prompt(fmt, status, views):
    if fmt in ("three", "blender"):
        what = ("preview.png (three-quarter hero view) and sheet.png (2x2 contact sheet: "
                "three-quarter, front +Z, side +X, top)")
        checks = ("proportions, silhouette, gaps, floating or intersecting parts, "
                  "z-fighting, symmetry, wheels/feet sitting on the ground, and whether "
                  "the materials read correctly")
        target = "./object.js"
        finish = "Run `node check.mjs` until it prints OK before finishing."
    else:
        what = "preview.png (the SVG displayed inline by the viewer)"
        checks = ("proportions, silhouette, line weights, shading, overlapping or "
                  "misaligned parts, and whether it reads as the real thing")
        target = "./object.svg"
        finish = "Validate with `xmllint --noout object.svg` before finishing."
    fix = f"by editing {target}"
    if fmt == "blender":
        # The render is three.js loading the export, not Blender's viewport.
        checks += (", and anything that looked right in Blender but did not survive the "
                   "export to three.js (lost materials, unapplied modifiers, flipped normals)")
        target = "./object.glb"
        fix = f"in the Blender scene, which is still open behind `{MCP_SERVER}`"
        finish = BLENDER_FINISH
    return "\n".join([
        f"Attached: {what} of the {target} you just wrote, rendered by the viewer.",
        status_summary(status),
        "",
        f"Review the render critically against the specification: {checks}. Fix every "
        f"defect you can see {fix}. Keep what already works. If the render "
        "shows an error, fix the error first.",
        finish,
        "Report what changed, checks run, and any remaining issues for Claude's review.",
        "",
    ])


def revise_prompt(fmt, change, status, inputs, blender=None, spec=""):
    target = FILES[fmt]["object"]
    finish = {"three": "Run `node check.mjs` until it prints OK before finishing.",
              "svg": "Validate with `xmllint --noout object.svg` before finishing.",
              "blender": BLENDER_FINISH}[fmt]
    # Revisions start fresh, with the contract and saved spec for shared context.
    read = (blender_contract(*blender, existing=True) + "\n\nRevise that object."
            if fmt == "blender" else
            {"three": THREE_CONTRACT, "svg": SVG_CONTRACT}[fmt] +
            f"\n\nRevise the existing ./{target} in this folder. Read it first.")
    lines = [
        read + (" The attached render(s) show its current state." if status else ""),
        status_summary(status) if status else "",
        "",
        "=== SHARED SPECIFICATION ===", spec.strip() or "Use the existing object's contract.",
        "=== END SHARED SPECIFICATION ===", "",
        "Read ./notes.md if present for prior decisions. The source files and current "
        "renders take precedence over old notes; Claude may have edited them between runs.",
        "=== CHANGE REQUEST ===", change.strip(), "=== END CHANGE REQUEST ===", "",
        "Keep everything the request does not mention identical: same contract, same "
        "part names, same proportions. The change request supersedes conflicting older "
        "specification details. Do not touch any other file.",
        finish,
        "Report what changed, checks run, and any remaining issues for Claude's review.",
        "",
    ]
    if inputs:
        lines.insert(3, "Additional attached image(s) are references for this change.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# codex
# ----------------------------------------------------------------------------

def run_codex(args, prompt, images, timeout, label):
    """Run `codex exec ...` with the prompt on stdin. Returns (thread_id, last_msg, rc)."""
    cmd = ["codex", "exec", *args]
    for img in images:
        cmd += ["-i", str(img)]
    if "resume" in args:
        cmd.append("-")          # explicit stdin marker: resume takes a positional prompt
    # The prompt always goes over stdin: `-i` is variadic on `exec` and would
    # swallow a positional prompt, and stdin sidesteps argv limits.

    thread_id, last_msg, err_lines = None, None, []
    err_file = tempfile.TemporaryFile(mode="w+")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_file,
                                stdin=subprocess.PIPE, text=True, bufsize=1,
                                start_new_session=True)
    except FileNotFoundError:
        err_file.close()
        return None, "codex CLI not found on PATH", 127

    timed_out = threading.Event()

    def _kill():
        timed_out.set()
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    watchdog = threading.Timer(timeout, _kill)
    watchdog.daemon = True
    watchdog.start()
    finished = threading.Event()

    def _heartbeat():
        waited = 0
        while not finished.wait(15):
            waited += 15
            log(f"  ... {label} still running ({waited}s elapsed)")

    threading.Thread(target=_heartbeat, daemon=True).start()

    try:
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")
            if etype == "thread.started":
                thread_id = evt.get("thread_id")
                log(f"  codex thread {thread_id}")
            elif etype == "item.completed":
                item = evt.get("item", {})
                if item.get("type") == "agent_message":
                    last_msg = item.get("text")
                elif item.get("type") == "command_execution":
                    cmdline = (item.get("command") or "")[:90].replace("\n", " ")
                    log(f"  $ {cmdline}")
                elif item.get("type") == "mcp_tool_call":
                    log(f"  > {item.get('tool')}" + ("  (failed)" if item.get("error") else ""))
            elif etype in ("error", "turn.failed"):
                err_lines.append(json.dumps(evt)[:500])
        proc.wait()
    finally:
        finished.set()
        watchdog.cancel()

    if timed_out.is_set():
        err_file.close()
        return thread_id, f"timed out after {timeout}s", 124
    try:
        err_file.seek(0)
        stderr = err_file.read()
    except Exception:
        stderr = ""
    finally:
        err_file.close()
    if proc.returncode != 0 and stderr.strip():
        err_lines.append(stderr.strip()[-800:])
    message = ("; ".join(err_lines) or last_msg) if proc.returncode else last_msg
    return thread_id, message, proc.returncode


def codex_exec_args(build_dir):
    return ["--json", "--skip-git-repo-check", "-s", "workspace-write", "-C", str(build_dir),
            "-m", DEFAULT_MODEL, "-c", f'model_reasoning_effort="{REASONING_EFFORT}"',
            "--disable", "multi_agent", "--disable", "multi_agent_v2",
            "-c", 'approval_policy="never"']


def codex_resume_args(thread_id):
    return ["resume", thread_id, "--json", "--skip-git-repo-check",
            "-m", DEFAULT_MODEL, "-c", f'model_reasoning_effort="{REASONING_EFFORT}"',
            "--disable", "multi_agent", "--disable", "multi_agent_v2",
            "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"']


def blender_mcp_args(mcp_cmd, port, build_dir):
    """`-c` overrides that make the throwaway Blender the only MCP server Codex
    sees. Overrides merge into the user's config rather than replace it, so
    every server configured there is switched off by name: if the user's own
    Blender server stayed visible, Sol could edit the session they have open."""
    r = subprocess.run(["codex", "mcp", "list", "--json"], capture_output=True, text=True,
                       cwd=build_dir)
    if r.returncode != 0:
        raise RuntimeError(f"`codex mcp list --json` failed: {r.stderr.strip()[-300:]}")
    args = []
    for srv in json.loads(r.stdout):
        args += ["-c", f"mcp_servers.{srv['name']}.enabled=false"]
    env = {"BLENDER_HOST": "127.0.0.1", "BLENDER_PORT": str(port),
           "DISABLE_TELEMETRY": "true", "BLENDER_MCP_SAFE_MODE": "1"}
    # json.dumps output is valid TOML for strings and string arrays. The tools
    # are pre-approved because `approval_policy="never"` rejects MCP calls otherwise.
    server = (f"command={json.dumps(mcp_cmd[0])},"
              f"args={json.dumps(mcp_cmd[1:] + ['--host', '127.0.0.1', '--port', str(port)])},"
              "env={" + ",".join(f"{k}={json.dumps(v)}" for k, v in env.items()) + "},"
              "startup_timeout_sec=90,tool_timeout_sec=600,required=true,"
              f'default_tools_approval_mode="approve",enabled_tools={json.dumps(MCP_TOOLS)}')
    return args + ["-c", f"mcp_servers.{MCP_SERVER}={{{server}}}"]


# ----------------------------------------------------------------------------
# build dir + node deps
# ----------------------------------------------------------------------------

def ensure_three():
    """Make `three` importable using a writable, versioned dependency cache."""
    pkg = VENDOR / "node_modules" / "three" / "package.json"
    if pkg.exists():
        return VENDOR / "node_modules"
    if not shutil.which("npm"):
        return None
    log(f"  provisioning three@{THREE_VERSION} into {VENDOR} (one-time)...")
    VENDOR.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["npm", "install", "--prefix", str(VENDOR), "--no-audit", "--no-fund",
                        "--silent", f"three@{THREE_VERSION}"], cwd=VENDOR, capture_output=True, text=True)
    if r.returncode != 0 or not pkg.exists():
        log(f"  WARNING: npm install failed; node check.mjs will not run: {r.stderr.strip()[-300:]}")
        return None
    return VENDOR / "node_modules"


def prepare_build_dir(build_dir, fmt):
    build_dir.mkdir(parents=True, exist_ok=True)
    viewer_src = (ASSETS / FILES[fmt]["viewer"]).read_text()
    (build_dir / "viewer.html").write_text(viewer_src.replace("{{THREE_VERSION}}", THREE_VERSION)
                                           .replace("{{OBJECT_FILE}}", FILES[fmt]["object"]))
    if fmt != "svg":
        shutil.copy2(ASSETS / "check.mjs", build_dir / "check.mjs")
        nm = ensure_three()
        link = build_dir / "node_modules"
        if nm and not link.exists():
            link.symlink_to(nm, target_is_directory=True)


def cleanup_build_dir(build_dir):
    link = build_dir / "node_modules"
    if link.is_symlink():
        link.unlink()
    for backup in build_dir.glob("object.blend[0-9]*"):   # Blender's save-versions (.blend1)
        backup.unlink()


# ----------------------------------------------------------------------------
# blender (--format blender)
# ----------------------------------------------------------------------------

def find_blender(override):
    cands = [override, os.environ.get("CODEX_3D_BLENDER"),
             "/Applications/Blender.app/Contents/MacOS/Blender", shutil.which("blender"),
             *sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender*\blender.exe"),
                     reverse=True)]
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


def find_blender_mcp():
    """The MCP server as an argv list. Deliberately never the bare `blender-mcp`
    name: on Ali's machines that is a launcher pinned to the shared Blender."""
    override = os.environ.get("CODEX_3D_BLENDER_MCP")
    if override:
        return shlex.split(override)
    found = shutil.which("mcp-for-blender")
    return [found] if found else None


def start_blender(blender, blend=None):
    """Start a throwaway GUI Blender whose MCP add-on listens on a free port.
    Returns (proc, port, scene name). The add-on cannot serve in background
    mode, so this needs a logged-in graphical session."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    scene = f"codex3d_{port}"
    # The add-on reads its port from a scene property. Put it back once the
    # server is bound so object.blend does not carry this run's port, and name
    # the scene so Sol can check which Blender it is talking to.
    expr = ("import bpy; s = bpy.context.scene; p = s.blendermcp_port; "
            f"s.blendermcp_port = {port}; bpy.ops.blendermcp.start_server(); "
            f"s.blendermcp_port = p; s.name = '{scene}'")
    cmd = [blender, "--window-geometry", "0", "0", "1280", "800"]
    if blend:
        cmd.append(str(blend))
    cmd += ["--python-exit-code", "1", "--python-expr", expr]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < 60 and proc.poll() is None:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                log(f"  blender pid {proc.pid}, MCP add-on on 127.0.0.1:{port}")
                return proc, port, scene
        except OSError:
            time.sleep(0.25)
    why = (f"exited during startup (rc={proc.returncode})" if proc.poll() is not None
           else "did not open its MCP port within 60 s")
    stop_blender(proc)
    raise RuntimeError(f"Blender {why}. It needs a logged-in graphical session and the Blender "
                       "MCP add-on installed and enabled (`mcp-for-blender install-addon`).")


def stop_blender(proc):
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------------

def find_chrome(override):
    cands = [override, os.environ.get("CODEX_3D_CHROME"),
             "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             "/Applications/Chromium.app/Contents/MacOS/Chromium",
             "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
             shutil.which("google-chrome"), shutil.which("google-chrome-stable"),
             shutil.which("chromium"), shutil.which("chromium-browser"),
             r"C:\Program Files\Google\Chrome\Application\chrome.exe",
             r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


def png_complete(path):
    try:
        with open(path, "rb") as fh:
            if fh.read(8) != b"\x89PNG\r\n\x1a\n":
                return False
            fh.seek(-8, 2)
            return fh.read()[:4] == b"IEND"
    except Exception:
        return False


class _Handler(SimpleHTTPRequestHandler):
    statuses = []

    def do_POST(self):
        if self.path.startswith("/__status"):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                self.statuses.append(json.loads(self.rfile.read(n) or b"{}"))
            except Exception:
                pass
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):
        pass


def screenshot(chrome, url, out, size, wait, software):
    """Headless Chrome writes the PNG then, on a display-less Mac, never exits.
    Poll for a complete PNG and kill the whole process group ourselves."""
    if out.exists():
        out.unlink()
    profile = Path(tempfile.mkdtemp(prefix="codex-3d-chrome-"))
    cmd = [chrome, "--headless=new", f"--user-data-dir={profile}", "--no-first-run",
           "--no-default-browser-check", "--hide-scrollbars", f"--window-size={size[0]},{size[1]}",
           "--virtual-time-budget=10000", f"--screenshot={out}"]
    if software:
        cmd += ["--disable-gpu", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
    cmd.append(url)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    t0 = time.time()
    ok = False
    try:
        while time.time() - t0 < wait:
            if png_complete(out):
                ok = True
                time.sleep(0.5)      # let the status POST land
                break
            if proc.poll() is not None:
                ok = png_complete(out)
                break
            time.sleep(0.2)
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)
    return ok


def render(build_dir, fmt, size, bg, chrome, wait=30):
    """Screenshot every view. Returns {view: {"png": path|None, "status": dict|None}}."""
    results = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, directory=str(build_dir)))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for view in FILES[fmt]["views"]:
            out = build_dir / VIEW_PNG[view]
            url = f"http://127.0.0.1:{port}/viewer.html?static=1&view={view}&bg={bg}"
            _Handler.statuses.clear()
            ok = screenshot(chrome, url, out, size, wait, software=False)
            if not ok:
                log(f"  render ({view}): no frame with GPU, retrying with software GL")
                _Handler.statuses.clear()
                ok = screenshot(chrome, url, out, size, wait, software=True)
            status = _Handler.statuses[-1] if _Handler.statuses else None
            results[view] = {"png": str(out) if ok else None, "status": status}
            if ok:
                log(f"  render ({view}) -> {out.name}")
            else:
                log(f"  render ({view}) FAILED: no screenshot produced")
    finally:
        server.shutdown()
        server.server_close()
    return results


def primary_status(results):
    for view in ("hero", "sheet"):
        st = results.get(view, {}).get("status")
        if st:
            return st
    return None


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def parse_size(s):
    m = re.fullmatch(r"(\d+)x(\d+)", s)
    if not m:
        raise argparse.ArgumentTypeError("size must look like 1536x1024")
    return int(m.group(1)), int(m.group(2))


def main():
    ap = argparse.ArgumentParser(
        description="Build a 3D object (three.js module, or a GLB modelled in Blender) or 2D "
                    "vector object (SVG) with GPT-6.2 Sol via Codex, then render it headlessly "
                    "for inspection.")
    ap.add_argument("--prompt", help="Object specification text.")
    ap.add_argument("--prompt-file", help="File containing the specification.")
    ap.add_argument("--out", required=True,
                    help="Build directory (created). Receives object.js|object.svg|object.glb, "
                         "viewer.html, preview.png, sheet.png, notes.md.")
    ap.add_argument("--format", default="three", choices=sorted(FILES),
                    help="three = three.js ES module (default); svg = 2D vector object; "
                         "blender = object.glb + object.blend modelled in a throwaway Blender "
                         "through Blender MCP.")
    ap.add_argument("--input", action="append", default=[],
                    help="Reference image to attach (repeatable).")
    ap.add_argument("--passes", type=int, default=1,
                    help="1 (default) = build/revise, render, hand back to Claude; "
                         "2+ adds Sol self-review before Claude's review.")
    ap.add_argument("--revise", metavar="CHANGE",
                    help="Instead of building from scratch, change the existing object in --out.")
    ap.add_argument("--render-only", action="store_true",
                    help="Skip Codex; just re-render the existing object in --out.")
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=[DEFAULT_MODEL],
                    help=f"Locked to {DEFAULT_MODEL}; no model fallback.")
    ap.add_argument("--effort", default=REASONING_EFFORT, choices=[REASONING_EFFORT],
                    help="Locked to max reasoning for every Codex pass.")
    ap.add_argument("--size", type=parse_size, default=(1536, 1024),
                    help="Render size WxH (default 1536x1024).")
    ap.add_argument("--bg", default="1a1a22", help="Render background hex (default 1a1a22).")
    ap.add_argument("--chrome", default=None, help="Path to a Chrome/Chromium binary.")
    ap.add_argument("--blender", default=None,
                    help="Path to a Blender binary (--format blender). The MCP server is "
                         "`mcp-for-blender` on PATH, or the command in CODEX_3D_BLENDER_MCP.")
    ap.add_argument("--timeout", type=int, default=1500, help="Seconds per Codex pass (default 1500).")
    ap.add_argument("--json", action="store_true", help="Emit JSON result on stdout.")
    args = ap.parse_args()
    if args.passes < 1:
        ap.error("--passes must be at least 1")

    fmt = args.format
    build_dir = Path(args.out).expanduser().resolve()
    obj = build_dir / FILES[fmt]["object"]
    bg = args.bg.lstrip("#")

    if args.render_only or args.revise:
        if not obj.exists():
            fail(args, f"{obj} does not exist; nothing to {'render' if args.render_only else 'revise'}")
    elif not args.prompt and not args.prompt_file:
        ap.error("one of --prompt or --prompt-file is required (or --revise / --render-only)")
    spec = args.prompt or (Path(args.prompt_file).expanduser().read_text() if args.prompt_file else "")
    if args.revise and not spec and (build_dir / "spec.txt").exists():
        spec = (build_dir / "spec.txt").read_text()
    for f in args.input:
        if not Path(f).expanduser().exists():
            ap.error(f"--input file not found: {f}")
    inputs = [Path(f).expanduser().resolve() for f in args.input]

    chrome = find_chrome(args.chrome)
    if not chrome:
        fail(args, "no Chrome/Chromium found; pass --chrome PATH or set CODEX_3D_CHROME")
    blender_bin = mcp_cmd = None
    if fmt == "blender" and not args.render_only:
        blender_bin, mcp_cmd = find_blender(args.blender), find_blender_mcp()
        if not blender_bin:
            fail(args, "no Blender found; pass --blender PATH or set CODEX_3D_BLENDER")
        if not mcp_cmd:
            fail(args, "Blender MCP server not found; put `mcp-for-blender` on PATH or set "
                       "CODEX_3D_BLENDER_MCP to its command")
        if args.revise and not obj.with_suffix(".blend").exists():
            fail(args, f"{obj.with_suffix('.blend')} does not exist; nothing to revise")

    t0 = time.time()
    prepare_build_dir(build_dir, fmt)
    notes = []
    thread_id = None
    results = {}
    blender_proc, mcp_args, contract_args = None, [], None
    try:
        if blender_bin:
            try:
                blender_proc, port, scene = start_blender(
                    blender_bin, obj.with_suffix(".blend") if args.revise else None)
                mcp_args = blender_mcp_args(mcp_cmd, port, build_dir)
            except (OSError, RuntimeError, ValueError) as exc:
                fail(args, str(exc))
            contract_args = (build_dir, scene)
        if args.render_only:
            log(f"[codex-3d] rendering {obj.name} in {build_dir}")
            results = render(build_dir, fmt, args.size, bg, chrome)
        else:
            if args.revise:
                log(f"[codex-3d] revising {obj.name} with {args.model} ({args.effort})")
                results = render(build_dir, fmt, args.size, bg, chrome)
                status = primary_status(results)
                shots = [Path(r["png"]) for r in results.values() if r["png"]]
                prompt = revise_prompt(fmt, args.revise, status, inputs, contract_args, spec)
                thread_id, msg, rc = run_codex(
                    codex_exec_args(build_dir) + mcp_args,
                    prompt, shots + inputs, args.timeout, "revise")
                notes.append(("Revision: " + args.revise.strip()[:120], msg))
                check_codex(args, thread_id, msg, rc, obj)
                if args.prompt or args.prompt_file:
                    (build_dir / "spec.txt").write_text(spec.strip() + "\n")
            else:
                log(f"[codex-3d] building {obj.name} with {args.model} ({args.effort}), "
                    f"{args.passes} pass(es)")
                (build_dir / "spec.txt").write_text(spec.strip() + "\n")
                prompt = build_prompt(fmt, spec, inputs, contract_args)
                thread_id, msg, rc = run_codex(
                    codex_exec_args(build_dir) + mcp_args,
                    prompt, inputs, args.timeout, "build")
                notes.append(("Build", msg))
                check_codex(args, thread_id, msg, rc, obj)

            results = render(build_dir, fmt, args.size, bg, chrome)
            for i in range(2, max(1, args.passes) + 1):
                status = primary_status(results)
                shots = [Path(r["png"]) for r in results.values() if r["png"]]
                if not shots:
                    log("  no render to review; skipping review pass")
                    break
                log(f"[codex-3d] review pass {i - 1}: Sol inspects its own render")
                prompt = review_prompt(fmt, status, list(results))
                _, msg, rc = run_codex(codex_resume_args(thread_id) + mcp_args,
                                       prompt, shots, args.timeout, f"review {i - 1}")
                notes.append((f"Review pass {i - 1}", msg))
                check_codex(args, thread_id, msg, rc, obj)
                results = render(build_dir, fmt, args.size, bg, chrome)
    finally:
        stop_blender(blender_proc)
        cleanup_build_dir(build_dir)

    status = primary_status(results)
    if notes:
        text = ["# codex-3d notes", "", f"Build dir: `{build_dir}`  ", f"Format: {fmt}  ",
                f"Model: {args.model} ({args.effort})  ", f"Thread: {thread_id or '-'}", ""]
        if spec:
            text += ["## Specification", "", "```", spec.strip(), "```", ""]
        for title, body in notes:
            text += [f"## {title}", "", (body or "(no message)").strip(), ""]
        with open(build_dir / "notes.md", "a") as fh:
            fh.write("\n".join(text) + "\n")

    elapsed = round(time.time() - t0, 1)
    files = sorted(p.name for p in build_dir.iterdir() if p.is_file())
    ok = bool(status and status.get("ok")) and any(r["png"] for r in results.values())
    log(f"[codex-3d] {'done' if ok else 'FINISHED WITH PROBLEMS'} in {elapsed}s -> {build_dir}")
    if status and status.get("ok"):
        log("  " + status_summary(status))
    elif status:
        log("  render error: " + str(status.get("error"))[:300])
    for r in results.values():
        if r["png"]:
            log(f"  {r['png']}")

    payload = {"ok": ok, "build_dir": str(build_dir), "format": fmt, "thread_id": thread_id,
               "model": DEFAULT_MODEL, "effort": REASONING_EFFORT,
               "elapsed_s": elapsed, "object": str(obj), "files": files,
               "renders": {v: r["png"] for v, r in results.items()}, "status": status,
               "notes": str(build_dir / "notes.md") if notes else None}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(str(obj))
        for r in results.values():
            if r["png"]:
                print(r["png"])
    return 0 if ok else 1


def check_codex(args, thread_id, msg, rc, obj):
    if rc == 124:
        fail(args, f"codex timed out after {args.timeout}s; raise --timeout and retry")
    if rc != 0:
        fail(args, f"{DEFAULT_MODEL} at {REASONING_EFFORT} failed (rc={rc}): "
                   f"{(msg or 'no error details')[:1200]}. No fallback was attempted.")
    if thread_id is None:
        fail(args, f"codex did not start a thread (rc={rc}): {msg}")
    if not obj.exists():
        hint = ""
        if msg and re.search(r"log ?in|auth|credential|401|unauthor", msg, re.I):
            hint = " -- run `codex login` and retry."
        fail(args, f"codex finished without writing {obj.name} (rc={rc}). "
                   f"Last message: {(msg or 'none')[:400]}{hint}")


def fail(args, msg):
    log(f"[codex-3d] ERROR: {msg}")
    if args.json:
        print(json.dumps({"ok": False, "error": msg}, indent=2))
    sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
