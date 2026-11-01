#!/usr/bin/env python3
"""Choose and export a reviewed subset of the shared fleet. Never install services."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def load_catalog(root=ROOT):
    return json.loads((root / 'bank/catalog.json').read_text())


def choose(catalog, preset=None, modules=(), skills=(), without=(), skip_skills=()):
    if preset and preset not in catalog['presets']:
        raise ValueError(f'Unknown preset: {preset}')
    start = catalog['presets'].get(preset, {'modules': [], 'skills': []})
    chosen_modules = set(start['modules']) | set(modules)
    chosen_skills = set(start['skills']) | set(skills)
    excluded_modules, excluded_skills = set(without), set(skip_skills)
    for names, known, kind in [(chosen_modules | excluded_modules, catalog['modules'], 'module'),
                               (chosen_skills | excluded_skills, catalog['skills'], 'skill')]:
        unknown = names - known.keys()
        if unknown:
            raise ValueError(f'Unknown {kind}: {", ".join(sorted(unknown))}')
    chosen_modules -= excluded_modules
    chosen_skills -= excluded_skills
    while True:
        before = (set(chosen_modules), set(chosen_skills))
        for name in list(chosen_modules):
            entry = catalog['modules'][name]
            chosen_modules.update(entry['requires'])
            chosen_skills.update(set(entry['skills']) - excluded_skills)
        for name in chosen_skills:
            chosen_modules.update(catalog['skills'][name]['requires'])
        if chosen_modules & excluded_modules:
            raise ValueError('Selection still needs excluded modules: ' + ', '.join(sorted(chosen_modules & excluded_modules)) + '. Skip dependent skills/modules or choose another preset.')
        if before == (chosen_modules, chosen_skills):
            break
    if not chosen_modules and not chosen_skills:
        raise ValueError('Choose at least one module, skill or preset. Run list to see options.')
    return {'preset': preset, 'modules': sorted(chosen_modules), 'skills': sorted(chosen_skills),
            'external_setup': sorted({step for name in chosen_modules for step in catalog['modules'][name]['external_setup']})}


def selection_files(catalog, selection, root=ROOT):
    inventory = json.loads((root / 'bank/files.json').read_text())
    prefixes = ['LICENSE', 'AGENTS.md', 'CLAUDE.md', 'docs/skills.md', 'docs/install-skills.md', 'docs/provenance.md', 'docs/public-release.md', 'bank/licenses']
    for name in selection['modules']:
        prefixes.extend(catalog['modules'][name]['files'])
    for name in selection['skills']:
        prefixes.append(catalog['skills'][name]['path'])
    result = {}
    for prefix in prefixes:
        matches = {name: value for name, value in inventory.items() if name == prefix or name.startswith(prefix + '/')}
        if not matches:
            raise ValueError(f'Missing bank source: {prefix}')
        result.update(matches)
    # Verify every selected source before making any output directory.
    for name, expected in result.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name:
            raise ValueError(f'Unsafe bank path: {name}')
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f'Unsafe bank source: {name}')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Bank source changed: {name}. Review/rebuild its inventory before exporting.')
    return result


def export(catalog, selection, destination, root=ROOT):
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Output already exists; choose a new folder.')
    if destination.resolve().is_relative_to(root.resolve()):
        raise ValueError('Export outside the source checkout to keep local state out of future shares.')
    sources = selection_files(catalog, selection, root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.fleet-bank-', dir=destination.parent))
    try:
        for name in sources:
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, target)
        # Fleet release only sees the selected custom and vendored skills.
        external = json.loads((root / 'skills/external.lock.json').read_text())
        lock = {'skills': [e for e in external['skills'] if e['name'] in selection['skills']]}
        (staging / 'skills').mkdir(exist_ok=True)
        (staging / 'skills/external.lock.json').write_text(json.dumps(lock, indent=2) + '\n')
        (staging / 'selection.json').write_text(json.dumps(selection, indent=2) + '\n')
        lines = ['# Your selected fleet bank', '', 'This is a source bundle. Nothing has been installed, signed in or enabled.', '',
                 '## Modules', '']
        lines.extend(f'- [{name}](docs/modules/{name}.md)' for name in selection['modules'])
        lines.extend(['', '## Skills', ''])
        lines.extend(f'- [{name}]({catalog["skills"][name]["path"]}/SKILL.md)' for name in selection['skills'])
        lines.extend(['', 'Follow [skill installation](docs/install-skills.md) for the selected skill folders.', '',
                      'Read the selected recipes before setup. Set your own paths and identities. Keep credentials and generated state outside this bundle.', '',
                      'For the complete catalog or a different selection, return to the original shared-fleet checkout.', '', '## External setup', ''])
        lines.extend(f'- {step}' for step in selection['external_setup'])
        (staging / 'README.md').write_text('\n'.join(lines) + '\n')
        hashes = {p.relative_to(staging).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(staging.rglob('*')) if p.is_file()}
        (staging / 'bundle-manifest.json').write_text(json.dumps(hashes, indent=2) + '\n')
        # A concurrently created output is preserved too.
        destination.mkdir()
        try:
            for child in staging.iterdir():
                os.rename(child, destination / child.name)
        except BaseException:
            # Do not remove a partially exported folder, which may now be inspected.
            raise
        return destination
    finally:
        shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('list')
    for action in ['plan', 'pull']:
        p = sub.add_parser(action)
        p.add_argument('--preset')
        for flag in ['module', 'skill', 'without', 'skip-skill']:
            p.add_argument('--' + flag, action='append', default=[])
        if action == 'pull':
            p.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        catalog = load_catalog()
        if args.action == 'list':
            for kind in ['presets', 'modules', 'skills']:
                print(kind.upper())
                for name, entry in catalog[kind].items():
                    print(f'  {name}: {entry.get("use_when", entry.get("description", ""))}')
            return
        selection = choose(catalog, args.preset, args.module, args.skill, args.without, args.skip_skill)
        if args.action == 'plan':
            print(json.dumps(selection, indent=2))
        else:
            print(export(catalog, selection, args.output))
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    main()
