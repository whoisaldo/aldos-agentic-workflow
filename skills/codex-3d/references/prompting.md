# Prompting recipes for codex-3d

Load this when a first build missed, when the object has many moving parts,
or when several objects must look like one set. Give Sol the constraints and
your proposed approach; ask it to check the geometry and explain better
alternatives. Every call uses GPT-6.2 Sol at max reasoning.

## The four rules that matter most

1. **Name every part the page will animate.** `Parts to expose: wheel_fl,
   wheel_fr, wheel_rl, wheel_rr, steer_fl, steer_fr, door_l`. Named parts
   become separate meshes or pivot groups in `group.userData.parts`; anything
   unnamed may be merged into the body and cannot be animated afterwards.
2. **Give real dimensions.** Bounds are measured by `check.mjs` and shown in
   the render status bar. "About 4.6 m long, wheelbase 2.75 m" beats "a car".
3. **Describe materials per surface, with hex values.** Sol sets
   metalness, roughness, clearcoat and emissive per material; it cannot read
   your site's palette unless you paste it.
4. **Say how it will be seen.** "User orbits it" means a finished underside
   and interior glass. "Seen from the front, sliding right to left" lets
   Sol spend the triangle budget where it shows.

## Copy-paste recipes

### Vehicle (car, bike, ship, aircraft)
```
Object: one <era/type> <vehicle>, <two or three defining shape words>
Use: <hero object on a page | drives across the screen | orbitable configurator>; wheels spin and front wheels steer
Style: stylised realism, continuous surfaces, no visible box seams
Materials: body gloss <#hex> clearcoat; glass dark tinted, slightly transmissive; tyres matte black; alloys brushed dark metal; lights emissive <#hex>
Size: <length> m long, <width> m wide, <height> m tall, wheelbase <n> m
Parts to expose: wheel_fl, wheel_fr, wheel_rl, wheel_rr (spin about their axles), steer_fl, steer_fr (pivot groups around the front wheels), body, headlights, taillights
Idle animation: none
Constraints: no text, no logos, symmetric, all tyres touch y = 0, under 150k triangles
```

### Product / device (bottle, headphones, phone, appliance)
```
Object: one <product>, <brand-neutral description of form>
Use: orbitable product spinner on a dark landing page
Style: studio product realism; crisp edge bevels; nothing floating
Materials: <per part: anodised aluminium #hex, soft-touch matte plastic #hex, glossy glass, brushed steel>
Size: <real dimensions in metres, e.g. 0.19 x 0.08 x 0.02 m>
Parts to expose: <lid, button, cable, ear_cup_l, ear_cup_r>
Idle animation: slow 8 s turntable rotation about y
Constraints: no text, no logos, no screen content (a dark glass slab is fine)
```

### Logo or wordmark in 3D
Font files are not available (no textures, no external files), so ask for
**letterforms built from Shape paths** or for an abstract mark instead of
typeset text:
```
Object: the letter "E" as a chunky extruded mark with a 45-degree chamfer
Style: minimal, geometric, rounded corners radius 0.05
Materials: brushed gold #d4af37, metalness 1, roughness 0.35
Size: 1 m tall, 0.25 m deep
Idle animation: gentle float and 20-degree yaw sway over 4 s
```

### Mechanism / gadget with moving parts (drone, robot arm, turbine)
```
Object: a quadcopter drone with four rotor arms
Use: hovers over a hero section; rotors spin, body bobs
Parts to expose: rotor_1..rotor_4 (spin about y at their hub), arm_1..arm_4, body, landing_skid_l, landing_skid_r
Idle animation: body bobs 0.03 m at 0.5 Hz and tilts 3 degrees; rotors spin at 30 rad/s
Constraints: rotor discs must not intersect arms; under 120k triangles
```
Sol implements the idle animation in `updateObject`. The page can still
override or ignore it.

### Environment piece (room, kiosk, low-poly island)
Ask for a **single object with an explicit footprint** and low-poly style, or
the triangle budget disappears into ground detail:
```
Object: a low-poly floating island, 6 m across, with one pine tree and a small cabin
Style: flat-shaded low-poly, faceted, no textures
Size: 6 m wide, 3 m tall including tree, 6 m deep; island bottom is a rocky cone
Parts to expose: tree, cabin, island
Idle animation: whole island bobs 0.1 m over 5 s
```

### Built in Blender (`--format blender`)
Every 3D recipe above works unchanged. Three things differ:

- Say "the ground", not `y = 0`. Blender is Z-up; the contract handles the
  axis conversion, and `check.mjs` measures the exported file in three.js space.
- Part names become glTF node names, so keep them to lowercase letters,
  digits and underscores. three.js rewrites dots and spaces.
- An idle animation is keyframed and exported as glTF clips, so give it a
  loop: "rotor_1..rotor_4 spin one full turn every 0.2 s; body bobs 0.03 m
  over a 2 s loop". The page plays it with an `AnimationMixer` or ignores it.

Name Blender's strengths when the form needs them: "subdivision-surface body
with bevelled panel gaps", "boolean-cut wheel arches", "curve-based exhaust
pipes". Materials still have to be plain values per surface, because
procedural shader nodes and image textures do not survive the glTF export.

### 2D vector object (`--format svg`)
```
Object: side view (profile, nose pointing right) of <thing>
Use: slides across the screen; wheels spin, so each wheel is its own pivot group
Style: flat vector with subtle gradient shading; no stroke heavier than 2 units on a 1000-wide viewBox
Colours: <per part: body #hex, shade #hex, glass #hex, highlight #hex>
Size: viewBox about 1000 x 360, artwork fills it with a 20-unit margin
Parts to expose: wheel-front, wheel-rear (pivot at hub centre), body, glass, headlight, shadow
Constraints: no text, no logos, ground line at the bottom of the viewBox
```
Groups are placed with `transform="translate(x y)"` so `rotate()` in CSS or
JS turns them about their own centre.

## Reference images

`--input photo.jpg` attaches a reference Sol can inspect. Use it for the
real thing's proportions ("match the attached car's stance and roofline") or
for palette ("match the colours of the attached site screenshot"). Say which
role each attachment plays in the spec.

## Consistent sets

For several objects that must look related, hold the `Style`, `Materials`
and `Constraints` lines constant and change only `Object`, `Size` and
`Parts to expose`. Run them in parallel background calls with separate
`--out` folders; each is an isolated Codex thread.

## Revising: one change per round

```bash
python3 ~/.claude/skills/codex-3d/scripts/codex_3d.py --out public/3d/car \
  --revise "lower the roofline by 10 cm and widen the rear haunches; keep everything else"
```

The wrapper attaches current renders and supplies the saved brief. Sol reads
the source and prior notes, including Claude's contributions between runs.
Describe the defect you observed, the intended change, and what must stay the
same. Keep each round focused so both agents can assess whether it worked.

## Failure modes and fixes

| Symptom | Fix |
|---|---|
| Boxes-and-cylinders look | Spec was vague. Add defining shapes and "stylised realism, continuous surfaces"; send a focused revision; reasoning stays at `max` |
| Wrong scale, tiny or huge | Put real metres in `Size`; `--revise "scale to N m long"` |
| Wheels or feet float / sink | `--revise "all tyres must touch y = 0 exactly; wheel centres at y = radius"` |
| Parts merged, cannot animate | `--revise "expose X as its own pivot group in userData.parts, pivot at <where>"` |
| Flat, plasticky materials | Give metalness / roughness / clearcoat per surface; make sure the page has an environment map |
| Object faces the wrong way | Contract says front faces +Z; `--revise "rotate so the front faces +Z"` |
| Too many triangles | Lower the budget in `Constraints`; ask for fewer segments on hidden parts |
| Render shows a red error page | Runtime error in `object.js`. The review pass usually fixes it; otherwise `--revise` with the error text |
| Blender build looks right in Blender, flat or grey in the render | A procedural shader did not export. `--revise "use plain Principled BSDF values on <part>"` |
| `getObjectByName('<part>')` returns undefined for a Blender build | The Blender name had a dot or space and three.js rewrote it; `check.mjs` warns about it. `--revise "rename <part> to snake_case"` |
| SVG parts rotate around the wrong point | `--revise "put each wheel in its own <g> translated to the hub centre with the circle at 0,0"` |
| SVG looks like clip art | Ask for gradient shading, a highlight stroke, and a soft ground shadow; name the medium |
| Review pass made it worse | `--passes 1` and drive changes yourself with `--revise`, one at a time |
