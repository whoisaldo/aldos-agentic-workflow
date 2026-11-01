import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fleetlib as fleet
import skills_sync as sync


class SkillSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'FLEET_USER_HOME': str(self.root), 'FLEET_STATE_DIR': str(self.root / 'state')})
        self.env.start()
        # Fictional homes under a temporary user directory, never a real profile.
        self.config = {'state_dir': '~/state', 'skill_policy': {'personal_identities': ['personal', 'secondary']},
                       'sync': {'include_native': True, 'shared_homes': ['~/.agents']}, 'profiles': {
            'personal': {'home': '~/.claude', 'driver': 'claude', 'identity': 'personal'},
            'secondary': {'home': '~/.claude_secondary', 'driver': 'claude', 'identity': 'secondary'},
            'work': {'home': '~/.claude_work', 'driver': 'claude', 'identity': 'work'},
            'codex': {'home': '~/.codex', 'driver': 'codex', 'identity': 'personal'},
        }}
        for p in self.config['profiles'].values():
            fleet.atomic_write(fleet.expand(p['home']) / 'settings.json', 'private settings')
        self.release = self.make_release('1' * 20, 'old')

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def make_release(self, revision, shared):
        release = self.root / 'state/releases' / revision
        for name, content, markers in [('shared', shared, []), ('bridge', 'personal bridge', ['.claude-only'])]:
            fleet.atomic_write(release / 'skills' / name / 'SKILL.md', content)
            for marker in markers:
                fleet.atomic_write(release / 'skills' / name / marker, '')
        fleet.atomic_write(release / 'manifest.json', fleet.encode({'revision': revision, 'skills': {p.name: fleet.tree_hash(p) for p in (release / 'skills').iterdir()}}))
        fleet.atomic_write(self.root / 'state/latest-release.json', fleet.encode({'revision': revision}))
        return release

    def test_policy_separates_provider_support_from_task_identity(self):
        report = sync.sync(self.config, apply=True)
        self.assertTrue(report['applied'])
        for name, p in self.config['profiles'].items():
            home = fleet.expand(p['home'])
            self.assertTrue((home / 'skills/shared/SKILL.md').is_file())
            self.assertEqual((home / 'skills/bridge').exists(), name in ('personal', 'secondary'))
            self.assertEqual((home / 'settings.json').read_text(), 'private settings')
        self.assertEqual(sync.status(self.config)['problems'], [])

    def test_default_policy_gives_claude_only_skills_to_the_personal_identity_alone(self):
        del self.config['skill_policy']
        sync.sync(self.config, apply=True)
        self.assertTrue((self.root / '.claude/skills/bridge').exists())
        self.assertFalse((self.root / '.claude_secondary/skills/bridge').exists())

    def test_native_homes_are_left_alone_unless_sync_opts_in(self):
        self.config['sync'] = {}
        self.config['profiles']['managed'] = {'home': '~/.claude_fleet', 'driver': 'claude', 'identity': 'personal', 'managed': True}
        (self.root / '.claude_fleet').mkdir()
        report = sync.sync(self.config, apply=True)
        self.assertEqual({c['profile'] for c in report['changes']}, {'managed'})
        self.assertFalse((self.root / '.claude/skills').exists())

    def test_live_update_preserves_credentials_histories_builtins_and_old_releases(self):
        home = self.root / '.claude_secondary'
        for name in ('auth.json', 'projects/session.jsonl', 'skills/.system/SKILL.md', 'skills/plugin-owned/SKILL.md'):
            fleet.atomic_write(home / name, 'preserve ' + name)
        sync.sync(self.config, apply=True)
        unchanged = (home / 'skills/bridge').resolve()
        new = self.make_release('2' * 20, 'new')
        drift = sync.status(self.config)
        self.assertEqual(len(drift['problems']), 4)
        self.assertTrue(all('outdated skill shared' in problem for problem in drift['problems']))
        result = sync.sync(self.config, apply=True)
        self.assertEqual((home / 'skills/shared').resolve(), (new / 'skills/shared').resolve())
        self.assertEqual((home / 'skills/bridge').resolve(), unchanged)
        self.assertEqual((self.release / 'skills/shared/SKILL.md').read_text(), 'old')
        self.assertTrue(json.loads((Path(result['backup']) / 'manifest.json').read_text())['complete'])
        for name in ('auth.json', 'projects/session.jsonl', 'skills/.system/SKILL.md', 'skills/plugin-owned/SKILL.md'):
            self.assertEqual((home / name).read_text(), 'preserve ' + name)
        self.assertEqual(sync.sync(self.config, apply=True)['changes'], [])

    def test_preview_changes_nothing(self):
        report = sync.sync(self.config)
        self.assertFalse(report['applied'])
        self.assertTrue(report['changes'])
        self.assertFalse((self.root / '.claude/skills').exists())

    def test_unmanaged_collision_blocks_all_writes(self):
        fleet.atomic_write(self.root / '.claude_secondary/skills/shared/SKILL.md', 'local edits')
        with self.assertRaisesRegex(ValueError, 'unmanaged'):
            sync.sync(self.config, apply=True)
        self.assertFalse((self.root / '.claude/skills/shared').exists())
        self.assertEqual((self.root / '.claude_secondary/skills/shared/SKILL.md').read_text(), 'local edits')

    def test_missing_skills_are_reported_for_native_profiles(self):
        report = sync.status(self.config)
        self.assertIn('secondary: missing skill bridge', report['problems'])
        self.assertIn('codex: missing skill shared', report['problems'])
        self.assertFalse(any('codex: missing skill bridge' in p for p in report['problems']))

    def test_tampered_release_cannot_overwrite_installed_skills(self):
        sync.sync(self.config, apply=True)
        new = self.make_release('2' * 20, 'new')
        (new / 'skills/shared/SKILL.md').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'differs'):
            sync.sync(self.config, apply=True)
        self.assertEqual((self.root / '.claude/skills/shared/SKILL.md').read_text(), 'old')

    def test_shared_root_receives_only_portable_skills(self):
        (self.root / '.agents/skills').mkdir(parents=True)
        sync.sync(self.config, apply=True)
        self.assertTrue((self.root / '.agents/skills/shared/SKILL.md').is_file())
        self.assertFalse((self.root / '.agents/skills/bridge').exists())

    def test_sync_lock_excludes_other_sync_and_releases_after_error(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with sync.sync_lock(self.config, 'fixture'):
                with self.assertRaisesRegex(ValueError, 'running'):
                    with sync.sync_lock(self.config, 'fixture'):
                        pass
                raise RuntimeError('fixture')
        with sync.sync_lock(self.config, 'fixture'):
            pass


if __name__ == '__main__':
    unittest.main()
