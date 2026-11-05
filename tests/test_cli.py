"""CLI and archive tests using synthetic data only."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from instagram_request_cleaner import cli


class CliTests(unittest.TestCase):
    def test_default_preview_never_starts_browser(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(cli, "read_export", return_value=(["example_user_1"], "digest")), \
             patch.object(cli, "start_verified_driver", side_effect=AssertionError("browser opened")), \
             patch.object(sys, "argv", [
                 "instagram-request-cleaner", "--archive", "synthetic.json",
                 "--state-dir", temp, "--dry-run",
             ]), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(), 0)
        self.assertIn("ÖNİZLEME", output.getvalue())

    def test_browser_mode_requires_expected_account(self):
        with patch.object(sys, "argv", [
                "instagram-request-cleaner", "--archive", "synthetic.json", "--apply"]), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                cli.main()
        self.assertEqual(stopped.exception.code, 2)

    def test_malformed_export_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            path.write_text(json.dumps({"not_the_expected_key": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                cli.read_export(path)


if __name__ == "__main__":
    unittest.main()
