from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()