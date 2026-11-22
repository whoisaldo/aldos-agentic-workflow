"""Offline regression checks for model pinning and the collaborative handoff."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "codex_3d.py"
spec = importlib.util.spec_from_file_location("codex_3d", MODULE)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.fmt = "three"
        self.failure_pass = None
        self.failure_code = 1

    def invoke(self, *arguments):
        out = self.root / self.fmt
        argv = [str(MODULE), "--out", str(out), "--format", self.fmt, "--json", *arguments]
        stdout = io.StringIO()

        def prepare(build_dir, fmt):
            build_dir.mkdir(parents=True, exist_ok=True)

        def run(args, prompt, images, timeout, label):
            self.calls.append((args, prompt, images, label))
            if len(self.calls) == self.failure_pass:
                return "thread-test", "model not supported for this account", self.failure_code
            (out / driver.FILES[self.fmt]["object"]).write_text("modeled artifact")
            if self.fmt == "blender":
                (out / "object.blend").write_text("editable source")
            return "thread-test", "Built the requested parts; inspect the renders.", 0

        def render(build_dir, fmt, *unused):
            return {view: {"png": str(build_dir / driver.VIEW_PNG[view]), "status": {"ok": True}}
                    for view in driver.FILES[fmt]["views"]}

        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            for name, value in {
                "find_chrome": lambda _: "chrome-test",
                "prepare_build_dir": prepare,
                "render": render,
                "run_codex": run,
                "find_blender": lambda _: "blender-test",
                "find_blender_mcp": lambda: ["mcp-test"],
                "start_blender": lambda *args: (None, 12345, "isolated-scene"),
                "blender_mcp_args": lambda *args: [],
            }.items():
                stack.enter_context(patch.object(driver, name, value))
            stack.enter_context(patch("sys.argv", argv))
            try:
                code = driver.main()
            except SystemExit as exc:
                code = exc.code
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None

    def assert_pinned(self):
        for args, _, _, _ in self.calls:
            self.assertEqual(args[args.index("-m") + 1], "gpt-6.2-sol")
            self.assertIn('model_reasoning_effort="max"', args)
            disabled = [args[i + 1] for i, arg in enumerate(args) if arg == "--disable"]
            self.assertIn("multi_agent", disabled)
            self.assertIn("multi_agent_v2", disabled)

    def test_build_revision_and_self_review_are_pinned_for_all_formats(self):
        for self.fmt in ("three", "svg", "blender"):
            with self.subTest(format=self.fmt):
                self.calls.clear()
                code, result = self.invoke("--prompt", "Expose the lid hinge", "--passes", "2")
                self.assertEqual(code, 0)
                self.assertTrue(result["ok"])
                self.assertEqual(len(self.calls), 2)
                self.assertEqual(self.calls[1][0][0], "resume")
                self.assert_pinned()
                self.calls.clear()
                code, result = self.invoke("--revise", "Increase lid clearance", "--passes", "2")
                self.assertEqual(code, 0)
                self.assertEqual(len(self.calls), 2)
                self.assertIn("Expose the lid hinge", self.calls[0][1])
                self.assertIn("Increase lid clearance", self.calls[0][1])
                self.assertIn("notes.md", self.calls[0][1])
                self.assertTrue(self.calls[0][2])
                self.assert_pinned()

    def test_default_returns_to_claude_after_one_pass(self):
        code, result = self.invoke("--prompt", "A hinged box")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls), 1)
        notes = Path(result["notes"]).read_text()
        self.assertIn("Built the requested parts", notes)
        self.assertEqual(result["model"], "gpt-6.2-sol")
        self.assertEqual(result["effort"], "max")

    def test_model_and_effort_overrides_are_rejected_before_work(self):
        for option in (("--model", "gpt-6-astra"), ("--effort", "high"), ("--passes", "0")):
            with self.subTest(option=option):
                code, _ = self.invoke("--prompt", "A box", *option)
                self.assertEqual(code, 2)
                self.assertFalse(self.calls)
                self.assertFalse((self.root / self.fmt).exists())

    def test_failed_revision_cannot_report_a_stale_artifact_as_success(self):
        self.invoke("--prompt", "A box")
        self.calls.clear()
        self.failure_pass = 1
        code, result = self.invoke("--revise", "Move the hinge")
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertIn("model not supported", result["error"])
        self.assertIn("No fallback", result["error"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.root / self.fmt / "object.js").read_text(), "modeled artifact")

    def test_failed_or_timed_out_self_review_fails_the_run(self):
        for rc in (1, 124):
            with self.subTest(returncode=rc):
                self.calls.clear()
                self.failure_pass, self.failure_code = 2, rc
                code, result = self.invoke("--prompt", "A box", "--passes", "3")
                self.assertEqual(code, 1)
                self.assertFalse(result["ok"])
                self.assertEqual(len(self.calls), 2)

    def test_updated_brief_persists_and_render_only_never_calls_codex(self):
        self.invoke("--prompt", "A box")
        code, _ = self.invoke("--revise", "Adjust lid", "--prompt", "A box with brass hinges")
        self.assertEqual(code, 0)
        self.calls.clear()
        self.invoke("--revise", "Polish the hinges")
        self.assertIn("A box with brass hinges", self.calls[0][1])
        self.calls.clear()
        code, _ = self.invoke("--render-only")
        self.assertEqual(code, 0)
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
