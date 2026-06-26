from __future__ import annotations

import os
import tempfile
import unittest

import control_panel


class FakeRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, cmd, timeout=20, cwd=None):
        key = tuple(cmd)
        self.calls.append((key, timeout, cwd))
        if key not in self.responses:
            raise AssertionError(f"Unexpected command: {cmd}")
        return self.responses[key]


class ControlPanelUpdateTests(unittest.TestCase):
    def test_perform_application_update_reports_up_to_date(self) -> None:
        runner = FakeRunner(
            {
                ("git", "rev-parse", "--is-inside-work-tree"): (0, "true"),
                ("git", "fetch", control_panel.UPDATE_REMOTE, "--quiet"): (0, ""),
                ("git", "status", "--porcelain"): (0, ""),
                ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): (0, "origin/main"),
                ("git", "rev-list", "--left-right", "--count", "HEAD...origin/main"): (0, "0 0"),
            }
        )

        result = control_panel.perform_application_update(command_runner=runner)

        self.assertEqual(result, {"ok": True, "changed": False, "message": "Application is already up to date."})
        self.assertFalse(any(call[0][:3] == ("git", "pull", "--ff-only") for call in runner.calls))

    def test_perform_application_update_skips_dirty_worktree(self) -> None:
        runner = FakeRunner(
            {
                ("git", "rev-parse", "--is-inside-work-tree"): (0, "true"),
                ("git", "fetch", control_panel.UPDATE_REMOTE, "--quiet"): (0, ""),
                ("git", "status", "--porcelain"): (0, " M control_panel.py"),
            }
        )

        result = control_panel.perform_application_update(command_runner=runner)

        self.assertEqual(
            result,
            {
                "ok": False,
                "changed": False,
                "message": "Automatic update skipped because the working tree has local changes.",
            },
        )

    def test_perform_application_update_pulls_when_remote_is_newer(self) -> None:
        runner = FakeRunner(
            {
                ("git", "rev-parse", "--is-inside-work-tree"): (0, "true"),
                ("git", "fetch", control_panel.UPDATE_REMOTE, "--quiet"): (0, ""),
                ("git", "status", "--porcelain"): (0, ""),
                ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): (0, "origin/main"),
                ("git", "rev-list", "--left-right", "--count", "HEAD...origin/main"): (0, "0 2"),
                ("git", "pull", "--ff-only", control_panel.UPDATE_REMOTE): (0, "Updating abc..def"),
            }
        )

        result = control_panel.perform_application_update(command_runner=runner)

        self.assertEqual(result, {"ok": True, "changed": True, "message": "Update installed. Restarting application."})
        self.assertTrue(any(call[0][:3] == ("git", "pull", "--ff-only") for call in runner.calls))


class ControlPanelProgressTests(unittest.TestCase):
    def test_progress_from_state_uses_global_pipeline_totals(self) -> None:
        progress = control_panel.progress_from_state(
            {
                "phase": "anime",
                "current_item": "Dr. STONE",
                "detail": "Season 04 evaluation",
                "failures": 2,
                "phases": {
                    "anime": {"done": 37, "total": 241},
                    "tv": {"done": 0, "total": 109},
                    "movies": {"done": 0, "total": 53},
                    "jellyfin": {"done": 0, "total": 1},
                },
            }
        )

        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress["percent"], 9)
        self.assertEqual(progress["phase"], "Anime")
        self.assertEqual(progress["processed"], 37)
        self.assertEqual(progress["total"], 404)
        self.assertEqual(progress["failures"], 2)
        self.assertEqual(progress["detail"], "Dr. STONE - Season 04 evaluation")

    def test_estimate_progress_prefers_persistent_state_while_running(self) -> None:
        payload = """{
  "phase": "movies",
  "current_item": "Anaconda (2025)",
  "detail": "Final language verification",
  "phases": {
    "anime": {"done": 241, "total": 241},
    "tv": {"done": 109, "total": 109},
    "movies": {"done": 12, "total": 53},
    "jellyfin": {"done": 0, "total": 1}
  }
}"""
        original_path = control_panel.PROGRESS_STATE_PATH
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
                handle.write(payload)
                temp_path = handle.name
            control_panel.PROGRESS_STATE_PATH = temp_path

            progress = control_panel.estimate_progress("", running=True)
        finally:
            control_panel.PROGRESS_STATE_PATH = original_path
            if "temp_path" in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)

        self.assertEqual(progress["phase"], "Movies")
        self.assertEqual(progress["processed"], 362)
        self.assertEqual(progress["total"], 404)
        self.assertEqual(progress["percent"], 90)
        self.assertIn("Anaconda (2025)", progress["detail"])

    def test_estimate_progress_ignores_stale_state_when_service_not_running(self) -> None:
        payload = """{
  "phase": "anime",
  "current_item": "Dr. STONE",
  "detail": "Season 04 evaluation",
  "phases": {
    "anime": {"done": 37, "total": 241},
    "tv": {"done": 0, "total": 109},
    "movies": {"done": 0, "total": 53},
    "jellyfin": {"done": 0, "total": 1}
  }
}"""
        original_path = control_panel.PROGRESS_STATE_PATH
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
                handle.write(payload)
                temp_path = handle.name
            control_panel.PROGRESS_STATE_PATH = temp_path

            progress = control_panel.estimate_progress("", running=False)
        finally:
            control_panel.PROGRESS_STATE_PATH = original_path
            if "temp_path" in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)

        self.assertEqual(progress["phase"], "Idle")
        self.assertEqual(progress["percent"], 0)


if __name__ == "__main__":
    unittest.main()