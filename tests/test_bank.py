import hashlib
import importlib.util
import json
import os
from unittest.mock import patch
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import bank
import fleetlib


class BankTests(unittest.TestCase):
    def setUp(self):
        self.catalog = bank.load_catalog()

    def test_work_selection_never_pulls_school_or_personal_bridges(self):
        selected = bank.choose(self.catalog, 'work-code')
        self.assertFalse({'canvas-lms', 'openclaw-canvas', 'proxy', 'personal-bridges'} & set(selected['modules']))
        self.assertFalse({'canvas-planning', 'codex-image', 'codex-3d', 'codex-computer-use'} & set(selected['skills']))

    def test_both_canvas_features_include_their_required_assistant(self):
        selected = bank.choose(self.catalog, modules=['canvas-lms', 'openclaw-canvas'])
        self.assertEqual(set(selected['modules']), {'canvas-lms', 'openclaw-canvas', 'openclaw'})
        self.assertEqual(selected['skills'], ['canvas-planning'])

    def test_canvas_export_is_a_capability_guide_without_automation(self):
        selected = bank.choose(self.catalog, modules=['canvas-lms'])
        with tempfile.TemporaryDirectory() as temp:
            output = bank.export(self.catalog, selected, Path(temp) / 'kit')
            skill = output / 'bank/skills/canvas-planning'
            self.assertEqual(sorted(p.name for p in skill.iterdir()), ['SKILL.md'])
            self.assertFalse((output / 'bank/automation').exists())
            catalog = json.loads((ROOT / 'bank/openclaw-source-catalog.json').read_text())
            self.assertTrue(all('eligible' not in s and 'disabled' not in s for s in catalog['skills']))

    def test_conflicting_exclusion_and_unknown_names_fail(self):
        with self.assertRaises(ValueError):
            bank.choose(self.catalog, 'student', without=['openclaw'])
        with self.assertRaises(ValueError):
            bank.choose(self.catalog, skills=['not-a-real-skill'])

    def test_minimal_export_is_selective_and_preserves_existing_outputs(self):
        selected = bank.choose(self.catalog, 'minimal')
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'kit'
            bank.export(self.catalog, selected, output)
            self.assertTrue((output / 'skills/unslop/SKILL.md').exists())
            self.assertFalse((output / 'bank/skills/canvas-planning').exists())
            self.assertFalse((output / 'scripts/gateway.py').exists())
            self.assertFalse((output / 'vendor/skills/brag').exists())
            manifest = json.loads((output / 'bundle-manifest.json').read_text())
            for name, expected in manifest.items():
                self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), expected)
            before = (output / 'README.md').read_bytes()
            with self.assertRaises(ValueError):
                bank.export(self.catalog, selected, output)
            self.assertEqual((output / 'README.md').read_bytes(), before)

    def test_uninventoried_files_are_excluded_and_changed_source_blocks_export(self):
        selected = bank.choose(self.catalog, skills=['unslop'])
        files = bank.selection_files(self.catalog, selected)
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            for name in set(files) | {'bank/files.json', 'skills/external.lock.json'}:
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, target)
            (source / 'skills/unslop/config.json').write_text('private fixture')
            output = Path(temp) / 'kit'
            bank.export(self.catalog, selected, output, root=source)
            self.assertFalse((output / 'skills/unslop/config.json').exists())
            (source / 'skills/unslop/SKILL.md').write_text('changed fixture')
            rejected = Path(temp) / 'rejected'
            with self.assertRaisesRegex(ValueError, 'Bank source changed'):
                bank.export(self.catalog, selected, rejected, root=source)
            self.assertFalse(rejected.exists())

    def test_every_preset_exports_and_subset_fleet_release_contains_only_selection(self):
        for name in self.catalog['presets']:
            with self.subTest(preset=name), tempfile.TemporaryDirectory() as temp:
                selected = bank.choose(self.catalog, name)
                output = bank.export(self.catalog, selected, Path(temp) / 'kit')
                self.assertEqual(json.loads((output / 'selection.json').read_text()), selected)
                if name in {'creative', 'remote'}:
                    previous = fleetlib.ROOT
                    try:
                        fleetlib.ROOT = output
                        with patch.dict(os.environ, {'FLEET_STATE_DIR': str(Path(temp) / 'state')}):
                            release = fleetlib.release({'state_dir': str(Path(temp) / 'state')})
                        actual = set(fleetlib.verify_release(release)['skills'])
                        expected = {s for s in selected['skills'] if not self.catalog['skills'][s]['path'].startswith('bank/')}
                        self.assertEqual(actual, expected)
                    finally:
                        fleetlib.ROOT = previous

    def test_every_export_carries_the_mit_notice_and_vendored_licenses(self):
        for name in self.catalog['presets']:
            with self.subTest(preset=name), tempfile.TemporaryDirectory() as temp:
                selected = bank.choose(self.catalog, name)
                output = bank.export(self.catalog, selected, Path(temp) / 'kit')
                self.assertEqual((output / 'LICENSE').read_bytes(), (ROOT / 'LICENSE').read_bytes())
                notices = [p.name.removesuffix('-LICENSE.txt') for p in (output / 'bank/licenses').iterdir()]
                for skill in selected['skills']:
                    if self.catalog['skills'][skill]['path'].startswith('vendor/'):
                        self.assertTrue([n for n in notices if skill.startswith(n)], skill)

    def test_automation_is_disabled(self):
        for path in (ROOT / 'bank/automation').glob('*.json'):
            self.assertIs(json.loads(path.read_text())['enabled'], False)

    def test_vendored_skills_match_their_pinned_upstream_trees(self):
        # Vendored skills stay byte-for-byte upstream; the pin is the tree of the upstream release.
        upstream = {'design-taste-frontend': '834b1946a8ffa40bc068ec4064709668a812aa36eb0eb348d39638d8c0de8b81'}
        for entry in json.loads((ROOT / 'skills/external.lock.json').read_text())['skills']:
            with self.subTest(entry['name']):
                self.assertEqual(fleetlib.tree_hash(ROOT / entry['path']), entry['sha256'])
                self.assertEqual(entry['sha256'], upstream.get(entry['name'], entry['sha256']))


if __name__ == '__main__':
    unittest.main()
