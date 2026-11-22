import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleetlib as fleet

spec = importlib.util.spec_from_file_location("migrate", Path(__file__).resolve().parents[1] / "scripts/migrate-skills.py")
migrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate)


class MigrationTests(unittest.TestCase):
    def test_migration_preserves_auth_history_unknown_skills_and_backups(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"FLEET_USER_HOME": temp, "FLEET_STATE_DIR": str(Path(temp) / "state")}):
            root = Path(temp)
            config = {"state_dir": "~/state", "profiles": {"native": {"home": "~/.claude", "driver": "claude", "identity": "secondary"}}}
            home = root / ".claude"
            for name, content in [("settings.json", "private settings"), ("auth.json", "private auth"), ("projects/session.jsonl", "history"), ("skills/custom/SKILL.md", "unrelated")]:
                fleet.atomic_write(home / name, content)
            release = root / "state/releases/1234567890abcdef1234"
            fleet.atomic_write(release / "skills/shared/SKILL.md", "new shared skill")
            fleet.atomic_write(release / "skills/bridge/SKILL.md", "personal bridge")
            fleet.atomic_write(release / "skills/bridge/.claude-only", "")
            fleet.atomic_write(release / "manifest.json", fleet.encode({"revision": release.name, "skills": {n: fleet.tree_hash(release / "skills" / n) for n in ("shared", "bridge")}}))
            fleet.atomic_write(home / "skills/shared/SKILL.md", "old shared skill")
            fleet.atomic_write(home / "skills/bridge/SKILL.md", "old bridge")
            changes = migrate.plan(config, release, ["native"])
            backup = migrate.apply(config, release, changes, lambda: None)
            self.assertEqual((home / "auth.json").read_text(), "private auth")
            self.assertEqual((home / "settings.json").read_text(), "private settings")
            self.assertEqual((home / "projects/session.jsonl").read_text(), "history")
            self.assertEqual((home / "skills/custom/SKILL.md").read_text(), "unrelated")
            self.assertEqual((home / "skills/shared/SKILL.md").read_text(), "new shared skill")
            self.assertEqual((home / "skills/shared").resolve(), (release / "skills/shared").resolve())
            self.assertFalse((home / "skills/bridge").exists())
            self.assertTrue(any(p.read_text() == "old bridge" for p in backup.glob("*/SKILL.md")))
            self.assertEqual(migrate.plan(config, release, ["native"]), [])

    def test_active_turn_blocks_migration_except_maintenance_owner(self):
        with patch.object(migrate, "read_json", return_value={"access_token": "fixture"}), patch.object(migrate, "t3_get", return_value={"threads": [{"id": "other", "session": {"activeTurnId": "turn"}}]}):
            with self.assertRaisesRegex(ValueError, "active"):
                migrate.assert_idle(Path("fixture"), ["maintenance"])
            migrate.assert_idle(Path("fixture"), ["other"])
        # Protocol 2 statuses, pending waits and unexpected snapshots also refuse.
        for shell, pattern in [
            ({"threads": [{"id": "p2", "status": "waiting"}]}, "active"),
            ({"threads": [{"id": "p2bg", "status": "idle", "pendingBackgroundTasks": [{"id": "t"}]}]}, "active"),
            ({"threads": [{"id": "mystery", "status": "unseen-state"}]}, "active"),
            ({"threads": [{"id": "bad"}]}, "cannot read"),
        ]:
            with patch.object(migrate, "read_json", return_value={"access_token": "fixture"}), patch.object(migrate, "t3_get", return_value=shell):
                with self.assertRaisesRegex(ValueError, pattern):
                    migrate.assert_idle(Path("fixture"), [])

    def test_preflight_detects_changes_before_touching_any_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "skill"
            fleet.atomic_write(path / "SKILL.md", "before")
            entry = {"path": str(path), "before": migrate.fingerprint(path)}
            (path / "SKILL.md").write_text("concurrent edit")
            with self.assertRaisesRegex(ValueError, "changed after planning"):
                migrate.apply({}, Path(temp), [entry], lambda: None)
            self.assertEqual((path / "SKILL.md").read_text(), "concurrent edit")

    def test_bridge_caches_live_outside_read_only_releases(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"AGENT_SKILL_CACHE_DIR": temp}):
            for name, filename in [("codex-image", "codex_image"), ("codex-3d", "codex_3d"), ("codex-computer-use", "codex_computer_use")]:
                spec = importlib.util.spec_from_file_location(filename, root / "skills" / name / "scripts" / (filename + ".py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                destination = getattr(module, "VENDOR", getattr(module, "VENV_DIR", None))
                self.assertTrue(destination.is_relative_to(Path(temp)))
                destination.mkdir(parents=True)
                (destination / "probe").write_text("writable")


if __name__ == "__main__":
    unittest.main()
