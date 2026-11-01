import importlib.util
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import fleetlib as fleet


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load('worker', 'sync-fleet.py')
installer = load('installer', 'install-skill-sync.py')


class WorkerTests(unittest.TestCase):
    def test_remote_archive_installation_and_repeat_without_real_profiles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local, remote = root / 'local', root / 'remote'
            local.mkdir(); remote.mkdir()
            config = {'state_dir': '~/state', 'profiles': {'claude': {'home': '~/.claude', 'driver': 'claude', 'identity': 'secondary'}},
                      'machines': {'peer': {'os': 'macos', 'ssh': 'fixture-peer'}}, 'sync': {'host': 'here', 'machines': ['peer'], 'include_native': True}}
            fleet.atomic_write(remote / '.claude/settings.json', 'private settings')
            release = local / 'state/releases' / ('a' * 20)
            fleet.atomic_write(release / 'skills/shared/SKILL.md', 'fixture')
            fleet.atomic_write(release / 'manifest.json', fleet.encode({'revision': release.name, 'skills': {'shared': fleet.tree_hash(release / 'skills/shared')}}))
            source = root / 'source'
            for name in worker.RUNTIME_FILES:
                if name == 'fleet.json':
                    fleet.atomic_write(source / name, fleet.encode(config))
                else:
                    (source / name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(worker.ROOT / name, source / name)
            remote_env = dict(os.environ, FLEET_USER_HOME=str(remote))
            remote_env.pop('FLEET_STATE_DIR', None)
            scripts = []
            def ssh(machine, script):
                self.assertEqual(machine['ssh'], 'fixture-peer')
                scripts.append(script)
                return subprocess.check_output([sys.executable, '-'], input=script, text=True, env=remote_env).strip()
            def transfer(argv, **kwargs):
                self.assertEqual(argv[0], 'scp')
                shutil.copy2(argv[-2], argv[-1].split(':', 1)[1])
                return ''
            with patch.dict(os.environ, {'FLEET_USER_HOME': str(local), 'FLEET_STATE_DIR': str(local / 'state')}), \
                    patch.object(worker, 'ROOT', source), patch.object(worker, 'config_path', return_value=source / 'fleet.json'), \
                    patch.object(worker, 'ssh_python', side_effect=ssh), patch.object(worker, 'command', side_effect=transfer):
                runtime = worker.runtime_archive(local / 'state')
                first = worker.sync_remote(config, 'peer', release, runtime)
                second = worker.sync_remote(config, 'peer', release, runtime)
            self.assertEqual(first['changed'], 1)
            self.assertEqual(second['changed'], 0)
            self.assertEqual(first['problems'], [])
            # The peer's state comes from fleet.json's state_dir under the peer's own home.
            self.assertTrue((remote / 'state/releases' / release.name).is_dir())
            self.assertEqual((remote / '.claude/skills/shared/SKILL.md').read_text(), 'fixture')
            self.assertEqual((remote / '.claude/settings.json').read_text(), 'private settings')
            launcher = (remote / '.local/bin/fleet').read_text()
            self.assertIn(str(remote / 'state/sync-runtime'), launcher)
            self.assertFalse(any(str(local) in script for script in scripts))

    def test_remote_python_and_launcher_follow_fleet_json(self):
        self.assertEqual(worker.remote_python({'os': 'windows'}), 'python')
        self.assertEqual(worker.remote_python({'os': 'macos', 'python': '/opt/homebrew/bin/python3'}), '/opt/homebrew/bin/python3')
        self.assertEqual(worker.remote_launcher({'os': 'windows', 'work_root': 'D:\\AgentWork'}), 'D:\\AgentWork\\bin\\fleet.cmd')
        self.assertIsNone(worker.remote_launcher({'os': 'windows'}))
        self.assertEqual(worker.remote_launcher({'os': 'linux'}), '~/.local/bin/fleet')

    def test_unreachable_peer_is_recorded_and_others_continue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {'state_dir': '~/state', 'profiles': {}, 'machines': {'a': {'ssh': 'a'}, 'b': {'ssh': 'b'}},
                      'sync': {'host': 'here', 'machines': ['a', 'b']}}
            release = root / 'state/releases' / ('b' * 20)
            fleet.atomic_write(release / 'skills/shared/SKILL.md', 'fixture')
            fleet.atomic_write(release / 'manifest.json', fleet.encode({'revision': release.name, 'skills': {'shared': fleet.tree_hash(release / 'skills/shared')}}))
            fleet.atomic_write(root / 'state/latest-release.json', fleet.encode({'revision': release.name}))
            def remote(config, name, release, runtime):
                if name == 'a':
                    raise ValueError('ssh failed: offline')
                return {'revision': release.name, 'profiles': 0, 'changed': 0, 'problems': []}
            with patch.dict(os.environ, {'FLEET_USER_HOME': str(root), 'FLEET_STATE_DIR': str(root / 'state'), 'FLEET_MACHINE': 'here'}), \
                    patch.object(worker, 'runtime_archive', return_value=None), patch.object(worker, 'sync_remote', side_effect=remote), \
                    patch.object(worker.sys, 'stderr'):
                results = worker.run(config)
            self.assertEqual(set(results), {'here', 'a', 'b'})
            self.assertIn('offline', results['a']['error'])
            self.assertEqual(fleet.read_json(root / 'state/fleet-sync-status.json')['machines'], results)


class InstallerTests(unittest.TestCase):
    def test_launch_agent_label_and_interval_come_from_fleet_json(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'FLEET_STATE_DIR': str(Path(temp) / 'state')}):
            path, job = installer.launch_agent({'state_dir': '~/state', 'sync': {'host': 'here'}}, home=temp)
            self.assertEqual(job['Label'], 'org.sharedfleet.skill-sync')
            self.assertEqual(job['StartInterval'], 120)
            self.assertEqual(path, Path(temp) / 'Library/LaunchAgents/org.sharedfleet.skill-sync.plist')
            path, job = installer.launch_agent({'state_dir': '~/state', 'sync': {'service_label': 'org.example.fleet-sync', 'interval_seconds': 300}}, home=temp)
            self.assertEqual((job['Label'], job['StartInterval'], path.name), ('org.example.fleet-sync', 300, 'org.example.fleet-sync.plist'))
            self.assertTrue(job['ProgramArguments'][-1].endswith('scripts/sync-fleet.py'))
            plistlib.dumps(job)


if __name__ == '__main__':
    unittest.main()
