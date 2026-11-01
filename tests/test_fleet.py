import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("fleetlib", Path(__file__).resolve().parents[1] / "scripts/fleetlib.py")
fleet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet)


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"FLEET_USER_HOME": str(self.root), "FLEET_STATE_DIR": str(self.root / "state")})
        self.env.start()
        self.config = {"state_dir": "~/state", "profiles": {"native": {"home": "~/.codex", "driver": "codex", "identity": "personal", "managed": False}, "new": {"home": "~/.codex_fleet", "driver": "codex", "identity": "personal", "managed": True, "model": "gpt-6-astra", "effort": "xhigh"}}, "gateway": {"runtime_dir": "~/gateway", "url": "http://127.0.0.1:8317"}}
        fleet.atomic_write(self.root / "gateway/private/client.json", fleet.encode({"api_key": "test-key", "management_key": "test-management"}))
        fleet.atomic_write(self.root / "state/gateway-keys/personal.json", fleet.encode({"api_key": "test-key"}))
        self.release = self.root / "state/releases/1234567890abcdef1234"
        skill = self.release / "skills/example/SKILL.md"
        fleet.atomic_write(skill, "---\nname: example\ndescription: fixture\n---\n")
        fleet.atomic_write(self.release / "manifest.json", fleet.encode({"revision": self.release.name, "skills": {"example": fleet.tree_hash(skill.parent)}}))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_native_and_alias_targets_are_protected(self):
        with self.assertRaisesRegex(ValueError, "read-only"):
            fleet.render_profile(self.config, "native", self.release)
        self.config["profiles"]["new"]["home"] = "~/.codex"
        if os.name == "nt":
            # Windows always derives managed homes under its separate state dir.
            self.assertEqual(fleet.profile(self.config, "new")["home"], self.root / "state/profiles/new")
        else:
            with self.assertRaisesRegex(ValueError, "existing native"):
                fleet.profile(self.config, "new")

    def test_unknown_toml_keys_and_tables_survive(self):
        original = 'model = "old"\ncustom = "keep"\n\n[mcp_servers.local]\ncommand = "server"\n'
        merged = tomllib.loads(fleet.merge_toml_top(original, {"model": "new", "openai_base_url": "http://localhost:8317/v1"}))
        self.assertEqual(merged["custom"], "keep")
        self.assertEqual(merged["mcp_servers"]["local"]["command"], "server")
        self.assertEqual(merged["model"], "new")

    def test_deployment_is_idempotent_and_blocks_updates_after_use(self):
        fleet.deploy_profile(self.config, "new", self.release, apply=True)
        home = fleet.profile(self.config, "new")["home"]
        fleet.atomic_write(home / "sessions/session.json", "{}")
        self.assertEqual(fleet.deploy_profile(self.config, "new", self.release, apply=True), 0)
        self.config["profiles"]["new"]["model"] = "changed"
        before = (home / "config.toml").read_bytes()
        with self.assertRaisesRegex(ValueError, "session history"):
            fleet.deploy_profile(self.config, "new", self.release, apply=True)
        self.assertEqual((home / "config.toml").read_bytes(), before)

    def test_existing_skill_is_never_replaced(self):
        skill = fleet.profile(self.config, "new")["home"] / "skills/example/SKILL.md"
        fleet.atomic_write(skill, "user-owned")
        with self.assertRaisesRegex(ValueError, "Existing skill is preserved"):
            fleet.deploy_profile(self.config, "new", self.release, apply=True)
        self.assertEqual(skill.read_text(), "user-owned")

    def test_release_tampering_is_detected(self):
        (self.release / "skills/example/SKILL.md").write_text("modified")
        with self.assertRaisesRegex(ValueError, "differs"):
            fleet.verify_release(self.release)

    def test_release_creation_seals_content_and_reuses_existing_revision(self):
        source = self.root / "source"
        fleet.atomic_write(source / "skills/local/SKILL.md", "local skill")
        with patch.object(fleet, "ROOT", source):
            released = fleet.release(self.config)
            self.assertTrue((released / "manifest.json").is_file())
            self.assertEqual(fleet.release(self.config), released)
            self.assertFalse((released / "skills/local/SKILL.md").stat().st_mode & 0o222)

    def test_skill_hash_order_is_the_same_on_windows_and_macos(self):
        root = self.root / "ordering"
        names = ["Z.md", "a/z.md", "a.txt", "b.md"]
        expected = hashlib.sha256()
        for name in names:
            fleet.atomic_write(root / name, name)
            expected.update(name.encode() + b"\0" + hashlib.sha256(name.encode()).digest())
        self.assertEqual(fleet.tree_hash(root), expected.hexdigest())
        fleet.atomic_write(root / "unicode.txt", "caf\u00e9\n")
        self.assertEqual((root / "unicode.txt").read_bytes(), "caf\u00e9\n".encode("utf-8"))

    def test_secondary_identity_uses_its_key_and_excludes_personal_bridges(self):
        self.config["additional_gateways"] = {"secondary": {"runtime_dir": "~/secondary-gateway", "url": "http://127.0.0.1:8318"}}
        fleet.atomic_write(self.root / "secondary-gateway/private/client.json", fleet.encode({"api_key": "secondary-key"}))
        fleet.atomic_write(self.root / "state/gateway-keys/secondary.json", fleet.encode({"api_key": "secondary-key"}))
        self.config["profiles"]["secondary"] = {"home": "~/.claude_fleet_secondary", "managed": True, "driver": "claude", "identity": "secondary", "gateway": "secondary", "model": "claude-opus-5-5", "effort": "xhigh"}
        bridge = self.release / "skills/bridge"
        fleet.atomic_write(bridge / "SKILL.md", "fixture bridge")
        fleet.atomic_write(bridge / ".claude-only", "")
        manifest = fleet.read_json(self.release / "manifest.json")
        manifest["skills"]["bridge"] = fleet.tree_hash(bridge)
        fleet.atomic_write(self.release / "manifest.json", fleet.encode(manifest))
        home = fleet.profile(self.config, "secondary")["home"]
        fleet.atomic_write(home / "settings.json", fleet.encode({"unrelated": True, "env": {"KEEP": "value"}}))
        _, files, links = fleet.render_profile(self.config, "secondary", self.release)
        settings = json.loads(files[home / "settings.json"])
        self.assertTrue(settings["unrelated"])
        self.assertEqual(settings["env"]["KEEP"], "value")
        self.assertEqual(settings["env"]["ANTHROPIC_AUTH_TOKEN"], "secondary-key")
        self.assertNotIn("CODEX_HOME", settings["env"])
        self.assertNotIn(home / "skills/bridge", links)

    def test_leases_exclude_competing_builds(self):
        with fleet.lease(self.config, "heavy-build"):
            with self.assertRaisesRegex(ValueError, "already reserved"):
                with fleet.lease(self.config, "heavy-build"):
                    pass
        self.assertFalse((self.root / "state/leases/heavy-build").exists())

    def test_task_keeps_dirty_checkout_unchanged(self):
        repo = self.root / "project"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
        (repo / "file.txt").write_text("base")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
        (repo / "file.txt").write_text("uncommitted work")
        branch = subprocess.check_output(["git", "-C", str(repo), "branch", "--show-current"])
        fleet.make_task(self.config, repo, "test-task", "native", "fixture checks pass")
        self.assertEqual((repo / "file.txt").read_text(), "uncommitted work")
        self.assertEqual(subprocess.check_output(["git", "-C", str(repo), "branch", "--show-current"]), branch)

    def test_t3_server_owned_by_ssh_client_is_reported(self):
        t3 = self.root / ".t3"
        fleet.atomic_write(t3 / "userdata/server-runtime.json", fleet.encode({"pid": 4242, "port": 3773}))
        for key, managed, pid in [("live", "managed", "4242"), ("old", "managed", "17"), ("kept", "external", None)]:
            fleet.atomic_write(t3 / f"ssh-launch/{key}/managed", managed + "\n")
            if pid:
                fleet.atomic_write(t3 / f"ssh-launch/{key}/pid", pid + "\n")
        self.assertEqual(fleet.t3_ssh_owned(t3), ["live"])
        fleet.atomic_write(t3 / "ssh-launch/live/managed", "external\n")
        self.assertEqual(fleet.t3_ssh_owned(t3), [])


if __name__ == "__main__":
    unittest.main()
